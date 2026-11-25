from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory

from .const import DOMAIN, EQ_BANDS
from .entity import StationEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        EqNumber(coordinator, entry.entry_id, entry.title, key, index) for index, key in enumerate(EQ_BANDS)
    )


def eq_gains(voice: dict) -> list[int]:
    parts = str(voice.get("eq") or "").split()
    try:
        gains = [int(p) for p in parts]
    except ValueError:
        gains = []
    return gains if len(gains) == len(EQ_BANDS) else [0] * len(EQ_BANDS)


class EqNumber(StationEntity, NumberEntity):
    """Полоса эквалайзера колонки, дБ. Звук меняется сразу, в том числе посреди песни."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = -12
    _attr_native_max_value = 12
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "dB"

    def __init__(self, coordinator, entry_id, title, key, index):
        super().__init__(coordinator, entry_id, title, key)
        self._index = index

    @property
    def native_value(self):
        return eq_gains(self.voice)[self._index]

    async def async_set_native_value(self, value):
        gains = eq_gains(self.voice)
        gains[self._index] = int(round(value))
        await self.coordinator.send("/api/ha/voice", {"eq": " ".join(str(g) for g in gains)})
