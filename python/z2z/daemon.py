"""On-demand Unix-socket daemon (`z2zd`) bridging Kali Linux and Flipper Zero BLE RPC.

Supports:
  1. `systemd` socket activation (`LISTEN_FDS=1`) or standalone auto-spawn with idle exit.
  2. Interactive passkey pairing handshake (`pin_required` event) for CLI and `z2z-ui`.
  3. Live bi-directional `PB_App.DataExchangeRequest` (`field 65`) with `zero2zero.fap`.
  4. Pocket Watch mode polling `/ext/apps_data/zero2zero/request.json` when untethered.
"""

from __future__ import annotations

import argparse
from collections import deque
import hashlib
import inspect
import json
import os
from pathlib import Path
import re
import select
import signal
import socket
import struct
import sys
import threading
import time
from typing import Callable, Deque, Dict, List, Optional, Tuple

from .ble_transport import BluezBleTransport, TransportBase, clean_dbus_error
from .captures import inspect_capture_bytes, list_remote_captures, parse_nfc_capture
from .hw_tools import (
    append_ir_signal_to_remote,
    build_ibutton_key,
    build_rfid_key,
    diff_capture_bytes,
    ir_to_irctl,
    irctl_to_ir,
    parse_ir_remote_full,
    scan_optical_code,
)
from .jobs import execute_job, load_jobs
from .netdiag import build_net_report, get_kali_tools_status
from .nfc_tools import build_ndef_nfc_tag, convert_nfc_bytes, encode_qr_with_qrencode
from .proto import CommandStatus, GUI_KEYS, render_screen_frame_unicode
from .rpc_client import FlipperRpcClient, FlipperRpcError
from .studio import (
    BUILTIN_BADUSB_TEMPLATES,
    build_ducky_script,
    default_studio_dir,
    list_local_templates,
    render_template_text,
    sanitize_script_filename,
)
from .subghz import (
    analyze_live_sub_burst,
    decode_with_rtl433,
    parse_and_analyze_sub,
    sub_to_ook,
    synthesize_raw_sub,
)
from .vault import sync_vault

MAILBOX_DIR = "/ext/apps_data/zero2zero"
MAILBOX_REQ = f"{MAILBOX_DIR}/request.json"
MAILBOX_RESP = f"{MAILBOX_DIR}/response.json"
DEFAULT_FAP_PATH = "/ext/apps/Bluetooth/zero2zero.fap"
_CLIENT_CRED = threading.local()


def find_bundled_fap() -> Optional[Path]:
    repo_fap = Path(__file__).resolve().parents[2] / "flipper_app" / "dist" / "zero2zero.fap"
    for cand in (repo_fap, Path("/usr/share/zero2zero/flipper_app/zero2zero.fap")):
        if cand.is_file():
            return cand
    return None


def resolve_socket_path() -> str:
    env_sock = os.environ.get("Z2Z_SOCKET")
    if env_sock:
        return env_sock
    sys_sock = "/run/zero2zero/rpc.sock"
    if os.path.exists(sys_sock) and os.access(sys_sock, os.W_OK):
        return sys_sock
    if os.geteuid() == 0:
        return sys_sock
    rt = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/zero2zero-{os.getuid()}"
    return os.path.join(rt, "rpc.sock")


class Zero2ZeroDaemon:
    def __init__(
        self,
        socket_path: str,
        transport: Optional[TransportBase] = None,
        idle_timeout_sec: float = 300.0,
        watch_enabled: bool = False,
        watch_interval_sec: float = 3.0,
    ) -> None:
        self.socket_path = socket_path
        self.transport: TransportBase = transport or BluezBleTransport()
        self.rpc = FlipperRpcClient(self.transport)
        self._fap_state_cond = threading.Condition()
        self._fap_duplex_ready: bool = False
        self._verified_fap_md5: str = ""
        self._ir_tx_seq: int = 1000
        self._ir_remote_cache: Dict[str, List[Dict[str, object]]] = {}
        self.rpc.set_app_callbacks(
            on_data=self._on_fap_data_exchange, on_state=self._on_fap_state
        )

        self.idle_timeout_sec = idle_timeout_sec
        self.watch_interval_sec = watch_interval_sec
        self._watch_enabled = watch_enabled
        self._watch_paused_by_user = False
        self._watch_fail_streak = 0
        self._last_mailbox_seq = -1
        self._last_activity = time.monotonic()
        self._active_clients = 0
        self._stop_event = threading.Event()
        self._op_lock = threading.RLock()
        self._recent_events: List[Dict[str, object]] = []
        self._upload_retry_delay_sec: Optional[float] = None
        self._connect_retry_delay_sec: float = 1.1
        self._gui_input_timeout_sec: float = 2.5
        self._cached_flipper_meta: Dict[str, object] = {}
        self._cached_flipper_meta_ts: float = 0.0
        self._gui_input_cond = threading.Condition()
        self._gui_input_deque: Deque[Tuple[str, str]] = deque()
        self._gui_input_inflight: int = 0
        self._gui_input_max_queue: int = 8
        self._last_gui_input_mono: float = 0.0
        self._gui_state_listeners: List[Callable[[int, bool, bool, str], None]] = []
        self._gui_state_listeners_lock = threading.Lock()
        self._gui_worker_started: bool = False
        self._last_sub_live: Dict[str, object] = {}
        self._last_ir_rx: Dict[str, object] = {}
        self.rpc.add_screen_listener(self._on_rpc_screen_event)

    def _on_fap_state(self, state: int) -> None:
        with self._fap_state_cond:
            # Flipper PB_App.AppState: APP_CLOSED = 0, APP_STARTED = 1
            self._fap_duplex_ready = state == 1
            self._fap_state_cond.notify_all()

    @property
    def gui_input_queue_depth(self) -> int:
        with self._gui_input_cond:
            return len(self._gui_input_deque) + self._gui_input_inflight

    def add_gui_state_listener(self, cb: Callable[[int, bool, bool, str], None]) -> None:
        with self._gui_state_listeners_lock:
            if cb not in self._gui_state_listeners:
                self._gui_state_listeners.append(cb)

    def remove_gui_state_listener(self, cb: Callable[[int, bool, bool, str], None]) -> None:
        with self._gui_state_listeners_lock:
            if cb in self._gui_state_listeners:
                self._gui_state_listeners.remove(cb)

    def _notify_gui_state_listeners(
        self, depth: int, connected: bool, streaming: bool, error: str = ""
    ) -> None:
        with self._gui_state_listeners_lock:
            listeners = list(self._gui_state_listeners)
        for cb in listeners:
            try:
                cb(depth, connected, streaming, error)
            except Exception:
                pass

    def _on_rpc_screen_event(
        self, _frame: bytes, _orient: int, _seq: int, streaming: bool
    ) -> None:
        if not streaming:
            self._clear_gui_input_queue()

    def _clear_gui_input_queue(self) -> None:
        with self._gui_input_cond:
            self._gui_input_deque.clear()
            depth = self._gui_input_inflight
            self._gui_input_cond.notify_all()
        self._notify_gui_state_listeners(
            depth, bool(self.transport.is_connected), bool(self.rpc.is_streaming_screen), ""
        )

    def _ensure_gui_input_worker(self) -> None:
        with self._gui_input_cond:
            if self._gui_worker_started:
                return
            self._gui_worker_started = True
            threading.Thread(
                target=self._gui_input_worker, name="z2z-gui-input", daemon=True
            ).start()

    def _send_gui_input_once(self, key: str, mode: str) -> None:
        with self._op_lock:
            was_streaming = self.rpc.is_streaming_screen
            self._last_gui_input_mono = time.monotonic()
            try:
                self._ensure_connected()
                self.rpc.gui_send_input(
                    key, mode=mode, timeout_sec=self._gui_input_timeout_sec
                )
            except (TimeoutError, ConnectionError) as exc:
                print(
                    f"[z2zd] GUI input ({key}/{mode}) stalled ({exc}); resetting Flipper BLE session and retrying once...",
                    file=sys.stderr,
                    flush=True,
                )
                self._reset_and_reconnect_rpc(restore_stream=was_streaming)
                try:
                    self.rpc.gui_send_input(
                        key, mode=mode, timeout_sec=self._gui_input_timeout_sec
                    )
                except TimeoutError:
                    try:
                        self.transport.disconnect()
                    except Exception:
                        pass
                    raise
            finally:
                self._last_gui_input_mono = time.monotonic()

    def _gui_input_worker(self) -> None:
        while not self._stop_event.is_set():
            with self._gui_input_cond:
                while not self._gui_input_deque and not self._stop_event.is_set():
                    self._gui_input_cond.wait(timeout=0.5)
                if self._stop_event.is_set():
                    break
                key, mode = self._gui_input_deque.popleft()
                self._gui_input_inflight = 1
                depth_start = len(self._gui_input_deque) + 1
            self._notify_gui_state_listeners(
                depth_start, bool(self.transport.is_connected), bool(self.rpc.is_streaming_screen), ""
            )
            err_msg = ""
            try:
                self._send_gui_input_once(key, mode)
            except Exception as exc:
                err_msg = clean_dbus_error(exc)
                self._log_event("err", err_msg[:80])
                print(
                    f"[z2zd] async GUI input error: {err_msg}",
                    file=sys.stderr,
                    flush=True,
                )
                with self._gui_input_cond:
                    self._gui_input_deque.clear()
            finally:
                with self._gui_input_cond:
                    self._gui_input_inflight = 0
                    depth_end = len(self._gui_input_deque)
                self._notify_gui_state_listeners(
                    depth_end,
                    bool(self.transport.is_connected),
                    bool(self.rpc.is_streaming_screen),
                    err_msg,
                )

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    def _log_event(self, source: str, summary: str) -> None:
        rec = {
            "ts": int(time.time()),
            "source": source,
            "summary": summary[:160],
        }
        self._recent_events.append(rec)
        if len(self._recent_events) > 25:
            del self._recent_events[:-25]

    # --- Bi-Directional Flipper `.fap` Command Handler (Duplex + Mailbox) ---

    def _execute_fap_request(self, req: Dict[str, object]) -> Dict[str, object]:
        self._touch()
        req_id = int(req.get("id", req.get("seq", 0)) or 0)
        op = str(req.get("op", "ping")).strip()
        self._log_event("flipper", f"op={op} id={req_id}")

        try:
            if op == "ping":
                soc = build_net_report()["soc"]
                txt = f"Kali Ready ({soc['temp_c']}C)\nRAM {soc['mem_used_mb']}/{soc['mem_total_mb']}MB"
                return {"id": req_id, "ok": True, "op": op, "text": txt}

            if op == "net":
                rep = build_net_report()
                return {"id": req_id, "ok": True, "op": op, "text": str(rep["compact_text"])[:700]}

            if op == "jobs":
                jobs = load_jobs()
                lines = [f"{j.id}: {j.title[:18]}" for j in jobs.values()]
                items = "\n".join(f"{j.id}|{j.title[:22]}" for j in jobs.values())
                return {
                    "id": req_id,
                    "ok": True,
                    "op": op,
                    "jobs": list(jobs.keys()),
                    "items": items,
                    "text": "\n".join(lines) if lines else "No jobs configured",
                }

            if op == "run":
                job_id = str(req.get("job", "")).strip()
                jobs = load_jobs()
                if job_id not in jobs:
                    return {"id": req_id, "ok": False, "op": op, "text": f"Unknown job: {job_id}"}
                job = jobs[job_id]
                needs_rpc = bool(job.pull_files or job.push_files or job.merge_lines_into)
                res = execute_job(
                    job,
                    rpc=self.rpc if needs_rpc else None,
                    rpc_lock=self._op_lock if needs_rpc else None,
                )
                ok = res["returncode"] == 0
                out = str(res["output"]) or f"Exit {res['returncode']}"
                if res["merged_added"]:
                    out = f"+{res['merged_added']} merged\n" + out
                resp_run: Dict[str, object] = {"id": req_id, "ok": ok, "op": op, "text": out[:700]}
                if self.rpc.is_streaming_screen:
                    resp_run["quiet"] = True
                return resp_run

            if op == "sub_live":
                freq = int(req.get("freq", 433920000) or 433920000)
                rssi = int(req["rssi"]) if req.get("rssi") is not None else None
                pulses = str(req.get("pulses", ""))
                live_res = analyze_live_sub_burst(pulses, frequency=freq, rssi_db=rssi)
                self._last_sub_live = {
                    "ts": int(time.time()),
                    **live_res,
                }
                if live_res.get("matched"):
                    self._log_event("rtl433", str(live_res.get("line1", "hit")))
                return {
                    "id": req_id,
                    "ok": True,
                    "op": op,
                    "matched": bool(live_res.get("matched")),
                    "line1": live_res.get("line1", ""),
                    "line2": live_res.get("line2", ""),
                    "line3": live_res.get("line3", ""),
                    "text": str(live_res.get("text", ""))[:400],
                    "quiet": (not bool(live_res.get("matched")))
                    or bool(self.rpc.is_streaming_screen),
                }

            if op == "ir_rx":
                sig_type = str(req.get("type", "parsed")).lower()
                if sig_type == "parsed":
                    proto = str(req.get("proto") or req.get("protocol") or "NEC")
                    addr_u32 = int(req.get("addr", 0) or 0)
                    cmd_u32 = int(req.get("cmd", 0) or 0)
                    summary = f"{proto} A:0x{addr_u32:X} C:0x{cmd_u32:X}"
                    self._last_ir_rx = {
                        "ts": int(time.time()),
                        "type": "parsed",
                        "protocol": proto,
                        "address_u32": addr_u32,
                        "command_u32": cmd_u32,
                        "summary": summary,
                    }
                else:
                    freq = int(req.get("freq", 38000) or 38000)
                    duty_pct = int(req.get("duty", 33) or 33)
                    pulses_s = str(req.get("pulses", ""))
                    timings = [int(t) for t in pulses_s.split() if t.isdigit() and int(t) > 0]
                    summary = f"RAW {len(timings)}p @{freq // 1000}kHz"
                    self._last_ir_rx = {
                        "ts": int(time.time()),
                        "type": "raw",
                        "frequency": freq,
                        "duty_cycle": round(duty_pct / 100.0, 2),
                        "timings": timings,
                        "pulses": len(timings),
                        "summary": summary,
                    }
                self._log_event("ir_rx", summary)
                return {
                    "id": req_id,
                    "ok": True,
                    "op": "ir_rx_ack",
                    "line3": "Kali cached! (ir-save)",
                    "quiet": True,
                }

            if op == "clock_sync":
                now_lt = time.localtime()
                iso_ts = time.strftime("%Y-%m-%d %H:%M:%S", now_lt)
                wday = int(time.strftime("%u", now_lt))
                rpc_synced = False
                if self.transport.is_connected:
                    try:
                        with self._op_lock:
                            dt = self.rpc.set_datetime()
                        iso_ts = dt.format_iso()
                        wday = dt.weekday
                        rpc_synced = True
                    except Exception:
                        pass
                resp_clk: Dict[str, object] = {
                    "id": req_id,
                    "ok": True,
                    "op": op,
                    "rtc": f"{iso_ts} {wday}",
                    "text": f"RTC Synced from Kali:\n{iso_ts}\nMode: {'BLE RPC + HAL' if rpc_synced else 'Flipper HAL'}",
                }
                if self.rpc.is_streaming_screen:
                    resp_clk["quiet"] = True
                return resp_clk

            if op == "sub_latest":
                with self._op_lock:
                    latest = self._inspect_latest_sub()
                resp_sub: Dict[str, object] = {
                    "id": req_id,
                    "ok": True,
                    "op": op,
                    "text": str(latest["compact_text"])[:700],
                }
                if self.rpc.is_streaming_screen:
                    resp_sub["quiet"] = True
                return resp_sub

            if op == "cap_latest":
                kind = str(req["kind"]) if req.get("kind") else None
                with self._op_lock:
                    latest = self._inspect_latest_capture(kind=kind)
                resp_cap: Dict[str, object] = {
                    "id": req_id,
                    "ok": True,
                    "op": op,
                    "text": str(latest["compact_text"])[:700],
                }
                if self.rpc.is_streaming_screen:
                    resp_cap["quiet"] = True
                return resp_cap

            if op == "vault":
                with self._op_lock:
                    vres = sync_vault(self.rpc)
                kb = round(int(vres["bytes_transferred"]) / 1024.0, 1)
                txt = (
                    f"Vault Sync OK\n"
                    f"New/Upd: {vres['pulled_count']} ({kb} KB)\n"
                    f"Indexed: {vres['total_indexed']}"
                )
                resp_v: Dict[str, object] = {"id": req_id, "ok": True, "op": op, "text": txt}
                if self.rpc.is_streaming_screen:
                    resp_v["quiet"] = True
                return resp_v

            return {"id": req_id, "ok": False, "op": op, "text": f"Unsupported op: {op}"}
        except Exception as exc:
            resp_err: Dict[str, object] = {"id": req_id, "ok": False, "op": op, "text": f"Err: {exc}"[:400]}
            if self.rpc.is_streaming_screen:
                resp_err["quiet"] = True
            return resp_err

    def _on_fap_data_exchange(self, payload: bytes) -> None:
        try:
            req = json.loads(payload.decode("utf-8", errors="replace"))
            if not isinstance(req, dict):
                return
        except Exception:
            return

        # Receiving DataExchange proves `.fap` is open right now (even if `z2zd` socket-activated
        # after `APP_STARTED`); if `APP_CLOSED` (`state=0`) arrives while `_execute_fap_request`
        # or the 45ms frame backoff runs, `_on_fap_state(0)` flips `_fap_duplex_ready` to False.
        with self._fap_state_cond:
            self._fap_duplex_ready = True

        # Run local Kali work WITHOUT holding `_op_lock` so Mirror `gui_input` never freezes.
        resp = self._execute_fap_request(req)
        raw_resp = (json.dumps(resp, separators=(",", ":")) + "\n").encode("utf-8")
        with self._op_lock:
            if not self._fap_duplex_ready or not self.transport.is_connected:
                return
            if self.rpc.is_streaming_screen:
                since_frame = time.monotonic() - self.rpc.last_screen_frame_mono
                if 0.0 <= since_frame < 0.045:
                    time.sleep(0.045 - since_frame)
            if not self._fap_duplex_ready or not self.transport.is_connected:
                return
            try:
                self.rpc.app_send_data(raw_resp)
            except Exception as exc:
                self._log_event("error", f"DataExchange reply failed: {exc}")

    def _poll_mirror_mailbox_if_idle(self) -> Optional[Dict[str, object]]:
        """Services manually opened `.fap` Mailbox requests during quiet Mirror idle windows."""
        if not (self.transport.is_connected and self.rpc.is_streaming_screen):
            return None
        now = time.monotonic()
        if self.gui_input_queue_depth > 0:
            return None
        if (now - self._last_gui_input_mono) < 0.9:
            return None
        if (now - self.rpc.last_screen_frame_mono) < 0.45:
            return None
        if not self._op_lock.acquire(blocking=False):
            return None
        req: Optional[Dict[str, object]] = None
        seq = 0
        try:
            if not (self.transport.is_connected and self.rpc.is_streaming_screen):
                return None
            if self.gui_input_queue_depth > 0:
                return None
            st = self.rpc.storage_stat(MAILBOX_REQ)
            if st is None or st.size == 0 or st.size > 8192:
                return None
            raw = self.rpc.storage_read(MAILBOX_REQ)
            req = json.loads(raw.decode("utf-8", errors="replace"))
            seq = int(req.get("seq", req.get("id", 0)) or 0)
            if seq == self._last_mailbox_seq:
                try:
                    self.rpc.storage_delete(MAILBOX_REQ)
                except Exception:
                    pass
                return None
            self._last_mailbox_seq = seq
        finally:
            self._op_lock.release()

        if req is None:
            return None
        resp = self._execute_fap_request(req)
        resp["seq"] = seq
        if self.rpc.is_streaming_screen:
            resp["quiet"] = True

        with self._op_lock:
            if not self.transport.is_connected:
                return None
            self.rpc.storage_mkdirs(MAILBOX_DIR)
            self.rpc.storage_write(
                MAILBOX_RESP, (json.dumps(resp, indent=2) + "\n").encode("utf-8")
            )
            try:
                self.rpc.storage_delete(MAILBOX_REQ)
            except Exception:
                pass
        return resp

    def _poll_pocket_mailbox_once(self) -> Optional[Dict[str, object]]:
        # Never contend with live GUI screen streaming or active UI/CLI requests,
        # and respect explicit user `disconnect` until `connect`/`pair`/`watch_set(True)`.
        if self._watch_paused_by_user or self.rpc.is_streaming_screen:
            return None
        if not self._op_lock.acquire(blocking=False):
            return None
        try:
            if self.rpc.is_streaming_screen:
                return None
            if not self.transport.is_connected:
                try:
                    self._connect_verified(None)
                except Exception as exc:
                    self._watch_fail_streak += 1
                    if self._watch_fail_streak == 1:
                        print(
                            f"[z2zd:watch] Flipper unavailable ({clean_dbus_error(exc)}); backing off",
                            file=sys.stderr,
                            flush=True,
                        )
                    return None
            self._watch_fail_streak = 0
            st = self.rpc.storage_stat(MAILBOX_REQ)
            if st is None or st.size == 0 or st.size > 8192:
                return None
            raw = self.rpc.storage_read(MAILBOX_REQ)
            req = json.loads(raw.decode("utf-8", errors="replace"))
            seq = int(req.get("seq", req.get("id", 0)) or 0)
            if seq <= self._last_mailbox_seq:
                return None
            self._last_mailbox_seq = seq
            resp = self._execute_fap_request(req)
            resp["seq"] = seq
            self.rpc.storage_mkdirs(MAILBOX_DIR)
            self.rpc.storage_write(
                MAILBOX_RESP, (json.dumps(resp, indent=2) + "\n").encode("utf-8")
            )
            try:
                self.rpc.trigger_gentle_alert()
            except Exception:
                pass
            return resp
        finally:
            self._op_lock.release()

    def _watch_worker(self) -> None:
        while not self._stop_event.is_set():
            if self.rpc.is_streaming_screen and self.transport.is_connected:
                try:
                    self._poll_mirror_mailbox_if_idle()
                except Exception as exc:
                    self._log_event("watch", f"Mirror mailbox poll: {exc}")
            elif self._watch_enabled:
                try:
                    self._poll_pocket_mailbox_once()
                except Exception as exc:
                    self._watch_fail_streak += 1
                    self._log_event("watch", f"Mailbox poll: {exc}")
            if self.rpc.is_streaming_screen and self.transport.is_connected:
                delay = 1.2
            elif self._watch_enabled and self._watch_fail_streak > 0:
                base = max(self.watch_interval_sec, 1.5)
                delay = min(20.0, base * (2 ** min(self._watch_fail_streak - 1, 3)))
            else:
                delay = self.watch_interval_sec
            self._stop_event.wait(delay)

    # --- Helpers Shared by IPC & Flipper Handlers ---

    def _connect_verified(self, target: Optional[str] = None) -> Dict[str, object]:
        last_exc: Optional[Exception] = None
        for attempt in range(2):
            info = self.transport.connect(target)
            self._cached_flipper_meta_ts = 0.0
            self._fap_duplex_ready = False
            self._verified_fap_md5 = ""
            self._ir_remote_cache.clear()
            self.rpc.reset_session_state()
            try:
                # Retry ping once on the SAME live BLE link before tearing down a valid session
                ping_err: Optional[Exception] = None
                for p_try in range(2):
                    try:
                        echo = self.rpc.ping(
                            b"z2z", timeout_sec=1.8 if p_try == 0 else 2.5
                        )
                        if echo != b"z2z":
                            raise RuntimeError(f"Unexpected Flipper RPC ping echo: {echo!r}")
                        ping_err = None
                        break
                    except Exception as p_exc:
                        ping_err = p_exc
                        if p_try == 0 and self.transport.is_connected:
                            time.sleep(0.08)
                            continue
                        break
                if ping_err is not None:
                    raise ping_err
                return info
            except Exception as exc:
                last_exc = exc
                print(
                    f"[z2zd] post-connect RPC ping check {attempt + 1}/2 failed: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                try:
                    self.transport.disconnect()
                except Exception:
                    pass
                if attempt == 0:
                    if self._connect_retry_delay_sec > 0:
                        time.sleep(self._connect_retry_delay_sec)
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    def _ensure_connected(self, target: Optional[str] = None) -> None:
        if not self.transport.is_connected:
            self._connect_verified(target)

    def _reset_and_reconnect_rpc(self, restore_stream: bool = False) -> None:
        """Disconnects a wedged BLE session (`bt.c:302` sets `BT_RPC_EVENT_DISCONNECTED`) & reconnects."""
        self.rpc.mark_screen_stream_stopped()
        self._cached_flipper_meta_ts = 0.0
        try:
            self.transport.disconnect()
        except Exception:
            pass
        if self._connect_retry_delay_sec > 0:
            time.sleep(self._connect_retry_delay_sec)
        self._connect_verified()
        if restore_stream:
            self.rpc.gui_start_screen_stream(wait_first_frame_sec=0.6)

    def _upload_file_reliable(
        self, remote_path: str, data: bytes, atomic: Optional[bool] = None
    ) -> str:
        """Uploads `data` to Flipper SD with MD5 verification, optional `.tmp` staging, and retry.

        Under shared 2.4 GHz Wi-Fi/BLE load on `BCM4343A2`, a dropped
        `WRITE_WITHOUT_RESPONSE` packet causes Flipper (`rpc.c:320-335`) to emit
        `Main(command_id=0, command_status=ERROR_DECODE)` and call `bt_profile_restore_default()`.
        Staging `.fap`/large files to `.tmp`, verifying `storage_md5`, and re-syncing the BLE
        session on decode/link errors prevents corrupt files and eliminates manual retries.
        """
        use_atomic = (
            (len(data) >= 1024 or remote_path.lower().endswith(".fap"))
            if atomic is None
            else bool(atomic)
        )
        expected_md5 = hashlib.md5(data).hexdigest().lower()
        parent = os.path.dirname(remote_path.rstrip("/")) or "/ext"
        stage_path = f"{remote_path}.tmp" if use_atomic else remote_path

        try:
            for attempt in range(3):
                try:
                    self._ensure_connected()
                    if attempt > 0 and hasattr(self.transport, "tune_upload_profile"):
                        self.transport.tune_upload_profile(conservative=True)
                    chunk_sz = 256 if attempt == 0 else 192
                    self.rpc.storage_mkdirs(parent)
                    if use_atomic:
                        self.rpc.storage_delete(stage_path)
                    self.rpc.storage_write(stage_path, data, chunk_size=chunk_sz)
                    if data:
                        got_md5 = self.rpc.storage_md5(stage_path).strip().lower()
                        if got_md5 and got_md5 != expected_md5:
                            raise RuntimeError(
                                f"Flipper SD MD5 mismatch after upload ({got_md5} != {expected_md5})"
                            )
                    if use_atomic:
                        self.rpc.storage_delete(remote_path)
                        self.rpc.storage_rename(stage_path, remote_path)
                    return expected_md5
                except (FlipperRpcError, ConnectionError, TimeoutError, OSError, RuntimeError) as exc:
                    is_decode_err = (
                        isinstance(exc, FlipperRpcError)
                        and exc.status_code == CommandStatus.ERROR_DECODE
                    )
                    is_retryable = is_decode_err or isinstance(
                        exc, (ConnectionError, TimeoutError, OSError)
                    ) or "MD5 mismatch" in str(exc)
                    if attempt >= 2 or not is_retryable:
                        raise
                    print(
                        f"[z2zd] BLE upload attempt {attempt + 1}/3 for {remote_path} failed ({exc}); re-syncing session...",
                        file=sys.stderr,
                        flush=True,
                    )
                    try:
                        self.transport.disconnect()
                    except Exception:
                        pass
                    if self._upload_retry_delay_sec is not None:
                        wait_s = self._upload_retry_delay_sec
                    else:
                        # Give Flipper's `bt_profile_restore_default(bt)` time to re-advertise after ERROR_DECODE
                        wait_s = 1.3 if is_decode_err else 0.6
                    if wait_s > 0:
                        time.sleep(wait_s)
        finally:
            if hasattr(self.transport, "tune_upload_profile"):
                self.transport.tune_upload_profile(conservative=False)
        return expected_md5

    def _inspect_latest_sub(self, remote_path: Optional[str] = None) -> Dict[str, object]:
        self._ensure_connected()
        chosen = remote_path
        if not chosen:
            items = [
                f for f in self.rpc.storage_list("/ext/subghz") if not f.is_dir and f.name.endswith(".sub")
            ]
            if not items:
                raise FileNotFoundError("No `.sub` captures found in /ext/subghz")
            items.sort(key=lambda x: x.name)
            chosen = f"/ext/subghz/{items[-1].name}"
        raw = self.rpc.storage_read(chosen)
        rep = parse_and_analyze_sub(raw)
        out = rep.to_dict()
        out["remote_path"] = chosen
        out["compact_text"] = f"File: {os.path.basename(chosen)}\n" + rep.format_compact()
        return out

    def _inspect_latest_capture(
        self, kind: Optional[str] = None, remote_path: Optional[str] = None
    ) -> Dict[str, object]:
        self._ensure_connected()
        chosen = remote_path
        if not chosen:
            rows = list_remote_captures(self.rpc, kind=kind, limit=16)
            if not rows:
                raise FileNotFoundError("No captures found in /ext/{subghz,nfc,lfrfid,infrared}")
            chosen = str(rows[0]["path"])
        raw = self.rpc.storage_read(chosen)
        out = inspect_capture_bytes(chosen, raw)
        out["remote_path"] = chosen
        return out

    @staticmethod
    def _write_local_file(local_path: str | Path, data: bytes | str) -> str:
        lp = Path(local_path).expanduser().resolve()
        lp.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, bytes):
            lp.write_bytes(data)
        else:
            lp.write_text(data, encoding="utf-8")
        if os.geteuid() == 0:
            try:
                peer_uid = int(getattr(_CLIENT_CRED, "uid", 0) or 0)
                peer_gid = int(getattr(_CLIENT_CRED, "gid", 0) or 0)
                if peer_uid > 0:
                    os.chown(lp, peer_uid, peer_gid)
                else:
                    p_stat = lp.parent.stat()
                    if p_stat.st_uid != 0:
                        os.chown(lp, p_stat.st_uid, p_stat.st_gid)
            except OSError:
                pass
        return str(lp)

    def _read_local_or_remote(self, path_str: str) -> bytes:
        s = path_str.strip()
        if s.startswith(("/ext/", "/int/")):
            with self._op_lock:
                self._ensure_connected()
                return self.rpc.storage_read(s)
        return Path(s).expanduser().read_bytes()

    def _launch_fap_duplex_locked(
        self, fap_path: str = DEFAULT_FAP_PATH, force_verify: bool = False
    ) -> bool:
        """Ensures `zero2zero.fap` is up-to-date on Flipper SD and starts it in RPC Duplex mode."""
        auto_installed = False
        self._ensure_connected()
        self.rpc.ensure_app_unlocked()
        bundled = find_bundled_fap()
        if bundled is not None and os.path.basename(fap_path) == bundled.name:
            bundled_bytes = bundled.read_bytes()
            expected_md5 = hashlib.md5(bundled_bytes).hexdigest().lower()
            if force_verify or self._verified_fap_md5 != expected_md5:
                st_fap = self.rpc.storage_stat(fap_path)
                remote_md5 = ""
                if st_fap is not None and st_fap.size == len(bundled_bytes):
                    try:
                        remote_md5 = self.rpc.storage_md5(fap_path).strip().lower()
                    except Exception:
                        remote_md5 = ""
                if (
                    st_fap is None
                    or st_fap.size != len(bundled_bytes)
                    or remote_md5 != expected_md5
                ):
                    self._upload_file_reliable(fap_path, bundled_bytes, atomic=True)
                    auto_installed = True
                    self._log_event("kali", f"installed {bundled.name} -> {fap_path}")
                self._verified_fap_md5 = expected_md5

        with self._fap_state_cond:
            self._fap_duplex_ready = False
        try:
            self.rpc.app_start(fap_path, "RPC")
        except FlipperRpcError as exc:
            if exc.status_code != CommandStatus.ERROR_APP_SYSTEM_LOCKED:
                raise
            if isinstance(self.transport, BluezBleTransport):
                time.sleep(0.35)
            self.rpc.ensure_app_unlocked()
            self.rpc.app_start(fap_path, "RPC")

        wait_cap = 2.0 if isinstance(self.transport, BluezBleTransport) else 0.02
        with self._fap_state_cond:
            if not self._fap_duplex_ready:
                self._fap_state_cond.wait(timeout=wait_cap)
            self._fap_duplex_ready = True
        return auto_installed

    def _send_fap_frame_reliable(self, payload_obj: Dict[str, object]) -> bool:
        """Sends a live JSON frame to `zero2zero.fap`, auto-launching it in RPC mode if needed."""
        raw = (json.dumps(payload_obj, separators=(",", ":")) + "\n").encode("utf-8")
        with self._op_lock:
            self._ensure_connected()
            if self._fap_duplex_ready:
                try:
                    self.rpc.app_send_data(raw)
                    return False
                except FlipperRpcError:
                    self._fap_duplex_ready = False
            self._launch_fap_duplex_locked(DEFAULT_FAP_PATH)
            retry_sleep = 0.18 if isinstance(self.transport, BluezBleTransport) else 0.005
            for attempt in range(3):
                try:
                    self.rpc.app_send_data(raw)
                    break
                except FlipperRpcError as exc:
                    if (
                        attempt >= 2
                        or exc.status_code
                        not in (
                            CommandStatus.ERROR_APP_NOT_RUNNING,
                            CommandStatus.ERROR_APP_CMD_ERROR,
                        )
                    ):
                        raise
                    time.sleep(retry_sleep)
            return True

    # --- Unix Socket IPC Dispatcher ---

    def handle_ipc_request(
        self,
        req: Dict[str, object],
        pin_requester: Optional[Callable[[str, str], str]] = None,
        pin_cancel_cb: Optional[Callable[[], None]] = None,
    ) -> Dict[str, object]:
        op = str(req.get("op", "status"))
        was_connected = self.transport.is_connected
        try:
            return self._dispatch_ipc_request(
                req, pin_requester=pin_requester, pin_cancel_cb=pin_cancel_cb
            )
        except TimeoutError:
            # On a still-connected Flipper session, an RPC timeout can leave
            # `bt_rpc_send_bytes_callback` waiting forever on `BT_RPC_EVENT_ALL`. Force a
            # clean BLE disconnect so Flipper sets `BT_RPC_EVENT_DISCONNECTED` and re-advertises.
            if op not in ("scan", "pair", "unpair", "connect"):
                self.rpc.mark_screen_stream_stopped()
                if self.transport.is_connected:
                    try:
                        self.transport.disconnect()
                    except Exception:
                        pass
            raise
        except ConnectionError as exc:
            if (
                was_connected
                and not self.transport.is_connected
                and op
                not in (
                    "pair",
                    "unpair",
                    "connect",
                    "disconnect",
                    "scan",
                    "shutdown",
                    "screen_frame",
                    "screen_stop",
                    "gui_input",
                )
            ):
                print(
                    f"[z2zd] BLE link dropped during {op} ({exc}); auto-reconnecting once...",
                    file=sys.stderr,
                    flush=True,
                )
                return self._dispatch_ipc_request(
                    req, pin_requester=pin_requester, pin_cancel_cb=pin_cancel_cb
                )
            raise

    def _dispatch_ipc_request(
        self,
        req: Dict[str, object],
        pin_requester: Optional[Callable[[str, str], str]] = None,
        pin_cancel_cb: Optional[Callable[[], None]] = None,
    ) -> Dict[str, object]:
        self._touch()
        op = str(req.get("op", "status"))

        if op == "ping_daemon":
            return {
                "ok": True,
                "connected": self.transport.is_connected,
                "watch_enabled": self._watch_enabled,
                "socket": self.socket_path,
            }

        if op == "shutdown":
            self._stop_event.set()
            return {"ok": True}

        if op == "scan":
            if hasattr(self.transport, "scan"):
                devs = [d.to_dict() for d in self.transport.scan(float(req.get("timeout", 4.0)))]
            else:
                devs = [self.transport.link_info()]
            return {"ok": True, "devices": devs}

        if op == "pair":
            target = req.get("target")
            preset_pin = str(req.get("pin", "")).strip()
            force = bool(req.get("force", False))
            auto_connect = bool(req.get("auto_connect", True))

            def _get_pin(name: str, addr: str) -> str:
                if preset_pin:
                    return preset_pin
                if pin_requester is not None:
                    return pin_requester(name, addr)
                raise RuntimeError("Passkey required; supply `pin` or interactive client")

            if hasattr(self.transport, "pair"):
                pair_fn = self.transport.pair
                sig_params = inspect.signature(pair_fn).parameters
                kwargs: Dict[str, object] = {"pin_provider": _get_pin}
                if "cancel_cb" in sig_params:
                    kwargs["cancel_cb"] = pin_cancel_cb
                if "force" in sig_params:
                    kwargs["force"] = force
                dev = pair_fn(str(target) if target else None, **kwargs)
                self._watch_paused_by_user = False
                self._watch_fail_streak = 0
                resp: Dict[str, object] = {"ok": True, "device": dev.to_dict()}
                if auto_connect:
                    with self._op_lock:
                        try:
                            resp["link"] = self._connect_verified(dev.address)
                            resp["connected"] = bool(self.transport.is_connected)
                        except Exception as exc:
                            print(
                                f"[z2zd] post-pair auto-connect warning: {exc}",
                                file=sys.stderr,
                                flush=True,
                            )
                            resp["connected"] = False
                            resp["connect_warning"] = str(exc)
                return resp
            return {"ok": True, "device": self.transport.link_info(), "connected": True}

        if op == "unpair":
            self._watch_paused_by_user = True
            self.rpc.mark_screen_stream_stopped()
            with self._op_lock:
                target = str(req["target"]) if req.get("target") else None
                if hasattr(self.transport, "unpair"):
                    info = self.transport.unpair(target)
                    return {"ok": True, **info}
                self.transport.disconnect()
                return {"ok": True, "removed": True}

        if op == "connect":
            self._watch_paused_by_user = False
            self._watch_fail_streak = 0
            with self._op_lock:
                info = self._connect_verified(str(req["target"]) if req.get("target") else None)
                return {"ok": True, "link": info}

        if op == "disconnect":
            self._watch_paused_by_user = True
            self._cached_flipper_meta = {}
            self._cached_flipper_meta_ts = 0.0
            self._fap_duplex_ready = False
            self._verified_fap_md5 = ""
            self._ir_remote_cache.clear()
            self._clear_gui_input_queue()
            was_streaming = self.rpc.is_streaming_screen
            self.rpc.mark_screen_stream_stopped()
            with self._op_lock:
                if self.transport.is_connected and was_streaming:
                    try:
                        self.rpc.gui_stop_screen_stream()
                    except Exception:
                        pass
                self.transport.disconnect()
                return {"ok": True}

        if op == "watch_set":
            self._watch_enabled = bool(req.get("enabled", True))
            if self._watch_enabled:
                self._watch_paused_by_user = False
                self._watch_fail_streak = 0
            return {"ok": True, "watch_enabled": self._watch_enabled}

        if op == "status":
            link = self.transport.link_info()
            soc = build_net_report()["soc"]
            known_devs: List[Dict[str, object]] = []
            if hasattr(self.transport, "list_known_flippers"):
                try:
                    known_devs = [d.to_dict() for d in self.transport.list_known_flippers()]
                except Exception:
                    pass
            flipper_meta: Dict[str, object] = {}
            if not self.transport.is_connected:
                self._cached_flipper_meta_ts = 0.0
            elif bool(req.get("query_rpc", True)):
                now_m = time.monotonic()
                force_rpc = bool(req.get("force_rpc", False))
                cache_fresh = bool(self._cached_flipper_meta) and (
                    (now_m - self._cached_flipper_meta_ts) < 10.0
                )
                if not force_rpc and (self.rpc.is_streaming_screen or cache_fresh):
                    flipper_meta = dict(self._cached_flipper_meta)
                else:
                    with self._op_lock:
                        try:
                            total_b, free_b = self.rpc.storage_info("/ext")
                            flipper_meta["ext_total_kb"] = total_b // 1024
                            flipper_meta["ext_free_kb"] = free_b // 1024
                        except Exception:
                            pass
                        try:
                            pinfo = self.rpc.power_info()
                            if "charge_level" in pinfo:
                                flipper_meta["charge_pct"] = int(pinfo["charge_level"])
                            if "charge_state" in pinfo:
                                flipper_meta["charge_state"] = pinfo["charge_state"]
                        except Exception:
                            pass
                    if flipper_meta:
                        self._cached_flipper_meta = dict(flipper_meta)
                        self._cached_flipper_meta_ts = time.monotonic()
            return {
                "ok": True,
                "link": link,
                "devices": known_devs,
                "kali_soc": soc,
                "kali_tools": get_kali_tools_status(),
                "flipper": flipper_meta,
                "watch_enabled": self._watch_enabled,
                "last_sub_live": dict(self._last_sub_live) if self._last_sub_live else {},
                "last_ir_rx": dict(self._last_ir_rx) if self._last_ir_rx else {},
                "recent_events": list(self._recent_events[-6:]),
            }

        if op == "net_summary":
            return {"ok": True, "report": build_net_report()}

        if op == "jobs_list":
            jobs = load_jobs()
            return {
                "ok": True,
                "jobs": [j.to_summary() for j in jobs.values()],
                "templates": list_local_templates(),
            }

        if op == "job_run":
            job_id = str(req.get("job_id", "")).strip()
            jobs = load_jobs()
            if job_id not in jobs:
                raise KeyError(f"Unknown job id: {job_id!r}")
            res = execute_job(
                jobs[job_id],
                rpc=self.rpc if self.transport.is_connected else None,
                rpc_lock=self._op_lock,
            )
            self._log_event("kali", f"job:{job_id} rc={res['returncode']}")
            return {"ok": res["returncode"] == 0, "result": res}

        if op == "ls":
            path = str(req.get("path", "/ext"))
            with self._op_lock:
                self._ensure_connected()
                entries = self.rpc.storage_list(path, include_md5=bool(req.get("md5", False)))
            return {
                "ok": True,
                "path": path,
                "entries": [
                    {
                        "name": e.name,
                        "is_dir": e.is_dir,
                        "size": e.size,
                        "md5sum": e.md5sum,
                    }
                    for e in entries
                ],
            }

        if op == "pull":
            remote = str(req["remote"])
            with self._op_lock:
                self._ensure_connected()
                blob = self.rpc.storage_read(remote)
            local_str = self._write_local_file(str(req["local"]), blob)
            return {"ok": True, "remote": remote, "local": local_str, "bytes": len(blob)}

        if op == "push":
            local = Path(str(req["local"])).expanduser().resolve()
            remote = str(req["remote"])
            render = bool(req.get("template", False))
            raw = local.read_bytes()
            if render:
                raw = render_template_text(raw.decode("utf-8", errors="replace")).encode("utf-8")
            with self._op_lock:
                self._upload_file_reliable(remote, raw)
                if req.get("load_on_flipper"):
                    self._fap_duplex_ready = False
                    self.rpc.open_file_in_flipper_app(remote)
            return {"ok": True, "local": str(local), "remote": remote, "bytes": len(raw)}

        if op == "studio_push":
            name = os.path.basename(str(req["name"]))
            dest_dir = str(req.get("dest_dir", "/ext/apps_data/zero2zero")).rstrip("/")
            src = default_studio_dir() / name
            if "content" in req and req["content"] is not None:
                src.write_text(str(req["content"]), encoding="utf-8")
            if not src.is_file():
                raise FileNotFoundError(f"Template not found: {src}")
            rendered = render_template_text(src.read_text(encoding="utf-8", errors="replace"))
            remote = f"{dest_dir}/{name}"
            with self._op_lock:
                self._upload_file_reliable(remote, rendered.encode("utf-8"))
            return {"ok": True, "remote": remote, "bytes": len(rendered)}

        if op == "sub_inspect":
            remote = str(req["remote"]) if req.get("remote") else None
            local = str(req["local"]) if req.get("local") else None
            if local:
                raw = Path(local).expanduser().read_bytes()
                rep = parse_and_analyze_sub(raw)
                d = rep.to_dict()
                d["local_path"] = local
                d["compact_text"] = rep.format_compact()
                return {"ok": True, "report": d}
            with self._op_lock:
                return {"ok": True, "report": self._inspect_latest_sub(remote)}

        if op == "sub_synth":
            sub_txt = synthesize_raw_sub(
                bitstream=str(req["bits"]),
                frequency=int(req.get("frequency", 433920000)),
                te_us=int(req.get("te_us", 350)),
                repeat=int(req.get("repeat", 4)),
            )
            remote = str(req.get("remote", ""))
            local = str(req.get("local", ""))
            if local:
                Path(local).expanduser().write_text(sub_txt, encoding="utf-8")
            if remote:
                with self._op_lock:
                    self._upload_file_reliable(remote, sub_txt.encode("utf-8"))
            return {"ok": True, "remote": remote, "local": local, "bytes": len(sub_txt)}

        if op == "vault_sync":
            with self._op_lock:
                self._ensure_connected()
                res = sync_vault(self.rpc)
            self._log_event("kali", f"vault synced {res['pulled_count']} files")
            return {"ok": True, "vault": res}

        if op == "fap_launch":
            fap_path = str(req.get("path") or DEFAULT_FAP_PATH)
            with self._op_lock:
                auto_installed = self._launch_fap_duplex_locked(fap_path, force_verify=True)
            return {"ok": True, "path": fap_path, "auto_installed": auto_installed}

        if op == "clock_sync":
            with self._op_lock:
                self._ensure_connected()
                dt = self.rpc.set_datetime()
            self._log_event("kali", f"clock_sync {dt.format_iso()}")
            return {"ok": True, "datetime": dt.format_iso(), "weekday": dt.weekday}

        if op == "alert_mode":
            with self._op_lock:
                self._ensure_connected()
                if req.get("mode"):
                    mode = self.rpc.write_flipper_alert_mode(str(req["mode"]))
                else:
                    mode = self.rpc.read_flipper_alert_mode()
            return {"ok": True, "alert_mode": mode}

        if op == "screen_start":
            with self._op_lock:
                self._ensure_connected()
                frame, orient, seq = self.rpc.gui_start_screen_stream(
                    wait_first_frame_sec=float(req.get("wait", 1.0))
                )
            resp_ss: Dict[str, object] = {
                "ok": True,
                "connected": bool(self.transport.is_connected),
                "streaming": bool(self.rpc.is_streaming_screen),
                "seq": seq,
                "orientation": orient,
                "frame_hex": frame.hex() if len(frame) >= 1024 else "",
            }
            if req.get("unicode") and len(frame) >= 1024:
                resp_ss["unicode"] = render_screen_frame_unicode(frame, orient)
            return resp_ss

        if op == "screen_stop":
            was_streaming = self.rpc.is_streaming_screen
            self._clear_gui_input_queue()
            self.rpc.mark_screen_stream_stopped()
            if was_streaming and self.transport.is_connected:
                if self._op_lock.acquire(timeout=0.6):
                    try:
                        if self.transport.is_connected:
                            self.rpc.gui_stop_screen_stream(timeout_sec=2.0)
                    except Exception:
                        pass
                    finally:
                        self._op_lock.release()
            return {
                "ok": True,
                "connected": bool(self.transport.is_connected),
                "streaming": False,
            }

        if op == "screen_frame":
            since_seq = (
                int(req["since_seq"])
                if ("since_seq" in req and req["since_seq"] is not None)
                else None
            )
            wait_ms = float(req.get("wait_ms", 0.0) or 0.0)
            frame, orient, seq, streaming = self.rpc.gui_get_latest_frame(
                wait_after_seq=since_seq,
                wait_sec=min(0.25, max(0.0, wait_ms / 1000.0)),
            )
            if not streaming and bool(req.get("auto_start", True)):
                with self._op_lock:
                    self._ensure_connected()
                    frame, orient, seq = self.rpc.gui_start_screen_stream(
                        wait_first_frame_sec=float(req.get("wait", 0.8))
                    )
                    streaming = bool(self.rpc.is_streaming_screen)
            changed = (since_seq is None) or (seq != since_seq)
            resp_sf: Dict[str, object] = {
                "ok": True,
                "connected": bool(self.transport.is_connected),
                "streaming": streaming,
                "seq": seq,
                "orientation": orient,
                "frame_hex": (frame.hex() if (changed and len(frame) >= 1024) else ""),
            }
            if req.get("unicode") and len(frame) >= 1024:
                resp_sf["unicode"] = render_screen_frame_unicode(frame, orient)
            return resp_sf

        if op == "gui_input":
            key = str(req["key"]).strip().lower()
            if key not in GUI_KEYS:
                raise ValueError(f"Unknown Flipper key {req['key']!r} (expected {sorted(GUI_KEYS)})")
            mode = str(req.get("mode", "short")).strip().lower()
            if mode not in ("short", "long"):
                raise ValueError(f"Unsupported input mode {mode!r} (expected 'short' or 'long')")
            if bool(req.get("async", False)):
                self._ensure_gui_input_worker()
                with self._gui_input_cond:
                    depth = len(self._gui_input_deque) + self._gui_input_inflight
                    if bool(req.get("coalesce_repeat", False)) and depth >= 1:
                        return {
                            "ok": True,
                            "async": True,
                            "queued": False,
                            "coalesced": True,
                            "queue_depth": depth,
                            "connected": bool(self.transport.is_connected),
                            "streaming": bool(self.rpc.is_streaming_screen),
                        }
                    if len(self._gui_input_deque) >= self._gui_input_max_queue:
                        self._gui_input_deque.popleft()
                    self._gui_input_deque.append((key, mode))
                    self._last_gui_input_mono = time.monotonic()
                    depth = len(self._gui_input_deque) + self._gui_input_inflight
                    self._gui_input_cond.notify()
                return {
                    "ok": True,
                    "async": True,
                    "queued": True,
                    "key": key,
                    "mode": mode,
                    "queue_depth": depth,
                    "connected": bool(self.transport.is_connected),
                    "streaming": bool(self.rpc.is_streaming_screen),
                }

            since_seq = (
                int(req["since_seq"])
                if ("since_seq" in req and req["since_seq"] is not None)
                else None
            )
            self._send_gui_input_once(key, mode)
            frame, orient, seq, streaming = self.rpc.gui_get_latest_frame()
            changed = (since_seq is None) or (seq != since_seq)
            return {
                "ok": True,
                "connected": bool(self.transport.is_connected),
                "key": key,
                "mode": mode,
                "seq": seq,
                "orientation": orient,
                "streaming": streaming,
                "frame_hex": (frame.hex() if (changed and len(frame) >= 1024) else ""),
            }

        if op == "captures_list":
            kind = str(req["kind"]) if req.get("kind") else None
            limit = int(req.get("limit", 24))
            with self._op_lock:
                self._ensure_connected()
                items = list_remote_captures(self.rpc, kind=kind, limit=limit)
            return {"ok": True, "captures": items}

        if op == "capture_inspect":
            remote = str(req["remote"]) if req.get("remote") else None
            local = str(req["local"]) if req.get("local") else None
            kind = str(req["kind"]) if req.get("kind") else None
            if local:
                p = Path(local).expanduser()
                rep = inspect_capture_bytes(str(p), p.read_bytes())
                rep["local_path"] = str(p)
                return {"ok": True, "report": rep}
            with self._op_lock:
                rep = self._inspect_latest_capture(kind=kind, remote_path=remote)
            return {"ok": True, "report": rep}

        if op == "capture_open":
            remote = str(req["remote"])
            with self._op_lock:
                self._ensure_connected()
                self._fap_duplex_ready = False
                app_name = self.rpc.open_file_in_flipper_app(remote)
            self._log_event("kali", f"open {os.path.basename(remote)} -> {app_name}")
            return {"ok": True, "remote": remote, "app": app_name}

        if op == "badusb_templates":
            return {"ok": True, "templates": BUILTIN_BADUSB_TEMPLATES}

        if op == "badusb_push":
            fname = sanitize_script_filename(str(req.get("name") or "z2z_payload"))
            text = str(req.get("content", ""))
            raw_ducky = bool(req.get("raw_ducky", False))
            expand = bool(req.get("template", True))
            open_app = bool(req.get("open_on_flipper", False))
            script = build_ducky_script(text, raw_ducky=raw_ducky, template_expand=expand)
            remote = f"/ext/badusb/{fname}"
            with self._op_lock:
                self._upload_file_reliable(remote, script.encode("utf-8"))
                app_name = ""
                if open_app:
                    self._fap_duplex_ready = False
                    app_name = self.rpc.open_file_in_flipper_app(remote)
            self._log_event("kali", f"badusb -> {remote} ({len(script)}B)")
            return {
                "ok": True,
                "remote": remote,
                "bytes": len(script),
                "opened_app": app_name,
                "script": script,
            }

        if op == "sub_decode":
            remote = str(req["remote"]) if req.get("remote") else None
            local = str(req["local"]) if req.get("local") else None
            ook_out = str(req["ook_out"]) if req.get("ook_out") else ""
            if local:
                p = Path(local).expanduser()
                raw = p.read_bytes()
                src_label = str(p)
            else:
                with self._op_lock:
                    self._ensure_connected()
                    chosen = remote
                    if not chosen:
                        items = [
                            f
                            for f in self.rpc.storage_list("/ext/subghz")
                            if not f.is_dir and f.name.endswith(".sub")
                        ]
                        if not items:
                            raise FileNotFoundError("No `.sub` captures found in /ext/subghz")
                        items.sort(key=lambda x: x.name)
                        chosen = f"/ext/subghz/{items[-1].name}"
                    raw = self.rpc.storage_read(chosen)
                    src_label = chosen

            if ook_out:
                ook_txt = sub_to_ook(raw)
                out_p = Path(ook_out).expanduser()
                out_p.parent.mkdir(parents=True, exist_ok=True)
                out_p.write_text(ook_txt, encoding="utf-8")

            rep = parse_and_analyze_sub(raw, use_rtl433=True)
            r433 = decode_with_rtl433(raw, frequency=rep.frequency)
            report = rep.to_dict()
            report["source"] = src_label
            report["rtl433"] = r433
            compact = f"File: {os.path.basename(src_label)}\n" + rep.format_compact()
            if not r433.get("available"):
                compact += f"\nNote: {r433.get('hint', 'rtl_433 unavailable')}"
            elif not r433.get("matched"):
                compact += "\nrtl433: (no known protocol match)"
            report["compact_text"] = compact
            return {"ok": True, "report": report, "ook_out": ook_out}

        if op == "nfc_convert":
            src = str(req["src"])
            dst = str(req["dst"])
            if src.startswith(("/ext/", "/int/")):
                with self._op_lock:
                    self._ensure_connected()
                    src_bytes = self.rpc.storage_read(src)
            else:
                src_bytes = Path(src).expanduser().read_bytes()

            converted = convert_nfc_bytes(src_bytes, src, dst)
            if dst.startswith(("/ext/", "/int/")):
                with self._op_lock:
                    self._upload_file_reliable(dst, converted)
            else:
                dst = self._write_local_file(dst, converted)
            return {"ok": True, "src": src, "dst": dst, "bytes": len(converted)}

        if op == "nfc_make":
            payload = str(req["payload"])
            rec_type = str(req.get("record_type", "auto"))
            uid = str(req["uid"]) if req.get("uid") else None
            nfc_txt = build_ndef_nfc_tag(payload, record_type=rec_type, uid=uid)
            local = str(req.get("local", ""))
            remote = str(req.get("remote", ""))
            open_app = bool(req.get("open_on_flipper", False))
            if open_app and not remote:
                remote = "/ext/nfc/z2z_ndef.nfc"
            if local:
                local = self._write_local_file(local, nfc_txt)
            opened_app = ""
            if remote:
                with self._op_lock:
                    self._upload_file_reliable(remote, nfc_txt.encode("utf-8"))
                    if open_app:
                        self._fap_duplex_ready = False
                        opened_app = self.rpc.open_file_in_flipper_app(remote)
            return {
                "ok": True,
                "payload": payload,
                "local": local,
                "remote": remote,
                "bytes": len(nfc_txt),
                "opened_app": opened_app,
            }

        if op == "qr_encode":
            payload = str(req.get("text", "")).strip()
            local = str(req.get("local", "")).strip()
            remote = str(req.get("remote", "")).strip()
            if not payload:
                if local:
                    raw = Path(local).expanduser().read_bytes()
                    src_name = local
                elif remote:
                    with self._op_lock:
                        self._ensure_connected()
                        raw = self.rpc.storage_read(remote)
                    src_name = remote
                else:
                    raise ValueError("Supply `text`, `local`, or `remote` for qr_encode")
                if src_name.lower().endswith(".nfc") or b"Flipper NFC device" in raw[:128]:
                    nfc_rep = parse_nfc_capture(raw, use_proxmark3=False)
                    payload = str(nfc_rep.get("qr_payload") or "")
                else:
                    payload = raw.decode("utf-8", errors="replace").strip()[:256]
            if not payload:
                raise ValueError("No NDEF URI/Text or UID found to encode as QR")
            qr_res = encode_qr_with_qrencode(payload)
            if not qr_res.get("ok"):
                return {"ok": False, "error": str(qr_res.get("error", "qrencode failed"))}
            return qr_res

        if op == "cam_scan":
            image_path = str(req.get("image", "")).strip()
            use_latest = bool(req.get("latest_photo", False))
            target = str(req.get("target", "nfc")).strip().lower()
            local = str(req.get("local", "")).strip()
            remote = str(req.get("remote", "")).strip()
            open_app = bool(req.get("open_on_flipper", False))
            push_flag = bool(req.get("push", False)) or open_app or bool(remote)

            scan_res = scan_optical_code(image_path=image_path, use_latest_photo=use_latest)
            if not scan_res.get("matched"):
                return {
                    "ok": False,
                    "matched": False,
                    "source": scan_res.get("source", ""),
                    "error": "No QR code or 1D barcode detected in camera frame",
                }

            codes = list(scan_res["codes"])  # type: ignore[arg-type]
            first = codes[0]
            sym = str(first["symbology"])
            payload_val = str(first["payload"])

            eff_target = target
            if eff_target == "auto":
                up_pay = payload_val.strip().upper()
                is_rfid_pfx = up_pay.startswith(("H10301:", "EM4100:", "FDX-B:", "FDXB:"))
                is_1d_badge = sym.upper() != "QR-CODE" and (
                    (payload_val.isdigit() and len(payload_val) <= 12)
                    or bool(re.match(r"^\d{1,3}:\d{1,5}$", payload_val.strip()))
                )
                eff_target = "rfid" if (is_rfid_pfx or is_1d_badge) else "nfc"

            tag_txt = ""
            opened_app = ""
            if eff_target == "rfid":
                _kt, _dh, tag_txt = build_rfid_key(payload_val, key_type="auto")
                if push_flag and not remote:
                    remote = "/ext/lfrfid/z2z_cam.rfid"
            elif eff_target in ("nfc", "ndef"):
                eff_target = "nfc"
                tag_txt = build_ndef_nfc_tag(payload_val, record_type="auto")
                if push_flag and not remote:
                    remote = "/ext/nfc/z2z_cam.nfc"
            elif eff_target != "none":
                raise ValueError(f"Unsupported cam_scan target: {target!r}")

            if tag_txt and local:
                local = self._write_local_file(local, tag_txt)
            if tag_txt and remote:
                with self._op_lock:
                    self._upload_file_reliable(remote, tag_txt.encode("utf-8"))
                    if open_app:
                        self._fap_duplex_ready = False
                        opened_app = self.rpc.open_file_in_flipper_app(remote)

            self._log_event("cam", f"{sym}: {payload_val[:42]}")
            return {
                "ok": True,
                "matched": True,
                "source": scan_res.get("source", ""),
                "symbology": sym,
                "payload": payload_val,
                "codes": codes,
                "target": eff_target,
                "local": local,
                "remote": remote,
                "bytes": len(tag_txt),
                "opened_app": opened_app,
            }

        if op == "rfid_make":
            spec = str(req["spec"])
            key_type = str(req.get("key_type", "auto"))
            ktype, data_hex, rfid_txt = build_rfid_key(spec, key_type=key_type)
            local = str(req.get("local", "")).strip()
            remote = str(req.get("remote", "")).strip()
            open_app = bool(req.get("open_on_flipper", False))
            if open_app and not remote:
                remote = "/ext/lfrfid/z2z_badge.rfid"
            if local:
                local = self._write_local_file(local, rfid_txt)
            opened_app = ""
            if remote:
                with self._op_lock:
                    self._upload_file_reliable(remote, rfid_txt.encode("utf-8"))
                    if open_app:
                        self._fap_duplex_ready = False
                        opened_app = self.rpc.open_file_in_flipper_app(remote)
            self._log_event("kali", f"rfid {ktype} ({data_hex})")
            return {
                "ok": True,
                "key_type": ktype,
                "data_hex": data_hex,
                "local": local,
                "remote": remote,
                "bytes": len(rfid_txt),
                "opened_app": opened_app,
            }

        if op == "ibtn_make":
            spec = str(req["spec"])
            key_type = str(req.get("key_type", "auto"))
            ktype, data_hex, ibtn_txt = build_ibutton_key(spec, key_type=key_type)
            local = str(req.get("local", "")).strip()
            remote = str(req.get("remote", "")).strip()
            open_app = bool(req.get("open_on_flipper", False))
            if open_app and not remote:
                remote = "/ext/ibutton/z2z_key.ibtn"
            if local:
                local = self._write_local_file(local, ibtn_txt)
            opened_app = ""
            if remote:
                with self._op_lock:
                    self._upload_file_reliable(remote, ibtn_txt.encode("utf-8"))
                    if open_app:
                        self._fap_duplex_ready = False
                        opened_app = self.rpc.open_file_in_flipper_app(remote)
            self._log_event("kali", f"ibtn {ktype} ({data_hex})")
            return {
                "ok": True,
                "key_type": ktype,
                "data_hex": data_hex,
                "local": local,
                "remote": remote,
                "bytes": len(ibtn_txt),
                "opened_app": opened_app,
            }

        if op == "capture_diff":
            src_a = str(req["src_a"]).strip()
            src_b = str(req["src_b"]).strip()
            bytes_a = self._read_local_or_remote(src_a)
            bytes_b = self._read_local_or_remote(src_b)
            diff_res = diff_capture_bytes(src_a, bytes_a, src_b, bytes_b)
            return {
                "ok": True,
                "src_a": src_a,
                "src_b": src_b,
                **diff_res,
            }

        if op == "ir_list":
            src = str(req.get("src", "")).strip()
            auto_select = bool(req.get("auto_select", False))

            def _list_remote_ir_locked() -> List[str]:
                found: List[str] = []
                for ir_dir in ("/ext/infrared", "/ext/infrared/assets"):
                    try:
                        entries = self.rpc.storage_list(ir_dir)
                    except FlipperRpcError as exc:
                        if exc.status_code == CommandStatus.ERROR_STORAGE_NOT_EXIST:
                            continue
                        raise
                    dir_files = sorted(
                        f"{ir_dir}/{e.name}"
                        for e in entries
                        if not e.is_dir and e.name.lower().endswith(".ir")
                    )
                    if dir_files:
                        found.extend(dir_files)
                        if ir_dir == "/ext/infrared":
                            break
                return found

            if not src and not auto_select:
                with self._op_lock:
                    self._ensure_connected()
                    files = _list_remote_ir_locked()
                return {"ok": True, "files": files, "count": len(files)}

            remote_files: List[str] = []
            if not src or src.startswith(("/ext/", "/int/")):
                with self._op_lock:
                    self._ensure_connected()
                    remote_files = _list_remote_ir_locked()
                    if not src:
                        src = remote_files[0] if remote_files else "/ext/infrared/z2z_learned.ir"
                    st_ir = self.rpc.storage_stat(src)
                    if st_ir is None or st_ir.size == 0:
                        raw = b""
                    else:
                        raw = self.rpc.storage_read(src)
                buttons = parse_ir_remote_full(raw) if raw else []
                self._ir_remote_cache[src] = buttons
                deck_remotes = list(remote_files)
                learned_default = "/ext/infrared/z2z_learned.ir"
                if src not in deck_remotes:
                    deck_remotes.append(src)
                if learned_default not in deck_remotes:
                    deck_remotes.append(learned_default)
                return {
                    "ok": True,
                    "src": src,
                    "count": len(buttons),
                    "buttons": buttons,
                    "remotes": deck_remotes,
                    "remotes_csv": "|".join(deck_remotes[:24]),
                }

            raw = self._read_local_or_remote(src)
            buttons = parse_ir_remote_full(raw)
            return {
                "ok": True,
                "src": src,
                "count": len(buttons),
                "buttons": buttons,
            }

        if op == "ir_tx":
            src = str(req.get("src", "")).strip()
            btn_q = str(req.get("button", "")).strip()
            repeat = max(1, min(8, int(req.get("repeat", 2) or 2)))

            self._ir_tx_seq += 1
            seq = self._ir_tx_seq
            tx_frame: Dict[str, object] = {
                "id": seq,
                "seq": seq,
                "op": "ir_tx",
                "quiet": bool(self.rpc.is_streaming_screen),
            }

            if src:
                buttons = self._ir_remote_cache.get(src)
                if buttons is None:
                    raw_ir = self._read_local_or_remote(src)
                    buttons = parse_ir_remote_full(raw_ir)
                    if src.startswith(("/ext/", "/int/")):
                        self._ir_remote_cache[src] = buttons
                if not buttons:
                    raise ValueError(f"No IR buttons found in {src}")

                chosen = buttons[0]
                if btn_q:
                    ql = btn_q.lower()
                    exact = next((b for b in buttons if str(b["name"]).lower() == ql), None)
                    sub = next((b for b in buttons if ql in str(b["name"]).lower()), None)
                    if exact is not None:
                        chosen = exact
                    elif sub is not None:
                        chosen = sub
                    elif btn_q.isdigit() and int(btn_q) < len(buttons):
                        chosen = buttons[int(btn_q)]
                    else:
                        raise KeyError(f"Button {btn_q!r} not found in {src}")

                btn_name = str(chosen["name"])
                summary = str(chosen.get("summary", btn_name))
                tx_frame["name"] = btn_name
                if chosen["type"] == "parsed":
                    tx_frame["proto"] = str(chosen["protocol"])
                    tx_frame["addr"] = int(chosen["address_u32"])
                    tx_frame["cmd"] = int(chosen["command_u32"])
                    tx_frame["repeat"] = repeat
                else:
                    tx_frame["freq"] = int(chosen.get("frequency", 38000))
                    duty_f = float(chosen.get("duty_cycle", 0.33))
                    tx_frame["duty"] = max(5, min(90, int(round(duty_f * 100.0))))
                    t_list = list(chosen.get("timings", []))  # type: ignore[arg-type]
                    tx_frame["raw"] = " ".join(str(x) for x in t_list[:255])
            else:
                btn_name = str(req.get("name") or "AdHoc")
                tx_frame["name"] = btn_name
                if req.get("proto"):
                    proto = str(req["proto"])
                    addr_v = int(str(req.get("addr", "0")), 0)
                    cmd_v = int(str(req.get("cmd", "0")), 0)
                    tx_frame["proto"] = proto
                    tx_frame["addr"] = addr_v
                    tx_frame["cmd"] = cmd_v
                    tx_frame["repeat"] = repeat
                    summary = f"{proto} A:0x{addr_v:X} C:0x{cmd_v:X}"
                elif req.get("raw"):
                    raw_in = req["raw"]
                    if isinstance(raw_in, list):
                        t_str = " ".join(str(int(x)) for x in raw_in[:255] if int(x) > 0)
                    else:
                        t_str = " ".join(
                            tok for tok in str(raw_in).replace(",", " ").split() if tok.isdigit()
                        )
                    freq_v = int(req.get("freq", 38000) or 38000)
                    duty_v = int(req.get("duty", 33) or 33)
                    tx_frame["freq"] = freq_v
                    tx_frame["duty"] = duty_v
                    tx_frame["raw"] = t_str
                    summary = f"RAW @{freq_v // 1000}kHz"
                else:
                    raise ValueError("Supply `src` (+ `button`) or `proto`/`raw` for ir_tx")

            auto_launched = self._send_fap_frame_reliable(tx_frame)
            self._log_event("ir_tx", f"{btn_name} ({summary})")
            return {
                "ok": True,
                "button": btn_name,
                "summary": summary,
                "auto_launched_fap": auto_launched,
            }

        if op == "ir_save":
            btn_name = str(req.get("name") or "Learned").strip() or "Learned"
            dst = str(req.get("dst") or "/ext/infrared/z2z_learned.ir").strip()
            sig_in = req.get("signal")
            sig = dict(sig_in) if isinstance(sig_in, dict) else dict(self._last_ir_rx)
            if not sig:
                if bool(req.get("arm_if_empty", False)):
                    self._ir_tx_seq += 1
                    self._send_fap_frame_reliable(
                        {"id": self._ir_tx_seq, "seq": self._ir_tx_seq, "op": "ir_rx_start"}
                    )
                    return {
                        "ok": True,
                        "armed_rx": True,
                        "dst": dst,
                        "name": btn_name,
                    }
                raise RuntimeError(
                    "No learned IR signal cached yet (open `IR Blaster & Learn` on Flipper first)"
                )
            sig["name"] = btn_name

            existing = ""
            if dst.startswith(("/ext/", "/int/")):
                with self._op_lock:
                    self._ensure_connected()
                    st = self.rpc.storage_stat(dst)
                    if st is not None and st.size > 0:
                        existing = self.rpc.storage_read(dst).decode("utf-8", errors="replace")
            else:
                dp = Path(dst).expanduser()
                if dp.is_file():
                    existing = dp.read_text(encoding="utf-8", errors="replace")

            updated = append_ir_signal_to_remote(existing, sig)
            saved_buttons = parse_ir_remote_full(updated.encode("utf-8"))
            if dst.startswith(("/ext/", "/int/")):
                with self._op_lock:
                    self._upload_file_reliable(dst, updated.encode("utf-8"))
                self._ir_remote_cache[dst] = saved_buttons
            else:
                dst = self._write_local_file(dst, updated)

            self._log_event("kali", f"ir_save {btn_name} -> {os.path.basename(dst)}")
            return {
                "ok": True,
                "dst": dst,
                "name": btn_name,
                "summary": str(sig.get("summary", "")),
                "bytes": len(updated),
                "count": len(saved_buttons),
                "buttons": saved_buttons,
            }

        if op == "ir_convert":
            src = str(req["src"]).strip()
            dst = str(req["dst"]).strip()
            button = str(req.get("button", "")).strip()
            src_bytes = self._read_local_or_remote(src)

            if dst.lower().endswith(".ir") and b"IR signals file" not in src_bytes[:64]:
                out_txt = irctl_to_ir(src_bytes, button_name=button or Path(dst).stem)
            else:
                out_txt = ir_to_irctl(src_bytes, button=button)

            if dst.startswith(("/ext/", "/int/")):
                with self._op_lock:
                    self._upload_file_reliable(dst, out_txt.encode("utf-8"))
                self._ir_remote_cache.pop(dst, None)
            else:
                dst = self._write_local_file(dst, out_txt)

            return {
                "ok": True,
                "src": src,
                "dst": dst,
                "bytes": len(out_txt),
                "preview": out_txt[:400],
            }

        raise ValueError(f"Unknown IPC op: {op!r}")

    def _serve_client(self, conn: socket.socket) -> None:
        self._active_clients += 1
        _CLIENT_CRED.uid = 0
        _CLIENT_CRED.gid = 0
        if hasattr(socket, "SO_PEERCRED"):
            try:
                cred = conn.getsockopt(
                    socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
                )
                _pid, _CLIENT_CRED.uid, _CLIENT_CRED.gid = struct.unpack("3i", cred)
            except OSError:
                pass
        rx_buf = bytearray()
        wlock = threading.Lock()

        push_lock = threading.Lock()
        push_ev = threading.Event()
        push_closed = False
        subscribed = False
        pending_frame: Optional[Tuple[bytes, int, int, bool]] = None
        pending_state: Optional[Tuple[int, bool, bool, str]] = None
        last_pushed_seq = -1
        push_thr: Optional[threading.Thread] = None

        def _send_line(payload: Dict[str, object]) -> None:
            raw = (json.dumps(payload) + "\n").encode("utf-8")
            with wlock:
                conn.sendall(raw)

        def _on_sub_frame(frame: bytes, orient: int, seq: int, streaming: bool) -> None:
            nonlocal pending_frame
            with push_lock:
                if push_closed:
                    return
                pending_frame = (frame, orient, seq, streaming)
            push_ev.set()

        def _on_sub_state(depth: int, connected: bool, streaming: bool, err: str) -> None:
            nonlocal pending_state
            with push_lock:
                if push_closed:
                    return
                pending_state = (depth, connected, streaming, err)
            push_ev.set()

        def _push_loop() -> None:
            nonlocal pending_frame, pending_state, last_pushed_seq, push_closed
            while not self._stop_event.is_set():
                push_ev.wait(timeout=0.5)
                push_ev.clear()
                with push_lock:
                    if push_closed:
                        break
                    fr = pending_frame
                    st_ev = pending_state
                    pending_frame = None
                    pending_state = None
                try:
                    if fr is not None:
                        frame, orient, seq, streaming = fr
                        if (not streaming) or (len(frame) >= 1024 and seq != last_pushed_seq):
                            last_pushed_seq = seq
                            _send_line(
                                {
                                    "event": "screen_frame",
                                    "seq": seq,
                                    "orientation": orient,
                                    "streaming": streaming,
                                    "connected": bool(self.transport.is_connected),
                                    "queue_depth": self.gui_input_queue_depth,
                                    "frame_hex": frame.hex() if len(frame) >= 1024 else "",
                                }
                            )
                    if st_ev is not None:
                        depth, connected, streaming, err = st_ev
                        _send_line(
                            {
                                "event": "gui_input_state",
                                "queue_depth": depth,
                                "connected": connected,
                                "streaming": streaming,
                                "error": err,
                            }
                        )
                except Exception:
                    with push_lock:
                        push_closed = True
                    break

        def _enable_subscription(initial_seq: int) -> None:
            nonlocal subscribed, push_closed, last_pushed_seq, push_thr
            if subscribed:
                with push_lock:
                    last_pushed_seq = initial_seq
                return
            with push_lock:
                push_closed = False
                last_pushed_seq = initial_seq
            self.rpc.add_screen_listener(_on_sub_frame)
            self.add_gui_state_listener(_on_sub_state)
            subscribed = True
            push_thr = threading.Thread(
                target=_push_loop, name="z2z-ipc-push", daemon=True
            )
            push_thr.start()

        def _disable_subscription() -> None:
            nonlocal subscribed, push_closed, push_thr
            if not subscribed:
                return
            subscribed = False
            self.rpc.remove_screen_listener(_on_sub_frame)
            self.remove_gui_state_listener(_on_sub_state)
            with push_lock:
                push_closed = True
            push_ev.set()
            thr = push_thr
            push_thr = None
            if thr is not None and thr is not threading.current_thread():
                thr.join(timeout=1.0)

        def _recv_line(cancel_ev: Optional[threading.Event] = None) -> Optional[str]:
            while not self._stop_event.is_set():
                nl = rx_buf.find(b"\n")
                if nl >= 0:
                    line_bytes = bytes(rx_buf[:nl])
                    del rx_buf[: nl + 1]
                    return line_bytes.decode("utf-8", errors="replace")
                if cancel_ev is not None and cancel_ev.is_set():
                    raise RuntimeError("Pairing cancelled by remote BLE peer")
                rdy, _, _ = select.select([conn], [], [], 0.15)
                if not rdy:
                    continue
                chunk = conn.recv(4096)
                if not chunk:
                    if rx_buf:
                        rem = bytes(rx_buf)
                        rx_buf.clear()
                        return rem.decode("utf-8", errors="replace")
                    return None
                rx_buf.extend(chunk)
            return None

        try:
            while not self._stop_event.is_set():
                line = _recv_line()
                if line is None:
                    break
                s = line.strip()
                if not s:
                    continue

                pin_cancel_ev = threading.Event()

                def _on_pin_cancel() -> None:
                    pin_cancel_ev.set()
                    try:
                        _send_line({"event": "pin_cancelled"})
                    except Exception:
                        pass

                def _request_pin_over_ipc(name: str, address: str) -> str:
                    pin_cancel_ev.clear()
                    _send_line({"event": "pin_required", "name": name, "address": address})
                    reply_line = _recv_line(cancel_ev=pin_cancel_ev)
                    if not reply_line:
                        raise RuntimeError("Client disconnected during PIN prompt")
                    obj = json.loads(reply_line)
                    return str(obj.get("pin", ""))

                req: Optional[Dict[str, object]] = None
                try:
                    req = json.loads(s)
                    resp = self.handle_ipc_request(
                        req,
                        pin_requester=_request_pin_over_ipc,
                        pin_cancel_cb=_on_pin_cancel,
                    )
                except Exception as exc:
                    pin_cancel_ev.set()
                    cleaned = clean_dbus_error(exc)
                    self._log_event("err", cleaned[:80])
                    print(
                        f"[z2zd] request error: {cleaned} (raw: {exc})",
                        file=sys.stderr,
                        flush=True,
                    )
                    resp = {"ok": False, "error": cleaned}
                finally:
                    pin_cancel_ev.set()

                if isinstance(req, dict) and req.get("op") in ("screen_stop", "disconnect"):
                    _disable_subscription()

                _send_line(resp)

                if (
                    isinstance(req, dict)
                    and req.get("op") == "screen_start"
                    and bool(req.get("subscribe", False))
                    and bool(resp.get("ok", False))
                ):
                    _enable_subscription(int(resp.get("seq", -1)))
        except Exception:
            pass
        finally:
            _disable_subscription()
            self._active_clients -= 1
            try:
                conn.close()
            except Exception:
                pass

    def serve_forever(self) -> None:
        # Check for systemd socket activation (`LISTEN_FDS=1` on fd 3)
        listen_fds = int(os.environ.get("LISTEN_FDS", "0") or "0")
        listen_pid = int(os.environ.get("LISTEN_PID", "0") or "0")
        inherited_socket = listen_fds >= 1 and listen_pid == os.getpid()

        if inherited_socket:
            srv = socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
        else:
            sock_dir = os.path.dirname(self.socket_path)
            if sock_dir:
                os.makedirs(sock_dir, mode=0o755, exist_ok=True)
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
            srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            srv.bind(self.socket_path)
            os.chmod(self.socket_path, 0o660 if os.geteuid() == 0 else 0o600)
            srv.listen(8)

        srv.settimeout(1.0)
        if hasattr(self, "_ready_event"):
            self._ready_event.set()
        watch_thread = threading.Thread(target=self._watch_worker, name="z2z-watch", daemon=True)
        watch_thread.start()

        try:
            while not self._stop_event.is_set():
                try:
                    conn, _ = srv.accept()
                    threading.Thread(
                        target=self._serve_client, args=(conn,), daemon=True
                    ).start()
                except socket.timeout:
                    if (
                        not self._watch_enabled
                        and not self.transport.is_connected
                        and self._active_clients <= 0
                        and self.idle_timeout_sec > 0
                        and (time.monotonic() - self._last_activity) > self.idle_timeout_sec
                    ):
                        break
        finally:
            self._stop_event.set()
            try:
                self.transport.disconnect()
            except Exception:
                pass
            try:
                srv.close()
            except Exception:
                pass
            if not inherited_socket and os.path.exists(self.socket_path):
                try:
                    os.unlink(self.socket_path)
                except Exception:
                    pass


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="zero2zero BLE RPC & Offload Daemon")
    ap.add_argument("--socket", default=resolve_socket_path(), help="Unix domain socket path")
    ap.add_argument(
        "--idle-timeout",
        type=float,
        default=300.0,
        help="Auto-exit after N idle seconds when watch mode is off (0 = never)",
    )
    ap.add_argument(
        "--watch",
        action="store_true",
        help="Enable continuous Pocket Watch polling for Flipper mailbox triggers",
    )
    args = ap.parse_args(argv)

    daemon = Zero2ZeroDaemon(
        socket_path=args.socket,
        idle_timeout_sec=args.idle_timeout,
        watch_enabled=args.watch,
    )

    def _handle_sig(_signum, _frame):
        daemon._stop_event.set()

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)
    daemon.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
