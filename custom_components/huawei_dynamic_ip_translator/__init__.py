"""Home Assistant lifecycle and network discovery for Huawei Dynamic IP Translator."""

from ipaddress import IPv4Interface

from homeassistant.components import network
from homeassistant.components.dhcp import async_discovered_service_info
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .discovery import scan_local_addresses
from .proxy import ModbusBridge

DOMAIN = "huawei_dynamic_ip_translator"
CONF_MAC = "mac"
CONF_DEVICE_PORT = "device_port"
CONF_LOCAL_PORT = "local_port"
CONF_UNIT_ID = "unit_id"
CONF_SERIAL = "serial"

type BridgeEntry = ConfigEntry[ModbusBridge]


async def async_setup_entry(hass: HomeAssistant, entry: BridgeEntry) -> bool:
    """Start the loopback listener, even when Huawei is currently offline."""
    mac = entry.data[CONF_MAC]

    @callback
    def remember_serial(serial: str) -> None:
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_SERIAL: serial})

    @callback
    def dhcp_addresses() -> list[str]:
        return [
            device.ip
            for device in async_discovered_service_info(hass)
            if device.macaddress.replace(":", "").lower() == mac
        ]

    async def scan_addresses() -> list[str]:
        interfaces = [
            (adapter["name"], IPv4Interface(f"{ip['address']}/{ip['network_prefix']}"))
            for adapter in await network.async_get_adapters(hass)
            if adapter["enabled"]
            for ip in adapter["ipv4"]
        ]
        return await scan_local_addresses(mac, interfaces)

    bridge = ModbusBridge(
        device_port=entry.data[CONF_DEVICE_PORT],
        unit_id=entry.data[CONF_UNIT_ID],
        serial=entry.data.get(CONF_SERIAL),
        remember_serial=remember_serial,
        dhcp_addresses=dhcp_addresses,
        scan_addresses=scan_addresses,
    )
    try:
        await bridge.start(entry.data[CONF_LOCAL_PORT])
    except OSError as err:
        raise ConfigEntryNotReady("Could not open the configured local TCP port") from err
    entry.runtime_data = bridge

    async def stop_bridge(_event: Event) -> None:
        await bridge.stop()

    entry.async_on_unload(hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, stop_bridge))
    return True


async def async_unload_entry(_hass: HomeAssistant, entry: BridgeEntry) -> bool:
    """Release the loopback listener and its client connection."""
    await entry.runtime_data.stop()
    return True
