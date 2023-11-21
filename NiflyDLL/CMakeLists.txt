cmake_minimum_required(VERSION 3.16...3.23)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED True)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_INTERPROCEDURAL_OPTIMIZATION OFF)
set(CMAKE_INSTALL_PREFIX "${CMAKE_SOURCE_DIR}/publish")
set(CMAKE_RUNTIME_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/bin")
set_property(GLOBAL PROPERTY USE_FOLDERS ON)

project(NiflyDLL LANGUAGES C CXX)

# --------------------------------------------------
# Add submodules
add_subdirectory(external/nifly)

# --------------------------------------------------
# Submodule Configuration
if(MSVC)
    set_property(TARGET nifly PROPERTY
        MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()

# --------------------------------------------------
# Create interface libs
add_library(nifly_dll_warnings INTERFACE)
if(MSVC)
    target_compile_options(nifly_dll_warnings INTERFACE /W3 /permissive-)
else()
    target_compile_options(nifly_dll_warnings INTERFACE -Wall -Wextra)
endif()

add_library(nifly_dll_definitions INTERFACE)
target_compile_definitions(nifly_dll_definitions INTERFACE NIFLYDLL_EXPORTS)
if(MSVC)
    target_compile_definitions(nifly_dll_definitions INTERFACE
        NOMINMAX)
endif()

add_library(nifly_dll_dependencies INTERFACE)
target_link_libraries(nifly_dll_dependencies INTERFACE
    nifly)

add_library(nifly_dll_pch INTERFACE)
target_precompile_headers(nifly_dll_pch INTERFACE
    <string>
    <string_view>
    <array>
    <vector>
    <deque>
    <map>
    <unordered_map>
    <set>
    <unordered_set>
    <fstream>
    <regex>
    <filesystem>
    <variant>
    <functional>
    <algorithm>
    <codecvt>
    <iomanip>
    <locale>
    <mutex>
    include/framework.hpp)


# --------------------------------------------------
# Create engine lib
set(nifly_dll_sources
	Anim.cpp
	dllmain.cpp
	Logger.cpp
	NiflyFunctions.cpp
	NiflyWrapper.cpp
	pch.cpp
#	TestDLL.cpp
)
set(nifly_dll_headers
	Logger.hpp
	NiflyDefs.hpp
	NiflyFunctions.hpp
	NiflyWrapper.hpp
)
set(nifly_dll_rc
	NiflyDLL.rc
)

add_library(nifly_dll SHARED ${nifly_dll_sources} ${nifly_dll_headers} ${nifly_dll_rc})
set_target_properties(nifly_dll PROPERTIES OUTPUT_NAME NiflyDLL)
target_link_libraries(nifly_dll PRIVATE
    nifly_dll_warnings
    nifly_dll_definitions
    nifly_dll_dependencies
    nifly_dll_pch)
# target_include_directories(nifly_dll PRIVATE "include")

if(MSVC)
    set_property(TARGET nifly_dll PROPERTY
        MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")
endif()

# --------------------------------------------------
# Merge files from source and include in the IDE
function(group_files sources)
    foreach(FILE ${sources})
        # Get the directory of the source file
        get_filename_component(PARENT_DIR "${FILE}" DIRECTORY)

        # Remove common directory prefix to make the group
        string(REPLACE "${CMAKE_CURRENT_SOURCE_DIR}" "" GROUP "${PARENT_DIR}")

        # Make sure we are using windows slashes
        string(REPLACE "/" "\\" GROUP "${GROUP}")

        # Strip the root parts for each possible component
        if("${FILE}" MATCHES "include/.*")
            string(SUBSTRING ${GROUP} 8 -1 GROUP)
        elseif("${FILE}" MATCHES "src/.*")
            string(SUBSTRING ${GROUP} 4 -1 GROUP)
        endif()

        # Do the grouping
        source_group("${GROUP}" FILES "${FILE}")
    endforeach()
endfunction()
group_files("${nifly_dll_sources};${nifly_dll_headers}")


# --------------------------------------------------
# Install app
install(TARGETS
    nifly_dll
    RUNTIME
    DESTINATION ${CMAKE_CURRENT_SOURCE_DIR}/../PyNifly)

