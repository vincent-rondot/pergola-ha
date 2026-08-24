"""Config flow for the Pergola integration."""

import logging
from typing import Any

from pergola_controller import Pergola
from pergola_controller.config import DEFAULT_PIN
import voluptuous as vol

from homeassistant.components import bluetooth
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

# Offered alongside real devices so the config flow (and the integration)
# can be set up and tested without real BLE hardware.
_SIMULATED_ADDRESS = "00:11:22:33:44:55"


class PergolaConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Pergola."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """List nearby pergolas already known to HA's Bluetooth manager, then ask for credentials."""
        errors: dict[str, str] = {}

        # Read from HA's own continuously-running Bluetooth scanner instead of
        # starting a competing one - a direct BleakScanner.discover() call
        # here gets cut off almost instantly when run inside the live HA
        # process (observed: 0 devices after 0.0s, no exception - consistent
        # with HA's own flow-step handling racing/cancelling it).
        service_infos = bluetooth.async_discovered_service_info(
            self.hass, connectable=True
        )
        options = [
            SelectOptionDict(
                value=info.address, label=f"{info.name or info.address} ({info.address})"
            )
            for info in service_infos
        ]
        options.append(
            SelectOptionDict(
                value=_SIMULATED_ADDRESS, label=f"Pergola (Simulated) ({_SIMULATED_ADDRESS})"
            )
        )

        no_real_devices = not service_infos
        warning = (
            "⚠️ No real Bluetooth devices found — check that a Bluetooth "
            "adapter is connected and working on this Home Assistant host, "
            "and that the Bluetooth integration is enabled. Only the "
            "simulated device is available."
            if no_real_devices
            else ""
        )

        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_ADDRESS])
            self._abort_if_unique_id_configured()

            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, user_input[CONF_ADDRESS], connectable=True
            )

            try:
                async with Pergola(
                    user_input[CONF_ADDRESS],
                    user_input[CONF_CLIENT_ID],
                    user_input[CONF_PIN],
                    ble_device=ble_device,
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
