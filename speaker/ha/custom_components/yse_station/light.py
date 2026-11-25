from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_EFFECT,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)

from .const import DOMAIN, EFFECTS
from .entity import StationEntity


def hex_rgb(text: str) -> tuple[int, int, int]:
    try:
        return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)
    except (TypeError, ValueError):
        return 255, 255, 255


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        LampLight(coordinator, entry.entry_id, entry.title, "lamp"),
        RingLight(coordinator, entry.entry_id, entry.title, "ring"),
    ])


class LampLight(StationEntity, LightEntity):
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}

    @property
    def lamp(self) -> dict:
        return self.device.get("lamp") or {}

    @property
    def is_on(self):
        return bool(self.lamp.get("on"))

    @property
    def brightness(self):
        return self.lamp.get("brightness")

    @property
    def rgb_color(self):
        return self.lamp.get("r", 255), self.lamp.get("g", 255), self.lamp.get("b", 255)

    async def async_turn_on(self, **kwargs):
        body = {"on": True}
        if ATTR_RGB_COLOR in kwargs:
            body["r"], body["g"], body["b"] = kwargs[ATTR_RGB_COLOR]
        if ATTR_BRIGHTNESS in kwargs:
            body["brightness"] = kwargs[ATTR_BRIGHTNESS]
        await self.coordinator.send("/api/ha/light", body)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.send("/api/ha/light", {"on": False})


class RingLight(StationEntity, LightEntity):
    _attr_color_mode = ColorMode.RGB
    _attr_supported_color_modes = {ColorMode.RGB}
    _attr_supported_features = LightEntityFeature.EFFECT
    _attr_effect_list = EFFECTS

    @property
    def ring(self) -> dict:
        return self.device.get("ring") or {}

    @property
    def is_on(self):
        return bool(self.ring.get("on"))

    @property
    def brightness(self):
        return self.ring.get("bright")

    @property
    def rgb_color(self):
        return hex_rgb(self.ring.get("color", "3aa0ff"))

    @property
    def effect(self):
        return self.ring.get("effect")

    async def async_turn_on(self, **kwargs):
        body = {"ring": True}
        if ATTR_RGB_COLOR in kwargs:
            body["color"] = "%02x%02x%02x" % tuple(kwargs[ATTR_RGB_COLOR])
        if ATTR_BRIGHTNESS in kwargs:
            body["bright"] = kwargs[ATTR_BRIGHTNESS]
        if kwargs.get(ATTR_EFFECT) in EFFECTS:
            body["effect"] = kwargs[ATTR_EFFECT]
        await self.coordinator.send("/api/ha/device", body)

    async def async_turn_off(self, **kwargs):
        await self.coordinator.send("/api/ha/device", {"ring": False})
