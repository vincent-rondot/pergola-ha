"""Base entity and shared BLE connection helper for the Pergola integration."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pergola_controller import Pergola

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_PIN
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import CONF_CLIENT_ID, DOMAIN


class PergolaEntity(Entity):
    """Base entity sharing device info and the connect-per-command BLE helper."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the entity."""
        self._address = entry.data[CONF_ADDRESS]
        self._client_id = entry.data[CONF_CLIENT_ID]
        self._pin = entry.data[CONF_PIN]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Pergola",
        )

    @asynccontextmanager
    async def _async_connect(self) -> AsyncIterator[Pergola]:
        """Open a BLE connection for the duration of one command.

        The device only accepts a single BLE connection at a time, so we
        never stay connected - that would lock the manufacturer's phone app
        out.
        """
        try:
            ble_device = bluetooth.async_ble_device_from_address(
                self.hass, self._address, connectable=True
            )
            async with Pergola(
                self._address, self._client_id, self._pin, ble_device=ble_device
            ) as pergola:
                yield pergola
        except Exception as err:
            raise HomeAssistantError(
                f"Could not reach the pergola at {self._address}: {err}"
            ) from err
