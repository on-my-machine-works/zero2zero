"""Multi-format Flipper Zero capture inspector (`.sub`, `.nfc`, `.rfid`, `.ir`, `.ibtn`).

Pure Python 3 stdlib parsers tailored for 52-column CardputerZero (`320x170`) and
Flipper Zero (`128x64`) screens, including NDEF URI/Text extraction for `.nfc` dumps,
Wiegand-26 (`H10301`) & `EM4100` decoding for `.rfid`, and multi-button `.ir` summaries.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath
import shutil
import struct
import subprocess
import tempfile
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .nfc_tools import inspect_nfc_with_proxmark3
from .rpc_client import FlipperRpcClient
from .subghz import parse_and_analyze_sub

CAPTURE_DIRS: Tuple[Tuple[str, str, str], ...] = (
    ("subghz", "/ext/subghz", ".sub"),
    ("nfc", "/ext/nfc", ".nfc"),
    ("lfrfid", "/ext/lfrfid", ".rfid"),
    ("infrared", "/ext/infrared", ".ir"),
    ("ibutton", "/ext/ibutton", ".ibtn"),
    ("pcap", "/ext/apps_data/pcaps", ".pcap"),
)

NDEF_URI_PREFIXES: Tuple[str, ...] = (
    "",
    "http://www.",
    "https://www.",
    "http://",
    "https://",
    "tel:",
    "mailto:",
    "ftp://anonymous:anonymous@",
    "ftp://ftp.",
    "ftps://",
    "sftp://",
    "smb://",
    "nfs://",
    "ftp://",
    "dav://",
    "news:",
    "telnet://",
    "imap:",
    "rtsp://",
    "urn:",
    "pop:",
    "sip:",
    "sips:",
    "tftp:",
    "btspp://",
    "btl2cap://",
    "btgoep://",
    "tcpobex://",
    "irdaobex://",
    "file://",
    "urn:epc:id:",
    "urn:epc:tag:",
    "urn:epc:pat:",
    "urn:epc:raw:",
    "urn:epc:",
    "urn:nfc:",
)


def _decode_ndef_records(msg: bytes) -> List[str]:
    """Extracts human-readable URI, Text, and MIME records from an NDEF message."""
    records: List[str] = []
    pos = 0
    total = len(msg)
    while pos + 2 <= total and len(records) < 6:
        hdr = msg[pos]
        pos += 1
        me = bool(hdr & 0x40)
        sr = bool(hdr & 0x10)
        il = bool(hdr & 0x08)
        tnf = hdr & 0x07

        type_len = msg[pos]
        pos += 1
        if sr:
            if pos >= total:
                break
            payload_len = msg[pos]
            pos += 1
        else:
            if pos + 4 > total:
                break
            payload_len = int.from_bytes(msg[pos : pos + 4], "big")
            pos += 4

        id_len = 0
        if il:
            if pos >= total:
                break
            id_len = msg[pos]
            pos += 1

        if pos + type_len + id_len + payload_len > total:
            break
        rec_type = msg[pos : pos + type_len]
        pos += type_len + id_len
        payload = msg[pos : pos + payload_len]
        pos += payload_len

        if tnf == 0x01:  # NFC Forum Well-Known Type
            if rec_type == b"U" and payload:
                pcode = payload[0]
                prefix = NDEF_URI_PREFIXES[pcode] if pcode < len(NDEF_URI_PREFIXES) else ""
                uri = prefix + payload[1:].decode("utf-8", errors="replace")
                records.append(f"URI: {uri}")
            elif rec_type == b"T" and payload:
                status = payload[0]
                lang_len = status & 0x3F
                enc = "utf-16" if (status & 0x80) else "utf-8"
                txt = payload[1 + lang_len :].decode(enc, errors="replace").strip()
                records.append(f"TXT: {txt}")
        elif tnf == 0x02:  # MIME media-type
            mtype = rec_type.decode("ascii", errors="replace")
            preview = payload[:32].decode("utf-8", errors="replace").strip()
            records.append(f"MIME({mtype}): {preview}")
        elif tnf == 0x04:  # NFC Forum External Type
            etype = rec_type.decode("ascii", errors="replace")
            preview = payload[:32].decode("utf-8", errors="replace").strip()
            records.append(f"EXT({etype}): {preview}")

        if me:
            break
    return records


def _extract_ndef_from_bytes(user_bytes: bytes) -> List[str]:
    """Scans Type 2 / Mifare TLV stream (`0x03 <len> <ndef> ... 0xFE`) for NDEF messages."""
    found: List[str] = []
    i = 0
    n = len(user_bytes)
    while i < n and len(found) < 6:
        tag = user_bytes[i]
        i += 1
        if tag == 0x00:
            continue
        if tag == 0xFE:
            break
        if i >= n:
            break
        length = user_bytes[i]
        i += 1
        if length == 0xFF:
            if i + 2 > n:
                break
            length = (user_bytes[i] << 8) | user_bytes[i + 1]
            i += 2
        val = user_bytes[i : i + length]
        i += length
        if tag == 0x03 and val:
            found.extend(_decode_ndef_records(val))
    return found


def _is_mifare_trailer_block(block_idx: int) -> bool:
    if block_idx < 128:
        return (block_idx + 1) % 4 == 0
    return (block_idx - 128 + 1) % 16 == 0


def parse_nfc_capture(
    content: str | bytes,
    use_proxmark3: bool = True,
    pm3_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> Dict[str, object]:
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    dev_type = ""
    uid = ""
    atqa = ""
    sak = ""
    mf_type = ""
    pages: Dict[int, List[Optional[int]]] = {}
    blocks: Dict[int, List[Optional[int]]] = {}
    unknown_bytes = 0

    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        k, v = s.split(":", 1)
        k = k.strip()
        v = v.strip()
        if k == "Device type":
            dev_type = v
        elif k == "UID":
            uid = v
        elif k == "ATQA":
            atqa = v
        elif k == "SAK":
            sak = v
        elif k == "Mifare Classic type":
            mf_type = v
        elif k.startswith("Page ") or k.startswith("Block "):
            parts = k.split()
            if len(parts) == 2 and parts[1].isdigit():
                idx = int(parts[1])
                row: List[Optional[int]] = []
                for tok in v.split():
                    if tok == "??":
                        row.append(None)
                        unknown_bytes += 1
                    else:
                        try:
                            row.append(int(tok, 16))
                        except ValueError:
                            pass
                if k.startswith("Page "):
                    pages[idx] = row
                else:
                    blocks[idx] = row

    # Reconstruct contiguous user memory for NDEF TLV scan
    user_buf = bytearray()
    if pages:
        for pidx in sorted(k for k in pages if k >= 4):
            for b in pages[pidx]:
                if b is None:
                    break
                user_buf.append(b)
    elif blocks:
        for bidx in sorted(k for k in blocks if k >= 4 and not _is_mifare_trailer_block(k)):
            for b in blocks[bidx]:
                if b is None:
                    break
                user_buf.append(b)

    ndef_records = _extract_ndef_from_bytes(bytes(user_buf)) if user_buf else []
    qr_payload = ""
    for rec in ndef_records:
        if rec.startswith("URI: "):
            qr_payload = rec[5:].strip()
            break
        if rec.startswith("TXT: ") and not qr_payload:
            qr_payload = rec[5:].strip()
    if not qr_payload and uid:
        qr_payload = f"NFC {dev_type or 'Tag'} UID:{uid}"

    label = dev_type or "NFC Tag"
    if mf_type and mf_type not in label:
        label = f"{label} ({mf_type})"

    lines = [f"NFC : {label}"]
    if uid:
        lines.append(f"UID : {uid}")
    if atqa or sak:
        lines.append(f"ISO : ATQA={atqa or '?'} SAK={sak or '?'}")
    if pages:
        lines.append(f"Dump: {len(pages)} pages" + (f" ({unknown_bytes}B ??)" if unknown_bytes else ""))
    elif blocks:
        lines.append(
            f"Dump: {len(blocks)} blocks" + (f" ({unknown_bytes}B locked)" if unknown_bytes else "")
        )
    for rec in ndef_records[:3]:
        lines.append(rec[:50])

    pm3_info: Dict[str, object] = {}
    if use_proxmark3 and (pages or blocks):
        pm3_info = inspect_nfc_with_proxmark3(content, runner=pm3_runner)
        if pm3_info.get("summary"):
            lines.extend(str(pm3_info["summary"]).splitlines()[:2])

    out_nfc: Dict[str, object] = {
        "kind": "nfc",
        "device_type": dev_type,
        "mifare_type": mf_type,
        "uid": uid,
        "atqa": atqa,
        "sak": sak,
        "page_count": len(pages),
        "block_count": len(blocks),
        "unknown_bytes": unknown_bytes,
        "ndef": ndef_records,
        "qr_payload": qr_payload,
        "compact_text": "\n".join(lines),
    }
    if pm3_info.get("summary"):
        out_nfc["proxmark3_summary"] = pm3_info["summary"]
    return out_nfc


def parse_rfid_capture(content: str | bytes) -> Dict[str, object]:
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    key_type = ""
    data_hex = ""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        k, v = s.split(":", 1)
        k = k.strip()
        v = v.strip()
        if k == "Key type":
            key_type = v
        elif k == "Data":
            data_hex = v

    raw_bytes = bytes(int(tok, 16) for tok in data_hex.split() if len(tok) == 2)
    decoded_summary = ""
    details: Dict[str, object] = {}

    if key_type == "H10301" and len(raw_bytes) >= 3:
        fc = raw_bytes[0]
        card = (raw_bytes[1] << 8) | raw_bytes[2]
        decoded_summary = f"Wiegand26 FC={fc} Card={card}"
        details["facility_code"] = fc
        details["card_number"] = card
    elif key_type == "EM4100" and len(raw_bytes) >= 5:
        cust = raw_bytes[0]
        dec_id = int.from_bytes(raw_bytes[1:5], "big")
        decoded_summary = f"Cust=0x{cust:02X} ID={dec_id:010d}"
        details["customer_id"] = cust
        details["card_id_dec"] = dec_id

    lines = [
        f"RFID: {key_type or 'Unknown'} (125 kHz)",
        f"Hex : {data_hex or '(empty)'}",
    ]
    if decoded_summary:
        lines.append(f"Dec : {decoded_summary}")

    return {
        "kind": "lfrfid",
        "key_type": key_type,
        "data_hex": data_hex,
        "decoded": decoded_summary,
        **details,
        "compact_text": "\n".join(lines),
    }


def parse_ir_capture(content: str | bytes) -> Dict[str, object]:
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    signals: List[Dict[str, object]] = []
    cur: Dict[str, object] = {}

    def _flush() -> None:
        nonlocal cur
        if cur.get("name"):
            signals.append(cur)
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
        k, v = s.split(":", 1)
        k = k.strip()
        v = v.strip()
        if k in ("Filetype", "Version"):
            continue
        if k == "name":
            if cur.get("name"):
                _flush()
            cur["name"] = v
        elif k in ("type", "protocol", "address", "command"):
            cur[k] = v
        elif k == "frequency":
            try:
                cur["frequency"] = int(v)
            except ValueError:
                pass
        elif k == "data":
            cur["pulses"] = len(v.split())
    _flush()

    protos = sorted({str(sig.get("protocol") or "RAW") for sig in signals})
    lines = [
        f"IR  : {len(signals)} button(s) [{', '.join(protos[:3]) or 'none'}]",
    ]
    for sig in signals[:6]:
        nm = str(sig.get("name", "?"))[:12]
        if sig.get("type") == "parsed":
            proto = str(sig.get("protocol", "?"))
            addr = " ".join(p for p in str(sig.get("address", "")).split() if p != "00") or "00"
            cmd = " ".join(p for p in str(sig.get("command", "")).split() if p != "00") or "00"
            lines.append(f"  {nm:<12} {proto} A:{addr} C:{cmd}")
        else:
            khz = int(sig.get("frequency", 38000)) / 1000.0
            pls = int(sig.get("pulses", 0))
            lines.append(f"  {nm:<12} RAW {pls}p @{khz:.0f}kHz")
    if len(signals) > 6:
        lines.append(f"  ... +{len(signals) - 6} more button(s)")

    return {
        "kind": "infrared",
        "signal_count": len(signals),
        "protocols": protos,
        "signals": signals[:16],
        "compact_text": "\n".join(lines),
    }


def parse_ibutton_capture(content: str | bytes) -> Dict[str, object]:
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    key_type = ""
    data_hex = ""
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        k, v = s.split(":", 1)
        k, v = k.strip(), v.strip()
        if k == "Key type":
            key_type = v
        elif k == "Data":
            data_hex = v

    lines = [
        f"iBtn: {key_type or 'Unknown'}",
        f"Data: {data_hex or '(empty)'}",
    ]
    raw_bytes = [int(t, 16) for t in data_hex.split() if len(t) == 2]
    if key_type == "Dallas" and len(raw_bytes) == 8:
        serial = "".join(f"{b:02X}" for b in reversed(raw_bytes[1:7]))
        lines.append(f"Fam : 0x{raw_bytes[0]:02X}  SN:{serial}  CRC:0x{raw_bytes[7]:02X}")

    return {
        "kind": "ibutton",
        "key_type": key_type,
        "data_hex": data_hex,
        "compact_text": "\n".join(lines),
    }


def parse_pcap_capture(
    content: bytes,
    tshark_runner: Optional[Callable[..., subprocess.CompletedProcess]] = None,
) -> Dict[str, object]:
    """Summarizes a `.pcap` / `.pcapng` capture using Kali's `tshark` (with stdlib header fallback)."""
    pkt_count = 0
    link_type = 0
    if len(content) >= 24 and content[:4] in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4"):
        endian = "<" if content[:4] == b"\xd4\xc3\xb2\xa1" else ">"
        link_type = struct.unpack(endian + "I", content[20:24])[0]
        off = 24
        n = len(content)
        while off + 16 <= n:
            incl_len = struct.unpack(endian + "I", content[off + 8 : off + 12])[0]
            pkt_count += 1
            off += 16 + incl_len

    lines = [f"PCAP: {len(content)}B  pkts={pkt_count or '?'}  dlt={link_type}"]
    tshark_bin = shutil.which("tshark")
    tshark_lines: List[str] = []

    if tshark_runner is not None or tshark_bin:
        exec_fn = tshark_runner or subprocess.run
        with tempfile.TemporaryDirectory(prefix="z2z-pcap-") as tmp:
            pcap_path = Path(tmp) / "capture.pcap"
            pcap_path.write_bytes(content)
            try:
                proc = exec_fn(
                    [tshark_bin or "tshark", "-n", "-r", str(pcap_path), "-q", "-z", "io,phs"],
                    capture_output=True,
                    text=True,
                    timeout=20.0,
                )
                for raw_ln in (proc.stdout or "").splitlines():
                    s = raw_ln.strip()
                    if "frames:" in s and "bytes:" in s:
                        tshark_lines.append("  " + " ".join(s.split())[:46])
                    if len(tshark_lines) >= 5:
                        break
            except Exception as exc:
                tshark_lines.append(f"  tshark err: {exc}"[:48])

    if tshark_lines:
        lines.append("tshark Protocol Hierarchy:")
        lines.extend(tshark_lines)

    return {
        "kind": "pcap",
        "bytes": len(content),
        "packet_count": pkt_count,
        "link_type": link_type,
        "tshark_used": bool(tshark_lines),
        "compact_text": "\n".join(lines),
    }


def inspect_capture_bytes(path_hint: str, content: bytes) -> Dict[str, object]:
    """Auto-dispatches capture analysis by extension or `Filetype:` / magic header."""
    low = path_hint.lower()
    head = content[:160].decode("utf-8", errors="replace")
    base = os.path.basename(path_hint.rstrip("/")) or "capture"

    if low.endswith((".pcap", ".pcapng", ".cap")) or content[:4] in (
        b"\xd4\xc3\xb2\xa1",
        b"\xa1\xb2\xc3\xd4",
        b"\x0a\x0d\x0d\x0a",
    ):
        out = parse_pcap_capture(content)
    elif low.endswith(".nfc") or "Flipper NFC device" in head:
        out = parse_nfc_capture(content)
    elif low.endswith(".rfid") or "Flipper RFID key" in head:
        out = parse_rfid_capture(content)
    elif low.endswith(".ir") or "IR signals file" in head:
        out = parse_ir_capture(content)
    elif low.endswith(".ibtn") or "Flipper iButton key" in head:
        out = parse_ibutton_capture(content)
    else:
        rep = parse_and_analyze_sub(content)
        out = rep.to_dict()
        out["kind"] = "subghz"
        text = content.decode("utf-8", errors="replace")
        extra_bits = ""
        extra_te = ""
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("Bit:"):
                extra_bits = s.split(":", 1)[1].strip()
            elif s.startswith("TE:"):
                extra_te = s.split(":", 1)[1].strip()
        compact = rep.format_compact()
        if extra_bits and "Bit" not in compact:
            compact += f"\nBits: {extra_bits}" + (f" (TE {extra_te}us)" if extra_te else "")
        out["compact_text"] = compact

    out["compact_text"] = f"File: {base}\n" + str(out["compact_text"])
    return out


def list_remote_captures(
    rpc: FlipperRpcClient,
    kind: Optional[str] = None,
    limit: int = 24,
    categories: Sequence[Tuple[str, str, str]] = CAPTURE_DIRS,
) -> List[Dict[str, object]]:
    """Lists capture files across Flipper's `/ext/{subghz,nfc,lfrfid,infrared,ibutton}` folders."""
    rows: List[Dict[str, object]] = []
    filt = (kind or "").strip().lower()
    for cat_key, remote_dir, ext in categories:
        if filt and filt not in (cat_key, ext.lstrip(".")):
            continue
        entries = rpc.storage_list(remote_dir, include_md5=False)
        matched = [
            e
            for e in entries
            if not e.is_dir and e.name and not e.name.startswith(".") and e.name.lower().endswith(ext)
        ]
        matched.sort(key=lambda e: e.name, reverse=True)
        for item in matched[:limit]:
            rows.append(
                {
                    "kind": cat_key,
                    "name": item.name,
                    "path": str(PurePosixPath(remote_dir) / item.name),
                    "size": item.size,
                }
            )
    return rows[:limit]
