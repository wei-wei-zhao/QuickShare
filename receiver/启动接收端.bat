@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 手机传到电脑 - 接收端
echo 正在启动接收端，请稍候...
echo 启动后会自动打开「电脑端面板」（含二维码）
echo.
python app.py
echo.
echo 接收端已退出。
pause
