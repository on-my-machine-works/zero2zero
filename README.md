# zero2zero

[![Latest Release](https://img.shields.io/github/v/release/on-my-machine-works/zero2zero?label=release)](https://github.com/on-my-machine-works/zero2zero/releases/latest)
[![Package Release](https://img.shields.io/github/actions/workflow/status/on-my-machine-works/zero2zero/release.yml?label=build)](https://github.com/on-my-machine-works/zero2zero/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

`zero2zero` connects a **Flipper Zero** and a **[CardputerZero running Kali Linux](https://github.com/on-my-machine-works/kali-on-cardputerzero)** over Bluetooth Low Energy (BLE).

## Why We Built This

Flipper Zero is great at talking to physical radio and hardware targets (Sub-GHz, NFC, 125 kHz RFID, Infrared, iButton, and BadUSB), but its small screen, 5-button pad, and microcontroller memory make heavy decoding, typing payloads, or running network tools hard.

CardputerZero runs full Kali Linux with a keyboard, camera, Wi-Fi, and tools like `rtl_433`, `proxmark3`, `tshark`, and `nmap`, but it does not have Flipper's built-in RF and tag transceivers.

`zero2zero` links both devices over BLE so each one does what it is best at: Flipper handles the physical signals, and CardputerZero handles decoding, authoring, camera scanning, networking, and storage. Because everything runs over BLE, the USB and GPIO ports on both devices stay free for Wi-Fi adapters, USB gadgets, or expansion modules.

## What It Does

- **Works from both sides**: Run actions from CardputerZero (`Zero2Zero` in `APPLaunch` or the `z2z` CLI) or directly from Flipper (`zero2zero.fap`). Supports live BLE sessions as well as a background Pocket Watch mode when CardputerZero is in a bag or pocket.
- **Sub-GHz with `rtl_433`**: Decode saved `.sub` files with Kali's `rtl_433` (weather sensors, TPMS, remotes), or stream live Sub-GHz pulses from Flipper straight into `rtl_433` and read the results on Flipper's screen.
- **Keyboard IR remote & learning**: Browse Flipper `.ir` remotes on CardputerZero, filter buttons by typing, fire IR signals over BLE, save signals learned on Flipper, and convert between Flipper `.ir` and Linux `ir-ctl` files.
- **Camera to NFC / RFID**: Scan a QR code or barcode with CardputerZero's camera, generate an NTAG215 `.nfc` tag or 125 kHz `.rfid` card, send it to Flipper, and open it in Flipper's NFC or RFID app.
- **Tag, iButton & BadUSB tools**: Convert `.nfc` files (`.mfd` / `.bin` / `.eml`), inspect MIFARE dumps with `proxmark3`, create `.nfc`, `.rfid`, `.ibtn`, and DuckyScript files from the keyboard, render NDEF payloads as QR codes, and diff two captures (`z2z diff`).
- **Screen mirror & remote control**: View Flipper's screen on CardputerZero's display (or in an SSH terminal), control Flipper from the keyboard, sync Flipper's clock from Kali, and choose how Flipper alerts you (`Vibro+LED`, `Soft Beep`, `LED Only`, `Silent`, or `Loud`).
- **Custom jobs & SD backup**: Add JSON job scripts in `/etc/zero2zero/jobs.d/` (such as `tshark` PCAP summaries or `nmap` LAN scans) that can be triggered from either device, and back up Flipper's `/ext/` files to Kali (`z2z sync`).

## Quick Start

### Install Prebuilt Release

On [Kali Linux for CardputerZero](https://github.com/on-my-machine-works/kali-on-cardputerzero), install from `KaliPkgs` (`APPLaunch -> KaliPkgs -> z`) or run:

```bash
sudo cz-kali-pkgs install zero2zero
```

Or download and verify the `.deb` directly from [Releases](https://github.com/on-my-machine-works/zero2zero/releases/latest):

```bash
curl -fsSLO https://github.com/on-my-machine-works/zero2zero/releases/latest/download/zero2zero_arm64.deb
curl -fsSLO https://github.com/on-my-machine-works/zero2zero/releases/latest/download/SHA256SUMS
grep '  zero2zero_arm64.deb$' SHA256SUMS | sha256sum -c -
sudo dpkg -i zero2zero_arm64.deb
```

Installing the `.deb` adds **`Zero2Zero`** to `APPLaunch`, sets up `z2zd.socket` (so the daemon only runs when needed), and bundles `zero2zero.fap` (`v0.1.0`), which installs onto Flipper automatically over BLE on first launch.

### Build from Source

```bash
git clone https://github.com/on-my-machine-works/zero2zero.git
cd zero2zero

make          # Build the z2z-ui LCD app
make fap      # Optional: rebuild flipper_app/dist/zero2zero.fap from source
make check    # Run UI, Python, and Flipper app tests
make deb      # Build dist/zero2zero_0.1.0_arm64.deb

# Or build and deploy over SSH in one step:
./packaging/deploy-live.sh kali@kali-cardputerzero.local
```

## Handheld UI (`z2z-ui`)

Use `,` / `.` (or `Left` / `Right`) to switch tabs, `;` / `/` (or `Up` / `Down`) to move through lists, and `[8]` (or hold `` ` `` / `ESC` for `1.2s`) to exit.

| Tab | Softkeys (`4` - `7`) | Extra Keys |
| :--- | :--- | :--- |
| **`LINK`** | `4:SCAN` · `5:PAIR/REPAIR` · `6:CONN/DISC` · `7:WATCH` | `Bksp/Del`: Unpair Flipper · `0-9`: Enter 6-digit BLE PIN |
| **`REMOTE`** | `4:MIRROR` · `5:CLOCK` · `6:ALERT` · `7:FAP` | `I`: IR Deck · In Mirror: arrows + `Enter`=OK + `Bksp`=Back |
| **`CAPS`** | `4:LIST` · `5:INSPECT` · `6:OPEN` · `7:VAULT` | `I`: IR Deck · `C`: Cam Scan · `D`: Diff 2 files · `Q`: Show QR |
| **`STUDIO`** | `4:EDIT` · `5:PRESET` · `6:MODE` · `7:PUSH` | `N`: `.nfc` · `R`: `.rfid` · `B`: `.ibtn` · `Q`: QR · `C`: Cam Scan |
| **`JOBS`** | `4:RUN` · `5:REFRESH` · `6:FAP` · `7:VAULT` | Runs any `/etc/zero2zero/jobs.d/*.json` job |
| **`NET`** | `4:RECON` · `5:NOTES` · `6:STATUS` · `7:WATCH` | Shows system thermals, memory, network IPs, and Flipper status |

## CLI Reference (`z2z`)

```bash
z2z --version                                # Show version (z2z 0.1.0)
z2z scan                                     # Scan for nearby Flipper Zero devices
z2z pair                                     # Pair using the 6-digit PIN shown on Flipper
z2z connect                                  # Connect to the paired Flipper
z2z status                                   # Show battery, /ext free space, RSSI, and system info

z2z jobs                                     # List jobs in /etc/zero2zero/jobs.d/
z2z run sys-health                           # Run a job by ID
z2z clock-sync                               # Sync Kali time to Flipper's clock
z2z alert vibro                              # Set alert mode (vibro|soft|led|off|loud)
z2z screen --watch                           # Stream Flipper's screen in the terminal
z2z key ok                                   # Send a button press (up|down|left|right|ok|back)

z2z captures                                 # List captures on Flipper (/ext/)
z2z inspect /ext/nfc/Badge.nfc               # Inspect .sub, .nfc, .rfid, .ir, or .pcap files
z2z open /ext/nfc/Badge.nfc                  # Open a file in Flipper's matching app
z2z diff /ext/nfc/Before.nfc /ext/nfc/After.nfc

z2z cam-scan --open                          # Scan a QR/barcode with the camera -> Flipper NFC/RFID
z2z ir-list /ext/infrared/TV.ir              # List buttons in an .ir file
z2z ir-tx /ext/infrared/TV.ir Power          # Send an IR button over BLE
z2z ir-save Mute -d /ext/infrared/TV.ir      # Save the last IR signal learned on Flipper
z2z ir-convert /ext/infrared/TV.ir pwr.irctl -b Power

z2z sub-decode /ext/subghz/Weather.sub --ook-out weather.ook
z2z sub-synth 0xA5C3F0 --te 350 -r /ext/subghz/Test.sub
z2z nfc-convert /ext/nfc/Card.nfc card.mfd   # Convert between .nfc and .mfd/.bin/.eml
z2z nfc-make https://kali.org -r /ext/nfc/kali.nfc --open
z2z qr /ext/nfc/kali.nfc                     # Render NDEF text/URL as a QR code
z2z rfid-make 123:45678 -r /ext/lfrfid/badge.rfid --open
z2z ibtn-make 112233445566 -r /ext/ibutton/door.ibtn --open
z2z badusb diag --template-id linux-net-diag --open
z2z push note.txt /ext/apps_data/note.txt --template
z2z sync                                     # Back up changed /ext/ captures to Kali

# Flipper companion app (.fap):
z2z fap-install                              # Copy zero2zero.fap to /ext/apps/Bluetooth/
z2z fap-launch                               # Install/update if needed and open Zero2Zero on Flipper
z2z watch on                                 # Turn on Pocket Watch mode for background requests
```

## Custom Jobs (`/etc/zero2zero/jobs.d/`)

Place a `.json` file in `/etc/zero2zero/jobs.d/` or `~/.config/zero2zero/jobs.d/` to add your own workflow:

```json
{
  "id": "custom-pipeline",
  "title": "Custom Log Processor",
  "description": "Pulls a log from Flipper, runs a script on Kali, and merges new lines back",
  "pull_files": [
    { "remote": "/ext/apps_data/input.log", "local": "input.log", "optional": false }
  ],
  "command": ["/usr/local/bin/my-script", "input.log", "-o", "results.txt"],
  "merge_lines_into": {
    "local": "results.txt",
    "remote": "/ext/apps_data/results_dict.txt"
  },
  "timeout_sec": 30,
  "alert_on_finish": true
}
```

You can also pull the newest matching file in a folder using `"remote_dir": "/ext/subghz", "suffix": ".sub"`. New jobs show up automatically in `z2z jobs`, the **`JOBS`** tab in `z2z-ui`, and **`Browse & Run Jobs...`** on the Flipper.

## Documentation & License

- [`ARCHITECTURE.md`](ARCHITECTURE.md): System design, BLE/Protobuf protocol notes, and repository layout.
- [`flipper_app/README.md`](flipper_app/README.md): Building, installing, and using `zero2zero.fap` on Flipper Zero.
- [`LICENSE`](LICENSE): MIT License.
