"""Zero-dependency wire-format Protocol Buffers codec for Flipper Zero RPC (`PB.Main`).

Matches upstream `flipperdevices/flipperzero-protobuf` wire encoding without requiring
`protoc` or `python3-protobuf` (~15 MB saved on microSD).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple


class CommandStatus:
    OK = 0
    ERROR = 1
    ERROR_DECODE = 2
    ERROR_NOT_IMPLEMENTED = 3
    ERROR_BUSY = 4
    ERROR_STORAGE_NOT_READY = 5
    ERROR_STORAGE_EXIST = 6
    ERROR_STORAGE_NOT_EXIST = 7
    ERROR_STORAGE_INVALID_PARAMETER = 8
    ERROR_STORAGE_DENIED = 9
    ERROR_STORAGE_INVALID_NAME = 10
    ERROR_STORAGE_INTERNAL = 11
    ERROR_STORAGE_NOT_IMPLEMENTED = 12
    ERROR_STORAGE_ALREADY_OPEN = 13
    ERROR_CONTINUOUS_COMMAND_INTERRUPTED = 14
    ERROR_INVALID_PARAMETERS = 15
    ERROR_APP_CANT_START = 16
    ERROR_APP_SYSTEM_LOCKED = 17
    ERROR_STORAGE_DIR_NOT_EMPTY = 18
    ERROR_VIRTUAL_DISPLAY_ALREADY_STARTED = 19
    ERROR_VIRTUAL_DISPLAY_NOT_STARTED = 20
    ERROR_APP_NOT_RUNNING = 21
    ERROR_APP_CMD_ERROR = 22

    NAMES: Dict[int, str] = {
        0: "OK",
        1: "ERROR",
        2: "ERROR_DECODE",
        3: "ERROR_NOT_IMPLEMENTED",
        4: "ERROR_BUSY",
        5: "ERROR_STORAGE_NOT_READY",
        6: "ERROR_STORAGE_EXIST",
        7: "ERROR_STORAGE_NOT_EXIST",
        8: "ERROR_STORAGE_INVALID_PARAMETER",
        9: "ERROR_STORAGE_DENIED",
        10: "ERROR_STORAGE_INVALID_NAME",
        11: "ERROR_STORAGE_INTERNAL",
        12: "ERROR_STORAGE_NOT_IMPLEMENTED",
        13: "ERROR_STORAGE_ALREADY_OPEN",
        14: "ERROR_CONTINUOUS_COMMAND_INTERRUPTED",
        15: "ERROR_INVALID_PARAMETERS",
        16: "ERROR_APP_CANT_START",
        17: "ERROR_APP_SYSTEM_LOCKED",
        18: "ERROR_STORAGE_DIR_NOT_EMPTY",
        19: "ERROR_VIRTUAL_DISPLAY_ALREADY_STARTED",
        20: "ERROR_VIRTUAL_DISPLAY_NOT_STARTED",
        21: "ERROR_APP_NOT_RUNNING",
        22: "ERROR_APP_CMD_ERROR",
    }

    @classmethod
    def name(cls, code: int) -> str:
        return cls.NAMES.get(code, f"STATUS_{code}")


# PB.Main oneof `content` field numbers (from upstream flipper.proto)
F_EMPTY = 4
F_SYS_PING_REQ = 5
F_SYS_PING_RESP = 6
F_STORAGE_LIST_REQ = 7
F_STORAGE_LIST_RESP = 8
F_STORAGE_READ_REQ = 9
F_STORAGE_READ_RESP = 10
F_STORAGE_WRITE_REQ = 11
F_STORAGE_DELETE_REQ = 12
F_STORAGE_MKDIR_REQ = 13
F_STORAGE_MD5_REQ = 14
F_STORAGE_MD5_RESP = 15
F_APP_START_REQ = 16
F_APP_LOCK_STATUS_REQ = 17
F_APP_LOCK_STATUS_RESP = 18
F_STOP_SESSION = 19
F_STORAGE_STAT_REQ = 24
F_STORAGE_STAT_RESP = 25
F_STORAGE_INFO_REQ = 28
F_STORAGE_INFO_RESP = 29
F_STORAGE_RENAME_REQ = 30
F_SYS_REBOOT_REQ = 31
F_SYS_DEVICE_INFO_REQ = 32
F_SYS_DEVICE_INFO_RESP = 33
F_SYS_GET_DATETIME_REQ = 35
F_SYS_GET_DATETIME_RESP = 36
F_SYS_SET_DATETIME_REQ = 37
F_SYS_AUDIOVISUAL_ALERT_REQ = 38
F_SYS_PROTO_VER_REQ = 39
F_SYS_PROTO_VER_RESP = 40
F_SYS_POWER_INFO_REQ = 44
F_SYS_POWER_INFO_RESP = 45
F_APP_EXIT_REQ = 47
F_APP_LOAD_FILE_REQ = 48
F_APP_STATE_RESP = 58
F_STORAGE_TIMESTAMP_REQ = 59
F_STORAGE_TIMESTAMP_RESP = 60
F_PROP_GET_REQ = 61
F_PROP_GET_RESP = 62
F_APP_GET_ERROR_REQ = 63
F_APP_GET_ERROR_RESP = 64
F_APP_DATA_EXCHANGE_REQ = 65
F_DESKTOP_IS_LOCKED_REQ = 66
F_GUI_START_STREAM_REQ = 20
F_GUI_STOP_STREAM_REQ = 21
F_GUI_SCREEN_FRAME = 22
F_GUI_SEND_INPUT_REQ = 23

GUI_KEYS: Dict[str, int] = {
    "up": 0,
    "down": 1,
    "right": 2,
    "left": 3,
    "ok": 4,
    "back": 5,
}

GUI_INPUT_TYPES: Dict[str, int] = {
    "press": 0,
    "release": 1,
    "short": 2,
    "long": 3,
    "repeat": 4,
}


# --- Low-level Protobuf Wire Primitives ---


def encode_varint(value: int) -> bytes:
    if value < 0:
        value &= (1 << 64) - 1
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def decode_varint(buf: bytes, pos: int = 0) -> Tuple[int, int]:
    result = 0
    shift = 0
    n = len(buf)
    while True:
        if pos >= n:
            raise ValueError("Truncated varint")
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, pos
        shift += 7
        if shift >= 70:
            raise ValueError("Varint overflow")


def _tag(field_no: int, wire_type: int) -> bytes:
    return encode_varint((field_no << 3) | wire_type)


def _f_varint(field_no: int, val: int) -> bytes:
    if val == 0:
        return b""
    return _tag(field_no, 0) + encode_varint(val)


def _f_bool(field_no: int, val: bool) -> bytes:
    return _tag(field_no, 0) + b"\x01" if val else b""


def _f_bytes(field_no: int, data: bytes, force_empty: bool = False) -> bytes:
    if not data and not force_empty:
        return b""
    return _tag(field_no, 2) + encode_varint(len(data)) + data


def _f_str(field_no: int, text: str) -> bytes:
    if not text:
        return b""
    raw = text.encode("utf-8")
    return _tag(field_no, 2) + encode_varint(len(raw)) + raw


def _f_submsg(field_no: int, body: bytes) -> bytes:
    # Note: oneof submessages MUST emit tag + 0x00 even when body is empty
    return _tag(field_no, 2) + encode_varint(len(body)) + body


def iter_fields(buf: bytes) -> Iterator[Tuple[int, int, int | bytes]]:
    pos = 0
    n = len(buf)
    while pos < n:
        tag_val, pos = decode_varint(buf, pos)
        field_no = tag_val >> 3
        wire_type = tag_val & 0x07
        if wire_type == 0:
            val, pos = decode_varint(buf, pos)
            yield field_no, wire_type, val
        elif wire_type == 1:
            if pos + 8 > n:
                raise ValueError("Truncated fixed64")
            yield field_no, wire_type, buf[pos : pos + 8]
            pos += 8
        elif wire_type == 2:
            ln, pos = decode_varint(buf, pos)
            if pos + ln > n:
                raise ValueError("Truncated length-delimited field")
            yield field_no, wire_type, buf[pos : pos + ln]
            pos += ln
        elif wire_type == 5:
            if pos + 4 > n:
                raise ValueError("Truncated fixed32")
            yield field_no, wire_type, buf[pos : pos + 4]
            pos += 4
        else:
            raise ValueError(f"Unsupported wire type {wire_type}")


# --- Data Models ---


@dataclass
class StorageFile:
    is_dir: bool = False
    name: str = ""
    size: int = 0
    data: bytes = b""
    md5sum: str = ""

    def encode(self) -> bytes:
        return b"".join(
            (
                _f_varint(1, 1 if self.is_dir else 0),
                _f_str(2, self.name),
                _f_varint(3, self.size),
                _f_bytes(4, self.data),
                _f_str(5, self.md5sum),
            )
        )

    @classmethod
    def decode(cls, raw: bytes) -> "StorageFile":
        out = cls()
        for fno, wt, val in iter_fields(raw):
            if fno == 1 and wt == 0:
                out.is_dir = bool(val)
            elif fno == 2 and wt == 2:
                out.name = bytes(val).decode("utf-8", errors="replace")
            elif fno == 3 and wt == 0:
                out.size = int(val)
            elif fno == 4 and wt == 2:
                out.data = bytes(val)
            elif fno == 5 and wt == 2:
                out.md5sum = bytes(val).decode("utf-8", errors="replace")
        return out


@dataclass
class MainEnvelope:
    command_id: int = 0
    command_status: int = 0
    has_next: bool = False
    content_field: int = F_EMPTY
    content_bytes: bytes = b""

    def encode_delimited(self) -> bytes:
        body = b"".join(
            (
                _f_varint(1, self.command_id),
                _f_varint(2, self.command_status),
                _f_bool(3, self.has_next),
                _f_submsg(self.content_field, self.content_bytes),
            )
        )
        return encode_varint(len(body)) + body

    @classmethod
    def decode(cls, raw: bytes) -> "MainEnvelope":
        env = cls()
        for fno, wt, val in iter_fields(raw):
            if fno == 1 and wt == 0:
                env.command_id = int(val)
            elif fno == 2 and wt == 0:
                env.command_status = int(val)
            elif fno == 3 and wt == 0:
                env.has_next = bool(val)
            elif wt == 2 and fno >= 4:
                env.content_field = fno
                env.content_bytes = bytes(val)
        return env


class FrameStreamDecoder:
    """Reassembles varint-delimited `PB.Main` frames across arbitrary BLE GATT chunks."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> List[MainEnvelope]:
        if chunk:
            self._buf.extend(chunk)
        frames: List[MainEnvelope] = []
        while self._buf:
            try:
                msg_len, hdr_end = decode_varint(self._buf, 0)
            except ValueError:
                # Need more bytes to finish varint header
                if len(self._buf) > 10:
                    raise ValueError("Corrupt varint frame prefix from BLE stream")
                break
            if len(self._buf) < hdr_end + msg_len:
                break
            payload = bytes(self._buf[hdr_end : hdr_end + msg_len])
            del self._buf[: hdr_end + msg_len]
            frames.append(MainEnvelope.decode(payload))
        return frames

    def clear(self) -> None:
        self._buf.clear()


# --- Request Builders ---


def build_ping_req(command_id: int, data: bytes = b"z2z") -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_SYS_PING_REQ, _f_bytes(1, data))


def build_proto_version_req(command_id: int) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_SYS_PROTO_VER_REQ, b"")


def build_device_info_req(command_id: int) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_SYS_DEVICE_INFO_REQ, b"")


def build_power_info_req(command_id: int) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_SYS_POWER_INFO_REQ, b"")


def build_audiovisual_alert_req(command_id: int) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_SYS_AUDIOVISUAL_ALERT_REQ, b"")


def build_property_get_req(command_id: int, key: str) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_PROP_GET_REQ, _f_str(1, key))


def build_storage_info_req(command_id: int, path: str = "/ext") -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_STORAGE_INFO_REQ, _f_str(1, path))


def build_storage_stat_req(command_id: int, path: str) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_STORAGE_STAT_REQ, _f_str(1, path))


def build_storage_timestamp_req(command_id: int, path: str) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_STORAGE_TIMESTAMP_REQ, _f_str(1, path))


def build_storage_list_req(
    command_id: int, path: str, include_md5: bool = False, filter_max_size: int = 0
) -> MainEnvelope:
    body = _f_str(1, path) + _f_bool(2, include_md5) + _f_varint(3, filter_max_size)
    return MainEnvelope(command_id, 0, False, F_STORAGE_LIST_REQ, body)


def build_storage_read_req(command_id: int, path: str) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_STORAGE_READ_REQ, _f_str(1, path))


def build_storage_write_chunks(
    command_id: int, path: str, data: bytes, chunk_size: int = 256
) -> List[MainEnvelope]:
    if not data:
        sf = StorageFile(data=b"").encode()
        body = _f_str(1, path) + _f_submsg(2, sf)
        return [MainEnvelope(command_id, 0, False, F_STORAGE_WRITE_REQ, body)]

    envs: List[MainEnvelope] = []
    total = len(data)
    for offset in range(0, total, chunk_size):
        piece = data[offset : offset + chunk_size]
        has_next = (offset + chunk_size) < total
        sf = StorageFile(data=piece).encode()
        body = _f_str(1, path) + _f_submsg(2, sf)
        envs.append(MainEnvelope(command_id, 0, has_next, F_STORAGE_WRITE_REQ, body))
    return envs


def build_storage_mkdir_req(command_id: int, path: str) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_STORAGE_MKDIR_REQ, _f_str(1, path))


def build_storage_delete_req(command_id: int, path: str, recursive: bool = False) -> MainEnvelope:
    body = _f_str(1, path) + _f_bool(2, recursive)
    return MainEnvelope(command_id, 0, False, F_STORAGE_DELETE_REQ, body)


def build_storage_rename_req(command_id: int, old_path: str, new_path: str) -> MainEnvelope:
    body = _f_str(1, old_path) + _f_str(2, new_path)
    return MainEnvelope(command_id, 0, False, F_STORAGE_RENAME_REQ, body)


def build_storage_md5_req(command_id: int, path: str) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_STORAGE_MD5_REQ, _f_str(1, path))


def build_app_start_req(command_id: int, name: str, args: str = "") -> MainEnvelope:
    body = _f_str(1, name) + _f_str(2, args)
    return MainEnvelope(command_id, 0, False, F_APP_START_REQ, body)


def build_app_exit_req(command_id: int) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_APP_EXIT_REQ, b"")


def build_app_load_file_req(command_id: int, path: str) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_APP_LOAD_FILE_REQ, _f_str(1, path))


def build_app_lock_status_req(command_id: int) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_APP_LOCK_STATUS_REQ, b"")


def build_app_data_exchange(command_id: int, payload: bytes) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_APP_DATA_EXCHANGE_REQ, _f_bytes(1, payload))


def build_desktop_is_locked_req(command_id: int) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_DESKTOP_IS_LOCKED_REQ, b"")


# --- Response Parsers ---


def parse_kv_response(raw: bytes) -> Tuple[str, str]:
    key, value = "", ""
    for fno, wt, val in iter_fields(raw):
        if fno == 1 and wt == 2:
            key = bytes(val).decode("utf-8", errors="replace")
        elif fno == 2 and wt == 2:
            value = bytes(val).decode("utf-8", errors="replace")
    return key, value


def parse_proto_version_response(raw: bytes) -> Tuple[int, int]:
    major, minor = 0, 0
    for fno, wt, val in iter_fields(raw):
        if fno == 1 and wt == 0:
            major = int(val)
        elif fno == 2 and wt == 0:
            minor = int(val)
    return major, minor


def parse_storage_info_response(raw: bytes) -> Tuple[int, int]:
    total, free = 0, 0
    for fno, wt, val in iter_fields(raw):
        if fno == 1 and wt == 0:
            total = int(val)
        elif fno == 2 and wt == 0:
            free = int(val)
    return total, free


def parse_storage_list_response(raw: bytes) -> List[StorageFile]:
    files: List[StorageFile] = []
    for fno, wt, val in iter_fields(raw):
        if fno == 1 and wt == 2:
            files.append(StorageFile.decode(bytes(val)))
    return files


def parse_storage_file_wrapper(raw: bytes) -> StorageFile:
    for fno, wt, val in iter_fields(raw):
        if fno == 1 and wt == 2:
            return StorageFile.decode(bytes(val))
    return StorageFile()


def parse_single_str(raw: bytes) -> str:
    for fno, wt, val in iter_fields(raw):
        if fno == 1 and wt == 2:
            return bytes(val).decode("utf-8", errors="replace")
    return ""


def parse_single_bytes(raw: bytes) -> bytes:
    for fno, wt, val in iter_fields(raw):
        if fno == 1 and wt == 2:
            return bytes(val)
    return b""


def parse_single_uint(raw: bytes) -> int:
    for fno, wt, val in iter_fields(raw):
        if fno == 1 and wt == 0:
            return int(val)
    return 0


# --- System DateTime & GUI Screen Stream / Input ---


@dataclass
class FlipperDateTime:
    hour: int = 0
    minute: int = 0
    second: int = 0
    day: int = 1
    month: int = 1
    year: int = 2026
    weekday: int = 1  # ISO 1..7 (Mon=1, Sun=7)

    def encode(self) -> bytes:
        return (
            _f_varint(1, self.hour)
            + _f_varint(2, self.minute)
            + _f_varint(3, self.second)
            + _f_varint(4, self.day)
            + _f_varint(5, self.month)
            + _f_varint(6, self.year)
            + _f_varint(7, self.weekday)
        )

    @classmethod
    def decode(cls, raw: bytes) -> "FlipperDateTime":
        dt = cls()
        for fno, wt, val in iter_fields(raw):
            if wt != 0:
                continue
            iv = int(val)
            if fno == 1:
                dt.hour = iv
            elif fno == 2:
                dt.minute = iv
            elif fno == 3:
                dt.second = iv
            elif fno == 4:
                dt.day = iv
            elif fno == 5:
                dt.month = iv
            elif fno == 6:
                dt.year = iv
            elif fno == 7:
                dt.weekday = iv
        return dt

    def format_iso(self) -> str:
        return f"{self.year:04d}-{self.month:02d}-{self.day:02d} {self.hour:02d}:{self.minute:02d}:{self.second:02d}"


def build_get_datetime_req(command_id: int) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_SYS_GET_DATETIME_REQ, b"")


def build_set_datetime_req(command_id: int, dt: FlipperDateTime) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_SYS_SET_DATETIME_REQ, _f_submsg(1, dt.encode()))


def parse_get_datetime_response(raw: bytes) -> FlipperDateTime:
    for fno, wt, val in iter_fields(raw):
        if fno == 1 and wt == 2:
            return FlipperDateTime.decode(bytes(val))
    return FlipperDateTime()


def build_gui_start_stream_req(command_id: int) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_GUI_START_STREAM_REQ, b"")


def build_gui_stop_stream_req(command_id: int) -> MainEnvelope:
    return MainEnvelope(command_id, 0, False, F_GUI_STOP_STREAM_REQ, b"")


def build_gui_send_input_req(command_id: int, key_code: int, input_type: int) -> MainEnvelope:
    body = _f_varint(1, key_code) + _f_varint(2, input_type)
    return MainEnvelope(command_id, 0, False, F_GUI_SEND_INPUT_REQ, body)


def parse_gui_screen_frame(raw: bytes) -> Tuple[bytes, int]:
    """Decodes `PB_Gui.ScreenFrame` -> `(1024-byte u8g2 vertical-tile buffer, orientation)`."""
    data = b""
    orientation = 0
    for fno, wt, val in iter_fields(raw):
        if fno == 1 and wt == 2:
            data = bytes(val)
        elif fno == 2 and wt == 0:
            orientation = int(val)
    return data, orientation


def screen_pixel_on(frame_1024: bytes, x: int, y: int, orientation: int = 0) -> bool:
    """Samples pixel `(x, y)` (`0..127`, `0..63`) from Flipper's u8g2 ST756x page buffer."""
    if len(frame_1024) < 1024 or not (0 <= x < 128 and 0 <= y < 64):
        return False
    if orientation == 1:  # HORIZONTAL_FLIP (left-handed 180 deg)
        x = 127 - x
        y = 63 - y
    idx = (y >> 3) * 128 + x
    return bool(frame_1024[idx] & (1 << (y & 7)))


def render_screen_frame_unicode(frame_1024: bytes, orientation: int = 0) -> str:
    """Renders Flipper's `128x64` 1-bit screen as 32 lines of UTF-8 half-blocks (`▀▄█ `)."""
    if len(frame_1024) < 1024:
        return "(empty screen frame)"
    rows: List[str] = []
    glyphs = (" ", "▀", "▄", "█")
    for y in range(0, 64, 2):
        line_chars: List[str] = []
        for x in range(128):
            top = 1 if screen_pixel_on(frame_1024, x, y, orientation) else 0
            bot = 2 if screen_pixel_on(frame_1024, x, y + 1, orientation) else 0
            line_chars.append(glyphs[top | bot])
        rows.append("".join(line_chars))
    return "\n".join(rows)

