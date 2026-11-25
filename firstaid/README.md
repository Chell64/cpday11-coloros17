# 酷派阔直板 CPDAY11 原厂回退包

原厂系统为 Android 16，构建号 `1793781667`。镜像来自项目保存的这台机器的原厂分区备份。适用于引导器已解锁、能进入 fastboot 或 fastbootd 的 CPDAY11，支持 Windows x64。

**恢复会清除全部用户数据，包括照片、应用、账号和 USB 调试授权。请先备份。** 原厂备份中的 `dtbo` 与原厂 AVB 记录有已知差异，这份原厂镜像曾在本机引导器解锁状态下启动。刷完后请保持引导器解锁。

## 刷入

1. 把所有 7z 分卷放到同一目录，从 `.7z.001` 解压。用 `7z-SHA256SUMS.txt` 核对分卷。解压目录与系统盘的位置无关。
2. 只连接目标手机，进入引导器 fastboot 或 Recovery 里的 fastbootd。可先在解压目录运行 `Flash.cmd --probe`。预检完成后手机会停在 fastbootd。
3. 双击 `Flash.cmd`，输入小写 `yes` 后按回车。输入其它内容会取消。如果需要释放原槽位的逻辑分区，脚本会列出将释放的分区，另行要求确认。释放后，旧槽需要完整恢复才能再次启动。
4. 脚本会写入目标槽的原厂逻辑分区和启动链，清除 `userdata` 和 `metadata`，确认活动槽后重启。等待酷派首次设置向导。

## 选择槽位

- 默认自动选择。当前在 B 槽时回退到 A。当前在 A 槽时，如果能核对本机的 B 槽固件读回记录，则切换到 B。否则在 A 槽恢复。
- 可用 `Flash.cmd --slot a` 指定恢复到 A，或用 `Flash.cmd --slot b` 指定恢复到已验证的 B。
- 包内如有 `verified-b-firmware.json`，它只适用于记录中的设备。B 槽固件补齐和读回校验是在手机上单独完成的，回退脚本保留这些底层分区，以及 NVRAM、校准和引导器锁状态。
- 如果脚本报错，保留当前界面和报错，从 fastboot 或 fastbootd 重新运行同一包。指定槽位时，请继续使用相同的 `--slot` 参数。

## 无写入检查

```cmd
Flash.cmd --check
Flash.cmd --probe
Flash.cmd --probe --slot a
Flash.cmd --probe --slot b
```

随包的 ADB、fastboot、Python、PyUSB 和 libusb 的许可文件位于 `tools` 目录。
