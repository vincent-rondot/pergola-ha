"""The Pergola integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_PIN, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant

from .const import CONF_CLIENT_ID, CONF_WEATHER_ENTITY_ID, PLATFORMS
from .entity import PergolaConnection, PergolaRuntimeData
from .sun_tracker import WinterSunController
from .weather_guard import WindProtectionController

_LOGGER = logging.getLogger(__name__)

# This integration depends on the private `pergola_controller` package (see
# manifest.json requirements), installed via `git+https://...` and
# authenticated through a GitHub PAT in a `.netrc` file. That file lives at
# /root/.netrc inside the `homeassistant` container, which is NOT persisted
# across Home Assistant Core updates (the container gets recreated from a
# fresh image; only /config, /media, /share, /ssl survive).
#
# A backup copy is kept at /config/.netrc (persistent). If this integration
# fails to load after a Core update with a "could not read Username for
# 'https://github.com'" error, restore it by running on the host:
#   docker cp homeassistant:/config/.netrc /tmp/n && docker cp /tmp/n homeassistant:/root/.netrc && docker exec homeassistant chmod 600 /root/.netrc
_LOGGER.info(
    "pergola: loaded OK. If this ever fails after a HA Core update with a "
    "git/github auth error, see the comment at the top of __init__.py "
    "(and pergola-ha's README) to restore /root/.netrc from /config/.netrc."
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Pergola from a config entry."""

    def _get_weather_entity_id() -> str | None:
        # Read live from entry.options (not captured once) so an options
        # change - which reloads the entry via OptionsFlowWithReload anyway -
        # isn't the only way this can pick up an edit.
        return entry.options.get(CONF_WEATHER_ENTITY_ID)

    connection = PergolaConnection(
        hass,
        entry.data[CONF_ADDRESS],
        entry.data[CONF_CLIENT_ID],
        entry.data[CONF_PIN],
    )
    wind_protection = WindProtectionController(hass, _get_weather_entity_id)
    winter_sun = WinterSunController(
        hass,
        entry,
        connection,
        _get_weather_entity_id,
        lambda: wind_protection.wind_speed_threshold,
    )
    entry.runtime_data = PergolaRuntimeData(
        connection=connection, wind_protection=wind_protection, winter_sun=winter_sun
    )

    async def _async_disconnect(_event=None) -> None:
        await wind_protection.async_disable()
        await winter_sun.async_disable()
        await connection.async_disconnect()

    # Disconnect on entry unload/removal (e.g. deleting the integration) AND
    # on Home Assistant shutdown/restart - a shared connection left dangling
    # would otherwise keep locking out the manufacturer's phone app.
    entry.async_on_unload(_async_disconnect)
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_disconnect)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Wind protection is a safety feature, not a seasonal one: it starts
    # enabled on every setup (a configured weather entity is required for it
    # to actually do anything - see WindProtectionController.async_enable).
    # It does not persist a user "off" across restarts, same as every other
    # switch in this integration (e.g. "Stay connected") - simplest to
    # reason about, and defaults back towards protection rather than away
    # from it.
    if _get_weather_entity_id() is not None:
        await wind_protection.async_enable()

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
