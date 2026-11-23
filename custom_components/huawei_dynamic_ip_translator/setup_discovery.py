"""Find candidates for a new translator configuration."""

import asyncio
from dataclasses import dataclass
from ipaddress import IPv4Address
import logging
import socket

from homeassistant.components.dhcp import async_discovered_service_info
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant

from . import CONF_MAC, DOMAIN, async_scan_neighbours
from .discovery import is_huawei_mac, load_huawei_prefixes
from .proxy import normalize_mac

LOGGER = logging.getLogger(__name__)
STANDARD_PORTS = (502, 6607)
STANDARD_UNITS = (1, 0, 100)


@dataclass(frozen=True)
class Candidate:
    host: str
    mac: str
    model: str = "Huawei Technologies"
    port: int | None = None
    unit_id: int | None = None
    serial: str | None = None

    @property
    def label(self) -> str:
        return f"{self.model} ({self.host} · {self.mac.upper()})"


async def resolve_ipv4(host: str) -> str | None:
    """Resolve the configured Huawei Solar host if it is a name."""
    try:
        address = IPv4Address(host)
    except ValueError:
        try:
            async with asyncio.timeout(2):
                records = await asyncio.get_running_loop().getaddrinfo(
                    host, None, family=socket.AF_INET, type=socket.SOCK_STREAM
                )
            address = IPv4Address(records[0][4][0])
        except (OSError, TimeoutError, IndexError):
            return None
    return None if address.is_loopback else str(address)


async def find_candidates(hass: HomeAssistant) -> list[Candidate]:
    """Suggest existing Huawei Solar endpoints and LAN neighbours with a Huawei MAC."""
    known = {}
    for entry in hass.config_entries.async_entries("huawei_solar"):
        if not (host := entry.data.get(CONF_HOST)) or not isinstance(host, str):
            continue
        port = entry.data.get(CONF_PORT)
        units = entry.data.get("slave_ids")
        unit_id = units[0] if isinstance(units, (tuple, list)) and units else units
        if not (
            isinstance(port, int) and 1 <= port <= 65535
            and isinstance(unit_id, int) and 0 <= unit_id <= 247
        ):
            continue
        ip = await resolve_ipv4(host)
        if ip is None:
            continue
        known[ip] = (port, unit_id, entry.title, entry.unique_id)

    try:
        async with asyncio.timeout(12):
            neighbours = await async_scan_neighbours(hass)
    except (OSError, TimeoutError, ValueError):
        LOGGER.debug("Local discovery could not read the neighbour table", exc_info=True)
        neighbours = {}

    dhcp = {device.ip: device.macaddress for device in async_discovered_service_info(hass)}
    configured_macs = {entry.data[CONF_MAC] for entry in hass.config_entries.async_entries(DOMAIN)}
    prefixes = await hass.async_add_executor_job(load_huawei_prefixes)
    candidates: dict[str, Candidate] = {}
    for ip, (port, unit_id, model, serial) in known.items():
        try:
            mac = normalize_mac(neighbours.get(ip, dhcp.get(ip, "")))
        except ValueError:
            continue
        if mac not in configured_macs:
            candidates[mac] = Candidate(ip, mac, model, port, unit_id, serial)
    for ip, observed_mac in neighbours.items():
        try:
            mac = normalize_mac(observed_mac)
        except ValueError:
            continue
        if mac not in configured_macs and is_huawei_mac(mac, prefixes):
            candidates.setdefault(mac, Candidate(ip, mac))
    return list(candidates.values())
