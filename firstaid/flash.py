"""Portable Windows installer for CPDAY11 ColorOS 17."""

import argparse
from contextlib import contextmanager, suppress
from hashlib import file_digest
import json
import os
from pathlib import Path
import re
import socket
import struct
import subprocess
import sys
import time

import usb.backend.libusb1
import usb.core
import usb.util

SCRIPT = Path(__file__).resolve()
ROOT = SCRIPT.parent if SCRIPT.suffix.lower() == ".cmd" else SCRIPT.parents[2]
LOGICAL = ("system_a", "vendor_a", "vendor_dlkm_a", "system_ext_a", "product_a")
PHYSICAL = ("vendor_boot_a", "vbmeta_system_a", "vbmeta_vendor_a", "vbmeta_a")
PARTITIONS = LOGICAL + PHYSICAL
AB_LOGICAL = LOGICAL + ("odm_dlkm_a", "system_dlkm_a")
STOCK_SLOT_FIRMWARE_MIB = {
    "lk_a": 4, "tee_a": 6, "gz_a": 32, "logo_a": 24, "dpm_a": 4,
    "scp_a": 6, "spmfw_a": 1, "sspm_a": 1, "mcupm_a": 1, "pi_img_a": 1,
    "modem_a": 200,
}
# A full-size sparse modem image fits the 128 MiB LK download limit.
# The device must still expose the target partition before the installer can proceed.
AB_PHYSICAL = (*STOCK_SLOT_FIRMWARE_MIB, "boot_a", "init_boot_a", "dtbo_a", *PHYSICAL)
AB_PARTITIONS = AB_LOGICAL + AB_PHYSICAL
SERIAL = re.compile(r"[A-Za-z0-9]+")
LK_VERSION = "k6789v1_64_79ec5a07814_202611040746"
LK_VERSION_PREFIX = "k6789v1_64_"
MIB = 1024 * 1024
MODE_NAMES = {"fastboot": "fastbootd", "recovery": "Recovery", "bootloader": "引导器 fastboot（LK）"}


class FlashError(Exception):
    pass


def report_reboot_timeout(target):
    print(f"切换到 {MODE_NAMES[target]} 时 USB 未在命令时限内响应，继续等待设备重新连接。", flush=True)


def verified_images(root):
    entries = {}
    for line in (root / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  (images/[a-z_]+\.img|extras/Gleaner\.apk)", line)
        if not match or match[2] in entries:
            raise FlashError("校验清单格式错误。")
        entries[match[2]] = match[1]
    partition_set = AB_PARTITIONS if "images/boot_a.img" in entries else PARTITIONS
    images = {name: root / "images" / f"{name}.img" for name in partition_set}
    required = {image.relative_to(root).as_posix() for image in images.values()}
    if entries.keys() != required | {"extras/Gleaner.apk"}:
        raise FlashError("校验清单缺少文件或包含多余文件。")
    for name, expected in entries.items():
        with (root / name).open("rb") as stream:
            actual = file_digest(stream, "sha256").hexdigest()
        if actual != expected:
            raise FlashError(f"SHA-256 校验失败。{name}")
        print(f"SHA-256 OK  {name}", flush=True)
    return images


class NativeFastboot:
    def __init__(self, run, serial):
        self.run = run
        self.serial = serial

    def command(self, *args, timeout=60):
        return self.run("fastboot", "-s", self.serial, *args, timeout=timeout)

    def get(self, name):
        output = self.command("getvar", name, timeout=5)
        match = re.search(rf"^{re.escape(name)}:\s*([^\r\n]+)", output, re.MULTILINE)
        if not match:
            raise FlashError(f"缺少 getvar {name} 的响应。{output.strip()}")
        return match[1].strip()

    def logical_status(self, name):
        try:
            return self.get(f"is-logical:{name}")
        except FlashError as error:
            if re.search(r"\bFAILED\s*\(remote: 'Partition not found'\)", str(error)):
                return "missing"
            raise

    def rebuild_super(self, image, root):
        print(self.command("wipe-super", str(image.relative_to(root)), timeout=180), flush=True)

    def flash(self, name, image):
        relative = Path("images") / image.name
        print(f"fastbootd 写入 {name}（{image.stat().st_size:,} 字节）", flush=True)
        print(self.command("-S", "112M", "flash", name, str(relative), timeout=1200), flush=True)

    def reboot(self, target=""):
        try:
            print(self.command("reboot", *([target] if target else []), timeout=15), flush=True)
        except subprocess.TimeoutExpired as error:
            # A mode change can take the USB interface down before the Windows
            # client exits. The next connect() must verify the serial and mode.
            output = error.output or b""
            if isinstance(output, bytes):
                output = output.decode("utf-8", "replace")
            if target not in MODE_NAMES:
                raise
            if "FAILED" in output:
                raise FlashError(output.strip()) from error
            report_reboot_timeout(target)

    def erase(self, name):
        output = self.command("erase", name, timeout=300)
        print(output, flush=True)
        if not re.search(rf"Erasing\s+'{name}'\s+OKAY", output):
            raise FlashError(f"未收到 {name} 擦除成功的确认。")

    def set_active(self, slot):
        print(self.command("set_active", slot, timeout=15), flush=True)

    def resize(self, name, size):
        print(self.command("resize-logical-partition", name, str(size), timeout=60), flush=True)


class UsbFastboot:
    def __init__(self, endpoint_in, endpoint_out):
        self.endpoint_in = endpoint_in
        self.endpoint_out = endpoint_out

    def send(self, data):
        if self.endpoint_out.write(data, timeout=5000) != len(data):
            raise FlashError("LK USB 写入长度不足。")

    def response(self, status, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            reply = bytes(self.endpoint_in.read(self.endpoint_in.wMaxPacketSize, timeout=remaining))
            if reply[:4] == b"INFO":
                print(reply[4:].decode("utf-8", "replace"), flush=True)
                continue
            if reply[:4] == status:
                return reply[4:].decode("ascii").strip("\x00 \r\n")
            raise FlashError(f"LK 返回意外响应。{reply.decode('utf-8', 'replace')}")
        raise FlashError("读取 LK 响应超时。")

    def get(self, name):
        self.send(f"getvar:{name}".encode("ascii"))
        return self.response(b"OKAY", 5)

    def flash(self, name, image):
        size = image.stat().st_size
        print(f"LK 写入 {name}（{size:,} 字节）", flush=True)
        self.send(f"download:{size:08x}".encode("ascii"))
        if int(self.response(b"DATA", 10), 16) != size:
            raise FlashError(f"{name} 的 LK 下载长度不匹配。")
        with image.open("rb") as stream:
            while chunk := stream.read(16384):
                self.send(chunk)
        self.response(b"OKAY", 180)
        self.send(f"flash:{name}".encode("ascii"))
        self.response(b"OKAY", 300)
        print(f"{name} OKAY", flush=True)

    def reboot(self, target=""):
        self.send(("reboot-" + target if target else "reboot").encode("ascii"))
        try:
            self.response(b"OKAY", 15)
        except usb.core.USBError as error:
            gone = error.backend_error_code == -4  # LIBUSB_ERROR_NO_DEVICE
            if target not in MODE_NAMES or not (gone or isinstance(error, usb.core.USBTimeoutError)):
                raise
            report_reboot_timeout(target)


class Transport:
    reconnect_timeout = 100

    def __init__(self, root, tools=None):
        self.root = root
        tools = tools if tools is not None else root / "tools"
        self.tools = tools / "platform-tools"
        libusb = tools / "python" / "libusb-1.0.dll"
        self.backend = usb.backend.libusb1.get_backend(find_library=lambda _: str(libusb))
        if self.backend is None:
            raise FlashError("无法加载包内 libusb。")
        self.env = {**os.environ, "ADB_LIBUSB": "1"}
        self.serial = None
        self.adb_socket = None
        self.adb_used = False

    @contextmanager
    def isolated_adb(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.adb_socket = f"tcp:localhost:{listener.getsockname()[1]}"
        try:
            yield
        finally:
            if self.adb_used:
                with suppress(FlashError, OSError, subprocess.TimeoutExpired):
                    self.run("adb", "kill-server", timeout=5)
            self.adb_socket = None
            self.adb_used = False

    def run(self, program, *args, timeout=10):
        command = [str(self.tools / f"{program}.exe")]
        if program == "adb" and self.adb_socket:
            self.adb_used = True
            command.extend(["-L", self.adb_socket])
        completed = subprocess.run(command + list(args), stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, encoding="utf-8", errors="replace",
                                   check=False, timeout=timeout, env=self.env, cwd=self.root)
        if completed.returncode:
            raise FlashError(f"{program} {' '.join(args)}\n{completed.stdout.strip()}")
        return completed.stdout

    def native_serials(self):
        results = self.run("fastboot", "devices")
        return {fields[0] for line in results.splitlines()
                if len(fields := line.split()) == 2 and fields[1] == "fastboot"}

    def enter_fastbootd_for_keep_data(self):
        devices = [line.split() for line in self.run("adb", "devices").splitlines()[1:] if line.strip()]
        if len(devices) != 1 or len(devices[0]) != 2 or devices[0][1] != "device":
            raise FlashError("保留数据刷入需要从已授权 USB 调试的当前系统开始，并且只连接一台设备。")
        self.check_serial(devices[0][0])
        expected = (self.root / "expected-boot-digest.txt").read_text(encoding="ascii").strip()
        fields = self.run("adb", "-s", self.serial, "shell",
                          "getprop ro.boot.slot_suffix; getprop ro.boot.vbmeta.digest; "
                          "getprop sys.boot_completed").splitlines()
        if (not re.fullmatch(r"[0-9a-f]{64}", expected) or len(fields) != 3
                or fields[0] not in {"_a", "_b"} or fields[1:] != [expected, "1"]):
            raise FlashError("保留数据刷入要求手机已从与刷机包相同的 AVB 启动链完整进入 Android。")
        self.run("adb", "-s", self.serial, "reboot", "fastboot", timeout=15)
        return fields[0][1:]

    def lk_interfaces(self):
        for device in usb.core.find(find_all=True, backend=self.backend, idVendor=0x0E8D, idProduct=0x201C):
            for config in device:
                for interface in config:
                    kind = (interface.bInterfaceClass, interface.bInterfaceSubClass, interface.bInterfaceProtocol)
                    if kind == (0xFF, 0x42, 3):
                        yield device, interface

    @contextmanager
    def open_lk(self, device, interface):
        try:
            try:
                device.get_active_configuration()
            except usb.core.USBError:
                device.set_configuration()
            usb.util.claim_interface(device, interface.bInterfaceNumber)
            bulk = {usb.util.endpoint_direction(endpoint.bEndpointAddress): endpoint for endpoint in interface
                    if usb.util.endpoint_type(endpoint.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK}
            yield UsbFastboot(bulk[usb.util.ENDPOINT_IN], bulk[usb.util.ENDPOINT_OUT])
        finally:
            with suppress(usb.core.USBError):
                usb.util.dispose_resources(device)

    def check_serial(self, serial):
        if not SERIAL.fullmatch(serial):
            raise FlashError("无法确认设备序列号。")
        if self.serial is not None and serial != self.serial:
            raise FlashError("USB 上出现了另一台设备。请只连接目标手机。")
        self.serial = serial

    @contextmanager
    def connect(self, mode=None, *, recovery=False):
        deadline = None if self.reconnect_timeout is None else time.monotonic() + self.reconnect_timeout
        if deadline is None:
            instructions = {
                "LK": "若手机仍停在 Recovery 或 fastbootd，请手动进入引导器 fastboot，脚本会继续。",
                "fastbootd": "若手机仍停在引导器，请在设备上选择进入 fastbootd，脚本会继续。",
            }
            print(f"正在等待 {mode or '引导器 fastboot / fastbootd'} 连接。"
                  f"{instructions.get(mode, '请连接设备并进入 fastboot 或 fastbootd。')}可按 Ctrl+C 退出等待。", flush=True)
        last_error = ""
        while deadline is None or time.monotonic() < deadline:
            try:
                serials = self.native_serials()
                lk = list(self.lk_interfaces())
            except (subprocess.TimeoutExpired, usb.core.USBError) as error:
                last_error = str(error)
                time.sleep(1)
                continue
            if len(serials) > 1 or len(lk) > 1:
                raise FlashError("检测到多台 fastboot 设备。请只连接目标手机。")
            if serials:
                self.check_serial(next(iter(serials)))
                native = NativeFastboot(self.run, self.serial)
                try:
                    status = native.get("is-userspace")
                except (FlashError, subprocess.TimeoutExpired) as error:
                    last_error = str(error)
                else:
                    if status == "yes" and lk:
                        raise FlashError("同时检测到 fastbootd 和 LK。请只连接目标手机。")
                    actual = {"yes": "fastbootd", "no": "LK"}.get(status)
                    if actual and (mode is None or mode == actual):
                        yield actual, native
                        return
            if lk and mode != "fastbootd":
                ready = False
                try:
                    with self.open_lk(*lk[0]) as raw:
                        serial = raw.get("serialno")
                        ready = True
                        self.check_serial(serial)
                        yield "LK", raw
                        return
                except (usb.core.USBError, FlashError, KeyError) as error:
                    if ready:
                        raise
                    last_error = str(error)
            if recovery:
                try:
                    adb_devices = self.run("adb", "devices")
                except (FlashError, subprocess.TimeoutExpired) as error:
                    last_error = str(error)
                    time.sleep(1)
                    continue
                recovered = [fields[0] for line in adb_devices.splitlines()
                             if len(fields := line.split()) == 2 and fields[1] == "recovery"]
                if len(recovered) > 1:
                    raise FlashError("检测到多台 Recovery 设备。")
                if recovered:
                    self.check_serial(recovered[0])
                    try:
                        self.run("adb", "-s", self.serial, "reboot", "bootloader", timeout=15)
                        recovery = False
                    except (FlashError, subprocess.TimeoutExpired) as error:
                        last_error = str(error)
            time.sleep(1)
        hint = ("若手机停在 Recovery，请在手机上选择进入 fastbootd，并检查 Windows 的 fastbootd 接口驱动。"
                if mode == "fastbootd" else "请核对当前模式、数据线和 WinUSB 驱动。")
        if mode == "LK" and recovery:
            hint += "如果 Recovery 一直未被识别，请关闭手机助手或其他占用 ADB 的程序，再从手机进入引导器 fastboot。"
        raise FlashError(f"等待 {mode or 'fastboot / fastbootd'} 超时。{hint}\n{last_error}")


def partition_size(connection, name):
    return hexadecimal_getvar(connection, f"partition-size:{name}")


def hexadecimal_getvar(connection, name):
    value = connection.get(name)
    try:
        return int(value, 16)
    except ValueError as error:
        raise FlashError(f"getvar {name} 返回无效的十六进制值 {value!r}。") from error


def validate(connection, mode, serial, images, *, active="a", target="a", complete=False,
             verified_firmware=(), check_logical=True):
    common = {"serialno": serial, "unlocked": "yes", "current-slot": active}
    if mode == "fastbootd":
        expected = {**common, "is-userspace": "yes", "product": "L950U", "snapshot-update-status": "none"}
    else:
        expected = {**common, "is-userspace": "no", "product": "k6789v1_64"}
    for name, value in expected.items():
        actual = connection.get(name)
        if actual != value:
            raise FlashError(f"{mode} 设备检查失败。{name} 为 {actual}，需要 {value}。")
    if mode == "LK":
        # Only the complete slot package can replace LK and the other board firmware.
        version = connection.get("version-bootloader")
        if complete and version != LK_VERSION:
            raise FlashError(f"LK 设备检查失败。version-bootloader 为 {version}，需要 {LK_VERSION}。")
        if not complete and not version.startswith(LK_VERSION_PREFIX):
            raise FlashError(f"LK 设备检查失败。version-bootloader 为 {version}，需要以 {LK_VERSION_PREFIX} 开头。")
    max_download = hexadecimal_getvar(connection, "max-download-size")
    if mode == "fastbootd":
        if max_download < 112 * MIB:
            raise FlashError("fastbootd 下载容量小于 112 MiB。")
        if check_logical:
            for source in AB_LOGICAL if complete else LOGICAL:
                name = for_slot(source, target)
                if connection.get(f"is-logical:{name}") != "yes":
                    raise FlashError(f"未找到预期的逻辑分区 {name}。")
        for name in ("userdata", "metadata"):
            if partition_size(connection, name) <= 0:
                raise FlashError(f"缺少有效的 {name} 分区。")
    else:
        for source in AB_PHYSICAL if complete else PHYSICAL:
            if source in verified_firmware:
                continue
            name = for_slot(source, target)
            expected_size = STOCK_SLOT_FIRMWARE_MIB.get(
                source, 64 if source in {"boot_a", "vendor_boot_a"} else 8) * MIB
            if partition_size(connection, name) != expected_size:
                raise FlashError(f"LK {name} 分区容量与 CPDAY11 不符。")
            if not 0 < images[source].stat().st_size <= min(expected_size, max_download):
                raise FlashError(f"{name} 镜像超出 LK 下载或分区容量。")
            if source == "modem_a":
                sparse_header = struct.Struct("<I4H4I")
                with images[source].open("rb") as image:
                    header = image.read(sparse_header.size)
                if len(header) != sparse_header.size:
                    raise FlashError(f"{name} 缺少完整的稀疏镜像头。")
                magic, major, minor, header_size, chunk_size, block, total_blocks, chunks, _ = sparse_header.unpack(header)
                if ((magic, major, minor, header_size, chunk_size, block) != (0xED26FF3A, 1, 0, 28, 12, 4096)
                        or block * total_blocks != expected_size or chunks < 1):
                    raise FlashError(f"{name} 稀疏镜像展开后与原厂分区大小不符。")


def install(transport, images, *, probe=False, confirm_release=None, firmware_receipt=None, keep_data=False):
    if images.keys() == set(AB_PARTITIONS):
        return install_inactive(transport, images, probe=probe, confirm_release=confirm_release,
                                firmware_receipt=firmware_receipt, keep_data=keep_data)
    if keep_data:
        raise FlashError("只有完整双槽包支持保留数据刷入。")
    if firmware_receipt is not None:
        raise FlashError("只有完整双槽包支持固件读回记录。")
    print("检查 fastboot 和 fastbootd 通道。", flush=True)
    with transport.connect() as (start, conn):
        validate(conn, start, transport.serial, images)
        if start == "fastbootd":
            conn.reboot("recovery")
        else:
            conn.reboot("fastboot")

    if start == "fastbootd":
        with transport.connect("LK", recovery=True) as (mode, conn):
            validate(conn, mode, transport.serial, images)
            conn.reboot("fastboot")

    with transport.connect("fastbootd") as (mode, conn):
        validate(conn, mode, transport.serial, images)
        if probe:
            print(f"两种模式验证通过。设备 {transport.serial} 停在 fastbootd。", flush=True)
            return
        print(f"写入设备 {transport.serial} 的 A 槽逻辑分区。", flush=True)
        for name in LOGICAL:
            conn.flash(name, images[name])
            if partition_size(conn, name) != images[name].stat().st_size:
                raise FlashError(f"{name} 分区写入后的容量与镜像不符。")
        conn.reboot("recovery")

    print("写入 A 槽启动配套和 AVB 签名。", flush=True)
    with transport.connect("LK", recovery=True) as (mode, conn):
        validate(conn, mode, transport.serial, images)
        for name in PHYSICAL:
            conn.flash(name, images[name])
        conn.reboot("fastboot")

    print("清除用户数据和加密元数据。", flush=True)
    with transport.connect("fastbootd") as (mode, conn):
        validate(conn, mode, transport.serial, images)
        conn.erase("userdata")
        conn.erase("metadata")
        conn.reboot()
    print("安装和清除数据完成，已发送重启命令。请等待 ColorOS 首次设置。", flush=True)


def for_slot(name, slot):
    return name.removesuffix("_a") + "_" + slot


def plan_inactive_space(connection, images, active, target):
    usable = partition_size(connection, "super") - MIB
    lengths = {name: partition_size(connection, for_slot(name, active)) for name in AB_LOGICAL}
    needed = sum(images[name].stat().st_size for name in AB_LOGICAL)
    available = usable - sum(lengths.values())
    release = []
    for name in ("product_a", "system_ext_a", "system_a"):
        if available >= needed:
            break
        if lengths[name]:
            release.append(for_slot(name, active))
            available += lengths[name]
    if available < needed:
        raise FlashError(f"按此设备的分区方案，目标槽 {target.upper()} 最多可用 "
                         f"{max(0, available):,} 字节，需要 {needed:,} 字节。现有槽位和数据未修改。")
    return release


def confirm_space_release(active, target, release):
    print(f"为刷入 {target.upper()} 槽，需要释放旧 {active.upper()} 槽的 {', '.join(release)}。"
          "释放后旧槽无法正常启动，需用原厂恢复包修复。", flush=True)
    command = f"release-{active}"
    try:
        return input(f"输入 {command} 确认释放分区并继续，输入其它内容则取消。 ") == command
    except EOFError:
        return False


def checked_slot_firmware(receipt_path, images, serial, active, target):
    if receipt_path is None:
        return ()
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    if not isinstance(receipt, dict) or (receipt.get("serial"), receipt.get("active"), receipt.get("target")) != (
            serial, active, target):
        raise FlashError("固件读回记录与当前设备或槽位不符。")
    digests = receipt.get("package_firmware_sha256")
    if not isinstance(digests, dict) or digests.keys() != STOCK_SLOT_FIRMWARE_MIB.keys():
        raise FlashError("读回记录缺少原厂固件。")
    for name, expected in digests.items():
        with images[name].open("rb") as stream:
            if file_digest(stream, "sha256").hexdigest() != expected:
                raise FlashError(f"读回记录与刷机包的 {name}.img 不符。")
    print(f"使用本机 {target.upper()} 槽固件读回记录，跳过 {len(digests)} 个已校验的原厂固件分区。", flush=True)
    return STOCK_SLOT_FIRMWARE_MIB.keys()


def install_inactive(transport, images, *, probe=False, confirm_release=None, firmware_receipt=None,
                     keep_data=False):
    source_slot = transport.enter_fastbootd_for_keep_data() if keep_data else None
    with transport.connect() as (start, conn):
        active = conn.get("current-slot")
        if active not in {"a", "b"}:
            raise FlashError(f"无法识别当前槽位。{active}")
        if keep_data and active != source_slot:
            raise FlashError("fastboot 当前槽与已校验的 Android 槽位不符。")
        target = "b" if active == "a" else "a"
        verified_firmware = checked_slot_firmware(firmware_receipt, images, transport.serial, active, target)
        slot_args = dict(active=active, target=target, complete=True, verified_firmware=verified_firmware)
        validate(conn, start, transport.serial, images, **slot_args)
        if start == "LK":
            conn.reboot("fastboot")

    with transport.connect("fastbootd") as (mode, conn):
        validate(conn, mode, transport.serial, images, **slot_args)
        release = plan_inactive_space(conn, images, active, target)
        if release and not probe:
            confirm = confirm_release or confirm_space_release
            if not confirm(active, target, release):
                print("已取消刷入。", flush=True)
                return
        conn.reboot("recovery")

    with transport.connect("LK", recovery=True) as (mode, conn):
        validate(conn, mode, transport.serial, images, **slot_args)
        if not probe:
            for source in AB_PHYSICAL:
                if source not in verified_firmware:
                    conn.flash(for_slot(source, target), images[source])
        conn.reboot("fastboot")

    with transport.connect("fastbootd") as (mode, conn):
        validate(conn, mode, transport.serial, images, **slot_args)
        if plan_inactive_space(conn, images, active, target) != release:
            raise FlashError("分区容量核对结果发生变化。请重新开始刷机。")
        if probe:
            print(f"两种刷机通道验证通过。当前槽 {active.upper()}，目标槽 {target.upper()}。", flush=True)
            if release:
                print(f"刷入前需要确认释放 {', '.join(release)}。", flush=True)
            return
        print(f"当前槽 {active.upper()}，正在写入另一槽 {target.upper()}。", flush=True)
        for name in release:
            conn.resize(name, 0)
            if partition_size(conn, name) != 0:
                raise FlashError(f"未确认释放 {name}。请保留当前 fastbootd 界面。")
        for source in AB_LOGICAL:
            name = for_slot(source, target)
            conn.flash(name, images[source])
            if partition_size(conn, name) != images[source].stat().st_size:
                raise FlashError(f"{name} 分区写入后的容量与镜像不符。")
        if keep_data:
            print("同一构建平刷，保留 userdata 和 metadata。", flush=True)
        else:
            conn.erase("userdata")
            conn.erase("metadata")
        conn.set_active(target)
        if conn.get("current-slot") != target:
            raise FlashError(f"fastbootd 未确认切换到 {target.upper()} 槽。请保留当前界面。")
        conn.reboot()
    suffix = "请等待系统启动。" if keep_data else "请等待首次设置。"
    print(f"已切换到 {target.upper()} 槽并发送重启命令。{suffix}", flush=True)


def confirm_flash(keep_data):
    print("若使用此自制固件，此手机的保修可能会失效。", flush=True)
    print("本次从同一构建平刷并保留用户数据。" if keep_data
          else "刷入完成后将清除手机上的全部用户数据。请先备份。", flush=True)
    print("双槽包如遇空间不足，会先请你确认释放旧槽分区。", flush=True)
    try:
        return input("确认要刷入吗？输入 yes 确认，输入其它内容则取消刷入。 ") == "yes"
    except EOFError:
        return False


def main(argv=None, *, firmware=None, tools=None, confirm=None, create_transport=None):
    parser = argparse.ArgumentParser(description=__doc__)
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--check", action="store_true", help="仅校验包内文件")
    checks.add_argument("--probe", action="store_true", help="校验文件和两种 fastboot 通道")
    parser.add_argument("--verified-slot-firmware", "--verified-b-firmware", type=Path,
                        help="本机目标槽已写入的原厂固件读回记录")
    parser.add_argument("--keep-data", action="store_true", help="从已启动的同一构建执行双槽平刷，保留数据")
    args = parser.parse_args(argv)
    root = firmware if firmware is not None else ROOT
    try:
        if not (args.check or args.probe) and not (confirm or confirm_flash)(args.keep_data):
            print("已取消刷入。", flush=True)
            return 0
        images = verified_images(root)
        if firmware is not None and images.keys() != set(AB_PARTITIONS):
            raise FlashError("此工具需要完整的 CPDAY11 A/B 固件包。")
        if args.check:
            return 0
        if create_transport is not None and not args.probe:
            transport = create_transport(root, tools)
        else:
            transport = Transport(root) if tools is None else Transport(root, tools)
        install_args = {"probe": args.probe}
        if args.verified_slot_firmware is not None:
            install_args["firmware_receipt"] = args.verified_slot_firmware
        if args.keep_data:
            install_args["keep_data"] = True
        install(transport, images, **install_args)
        return 0
    except (FlashError, OSError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        print(f"流程已停止。{error}", file=sys.stderr, flush=True)
        if args.keep_data:
            print("请保留当前界面和日志。核对分区状态后再继续保留数据的安装。", file=sys.stderr)
        else:
            print("请保留当前界面。排除问题后，从 fastboot 或 fastbootd 重新运行同一刷机包。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
