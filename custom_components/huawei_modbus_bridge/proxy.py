"""Discover and verify a Huawei endpoint before relaying Modbus TCP."""

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from ipaddress import IPv4Address
import logging
import re
import struct
from time import monotonic

LOGGER = logging.getLogger(__name__)
LOOPBACK = "127.0.0.1"
SCAN_INTERVAL = 60
SCAN_TIMEOUT = 15
CONNECT_TIMEOUT = 3
IDENTITY_TIMEOUT = 5
RESPONSE_IDLE_TIMEOUT = 120


def normalize_mac(value: str) -> str:
    """Return an unseparated MAC address or reject an invalid Ethernet address."""
    mac = re.sub(r"[:.\-]", "", value.strip()).lower()
    if (
        not re.fullmatch(r"[0-9a-f]{12}", mac)
        or mac == "000000000000"
        or int(mac[:2], 16) & 1
    ):
        raise ValueError("Invalid unicast MAC address")
    return mac


async def close_stream(writer: asyncio.StreamWriter) -> None:
    """Close a socket even when the peer has stopped responding."""
    writer.close()
    try:
        async with asyncio.timeout(1):
            await writer.wait_closed()
    except (OSError, TimeoutError):
        writer.transport.abort()


async def read_serial(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, unit_id: int
) -> str:
    """Read the ten Huawei serial-number registers using function 03."""
    transaction_id = 0xA57E
    request = struct.pack(">HHHBBHH", transaction_id, 0, 6, unit_id, 3, 30015, 10)
    async with asyncio.timeout(IDENTITY_TIMEOUT):
        # Some Huawei endpoints need time to accept requests after TCP connects.
        await asyncio.sleep(1)
        writer.write(request)
        await writer.drain()
        transaction, protocol, length = struct.unpack(">HHH", await reader.readexactly(6))
        if (transaction, protocol, length) != (transaction_id, 0, 23):
            raise ValueError("Invalid Huawei serial response header")
        response = await reader.readexactly(length)
    if response[:3] != bytes((unit_id, 3, 20)):
        raise ValueError("Invalid Huawei serial response")
    serial = response[3:].strip(b"\x00 ").decode("ascii")
    if not serial or not serial.isprintable() or not any(c.isalnum() for c in serial):
        raise ValueError("Missing Huawei serial number")
    return serial


async def copy_stream(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, idle_timeout: int | None
) -> None:
    """Forward bytes without interpreting or replaying the client's commands."""
    while True:
        async with asyncio.timeout(idle_timeout):
            data = await reader.read(65536)
        if not data:
            return
        writer.write(data)
        async with asyncio.timeout(RESPONSE_IDLE_TIMEOUT):
            await writer.drain()


class ModbusBridge:
    """Allow one client per endpoint and reverify each new upstream connection."""

    def __init__(
        self,
        device_port: int,
        unit_id: int,
        serial: str | None,
        remember_serial: Callable[[str], None],
        dhcp_addresses: Callable[[], Iterable[str]],
        scan_addresses: Callable[[], Awaitable[Iterable[str]]],
    ) -> None:
        self._device_port = device_port
        self._unit_id = unit_id
        self._serial = serial
        self._remember_serial = remember_serial
        self._dhcp_addresses = dhcp_addresses
        self._scan_addresses = scan_addresses
        self._last_ip: str | None = None
        self._next_scan = 0.0
        self._server: asyncio.Server | None = None
        self._client_task: asyncio.Task | None = None
        self._client_writer: asyncio.StreamWriter | None = None
        self._closing = False

    async def start(self, local_port: int) -> int:
        """Listen only on the Home Assistant host's loopback interface."""
        self._server = await asyncio.start_server(self._accept, LOOPBACK, local_port)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        """Stop listening and close the current session."""
        self._closing = True
        if self._server is not None:
            self._server.close()
        if self._client_writer is not None:
            self._client_writer.close()
        if self._client_task is not None:
            self._client_task.cancel()
            await asyncio.gather(self._client_task, return_exceptions=True)
        if self._server is not None:
            await self._server.wait_closed()

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._closing or self._client_task is not None:
            writer.close()
            return
        task = asyncio.create_task(self._handle_client(reader, writer))
        self._client_writer = writer
        self._client_task = task
        task.add_done_callback(self._client_done)

    def _client_done(self, task: asyncio.Task) -> None:
        self._client_task = None
        self._client_writer = None
        if not task.cancelled() and (error := task.exception()) is not None:
            LOGGER.error("Huawei Modbus Bridge session failed", exc_info=error)

    async def _try_addresses(
        self, addresses: Iterable[str], tried: set[str]
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        for candidate in addresses:
            try:
                address = IPv4Address(candidate)
            except ValueError:
                continue
            ip = str(address)
            if (
                address.is_loopback or address.is_unspecified or address.is_multicast
                or ip == "255.255.255.255" or ip in tried
            ):
                continue
            tried.add(ip)
            if connection := await self._verify(ip):
                return connection
        return None

    async def _verify(
        self, ip: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        try:
            async with asyncio.timeout(CONNECT_TIMEOUT):
                reader, writer = await asyncio.open_connection(ip, self._device_port)
        except (OSError, TimeoutError):
            return None
        verified = False
        try:
            serial = await read_serial(reader, writer, self._unit_id)
            if self._serial is None:
                self._remember_serial(serial)
                self._serial = serial
            elif serial != self._serial:
                LOGGER.debug("Rejected Huawei endpoint at %s because its serial changed", ip)
                return None
            if ip != self._last_ip:
                LOGGER.info("Verified Huawei Modbus endpoint at %s:%s", ip, self._device_port)
                self._last_ip = ip
            verified = True
            return reader, writer
        except (OSError, TimeoutError, asyncio.IncompleteReadError, ValueError):
            LOGGER.debug("Could not verify Huawei endpoint at %s", ip, exc_info=True)
            return None
        finally:
            if not verified:
                await close_stream(writer)

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        tried: set[str] = set()
        addresses = list(self._dhcp_addresses())
        if self._last_ip is not None:
            addresses.append(self._last_ip)
        if connection := await self._try_addresses(addresses, tried):
            return connection
        if monotonic() < self._next_scan:
            return None
        self._next_scan = monotonic() + SCAN_INTERVAL
        try:
            async with asyncio.timeout(SCAN_TIMEOUT):
                addresses = await self._scan_addresses()
        except (OSError, TimeoutError, RuntimeError, ValueError):
            LOGGER.debug("Huawei local network discovery failed", exc_info=True)
            return None
        # A scan can refresh the neighbour table even for an address already tried.
        return await self._try_addresses(addresses, set())

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        upstream = None
        pumps: list[asyncio.Task] = []
        try:
            upstream = await self._connect()
            if upstream is None:
                return
            upstream_reader, upstream_writer = upstream
            pumps = [
                asyncio.create_task(copy_stream(reader, upstream_writer, None)),
                asyncio.create_task(copy_stream(upstream_reader, writer, RESPONSE_IDLE_TIMEOUT)),
            ]
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for pump in pumps:
                pump.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            writer.close()
            if upstream is not None:
                await close_stream(upstream[1])
            await close_stream(writer)
