"""Unit and integration tests for Phase 2: Sub-GHz, Studio, Vault, Jobs, and Daemon IPC."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "python"))
sys.path.insert(0, os.path.dirname(__file__))

from test_proto_and_rpc import SimulatedFlipperTransport
from z2z.ble_transport import BleDeviceSummary
from z2z.captures import inspect_capture_bytes
from z2z.daemon import MAILBOX_REQ, MAILBOX_RESP, Zero2ZeroDaemon
from z2z.jobs import JobSpec, execute_job
from z2z.proto import StorageFile
from z2z.rpc_client import FlipperRpcClient
from z2z.studio import build_ducky_script, render_template_text
from z2z.subghz import MAX_RAW_VALUES_PER_LINE, parse_and_analyze_sub, synthesize_raw_sub
from z2z.vault import sync_vault


class PairableSimTransport(SimulatedFlipperTransport):
    def __init__(self) -> None:
        super().__init__(initial_credits=512)
        self.paired_pin = ""
        self.is_paired = False
        self.last_force = False

    def list_known_flippers(self):
        return [
            BleDeviceSummary(
                address="80:E1:26:AA:BB:CC",
                name="Flipper ZeroSim",
                rssi=-50,
                paired=self.is_paired,
                trusted=self.is_paired,
                connected=self.is_connected,
                shell_color="Black",
            )
        ]

    def pair(self, target, pin_provider, timeout_sec=10.0, force=False):
        self.last_force = bool(force)
        if self.is_paired and not force:
            return self.list_known_flippers()[0]
        self.paired_pin = pin_provider("Flipper ZeroSim", "80:E1:26:AA:BB:CC")
        self.is_paired = True
        return self.list_known_flippers()[0]

    def unpair(self, target=None):
        self.disconnect()
        self.is_paired = False
        return {"removed": True, "address": "80:E1:26:AA:BB:CC", "name": "Flipper ZeroSim"}


class OffloadAndDaemonTests(unittest.TestCase):
    def test_subghz_synthesize_and_decode_roundtrip(self):
        # 24-bit payload repeated 6 times (> 288 pulses, tests clustering & consensus)
        raw_sub = synthesize_raw_sub(
            "0xA5C3F0", frequency=433920000, te_us=350, repeat=12, gap_us=11000
        )
        for line in raw_sub.splitlines():
            if line.startswith("RAW_Data:"):
                vals = line.split(":", 1)[1].split()
                self.assertLessEqual(len(vals), MAX_RAW_VALUES_PER_LINE)

        rep = parse_and_analyze_sub(raw_sub)
        self.assertEqual(rep.frequency, 433920000)
        self.assertEqual(rep.frame_count, 12)
        self.assertAlmostEqual(rep.estimated_te_us, 350, delta=30)
        self.assertEqual(rep.consensus_bits, 24)
        self.assertEqual(rep.consensus_hex, "A5C3F0")

    def test_studio_template_rendering(self):
        tpl = "HOST={{HOST_IP}} CUSTOM={{TOKEN}} UNTOUCHED={{UNKNOWN_VAR}}"
        out = render_template_text(tpl, {"HOST_IP": "10.55.0.1", "TOKEN": "abc123"})
        self.assertEqual(out, "HOST=10.55.0.1 CUSTOM=abc123 UNTOUCHED={{UNKNOWN_VAR}}")

    def test_vault_incremental_md5_dedup(self):
        sim = PairableSimTransport()
        sub_blob = synthesize_raw_sub("0xCAFE12", repeat=4).encode("utf-8")
        md5_hex = hashlib.md5(sub_blob).hexdigest()
        sim.files["/ext/subghz/Gate.sub"] = sub_blob

        # Override ListRequest to return exact MD5 for /ext/subghz
        orig_handle = sim._handle_rpc

        def custom_handle(req, write_accum):
            if req.content_field == 7:
                from z2z.proto import F_STORAGE_LIST_RESP, MainEnvelope, _f_submsg, parse_single_str

                p = parse_single_str(req.content_bytes)
                if p == "/ext/subghz":
                    sf = StorageFile(
                        is_dir=False, name="Gate.sub", size=len(sub_blob), md5sum=md5_hex
                    )
                    sim._emit_to_host(
                        MainEnvelope(
                            req.command_id, 0, False, F_STORAGE_LIST_RESP, _f_submsg(1, sf.encode())
                        )
                    )
                else:
                    sim._emit_to_host(MainEnvelope(req.command_id, 0, False, 4, b""))
                return
            orig_handle(req, write_accum)

        sim._handle_rpc = custom_handle
        rpc = FlipperRpcClient(sim)

        with tempfile.TemporaryDirectory() as tmp:
            vdir = Path(tmp)
            r1 = sync_vault(rpc, vault_dir=vdir)
            self.assertEqual(r1["pulled_count"], 1)
            self.assertEqual(r1["skipped_count"], 0)

            # Second sync must skip unchanged file (0 bytes over BLE)
            r2 = sync_vault(rpc, vault_dir=vdir)
            self.assertEqual(r2["pulled_count"], 0)
            self.assertEqual(r2["skipped_count"], 1)
            self.assertEqual(r2["bytes_transferred"], 0)

    def test_job_runner_pull_process_and_merge_lines(self):
        sim = PairableSimTransport()
        sim.files["/ext/apps_data/sample.in"] = b"alpha\nbeta\n"
        sim.files["/ext/apps_data/dict.txt"] = b"# Header\nALPHA\n"
        rpc = FlipperRpcClient(sim)

        spec = JobSpec(
            id="upper-merge",
            title="Uppercase & Merge",
            pull_files=[{"remote": "/ext/apps_data/sample.in", "local": "sample.in"}],
            command=[
                "python3",
                "-c",
                "import pathlib; d=pathlib.Path('sample.in').read_text().upper(); pathlib.Path('out.txt').write_text(d + 'GAMMA\\n'); print('processed')",
            ],
            merge_lines_into={"local": "out.txt", "remote": "/ext/apps_data/dict.txt"},
            alert_on_finish=True,
        )
        res = execute_job(spec, rpc=rpc)
        self.assertEqual(res["returncode"], 0)
        self.assertEqual(res["merged_added"], 2)  # BETA and GAMMA added; ALPHA deduplicated
        merged = sim.files["/ext/apps_data/dict.txt"].decode("utf-8").splitlines()
        self.assertEqual(merged, ["# Header", "ALPHA", "BETA", "GAMMA"])

    def test_daemon_ipc_pin_handshake_duplex_and_mailbox(self):
        sim = PairableSimTransport()
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = os.path.join(tmp, "rpc.sock")
            daemon = Zero2ZeroDaemon(
                socket_path=sock_path,
                transport=sim,
                idle_timeout_sec=30.0,
                watch_enabled=True,
                watch_interval_sec=0.15,
            )
            t = threading.Thread(target=daemon.serve_forever, daemon=True)
            t.start()

            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cli:
                    deadline = time.monotonic() + 3.0
                    while True:
                        try:
                            cli.connect(sock_path)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.02)
                    rf = cli.makefile("r", encoding="utf-8")
                    wf = cli.makefile("w", encoding="utf-8")

                    # 1. Interactive PIN pairing handshake
                    wf.write(json.dumps({"op": "pair", "target": "ZeroSim"}) + "\n")
                    wf.flush()
                    ev = json.loads(rf.readline())
                    self.assertEqual(ev["event"], "pin_required")
                    wf.write(json.dumps({"pin": "654321"}) + "\n")
                    wf.flush()
                    pair_resp = json.loads(rf.readline())
                    self.assertTrue(pair_resp["ok"])
                    self.assertEqual(sim.paired_pin, "654321")

                    # 2. Bi-directional Flipper `.fap` DataExchange trigger
                    sim.received_app_frames.clear()
                    sim.simulate_fap_push(b'{"id":42,"op":"ping"}')
                    wait_end = time.monotonic() + 2.0
                    while not sim.received_app_frames and time.monotonic() < wait_end:
                        time.sleep(0.02)
                    self.assertTrue(sim.received_app_frames)
                    fap_reply = json.loads(sim.received_app_frames[-1].decode("utf-8"))
                    self.assertEqual(fap_reply["id"], 42)
                    self.assertTrue(fap_reply["ok"])
                    self.assertIn("Kali Ready", fap_reply["text"])

                    # 2b. If `.fap` exits (`APP_CLOSED = 0`) mid-request, late reply MUST be dropped
                    sim.received_app_frames.clear()
                    with daemon._op_lock:
                        late_thr = threading.Thread(
                            target=sim.simulate_fap_push,
                            args=(b'{"id":43,"op":"ping"}',),
                            daemon=True,
                        )
                        late_thr.start()
                        time.sleep(0.05)
                        daemon._on_fap_state(0)
                    late_thr.join(timeout=2.0)
                    self.assertEqual(sim.received_app_frames, [])

                    # 3. Pocket Watch Mailbox trigger (`request.json` -> `response.json`)
                    sim.files[MAILBOX_REQ] = json.dumps({"seq": 9, "op": "jobs"}).encode("utf-8")
                    wait_end = time.monotonic() + 2.5
                    while MAILBOX_RESP not in sim.files and time.monotonic() < wait_end:
                        time.sleep(0.05)
                    self.assertIn(MAILBOX_RESP, sim.files)
                    mb_resp = json.loads(sim.files[MAILBOX_RESP].decode("utf-8"))
                    self.assertEqual(mb_resp["seq"], 9)
                    self.assertTrue(mb_resp["ok"])

                    wf.write(json.dumps({"op": "shutdown"}) + "\n")
                    wf.flush()
                    rf.readline()
            finally:
                daemon._stop_event.set()
                t.join(timeout=2.0)

    def test_daemon_pin_cancel_interrupts_pending_prompt(self):
        class CancellingPairTransport(PairableSimTransport):
            def pair(self, target, pin_provider, timeout_sec=45.0, cancel_cb=None):
                def _drop_link():
                    time.sleep(0.12)
                    if cancel_cb is not None:
                        cancel_cb()

                threading.Thread(target=_drop_link, daemon=True).start()
                pin_provider("Flipper Test", "80:E1:26:AA:BB:CC")
                raise RuntimeError("unreachable")

        sim = CancellingPairTransport()
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = os.path.join(tmp, "rpc-cancel.sock")
            daemon = Zero2ZeroDaemon(
                socket_path=sock_path,
                transport=sim,
                idle_timeout_sec=10.0,
                watch_enabled=False,
            )
            t = threading.Thread(target=daemon.serve_forever, daemon=True)
            t.start()
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as cli:
                    deadline = time.monotonic() + 3.0
                    while True:
                        try:
                            cli.connect(sock_path)
                            break
                        except OSError:
                            if time.monotonic() >= deadline:
                                raise
                            time.sleep(0.02)
                    rf = cli.makefile("r", encoding="utf-8")
                    wf = cli.makefile("w", encoding="utf-8")

                    wf.write(json.dumps({"op": "pair", "target": "Flipper Test"}) + "\n")
                    wf.flush()
                    ev1 = json.loads(rf.readline())
                    self.assertEqual(ev1["event"], "pin_required")
                    ev2 = json.loads(rf.readline())
                    self.assertEqual(ev2["event"], "pin_cancelled")
                    err_resp = json.loads(rf.readline())
                    self.assertFalse(err_resp["ok"])
                    self.assertIn("cancelled", err_resp["error"].lower())

                    # Socket remains clean for subsequent requests without BufferedReader race
                    wf.write(json.dumps({"op": "ping_daemon"}) + "\n")
                    wf.flush()
                    pong = json.loads(rf.readline())
                    self.assertTrue(pong["ok"])
            finally:
                daemon._stop_event.set()
                t.join(timeout=2.0)

    def test_pair_autoconnect_repair_and_unpair(self):
        sim = PairableSimTransport()
        sim.disconnect()  # Start disconnected & unpaired
        daemon = Zero2ZeroDaemon(
            socket_path="/tmp/unused-z2z.sock",
            transport=sim,
            watch_enabled=False,
        )
        # 1. Pair auto-connects immediately without separate connect step
        r_pair = daemon.handle_ipc_request({"op": "pair", "pin": "112233"})
        self.assertTrue(r_pair["ok"])
        self.assertTrue(r_pair["connected"])
        self.assertTrue(sim.is_connected)

        # 2. Status returns known_devices for instant UI population on startup
        r_st = daemon.handle_ipc_request({"op": "status", "query_rpc": False})
        self.assertEqual(len(r_st["devices"]), 1)
        self.assertTrue(r_st["devices"][0]["paired"])

        # 3. Force re-pair resets stale bond and accepts new passkey
        r_rep = daemon.handle_ipc_request({"op": "pair", "pin": "998877", "force": True})
        self.assertTrue(r_rep["ok"])
        self.assertTrue(sim.last_force)
        self.assertEqual(sim.paired_pin, "998877")

        # 4. Unpair removes bond cleanly
        r_unp = daemon.handle_ipc_request({"op": "unpair"})
        self.assertTrue(r_unp["ok"])
        self.assertTrue(r_unp["removed"])
        self.assertFalse(sim.is_paired)

    def test_multiformat_captures_usb_studio_and_remote_daemon_ops(self):
        # 1. NFC NTAG215 dump with NDEF URI (`https://example.org`) + locked byte `??`
        # TLV: 0x03, len=16, NDEF Short Record MB|ME|SR|TNF=1 (0xD1), type_len=1, payload_len=12,
        # type='U' (0x55), prefix=0x04 ('https://'), 'example.org' (11B), terminator 0xFE
        nfc_dump = (
            "Filetype: Flipper NFC device\n"
            "Version: 4\n"
            "Device type: NTAG/Ultralight\n"
            "UID: 04 11 22 33 44 55 66\n"
            "ATQA: 00 44\n"
            "SAK: 00\n"
            "Page 4: 03 10 D1 01\n"
            "Page 5: 0C 55 04 65\n"
            "Page 6: 78 61 6D 70\n"
            "Page 7: 6C 65 2E 6F\n"
            "Page 8: 72 67 FE 00\n"
            "Page 9: ?? ?? ?? ??\n"
        ).encode("utf-8")
        nfc_rep = inspect_capture_bytes("/ext/nfc/TagUrl.nfc", nfc_dump)
        self.assertEqual(nfc_rep["kind"], "nfc")
        self.assertEqual(nfc_rep["uid"], "04 11 22 33 44 55 66")
        self.assertEqual(nfc_rep["unknown_bytes"], 4)
        self.assertIn("URI: https://example.org", nfc_rep["ndef"])
        self.assertIn("URI: https://example.org", nfc_rep["compact_text"])

        # 2. 125 kHz RFID Wiegand-26 (`H10301`) facility + card number decode
        rfid_dump = (
            "Filetype: Flipper RFID key\n"
            "Version: 1\n"
            "Key type: H10301\n"
            "Data: 65 12 34\n"
        ).encode("utf-8")
        rfid_rep = inspect_capture_bytes("/ext/lfrfid/Office.rfid", rfid_dump)
        self.assertEqual(rfid_rep["facility_code"], 101)
        self.assertEqual(rfid_rep["card_number"], 4660)
        self.assertIn("Wiegand26 FC=101 Card=4660", rfid_rep["compact_text"])

        # 3. Infrared `.ir` multi-button NEC + RAW summary
        ir_dump = (
            "Filetype: IR signals file\n"
            "Version: 1\n"
            "#\n"
            "name: Power\n"
            "type: parsed\n"
            "protocol: NEC\n"
            "address: 04 00 00 00\n"
            "command: 08 00 00 00\n"
            "#\n"
            "name: Mute\n"
            "type: raw\n"
            "frequency: 38000\n"
            "duty_cycle: 0.330000\n"
            "data: 9000 4500 560 560 560 1690\n"
        ).encode("utf-8")
        ir_rep = inspect_capture_bytes("/ext/infrared/TV.ir", ir_dump)
        self.assertEqual(ir_rep["signal_count"], 2)
        self.assertIn("Power", ir_rep["compact_text"])
        self.assertIn("Mute", ir_rep["compact_text"])

        # 4. USB Script Studio builder (`Auto-line` + variable expansion)
        auto_txt = build_ducky_script("Hello {{HOST_IP}}", raw_ducky=False, variables={"HOST_IP": "10.0.0.9"})
        self.assertIn("STRING Hello 10.0.0.9\nENTER\n", auto_txt)

        # 5. End-to-end daemon IPC for captures, studio push+open, screen stream, clock_sync & fap ops
        sim = PairableSimTransport()
        sim.files["/ext/nfc/TagUrl.nfc"] = nfc_dump
        sim.files["/ext/lfrfid/Office.rfid"] = rfid_dump
        sim.files["/ext/infrared/TV.ir"] = ir_dump
        daemon = Zero2ZeroDaemon(socket_path="/tmp/unused-z2z-2.sock", transport=sim)

        r_caps = daemon.handle_ipc_request({"op": "captures_list"})
        self.assertTrue(r_caps["ok"])
        paths = [c["path"] for c in r_caps["captures"]]
        self.assertIn("/ext/nfc/TagUrl.nfc", paths)
        self.assertIn("/ext/lfrfid/Office.rfid", paths)
        self.assertIn("/ext/infrared/TV.ir", paths)

        r_insp = daemon.handle_ipc_request({"op": "capture_inspect", "remote": "/ext/nfc/TagUrl.nfc"})
        self.assertIn("https://example.org", r_insp["report"]["compact_text"])

        r_open = daemon.handle_ipc_request({"op": "capture_open", "remote": "/ext/infrared/TV.ir"})
        self.assertEqual(r_open["app"], "Infrared")

        r_bad = daemon.handle_ipc_request(
            {
                "op": "badusb_push",
                "name": "diag check",
                "content": "echo {{HOSTNAME}}",
                "raw_ducky": False,
                "open_on_flipper": True,
            }
        )
        self.assertTrue(r_bad["ok"])
        self.assertEqual(r_bad["remote"], "/ext/badusb/diag_check.txt")
        self.assertEqual(r_bad["opened_app"], "Bad USB")
        self.assertIn(b"STRING echo ", sim.files["/ext/badusb/diag_check.txt"])

        r_ss = daemon.handle_ipc_request({"op": "screen_start", "unicode": True})
        self.assertTrue(r_ss["ok"])
        self.assertEqual(len(r_ss["frame_hex"]), 2048)
        self.assertIn("▀", r_ss["unicode"])

        r_key = daemon.handle_ipc_request({"op": "gui_input", "key": "down", "mode": "short"})
        self.assertTrue(r_key["ok"])
        self.assertEqual(sim.gui_inputs[-3:], [(1, 0), (1, 2), (1, 1)])

        r_clk = daemon.handle_ipc_request({"op": "clock_sync"})
        self.assertTrue(r_clk["ok"])
        self.assertRegex(r_clk["datetime"], r"^\d{4}-\d{2}-\d{2} ")

        # Flipper `.fap` dynamic jobs + clock_sync + cap_latest handlers
        f_jobs = daemon._execute_fap_request({"id": 1, "op": "jobs"})
        self.assertIn("|", f_jobs["items"])
        f_clk = daemon._execute_fap_request({"id": 2, "op": "clock_sync"})
        self.assertIn("rtc", f_clk)
        f_cap = daemon._execute_fap_request({"id": 3, "op": "cap_latest", "kind": "lfrfid"})
        self.assertIn("Wiegand26 FC=101 Card=4660", f_cap["text"])

    def test_daemon_recovers_from_closed_flow_and_silent_first_connect(self):
        # Verify (1) `_connect_verified` retries if the first BLE session drops RX,
        # and (2) `handle_ipc_request` transparently reconnects if `_flow` closes mid-idle.
        class FlakyFirstConnectTransport(PairableSimTransport):
            def __init__(self) -> None:
                super().__init__()
                self._connected = False
                self.flow.close()
                self.connect_calls = 0

            def connect(self, target=None):
                self.connect_calls += 1
                return super().connect(target)

            def _emit_to_host(self, env) -> None:
                # First connect session silently blackholes TX indications (like missing CCCD)
                if self.connect_calls == 1:
                    return
                super()._emit_to_host(env)

        sim = FlakyFirstConnectTransport()
        daemon = Zero2ZeroDaemon(socket_path="/tmp/unused-flaky.sock", transport=sim)

        # `connect` detects dead first session via ping timeout? Use fast ping override for unit speed:
        orig_ping = daemon.rpc.ping
        daemon.rpc.ping = lambda payload=b"z2z", timeout_sec=0.15: orig_ping(payload, timeout_sec=0.12)

        r_conn = daemon.handle_ipc_request({"op": "connect"})
        self.assertTrue(r_conn["ok"])
        self.assertEqual(sim.connect_calls, 2)
        self.assertTrue(sim.is_connected)

        # Now simulate sudden Flipper BLE drop closing `_flow` right before `ls`
        sim.flow.close()
        self.assertFalse(sim.is_connected)
        r_ls = daemon.handle_ipc_request({"op": "ls", "path": "/ext/subghz"})
        self.assertTrue(r_ls["ok"])
        self.assertEqual(sim.connect_calls, 3)

    def test_reliable_upload_retries_on_ble_error_decode_and_stages_atomically(self):
        # Simulate a dropped BLE packet causing Flipper `ERROR_DECODE` + profile reset
        # during a multi-chunk upload; verify daemon reconnects, stages via `.tmp`, verifies MD5,
        # and atomically renames without leaving `.tmp` artifacts or corrupt target files.
        from z2z.proto import CommandStatus, F_EMPTY, F_STORAGE_WRITE_REQ, MainEnvelope

        sim = PairableSimTransport()
        sim.files["/ext/apps/Bluetooth/demo.fap"] = b"OLD_CORRUPT_DATA"
        write_sessions = [0]
        tuned_conservative = [False]
        orig_handle = sim._handle_rpc

        def flaky_upload_handle(req, write_accum):
            if req.content_field == F_STORAGE_WRITE_REQ and write_sessions[0] == 0:
                write_sessions[0] += 1
                # Simulate Flipper `rpc.c:322` ERROR_DECODE + `bt_profile_restore_default`
                sim._emit_to_host(MainEnvelope(0, CommandStatus.ERROR_DECODE, False, F_EMPTY, b""))
                sim.disconnect()
                return
            orig_handle(req, write_accum)

        sim._handle_rpc = flaky_upload_handle  # type: ignore[method-assign]
        sim.tune_upload_profile = lambda conservative=False: tuned_conservative.__setitem__(0, tuned_conservative[0] or bool(conservative))  # type: ignore[attr-defined]

        with tempfile.TemporaryDirectory() as tmp:
            payload = b"\x7fELF" + (b"Z2Z_PAYLOAD_BLOCK_" * 120)
            local_file = Path(tmp) / "demo.fap"
            local_file.write_bytes(payload)

            daemon = Zero2ZeroDaemon(socket_path=os.path.join(tmp, "rpc.sock"), transport=sim)
            daemon._upload_retry_delay_sec = 0.01

            resp = daemon.handle_ipc_request(
                {
                    "op": "push",
                    "local": str(local_file),
                    "remote": "/ext/apps/Bluetooth/demo.fap",
                }
            )
            self.assertTrue(resp["ok"])
            self.assertEqual(resp["bytes"], len(payload))
            self.assertTrue(tuned_conservative[0])
            self.assertEqual(sim.files["/ext/apps/Bluetooth/demo.fap"], payload)
            self.assertNotIn("/ext/apps/Bluetooth/demo.fap.tmp", sim.files)

    def test_mirror_delta_frame_disconnect_autostop_and_watch_yield(self):
        # Verify mirror delta polling, zero reconnect storm on `auto_start=False`
        # when Flipper disconnects, non-blocking `screen_stop`, and Pocket Watch yielding during stream.
        sim = PairableSimTransport()
        connect_calls = [0]
        orig_connect = sim.connect

        def counted_connect(target=None):
            connect_calls[0] += 1
            return orig_connect(target)

        sim.connect = counted_connect  # type: ignore[method-assign]
        daemon = Zero2ZeroDaemon(
            socket_path="/tmp/unused-mirror.sock",
            transport=sim,
            watch_enabled=True,
        )

        r_start = daemon.handle_ipc_request({"op": "screen_start"})
        self.assertTrue(r_start["ok"])
        self.assertTrue(r_start["streaming"])
        seq0 = r_start["seq"]
        self.assertEqual(len(r_start["frame_hex"]), 2048)

        # Unchanged `since_seq` omits 2048-char hex payload
        r_same = daemon.handle_ipc_request(
            {"op": "screen_frame", "auto_start": False, "since_seq": seq0}
        )
        self.assertTrue(r_same["streaming"])
        self.assertEqual(r_same["frame_hex"], "")

        # Pocket Watch must skip mailbox SD reads while screen streaming is active
        sim.files[MAILBOX_REQ] = json.dumps({"seq": 77, "op": "ping"}).encode("utf-8")
        self.assertIsNone(daemon._poll_pocket_mailbox_once())
        self.assertNotIn(MAILBOX_RESP, sim.files)

        # Simulate Flipper disconnecting while mirror is active -> `auto_start=False` returns
        # `streaming=False, connected=False` immediately with ZERO background reconnect calls!
        sim.disconnect()
        r_drop = daemon.handle_ipc_request(
            {"op": "screen_frame", "auto_start": False, "since_seq": seq0}
        )
        self.assertTrue(r_drop["ok"])
        self.assertFalse(r_drop["connected"])
        self.assertFalse(r_drop["streaming"])
        self.assertEqual(connect_calls[0], 0)

        # `screen_stop` succeeds immediately even if `_op_lock` is held on another thread
        lock_ready = threading.Event()
        release_lock = threading.Event()

        def hold_lock():
            with daemon._op_lock:
                lock_ready.set()
                release_lock.wait(timeout=2.0)

        thr = threading.Thread(target=hold_lock, daemon=True)
        thr.start()
        lock_ready.wait(timeout=1.0)
        try:
            t0 = time.monotonic()
            r_stop = daemon.handle_ipc_request({"op": "screen_stop"})
            self.assertTrue(r_stop["ok"])
            self.assertFalse(r_stop["streaming"])
            self.assertLess(time.monotonic() - t0, 0.2)
        finally:
            release_lock.set()
            thr.join(timeout=1.0)

        # Explicit `disconnect` pauses Pocket Watch auto-reconnect until `connect` or `watch_set(True)`
        daemon.handle_ipc_request({"op": "disconnect"})
        self.assertIsNone(daemon._poll_pocket_mailbox_once())
        self.assertEqual(connect_calls[0], 0)

    def test_status_caches_flipper_meta_and_skips_rpc_while_streaming(self):
        sim = PairableSimTransport()
        sim.connect()
        rpc_calls = {"storage_info": 0, "power_info": 0}
        daemon = Zero2ZeroDaemon(
            socket_path="/tmp/unused-status-cache.sock",
            transport=sim,
            watch_enabled=False,
        )
        orig_si = daemon.rpc.storage_info
        orig_pi = daemon.rpc.power_info

        def counted_si(path: str = "/ext"):
            rpc_calls["storage_info"] += 1
            return orig_si(path)

        def counted_pi():
            rpc_calls["power_info"] += 1
            return orig_pi()

        daemon.rpc.storage_info = counted_si  # type: ignore[method-assign]
        daemon.rpc.power_info = counted_pi  # type: ignore[method-assign]

        r1 = daemon.handle_ipc_request({"op": "status"})
        self.assertTrue(r1["ok"])
        self.assertEqual(rpc_calls["storage_info"], 1)
        self.assertEqual(rpc_calls["power_info"], 1)
        self.assertIn("ext_free_kb", r1["flipper"])

        # Immediate second status reuses cached Flipper metadata without extra BLE RPCs
        r2 = daemon.handle_ipc_request({"op": "status"})
        self.assertEqual(r2["flipper"], r1["flipper"])
        self.assertEqual(rpc_calls["storage_info"], 1)
        self.assertEqual(rpc_calls["power_info"], 1)

        # While screen streaming is active, status skips RPC queries even if cache expired
        daemon._cached_flipper_meta_ts = 0.0
        daemon.handle_ipc_request({"op": "screen_start"})
        r3 = daemon.handle_ipc_request({"op": "status"})
        self.assertEqual(r3["flipper"], r1["flipper"])
        self.assertEqual(rpc_calls["storage_info"], 1)
        self.assertEqual(rpc_calls["power_info"], 1)

    def test_gui_input_timeout_unwedges_flipper_session_and_restores_mirror(self):
        # Reproduce `Flipper GUI input ... timed out` when Flipper's
        # `bt_rpc_send_bytes_callback` wedges while Link Layer stays `Connected=True`,
        # and verify daemon disconnects, reconnects, restores mirror stream, and retries input.
        from z2z.proto import F_GUI_SEND_INPUT_REQ

        sim = PairableSimTransport()
        daemon = Zero2ZeroDaemon(
            socket_path="/tmp/unused-gui-unwedge.sock",
            transport=sim,
            watch_enabled=False,
        )
        daemon._gui_input_timeout_sec = 0.15
        daemon._connect_retry_delay_sec = 0.01

        r_start = daemon.handle_ipc_request({"op": "screen_start"})
        self.assertTrue(r_start["ok"])
        self.assertTrue(r_start["streaming"])

        disconnect_calls = [0]
        orig_disc = sim.disconnect

        def counted_disc():
            disconnect_calls[0] += 1
            orig_disc()

        sim.disconnect = counted_disc  # type: ignore[method-assign]

        gui_reqs = [0]
        orig_handle = sim._handle_rpc

        def wedge_second_step(req, write_accum):
            if req.content_field == F_GUI_SEND_INPUT_REQ:
                gui_reqs[0] += 1
                if gui_reqs[0] == 2:
                    # Simulate dropped SHORT Indication while BLE ACL stays Connected=True
                    return
            orig_handle(req, write_accum)

        sim._handle_rpc = wedge_second_step  # type: ignore[method-assign]

        sim.gui_inputs.clear()
        r_key = daemon.handle_ipc_request({"op": "gui_input", "key": "down", "mode": "short"})
        self.assertTrue(r_key["ok"])
        self.assertTrue(r_key["connected"])
        self.assertTrue(r_key["streaming"])
        self.assertGreaterEqual(disconnect_calls[0], 1)
        self.assertEqual(sim.gui_inputs[-3:], [(1, 0), (1, 2), (1, 1)])

    def test_async_fifo_gui_queue_server_push_and_mirror_mailbox_bridge(self):
        import socket as py_sock

        sim = PairableSimTransport()
        with tempfile.TemporaryDirectory() as tmp:
            sock_path = os.path.join(tmp, "rpc.sock")
            daemon = Zero2ZeroDaemon(
                socket_path=sock_path,
                transport=sim,
                watch_enabled=False,
            )

            # 1. Start live server thread and subscribe over Unix SOCK_STREAM
            daemon._ready_event = threading.Event()  # type: ignore[attr-defined]
            srv_thr = threading.Thread(target=daemon.serve_forever, daemon=True)
            srv_thr.start()
            self.assertTrue(daemon._ready_event.wait(timeout=2.0))

            cli = py_sock.socket(py_sock.AF_UNIX, py_sock.SOCK_STREAM)
            cli.settimeout(3.0)
            cli.connect(sock_path)
            rfile = cli.makefile("r", encoding="utf-8")
            try:
                cli.sendall((json.dumps({"op": "screen_start", "subscribe": True}) + "\n").encode())
                first_resp = json.loads(rfile.readline())
                self.assertTrue(first_resp["ok"])
                self.assertTrue(first_resp["streaming"])

                # Rapid async FIFO taps must all execute in strict order without blocking caller
                sim.gui_inputs.clear()
                t0 = time.monotonic()
                for k in ("down", "down", "ok"):
                    ack = daemon.handle_ipc_request(
                        {"op": "gui_input", "key": k, "mode": "short", "async": True}
                    )
                    self.assertTrue(ack["ok"])
                    self.assertTrue(ack["queued"])
                self.assertLess(time.monotonic() - t0, 0.15)

                # While queued/recent, `coalesce_repeat=True` must coalesce instead of stacking
                rep_ack = daemon.handle_ipc_request(
                    {
                        "op": "gui_input",
                        "key": "down",
                        "mode": "short",
                        "async": True,
                        "coalesce_repeat": True,
                    }
                )
                if daemon.gui_input_queue_depth >= 1:
                    self.assertTrue(rep_ack["ok"])

                # Wait briefly for worker to drain the 3 queued keys in strict FIFO order
                deadline = time.monotonic() + 2.0
                while len(sim.gui_inputs) < 9 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(
                    sim.gui_inputs[:9],
                    [
                        (1, 0), (1, 2), (1, 1),  # down
                        (1, 0), (1, 2), (1, 1),  # down
                        (4, 0), (4, 2), (4, 1),  # ok
                    ],
                )

                # Simulate incoming ScreenFrame from Flipper -> client receives server-pushed event
                from z2z.proto import F_GUI_SCREEN_FRAME, MainEnvelope, _f_bytes, _f_varint

                fb = bytearray(1024)
                fb[5] = 0xAA
                sim._emit_to_host(
                    MainEnvelope(
                        0, 0, False, F_GUI_SCREEN_FRAME, _f_bytes(1, bytes(fb)) + _f_varint(2, 0)
                    )
                )

                pushed_frame = None
                push_deadline = time.monotonic() + 2.0
                while time.monotonic() < push_deadline:
                    msg = json.loads(rfile.readline())
                    if msg.get("event") == "screen_frame" and msg.get("frame_hex"):
                        pushed_frame = msg
                        break
                self.assertIsNotNone(pushed_frame)
                self.assertEqual(pushed_frame["frame_hex"][10:12], "aa")

                # 2. Quiet-window Mirror mailbox bridge: `_poll_pocket_mailbox_once` MUST still
                # return None during streaming, while `_poll_mirror_mailbox_if_idle` services it
                # cleanly once quiet window passes and sets `"quiet": True`!
                sim.files[MAILBOX_REQ] = json.dumps({"seq": 313, "op": "ping"}).encode("utf-8")
                self.assertIsNone(daemon._poll_pocket_mailbox_once())
                daemon._last_gui_input_mono = time.monotonic() - 2.0
                daemon.rpc._last_screen_frame_mono = time.monotonic() - 2.0
                while daemon.gui_input_queue_depth > 0:
                    time.sleep(0.01)
                m_resp = daemon._poll_mirror_mailbox_if_idle()
                self.assertIsNotNone(m_resp)
                self.assertTrue(m_resp["ok"])
                self.assertTrue(m_resp["quiet"])
                self.assertEqual(m_resp["seq"], 313)
                self.assertNotIn(MAILBOX_REQ, sim.files)
                self.assertIn(MAILBOX_RESP, sim.files)

                cli.sendall(b'{"op":"screen_stop"}\n')
                stop_deadline = time.monotonic() + 2.0
                got_stop = False
                while time.monotonic() < stop_deadline:
                    msg = json.loads(rfile.readline())
                    if "ok" in msg and msg.get("streaming") is False:
                        got_stop = True
                        break
                self.assertTrue(got_stop)
            finally:
                rfile.close()
                cli.close()
                daemon._stop_event.set()
                srv_thr.join(timeout=2.0)

    def test_subghz_ook_roundtrip_and_rtl433_live_bridge(self):
        import subprocess
        from z2z.subghz import (
            analyze_live_sub_burst,
            decode_with_rtl433,
            ook_to_sub,
            sub_to_ook,
            synthesize_raw_sub,
        )

        sub_txt = synthesize_raw_sub("0xA5C3", frequency=433920000, te_us=350, repeat=3)
        ook_txt = sub_to_ook(sub_txt, rssi_db=-51.5)
        self.assertIn(";pulse data", ook_txt)
        self.assertIn(";timescale 1us", ook_txt)
        self.assertIn(";freq1 433920000", ook_txt)
        self.assertIn(";rssi -51.5 dB", ook_txt)

        sub_back = ook_to_sub(ook_txt)
        self.assertIn("Filetype: Flipper SubGhz RAW File", sub_back)
        self.assertIn("Frequency: 433920000", sub_back)

        captured_ook = []

        def fake_rtl433(cmd, **_kwargs):
            self.assertEqual(cmd[1], "-r")
            ook_file = Path(cmd[2])
            self.assertEqual(ook_file.suffix, ".ook")
            captured_ook.append(ook_file.read_text(encoding="utf-8"))
            stdout = (
                '{"time":"2026-11-13 12:00:00","model":"Oregon-THGR122N","id":142,"channel":1,"temperature_C":22.4,"humidity":54,"battery_ok":1}\n'
                '{"time":"2026-11-13 12:00:01","model":"Oregon-THGR122N","id":142,"channel":1,"temperature_C":22.4,"humidity":54,"battery_ok":1}\n'
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")

        res = decode_with_rtl433(sub_txt, runner=fake_rtl433)
        self.assertTrue(res["matched"])
        self.assertEqual(len(res["events"]), 1)  # Duplicate burst frames deduplicated
        self.assertIn("Oregon-THGR122N", res["summary"])
        self.assertIn("22.4C", res["summary"])
        self.assertIn("RH=54%", res["summary"])
        self.assertEqual(len(captured_ook), 1)

        live = analyze_live_sub_burst(
            "350 -1050 1050 -350 350 -1050 1050 -350 350 -1050 1050 -350 350 -1050 1050 -350 350 -10000",
            frequency=433920000,
            rssi_db=-49,
            rtl433_runner=fake_rtl433,
        )
        self.assertTrue(live["matched"])
        self.assertEqual(live["line1"], "Oregon-THGR122N")
        self.assertIn("22.4C", live["line2"])

        # Verify daemon `sub_live` and `sub_decode` handlers
        sim = SimulatedFlipperTransport()
        sim.files["/ext/subghz/Weather_01.sub"] = sub_txt.encode("utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            daemon = Zero2ZeroDaemon(socket_path=os.path.join(tmp, "rpc.sock"), transport=sim)
            fap_resp = daemon._execute_fap_request(
                {
                    "id": 77,
                    "op": "sub_live",
                    "freq": 433920000,
                    "rssi": -53,
                    "pulses": "350 -1050 1050 -350 350 -1050 1050 -350 350 -1050 1050 -350 350 -1050 1050 -350 350 -10000",
                }
            )
            self.assertTrue(fap_resp["ok"])
            self.assertEqual(fap_resp["op"], "sub_live")

            ook_out = os.path.join(tmp, "exported.ook")
            dec_resp = daemon.handle_ipc_request({"op": "sub_decode", "ook_out": ook_out})
            self.assertTrue(dec_resp["ok"])
            self.assertTrue(os.path.isfile(ook_out))
            self.assertIn("Hex(16b): A5C3", dec_resp["report"]["compact_text"])

    def test_nfc_converters_ndef_builder_proxmark3_and_qrencode(self):
        import subprocess
        from z2z.captures import parse_nfc_capture
        from z2z.nfc_tools import (
            build_ndef_nfc_tag,
            convert_nfc_bytes,
            eml_to_nfc,
            encode_qr_with_qrencode,
            inspect_nfc_with_proxmark3,
            mfd_to_nfc,
            nfc_to_eml,
            nfc_to_mfd,
        )

        # 1. NDEF NTAG215 builder -> parse_nfc_capture -> `.mfd` / `.eml` round-trip
        ndef_nfc = build_ndef_nfc_tag("https://kali.org/docs")
        parsed = parse_nfc_capture(ndef_nfc, use_proxmark3=False)
        self.assertEqual(parsed["page_count"], 135)
        self.assertIn("URI: https://kali.org/docs", parsed["ndef"])
        self.assertEqual(parsed["qr_payload"], "https://kali.org/docs")

        mfd_bytes = nfc_to_mfd(ndef_nfc)
        self.assertEqual(len(mfd_bytes), 135 * 4)
        nfc_from_mfd = mfd_to_nfc(mfd_bytes)
        self.assertIn("URI: https://kali.org/docs", parse_nfc_capture(nfc_from_mfd, use_proxmark3=False)["ndef"])

        # 2. Mifare Classic 1K (64 blocks = 1024B) + `.eml` `??` preservation
        raw_1k = bytes((i % 256) for i in range(1024))
        mf_nfc = mfd_to_nfc(raw_1k)
        self.assertEqual(nfc_to_mfd(mf_nfc), raw_1k)
        eml_txt = nfc_to_eml(mf_nfc)
        self.assertEqual(len(eml_txt.strip().splitlines()), 64)
        eml_masked = "????????" + eml_txt[8:]
        nfc_masked = eml_to_nfc(eml_masked)
        self.assertIn("Block 0: ?? ?? ?? ??", nfc_masked)

        # 3. Mock `proxmark3 -c "hf mf view -f ..."` offline inspection
        def fake_pm3(cmd, **_kwargs):
            self.assertEqual(cmd[0], "proxmark3")
            self.assertEqual(cmd[1], "-c")
            self.assertIn("hf mf view -f", cmd[2])
            out = "[=]   UID: 00 01 02 03\n[+] MAD v1 detected\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        pm3_res = inspect_nfc_with_proxmark3(mf_nfc, runner=fake_pm3)
        self.assertTrue(pm3_res["available"])
        self.assertIn("pm3: UID: 00 01 02 03", pm3_res["summary"])
        self.assertIn("pm3: MAD v1 detected", pm3_res["summary"])

        # 4. Mock `qrencode -t ASCII -m 1 -o -`
        def fake_qrencode(cmd, **_kwargs):
            self.assertIn("-t", cmd)
            self.assertIn("ASCII", cmd)
            ascii_qr = "######  ##\n##  ##    \n######  ##\n          \n##  ######\n"
            return subprocess.CompletedProcess(cmd, 0, stdout=ascii_qr, stderr="")

        qr = encode_qr_with_qrencode("https://kali.org", runner=fake_qrencode)
        self.assertTrue(qr["ok"])
        self.assertEqual(qr["size"], 5)
        self.assertEqual(qr["rows"][0], "11101")
        self.assertTrue(len(qr["utf8"]) > 0)

        # 5. Daemon IPC `nfc_make` + `nfc_convert`
        sim = SimulatedFlipperTransport()
        with tempfile.TemporaryDirectory() as tmp:
            daemon = Zero2ZeroDaemon(socket_path=os.path.join(tmp, "rpc.sock"), transport=sim)
            mk = daemon.handle_ipc_request(
                {
                    "op": "nfc_make",
                    "payload": "https://cardputer.kali.org",
                    "remote": "/ext/nfc/cz.nfc",
                    "open_on_flipper": True,
                }
            )
            self.assertTrue(mk["ok"])
            self.assertEqual(mk["opened_app"], "NFC")
            self.assertIn("/ext/nfc/cz.nfc", sim.files)

            out_bin = os.path.join(tmp, "cz.mfd")
            conv = daemon.handle_ipc_request(
                {"op": "nfc_convert", "src": "/ext/nfc/cz.nfc", "dst": out_bin}
            )
            self.assertTrue(conv["ok"])
            self.assertEqual(conv["bytes"], 540)
            self.assertEqual(
                convert_nfc_bytes(Path(out_bin).read_bytes(), out_bin, "back.eml").count(b"\n"),
                135,
            )

    def test_pcap_tshark_dissector_and_lockfree_wildcard_jobs(self):
        import struct
        import subprocess
        import threading
        from z2z.captures import inspect_capture_bytes, parse_pcap_capture
        from z2z.jobs import JobSpec, execute_job, load_jobs

        # Build valid 1-packet little-endian libpcap buffer
        pcap_hdr = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
        pkt_payload = b"\xff\xff\xff\xff\xff\xff" + b"\x00\x11\x22\x33\x44\x55" + b"\x08\x06" + b"\x00" * 28
        pkt_hdr = struct.pack("<IIII", 1700000000, 0, len(pkt_payload), len(pkt_payload))
        pcap_blob = pcap_hdr + pkt_hdr + pkt_payload

        def fake_tshark(cmd, **_kwargs):
            self.assertIn("-n", cmd)
            self.assertIn("-r", cmd)
            out = (
                "===================================================================\n"
                "Protocol Hierarchy Statistics\n"
                "eth                                      frames:1 bytes:42\n"
                "  arp                                    frames:1 bytes:42\n"
            )
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        pcap_rep = parse_pcap_capture(pcap_blob, tshark_runner=fake_tshark)
        self.assertEqual(pcap_rep["packet_count"], 1)
        self.assertTrue(pcap_rep["tshark_used"])
        self.assertIn("arp frames:1 bytes:42", pcap_rep["compact_text"])
        auto_rep = inspect_capture_bytes("sniff.pcap", pcap_blob)
        self.assertEqual(auto_rep["kind"], "pcap")

        # Verify all 6 built-in jobs load cleanly (`rtl433-sub`, `tshark-pcap`, `nmap-lan`)
        repo_root = Path(__file__).resolve().parents[1]
        repo_jobs = load_jobs([repo_root / "etc" / "jobs.d"])
        for jid in ("sys-health", "note-sync", "lan-neighbors", "rtl433-sub", "tshark-pcap", "nmap-lan"):
            self.assertIn(jid, repo_jobs)

        # Verify `remote_dir` + `suffix` newest-file pull AND lock-free Step 2 subprocess execution
        sim = SimulatedFlipperTransport()
        sim.connect()
        sim.files.pop("/ext/subghz/Gate.sub", None)
        sim.files["/ext/subghz/RAW_20261112-090000.sub"] = b"OLD"
        sim.files["/ext/subghz/RAW_20261113-120000.sub"] = b"NEWEST_PAYLOAD"
        rpc = FlipperRpcClient(sim)
        op_lock = threading.RLock()

        # Child command checks that `Z2Z_PULLED_REMOTE` resolved to `RAW_20261113-120000.sub`
        spec = JobSpec(
            id="test-wildcard",
            title="Wildcard Pull Test",
            pull_files=[{"remote_dir": "/ext/subghz", "suffix": ".sub", "local": "in.sub"}],
            command=[
                "python3",
                "-c",
                "import os, pathlib; print(os.environ['Z2Z_PULLED_REMOTE'] + ':' + pathlib.Path('in.sub').read_text())",
            ],
        )
        res = execute_job(spec, rpc=rpc, rpc_lock=op_lock)
        self.assertEqual(res["returncode"], 0)
        self.assertEqual(res["pulled"], ["/ext/subghz/RAW_20261113-120000.sub"])
        self.assertIn("/ext/subghz/RAW_20261113-120000.sub:NEWEST_PAYLOAD", res["output"])

    def test_v040_optical_scan_rfid_ibutton_diff_and_ir_deck(self):
        import subprocess
        from unittest import mock
        from z2z.hw_tools import (
            append_ir_signal_to_remote,
            build_ibutton_key,
            build_rfid_key,
            dallas_crc8,
            diff_capture_bytes,
            ir_to_irctl,
            irctl_to_ir,
            parse_ir_remote_full,
            scan_optical_code,
        )
        from z2z.nfc_tools import build_ndef_nfc_tag

        # 1. LF 125 kHz RFID (`H10301`, `EM4100`, `FDX-B`) & 1-Wire iButton (`Dallas` CRC-8)
        kt_h, hex_h, txt_h = build_rfid_key("123:45678")
        self.assertEqual(kt_h, "H10301")
        self.assertEqual(hex_h, "7B B2 6E")
        self.assertIn("Key type: H10301", txt_h)

        kt_em, hex_em, _ = build_rfid_key("18000DC7EB")
        self.assertEqual(kt_em, "EM4100")
        self.assertEqual(hex_em, "18 00 0D C7 EB")

        kt_fdx, hex_fdx, _ = build_rfid_key("900:123456789012")
        self.assertEqual(kt_fdx, "FDX-B")
        self.assertEqual(len(hex_fdx.split()), 8)

        kt_ib, hex_ib, txt_ib = build_ibutton_key("11 22 33 44 55 66")
        self.assertEqual(kt_ib, "Dallas")
        ib_bytes = bytes.fromhex(hex_ib.replace(" ", ""))
        self.assertEqual(ib_bytes[0], 0x01)
        self.assertEqual(ib_bytes[7], dallas_crc8(ib_bytes[:7]))
        self.assertIn("Filetype: Flipper iButton key", txt_ib)

        self.assertEqual(build_ibutton_key("12 34")[0], "Cyfral")
        self.assertEqual(build_ibutton_key("11 22 33 44")[0], "Metakom")

        # 2. Structural Capture Diff (`.nfc` pages, `.ir` buttons, `.rfid` lines)
        nfc_a = build_ndef_nfc_tag("https://kali.org/one").encode("utf-8")
        nfc_b = build_ndef_nfc_tag("https://kali.org/two").encode("utf-8")
        self.assertTrue(diff_capture_bytes("a.nfc", nfc_a, "a2.nfc", nfc_a)["identical"])
        d_nfc = diff_capture_bytes("/ext/nfc/a.nfc", nfc_a, "/ext/nfc/b.nfc", nfc_b)
        self.assertFalse(d_nfc["identical"])
        self.assertEqual(d_nfc["kind"], "nfc")
        self.assertGreater(d_nfc["changed_count"], 0)
        self.assertIn("pages", d_nfc["compact_text"])

        # 3. Flipper `.ir` 32-bit LE hex parser + `ir-ctl` converter
        apple_ir = (
            "Filetype: IR signals file\n"
            "Version: 1\n"
            "#\n"
            "name: Menu\n"
            "type: parsed\n"
            "protocol: NECext\n"
            "address: EE 87 00 00\n"
            "command: 5F 98 00 00\n"
            "#\n"
            "name: Play\n"
            "type: raw\n"
            "frequency: 38000\n"
            "duty_cycle: 0.330000\n"
            "data: 9000 4500 560 560 560\n"
        )
        sigs = parse_ir_remote_full(apple_ir)
        self.assertEqual(len(sigs), 2)
        self.assertEqual(sigs[0]["name"], "Menu")
        self.assertEqual(sigs[0]["address_u32"], 0x87EE)
        self.assertEqual(sigs[0]["command_u32"], 0x985F)

        irctl_txt = ir_to_irctl(apple_ir, button="Menu")
        self.assertIn("carrier 38000", irctl_txt)
        self.assertIn("+9000 -4500", irctl_txt)
        back_ir = irctl_to_ir(irctl_txt, button_name="MenuRaw")
        back_sigs = parse_ir_remote_full(back_ir)
        self.assertEqual(back_sigs[0]["name"], "MenuRaw")
        self.assertEqual(back_sigs[0]["type"], "raw")
        self.assertEqual(back_sigs[0]["pulses"], 67)

        apple_ir_v2 = append_ir_signal_to_remote(
            apple_ir, {"name": "VolUp", "type": "parsed", "proto": "NEC", "addr": 4, "cmd": 2}
        )
        d_ir = diff_capture_bytes("v1.ir", apple_ir.encode(), "v2.ir", apple_ir_v2.encode())
        self.assertEqual(d_ir["kind"], "infrared")
        self.assertIn("+ VolUp", d_ir["compact_text"])

        # 4. Optical `IMX219` + `zbarimg` scanner mock (4-frame AE settle burst)
        def fake_cam(cmd, **_kwargs):
            for arg in cmd:
                if arg.startswith("--file="):
                    pat = arg.split("=", 1)[1]
                    for idx in range(4):
                        Path(pat.replace("#", str(idx))).write_bytes(b"P6\n2 2\n255\n" + b"\x00" * 12)
            return subprocess.CompletedProcess(cmd, 0, stdout="Captured 4 frames\n", stderr="")

        def fake_zbar(cmd, **_kwargs):
            img_name = os.path.basename(cmd[-1])
            # Frame 3 (settled AE) yields QR decode
            if img_name.endswith("-3.ppm"):
                return subprocess.CompletedProcess(cmd, 0, stdout="QR-Code:https://kali.org/imx219\n", stderr="")
            return subprocess.CompletedProcess(cmd, 4, stdout="", stderr="")

        opt = scan_optical_code(cam_runner=fake_cam, zbar_runner=fake_zbar)
        self.assertTrue(opt["matched"])
        self.assertEqual(opt["source"], "imx219")
        self.assertEqual(opt["codes"][0]["payload"], "https://kali.org/imx219")

        # 5. End-to-end Daemon IPC for `rfid_make`, `ibtn_make`, `capture_diff`, `ir_*`, `cam_scan`
        sim = SimulatedFlipperTransport()
        sim.files["/ext/infrared/Apple_TV.ir"] = apple_ir.encode("utf-8")
        sim.files["/ext/nfc/tag1.nfc"] = nfc_a
        sim.files["/ext/nfc/tag2.nfc"] = nfc_b

        with tempfile.TemporaryDirectory() as tmp:
            daemon = Zero2ZeroDaemon(socket_path=os.path.join(tmp, "rpc.sock"), transport=sim)

            r_rfid = daemon.handle_ipc_request(
                {
                    "op": "rfid_make",
                    "spec": "123:45678",
                    "remote": "/ext/lfrfid/badge.rfid",
                    "open_on_flipper": True,
                }
            )
            self.assertTrue(r_rfid["ok"])
            self.assertEqual(r_rfid["key_type"], "H10301")
            self.assertEqual(r_rfid["opened_app"], "125 kHz RFID")
            self.assertIn("/ext/lfrfid/badge.rfid", sim.files)

            r_ibtn = daemon.handle_ipc_request(
                {
                    "op": "ibtn_make",
                    "spec": "112233445566",
                    "remote": "/ext/ibutton/door.ibtn",
                    "open_on_flipper": True,
                }
            )
            self.assertTrue(r_ibtn["ok"])
            self.assertEqual(r_ibtn["key_type"], "Dallas")
            self.assertEqual(r_ibtn["opened_app"], "iButton")
            self.assertIn("/ext/ibutton/door.ibtn", sim.files)

            r_diff = daemon.handle_ipc_request(
                {"op": "capture_diff", "src_a": "/ext/nfc/tag1.nfc", "src_b": "/ext/nfc/tag2.nfc"}
            )
            self.assertTrue(r_diff["ok"])
            self.assertFalse(r_diff["identical"])

            r_list = daemon.handle_ipc_request({"op": "ir_list", "src": "/ext/infrared/Apple_TV.ir"})
            self.assertTrue(r_list["ok"])
            self.assertEqual(r_list["count"], 2)
            self.assertIn("/ext/infrared/Apple_TV.ir", r_list["remotes"])
            self.assertIn("/ext/infrared/z2z_learned.ir", r_list["remotes"])

            # `ir_list` with `auto_select=True` picks first `.ir` remote; non-existent `z2z_learned.ir`
            # opens cleanly with `count=0` instead of throwing `ERROR_STORAGE_NOT_EXIST`!
            r_auto = daemon.handle_ipc_request({"op": "ir_list", "src": "", "auto_select": True})
            self.assertEqual(r_auto["src"], "/ext/infrared/Apple_TV.ir")
            self.assertEqual(r_auto["count"], 2)
            r_empty_learned = daemon.handle_ipc_request(
                {"op": "ir_list", "src": "/ext/infrared/z2z_learned.ir"}
            )
            self.assertTrue(r_empty_learned["ok"])
            self.assertEqual(r_empty_learned["count"], 0)
            self.assertEqual(r_empty_learned["buttons"], [])

            # `ir_save` with `arm_if_empty=True` when no signal is cached arms `ir_rx_start` on Flipper
            r_arm = daemon.handle_ipc_request(
                {"op": "ir_save", "dst": "/ext/infrared/z2z_learned.ir", "arm_if_empty": True}
            )
            self.assertTrue(r_arm["ok"])
            self.assertTrue(r_arm["armed_rx"])
            self.assertEqual(
                json.loads(sim.received_app_frames[-1].decode("utf-8"))["op"], "ir_rx_start"
            )
            daemon._fap_duplex_ready = False  # reset so `r_tx1` below still tests `auto_launched_fap`

            # First `ir_tx` auto-launches `zero2zero.fap` in RPC Duplex and blasts `Menu`
            r_tx1 = daemon.handle_ipc_request(
                {"op": "ir_tx", "src": "/ext/infrared/Apple_TV.ir", "button": "Menu", "repeat": 2}
            )
            self.assertTrue(r_tx1["ok"])
            self.assertTrue(r_tx1["auto_launched_fap"])
            tx_json = json.loads(sim.received_app_frames[-1].decode("utf-8"))
            self.assertEqual(tx_json["op"], "ir_tx")
            self.assertEqual(tx_json["proto"], "NECext")
            self.assertEqual(tx_json["addr"], 0x87EE)
            self.assertEqual(tx_json["cmd"], 0x985F)

            # Second `ir_tx` reuses active duplex session & cached remote
            r_tx2 = daemon.handle_ipc_request(
                {"op": "ir_tx", "src": "/ext/infrared/Apple_TV.ir", "button": "Play"}
            )
            self.assertTrue(r_tx2["ok"])
            self.assertFalse(r_tx2["auto_launched_fap"])
            tx_raw_json = json.loads(sim.received_app_frames[-1].decode("utf-8"))
            self.assertEqual(tx_raw_json["freq"], 38000)
            self.assertIn("9000 4500", tx_raw_json["raw"])

            # User quits `.fap` on Flipper right after `ir_tx` (`ERROR_APP_CMD_ERROR`
            # mid-exit + `loader_lock` clearing 2 polls later) -> next `ir_tx` MUST auto-relaunch
            # `.fap` with ZERO spurious `BACK` presses and deliver the IR frame cleanly!
            sim.gui_inputs.clear()
            sim.app_locked = True
            daemon.rpc._unlock_poll_sec = 0.001
            orig_sim_h = sim._handle_rpc
            rejected_once = [False]
            quit_lock_polls = [0]

            def post_quit_handle(req, write_accum):
                from z2z.proto import CommandStatus, F_APP_DATA_EXCHANGE_REQ, F_APP_EXIT_REQ, F_APP_LOCK_STATUS_REQ, F_EMPTY, MainEnvelope

                if req.content_field == F_APP_DATA_EXCHANGE_REQ and not rejected_once[0]:
                    rejected_once[0] = True
                    sim._emit_to_host(
                        MainEnvelope(req.command_id, CommandStatus.ERROR_APP_CMD_ERROR, False, F_EMPTY, b"")
                    )
                    return
                if req.content_field == F_APP_EXIT_REQ:
                    sim._emit_to_host(
                        MainEnvelope(req.command_id, CommandStatus.ERROR_APP_NOT_RUNNING, False, F_EMPTY, b"")
                    )
                    return
                if req.content_field == F_APP_LOCK_STATUS_REQ and sim.app_locked:
                    quit_lock_polls[0] += 1
                    if quit_lock_polls[0] >= 3:
                        sim.app_locked = False
                orig_sim_h(req, write_accum)

            sim._handle_rpc = post_quit_handle  # type: ignore[method-assign]
            try:
                r_tx_after_quit = daemon.handle_ipc_request(
                    {"op": "ir_tx", "src": "/ext/infrared/Apple_TV.ir", "button": "Menu"}
                )
            finally:
                sim._handle_rpc = orig_sim_h  # type: ignore[method-assign]
            self.assertTrue(r_tx_after_quit["ok"])
            self.assertTrue(r_tx_after_quit["auto_launched_fap"])
            self.assertEqual(sim.gui_inputs, [])
            self.assertEqual(
                json.loads(sim.received_app_frames[-1].decode("utf-8"))["name"], "Menu"
            )

            # Simulate Flipper streaming `op="ir_rx"` -> save button via `ir_save`
            fap_ack = daemon._execute_fap_request(
                {"id": 88, "op": "ir_rx", "type": "parsed", "proto": "NEC", "addr": 0x04, "cmd": 0x08}
            )
            self.assertEqual(fap_ack["op"], "ir_rx_ack")
            r_save = daemon.handle_ipc_request(
                {"op": "ir_save", "dst": "/ext/infrared/Apple_TV.ir", "name": "Power"}
            )
            self.assertTrue(r_save["ok"])
            self.assertEqual(
                len(parse_ir_remote_full(sim.files["/ext/infrared/Apple_TV.ir"])), 3
            )

            # Bidirectional `ir_convert` IPC
            irctl_path = os.path.join(tmp, "menu.irctl")
            r_conv = daemon.handle_ipc_request(
                {"op": "ir_convert", "src": "/ext/infrared/Apple_TV.ir", "dst": irctl_path, "button": "Menu"}
            )
            self.assertTrue(r_conv["ok"])
            self.assertTrue(os.path.isfile(irctl_path))

            # Optical `cam_scan` -> auto NTAG215 `.nfc` and `H10301` `.rfid`
            qr_img = Path(tmp) / "qr.png"
            qr_img.write_bytes(b"\x89PNG\r\n\x1a\n")
            with mock.patch(
                "z2z.hw_tools.decode_barcodes_with_zbar",
                return_value=[{"symbology": "QR-Code", "payload": "https://kali.org/qr"}],
            ):
                r_cam_nfc = daemon.handle_ipc_request(
                    {"op": "cam_scan", "image": str(qr_img), "target": "auto", "open_on_flipper": True}
                )
                self.assertTrue(r_cam_nfc["ok"])
                self.assertEqual(r_cam_nfc["target"], "nfc")
                self.assertEqual(r_cam_nfc["opened_app"], "NFC")

            with mock.patch(
                "z2z.hw_tools.decode_barcodes_with_zbar",
                return_value=[{"symbology": "CODE-128", "payload": "123:45678"}],
            ):
                r_cam_rfid = daemon.handle_ipc_request(
                    {"op": "cam_scan", "image": str(qr_img), "target": "auto", "open_on_flipper": True}
                )
                self.assertTrue(r_cam_rfid["ok"])
                self.assertEqual(r_cam_rfid["target"], "rfid")
                self.assertEqual(r_cam_rfid["opened_app"], "125 kHz RFID")


            # Verify CLI `z2z ir-tx` / `diff` / `rfid-make` / `ibtn-make` / `cam-scan` arg routing
            from z2z import cli as z2z_cli

            with mock.patch.object(z2z_cli, "ipc_call", side_effect=daemon.handle_ipc_request):
                self.assertEqual(
                    z2z_cli.main(["ir-tx", "/ext/infrared/Apple_TV.ir", "Menu"]),
                    0,
                )
                self.assertEqual(
                    z2z_cli.main(["ir-tx", "--proto", "NEC", "--addr", "0x04", "--cmd", "0x08"]),
                    0,
                )
                self.assertEqual(
                    z2z_cli.main(["diff", "/ext/nfc/tag1.nfc", "/ext/nfc/tag2.nfc"]),
                    0,
                )


if __name__ == "__main__":
    unittest.main()
