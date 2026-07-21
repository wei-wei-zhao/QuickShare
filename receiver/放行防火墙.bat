@echo off
chcp 65001 >nul
:: 需要管理员权限：放行 8765 端口，让同一 WiFi 下的手机能访问接收端
net session >nul 2>&1
if %errorLevel% neq 0 (
    echo 正在请求管理员权限...
    powershell -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

echo 正在添加防火墙规则 PhoneToPC-Receiver-8765 ...
netsh advfirewall firewall delete rule name="PhoneToPC-Receiver-8765" >nul 2>&1
netsh advfirewall firewall add rule name="PhoneToPC-Receiver-8765" dir=in action=allow protocol=TCP localport=8765 profile=any description="Allow phone to send files to PC"

if %errorLevel% equ 0 (
    echo.
    echo [成功] 防火墙已放行 8765 端口
) else (
    echo.
    echo [失败] 请手动在「Windows 安全中心 - 防火墙」里允许 Python
)

echo.
pause
