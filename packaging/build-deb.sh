#!/usr/bin/env bash
# build-deb.sh — Build ultra-lean Debian/Kali package (`zero2zero_<ver>_<arch>.deb`).
# Zero pip wheels, zero Qt/protobuf bloat: relies strictly on base packages already
# preinstalled in `kali-on-cardputerzero` (`libc6`, `python3`, `python3-gi`, `bluez`).

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="${VERSION:-0.1.0}"
ARCH="${ARCH:-$(dpkg --print-architecture)}"
PKG_NAME="zero2zero"
STAGE_DIR="$(mktemp -d)"
chmod 0755 "$STAGE_DIR"
trap 'rm -rf "$STAGE_DIR"' EXIT

make -C "$ROOT_DIR" build

install -d -m 755 \
    "$STAGE_DIR/DEBIAN" \
    "$STAGE_DIR/usr/bin" \
    "$STAGE_DIR/usr/lib/zero2zero/python/z2z" \
    "$STAGE_DIR/usr/share/zero2zero/flipper_app" \
    "$STAGE_DIR/usr/share/doc/zero2zero" \
    "$STAGE_DIR/etc/zero2zero/jobs.d" \
    "$STAGE_DIR/lib/systemd/system"

# Binaries & entrypoints
install -m 755 "$ROOT_DIR/src/z2z-ui" "$STAGE_DIR/usr/bin/z2z-ui"
strip --strip-unneeded "$STAGE_DIR/usr/bin/z2z-ui" 2>/dev/null || true
install -m 755 "$ROOT_DIR/bin/z2z" "$STAGE_DIR/usr/bin/z2z"
install -m 755 "$ROOT_DIR/bin/z2zd" "$STAGE_DIR/usr/lib/zero2zero/z2zd"
install -m 755 "$ROOT_DIR/packaging/register-applaunch.py" "$STAGE_DIR/usr/lib/zero2zero/register-applaunch.py"

# Python package (`z2z`)
install -m 644 "$ROOT_DIR"/python/z2z/*.py "$STAGE_DIR/usr/lib/zero2zero/python/z2z/"

# Declarative offload job hooks
install -m 644 "$ROOT_DIR"/etc/jobs.d/*.json "$STAGE_DIR/etc/zero2zero/jobs.d/"

# systemd units
install -m 644 "$ROOT_DIR/etc/systemd/z2zd.socket" "$STAGE_DIR/lib/systemd/system/"
install -m 644 "$ROOT_DIR/etc/systemd/z2zd.service" "$STAGE_DIR/lib/systemd/system/"
install -m 644 "$ROOT_DIR/etc/systemd/z2z-watch.service" "$STAGE_DIR/lib/systemd/system/"

# Flipper `.fap` binary + source bundle & docs
FAP_BIN="$ROOT_DIR/flipper_app/dist/zero2zero.fap"
if [[ ( ! -f "$FAP_BIN" || "$ROOT_DIR/flipper_app/zero2zero_app.c" -nt "$FAP_BIN" || "$ROOT_DIR/flipper_app/application.fam" -nt "$FAP_BIN" || "$ROOT_DIR/flipper_app/zero2zero_10px.png" -nt "$FAP_BIN" ) && -f "${UFBT_STATE_DIR:-$HOME/.ufbt}/current/ufbt_state.json" ]]; then
    "$ROOT_DIR/packaging/build-fap.sh"
fi
if [[ -f "$FAP_BIN" ]]; then
    install -m 644 "$FAP_BIN" "$STAGE_DIR/usr/share/zero2zero/flipper_app/zero2zero.fap"
fi
install -m 644 "$ROOT_DIR/flipper_app/application.fam" "$STAGE_DIR/usr/share/zero2zero/flipper_app/"
install -m 644 "$ROOT_DIR/flipper_app/zero2zero_10px.png" "$STAGE_DIR/usr/share/zero2zero/flipper_app/"
install -m 644 "$ROOT_DIR/flipper_app/zero2zero_app.c" "$STAGE_DIR/usr/share/zero2zero/flipper_app/"
install -m 644 "$ROOT_DIR/flipper_app/README.md" "$STAGE_DIR/usr/share/zero2zero/flipper_app/"
install -m 644 "$ROOT_DIR/README.md" "$STAGE_DIR/usr/share/doc/zero2zero/README.md"
install -m 644 "$ROOT_DIR/ARCHITECTURE.md" "$STAGE_DIR/usr/share/doc/zero2zero/ARCHITECTURE.md"
install -m 644 "$ROOT_DIR/LICENSE" "$STAGE_DIR/usr/share/doc/zero2zero/copyright"

INSTALLED_KB="$(du -sk "$STAGE_DIR/usr" "$STAGE_DIR/etc" "$STAGE_DIR/lib" | awk '{s+=$1} END {print s}')"

for jf in "$STAGE_DIR"/etc/zero2zero/jobs.d/*.json; do
    [[ -f "$jf" ]] && echo "/etc/zero2zero/jobs.d/$(basename "$jf")"
done > "$STAGE_DIR/DEBIAN/conffiles"

cat > "$STAGE_DIR/DEBIAN/control" <<EOF
Package: ${PKG_NAME}
Version: ${VERSION}
Section: utils
Priority: optional
Architecture: ${ARCH}
Installed-Size: ${INSTALLED_KB}
Depends: libc6, python3, python3-gi, bluez
Recommends: rtl-433, qrencode, zbar-tools, libcamera-tools, tshark, nmap, libnfc-bin
Suggests: proxmark3
Maintainer: Lwrless <happy.lwr@gmail.com>
Homepage: https://github.com/on-my-machine-works/zero2zero
Description: BLE companion for Kali Linux on CardputerZero and Flipper Zero
 Connects M5Stack CardputerZero running Kali Linux with a Flipper Zero over
 Bluetooth Low Energy. Includes the Zero2Zero LCD app (z2z-ui), CLI (z2z),
 on-demand BLE RPC daemon (z2zd), Sub-GHz rtl_433 bridge, IR remote deck and
 ir-ctl converter, camera QR/barcode to NFC/RFID scanner, tag/iButton tools,
 capture vault sync, and the prebuilt zero2zero.fap Flipper app.
EOF

cat > "$STAGE_DIR/DEBIAN/postinst" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "configure" ]; then
    /usr/lib/zero2zero/register-applaunch.py --root / || true
    if [ -d /run/systemd/system ]; then
        systemctl daemon-reload >/dev/null 2>&1 || true
        systemctl stop z2zd.service >/dev/null 2>&1 || true
        systemctl enable --now z2zd.socket >/dev/null 2>&1 || true
    fi
    pkill -f '/usr/lib/zero2zero/z2zd|z2z\.daemon' >/dev/null 2>&1 || true
fi
exit 0
EOF
chmod 0755 "$STAGE_DIR/DEBIAN/postinst"

cat > "$STAGE_DIR/DEBIAN/prerm" <<'EOF'
#!/bin/sh
set -e
if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then
    if [ -d /run/systemd/system ]; then
        systemctl disable --now z2z-watch.service z2zd.service z2zd.socket >/dev/null 2>&1 || true
    fi
    /usr/lib/zero2zero/register-applaunch.py --root / --remove || true
fi
exit 0
EOF
chmod 0755 "$STAGE_DIR/DEBIAN/prerm"
find "$STAGE_DIR" -type d -exec chmod 0755 {} +

mkdir -p "$ROOT_DIR/dist"
OUT_DEB="$ROOT_DIR/dist/${PKG_NAME}_${VERSION}_${ARCH}.deb"
dpkg-deb --root-owner-group -Zxz --build "$STAGE_DIR" "$OUT_DEB" >/dev/null
echo "[OK] Built $OUT_DEB ($(wc -c < "$OUT_DEB") bytes, installed ${INSTALLED_KB} KiB)"
