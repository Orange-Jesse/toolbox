@echo off
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
where py >nul 2>nul && set "PY=py -3"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo [x] 没有找到 Python，请先安装 Python 3.8 或更高版本。
    echo     下载：https://www.python.org/downloads/
    echo     安装时记得勾选 "Add Python to PATH"。
    pause
    exit /b 1
)

%PY% -c "import tkinter" >nul 2>nul
if errorlevel 1 (
    echo [x] 这个 Python 没有自带 tkinter，请改用 python.org 的官方安装包。
    pause
    exit /b 1
)

%PY% -c "import PIL, webview, watchdog" >nul 2>nul
if errorlevel 1 (
    echo [!] 缺少依赖，正在安装 Pillow / pywebview / watchdog ...
    %PY% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [x] 依赖安装失败，请手动执行：%PY% -m pip install -r requirements.txt
        pause
        exit /b 1
    )
)

%PY% personal_page_builder.py
if errorlevel 1 pause
