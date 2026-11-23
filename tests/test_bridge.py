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
    def test_huawei_vendor_prefixes_include_both_assignments(self):
        prefixes = discovery.load_huawei_prefixes()
        self.assertTrue(discovery.is_huawei_mac("108ffe010203", prefixes))
        self.assertTrue(discovery.is_huawei_mac("a8ffba010203", prefixes))
        self.assertFalse(discovery.is_huawei_mac("020000010203", prefixes))

    def test_longer_vendor_assignments_are_matched_at_their_exact_length(self):
        self.assertTrue(discovery.is_huawei_mac("012345678123", frozenset({"012345678"})))
        self.assertFalse(discovery.is_huawei_mac("012345679123", frozenset({"012345678"})))

    def test_only_complete_neighbours_on_selected_subnet_are_returned(self):
        table = """IP address HW type Flags HW address Mask Device
192.168.10.40 0x1 0x2 94:25:33:12:34:56 * eth0
192.168.10.41 0x1 0x0 94:25:33:12:34:56 * eth0
192.168.10.42 0x1 0x2 94:25:33:12:34:57 * eth0
192.168.10.43 0x1 0x2 94:25:33:12:34:56 * eth1
192.168.11.40 0x1 0x2 94:25:33:12:34:56 * eth0
"""
        self.assertEqual(
            discovery.parse_neighbours(table, "eth0", IPv4Interface("192.168.10.1/24")),
            {"192.168.10.40": "942533123456", "192.168.10.42": "942533123457"},
        )


class DeviceProbeTest(unittest.IsolatedAsyncioTestCase):
    async def check_model(self, model):
        async def serve(reader, writer):
            try:
                while True:
                    request = await reader.readexactly(12)
                    tid, protocol, length, unit, function, register, count = struct.unpack(
                        ">HHHBBHH", request
                    )
                    self.assertEqual((protocol, length, function), (0, 6, 3))
                    if unit != 0:
                        writer.write(struct.pack(">HHHBBB", tid, 0, 3, unit, 0x83, 2))
                    else:
                        value = model if register == 30000 else b"HUAWEI-123"
                        size = 2 * count
                        writer.write(struct.pack(">HHHBBB", tid, 0, 3 + size, unit, 3, size))
                        writer.write(value.ljust(size, b"\x00"))
                    await writer.drain()
            except asyncio.IncompleteReadError:
                pass
            finally:
                await proxy.close_stream(writer)

        async with await asyncio.start_server(serve, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            return await proxy.probe_huawei("127.0.0.1", port, (1, 0))

    async def test_discovers_huawei_and_finds_responding_unit(self):
        self.assertEqual(await self.check_model(b"SUN2000-5KTL"), ("SUN2000-5KTL", "HUAWEI-123", 0))

    async def test_ignores_non_huawei_modbus_device(self):
        self.assertIsNone(await self.check_model(b"OTHER-MODEL"))
