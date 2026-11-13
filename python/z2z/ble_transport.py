"""BlueZ D-Bus BLE GATT transport, Passkey Agent1 pairing, and Flow-Control pacing.

Uses PyGObject (`gi.repository.Gio`, `GLib`), which is preinstalled on
`kali-on-cardputerzero` — requiring zero extra APT or pip libraries.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import os
import queue
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

# Linux Kernel Bluetooth Management API (`include/net/bluetooth/mgmt.h`, ABI v1.23)
MGMT_EV_CMD_COMPLETE = 0x0001
MGMT_EV_CMD_STATUS = 0x0002
MGMT_EV_DEVICE_DISCONNECTED = 0x000C
MGMT_EV_AUTH_FAILED = 0x0011
MGMT_OP_READ_INFO = 0x0004
MGMT_OP_SET_POWERED = 0x0005
MGMT_OP_SET_SECURE_CONN = 0x002D
MGMT_OP_LOAD_CONN_PARAM = 0x0035
MGMT_OP_GET_PHY_CONFIGURATION = 0x0044
MGMT_OP_SET_PHY_CONFIGURATION = 0x0045
MGMT_SETTING_POWERED = 1 << 0
MGMT_SETTING_SECURE_CONN = 1 << 11
MGMT_SETTING_PHY_CONFIGURATION = 1 << 16
MGMT_DEV_DISCONN_AUTH_FAILURE = 0x04

BDADDR_LE_PUBLIC = 0x01
BDADDR_LE_RANDOM = 0x02

MGMT_PHY_LE_1M_TX = 1 << 9
MGMT_PHY_LE_1M_RX = 1 << 10
MGMT_PHY_LE_2M_TX = 1 << 11
MGMT_PHY_LE_2M_RX = 1 << 12
MGMT_PHY_LE_CODED_TX = 1 << 13
MGMT_PHY_LE_CODED_RX = 1 << 14

# Flipper `serial_profile.c` accepts 6..36 (7.5ms..45ms) and uses `.supervisor_timeout = 0`
# (which BlueZ `gas.c` rejects, leaving Linux at 420ms default). Setting 9..14 (11.25..17.5ms) +
# 500 (5000ms) fits Flipper's range in 0 negotiation rounds, halves Indication RTT, and
# survives shared Wi-Fi TDM bursts.
FLIPPER_CONN_MIN_INTERVAL = 9
FLIPPER_CONN_MAX_INTERVAL = 14
FLIPPER_CONN_LATENCY = 0
FLIPPER_SUPERVISION_TIMEOUT = 500


def _pack_mgmt_load_conn_params(
    address: str,
    min_interval: int = FLIPPER_CONN_MIN_INTERVAL,
    max_interval: int = FLIPPER_CONN_MAX_INTERVAL,
    latency: int = FLIPPER_CONN_LATENCY,
    timeout: int = FLIPPER_SUPERVISION_TIMEOUT,
    addr_types: Tuple[int, ...] = (BDADDR_LE_PUBLIC, BDADDR_LE_RANDOM),
) -> bytes:
    """Packs `struct mgmt_cp_load_conn_param` (`0x0035`) with reversed little-endian BDADDR."""
    parts = address.strip().split(":")
    if len(parts) != 6:
        raise ValueError(f"Invalid Bluetooth address: {address!r}")
    bdaddr_le = bytes(int(p, 16) for p in parts)[::-1]
    buf = bytearray(struct.pack("<H", len(addr_types)))
    for addr_type in addr_types:
        buf.extend(
            struct.pack(
                "<6sBHHHH",
                bdaddr_le,
                addr_type,
                min_interval,
                max_interval,
                latency,
                timeout,
            )
        )
    return bytes(buf)


def _compute_le_1m_phy_selection(
    supported_phys: int, configurable_phys: int, selected_phys: int
) -> int:
    """Selects `LE 1M PHY` (disabling configurable 2M/Coded LE PHYs) per `mgmt.c:4333-4343`."""
    unconfigure_phys = supported_phys & ~configurable_phys
    disable_le_fast = (
        MGMT_PHY_LE_2M_TX
        | MGMT_PHY_LE_2M_RX
        | MGMT_PHY_LE_CODED_TX
        | MGMT_PHY_LE_CODED_RX
    ) & configurable_phys
    enable_le_1m = (MGMT_PHY_LE_1M_TX | MGMT_PHY_LE_1M_RX) & supported_phys
    desired = ((selected_phys & ~disable_le_fast) | enable_le_1m | unconfigure_phys) & supported_phys
    if not (desired & MGMT_PHY_LE_1M_TX) or not (desired & MGMT_PHY_LE_1M_RX):
        return selected_phys
    return desired


def _mgmt_transact(
    sock: socket.socket,
    opcode: int,
    hci_index: int,
    payload: bytes = b"",
    timeout_sec: float = 3.0,
) -> Tuple[int, bytes]:
    """Executes one synchronous command on a Linux `BTPROTO_HCI` (`HCI_CHANNEL_CONTROL`) socket."""
    deadline = time.monotonic() + timeout_sec
    sock.settimeout(max(0.2, timeout_sec))
    sock.sendall(struct.pack("<HHH", opcode, hci_index, len(payload)) + payload)
    while True:
        rem = deadline - time.monotonic()
        if rem <= 0:
            raise TimeoutError(f"Kernel Bluetooth MGMT op 0x{opcode:04x} timed out")
        sock.settimeout(max(0.05, rem))
        pkt = sock.recv(2048)
        if len(pkt) < 9:
            continue
        ev, idx, plen = struct.unpack_from("<HHH", pkt, 0)
        if idx != hci_index or ev not in (MGMT_EV_CMD_COMPLETE, MGMT_EV_CMD_STATUS):
            continue
        cmd_op, status = struct.unpack_from("<HB", pkt, 6)
        if cmd_op != opcode:
            continue
        end = min(len(pkt), 6 + plen)
        return status, pkt[9:end]


def _parse_mgmt_auth_failure(pkt: bytes, hci_index: int, address: str) -> bool:
    """Returns `True` if `pkt` is a kernel MGMT auth-failure disconnect for `address`.

    When Flipper loses its BLE bond LTK (e.g. after a hard crash/reboot or BT key
    wipe), STM32WB55 replies to `HCI_LE_Start_Encryption` with `Status: PIN or Key Missing (0x06)`.
    BlueZ `Device1.Connect` only surfaces generic `le-connection-abort-by-local`, while the kernel
    MGMT channel emits `MGMT_EV_DEVICE_DISCONNECTED (0x000C)` with `reason == 0x04`
    (`MGMT_DEV_DISCONN_AUTH_FAILURE`) or `MGMT_EV_AUTH_FAILED (0x0011)`.
    """
    if len(pkt) < 14:
        return False
    ev, idx, plen = struct.unpack_from("<HHH", pkt, 0)
    if idx != hci_index or plen < 8:
        return False
    if ev not in (MGMT_EV_DEVICE_DISCONNECTED, MGMT_EV_AUTH_FAILED):
        return False
    parts = address.strip().split(":")
    if len(parts) != 6:
        return False
    try:
        bdaddr_le = bytes(int(p, 16) for p in parts)[::-1]
    except ValueError:
        return False
    if pkt[6:12] != bdaddr_le:
        return False
    reason_or_status = pkt[13]
    if ev == MGMT_EV_DEVICE_DISCONNECTED:
        return reason_or_status == MGMT_DEV_DISCONN_AUTH_FAILURE
    return reason_or_status != 0x00


def clean_dbus_error(exc: BaseException) -> str:
    """Strips verbose GLib/GDBus quark prefixes so BlueZ errors fit handheld UIs."""
    msg = str(exc).strip()
    msg = re.sub(r"^g-[a-z-]+-quark:\s*", "", msg)
    msg = re.sub(
        r"^GDBus\.Error:org\.(?:bluez|freedesktop\.DBus)\.Error\.([A-Za-z0-9_]+):\s*",
        r"\1: ",
        msg,
    )
    msg = re.sub(r"^GDBus\.Error:", "", msg)
    msg = re.sub(r"\s*\(\d+\)$", "", msg)
    return msg

# Flipper Zero BLE GATT UUIDs (verified against targets/f7/ble_glue/services/serial_service_uuid.inc)
UUID_SERIAL_SVC = "8fe5b3d5-2e7f-4a98-2a48-7acc60fe0000"
UUID_CHAR_TX = "19ed82ae-ed21-4c9d-4145-228e61fe0000"  # Flipper -> Host (Indicate/Read)
UUID_CHAR_RX = "19ed82ae-ed21-4c9d-4145-228e62fe0000"  # Host -> Flipper (Write/WriteWithoutResp)
UUID_CHAR_FLOW = "19ed82ae-ed21-4c9d-4145-228e63fe0000"  # Big-endian uint32 free RX buffer bytes
UUID_CHAR_RPC_STATE = "19ed82ae-ed21-4c9d-4145-228e64fe0000"  # RPC active status
UUID_BATTERY_LEVEL = "00002a19-0000-1000-8000-00805f9b34fb"

FLIPPER_APPEARANCE = 0x8600
SHELL_COLORS = {
    0x3080: "Unknown",
    0x3081: "Black",
    0x3082: "White",
    0x3083: "Transparent",
}

AGENT_PATH = "/org/zero2zero/agent"
AGENT_XML = """
<node>
  <interface name="org.bluez.Agent1">
    <method name="Release"/>
    <method name="RequestPinCode">
      <arg type="o" name="device" direction="in"/>
      <arg type="s" name="pincode" direction="out"/>
    </method>
    <method name="DisplayPinCode">
      <arg type="o" name="device" direction="in"/>
      <arg type="s" name="pincode" direction="in"/>
    </method>
    <method name="RequestPasskey">
      <arg type="o" name="device" direction="in"/>
      <arg type="u" name="passkey" direction="out"/>
    </method>
    <method name="DisplayPasskey">
      <arg type="o" name="device" direction="in"/>
      <arg type="u" name="passkey" direction="in"/>
      <arg type="q" name="entered" direction="in"/>
    </method>
    <method name="RequestConfirmation">
      <arg type="o" name="device" direction="in"/>
      <arg type="u" name="passkey" direction="in"/>
    </method>
    <method name="RequestAuthorization">
      <arg type="o" name="device" direction="in"/>
    </method>
    <method name="AuthorizeService">
      <arg type="o" name="device" direction="in"/>
      <arg type="s" name="uuid" direction="in"/>
    </method>
    <method name="Cancel"/>
  </interface>
</node>
"""


@dataclass
class BleDeviceSummary:
    address: str
    name: str
    rssi: int = -100
    paired: bool = False
    trusted: bool = False
    connected: bool = False
    shell_color: str = "Unknown"
    object_path: str = ""

    def to_dict(self) -> Dict[str, object]:
        return {
            "address": self.address,
            "name": self.name,
            "rssi": self.rssi,
            "paired": self.paired,
            "trusted": self.trusted,
            "connected": self.connected,
            "shell_color": self.shell_color,
        }


def detect_flipper_adv(
    name: str, uuids: List[str], appearance: int = 0
) -> Optional[str]:
    """Returns shell color string if advertisement matches Flipper Zero BLE profile, else None."""
    for u in uuids:
        ul = u.lower()
        if ul == UUID_SERIAL_SVC:
            return "Flipper"
        if ul.startswith("0000308") and ul.endswith("-0000-1000-8000-00805f9b34fb"):
            try:
                short = int(ul[4:8], 16)
                if 0x3080 <= short <= 0x308F:
                    return SHELL_COLORS.get(short, f"0x{short:04x}")
            except ValueError:
                pass
    if appearance == FLIPPER_APPEARANCE:
        return "Flipper"
    if name and "flipper" in name.lower():
        return "Flipper"
    return None


class CreditFlowController:
    """Tracks Flipper's big-endian uint32 BLE RX buffer credits (`...63fe0000`)."""

    def __init__(self, initial_credits: int = 1024, max_chunk: int = 180) -> None:
        self._cond = threading.Condition()
        self._credits = max(0, initial_credits)
        self._closed = False
        self.max_chunk = max_chunk

    @property
    def credits(self) -> int:
        with self._cond:
            return self._credits

    @property
    def is_closed(self) -> bool:
        with self._cond:
            return self._closed

    def update_from_be_bytes(self, payload: bytes) -> int:
        if len(payload) < 4:
            return self.credits
        # serial_service.c uses REVERSE_BYTES_U32 (big-endian uint32)
        (avail,) = struct.unpack(">I", payload[:4])
        with self._cond:
            self._credits = avail
            self._cond.notify_all()
            return avail

    def close(self) -> None:
        with self._cond:
            self._closed = True
            self._cond.notify_all()

    def write_paced(
        self,
        data: bytes,
        write_chunk_fn: Callable[[bytes], None],
        timeout_sec: float = 10.0,
    ) -> None:
        mv = memoryview(data)
        pos = 0
        total = len(data)
        while pos < total:
            with self._cond:
                deadline = time.monotonic() + timeout_sec
                while self._credits <= 0 and not self._closed:
                    rem = deadline - time.monotonic()
                    if rem <= 0:
                        raise TimeoutError(
                            "Timed out waiting for Flipper BLE RX flow-control credits"
                        )
                    self._cond.wait(timeout=rem)
                if self._closed:
                    raise ConnectionError("BLE transport closed during write")
                take = min(total - pos, self.max_chunk, self._credits)
                self._credits -= take
            chunk = bytes(mv[pos : pos + take])
            write_chunk_fn(chunk)
            pos += take


class TransportBase:
    """Abstract transport interface implemented by BlueZ BLE and test mocks."""

    def connect(self, target: Optional[str] = None) -> Dict[str, object]:
        raise NotImplementedError

    def disconnect(self) -> None:
        raise NotImplementedError

    def send_bytes(self, data: bytes, timeout_sec: float = 10.0) -> None:
        raise NotImplementedError

    def set_rx_callback(self, cb: Optional[Callable[[bytes], None]]) -> None:
        raise NotImplementedError

    def set_disconnect_callback(self, cb: Optional[Callable[[], None]]) -> None:
        pass

    @property
    def is_connected(self) -> bool:
        raise NotImplementedError

    def link_info(self) -> Dict[str, object]:
        raise NotImplementedError


def _load_gio():
    import gi  # type: ignore

    from gi.repository import GLib, Gio  # type: ignore

    return Gio, GLib


class BluezBleTransport(TransportBase):
    """Direct BlueZ D-Bus BLE client using PyGObject (`Gio.DBusConnection`)."""

    def __init__(self, adapter_hint: str = "hci0") -> None:
        self._adapter_hint = adapter_hint
        self._rx_cb: Optional[Callable[[bytes], None]] = None
        self._disc_cb: Optional[Callable[[], None]] = None
        self._max_chunk_cap: int = 120
        self._chunk_delay_sec: float = 0.016
        self.inter_envelope_delay_sec: float = 0.012
        self._flow = CreditFlowController(initial_credits=1024, max_chunk=self._max_chunk_cap)
        self._lock = threading.Lock()
        self._tx_lock = threading.Lock()
        self._bus = None
        self._notify_bus = None
        self._rx_write_fd: int = -1
        self._loop_ctx = None
        self._loop = None
        self._loop_thread: Optional[threading.Thread] = None
        self._sub_id: int = 0
        self._session_gen: int = 0
        self._rx_chunks: int = 0
        self._rx_bytes: int = 0
        self._tx_write_mode: str = "command"
        self._att_mtu: int = 0
        self._dev_path: str = ""
        self._dev_info: Optional[BleDeviceSummary] = None
        self._char_tx: str = ""
        self._char_rx: str = ""
        self._char_flow: str = ""
        self._char_rpc_state: str = ""
        self._char_batt: str = ""
        self._last_batt_pct: Optional[int] = None
        self._connected = False
        self._sc_disabled_by_us = False
        self._last_sc_power_cycled = False
        self._last_pair_ok_mono: float = 0.0

    def set_rx_callback(self, cb: Optional[Callable[[bytes], None]]) -> None:
        self._rx_cb = cb

    def set_disconnect_callback(self, cb: Optional[Callable[[], None]]) -> None:
        self._disc_cb = cb

    def _fire_disconnect_cb(self) -> None:
        cb = self._disc_cb
        if cb is not None:
            try:
                cb()
            except Exception:
                pass

    @property
    def is_connected(self) -> bool:
        return bool(self._connected and not self._flow.is_closed)

    @property
    def rx_stats(self) -> Dict[str, int]:
        return {
            "rx_chunks": self._rx_chunks,
            "rx_bytes": self._rx_bytes,
            "credits": self._flow.credits,
        }

    def _get_bus(self):
        Gio, _ = _load_gio()
        if self._bus is None:
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
        return self._bus

    def _get_managed_objects(self) -> dict:
        Gio, GLib = _load_gio()
        bus = self._get_bus()
        res = bus.call_sync(
            "org.bluez",
            "/",
            "org.freedesktop.DBus.ObjectManager",
            "GetManagedObjects",
            None,
            GLib.VariantType.new("(a{oa{sa{sv}}})"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return res.unpack()[0]

    def _find_adapter_path(self, objects: Optional[dict] = None) -> str:
        objs = objects if objects is not None else self._get_managed_objects()
        fallback = ""
        for path, ifaces in objs.items():
            if "org.bluez.Adapter1" in ifaces:
                if path.endswith("/" + self._adapter_hint):
                    return path
                if not fallback:
                    fallback = path
        if not fallback:
            raise RuntimeError("No BlueZ Bluetooth adapter (org.bluez.Adapter1) found")
        return fallback

    def _ensure_adapter_powered(self, adapter_path: str, pairable: bool = False) -> None:
        Gio, GLib = _load_gio()
        bus = self._get_bus()
        bus.call_sync(
            "org.bluez",
            adapter_path,
            "org.freedesktop.DBus.Properties",
            "Set",
            GLib.Variant("(ssv)", ("org.bluez.Adapter1", "Powered", GLib.Variant("b", True))),
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        if pairable:
            try:
                bus.call_sync(
                    "org.bluez",
                    adapter_path,
                    "org.freedesktop.DBus.Properties",
                    "Set",
                    GLib.Variant(
                        "(ssv)", ("org.bluez.Adapter1", "Pairable", GLib.Variant("b", True))
                    ),
                    None,
                    Gio.DBusCallFlags.NONE,
                    3000,
                    None,
                )
            except Exception:
                pass

    def _open_mgmt_watch_socket(self) -> Optional[socket.socket]:
        """Opens a non-blocking kernel MGMT socket to observe `MGMT_DEV_DISCONN_AUTH_FAILURE`."""
        if os.geteuid() != 0:
            return None
        try:
            af_bt = getattr(socket, "AF_BLUETOOTH", 31)
            bt_hci = getattr(socket, "BTPROTO_HCI", 1)
            dev_none = getattr(socket, "HCI_DEV_NONE", 0xFFFF)
            chan_ctrl = getattr(socket, "HCI_CHANNEL_CONTROL", 3)
            msock = socket.socket(af_bt, socket.SOCK_RAW, bt_hci)
            msock.bind((dev_none, chan_ctrl))
            msock.setblocking(False)
            return msock
        except Exception:
            return None

    def _drain_mgmt_auth_failure(
        self,
        msock: Optional[socket.socket],
        adapter_path: str,
        address: str,
        wait_sec: float = 0.0,
    ) -> bool:
        if msock is None:
            return False
        hci_name = adapter_path.rsplit("/", 1)[-1] or "hci0"
        m_idx = re.search(r"(\d+)$", hci_name)
        hci_index = int(m_idx.group(1)) if m_idx else 0
        deadline = time.monotonic() + max(0.0, wait_sec)
        while True:
            drained_any = False
            while True:
                try:
                    pkt = msock.recv(2048)
                except (BlockingIOError, InterruptedError):
                    break
                except Exception:
                    return False
                if not pkt:
                    break
                drained_any = True
                if _parse_mgmt_auth_failure(pkt, hci_index, address):
                    return True
            rem = deadline - time.monotonic()
            if rem <= 0:
                return False
            try:
                rlist, _, _ = select.select([msock], [], [], min(0.06, rem))
            except Exception:
                return False
            if not rlist and drained_any:
                return False

    def _force_disconnect_device(
        self,
        adapter_path: str,
        dev_path: str,
        t0: float,
        log_fn: Optional[Callable[[float, str], None]] = None,
    ) -> bool:
        """Clears a live or ghost `Device1.Connected=True` state (cycling `hci` via MGMT if wedged).

        On BlueZ `5.87` + `BCM4343A2`, aborted encryption (`PIN or Key Missing`) or
        peripheral crashes can wedge `org.bluez.Device1.Connected = True` in `bluetoothd` with no
        live ACL handle, causing every subsequent `connect()` to skip `Device1.Connect()` and hang!
        """
        emit = log_fn or self._conn_log
        try:
            d_props = self._get_managed_objects().get(dev_path, {}).get("org.bluez.Device1", {})
            if not d_props.get("Connected", False):
                return False
        except Exception:
            return False

        try:
            Gio, _ = _load_gio()
            self._get_bus().call_sync(
                "org.bluez",
                dev_path,
                "org.bluez.Device1",
                "Disconnect",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
        except Exception:
            pass

        for _ in range(4):
            time.sleep(0.15)
            try:
                d_chk = self._get_managed_objects().get(dev_path, {}).get("org.bluez.Device1", {})
                if not d_chk.get("Connected", False):
                    return True
            except Exception:
                return True

        if os.geteuid() != 0:
            return True

        hci_name = adapter_path.rsplit("/", 1)[-1] or "hci0"
        m_idx = re.search(r"(\d+)$", hci_name)
        hci_index = int(m_idx.group(1)) if m_idx else 0
        emit(
            t0,
            f"BlueZ ghost Connected=True persisted on {dev_path}; cycling {hci_name} power via MGMT",
        )
        try:
            af_bt = getattr(socket, "AF_BLUETOOTH", 31)
            bt_hci = getattr(socket, "BTPROTO_HCI", 1)
            dev_none = getattr(socket, "HCI_DEV_NONE", 0xFFFF)
            chan_ctrl = getattr(socket, "HCI_CHANNEL_CONTROL", 3)
            with socket.socket(af_bt, socket.SOCK_RAW, bt_hci) as msock:
                msock.bind((dev_none, chan_ctrl))
                _mgmt_transact(msock, MGMT_OP_SET_POWERED, hci_index, b"\x00")
                _mgmt_transact(msock, MGMT_OP_SET_SECURE_CONN, hci_index, b"\x00")
                _mgmt_transact(msock, MGMT_OP_SET_POWERED, hci_index, b"\x01")
            self._last_sc_power_cycled = True
            time.sleep(0.35)
            self._ensure_adapter_powered(adapter_path)
        except Exception as exc:
            emit(t0, f"MGMT ghost-link power cycle warning: {exc}")
        return True

    def _remove_device(self, adapter_path: str, dev_path: str) -> None:
        """Disconnects and removes `dev_path` from BlueZ, waiting until `GetManagedObjects` drops it."""
        Gio, GLib = _load_gio()
        bus = self._get_bus()
        self._force_disconnect_device(
            adapter_path, dev_path, time.monotonic(), log_fn=self._pair_log
        )

        try:
            bus.call_sync(
                "org.bluez",
                adapter_path,
                "org.bluez.Adapter1",
                "RemoveDevice",
                GLib.Variant("(o)", (dev_path,)),
                None,
                Gio.DBusCallFlags.NONE,
                4000,
                None,
            )
        except Exception:
            return

        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            try:
                objs = self._get_managed_objects()
                d_rem = objs.get(dev_path, {}).get("org.bluez.Device1")
                if d_rem is None or not d_rem.get("Paired", False):
                    if d_rem is None:
                        return
            except Exception:
                return
            time.sleep(0.1)

    def _refresh_le_presence(
        self,
        adapter_path: str,
        address: str,
        max_wait: float = 5.0,
        min_scan_sec: float = 0.45,
    ) -> Optional[BleDeviceSummary]:
        """Refreshes Flipper's `le_seen` timestamp in BlueZ before `Pair()`/`Connect()`.

        On dual-mode controllers like CardputerZero's `BCM4343A2`, BlueZ's
        `select_conn_bearer()` falls back to Classic BR/EDR paging (`Page Timeout`) if
        `le_seen` is older than `LAST_SEEN_TIMEOUT` (10s), and `BCM4343A2` aborts with
        `le-connection-abort-by-local` if `Pair()`/`Connect()` runs <350ms after `StopDiscovery`.
        """
        Gio, GLib = _load_gio()
        bus = self._get_bus()
        try:
            bus.call_sync(
                "org.bluez",
                adapter_path,
                "org.bluez.Adapter1",
                "SetDiscoveryFilter",
                GLib.Variant("(a{sv})", ({"Transport": GLib.Variant("s", "le")},)),
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
        except Exception:
            pass

        started = False
        try:
            bus.call_sync(
                "org.bluez",
                adapter_path,
                "org.bluez.Adapter1",
                "StartDiscovery",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                4000,
                None,
            )
            started = True
        except Exception as exc:
            if "InProgress" not in str(exc):
                raise

        found: Optional[BleDeviceSummary] = None
        try:
            t0 = time.monotonic()
            deadline = t0 + max(1.0, max_wait)
            addr_lc = address.lower()
            while time.monotonic() < deadline:
                time.sleep(0.2)
                for cand in self.list_known_flippers():
                    if cand.address.lower() == addr_lc and cand.rssi > -100:
                        found = cand
                        if (time.monotonic() - t0) >= min_scan_sec:
                            return found
            return found
        finally:
            if started:
                try:
                    bus.call_sync(
                        "org.bluez",
                        adapter_path,
                        "org.bluez.Adapter1",
                        "StopDiscovery",
                        None,
                        None,
                        Gio.DBusCallFlags.NONE,
                        3000,
                        None,
                    )
                except Exception:
                    pass
                # BCM4343A2 HCI UART settle time before LE Create Connection
                time.sleep(0.4)

    def list_known_flippers(self, objects: Optional[dict] = None) -> List[BleDeviceSummary]:
        objs = objects if objects is not None else self._get_managed_objects()
        found: List[BleDeviceSummary] = []
        for path, ifaces in objs.items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev:
                continue
            name = str(dev.get("Name") or dev.get("Alias") or "")
            uuids = [str(u) for u in dev.get("UUIDs", [])]
            appearance = int(dev.get("Appearance", 0))
            shell = detect_flipper_adv(name, uuids, appearance)
            if shell is None:
                continue
            found.append(
                BleDeviceSummary(
                    address=str(dev.get("Address", "")),
                    name=name or "Flipper",
                    rssi=int(dev.get("RSSI", -100)),
                    paired=bool(dev.get("Paired", False)),
                    trusted=bool(dev.get("Trusted", False)),
                    connected=bool(dev.get("Connected", False)),
                    shell_color=shell,
                    object_path=path,
                )
            )
        found.sort(key=lambda d: (not d.connected, not d.paired, -d.rssi))
        return found

    def scan(self, timeout_sec: float = 4.0) -> List[BleDeviceSummary]:
        Gio, GLib = _load_gio()
        bus = self._get_bus()
        adapter = self._find_adapter_path()
        self._ensure_adapter_powered(adapter)

        try:
            bus.call_sync(
                "org.bluez",
                adapter,
                "org.bluez.Adapter1",
                "SetDiscoveryFilter",
                GLib.Variant("(a{sv})", ({"Transport": GLib.Variant("s", "le")},)),
                None,
                Gio.DBusCallFlags.NONE,
                3000,
                None,
            )
        except Exception:
            pass

        started = False
        try:
            bus.call_sync(
                "org.bluez",
                adapter,
                "org.bluez.Adapter1",
                "StartDiscovery",
                None,
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            started = True
        except Exception as exc:
            if "InProgress" not in str(exc):
                raise

        try:
            time.sleep(max(0.5, timeout_sec))
            return self.list_known_flippers()
        finally:
            if started:
                try:
                    bus.call_sync(
                        "org.bluez",
                        adapter,
                        "org.bluez.Adapter1",
                        "StopDiscovery",
                        None,
                        None,
                        Gio.DBusCallFlags.NONE,
                        3000,
                        None,
                    )
                except Exception:
                    pass
                time.sleep(0.35)

    def _resolve_target_device(
        self, target: Optional[str], scan_if_missing: bool = True
    ) -> BleDeviceSummary:
        candidates = self.list_known_flippers()
        if not candidates and scan_if_missing:
            candidates = self.scan(timeout_sec=3.5)
        if not candidates:
            raise RuntimeError("No Flipper Zero BLE devices found nearby")

        if not target:
            return candidates[0]

        needle = target.strip().lower()
        for c in candidates:
            if c.address.lower() == needle or c.name.lower() == needle or needle in c.name.lower():
                return c

        if scan_if_missing:
            for c in self.scan(timeout_sec=4.0):
                if c.address.lower() == needle or c.name.lower() == needle or needle in c.name.lower():
                    return c
        raise RuntimeError(f"Flipper Zero '{target}' not found in BlueZ discovery")

    def _pair_log(self, t0: float, msg: str) -> None:
        dt = time.monotonic() - t0
        print(f"[z2zd:pair +{dt:5.2f}s] {msg}", file=sys.stderr, flush=True)

    def _conn_log(self, t0: float, msg: str) -> None:
        dt = time.monotonic() - t0
        print(f"[z2zd:conn +{dt:5.2f}s] {msg}", file=sys.stderr, flush=True)

    def _set_adapter_sc(
        self,
        adapter_path: str,
        enable_sc: bool,
        t0: float,
        allow_power_cycle: bool = True,
        log_fn: Optional[Callable[[float, str], None]] = None,
    ) -> bool:
        """Configures & verifies kernel `HCI_SC_ENABLED` (`MGMT_SETTING_SECURE_CONN`) for Flipper BLE.

        Flipper Zero sets `CFG_SC_SUPPORT = SC_PAIRING_OPTIONAL`. Under
        `SC=on`, 80-PDU LESC Passkey Entry + ECDH `DHKey Check` collides with BlueZ GATT
        discovery / PHY updates on `BCM4343A2`, causing `AuthenticationCanceled` ~0.8s after PIN submit.

        `hci0` MUST also remain `sc off` during `connect()` (including after
        host reboots)! Flipper's `aci_gap_slave_security_req()` sets `SMP_AUTH_SC` in
        `SMP_CMD_SECURITY_REQ`; if `HCI_SC_ENABLED` is `1` (`sc on`), Linux `smp.c`
        (`smp_cmd_security_req`) escalates `sec_level` to `BT_SECURITY_FIPS` (4), rejects
        our stored `BT_SECURITY_HIGH` (3) Legacy LTK, and drops the link ~2s later!

        Never rely on unverified `btmgmt sc off` exit code `0`! `btmgmt` exits `0`
        even on lowercase `"Set Secure Connections for hci0 failed with status 0x11 (Powered)"`
        (or early `stdin` EOF). Instead, talk directly to Linux's `BTPROTO_HCI`
        `HCI_CHANNEL_CONTROL` MGMT socket and verify `current_settings & MGMT_SETTING_SECURE_CONN`.
        """
        self._last_sc_power_cycled = False
        emit = log_fn or self._pair_log
        if os.geteuid() != 0:
            return False

        hci_name = adapter_path.rsplit("/", 1)[-1] or "hci0"
        m_idx = re.search(r"(\d+)$", hci_name)
        hci_index = int(m_idx.group(1)) if m_idx else 0
        mode = "on" if enable_sc else "off"
        val_byte = b"\x01" if enable_sc else b"\x00"

        # 1. Direct Linux kernel Bluetooth MGMT socket (`AF_BLUETOOTH` / `HCI_CHANNEL_CONTROL`)
        try:
            af_bt = getattr(socket, "AF_BLUETOOTH", 31)
            bt_hci = getattr(socket, "BTPROTO_HCI", 1)
            dev_none = getattr(socket, "HCI_DEV_NONE", 0xFFFF)
            chan_ctrl = getattr(socket, "HCI_CHANNEL_CONTROL", 3)
            with socket.socket(af_bt, socket.SOCK_RAW, bt_hci) as msock:
                msock.bind((dev_none, chan_ctrl))
                st_info, rp_info = _mgmt_transact(msock, MGMT_OP_READ_INFO, hci_index)
                if st_info == 0 and len(rp_info) >= 17:
                    supp_mask, curr_mask = struct.unpack_from("<II", rp_info, 9)
                    sc_now = bool(curr_mask & MGMT_SETTING_SECURE_CONN)
                    if sc_now == enable_sc:
                        emit(
                            t0,
                            f"mgmt {hci_name} sc already {mode} (settings=0x{curr_mask:05x} supp=0x{supp_mask:05x})",
                        )
                        return True

                st_live, rp_live = _mgmt_transact(
                    msock, MGMT_OP_SET_SECURE_CONN, hci_index, val_byte
                )
                if st_live == 0 and len(rp_live) >= 4:
                    (new_mask,) = struct.unpack_from("<I", rp_live, 0)
                    if bool(new_mask & MGMT_SETTING_SECURE_CONN) == enable_sc:
                        emit(
                            t0,
                            f"mgmt {hci_name} sc {mode} -> ok (settings=0x{new_mask:05x})",
                        )
                        return True

                if not allow_power_cycle:
                    emit(
                        t0,
                        f"mgmt {hci_name} live sc {mode} deferred while link active (status=0x{st_live:02x})",
                    )
                    return False

                emit(
                    t0,
                    f"mgmt {hci_name} live sc {mode} returned status=0x{st_live:02x}; cycling power to stage sc={mode}",
                )
                try:
                    Gio, GLib = _load_gio()
                    self._get_bus().call_sync(
                        "org.bluez",
                        adapter_path,
                        "org.freedesktop.DBus.Properties",
                        "Set",
                        GLib.Variant(
                            "(ssv)",
                            ("org.bluez.Adapter1", "Powered", GLib.Variant("b", False)),
                        ),
                        None,
                        Gio.DBusCallFlags.NONE,
                        3000,
                        None,
                    )
                except Exception:
                    pass

                _mgmt_transact(msock, MGMT_OP_SET_POWERED, hci_index, b"\x00")
                st_off_sc, _ = _mgmt_transact(
                    msock, MGMT_OP_SET_SECURE_CONN, hci_index, val_byte
                )
                _mgmt_transact(msock, MGMT_OP_SET_POWERED, hci_index, b"\x01")
                self._last_sc_power_cycled = True
                time.sleep(0.35)
                self._ensure_adapter_powered(adapter_path, pairable=True)

                st_chk, rp_chk = _mgmt_transact(msock, MGMT_OP_READ_INFO, hci_index)
                if st_chk == 0 and len(rp_chk) >= 17:
                    _, final_mask = struct.unpack_from("<II", rp_chk, 9)
                    sc_after = bool(final_mask & MGMT_SETTING_SECURE_CONN)
                    ok = sc_after == enable_sc
                    emit(
                        t0,
                        f"mgmt {hci_name} cycled sc {mode} (op_status=0x{st_off_sc:02x}) -> settings=0x{final_mask:05x} sc={int(sc_after)} ok={ok}",
                    )
                    return ok
                return st_off_sc == 0
        except Exception as mgmt_exc:
            emit(t0, f"kernel mgmt socket fallback to btmgmt ({mgmt_exc})")

        # 2. Fallback to `btmgmt` CLI with case-insensitive error check + `btmgmt info` verification
        btmgmt = shutil.which("btmgmt")
        if not btmgmt:
            return False

        def _run_btmgmt(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [btmgmt, "-i", str(hci_index), *args],
                input="",
                capture_output=True,
                text=True,
                timeout=4.0,
            )

        def _btmgmt_sc_state() -> Optional[bool]:
            r_inf = _run_btmgmt("info")
            for ln in f"{r_inf.stdout}\n{r_inf.stderr}".splitlines():
                s = ln.strip().lower()
                if s.startswith("current settings:"):
                    toks: Set[str] = set(s.split(":", 1)[1].split())
                    return "sc" in toks
            return None

        try:
            cur_sc = _btmgmt_sc_state()
            if cur_sc is not None and cur_sc == enable_sc:
                emit(t0, f"btmgmt {hci_name} sc already {mode}")
                return True

            res = _run_btmgmt("sc", mode)
            out = f"{res.stdout} {res.stderr}".strip()
            out_lc = out.lower()
            bad_tokens = ("failed", "rejected", "busy", "invalid", "powered", "not supported", "error")
            if res.returncode == 0 and not any(tok in out_lc for tok in bad_tokens):
                chk_sc = _btmgmt_sc_state()
                if chk_sc is None or chk_sc == enable_sc:
                    emit(t0, f"btmgmt {hci_name} sc {mode} -> ok")
                    return True

            if not allow_power_cycle:
                emit(t0, f"btmgmt live sc {mode} deferred while link active ({out})")
                return False

            emit(t0, f"btmgmt live sc {mode} returned ({out}); cycling adapter power")
            _run_btmgmt("power", "off")
            res2 = _run_btmgmt("sc", mode)
            _run_btmgmt("power", "on")
            self._last_sc_power_cycled = True
            time.sleep(0.35)
            self._ensure_adapter_powered(adapter_path, pairable=True)
            out2 = f"{res2.stdout} {res2.stderr}".strip()
            fin_sc = _btmgmt_sc_state()
            ok = (fin_sc == enable_sc) if fin_sc is not None else (
                res2.returncode == 0 and not any(tok in out2.lower() for tok in bad_tokens)
            )
            emit(t0, f"btmgmt cycled sc {mode} -> ok={ok} sc={fin_sc} ({out2})")
            return ok
        except Exception as exc:
            emit(t0, f"btmgmt sc {mode} skipped: {exc}")
            return False

    def _tune_le_link_params(
        self,
        adapter_path: str,
        device_addr: str,
        t0: float,
        allow_phy_change: bool = True,
        log_fn: Optional[Callable[[float, str], None]] = None,
    ) -> None:
        """Configures kernel `LE 1M PHY` & `20-35ms / 5000ms` LE connection params for Flipper.

        Linux `hci_core.c` defaults `le_supv_timeout` to `420ms` (`0x002a`) and
        BlueZ `gas.c` rejects Flipper's invalid `0x2A04` PPCP (`supervisor_timeout=0`), causing
        Flipper `gap.c` to force `7.5ms` + `LE 2M PHY` while echoing `420ms` back to Linux.
        """
        emit = log_fn or self._conn_log
        if os.geteuid() != 0:
            return

        hci_name = adapter_path.rsplit("/", 1)[-1] or "hci0"
        m_idx = re.search(r"(\d+)$", hci_name)
        hci_index = int(m_idx.group(1)) if m_idx else 0

        try:
            af_bt = getattr(socket, "AF_BLUETOOTH", 31)
            bt_hci = getattr(socket, "BTPROTO_HCI", 1)
            dev_none = getattr(socket, "HCI_DEV_NONE", 0xFFFF)
            chan_ctrl = getattr(socket, "HCI_CHANNEL_CONTROL", 3)
            with socket.socket(af_bt, socket.SOCK_RAW, bt_hci) as msock:
                msock.bind((dev_none, chan_ctrl))
                st_info, rp_info = _mgmt_transact(msock, MGMT_OP_READ_INFO, hci_index)
                supp_mask = 0
                if st_info == 0 and len(rp_info) >= 17:
                    (supp_mask,) = struct.unpack_from("<I", rp_info, 9)

                if allow_phy_change and (supp_mask & MGMT_SETTING_PHY_CONFIGURATION):
                    st_gp, rp_gp = _mgmt_transact(
                        msock, MGMT_OP_GET_PHY_CONFIGURATION, hci_index
                    )
                    if st_gp == 0 and len(rp_gp) >= 12:
                        supp_p, conf_p, sel_p = struct.unpack_from("<III", rp_gp, 0)
                        desired_p = _compute_le_1m_phy_selection(supp_p, conf_p, sel_p)
                        if desired_p != sel_p:
                            st_sp, _ = _mgmt_transact(
                                msock,
                                MGMT_OP_SET_PHY_CONFIGURATION,
                                hci_index,
                                struct.pack("<I", desired_p),
                            )
                            emit(
                                t0,
                                f"mgmt {hci_name} phy 0x{sel_p:x}->0x{desired_p:x} (LE_1M) status=0x{st_sp:02x}",
                            )

                if device_addr:
                    # Single param_count=2 call avoids `mgmt.c:8244` `hci_conn_params_clear_disabled`.
                    cp_payload = _pack_mgmt_load_conn_params(device_addr)
                    st_cp, _ = _mgmt_transact(
                        msock, MGMT_OP_LOAD_CONN_PARAM, hci_index, cp_payload
                    )
                    emit(
                        t0,
                        f"mgmt {hci_name} conn_param {device_addr} -> 15-27.5ms supv=5000ms (status=0x{st_cp:02x})",
                    )
                    if not allow_phy_change and st_cp == 0:
                        # param_count=1 triggers live L2CAP connection-parameter update on existing link.
                        upd_payload = _pack_mgmt_load_conn_params(
                            device_addr, addr_types=(BDADDR_LE_PUBLIC,)
                        )
                        _mgmt_transact(msock, MGMT_OP_LOAD_CONN_PARAM, hci_index, upd_payload)
        except Exception as exc:
            emit(t0, f"mgmt {hci_name} link tuning skipped: {exc}")

    def unpair(self, target: Optional[str] = None) -> Dict[str, object]:
        """Removes a Flipper's BlueZ bond/cache cleanly so fresh pairing can proceed."""
        adapter = self._find_adapter_path()
        self._ensure_adapter_powered(adapter)
        dev = self._resolve_target_device(target, scan_if_missing=False)
        if self._connected and self._dev_path == dev.object_path:
            self.disconnect()
        self._remove_device(adapter, dev.object_path)
        return {
            "removed": True,
            "address": dev.address,
            "name": dev.name,
        }

    def pair(
        self,
        target: Optional[str],
        pin_provider: Callable[[str, str], str],
        timeout_sec: float = 45.0,
        cancel_cb: Optional[Callable[[], None]] = None,
        force: bool = False,
    ) -> BleDeviceSummary:
        """Bonds with Flipper Zero using an isolated BlueZ DefaultAgent (`KeyboardOnly`).

        Handles:
          - Async `Device1.Pair()` + threaded `RequestPasskey` marshaled back onto
            the thread-default `GLib.MainContext` so GDBus never deadlocks or races across threads.
          - `RequestDefaultAgent` + `Adapter1.Pairable=True` + `"KeyboardOnly"` IO
            capability so Flipper's `aci_gap_slave_security_req()` routes Passkey Entry to `z2zd`.
          - Fresh LE discovery + 350ms post-`StopDiscovery` settle + stale-cache purge.
          - `btmgmt sc off` LE Legacy Passkey Pairing to avoid `BCM4343A2` + `STM32WB55`
            80-PDU LESC Passkey Entry / `DHKey Check` disconnect (`AuthenticationCanceled`).
          - Preserves the live link after `Pair()` (no post-pair adapter power-cycle)
            and supports `force=True` bond reset when Flipper/BlueZ bonds drift out of sync.
        """
        Gio, GLib = _load_gio()
        t0 = time.monotonic()
        adapter = self._find_adapter_path()
        self._ensure_adapter_powered(adapter, pairable=True)

        dev = self._resolve_target_device(target, scan_if_missing=True)
        if dev.paired and not force:
            self._set_device_trusted(dev.object_path, True)
            dev.trusted = True
            return dev

        target_addr = dev.address
        target_name = dev.name
        self._pair_log(
            t0,
            f"Starting BLE pair with {target_name} ({target_addr}) on {adapter} (force={force})",
        )

        # Purge any stale unpaired cache (or out-of-sync old bond when force=True)
        if force and dev.paired:
            if self._connected and self._dev_path == dev.object_path:
                self.disconnect()
            self._remove_device(adapter, dev.object_path)
            time.sleep(0.3)
        elif not dev.connected:
            self._remove_device(adapter, dev.object_path)
            time.sleep(0.2)

        sc_toggled_off = self._set_adapter_sc(adapter, False, t0, allow_power_cycle=True)
        if sc_toggled_off:
            self._sc_disabled_by_us = True
        self._ensure_adapter_powered(adapter, pairable=True)
        self._tune_le_link_params(
            adapter, target_addr, t0, allow_phy_change=True, log_fn=self._pair_log
        )

        ctx = GLib.MainContext.new()
        ctx.push_thread_default()
        reg_id = 0
        agent_registered = False
        bus = None
        node_info = Gio.DBusNodeInfo.new_for_xml(AGENT_XML)
        iface_info = node_info.lookup_interface("org.bluez.Agent1")

        pending_glib_ops: "queue.Queue[Callable[[], None]]" = queue.Queue()
        pin_state = {
            "prompted": False,
            "submitted": False,
            "cancelled_by_bluez": False,
        }

        def _drain_glib_ops() -> None:
            while True:
                try:
                    fn = pending_glib_ops.get_nowait()
                except queue.Empty:
                    break
                try:
                    fn()
                except Exception as exc:
                    self._pair_log(t0, f"GLib op error: {exc}")

        def _answer_passkey_async(invocation, is_numeric: bool, dev_name: str, dev_addr: str):
            def _worker():
                try:
                    raw_pin = pin_provider(dev_name, dev_addr).strip()
                    digits = "".join(ch for ch in raw_pin if ch.isdigit())
                    if len(digits) != 6:
                        raise ValueError(
                            "Passkey entry cancelled"
                            if not digits
                            else f"Expected 6-digit passkey, got {len(digits)} digits"
                        )
                    pin_state["submitted"] = True
                    lead_zero = digits.startswith("0")
                    self._pair_log(
                        t0,
                        f"Submitting 6-digit passkey to BlueZ (numeric={is_numeric}, leading_zero={lead_zero})",
                    )
                    if is_numeric:
                        val = GLib.Variant("(u)", (int(digits),))
                    else:
                        val = GLib.Variant("(s)", (digits,))
                    pending_glib_ops.put(lambda: invocation.return_value(val))
                except Exception as exc:
                    self._pair_log(t0, f"Rejecting passkey request: {exc}")
                    err_msg = str(exc)
                    pending_glib_ops.put(
                        lambda: invocation.return_dbus_error("org.bluez.Error.Rejected", err_msg)
                    )
                finally:
                    ctx.wakeup()

            threading.Thread(target=_worker, name="z2z-pin-prompt", daemon=True).start()

        def on_method_call(_conn, _sender, _path, _iface, method_name, params, invocation):
            self._pair_log(t0, f"Agent1.{method_name}{params}")
            try:
                if method_name == "RequestPasskey":
                    pin_state["prompted"] = True
                    _answer_passkey_async(invocation, True, target_name, target_addr)
                elif method_name == "RequestPinCode":
                    pin_state["prompted"] = True
                    _answer_passkey_async(invocation, False, target_name, target_addr)
                elif method_name == "Cancel":
                    pin_state["cancelled_by_bluez"] = True
                    invocation.return_value(None)
                    if cancel_cb is not None:
                        try:
                            cancel_cb()
                        except Exception:
                            pass
                else:
                    # RequestConfirmation / AuthorizeService / DisplayPasskey / Release
                    invocation.return_value(None)
            except Exception as exc:
                invocation.return_dbus_error("org.bluez.Error.Rejected", str(exc))

        try:
            sys_addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM, None)
            bus = Gio.DBusConnection.new_for_address_sync(
                sys_addr,
                Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                None,
                None,
            )
            reg_fn = getattr(bus, "register_object_with_closures2", bus.register_object)
            reg_id = reg_fn(AGENT_PATH, iface_info, on_method_call, None, None)

            bus.call_sync(
                "org.bluez",
                "/org/bluez",
                "org.bluez.AgentManager1",
                "RegisterAgent",
                GLib.Variant("(os)", (AGENT_PATH, "KeyboardOnly")),
                None,
                Gio.DBusCallFlags.NONE,
                5000,
                None,
            )
            agent_registered = True

            try:
                bus.call_sync(
                    "org.bluez",
                    "/org/bluez",
                    "org.bluez.AgentManager1",
                    "RequestDefaultAgent",
                    GLib.Variant("(o)", (AGENT_PATH,)),
                    None,
                    Gio.DBusCallFlags.NONE,
                    5000,
                    None,
                )
            except Exception as exc:
                self._pair_log(t0, f"RequestDefaultAgent warning: {exc}")

            last_err = ""
            max_attempts = 4
            for attempt in range(max_attempts):
                min_scan = 0.8 if attempt > 0 else 0.5
                wait_scan = 5.5 if attempt > 0 else 4.5
                refreshed = self._refresh_le_presence(
                    adapter, target_addr, max_wait=wait_scan, min_scan_sec=min_scan
                )
                if refreshed is not None:
                    dev = refreshed
                else:
                    dev = self._resolve_target_device(target_addr, scan_if_missing=True)

                # Never short-circuit `attempt == 0` when `force=True` just removed an old bond
                if attempt > 0 and dev.paired:
                    last_err = ""
                    break

                pair_done = {"finished": False, "error": None}
                pin_state["prompted"] = False
                pin_state["submitted"] = False
                pin_state["cancelled_by_bluez"] = False

                def on_pair_ready(source, res, _user_data):
                    try:
                        source.call_finish(res)
                    except Exception as exc:
                        if "AlreadyExists" not in str(exc):
                            pair_done["error"] = exc
                    pair_done["finished"] = True
                    ctx.wakeup()

                self._pair_log(
                    t0,
                    f"Attempt {attempt + 1}/{max_attempts}: calling Device1.Pair({dev.object_path}) rssi={dev.rssi}",
                )
                bus.call(
                    "org.bluez",
                    dev.object_path,
                    "org.bluez.Device1",
                    "Pair",
                    None,
                    None,
                    Gio.DBusCallFlags.NONE,
                    int(timeout_sec * 1000),
                    None,
                    on_pair_ready,
                    None,
                )

                deadline = time.monotonic() + timeout_sec + 2.0
                while not pair_done["finished"] and time.monotonic() < deadline:
                    _drain_glib_ops()
                    if not ctx.iteration(False):
                        time.sleep(0.02)
                _drain_glib_ops()

                if not pair_done["finished"]:
                    raise TimeoutError("Timed out waiting for Flipper BLE passkey")

                if pair_done["error"] is None:
                    self._pair_log(t0, f"Device1.Pair succeeded on attempt {attempt + 1}")
                    last_err = ""
                    break

                last_err = clean_dbus_error(pair_done["error"])
                self._pair_log(
                    t0,
                    f"Attempt {attempt + 1} error: {last_err} "
                    f"(prompted={pin_state['prompted']}, submitted={pin_state['submitted']}, "
                    f"bluez_cancel={pin_state['cancelled_by_bluez']})",
                )

                # Check if bond succeeded despite trailing disconnect notification
                try:
                    chk = self._resolve_target_device(target_addr, scan_if_missing=False)
                    if chk.paired:
                        self._pair_log(t0, "Device1.Paired=True verified after Pair callback")
                        dev = chk
                        last_err = ""
                        break
                except Exception:
                    pass

                # If custom SC-only firmware rejected SC=off before passkey entry, restore SC=on & retry
                if (
                    sc_toggled_off
                    and not pin_state["prompted"]
                    and "AuthenticationFailed" in last_err
                    and attempt < (max_attempts - 1)
                ):
                    self._set_adapter_sc(adapter, True, t0)
                    sc_toggled_off = False
                    self._remove_device(adapter, dev.object_path)
                    time.sleep(1.2)
                    continue

                # Never re-prompt after the user already submitted a PIN; allow automatic retry
                # if Flipper cancelled BEFORE PIN submit (`aci_gap_allow_rebond` / GAP reset)
                if pin_state["submitted"] or attempt >= (max_attempts - 1):
                    break
                if pin_state["prompted"] and not pin_state["cancelled_by_bluez"]:
                    break

                # Stale half-bond / Flipper GAP profile reset -> purge & wait ~1.8s for Flipper re-adv
                if any(
                    k in last_err
                    for k in (
                        "AuthenticationFailed",
                        "AuthenticationRejected",
                        "AuthenticationCanceled",
                        "AlreadyExists",
                    )
                ):
                    self._remove_device(adapter, dev.object_path)
                    time.sleep(1.8)
                    continue

                # Transient BCM4343A2 / Flipper post-reset adv miss (`Page Timeout` / `0x3e`) -> backoff
                if any(
                    k in last_err
                    for k in (
                        "le-connection-abort",
                        "ConnectionAttemptFailed",
                        "Page Timeout",
                        "Timeout",
                        "InProgress",
                        "Software caused connection abort",
                        "Host is down",
                    )
                ):
                    time.sleep(1.2)
                    continue

                break

            if last_err:
                if "AuthenticationCanceled" in last_err and pin_state["cancelled_by_bluez"] and not pin_state["submitted"]:
                    raise RuntimeError(
                        "Pair failed: Flipper disconnected before PIN submit (forget old bonds on Flipper BT menu)"
                    )
                raise RuntimeError(f"Pair failed: {last_err}")

            self._set_device_trusted(dev.object_path, True)
            self._last_pair_ok_mono = time.monotonic()
            updated = self._resolve_target_device(target_addr, scan_if_missing=False)
            return updated
        finally:
            if agent_registered and bus is not None:
                try:
                    bus.call_sync(
                        "org.bluez",
                        "/org/bluez",
                        "org.bluez.AgentManager1",
                        "UnregisterAgent",
                        GLib.Variant("(o)", (AGENT_PATH,)),
                        None,
                        Gio.DBusCallFlags.NONE,
                        2000,
                        None,
                    )
                except Exception:
                    pass
            if reg_id and bus is not None:
                try:
                    bus.unregister_object(reg_id)
                except Exception:
                    pass
            if bus is not None:
                try:
                    bus.close_sync(None)
                except Exception:
                    pass
            ctx.pop_thread_default()
            # Keep `hci0` in `sc off` after pairing! Re-enabling `sc on` causes
            # Linux `smp_cmd_security_req` to upgrade Flipper's `SMP_AUTH_SC` request to
            # `BT_SECURITY_FIPS`, rejecting our stored `BT_SECURITY_HIGH` Legacy LTK on connect!

    def _set_device_trusted(self, dev_path: str, trusted: bool) -> None:
        Gio, GLib = _load_gio()
        bus = self._get_bus()
        bus.call_sync(
            "org.bluez",
            dev_path,
            "org.freedesktop.DBus.Properties",
            "Set",
            GLib.Variant("(ssv)", ("org.bluez.Device1", "Trusted", GLib.Variant("b", trusted))),
            None,
            Gio.DBusCallFlags.NONE,
            3000,
            None,
        )

    def _read_gatt_char(self, char_path: str) -> bytes:
        Gio, GLib = _load_gio()
        bus = self._get_bus()
        res = bus.call_sync(
            "org.bluez",
            char_path,
            "org.bluez.GattCharacteristic1",
            "ReadValue",
            GLib.Variant("(a{sv})", ({},)),
            GLib.VariantType.new("(ay)"),
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )
        return bytes(res.unpack()[0])

    def _close_rx_write_fd(self) -> None:
        fd = self._rx_write_fd
        self._rx_write_fd = -1
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass

    def _try_acquire_rx_write_fd(self, nbus: Any) -> bool:
        """Acquires a direct BlueZ `SOCK_SEQPACKET` FD via `AcquireWrite` for low-latency RX writes."""
        if (
            self._tx_write_mode != "command"
            or not self._char_rx
            or nbus is None
            or not hasattr(nbus, "call_with_unix_fd_list_sync")
        ):
            return False
        self._close_rx_write_fd()
        try:
            Gio, GLib = _load_gio()
            res, out_fd_list = nbus.call_with_unix_fd_list_sync(
                "org.bluez",
                self._char_rx,
                "org.bluez.GattCharacteristic1",
                "AcquireWrite",
                GLib.Variant("(a{sv})", ({},)),
                GLib.VariantType.new("(hq)"),
                Gio.DBusCallFlags.NONE,
                3000,
                None,
                None,
            )
            if out_fd_list is None:
                return False
            fd_idx, write_mtu = res.unpack()
            fd = out_fd_list.get(int(fd_idx))
            if fd < 0:
                return False
            self._rx_write_fd = int(fd)
            if int(write_mtu) >= 23:
                self._att_mtu = int(write_mtu)
            return True
        except Exception:
            self._close_rx_write_fd()
            return False

    def _write_gatt_char(
        self, char_path: str, chunk: bytes, mode: Optional[str] = None
    ) -> None:
        eff_mode = mode or self._tx_write_mode or "command"
        if eff_mode == "command" and char_path == self._char_rx and self._rx_write_fd >= 0:
            try:
                os.write(self._rx_write_fd, bytes(chunk))
                return
            except BlockingIOError:
                _, ready, _ = select.select([], [self._rx_write_fd], [], 2.0)
                if ready:
                    try:
                        os.write(self._rx_write_fd, bytes(chunk))
                        return
                    except OSError as exc2:
                        self._close_rx_write_fd()
                        if exc2.errno in (errno.EPIPE, errno.ECONNRESET, errno.ENOTCONN) or not self._connected:
                            self._connected = False
                            raise ConnectionError(f"BLE AcquireWrite socket closed ({exc2})") from exc2
                else:
                    self._close_rx_write_fd()
            except OSError as exc:
                # BlueZ rejects D-Bus `WriteValue` with NotPermitted until the acquired FD is closed.
                self._close_rx_write_fd()
                if exc.errno in (errno.EPIPE, errno.ECONNRESET, errno.ENOTCONN) or not self._connected:
                    self._connected = False
                    raise ConnectionError(f"BLE AcquireWrite socket closed ({exc})") from exc

        Gio, GLib = _load_gio()
        bus = self._get_bus()
        opts = {"type": GLib.Variant("s", eff_mode)}
        bus.call_sync(
            "org.bluez",
            char_path,
            "org.bluez.GattCharacteristic1",
            "WriteValue",
            GLib.Variant("(aya{sv})", (list(chunk), opts)),
            None,
            Gio.DBusCallFlags.NONE,
            5000,
            None,
        )

    def _stop_notify_pump(self, stop_notify: bool = True) -> None:
        self._close_rx_write_fd()
        Gio, _ = _load_gio()
        self._session_gen += 1
        nbus = self._notify_bus
        self._notify_bus = None
        if stop_notify and nbus is not None:
            for cpath in (self._char_tx, self._char_flow):
                if cpath:
                    try:
                        nbus.call_sync(
                            "org.bluez",
                            cpath,
                            "org.bluez.GattCharacteristic1",
                            "StopNotify",
                            None,
                            None,
                            Gio.DBusCallFlags.NONE,
                            1500,
                            None,
                        )
                    except Exception:
                        pass
        loop = self._loop
        self._loop = None
        ctx = self._loop_ctx
        self._loop_ctx = None
        if loop is not None:
            try:
                loop.quit()
            except Exception:
                pass
        if ctx is not None:
            try:
                ctx.wakeup()
            except Exception:
                pass
        thr = self._loop_thread
        self._loop_thread = None
        if thr is not None:
            thr.join(timeout=1.5)

    def _start_notify_pump(
        self,
        session_gen: int,
        dev_path: str,
        char_tx: str,
        char_flow: str,
        flow_obj: CreditFlowController,
    ) -> None:
        Gio, GLib = _load_gio()
        ready = threading.Event()
        err_box: List[Exception] = []

        def worker():
            ctx = GLib.MainContext.new()
            ctx.push_thread_default()
            loop = GLib.MainLoop.new(ctx, False)
            notify_bus = None
            sub_id = 0
            try:
                # Dedicated private D-Bus connection per BLE session: closing it on teardown
                # causes BlueZ (`src/gatt-client.c` `notify_client_disconnect`) to purge any
                # stale `notify_clients` sender entry instead of racing CCCD writes on reconnect.
                addr = Gio.dbus_address_get_for_bus_sync(Gio.BusType.SYSTEM, None)
                notify_bus = Gio.DBusConnection.new_for_address_sync(
                    addr,
                    Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
                    | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION,
                    None,
                    None,
                )

                def on_props_changed(_conn, _sender, obj_path, _iface, _sig, params):
                    if session_gen != self._session_gen:
                        return
                    iface_name, changed, _invalid = params.unpack()
                    if iface_name == "org.bluez.Device1" and obj_path == dev_path:
                        if "Connected" in changed and not changed["Connected"]:
                            self._connected = False
                            flow_obj.close()
                            self._fire_disconnect_cb()
                    elif iface_name == "org.bluez.GattCharacteristic1" and "Value" in changed:
                        raw = bytes(changed["Value"])
                        if obj_path == char_tx:
                            if raw:
                                self._rx_chunks += 1
                                self._rx_bytes += len(raw)
                                cb = self._rx_cb
                                if cb is not None:
                                    cb(raw)
                        elif obj_path == char_flow:
                            flow_obj.update_from_be_bytes(raw)

                sub_id = notify_bus.signal_subscribe(
                    "org.bluez",
                    "org.freedesktop.DBus.Properties",
                    "PropertiesChanged",
                    None,
                    None,
                    Gio.DBusSignalFlags.NONE,
                    on_props_changed,
                )
                self._notify_bus = notify_bus
                self._loop_ctx = ctx
                self._loop = loop
                self._sub_id = sub_id
            except Exception as exc:
                err_box.append(exc)
                ready.set()
                if notify_bus is not None:
                    try:
                        notify_bus.close_sync(None)
                    except Exception:
                        pass
                ctx.pop_thread_default()
                return

            ready.set()
            try:
                loop.run()
            finally:
                if notify_bus is not None:
                    if sub_id:
                        try:
                            notify_bus.signal_unsubscribe(sub_id)
                        except Exception:
                            pass
                    try:
                        notify_bus.close_sync(None)
                    except Exception:
                        pass
                ctx.pop_thread_default()

        self._loop_thread = threading.Thread(target=worker, name="z2z-ble-glib", daemon=True)
        self._loop_thread.start()
        if not ready.wait(timeout=4.0):
            raise TimeoutError("Timed out starting BLE D-Bus notification pump")
        if err_box:
            raise err_box[0]

    def check_link_alive(self) -> bool:
        if not self.is_connected or not self._dev_path:
            self._connected = False
            return False
        try:
            d_live = (
                self._get_managed_objects()
                .get(self._dev_path, {})
                .get("org.bluez.Device1", {})
            )
            if bool(d_live.get("Connected")):
                return True
        except Exception:
            pass
        self._connected = False
        self._flow.close()
        self._fire_disconnect_cb()
        return False

    def connect(self, target: Optional[str] = None) -> Dict[str, object]:
        Gio, _ = _load_gio()
        with self._lock:
            if self.is_connected and self._dev_path:
                if self.check_link_alive():
                    return self.link_info()

            self._connected = False
            self._flow.close()
            self._stop_notify_pump(stop_notify=True)

            t0 = time.monotonic()
            adapter = self._find_adapter_path()
            self._ensure_adapter_powered(adapter)
            dev = self._resolve_target_device(target, scan_if_missing=True)
            if not dev.paired:
                dev_label = dev.name if "flipper" in dev.name.lower() else f"Flipper {dev.name}"
                raise RuntimeError(
                    f"{dev_label} ({dev.address}) is not paired yet; press [5:PAIR] or run `z2z pair`"
                )

            # Clear BlueZ `5.87` ghost/unresolved `Connected=True` state up front
            # (unless we are inside the 8s post-`pair()` temporary-link handoff window).
            fresh_pair_handoff = (time.monotonic() - self._last_pair_ok_mono) < 8.0
            if dev.connected and not fresh_pair_handoff:
                try:
                    init_d = (
                        self._get_managed_objects()
                        .get(dev.object_path, {})
                        .get("org.bluez.Device1", {})
                    )
                    if init_d.get("Connected") and not init_d.get("ServicesResolved", False):
                        self._conn_log(
                            t0,
                            f"Clearing stale unresolved Connected=True state on {dev.object_path}",
                        )
                        self._force_disconnect_device(
                            adapter, dev.object_path, t0, log_fn=self._conn_log
                        )
                        dev = self._resolve_target_device(dev.address, scan_if_missing=False)
                except Exception:
                    pass

            # Ensure `hci0` has verified `sc off` BEFORE `Device1.Connect()`!
            self._set_adapter_sc(
                adapter,
                False,
                t0,
                allow_power_cycle=not dev.connected,
                log_fn=self._conn_log,
            )
            sc_cycled = self._last_sc_power_cycled
            if sc_cycled:
                self._ensure_adapter_powered(adapter)
            self._tune_le_link_params(
                adapter,
                dev.address,
                t0,
                allow_phy_change=not dev.connected,
                log_fn=self._conn_log,
            )

            bus = self._get_bus()
            mgmt_watch = self._open_mgmt_watch_socket()

            def _close_mgmt_watch() -> None:
                nonlocal mgmt_watch
                if mgmt_watch is not None:
                    try:
                        mgmt_watch.close()
                    except Exception:
                        pass
                    mgmt_watch = None

            def _raise_bond_key_missing(
                cur_path: str, cause: Optional[BaseException] = None
            ) -> None:
                _close_mgmt_watch()
                self._force_disconnect_device(adapter, cur_path, t0, log_fn=self._conn_log)
                err = RuntimeError(
                    "Flipper rejected BLE bond key (PIN or Key Missing after crash/reset) — "
                    "press [5:REPAIR] in LINK tab or run `z2z pair --force`"
                )
                if cause is not None:
                    raise err from cause
                raise err

            def _open_le_link(cur_dev: BleDeviceSummary) -> BleDeviceSummary:
                conn_err = ""
                for attempt in range(3):
                    # Refresh LE presence BEFORE Attempt 1 when `rssi <= -95`
                    # (cold boot cache) OR when `_set_adapter_sc` power-cycled `hci0` (which wipes
                    # kernel `le_seen` even if BlueZ D-Bus still caches an older RSSI!).
                    if (
                        attempt > 0 or cur_dev.rssi <= -95 or (attempt == 0 and sc_cycled)
                    ) and not cur_dev.connected:
                        wait_s = 5.0 if attempt == 0 else 5.5
                        refreshed = self._refresh_le_presence(
                            adapter, cur_dev.address, max_wait=wait_s
                        )
                        if refreshed is not None:
                            cur_dev = refreshed
                        if cur_dev.connected:
                            conn_err = ""
                            break
                    self._conn_log(
                        t0,
                        f"Attempt {attempt + 1}/3: calling Device1.Connect({cur_dev.object_path}) rssi={cur_dev.rssi}",
                    )
                    try:
                        bus.call_sync(
                            "org.bluez",
                            cur_dev.object_path,
                            "org.bluez.Device1",
                            "Connect",
                            None,
                            None,
                            Gio.DBusCallFlags.NONE,
                            20000,
                            None,
                        )
                        conn_err = ""
                        # Allow Flipper's `aci_gap_slave_security_req()` -> `HCI_Encryption_Change` to settle
                        time.sleep(0.35)
                        if self._drain_mgmt_auth_failure(
                            mgmt_watch, adapter, cur_dev.address, wait_sec=0.05
                        ):
                            _raise_bond_key_missing(cur_dev.object_path)
                        break
                    except Exception as exc:
                        if "rejected BLE bond key" in str(exc):
                            raise
                        conn_err = clean_dbus_error(exc)
                        self._conn_log(t0, f"Connect attempt {attempt + 1} error: {conn_err}")
                        if self._drain_mgmt_auth_failure(
                            mgmt_watch, adapter, cur_dev.address, wait_sec=0.12
                        ):
                            _raise_bond_key_missing(cur_dev.object_path, exc)
                        # ONLY check `live_now.connected` on D-Bus `Timeout`! On `le-connection-abort`,
                        # BlueZ briefly leaves `Connected=True` for ~100ms during teardown, which
                        # would otherwise hijack `_open_le_link`'s retry loop!
                        if "Timeout" in conn_err or "Timed out" in conn_err:
                            try:
                                time.sleep(0.35)
                                live_now = self._resolve_target_device(
                                    cur_dev.address, scan_if_missing=False
                                )
                                if live_now.connected:
                                    cur_dev = live_now
                                    conn_err = ""
                                    break
                            except Exception:
                                pass
                        if any(
                            k in conn_err
                            for k in (
                                "AuthenticationFailed",
                                "AuthenticationRejected",
                                "Insufficient Authentication",
                            )
                        ):
                            _raise_bond_key_missing(cur_dev.object_path, exc)
                        if attempt < 2 and any(
                            k in conn_err
                            for k in (
                                "le-connection-abort",
                                "ConnectionAttemptFailed",
                                "Page Timeout",
                                "Timeout",
                                "Timed out",
                                "InProgress",
                                "Software caused connection abort",
                                "Host is down",
                                "AuthenticationCanceled",
                            )
                        ):
                            self._force_disconnect_device(
                                adapter, cur_dev.object_path, t0, log_fn=self._conn_log
                            )
                            cur_dev.connected = False
                            time.sleep(0.35)
                            continue
                        break
                if conn_err:
                    self._force_disconnect_device(
                        adapter, cur_dev.object_path, t0, log_fn=self._conn_log
                    )
                    raise RuntimeError(f"BLE connect failed: {conn_err}")
                return cur_dev

            last_gatt_err: Optional[Exception] = None
            for gatt_attempt in range(2):
                try:
                    if gatt_attempt > 0:
                        self._stop_notify_pump(stop_notify=False)
                        time.sleep(0.45)
                        dev = self._resolve_target_device(dev.address, scan_if_missing=False)

                    was_temp_pair_link = (
                        dev.connected and gatt_attempt == 0 and fresh_pair_handoff
                    )
                    if not dev.connected:
                        dev = _open_le_link(dev)

                    # Wait for GATT service discovery (`ServicesResolved`)
                    deadline = time.monotonic() + 12.0
                    objs = {}
                    reopened_after_pair = False
                    while time.monotonic() < deadline:
                        objs = self._get_managed_objects()
                        d_props = objs.get(dev.object_path, {}).get("org.bluez.Device1", {})
                        if d_props.get("Connected") and d_props.get("ServicesResolved"):
                            break
                        if not d_props.get("Connected", False):
                            if was_temp_pair_link and not reopened_after_pair:
                                reopened_after_pair = True
                                self._conn_log(
                                    t0,
                                    "Temporary Pair() link closed; opening persistent Device1.Connect()",
                                )
                                time.sleep(0.35)
                                self._set_adapter_sc(
                                    adapter, False, t0, allow_power_cycle=True, log_fn=self._conn_log
                                )
                                self._ensure_adapter_powered(adapter)
                                self._tune_le_link_params(
                                    adapter,
                                    dev.address,
                                    t0,
                                    allow_phy_change=True,
                                    log_fn=self._conn_log,
                                )
                                dev = _open_le_link(dev)
                                deadline = time.monotonic() + 12.0
                                continue
                            raise RuntimeError(
                                "Flipper disconnected during GATT setup (if Flipper forgot bond, press [5:REPAIR])"
                            )
                        time.sleep(0.2)
                    else:
                        raise TimeoutError("BlueZ did not finish resolving Flipper GATT services")

                    # Locate Serial/RPC GATT characteristics
                    self._char_tx = ""
                    self._char_rx = ""
                    self._char_flow = ""
                    self._char_rpc_state = ""
                    self._char_batt = ""
                    rx_flags: List[str] = []
                    prefix = dev.object_path + "/"
                    for path, ifaces in objs.items():
                        if not path.startswith(prefix):
                            continue
                        ch = ifaces.get("org.bluez.GattCharacteristic1")
                        if not ch:
                            continue
                        cuuid = str(ch.get("UUID", "")).lower()
                        if cuuid == UUID_CHAR_TX:
                            self._char_tx = path
                        elif cuuid == UUID_CHAR_RX:
                            self._char_rx = path
                            rx_flags = [str(f).lower() for f in ch.get("Flags", [])]
                        elif cuuid == UUID_CHAR_FLOW:
                            self._char_flow = path
                        elif cuuid == UUID_CHAR_RPC_STATE:
                            self._char_rpc_state = path
                        elif cuuid == UUID_BATTERY_LEVEL:
                            self._char_batt = path

                    if not (self._char_tx and self._char_rx and self._char_flow):
                        raise RuntimeError(
                            "Flipper Serial GATT characteristics not accessible (press [5:REPAIR] to re-bond)"
                        )

                    # Flipper's BLE Serial RX (`...62fe0000`) is designed for
                    # `WRITE_WITHOUT_RESPONSE` (`type="command"`) paced by `CreditFlowController`.
                    # Using `type="request"` causes BlueZ (`gatt-client.c:1102`) to emit
                    # `ATT_OP_PREP_WRITE_REQ` (`start_long_write`) or collide with concurrent TX
                    # Indications on STM32WB55 CPU2, failing with `ATT error: 0x0e` (`Unlikely Error`).
                    self._tx_write_mode = (
                        "command"
                        if (not rx_flags or "write-without-response" in rx_flags)
                        else "request"
                    )

                    # On bonded reconnects, BlueZ marks `ServicesResolved=True` from disk
                    # cache BEFORE `ACI_GAP_PAIRING_COMPLETE_VSEVT_CODE` (`GapEventTypeConnected`) has
                    # opened `bt->rpc_session` on Flipper (`bt.c:273`) or finished link encryption, AND
                    # while BlueZ `battery.c` probes Battery CCCD. Poll authenticated `_char_flow` +
                    # `_char_rpc_state` (do NOT swallow errors!) until encryption and `rpc_session` are up.
                    init_flow = b""
                    flow_err = ""
                    for f_try in range(7):
                        try:
                            init_flow = self._read_gatt_char(self._char_flow)
                            flow_err = ""
                            if len(init_flow) >= 4 and struct.unpack(">I", init_flow[:4])[0] > 0:
                                break
                            time.sleep(0.25)
                        except Exception as exc:
                            flow_err = clean_dbus_error(exc)
                            if f_try < 6 and not any(
                                k in flow_err for k in ("Not connected", "Does Not Exist")
                            ):
                                time.sleep(0.35)
                                continue
                            break
                    if flow_err:
                        if any(
                            k in flow_err
                            for k in (
                                "Authentication",
                                "Insufficient",
                                "NotPermitted",
                                "NotAuthorized",
                            )
                        ):
                            raise RuntimeError(
                                f"BLE authenticated GATT read rejected ({flow_err}); press [5:REPAIR] and forget Kali on Flipper"
                            )
                        raise RuntimeError(
                            f"Flipper disconnected during GATT auth ({flow_err})"
                        )

                    rpc_active = 1
                    if self._char_rpc_state:
                        for s_try in range(6):
                            try:
                                st = self._read_gatt_char(self._char_rpc_state)
                                # `SerialServiceRpcStatus` is a 1-byte `-fshort-enums` enum on STM32;
                                # bytes 1..3 hold uninitialized stack bytes (`ble_svc_serial_update_rpc_char`).
                                rpc_active = int(st[0]) if st else 0
                                if rpc_active != 0:
                                    break
                            except Exception as exc:
                                s_err = clean_dbus_error(exc)
                                if "Not connected" in s_err:
                                    raise RuntimeError(
                                        "Flipper disconnected while checking RPC state"
                                    ) from exc
                            time.sleep(0.25)
                        if rpc_active == 0:
                            raise RuntimeError(
                                "Flipper BLE RPC session is inactive/busy (close USB qFlipper/CLI on Flipper)"
                            )

                    flow_obj = CreditFlowController(
                        initial_credits=1024, max_chunk=self._max_chunk_cap
                    )
                    if len(init_flow) >= 4:
                        parsed_c = flow_obj.update_from_be_bytes(init_flow)
                        if parsed_c <= 0:
                            flow_obj.update_from_be_bytes(struct.pack(">I", 1024))

                    self._dev_path = dev.object_path
                    self._dev_info = dev
                    self._rx_chunks = 0
                    self._rx_bytes = 0
                    self._session_gen += 1
                    cur_gen = self._session_gen
                    self._start_notify_pump(
                        cur_gen, dev.object_path, self._char_tx, self._char_flow, flow_obj
                    )

                    nbus = self._notify_bus
                    if nbus is None:
                        raise RuntimeError("BLE notify bus failed to initialize")

                    # Subscribe to Flow Notify (`0x0001`) and TX Indicate (`0x0002`) on private `nbus`
                    for cpath in (self._char_flow, self._char_tx):
                        for n_try in range(4):
                            try:
                                nbus.call_sync(
                                    "org.bluez",
                                    cpath,
                                    "org.bluez.GattCharacteristic1",
                                    "StartNotify",
                                    None,
                                    None,
                                    Gio.DBusCallFlags.NONE,
                                    5000,
                                    None,
                                )
                                break
                            except Exception as exc:
                                emsg = clean_dbus_error(exc)
                                if "Already notifying" in emsg or "AlreadyPermitted" in emsg:
                                    break
                                if n_try < 3 and "Not connected" not in emsg:
                                    time.sleep(0.3)
                                    continue
                                raise RuntimeError(f"BLE StartNotify failed: {emsg}") from exc

                    # Allow STM32WB55 TX Indication CCCD (`0x0002`), BlueZ `gas.c`, and `battery.c`
                    # probes to settle before verifying live Flow / MTU state.
                    time.sleep(0.15)
                    try:
                        fin_flow = self._read_gatt_char(self._char_flow)
                        if len(fin_flow) >= 4 and struct.unpack(">I", fin_flow[:4])[0] > 0:
                            flow_obj.update_from_be_bytes(fin_flow)
                    except Exception as fin_exc:
                        fin_err = clean_dbus_error(fin_exc)
                        if any(k in fin_err for k in ("Not connected", "Does Not Exist")):
                            raise RuntimeError(
                                "Flipper disconnected before BLE GATT ready"
                            ) from fin_exc
                        self._conn_log(
                            t0,
                            f"post-StartNotify flow check skipped ({fin_err}); keeping credits={flow_obj.credits}",
                        )

                    live_objs = self._get_managed_objects()
                    d_check = live_objs.get(dev.object_path, {}).get("org.bluez.Device1", {})
                    if flow_obj.is_closed or not d_check.get("Connected", False):
                        raise RuntimeError("Flipper disconnected before BLE GATT ready")

                    rx_live = live_objs.get(self._char_rx, {}).get(
                        "org.bluez.GattCharacteristic1", {}
                    )
                    self._att_mtu = int(rx_live.get("MTU", 0) or 0)
                    self._close_rx_write_fd()
                    if self._tx_write_mode == "command":
                        self._try_acquire_rx_write_fd(nbus)
                    if self._att_mtu >= 23:
                        flow_obj.max_chunk = max(
                            20, min(self._max_chunk_cap, self._att_mtu - 3)
                        )

                    self._flow = flow_obj
                    self._connected = True
                    _close_mgmt_watch()
                    write_label = "acquire-fd" if self._rx_write_fd >= 0 else self._tx_write_mode
                    self._conn_log(
                        t0,
                        f"BLE RPC GATT ready on {dev.name} ({dev.address}) "
                        f"credits={self._flow.credits} mtu={self._att_mtu or '?'} "
                        f"chunk={self._flow.max_chunk} rpc_state={rpc_active} write={write_label}",
                    )
                    return self.link_info()
                except Exception as exc:
                    last_gatt_err = exc
                    self._connected = False
                    self._stop_notify_pump(stop_notify=False)
                    emsg = str(exc)
                    if "rejected BLE bond key" not in emsg and self._drain_mgmt_auth_failure(
                        mgmt_watch, adapter, dev.address, wait_sec=0.12
                    ):
                        _raise_bond_key_missing(dev.object_path, exc)
                    if self._force_disconnect_device(
                        adapter, dev.object_path, t0, log_fn=self._conn_log
                    ):
                        sc_cycled = sc_cycled or self._last_sc_power_cycled
                    if gatt_attempt == 0 and any(
                        k in emsg
                        for k in (
                            "disconnected during GATT",
                            "disconnected while checking",
                            "disconnected before BLE GATT",
                            "did not finish resolving Flipper GATT",
                            "StartNotify failed",
                            "ATT error: 0x0e",
                            "Unlikely",
                            "InProgress",
                        )
                    ):
                        self._conn_log(t0, f"Transient GATT setup drop ({emsg}); retrying once...")
                        continue
                    _close_mgmt_watch()
                    self._conn_log(t0, f"GATT setup aborted: {emsg}")
                    raise
            _close_mgmt_watch()
            assert last_gatt_err is not None
            raise last_gatt_err

    def tune_upload_profile(self, conservative: bool = False) -> None:
        """Adjusts `WRITE_WITHOUT_RESPONSE` chunk cap & pacing for `BCM4343A2` Wi-Fi/BLE coexistence."""
        if conservative:
            self._max_chunk_cap = 80
            self._chunk_delay_sec = 0.030
            self.inter_envelope_delay_sec = 0.020
        else:
            self._max_chunk_cap = 120
            self._chunk_delay_sec = 0.016
            self.inter_envelope_delay_sec = 0.012
        mtu_bound = (self._att_mtu - 3) if self._att_mtu >= 23 else self._max_chunk_cap
        if self._flow.max_chunk != 20:
            self._flow.max_chunk = max(20, min(self._max_chunk_cap, mtu_bound))

    def sync_flow_credits(self) -> int:
        """Re-reads Flipper's big-endian Flow-Control GATT char (`...63fe0000`) before multi-chunk uploads."""
        if not self.is_connected or not self._char_flow:
            return self._flow.credits
        try:
            raw = self._read_gatt_char(self._char_flow)
            if len(raw) >= 4:
                avail = struct.unpack(">I", raw[:4])[0]
                if avail > 0:
                    return self._flow.update_from_be_bytes(raw)
        except Exception as exc:
            emsg = clean_dbus_error(exc)
            if any(k in emsg for k in ("Not connected", "NotConnected", "Disconnected")):
                self._connected = False
                self._flow.close()
                self._fire_disconnect_cb()
                raise ConnectionError(f"BLE link dropped before upload: {emsg}") from exc
        return self._flow.credits

    def send_bytes(self, data: bytes, timeout_sec: float = 10.0) -> None:
        if not self.is_connected or not self._char_rx:
            self._connected = False
            raise ConnectionError("Not connected to Flipper Zero over BLE")

        chunks_sent = [0]
        delay_s = self._chunk_delay_sec

        def _write_one(chunk: bytes) -> None:
            if chunks_sent[0] > 0 and delay_s > 0:
                time.sleep(delay_s)
            chunks_sent[0] += 1
            try:
                self._write_gatt_char(self._char_rx, chunk, self._tx_write_mode)
            except Exception as exc:
                emsg = clean_dbus_error(exc)
                # If chunk exceeded un-exchanged ATT MTU (`pdu_len > att->mtu`) or hit STM32WB55
                # `ATT error: 0x0e` on `request` mode, clamp to 20-byte `command` slices.
                if len(chunk) > 20 and any(
                    k in emsg
                    for k in (
                        "Failed to initiate write",
                        "InvalidValueLength",
                        "ATT error: 0x0e",
                        "ATT error: 0x0d",
                    )
                ):
                    self._tx_write_mode = "command"
                    self._flow.max_chunk = 20
                    slice_delay = max(0.008, delay_s * 0.5) if delay_s > 0 else 0.0
                    for off in range(0, len(chunk), 20):
                        if off > 0 and slice_delay > 0:
                            time.sleep(slice_delay)
                        self._write_gatt_char(self._char_rx, chunk[off : off + 20], "command")
                    return
                if self._tx_write_mode == "request" and (
                    "ATT error: 0x0e" in emsg or "NotSupported" in emsg
                ):
                    self._tx_write_mode = "command"
                    time.sleep(0.02)
                    self._write_gatt_char(self._char_rx, chunk, "command")
                    return
                if any(
                    k in emsg
                    for k in (
                        "Not connected",
                        "NotConnected",
                        "Disconnected",
                        "Does Not Exist",
                        "UnknownObject",
                        "Broken pipe",
                        "closed",
                    )
                ):
                    self._connected = False
                    self._flow.close()
                    self._fire_disconnect_cb()
                    raise ConnectionError(f"BLE link dropped during write: {emsg}") from exc
                raise

        # Proactive low-credit refresh (`serial_service.c:200` only notifies `char_flow` at 0!)
        if self._flow.credits < max(128, len(data) + 16):
            self.sync_flow_credits()

        with self._tx_lock:
            try:
                self._flow.write_paced(data, _write_one, timeout_sec=timeout_sec)
            except ConnectionError:
                self._connected = False
                self._fire_disconnect_cb()
                raise

    def disconnect(self) -> None:
        Gio, _ = _load_gio()
        was_conn = False
        with self._lock:
            was_conn = self._connected
            self._connected = False
            self._flow.close()
            self._stop_notify_pump(stop_notify=was_conn)
            bus = self._bus
            if was_conn and bus is not None and self._dev_path:
                try:
                    bus.call_sync(
                        "org.bluez",
                        self._dev_path,
                        "org.bluez.Device1",
                        "Disconnect",
                        None,
                        None,
                        Gio.DBusCallFlags.NONE,
                        4000,
                        None,
                    )
                except Exception:
                    pass
            # Keep `hci0` in `sc off` so future reconnects match our Legacy LTK.
        if was_conn:
            self._fire_disconnect_cb()

    def _extract_passive_batt_pct(self, objs: dict) -> Optional[int]:
        if not self._dev_path:
            return None
        dev_entry = objs.get(self._dev_path, {})
        batt_iface = dev_entry.get("org.bluez.Battery1")
        if isinstance(batt_iface, dict) and "Percentage" in batt_iface:
            try:
                self._last_batt_pct = int(batt_iface["Percentage"])
                return self._last_batt_pct
            except Exception:
                pass
        if self._char_batt:
            ch_props = objs.get(self._char_batt, {}).get("org.bluez.GattCharacteristic1", {})
            val = ch_props.get("Value")
            if val:
                try:
                    self._last_batt_pct = int(bytes(val)[0])
                    return self._last_batt_pct
                except Exception:
                    pass
        return self._last_batt_pct

    def read_ble_battery_pct(self) -> Optional[int]:
        if not self.is_connected:
            return None
        try:
            objs = self._get_managed_objects()
            passive = self._extract_passive_batt_pct(objs)
            if passive is not None:
                return passive
        except Exception:
            pass
        if not self._char_batt:
            return self._last_batt_pct
        try:
            raw = self._read_gatt_char(self._char_batt)
            if raw:
                self._last_batt_pct = int(raw[0])
                return self._last_batt_pct
        except Exception:
            pass
        return self._last_batt_pct

    def link_info(self) -> Dict[str, object]:
        live_conn = self.is_connected
        rssi = self._dev_info.rssi if self._dev_info else -100
        batt_pct: Optional[int] = self._last_batt_pct if live_conn else None
        if live_conn and self._dev_path:
            try:
                objs = self._get_managed_objects()
                d_props = objs.get(self._dev_path, {}).get("org.bluez.Device1", {})
                if not d_props.get("Connected", True):
                    self._connected = False
                    self._flow.close()
                    live_conn = False
                    batt_pct = None
                else:
                    if "RSSI" in d_props:
                        rssi = int(d_props["RSSI"])
                    # Passive lookup from `org.bluez.Battery1` / cached char value: zero extra ATT traffic.
                    batt_pct = self._extract_passive_batt_pct(objs)
            except Exception:
                pass
        return {
            "connected": live_conn,
            "address": self._dev_info.address if self._dev_info else "",
            "name": self._dev_info.name if self._dev_info else "",
            "shell_color": self._dev_info.shell_color if self._dev_info else "",
            "rssi": rssi,
            "credits": self._flow.credits,
            "ble_battery_pct": batt_pct if live_conn else None,
        }
