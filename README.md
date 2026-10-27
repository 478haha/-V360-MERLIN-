# Valkyrie V360 MERLIN 水冷屏控件

Windows 后台控件，可将本地视频转换为 320×240 画面并通过 MSDisplay USB 协议写入 V360 MERLIN 水冷屏。支持本机网页控制、托盘菜单和当前用户登录自启。

## 版本

当前版本：`0.1.1`。本版移除了所有内置演示视频和对应资源引用。迭代规则见 [版本与发布](#版本与发布)。

## 使用条件

- Windows 10/11 x64
- V360 MERLIN 水冷屏通过 USB 数据线连接
- 已安装官方 Myth.Cool 驱动；使用 EXE 时需要准备与官方安装目录对应的 `libusb0.dll` 和 FFmpeg 运行库
- 退出 Myth.Cool 后再启动本控件，避免设备被同时占用

## 从源码启动

1. 安装 Python 3.10 或更新版本，以及 FFmpeg，并确保 `ffmpeg.exe` 可从 `PATH` 找到。
2. 在官方 Myth.Cool 安装目录或项目的本机 `水冷屏自定义/runtime/` 中准备 `libusb0.dll`。
3. 运行：

   ```powershell
   python .\水冷屏自定义\水冷屏控件.py
   ```

控件只监听 `127.0.0.1:8765`。页面为仓库根目录的 `水冷控件简版.html`。选择本地视频后可调整画面并写入设备。后台日志写入 `%LOCALAPPDATA%\ValkyrieV360\control.log`。

## 构建 EXE

在已安装 Python 的 Windows x64 环境中执行：

```powershell
python -m pip install -r requirements.txt
./build.ps1 -RuntimeDirectory 'C:\path\to\approved\runtime'
```

`RuntimeDirectory` 应由使用者从其合法安装的 Myth.Cool 环境准备，至少包含 FFmpeg 运行文件及 `libusb0.dll`。构建产物写入 `水冷屏自定义/dist/`，该目录不会提交到 Git。不要将官方安装包、驱动文件或第三方运行库提交到此仓库。

首次运行后可通过托盘菜单管理后台服务。命令行参数 `--install-startup` 和 `--remove-startup` 分别设置和移除当前 Windows 用户的登录自启项。

## 协议摘要

当前实现面向 MSDisplay 设备 `VID/PID 345F:9132`：HID feature report 控制通道及 interface 3 / bulk OUT `0x04` 视频通道，模式为 320×240、私有 VIC `0x94`。详细的已确认信息与限制见 [协议摘要](docs/protocol-summary.md)。协议依据官方应用行为和公开参考资料整理；设备兼容性取决于控制器、驱动与 USB 接口状态。

## 版本与发布

- 使用语义化版本：`MAJOR.MINOR.PATCH`。
- 每次迭代更新根目录 `VERSION` 与 `CHANGELOG.md`。
- 发布版本时创建匹配的 Git 标签，例如 `v0.1.0`；推送提交和标签：

  ```powershell
  git push origin main
  git push origin v0.1.0
  ```

- 持续迭代使用功能分支和 Pull Request；合并后更新变更记录、版本号，再打版本标签。

## 目录

- `水冷屏自定义/水冷屏控件.py`：设备服务、视频处理和托盘程序
- `水冷控件简版.html`：本地控制界面，视频由使用者自行选择
- `build.ps1`：Windows EXE 构建脚本
- `docs/protocol-summary.md`：已确认协议摘要和待验证范围

## 第三方组件与许可

本仓库不包含 Myth.Cool 安装程序、驱动、FFmpeg 二进制或其他第三方专有运行库。使用者需自行遵守各组件的许可和厂商条款。本项目未授予官方组件的再分发权。

