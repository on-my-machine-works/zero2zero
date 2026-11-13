# `zero2zero.fap` (`v0.1.0`)

Flipper Zero companion app (`Apps -> Bluetooth -> Zero2Zero Kali`) for [`zero2zero`](../README.md). It lets you trigger Kali tools, stream live Sub-GHz pulses to `rtl_433`, and send or learn Infrared signals over BLE.

## Building

A prebuilt `f7` binary (`flipper_app/dist/zero2zero.fap`) is checked into the repo so `make check` and `make deb` work without extra setup. To rebuild it from source on Linux (`aarch64` or `x86_64`), run:

```bash
make fap
# Output: flipper_app/dist/zero2zero.fap

# Optional: update the cached Flipper SDK to the newest release:
./packaging/build-fap.sh --update
```

`packaging/build-deb.sh` packages `zero2zero.fap` into `/usr/share/zero2zero/flipper_app/`, and `z2zd` installs or updates `/ext/apps/Bluetooth/zero2zero.fap` automatically when its MD5 changes.

## Installing Over BLE

```bash
z2z fap-install    # Copy zero2zero.fap to /ext/apps/Bluetooth/
z2z fap-launch     # Install/update if needed and launch on Flipper
```

## Menu Items (`Zero2Zero v0.1.0 (Kali)`)

- **Kali Health & Ping** / **Wireless & LAN Recon**: Check CardputerZero CPU temperature, memory, and Wi-Fi/LAN addresses.
- **Live Sub-GHz -> rtl_433**: Listen on `433.92 / 315.00 / 868.35 / 915.00 MHz` (`Left`/`Right` changes frequency, `OK` pauses, `Up` saves `/ext/subghz/z2z_live.sub`) and stream pulse bursts over BLE to Kali's `rtl_433`, showing decoded output live on Flipper.
- **IR Blaster & Learn**: Receive and decode IR remotes (`OK` replays the last signal, `Up` appends to `/ext/infrared/z2z_learned.ir`), send learned signals to Kali for naming (`z2z ir-save`), and transmit IR commands sent from CardputerZero's IR Deck.
- **Browse & Run Jobs...**: Fetch and run any `/etc/zero2zero/jobs.d/*.json` job from Kali.
- **Inspect Latest Capture** / **Decode Latest .sub**: Decode the newest `.nfc`, `.rfid`, `.ir`, `.pcap`, or `.sub` file on Kali and read the summary on Flipper.
- **Vault Sync Captures**: Back up new or changed `/ext/` captures to Kali.
- **Sync Clock from Kali**: Set Flipper's clock from Kali's system time.
- **Alert**: Cycle through `Vibro+LED`, `Soft Beep`, `LED Only`, `Silent`, and `Loud` (saved in `/ext/apps_data/zero2zero/settings.cfg`).
- **About: `v0.1.0 (Kali 0.1.0)`**: Shows the `.fap` and Kali suite versions on one line, with a short summary screen when opened.

## Operating Modes

1. **Live RPC Mode**: Used when opened from CardputerZero (`z2z fap-launch`, the IR Deck, or **`REMOTE -> [7:FAP]`**). Exchanges JSON messages directly over Flipper's BLE `DataExchange` channel.
2. **Pocket Mailbox Mode**: Used when opened directly on the Flipper while CardputerZero has `z2z watch on` (or **`LINK -> [7:WATCH]`**) enabled. Exchanges requests through `/ext/apps_data/zero2zero/{request,response}.json`.
