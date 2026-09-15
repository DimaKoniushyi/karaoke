cmake_minimum_required(VERSION 3.24)
project(KaraokeAsioBridge LANGUAGES CXX)
enable_testing()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

set(ASIO_SDK_DIR "${CMAKE_CURRENT_SOURCE_DIR}/../../../downloads/engines/asio-sdk"
    CACHE PATH "Path to the Steinberg ASIO SDK")

if(NOT EXISTS "${ASIO_SDK_DIR}/common/asio.cpp")
  message(FATAL_ERROR "ASIO SDK was not found at ${ASIO_SDK_DIR}")
endif()

add_executable(KaraokeAsioBridge
  bridge_main.cpp
  ${ASIO_SDK_DIR}/common/asio.cpp
  ${ASIO_SDK_DIR}/host/asiodrivers.cpp
  ${ASIO_SDK_DIR}/host/pc/asiolist.cpp
)

target_include_directories(KaraokeAsioBridge PRIVATE
  ${ASIO_SDK_DIR}/common
  ${ASIO_SDK_DIR}/host
  ${ASIO_SDK_DIR}/host/pc
)
target_compile_definitions(KaraokeAsioBridge PRIVATE _CRT_SECURE_NO_WARNINGS NOMINMAX)
target_link_libraries(KaraokeAsioBridge PRIVATE ole32)

# Independent shared-mode I/O library; the existing ASIO target is unchanged.
add_library(KaraokeWasapi SHARED ../wasapi/monitor.cpp)
target_link_libraries(KaraokeWasapi PRIVATE ole32 avrt ksuser)
add_executable(WasapiBufferTests ../wasapi/monitor_buffer_test.cpp)
add_executable(VirtualAudioBridgeTests ../virtual_audio/bridge_ring_test.cpp)
add_test(NAME wasapi-monitor-buffer COMMAND WasapiBufferTests)
add_test(NAME virtual-audio-bridge COMMAND VirtualAudioBridgeTests)
