import hmac
import re
from collections.abc import AsyncIterable

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.util import dt as dt_util

from .const import CONF_TOKEN, DOMAIN, WEATHER_ENTITY

HERE_WORDS = ("здесь", "тут", "в этой комнате", "у меня")
SKIP_SENSOR = re.compile(
    r"printer|принтер|kettle|чайник|cpu|battery|батаре|soc|3d|nozzle|bed_|холодильник|fridge|freezer|board_temperature|платы"
)


def entry_for(hass: HomeAssistant, token: str):
    for entry in hass.config_entries.async_entries(DOMAIN):
        saved = entry.data.get(CONF_TOKEN) or ""
        if saved and token and hmac.compare_digest(saved, token):
            return entry
    return None


def own_area(hass: HomeAssistant, entry) -> str | None:
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    return device.area_id if device else None


def _words(text: str) -> list[str]:
    return [w for w in re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е")) if len(w) >= 3]


def _same(a: str, b: str) -> bool:
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= max(3, min(len(a), len(b)) - 2)


def named_area(hass: HomeAssistant, text: str, own: str | None):
    """Комната, названная во фразе: «в спальне», «в детской Вани». Лучшая по числу совпавших слов."""
    said = _words(text)
    best = []
    top = 0
    for area in ar.async_get(hass).async_list_areas():
        names = [area.name, *getattr(area, "aliases", ())]
        score = 0
        for name in names:
            parts = _words(name)
            hit = sum(1 for p in parts if any(_same(p, w) for w in said))
            score = max(score, hit)
        if score > top:
            top, best = score, [area]
        elif score and score == top:
            best.append(area)
    if not best:
        return None
    for area in best:
        if area.id == own:
            return area
    return best[0]


def area_temperature(hass: HomeAssistant, area_id: str):
    ents = er.async_get(hass)
    devs = dr.async_get(hass)
    found = []
    for item in ents.entities.values():
        if item.domain != "sensor" or item.disabled_by:
            continue
        where = item.area_id
        if not where and item.device_id:
            device = devs.async_get(item.device_id)
            where = device.area_id if device else None
        if where != area_id:
            continue
        state = hass.states.get(item.entity_id)
        if state is None or state.attributes.get("device_class") != "temperature":
            continue
        if SKIP_SENSOR.search(item.entity_id) or SKIP_SENSOR.search(str(state.name).lower()):
            continue
        try:
            value = float(state.state)
        except ValueError:
            continue
        rank = 0 if item.entity_id.endswith("_temperature_vrt") else (1 if "_vrt" in item.entity_id else 2)
        found.append((rank, item.entity_id, value))
    if not found:
        return None
    found.sort()
    return found[0][2]


class ClimateView(HomeAssistantView):
    """Температура комнаты колонки или названной комнаты и погода. Вход по ключу страницы колонки."""

    url = "/api/yse_station/climate"
    name = "api:yse_station:climate"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        entry = entry_for(self.hass, request.headers.get("X-Token", ""))
        if entry is None:
            return self.json({"error": "token"}, status_code=401)
        try:
            data = await request.json()
        except ValueError:
            return self.json({"error": "json"}, status_code=400)
        text = str(data.get("text") or "")[:400]
        if data.get("kind") == "weather":
            return self.json(await self.weather())
        own = own_area(self.hass, entry)
        area = None
        if not any(word in text.lower() for word in HERE_WORDS):
            area = named_area(self.hass, text, own)
        if area is None and own:
            area = ar.async_get(self.hass).async_get_area(own)
        if area is None:
            return self.json({"ok": False})
        value = area_temperature(self.hass, area.id)
        if value is None:
            return self.json({"ok": False, "room": area.name})
        return self.json({"ok": True, "room": area.name, "own": area.id == own, "temperature": value})

    async def weather(self) -> dict:
        entity_id = WEATHER_ENTITY
        state = self.hass.states.get(entity_id)
        if state is None:
            others = self.hass.states.async_all("weather")
            if not others:
                return {"ok": False}
            state = others[0]
            entity_id = state.entity_id
        attrs = state.attributes
        out = {
            "ok": True,
            "condition": state.state,
            "temperature": attrs.get("temperature"),
            "humidity": attrs.get("humidity"),
            "wind_speed": attrs.get("wind_speed"),
            "wind_unit": attrs.get("wind_speed_unit") or "",
        }
        days = {}
        # Погода Яндекса даёт только twice_daily (ночь и день) и hourly.
        for kind in ("daily", "twice_daily"):
            try:
                resp = await self.hass.services.async_call(
                    "weather", "get_forecasts", {"entity_id": entity_id, "type": kind},
                    blocking=True, return_response=True,
                )
            except Exception:  # noqa: BLE001 — прогноз не обязателен
                continue
            for part in ((resp or {}).get(entity_id) or {}).get("forecast") or []:
                when = dt_util.parse_datetime(str(part.get("datetime") or ""))
                if when is None or part.get("temperature") is None:
                    continue
                date = dt_util.as_local(when).date()
                day = days.setdefault(date, {"temperature": None, "templow": None, "condition": None})
                high = float(part["temperature"])
                low = float(part["templow"]) if part.get("templow") is not None else high
                day["temperature"] = high if day["temperature"] is None else max(day["temperature"], high)
                day["templow"] = low if day["templow"] is None else min(day["templow"], low)
                if part.get("is_daytime", True) or day["condition"] is None:
                    day["condition"] = part.get("condition")
            if days:
                break
        today = dt_util.now().date()
        for date, day in days.items():
            if date == today:
                out["today"] = day
            elif (date - today).days == 1:
                out["tomorrow"] = day
        return out


class SttView(HomeAssistantView):
    """Фраза 16 кГц, 16 бит, моно — в распознавание помощника HA по умолчанию."""

    url = "/api/yse_station/stt"
    name = "api:yse_station:stt"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

    async def post(self, request: web.Request) -> web.Response:
        from homeassistant.components import stt
        from homeassistant.components.assist_pipeline import async_get_pipeline

        entry = entry_for(self.hass, request.headers.get("X-Token", ""))
        if entry is None:
            return self.json({"error": "token"}, status_code=401)
        pcm = await request.read()
        if len(pcm) < 3200 or len(pcm) > 16000 * 2 * 15:
            return self.json({"ok": False, "error": "length"}, status_code=400)
        pipeline = async_get_pipeline(self.hass)
        engine = stt.async_get_speech_to_text_engine(self.hass, pipeline.stt_engine) if pipeline.stt_engine else None
        if engine is None:
            return self.json({"ok": False, "error": "no_stt"})
        metadata = stt.SpeechMetadata(
            language=pipeline.stt_language or pipeline.language or "ru",
            format=stt.AudioFormats.WAV,
            codec=stt.AudioCodecs.PCM,
            bit_rate=stt.AudioBitRates.BITRATE_16,
            sample_rate=stt.AudioSampleRates.SAMPLERATE_16000,
            channel=stt.AudioChannels.CHANNEL_MONO,
        )

        async def chunks() -> AsyncIterable[bytes]:
            for offset in range(0, len(pcm), 3200):
                yield pcm[offset:offset + 3200]

        result = await engine.async_process_audio_stream(metadata, chunks())
        if result.result != stt.SpeechResultState.SUCCESS:
            return self.json({"ok": False, "error": "stt"})
        return self.json({"ok": True, "text": result.text or ""})
