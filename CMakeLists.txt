cmake_minimum_required(VERSION 3.16)
project(MtkCSIdump VERSION 2.0.0 LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

option(CSI_BUILD_CAPTURE "Build the OpenWrt nl80211 capture executable" ON)
option(CSI_STATIC_LINK "Request a fully static CSIdump executable" OFF)

include(CTest)
find_package(Threads REQUIRED)

add_library(csi_core STATIC
    parsers/parser_mt76.cpp
    udp_protocol.cpp
    wifi_drv_api/csi_types.cpp
)
target_include_directories(csi_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR})
target_compile_options(csi_core PRIVATE -Wall -Wextra -Wpedantic)

if(CSI_BUILD_CAPTURE)
    find_path(LIBNL_TINY_INCLUDE_DIR NAMES unl.h PATH_SUFFIXES libnl-tiny)
    find_library(LIBNL_TINY_LIBRARY NAMES nl-tiny)
    if(NOT LIBNL_TINY_INCLUDE_DIR OR NOT LIBNL_TINY_LIBRARY)
        message(FATAL_ERROR
            "libnl-tiny was not found. Set LIBNL_TINY_INCLUDE_DIR and "
            "LIBNL_TINY_LIBRARY, or configure hardware-free tests with "
            "-DCSI_BUILD_CAPTURE=OFF.")
    endif()

    add_executable(CSIdump
        main.cpp
        md.cpp
        motion_detector.cpp
        wifi_drv_api/mt76_api.cpp
    )
    target_include_directories(CSIdump PRIVATE ${LIBNL_TINY_INCLUDE_DIR})
    target_compile_options(CSIdump PRIVATE -Wall -Wextra -Wpedantic)
    target_link_libraries(CSIdump PRIVATE
        csi_core
        Threads::Threads
        ${LIBNL_TINY_LIBRARY}
    )
    install(TARGETS CSIdump RUNTIME DESTINATION sbin)

    if(CSI_STATIC_LINK)
        target_link_options(CSIdump PRIVATE -static)
        target_link_libraries(CSIdump PRIVATE -static-libgcc -static-libstdc++)
    endif()
endif()

if(BUILD_TESTING)
    add_executable(csi_capture_tests tests/test_capture.cpp)
    target_link_libraries(csi_capture_tests PRIVATE csi_core)
    target_compile_options(csi_capture_tests PRIVATE -Wall -Wextra -Wpedantic)
    add_test(NAME csi_capture_tests COMMAND csi_capture_tests)

    find_package(Python3 COMPONENTS Interpreter)
    if(Python3_Interpreter_FOUND)
        add_test(
            NAME csi_protocol_python_tests
            COMMAND ${Python3_EXECUTABLE} -m unittest -v tests.test_csi_protocol
        )
        set_tests_properties(csi_protocol_python_tests PROPERTIES
            WORKING_DIRECTORY ${CMAKE_CURRENT_SOURCE_DIR}
        )
    endif()
endif()
