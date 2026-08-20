cmake_minimum_required(VERSION 3.16)

# Настройки Wi-Fi и адрес сервера держим в отдельном файле, а не в sdkconfig.
# sdkconfig приходится удалять при смене разметки флеша или набора моделей,
# и вместе с ним молча пропадали имя сети с паролем — колонка уходила искать
# заводскую «myssid» и не поднималась. sdkconfig.local в .gitignore, поэтому
# пароль от домашней сети в репозиторий не попадает.
set(SDKCONFIG_DEFAULTS "sdkconfig.defaults")
if(EXISTS "${CMAKE_CURRENT_LIST_DIR}/sdkconfig.local")
    list(APPEND SDKCONFIG_DEFAULTS "sdkconfig.local")
endif()

include($ENV{IDF_PATH}/tools/cmake/project.cmake)
project(happy_speaker)
