"""Constants for the Pergola integration."""

from homeassistant.const import Platform

DOMAIN = "pergola"

PLATFORMS: list[Platform] = [Platform.COVER, Platform.LIGHT]

CONF_ANDROID_ID = "android_id"
