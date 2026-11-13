"""NFC format translators (`.nfc` <-> `.mfd`/`.eml`), NDEF builder, `proxmark3` & `qrencode` bridges."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
from typing import Callable, Dict, List, Optional, Sequence, Tuple

SubprocessRunner = Callable[..., subprocess.CompletedProcess]

# Standard NFC Forum URI identifier prefixes (longest-match first when encoding)
NDEF_URI_PREFIX_TABLE: Tuple[str, ...] = (
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

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _parse_nfc_rows(
    content: str | bytes,
) -> Tuple[Dict[str, str], str, Dict[int, List[Optional[int]]]]:
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    meta: Dict[str, str] = {}
    unit_kind = "block"
    rows: Dict[int, List[Optional[int]]] = {}

    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        k, v = s.split(":", 1)
        k = k.strip()
        v = v.strip()
        if k.startswith("Block ") or k.startswith("Page "):
            parts = k.split()
            if len(parts) == 2 and parts[1].isdigit():
                idx = int(parts[1])
                if k.startswith("Page "):
                    unit_kind = "page"
                vals: List[Optional[int]] = []
                for tok in v.split():
                    if tok == "??":
                        vals.append(None)
                    else:
                        try:
                            vals.append(int(tok, 16))
                        except ValueError:
                            pass
                rows[idx] = vals
        else:
            meta[k] = v

    return meta, unit_kind, rows


def nfc_to_mfd(content: str | bytes, fill_unknown: int = 0x00) -> bytes:
    """Converts a Flipper `.nfc` dump into a contiguous `.mfd`/`.bin` image for `libnfc`/`proxmark3`."""
    _meta, unit_kind, rows = _parse_nfc_rows(content)
    if not rows:
        raise ValueError("Flipper `.nfc` dump contains no Block or Page data rows")
    row_width = 4 if unit_kind == "page" else 16
    max_idx = max(rows.keys())
    out = bytearray()
    fill_b = fill_unknown & 0xFF
    for idx in range(max_idx + 1):
        r = rows.get(idx, [])
        for col in range(row_width):
            val = r[col] if col < len(r) else None
            out.append(fill_b if val is None else (val & 0xFF))
    return bytes(out)


def nfc_to_eml(content: str | bytes) -> str:
    """Converts a Flipper `.nfc` dump into Proxmark3 `.eml` ASCII hex lines (preserving `??` as `00`)."""
    _meta, unit_kind, rows = _parse_nfc_rows(content)
    if not rows:
        raise ValueError("Flipper `.nfc` dump contains no Block or Page data rows")
    row_width = 4 if unit_kind == "page" else 16
    max_idx = max(rows.keys())
    lines: List[str] = []
    for idx in range(max_idx + 1):
        r = rows.get(idx, [])
        hex_bytes = [
            "00" if (col >= len(r) or r[col] is None) else f"{int(r[col]) & 0xFF:02X}"
            for col in range(row_width)
        ]
        lines.append("".join(hex_bytes))
    return "\n".join(lines) + "\n"


def mfd_to_nfc(
    mfd_bytes: bytes,
    uid: Optional[str] = None,
    atqa: str = "",
    sak: str = "",
) -> str:
    """Converts a binary `.mfd`/`.bin` tag dump into a Flipper `.nfc` (`Version: 4`) file."""
    total = len(mfd_bytes)
    if total == 0:
        raise ValueError("Empty binary NFC dump")

    # Mifare Classic Mini (320B), 1K (1024B), 4K (4096B)
    if total in (320, 1024, 4096) or (total % 16 == 0 and total >= 320):
        mf_type = "1K"
        default_sak = "08"
        default_atqa = "00 04"
        if total == 320:
            mf_type = "Mini"
            default_sak = "09"
        elif total == 4096:
            mf_type = "4K"
            default_sak = "18"
            default_atqa = "00 02"

        uid_str = uid or " ".join(f"{b:02X}" for b in mfd_bytes[0:4])
        lines = [
            "Filetype: Flipper NFC device",
            "Version: 4",
            "Device type: Mifare Classic",
            f"UID: {uid_str}",
            f"ATQA: {atqa or default_atqa}",
            f"SAK: {sak or default_sak}",
            f"Mifare Classic type: {mf_type}",
            "Data format version: 2",
        ]
        n_blocks = total // 16
        for bidx in range(n_blocks):
            blk = mfd_bytes[bidx * 16 : (bidx + 1) * 16]
            lines.append(f"Block {bidx}: " + " ".join(f"{b:02X}" for b in blk))
        return "\n".join(lines) + "\n"

    if total % 4 != 0 or total < 16:
        raise ValueError(
            f"Unsupported binary dump size {total} bytes (expected 320/1024/4096B Mifare or 4B-aligned NTAG)"
        )

    n_pages = total // 4
    if n_pages == 45:
        ntag_subtype = "NTAG213"
        ver_sz = "0F"
    elif n_pages == 135:
        ntag_subtype = "NTAG215"
        ver_sz = "11"
    elif n_pages == 231:
        ntag_subtype = "NTAG216"
        ver_sz = "13"
    else:
        ntag_subtype = "Mifare Ultralight"
        ver_sz = "0B"

    if uid:
        uid_str = uid
    else:
        # 7-byte UID stored in Page 0[0..2] + Page 1[0..3]
        uid7 = mfd_bytes[0:3] + mfd_bytes[4:8]
        uid_str = " ".join(f"{b:02X}" for b in uid7)

    # Flipper SDK 1.5.1 `mf_ultralight_load` (`libnfc.a`) strictly requires
    # `Signature`, `Mifare version`, and `Counter 0..2` / `Tearing 0..2` on `Version: 4`!
    lines = [
        "Filetype: Flipper NFC device",
        "Version: 4",
        "Device type: NTAG/Ultralight",
        f"UID: {uid_str}",
        f"ATQA: {atqa or '00 44'}",
        f"SAK: {sak or '00'}",
        "Data format version: 2",
        f"NTAG/Ultralight type: {ntag_subtype}",
        "Signature: " + " ".join(["00"] * 32),
        f"Mifare version: 00 04 04 02 01 00 {ver_sz} 03",
        "Counter 0: 0",
        "Tearing 0: 00",
        "Counter 1: 0",
        "Tearing 1: 00",
        "Counter 2: 0",
        "Tearing 2: 00",
        f"Pages total: {n_pages}",
        f"Pages read: {n_pages}",
    ]
    for pidx in range(n_pages):
        pg = mfd_bytes[pidx * 4 : (pidx + 1) * 4]
        lines.append(f"Page {pidx}: " + " ".join(f"{b:02X}" for b in pg))
    lines.append("Failed authentication attempts: 0")
    return "\n".join(lines) + "\n"


def eml_to_nfc(
    eml_content: str | bytes,
    uid: Optional[str] = None,
    atqa: str = "",
    sak: str = "",
) -> str:
    """Converts a Proxmark3 `.eml` ASCII dump into a Flipper `.nfc` (`Version: 4`) file."""
    text = (
        eml_content.decode("utf-8", errors="replace")
        if isinstance(eml_content, bytes)
        else eml_content
    )
    raw_lines = [
        ln.strip().replace(" ", "")
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    if not raw_lines:
        raise ValueError("Empty `.eml` file")

    first_bytes = len(raw_lines[0]) // 2
    if first_bytes not in (4, 16):
        raise ValueError(f"Unsupported `.eml` line width ({len(raw_lines[0])} hex chars)")

    parsed_rows: List[List[Optional[int]]] = []
    concrete_buf = bytearray()
    has_unknown = False

    for ln in raw_lines:
        if len(ln) < first_bytes * 2:
            continue
        row: List[Optional[int]] = []
        for i in range(0, first_bytes * 2, 2):
            pair = ln[i : i + 2]
            if pair in ("??", "--", "**"):
                row.append(None)
                concrete_buf.append(0)
                has_unknown = True
            else:
                val = int(pair, 16)
                row.append(val)
                concrete_buf.append(val)
        parsed_rows.append(row)

    base_nfc = mfd_to_nfc(bytes(concrete_buf), uid=uid, atqa=atqa, sak=sak)
    if not has_unknown:
        return base_nfc

    prefix = "Page" if first_bytes == 4 else "Block"
    out_lines: List[str] = []
    for ln in base_nfc.splitlines():
        if ln.startswith(f"{prefix} "):
            idx_s = ln.split(":", 1)[0].split()[1]
            idx = int(idx_s)
            if idx < len(parsed_rows):
                toks = ["??" if b is None else f"{b:02X}" for b in parsed_rows[idx]]
                out_lines.append(f"{prefix} {idx}: " + " ".join(toks))
                continue
        out_lines.append(ln)
    return "\n".join(out_lines) + "\n"


def convert_nfc_bytes(src_bytes: bytes, src_hint: str, dst_hint: str) -> bytes:
    """Converts between `.nfc`, `.mfd`/`.bin`, and `.eml` based on filename extensions."""
    s_ext = Path(src_hint.lower()).suffix
    d_ext = Path(dst_hint.lower()).suffix

    if s_ext == ".nfc":
        if d_ext in (".mfd", ".bin", ".dmp"):
            return nfc_to_mfd(src_bytes)
        if d_ext == ".eml":
            return nfc_to_eml(src_bytes).encode("utf-8")
        if d_ext == ".nfc":
            return src_bytes
    elif s_ext in (".mfd", ".bin", ".dmp"):
        nfc_txt = mfd_to_nfc(src_bytes)
        if d_ext == ".nfc":
            return nfc_txt.encode("utf-8")
        if d_ext == ".eml":
            return nfc_to_eml(nfc_txt).encode("utf-8")
    elif s_ext == ".eml":
        nfc_txt = eml_to_nfc(src_bytes)
        if d_ext == ".nfc":
            return nfc_txt.encode("utf-8")
        if d_ext in (".mfd", ".bin", ".dmp"):
            return nfc_to_mfd(nfc_txt)

    raise ValueError(f"Unsupported NFC conversion: {s_ext or src_hint!r} -> {d_ext or dst_hint!r}")


def _encode_ndef_record(payload: str, record_type: str = "auto") -> bytes:
    mode = record_type.lower().strip()
    is_uri = mode == "uri" or (
        mode == "auto"
        and any(
            payload.startswith(pfx)
            for pfx in ("https://", "http://", "tel:", "mailto:", "smb://", "sftp://", "urn:")
        )
    )

    if is_uri:
        best_code = 0
        best_pfx = ""
        for code, pfx in enumerate(NDEF_URI_PREFIX_TABLE):
            if pfx and payload.startswith(pfx) and len(pfx) > len(best_pfx):
                best_code = code
                best_pfx = pfx
        body = bytes([best_code]) + payload[len(best_pfx) :].encode("utf-8")
        rec_type = b"U"
    else:
        lang = b"en"
        txt_bytes = payload.encode("utf-8")
        body = bytes([len(lang) & 0x3F]) + lang + txt_bytes
        rec_type = b"T"

    if len(body) <= 255:
        # MB=1, ME=1, CF=0, SR=1, IL=0, TNF=0x01 (Well-Known) -> 0xD1
        return bytes([0xD1, len(rec_type), len(body)]) + rec_type + body
    # Non-short record: MB=1, ME=1, SR=0, TNF=0x01 -> 0xC1
    return bytes([0xC1, len(rec_type)]) + len(body).to_bytes(4, "big") + rec_type + body


def build_ndef_nfc_tag(
    payload: str,
    record_type: str = "auto",
    uid: Optional[str] = None,
) -> str:
    """Synthesizes a complete Flipper NTAG215 `.nfc` file containing an NDEF URI or Text record."""
    if not payload:
        raise ValueError("NDEF payload must not be empty")

    ndef_msg = _encode_ndef_record(payload, record_type=record_type)
    if len(ndef_msg) < 0xFF:
        tlv = bytes([0x03, len(ndef_msg)]) + ndef_msg + b"\xFE"
    else:
        tlv = b"\x03\xFF" + len(ndef_msg).to_bytes(2, "big") + ndef_msg + b"\xFE"

    max_user_bytes = 126 * 4  # Pages 4..129 on NTAG215 (504 bytes)
    if len(tlv) > max_user_bytes:
        raise ValueError(f"NDEF TLV ({len(tlv)} bytes) exceeds NTAG215 capacity ({max_user_bytes}B)")

    if uid:
        uid_bytes = [int(x, 16) for x in uid.replace(":", " ").split() if x]
        if len(uid_bytes) != 7:
            raise ValueError("NTAG215 UID must contain 7 hex bytes (e.g. '04 A1 B2 C3 D4 E5 F6')")
    else:
        digest = hashlib.sha256(payload.encode("utf-8")).digest()
        uid_bytes = [0x04, digest[0], digest[1], digest[2], digest[3], digest[4], digest[5]]

    bcc0 = 0x88 ^ uid_bytes[0] ^ uid_bytes[1] ^ uid_bytes[2]
    bcc1 = uid_bytes[3] ^ uid_bytes[4] ^ uid_bytes[5] ^ uid_bytes[6]

    pages: List[bytes] = [
        bytes([uid_bytes[0], uid_bytes[1], uid_bytes[2], bcc0]),  # Page 0: UID0..2 + BCC0
        bytes([uid_bytes[3], uid_bytes[4], uid_bytes[5], uid_bytes[6]]),  # Page 1: UID3..6
        bytes([bcc1, 0x48, 0x00, 0x00]),  # Page 2: BCC1 + Internal + Lock
        bytes([0xE1, 0x10, 0x3E, 0x00]),  # Page 3: Capability Container (NDEF 496B R/W)
    ]

    padded_user = tlv + b"\x00" * (max_user_bytes - len(tlv))
    for off in range(0, max_user_bytes, 4):
        pages.append(padded_user[off : off + 4])

    # NTAG215 configuration & trailer pages (130..134)
    pages.extend(
        [
            bytes([0x00, 0x00, 0x00, 0xBD]),
            bytes([0x04, 0x00, 0x00, 0xFF]),
            bytes([0x00, 0x05, 0x00, 0x00]),
            bytes([0xFF, 0xFF, 0xFF, 0xFF]),
            bytes([0x00, 0x00, 0x00, 0x00]),
        ]
    )

    uid_str = " ".join(f"{b:02X}" for b in uid_bytes)
    lines = [
        "Filetype: Flipper NFC device",
        "Version: 4",
        "Device type: NTAG/Ultralight",
        f"UID: {uid_str}",
        "ATQA: 00 44",
        "SAK: 00",
        "Data format version: 2",
        "NTAG/Ultralight type: NTAG215",
        "Signature: " + " ".join(["00"] * 32),
        "Mifare version: 00 04 04 02 01 00 11 03",
        "Counter 0: 0",
        "Tearing 0: 00",
        "Counter 1: 0",
        "Tearing 1: 00",
        "Counter 2: 0",
        "Tearing 2: 00",
        "Pages total: 135",
        "Pages read: 135",
        "Failed authentication attempts: 0",
    ]
    for idx, pg in enumerate(pages):
        lines.append(f"Page {idx}: " + " ".join(f"{b:02X}" for b in pg))
    return "\n".join(lines) + "\n"


def inspect_nfc_with_proxmark3(
    content: str | bytes,
    runner: Optional[SubprocessRunner] = None,
    timeout_sec: float = 4.5,
) -> Dict[str, object]:
    """Runs Kali's `proxmark3 -c 'hf mf view -f ...'` offline against a Flipper `.nfc` dump."""
    pm3_bin = shutil.which("proxmark3")
    if runner is None and not pm3_bin:
        return {
            "available": False,
            "summary": "",
            "hint": "proxmark3 not installed (`sudo cz-kali-pkgs install wireless` or `apt install proxmark3`)",
        }

    meta, unit_kind, rows = _parse_nfc_rows(content)
    if not rows:
        return {"available": True, "summary": ""}

    mfd = nfc_to_mfd(content)
    exec_fn = runner or subprocess.run
    sub_cmd = "hf mfu view -f" if unit_kind == "page" else "hf mf view -f"

    with tempfile.TemporaryDirectory(prefix="z2z-pm3-") as tmp:
        dump_path = Path(tmp) / "dump.bin"
        dump_path.write_bytes(mfd)
        cmd = [pm3_bin or "proxmark3", "-c", f"{sub_cmd} {dump_path}"]
        try:
            proc = exec_fn(
                cmd,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=max(1.0, float(timeout_sec)),
            )
        except Exception as exc:
            return {"available": True, "summary": "", "error": str(exc)}

    clean_out = _ANSI_RE.sub("", proc.stdout or "")
    useful: List[str] = []
    for raw_ln in clean_out.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        if any(
            ln.startswith(prefix)
            for prefix in ("[=]   ", "[+] ", "[!] ")
        ) and not ln.startswith("[=] Session log"):
            trimmed = re.sub(r"^\[[=+!]\]\s*", "", ln).strip()
            if trimmed and not trimmed.startswith("---"):
                useful.append(trimmed[:48])
        if len(useful) >= 4:
            break

    return {
        "available": True,
        "returncode": proc.returncode,
        "device_type": meta.get("Device type", ""),
        "summary": "\n".join(f"pm3: {u}" for u in useful),
        "raw_output": clean_out[:2048],
    }


def _matrix_rows_to_utf8(rows: Sequence[str]) -> str:
    """Renders `'1'`/`'0'` QR matrix rows using compact UTF-8 half-blocks (`▀`, `▄`, `█`, ` `)."""
    if not rows:
        return ""
    width = len(rows[0])
    out_lines: List[str] = []
    for y in range(0, len(rows), 2):
        top = rows[y]
        bot = rows[y + 1] if (y + 1) < len(rows) else ("0" * width)
        chars: List[str] = []
        for x in range(width):
            t = top[x] == "1"
            b = bot[x] == "1"
            if t and b:
                chars.append("█")
            elif t:
                chars.append("▀")
            elif b:
                chars.append("▄")
            else:
                chars.append(" ")
        out_lines.append("".join(chars))
    return "\n".join(out_lines)


def encode_qr_with_qrencode(
    text: str,
    runner: Optional[SubprocessRunner] = None,
    timeout_sec: float = 3.0,
) -> Dict[str, object]:
    """Generates a compact 1-bit QR matrix & UTF-8 terminal block via Kali's `qrencode`."""
    payload = text.strip()
    if not payload:
        raise ValueError("QR payload must not be empty")

    qr_bin = shutil.which("qrencode")
    if runner is None and not qr_bin:
        return {
            "ok": False,
            "available": False,
            "error": "qrencode not found in PATH (`sudo apt install qrencode`)",
        }

    exec_fn = runner or subprocess.run
    cmd = [qr_bin or "qrencode", "-t", "ASCII", "-m", "1", "-o", "-", payload]
    proc = exec_fn(
        cmd,
        capture_output=True,
        text=True,
        timeout=max(1.0, float(timeout_sec)),
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "qrencode failed").strip()
        return {"ok": False, "available": True, "error": err}

    rows: List[str] = []
    for raw_ln in (proc.stdout or "").splitlines():
        ln = raw_ln.rstrip("\r\n")
        if not ln:
            continue
        # `qrencode -t ASCII` emits `##` per dark module and `  ` per light module
        if len(ln) % 2 != 0:
            ln += " "
        bits = "".join("1" if ln[i : i + 2] == "##" else "0" for i in range(0, len(ln), 2))
        if bits:
            rows.append(bits)

    if not rows:
        return {"ok": False, "available": True, "error": "qrencode produced empty matrix"}

    size = len(rows[0])
    return {
        "ok": True,
        "available": True,
        "payload": payload,
        "size": size,
        "rows": rows,
        "flat_bits": "".join(rows),
        "utf8": _matrix_rows_to_utf8(rows),
    }
