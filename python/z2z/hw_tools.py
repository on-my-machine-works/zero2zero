"""Hardware-exclusive companion tools for `zero2zero`.

Covers the three non-overlapping hardware pillars pairing CardputerZero x Flipper Zero:
  1. 125 kHz LF RFID (`H10301`, `EM4100`, `FDX-B`) & 1-Wire `iButton` (`DS1990A` CRC-8,
     `Cyfral`, `Metakom`) key synthesis.
  2. Structural Capture Diffing (`.nfc` page/block diff, `.rfid`, `.ibtn`, `.ir`, `.sub`).
  3. Flipper Infrared (`.ir`) full remote deck parser, learned-button appender, and
     bidirectional Linux `ir-ctl` (`LIRC` pulse/space) <-> Flipper `.ir` converter.
  4. CardputerZero `IMX219` CSI Camera + Kali `zbarimg` (`zbar-tools`) QR & 1D barcode
     optical scanner -> instant Flipper `.nfc` / `.rfid` tag synthesis.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Callable, Dict, List, Optional, Sequence, Tuple

SubprocessRunner = Callable[..., subprocess.CompletedProcess]


# ============================================================================
# 1. 125 kHz LF RFID & 1-Wire iButton Synthesizers
# ============================================================================


def _clean_hex_bytes(raw: str) -> bytes:
    cleaned = re.sub(r"0x", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"[^0-9A-Fa-f]", "", cleaned)
    if len(cleaned) % 2 != 0 or not cleaned:
        raise ValueError(f"Expected even number of hex digits, got {raw!r}")
    return bytes.fromhex(cleaned)


def build_rfid_key(spec: str, key_type: str = "auto") -> Tuple[str, str, str]:
    """Synthesizes a Flipper `.rfid` file (`H10301`, `EM4100`, or `FDX-B`).

    Accepted `spec` formats:
      - `H10301`: `"123:45678"` (`FC:Card`), `"FC=123 Card=45678"`, or 3 hex bytes `"7B B2 6E"`
      - `EM4100`: 5 hex bytes `"18 00 0D C7 EB"`, `"0x18:903147"`, or decimal `"903147"`
      - `FDX-B` : `"900:123456789012"` (`Country:NationalID`) or 8 hex bytes

    Returns `(resolved_key_type, data_hex, file_content)`.
    """
    s = spec.strip()
    if not s:
        raise ValueError("RFID spec cannot be empty")

    kt = key_type.strip()
    upper_kt = kt.upper()

    # Allow prefix override like "H10301:12:3456" or "EM4100:18000DC7EB"
    for pfx, canon in (("H10301:", "H10301"), ("EM4100:", "EM4100"), ("FDX-B:", "FDX-B"), ("FDXB:", "FDX-B")):
        if s.upper().startswith(pfx):
            upper_kt = canon
            s = s[len(pfx) :].strip()
            break

    fc_match = re.match(
        r"^(?:fc\s*=\s*)?(\d+)\s*[: ,/]\s*(?:card\s*=\s*)?(\d+)$", s, flags=re.IGNORECASE
    )

    if upper_kt == "AUTO":
        if fc_match:
            c_a = int(fc_match.group(1))
            c_b = int(fc_match.group(2))
            if c_a <= 255 and c_b <= 65535:
                upper_kt = "H10301"
            else:
                upper_kt = "FDX-B"
        else:
            try:
                hb = _clean_hex_bytes(s)
                if len(hb) == 3:
                    upper_kt = "H10301"
                elif len(hb) == 5:
                    upper_kt = "EM4100"
                elif len(hb) == 8:
                    upper_kt = "FDX-B"
                else:
                    upper_kt = "EM4100"
            except ValueError:
                upper_kt = "EM4100"

    if upper_kt == "H10301":
        canon_type = "H10301"
        if fc_match:
            fc = int(fc_match.group(1))
            card = int(fc_match.group(2))
            if not (0 <= fc <= 255):
                raise ValueError(f"H10301 Facility Code must be 0..255 (got {fc})")
            if not (0 <= card <= 65535):
                raise ValueError(f"H10301 Card Number must be 0..65535 (got {card})")
            raw_bytes = bytes([fc, (card >> 8) & 0xFF, card & 0xFF])
        else:
            raw_bytes = _clean_hex_bytes(s)
            if len(raw_bytes) != 3:
                raise ValueError("H10301 requires `FC:Card` (e.g. `123:45678`) or 3 hex bytes")
    elif upper_kt == "EM4100":
        canon_type = "EM4100"
        em_pair = re.match(r"^(0x[0-9a-fA-F]+|\d+)\s*:\s*(\d+)$", s)
        if em_pair:
            cust = int(em_pair.group(1), 0)
            dec_id = int(em_pair.group(2), 10)
            if not (0 <= cust <= 255) or not (0 <= dec_id <= 0xFFFFFFFF):
                raise ValueError("EM4100 Cust:ID out of range (Cust 0..255, ID 0..4294967295)")
            raw_bytes = bytes([cust]) + dec_id.to_bytes(4, "big")
        elif s.isdigit() and len(s) != 10:
            dec_id = int(s, 10)
            if not (0 <= dec_id <= 0xFFFFFFFFFF):
                raise ValueError("EM4100 decimal ID exceeds 40-bit range")
            raw_bytes = dec_id.to_bytes(5, "big")
        else:
            raw_bytes = _clean_hex_bytes(s)
            if len(raw_bytes) != 5:
                raise ValueError("EM4100 requires 5 hex bytes (10 hex chars) or decimal ID")
    elif upper_kt in ("FDX-B", "FDXB"):
        canon_type = "FDX-B"
        if fc_match:
            country = int(fc_match.group(1))
            nat_id = int(fc_match.group(2))
            if not (0 <= country <= 1023) or not (0 <= nat_id <= (1 << 38) - 1):
                raise ValueError("FDX-B Country (0..1023) or National ID (38-bit) out of range")
            val64 = ((country & 0x3FF) << 38) | (nat_id & ((1 << 38) - 1))
            raw_bytes = val64.to_bytes(8, "big")
        else:
            raw_bytes = _clean_hex_bytes(s)
            if len(raw_bytes) != 8:
                raise ValueError("FDX-B requires `Country:NationalID` or 8 hex bytes")
    else:
        raise ValueError(f"Unsupported LF RFID key type: {key_type!r}")

    data_hex = " ".join(f"{b:02X}" for b in raw_bytes)
    content = (
        "Filetype: Flipper RFID key\n"
        "Version: 1\n"
        f"Key type: {canon_type}\n"
        f"Data: {data_hex}\n"
    )
    return canon_type, data_hex, content


def dallas_crc8(data: bytes) -> int:
    """Computes Dallas/Maxim 1-Wire CRC-8 (`x^8 + x^5 + x^4 + 1`, reflected `0x8C`)."""
    crc = 0
    for byte in data:
        cur = byte
        for _ in range(8):
            mix = (crc ^ cur) & 0x01
            crc >>= 1
            if mix:
                crc ^= 0x8C
            cur >>= 1
    return crc & 0xFF


def build_ibutton_key(spec: str, key_type: str = "auto") -> Tuple[str, str, str]:
    """Synthesizes a Flipper `.ibtn` key (`Dallas` DS1990A with auto CRC-8, `Cyfral`, `Metakom`)."""
    s = spec.strip()
    if not s:
        raise ValueError("iButton spec cannot be empty")

    upper_kt = key_type.strip().upper()
    for pfx, canon in (("DALLAS:", "DALLAS"), ("DS1990A:", "DALLAS"), ("CYFRAL:", "CYFRAL"), ("METAKOM:", "METAKOM")):
        if s.upper().startswith(pfx):
            upper_kt = canon
            s = s[len(pfx) :].strip()
            break

    raw_bytes = _clean_hex_bytes(s)
    if upper_kt == "AUTO":
        if len(raw_bytes) == 2:
            upper_kt = "CYFRAL"
        elif len(raw_bytes) == 4:
            upper_kt = "METAKOM"
        else:
            upper_kt = "DALLAS"

    if upper_kt in ("DALLAS", "DS1990A"):
        canon_type = "Dallas"
        if len(raw_bytes) == 6:
            body = bytes([0x01]) + raw_bytes
            raw_bytes = body + bytes([dallas_crc8(body)])
        elif len(raw_bytes) == 7:
            raw_bytes = raw_bytes + bytes([dallas_crc8(raw_bytes)])
        elif len(raw_bytes) == 8:
            raw_bytes = raw_bytes[:7] + bytes([dallas_crc8(raw_bytes[:7])])
        else:
            raise ValueError("Dallas DS1990A requires 6-byte serial (or 7/8-byte ROM hex)")
    elif upper_kt == "CYFRAL":
        canon_type = "Cyfral"
        if len(raw_bytes) != 2:
            raise ValueError("Cyfral requires 2 hex bytes")
    elif upper_kt == "METAKOM":
        canon_type = "Metakom"
        if len(raw_bytes) != 4:
            raise ValueError("Metakom requires 4 hex bytes")
    else:
        raise ValueError(f"Unsupported iButton key type: {key_type!r}")

    data_hex = " ".join(f"{b:02X}" for b in raw_bytes)
    content = (
        "Filetype: Flipper iButton key\n"
        "Version: 1\n"
        f"Key type: {canon_type}\n"
        f"Data: {data_hex}\n"
    )
    return canon_type, data_hex, content


# ============================================================================
# 2. Structural Capture Diff Engine (`.nfc`, `.rfid`, `.ibtn`, `.ir`, `.sub`)
# ============================================================================


def _extract_nfc_units(text: str) -> Tuple[Dict[str, str], str, Dict[int, str]]:
    meta: Dict[str, str] = {}
    unit_kind = "Unit"
    units: Dict[int, str] = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        k, v = [p.strip() for p in s.split(":", 1)]
        if k.startswith("Page "):
            unit_kind = "Page"
            try:
                units[int(k.split()[1])] = v
            except ValueError:
                pass
        elif k.startswith("Block "):
            unit_kind = "Blk"
            try:
                units[int(k.split()[1])] = v
            except ValueError:
                pass
        elif k in ("Device type", "UID", "ATQA", "SAK", "Mifare Classic type"):
            meta[k] = v
    return meta, unit_kind, units


def diff_capture_bytes(
    name_a: str,
    bytes_a: bytes,
    name_b: str,
    bytes_b: bytes,
) -> Dict[str, object]:
    """Computes a structural, 50-col LCD-friendly diff between two Flipper captures."""
    base_a = os.path.basename(name_a) or "A"
    base_b = os.path.basename(name_b) or "B"
    lines: List[str] = [
        f"A: {base_a[:44]}",
        f"B: {base_b[:44]}",
    ]

    if bytes_a == bytes_b:
        lines.append("IDENTICAL (0 byte differences)")
        return {
            "identical": True,
            "changed_count": 0,
            "compact_text": "\n".join(lines),
        }

    head_a = bytes_a[:128].decode("utf-8", errors="replace")
    head_b = bytes_b[:128].decode("utf-8", errors="replace")
    text_a = bytes_a.decode("utf-8", errors="replace")
    text_b = bytes_b.decode("utf-8", errors="replace")

    changed_count = 0

    # 1. NFC Page / Block structural diff
    if "Flipper NFC device" in head_a and "Flipper NFC device" in head_b:
        meta_a, ukind, units_a = _extract_nfc_units(text_a)
        meta_b, _uk2, units_b = _extract_nfc_units(text_b)
        for mk in ("Device type", "UID", "Mifare Classic type"):
            va, vb = meta_a.get(mk, ""), meta_b.get(mk, "")
            if va != vb:
                changed_count += 1
                lines.append(f"{mk}: {va} -> {vb}"[:49])

        all_indices = sorted(set(units_a) | set(units_b))
        diff_rows: List[str] = []
        byte_diffs = 0
        for idx in all_indices:
            ua = units_a.get(idx, "(missing)")
            ub = units_b.get(idx, "(missing)")
            if ua != ub:
                changed_count += 1
                ta = ua.split()
                tb = ub.split()
                byte_diffs += sum(1 for x, y in zip(ta, tb) if x != y) + abs(len(ta) - len(tb))
                if len(ua) <= 14 and len(ub) <= 14:
                    diff_rows.append(f"  {ukind[0]}{idx:03d}: {ua} -> {ub}"[:49])
                else:
                    # Highlight first differing byte offset inside 16-byte MIFARE block
                    chg_pos = [i for i, (x, y) in enumerate(zip(ta, tb)) if x != y]
                    off_note = f"@{chg_pos[0]}" if chg_pos else ""
                    diff_rows.append(
                        f"  {ukind}{idx:02d}{off_note}: {ua[:17]}.. -> {ub[:17]}.."[:49]
                    )
        lines.append(f"Diff: { len(diff_rows) }/{len(all_indices)} {ukind.lower()}s ({byte_diffs}B changed)")
        lines.extend(diff_rows[:12])
        if len(diff_rows) > 12:
            lines.append(f"  ... +{len(diff_rows) - 12} more {ukind.lower()}(s)")
        return {
            "identical": False,
            "kind": "nfc",
            "changed_count": changed_count,
            "compact_text": "\n".join(lines),
        }

    # 2. IR remote button diff
    if "IR signals file" in head_a and "IR signals file" in head_b:
        sigs_a = {s["name"]: s for s in parse_ir_remote_full(text_a)}
        sigs_b = {s["name"]: s for s in parse_ir_remote_full(text_b)}
        only_a = sorted(set(sigs_a) - set(sigs_b))
        only_b = sorted(set(sigs_b) - set(sigs_a))
        modified: List[str] = []
        for nm in sorted(set(sigs_a) & set(sigs_b)):
            sa, sb = sigs_a[nm], sigs_b[nm]
            if (sa.get("type"), sa.get("protocol"), sa.get("address_hex"), sa.get("command_hex"), sa.get("pulses")) != (
                sb.get("type"),
                sb.get("protocol"),
                sb.get("address_hex"),
                sb.get("command_hex"),
                sb.get("pulses"),
            ):
                modified.append(nm)
        changed_count = len(only_a) + len(only_b) + len(modified)
        lines.append(f"IR Diff: +{len(only_b)} -{len(only_a)} ~{len(modified)} buttons")
        for nm in only_b[:4]:
            lines.append(f"  + {nm}")
        for nm in only_a[:4]:
            lines.append(f"  - {nm}")
        for nm in modified[:6]:
            lines.append(f"  ~ {nm}: {sigs_a[nm].get('summary','')} -> {sigs_b[nm].get('summary','')}"[:49])
        return {
            "identical": False,
            "kind": "infrared",
            "changed_count": changed_count,
            "compact_text": "\n".join(lines),
        }

    # 3. Line-oriented key/value Flipper files (`.rfid`, `.ibtn`, `.sub`, text)
    lines_a = [ln.strip() for ln in text_a.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    lines_b = [ln.strip() for ln in text_b.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    max_ln = max(len(lines_a), len(lines_b))
    diff_lns: List[str] = []
    for i in range(max_ln):
        la = lines_a[i] if i < len(lines_a) else "(eof)"
        lb = lines_b[i] if i < len(lines_b) else "(eof)"
        if la != lb:
            changed_count += 1
            diff_lns.append(f"L{i+1}: {la[:20]} -> {lb[:20]}"[:49])
    lines.append(f"Changed lines: {changed_count}")
    lines.extend(diff_lns[:12])
    return {
        "identical": False,
        "changed_count": changed_count,
        "compact_text": "\n".join(lines),
    }


# ============================================================================
# 3. Flipper Infrared (`.ir`) Full Remote Deck & Linux `ir-ctl` Converter
# ============================================================================


def _u32_to_le_hex(val: int) -> str:
    b = (int(val) & 0xFFFFFFFF).to_bytes(4, "little")
    return " ".join(f"{x:02X}" for x in b)


def _le_hex_to_u32(hex_str: str) -> int:
    toks = [int(t, 16) for t in hex_str.split() if len(t) == 2]
    raw = bytes(toks[:4]).ljust(4, b"\x00")
    return int.from_bytes(raw, "little")


def parse_ir_remote_full(content: str | bytes) -> List[Dict[str, object]]:
    """Parses all buttons in a Flipper `.ir` remote file (preserving 32-bit LE addr/cmd)."""
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    signals: List[Dict[str, object]] = []
    cur: Dict[str, object] = {}

    def _flush() -> None:
        nonlocal cur
        if not cur.get("name"):
            cur = {}
            return
        idx = len(signals)
        stype = str(cur.get("type", "parsed")).lower()
        name = str(cur.get("name", f"Btn_{idx}"))
        if stype == "parsed":
            proto = str(cur.get("protocol", "NEC"))
            addr_hex = str(cur.get("address", "00 00 00 00"))
            cmd_hex = str(cur.get("command", "00 00 00 00"))
            addr_u32 = _le_hex_to_u32(addr_hex)
            cmd_u32 = _le_hex_to_u32(cmd_hex)
            summary = f"{proto} A:0x{addr_u32:X} C:0x{cmd_u32:X}"
            signals.append(
                {
                    "index": idx,
                    "name": name,
                    "type": "parsed",
                    "protocol": proto,
                    "address_hex": addr_hex,
                    "command_hex": cmd_hex,
                    "address_u32": addr_u32,
                    "command_u32": cmd_u32,
                    "summary": summary,
                }
            )
        else:
            freq = int(cur.get("frequency", 38000))
            duty = float(cur.get("duty_cycle", 0.33))
            raw_data = str(cur.get("data", ""))
            timings = [int(tok) for tok in raw_data.split() if tok.isdigit() and int(tok) > 0]
            summary = f"RAW {len(timings)}p @{freq // 1000}kHz"
            signals.append(
                {
                    "index": idx,
                    "name": name,
                    "type": "raw",
                    "frequency": freq,
                    "duty_cycle": duty,
                    "timings": timings,
                    "pulses": len(timings),
                    "summary": summary,
                }
            )
        cur = {}

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if cur.get("name"):
                _flush()
            continue
        if ":" not in s:
            continue
        k, v = [p.strip() for p in s.split(":", 1)]
        if k in ("Filetype", "Version"):
            continue
        if k == "name":
            if cur.get("name"):
                _flush()
            cur["name"] = v
        elif k in ("type", "protocol", "address", "command", "data"):
            cur[k] = v
        elif k == "frequency":
            try:
                cur["frequency"] = int(float(v))
            except ValueError:
                pass
        elif k == "duty_cycle":
            try:
                cur["duty_cycle"] = float(v)
            except ValueError:
                pass
    _flush()
    return signals


def append_ir_signal_to_remote(existing_content: str | bytes, signal: Dict[str, object]) -> str:
    """Appends a parsed or raw IR button block to a Flipper `.ir` file."""
    base = (
        existing_content.decode("utf-8", errors="replace")
        if isinstance(existing_content, bytes)
        else existing_content
    ).strip()
    if not base or "IR signals file" not in base[:64]:
        base = "Filetype: IR signals file\nVersion: 1"

    name = re.sub(r"[\r\n#:]", "_", str(signal.get("name") or "Learned")).strip() or "Learned"
    stype = str(signal.get("type", "parsed")).lower()
    block_lines = ["#", f"name: {name}"]

    if stype == "parsed":
        proto = str(signal.get("protocol") or signal.get("proto") or "NEC")
        if "address_hex" in signal:
            addr_hex = str(signal["address_hex"])
        else:
            addr_hex = _u32_to_le_hex(int(signal.get("address_u32", signal.get("addr", 0))))
        if "command_hex" in signal:
            cmd_hex = str(signal["command_hex"])
        else:
            cmd_hex = _u32_to_le_hex(int(signal.get("command_u32", signal.get("cmd", 0))))
        block_lines.extend(
            [
                "type: parsed",
                f"protocol: {proto}",
                f"address: {addr_hex}",
                f"command: {cmd_hex}",
            ]
        )
    else:
        freq = int(signal.get("frequency", signal.get("freq", 38000)))
        duty = float(signal.get("duty_cycle", 0.33))
        if duty > 1.0:
            duty = duty / 100.0
        raw_timings = signal.get("timings")
        if isinstance(raw_timings, str):
            t_list = [int(x) for x in re.split(r"[,\s]+", raw_timings.strip()) if x.isdigit()]
        else:
            t_list = [int(x) for x in (raw_timings or []) if int(x) > 0]
        if not t_list:
            raise ValueError("Raw IR signal requires non-empty timings")
        block_lines.extend(
            [
                "type: raw",
                f"frequency: {freq}",
                f"duty_cycle: {duty:.6f}",
                "data: " + " ".join(str(x) for x in t_list),
            ]
        )

    return base + "\n" + "\n".join(block_lines) + "\n"


def _synthesize_parsed_ir_pulses(proto: str, addr: int, cmd: int) -> Tuple[int, List[int]]:
    """Synthesizes mark/space microsecond timings for common IR protocols for Linux `ir-ctl`."""
    p = proto.upper()
    if p in ("NEC", "NECEXT", "SAMSUNG32"):
        is_samsung = p == "SAMSUNG32"
        hdr_mark = 4500 if is_samsung else 9000
        hdr_space = 4500
        if p == "NEC":
            a_lo = addr & 0xFF
            c_lo = cmd & 0xFF
            word = a_lo | ((a_lo ^ 0xFF) << 8) | (c_lo << 16) | ((c_lo ^ 0xFF) << 24)
        elif p == "NECEXT":
            a16 = addr & 0xFFFF
            c16 = cmd & 0xFFFF
            # If high byte of cmd is 00, standard NECext still uses inverted cmd byte unless full 16b
            if (c16 >> 8) == 0:
                c_lo = c16 & 0xFF
                c16 = c_lo | ((c_lo ^ 0xFF) << 8)
            word = a16 | (c16 << 16)
        else:
            a16 = addr & 0xFFFF
            if (a16 >> 8) == 0:
                a16 = (a16 & 0xFF) | ((a16 & 0xFF) << 8)
            c_lo = cmd & 0xFF
            word = a16 | (c_lo << 16) | ((c_lo ^ 0xFF) << 24)

        pulses = [hdr_mark, hdr_space]
        for bit_idx in range(32):
            bit = (word >> bit_idx) & 1
            pulses.extend([560, 1690 if bit else 560])
        pulses.append(560)
        return 38000, pulses

    if p in ("SIRC", "SIRC15", "SIRC20"):
        nbits = 12 if p == "SIRC" else (15 if p == "SIRC15" else 20)
        word = (cmd & 0x7F) | ((addr & ((1 << (nbits - 7)) - 1)) << 7)
        pulses = [2400, 600]
        for bit_idx in range(nbits):
            bit = (word >> bit_idx) & 1
            pulses.extend([1200 if bit else 600, 600])
        return 40000, pulses[:-1]

    raise ValueError(f"Protocol {proto!r} cannot be pulse-expanded; use raw timings")


def ir_to_irctl(ir_content: str | bytes, button: str = "") -> str:
    """Converts a Flipper `.ir` button into a Linux `ir-ctl` (`v4l-utils`) raw pulse-space file."""
    signals = parse_ir_remote_full(ir_content)
    if not signals:
        raise ValueError("Flipper `.ir` file contains no signals")

    chosen = signals[0]
    if button:
        q = button.strip().lower()
        exact = next((s for s in signals if str(s["name"]).lower() == q), None)
        sub = next((s for s in signals if q in str(s["name"]).lower()), None)
        if exact:
            chosen = exact
        elif sub:
            chosen = sub
        elif button.isdigit() and int(button) < len(signals):
            chosen = signals[int(button)]
        else:
            raise KeyError(f"Button {button!r} not found in `.ir` file")

    if chosen["type"] == "raw":
        freq = int(chosen["frequency"])
        timings = list(chosen["timings"])  # type: ignore[arg-type]
    else:
        freq, timings = _synthesize_parsed_ir_pulses(
            str(chosen["protocol"]),
            int(chosen["address_u32"]),
            int(chosen["command_u32"]),
        )

    out_lines = [
        f"# Exported from Flipper button: {chosen['name']} ({chosen['summary']})",
        f"carrier {freq}",
    ]
    row: List[str] = []
    for idx, dur in enumerate(timings):
        prefix = "+" if (idx % 2 == 0) else "-"
        row.append(f"{prefix}{int(dur)}")
        if len(row) >= 12:
            out_lines.append(" ".join(row))
            row = []
    if row:
        out_lines.append(" ".join(row))
    return "\n".join(out_lines) + "\n"


def irctl_to_ir(irctl_content: str | bytes, button_name: str = "Imported") -> str:
    """Converts a Linux `ir-ctl` raw pulse-space text file into a Flipper `.ir` file."""
    text = (
        irctl_content.decode("utf-8", errors="replace")
        if isinstance(irctl_content, bytes)
        else irctl_content
    )
    freq = 38000
    timings: List[int] = []

    for raw_ln in text.splitlines():
        s = raw_ln.strip()
        if not s or s.startswith("#"):
            continue
        low = s.lower()
        if low.startswith("carrier "):
            parts = low.split()
            if len(parts) >= 2 and parts[1].isdigit():
                freq = int(parts[1])
            continue
        if low.startswith("pulse ") or low.startswith("space "):
            parts = low.split()
            if len(parts) >= 2 and parts[1].isdigit():
                val = int(parts[1])
                if low.startswith("space ") and not timings:
                    continue
                timings.append(val)
            continue
        for tok in s.split():
            m = re.match(r"^([+-]?)(\d+)$", tok)
            if m:
                sign, digits = m.group(1), int(m.group(2))
                if sign == "-" and not timings:
                    continue
                if digits > 0:
                    timings.append(digits)

    if not timings:
        raise ValueError("No pulse/space timings found in `ir-ctl` input")
    # Ensure odd number of timings (ends on mark) as Flipper prefers
    if len(timings) % 2 == 0:
        timings = timings[:-1]

    return append_ir_signal_to_remote(
        "",
        {
            "name": button_name,
            "type": "raw",
            "frequency": freq,
            "duty_cycle": 0.33,
            "timings": timings,
        },
    )


# ============================================================================
# 4. CardputerZero `IMX219` CSI Camera + Kali `zbarimg` Optical Scanner
# ============================================================================


def find_latest_camera_photo(search_dirs: Optional[Sequence[Path]] = None) -> Optional[Path]:
    """Finds the newest `.jpg`/`.png`/`.ppm` captured by CardputerZero's Camera app."""
    dirs = list(search_dirs) if search_dirs is not None else [
        Path("/home/kali/Pictures"),
        Path("/home/kali/DCIM"),
        Path("/home/kali"),
        Path.home() / "Pictures",
        Path("/tmp"),
    ]
    candidates: List[Tuple[float, Path]] = []
    exts = (".jpg", ".jpeg", ".png", ".ppm", ".pgm")
    for d in dirs:
        if not d.is_dir():
            continue
        try:
            for entry in d.iterdir():
                if entry.is_file() and entry.suffix.lower() in exts and not entry.name.startswith("."):
                    candidates.append((entry.stat().st_mtime, entry))
        except OSError:
            continue
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def decode_barcodes_with_zbar(
    image_path: str | Path,
    runner: Optional[SubprocessRunner] = None,
) -> List[Dict[str, str]]:
    """Decodes QR codes and 1D barcodes from an image via Kali's `zbarimg` (`zbar-tools`)."""
    zbar_bin = shutil.which("zbarimg")
    if runner is None and not zbar_bin:
        raise RuntimeError("`zbarimg` not found in PATH (install Kali package `zbar-tools`)")

    exec_fn = runner or subprocess.run
    proc = exec_fn(
        [zbar_bin or "zbarimg", "--quiet", str(image_path)],
        capture_output=True,
        text=True,
        timeout=10.0,
    )
    # `zbarimg` returns exit code 4 when zero barcodes were found in a valid image
    if proc.returncode not in (0, 4):
        err = (proc.stderr or proc.stdout or f"zbarimg exited with {proc.returncode}").strip()
        raise RuntimeError(err)

    results: List[Dict[str, str]] = []
    for line in (proc.stdout or "").splitlines():
        if ":" not in line:
            continue
        sym, data = line.split(":", 1)
        sym = sym.strip()
        data = data.strip()
        if sym and data:
            results.append({"symbology": sym, "payload": data})
    return results


def _build_rpi_libcamera_env() -> Dict[str, str]:
    env = os.environ.copy()
    rpi_root = Path("/opt/rpi-libcamera")
    lib_dir = rpi_root / "usr/lib/aarch64-linux-gnu"
    if lib_dir.is_dir():
        cur_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{lib_dir}:{cur_ld}" if cur_ld else str(lib_dir)
        env["LIBCAMERA_IPA_MODULE_PATH"] = str(lib_dir / "libcamera/ipa")
        env["LIBCAMERA_IPA_PROXY_PATH"] = str(rpi_root / "usr/libexec/aarch64-linux-gnu/libcamera")
        env["LIBCAMERA_IPA_CONFIG_PATH"] = str(rpi_root / "usr/share/libcamera/ipa")
        rpi_yaml = rpi_root / "usr/share/libcamera/pipeline/rpi/vc4/rpi_apps.yaml"
        if rpi_yaml.is_file():
            env.setdefault("LIBCAMERA_RPI_CONFIG_FILE", str(rpi_yaml))
    return env


def capture_imx219_burst(
    out_ppm: Path,
    runner: Optional[SubprocessRunner] = None,
) -> List[Path]:
    """Captures a settled 4-frame RGB burst from CardputerZero's `IMX219` CSI camera via `cam`."""
    cam_bin = shutil.which("cam") or shutil.which("rpicam-still") or shutil.which("libcamera-still")
    if runner is None and not cam_bin:
        raise RuntimeError(
            "No camera CLI (`cam` / `rpicam-still`) found; install `libcamera-tools` or pass `--image`"
        )

    exec_fn = runner or subprocess.run
    exe_name = os.path.basename(cam_bin or "cam")
    if exe_name == "cam":
        # Capture 4 frames so AE/AGC settles on IMX219, returning newest-first!
        stem = out_ppm.with_suffix("")
        pattern = f"{stem}-#.ppm"
        cmd = [
            cam_bin or "cam",
            "-c",
            "1",
            "--capture=4",
            "-s",
            "role=still,width=1280,height=960,pixelformat=BGR888",
            f"--file={pattern}",
        ]
        proc = exec_fn(cmd, capture_output=True, text=True, timeout=12.0)
        produced = sorted(out_ppm.parent.glob(f"{stem.name}-*.ppm"))
        if not produced and Path("/opt/rpi-libcamera").is_dir() and runner is None:
            proc = exec_fn(
                cmd,
                capture_output=True,
                text=True,
                timeout=12.0,
                env=_build_rpi_libcamera_env(),
            )
            produced = sorted(out_ppm.parent.glob(f"{stem.name}-*.ppm"))
        if not produced:
            if out_ppm.is_file():
                return [out_ppm]
            err = (proc.stderr or proc.stdout or "IMX219 frame capture produced no file").strip()
            raise RuntimeError(err[-240:])
        return list(reversed(produced))

    cmd = [cam_bin or exe_name, "-n", "-t", "500", "--width", "1280", "--height", "960", "-o", str(out_ppm)]
    proc = exec_fn(cmd, capture_output=True, text=True, timeout=12.0)
    if not out_ppm.is_file():
        err = (proc.stderr or proc.stdout or "Camera capture failed").strip()
        raise RuntimeError(err[-240:])
    return [out_ppm]


def scan_optical_code(
    image_path: str = "",
    use_latest_photo: bool = False,
    cam_runner: Optional[SubprocessRunner] = None,
    zbar_runner: Optional[SubprocessRunner] = None,
) -> Dict[str, object]:
    """Scans a QR or 1D barcode from CardputerZero's `IMX219` camera, latest photo, or image path."""
    if image_path:
        src = Path(image_path).expanduser()
        if not src.is_file():
            raise FileNotFoundError(f"Image file not found: {image_path}")
        codes = decode_barcodes_with_zbar(src, runner=zbar_runner)
        return {
            "source": str(src),
            "codes": codes,
            "matched": len(codes) > 0,
        }

    if use_latest_photo:
        latest = find_latest_camera_photo()
        if latest is None:
            raise FileNotFoundError("No captured photos found in ~/Pictures or /tmp")
        codes = decode_barcodes_with_zbar(latest, runner=zbar_runner)
        return {
            "source": str(latest),
            "codes": codes,
            "matched": len(codes) > 0,
        }

    with tempfile.TemporaryDirectory(prefix="z2z-cam-") as tmp:
        frames = capture_imx219_burst(Path(tmp) / "capture.ppm", runner=cam_runner)
        codes: List[Dict[str, str]] = []
        for frame in frames:
            codes = decode_barcodes_with_zbar(frame, runner=zbar_runner)
            if codes:
                break
        return {
            "source": "imx219",
            "codes": codes,
            "matched": len(codes) > 0,
        }
