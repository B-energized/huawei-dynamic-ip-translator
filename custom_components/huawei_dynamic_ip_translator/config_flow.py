"""Setup flow for Huawei Dynamic IP Translator."""

import asyncio
from collections.abc import Mapping

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.selector import SelectOptionDict, SelectSelector, SelectSelectorConfig

from . import CONF_DEVICE_PORT, CONF_LOCAL_PORT, CONF_MAC, CONF_SERIAL, CONF_UNIT_ID, DOMAIN
from .proxy import normalize_mac, probe_huawei
from .setup_discovery import STANDARD_PORTS, STANDARD_UNITS, Candidate, find_candidates

DEFAULTS = {CONF_DEVICE_PORT: 502, CONF_UNIT_ID: 1, CONF_LOCAL_PORT: 15020}
DEVICE = "device"
PORT = vol.All(int, vol.Range(min=1, max=65535))


class BridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Register each target MAC once and assign it a loopback port."""

    VERSION = 1

    def __init__(self) -> None:
        self._scan_task: asyncio.Task[list[Candidate]] | None = None
        self._candidates: dict[str, Candidate] = {}
        self._manual_errors: dict[str, str] = {}
        self._selected: Candidate | None = None
        self._local_port = DEFAULTS[CONF_LOCAL_PORT]
        self._ports = STANDARD_PORTS
        self._unit_ids = STANDARD_UNITS
        self._check_task: asyncio.Task[Candidate | None] | None = None

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["scan", "manual"])

    async def async_step_scan(self, user_input: dict | None = None) -> ConfigFlowResult:
        if self._scan_task is None:
            self._scan_task = self.hass.async_create_task(
                find_candidates(self.hass), "Discover Huawei Modbus endpoints"
            )
        if not self._scan_task.done():
            return self.async_show_progress(
                step_id="scan", progress_action="scanning", progress_task=self._scan_task
            )

        try:
            self._candidates = {item.mac: item for item in self._scan_task.result()}
        except OSError:
            self._candidates = {}
        self._scan_task = None
        if not self._candidates:
            self._manual_errors = {"base": "no_devices"}
        return self.async_show_progress_done(next_step_id="select" if self._candidates else "manual")

    def _local_ports(self) -> set[int]:
        return {entry.data[CONF_LOCAL_PORT] for entry in self._async_current_entries()}

    async def _create_entry(self, values: Mapping, mac: str, serial: str | None = None) -> ConfigFlowResult:
        if values[CONF_LOCAL_PORT] in self._local_ports():
            return self.async_abort(reason="port_in_use")
        await self.async_set_unique_id(mac)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Huawei Dynamic IP Translator ({values[CONF_LOCAL_PORT]})",
            data={
                CONF_MAC: mac,
                CONF_LOCAL_PORT: values[CONF_LOCAL_PORT],
                CONF_DEVICE_PORT: values[CONF_DEVICE_PORT],
                CONF_UNIT_ID: values[CONF_UNIT_ID],
                CONF_SERIAL: serial,
            },
        )

    async def async_step_select(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors = {}
        if user_input is not None:
            if user_input[CONF_LOCAL_PORT] in self._local_ports():
                errors[CONF_LOCAL_PORT] = "port_in_use"
            else:
                self._selected = self._candidates[user_input[DEVICE]]
                self._local_port = user_input[CONF_LOCAL_PORT]
                if self._selected.port is not None:
                    return await self.async_step_confirm_existing()
                self._ports = STANDARD_PORTS
                self._unit_ids = STANDARD_UNITS
                return await self.async_step_check()
        selection = user_input or {}
        used_ports = self._local_ports()
        port = next(
            (port for port in range(DEFAULTS[CONF_LOCAL_PORT], 65536) if port not in used_ports),
            DEFAULTS[CONF_LOCAL_PORT],
        )
        options = [SelectOptionDict(value=item.mac, label=item.label) for item in self._candidates.values()]
        return self.async_show_form(
            step_id="select",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        DEVICE, default=selection.get(DEVICE, next(iter(self._candidates)))
                    ): SelectSelector(SelectSelectorConfig(options=options)),
                    vol.Required(CONF_LOCAL_PORT, default=selection.get(CONF_LOCAL_PORT, port)): PORT,
                }
            ),
        )

    async def _check_selected(self, selected: Candidate) -> Candidate | None:
        for port in self._ports:
            if result := await probe_huawei(selected.host, port, self._unit_ids):
                model, serial, unit_id = result
                return Candidate(selected.host, selected.mac, model, port, unit_id, serial)
        return None

    async def async_step_check(self, user_input: dict | None = None) -> ConfigFlowResult:
        assert self._selected is not None
        if self._check_task is None:
            self._check_task = self.hass.async_create_task(
                self._check_selected(self._selected), "Verify selected Huawei endpoint"
            )
        if not self._check_task.done():
            return self.async_show_progress(
                step_id="check", progress_action="checking", progress_task=self._check_task,
                description_placeholders={"host": self._selected.host},
            )
        verified = self._check_task.result()
        self._check_task = None
        if verified is None:
            return self.async_show_progress_done(next_step_id="check_failed")
        self._selected = verified
        return self.async_show_progress_done(next_step_id="confirm")

    async def async_step_check_failed(self, user_input: dict | None = None) -> ConfigFlowResult:
        assert self._selected is not None
        return self.async_show_menu(
            step_id="check_failed",
            menu_options=["select", "connection", "manual"],
            description_placeholders={"host": self._selected.host},
        )

    async def async_step_connection(self, user_input: dict | None = None) -> ConfigFlowResult:
        assert self._selected is not None
        if user_input is not None:
            self._ports = (user_input[CONF_DEVICE_PORT],)
            self._unit_ids = (user_input[CONF_UNIT_ID],)
            return await self.async_step_check()
        return self.async_show_form(
            step_id="connection",
            description_placeholders={"host": self._selected.host},
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_DEVICE_PORT, default=self._ports[0]): PORT,
                    vol.Required(CONF_UNIT_ID, default=self._unit_ids[0]): vol.All(
                        int, vol.Range(min=0, max=247)
                    ),
                }
            ),
        )

    async def async_step_confirm(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self._confirm("confirm", user_input)

    async def async_step_confirm_existing(self, user_input: dict | None = None) -> ConfigFlowResult:
        return await self._confirm("confirm_existing", user_input)

    async def _confirm(self, step: str, user_input: dict | None) -> ConfigFlowResult:
        selected = self._selected
        assert selected is not None and selected.port is not None and selected.unit_id is not None
        if user_input is None:
            return self.async_show_form(
                step_id=step,
                description_placeholders={
                    "model": selected.model,
                    "host": selected.host,
                    "port": str(selected.port),
                    "unit": str(selected.unit_id),
                    "serial": selected.serial or "",
                    "local_port": str(self._local_port),
                },
            )
        return await self._create_entry(
            {CONF_LOCAL_PORT: self._local_port, CONF_DEVICE_PORT: selected.port, CONF_UNIT_ID: selected.unit_id},
            selected.mac,
            selected.serial,
        )

    async def async_step_manual(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors, self._manual_errors = self._manual_errors, {}
        if user_input is not None:
            try:
                mac = normalize_mac(user_input[CONF_MAC])
            except ValueError:
                errors[CONF_MAC] = "invalid_mac"
            else:
                if user_input[CONF_LOCAL_PORT] in self._local_ports():
                    errors[CONF_LOCAL_PORT] = "port_in_use"
                else:
                    return await self._create_entry(user_input, mac)

        values = DEFAULTS | (user_input or {})
        return self.async_show_form(
            step_id="manual",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAC, default=values.get(CONF_MAC, "")): str,
                    vol.Required(CONF_DEVICE_PORT, default=values[CONF_DEVICE_PORT]): PORT,
                    vol.Required(CONF_UNIT_ID, default=values[CONF_UNIT_ID]): vol.All(
                        int, vol.Range(min=0, max=247)
                    ),
                    vol.Required(CONF_LOCAL_PORT, default=values[CONF_LOCAL_PORT]): PORT,
                }
            ),
        )
