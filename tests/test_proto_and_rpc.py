"""Unit tests for Phase 1: wire Protobuf framing, BLE flow-control credits, and RPC client."""

from __future__ import annotations

import hashlib
import os
import struct
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))

from z2z.ble_transport import (
    BluezBleTransport,
    CreditFlowController,
    TransportBase,
    clean_dbus_error,
    detect_flipper_adv,
)
from z2z.proto import (
    CommandStatus,
    F_APP_DATA_EXCHANGE_REQ,
    F_APP_EXIT_REQ,
    F_APP_LOCK_STATUS_REQ,
    F_APP_LOCK_STATUS_RESP,
    F_APP_START_REQ,
    F_DESKTOP_IS_LOCKED_REQ,
    F_EMPTY,
    F_GUI_SCREEN_FRAME,
    F_GUI_SEND_INPUT_REQ,
    F_GUI_START_STREAM_REQ,
    F_GUI_STOP_STREAM_REQ,
    F_STORAGE_DELETE_REQ,
    F_STORAGE_INFO_RESP,
    F_STORAGE_LIST_RESP,
    F_STORAGE_MD5_REQ,
    F_STORAGE_MD5_RESP,
    F_STORAGE_READ_RESP,
    F_STORAGE_RENAME_REQ,
    F_STORAGE_STAT_RESP,
    F_STORAGE_WRITE_REQ,
    F_SYS_AUDIOVISUAL_ALERT_REQ,
    F_SYS_DEVICE_INFO_REQ,
    F_SYS_DEVICE_INFO_RESP,
    F_SYS_GET_DATETIME_REQ,
    F_SYS_GET_DATETIME_RESP,
    F_SYS_PING_REQ,
    F_SYS_PING_RESP,
    F_SYS_POWER_INFO_REQ,
    F_SYS_SET_DATETIME_REQ,
    FlipperDateTime,
    FrameStreamDecoder,
    MainEnvelope,
    StorageFile,
    _f_bytes,
    _f_str,
    _f_submsg,
    _f_varint,
    build_power_info_req,
    iter_fields,
    parse_storage_file_wrapper,
    render_screen_frame_unicode,
    screen_pixel_on,
)
from z2z.rpc_client import FlipperRpcClient, FlipperRpcError


class SimulatedFlipperTransport(TransportBase):
    """In-memory simulated Flipper Zero BLE GATT + RPC responder with credit backpressure."""

    def __init__(self, initial_credits: int = 120) -> None:
        self._connected = True
        self._rx_cb = None
        self._disc_cb = None
        self.send_calls = 0
        self.flow = CreditFlowController(initial_credits=initial_credits, max_chunk=60)
        self._in_decoder = FrameStreamDecoder()
        self.files = {
            "/ext/subghz/Gate.sub": b"Filetype: Flipper SubGhz RAW File\nVersion: 1\n",
        }
        self.received_app_frames: list[bytes] = []
        self.loud_alert_count = 0
        self.rtc = FlipperDateTime(hour=12, minute=0, second=0, day=1, month=1, year=2026, weekday=4)
        self.gui_inputs: list[tuple[int, int]] = []
        self.app_locked = False
        self.desktop_locked = False
        self.started_apps: list[tuple[str, str]] = []
        self.screen_streaming = False

    def connect(self, target=None):
        if self.flow.is_closed:
            self.flow = CreditFlowController(initial_credits=256, max_chunk=60)
        self._in_decoder = FrameStreamDecoder()
        self._connected = True
        return self.link_info()

    def disconnect(self) -> None:
        was_conn = self._connected
        self._connected = False
        self.flow.close()
        if was_conn and self._disc_cb:
            self._disc_cb()

    def set_rx_callback(self, cb) -> None:
        self._rx_cb = cb

    def set_disconnect_callback(self, cb) -> None:
        self._disc_cb = cb

    @property
    def is_connected(self) -> bool:
        return bool(self._connected and not self.flow.is_closed)

    def link_info(self):
        return {
            "connected": self._connected,
            "address": "80:E1:26:AA:BB:CC",
            "name": "Flipper ZeroSim",
            "shell_color": "Transparent",
            "rssi": -54,
            "credits": self.flow.credits,
            "ble_battery_pct": 87,
        }

    def _emit_to_host(self, env: MainEnvelope) -> None:
        raw = env.encode_delimited()
        # Deliberately fragment into tiny 7-byte BLE indications to exercise reassembly
        for idx in range(0, len(raw), 7):
            if self._rx_cb:
                self._rx_cb(raw[idx : idx + 7])

    def send_bytes(self, data: bytes, timeout_sec: float = 10.0) -> None:
        self.send_calls += 1
        writes: list[bytes] = []

        def _write_chunk(chunk: bytes) -> None:
            writes.append(chunk)
            # Simulate STM32WB55 draining RX buffer & notifying big-endian Flow Control
            if self.flow.credits == 0:
                def _refill():
                    time.sleep(0.005)
                    self.flow.update_from_be_bytes(struct.pack(">I", 256))

                threading.Thread(target=_refill, daemon=True).start()

        self.flow.write_paced(data, _write_chunk, timeout_sec=timeout_sec)

        write_accum = bytearray()
        for chunk in writes:
            for req in self._in_decoder.feed(chunk):
                self._handle_rpc(req, write_accum)

    def _handle_rpc(self, req: MainEnvelope, write_accum: bytearray) -> None:
        cid = req.command_id
        if req.content_field == F_SYS_PING_REQ:
            self._emit_to_host(MainEnvelope(cid, 0, False, F_SYS_PING_RESP, req.content_bytes))
        elif req.content_field == F_SYS_DEVICE_INFO_REQ:
            r1 = _f_str(1, "hardware_name") + _f_str(2, "ZeroSim")
            r2 = _f_str(1, "firmware_commit") + _f_str(2, "0.105.0")
            self._emit_to_host(MainEnvelope(cid, 0, True, F_SYS_DEVICE_INFO_RESP, r1))
            self._emit_to_host(MainEnvelope(cid, 0, False, F_SYS_DEVICE_INFO_RESP, r2))
        elif req.content_field == F_STORAGE_INFO_RESP or req.content_field == 28:
            body = _f_varint(1, 32 * 1024 * 1024) + _f_varint(2, 24 * 1024 * 1024)
            self._emit_to_host(MainEnvelope(cid, 0, False, F_STORAGE_INFO_RESP, body))
        elif req.content_field == 24:  # StatRequest
            from z2z.proto import parse_single_str

            p = parse_single_str(req.content_bytes)
            if p in self.files:
                sf = StorageFile(is_dir=False, name=os.path.basename(p), size=len(self.files[p]))
                self._emit_to_host(
                    MainEnvelope(cid, 0, False, F_STORAGE_STAT_RESP, _f_submsg(1, sf.encode()))
                )
            else:
                self._emit_to_host(
                    MainEnvelope(cid, CommandStatus.ERROR_STORAGE_NOT_EXIST, False, F_EMPTY, b"")
                )
        elif req.content_field == 9:  # ReadRequest
            from z2z.proto import parse_single_str

            p = parse_single_str(req.content_bytes)
            if p not in self.files:
                self._emit_to_host(
                    MainEnvelope(cid, CommandStatus.ERROR_STORAGE_NOT_EXIST, False, F_EMPTY, b"")
                )
                return
            blob = self.files[p]
            mid = max(1, len(blob) // 2)
            c1 = StorageFile(data=blob[:mid]).encode()
            c2 = StorageFile(data=blob[mid:]).encode()
            self._emit_to_host(MainEnvelope(cid, 0, True, F_STORAGE_READ_RESP, _f_submsg(1, c1)))
            self._emit_to_host(MainEnvelope(cid, 0, False, F_STORAGE_READ_RESP, _f_submsg(1, c2)))
        elif req.content_field == F_STORAGE_WRITE_REQ:
            path = ""
            chunk_data = b""
            for fno, wt, val in iter_fields(req.content_bytes):
                if fno == 1 and wt == 2:
                    path = bytes(val).decode()
                elif fno == 2 and wt == 2:
                    chunk_data = StorageFile.decode(bytes(val)).data
            buf = self.files.get(path + ".__part", b"") + chunk_data
            if req.has_next:
                self.files[path + ".__part"] = buf
            else:
                self.files.pop(path + ".__part", None)
                self.files[path] = buf
                self._emit_to_host(MainEnvelope(cid, 0, False, F_EMPTY, b""))
        elif req.content_field == F_STORAGE_DELETE_REQ:
            from z2z.proto import parse_single_str

            p = parse_single_str(req.content_bytes)
            if p in self.files:
                del self.files[p]
                self._emit_to_host(MainEnvelope(cid, 0, False, F_EMPTY, b""))
            else:
                self._emit_to_host(
                    MainEnvelope(cid, CommandStatus.ERROR_STORAGE_NOT_EXIST, False, F_EMPTY, b"")
                )
        elif req.content_field == F_STORAGE_RENAME_REQ:
            old_p, new_p = "", ""
            for fno, wt, val in iter_fields(req.content_bytes):
                if fno == 1 and wt == 2:
                    old_p = bytes(val).decode()
                elif fno == 2 and wt == 2:
                    new_p = bytes(val).decode()
            if old_p not in self.files:
                self._emit_to_host(
                    MainEnvelope(cid, CommandStatus.ERROR_STORAGE_NOT_EXIST, False, F_EMPTY, b"")
                )
            else:
                self.files[new_p] = self.files.pop(old_p)
                self._emit_to_host(MainEnvelope(cid, 0, False, F_EMPTY, b""))
        elif req.content_field == F_STORAGE_MD5_REQ:
            from z2z.proto import parse_single_str

            p = parse_single_str(req.content_bytes)
            if p not in self.files:
                self._emit_to_host(
                    MainEnvelope(cid, CommandStatus.ERROR_STORAGE_NOT_EXIST, False, F_EMPTY, b"")
                )
            else:
                digest = hashlib.md5(self.files[p]).hexdigest()
                self._emit_to_host(
                    MainEnvelope(cid, 0, False, F_STORAGE_MD5_RESP, _f_str(1, digest))
                )
        elif req.content_field == F_APP_DATA_EXCHANGE_REQ:
            from z2z.proto import parse_single_bytes

            self.received_app_frames.append(parse_single_bytes(req.content_bytes))
            self._emit_to_host(MainEnvelope(cid, 0, False, F_EMPTY, b""))
        elif req.content_field == F_SYS_AUDIOVISUAL_ALERT_REQ:
            self.loud_alert_count += 1
            self._emit_to_host(MainEnvelope(cid, 0, False, F_EMPTY, b""))
        elif req.content_field == F_SYS_GET_DATETIME_REQ:
            self._emit_to_host(
                MainEnvelope(cid, 0, False, F_SYS_GET_DATETIME_RESP, _f_submsg(1, self.rtc.encode()))
            )
        elif req.content_field == F_SYS_SET_DATETIME_REQ:
            for fno, wt, val in iter_fields(req.content_bytes):
                if fno == 1 and wt == 2:
                    self.rtc = FlipperDateTime.decode(bytes(val))
            self._emit_to_host(MainEnvelope(cid, 0, False, F_EMPTY, b""))
        elif req.content_field == F_GUI_START_STREAM_REQ:
            self.screen_streaming = True
            self._emit_to_host(MainEnvelope(cid, 0, False, F_EMPTY, b""))
            fb = bytearray(1024)
            fb[0] = 0x01  # (x=0, y=0) on
            fb[127] = 0x80  # (x=127, y=7) on
            frame_msg = _f_bytes(1, bytes(fb)) + _f_varint(2, 0)
            self._emit_to_host(MainEnvelope(0, 0, False, F_GUI_SCREEN_FRAME, frame_msg))
        elif req.content_field == F_GUI_STOP_STREAM_REQ:
            self.screen_streaming = False
            self._emit_to_host(MainEnvelope(cid, 0, False, F_EMPTY, b""))
        elif req.content_field == F_GUI_SEND_INPUT_REQ:
            k, t = 0, 0
            for fno, wt, val in iter_fields(req.content_bytes):
                if fno == 1 and wt == 0:
                    k = int(val)
                elif fno == 2 and wt == 0:
                    t = int(val)
            self.gui_inputs.append((k, t))
            self._emit_to_host(MainEnvelope(cid, 0, False, F_EMPTY, b""))
        elif req.content_field == F_APP_LOCK_STATUS_REQ:
            self._emit_to_host(
                MainEnvelope(
                    cid, 0, False, F_APP_LOCK_STATUS_RESP, _f_varint(1, 1 if self.app_locked else 0)
                )
            )
        elif req.content_field == F_DESKTOP_IS_LOCKED_REQ:
            st = CommandStatus.OK if self.desktop_locked else CommandStatus.ERROR
            self._emit_to_host(MainEnvelope(cid, st, False, F_EMPTY, b""))
        elif req.content_field == F_APP_EXIT_REQ:
            self.app_locked = False
            self._emit_to_host(MainEnvelope(cid, 0, False, F_EMPTY, b""))
        elif req.content_field == F_APP_START_REQ:
            nm, ag = "", ""
            for fno, wt, val in iter_fields(req.content_bytes):
                if fno == 1 and wt == 2:
                    nm = bytes(val).decode()
                elif fno == 2 and wt == 2:
                    ag = bytes(val).decode()
            self.app_locked = True
            self.started_apps.append((nm, ag))
            self._emit_to_host(MainEnvelope(cid, 0, False, F_EMPTY, b""))
        elif req.content_field == 7:  # ListRequest
            from z2z.proto import parse_single_str

            dpath = parse_single_str(req.content_bytes).rstrip("/")
            matched = []
            for fp, blob in sorted(self.files.items()):
                if os.path.dirname(fp) == dpath:
                    matched.append(
                        StorageFile(is_dir=False, name=os.path.basename(fp), size=len(blob), md5sum="deadbeef")
                    )
            body = b"".join(_f_submsg(1, sf.encode()) for sf in matched)
            self._emit_to_host(MainEnvelope(cid, 0, False, F_STORAGE_LIST_RESP, body))
        else:
            self._emit_to_host(MainEnvelope(cid, 0, False, F_EMPTY, b""))

    def simulate_fap_push(self, payload: bytes) -> None:
        """Simulates `rpc_system_app_exchange_data()` emitted from Flipper (`command_id = 0`)."""
        self._emit_to_host(
            MainEnvelope(0, 0, False, F_APP_DATA_EXCHANGE_REQ, _f_bytes(1, payload))
        )


class ProtoAndRpcTests(unittest.TestCase):
    def test_empty_oneof_submessage_preserved_on_wire(self):
        # PowerInfoRequest has 0 fields inside, must still decode as field 44
        req = build_power_info_req(command_id=9)
        raw = req.encode_delimited()
        decoded = FrameStreamDecoder().feed(raw)
        self.assertEqual(len(decoded), 1)
        self.assertEqual(decoded[0].command_id, 9)
        self.assertEqual(decoded[0].content_field, F_SYS_POWER_INFO_REQ)

    def test_flipper_adv_shell_color_detection(self):
        # 0x3081 = Black, 0x3082 = White, 0x3083 = Transparent
        self.assertEqual(
            detect_flipper_adv("Dev", ["00003083-0000-1000-8000-00805f9b34fb"]),
            "Transparent",
        )
        self.assertEqual(
            detect_flipper_adv("Dev", ["00003082-0000-1000-8000-00805f9b34fb"]),
            "White",
        )
        self.assertIsNone(detect_flipper_adv("Headphones", ["0000110b-0000-1000-8000-00805f9b34fb"]))

    def test_clean_dbus_error_strips_glib_quark_boilerplate(self):
        # Ensure full BlueZ error reason fits in 50-column handheld status bar
        raw = RuntimeError(
            "g-io-error-quark: GDBus.Error:org.bluez.Error.ConnectionAttemptFailed: Page Timeout (36)"
        )
        cleaned = clean_dbus_error(raw)
        self.assertEqual(cleaned, "ConnectionAttemptFailed: Page Timeout")
        self.assertLessEqual(len(cleaned), 50)

    def test_credit_flow_big_endian_and_rpc_roundtrip(self):
        sim = SimulatedFlipperTransport(initial_credits=100)
        rpc = FlipperRpcClient(sim)

        self.assertEqual(rpc.ping(b"hello-zero"), b"hello-zero")
        info = rpc.device_info()
        self.assertEqual(info["hardware_name"], "ZeroSim")
        self.assertEqual(info["firmware_commit"], "0.105.0")

        # Multi-chunk write larger than initial 100-byte BLE flow-control window
        big_blob = b"A" * 1350 + b"\nEND\n"
        rpc.storage_write("/ext/apps_data/zero2zero/test.bin", big_blob, chunk_size=300)
        read_back = rpc.storage_read("/ext/apps_data/zero2zero/test.bin")
        self.assertEqual(read_back, big_blob)

        # Missing file stat returns None cleanly
        self.assertIsNone(rpc.storage_stat("/ext/does_not_exist.txt"))
        st = rpc.storage_stat("/ext/subghz/Gate.sub")
        self.assertIsNotNone(st)
        self.assertEqual(st.size, len(sim.files["/ext/subghz/Gate.sub"]))

    def test_bidirectional_app_data_exchange(self):
        sim = SimulatedFlipperTransport()
        rpc = FlipperRpcClient(sim)
        got = []
        ev = threading.Event()

        def on_fap_frame(b: bytes):
            got.append(b)
            ev.set()

        rpc.set_app_callbacks(on_data=on_fap_frame)
        rpc.app_send_data(b'{"op":"ping"}')
        self.assertEqual(sim.received_app_frames, [b'{"op":"ping"}'])

        sim.simulate_fap_push(b'{"id":7,"op":"net.scan"}')
        self.assertTrue(ev.wait(timeout=2.0))
        self.assertEqual(got, [b'{"id":7,"op":"net.scan"}'])

    def test_screen_stream_input_sequence_rtc_and_gentle_alerts(self):
        sim = SimulatedFlipperTransport()
        rpc = FlipperRpcClient(sim)

        # 1. Gentle alert defaults to 'vibro' -> 0 loud Find-My-Flipper sirens
        self.assertEqual(rpc.read_flipper_alert_mode(), "vibro")
        self.assertEqual(rpc.trigger_gentle_alert(), "vibro")
        self.assertEqual(sim.loud_alert_count, 0)

        rpc.write_flipper_alert_mode("loud")
        self.assertEqual(rpc.read_flipper_alert_mode(), "loud")
        self.assertEqual(rpc.trigger_gentle_alert(), "loud")
        self.assertEqual(sim.loud_alert_count, 1)

        # 2. RTC DateTime roundtrip
        target_dt = FlipperDateTime(hour=23, minute=45, second=12, day=13, month=11, year=2026, weekday=5)
        rpc.set_datetime(target_dt)
        got_dt = rpc.get_datetime()
        self.assertEqual(got_dt.format_iso(), "2026-11-13 23:45:12")
        self.assertEqual(got_dt.weekday, 5)

        # 3. Screen stream u8g2 vertical-tile pixel decoding + HORIZONTAL_FLIP
        frame, orient, seq = rpc.gui_start_screen_stream(wait_first_frame_sec=1.0)
        self.assertEqual(len(frame), 1024)
        self.assertEqual(orient, 0)
        self.assertGreaterEqual(seq, 1)
        self.assertTrue(screen_pixel_on(frame, 0, 0, orientation=0))
        self.assertTrue(screen_pixel_on(frame, 127, 7, orientation=0))
        self.assertFalse(screen_pixel_on(frame, 1, 0, orientation=0))
        # Left-handed HORIZONTAL_FLIP (1) mirrors (0,0) -> (127,63)
        self.assertTrue(screen_pixel_on(frame, 127, 63, orientation=1))
        uni = render_screen_frame_unicode(frame, orientation=0)
        self.assertEqual(len(uni.splitlines()), 32)
        self.assertEqual(uni[0], "▀")
        rpc.gui_stop_screen_stream()
        self.assertFalse(sim.screen_streaming)

        # 4. 3-step synthetic input sequence (PRESS=0 -> SHORT=2 -> RELEASE=1)
        sim.gui_inputs.clear()
        rpc.gui_send_input("ok", mode="short")
        self.assertEqual(sim.gui_inputs, [(4, 0), (4, 2), (4, 1)])

        # 5. Open capture in native Flipper app (unlocking previous app first)
        sim.app_locked = True
        app_nm = rpc.open_file_in_flipper_app("/ext/nfc/Badge.nfc")
        self.assertEqual(app_nm, "NFC")
        self.assertEqual(sim.started_apps[-1], ("NFC", "/ext/nfc/Badge.nfc"))

    def test_ble_transport_closed_flow_clears_is_connected(self):
        # If `on_props_changed` closes `_flow` while `_connected` was set,
        # `is_connected` must immediately report False so `_ensure_connected` reconnects.
        bt = BluezBleTransport()
        bt._connected = True
        bt._char_rx = "/org/bluez/hci0/dev_80_E1_26_1B_A7_6D/service000c/char000d"
        self.assertTrue(bt.is_connected)

        bt._flow.close()
        self.assertTrue(bt._flow.is_closed)
        self.assertFalse(bt.is_connected)
        with self.assertRaises(ConnectionError):
            bt.send_bytes(b"\x01\x02")
        self.assertFalse(bt._connected)

    def test_ble_transport_send_bytes_mtu_clamp_and_att_0x0e_fallback(self):
        # Verify `BluezBleTransport` defaults to `command` (`WRITE_WITHOUT_RESPONSE`)
        # and cleanly clamps to 20-byte `command` slices if BlueZ rejects an oversized chunk or
        # `ATT error: 0x0e` occurs, preserving exact payload framing.
        bt = BluezBleTransport()
        bt._connected = True
        bt._char_rx = "/org/bluez/hci0/dev_80_E1_26_1B_A7_6D/service000c/char000d"
        self.assertEqual(bt._tx_write_mode, "command")

        sent_calls: list[tuple[bytes, str]] = []

        def fake_write(_path: str, chunk: bytes, mode: str = "command") -> None:
            if mode == "request" or len(chunk) > 20:
                raise RuntimeError(
                    "g-io-error-quark: GDBus.Error:org.bluez.Error.Failed: Operation failed with ATT error: 0x0e (36)"
                )
            sent_calls.append((chunk, mode))

        bt._tx_write_mode = "request"
        bt._write_gatt_char = fake_write  # type: ignore[method-assign]
        payload = bytes(range(75))
        bt.send_bytes(payload)

        self.assertEqual(bt._tx_write_mode, "command")
        self.assertEqual(bt._flow.max_chunk, 20)
        self.assertTrue(all(mode == "command" and len(c) <= 20 for c, mode in sent_calls))
        self.assertEqual(b"".join(c for c, _ in sent_calls), payload)

    def test_command_id_zero_error_decode_fast_fails_stream(self):
        # Flipper `rpc.c:322` emits `Main(command_id=0, command_status=ERROR_DECODE)`
        # on stream framing drop. `FlipperRpcClient` must wake pending `call_stream` immediately
        # and stop sending remaining envelopes instead of hanging 30s!
        sim = SimulatedFlipperTransport(initial_credits=1024)
        rpc = FlipperRpcClient(sim)
        frames_seen = [0]
        orig_handle = sim._handle_rpc

        def decode_drop_on_second_chunk(req: MainEnvelope, write_accum: bytearray) -> None:
            if req.content_field == F_STORAGE_WRITE_REQ:
                frames_seen[0] += 1
                if frames_seen[0] == 2:
                    sim._emit_to_host(
                        MainEnvelope(0, CommandStatus.ERROR_DECODE, False, F_EMPTY, b"")
                    )
                    return
            orig_handle(req, write_accum)

        sim._handle_rpc = decode_drop_on_second_chunk  # type: ignore[method-assign]
        t0 = time.monotonic()
        with self.assertRaises(FlipperRpcError) as ctx:
            rpc.storage_write("/ext/apps_data/big.bin", b"X" * 2560, chunk_size=256, timeout_sec=10.0)
        elapsed = time.monotonic() - t0
        self.assertEqual(ctx.exception.status_code, CommandStatus.ERROR_DECODE)
        self.assertLess(elapsed, 0.5)
        self.assertLess(frames_seen[0], 10)  # Stopped early instead of sending all 10 chunks!

    def test_kernel_mgmt_transact_skips_broadcasts_and_parses_sc_bit(self):
        # Verify `_mgmt_transact` ignores unrelated broadcast events (`MGMT_EV_NEW_SETTINGS`)
        # and accurately extracts `MGMT_SETTING_SECURE_CONN` (`1 << 11`) from `MGMT_OP_READ_INFO`.
        import socket as py_sock
        from z2z.ble_transport import (
            MGMT_EV_CMD_COMPLETE,
            MGMT_OP_READ_INFO,
            MGMT_OP_SET_SECURE_CONN,
            MGMT_SETTING_POWERED,
            MGMT_SETTING_SECURE_CONN,
            _mgmt_transact,
        )

        c_sock, k_sock = py_sock.socketpair(py_sock.AF_UNIX, py_sock.SOCK_DGRAM)
        try:
            def fake_kernel() -> None:
                # 1. Read MGMT_OP_READ_INFO -> emit unrelated broadcast first, then CMD_COMPLETE with SC=1
                req1 = k_sock.recv(1024)
                op1, idx1, _ = struct.unpack_from("<HHH", req1, 0)
                self.assertEqual((op1, idx1), (MGMT_OP_READ_INFO, 0))
                k_sock.sendall(struct.pack("<HHHI", 0x0006, 0, 4, 0x0801))  # MGMT_EV_NEW_SETTINGS broadcast
                rp_info = (
                    b"\x11\x22\x33\x44\x55\x66"
                    + b"\x07"
                    + struct.pack("<HII", 15, 0x1FFFF, MGMT_SETTING_POWERED | MGMT_SETTING_SECURE_CONN)
                )
                body1 = struct.pack("<HB", op1, 0x00) + rp_info
                k_sock.sendall(struct.pack("<HHH", MGMT_EV_CMD_COMPLETE, 0, len(body1)) + body1)

                # 2. Read MGMT_OP_SET_SECURE_CONN(0) -> simulate 0x11 (MGMT_STATUS_POWERED) rejection!
                req2 = k_sock.recv(1024)
                op2, idx2, plen2 = struct.unpack_from("<HHH", req2, 0)
                self.assertEqual((op2, idx2, plen2, req2[6:7]), (MGMT_OP_SET_SECURE_CONN, 0, 1, b"\x00"))
                body2 = struct.pack("<HB", op2, 0x11)
                k_sock.sendall(struct.pack("<HHH", 0x0002, 0, len(body2)) + body2)

            thr = threading.Thread(target=fake_kernel, daemon=True)
            thr.start()

            st1, rp1 = _mgmt_transact(c_sock, MGMT_OP_READ_INFO, 0, timeout_sec=2.0)
            self.assertEqual(st1, 0)
            _, curr_mask = struct.unpack_from("<II", rp1, 9)
            self.assertTrue(bool(curr_mask & MGMT_SETTING_SECURE_CONN))

            st2, _ = _mgmt_transact(c_sock, MGMT_OP_SET_SECURE_CONN, 0, b"\x00", timeout_sec=2.0)
            self.assertEqual(st2, 0x11)
            thr.join(timeout=1.0)
        finally:
            c_sock.close()
            k_sock.close()

    def test_disconnect_wakes_pending_rpc_and_sequential_gui_input_avoids_tx_wedge(self):
        # Verify (1) short D-pad click sends PRESS, SHORT, RELEASE as 3
        # Indication-paced RPC calls (avoiding Flipper `ble_svc_serial_update_tx` CPU2 wedge),
        # and (2) BLE disconnect mid-RPC wakes `call_stream` in < 0.4s instead of hanging 10s!
        sim = SimulatedFlipperTransport(initial_credits=1024)
        rpc = FlipperRpcClient(sim)

        before_sends = sim.send_calls
        sim.gui_inputs.clear()
        rpc.gui_send_input("right", mode="short")
        self.assertEqual(sim.gui_inputs, [(2, 0), (2, 2), (2, 1)])
        self.assertEqual(sim.send_calls - before_sends, 3)

        orig_handle = sim._handle_rpc

        def hang_then_drop(req: MainEnvelope, write_accum: bytearray) -> None:
            if req.content_field == F_SYS_PING_REQ:
                threading.Timer(0.06, sim.disconnect).start()
                return
            orig_handle(req, write_accum)

        sim._handle_rpc = hang_then_drop  # type: ignore[method-assign]
        t0 = time.monotonic()
        with self.assertRaises(ConnectionError):
            rpc.ping(b"drop", timeout_sec=10.0)
        self.assertLess(time.monotonic() - t0, 0.4)

    def test_mgmt_load_conn_param_and_le_1m_phy_selection(self):
        # Verify MGMT_OP_LOAD_CONN_PARAM (0x0035) layout + LE 1M PHY mask +
        # GET_PHY_CONFIGURATION / SET_PHY_CONFIGURATION / LOAD_CONN_PARAM socket exchange.
        import socket as py_sock
        from z2z.ble_transport import (
            BDADDR_LE_PUBLIC,
            BDADDR_LE_RANDOM,
            MGMT_EV_CMD_COMPLETE,
            MGMT_OP_GET_PHY_CONFIGURATION,
            MGMT_OP_LOAD_CONN_PARAM,
            MGMT_OP_SET_PHY_CONFIGURATION,
            MGMT_PHY_LE_1M_RX,
            MGMT_PHY_LE_1M_TX,
            MGMT_PHY_LE_2M_RX,
            MGMT_PHY_LE_2M_TX,
            MGMT_PHY_LE_CODED_RX,
            MGMT_PHY_LE_CODED_TX,
            _compute_le_1m_phy_selection,
            _mgmt_transact,
            _pack_mgmt_load_conn_params,
        )

        cp = _pack_mgmt_load_conn_params("80:E1:26:01:02:03")
        self.assertEqual(len(cp), 32)
        (cnt,) = struct.unpack_from("<H", cp, 0)
        self.assertEqual(cnt, 2)
        addr0, at0, min0, max0, lat0, to0 = struct.unpack_from("<6sBHHHH", cp, 2)
        addr1, at1, min1, max1, lat1, to1 = struct.unpack_from("<6sBHHHH", cp, 17)
        self.assertEqual(addr0, b"\x03\x02\x01\x26\xe1\x80")
        self.assertEqual(addr1, addr0)
        self.assertEqual((at0, at1), (BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM))
        self.assertEqual((min0, max0, lat0, to0), (9, 14, 0, 500))
        self.assertEqual((min1, max1, lat1, to1), (9, 14, 0, 500))

        # Preserve unconfigurable bits + BR/EDR (0x1ff), enable LE 1M TX|RX, clear LE 2M/Coded
        bredr_all = 0x1FF
        supported = bredr_all | MGMT_PHY_LE_1M_TX | MGMT_PHY_LE_1M_RX | MGMT_PHY_LE_2M_TX | MGMT_PHY_LE_2M_RX | MGMT_PHY_LE_CODED_TX
        configurable = MGMT_PHY_LE_1M_TX | MGMT_PHY_LE_1M_RX | MGMT_PHY_LE_2M_TX | MGMT_PHY_LE_2M_RX
        selected = supported
        desired = _compute_le_1m_phy_selection(supported, configurable, selected)
        self.assertEqual(desired & bredr_all, bredr_all)
        self.assertTrue(desired & MGMT_PHY_LE_1M_TX)
        self.assertTrue(desired & MGMT_PHY_LE_1M_RX)
        self.assertFalse(desired & (MGMT_PHY_LE_2M_TX | MGMT_PHY_LE_2M_RX))
        self.assertTrue(desired & MGMT_PHY_LE_CODED_TX)  # unconfigurable bit preserved

        c_sock, k_sock = py_sock.socketpair(py_sock.AF_UNIX, py_sock.SOCK_DGRAM)
        try:
            def fake_kernel() -> None:
                req1 = k_sock.recv(1024)
                op1, idx1, _ = struct.unpack_from("<HHH", req1, 0)
                self.assertEqual((op1, idx1), (MGMT_OP_GET_PHY_CONFIGURATION, 0))
                body1 = struct.pack("<HBIII", op1, 0, supported, configurable, selected)
                k_sock.sendall(struct.pack("<HHH", MGMT_EV_CMD_COMPLETE, 0, len(body1)) + body1)

                req2 = k_sock.recv(1024)
                op2, idx2, plen2 = struct.unpack_from("<HHH", req2, 0)
                self.assertEqual((op2, idx2, plen2), (MGMT_OP_SET_PHY_CONFIGURATION, 0, 4))
                self.assertEqual(struct.unpack_from("<I", req2, 6)[0], desired)
                body2 = struct.pack("<HB", op2, 0)
                k_sock.sendall(struct.pack("<HHH", MGMT_EV_CMD_COMPLETE, 0, len(body2)) + body2)

                req3 = k_sock.recv(1024)
                op3, idx3, plen3 = struct.unpack_from("<HHH", req3, 0)
                self.assertEqual((op3, idx3, plen3), (MGMT_OP_LOAD_CONN_PARAM, 0, 32))
                self.assertEqual(req3[6:38], cp)
                body3 = struct.pack("<HB", op3, 0)
                k_sock.sendall(struct.pack("<HHH", MGMT_EV_CMD_COMPLETE, 0, len(body3)) + body3)

            thr = threading.Thread(target=fake_kernel, daemon=True)
            thr.start()

            st1, rp1 = _mgmt_transact(c_sock, MGMT_OP_GET_PHY_CONFIGURATION, 0, timeout_sec=2.0)
            self.assertEqual(st1, 0)
            s_p, c_p, cur_p = struct.unpack_from("<III", rp1, 0)
            target_p = _compute_le_1m_phy_selection(s_p, c_p, cur_p)
            st2, _ = _mgmt_transact(
                c_sock, MGMT_OP_SET_PHY_CONFIGURATION, 0, struct.pack("<I", target_p), timeout_sec=2.0
            )
            self.assertEqual(st2, 0)
            st3, _ = _mgmt_transact(c_sock, MGMT_OP_LOAD_CONN_PARAM, 0, cp, timeout_sec=2.0)
            self.assertEqual(st3, 0)
            thr.join(timeout=1.0)
        finally:
            c_sock.close()
            k_sock.close()

    def test_passive_battery_lookup_avoids_gatt_read_and_disconnect_skips_when_already_down(self):
        from z2z.ble_transport import BleDeviceSummary, BluezBleTransport

        tr = BluezBleTransport()
        tr._connected = True
        tr._dev_path = "/org/bluez/hci0/dev_80_E1_26_01_02_03"
        tr._char_batt = tr._dev_path + "/service0020/char0021"
        tr._dev_info = BleDeviceSummary(
            address="80:E1:26:01:02:03",
            name="Flipper Test01",
            rssi=-40,
            paired=True,
            trusted=True,
            connected=True,
            shell_color="White",
            object_path=tr._dev_path,
        )

        tr._get_managed_objects = lambda: {  # type: ignore[method-assign]
            tr._dev_path: {
                "org.bluez.Device1": {"Connected": True, "RSSI": -29},
                "org.bluez.Battery1": {"Percentage": 87},
            }
        }

        def _forbid_gatt_read(_path: str) -> bytes:
            raise AssertionError("link_info() must not issue active ATT ReadValue for battery")

        tr._read_gatt_char = _forbid_gatt_read  # type: ignore[method-assign]
        info = tr.link_info()
        self.assertTrue(info["connected"])
        self.assertEqual(info["rssi"], -29)
        self.assertEqual(info["ble_battery_pct"], 87)

        # When already disconnected remotely, disconnect() must not call D-Bus Device1.Disconnect
        class _FakeBus:
            def call_sync(self, *args, **kwargs):
                raise AssertionError("disconnect() must skip D-Bus Disconnect when already down")

        tr._connected = False
        tr._bus = _FakeBus()
        tr.disconnect()

    def test_screen_listener_fast_long_input_and_acquire_write_fast_path(self):
        import socket as py_sock
        from z2z.ble_transport import BluezBleTransport

        sim = SimulatedFlipperTransport()
        rpc = FlipperRpcClient(sim)

        events = []

        def on_frame(frame: bytes, orient: int, seq: int, streaming: bool) -> None:
            events.append((len(frame), orient, seq, streaming))

        rpc.add_screen_listener(on_frame)
        rpc.gui_start_screen_stream(wait_first_frame_sec=1.0)
        self.assertGreaterEqual(len(events), 1)
        self.assertGreater(rpc.last_screen_frame_mono, 0.0)
        self.assertTrue(events[-1][3])

        # `gui_send_input("back", mode="long")` has ZERO 260ms artificial delay
        t0 = time.monotonic()
        rpc.gui_send_input("back", mode="long", timeout_sec=1.0)
        self.assertLess(time.monotonic() - t0, 0.10)
        self.assertEqual(sim.gui_inputs[-3:], [(5, 0), (5, 3), (5, 1)])

        # Stopping stream notifies listeners immediately with streaming=False
        rpc.mark_screen_stream_stopped()
        self.assertFalse(events[-1][3])
        rpc.remove_screen_listener(on_frame)

        # AcquireWrite FD fast path + FD cleanup and ConnectionError on closed peer
        a_sock, b_sock = py_sock.socketpair(py_sock.AF_UNIX, py_sock.SOCK_SEQPACKET)
        try:
            tr = BluezBleTransport()
            tr._connected = True
            tr._char_rx = "/org/bluez/hci0/dev_XX/service0001/char0002"
            tr._rx_write_fd = a_sock.detach()

            tr._write_gatt_char(tr._char_rx, b"PING_FAST", "command")
            self.assertEqual(b_sock.recv(64), b"PING_FAST")

            b_sock.close()
            with self.assertRaises(ConnectionError):
                tr._write_gatt_char(tr._char_rx, b"AFTER_CLOSE", "command")
            self.assertEqual(tr._rx_write_fd, -1)
            self.assertFalse(tr.is_connected)
        finally:
            try:
                b_sock.close()
            except Exception:
                pass

    def test_post_crash_bond_auth_detection_low_credit_refresh_and_safe_app_unlock(self):
        import socket as py_sock
        from z2z.ble_transport import (
            BDADDR_LE_PUBLIC,
            BluezBleTransport,
            MGMT_DEV_DISCONN_AUTH_FAILURE,
            MGMT_EV_AUTH_FAILED,
            MGMT_EV_DEVICE_DISCONNECTED,
            _parse_mgmt_auth_failure,
        )
        from z2z.nfc_tools import mfd_to_nfc

        # 1. MGMT `0x000C` (`MGMT_EV_DEVICE_DISCONNECTED`) `reason=0x04` (`PIN or Key Missing`)
        bdaddr_le = b"\x03\x02\x01\x26\xe1\x80"  # 80:E1:26:01:02:03 little-endian
        pkt_auth_disc = struct.pack(
            "<HHH6sBB",
            MGMT_EV_DEVICE_DISCONNECTED,
            0,
            8,
            bdaddr_le,
            BDADDR_LE_PUBLIC,
            MGMT_DEV_DISCONN_AUTH_FAILURE,
        )
        pkt_user_disc = struct.pack(
            "<HHH6sBB",
            MGMT_EV_DEVICE_DISCONNECTED,
            0,
            8,
            bdaddr_le,
            BDADDR_LE_PUBLIC,
            0x02,  # local host terminated (not auth failure)
        )
        pkt_auth_ev = struct.pack(
            "<HHH6sBB",
            MGMT_EV_AUTH_FAILED,
            0,
            8,
            bdaddr_le,
            BDADDR_LE_PUBLIC,
            0x06,  # HCI PIN or Key Missing
        )
        self.assertTrue(_parse_mgmt_auth_failure(pkt_auth_disc, 0, "80:E1:26:01:02:03"))
        self.assertFalse(_parse_mgmt_auth_failure(pkt_user_disc, 0, "80:E1:26:01:02:03"))
        self.assertFalse(_parse_mgmt_auth_failure(pkt_auth_disc, 1, "80:E1:26:01:02:03"))
        self.assertFalse(_parse_mgmt_auth_failure(pkt_auth_disc, 0, "80:E1:26:AA:BB:CC"))
        self.assertTrue(_parse_mgmt_auth_failure(pkt_auth_ev, 0, "80:E1:26:01:02:03"))

        r_sock, w_sock = py_sock.socketpair(py_sock.AF_UNIX, py_sock.SOCK_DGRAM)
        r_sock.setblocking(False)
        try:
            tr = BluezBleTransport()
            w_sock.sendall(pkt_user_disc)
            w_sock.sendall(pkt_auth_disc)
            self.assertTrue(
                tr._drain_mgmt_auth_failure(
                    r_sock, "/org/bluez/hci0", "80:E1:26:01:02:03", wait_sec=0.05
                )
            )
        finally:
            r_sock.close()
            w_sock.close()

        # 2. Proactive low-credit `sync_flow_credits()` refresh before `credits=3` cliff
        a_sock, b_sock = py_sock.socketpair(py_sock.AF_UNIX, py_sock.SOCK_SEQPACKET)
        try:
            tr2 = BluezBleTransport()
            tr2._connected = True
            tr2._char_rx = "/org/bluez/hci0/dev_XX/service0001/char0002"
            tr2._char_flow = "/org/bluez/hci0/dev_XX/service0001/char0003"
            tr2._rx_write_fd = a_sock.detach()
            tr2._chunk_delay_sec = 0.0
            tr2._flow.update_from_be_bytes(struct.pack(">I", 3))
            flow_reads = [0]

            def fake_read_flow(cpath: str) -> bytes:
                self.assertEqual(cpath, tr2._char_flow)
                flow_reads[0] += 1
                return struct.pack(">I", 1024)

            tr2._read_gatt_char = fake_read_flow  # type: ignore[method-assign]
            tr2.send_bytes(b"0123456789ABCDEF", timeout_sec=1.0)
            self.assertEqual(flow_reads[0], 1)
            self.assertEqual(b_sock.recv(64), b"0123456789ABCDEF")
            self.assertEqual(tr2._flow.credits, 1024 - 16)
        finally:
            try:
                b_sock.close()
            except Exception:
                pass

        # 3. `ensure_app_unlocked`: native non-RPC app (`ERROR_APP_NOT_RUNNING`) + slow RF teardown
        # must NOT fire a 2nd `BACK` mid-teardown!
        sim = SimulatedFlipperTransport()
        sim.app_locked = True
        rpc = FlipperRpcClient(sim)
        rpc._unlock_poll_sec = 0.005
        rpc._gui_idle_step_gap_sec = 0.001

        orig_handle = sim._handle_rpc
        lock_polls_after_back = [0]

        def native_nfc_app_handle(req: MainEnvelope, write_accum: bytearray) -> None:
            if req.content_field == F_APP_EXIT_REQ:
                # Native NFC app returns ERROR_APP_NOT_RUNNING without unlocking!
                sim._emit_to_host(
                    MainEnvelope(req.command_id, CommandStatus.ERROR_APP_NOT_RUNNING, False, F_EMPTY, b"")
                )
                return
            if req.content_field == F_APP_LOCK_STATUS_REQ and sim.gui_inputs:
                lock_polls_after_back[0] += 1
                # Simulate ST25R3916 RF teardown finishing on the 2nd poll after a single BACK!
                if lock_polls_after_back[0] >= 2:
                    sim.app_locked = False
            orig_handle(req, write_accum)

        sim._handle_rpc = native_nfc_app_handle  # type: ignore[method-assign]
        rpc.ensure_app_unlocked()
        self.assertFalse(sim.app_locked)
        # Exactly ONE `BACK` (`PRESS, SHORT, RELEASE` = 3 input events), NOT two!
        self.assertEqual(sim.gui_inputs, [(5, 0), (5, 2), (5, 1)])

        # 4. `mfd_to_nfc` emits `Signature`, `Mifare version`, and `Counter`/`Tearing` for SDK 1.5.1
        ntag215_bin = b"\x04\x11\x22\x88\x33\x44\x55\x66" + b"\x00" * (135 * 4 - 8)
        nfc_txt = mfd_to_nfc(ntag215_bin)
        self.assertIn("NTAG/Ultralight type: NTAG215", nfc_txt)
        self.assertIn("Signature: " + " ".join(["00"] * 32), nfc_txt)
        self.assertIn("Mifare version: 00 04 04 02 01 00 11 03", nfc_txt)
        self.assertIn("Counter 0: 0\nTearing 0: 00", nfc_txt)
        self.assertIn("Pages total: 135", nfc_txt)

        # 5. Mid-exit `.fap` (`app_exit() -> False`, `loader_lock` clearing on settle
        # poll #2) unlocks with ZERO `BACK` inputs; locked Desktop raises clean `FlipperRpcError`
        # with ZERO `BACK` inputs; stubborn app sends at most 2 `BACK`s (< 3, never locking keypad)
        # and raises `FlipperRpcError(ERROR_APP_SYSTEM_LOCKED)` without `TypeError`.
        sim_exit = SimulatedFlipperTransport()
        sim_exit.app_locked = True
        rpc_exit = FlipperRpcClient(sim_exit)
        rpc_exit._unlock_poll_sec = 0.002
        rpc_exit._gui_idle_step_gap_sec = 0.001
        orig_exit_h = sim_exit._handle_rpc
        settle_polls = [0]

        def mid_exit_handle(req: MainEnvelope, write_accum: bytearray) -> None:
            if req.content_field == F_APP_EXIT_REQ:
                sim_exit._emit_to_host(
                    MainEnvelope(req.command_id, CommandStatus.ERROR_APP_NOT_RUNNING, False, F_EMPTY, b"")
                )
                return
            if req.content_field == F_APP_LOCK_STATUS_REQ:
                settle_polls[0] += 1
                if settle_polls[0] >= 3:
                    sim_exit.app_locked = False
            orig_exit_h(req, write_accum)

        sim_exit._handle_rpc = mid_exit_handle  # type: ignore[method-assign]
        rpc_exit.ensure_app_unlocked()
        self.assertFalse(sim_exit.app_locked)
        self.assertEqual(sim_exit.gui_inputs, [])

        # Desktop keypad locked -> immediate FlipperRpcError, 0 BACK presses
        sim_desk = SimulatedFlipperTransport()
        sim_desk.app_locked = True
        sim_desk.desktop_locked = True
        rpc_desk = FlipperRpcClient(sim_desk)
        with self.assertRaises(FlipperRpcError) as ctx_desk:
            rpc_desk.ensure_app_unlocked()
        self.assertEqual(ctx_desk.exception.status_code, CommandStatus.ERROR_APP_SYSTEM_LOCKED)
        self.assertIn("keypad is locked", str(ctx_desk.exception))
        self.assertEqual(sim_desk.gui_inputs, [])

        # Stubborn native app -> caps at 2 BACKs (6 events) and raises clean FlipperRpcError
        sim_stubborn = SimulatedFlipperTransport()
        sim_stubborn.app_locked = True
        rpc_stubborn = FlipperRpcClient(sim_stubborn)
        rpc_stubborn._unlock_poll_sec = 0.001
        rpc_stubborn._gui_idle_step_gap_sec = 0.001
        orig_stub_h = sim_stubborn._handle_rpc

        def stubborn_handle(req: MainEnvelope, write_accum: bytearray) -> None:
            if req.content_field == F_APP_EXIT_REQ:
                sim_stubborn._emit_to_host(
                    MainEnvelope(req.command_id, CommandStatus.ERROR_APP_NOT_RUNNING, False, F_EMPTY, b"")
                )
                return
            orig_stub_h(req, write_accum)

        sim_stubborn._handle_rpc = stubborn_handle  # type: ignore[method-assign]
        with self.assertRaises(FlipperRpcError) as ctx_stub:
            rpc_stubborn.ensure_app_unlocked()
        self.assertEqual(ctx_stub.exception.status_code, CommandStatus.ERROR_APP_SYSTEM_LOCKED)
        self.assertIn("still open", str(ctx_stub.exception))
        self.assertEqual(len(sim_stubborn.gui_inputs), 6)  # 2 * (PRESS, SHORT, RELEASE)


if __name__ == "__main__":
    unittest.main()
