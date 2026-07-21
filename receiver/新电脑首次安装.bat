@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 首次安装依赖
echo ========================================
echo   局域网传文件 - 新电脑首次安装
echo ========================================
echo.
echo 正在检查 Python...
python --version
if errorlevel 1 (
    echo.
    echo [错误] 没有检测到 Python。
    echo 请先安装 Python 3：https://www.python.org/downloads/
    echo 安装时务必勾选：Add python.exe to PATH
    echo 装好后再重新双击本文件。
    echo.
    pause
    exit /b 1
)

echo.
echo 正在安装所需组件（flask / qrcode / winotify）...
python -m pip install -r requirements.txt -q
if errorlevel 1 (
    echo [错误] 安装失败，请检查网络后重试。
    pause
    exit /b 1
)

echo.
echo [成功] 安装完成！
echo 下一步：双击「启动接收端.bat」
echo 然后浏览器打开 http://127.0.0.1:8765/pc
echo.
pause
