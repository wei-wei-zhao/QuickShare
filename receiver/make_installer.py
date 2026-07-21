# -*- coding: utf-8 -*-
"""
把 PyInstaller 产物整理成可分发的 Windows「安装包」目录：
- release/PhoneToPC-Setup/   绿色安装目录 + 安装.bat / 卸载.bat
- release/PhoneToPC-便携版/  解压即用
"""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST_APP = ROOT / "dist" / "PhoneToPC"
RELEASE = ROOT.parent / "release"
SETUP_DIR = RELEASE / "PhoneToPC-安装包"
PORTABLE_DIR = RELEASE / "PhoneToPC-便携版"


INSTALL_BAT = r"""@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 安装 PhoneToPC 扫码传文件

echo ========================================
echo   PhoneToPC 扫码传文件 - 安装向导
echo ========================================
echo.
echo 将弹出窗口，请选择安装位置所在的文件夹。
echo 程序会安装到：你选的文件夹\PhoneToPC
echo （可点「新建文件夹」，也可换到 D盘 / E盘）
echo.

set "TARGET="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0select_install_dir.ps1"`) do set "TARGET=%%I"

if "%TARGET%"=="" (
    echo 已取消安装。
    pause
    exit /b 1
)

echo.
echo 安装位置：%TARGET%
echo.
set /p CONFIRM=确认安装到这里？(Y/N) 
if /i not "%CONFIRM%"=="Y" (
    echo 已取消。
    pause
    exit /b 1
)

if exist "%TARGET%" (
    echo 目标文件夹已存在，正在覆盖...
    rmdir /s /q "%TARGET%"
)

mkdir "%TARGET%" 2>nul
xcopy /E /I /Y "App\*" "%TARGET%\" >nul
if errorlevel 1 (
    echo [失败] 复制文件失败，请换一个有写入权限的目录再试。
    pause
    exit /b 1
)

> "%TARGET%\install_path.txt" echo %TARGET%
mkdir "%LOCALAPPDATA%\PhoneToPC" 2>nul
> "%LOCALAPPDATA%\PhoneToPC\install_path.txt" echo %TARGET%

copy /Y "卸载.bat" "%TARGET%\卸载.bat" >nul

set "SM=%APPDATA%\Microsoft\Windows\Start Menu\Programs\PhoneToPC"
mkdir "%SM%" 2>nul
powershell -NoProfile -Command ^
  "$ws=New-Object -ComObject WScript.Shell; $s=$ws.CreateShortcut('%SM%\PhoneToPC 扫码传文件.lnk'); $s.TargetPath='%TARGET%\PhoneToPC.exe'; $s.WorkingDirectory='%TARGET%'; $s.Description='局域网扫码传文件'; $s.Save(); $u=$ws.CreateShortcut('%SM%\卸载 PhoneToPC.lnk'); $u.TargetPath='%TARGET%\卸载.bat'; $u.WorkingDirectory='%TARGET%'; $u.Save()"

powershell -NoProfile -Command ^
  "$ws=New-Object -ComObject WScript.Shell; $desk=[Environment]::GetFolderPath('Desktop'); $s=$ws.CreateShortcut(($desk + '\PhoneToPC 扫码传文件.lnk')); $s.TargetPath='%TARGET%\PhoneToPC.exe'; $s.WorkingDirectory='%TARGET%'; $s.Description='局域网扫码传文件'; $s.Save()"

echo.
echo [成功] 安装完成！
echo 安装目录: %TARGET%
echo 已创建桌面和开始菜单快捷方式。
echo.
pause
"""

SELECT_DIR_PS1 = r"""# 选择安装目录（弹出文件夹窗口）
Add-Type -AssemblyName System.Windows.Forms
$dialog = New-Object System.Windows.Forms.FolderBrowserDialog
$dialog.Description = "请选择 PhoneToPC 的安装位置（程序会装到该目录下的 PhoneToPC 文件夹）"
$dialog.ShowNewFolderButton = $true

$defaultParent = Join-Path $env:LOCALAPPDATA "Programs"
if (-not (Test-Path $defaultParent)) {
    New-Item -ItemType Directory -Force -Path $defaultParent | Out-Null
}
$dialog.SelectedPath = $defaultParent

$result = $dialog.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK -and $dialog.SelectedPath) {
    $target = Join-Path $dialog.SelectedPath "PhoneToPC"
    # 若用户已经选中了名为 PhoneToPC 的文件夹，就直接用，避免套两层
    if ((Split-Path $dialog.SelectedPath -Leaf) -ieq "PhoneToPC") {
        $target = $dialog.SelectedPath
    }
    Write-Output $target
}
"""

UNINSTALL_BAT = r"""@echo off
chcp 65001 >nul
title 卸载 PhoneToPC

:: 优先：卸载脚本所在目录就是安装目录
set "TARGET=%~dp0"
if "%TARGET:~-1%"=="\" set "TARGET=%TARGET:~0,-1%"

:: 若从别处调用，尝试读记录的安装路径
if not exist "%TARGET%\PhoneToPC.exe" (
  if exist "%LOCALAPPDATA%\PhoneToPC\install_path.txt" (
    set /p TARGET=<"%LOCALAPPDATA%\PhoneToPC\install_path.txt"
  )
)

set "SM=%APPDATA%\Microsoft\Windows\Start Menu\Programs\PhoneToPC"

echo 即将卸载 PhoneToPC
echo 安装目录: %TARGET%
echo.
pause

taskkill /IM PhoneToPC.exe /F >nul 2>&1

if exist "%TARGET%" (
  :: 先删快捷方式再删目录，避免删到正在运行的卸载脚本所在盘时出问题
  cd /d "%TEMP%"
  rmdir /s /q "%TARGET%"
)
if exist "%SM%" rmdir /s /q "%SM%"
if exist "%LOCALAPPDATA%\PhoneToPC" rmdir /s /q "%LOCALAPPDATA%\PhoneToPC"

powershell -NoProfile -Command ^
  "$desk=[Environment]::GetFolderPath('Desktop'); $p=Join-Path $desk 'PhoneToPC 扫码传文件.lnk'; if (Test-Path $p) { Remove-Item $p -Force }"

echo.
echo 卸载完成。
pause
"""

README = """PhoneToPC 扫码传文件 - Windows 安装说明
====================================

【推荐】安装版
1. 打开「PhoneToPC-安装包」文件夹
2. 双击「安装.bat」
3. 在弹出窗口中选择安装位置（可改盘符/文件夹）
4. 输入 Y 确认安装
5. 桌面会出现「PhoneToPC 扫码传文件」
6. 双击启动后，浏览器打开电脑面板，手机扫码即可传图

【便携版】不解压安装
直接打开「PhoneToPC-便携版」里的 PhoneToPC.exe

注意：
- 安装时可自己选目录，例如 D:\\Tools\\PhoneToPC
- 手机和电脑需同一 WiFi（局域网）
- 首次若手机打不开，可在安装目录运行「放行防火墙.bat」（管理员）
- 本安装包已内置依赖，无需再装 Python / pip
- 「选择保存位置」是接收文件存放目录，在软件面板里设置，与安装目录不同
"""

FIREWALL_BAT = r"""@echo off
chcp 65001 >nul
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo 正在请求管理员权限...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo 正在放行 PhoneToPC 端口 8765 ...
netsh advfirewall firewall delete rule name="PhoneToPC-Receiver-8765" >nul 2>&1
netsh advfirewall firewall add rule name="PhoneToPC-Receiver-8765" dir=in action=allow protocol=TCP localport=8765 profile=any description="PhoneToPC LAN upload"

echo.
if %errorLevel% equ 0 (
    echo [成功] 防火墙已放行 8765
) else (
    echo [失败] 请手动在防火墙中允许 PhoneToPC.exe
)
pause
"""


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def zip_dir(src: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in src.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(src.parent))


def main() -> None:
    if not DIST_APP.is_dir():
        raise SystemExit(f"找不到打包结果: {DIST_APP}，请先运行 PyInstaller")

    RELEASE.mkdir(parents=True, exist_ok=True)

    # --- 安装包目录 ---
    if SETUP_DIR.exists():
        shutil.rmtree(SETUP_DIR)
    app_dst = SETUP_DIR / "App"
    copy_tree(DIST_APP, app_dst)
    (SETUP_DIR / "安装.bat").write_text(INSTALL_BAT, encoding="utf-8-sig")
    (SETUP_DIR / "select_install_dir.ps1").write_text(SELECT_DIR_PS1, encoding="utf-8-sig")
    (SETUP_DIR / "卸载.bat").write_text(UNINSTALL_BAT, encoding="utf-8-sig")
    (app_dst / "放行防火墙.bat").write_text(FIREWALL_BAT, encoding="utf-8-sig")
    (SETUP_DIR / "使用说明.txt").write_text(README, encoding="utf-8-sig")

    # --- 便携版 ---
    if PORTABLE_DIR.exists():
        shutil.rmtree(PORTABLE_DIR)
    copy_tree(DIST_APP, PORTABLE_DIR)
    (PORTABLE_DIR / "放行防火墙.bat").write_text(FIREWALL_BAT, encoding="utf-8-sig")
    (PORTABLE_DIR / "使用说明.txt").write_text(README, encoding="utf-8-sig")

    # --- 打成 zip 方便拷贝 ---
    zip_setup = RELEASE / "PhoneToPC-安装包.zip"
    zip_portable = RELEASE / "PhoneToPC-便携版.zip"
    zip_dir(SETUP_DIR, zip_setup)
    zip_dir(PORTABLE_DIR, zip_portable)

    print("已生成:")
    print(" ", SETUP_DIR)
    print(" ", PORTABLE_DIR)
    print(" ", zip_setup)
    print(" ", zip_portable)


if __name__ == "__main__":
    main()
