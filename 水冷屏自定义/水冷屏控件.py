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
            raise RuntimeError("æ²¡æ‰¾åˆ° Myth.Cool å®‰è£…çš„ libusb0.dllã€‚è¯·å…ˆè¿è¡Œä¸€æ¬¡å®˜æ–¹ Myth.Cool å®‰è£…é©±åŠ¨ã€‚")
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
            raise RuntimeError("æœªæ‰¾åˆ°æ°´å†·å± USB è®¾å¤‡ 345F:9132ã€‚è¯·æ£€æŸ¥æ°´å†·å± USB çº¿ï¼Œå¹¶å…³é—­ Myth.Coolã€‚")
        self.lib.usb_set_configuration(self.handle, 1)
        # Interface 3 is bound by the bundled MS USB Display driver; interface 0
        # remains the HID control plane used by the vendor protocol.
        if self.lib.usb_claim_interface(self.handle, 3) != 0:
            detail = self.lib.usb_strerror()
            message = detail.decode("mbcs", "replace") if detail else "æœªçŸ¥é”™è¯¯"
            self.lib.usb_close(self.handle)
            self.handle = None
            raise RuntimeError(f"æ— æ³•å ç”¨å±å¹•è§†é¢‘æ¥å£ 3ï¼š{message}ã€‚è¯·å…³é—­ Myth.Coolã€‚")
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
            raise RuntimeError(f"USB æ§åˆ¶ä¼ è¾“å¤±è´¥ï¼ˆ{result}ï¼‰ï¼›è¯·å…ˆå…³é—­ Myth.Cool å¹¶ç¡®è®¤é©±åŠ¨å·²å®‰è£…ã€‚")
        return buf.raw[:result]

    def set_report(self, payload):
        self._control(0x21, 0x09, 0x0300, bytes(payload[:8]))

    def get_report(self):
        return self._control(0xA1, 0x01, 0x0300, b"")

    def xread(self, addr):
        self.set_report([0xB5, addr >> 8, addr & 0xFF])
        reply = self.get_report()
        if len(reply) < 4:
            raise RuntimeError("è®¾å¤‡è¿”å›äº†ä¸å®Œæ•´çš„ HID æŠ¥æ–‡ã€‚")
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
            raise RuntimeError(f"å±å¹•å‘½ä»¤æœªå®Œæˆï¼ˆçŠ¶æ€å¯„å­˜å™¨ 0x{status:04X}ï¼‰ã€‚")

    def initialize(self):
        chip = bytes(self.xread(0xF000 + i) for i in range(3))
        chip_ext = bytes(self.xread(0xFF00 + i) for i in range(3))
        port = self.xread(49)
        is_9132 = chip_ext[1:] == b"\x13\x0a"
        if not is_9132 and chip[1:] != b"\x16\x0a":
            raise RuntimeError(f"è¿™ä¸æ˜¯å·²è¯†åˆ«çš„ MS912x å±å¹•æ§åˆ¶å™¨ï¼ˆèŠ¯ç‰‡æ ‡è®° {chip.hex(' ')}ï¼‰ã€‚")
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
            raise ValueError("è§£ç çš„è§†é¢‘å¸§å°ºå¯¸ä¸ç¬¦ã€‚")
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
                raise RuntimeError(f"è§†é¢‘å¸§å†™å…¥å¤±è´¥ï¼Œåç§» {offset}ã€‚")
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
LIBRARY_ROOT = APP_DATA / "library"
LIBRARY_INDEX = APP_DATA / "library.json"
LIBRARY_LOCK = threading.RLock()
LIBRARY_EXTENSIONS = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".webm", ".wmv", ".flv", ".mpeg", ".mpg", ".ts", ".3gp"}
MAX_VIDEO_BYTES = 1024 * 1024 * 1024
STATE = {
    "status": "æ§ä»¶æœåŠ¡å·²å¯åŠ¨ï¼Œé€‰æ‹©è§†é¢‘åå³å¯å†™å…¥ã€‚", "active": False,
    "device_connected": False, "current_video": "", "frames_sent": 0,
    "elapsed_seconds": 0, "last_error": "", "started_at": time.time(),
}
CURRENT = {"thread": None, "process": None, "library_id": None}


def set_status(status, active=None, **updates):
    with STATE_LOCK:
        STATE["status"] = status
        if active is not None:
            STATE["active"] = active
        if "å‘é€å¤±è´¥ï¼š" in status:
            STATE["last_error"] = status.removeprefix("å‘é€å¤±è´¥ï¼š")
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
            status="è§†é¢‘å·²æ¥æ”¶ï¼Œæ­£åœ¨è¿æ¥æ°´å†·å±â€¦", active=True,
            device_connected=False, current_video=display_name,
            frames_sent=0, elapsed_seconds=0, last_error="",
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


def make_filter(rotation, fit, brightness):
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
    filters.extend((f"eq=brightness={(brightness - 100) / 100:.3f}", "fps=15", "setsar=1"))
    return ",".join(filters)


def play_video(video_path, settings, display_name, library_id=None, delete_after=False):
    device = process = None
    frame_no = 0
    try:
        bundled_ffmpeg = RESOURCE_ROOT / "runtime" / "ffmpeg.exe"
        ffmpeg = str(bundled_ffmpeg) if bundled_ffmpeg.is_file() else shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("æœªæ‰¾åˆ° FFmpegã€‚è¯·å°† ffmpeg.exe åŠ å…¥ PATH åé‡å¯æ§ä»¶ã€‚")
        set_status("æ­£åœ¨è¿æ¥ 345F:9132 å±å¹•â€¦", True, current_video=display_name)
        device = MSDisplay()
        set_status("å±å¹• USB ä¼šè¯å·²è¿æ¥ï¼Œæ­£åœ¨åˆå§‹åŒ– 320Ã—240 æ¨¡å¼â€¦", device_connected=True)
        set_status("å±å¹•å·²è¿æ¥ï¼Œæ­£åœ¨åˆå§‹åŒ– 320Ã—240 æ¨¡å¼â€¦")
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
        set_status("æ­£åœ¨å‘å±å¹•å†™å…¥è§†é¢‘å¸§â€¦")
        frame_period = 1 / 15
        next_frame_time = time.monotonic()
        while not STOP_EVENT.is_set():
            frame = read_exact(process.stdout, WIDTH * HEIGHT * 3)
            if not frame:
                if STOP_EVENT.is_set():
                    break
                code = process.poll()
                if code is not None and code != 0:
                    err = process.stderr.read().decode("utf-8", "replace").strip()[-1000:]
                    raise RuntimeError(err or f"FFmpeg å·²é€€å‡ºï¼ˆä»£ç  {code}ï¼‰ï¼Œæ²¡æœ‰è¾“å‡ºè§†é¢‘å¸§ã€‚")
                break
            device.send_frame(frame)
            frame_no += 1
            next_frame_time += frame_period
            delay = next_frame_time - time.monotonic()
            if delay > 0:
                STOP_EVENT.wait(delay)
            else:
                next_frame_time = time.monotonic()
            if frame_no % 15 == 0:
                set_status(f"æ­£åœ¨æ’­æ”¾å¹¶å†™å…¥è§†é¢‘å¸§â€¦ {frame_no // 15} ç§’",
                           frames_sent=frame_no, elapsed_seconds=frame_no // 15)
        if STOP_EVENT.is_set():
            set_status("å‘é€å·²åœæ­¢ï¼Œå±å¹•ä¿ç•™æœ€åä¸€å¸§ã€‚", False,
                       device_connected=False, frames_sent=frame_no, elapsed_seconds=frame_no // 15)
        else:
            set_status("è§†é¢‘æ’­æ”¾ç»“æŸï¼Œå±å¹•ä¿ç•™æœ€åä¸€å¸§ã€‚", False,
                       device_connected=False, frames_sent=frame_no, elapsed_seconds=frame_no // 15)
    except Exception as exc:
        set_status(f"å‘é€å¤±è´¥ï¼š{exc}", False, device_connected=False,
                   frames_sent=frame_no, elapsed_seconds=frame_no // 15)
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
            return self.send_json(404, {"ok": False, "error": "è§†é¢‘åº“ä¸­æ‰¾ä¸åˆ°è¯¥è§†é¢‘ã€‚"})
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
                    raise ConnectionError("è§†é¢‘ä¸Šä¼ ä¸­æ–­ã€‚")
                output.write(chunk)
                remaining -= len(chunk)

    def parsed_settings(self):
        rotation = int(self.headers.get("X-Rotation", "0"))
        fit = self.headers.get("X-Fit", "cover")
        brightness = int(self.headers.get("X-Brightness", "100"))
        if rotation not in (0, 90, 180, 270) or fit not in ("cover", "contain"):
            raise ValueError("ç”»é¢è®¾ç½®æ— æ•ˆã€‚")
        return rotation, fit, max(30, min(150, brightness))

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
                                        "total_bytes": sum(item["size"] for item in items)})
        if path.startswith("/api/library/file/"):
            video_id = path.rsplit("/", 1)[-1]
            return self.send_library_video(video_id)
        if path == "/api/stop":
            STOP_EVENT.set()
            return self.send_json(200, {"ok": True, "status": "æ­£åœ¨åœæ­¢å‘é€â€¦"})
        return super().do_GET()

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if not self.local_origin_only():
            return self.send_json(403, {"ok": False, "error": "åªå…è®¸æœ¬æœºæ§ä»¶é¡µé¢è®¿é—®ã€‚"})
        if path == "/api/library":
            return self.save_uploaded_video()
        if path == "/api/write-library":
            return self.write_library_video()
        if path != "/api/write":
            return self.send_json(404, {"ok": False, "error": "æœªçŸ¥æ¥å£"})
        with STATE_LOCK:
            thread = CURRENT["thread"]
            if thread and thread.is_alive():
                return self.send_json(409, {"ok": False, "error": "å½“å‰å·²æœ‰è§†é¢‘æ­£åœ¨å†™å…¥ï¼Œè¯·å…ˆåœæ­¢ã€‚"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > MAX_VIDEO_BYTES:
                return self.send_json(413, {"ok": False, "error": "è§†é¢‘æ–‡ä»¶ä¸ºç©ºæˆ–è¶…è¿‡ 1 GBã€‚"})
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
                        raise ConnectionError("è§†é¢‘ä¸Šä¼ ä¸­æ–­ã€‚")
                    output.write(chunk)
                    remaining -= len(chunk)
            display_name = "".join(ch for ch in display_name if ord(ch) >= 32 and ch != "\x7f")[:180] or "video.mp4"
            if not begin_playback(tmp_name, settings, display_name, delete_after=True):
                Path(tmp_name).unlink(missing_ok=True)
                return self.send_json(409, {"ok": False, "error": "å½“å‰å·²æœ‰è§†é¢‘æ­£åœ¨å†™å…¥ï¼Œè¯·å…ˆåœæ­¢ã€‚"})
            return self.send_json(202, {"ok": True, "status": "è§†é¢‘å·²æ¥æ”¶ï¼Œæ­£åœ¨è¿æ¥æ°´å†·å±â€¦"})
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
            return self.send_json(413, {"ok": False, "error": "è§†é¢‘æ–‡ä»¶ä¸ºç©ºæˆ–è¶…è¿‡ 1 GBã€‚"})
        raw_name = urllib.parse.unquote(self.headers.get("X-Filename", "video.mp4"))
        name = Path(raw_name.replace("\\", "/")).name
        name = "".join(ch for ch in name if ord(ch) >= 32 and ch != "\x7f")[:180]
        suffix = Path(name).suffix.lower()
        if not name or suffix not in LIBRARY_EXTENSIONS:
            return self.send_json(415, {"ok": False, "error": "ä¸æ”¯æŒæ­¤è§†é¢‘æ ¼å¼ã€‚è¯·é€‰æ‹©å¸¸è§è§†é¢‘æ–‡ä»¶ã€‚"})
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
        6ó¾­¢G§²ÚîÆ­yØÛÛ[X[™OHÙ[‹’QÑVU‚ˆÙ[‹\Ù\ŒÌ‹”Üİ]Z]Y\ÜØYÙJ
B‚ˆYˆÙ\Ü]Ú
Ù[‹Û™Y\ÜØYÙKÜ\˜[K\˜[JN‚ˆYˆY\ÜØYÙHOHÙ[‹•ÓWÕVN‚ˆ]™[H\˜[H	ˆ‘‘‘‚ˆYˆ]™[OHÙ[‹•ÓWÓ•UÓ‘“ÓÎ‚ˆÙ[‹—ÛÜ[—ÜYÙJ
Bˆ[Yˆ]™[OHÙ[‹•ÓWÔ•UÓ•T‚ˆÙ[‹—ÜÚİ×ÛY[J
Bˆ™]\›ˆˆYˆY\ÜØYÙHOHÙ[‹•ÓWĞÓÓSPS‘[™
Ü\˜[H	ˆ‘‘‘ŠHOHÙ[‹’QÑVU‚ˆÙ[‹\Ù\ŒÌ‹”Üİ]Z]Y\ÜØYÙJ
Bˆ™]\›ˆˆYˆY\ÜØYÙHOHÙ[‹•ÓWÑTÕ“ÖN‚ˆÙ[‹œÚ[Ì‹”Ú[Ó›İYRXÛÛ•ÊÙ[‹“’SWÑSUKİ\\Ë˜\™YŠÙ[‹™]JJBˆÙ[‹\Ù\ŒÌ‹”Üİ]Z]Y\ÜØYÙJ
Bˆ™]\›ˆˆÚ[\—ÛX\ÚÈH
H
İ\\ËœÚ^™[ÙŠİ\\Ë˜×İ›ÚYÜ
H
ˆ
JHHBˆ™]\›ˆÙ[‹\Ù\ŒÌ‹‘Y•Ú[™İÔ›ØÕÊÛ™Y\ÜØYÙKÜ\˜[Kİ\\Ë˜×İ›ÚYÜ
\˜[H	ˆÚ[\—ÛX\ÚÊJB‚ˆYˆ[ŠÙ[ŠN‚ˆœ›ÛHİ\\È[\ÜÚ[\\È\ÈİˆÙ[‹\Ù\ŒÌ‹‘Ù]Y\ÜØYÙUË˜\™İ\\ÈHØİ\\Ë”ÒS•TŠİ“TÑÊKİ\\Ë˜×İ›ÚYÜİ\\Ë˜×İZ[İ\\Ë˜×İZ[BˆÙ[‹\Ù\ŒÌ‹•˜[œÛ]SY\ÜØYÙK˜\™İ\\ÈHØİ\\Ë”ÒS•TŠİ“TÑÊWBˆÙ[‹\Ù\ŒÌ‹‘\Ü]ÚY\ÜØYÙUË˜\™İ\\ÈHØİ\\Ë”ÒS•TŠİ“TÑÊWBˆY\ÜØYÙHHİ“TÑÊ
BˆÚ[HÙ[‹\Ù\ŒÌ‹‘Ù]Y\ÜØYÙUÊİ\\Ë˜\™YŠY\ÜØYÙJK›Û™K
Hˆ‚ˆÙ[‹\Ù\ŒÌ‹•˜[œÛ]SY\ÜØYÙJİ\\Ë˜\™YŠY\ÜØYÙJJBˆÙ[‹\Ù\ŒÌ‹‘\Ü]ÚY\ÜØYÙUÊİ\\Ë˜\™YŠY\ÜØYÙJJBˆÙ[‹œÚ[Ì‹”Ú[Ó›İYRXÛÛ•ÊÙ[‹“’SWÑSUKİ\\Ë˜\™YŠÙ[‹™]JJB‚‚™YˆXZ[Š
N‚ˆYˆ‹KZ[œİ[\İ\\ˆ[ˆŞ\Ë˜\™İ‚ˆ[\ÜÚ[œ™YÂˆ^WÜ]H]
Ş\Ë™^Xİ]X›HYˆÙ]]ŠŞ\Ë™œ›Ş™[ˆ‹˜[ÙJH[ÙH×Ùš[W×ÊKœ™\ÛÛ™J
BˆÛÛ[X[™H‰ÈÙ^WÜ]HˆKX˜XÚÙÜ›İ[™	ÂˆÚ]Ú[œ™YËÜ™X]RÙ^JÚ[œ™YË’ÑVWĞÕT”‘S•ÕTÑT‹ˆ”ÛÙØ\™WZXÜ›ÜÛÙÚ[™İÜ×İ\œ™[™\œÚ[Û—[ˆŠH\ÈÙ^N‚ˆÚ[œ™YË”Ù]˜[YQ^
Ù^K•˜[Ş\šYUŒÍŒØ]\ÛÛÛ‹Ú[œ™YË”‘Q×ÔÖ‹ÛÛ[X[™
Bˆš[
¹mìº+¯¹ïk¹odùbcyå*9¢-ùænùoez!ê¹d+ûï&ˆˆ
ÈÛÛ[X[™
Bˆ™]\›‚ˆYˆ‹K\™[[İ™K\İ\\ˆ[ˆŞ\Ë˜\™İ‚ˆ[\ÜÚ[œ™YÂˆÚ]Ú[œ™YËÜ™X]RÙ^JÚ[œ™YË’ÑVWĞÕT”‘S•ÕTÑT‹ˆ”ÛÙØ\™WZXÜ›ÜÛÙÚ[™İÜ×İ\œ™[™\œÚ[Û—[ˆŠH\ÈÙ^N‚ˆN‚ˆÚ[œ™YË‘[]U˜[YJÙ^K•˜[Ş\šYUŒÍŒØ]\ÛÛÛŠBˆ^Ù\š[S›İ›İ[™\œ›Ü‚ˆ\ÜÂˆš[
¹mì¹éîúfi9odùbcyå*9¢-ùænùoez!ê¹d+úhnxà ˆŠBˆ™]\›‚ˆYˆ‹KX˜XÚÙÜ›İ[™ˆ[ˆŞ\Ë˜\™İ‚ˆÙ×Ù\ˆH]
ÜË™[š\›Û‹™Ù]
“ĞĞSTUH‹]šÛYJ
JJHÈ•˜[Ş\šYUŒÍŒ‚ˆÙ×Ù\‹›ZÙ\Š\™[ÏUYK^\İÛÚÏUYJBˆÙ×Ùš[HHÜ[ŠÙ×Ù\ˆÈ˜ÛÛ›Û›ÙÈ‹˜H‹[˜ÛÙ[™ÏH]‹N‹Y™™\š[™ÏLJBˆŞ\Ëœİİ]HÙ×Ùš[BˆŞ\Ëœİ\œˆHÙ×Ùš[BˆYÙHHš‹ËÌLËŒŒŒNÍKÈˆ
È\›X‹œ\œÙKœ][İJ¹¬-9a­ù£©ù.í¹ë 9âbš[ŠBˆ˜XÚÙÜ›İ[™H‹KX˜XÚÙÜ›İ[™ˆ[ˆŞ\Ë˜\™İ‚ˆYˆ›İ˜XÚÙÜ›İ[™‚ˆN‚ˆ\›X‹œ™\]Y\İ\›Ü[Šš‹ËÌLËŒŒŒNÍKØ\KÜİ]\È‹[Y[İ]LÊK˜ÛÜÙJ
Bˆ^Ù\^Ù\[Û‚ˆ\ÜÂˆ[ÙN‚ˆÙX˜œ›İÜÙ\‹›Ü[ŠYÙJBˆ™]\›‚ˆÙ\™\ˆHœÙ\™\‹•™XY[™ÒÙ\™\Š
ŒLËŒŒŒH‹ÍJK[™\ŠBˆÙ\™\‹™Y[[Û—İ™XYÈHYBˆš[
•ŒÍŒQT“Sˆ9¬-9a­ùlcù£©ù.í¹mì¹d+ùbª;ï&ˆˆ
ÈYÙJBˆš[
¹.áyæäyd+9§+9§.¹fç¹ã«ùg,9g`;ï&ùcìúe+¹.îùb¨y¨#ú`&¹çéyc.¹fï¹¨!ùcëù¢dùo 9£©ù.í¹¢%º` 9aî¹d#¹cì9§#yb¨xà ˆŠBˆÙ\™Wİ™XYH›Û™BˆN‚ˆ˜^HH˜^RXÛÛŠÙ\™\ŠBˆ^Ù\^Ù\[Ûˆ\È^Î‚ˆš[
ˆ–İ˜^KY\œ›Ü—H9¥è9¬åyb&ùnî¹.îùb¨y¨#ùfï¹¨!ûï&Ù^ßHŠBˆYˆ›İ˜XÚÙÜ›İ[™‚ˆ™XY[™Ë•[Y\Š[X™NˆÙX˜œ›İÜÙ\‹›Ü[ŠYÙJJKœİ\

BˆN‚ˆÙ\™\‹œÙ\™WÙ›Ü™]™\Š
Bˆ^Ù\Ù^X›Ø\™[\œ\‚ˆ\ÜÂˆ[ÙN‚ˆÙ\™Wİ™XYH™XY[™Ë•™XY
\™Ù]\Ù\™\‹œÙ\™WÙ›Ü™]™\‹˜[YOHØ]\˜ÛÛÛZ‹Y[[ÛUYJBˆÙ\™Wİ™XYœİ\

BˆYˆ›İ˜XÚÙÜ›İ[™‚ˆ™XY[™Ë•[Y\Š[X™NˆÙX˜œ›İÜÙ\‹›Ü[ŠYÙJJKœİ\

Bˆ˜^Kœ[Š
Bˆš[˜[N‚ˆÕÔÑU‘S•œÙ]

BˆÚ]ÕUWÓĞÒÎ‚ˆ›ØÙ\ÜÈHÕT”‘S•Èœ›ØÙ\ÜÈ—BˆÛÜšÙ\ˆHÕT”‘S•È™XY—BˆYˆ›ØÙ\ÜÈ[™›ØÙ\ÜËœÛ

H\È›Û™N‚ˆ›ØÙ\ÜË\›Z[˜]J
BˆYˆÛÜšÙ\ˆ[™ÛÜšÙ\‹š\×Ø[]™J
N‚ˆÛÜšÙ\‹š›Ú[Š[Y[İ]LÊBˆYˆÙ]]ŠØØ[Ê
K™Ù]
˜^H‹›Û™JKšÛ™‹›Û™JN‚ˆ˜^K\Ù\ŒÌ‹‘\İ›ŞUÚ[™İÊ˜^KšÛ™
BˆYˆÙ\™Wİ™XY[™Ù\™Wİ™XYš\×Ø[]™J
N‚ˆÙ\™\‹œÚ]İÛŠ
BˆÙ\™\‹œÙ\™\—ØÛÜÙJ
B‚‚šYˆ×Û˜[YW×ÈOH—×ÛXZ[—×È‚ˆXZ[Š
B