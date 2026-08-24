"""The Pergola integration."""

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_ADDRESS, CONF_PIN, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant

from .const import CONF_CLIENT_ID, PLATFORMS
from .entity import PergolaConnection

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
    entry.runtime_data = PergolaConnection(
        hass,
        entry.data[CONF_ADDRESS],
        entry.data[CONF_CLIENT_ID],
        entry.data[CONF_PIN],
    )

    async def _async_disconnect(_event=None) -> None:
        await entry.runtime_data.async_disconnect()

    # Disconnect on entry unload/removal (e.g. deleting the integration) AND
    # on Home Assistant shutdown/restart - a shared connection left dangling
    # would otherwise keep locking out the manufacturer's phone app.
    entry.async_on_unload(_async_disconnect)
    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_disconnect)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
