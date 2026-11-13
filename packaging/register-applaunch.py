#!/usr/bin/env python3
"""Idempotently registers or removes `Zero2Zero` in M5Stack CardputerZero APPLaunch.

Matches `kali-on-cardputerzero/scripts/configure-kali-launcher.py`:
  - Uses `zero2zero.desktop` (not `kali-*.desktop`).
  - Generates a 100x100 RGB PNG launcher icon using stdlib `zlib` only.
  - Maintains 1:1 row parity in `/usr/share/APPLaunch/preinstalled-desktop-apps.tsv`.
  - Updates FNV-1a toggle key in `/home/kali/.config/cardputerzero/config.json` & `/etc/skel`.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import struct
import zlib

DESKTOP_FILENAME = "zero2zero.desktop"
APP_NAME = "Zero2Zero"
ICON_FILENAME = "zero2zero_100.png"
ICON_REL = f"share/images/{ICON_FILENAME}"
EXEC_PATH = "/usr/bin/z2z-ui"
Z2Z_MTIME = 1767225700  # Sorts right after the 9 core Kali entries, ahead of M5 entries


def desktop_config_key(filename: str) -> str:
    raw = filename.encode("utf-8")
    sanitized = [
        chr(b) if (48 <= b <= 57) or (65 <= b <= 90) or (97 <= b <= 122) else "_"
        for b in raw
    ]
    h = 2166136261
    for b in raw:
        h ^= b
        h = (h * 16777619) & 0xFFFFFFFF
    return f"app_desktop_{''.join(sanitized)}_{h:08x}"


def write_z2z_icon_png(out_path: Path) -> None:
    w, h = 100, 100
    bg = (12, 22, 42)
    fg = (255, 142, 44)  # Flipper orange
    accent = (56, 196, 255)  # Kali cyan

    # 8x8 bidirectional link glyph
    glyph = [0x18, 0x3C, 0x66, 0xFF, 0xFF, 0x66, 0x3C, 0x18]

    raw = bytearray()
    for y in range(h):
        raw.append(0)  # PNG filter type 0
        for x in range(w):
            r, g, b = bg
            # Rounded card border
            if x in (3, 96) and 8 <= y <= 91:
                r, g, b = accent
            elif y in (3, 96) and 8 <= x <= 91:
                r, g, b = accent
            # Centered 8x8 glyph scaled 6x (48x48 at x=26..73, y=20..67)
            elif 26 <= x < 74 and 20 <= y < 68:
                gx = (x - 26) // 6
                gy = (y - 20) // 6
                if (glyph[gy] >> (7 - gx)) & 1:
                    r, g, b = fg if gx >= 4 else accent
            # Bottom accent bar
            elif 24 <= x <= 76 and 78 <= y <= 83:
                r, g, b = fg if x >= 50 else accent
            raw.extend((r, g, b))

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + ctype
            + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(png)


def _chown_match_owner(target: Path, owner_ref: Path) -> None:
    try:
        st = owner_ref.stat()
        os.chown(target, st.st_uid, st.st_gid)
    except OSError:
        pass


def _update_config_json(cfg_path: Path, key: str, remove: bool) -> None:
    if not cfg_path.parent.exists() and remove:
        return
    owner_ref = cfg_path.parent.parent  # e.g. /home/kali/.config or /etc/skel/.config
    user_home = owner_ref.parent if owner_ref.name == ".config" else owner_ref
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    data = {}
    if cfg_path.is_file():
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if remove:
        data.pop(key, None)
    else:
        data.setdefault(key, 1)
    cfg_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if user_home.exists():
        _chown_match_owner(owner_ref, user_home)
        _chown_match_owner(cfg_path.parent, user_home)
        _chown_match_owner(cfg_path, user_home)


def register(root: Path, remove: bool = False) -> bool:
    applaunch_dir = root / "usr/share/APPLaunch"
    if not applaunch_dir.exists() and not os.environ.get("Z2Z_FORCE_APPLAUNCH"):
        return False

    apps_dir = applaunch_dir / "applications"
    imgs_dir = applaunch_dir / "share/images"
    tsv_path = applaunch_dir / "preinstalled-desktop-apps.tsv"
    dpath = apps_dir / DESKTOP_FILENAME
    ipath = imgs_dir / ICON_FILENAME
    cfg_key = desktop_config_key(DESKTOP_FILENAME)
    tsv_row = f"{DESKTOP_FILENAME}\t{APP_NAME}\t{ICON_REL}\t{EXEC_PATH}\tfalse\ttrue"

    if remove:
        dpath.unlink(missing_ok=True)
        ipath.unlink(missing_ok=True)
        if tsv_path.is_file():
            kept = [
                ln
                for ln in tsv_path.read_text(encoding="utf-8").splitlines()
                if not ln.startswith(DESKTOP_FILENAME + "\t")
            ]
            tsv_path.write_text("\n".join(kept) + "\n", encoding="utf-8")
    else:
        apps_dir.mkdir(parents=True, exist_ok=True)
        imgs_dir.mkdir(parents=True, exist_ok=True)
        write_z2z_icon_png(ipath)
        dpath.write_text(
            "[Desktop Entry]\n"
            f"Name={APP_NAME}\n"
            f"Exec={EXEC_PATH}\n"
            "Terminal=false\n"
            "Sysplause=true\n"
            f"Icon={ICON_REL}\n"
            "Type=Application\n",
            encoding="utf-8",
        )
        os.chmod(dpath, 0o644)
        os.utime(dpath, (Z2Z_MTIME, Z2Z_MTIME))

        lines = []
        if tsv_path.is_file():
            lines = [
                ln
                for ln in tsv_path.read_text(encoding="utf-8").splitlines()
                if not ln.startswith(DESKTOP_FILENAME + "\t")
            ]
        else:
            lines = ["# filename\tName\tIcon\tExec\tTerminal\tSysplause"]
        lines.append(tsv_row)
        tsv_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    for cfg in (
        root / "home/kali/.config/cardputerzero/config.json",
        root / "etc/skel/.config/cardputerzero/config.json",
    ):
        if cfg.parent.parent.exists():
            _update_config_json(cfg, cfg_key, remove=remove)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Register Zero2Zero in CardputerZero APPLaunch")
    ap.add_argument("--root", default="/", help="Filesystem root directory")
    ap.add_argument("--remove", action="store_true", help="Remove launcher entry")
    args = ap.parse_args()
    register(Path(args.root).resolve(), remove=args.remove)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
