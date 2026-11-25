from homeassistant.components.button import ButtonEntity

from .const import DOMAIN
from .entity import StationEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        MarkButton(coordinator, entry.entry_id, entry.title, "like"),
        MarkButton(coordinator, entry.entry_id, entry.title, "dislike"),
    ])


class MarkButton(StationEntity, ButtonEntity):
    def __init__(self, coordinator, entry_id, title, key):
        super().__init__(coordinator, entry_id, title, key)
        self._key = key

    async def async_press(self):
        await self.coordinator.send("/api/music/mark", {"cmd": self._key})
