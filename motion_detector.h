#pragma once

#include <atomic>
#include <cstdint>
#include <string>
#include <thread>
#include <mutex>
#include <chrono>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <vector>

#include "parsers/parser.h"
#include "wifi_drv_api/mt76_api.h"

class MotionDetector
{
public:
    static MotionDetector& getInstance();

    MotionDetector(const MotionDetector&) = delete;
    MotionDetector& operator=(const MotionDetector&) = delete;

    int startMonitoring(std::string ifname, unsigned interval);
    int stopMonitoring();

    int setAntennaIdx(unsigned idx);
    unsigned getAntennaIdx();
    double getMotion();
    bool getIsMonitoring();
    int startUdpServer(int port);
    int stopUdpServer();
    void addUdpClient(const std::string& clientIp, int clientPort);
    void removeUdpClient(const std::string& clientIp, int clientPort);

private:
    void runMonitoring();
    void udpServerListen();
    void sendCsiDataUdp(const CsiPacket& packet);

    static MotionDetector* instance;
    MotionDetector() : interval(0), antMonIdx(0), stopFlag(false),
                       motion_result(0.0), isMonitoring(false),
                       udpSocket(-1), udpServerRunning(false), sequence(0) {}

    std::string ifname;
    unsigned interval;
    unsigned antMonIdx;
    std::atomic<bool> stopFlag;
    std::chrono::time_point<std::chrono::steady_clock> startMon;
    MT76API wifi;

    double motion_result;
    std::atomic<bool> isMonitoring;
    std::thread monitorWorker;
    std::thread udpServerWorker;
    std::mutex dataMutex;
    std::mutex udpMutex;
    std::vector<std::pair<std::string, int>> udpClients;
    int udpSocket;
    std::atomic<bool> udpServerRunning;
    std::atomic<uint32_t> sequence;
};
