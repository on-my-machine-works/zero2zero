"""Flipper Zero `.sub` Sub-GHz RAW waveform parser, `.ook` adapter, and Kali `rtl_433` bridge."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from typing import Callable, Dict, List, Optional, Sequence, Tuple


# Flipper's `flipper_format` parser expects <= 512 integers per `RAW_Data:` line.
MAX_RAW_VALUES_PER_LINE = 512

SubprocessRunner = Callable[..., subprocess.CompletedProcess]


@dataclass
class DecodedFrame:
    pulses: int
    te_us: int
    bits_pwm: str
    hex_pwm: str
    bits_inv: str
    hex_inv: str


@dataclass
class SubGhzReport:
    filetype: str = ""
    frequency: int = 433920000
    preset: str = "FuriHalSubGhzPresetOok650Async"
    protocol: str = ""
    key_hex: str = ""
    raw_count: int = 0
    duration_ms: float = 0.0
    frame_count: int = 0
    estimated_te_us: int = 0
    consensus_hex: str = ""
    consensus_bits: int = 0
    frames: List[DecodedFrame] = field(default_factory=list)
    rtl433_events: List[Dict[str, object]] = field(default_factory=list)
    rtl433_summary: str = ""

    def to_dict(self) -> Dict[str, object]:
        out: Dict[str, object] = {
            "filetype": self.filetype,
            "frequency": self.frequency,
            "preset": self.preset,
            "protocol": self.protocol,
            "key_hex": self.key_hex,
            "raw_count": self.raw_count,
            "duration_ms": round(self.duration_ms, 2),
            "frame_count": self.frame_count,
            "estimated_te_us": self.estimated_te_us,
            "consensus_hex": self.consensus_hex,
            "consensus_bits": self.consensus_bits,
            "frames": [
                {
                    "pulses": f.pulses,
                    "te_us": f.te_us,
                    "bits_pwm": f.bits_pwm,
                    "hex_pwm": f.hex_pwm,
                }
                for f in self.frames[:8]
            ],
        }
        if self.rtl433_events:
            out["rtl433_events"] = self.rtl433_events
        if self.rtl433_summary:
            out["rtl433_summary"] = self.rtl433_summary
        return out

    def format_compact(self) -> str:
        mhz = self.frequency / 1_000_000.0
        lines = [
            f"Freq: {mhz:.2f} MHz",
            f"Type: {self.protocol or 'RAW'}",
        ]
        if self.key_hex:
            lines.append(f"Key : {self.key_hex}")
        if self.rtl433_summary:
            lines.extend(self.rtl433_summary.splitlines())
        if self.raw_count:
            lines.append(f"Pulses: {self.raw_count} ({self.duration_ms:.1f}ms)")
            lines.append(f"Frames: {self.frame_count} Te~{self.estimated_te_us}us")
        if self.consensus_hex:
            lines.append(f"Hex({self.consensus_bits}b): {self.consensus_hex}")
        return "\n".join(lines)


def _bits_to_hex(bits: str) -> str:
    if not bits:
        return ""
    pad = (4 - (len(bits) % 4)) % 4
    padded = ("0" * pad) + bits
    return "".join(f"{int(padded[i : i + 4], 2):X}" for i in range(0, len(padded), 4))


def _estimate_two_clusters(durations: List[int]) -> Optional[Tuple[float, float]]:
    """1D 2-means clustering to separate short (`Te`) and long (`2..3 Te`) pulses."""
    clean = sorted(d for d in durations if 40 <= d <= 10000)
    if len(clean) < 6:
        return None
    c_short = float(clean[len(clean) // 4])
    c_long = float(clean[(len(clean) * 3) // 4])
    if c_long <= c_short * 1.25:
        return None

    for _ in range(12):
        mid = (c_short + c_long) / 2.0
        lo = [x for x in clean if x <= mid]
        hi = [x for x in clean if x > mid]
        if not lo or not hi:
            return None
        c_short = sum(lo) / len(lo)
        c_long = sum(hi) / len(hi)

    if c_long < c_short * 1.4:
        return None
    return c_short, c_long


def extract_raw_samples(
    content: str | bytes, default_freq: int = 433920000
) -> Tuple[int, str, List[int]]:
    """Extracts `(frequency_hz, preset, signed_us_samples)` from a `.sub` file or raw pulse text."""
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    freq = int(default_freq)
    preset = "FuriHalSubGhzPresetOok650Async"
    raw_samples: List[int] = []
    saw_header = False

    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if ":" in s:
            k, v = s.split(":", 1)
            k = k.strip()
            v = v.strip()
            saw_header = True
            if k == "Frequency":
                try:
                    freq = int(v)
                except ValueError:
                    pass
            elif k == "Preset" and v:
                preset = v
            elif k == "RAW_Data":
                for tok in v.split():
                    try:
                        raw_samples.append(int(tok))
                    except ValueError:
                        pass
        elif not saw_header:
            for tok in s.split():
                try:
                    raw_samples.append(int(tok))
                except ValueError:
                    pass

    return freq, preset, raw_samples


def raw_samples_to_ook(
    raw_samples: Sequence[int],
    frequency: int = 433920000,
    rssi_db: Optional[float] = None,
) -> str:
    """Converts signed microsecond durations (`+mark`, `-space`) to `rtl_433` `.ook` format."""
    coalesced: List[int] = []
    for raw in raw_samples:
        val = int(raw)
        if val == 0:
            continue
        if coalesced and ((coalesced[-1] > 0) == (val > 0)):
            coalesced[-1] += val
        else:
            coalesced.append(val)

    # `.ook` pulse rows always begin on a positive mark pulse
    while coalesced and coalesced[0] < 0:
        coalesced.pop(0)

    pairs: List[Tuple[int, int]] = []
    idx = 0
    n = len(coalesced)
    while idx < n:
        mark = coalesced[idx]
        if mark <= 0:
            idx += 1
            continue
        if idx + 1 < n and coalesced[idx + 1] < 0:
            gap = abs(coalesced[idx + 1])
            idx += 2
        else:
            gap = 10000
            idx += 1
        pairs.append((mark, gap))

    header = [
        ";pulse data",
        ";version 1",
        ";timescale 1us",
        f";ook {len(pairs)} pulses",
        f";freq1 {int(frequency)}",
        f";centerfreq {int(frequency)} Hz",
        ";samplerate 250000 Hz",
        ";sampledepth 8 bits",
    ]
    if rssi_db is not None:
        header.append(f";rssi {float(rssi_db):.1f} dB")
    body = [f"{m} {g}" for m, g in pairs]
    return "\n".join(header + body) + "\n"


def sub_to_ook(content: str | bytes, rssi_db: Optional[float] = None) -> str:
    """Converts a Flipper `.sub` RAW capture into an `rtl_433` `.ook` pulse file."""
    freq, _preset, samples = extract_raw_samples(content)
    if not samples:
        raise ValueError("Input `.sub` contains no RAW_Data pulse samples")
    return raw_samples_to_ook(samples, frequency=freq, rssi_db=rssi_db)


def ook_to_sub(
    ook_content: str | bytes,
    default_frequency: int = 433920000,
    preset: str = "FuriHalSubGhzPresetOok650Async",
) -> str:
    """Converts an `rtl_433` `.ook` pulse file back into a valid Flipper `.sub` RAW file."""
    text = (
        ook_content.decode("utf-8", errors="replace")
        if isinstance(ook_content, bytes)
        else ook_content
    )
    freq = int(default_frequency)
    pulses: List[int] = []

    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith(";"):
            low = s[1:].strip()
            if low.startswith("freq1 ") or low.startswith("centerfreq "):
                parts = low.split()
                if len(parts) >= 2:
                    try:
                        parsed_f = int(float(parts[1]))
                        if parsed_f > 1_000_000:
                            freq = parsed_f
                    except ValueError:
                        pass
            continue
        toks = s.split()
        if len(toks) >= 2:
            try:
                mark = abs(int(float(toks[0])))
                gap = abs(int(float(toks[1])))
            except ValueError:
                continue
            if mark > 0:
                pulses.append(mark)
            if gap > 0:
                pulses.append(-gap)

    if not pulses:
        raise ValueError("Input `.ook` contains no pulse pairs")

    lines = [
        "Filetype: Flipper SubGhz RAW File",
        "Version: 1",
        f"Frequency: {freq}",
        f"Preset: {preset}",
        "Protocol: RAW",
    ]
    for offset in range(0, len(pulses), MAX_RAW_VALUES_PER_LINE):
        chunk = pulses[offset : offset + MAX_RAW_VALUES_PER_LINE]
        lines.append("RAW_Data: " + " ".join(str(x) for x in chunk))
    return "\n".join(lines) + "\n"


def _format_rtl433_event_line(ev: Dict[str, object]) -> str:
    model = str(ev.get("model") or "Unknown")
    parts: List[str] = [model]
    for id_key in ("id", "device", "sensor_id"):
        if id_key in ev and ev[id_key] not in (None, ""):
            parts.append(f"id={ev[id_key]}")
            break
    if "channel" in ev and ev["channel"] not in (None, ""):
        parts.append(f"ch={ev['channel']}")
    if "temperature_C" in ev and isinstance(ev["temperature_C"], (int, float)):
        parts.append(f"{float(ev['temperature_C']):.1f}C")
    elif "temperature_F" in ev and isinstance(ev["temperature_F"], (int, float)):
        parts.append(f"{float(ev['temperature_F']):.1f}F")
    if "humidity" in ev and isinstance(ev["humidity"], (int, float)):
        parts.append(f"RH={int(round(float(ev['humidity'])))}%")
    if "pressure_kPa" in ev and isinstance(ev["pressure_kPa"], (int, float)):
        parts.append(f"{float(ev['pressure_kPa']):.1f}kPa")
    elif "pressure_PSI" in ev and isinstance(ev["pressure_PSI"], (int, float)):
        parts.append(f"{float(ev['pressure_PSI']):.1f}psi")
    for extra_key in ("cmd", "command", "state", "button", "code", "data"):
        if extra_key in ev and ev[extra_key] not in (None, ""):
            parts.append(f"{extra_key}={ev[extra_key]}")
            break
    if "battery_ok" in ev and ev["battery_ok"] in (0, 1):
        parts.append("bat=OK" if int(ev["battery_ok"]) == 1 else "bat=LOW")
    return " ".join(parts)


def decode_with_rtl433(
    content_or_samples: str | bytes | Sequence[int],
    frequency: int = 433920000,
    rssi_db: Optional[float] = None,
    runner: Optional[SubprocessRunner] = None,
    timeout_sec: float = 5.0,
) -> Dict[str, object]:
    """Pipes `.sub` or raw pulse durations through Kali's `rtl_433 -r <tmp.ook> -F json`."""
    if isinstance(content_or_samples, (str, bytes)):
        freq, _preset, samples = extract_raw_samples(content_or_samples, default_freq=frequency)
    else:
        freq = int(frequency)
        samples = [int(x) for x in content_or_samples]

    if not samples:
        return {
            "available": bool(runner or shutil.which("rtl_433")),
            "matched": False,
            "frequency": freq,
            "events": [],
            "summary": "",
        }

    rtl_bin = shutil.which("rtl_433")
    if runner is None and not rtl_bin:
        return {
            "available": False,
            "matched": False,
            "frequency": freq,
            "events": [],
            "summary": "",
            "hint": "rtl_433 not found in PATH (install package `rtl-433`)",
        }

    ook_text = raw_samples_to_ook(samples, frequency=freq, rssi_db=rssi_db)
    exec_fn = runner or subprocess.run

    with tempfile.TemporaryDirectory(prefix="z2z-ook-") as tmp:
        ook_path = Path(tmp) / "capture.ook"
        ook_path.write_text(ook_text, encoding="utf-8")
        cmd = [rtl_bin or "rtl_433", "-r", str(ook_path), "-F", "json"]
        try:
            proc = exec_fn(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(1.0, float(timeout_sec)),
            )
            if runner is None and not (proc.stdout or "").strip():
                proc = exec_fn(
                    [rtl_bin or "rtl_433", "-R", "30", "-R", "87", "-r", str(ook_path), "-F", "json"],
                    capture_output=True,
                    text=True,
                    timeout=max(1.0, float(timeout_sec)),
                )
        except Exception as exc:
            return {
                "available": True,
                "matched": False,
                "frequency": freq,
                "events": [],
                "summary": "",
                "error": str(exc),
            }

    events: List[Dict[str, object]] = []
    seen_sigs = set()
    for raw_ln in (proc.stdout or "").splitlines():
        ln = raw_ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except Exception:
            continue
        if not isinstance(obj, dict) or not obj.get("model"):
            continue
        dedup_obj = {k: v for k, v in obj.items() if k not in ("time", "mic")}
        sig = json.dumps(dedup_obj, sort_keys=True)
        if sig in seen_sigs:
            continue
        seen_sigs.add(sig)
        events.append(obj)

    summary_lines = [f"rtl433: {_format_rtl433_event_line(ev)}" for ev in events[:3]]
    return {
        "available": True,
        "matched": len(events) > 0,
        "frequency": freq,
        "ook_pulses": ook_text.count("\n") - 8,
        "events": events[:8],
        "summary": "\n".join(summary_lines),
    }


def parse_and_analyze_sub(
    content: str | bytes,
    use_rtl433: bool = True,
    rtl433_runner: Optional[SubprocessRunner] = None,
) -> SubGhzReport:
    text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else content
    rep = SubGhzReport()
    raw_samples: List[int] = []

    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#") or ":" not in s:
            continue
        k, v = s.split(":", 1)
        k = k.strip()
        v = v.strip()
        if k == "Filetype":
            rep.filetype = v
        elif k == "Frequency":
            try:
                rep.frequency = int(v)
            except ValueError:
                pass
        elif k == "Preset":
            rep.preset = v
        elif k == "Protocol":
            rep.protocol = v
        elif k == "Key":
            rep.key_hex = v
        elif k == "RAW_Data":
            for tok in v.split():
                try:
                    raw_samples.append(int(tok))
                except ValueError:
                    pass

    rep.raw_count = len(raw_samples)
    if not raw_samples:
        return rep

    rep.duration_ms = sum(abs(x) for x in raw_samples) / 1000.0
    marks = [x for x in raw_samples if x > 0]
    clusters = _estimate_two_clusters(marks)
    if clusters is None:
        all_abs = [abs(x) for x in raw_samples if 40 <= abs(x) <= 8000]
        clusters = _estimate_two_clusters(all_abs)

    if clusters is not None:
        c_short, c_long = clusters
        rep.estimated_te_us = int(round(c_short))
        threshold = (c_short + c_long) / 2.0
        gap_cutoff = max(3500.0, c_long * 4.5)
    else:
        med = sorted(marks)[len(marks) // 2] if marks else 350
        rep.estimated_te_us = med
        threshold = med * 1.6
        gap_cutoff = max(4000.0, med * 8.0)

    # Split into pulse frames on inter-frame silence gaps
    raw_frames: List[List[int]] = []
    cur: List[int] = []
    for sample in raw_samples:
        if sample < 0 and abs(sample) >= gap_cutoff:
            if len(cur) >= 8:
                raw_frames.append(cur)
            cur = []
        else:
            cur.append(sample)
    if len(cur) >= 8:
        raw_frames.append(cur)

    rep.frame_count = len(raw_frames)
    vote_counts: Dict[Tuple[str, int], int] = {}

    for fr in raw_frames:
        # Pair consecutive (+mark, -space) symbols
        idx = 0
        if fr and fr[0] < 0:
            idx = 1
        bits_pwm: List[str] = []
        bits_inv: List[str] = []
        while idx + 1 < len(fr):
            m, sp = fr[idx], fr[idx + 1]
            idx += 2
            if m <= 0 or sp >= 0:
                continue
            sm = abs(sp)
            # Standard OOK PWM: short mark + long space = 0, long mark + short space = 1
            if m < threshold and sm >= threshold:
                bits_pwm.append("0")
                bits_inv.append("1")
            elif m >= threshold and sm < threshold:
                bits_pwm.append("1")
                bits_inv.append("0")
            elif m >= threshold and sm >= threshold:
                bits_pwm.append("1")
                bits_inv.append("0")
            else:
                bits_pwm.append("0")
                bits_inv.append("1")

        b_str = "".join(bits_pwm)
        bi_str = "".join(bits_inv)
        if len(b_str) >= 8:
            hx = _bits_to_hex(b_str)
            rep.frames.append(
                DecodedFrame(
                    pulses=len(fr),
                    te_us=rep.estimated_te_us,
                    bits_pwm=b_str,
                    hex_pwm=hx,
                    bits_inv=bi_str,
                    hex_inv=_bits_to_hex(bi_str),
                )
            )
            vote_counts[(hx, len(b_str))] = vote_counts.get((hx, len(b_str)), 0) + 1

    if vote_counts:
        (best_hex, best_len), _ = max(vote_counts.items(), key=lambda kv: (kv[1], kv[0][1]))
        rep.consensus_hex = best_hex
        rep.consensus_bits = best_len

    if use_rtl433:
        r433 = decode_with_rtl433(
            raw_samples, frequency=rep.frequency, runner=rtl433_runner
        )
        if r433.get("matched"):
            rep.rtl433_events = list(r433.get("events", []))  # type: ignore[arg-type]
            rep.rtl433_summary = str(r433.get("summary", ""))
            if not rep.protocol and rep.rtl433_events:
                rep.protocol = str(rep.rtl433_events[0].get("model", ""))

    return rep


def analyze_live_sub_burst(
    pulse_str: str,
    frequency: int = 433920000,
    rssi_db: Optional[int] = None,
    rtl433_runner: Optional[SubprocessRunner] = None,
) -> Dict[str, object]:
    """Analyzes a live CC1101 pulse burst from `zero2zero.fap` via `rtl_433` + PWM fallback."""
    _f, _p, samples = extract_raw_samples(pulse_str, default_freq=frequency)
    mhz = frequency / 1_000_000.0
    if not samples:
        return {
            "matched": False,
            "frequency": frequency,
            "rssi": rssi_db,
            "pulses": 0,
            "text": f"{mhz:.2f}MHz idle",
        }

    sub_doc = (
        "Filetype: Flipper SubGhz RAW File\n"
        "Version: 1\n"
        f"Frequency: {int(frequency)}\n"
        "Preset: FuriHalSubGhzPresetOok650Async\n"
        "Protocol: RAW\n"
        "RAW_Data: " + " ".join(str(x) for x in samples) + "\n"
    )
    rep = parse_and_analyze_sub(sub_doc, use_rtl433=True, rtl433_runner=rtl433_runner)
    matched = bool(rep.rtl433_events)
    if matched:
        ev0 = rep.rtl433_events[0]
        line1 = str(ev0.get("model", "Sensor"))[:21]
        detail = _format_rtl433_event_line(ev0)
        # Strip model prefix from detail line so 21-col Flipper LCD shows extra fields cleanly
        if detail.startswith(str(ev0.get("model", ""))):
            detail = detail[len(str(ev0.get("model", ""))) :].strip()
        line2 = (detail or f"Pulses:{rep.raw_count} Te:{rep.estimated_te_us}us")[:21]
        line3 = f"{mhz:.2f}M RSSI:{rssi_db if rssi_db is not None else '?'}dBm"[:21]
    else:
        line1 = f"RAW {rep.raw_count}p Te~{rep.estimated_te_us}us"[:21]
        line2 = (
            f"Hex:{rep.consensus_hex[:16]}"
            if rep.consensus_hex
            else f"Dur:{rep.duration_ms:.1f}ms"
        )[:21]
        line3 = f"{mhz:.2f}M RSSI:{rssi_db if rssi_db is not None else '?'}dBm"[:21]

    return {
        "matched": matched,
        "frequency": frequency,
        "rssi": rssi_db,
        "pulses": rep.raw_count,
        "estimated_te_us": rep.estimated_te_us,
        "consensus_hex": rep.consensus_hex,
        "rtl433_events": rep.rtl433_events,
        "rtl433_summary": rep.rtl433_summary,
        "line1": line1,
        "line2": line2,
        "line3": line3,
        "text": f"{line1}\n{line2}\n{line3}",
    }


def synthesize_raw_sub(
    bitstream: str,
    frequency: int = 433920000,
    preset: str = "FuriHalSubGhzPresetOok650Async",
    te_us: int = 350,
    repeat: int = 4,
    gap_us: int = 10000,
) -> str:
    """Synthesizes a Flipper-compatible RAW `.sub` file from binary (`0`/`1`) or `0x...` hex."""
    s = bitstream.strip()
    if s.lower().startswith("0x"):
        hex_part = s[2:].strip()
        bits = "".join(f"{int(ch, 16):04b}" for ch in hex_part)
    elif any(ch in "23456789abcdefABCDEF" for ch in s):
        bits = "".join(f"{int(ch, 16):04b}" for ch in s if ch.isalnum())
    else:
        bits = "".join(ch for ch in s if ch in ("0", "1"))

    if not bits:
        raise ValueError("Bitstream must contain at least 1 bit")
    if te_us < 50 or te_us > 10000:
        raise ValueError("te_us must be between 50 and 10000 microseconds")

    short_p = int(te_us)
    long_p = int(te_us * 3)
    pulses: List[int] = []

    for _ in range(max(1, min(repeat, 20))):
        for b in bits:
            if b == "0":
                pulses.extend((short_p, -long_p))
            else:
                pulses.extend((long_p, -short_p))
        # Trailing stop pulse + inter-frame silence gap
        pulses.extend((short_p, -abs(gap_us)))

    lines = [
        "Filetype: Flipper SubGhz RAW File",
        "Version: 1",
        f"Frequency: {int(frequency)}",
        f"Preset: {preset}",
        "Protocol: RAW",
    ]
    for offset in range(0, len(pulses), MAX_RAW_VALUES_PER_LINE):
        chunk = pulses[offset : offset + MAX_RAW_VALUES_PER_LINE]
        lines.append("RAW_Data: " + " ".join(str(x) for x in chunk))
    return "\n".join(lines) + "\n"
