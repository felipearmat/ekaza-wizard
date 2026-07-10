"""Config flow — one-step setup that creates the integration and sidebar panel."""
from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_RTSP_PASSWORD,
    CONF_TUYA_ACCESS_ID,
    CONF_TUYA_ACCESS_SECRET,
    CONF_TUYA_REGION,
    DOMAIN,
)

_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_TUYA_ACCESS_ID, default=""): str,
        vol.Optional(CONF_TUYA_ACCESS_SECRET, default=""): str,
        vol.Optional(CONF_TUYA_REGION, default="us"): vol.In(["us", "eu", "in", "cn"]),
        vol.Optional(CONF_RTSP_PASSWORD, default=""): str,
    }
)


class EkazaWizardConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None) -> FlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            return self.async_create_entry(
                title="eKaza Wizard",
                data=user_input,
            )

        return self.async_show_form(step_id="user", data_schema=_SCHEMA)
