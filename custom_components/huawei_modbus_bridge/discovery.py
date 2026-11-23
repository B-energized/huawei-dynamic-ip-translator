"""Discover a configured MAC on directly connected Linux IPv4 networks."""

import asyncio
from collections.abc import Iterable
from contextlib import suppress
from ipaddress import IPv4Address, IPv4Interface
from itertools import batched
from pathlib import Path
import socket

ARP_TABLE = Path("/proc/net/arp")
MAX_SUBNET_ADDRESSES = 1024


def matching_addresses(table: str, mac: str, name: str, interface: IPv4Interface) -> list[str]:
    """Filter complete ARP entries by interface, subnet and exact MAC."""
    matches = []
    for line in table.splitlines()[1:]:
        fields = line.split()
        if len(fields) != 6:
            continue
        ip, _, flags, hardware, _, device = fields
        if (
            device == name
            and hardware.replace(":", "").lower() == mac
            and int(flags, 16) & 2
            and IPv4Address(ip) in interface.network
        ):
            matches.append(ip)
    return matches


async def scan_local_addresses(
    mac: str, interfaces: Iterable[tuple[str, IPv4Interface]]
) -> list[str]:
    """Trigger ordinary neighbour resolution without requiring raw sockets or DNS."""
    loop = asyncio.get_running_loop()
    matches: list[str] = []
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
        matches.extend(matching_addresses(table, mac, name, interface))
    return matches
