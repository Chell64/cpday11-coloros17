"""Detect and bind the bundled MediaTek WinUSB driver in LK fastboot."""

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import time

from flash import FlashError, Transport


def bind_driver(inf):
    update = ctypes.WinDLL("newdev", use_last_error=True).UpdateDriverForPlugAndPlayDevicesW
    update.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                       ctypes.POINTER(wintypes.BOOL))
    update.restype = wintypes.BOOL
    restart = wintypes.BOOL()
    if not update(None, r"USB\VID_0E8D&PID_201C", str(inf), 0, ctypes.byref(restart)):
        error = ctypes.get_last_error()
        if error != 259:  # Windows selected an equal or better-ranked driver.
            raise FlashError(f"Windows 未能绑定 LK 驱动（错误 0x{error:08x}）。")
    if restart.value:
        raise FlashError("驱动已加入 Windows。请重启电脑后重新运行工具。")


def install_driver(tool_root):
    inf = tool_root / "drivers" / "mediatek-lk" / "android_winusb.inf"
    if not inf.is_file():
        raise FlashError("找不到包内 LK 驱动。请重新解压工具包。")
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    detect = r"""
$devices = @(Get-PnpDevice -PresentOnly | Where-Object { $_.InstanceId -like 'USB\VID_0E8D&PID_201C\*' })
$lk = @($devices | Where-Object {
    $compatible = (Get-PnpDeviceProperty -InstanceId $_.InstanceId -KeyName DEVPKEY_Device_CompatibleIds -ErrorAction SilentlyContinue).Data
    @($compatible) -match '^USB\(COMPAT_VID_0E8D&)?Class_FF&SubClass_42&Prot_03$'
})
if ($devices.Count -eq 0 -or $lk.Count -eq 0) { exit 10 }
if ($devices.Count -ne 1 -or $lk.Count -ne 1) { exit 11 }
if ($lk[0].Status -eq 'OK' -and
    (Get-PnpDeviceProperty -InstanceId $lk[0].InstanceId -KeyName DEVPKEY_Device_Service -ErrorAction SilentlyContinue).Data -eq 'WinUSB') { exit 12 }
exit 0
"""
    powershell = system32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    detect_command = [powershell, "-NoProfile", "-NonInteractive", "-Command", detect]
    detected = subprocess.run(detect_command, check=False, timeout=15).returncode
    if detected in (10, 12):
        return
    if detected:
        raise FlashError("没有识别到唯一的 LK fastboot 接口（0E8D:201C / FF:42:03）。请在手机上进入引导器 fastboot 后重试。")
    print("正在安装包内签名的 MediaTek LK WinUSB 驱动。", flush=True)
    bind_driver(inf)
    print("正在刷新 USB 设备列表。请保持手机连接。", flush=True)
    pnputil = system32 / "pnputil.exe"
    status = subprocess.run([pnputil, "/scan-devices"], check=False, timeout=60).returncode
    if status:
        raise FlashError(f"Windows 刷新 USB 设备列表失败（PnPUtil 返回 {status}）。")
    for _ in range(5):
        detected = subprocess.run(detect_command, check=False, timeout=15).returncode
        if detected == 12:
            print("已确认 LK 接口绑定 WinUSB。", flush=True)
            return
        if detected not in (0, 10):
            break
        time.sleep(1)
    raise FlashError("已安装驱动并刷新 USB 列表，但 LK 仍未绑定 WinUSB。请重新插拔手机后再次运行 Flash.cmd。")


class LkDriverTransport(Transport):
    next_driver_check = 0

    def ensure_driver(self):
        if time.monotonic() >= self.next_driver_check:
            install_driver(self.tools.parents[1])
            self.next_driver_check = time.monotonic() + 5

    def lk_interfaces(self):
        self.ensure_driver()
        yield from super().lk_interfaces()
