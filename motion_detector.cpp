#include "motion_detector.h"

#include "parsers/parser_mt76.h"
#include "udp_protocol.h"

#include <algorithm>
#include <cerrno>
#include <chrono>
#include <cstring>
#include <iostream>
#include <limits>
#include <thread>
#include <utility>

#include <arpa/inet.h>
#include <sys/socket.h>
#include <unistd.h>

namespace
{
constexpr int CSI_PACKETS_PER_POLL = 96;
constexpr size_t MAX_UDP_CLIENTS = 16;
}

MotionDetector *MotionDetector::instance = nullptr;

MotionDetector &MotionDetector::getInstance()
{
    if (!instance)
        instance = new MotionDetector();
    return *instance;
}

void MotionDetector::runMonitoring()
{
    ParserMT76 parser;

    while (!stopFlag.load())
    {
        std::vector<csi_data *> *raw =
            wifi.motion_detection_dump(ifname.c_str(), CSI_PACKETS_PER_POLL);
        if (raw)
        {
            for (const CsiPacket &packet : parser.processRawData(raw))
            {
                if (udpServerRunning.load())
                    sendCsiDataUdp(packet);
            }
        }

        if (!stopFlag.load())
            std::this_thread::sleep_for(std::chrono::milliseconds(interval));
    }
}

void MotionDetector::udpServerListen()
{
    char buffer[128];
    while (udpServerRunning.load())
    {
        struct sockaddr_in client_addr = {};
        socklen_t client_addr_len = sizeof(client_addr);
        const ssize_t received =
            recvfrom(udpSocket, buffer, sizeof(buffer) - 1, 0,
                     reinterpret_cast<struct sockaddr *>(&client_addr),
                     &client_addr_len);
        if (received < 0)
        {
            if (!udpServerRunning.load() || errno == EAGAIN || errno == EWOULDBLOCK ||
                errno == EINTR)
                continue;
            std::cerr << "UDP registration receive failed: "
                      << std::strerror(errno) << std::endl;
            continue;
        }

        buffer[received] = '\0';
        if (std::strcmp(buffer, "register") != 0 &&
            std::strcmp(buffer, "register-v2") != 0)
            continue;

        char client_ip[INET_ADDRSTRLEN] = {};
        if (!inet_ntop(AF_INET, &client_addr.sin_addr, client_ip,
                       sizeof(client_ip)))
            continue;
        addUdpClient(client_ip, ntohs(client_addr.sin_port));
    }
}

int MotionDetector::startMonitoring(std::string interface_name,
                                    unsigned poll_interval_ms)
{
    if (interface_name.empty() || poll_interval_ms == 0)
        return -EINVAL;

    if (isMonitoring.load())
    {
        const int stop_ret = stopMonitoring();
        if (stop_ret < 0)
            return stop_ret;
    }

    ifname = std::move(interface_name);
    interval = poll_interval_ms;

    const int ret = wifi.motion_detection_start(ifname.c_str());
    if (ret < 0)
    {
        ifname.clear();
        interval = 0;
        return ret;
    }

    startMon = std::chrono::steady_clock::now();
    stopFlag.store(false);
    isMonitoring.store(true);
    monitorWorker = std::thread(&MotionDetector::runMonitoring, this);
    return 0;
}

int MotionDetector::stopMonitoring()
{
    if (!isMonitoring.load())
        return 0;

    stopFlag.store(true);
    if (monitorWorker.joinable())
        monitorWorker.join();

    const int ret = wifi.motion_detection_stop(ifname.c_str());
    // The worker is stopped even when the kernel reports a disable error. Keep
    // enough state for the caller to retry rather than pretending success.
    if (ret >= 0)
    {
        isMonitoring.store(false);
        ifname.clear();
        interval = 0;
    }
    return ret;
}

int MotionDetector::setAntennaIdx(unsigned index)
{
    if (index > std::numeric_limits<uint16_t>::max())
        return -ERANGE;

    std::lock_guard<std::mutex> lock(dataMutex);
    antMonIdx = index;
    return 0;
}

unsigned MotionDetector::getAntennaIdx()
{
    std::lock_guard<std::mutex> lock(dataMutex);
    return antMonIdx;
}

double MotionDetector::getMotion()
{
    std::lock_guard<std::mutex> lock(dataMutex);
    return motion_result;
}

bool MotionDetector::getIsMonitoring()
{
    return isMonitoring.load();
}

int MotionDetector::startUdpServer(int port)
{
    if (port <= 0 || port > 65535)
        return -EINVAL;
    if (udpServerRunning.load())
        return -EALREADY;

    udpSocket = socket(AF_INET, SOCK_DGRAM, 0);
    if (udpSocket < 0)
        return -errno;

    int enabled = 1;
    if (setsockopt(udpSocket, SOL_SOCKET, SO_REUSEADDR, &enabled,
                   sizeof(enabled)) < 0)
    {
        const int ret = -errno;
        close(udpSocket);
        udpSocket = -1;
        return ret;
    }

    struct timeval timeout = {};
    timeout.tv_sec = 1;
    if (setsockopt(udpSocket, SOL_SOCKET, SO_RCVTIMEO, &timeout,
                   sizeof(timeout)) < 0)
    {
        const int ret = -errno;
        close(udpSocket);
        udpSocket = -1;
        return ret;
    }

    struct sockaddr_in server_addr = {};
    server_addr.sin_family = AF_INET;
    server_addr.sin_addr.s_addr = htonl(INADDR_ANY);
    server_addr.sin_port = htons(static_cast<uint16_t>(port));
    if (bind(udpSocket, reinterpret_cast<struct sockaddr *>(&server_addr),
             sizeof(server_addr)) < 0)
    {
        const int ret = -errno;
        close(udpSocket);
        udpSocket = -1;
        return ret;
    }

    sequence.store(0);
    udpServerRunning.store(true);
    udpServerWorker = std::thread(&MotionDetector::udpServerListen, this);
    std::cout << "CSI UDP v2 server listening on port " << port << std::endl;
    return 0;
}

int MotionDetector::stopUdpServer()
{
    if (!udpServerRunning.exchange(false))
        return 0;

    if (udpSocket >= 0)
        shutdown(udpSocket, SHUT_RDWR);
    if (udpServerWorker.joinable())
        udpServerWorker.join();
    if (udpSocket >= 0)
    {
        close(udpSocket);
        udpSocket = -1;
    }

    std::lock_guard<std::mutex> lock(udpMutex);
    udpClients.clear();
    return 0;
}

void MotionDetector::addUdpClient(const std::string &client_ip, int client_port)
{
    std::lock_guard<std::mutex> lock(udpMutex);
    const auto client = std::make_pair(client_ip, client_port);
    if (std::find(udpClients.begin(), udpClients.end(), client) !=
        udpClients.end())
        return;
    if (udpClients.size() >= MAX_UDP_CLIENTS)
    {
        std::cerr << "Rejected UDP client " << client_ip << ':' << client_port
                  << ": registration limit reached" << std::endl;
        return;
    }

    udpClients.push_back(client);
    std::cout << "Registered UDP client " << client_ip << ':' << client_port
              << std::endl;
}

void MotionDetector::removeUdpClient(const std::string &client_ip,
                                     int client_port)
{
    std::lock_guard<std::mutex> lock(udpMutex);
    const auto client = std::make_pair(client_ip, client_port);
    const auto found = std::find(udpClients.begin(), udpClients.end(), client);
    if (found != udpClients.end())
        udpClients.erase(found);
}

void MotionDetector::sendCsiDataUdp(const CsiPacket &packet)
{
    if (!udpServerRunning.load() || udpSocket < 0)
        return;

    std::vector<std::pair<std::string, int>> clients;
    {
        std::lock_guard<std::mutex> lock(udpMutex);
        clients = udpClients;
    }
    if (clients.empty())
        return;

    const std::vector<uint8_t> datagram =
        csi_udp::encode_v2(packet, sequence.fetch_add(1));
    if (datagram.empty())
    {
        std::cerr << "CSI packet cannot be represented by UDP v2" << std::endl;
        return;
    }

    for (const auto &client : clients)
    {
        struct sockaddr_in client_addr = {};
        client_addr.sin_family = AF_INET;
        client_addr.sin_port = htons(static_cast<uint16_t>(client.second));
        if (inet_pton(AF_INET, client.first.c_str(), &client_addr.sin_addr) != 1)
            continue;

        if (sendto(udpSocket, datagram.data(), datagram.size(), 0,
                   reinterpret_cast<struct sockaddr *>(&client_addr),
                   sizeof(client_addr)) < 0 &&
            udpServerRunning.load())
        {
            std::cerr << "CSI UDP send to " << client.first << ':'
                      << client.second << " failed: " << std::strerror(errno)
                      << std::endl;
        }
    }
}
