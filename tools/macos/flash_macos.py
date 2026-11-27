"""macOS launcher for the CPDAY11 ColorOS 17.0.1.200 SP2 Windows release."""

import argparse
from contextlib import contextmanager
from ctypes.util import find_library
import hashlib
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import BadZipFile, ZipFile


TOOL_DIR = Path(__file__).resolve().parent
WINDOWS_ZIP = "CPDAY11-ColorOS17-17.0.1.200-SP2-Flash-Tools-Windows-x64.zip"
WINDOWS_ZIP_SHA256 = "9f763023f7659503d9fefcfd9ce56d750bb30b998b781a430ed0653a8f53b06a"
FIRMWARE_MANIFEST_SHA256 = "e981be38319e5f1a217a418eb2d40cd92b8c539984575430e0441d65a9b06d48"
WINDOWS_TOOLS = "CPDAY11-Flash-Tools/tools/"
REQUIRED_FILES = {"python/flash.py", "python/usb/__init__.py", "python/usb/backend/libusb1.py",
                  "super_empty.img", "update-compatibility.json"}


def verify(stream, expected, label):
    if hashlib.file_digest(stream, "sha256").hexdigest() != expected:
        raise ValueError(f"{label} 的 SHA-256 与 SP2 发布包不符。")


def stage_upstream(archive, directory):
    with archive.open("rb") as source:
        verify(source, WINDOWS_ZIP_SHA256, archive.name)
        source.seek(0)
        with ZipFile(source) as bundle:
            files = set()
            for entry in bundle.namelist():
                if not entry.startswith(WINDOWS_TOOLS):
                    continue
                name = entry[len(WINDOWS_TOOLS):]
                if name in REQUIRED_FILES or re.fullmatch(r"python/usb/(?:\w+/)*\w+\.py", name):
                    bundle.extract(entry, directory)
                    files.add(name)
            if not REQUIRED_FILES <= files:
                raise ValueError("Windows 工具包缺少 SP2 刷写文件。")
    return directory / WINDOWS_TOOLS


def native_platform_tools(directory):
    programs = {}
    for name in ("adb", "fastboot"):
        path = shutil.which(str(directory / name) if directory else name)
        if path is None:
            raise ValueError(f"找不到 {name}。请安装 Android Platform-Tools 或指定 --platform-tools。")
        programs[name] = str(Path(path).resolve())
    return programs


def native_libusb(explicit):
    if explicit is not None:
        if explicit.is_file():
            return str(explicit.resolve())
        raise ValueError(f"找不到指定的 libusb 动态库 {explicit}")
    prefix = "/opt/homebrew" if platform.machine() == "arm64" else "/usr/local"
    homebrew = Path(prefix) / "opt/libusb/lib/libusb-1.0.dylib"
    return str(homebrew) if homebrew.is_file() else find_library("usb-1.0")


def make_transport(upstream, firmware, programs, library, serial, expected_active):
    import usb.backend.libusb1

    class MacTransport(upstream.Transport):
        # Match the SP2 Windows launcher, which accepts the documented LK version prefix.
        require_model = False

        def __init__(self):
            self.root = firmware
            self.serial = serial
            self.adb_socket = None
            self.adb_used = False
            self.backend = usb.backend.libusb1.get_backend(find_library=lambda _: library)
            if self.backend is None:
                raise upstream.FlashError("无法加载 macOS libusb。请安装 libusb 或指定 --libusb。")

        def run(self, program, *args, timeout=10):
            if program == "fastboot" and args[-2:] == ("getvar", "is-userspace"):
                if any(self.lk_interfaces()):
                    raise upstream.FlashError("LK 模式使用 libusb 通道。")
            command = [programs[program]]
            if program == "adb" and self.adb_socket:
                self.adb_used = True
                command.extend(["-L", self.adb_socket])
            result = subprocess.run(command + list(args), cwd=self.root, timeout=timeout,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    encoding="utf-8", errors="replace")
            if result.returncode:
                raise upstream.FlashError(f"{program} {' '.join(args)}\n{result.stdout.strip()}")
            return result.stdout

        def native_serials(self):
            serials = super().native_serials()
            if any(self.lk_interfaces()):
                # macOS fastboot can list LK. Opening its endpoints may detach the device.
                if len(serials) > 1:
                    raise upstream.FlashError("检测到多台 fastboot 设备。请只连接目标手机。")
                if serials:
                    self.check_serial(next(iter(serials)))
                return set()
            return serials

        @contextmanager
        def connect(self, mode=None, *, recovery=False):
            with super().connect(mode, recovery=recovery) as (actual, connection):
                product = "k6789v1_64" if actual == "LK" else "L950U"
                if connection.get("product") != product:
                    raise upstream.FlashError(f"{actual} 设备型号与 CPDAY11 不符。")
                if expected_active and connection.get("current-slot") != expected_active:
                    raise upstream.FlashError("实际槽位与指定的当前槽位不符。")
                yield actual, connection

    return MacTransport()


def interactive_mode():
    print("CPDAY11 ColorOS 17.0.1.200 SP2 · macOS\n1. 清除数据安装\n2. 设备预检\n3. 离线校验")
    return {"1": "clean-install", "2": "probe", "3": "check"}.get(input("请选择，其他输入退出：").strip())


def main(argv=None):
    parser = argparse.ArgumentParser(description="酷派 COOL X Wide ColorOS SP2 macOS 刷写工具")
    parser.add_argument("--firmware", type=Path, default=TOOL_DIR.parent, help="解包后的 SP2 固件目录")
    parser.add_argument("--windows-tools", type=Path, help="对应 SP2 的 Windows 工具 ZIP")
    parser.add_argument("--platform-tools", type=Path, help="macOS Android Platform-Tools 目录")
    parser.add_argument("--libusb", type=Path, help="macOS libusb-1.0.dylib 文件")
    parser.add_argument("--serial", help="只允许操作指定 fastboot 设备")
    parser.add_argument("--expected-active", choices=("a", "b"), help="要求设备处于指定的当前槽")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--check", action="store_const", dest="mode", const="check", help="离线校验")
    modes.add_argument("--probe", action="store_const", dest="mode", const="probe", help="设备预检")
    modes.add_argument("--clean-install", action="store_const", dest="mode", const="clean-install",
                       help="清除数据安装，须在终端确认")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        parser.error("需要 Python 3.11 或更高版本。")
    if args.serial and not re.fullmatch(r"[A-Za-z0-9]+", args.serial):
        parser.error("设备序列号格式不正确。")

    try:
        if not args.mode and not sys.stdin.isatty():
            parser.error("请指定 --check 或 --probe。清除数据安装请在终端中进行。")
        mode = args.mode or interactive_mode()
        if mode is None:
            return 0
        if mode == "clean-install" and not sys.stdin.isatty():
            parser.error("清除数据安装需要交互式终端。")
        if mode != "check" and sys.platform != "darwin":
            parser.error("设备预检和刷写需要 macOS。")

        firmware = args.firmware.resolve()
        archive = args.windows_tools or firmware / WINDOWS_ZIP
        with (firmware / "SHA256SUMS.txt").open("rb") as manifest:
            verify(manifest, FIRMWARE_MANIFEST_SHA256, "固件 SHA256SUMS.txt")
        programs = native_platform_tools(args.platform_tools) if mode != "check" else {}
        library = native_libusb(args.libusb) if mode != "check" else None
        if mode != "check" and library is None:
            raise ValueError("找不到 libusb。请安装 libusb 或指定 --libusb。")

        # Upstream wipe-super expects its image to live underneath the firmware directory.
        with TemporaryDirectory(prefix=".cpday11-macos-", dir=firmware) as directory:
            tools = stage_upstream(archive, Path(directory))
            sys.path.insert(0, str(tools / "python"))
            try:
                import flash as upstream

                return upstream.main(
                    [] if mode == "clean-install" else [f"--{mode}"],
                    firmware=firmware, tools=tools, super_layout=tools / "super_empty.img",
                    create_transport=lambda root, _tools, probe: make_transport(
                        upstream, root, programs, library, args.serial, args.expected_active),
                )
            finally:
                sys.path.pop(0)
    except (BadZipFile, ImportError, OSError, ValueError) as error:
        print(f"流程已停止。{error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("已退出。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
