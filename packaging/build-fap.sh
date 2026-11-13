#!/usr/bin/env bash
# build-fap.sh — Reproducible Flipper Zero `.fap` builder for `flipper_app/zero2zero.fap`.
#
# Works out of the box on minimal `aarch64` and `x86_64` Linux hosts even without
# `pip`, `ufbt`, `scons`, `unzip`, or `arm-none-eabi-gcc` preinstalled by bootstrapping
# Flipper's official `f7` SDK + prebuilt toolchain (`fbtenv.sh`) into `${UFBT_STATE_DIR:-$HOME/.ufbt}`.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT_DIR/flipper_app"
UFBT_STATE_DIR="${UFBT_STATE_DIR:-$HOME/.ufbt}"
FLIPPER_CHANNEL="${FLIPPER_CHANNEL:-release}"
FLIPPER_TARGET="${FLIPPER_TARGET:-f7}"
FORCE_UPDATE=0

if [[ "${1:-}" == "--update" ]]; then
    FORCE_UPDATE=1
fi

mkdir -p "$UFBT_STATE_DIR"

# Bootstrap or refresh Flipper SDK in `$UFBT_STATE_DIR/current` using only Python stdlib
if [[ "$FORCE_UPDATE" -eq 1 || ! -f "$UFBT_STATE_DIR/current/ufbt_state.json" || ! -f "$UFBT_STATE_DIR/current/components.json" ]]; then
    echo "[*] Bootstrapping Flipper Zero SDK (channel=$FLIPPER_CHANNEL, target=$FLIPPER_TARGET) -> $UFBT_STATE_DIR/current ..."
    python3 - "$UFBT_STATE_DIR" "$FLIPPER_CHANNEL" "$FLIPPER_TARGET" <<'PY'
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile

state_dir, channel_id, hw_target = sys.argv[1], sys.argv[2], sys.argv[3]
index_url = "https://update.flipperzero.one/firmware/directory.json"

req = urllib.request.Request(index_url, headers={"User-Agent": "zero2zero-build-fap/0.1.0"})
with urllib.request.urlopen(req, timeout=30) as resp:
    directory = json.loads(resp.read().decode("utf-8"))

channels = {c["id"]: c for c in directory.get("channels", [])}
if channel_id not in channels:
    raise SystemExit(f"Channel {channel_id!r} not found in {index_url}")

versions = channels[channel_id].get("versions", [])
if not versions:
    raise SystemExit(f"No firmware versions listed for channel {channel_id!r}")

latest = versions[0]
ver = latest["version"]
sdk_entry = None
for f in latest.get("files", []):
    if f.get("target") == hw_target and f.get("type") == "sdk_zip":
        sdk_entry = f
        break

if not sdk_entry:
    raise SystemExit(f"No sdk_zip found for target={hw_target!r} in version {ver}")

sdk_url = sdk_entry["url"]
expected_sha256 = sdk_entry.get("sha256", "").lower()

with tempfile.NamedTemporaryFile(prefix="flipper-sdk-", suffix=".zip", delete=False) as tmp:
    tmp_path = tmp.name

try:
    dl_req = urllib.request.Request(sdk_url, headers={"User-Agent": "zero2zero-build-fap/0.1.0"})
    with urllib.request.urlopen(dl_req, timeout=120) as r, open(tmp_path, "wb") as out:
        shutil.copyfileobj(r, out)

    actual_sha256 = hashlib.sha256(open(tmp_path, "rb").read()).hexdigest().lower()
    if expected_sha256 and actual_sha256 != expected_sha256:
        raise SystemExit(f"SHA-256 mismatch for {sdk_url}: expected {expected_sha256}, got {actual_sha256}")

    cur_dir = os.path.join(state_dir, "current")
    if os.path.exists(cur_dir):
        shutil.rmtree(cur_dir)
    os.makedirs(cur_dir, exist_ok=True)

    with zipfile.ZipFile(tmp_path) as zf:
        for info in zf.infolist():
            extracted = zf.extract(info, cur_dir)
            mode = info.external_attr >> 16
            if mode:
                os.chmod(extracted, mode)

    with open(os.path.join(cur_dir, "ufbt_state.json"), "w", encoding="utf-8") as sf:
        json.dump(
            {
                "branch": channel_id,
                "target": hw_target,
                "version": ver,
                "url": sdk_url,
            },
            sf,
            indent=2,
        )
    print(f"[+] Installed Flipper SDK {ver} ({hw_target}) [sha256={actual_sha256[:12]}...]")
finally:
    if os.path.exists(tmp_path):
        os.unlink(tmp_path)
PY
    # Normalize restrictive zip entry modes (e.g. 0600 on components.json)
    chmod -R u+rwX,go+rX "$UFBT_STATE_DIR/current"
fi

export UFBT_STATE_DIR
export UFBT_SCRIPT_DIR="$UFBT_STATE_DIR/current/scripts/ufbt"
export FBT_TOOLCHAIN_PATH="$UFBT_STATE_DIR"
export FBT_SKIP_CHECK_SOURCED=1

# Source Flipper's official toolchain environment (`aarch64-linux` / `x86_64-linux`)
# shellcheck disable=SC1091
. "$UFBT_STATE_DIR/current/scripts/toolchain/fbtenv.sh"

python3 -m SCons -Q --warn=target-not-built \
    -C "$UFBT_SCRIPT_DIR" \
    UFBT_APP_DIR="$APP_DIR" \
    faps

OUT_FAP="$APP_DIR/dist/zero2zero.fap"
if [[ ! -f "$OUT_FAP" ]]; then
    echo "[!] Expected output binary not found: $OUT_FAP" >&2
    exit 1
fi

SDK_VER="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$UFBT_STATE_DIR/current/ufbt_state.json")"
echo "[OK] Built $OUT_FAP ($(wc -c < "$OUT_FAP") bytes, Flipper SDK $SDK_VER / $FLIPPER_TARGET)"
