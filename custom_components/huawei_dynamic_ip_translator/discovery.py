"""Find IPv4 neighbours on directly connected Linux networks."""

import asyncio
from collections.abc import Iterable
from contextlib import suppress
from ipaddress import IPv4Address, IPv4Interface
from itertools import batched
from pathlib import Path
import socket

ARP_TABLE = Path("/proc/net/arp")
MAX_SUBNET_ADDRESSES = 1024
HUAWEI_PREFIX_FILE = Path(__file__).with_name("huawei_prefixes.txt")


def load_huawei_prefixes() -> frozenset[str]:
    """Load Huawei assignments from the bundled IEEE MA-L, MA-M and MA-S subset."""
    return frozenset(HUAWEI_PREFIX_FILE.read_text(encoding="ascii").splitlines())


def is_huawei_mac(mac: str, prefixes: frozenset[str]) -> bool:
    """Check the registered prefixes of a normalized MAC address."""
    return any(mac[:length] in prefixes for length in (6, 7, 9))


def parse_neighbours(table: str, name: str, interface: IPv4Interface) -> dict[str, str]:
    """Return complete ARP entries on the selected interface and subnet."""
    neighbours = {}
    for line in table.splitlines()[1:]:
        fields = line.split()
        if len(fields) != 6:
            continue
        ip, _, flags, hardware, _, device = fields
        if (
            device == name
            and int(flags, 16) & 2
            and IPv4Address(ip) in interface.network
        ):
            neighbours[ip] = hardware.replace(":", "").lower()
    return neighbours


async def scan_neighbours(interfaces: Iterable[tuple[str, IPv4Interface]]) -> dict[str, str]:
    """Trigger ordinary neighbour resolution without requiring raw sockets or DNS."""
    loop = asyncio.get_running_loop()
    neighbours: dict[str, str] = {}
    for name, interface in interfaces:
        if interface.ip.is_loopback or interface.network.num_addresses > MAX_SUBNET_ADDRESSES:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.setblocking(False)
            probe.bind((str(interface.ip), 0))
            hosts = (host for host in interface.network.hosts() if host != interface.ip)
            for batch in batched(hosts, 32):
                for host in batch:
                    # An empty local datagram makes the kernel resolve the destination MAC.
                    with suppress(OSError):
                        await loop.sock_sendto(probe, b"", (str(host), 9))
                await asyncio.sleep(0.04)
        await asyncio.sleep(0.75)
        table = await asyncio.to_thread(ARP_TABLE.read_text, encoding="ascii")
        neighbours.update(parse_neighbours(table, name, interface))
    return neighbours
