# Architecture & Protocol Reference (`v0.1.0`)

Technical overview of how `zero2zero` links a Flipper Zero (`STM32WB55`) with [Kali Linux on CardputerZero](https://github.com/on-my-machine-works/kali-on-cardputerzero) over Bluetooth Low Energy (BLE).

## 1. Design Split

- **Flipper Zero** handles time-critical RF and hardware I/O (Sub-GHz CC1101, NFC, 125 kHz LF RFID, Infrared TX/RX, 1-Wire iButton, and USB HID).
- **CardputerZero** handles heavy decoding (`rtl_433`, `tshark`, `proxmark3`), camera barcode/QR scanning (`libcamera` + `zbarimg`), keyboard editing, network scans (`nmap`), and capture backups.
- **On-demand daemon**: `z2zd.socket` activates `z2zd` on `/run/zero2zero/rpc.sock` when `z2z` or `z2z-ui` connects, using packages already present in `kali-on-cardputerzero` (`libc6`, `python3`, `python3-gi`, `bluez`).

## 2. System Overview

```text
+---------------------------------------+        BLE 5.0 Encrypted GATT         +-----------------------------------------------+
|       Flipper Zero (STM32WB55)        |  Service: 8fe5b3d5-2e7f-4a98-2a48-... |          CardputerZero (Kali Linux)           |
|                                       | <-----------------------------------> |                                               |
|  * Native BLE Serial/RPC Profile      |   TX   (...61fe): Indicate PB.Main    |  * BlueZ Gio D-Bus + Kernel MGMT HCI Tuning   |
|    (Auto-opens RpcOwnerBle)           |   RX   (...62fe): WriteCmd PB.Main    |  * In-process Agent1 6-digit Passkey Handler  |
|  * zero2zero.fap (v0.1.0)             |   FLOW (...63fe): Notify   >u32 cred  |  * z2zd (/run/zero2zero/rpc.sock JSON-Lines)  |
|    |- Mode A: RpcAppSystem Duplex     |   STAT (...64fe): R/W/Not  RPC=1      |      ^                                        |
|    |  (PB_App.DataExchange #65)       |                                       |      +---------------+------------------------+
|    |- Live CC1101 -> rtl_433 Stream   |                                       |  * z2z-ui (C)    * z2z (CLI)    * jobs.d/     |
|    |- IR Blaster & TSOP Learn         |                                       |    /dev/fb_lcd     Shell/SSH      Job Runner  |
|    +- Mode B: /ext/apps_data/         |                                       |                                               |
|       zero2zero/{request,response}    |                                       +-----------------------------------------------+
+---------------------------------------+
```

## 3. Transport & Protocol Stack

### 3.1 BlueZ BLE GATT & Kernel MGMT Tuning (`python/z2z/ble_transport.py`)
- **Discovery**: Matches Flipper Zero BLE advertisements by 16-bit Service UUIDs `0x3080..0x308f` (which also encode shell color) and GAP Appearance `0x8600`.
- **Pairing (`org.bluez.Agent1`)**: Registers an in-process D-Bus `KeyboardOnly` agent so Flipper's 6-digit PIN can be entered from either `z2z` CLI or the `z2z-ui` keypad dialog.
- **Link Stability**: Uses Linux `AF_BLUETOOTH` `BTPROTO_MGMT` (`LOAD_CONN_PARAM` `15-27.5ms` interval, `5000ms` supervision timeout, LE 1M PHY), BlueZ `AcquireWrite` file-descriptor writes, and bond-mismatch detection (`PIN or Key Missing`) if one side loses its pairing keys.
- **Credit Flow Control**: Reads Flipper's free RX buffer characteristic (`19ed82ae-ed21-4c9d-4145-228e63fe0000`, big-endian `uint32`), pacing `MTU`-clamped writes so Flipper's serial buffer does not overflow.

### 3.2 Wire Protobuf Codec (`python/z2z/proto.py`)
Encodes and decodes varint-prefixed `PB.Main` frames directly in Python stdlib:
- **`PB_System`**: `Ping` (`5`/`6`), `DeviceInfo` (`32`/`33`), `GetDateTime`/`SetDateTime` (`35`-`37`), `PlayAudiovisualAlert` (`38`), `ProtobufVersion` (`39`/`40`), `PowerInfo` (`44`/`45`).
- **`PB_Storage`**: `Info` (`28`/`29`), `Stat` (`24`/`25`), `List` (`7`/`8`), `Read` (`9`/`10`), `Write` (`11`), `Delete` (`12`), `Mkdir` (`13`), `Md5sum` (`14`/`15`), `Rename` (`30`).
- **`PB_App`**: `Start` (`16`), `LockStatus` (`17`/`18`), `AppExit` (`47`), `AppLoadFile` (`48`), `AppStateResponse` (`58`), `GetError` (`63`/`64`), `DataExchange` (`65`).
- **`PB_Gui` & `PB_Desktop`**: `StartScreenStream` (`20`), `StopScreenStream` (`21`), `ScreenFrame` (`22`), `SendInputEvent` (`23`), `IsLockedRequest` (`66`).
- **`PB_Property`**: `Get` (`61`/`62`).

### 3.3 Flipper Companion App (`flipper_app/zero2zero_app.c`)
- **6-State `RpcAppSystem` Lifecycle**: Coordinates `RpcAppSystem*` ownership across Flipper's GUI thread, `RpcSessionWorker`, and FreeRTOS `Tmr Svc` (`Z2ZRpcNone -> Z2ZRpcActive <-> Z2ZRpcSending -> Z2ZRpcExiting -> Z2ZRpcClosed`) so both remote `AppExit` and Flipper `Back` exit cleanly.
- **On-Demand Radio & IR Workers**: Allocates `InfraredWorker` only while inside `IR Blaster & Learn` (leaving TX-only `op="ir_tx"` blasts lightweight) and puts `CC1101` to sleep as soon as `Live Sub-GHz` exits.
- **Atomic Provisioning**: `z2zd` checks `/ext/apps/Bluetooth/zero2zero.fap` size and MD5 before `app_start`, uploading to a `.tmp` path and renaming atomically when an update is needed.

## 4. Repository Layout

- [`README.md`](README.md): Overview, quick start, UI keymap, CLI commands, and custom job format.
- [`Makefile`](Makefile): Build `z2z-ui`, build `zero2zero.fap`, run tests, and package the `.deb`.
- [`bin/z2z`](bin/z2z), [`bin/z2zd`](bin/z2zd): CLI and daemon entrypoints.
- [`src/cz_ui.h`](src/cz_ui.h), [`src/z2z-ui.c`](src/z2z-ui.c): Handheld LCD UI (`Zero2Zero`).
- [`python/z2z/`](python/z2z/): BLE GATT transport, Protobuf codec, RPC client, daemon, CLI, and Sub-GHz/NFC/RFID/iButton/IR/camera/vault/job modules.
- [`flipper_app/`](flipper_app/): Flipper Zero external app source, icon, manifest, and prebuilt `dist/zero2zero.fap`.
- [`etc/systemd/`](etc/systemd/), [`etc/jobs.d/`](etc/jobs.d/): `systemd` unit files and default offload job definitions.
- [`packaging/`](packaging/): `.fap` builder, `.deb` packager, `APPLaunch` registrar, and SSH deploy script.
- [`.github/workflows/release.yml`](.github/workflows/release.yml): Automated `arm64` `.deb` and `.fap` test, build, and GitHub Release workflow.
- [`tests/`](tests/): Unit, mock-BLE/IPC, and ASan/UBSan `.fap` tests.
