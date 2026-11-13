"""Compact Kali Linux system, wireless, and LAN summary collector for CardputerZero + Flipper."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Dict, List

KALI_COMPANION_TOOLS = (
    ("rtl_433", "rtl_433"),
    ("qrencode", "qrencode"),
    ("zbar", "zbarimg"),
    ("libcamera", "cam"),
    ("tshark", "tshark"),
    ("nmap", "nmap"),
    ("libnfc", "nfc-list"),
    ("pm3", "proxmark3"),
)


def get_kali_tools_status() -> Dict[str, bool]:
    return {label: bool(shutil.which(binary)) for label, binary in KALI_COMPANION_TOOLS}


def _read_first_line(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[0].strip()
    except Exception:
        return ""


def get_soc_metrics() -> Dict[str, object]:
    temp_c = 0.0
    raw_t = _read_first_line("/sys/class/thermal/thermal_zone0/temp")
    if raw_t.isdigit():
        temp_c = round(int(raw_t) / 1000.0, 1)

    bat_pct = -1
    bat_status = ""
    ps_root = Path("/sys/class/power_supply")
    if ps_root.is_dir():
        for entry in sorted(ps_root.iterdir()):
            cap = _read_first_line(str(entry / "capacity"))
            if cap.isdigit():
                bat_pct = int(cap)
                bat_status = _read_first_line(str(entry / "status"))
                break

    mem_total_mb = 0
    mem_avail_mb = 0
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            if line.startswith("MemTotal:"):
                mem_total_mb = int(line.split()[1]) // 1024
            elif line.startswith("MemAvailable:"):
                mem_avail_mb = int(line.split()[1]) // 1024
    except Exception:
        pass

    return {
        "temp_c": temp_c,
        "battery_pct": bat_pct,
        "battery_status": bat_status,
        "mem_used_mb": max(0, mem_total_mb - mem_avail_mb),
        "mem_total_mb": mem_total_mb,
    }


def get_interfaces() -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    try:
        raw = subprocess.check_output(
            ["ip", "-j", "addr", "show"], stderr=subprocess.DEVNULL, text=True, timeout=3.0
        )
        for item in json.loads(raw):
            ifname = item.get("ifname", "")
            if ifname == "lo":
                continue
            state = item.get("operstate", "DOWN")
            ipv4 = ""
            for ai in item.get("addr_info", []):
                if ai.get("family") == "inet":
                    ipv4 = f"{ai.get('local')}/{ai.get('prefixlen')}"
                    break
            out.append({"ifname": ifname, "state": state, "ipv4": ipv4})
    except Exception:
        pass
    return out


def get_wifi_aps(limit: int = 8) -> List[Dict[str, str]]:
    aps: List[Dict[str, str]] = []
    try:
        raw = subprocess.check_output(
            ["nmcli", "-t", "-f", "SSID,CHAN,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "no"],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=4.0,
        )
        seen = set()
        for line in raw.splitlines():
            parts = line.split(":")
            if len(parts) < 4:
                continue
            ssid = parts[0].strip() or "<hidden>"
            chan, sig, sec = parts[1].strip(), parts[2].strip(), parts[3].strip()
            if (ssid, chan) in seen:
                continue
            seen.add((ssid, chan))
            aps.append({"ssid": ssid, "chan": chan, "signal": sig, "security": sec})
            if len(aps) >= limit:
                break
    except Exception:
        pass
    return aps


def get_lan_neighbors(limit: int = 10) -> List[Dict[str, str]]:
    peers: List[Dict[str, str]] = []
    try:
        raw = subprocess.check_output(
            ["ip", "-4", "neigh", "show"], stderr=subprocess.DEVNULL, text=True, timeout=2.5
        )
        for line in raw.splitlines():
            toks = line.split()
            if not toks or "FAILED" in toks:
                continue
            ip_addr = toks[0]
            mac = ""
            dev = ""
            for idx, t in enumerate(toks):
                if t == "lladdr" and idx + 1 < len(toks):
                    mac = toks[idx + 1]
                elif t == "dev" and idx + 1 < len(toks):
                    dev = toks[idx + 1]
            peers.append({"ip": ip_addr, "mac": mac, "dev": dev})
            if len(peers) >= limit:
                break
    except Exception:
        pass
    return peers


def build_net_report() -> Dict[str, object]:
    soc = get_soc_metrics()
    ifaces = get_interfaces()
    aps = get_wifi_aps()
    peers = get_lan_neighbors()
    tools = get_kali_tools_status()

    # Format 22-column summary tailored for Flipper's 128x64 TextBox
    lines = [
        f"CZ {soc['temp_c']}C RAM:{soc['mem_used_mb']}/{soc['mem_total_mb']}M",
    ]
    if int(soc["battery_pct"]) >= 0:
        lines[0] += f" B:{soc['battery_pct']}%"
    for ifc in ifaces[:4]:
        lines.append(f"{ifc['ifname']}: {ifc['ipv4'] or ifc['state']}")
    present_tools = [k for k, ok in tools.items() if ok]
    if present_tools:
        lines.append("Tools: " + " ".join(present_tools[:4]))
        if len(present_tools) > 4:
            lines.append("  +" + " ".join(present_tools[4:]))
    if aps:
        lines.append(f"-- WiFi ({len(aps)}) --")
        for ap in aps[:5]:
            lines.append(f"c{ap['chan']} {ap['signal']}% {ap['ssid'][:12]}")
    if peers:
        lines.append(f"-- Peers ({len(peers)}) --")
        for pr in peers[:4]:
            lines.append(f"{pr['ip']} {pr['dev']}")

    return {
        "soc": soc,
        "interfaces": ifaces,
        "wifi_aps": aps,
        "neighbors": peers,
        "kali_tools": tools,
        "compact_text": "\n".join(lines),
    }


if __name__ == "__main__":
    rep = build_net_report()
    if "--compact" in os.sys.argv:
        print(rep["compact_text"])
    else:
        print(json.dumps(rep, indent=2))
