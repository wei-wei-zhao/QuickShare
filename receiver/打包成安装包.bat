@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 打包 PhoneToPC 安装包（国内镜像）

echo ========================================
echo   使用国内 PyPI 镜像加速下载
echo   镜像: 清华 TUNA
echo ========================================
echo.

set PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
set PIP_TRUSTED_HOST=pypi.tuna.tsinghua.edu.cn

echo [1/4] 安装/更新打包依赖...
python -m pip install -i %PIP_INDEX_URL% --trusted-host %PIP_TRUSTED_HOST% -U pip
python -m pip install -i %PIP_INDEX_URL% --trusted-host %PIP_TRUSTED_HOST% -r requirements.txt
python -m pip install -i %PIP_INDEX_URL% --trusted-host %PIP_TRUSTED_HOST% -U pyinstaller pillow

echo.
echo [2/4] 清理旧的构建...
if exist "build" rmdir /s /q "build"
if exist "dist\PhoneToPC" rmdir /s /q "dist\PhoneToPC"
if exist "..\release" rmdir /s /q "..\release"

echo.
echo [3/4] PyInstaller 打包（稍等几分钟）...
python -m PyInstaller --noconfirm --clean PhoneToPC.spec
if errorlevel 1 (
    echo [失败] 打包出错
    pause
    exit /b 1
)

echo.
echo [4/4] 生成安装目录与安装脚本...
python "%~dp0make_installer.py"
if errorlevel 1 (
    echo [失败] 生成安装包出错
    pause
    exit /b 1
)

echo.
echo ========================================
echo   完成！
echo   安装包目录: D:\model\Python\phone-to-pc\release
echo ========================================
explorer "..\release"
pause
