#!/usr/bin/env bash
# deploy-live.sh — Build and install `zero2zero` onto a live CardputerZero over SSH.
# Usage: ./packaging/deploy-live.sh kali@kali-cardputerzero.local

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "Usage: $0 <user@cardputer-host>" >&2
    exit 1
fi

TARGET="$1"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

"$ROOT_DIR/packaging/build-deb.sh"
DEB_FILE="$(ls -1t "$ROOT_DIR"/dist/zero2zero_*.deb | head -n 1)"
DEB_NAME="$(basename "$DEB_FILE")"

echo "[*] Copying $DEB_NAME -> $TARGET:/tmp/$DEB_NAME ..."
scp "$DEB_FILE" "$TARGET:/tmp/$DEB_NAME"
echo "[*] Installing package on $TARGET ..."
ssh "$TARGET" "sudo dpkg -i /tmp/$DEB_NAME && rm -f /tmp/$DEB_NAME"
echo "[+] Deployed $DEB_NAME to $TARGET"
