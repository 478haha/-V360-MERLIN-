# MSDisplay 协议摘要

## 已确认

- 官方安装包包含 32 位 Electron 应用和原生 N-API 模块 `MSDisplayAddon.node`；公开接口名包含 `DisplayStart`、`DisplaySetVideoParam`、`DisplaySendPicture`、`PushData` 等。
- 设备 USB 标识为 `VID_345F&PID_9132`。视频接口绑定到 `MI_03`，控件实现使用 interface 3 / bulk OUT endpoint `0x04`；控制通道使用 HID feature report。
- 当前实现采用 320×240、私有 VIC `0x94`。视频编码与设备写入逻辑见 `水冷屏自定义/水冷屏控件.py`。
- MSDisplay 与 BoWei AIO 是不同设备协议分支；不能仅凭“水冷屏”类别将两种协议混用。

## 限制

官方原生模块参数说明不完整。应用前端逻辑不能直接作为可读源码检查。控制器标记、帧方向和不同硬件批次的兼容性需要在对应设备上核验；不能推断所有 V360 或其他品牌设备都兼容。

已识别的 MSDisplay 通道提供屏幕初始化状态和视频帧传输，不提供水温或泵速遥测。界面监控仅展示控件服务、USB 会话和视频传输信息。

## 参考

- [MS912x USB 显示协议参考](https://github.com/codehz/msdisplay/blob/main/ms2160/PROTOCOL.md)
- [BoWei AIO 独立协议实现](https://github.com/codehz/msdisplay/blob/main/bowei_aio/display.py)

此摘要刻意不包含任何设备序列号、安装日志或厂商二进制文件。
