from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory

from .const import BRAINS, DOMAIN, EQ_PRESETS, NEWS, STT_ENGINES, VOICES, WAKE_WORDS
from .entity import StationEntity
from .number import eq_gains


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        BrainSelect(coordinator, entry.entry_id, entry.title, "brain"),
        VoiceSelect(coordinator, entry.entry_id, entry.title, "voice"),
        SttSelect(coordinator, entry.entry_id, entry.title, "stt"),
        WakeSelect(coordinator, entry.entry_id, entry.title, "wake"),
        NewsSelect(coordinator, entry.entry_id, entry.title, "news"),
        EqSelect(coordinator, entry.entry_id, entry.title, "eq"),
    ])


class EqSelect(StationEntity, SelectEntity):
    """Готовый набор эквалайзера. Свои ползунки — пустое значение."""

    _attr_options = list(EQ_PRESETS)
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def current_option(self):
        now = " ".join(str(g) for g in eq_gains(self.voice))
        return next((name for name, gains in EQ_PRESETS.items() if gains == now), None)

    async def async_select_option(self, option):
        await self.coordinator.send("/api/ha/voice", {"eq": EQ_PRESETS[option]})


class BrainSelect(StationEntity, SelectEntity):
    _attr_options = BRAINS
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def current_option(self):
        value = self.voice.get("nn") or ""
        if value in BRAINS:
            return value
        return "none" if not value else None

    async def async_select_option(self, option):
        await self.coordinator.send("/api/ha/voice", {"nn": "" if option == "none" else option})


class VoiceSelect(StationEntity, SelectEntity):
    _attr_options = VOICES
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def current_option(self):
        value = (self.voice.get("voice") or "").replace("own:", "own_")
        return value if value in VOICES else None

    async def async_select_option(self, option):
        await self.coordinator.send("/api/ha/voice", {"voice": option.replace("own_", "own:")})


class SttSelect(StationEntity, SelectEntity):
    """Кто распознаёт фразу на сервере. С галочкой «Локально» спальня разбирает сама."""

    _attr_options = STT_ENGINES
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def current_option(self):
        value = self.voice.get("stt")
        return value if value in STT_ENGINES else None

    async def async_select_option(self, option):
        await self.coordinator.send("/api/ha/voice", {"stt": option})


class WakeSelect(StationEntity, SelectEntity):
    """Слово пробуждения: джарвис локально на колонке, джарвис на сервере, алиса и яндекс штатные."""

    _attr_options = WAKE_WORDS
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def options(self):
        value = (self.voice.get("wake") or "").strip()
        if value and value not in WAKE_WORDS:
            return [*WAKE_WORDS, value]
        return list(WAKE_WORDS)

    @property
    def current_option(self):
        value = (self.voice.get("wake") or "").strip()
        return value if value in self.options else None

    async def async_select_option(self, option):
        await self.coordinator.send("/api/ha/voice", {"wake": option})


class NewsSelect(StationEntity, SelectEntity):
    """Кто отвечает на новости и свежие факты. Адрес и ключ — отдельные от разговора."""

    _attr_options = NEWS
    _attr_entity_category = EntityCategory.CONFIG

    @property
    def current_option(self):
        value = self.voice.get("news") or "yandex"
        return value if value in NEWS else "yandex"

    async def async_select_option(self, option):
        await self.coordinator.send("/api/ha/voice", {"news": option})
