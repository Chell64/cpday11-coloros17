"""Windows fastboot installer for the unlocked CPDAY11 stock firmware."""

import argparse
import base64
from hashlib import file_digest
import json
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
import zlib


HERE = Path(__file__).resolve()
ROOT = HERE.parent if HERE.suffix.lower() == ".cmd" else HERE.parents[2]
sys.path.insert(0, str(ROOT / "tools" / "python"))

from flash import (AB_LOGICAL, LK_VERSION_PREFIX, LOGICAL, MIB, PARTITIONS as COLOROS_PARTITIONS,
                   PHYSICAL, STOCK_SLOT_FIRMWARE_MIB, FlashError,
                   Transport, confirm_space_release, for_slot, hexadecimal_getvar, partition_size,
                   validate, verified_images as verified_coloros_images)


BOOT = ("boot_a", "init_boot_a", "dtbo_a") + PHYSICAL
PARTITIONS = AB_LOGICAL + BOOT
SLOT_FIRMWARE = tuple(name for name in STOCK_SLOT_FIRMWARE_MIB if name != "lk_a") + ("lk_a",)
B_REFERENCE = "stock-b-firmware.sha256"
B_RECEIPT = "verified-b-firmware.json"
SUPER_SIZE = 9 * 1024**3
STOCK_SUPER_LAYOUT = 'eJxLd8lJNGFgYBD6H/ph9fKtbJ9itt6KPcVp4mTy4r7snMnrmpbMvNkg6VbHwMDIwMzAwMAgACJGwSgYBaNgFIyCUTAKRsEoGAWjYBSMglEwlIBBgI8jFwMTqHvPIDM/3GjqR+2Zu7Q6rnIePSt9KNH5uOr5lsTStm1JQjFXtq5gZmCI3X/cVETiqU+t4FpmiVNTw3/Vfr2Y9fyymMVn/c9hl5OaQGbyMTAwgMYUbjBB7JCAskHjBwYMDAwZzGDrGBwYIPRAgvyU3PiUnOzc+ETcahjR2HA9ScTpAQVDQVF+SmlyCT5rMOyB6cFjDYY9xZXFJal4fYNpD1QPPmtw2YM36HDYgzfocNiTWoEn6HDYA9KD00/o9pSl5qXkF5EWblA9JIUbVA9J4VaGpIfYcEtJTUsszSnB5zQUkJuYmYff9zDwwB5kPlg9Xp+jqmfggPHhA4gO4PgtLUgtIsIYAIJ3gHI='
COLOROS_V1_BOOT_DIGEST = "975c2709d403979f194476066a666cc6107e7b4176cdf137124e755308a2a445"
COLOROS_V1_SUPPORT = {
    "boot_a": "39e1c9935f3adc3782449eb1285a67974094210cfbc3f79bbab7e6566f62236d",
    "init_boot_a": "5881e0cb446d86a45de15653d55cfdf9b11b138d0a6e8385a4c9dafa53514119",
    "dtbo_a": "947bc476377255415cf72a4997976d82b11feb3cb977cb2e6d837640e8d4d8e7",
    "odm_dlkm_a": "8f8f0f28acd845c65bd27822485ac5428a59dadd88325af1194d9cf3cff5e646",
    "system_dlkm_a": "2d5f9aa335d643c0b03f036e1e6f9a364ecbc50ee41a50796cedd3897f6f0227",
}


def sha256(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def verified_images(root, *, inactive=False):
    partitions = PARTITIONS + SLOT_FIRMWARE if inactive else PARTITIONS
    expected = {f"images/{name}.img" for name in partitions} | {B_REFERENCE}
    checked = set()
    for line in (root / "SHA256SUMS.txt").read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([a-z0-9_./-]+)", line)
        if not match or match[2] not in expected - checked:
            raise FlashError("原厂回退包的校验清单有缺项、重复项或多余文件。")
        digest, name = match.groups()
        if sha256(root / name) != digest:
            raise FlashError(f"SHA-256 校验失败。{name}")
        checked.add(name)
        print(f"SHA-256 OK  {name}", flush=True)
    if checked != expected:
        raise FlashError("原厂回退包的校验清单不完整。")
    return {name: root / "images" / f"{name}.img" for name in partitions}


def verified_coloros_v1(root):
    if (root / "expected-boot-digest.txt").read_text(encoding="ascii").strip() != COLOROS_V1_BOOT_DIGEST:
        raise FlashError("双槽补丁适用于 ColorOS 17.0.0.101 V1。")
    images = verified_coloros_images(root)
    if images.keys() != set(COLOROS_PARTITIONS):
        raise FlashError("未识别 ColorOS V1 原始镜像清单。")
    for name, expected in COLOROS_V1_SUPPORT.items():
        image = root / "images" / f"{name}.img"
        if not image.is_file():
            raise FlashError(f"缺少 {image.name}。请将双槽补丁完整复制到 ColorOS V1 解压目录。")
        if sha256(image) != expected:
            raise FlashError(f"SHA-256 校验失败。{image.name}")
        print(f"SHA-256 OK  images/{image.name}", flush=True)
        images[name] = image
    return images


def choose_target(root, serial, active, requested):
    if requested == "a" or (requested == "auto" and active == "b"):
        return "a"
    receipt_path = root / B_RECEIPT
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt == {"serial": serial, "slot": "b", "reference_sha256": sha256(root / B_REFERENCE)}:
            print("已匹配本机的 B 槽原厂固件读回记录。", flush=True)
            return "b"
    if requested == "b":
        raise FlashError("当前设备的 B 槽固件尚无匹配的读回记录。请先补齐固件，或使用 --slot a。")
    print("B 槽固件状态尚未确认，本次恢复到原厂默认的 A 槽。", flush=True)
    return "a"


def check_transport(conn, mode, serial, images, active, target, *, check_logical=True):
    validate(conn, mode, serial, images, active=active, target=target, check_logical=check_logical)
    if mode == "fastbootd":
        if check_logical:
            for source in AB_LOGICAL:
                name = for_slot(source, target)
                if source not in LOGICAL and conn.get(f"is-logical:{name}") != "yes":
                    raise FlashError(f"未识别原厂逻辑分区 {name}。")
        return
    limit = hexadecimal_getvar(conn, "max-download-size")
    for source in ("boot_a", "init_boot_a", "dtbo_a"):
        name = for_slot(source, target)
        size = (64 if source == "boot_a" else 8) * MIB
        if partition_size(conn, name) != size or images[source].stat().st_size != size or size > limit:
            raise FlashError(f"原厂启动镜像与 {name} 的容量或下载限制不符。")


def plan_partitions(conn, images, target):
    other = "b" if target == "a" else "a"
    super_size = partition_size(conn, "super")
    if super_size != SUPER_SIZE:
        raise FlashError("super 的容量与此原厂备份的分区布局不符。")
    desired = {for_slot(name, target): images[name].stat().st_size for name in AB_LOGICAL}
    occupied = {for_slot(name, other): partition_size(conn, for_slot(name, other)) for name in AB_LOGICAL}
    if any(size <= 0 or size % 4096 for size in desired.values()) or any(
            size < 0 or size % 4096 for size in occupied.values()):
        raise FlashError("逻辑分区的容量或块对齐与备份不符。")

    def aligned(size):
        return (size + MIB - 1) // MIB * MIB

    needed = sum(map(aligned, desired.values()))
    available = super_size - MIB - sum(map(aligned, occupied.values()))
    release = []
    for source in ("product_a", "system_ext_a", "system_a"):
        name = for_slot(source, other)
        if available >= needed:
            break
        if occupied[name]:
            release.append(name)
            available += aligned(occupied[name])
    if available < needed:
        raise FlashError(f"目标槽最多可使用 {max(0, available):,} 字节，原厂镜像需要 {needed:,} 字节。")
    shrink = [(name, size) for name, size in desired.items() if partition_size(conn, name) > size]
    return release, shrink


def logical_layout(conn):
    layout = {}
    for slot in ("a", "b"):
        for source in AB_LOGICAL:
            name = for_slot(source, slot)
            status = conn.logical_status(name)
            if status == "yes":
                layout[name] = partition_size(conn, name)
            elif status == "missing":
                layout[name] = None
            else:
                raise FlashError(f"{name} 返回异常的逻辑分区状态 {status!r}。")
    return layout


def confirm_layout_rebuild(missing, target):
    print(f"缺少逻辑分区 {', '.join(missing)}。需要重建 super 的 A/B 分区表，再写入原厂 {target.upper()} 槽。"
          "重建会清掉 A/B 两槽的系统分区布局，刷完前请勿拔线或尝试进系统。", flush=True)
    return input("输入 rebuild-super 确认重建并继续，输入其它内容则取消。 ") == "rebuild-super"


def require_slot(conn, expected):
    actual = conn.get("current-slot")
    if actual != expected:
        raise FlashError(f"活动槽预期为 {expected.upper()}，实际为 {actual!r}。请保持当前界面并重新运行预检。")


def check_restore_lk(conn, active):
    require_slot(conn, active)
    version = conn.get("version-bootloader")
    if not version.startswith(LK_VERSION_PREFIX):
        raise FlashError(f"LK 版本为 {version!r}，要求以 {LK_VERSION_PREFIX} 开头。")


def snapshot_state(conn):
    state = conn.get("snapshot-update-status")
    if state not in {"none", "merging", "snapshotted"}:
        raise FlashError(f"无法确认 OTA 快照状态 {state!r}，恢复已停止。")
    return state


def require_no_snapshot(conn):
    state = snapshot_state(conn)
    if state != "none":
        raise FlashError(f"OTA 快照仍处于 {state}，恢复已停止。请保持当前 fastbootd 界面。")


def request_bootloader(conn):
    print("正在从 fastbootd 切换到引导器 fastboot（LK）。", flush=True)
    try:
        conn.reboot("bootloader")
    except FlashError as error:
        print(f"直接切换命令被拒绝，尝试通过 Recovery 切换。{error}", flush=True)
        conn.reboot("recovery")


def install_inactive(transport, root, images, *, probe=False):
    if not STOCK_SUPER_LAYOUT:
        raise FlashError("刷写工具缺少原厂分区表。")
    try:
        empty_layout = zlib.decompress(base64.b64decode(STOCK_SUPER_LAYOUT, validate=True))
    except (ValueError, zlib.error) as error:
        raise FlashError("刷写工具内置的原厂分区表损坏。") from error
    image_sizes = [images[name].stat().st_size for name in AB_LOGICAL]
    if any(size <= 0 or size % 4096 for size in image_sizes) or sum(
            (size + MIB - 1) // MIB * MIB for size in image_sizes) > SUPER_SIZE - MIB:
        raise FlashError("原厂镜像的容量或对齐与重建后的 super 不符。")

    with transport.connect() as (entry, conn):
        active = conn.get("current-slot")
        if active not in {"a", "b"}:
            raise FlashError("无法读取当前活动槽。")
        target = "b" if active == "a" else "a"
        print(f"当前槽 {active.upper()}，酷派原厂系统将写入 {target.upper()} 槽。", flush=True)
        if entry == "LK":
            check_restore_lk(conn, active)
            conn.reboot("fastboot")
        else:
            request_bootloader(conn)

    if entry != "LK":
        with transport.connect("LK", recovery=True) as (_, conn):
            check_restore_lk(conn, active)
            conn.reboot("fastboot")

    with transport.connect("fastbootd") as (_, conn):
        require_slot(conn, active)
        if partition_size(conn, "super") != SUPER_SIZE:
            raise FlashError("super 的容量与原厂分区表不符。")
        state = snapshot_state(conn)
        if probe:
            print(f"引导器与 fastbootd 预检通过。OTA 快照状态 {state}。"
                  f"正式恢复将清理 super 并写入 {target.upper()} 槽。", flush=True)
            return
        if state != "none":
            print(f"正在取消 OTA 快照（{state}）。", flush=True)
            print(conn.command("snapshot-update", "cancel"), flush=True)
            require_no_snapshot(conn)
        request_bootloader(conn)

    with transport.connect("LK", recovery=True) as (_, conn):
        check_restore_lk(conn, active)
        for name in BOOT + SLOT_FIRMWARE:
            conn.flash(for_slot(name, target), images[name])
        conn.reboot("fastboot")

    with transport.connect("fastbootd") as (_, conn):
        require_slot(conn, active)
        require_no_snapshot(conn)
        print(f"正在清理 super、重建原厂分区表。请保持连接，等待 {target.upper()} 槽写入完成。", flush=True)
        with TemporaryDirectory(prefix="cpday11-firstaid-", dir=root) as temporary:
            super_empty = Path(temporary) / "super_empty.img"
            super_empty.write_bytes(empty_layout)
            conn.rebuild_super(super_empty, root)
        if any(size != 0 for size in logical_layout(conn).values()):
            raise FlashError("重建后未读到预期的 14 个空逻辑分区。")
        for name in AB_LOGICAL:
            conn.flash(for_slot(name, target), images[name])
        require_slot(conn, active)
        require_no_snapshot(conn)
        conn.erase("userdata")
        conn.erase("metadata")
        conn.set_active(target)
        require_slot(conn, target)
        conn.reboot()
    print(f"酷派原厂系统已写入 {target.upper()} 槽，用户数据已清除。请等待首次设置。", flush=True)


def install(transport, root, images, *, requested="auto", probe=False, coloros=False,
            confirm_release=confirm_space_release, confirm_rebuild=confirm_layout_rebuild):
    if coloros and requested == "b":
        raise FlashError("ColorOS V1 双槽补丁以 A 槽为安装目标。请使用默认选项或 --slot a。")
    with transport.connect() as (mode, conn):
        active = conn.get("current-slot")
        if active not in {"a", "b"}:
            raise FlashError("无法读取当前活动槽。")
        target = "a" if coloros else choose_target(root, transport.serial, active, requested)
        label = "ColorOS V1" if coloros else "酷派原厂系统"
        print(f"当前槽 {active.upper()}，{label} 将写入 {target.upper()} 槽。", flush=True)
        check_transport(conn, mode, transport.serial, images, active, target, check_logical=coloros)
        if mode == "LK":
            conn.reboot("fastboot")

    with transport.connect("fastbootd") as (mode, conn):
        check_transport(conn, mode, transport.serial, images, active, target, check_logical=coloros)
        layout = {} if coloros else logical_layout(conn)
        missing = [name for name, size in layout.items() if size is None]
        if missing:
            if not STOCK_SUPER_LAYOUT:
                raise FlashError("此 Flash.cmd 缺少原厂分区表。")
            try:
                empty_layout = zlib.decompress(base64.b64decode(STOCK_SUPER_LAYOUT, validate=True))
            except (ValueError, zlib.error) as error:
                raise FlashError("Flash.cmd 内置的原厂分区表损坏。") from error
            super_name = conn.get("super-partition-name")
            if super_name != "super" or partition_size(conn, "super") != SUPER_SIZE:
                raise FlashError("super 分区与 CPDAY11 原厂布局不符。")
            image_sizes = [images[name].stat().st_size for name in AB_LOGICAL]
            if any(size <= 0 or size % 4096 for size in image_sizes) or sum(
                    (size + MIB - 1) // MIB * MIB for size in image_sizes) > SUPER_SIZE - MIB:
                raise FlashError("原厂镜像的容量或对齐与重建后的 super 不符。")
            if not probe and not confirm_rebuild(missing, target):
                print("已取消刷入。", flush=True)
                return
            release, shrink = [], []
        else:
            release, shrink = plan_partitions(conn, images, target)
        other = "b" if target == "a" else "a"
        if release and not probe and not confirm_release(other, target, release):
            print("已取消刷入。", flush=True)
            return
        conn.reboot("recovery")

    with transport.connect("LK", recovery=True) as (mode, conn):
        check_transport(conn, mode, transport.serial, images, active, target)
        if not probe:
            for name in BOOT:
                conn.flash(for_slot(name, target), images[name])
        conn.reboot("fastboot")

    with transport.connect("fastbootd") as (mode, conn):
        check_transport(conn, mode, transport.serial, images, active, target, check_logical=coloros)
        if not coloros and logical_layout(conn) != layout:
            raise FlashError("重连后 super 分区布局变化，请重新开始恢复。")
        if not missing and plan_partitions(conn, images, target) != (release, shrink):
            raise FlashError("分区容量计划发生变化，请重新开始恢复。")
        if probe:
            print("引导器与 fastbootd 预检通过，未刷写分区。", flush=True)
            if missing:
                print(f"缺少 {', '.join(missing)}。正式刷入时需要确认重建 super。", flush=True)
            if release:
                print(f"继续恢复需要确认释放 {', '.join(release)}。", flush=True)
            return
        if missing:
            if conn.get("super-partition-name") != "super" or partition_size(conn, "super") != SUPER_SIZE:
                raise FlashError("重连后 super 分区与 CPDAY11 原厂布局不符。")
            with TemporaryDirectory(prefix="cpday11-firstaid-", dir=root) as temporary:
                super_empty = Path(temporary) / "super_empty.img"
                super_empty.write_bytes(empty_layout)
                conn.rebuild_super(super_empty, root)
            if logical_layout(conn) != dict.fromkeys(layout, 0):
                raise FlashError("重建后未读到预期的 14 个空逻辑分区。")
            if plan_partitions(conn, images, target) != ([], []):
                raise FlashError("重建后的 super 容量与刷入计划不符。")
        for name, size in [(name, 0) for name in release] + shrink:
            conn.resize(name, size)
            if partition_size(conn, name) != size:
                raise FlashError(f"{name} 的容量与计划不符。")
        for name in AB_LOGICAL:
            target_name = for_slot(name, target)
            conn.flash(target_name, images[name])
            if partition_size(conn, target_name) != images[name].stat().st_size:
                raise FlashError(f"{target_name} 刷入后的容量与镜像不符。")
        conn.erase("userdata")
        conn.erase("metadata")
        conn.set_active(target)
        if conn.get("current-slot") != target:
            raise FlashError(f"未收到 {target.upper()} 槽激活确认。")
        conn.reboot()
    print(f"{label} 已写入 {target.upper()} 槽，用户数据已清除。请等待首次设置。", flush=True)


def main(argv=None, *, firmware=None, tools=None):
    parser = argparse.ArgumentParser(description=__doc__)
    checks = parser.add_mutually_exclusive_group()
    checks.add_argument("--check", action="store_true", help="仅核对镜像")
    checks.add_argument("--probe", action="store_true", help="只读检查刷机通道及槽位")
    parser.add_argument("--slot", choices=("auto", "a", "b"), default="auto", help="安装目标槽")
    args = parser.parse_args(argv)
    root = firmware if firmware is not None else ROOT
    try:
        coloros = firmware is None and (root / "extras" / "Gleaner.apk").is_file()
        if not (args.check or args.probe):
            if coloros:
                print("若使用此自制固件，此手机的保修可能会失效。")
            print("刷入会清除全部用户数据，包括照片、应用和 USB 调试授权。请先备份。")
            if input("确认要刷入吗？输入 yes 确认，输入其它内容则取消。 ") != "yes":
                print("已取消刷入。")
                return 0
        images = verified_coloros_v1(root) if coloros else verified_images(root)
        if not args.check:
            transport = Transport(root) if tools is None else Transport(root, tools)
            with transport.isolated_adb():
                install(transport, root, images, requested=args.slot, probe=args.probe, coloros=coloros)
        return 0
    except EOFError:
        print("已取消刷入。")
        return 0
    except (FlashError, OSError, ValueError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
        print(f"刷入流程已停止。{error}", file=sys.stderr, flush=True)
        print("请保留当前界面。排除问题后，从 fastboot 或 fastbootd 重新运行同一刷机包。",
              file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
