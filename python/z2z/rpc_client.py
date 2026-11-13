"""Thread-safe Flipper Zero Protobuf RPC session client over `TransportBase`."""

from __future__ import annotations

from datetime import datetime
import queue
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

from .ble_transport import TransportBase
from .proto import (
    CommandStatus,
    F_APP_DATA_EXCHANGE_REQ,
    F_APP_STATE_RESP,
    F_GUI_SCREEN_FRAME,
    FlipperDateTime,
    FrameStreamDecoder,
    GUI_INPUT_TYPES,
    GUI_KEYS,
    MainEnvelope,
    StorageFile,
    build_app_data_exchange,
    build_app_exit_req,
    build_app_load_file_req,
    build_app_lock_status_req,
    build_app_start_req,
    build_audiovisual_alert_req,
    build_desktop_is_locked_req,
    build_device_info_req,
    build_get_datetime_req,
    build_gui_send_input_req,
    build_gui_start_stream_req,
    build_gui_stop_stream_req,
    build_ping_req,
    build_power_info_req,
    build_property_get_req,
    build_proto_version_req,
    build_set_datetime_req,
    build_storage_delete_req,
    build_storage_info_req,
    build_storage_list_req,
    build_storage_md5_req,
    build_storage_mkdir_req,
    build_storage_read_req,
    build_storage_rename_req,
    build_storage_stat_req,
    build_storage_write_chunks,
    parse_get_datetime_response,
    parse_gui_screen_frame,
    parse_kv_response,
    parse_proto_version_response,
    parse_single_bytes,
    parse_single_str,
    parse_single_uint,
    parse_storage_file_wrapper,
    parse_storage_info_response,
    parse_storage_list_response,
)


_STATUS_DISCONNECTED = -1001


class FlipperRpcError(RuntimeError):
    def __init__(self, status_code: int, message: str = "") -> None:
        if isinstance(status_code, str) and isinstance(message, int):
            status_code, message = message, status_code
        self.status_code = int(status_code)
        label = CommandStatus.name(self.status_code)
        msg_str = str(message) if message else ""
        super().__init__(
            f"Flipper RPC {label} ({self.status_code}){': ' + msg_str if msg_str else ''}"
        )


class FlipperRpcClient:
    """Multiplexes request/response `command_id` streams and unsolicited app frames."""

    def __init__(self, transport: TransportBase) -> None:
        self.transport = transport
        self._decoder = FrameStreamDecoder()
        self._cmd_lock = threading.Lock()
        self._next_id = 1
        self._pending: Dict[int, "queue.Queue[MainEnvelope]"] = {}
        self._pending_lock = threading.Lock()
        self._app_data_cb: Optional[Callable[[bytes], None]] = None
        self._app_state_cb: Optional[Callable[[int], None]] = None
        self._screen_cond = threading.Condition()
        self._screen_frame: bytes = b""
        self._screen_orient: int = 0
        self._screen_seq: int = 0
        self._streaming_screen: bool = False
        self._last_screen_frame_mono: float = 0.0
        self._screen_listeners: List[Callable[[bytes, int, int, bool], None]] = []
        self._screen_listeners_lock = threading.Lock()
        self._gui_stream_step_gap_sec: float = 0.002
        self._gui_idle_step_gap_sec: float = 0.016
        self._unlock_poll_sec: float = 0.18
        self.transport.set_rx_callback(self._on_rx_bytes)
        self.transport.set_disconnect_callback(self._on_transport_disconnect)

    @property
    def is_streaming_screen(self) -> bool:
        with self._screen_cond:
            return bool(self._streaming_screen and self.transport.is_connected)

    @property
    def last_screen_frame_mono(self) -> float:
        return self._last_screen_frame_mono

    def add_screen_listener(self, cb: Callable[[bytes, int, int, bool], None]) -> None:
        with self._screen_listeners_lock:
            if cb not in self._screen_listeners:
                self._screen_listeners.append(cb)

    def remove_screen_listener(self, cb: Callable[[bytes, int, int, bool], None]) -> None:
        with self._screen_listeners_lock:
            if cb in self._screen_listeners:
                self._screen_listeners.remove(cb)

    def _notify_screen_listeners(
        self, frame: bytes, orient: int, seq: int, streaming: bool
    ) -> None:
        with self._screen_listeners_lock:
            listeners = list(self._screen_listeners)
        for cb in listeners:
            try:
                cb(frame, orient, seq, streaming)
            except Exception:
                pass

    def mark_screen_stream_stopped(self) -> None:
        with self._screen_cond:
            self._streaming_screen = False
            frame = self._screen_frame
            orient = self._screen_orient
            seq = self._screen_seq
            self._screen_cond.notify_all()
        self._notify_screen_listeners(frame, orient, seq, False)

    def _on_transport_disconnect(self) -> None:
        """Wakes all blocked RPC and screen-frame waiters immediately on BLE link loss."""
        with self._screen_cond:
            self._streaming_screen = False
            frame = self._screen_frame
            orient = self._screen_orient
            seq = self._screen_seq
            self._screen_cond.notify_all()
        self._notify_screen_listeners(frame, orient, seq, False)
        sentinel = MainEnvelope(command_id=0, command_status=_STATUS_DISCONNECTED)
        with self._pending_lock:
            waiters = list(self._pending.values())
        for wq in waiters:
            wq.put(sentinel)

    def set_app_callbacks(
        self,
        on_data: Optional[Callable[[bytes], None]] = None,
        on_state: Optional[Callable[[int], None]] = None,
    ) -> None:
        self._app_data_cb = on_data
        self._app_state_cb = on_state

    def reset_session_state(self) -> None:
        """Reset framing decoder and screen-stream flag when opening a fresh BLE session."""
        self._decoder = FrameStreamDecoder()
        with self._screen_cond:
            self._streaming_screen = False
            self._screen_cond.notify_all()

    def _alloc_command_id(self) -> int:
        with self._cmd_lock:
            cid = self._next_id
            self._next_id = 1 if cid >= 0x7FFFFFFF else cid + 1
            return cid

    def _on_rx_bytes(self, chunk: bytes) -> None:
        for env in self._decoder.feed(chunk):
            if env.command_id == 0:
                if env.command_status != CommandStatus.OK:
                    # Flipper `rpc.c:322` emits `Main(command_id=0, command_status=ERROR_DECODE)`
                    # AND calls `bt_profile_restore_default()` on stream decode error. Broadcast to all
                    # pending waiters immediately so `call_stream` fails fast instead of hanging 30s!
                    with self._pending_lock:
                        waiters = list(self._pending.values())
                    for wq in waiters:
                        wq.put(env)
                    continue
                if env.content_field == F_GUI_SCREEN_FRAME:
                    frame_bytes, orient = parse_gui_screen_frame(env.content_bytes)
                    if frame_bytes:
                        with self._screen_cond:
                            self._screen_frame = frame_bytes
                            self._screen_orient = orient
                            self._screen_seq += 1
                            self._last_screen_frame_mono = time.monotonic()
                            seq = self._screen_seq
                            streaming = bool(
                                self._streaming_screen and self.transport.is_connected
                            )
                            self._screen_cond.notify_all()
                        self._notify_screen_listeners(frame_bytes, orient, seq, streaming)
                elif env.content_field == F_APP_DATA_EXCHANGE_REQ and self._app_data_cb:
                    payload = parse_single_bytes(env.content_bytes)
                    threading.Thread(
                        target=self._app_data_cb, args=(payload,), daemon=True
                    ).start()
                elif env.content_field == F_APP_STATE_RESP and self._app_state_cb:
                    st = parse_single_uint(env.content_bytes)
                    threading.Thread(
                        target=self._app_state_cb, args=(st,), daemon=True
                    ).start()
                continue

            with self._pending_lock:
                q = self._pending.get(env.command_id)
            if q is not None:
                q.put(env)

    def call_stream(
        self,
        req_envelopes: List[MainEnvelope],
        timeout_sec: float = 12.0,
        allow_statuses: Tuple[int, ...] = (CommandStatus.OK,),
    ) -> List[MainEnvelope]:
        if not req_envelopes:
            return []
        cid = req_envelopes[0].command_id
        resp_q: "queue.Queue[MainEnvelope]" = queue.Queue()
        with self._pending_lock:
            self._pending[cid] = resp_q
        try:
            if len(req_envelopes) == 1:
                self.transport.send_bytes(
                    req_envelopes[0].encode_delimited(), timeout_sec=timeout_sec
                )
            else:
                if hasattr(self.transport, "sync_flow_credits"):
                    self.transport.sync_flow_credits()
                env_gap = float(getattr(self.transport, "inter_envelope_delay_sec", 0.0) or 0.0)
                total_envs = len(req_envelopes)
                for idx, req_env in enumerate(req_envelopes):
                    if not resp_q.empty():
                        break
                    self.transport.send_bytes(
                        req_env.encode_delimited(), timeout_sec=timeout_sec
                    )
                    if idx + 1 < total_envs and env_gap > 0 and resp_q.empty():
                        time.sleep(env_gap)

            out: List[MainEnvelope] = []
            while True:
                try:
                    env = resp_q.get(timeout=timeout_sec)
                except queue.Empty as exc:
                    diag = ""
                    if hasattr(self.transport, "rx_stats"):
                        st = getattr(self.transport, "rx_stats")
                        diag = f" (rx_chunks={st.get('rx_chunks')}, rx_bytes={st.get('rx_bytes')}, credits={st.get('credits')})"
                    if hasattr(self.transport, "check_link_alive"):
                        try:
                            if not self.transport.check_link_alive():
                                raise ConnectionError(
                                    f"BLE link disconnected during Flipper RPC command {cid}{diag}"
                                ) from exc
                        except ConnectionError:
                            raise
                        except Exception:
                            pass
                    if not self.transport.is_connected:
                        raise ConnectionError(
                            f"BLE link disconnected during Flipper RPC command {cid}{diag}"
                        ) from exc
                    raise TimeoutError(f"Flipper RPC command {cid} timed out{diag}") from exc
                if env.command_status == _STATUS_DISCONNECTED:
                    raise ConnectionError(f"BLE link disconnected during Flipper RPC command {cid}")
                if env.command_status not in allow_statuses:
                    raise FlipperRpcError(env.command_status)
                out.append(env)
                if not env.has_next:
                    break
            return out
        finally:
            with self._pending_lock:
                self._pending.pop(cid, None)

    def call_one(
        self,
        env: MainEnvelope,
        timeout_sec: float = 10.0,
        allow_statuses: Tuple[int, ...] = (CommandStatus.OK,),
    ) -> List[MainEnvelope]:
        return self.call_stream([env], timeout_sec=timeout_sec, allow_statuses=allow_statuses)

    # --- System & Property RPCs ---

    def ping(self, payload: bytes = b"z2z", timeout_sec: float = 10.0) -> bytes:
        cid = self._alloc_command_id()
        resps = self.call_one(build_ping_req(cid, payload), timeout_sec=timeout_sec)
        return parse_single_bytes(resps[-1].content_bytes)

    def protobuf_version(self) -> Tuple[int, int]:
        cid = self._alloc_command_id()
        resps = self.call_one(build_proto_version_req(cid))
        return parse_proto_version_response(resps[-1].content_bytes)

    def device_info(self) -> Dict[str, str]:
        cid = self._alloc_command_id()
        resps = self.call_one(build_device_info_req(cid))
        info: Dict[str, str] = {}
        for r in resps:
            k, v = parse_kv_response(r.content_bytes)
            if k:
                info[k] = v
        return info

    def power_info(self) -> Dict[str, str]:
        cid = self._alloc_command_id()
        resps = self.call_one(build_power_info_req(cid))
        info: Dict[str, str] = {}
        for r in resps:
            k, v = parse_kv_response(r.content_bytes)
            if k:
                info[k] = v
        return info

    def property_get(self, key: str) -> Dict[str, str]:
        cid = self._alloc_command_id()
        resps = self.call_one(build_property_get_req(cid, key))
        out: Dict[str, str] = {}
        for r in resps:
            k, v = parse_kv_response(r.content_bytes)
            if k:
                out[k] = v
        return out

    def audiovisual_alert(self) -> None:
        cid = self._alloc_command_id()
        self.call_one(build_audiovisual_alert_req(cid))

    def get_datetime(self) -> FlipperDateTime:
        cid = self._alloc_command_id()
        resps = self.call_one(build_get_datetime_req(cid))
        return parse_get_datetime_response(resps[-1].content_bytes)

    def set_datetime(self, dt: Optional[FlipperDateTime] = None) -> FlipperDateTime:
        if dt is None:
            now = datetime.now()
            dt = FlipperDateTime(
                hour=now.hour,
                minute=now.minute,
                second=now.second,
                day=now.day,
                month=now.month,
                year=now.year,
                weekday=now.isoweekday(),
            )
        cid = self._alloc_command_id()
        self.call_one(build_set_datetime_req(cid, dt))
        return dt

    def read_flipper_alert_mode(self) -> str:
        """Reads `/ext/apps_data/zero2zero/settings.cfg` (`vibro` default; tames loud siren)."""
        cfg_path = "/ext/apps_data/zero2zero/settings.cfg"
        try:
            st = self.storage_stat(cfg_path)
            if st is None or st.is_dir or st.size == 0 or st.size > 1024:
                return "vibro"
            raw = self.storage_read(cfg_path).decode("utf-8", errors="replace")
            for line in raw.splitlines():
                s = line.strip()
                if s.startswith("alert_mode="):
                    val = s.split("=", 1)[1].strip().lower()
                    if val in ("vibro", "soft", "led", "off", "loud"):
                        return val
        except Exception:
            pass
        return "vibro"

    def write_flipper_alert_mode(self, mode: str) -> str:
        clean = mode.strip().lower()
        if clean not in ("vibro", "soft", "led", "off", "loud"):
            raise ValueError(f"Invalid alert_mode {mode!r} (expected vibro|soft|led|off|loud)")
        self.storage_mkdirs("/ext/apps_data/zero2zero")
        self.storage_write(
            "/ext/apps_data/zero2zero/settings.cfg",
            f"alert_mode={clean}\n".encode("utf-8"),
        )
        return clean

    def trigger_gentle_alert(self, mode: Optional[str] = None) -> str:
        """Honors Flipper's `alert_mode` instead of firing `audiovisual_alert` unconditionally."""
        effective = (mode or self.read_flipper_alert_mode()).strip().lower()
        if effective == "loud":
            self.audiovisual_alert()
        return effective

    # --- Storage RPCs ---

    def storage_info(self, path: str = "/ext") -> Tuple[int, int]:
        cid = self._alloc_command_id()
        resps = self.call_one(build_storage_info_req(cid, path))
        return parse_storage_info_response(resps[-1].content_bytes)

    def storage_stat(self, path: str) -> Optional[StorageFile]:
        cid = self._alloc_command_id()
        resps = self.call_one(
            build_storage_stat_req(cid, path),
            allow_statuses=(CommandStatus.OK, CommandStatus.ERROR_STORAGE_NOT_EXIST),
        )
        if resps[-1].command_status == CommandStatus.ERROR_STORAGE_NOT_EXIST:
            return None
        return parse_storage_file_wrapper(resps[-1].content_bytes)

    def storage_list(self, path: str, include_md5: bool = False) -> List[StorageFile]:
        cid = self._alloc_command_id()
        resps = self.call_one(
            build_storage_list_req(cid, path, include_md5=include_md5),
            allow_statuses=(CommandStatus.OK, CommandStatus.ERROR_STORAGE_NOT_EXIST),
        )
        if resps[-1].command_status == CommandStatus.ERROR_STORAGE_NOT_EXIST:
            return []
        items: List[StorageFile] = []
        for r in resps:
            items.extend(parse_storage_list_response(r.content_bytes))
        return items

    def storage_read(self, path: str, timeout_sec: float = 25.0) -> bytes:
        cid = self._alloc_command_id()
        resps = self.call_one(build_storage_read_req(cid, path), timeout_sec=timeout_sec)
        buf = bytearray()
        for r in resps:
            sf = parse_storage_file_wrapper(r.content_bytes)
            buf.extend(sf.data)
        return bytes(buf)

    def storage_write(
        self, path: str, data: bytes, chunk_size: int = 256, timeout_sec: float = 30.0
    ) -> None:
        cid = self._alloc_command_id()
        chunks = build_storage_write_chunks(cid, path, data, chunk_size=chunk_size)
        self.call_stream(chunks, timeout_sec=timeout_sec)

    def storage_mkdirs(self, path: str) -> None:
        parts = [p for p in path.strip("/").split("/") if p]
        cur = ""
        for idx, part in enumerate(parts):
            cur += "/" + part
            if idx == 0 and part in ("ext", "int"):
                continue
            cid = self._alloc_command_id()
            self.call_one(
                build_storage_mkdir_req(cid, cur),
                allow_statuses=(CommandStatus.OK, CommandStatus.ERROR_STORAGE_EXIST),
            )

    def storage_delete(self, path: str, recursive: bool = False) -> None:
        cid = self._alloc_command_id()
        self.call_one(
            build_storage_delete_req(cid, path, recursive=recursive),
            allow_statuses=(CommandStatus.OK, CommandStatus.ERROR_STORAGE_NOT_EXIST),
        )

    def storage_rename(self, old_path: str, new_path: str) -> None:
        cid = self._alloc_command_id()
        self.call_one(build_storage_rename_req(cid, old_path, new_path))

    def storage_md5(self, path: str) -> str:
        cid = self._alloc_command_id()
        resps = self.call_one(build_storage_md5_req(cid, path))
        return parse_single_str(resps[-1].content_bytes)

    # --- Application RPCs (Bi-Directional `.fap` Bridge) ---

    def app_lock_status(self) -> bool:
        cid = self._alloc_command_id()
        resps = self.call_one(build_app_lock_status_req(cid))
        return bool(parse_single_uint(resps[-1].content_bytes))

    def app_start(self, name: str, args: str = "") -> None:
        cid = self._alloc_command_id()
        self.call_one(build_app_start_req(cid, name, args))

    def app_load_file(self, path: str) -> None:
        cid = self._alloc_command_id()
        self.call_one(build_app_load_file_req(cid, path))

    def app_exit(self) -> bool:
        """Requests graceful RPC exit (`RpcAppEventTypeAppExit`).

        Returns `True` only when an RPC-mode app (`args="RPC"`) accepted exit (`OK`), and
        `False` when a native non-RPC app (`NFC`, `Sub-GHz`, etc.) is running
        (`rpc_app.c:215` -> `ERROR_APP_NOT_RUNNING`).
        """
        cid = self._alloc_command_id()
        resps = self.call_one(
            build_app_exit_req(cid),
            allow_statuses=(
                CommandStatus.OK,
                CommandStatus.ERROR_APP_NOT_RUNNING,
                CommandStatus.ERROR_APP_CMD_ERROR,
            ),
        )
        return bool(resps and resps[-1].command_status == CommandStatus.OK)

    def app_send_data(self, payload: bytes) -> None:
        cid = self._alloc_command_id()
        self.call_one(build_app_data_exchange(cid, payload))

    def desktop_is_locked(self) -> bool:
        """Queries `PB_Desktop.IsLockedRequest` (`field 66`).

        In Flipper firmware (`rpc_desktop.c`), `command_status == OK (0)` means the Desktop
        keypad is locked, while `command_status == ERROR (1)` means unlocked.
        """
        cid = self._alloc_command_id()
        resps = self.call_one(
            build_desktop_is_locked_req(cid),
            allow_statuses=(
                CommandStatus.OK,
                CommandStatus.ERROR,
                CommandStatus.ERROR_NOT_IMPLEMENTED,
            ),
        )
        return bool(resps and resps[-1].command_status == CommandStatus.OK)

    def ensure_app_unlocked(self) -> None:
        """Exits any active Flipper app holding the loader lock before `.fap` update or start."""
        if not self.app_lock_status():
            return
        if self.desktop_is_locked():
            raise FlipperRpcError(
                CommandStatus.ERROR_APP_SYSTEM_LOCKED,
                "Flipper keypad is locked (unlock Flipper first)",
            )
        if self.app_exit():
            for _ in range(8):
                time.sleep(self._unlock_poll_sec)
                if not self.app_lock_status():
                    return
        else:
            # When the user quits `zero2zero.fap` on Flipper, `zero2zero_free`
            # clears `rpc_app->callback = NULL` (`app_exit() -> False`) ~150-350ms before
            # `flipper_application_free` releases `loader_lock`. Poll first BEFORE sending any
            # `BACK` so we never send spurious `BACK` presses onto Flipper's Desktop (where
            # 3x `BACK` locks Flipper's keypad!).
            for _ in range(3):
                if self._unlock_poll_sec > 0:
                    time.sleep(self._unlock_poll_sec)
                if not self.app_lock_status():
                    return
        # Native non-RPC apps (`NFC`, `Sub-GHz`, `Infrared`) return
        # `ERROR_APP_NOT_RUNNING` to `AppExitRequest` and need `250-550ms` to stop RF workers.
        # Send at most 2 `BACK` presses (never >= 3, preventing accidental 3x-Back keypad lock)
        # and verify `desktop_is_locked()` before each step.
        for _ in range(2):
            if self.desktop_is_locked():
                raise FlipperRpcError(
                    CommandStatus.ERROR_APP_SYSTEM_LOCKED,
                    "Flipper keypad is locked (unlock Flipper first)",
                )
            self.gui_send_input("back", "short")
            for poll_idx in range(3):
                wait_s = self._unlock_poll_sec * (1.4 if poll_idx == 0 else 1.0)
                if wait_s > 0:
                    time.sleep(wait_s)
                if not self.app_lock_status():
                    return
        if self.app_lock_status():
            if self.desktop_is_locked():
                raise FlipperRpcError(
                    CommandStatus.ERROR_APP_SYSTEM_LOCKED,
                    "Flipper keypad is locked (unlock Flipper first)",
                )
            raise FlipperRpcError(
                CommandStatus.ERROR_APP_SYSTEM_LOCKED,
                "Flipper app is still open (press Back/Exit on Flipper first)",
            )

    def open_file_in_flipper_app(self, remote_path: str) -> str:
        """Opens a capture or script directly in Flipper's native app via `AppStartRequest`."""
        p = remote_path.strip()
        low = p.lower()
        if low.endswith(".sub"):
            app_name = "Sub-GHz"
        elif low.endswith(".nfc"):
            app_name = "NFC"
        elif low.endswith(".rfid"):
            app_name = "125 kHz RFID"
        elif low.endswith(".ir"):
            app_name = "Infrared"
        elif low.endswith(".ibtn"):
            app_name = "iButton"
        elif low.startswith("/ext/badusb/") or low.endswith(".ducky") or low.endswith(".txt"):
            app_name = "Bad USB"
        elif low.endswith(".fap"):
            app_name = p
            p = ""
        else:
            raise ValueError(f"Unsupported Flipper file type for remote launch: {remote_path!r}")

        # If another app holds the loader lock, exit it cleanly first.
        self.ensure_app_unlocked()

        self.app_start(app_name, p)
        return app_name

    # --- GUI Screen Stream & Remote Input RPCs ---

    def gui_start_screen_stream(self, wait_first_frame_sec: float = 1.2) -> Tuple[bytes, int, int]:
        cid = self._alloc_command_id()
        with self._screen_cond:
            self._streaming_screen = True
        try:
            self.call_one(
                build_gui_start_stream_req(cid),
                allow_statuses=(
                    CommandStatus.OK,
                    CommandStatus.ERROR_VIRTUAL_DISPLAY_ALREADY_STARTED,
                ),
            )
        except Exception:
            with self._screen_cond:
                self._streaming_screen = False
                self._screen_cond.notify_all()
            raise
        with self._screen_cond:
            self._streaming_screen = True
            if not self._screen_frame and wait_first_frame_sec > 0:
                self._screen_cond.wait(timeout=wait_first_frame_sec)
            return self._screen_frame, self._screen_orient, self._screen_seq

    def gui_stop_screen_stream(self, timeout_sec: float = 2.5) -> None:
        cid = self._alloc_command_id()
        try:
            self.call_one(
                build_gui_stop_stream_req(cid),
                timeout_sec=timeout_sec,
                allow_statuses=(
                    CommandStatus.OK,
                    CommandStatus.ERROR_VIRTUAL_DISPLAY_NOT_STARTED,
                ),
            )
        finally:
            with self._screen_cond:
                self._streaming_screen = False
                self._screen_cond.notify_all()

    def gui_get_latest_frame(
        self, wait_after_seq: Optional[int] = None, wait_sec: float = 0.0
    ) -> Tuple[bytes, int, int, bool]:
        with self._screen_cond:
            if (
                wait_sec > 0.0
                and wait_after_seq is not None
                and self._streaming_screen
                and self.transport.is_connected
                and self._screen_seq <= wait_after_seq
            ):
                self._screen_cond.wait(timeout=wait_sec)
            return (
                self._screen_frame,
                self._screen_orient,
                self._screen_seq,
                bool(self._streaming_screen and self.transport.is_connected),
            )

    def gui_send_input(self, key: str, mode: str = "short", timeout_sec: float = 2.5) -> None:
        """Sends a 3-step `PRESS -> SHORT/LONG -> RELEASE` sequence.

        Never pack `PRESS + SHORT + RELEASE` into a single `send_bytes()` write!
        In Flipper firmware (`serial_service.c:226` / `bt.c:230`), `ble_svc_serial_update_tx()` has
        no retry loop when STM32WB55 CPU2's Indication slot is busy, and `bt_rpc_send_bytes_callback()`
        ignores `false` returns and waits forever in `furi_event_flag_wait(..., FuriWaitForever)`.
        Sending sequential `call_one()` steps guarantees each Indication + `ATT_OP_HANDLE_VALUE_CFM`
        finishes on CPU2 before the next `SendInputEventRequest` enters Flipper's RPC stream.
        """
        k = key.strip().lower()
        if k not in GUI_KEYS:
            raise ValueError(f"Unknown Flipper key {key!r} (expected {sorted(GUI_KEYS)})")
        m = mode.strip().lower()
        if m not in ("short", "long"):
            raise ValueError(f"Unsupported input mode {mode!r} (expected 'short' or 'long')")

        key_code = GUI_KEYS[k]
        mid_type = GUI_INPUT_TYPES["long"] if m == "long" else GUI_INPUT_TYPES["short"]

        def _send_step(step_type: int) -> None:
            cid = self._alloc_command_id()
            try:
                self.call_one(
                    build_gui_send_input_req(cid, key_code, step_type),
                    timeout_sec=timeout_sec,
                )
            except TimeoutError as exc:
                raise TimeoutError(f"Flipper GUI input ({k}/{m}) timed out: {exc}") from exc

        step_gap = (
            self._gui_stream_step_gap_sec
            if self.is_streaming_screen
            else self._gui_idle_step_gap_sec
        )
        _send_step(GUI_INPUT_TYPES["press"])
        if step_gap > 0:
            time.sleep(step_gap)
        _send_step(mid_type)
        if step_gap > 0:
            time.sleep(step_gap)
        _send_step(GUI_INPUT_TYPES["release"])
