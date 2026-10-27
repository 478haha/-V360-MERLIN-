import ctypes
import http.server
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import tempfile
import time
import urllib.parse
import urllib.request
import webbrowser
import uuid
from ctypes import POINTER, Structure, c_char, c_char_p, c_int, c_ubyte, c_uint16, c_uint32, c_void_p
from pathlib import Path

VID, PID = 0x345F, 0x9132
WIDTH, HEIGHT, VIC = 320, 240, 0x94
COLOR_BGR888 = 0x11
CHUNK = 0x80000
RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


class UsbDescriptor(Structure):
    _pack_ = 1
    _fields_ = [
        ("length", c_ubyte), ("dtype", c_ubyte), ("bcd_usb", c_uint16),
        ("dev_class", c_ubyte), ("dev_subclass", c_ubyte), ("dev_protocol", c_ubyte),
        ("max_packet", c_ubyte), ("vid", c_uint16), ("pid", c_uint16),
        ("bcd_device", c_uint16), ("i_manufacturer", c_ubyte),
        ("i_product", c_ubyte), ("i_serial", c_ubyte), ("num_configs", c_ubyte),
    ]


class UsbBus(Structure):
    pass


class UsbDevice(Structure):
    pass


UsbBusPtr, UsbDevicePtr = POINTER(UsbBus), POINTER(UsbDevice)
UsbDevice._fields_ = [
    ("next", UsbDevicePtr), ("prev", UsbDevicePtr), ("filename", c_char * 512),
    ("bus", UsbBusPtr), ("descriptor", UsbDescriptor), ("config", c_void_p),
    ("dev", c_void_p), ("devnum", c_ubyte), ("num_children", c_ubyte),
    ("children", POINTER(UsbDevicePtr)),
]
UsbBus._fields_ = [
    ("next", UsbBusPtr), ("prev", UsbBusPtr), ("dirname", c_char * 512),
    ("devices", UsbDevicePtr), ("location", c_uint32), ("root_dev", UsbDevicePtr),
]


def _find_libusb():
    bundled = RESOURCE_ROOT / "runtime" / "libusb0.dll"
    if bundled.is_file():
        return bundled
    local = Path(os.environ.get("LOCALAPPDATA", ""))
    candidates = list(local.glob("MythCool/Apps/*/*/win32/libusb/amd64/libusb0.dll"))
    candidates += list(local.glob("MythCool/Apps/*/*/win32/libusb/amd64/libusb0_x64.dll"))
    for p in candidates:
        if p.is_file():
            return p
    return None


class MSDisplay:
    def __init__(self):
        dll = _find_libusb()
        if not dll:
            raise RuntimeError("没找到 Myth.Cool 安装的 libusb0.dll。请先运行一次官方 Myth.Cool 安装驱动。")
        self.lib = ctypes.CDLL(str(dll))
        self._bind()
        self.lib.usb_init()
        self.lib.usb_find_busses()
        self.lib.usb_find_devices()
        self.lib.usb_get_busses.restype = UsbBusPtr
        self.handle = None
        bus = self.lib.usb_get_busses()
        while bus:
            dev = bus.contents.devices
            while dev:
                d = dev.contents.descriptor
                if d.vid == VID and d.pid == PID:
                    self.handle = self.lib.usb_open(dev)
                    break
                dev = dev.contents.next
            if self.handle or not bus.contents.next:
                break
            bus = bus.contents.next
        if not self.handle:
            raise RuntimeError("未找到水冷屏 USB 设备 345F:9132。请检查水冷屏 USB 线，并关闭 Myth.Cool。")
        self.lib.usb_set_configuration(self.handle, 1)
        # Interface 3 is bound by the bundled MS USB Display driver; interface 0
        # remains the HID control plane used by the vendor protocol.
        if self.lib.usb_claim_interface(self.handle, 3) != 0:
            detail = self.lib.usb_strerror()
            message = detail.decode("mbcs", "replace") if detail else "未知错误"
            self.lib.usb_close(self.handle)
            self.handle = None
            raise RuntimeError(f"无法占用屏幕视频接口 3：{message}。请关闭 Myth.Cool。")
        self._fid = 0
        self._initialized = False

    def _bind(self):
        l = self.lib
        l.usb_find_busses.restype = c_int
        l.usb_find_devices.restype = c_int
        l.usb_open.argtypes = [UsbDevicePtr]
        l.usb_open.restype = c_void_p
        l.usb_set_configuration.argtypes = [c_void_p, c_int]
        l.usb_claim_interface.argtypes = [c_void_p, c_int]
        l.usb_release_interface.argtypes = [c_void_p, c_int]
        l.usb_close.argtypes = [c_void_p]
        l.usb_control_msg.argtypes = [c_void_p, c_int, c_int, c_int, c_int, c_char_p, c_int, c_int]
        l.usb_control_msg.restype = c_int
        l.usb_bulk_write.argtypes = [c_void_p, c_int, c_char_p, c_int, c_int]
        l.usb_bulk_write.restype = c_int
        l.usb_strerror.restype = c_char_p

    def close(self):
        if self.handle:
            self.lib.usb_release_interface(self.handle, 3)
            self.lib.usb_close(self.handle)
            self.handle = None

    def _control(self, request_type, request, value, payload=b""):
        buf = ctypes.create_string_buffer(payload.ljust(8, b"\0"), max(1, len(payload), 8))
        result = self.lib.usb_control_msg(
            self.handle, request_type, request, value, 0, ctypes.cast(buf, c_char_p), 8, 1000
        )
        if result < 0:
            raise RuntimeError(f"USB 控制传输失败（{result}）；请先关闭 Myth.Cool 并确认驱动已安装。")
        return buf.raw[:result]

    def set_report(self, payload):
        self._control(0x21, 0x09, 0x0300, bytes(payload[:8]))

    def get_report(self):
        return self._control(0xA1, 0x01, 0x0300, b"")

    def xread(self, addr):
        self.set_report([0xB5, addr >> 8, addr & 0xFF])
        reply = self.get_report()
        if len(reply) < 4:
            raise RuntimeError("设备返回了不完整的 HID 报文。")
        return reply[3]

    def xwrite(self, addr, value):
        self.set_report([0xB6, addr >> 8, addr & 0xFF, value])

    def cmd(self, *payload, settle=0.02, status=None):
        self.set_report(payload)
        time.sleep(settle)
        if status is not None:
            deadline = time.time() + 1.5
            while time.time() < deadline:
                if self.xread(status) == 0:
                    return
                time.sleep(0.005)
            raise RuntimeError(f"屏幕命令未完成（状态寄存器 0x{status:04X}）。")

    def initialize(self):
        chip = bytes(self.xread(0xF000 + i) for i in range(3))
        chip_ext = bytes(self.xread(0xFF00 + i) for i in range(3))
        port = self.xread(49)
        is_9132 = chip_ext[1:] == b"\x13\x0a"
        if not is_9132 and chip[1:] != b"\x16\x0a":
            raise RuntimeError(f"这不是已识别的 MS912x 屏幕控制器（芯片标记 {chip.hex(' ')}）。")
        if is_9132:
            status_xfer, status_in, status_out = 0x1026, 0x1123, 0x1125
            self._status_video_on = 0x118D
        else:
            status_xfer, status_in, status_out = 0xC558, 0xC555, 0xC557
            self._status_video_on = 0xC555
        self.cmd(0xA6, 0x03, 0x03, status=status_xfer, settle=0.01)
        self.xwrite(0xDEEE, 1)
        self.cmd(0xA6, 0x07, 0x01, status=0xC454, settle=0.1)
        self.cmd(0xA6, 0x05, 0x00, status=status_in, settle=0.01)
        self._is_9132 = is_9132
        self._set_screen_gate(False, port)
        self.cmd(0xA6, 0x01, 0x01, WIDTH >> 8, WIDTH & 0xFF, HEIGHT >> 8, HEIGHT & 0xFF,
                 COLOR_BGR888, 0, status=status_in, settle=0.01)
        self.cmd(0xA6, 0x02, VIC, 0, WIDTH >> 8, WIDTH & 0xFF, HEIGHT >> 8, HEIGHT & 0xFF,
                 status=status_out, settle=0.02)
        self.cmd(0xA6, 0x04, 0x01, status=status_in, settle=0.01)
        self._initialized = True
        self._port = port

    def _set_screen_gate(self, on, port):
        if self._is_9132 and port == 5:
            addr, bit, invert = 0xFB07, 0x02, True
        elif self._is_9132:
            addr, bit, invert = 0xF037, 0x01, False
        elif port == 6:
            addr, bit, invert = 0xF005, 0x10, False
        elif port == 5:
            addr, bit, invert = 0xF507, 0x02, True
        elif port == 2:
            addr, bit, invert = 0xF004, 0x80, False
        elif port == 3:
            addr, bit, invert = 0xF030, 0x01, False
        else:
            addr, bit, invert = 0xF004, 0x02, False
        current = self.xread(addr)
        value = (current & ~bit) if (on == invert) else (current | bit)
        if value != current:
            self.xwrite(addr, value)

    def send_frame(self, rgb):
        if not self._initialized:
            self.initialize()
        if len(rgb) != WIDTH * HEIGHT * 3:
            raise ValueError("解码的视频帧尺寸不符。")
        bgr = bytearray(len(rgb))
        bgr[0::3], bgr[1::3], bgr[2::3] = rgb[2::3], rgb[1::3], rgb[0::3]
        bgr = bgr.replace(b"\xff", b"\xfe")
        header = bytes((0xFF, 0, 0, 0, 0, WIDTH >> 4, ((WIDTH & 15) << 4) | (HEIGHT >> 8), HEIGHT & 255))
        packet = header + bgr + b"\xff\xc0\0\0\0\0\0\0"
        offset = 0
        while offset < len(packet):
            part = packet[offset:offset + CHUNK]
            n = self.lib.usb_bulk_write(self.handle, 0x04, ctypes.cast(ctypes.create_string_buffer(part), c_char_p), len(part), 10000)
            if n <= 0:
                raise RuntimeError(f"视频帧写入失败，偏移 {offset}。")
            offset += n
        self.lib.usb_bulk_write(self.handle, 0x04, ctypes.cast(ctypes.create_string_buffer(b""), c_char_p), 0, 10000)
        self._fid ^= 1
        self.set_report([0xA6, 0x00, self._fid, 100, 0, 0, 0, 0])
        if not getattr(self, "_output", False):
            self._set_screen_gate(True, self._port)
            self.cmd(0xA6, 0x05, 0x01, status=self._status_video_on, settle=0.01)
            self._output = True


ROOT = RESOURCE_ROOT
STOP_EVENT = threading.Event()
STATE_LOCK = threading.Lock()
APP_DATA = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ValkyrieV360"
DEFAULT_LIBRARY_ROOT = APP_DATA / "library"
LIBRARY_SETTINGS = APP_DATA / "library-location.json"
try:
    _configured_library = json.loads(LIBRARY_SETTINGS.read_text(encoding="utf-8")).get("path", "")
    _configured_library = Path(_configured_library).expanduser()
    LIBRARY_ROOT = _configured_library.resolve() if _configured_library.is_absolute() else DEFAULT_LIBRARY_ROOT
except (OSError, ValueError, TypeError, AttributeError):
    LIBRARY_ROOT = DEFAULT_LIBRARY_ROOT
LIBRARY_INDEX = APP_DATA / "library.json"
LIBRARY_LOCK = threading.RLock()
LIBRARY_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".flv", ".mpeg", ".mpg", ".ts", ".3gp"}
MAX_VIDEO_BYTES = 1024 * 1024 * 1024
PLAYBACK_FPS = 10  # The vendor frame trigger carries a 100 ms delay.
STATE = {
    "status": "控件服务已启动，选择视频后即可写入。", "active": False,
    "device_connected": False, "current_video": "", "frames_sent": 0,
    "elapsed_seconds": 0, "fps": PLAYBACK_FPS, "last_error": "", "started_at": time.time(),
}
CURRENT = {"thread": None, "process": None, "library_id": None}


def set_status(status, active=None, **updates):
    with STATE_LOCK:
        STATE["status"] = status
        if active is not None:
            STATE["active"] = active
        if "发送失败：" in status:
            STATE["last_error"] = status.removeprefix("发送失败：")
        STATE.update(updates)


def _library_read_unlocked():
    LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        raw = json.loads(LIBRARY_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(raw, list):
        return []
    items = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("id", ""))
        name = str(item.get("name", ""))
        suffix = Path(name).suffix.lower()
        if not re.fullmatch(r"[0-9a-f]{32}", video_id) or suffix not in LIBRARY_EXTENSIONS:
            continue
        path = LIBRARY_ROOT / f"{video_id}{suffix}"
        if not path.is_file():
            continue
        items.append({
            "id": video_id, "name": name[:180], "size": path.stat().st_size,
            "added": str(item.get("added", "")),
        })
    return sorted(items, key=lambda item: item["added"], reverse=True)


def get_library_items():
    with LIBRARY_LOCK:
        return _library_read_unlocked()


def save_library_items(items):
    APP_DATA.mkdir(parents=True, exist_ok=True)
    temp_index = APP_DATA / f"library-{uuid.uuid4().hex}.tmp"
    temp_index.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_index, LIBRARY_INDEX)


def get_library_item(video_id):
    if not re.fullmatch(r"[0-9a-f]{32}", video_id or ""):
        return None, None
    with LIBRARY_LOCK:
        for item in _library_read_unlocked():
            if item["id"] == video_id:
                return item, LIBRARY_ROOT / f"{video_id}{Path(item['name']).suffix.lower()}"
    return None, None


def set_library_location(raw_path):
    global LIBRARY_ROOT
    candidate = Path(str(raw_path or "").strip()).expanduser()
    if not candidate.is_absolute():
        raise ValueError("请输入完整的绝对文件夹路径。")
    target = candidate.resolve()
    app_data = APP_DATA.resolve()
    if target == app_data or target == app_data.parent:
        raise ValueError("请选择专用视频文件夹，不能将应用数据目录作为视频库。")
    with LIBRARY_LOCK:
        current = LIBRARY_ROOT.resolve()
        target.mkdir(parents=True, exist_ok=True)
        items = _library_read_unlocked()
        if target != current:
            for item in items:
                suffix = Path(item["name"]).suffix.lower()
                source = current / f"{item['id']}{suffix}"
                destination = target / source.name
                if source != destination:
                    shutil.copy2(source, destination)
                    if destination.stat().st_size != source.stat().st_size:
                        destination.unlink(missing_ok=True)
                        raise OSError(f"复制视频失败：{item['name']}")
        APP_DATA.mkdir(parents=True, exist_ok=True)
        temp_settings = APP_DATA / f"library-location-{uuid.uuid4().hex}.tmp"
        temp_settings.write_text(json.dumps({"path": str(target)}, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp_settings, LIBRARY_SETTINGS)
        LIBRARY_ROOT = target
        return {"path": str(LIBRARY_ROOT), "migrated_count": len(items) if target != current else 0}


def begin_playback(video_path, settings, display_name, library_id=None, delete_after=False):
    with STATE_LOCK:
        thread = CURRENT["thread"]
        if thread and thread.is_alive():
            return False
        STOP_EVENT.clear()
        worker = threading.Thread(
            target=play_video,
            args=(str(video_path), settings, display_name, library_id, delete_after),
            daemon=True,
        )
        CURRENT.update(thread=worker, library_id=library_id)
        STATE.update(
            status="视频已接收，正在连接水冷屏…", active=True,
            device_connected=False, current_video=display_name,
            frames_sent=0, elapsed_seconds=0, fps=settings[3], last_error="",
        )
        worker.start()
        return True


def read_exact(stream, length):
    chunks = bytearray()
    while len(chunks) < length:
        part = stream.read(length - len(chunks))
        if not part:
            return None
        chunks.extend(part)
    return bytes(chunks)


def make_filter(rotation, fit, brightness, fps=PLAYBACK_FPS):
    filters = []
    if rotation == 90:
        filters.append("transpose=clock")
    elif rotation == 180:
        filters.extend(("hflip", "vflip"))
    elif rotation == 270:
        filters.append("transpose=cclock")
    aspect = "decrease" if fit == "contain" else "increase"
    filters.append(f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio={aspect}")
    if aspect == "decrease":
        filters.append(f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:black")
    else:
        filters.append(f"crop={WIDTH}:{HEIGHT}")
    filters.extend((f"eq=brightness={(brightness - 100) / 100:.3f}", f"fps={fps}", "setsar=1"))
    return ",".join(filters)


def play_video(video_path, settings, display_name, library_id=None, delete_after=False):
    device = process = None
    frame_no = 0
    try:
        bundled_ffmpeg = RESOURCE_ROOT / "runtime" / "ffmpeg.exe"
        ffmpeg = str(bundled_ffmpeg) if bundled_ffmpeg.is_file() else shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("未找到 FFmpeg。请将 ffmpeg.exe 加入 PATH 后重启控件。")
        set_status("正在连接 345F:9132 屏幕…", True, current_video=display_name)
        device = MSDisplay()
        set_status("屏幕 USB 会话已连接，正在初始化 320×240 模式…", device_connected=True)
        set_status("屏幕已连接，正在初始化 320×240 模式…")
        device.initialize()
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-stream_loop", "-1", "-i", str(video_path),
                   "-vf", make_filter(*settings), "-an", "-sn", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = subprocess.SW_HIDE
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   bufsize=WIDTH * HEIGHT * 3 * 2,
                                   startupinfo=startupinfo,
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        with STATE_LOCK:
            CURRENT["process"] = process
        set_status("正在向屏幕写入视频帧…")
        fps = settings[3]
        frame_period = 1 / fps
        next_frame_time = time.monotonic()
        while not STOP_EVENT.is_set():
            frame = read_exact(process.stdout, WIDTH * HEIGHT * 3)
            if not frame:
                if STOP_EVENT.is_set():
                    break
                code = process.poll()
                if code is not None and code != 0:
                    err = process.stderr.read().decode("utf-8", "replace").strip()[-1000:]
                    raise RuntimeError(err or f"FFmpeg 已退出（代码 {code}），没有输出视频帧。")
                break
            device.send_frame(frame)
            frame_no += 1
            next_frame_time += frame_period
            delay = next_frame_time - time.monotonic()
            if delay > 0:
                STOP_EVENT.wait(delay)
            else:
                next_frame_time = time.monotonic()
            if frame_no % fps == 0:
                set_status(f"正在播放并写入视频帧… {frame_no // fps} 秒",
                           frames_sent=frame_no, elapsed_seconds=frame_no // fps)
        if STOP_EVENT.is_set():
            set_status("发送已停止，屏幕保留最后一帧。", False,
                       device_connected=False, frames_sent=frame_no, elapsed_seconds=frame_no // fps)
        else:
            set_status("视频播放结束，屏幕保留最后一帧。", False,
                       device_connected=False, frames_sent=frame_no, elapsed_seconds=frame_no // settings[3])
    except Exception as exc:
        set_status(f"发送失败：{exc}", False, device_connected=False,
                   frames_sent=frame_no, elapsed_seconds=frame_no // settings[3])
        print(f"[video-error] {exc}")
    finally:
        if process:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
        if device:
            device.close()
        if delete_after:
            try:
                Path(video_path).unlink(missing_ok=True)
            except OSError:
                pass
        with STATE_LOCK:
            CURRENT["process"] = None
            CURRENT["thread"] = None
            CURRENT["library_id"] = None
            STATE["active"] = False
            STATE["device_connected"] = False
        STOP_EVENT.clear()


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt, *args):
        print("[http] " + fmt % args)

    def send_json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def local_origin_only(self):
        origin = self.headers.get("Origin")
        return not origin or origin.rstrip("/") == "http://127.0.0.1:8765"

    def send_library_video(self, video_id):
        item, video_path = get_library_item(video_id)
        if not item or not video_path:
            return self.send_json(404, {"ok": False, "error": "视频库中找不到该视频。"})
        size = video_path.stat().st_size
        start, end, status = 0, size - 1, 200
        range_header = self.headers.get("Range")
        if range_header:
            match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
            if not match or not (match.group(1) or match.group(2)):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            if match.group(1):
                start = int(match.group(1))
                end = int(match.group(2)) if match.group(2) else size - 1
            else:
                suffix_length = int(match.group(2))
                start = max(0, size - suffix_length)
            if start >= size or end < start:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            end = min(end, size - 1)
            status = 206
        length = end - start + 1
        content_type = mimetypes.guess_type(item["name"])[0] or "application/octet-stream"
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition", "inline; filename*=UTF-8''" + urllib.parse.quote(item["name"]))
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        try:
            with video_path.open("rb") as source:
                source.seek(start)
                remaining = length
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def read_video_upload(self, length, target):
        with target.open("wb") as output:
            remaining = length
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ConnectionError("视频上传中断。")
                output.write(chunk)
                remaining -= len(chunk)

    def parsed_settings(self):
        rotation = int(self.headers.get("X-Rotation", "0"))
        fit = self.headers.get("X-Fit", "cover")
        brightness = int(self.headers.get("X-Brightness", "100"))
        fps = int(self.headers.get("X-Fps", str(PLAYBACK_FPS)))
        if rotation not in (0, 90, 180, 270) or fit not in ("cover", "contain"):
            raise ValueError("画面设置无效。")
        if fps < 1 or fps > 30:
            raise ValueError("视频帧率必须在 1 到 30 FPS 之间。")
        return rotation, fit, max(30, min(150, brightness)), fps

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/status":
            with STATE_LOCK:
                body = dict(STATE)
            body["uptime_seconds"] = int(time.time() - body.pop("started_at", time.time()))
            return self.send_json(200, body)
        if path == "/api/library":
            items = get_library_items()
            return self.send_json(200, {"items": items, "count": len(items),
                                        "total_bytes": sum(item["size"] for item in items),
                                        "path": str(LIBRARY_ROOT)})
        if path == "/api/settings/library":
            return self.send_json(200, {"path": str(LIBRARY_ROOT)})
        if path.startswith("/api/library/file/"):
            video_id = path.rsplit("/", 1)[-1]
            return self.send_library_video(video_id)
        if path == "/api/stop":
            STOP_EVENT.set()
            return self.send_json(200, {"ok": True, "status": "正在停止发送…"})
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.local_origin_only():
            return self.send_json(403, {"ok": False, "error": "只允许本机控件页面访问。"})
        if path == "/api/library":
            return self.save_uploaded_video()
        if path == "/api/settings/library":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 8192:
                    raise ValueError("文件夹路径无效。")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("视频库设置格式无效。")
                result = set_library_location(payload.get("path", ""))
                return self.send_json(200, {"ok": True, **result})
            except (UnicodeDecodeError, ValueError, OSError, TypeError) as exc:
                return self.send_json(400, {"ok": False, "error": str(exc) or "无法设置视频库位置。"})
        if path == "/api/write-library":
            return self.write_library_video()
        if path != "/api/write":
            return self.send_json(404, {"ok": False, "error": "未知接口"})
        with STATE_LOCK:
            thread = CURRENT["thread"]
            if thread and thread.is_alive():
                return self.send_json(409, {"ok": False, "error": "当前已有视频正在写入，请先停止。"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_VIDEO_BYTES:
                return self.send_json(413, {"ok": False, "error": "视频文件为空或超过 1 GB。"})
            settings = self.parsed_settings()
            raw_name = urllib.parse.unquote(self.headers.get("X-Filename", "video.mp4"))
            display_name = Path(raw_name.replace("\\", "/")).name
            suffix = Path(display_name).suffix.lower()
            if suffix not in LIBRARY_EXTENSIONS:
                suffix = ".mp4"
            fd, tmp_name = tempfile.mkstemp(prefix="mythcool-", suffix=suffix)
            with os.fdopen(fd, "wb") as output:
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise ConnectionError("视频上传中断。")
                    output.write(chunk)
                    remaining -= len(chunk)
            display_name = "".join(ch for ch in display_name if ord(ch) >= 32 and ch != "\x7f")[:180] or "video.mp4"
            if not begin_playback(tmp_name, settings, display_name, delete_after=True):
                Path(tmp_name).unlink(missing_ok=True)
                return self.send_json(409, {"ok": False, "error": "当前已有视频正在写入，请先停止。"})
            return self.send_json(202, {"ok": True, "status": "视频已接收，正在连接水冷屏…"})
        except Exception as exc:
            try:
                if "tmp_name" in locals():
                    Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass
            return self.send_json(400, {"ok": False, "error": str(exc)})

    def save_uploaded_video(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > MAX_VIDEO_BYTES:
            return self.send_json(413, {"ok": False, "error": "视频文件为空或超过 1 GB。"})
        raw_name = urllib.parse.unquote(self.headers.get("X-Filename", "video.mp4"))
        name = Path(raw_name.replace("\\", "/")).name
        name = "".join(ch for ch in name if ord(ch) >= 32 and ch != "\x7f")[:180]
        suffix = Path(name).suffix.lower()
        if not name or suffix not in LIBRARY_EXTENSIONS:
            return self.send_json(415, {"ok": False, "error": "不支持此视频格式。请选择常见视频文件。"})
        video_id = uuid.uuid4().hex
        video_path = LIBRARY_ROOT / f"{video_id}{suffix}"
        temp_path = LIBRARY_ROOT / f"{video_id}.part"
        try:
            with LIBRARY_LOCK:
                LIBRARY_ROOT.mkdir(parents=True, exist_ok=True)
                self.read_video_upload(length, temp_path)
                os.replace(temp_path, video_path)
                item = {"id": video_id, "name": name, "size": length,
                        "added": time.strftime("%Y-%m-%d %H:%M:%S")}
                items = [item] + _library_read_unlocked()
                save_library_items(items)
            return self.send_json(201, {"ok": True, "item": item})
        except Exception as exc:
            temp_path.unlink(missing_ok=True)
            video_path.unlink(missing_ok=True)
            return self.send_json(400, {"ok": False, "error": str(exc)})

    def write_library_video(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 4096:
            return self.send_json(400, {"ok": False, "error": "视频库请求内容无效。"})
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            video_id = str(payload.get("id", ""))
            settings = self.parsed_settings()
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            return self.send_json(400, {"ok": False, "error": str(exc) or "视频库请求内容无效。"})
        item, video_path = get_library_item(video_id)
        if not item or not video_path:
            return self.send_json(404, {"ok": False, "error": "视频库中找不到该视频。"})
        if not begin_playback(video_path, settings, item["name"], video_id):
            return self.send_json(409, {"ok": False, "error": "当前已有视频正在写入，请先停止。"})
        return self.send_json(202, {"ok": True, "status": "正在从视频库读取并写入水冷屏…"})

    def do_DELETE(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.local_origin_only():
            return self.send_json(403, {"ok": False, "error": "只允许本机控件页面访问。"})
        if not path.startswith("/api/library/"):
            return self.send_json(404, {"ok": False, "error": "未知接口"})
        video_id = path.rsplit("/", 1)[-1]
        with STATE_LOCK:
            if CURRENT.get("library_id") == video_id and STATE.get("active"):
                return self.send_json(409, {"ok": False, "error": "该视频正在写入，请停止发送后再删除。"})
        with LIBRARY_LOCK:
            items = _library_read_unlocked()
            item = next((item for item in items if item["id"] == video_id), None)
            if not item:
                return self.send_json(404, {"ok": False, "error": "视频库中找不到该视频。"})
            video_path = LIBRARY_ROOT / f"{video_id}{Path(item['name']).suffix.lower()}"
            video_path.unlink(missing_ok=True)
            save_library_items([entry for entry in items if entry["id"] != video_id])
        return self.send_json(200, {"ok": True, "id": video_id})


class TrayIcon:
    """Small native Windows notification-area icon for the hidden server."""
    WM_TRAY = 0x8000 + 21
    WM_COMMAND = 0x0111
    WM_DESTROY = 0x0002
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    NIM_ADD, NIM_DELETE, NIM_SETVERSION = 0, 2, 4
    NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4
    MF_STRING, MF_SEPARATOR = 0, 0x0800
    TPM_RIGHTBUTTON, TPM_RETURNCMD = 2, 0x0100
    ID_OPEN, ID_EXIT = 1001, 1002

    class Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class NotifyData(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_uint), ("hWnd", ctypes.c_void_p), ("uID", ctypes.c_uint),
            ("uFlags", ctypes.c_uint), ("uCallbackMessage", ctypes.c_uint),
            ("hIcon", ctypes.c_void_p), ("szTip", ctypes.c_wchar * 128),
            ("dwState", ctypes.c_uint), ("dwStateMask", ctypes.c_uint),
            ("szInfo", ctypes.c_wchar * 256), ("uTimeout", ctypes.c_uint),
            ("szInfoTitle", ctypes.c_wchar * 64), ("dwInfoFlags", ctypes.c_uint),
            ("guidItem", ctypes.c_byte * 16), ("hBalloonIcon", ctypes.c_void_p),
        ]

    class WindowClass(ctypes.Structure):
        pass

    def __init__(self, server):
        from ctypes import wintypes as wt
        self.server = server
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        self.kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        self.hinstance = self.kernel32.GetModuleHandleW(None)
        self.class_name = "ValkyrieV360WaterCoolTray"
        self.WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_void_p, ctypes.c_uint,
                                          ctypes.c_size_t, ctypes.c_ssize_t)
        self.WindowClass._fields_ = [
            ("style", ctypes.c_uint), ("lpfnWndProc", self.WNDPROC),
            ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
            ("hInstance", ctypes.c_void_p), ("hIcon", ctypes.c_void_p),
            ("hCursor", ctypes.c_void_p), ("hbrBackground", ctypes.c_void_p),
            ("lpszMenuName", ctypes.c_wchar_p), ("lpszClassName", ctypes.c_wchar_p),
        ]
        self.user32.RegisterClassW.argtypes = [ctypes.POINTER(self.WindowClass)]
        self.user32.RegisterClassW.restype = ctypes.c_ushort
        self.user32.CreateWindowExW.argtypes = [ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_wchar_p,
            ctypes.c_uint, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        self.user32.CreateWindowExW.restype = ctypes.c_void_p
        self.user32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        self.user32.LoadIconW.restype = ctypes.c_void_p
        self._wndproc = self.WNDPROC(self._dispatch)
        wc = self.WindowClass()
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = self.hinstance
        wc.lpszClassName = self.class_name
        if not self.user32.RegisterClassW(ctypes.byref(wc)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.hwnd = self.user32.CreateWindowExW(0, self.class_name, "Valkyrie V360 水冷屏控件",
                                                0, 0, 0, 0, 0, None, None, self.hinstance, None)
        if not self.hwnd:
            raise ctypes.WinError(ctypes.get_last_error())
        self.data = self.NotifyData()
        self.data.cbSize = ctypes.sizeof(self.NotifyData)
        self.data.hWnd = self.hwnd
        self.data.uID = 1
        self.data.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
        self.data.uCallbackMessage = self.WM_TRAY
        self.data.hIcon = self.user32.LoadIconW(None, ctypes.cast(ctypes.c_void_p(32512), ctypes.c_wchar_p))
        self.data.szTip = "Valkyrie V360 水冷屏控件（后台运行）"
        self.shell32.Shell_NotifyIconW.argtypes = [ctypes.c_uint, ctypes.POINTER(self.NotifyData)]
        self.shell32.Shell_NotifyIconW.restype = ctypes.c_int
        if not self.shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(self.data)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.user32.CreatePopupMenu.restype = ctypes.c_void_p
        self.user32.AppendMenuW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_wchar_p]
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(self.Point)]
        self.user32.TrackPopupMenu.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
                                               ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p]
        self.user32.TrackPopupMenu.restype = ctypes.c_uint
        self.user32.DestroyMenu.argtypes = [ctypes.c_void_p]
        self.user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        self.user32.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        self.user32.DefWindowProcW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]
        self.user32.DefWindowProcW.restype = ctypes.c_ssize_t
        self.user32.PostQuitMessage.argtypes = [ctypes.c_int]

    def _open_page(self):
        page = "http://127.0.0.1:8765/" + urllib.parse.quote("水冷控件简版.html")
        webbrowser.open(page)

    def _show_menu(self):
        menu = self.user32.CreatePopupMenu()
        self.user32.AppendMenuW(menu, self.MF_STRING, self.ID_OPEN, "打开水冷屏控件")
        self.user32.AppendMenuW(menu, self.MF_SEPARATOR, 0, None)
        self.user32.AppendMenuW(menu, self.MF_STRING, self.ID_EXIT, "退出后台服务")
        point = self.Point()
        self.user32.GetCursorPos(ctypes.byref(point))
        self.user32.SetForegroundWindow(self.hwnd)
        command = self.user32.TrackPopupMenu(menu, self.TPM_RIGHTBUTTON | self.TPM_RETURNCMD,
                                             point.x, point.y, 0, self.hwnd, None)
        self.user32.DestroyMenu(menu)
        self.user32.PostMessageW(self.hwnd, 0, 0, 0)
        if command == self.ID_OPEN:
            self._open_page()
        elif command == self.ID_EXIT:
            self.user32.PostQuitMessage(0)

    def _dispatch(self, hwnd, message, wparam, lparam):
        if message == self.WM_TRAY:
            event = lparam & 0xFFFF
            if event == self.WM_LBUTTONDBLCLK:
                self._open_page()
            elif event == self.WM_RBUTTONUP:
                self._show_menu()
            return 0
        if message == self.WM_COMMAND and (wparam & 0xFFFF) == self.ID_EXIT:
            self.user32.PostQuitMessage(0)
            return 0
        if message == self.WM_DESTROY:
            self.shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self.data))
            self.user32.PostQuitMessage(0)
            return 0
        pointer_mask = (1 << (ctypes.sizeof(ctypes.c_void_p) * 8)) - 1
        return self.user32.DefWindowProcW(hwnd, message, wparam, ctypes.c_void_p(lparam & pointer_mask))

    def run(self):
        from ctypes import wintypes as wt
        self.user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint]
        self.user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
        self.user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
        message = wt.MSG()
        while self.user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            self.user32.TranslateMessage(ctypes.byref(message))
            self.user32.DispatchMessageW(ctypes.byref(message))
        self.shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(self.data))


def main():
    if "--install-startup" in sys.argv:
        import winreg
        exe_path = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve()
        command = f'"{exe_path}" --background'
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            winreg.SetValueEx(key, "ValkyrieV360WaterCool", 0, winreg.REG_SZ, command)
        print("已设置当前用户登录自启：" + command)
        return
    if "--remove-startup" in sys.argv:
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as key:
            try:
                winreg.DeleteValue(key, "ValkyrieV360WaterCool")
            except FileNotFoundError:
                pass
        print("已移除当前用户登录自启项。")
        return
    if "--background" in sys.argv:
        log_dir = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "ValkyrieV360"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(log_dir / "control.log", "a", encoding="utf-8", buffering=1)
        sys.stdout = log_file
        sys.stderr = log_file
    page = "http://127.0.0.1:8765/" + urllib.parse.quote("水冷控件简版.html")
    background = "--background" in sys.argv
    if not background:
        try:
            urllib.request.urlopen("http://127.0.0.1:8765/api/status", timeout=0.7).close()
        except Exception:
            pass
        else:
            webbrowser.open(page)
            return
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 8765), Handler)
    server.daemon_threads = True
    print("V360 MERLIN 水冷屏控件已启动：" + page)
    print("仅监听本机回环地址；右键任务栏通知区图标可打开控件或退出后台服务。")
    serve_thread = None
    try:
        tray = TrayIcon(server)
    except Exception as exc:
        print(f"[tray-error] 无法创建任务栏图标：{exc}")
        if not background:
            threading.Timer(0.8, lambda: webbrowser.open(page)).start()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    else:
        serve_thread = threading.Thread(target=server.serve_forever, name="watercool-http", daemon=True)
        serve_thread.start()
        if not background:
            threading.Timer(0.8, lambda: webbrowser.open(page)).start()
        tray.run()
    finally:
        STOP_EVENT.set()
        with STATE_LOCK:
            process = CURRENT["process"]
            worker = CURRENT["thread"]
        if process and process.poll() is None:
            process.terminate()
        if worker and worker.is_alive():
            worker.join(timeout=3)
        if getattr(locals().get("tray", None), "hwnd", None):
            tray.user32.DestroyWindow(tray.hwnd)
        if serve_thread and serve_thread.is_alive():
            server.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
