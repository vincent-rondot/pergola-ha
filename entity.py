"""Base entity and shared BLE connection helper for the Pergola integration."""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from pergola_controller import Pergola

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN
from .sun_tracker import WinterSunController
from .weather_guard import WindProtectionController

DEFAULT_IDLE_TIMEOUT = 60.0


class PergolaConnection:
    """Holds a single BLE connection to the pergola, shared by every entity
    of a config entry.

    The device only accepts a single BLE connection at a time, so staying
    connected locks out the manufacturer's phone app. Two modes, controlled
    by the "Stay connected" switch entity:

    - Off (default): connect on demand, but keep the link open for
      `idle_timeout` seconds after the last command instead of disconnecting
      immediately - repeated commands in a short session stay fast, and the
      phone app is only blocked briefly after actual use.
    - On: stay connected indefinitely, until turned off, entry unload, or HA
      stop.
    """

    def __init__(self, hass: HomeAssistant, address: str, client_id: str, pin: str) -> None:
        self._hass = hass
        self._address = address
        self._client_id = client_id
        self._pin = pin
        self._pergola: Pergola | None = None
        self.stay_connected: bool = False
        self.idle_timeout: float = DEFAULT_IDLE_TIMEOUT
        self._cancel_idle_timer: Callable[[], None] | None = None
        self._listeners: list[Callable[[], None]] = []

    def add_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: Callable[[], None]) -> None:
        self._listeners.remove(callback)

    def _notify_listeners(self) -> None:
        for callback in self._listeners:
            callback()

    @property
    def is_connected(self) -> bool:
        return self._pergola is not None and self._pergola.client.is_connected

    async def async_get(self) -> Pergola:
        """Return a connected Pergola, reconnecting only if needed."""
        self._cancel_pending_timer()
        if self._pergola is None or not self._pergola.client.is_connected:
            ble_device = bluetooth.async_ble_device_from_address(
                self._hass, self._address, connectable=True
            )
            self._pergola = Pergola(
                self._address, self._client_id, self._pin, ble_device=ble_device
            )
            await self._pergola.connect()
            self._notify_listeners()
        return self._pergola

    def async_schedule_idle_disconnect(self) -> None:
        """Call after a command completes. (Re)starts the idle-disconnect
        countdown unless staying connected."""
        if self.stay_connected:
            return
        self._cancel_pending_timer()
        self._cancel_idle_timer = async_call_later(
            self._hass, self.idle_timeout, self._async_idle_disconnect
        )

    def async_set_stay_connected(self, value: bool) -> None:
        self.stay_connected = value
        if value:
            self._cancel_pending_timer()
        elif self.is_connected:
            self.async_schedule_idle_disconnect()

    def _cancel_pending_timer(self) -> None:
        if self._cancel_idle_timer is not None:
            self._cancel_idle_timer()
            self._cancel_idle_timer = None

    async def _async_idle_disconnect(self, _now=None) -> None:
        self._cancel_idle_timer = None
        await self.async_disconnect()

    async def async_disconnect(self) -> None:
        """Disconnect if connected. Call on entry unload / HA stop / idle timeout."""
        self._cancel_pending_timer()
        if self._pergola is not None:
            try:
                await self._pergola.disconnect()
            except Exception:  # pragma: no cover - best-effort cleanup
                pass
            self._pergola = None
            self._notify_listeners()


@dataclass
class PergolaRuntimeData:
    """Everything shared across this config entry's entities."""

    connection: PergolaConnection
    wind_protection: WindProtectionController
    winter_sun: WinterSunController


class PergolaEntity(Entity):
    """Base entity sharing device info and the shared BLE connection."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry) -> None:
        """Initialize the entity."""
        runtime_data: PergolaRuntimeData = entry.runtime_data
        self._connection = runtime_data.connection
        self._wind_protection = runtime_data.wind_protection
        self._winter_sun = runtime_data.winter_sun
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
        finally:
            self._connection.async_schedule_idle_disconnect()
