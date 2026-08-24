"""Config flow for the Pergola integration."""

import logging
from typing import Any

from pergola_controller import Pergola
from pergola_controller.client import PergolaClient
from pergola_controller.config import DEFAULT_PIN
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS, CONF_PIN
from homeassistant.helpers import instance_id as instance_id_helper
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from .const import CONF_CLIENT_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)

# Address scan_devices() always appends as a fallback, so the config flow
# (and the integration) can be used/tested without real BLE hardware.
_SIMULATED_ADDRESS = "00:11:22:33:44:55"


class PergolaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Pergola."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Scan for nearby pergolas, then ask for its credentials."""
        errors: dict[str, str] = {}
        devices = await PergolaClient(android_id="0").scan_devices()
        options = [
            SelectOptionDict(
                value=device["address"],
                label=f"{device['name']} ({device['address']})",
            )
            for device in devices
        ]

        # scan_devices() never raises - it always returns at least the
        # simulated entry even when no real Bluetooth adapter is available.
        # Surface that distinction explicitly instead of silently offering
        # only the simulated option with no explanation.
        no_real_devices = all(d["address"] == _SIMULATED_ADDRESS for d in devices)
        warning = (
            "⚠️ No real Bluetooth devices found — check that a Bluetooth "
            "adapter is connected and working on this Home Assistant host. "
            "Only the simulated device is available."
            if no_real_devices
            else ""
        )

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_ADDRESS])
            self._abort_if_unique_id_configured()

            try:
                async with Pergola(
                    user_input[CONF_ADDRESS],
                    user_input[CONF_CLIENT_ID],
                    user_input[CONF_PIN],
                ):
                    pass
            except Exception:
                _LOGGER.exception("Could not connect to the pergola")
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="Pergola", data=user_input)

        instance_uuid = await instance_id_helper.async_get(self.hass)
        default_client_id = instance_uuid.replace("-", "")[:16]

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): SelectSelector(
                    SelectSelectorConfig(options=options)
                ),
                vol.Required(CONF_CLIENT_ID, default=default_client_id): str,
                vol.Required(CONF_PIN, default=DEFAULT_PIN): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors,
            description_placeholders={"warning": warning},
        )
