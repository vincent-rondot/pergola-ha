"""Config flow for the Pergola integration."""

import logging
from typing import Any

from pergola_controller import Pergola
from pergola_controller.client import PergolaClient
from pergola_controller.config import DEFAULT_PIN
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS, CONF_PIN
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
)

from .const import CONF_ANDROID_ID, DOMAIN

_LOGGER = logging.getLogger(__name__)


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

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_ADDRESS])
            self._abort_if_unique_id_configured()

            try:
                async with Pergola(
                    user_input[CONF_ADDRESS],
                    user_input[CONF_ANDROID_ID],
                    user_input[CONF_PIN],
                ):
                    pass
            except Exception:
                _LOGGER.exception("Could not connect to the pergola")
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(title="Pergola", data=user_input)

        schema = vol.Schema(
            {
                vol.Required(CONF_ADDRESS): SelectSelector(
                    SelectSelectorConfig(options=options)
                ),
                vol.Required(CONF_ANDROID_ID): str,
                vol.Required(CONF_PIN, default=DEFAULT_PIN): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
