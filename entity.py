"""Base entity and shared BLE connection helper for the Pergola integration."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from pergola_controller import Pergola

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity

from .const import DOMAIN


class PergolaConnection:
    """Holds a single BLE connection to the pergola, shared by every entity
    of a config entry.

    TEMPORARY: stays connected once established, instead of disconnecting
    after each command - being tested to see whether the "Notify acquired"
    BlueZ errors seen on the production HAOS instance are caused by the
    connect/disconnect churn (one cycle per command) rather than the
    connection itself. The device only accepts a single BLE connection at a
    time, so if/when this becomes permanent it needs to be an opt-in,
    user-visible choice (a "Stay connected" switch entity), not the default -
    staying connected locks out the manufacturer's phone app.
    """

    def __init__(self, hass: HomeAssistant, address: str, client_id: str, pin: str) -> None:
        self._hass = hass
        self._address = address
        self._client_id = client_id
        self._pin = pin
        self._pergola: Pergola | None = None

    async def async_get(self) -> Pergola:
        """Return a connected Pergola, reconnecting only if needed."""
        if self._pergola is None or not self._pergola.client.is_connected:
            ble_device = bluetooth.async_ble_device_from_address(
                self._hass, self._address, connectable=True
            )
            self._pergola = Pergola(
                self._address, self._client_id, self._pin, ble_device=ble_device
            )
            await self._pergola.connect()
        return self._pergola

    async def async_disconnect(self) -> None:
        """Disconnect if connected. Call on entry unload / HA stop."""
        if self._pergola is not None:
            try:
                await self._pergola.disconnect()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._pergola = None


class PergolaEntity(Entity):
    """Base entity sharing device info and the shared BLE connection."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the entity."""
        self._connection: PergolaConnection = entry.runtime_data
        self._address = entry.data[CONF_ADDRESS]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="Pergola",
        )

    @asynccontextmanager
    async def _async_connect(self) -> AsyncIterator[Pergola]:
        """Get the shared connection, connecting if needed."""
        try:
            pergola = await self._connection.async_get()
            yield pergola
        except Exception as err:
            raise HomeAssistantError(
                f"Could not reach the pergola at {self._address}: {err}"
            ) from err
