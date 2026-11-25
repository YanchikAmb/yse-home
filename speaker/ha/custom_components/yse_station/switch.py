from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory

from .const import DOMAIN
from .entity import StationEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        VoiceSwitch(coordinator, entry.entry_id, entry.title, "wake_sound"),
        VoiceSwitch(coordinator, entry.entry_id, entry.title, "bluetooth"),
        LocalSwitch(coordinator, entry.entry_id, entry.title, "local"),
    ])


class VoiceSwitch(StationEntity, SwitchEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry_id, title, key):
        super().__init__(coordinator, entry_id, title, key)
        self._key = key

    @property
    def is_on(self):
        return bool(self.voice.get(self._key))

    async def async_turn_on(self, **kwargs):
        await self.coordinator.send("/api/ha/voice", {self._key: True})

    async def async_turn_off(self, **kwargs):
        await self.coordinator.send("/api/ha/voice", {self._key: False})


class LocalSwitch(VoiceSwitch):
    """Распознавание на самой колонке. Есть только у Станции 2 (спальня), у Мини 2 недоступно."""

    @property
    def available(self):
        return super().available and bool(self.voice.get("local_ok"))

    @property
    def is_on(self):
        return self.voice.get("local") == "1"

    async def async_turn_on(self, **kwargs):
        await self.coordinator.send("/api/ha/voice", {"local": "1"})

    async def async_turn_off(self, **kwargs):
        await self.coordinator.send("/api/ha/voice", {"local": "0"})
