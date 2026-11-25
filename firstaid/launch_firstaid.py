"""Launch FirstAid-V3 using the image directory above the extracted tools."""

import argparse
import ctypes
from ctypes import wintypes
import os
import msvcrt
from pathlib import Path
import subprocess
import sys

from flash import FlashError, Transport
from lk_winusb import LkDriverTransport
import stock_restore


SCRIPT = Path(__file__).resolve()
TOOL_ROOT = SCRIPT.parents[2]
UAC_CHILD = "--firstaid-elevated"


def elevate(shell, args):
    execute = shell.ShellExecuteW
    execute.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                        wintypes.LPCWSTR, ctypes.c_int)
    execute.restype = ctypes.c_ssize_t
    parameters = subprocess.list2cmdline(["-B", "-u", "-X", "utf8", str(SCRIPT), UAC_CHILD, *args])
    result = execute(None, "runas", sys.executable, parameters, str(TOOL_ROOT), 1)
    if result <= 32:
        raise FlashError(f"未能取得管理员授权（Windows 返回 {result}）。")


def firmware_directory():
    root = TOOL_ROOT.parent
    if not ((root / "images").is_dir() and (root / "SHA256SUMS.txt").is_file()
            and (root / stock_restore.B_REFERENCE).is_file()):
        raise FlashError("找不到 FirstAid-V3 镜像。请把整个 CPDAY11-FirstAid-Tools 文件夹放到与 images 同级的位置。")
    return root


def page(title):
    if sys.stdout.isatty():
        os.system("cls")
    print(title, flush=True)


def keypress():
    return msvcrt.getwch() if sys.stdin.isatty() else input()


def restore(args, firmware):
    parser = argparse.ArgumentParser(description="COOL X Wide FirstAid 工具")
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--check", action="store_true", help="仅校验镜像")
    checks.add_argument("--probe", action="store_true", help="检查刷写通道和槽位，不安装驱动或写入分区")
    options = parser.parse_args(args)
    if options.check:
        stock_restore.verified_images(firmware, inactive=True)
        return 0
    tools = TOOL_ROOT / "tools"
    for name in ("adb.exe", "fastboot.exe"):
        if not (tools / "platform-tools" / name).is_file():
            raise FlashError(f"缺少包内工具 {name}，请重新解压工具包。")
    transport = Transport(firmware, tools) if options.probe else LkDriverTransport(firmware, tools)
    transport.reconnect_timeout = None
    if not options.probe:
        page("COOL X Wide FirstAid工具")
        print("要恢复设备，请将设备置于 fastboot/fastbootd 模式并连接。\n\n"
              "感谢@先看评测 提供的资源支持。如果您喜欢本项目，可以关注先看评测，您的点赞关注将鼓励我们做出更多更好的项目。\n\n"
              "此工具用于恢复您的COOL X Wide到出厂状态，要恢复设备，请按Y。"
              "按下其它按钮将退出此工具。", flush=True)
        if keypress().lower() != "y":
            return None
        page("确认恢复")
        print("您是否同意恢复您的COOL X Wide到出厂状态？您将丢失此设备上的所有数据。"
              "如果您同意，请输入Yes，如果您不同意，请关闭此工具。\n"
              "工具会清理 super 中两槽原有的系统布局，重建原厂分区表并刷入当前槽的另一槽。", flush=True)
        if input().strip() != "Yes":
            return None
        page("正在恢复，请保持设备连接")
        print("正在检查 LK WinUSB 驱动。", flush=True)
        transport.ensure_driver()

    images = stock_restore.verified_images(firmware, inactive=True)
    with transport.isolated_adb():
        stock_restore.install_inactive(transport, firmware, images, probe=options.probe)
    if not options.probe:
        page("恢复完成")
        print("设备已切换到恢复后的槽位并重启。请等待首次设置。", flush=True)
    return 0


def main(args):
    child = bool(args and args[0] == UAC_CHILD)
    if child:
        args = args[1:]
    pause = not args or child
    try:
        if any(option in args for option in ("--help", "-h")):
            return restore(args, None)
        firmware = firmware_directory()
        if "--check" not in args:
            shell = ctypes.WinDLL("shell32", use_last_error=True)
            if not shell.IsUserAnAdmin():
                if child:
                    raise FlashError("当前进程没有管理员权限。")
                elevate(shell, args)
                pause = False
                return 0
        result = restore(args, firmware)
        if result is None:
            pause = False
            return 0
        return result
    except (EOFError, KeyboardInterrupt):
        return 1
    except (FlashError, OSError, ValueError, subprocess.TimeoutExpired) as error:
        page("刷写错误，请联系作者，您的报错内容为：")
        print(error, file=sys.stderr, flush=True)
        return 1
    finally:
        if pause:
            try:
                print("按下任何按钮退出此工具", flush=True)
                keypress()
            except (EOFError, KeyboardInterrupt):
                pass


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
