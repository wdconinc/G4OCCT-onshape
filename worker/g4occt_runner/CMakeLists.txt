# SPDX-License-Identifier: LGPL-2.1-or-later
# Copyright (C) 2026 G4OCCT Contributors
#
# CMakeLists.txt for the g4occt_runner executable.
#
# Build (standalone, after G4OCCT is installed):
#   cmake -S . -B build \
#     -DCMAKE_BUILD_TYPE=Release \
#     -DG4OCCT_DIR=/path/to/G4OCCT/lib/cmake/G4OCCT
#   cmake --build build -- -j$(nproc)
#   cmake --install build --prefix /usr/local

cmake_minimum_required(VERSION 3.16 FATAL_ERROR)

project(
  g4occt_runner
  VERSION 0.1.0
  DESCRIPTION "G4OCCT simulation runner with CLI and JSON steering"
  LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# ─── External dependencies ────────────────────────────────────────────────────

find_package(G4OCCT REQUIRED)
# G4OCCT::G4OCCT pulls in Geant4 and OpenCASCADE transitively.

find_package(nlohmann_json 3.2.0 QUIET)
if(NOT nlohmann_json_FOUND)
  include(FetchContent)
  FetchContent_Declare(
    nlohmann_json
    URL https://github.com/nlohmann/json/releases/download/v3.12.0/json.tar.xz
    URL_HASH SHA256=42f6e95cad6ec532fd372391373363b62a14af6d771056dbfc86160e6dfff7aa)
  FetchContent_MakeAvailable(nlohmann_json)
endif()

# ─── Executable ──────────────────────────────────────────────────────────────

add_executable(g4occt_runner main.cc)

target_link_libraries(g4occt_runner PRIVATE G4OCCT::G4OCCT nlohmann_json::nlohmann_json)

# ─── Installation ─────────────────────────────────────────────────────────────

include(GNUInstallDirs)
install(TARGETS g4occt_runner RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
