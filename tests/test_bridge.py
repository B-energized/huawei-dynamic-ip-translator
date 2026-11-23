"""Exercise recovery over real TCP sockets without a Home Assistant installation."""

import asyncio
from contextlib import suppress
import importlib.util
from ipaddress import IPv4Interface
from pathlib import Path
import struct
import unittest
from unittest.mock import AsyncMock, patch


def load_component_module(name):
    folder = Path(__file__).resolve().parents[1] / "custom_components" / "huawei_dynamic_ip_translator"
    spec = importlib.util.spec_from_file_location(name, folder / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


proxy = load_component_module("proxy")
discovery = load_component_module("discovery")


class BridgeTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.open_connection = asyncio.open_connection
        self.dhcp = ["192.168.10.40"]
        self.scan = AsyncMock(return_value=[])
        self.serials = []
        self.endpoints = {}
        self.received = {}
        self.bridge = proxy.ModbusBridge(
            502, 1, None, self.serials.append, lambda: self.dhcp, self.scan
        )
        self.port = await self.bridge.start(0)
        self.addAsyncCleanup(self.bridge.stop)
        self.enterContext(patch.object(proxy.asyncio, "open_connection", self.connect_upstream))

    async def connect_upstream(self, ip, port):
        self.assertEqual(port, 502)
        if ip not in self.endpoints:
            raise OSError("Device offline")
        return await self.open_connection("127.0.0.1", self.endpoints[ip])

    async def add_device(self, ip, serial):
        self.received[ip] = []

        async def serve(reader, writer):
            try:
                request = await reader.readexactly(12)
                self.assertEqual(struct.unpack(">HHHBBHH", request), (0xA57E, 0, 6, 1, 3, 30015, 10))
                writer.write(request[:4] + b"\x00\x17\x01\x03\x14" + serial.ljust(20, b"\x00"))
                await writer.drain()
                if payload := await reader.read(128):
                    self.received[ip].append(payload)
                    writer.write(payload)
                    await writer.drain()
            finally:
                await proxy.close_stream(writer)

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        self.endpoints[ip] = server.sockets[0].getsockname()[1]
        self.addAsyncCleanup(server.wait_closed)
        self.addCleanup(server.close)

    async def request(self, payload):
        reader, writer = await self.open_connection("127.0.0.1", self.port)
        try:
            writer.write(payload)
            await writer.drain()
            async with asyncio.timeout(8):
                with suppress(ConnectionResetError):
                    return await reader.read()
            return b""
        finally:
            await proxy.close_stream(writer)
            # Let the first handler finish relinquishing the one upstream session.
            async with asyncio.timeout(2):
                while self.bridge._client_task is not None:
                    await asyncio.sleep(0.01)

    async def test_dhcp_change_reuses_local_endpoint(self):
        await self.add_device("192.168.10.40", b"HUAWEI-OLD")
        self.assertEqual(await self.request(b"first"), b"first")
        del self.endpoints["192.168.10.40"]
        await self.add_device("192.168.10.61", b"HUAWEI-OLD")
        self.dhcp[:] = ["192.168.10.61"]
        self.assertEqual(await self.request(b"second"), b"second")
        self.assertEqual(self.received["192.168.10.61"], [b"second"])
        self.assertEqual(self.serials, ["HUAWEI-OLD"])
        self.scan.assert_not_awaited()

    async def test_scan_recovers_when_old_ip_has_been_reused(self):
        await self.add_device("192.168.10.40", b"HUAWEI-OLD")
        self.assertEqual(await self.request(b"first"), b"first")
        await self.add_device("192.168.10.40", b"DIFFERENT")
        await self.add_device("192.168.10.77", b"HUAWEI-OLD")
        self.scan.return_value = ["192.168.10.77"]
        self.assertEqual(await self.request(b"second"), b"second")
        self.assertEqual(self.received["192.168.10.40"], [])
        self.assertEqual(self.received["192.168.10.77"], [b"second"])
        self.scan.assert_awaited_once()

    async def test_failed_scans_are_rate_limited(self):
        self.assertEqual(await self.request(b"offline"), b"")
        self.assertEqual(await self.request(b"still offline"), b"")
        self.scan.assert_awaited_once()

    async def test_stop_closes_active_connection(self):
        await self.add_device("192.168.10.40", b"HUAWEI-OLD")
        reader, writer = await self.open_connection("127.0.0.1", self.port)
        try:
            async with asyncio.timeout(5):
                while not self.serials:
                    await asyncio.sleep(0.01)
                await self.bridge.stop()
                self.assertEqual(await reader.read(), b"")
        finally:
            await proxy.close_stream(writer)


class DiscoveryTest(unittest.TestCase):
    def test_only_complete_matching_mac_on_selected_subnet_is_returned(self):
        table = """IP address HW type Flags HW address Mask Device
192.168.10.40 0x1 0x2 94:25:33:12:34:56 * eth0
192.168.10.41 0x1 0x0 94:25:33:12:34:56 * eth0
192.168.10.42 0x1 0x2 94:25:33:12:34:57 * eth0
192.168.10.43 0x1 0x2 94:25:33:12:34:56 * eth1
192.168.11.40 0x1 0x2 94:25:33:12:34:56 * eth0
"""
        self.assertEqual(
            discovery.matching_addresses(table, "942533123456", "eth0", IPv4Interface("192.168.10.1/24")),
            ["192.168.10.40"],
        )
