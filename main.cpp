#include "motion_detector.h"

#include <cerrno>
#include <chrono>
#include <csignal>
#include <cstring>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <string>
#include <thread>

namespace
{
volatile std::sig_atomic_t stop_requested = 0;

void signal_handler(int)
{
    stop_requested = 1;
}

bool parse_unsigned(const char *text, unsigned long maximum,
                    unsigned long &result)
{
    if (!text || !*text || *text == '-')
        return false;
    errno = 0;
    char *end = nullptr;
    const unsigned long value = std::strtoul(text, &end, 10);
    if (errno || !end || *end || value == 0 || value > maximum)
        return false;
    result = value;
    return true;
}
} // namespace

int main(int argc, char **argv)
{
    if (argc != 4)
    {
        std::cerr << "Usage: " << argv[0]
                  << " <wifi-interface> <poll-ms> <udp-port>\n";
        return EXIT_FAILURE;
    }

    unsigned long interval = 0;
    unsigned long port = 0;
    if (!parse_unsigned(argv[2], std::numeric_limits<unsigned>::max(), interval) ||
        !parse_unsigned(argv[3], 65535, port))
    {
        std::cerr << "poll-ms and udp-port must be positive integers; port must "
                     "be <= 65535\n";
        return EXIT_FAILURE;
    }

    struct sigaction action = {};
    action.sa_handler = signal_handler;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGINT, &action, nullptr) ||
        sigaction(SIGTERM, &action, nullptr))
    {
        std::cerr << "Failed to install signal handlers: "
                  << std::strerror(errno) << '\n';
        return EXIT_FAILURE;
    }

    MotionDetector &detector = MotionDetector::getInstance();
    int ret = detector.startUdpServer(static_cast<int>(port));
    if (ret < 0)
    {
        std::cerr << "Failed to start UDP server: " << std::strerror(-ret)
                  << '\n';
        return EXIT_FAILURE;
    }

    ret = detector.startMonitoring(argv[1], static_cast<unsigned>(interval));
    if (ret < 0)
    {
        std::cerr << "Failed to start CSI capture: " << std::strerror(-ret)
                  << '\n';
        detector.stopUdpServer();
        return EXIT_FAILURE;
    }

    while (!stop_requested)
        std::this_thread::sleep_for(std::chrono::milliseconds(200));

    const int capture_ret = detector.stopMonitoring();
    detector.stopUdpServer();
    if (capture_ret < 0)
    {
        std::cerr << "CSI capture stopped locally, but driver disable failed: "
                  << std::strerror(-capture_ret) << '\n';
        return EXIT_FAILURE;
    }
    return EXIT_SUCCESS;
}
