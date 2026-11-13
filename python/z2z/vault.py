"""Incremental MD5-deduplicated `/ext/` capture vault on Kali `ext4` storage."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import socket
from typing import Dict, List, Optional, Sequence, Tuple

from .rpc_client import FlipperRpcClient

DEFAULT_VAULT_CATEGORIES: Tuple[str, ...] = (
    "/ext/nfc",
    "/ext/lfrfid",
    "/ext/subghz",
    "/ext/ibutton",
    "/ext/infrared",
)


def default_vault_dir() -> Path:
    env_dir = os.environ.get("Z2Z_VAULT_DIR")
    if env_dir:
        base = Path(env_dir)
    elif os.geteuid() == 0:
        base = Path("/var/lib/zero2zero/vault")
    else:
        xdg = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        base = Path(xdg) / "zero2zero" / "vault"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _try_gpsd_fix() -> Optional[Dict[str, float]]:
    """Reads an instantaneous fix from local `gpsd` (`127.0.0.1:2947`) if active."""
    try:
        with socket.create_connection(("127.0.0.1", 2947), timeout=0.35) as sock:
            sock.settimeout(0.45)
            sock.sendall(b'?WATCH={"enable":true,"json":true};\n')
            buf = sock.recv(4096).decode("utf-8", errors="replace")
            for line in buf.splitlines():
                if '"class":"TPV"' in line:
                    obj = json.loads(line)
                    if "lat" in obj and "lon" in obj:
                        return {"lat": float(obj["lat"]), "lon": float(obj["lon"])}
    except Exception:
        pass
    return None


def sync_vault(
    rpc: FlipperRpcClient,
    categories: Sequence[str] = DEFAULT_VAULT_CATEGORIES,
    vault_dir: Optional[Path] = None,
    max_file_bytes: int = 256 * 1024,
) -> Dict[str, object]:
    """Incrementally pulls new or modified Flipper capture files over BLE."""
    base = vault_dir or default_vault_dir()
    index_path = base / "index.json"
    if index_path.exists():
        try:
            index: Dict[str, dict] = json.loads(index_path.read_text(encoding="utf-8"))
        except Exception:
            index = {}
    else:
        index = {}

    gps_fix = _try_gpsd_fix()
    pulled: List[str] = []
    skipped = 0
    bytes_transferred = 0

    for remote_dir in categories:
        entries = rpc.storage_list(remote_dir, include_md5=True)
        cat_name = remote_dir.strip("/").replace("/", "_")
        local_cat = base / cat_name
        local_cat.mkdir(parents=True, exist_ok=True)

        for item in entries:
            if item.is_dir or not item.name or item.name.startswith("."):
                continue
            if item.size > max_file_bytes:
                skipped += 1
                continue
            remote_path = f"{remote_dir.rstrip('/')}/{item.name}"
            prev = index.get(remote_path)
            local_file = local_cat / item.name

            if (
                prev
                and local_file.exists()
                and item.md5sum
                and prev.get("md5sum") == item.md5sum
                and prev.get("size") == item.size
            ):
                skipped += 1
                continue

            blob = rpc.storage_read(remote_path)
            md5_hex = item.md5sum or hashlib.md5(blob).hexdigest()
            sha256_hex = hashlib.sha256(blob).hexdigest()
            local_file.write_bytes(blob)

            rec: Dict[str, object] = {
                "remote_path": remote_path,
                "local_path": str(local_file),
                "size": len(blob),
                "md5sum": md5_hex,
                "sha256": sha256_hex,
                "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            if gps_fix:
                rec["gps"] = gps_fix
            index[remote_path] = rec
            pulled.append(remote_path)
            bytes_transferred += len(blob)

    index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {
        "vault_dir": str(base),
        "pulled_count": len(pulled),
        "pulled": pulled,
        "skipped_count": skipped,
        "bytes_transferred": bytes_transferred,
        "total_indexed": len(index),
        "gps_tagged": bool(gps_fix),
    }
