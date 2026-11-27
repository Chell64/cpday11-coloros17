# CPDAY11 ColorOS 17 SP2 macOS 刷写工具

适用于已解锁的酷派 COOL X Wide（CPDAY11）和 ColorOS `17.0.1.200 SP2` 发布包。提供离线校验、设备预检和清除数据安装。已在 Apple Silicon Mac 上实机验证清除数据安装。

**清除数据安装会重建 `super` 的两槽逻辑分区布局，清除手机上的照片、应用和其他用户数据，并刷入当前槽的另一槽。原槽需要恢复系统分区后才能启动。请先备份。** 回退原厂 Android 16 可使用发布者的 [FirstAid-V3](https://github.com/Chell64/cpday11-coloros17/releases/tag/FirstAid-V3)。

## 准备

1. 准备 Python 3.11 或更高版本。设备预检和刷入还需要 macOS 版 Android Platform-Tools 和 libusb。Homebrew 用户可运行 `brew install libusb` 和 `brew install --cask android-platform-tools`。[Android Platform-Tools 下载](https://developer.android.com/tools/releases/platform-tools)
2. 下载 SP2 的三个 `.7z` 分卷、SHA-256 校验清单和配套 Windows 刷写工具 ZIP。按发布页核对这四项的 SHA-256，再从 `.7z.001` 解出完整固件。[SP2 发布页](https://github.com/Chell64/cpday11-coloros17/releases/tag/17.0.1.200-sp2)
3. 将本目录和 Windows 工具 ZIP 放在解包后的可写固件目录中，与 `images` 同级。保留 ZIP 原样。

```text
CPDAY11-ColorOS17-17.0.1.200-SP2/
├── images/
├── extras/
├── expected-boot-digest.txt
├── SHA256SUMS.txt
├── CPDAY11-ColorOS17-17.0.1.200-SP2-Flash-Tools-Windows-x64.zip
└── macos/
    ├── Flash.command
    └── flash_macos.py
```

## 使用

在 Finder 双击 `macos/Flash.command`，或在固件目录的终端运行 `bash macos/Flash.command`，然后选择操作。

```bash
# 离线校验，手机可以断开
bash macos/Flash.command --check

# 手机连接 Mac 并进入 LK fastboot 或 fastbootd
bash macos/Flash.command --probe

# 在终端执行，输入 yes 和 rebuild-super 确认后才会写入
bash macos/Flash.command --clean-install
```

从 LK 开始预检时会检查引导器、目标槽和镜像。从 fastbootd 开始时也会检查 `super`，然后切换并停在 LK。刷写中请保持数据线连接，完成后手机会自动重启进入首次设置。

在仓库目录中也可以用 `bash tools/macos/Flash.command --firmware /path/to/SP2 --check`。工具 ZIP 放在其他目录时指定 `--windows-tools /path/to/tools.zip`。使用手动安装的 Platform-Tools 时指定 `--platform-tools /path/to/platform-tools`。使用自建的 libusb 时指定 `--libusb /path/to/libusb-1.0.dylib`。

## 刷写方式

工具先验证 SP2 工具 ZIP 和固件校验清单，再临时提取作者的刷写逻辑、PyUSB 和 `super_empty.img`。LK 通道使用 macOS libusb，fastbootd 通道使用 macOS 原生 fastboot。校验、写入顺序、清除数据和槽位切换沿用发布包的逻辑。

## 许可与署名

本工具按项目的 [PolyForm Noncommercial License 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0) 分发。

```text
Required Notice: COOL X Wide ColorOS 17 移植项目（https://github.com/Chell64/cpday11-coloros17）
```
