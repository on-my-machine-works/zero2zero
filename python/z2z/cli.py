"""Command-line interface (`z2z`) for CardputerZero x Flipper Zero BLE companion."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from typing import Dict, List, Optional

from . import __version__
from .daemon import DEFAULT_FAP_PATH, find_bundled_fap, resolve_socket_path
from .studio import BUILTIN_BADUSB_TEMPLATES


def _connect_or_autospawn(sock_path: str, timeout_sec: float = 3.0) -> socket.socket:
    def _try_conn() -> Optional[socket.socket]:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            s.connect(sock_path)
            return s
        except OSError:
            s.close()
            return None

    conn = _try_conn()
    if conn is not None:
        return conn

    # Auto-spawn on-demand `z2zd` background daemon
    py_root = str(Path(__file__).resolve().parents[1])
    env = os.environ.copy()
    env["PYTHONPATH"] = py_root + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    subprocess.Popen(
        [sys.executable, "-m", "z2z.daemon", "--socket", sock_path],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        conn = _try_conn()
        if conn is not None:
            return conn
        time.sleep(0.08)
    raise RuntimeError(f"Unable to connect to or start z2zd on {sock_path}")


def ipc_call(req: Dict[str, object], sock_path: Optional[str] = None) -> Dict[str, object]:
    sp = sock_path or resolve_socket_path()
    with _connect_or_autospawn(sp) as conn:
        rf = conn.makefile("r", encoding="utf-8", errors="replace")
        wf = conn.makefile("w", encoding="utf-8")
        wf.write(json.dumps(req) + "\n")
        wf.flush()

        while True:
            line = rf.readline()
            if not line:
                raise RuntimeError("z2zd closed connection unexpectedly")
            msg = json.loads(line)
            if msg.get("event") == "pin_required":
                prompt = (
                    f"Enter 6-digit passkey shown on {msg.get('name', 'Flipper')} "
                    f"({msg.get('address', '')}): "
                )
                pin = input(prompt).strip()
                wf.write(json.dumps({"pin": pin}) + "\n")
                wf.flush()
                continue
            if msg.get("event") == "pin_cancelled":
                continue
            return msg


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="z2z",
        description="zero2zero — BLE Offload Companion for Kali on CardputerZero + Flipper Zero",
    )
    ap.add_argument("-V", "--version", action="version", version=f"z2z {__version__}")
    ap.add_argument("--json", action="store_true", help="Emit raw JSON response")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="Scan for nearby Flipper Zero BLE advertisers")
    p_scan.add_argument("-t", "--timeout", type=float, default=4.0)

    p_pair = sub.add_parser("pair", help="Bond (and connect) with a Flipper Zero via 6-digit passkey")
    p_pair.add_argument("target", nargs="?", default="")
    p_pair.add_argument("--pin", default="", help="Optional preset 6-digit passkey")
    p_pair.add_argument("-f", "--force", action="store_true", help="Remove existing BlueZ bond first")

    p_unpair = sub.add_parser("unpair", help="Forget/remove a paired Flipper Zero from BlueZ")
    p_unpair.add_argument("target", nargs="?", default="")

    p_conn = sub.add_parser("connect", help="Open encrypted BLE RPC link to paired Flipper")
    p_conn.add_argument("target", nargs="?", default="")

    sub.add_parser("disconnect", help="Disconnect active Flipper BLE session")
    sub.add_parser("status", help="Show BLE link, Flipper battery/SD, and Kali SoC status")

    p_watch = sub.add_parser("watch", help="Toggle Pocket Watch mailbox polling (`on`/`off`)")
    p_watch.add_argument("mode", choices=["on", "off"])

    p_alert = sub.add_parser(
        "alert", help="Get or set Flipper companion alert mode (`vibro`, `soft`, `led`, `off`, `loud`)"
    )
    p_alert.add_argument("mode", nargs="?", choices=["vibro", "soft", "led", "off", "loud"], default="")

    sub.add_parser("clock-sync", help="Sync Kali's system clock to Flipper Zero's hardware RTC")

    p_scr = sub.add_parser("screen", help="Render Flipper's 128x64 screen in UTF-8 half-blocks")
    p_scr.add_argument("-w", "--watch", action="store_true", help="Stream live frames until Ctrl-C")
    p_scr.add_argument("-i", "--interval", type=float, default=0.22, help="Refresh interval in seconds")
    p_scr.add_argument("--stop", action="store_true", help="Stop active BLE screen stream")

    p_key = sub.add_parser("key", help="Send a remote button press to Flipper over BLE")
    p_key.add_argument("button", choices=["up", "down", "left", "right", "ok", "back"])
    p_key.add_argument("-l", "--long", action="store_true", help="Send a long press instead of short")

    p_ls = sub.add_parser("ls", help="List directory on Flipper SD (`/ext/...`)")
    p_ls.add_argument("path", nargs="?", default="/ext")
    p_ls.add_argument("--md5", action="store_true")

    p_pull = sub.add_parser("pull", help="Download a file from Flipper over BLE")
    p_pull.add_argument("remote")
    p_pull.add_argument("local")

    p_push = sub.add_parser("push", help="Upload a file to Flipper over BLE")
    p_push.add_argument("local")
    p_push.add_argument("remote")
    p_push.add_argument("--template", action="store_true", help="Expand {{HOST_IP}} variables")
    p_push.add_argument("--load", action="store_true", help="Open uploaded file on Flipper")

    sub.add_parser("jobs", help="List configured offload jobs (`/etc/zero2zero/jobs.d/`)")

    p_run = sub.add_parser("run", help="Execute a declarative offload job by ID")
    p_run.add_argument("job_id")

    p_caps = sub.add_parser("captures", help="List recent captures across Sub-GHz, NFC, RFID, IR, iButton")
    p_caps.add_argument("-k", "--kind", default="", help="Filter by category (`subghz`, `nfc`, `lfrfid`, `infrared`, `ibutton`)")
    p_caps.add_argument("-n", "--limit", type=int, default=20)

    p_insp = sub.add_parser("inspect", help="Inspect a `.sub`, `.nfc`, `.rfid`, `.ir`, or `.ibtn` capture")
    p_insp.add_argument("path", nargs="?", default="", help="Local or `/ext/...` path (omit for newest)")
    p_insp.add_argument("-k", "--kind", default="", help="Category hint when inspecting newest")

    p_open = sub.add_parser("open", help="Open a capture or BadUSB script in Flipper's native app")
    p_open.add_argument("remote", help="Path on Flipper SD (e.g., `/ext/nfc/tag.nfc`)")

    p_bad = sub.add_parser("badusb", help="Compose/expand DuckyScript or typed text and push to `/ext/badusb/`")
    p_bad.add_argument("name", nargs="?", default="z2z_payload", help="Script name on Flipper")
    p_bad.add_argument("-t", "--text", default="", help="Inline text or DuckyScript lines")
    p_bad.add_argument("-f", "--file", default="", help="Read input from local file")
    p_bad.add_argument("--template-id", default="", help="Use a built-in starter template ID")
    p_bad.add_argument("--list-templates", action="store_true", help="List built-in BadUSB templates")
    p_bad.add_argument("--raw", action="store_true", help="Treat input as raw DuckyScript commands")
    p_bad.add_argument("--no-expand", action="store_true", help="Disable {{HOST_IP}} variable expansion")
    p_bad.add_argument("--open", action="store_true", help="Open script immediately in Flipper's Bad USB app")

    p_subi = sub.add_parser("sub-inspect", help="Analyze a local or remote `.sub` capture")
    p_subi.add_argument("path", nargs="?", default="")

    p_subd = sub.add_parser(
        "sub-decode",
        help="Convert `.sub` to `.ook` and decode protocols via Kali `rtl_433`",
    )
    p_subd.add_argument("path", nargs="?", default="", help="Local or `/ext/subghz/...` path (omit for newest)")
    p_subd.add_argument("--ook-out", default="", help="Optional local path to save converted `.ook` file")

    p_subs = sub.add_parser("sub-synth", help="Synthesize a RAW `.sub` waveform from bits/hex")
    p_subs.add_argument("bits", help="Bitstring (`101100...`) or hex (`0xA5C3F0`)")
    p_subs.add_argument("-f", "--frequency", type=int, default=433920000)
    p_subs.add_argument("--te", type=int, default=350, help="Base short pulse width in us")
    p_subs.add_argument("--repeat", type=int, default=4)
    p_subs.add_argument("-o", "--local", default="")
    p_subs.add_argument("-r", "--remote", default="")

    p_nfcc = sub.add_parser(
        "nfc-convert",
        help="Convert between Flipper `.nfc` and `libnfc`/`proxmark3` `.mfd`/`.bin`/`.eml` dumps",
    )
    p_nfcc.add_argument("src", help="Source path (local file or `/ext/nfc/...`)")
    p_nfcc.add_argument("dst", nargs="?", default="", help="Destination path (local file or `/ext/nfc/...`)")
    p_nfcc.add_argument("-o", "--output", dest="dst_opt", default="", help="Destination path alias")

    p_nfcm = sub.add_parser(
        "nfc-make",
        help="Synthesize an NDEF NTAG215 `.nfc` tag from a URL or text string",
    )
    p_nfcm.add_argument("payload", help="URL (`https://...`) or text payload")
    p_nfcm.add_argument("--type", choices=["auto", "uri", "text"], default="auto")
    p_nfcm.add_argument("--uid", default="", help="Optional 7-byte hex UID")
    p_nfcm.add_argument("-o", "--local", default="", help="Save `.nfc` locally")
    p_nfcm.add_argument("-r", "--remote", default="", help="Upload `.nfc` to Flipper SD")
    p_nfcm.add_argument("--push", action="store_true", help="Upload to `/ext/nfc/z2z_ndef.nfc` if `-r` omitted")
    p_nfcm.add_argument("--open", action="store_true", help="Open tag immediately in Flipper NFC app")

    p_qr = sub.add_parser(
        "qr",
        help="Render a scannable QR code via Kali `qrencode` from text or a Flipper `.nfc` file",
    )
    p_qr.add_argument("source", help="Text/URL, local `.nfc` file, or `/ext/nfc/...` path")

    p_cam = sub.add_parser(
        "cam-scan",
        help="Scan a QR or 1D barcode via CardputerZero IMX219 (`zbarimg`) and synthesize Flipper NFC/RFID",
    )
    p_cam.add_argument("-i", "--image", default="", help="Optional image path instead of live IMX219 capture")
    p_cam.add_argument("--latest-photo", action="store_true", help="Scan newest photo in ~/Pictures or /tmp")
    p_cam.add_argument("--target", choices=["auto", "nfc", "rfid", "none"], default="auto")
    p_cam.add_argument("-o", "--local", default="", help="Save synthesized `.nfc`/`.rfid` locally")
    p_cam.add_argument("-r", "--remote", default="", help="Upload synthesized tag to Flipper SD")
    p_cam.add_argument("--push", action="store_true", help="Upload to `/ext/nfc/z2z_cam.nfc` (or `/ext/lfrfid/z2z_cam.rfid`)")
    p_cam.add_argument("--open", action="store_true", help="Upload and open immediately on Flipper")

    p_rfid = sub.add_parser(
        "rfid-make",
        help="Synthesize a 125 kHz `.rfid` badge (`H10301` FC:Card, `EM4100`, or `FDX-B`)",
    )
    p_rfid.add_argument("spec", help="Badge spec, e.g. `123:45678` (H10301) or `18 00 0D C7 EB` (EM4100)")
    p_rfid.add_argument("--type", default="auto", help="Key type (`auto`, `H10301`, `EM4100`, `FDX-B`)")
    p_rfid.add_argument("-o", "--local", default="", help="Save `.rfid` locally")
    p_rfid.add_argument("-r", "--remote", default="", help="Upload `.rfid` to Flipper SD")
    p_rfid.add_argument("--push", action="store_true", help="Upload to `/ext/lfrfid/z2z_badge.rfid` if `-r` omitted")
    p_rfid.add_argument("--open", action="store_true", help="Open badge immediately in Flipper `125 kHz RFID` app")

    p_ibtn = sub.add_parser(
        "ibtn-make",
        help="Synthesize a 1-Wire `.ibtn` key (`Dallas` DS1990A with auto CRC-8, `Cyfral`, `Metakom`)",
    )
    p_ibtn.add_argument("spec", help="Hex key ID, e.g. `11 22 33 44 55 66` (auto DS1990A family + CRC-8)")
    p_ibtn.add_argument("--type", default="auto", help="Key type (`auto`, `Dallas`, `Cyfral`, `Metakom`)")
    p_ibtn.add_argument("-o", "--local", default="", help="Save `.ibtn` locally")
    p_ibtn.add_argument("-r", "--remote", default="", help="Upload `.ibtn` to Flipper SD")
    p_ibtn.add_argument("--push", action="store_true", help="Upload to `/ext/ibutton/z2z_key.ibtn` if `-r` omitted")
    p_ibtn.add_argument("--open", action="store_true", help="Open key immediately in Flipper `iButton` app")

    p_diff = sub.add_parser(
        "diff",
        help="Structural side-by-side diff of two Flipper captures (`.nfc` pages/blocks, `.ir` buttons, `.rfid`, `.sub`)",
    )
    p_diff.add_argument("src_a", help="First capture (local path or `/ext/...`)")
    p_diff.add_argument("src_b", help="Second capture (local path or `/ext/...`)")

    p_irl = sub.add_parser(
        "ir-list",
        help="List `/ext/infrared/*.ir` remotes or all buttons inside a `.ir` remote file",
    )
    p_irl.add_argument("src", nargs="?", default="", help="Local or `/ext/infrared/...` `.ir` path (omit to list remotes)")

    p_irtx = sub.add_parser(
        "ir-tx",
        help="Blast an IR button live over BLE out of Flipper's 3-LED array (`zero2zero.fap`)",
    )
    p_irtx.add_argument("src", nargs="?", default="", help="`.ir` file path (local or `/ext/infrared/...`)")
    p_irtx.add_argument("button", nargs="?", default="", help="Button name or index (e.g. `Power`, `Vol_up`, `0`)")
    p_irtx.add_argument("--proto", default="", help="Ad-hoc IR protocol (e.g. `NEC`, `NECext`, `Samsung32`, `SIRC`)")
    p_irtx.add_argument("--addr", default="0", help="Ad-hoc IR address (hex `0x87EE` or decimal)")
    p_irtx.add_argument("--cmd", "--command", dest="ir_cmd", default="0", help="Ad-hoc IR command (hex `0x985F` or decimal)")
    p_irtx.add_argument("--raw", default="", help="Ad-hoc raw timings in us (`9000 4500 560 ...`)")
    p_irtx.add_argument("--freq", type=int, default=38000, help="Carrier frequency in Hz for `--raw`")
    p_irtx.add_argument("--duty", type=int, default=33, help="Duty cycle %% for `--raw`")
    p_irtx.add_argument("--name", default="AdHoc", help="Display label on Flipper for ad-hoc TX")
    p_irtx.add_argument("--repeat", type=int, default=2, help="Transmit repeat count (1..8)")

    p_irs = sub.add_parser(
        "ir-save",
        help="Name and append the last IR signal learned on Flipper into a `.ir` remote file",
    )
    p_irs.add_argument("name", help="Button name to save (e.g. `Power`, `Mute`, `Input`)")
    p_irs.add_argument("-d", "--dst", default="/ext/infrared/z2z_learned.ir", help="Target `.ir` file (local or `/ext/infrared/...`)")

    p_irc = sub.add_parser(
        "ir-convert",
        help="Convert between Flipper `.ir` remotes and Linux `ir-ctl` (`v4l-utils`) raw pulse/space files",
    )
    p_irc.add_argument("src", help="Source `.ir` or `.irctl` path (local or `/ext/infrared/...`)")
    p_irc.add_argument("dst", nargs="?", default="", help="Destination `.irctl` or `.ir` path")
    p_irc.add_argument("-o", "--output", dest="dst_opt", default="", help="Destination path alias")
    p_irc.add_argument("-b", "--button", default="", help="Button name (or index) to export/import")

    sub.add_parser("net", help="Show compact Kali wireless/interface summary")
    sub.add_parser("sync", help="Incrementally vault new `/ext/` captures onto Kali")

    p_fap = sub.add_parser("fap-launch", help="Launch `zero2zero.fap` on Flipper in RPC duplex mode")
    p_fap.add_argument("--path", default=DEFAULT_FAP_PATH)

    p_fapi = sub.add_parser("fap-install", help="Upload compiled `zero2zero.fap` to Flipper SD")
    p_fapi.add_argument("local_fap", nargs="?", default="", help="Optional path to `.fap` (defaults to bundled binary)")
    p_fapi.add_argument("--dest", default=DEFAULT_FAP_PATH)

    args = ap.parse_args(argv)

    if args.cmd == "badusb" and args.list_templates:
        if args.json:
            print(json.dumps({"ok": True, "templates": BUILTIN_BADUSB_TEMPLATES}, indent=2))
        else:
            for t in BUILTIN_BADUSB_TEMPLATES:
                print(f"  {t['id']:<20} {t['title']}")
        return 0

    if args.cmd == "screen" and args.watch and not args.stop:
        try:
            resp = ipc_call({"op": "screen_start", "unicode": True, "wait": 1.2})
            if not resp.get("ok"):
                print(f"[!] {resp.get('error') or resp}", file=sys.stderr)
                return 1
            last_seq = -1
            while True:
                resp = ipc_call({"op": "screen_frame", "unicode": True, "auto_start": True})
                if not resp.get("ok"):
                    print(f"[!] {resp.get('error') or resp}", file=sys.stderr)
                    return 1
                seq = int(resp.get("seq", 0))
                if seq != last_seq and resp.get("unicode"):
                    last_seq = seq
                    sys.stdout.write("\033[H\033[2J")
                    sys.stdout.write(f"=== Flipper Zero Live Screen (frame #{seq}, Ctrl-C to stop) ===\n")
                    sys.stdout.write(str(resp["unicode"]) + "\n")
                    sys.stdout.flush()
                time.sleep(max(0.08, args.interval))
        except KeyboardInterrupt:
            pass
        finally:
            try:
                ipc_call({"op": "screen_stop"})
            except Exception:
                pass
        return 0

    def _abs_local(p: str) -> str:
        if not p or p.startswith(("/ext/", "/int/")):
            return p
        return os.path.abspath(os.path.expanduser(p))

    def _resolve_fap_arg() -> str:
        if args.local_fap:
            return _abs_local(args.local_fap)
        bundled = find_bundled_fap()
        if bundled is None:
            raise FileNotFoundError(
                "Bundled zero2zero.fap not found; build it with `make fap` or supply a path"
            )
        return str(bundled)

    def _build_badusb_req() -> Dict[str, object]:
        raw_ducky = bool(args.raw)
        content = args.text
        name = args.name
        if args.template_id:
            matched = next((t for t in BUILTIN_BADUSB_TEMPLATES if t["id"] == args.template_id), None)
            if matched is None:
                raise KeyError(f"Unknown BadUSB template id: {args.template_id!r}")
            content = matched["content"]
            raw_ducky = matched.get("raw_ducky") == "true" or raw_ducky
            if name == "z2z_payload":
                name = matched["id"]
        elif args.file:
            content = Path(args.file).expanduser().read_text(encoding="utf-8", errors="replace")
        if not content:
            raise ValueError("Provide --text, --file, or --template-id (or --list-templates)")
        return {
            "op": "badusb_push",
            "name": name,
            "content": content,
            "raw_ducky": raw_ducky,
            "template": not args.no_expand,
            "open_on_flipper": args.open,
        }

    req_map = {
        "scan": lambda: {"op": "scan", "timeout": args.timeout},
        "pair": lambda: {
            "op": "pair",
            "target": args.target,
            "pin": args.pin,
            "force": args.force,
        },
        "unpair": lambda: {"op": "unpair", "target": args.target},
        "connect": lambda: {"op": "connect", "target": args.target},
        "disconnect": lambda: {"op": "disconnect"},
        "status": lambda: {"op": "status", "query_rpc": True},
        "watch": lambda: {"op": "watch_set", "enabled": args.mode == "on"},
        "alert": lambda: {"op": "alert_mode", "mode": args.mode},
        "clock-sync": lambda: {"op": "clock_sync"},
        "screen": lambda: (
            {"op": "screen_stop"}
            if args.stop
            else {"op": "screen_frame", "unicode": True, "auto_start": True}
        ),
        "key": lambda: {
            "op": "gui_input",
            "key": args.button,
            "mode": "long" if args.long else "short",
        },
        "ls": lambda: {"op": "ls", "path": args.path, "md5": args.md5},
        "pull": lambda: {"op": "pull", "remote": args.remote, "local": _abs_local(args.local)},
        "push": lambda: {
            "op": "push",
            "local": _abs_local(args.local),
            "remote": args.remote,
            "template": args.template,
            "load_on_flipper": args.load,
        },
        "jobs": lambda: {"op": "jobs_list"},
        "run": lambda: {"op": "job_run", "job_id": args.job_id},
        "captures": lambda: {"op": "captures_list", "kind": args.kind, "limit": args.limit},
        "inspect": lambda: (
            {"op": "capture_inspect", "local": _abs_local(args.path)}
            if args.path and os.path.exists(os.path.expanduser(args.path))
            else {"op": "capture_inspect", "remote": args.path, "kind": args.kind}
        ),
        "open": lambda: {"op": "capture_open", "remote": args.remote},
        "badusb": _build_badusb_req,
        "sub-inspect": lambda: (
            {"op": "sub_inspect", "local": _abs_local(args.path)}
            if args.path and os.path.exists(os.path.expanduser(args.path))
            else {"op": "sub_inspect", "remote": args.path}
        ),
        "sub-decode": lambda: (
            {"op": "sub_decode", "local": _abs_local(args.path), "ook_out": _abs_local(args.ook_out)}
            if args.path and os.path.exists(os.path.expanduser(args.path))
            else {"op": "sub_decode", "remote": args.path, "ook_out": _abs_local(args.ook_out)}
        ),
        "sub-synth": lambda: {
            "op": "sub_synth",
            "bits": args.bits,
            "frequency": args.frequency,
            "te_us": args.te,
            "repeat": args.repeat,
            "local": _abs_local(args.local),
            "remote": args.remote,
        },
        "nfc-convert": lambda: {
            "op": "nfc_convert",
            "src": _abs_local(args.src),
            "dst": _abs_local(args.dst or args.dst_opt),
        },
        "nfc-make": lambda: {
            "op": "nfc_make",
            "payload": args.payload,
            "record_type": args.type,
            "uid": args.uid,
            "local": _abs_local(args.local),
            "remote": args.remote or ("/ext/nfc/z2z_ndef.nfc" if args.push else ""),
            "open_on_flipper": args.open,
        },
        "qr": lambda: (
            {"op": "qr_encode", "local": _abs_local(args.source)}
            if os.path.exists(os.path.expanduser(args.source))
            else (
                {"op": "qr_encode", "remote": args.source}
                if args.source.startswith(("/ext/", "/int/"))
                else {"op": "qr_encode", "text": args.source}
            )
        ),
        "cam-scan": lambda: {
            "op": "cam_scan",
            "image": _abs_local(args.image),
            "latest_photo": args.latest_photo,
            "target": args.target,
            "local": _abs_local(args.local),
            "remote": args.remote,
            "push": args.push,
            "open_on_flipper": args.open,
        },
        "rfid-make": lambda: {
            "op": "rfid_make",
            "spec": args.spec,
            "key_type": args.type,
            "local": _abs_local(args.local),
            "remote": args.remote or ("/ext/lfrfid/z2z_badge.rfid" if args.push else ""),
            "open_on_flipper": args.open,
        },
        "ibtn-make": lambda: {
            "op": "ibtn_make",
            "spec": args.spec,
            "key_type": args.type,
            "local": _abs_local(args.local),
            "remote": args.remote or ("/ext/ibutton/z2z_key.ibtn" if args.push else ""),
            "open_on_flipper": args.open,
        },
        "diff": lambda: {
            "op": "capture_diff",
            "src_a": _abs_local(args.src_a),
            "src_b": _abs_local(args.src_b),
        },
        "ir-list": lambda: {
            "op": "ir_list",
            "src": _abs_local(args.src) if args.src else "",
        },
        "ir-tx": lambda: {
            "op": "ir_tx",
            "src": _abs_local(args.src) if args.src else "",
            "button": args.button,
            "proto": args.proto,
            "addr": args.addr,
            "cmd": args.ir_cmd,
            "raw": args.raw,
            "freq": args.freq,
            "duty": args.duty,
            "name": args.name,
            "repeat": args.repeat,
        },
        "ir-save": lambda: {
            "op": "ir_save",
            "name": args.name,
            "dst": _abs_local(args.dst),
        },
        "ir-convert": lambda: {
            "op": "ir_convert",
            "src": _abs_local(args.src),
            "dst": _abs_local(args.dst or args.dst_opt),
            "button": args.button,
        },
        "net": lambda: {"op": "net_summary"},
        "sync": lambda: {"op": "vault_sync"},
        "fap-launch": lambda: {"op": "fap_launch", "path": args.path},
        "fap-install": lambda: {"op": "push", "local": _resolve_fap_arg(), "remote": args.dest},
    }

    try:
        req_obj = req_map[args.cmd]()
    except Exception as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1

    resp = ipc_call(req_obj)
    if args.cmd == "screen" and not args.stop:
        # Stop one-shot stream cleanly so Flipper doesn't keep transmitting frames idle
        try:
            ipc_call({"op": "screen_stop"})
        except Exception:
            pass

    if args.json:
        print(json.dumps(resp, indent=2))
        return 0 if resp.get("ok") else 1

    if not resp.get("ok"):
        print(f"[!] {resp.get('error') or resp}", file=sys.stderr)
        return 1

    if args.cmd == "scan":
        devs = resp.get("devices", [])
        if not devs:
            print("No Flipper Zero BLE devices detected.")
        for d in devs:
            flags = []
            if d.get("paired"):
                flags.append("paired")
            if d.get("connected"):
                flags.append("connected")
            fl = f" [{','.join(flags)}]" if flags else ""
            print(f"{d['address']}  {d['name']:<18} RSSI:{d['rssi']:>4}  ({d['shell_color']}){fl}")
    elif args.cmd == "status":
        lk = resp["link"]
        soc = resp["kali_soc"]
        flp = resp.get("flipper", {})
        st = "CONNECTED" if lk.get("connected") else "IDLE"
        print(f"BLE Link : {st} {lk.get('name', '')} {lk.get('address', '')} (RSSI {lk.get('rssi')} dBm)")
        if flp:
            print(
                f"Flipper  : Charge {flp.get('charge_pct', '?')}% ({flp.get('charge_state', '?')}), "
                f"/ext free {flp.get('ext_free_kb', 0) // 1024} MiB"
            )
        print(
            f"Kali CM0 : {soc['temp_c']}C, RAM {soc['mem_used_mb']}/{soc['mem_total_mb']} MiB, "
            f"Watch={'ON' if resp.get('watch_enabled') else 'OFF'}"
        )
        tools = resp.get("kali_tools") or {}
        if tools:
            active_t = [k for k, v in tools.items() if v]
            print(f"Tools    : {', '.join(active_t) if active_t else '(none detected)'}")
        if resp.get("last_ir_rx"):
            print(f"Last IR  : {resp['last_ir_rx'].get('summary', '')}")
    elif args.cmd == "alert":
        print(f"[+] Flipper alert mode: {resp['alert_mode']}")
    elif args.cmd == "clock-sync":
        print(f"[+] Synced Flipper RTC to {resp['datetime']} (ISO weekday {resp['weekday']})")
    elif args.cmd == "screen":
        if args.stop:
            print("[+] Stopped BLE screen stream")
        else:
            print(resp.get("unicode") or "(no frame received)")
    elif args.cmd == "key":
        print(f"[+] Sent {resp['mode']} '{resp['key']}' to Flipper")
    elif args.cmd == "ls":
        for e in resp.get("entries", []):
            tag = "<DIR>" if e["is_dir"] else f"{e['size']:>7}B"
            md5 = f"  {e['md5sum']}" if e.get("md5sum") else ""
            print(f"{tag}  {e['name']}{md5}")
    elif args.cmd == "jobs":
        for j in resp.get("jobs", []):
            print(f"  {j['id']:<16} {j['title']} — {j['description']}")
    elif args.cmd == "run":
        r = resp["result"]
        if r.get("output"):
            print(r["output"])
        print(
            f"[+] Job '{r['job_id']}' finished (rc={r['returncode']}, "
            f"pulled={len(r['pulled'])}, pushed={len(r['pushed'])}, merged={r['merged_added']})"
        )
    elif args.cmd == "captures":
        caps = resp.get("captures", [])
        if not caps:
            print("No captures found on Flipper.")
        for c in caps:
            print(f"  [{c['kind']:<8}] {c['size']:>6}B  {c['path']}")
    elif args.cmd in ("inspect", "sub-inspect", "sub-decode"):
        print(resp["report"]["compact_text"])
        if resp.get("ook_out"):
            print(f"[+] Wrote .ook pulse file -> {resp['ook_out']}")
    elif args.cmd == "nfc-convert":
        print(f"[+] Converted {resp['src']} -> {resp['dst']} ({resp['bytes']} bytes)")
    elif args.cmd == "nfc-make":
        dest = resp.get("remote") or resp.get("local") or "(in-memory)"
        extra = f" and opened in '{resp['opened_app']}'" if resp.get("opened_app") else ""
        print(f"[+] Created NDEF tag -> {dest} ({resp['bytes']} bytes){extra}")
    elif args.cmd == "qr":
        print(f"QR Payload: {resp['payload']}")
        print(resp["utf8"])
    elif args.cmd == "cam-scan":
        print(f"[+] Decoded [{resp['symbology']}] from {resp['source']}: {resp['payload']}")
        if resp.get("remote") or resp.get("local"):
            dest = resp.get("remote") or resp.get("local")
            extra = f" and opened in '{resp['opened_app']}'" if resp.get("opened_app") else ""
            print(f"[+] Synthesized {resp['target'].upper()} tag -> {dest} ({resp['bytes']} bytes){extra}")
    elif args.cmd == "rfid-make":
        dest = resp.get("remote") or resp.get("local") or "(in-memory)"
        extra = f" and opened in '{resp['opened_app']}'" if resp.get("opened_app") else ""
        print(
            f"[+] Created 125 kHz {resp['key_type']} ({resp['data_hex']}) -> {dest} "
            f"({resp['bytes']} bytes){extra}"
        )
    elif args.cmd == "ibtn-make":
        dest = resp.get("remote") or resp.get("local") or "(in-memory)"
        extra = f" and opened in '{resp['opened_app']}'" if resp.get("opened_app") else ""
        print(
            f"[+] Created 1-Wire {resp['key_type']} ({resp['data_hex']}) -> {dest} "
            f"({resp['bytes']} bytes){extra}"
        )
    elif args.cmd == "diff":
        print(resp["compact_text"])
    elif args.cmd == "ir-list":
        if "files" in resp:
            files = resp.get("files", [])
            if not files:
                print("No `.ir` remotes found in /ext/infrared.")
            for fp in files:
                print(f"  {fp}")
        else:
            print(f"Remote: {resp['src']} ({resp['count']} button(s))")
            for b in resp.get("buttons", []):
                print(f"  [{b['index']:>2}] {b['name']:<18} {b['summary']}")
    elif args.cmd == "ir-tx":
        note = " (auto-launched zero2zero.fap)" if resp.get("auto_launched_fap") else ""
        print(f"[+] Blasted IR '{resp['button']}' [{resp['summary']}] via Flipper{note}")
    elif args.cmd == "ir-save":
        print(f"[+] Saved IR button '{resp['name']}' [{resp['summary']}] -> {resp['dst']} ({resp['bytes']} bytes)")
    elif args.cmd == "ir-convert":
        print(f"[+] Converted {resp['src']} -> {resp['dst']} ({resp['bytes']} bytes)")
    elif args.cmd == "open":
        print(f"[+] Opened {resp['remote']} in Flipper '{resp['app']}' app")
    elif args.cmd == "badusb":
        extra = f" and opened in '{resp['opened_app']}'" if resp.get("opened_app") else ""
        print(f"[+] Pushed {resp['remote']} ({resp['bytes']} bytes){extra}")
    elif args.cmd == "net":
        print(resp["report"]["compact_text"])
    elif args.cmd == "sync":
        v = resp["vault"]
        print(
            f"[+] Vault synced {v['pulled_count']} new/updated files "
            f"({v['bytes_transferred']} B, {v['skipped_count']} unchanged) -> {v['vault_dir']}"
        )
    elif args.cmd == "fap-install":
        print(f"[+] Installed {resp['local']} -> {resp['remote']} ({resp['bytes']} bytes)")
    elif args.cmd == "fap-launch":
        note = " (auto-installed bundled .fap first)" if resp.get("auto_installed") else ""
        print(f"[+] Launched {resp['path']} in BLE RPC Duplex mode{note}")
    else:
        print(json.dumps(resp, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
