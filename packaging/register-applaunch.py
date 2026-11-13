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


TINY3X5 = {
    "2": [3, 1, 2, 4, 7],
    "B": [6, 5, 6, 5, 6],
    "E": [7, 4, 6, 4, 7],
    "L": [4, 4, 4, 4, 7],
    "Z": [7, 1, 2, 4, 7],
}


def write_z2z_icon_png(out_path: Path) -> None:
    """Render a 100x100 APPLaunch icon matching kali-on-cardputerzero's visual style."""
    w, h = 100, 100
    bg = (28, 18, 12)
    fg = (255, 142, 44)       # Flipper orange border & glyph
    accent = (64, 212, 255)   # Kali cyan bottom badge
    glyph = [0x08, 0x04, 0xFE, 0x00, 0x00, 0x7F, 0x20, 0x10]  # Bi-directional harpoons (⇌)
    tag = "BLE"

    buf = [[bg for _ in range(w)] for _ in range(h)]

    for y in range(h):
        for x in range(w):
            dx = min(x, w - 1 - x)
            dy = min(y, h - 1 - y)
            if dx + dy < 10:
                buf[y][x] = (0, 0, 0)
            elif x in (2, 3, w - 3, w - 4) or y in (2, 3, h - 3, h - 4):
                buf[y][x] = fg
            elif y == 74 and 10 <= x < w - 10:
                buf[y][x] = (fg[0] // 2, fg[1] // 2, fg[2] // 2)

    # 8x8 main glyph in 5x5 cells on a 6px grid at (26, 16)
    for gy in range(8):
        for gx in range(8):
            if (glyph[gy] >> (7 - gx)) & 1:
                for sy in range(5):
                    for sx in range(5):
                        buf[16 + gy * 6 + sy][26 + gx * 6 + sx] = fg

    # Bottom 3x5 mini-label scaled 2x at y=80
    tw = len(tag) * 8 - 2
    tx0 = (w - tw) // 2
    for idx, ch in enumerate(tag):
        pat = TINY3X5.get(ch)
        if not pat:
            continue
        for ry in range(5):
            for rx in range(3):
                if (pat[ry] >> (2 - rx)) & 1:
                    for sy in range(2):
                        for sx in range(2):
                            px = tx0 + idx * 8 + rx * 2 + sx
                            py = 80 + ry * 2 + sy
                            if 0 <= px < w and 0 <= py < h:
                                buf[py][px] = accent

    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for r, g, b in buf[y]:
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
