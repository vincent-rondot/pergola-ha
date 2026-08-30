"""Constants for the Pergola integration."""

from homeassistant.const import Platform

DOMAIN = "pergola"

PLATFORMS: list[Platform] = [
    Platform.COVER,
    Platform.LIGHT,
    Platform.SWITCH,
    Platform.BINARY_SENSOR,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SELECT,
]

CONF_CLIENT_ID = "client_id"
CONF_WEATHER_ENTITY_ID = "weather_entity_id"
