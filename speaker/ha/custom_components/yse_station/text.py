from homeassistant.components.text import TextEntity, TextMode
from homeassistant.const import EntityCategory

from .const import DOMAIN
from .entity import StationEntity


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        VoiceText(coordinator, entry.entry_id, entry.title, "persona", 0, 3000),
        OpenAiText(coordinator, entry.entry_id, entry.title, "llm_url", 0, 200),
        OpenAiText(coordinator, entry.entry_id, entry.title, "llm_model", 0, 80),
        KeyText(coordinator, entry.entry_id, entry.title, "llm_key", 0, 200),
        NewsText(coordinator, entry.entry_id, entry.title, "news_url", 0, 200),
        NewsText(coordinator, entry.entry_id, entry.title, "news_model", 0, 80),
        NewsKeyText(coordinator, entry.entry_id, entry.title, "news_key", 0, 200),
    ])


class VoiceText(StationEntity, TextEntity):
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator, entry_id, title, key, low, high):
        super().__init__(coordinator, entry_id, title, key)
        self._key = key
        self._attr_native_min = low
        self._attr_native_max = high

    @property
    def native_value(self):
        return self.voice.get(self._key) or ""

    async def async_set_value(self, value):
        value = " ".join(value.split())
        if self._key == "wake" and not value:
            return
        await self.coordinator.send("/api/ha/voice", {self._key: value})


class OpenAiText(VoiceText):
    """Адрес и модель своей нейросети. Имеют смысл только при выборе OpenAI-совместимой."""

    @property
    def available(self):
        return super().available and (self.voice.get("nn") or "") == "openai"


class KeyText(OpenAiText):
    """Ключ API нейросети. Колонка его не отдаёт, видно только, сохранён ли он. «-» стирает."""

    _attr_mode = TextMode.PASSWORD

    @property
    def native_value(self):
        return "********" if self.voice.get("llm_key_set") else ""

    async def async_set_value(self, value):
        value = value.strip()
        if value and value != "********":
            await self.coordinator.send("/api/ha/voice", {"llm_key": value})


class NewsText(VoiceText):
    """Адрес и модель сети для новостей. Видны, когда новости отданы своей сети."""

    @property
    def available(self):
        return super().available and (self.voice.get("news") or "") == "openai"


class NewsKeyText(NewsText):
    """Ключ сети для новостей. Колонка его не отдаёт. «-» стирает."""

    _attr_mode = TextMode.PASSWORD

    @property
    def native_value(self):
        return "********" if self.voice.get("news_key_set") else ""

    async def async_set_value(self, value):
        value = value.strip()
        if value and value != "********":
            await self.coordinator.send("/api/ha/voice", {"news_key": value})
