"""Setup form for a Huawei Dynamic IP Translator."""

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult

from . import CONF_DEVICE_PORT, CONF_LOCAL_PORT, CONF_MAC, CONF_UNIT_ID, DOMAIN
from .proxy import normalize_mac

DEFAULTS = {CONF_DEVICE_PORT: 502, CONF_UNIT_ID: 1, CONF_LOCAL_PORT: 15020}


class BridgeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Register each target MAC once and assign it a loopback port."""

    VERSION = 1

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                mac = normalize_mac(user_input[CONF_MAC])
            except ValueError:
                errors[CONF_MAC] = "invalid_mac"
            else:
                await self.async_set_unique_id(mac)
                self._abort_if_unique_id_configured()
                if any(
                    entry.data[CONF_LOCAL_PORT] == user_input[CONF_LOCAL_PORT]
                    for entry in self._async_current_entries()
                ):
                    errors[CONF_LOCAL_PORT] = "port_in_use"
                else:
                    return self.async_create_entry(
                        title=f"Huawei Dynamic IP Translator ({user_input[CONF_LOCAL_PORT]})",
                        data={**user_input, CONF_MAC: mac},
                    )

        values = DEFAULTS | (user_input or {})
        return self.async_show_form(
            step_id="user",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_MAC, default=values.get(CONF_MAC, "")): str,
                    vol.Required(CONF_DEVICE_PORT, default=values[CONF_DEVICE_PORT]): vol.All(
                        int, vol.Range(min=1, max=65535)
                    ),
                    vol.Required(CONF_UNIT_ID, default=values[CONF_UNIT_ID]): vol.All(
                        int, vol.Range(min=0, max=247)
                    ),
                    vol.Required(CONF_LOCAL_PORT, default=values[CONF_LOCAL_PORT]): vol.All(
                        int, vol.Range(min=1, max=65535)
                    ),
                }
            ),
        )
