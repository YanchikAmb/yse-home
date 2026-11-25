import hmac
import re
import time

from aiohttp import web
from homeassistant.components import conversation
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import Context, HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er, intent

from .const import CONF_TOKEN, DOMAIN

KEEP_SECONDS = 300
# Глаголы не называют объект. Иначе «включи» цепляется к переключателю «Включить».
VERBS = {
    "включи", "включите", "включить", "выключи", "выключите", "выключить",
    "отключи", "отключите", "отключить", "поставь", "поставьте", "поставить",
    "запусти", "запустите", "запустить", "останови", "остановите", "остановить",
    "открой", "откройте", "открыть", "закрой", "закройте", "закрыть",
    "зажги", "зажечь", "погаси", "погасить", "вскипяти", "вскипятить",
    "закипяти", "закипятить", "сделай", "сделайте", "давай", "пожалуйста",
    "колонка", "джарвис", "мне",
}
OFF_WORDS = re.compile(r"^(выключ|отключ|погас|закро|останови)")
COMMAND = re.compile(
    r"\b(включи\w*|выключи\w*|отключи\w*|поставь\w*|запусти\w*|останови\w*|"
    r"откро\w*|закро\w*|зажги\w*|погас\w*|вскипяти\w*|закипяти\w*|нажми\w*)"
)
# То, чем можно командовать голосом. Датчики и сырой английский id сюда не входят:
# сравниваем только имя, которое видно в доме.
NAMED = (
    "automation", "script", "scene", "input_boolean", "input_button", "button",
    "switch", "light", "fan", "cover", "lock", "vacuum", "climate",
    "humidifier", "media_player", "water_heater",
)
RUN = {"automation", "script", "scene", "button", "input_button"}
TAIL = ("а", "у", "е", "ы", "и", "ом", "ем", "ой", "ей")


def _words(text: str) -> list[str]:
    # В HA 2026 в aliases бывает служебная метка ComputedNameType, не строка.
    if not isinstance(text, str):
        return []
    return re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е"))


def _tokens(text: str) -> list[str]:
    return [word for word in _words(text) if len(word) >= 3 and word not in VERBS]


def _plain(text: str) -> str:
    return " ".join(word for word in _words(text) if len(word) >= 3)


def _remainder(text: str) -> str:
    return " ".join(word for word in _words(text) if len(word) >= 3 and word not in VERBS)


END = set("аяуюеиыо")


def _same(left: str, right: str) -> bool:
    if left == right:
        return True
    if len(left) < len(right):
        left, right = right, left
    if len(right) >= 5 and left.startswith(right) and left[len(right):] in TAIL:
        return True
    # «вани» и «ваня»: окончание сменилось, основа та же.
    return (
        len(left) == len(right) >= 4
        and left[:-1] == right[:-1]
        and left[-1] in END
        and right[-1] in END
    )


def _names(hass: HomeAssistant, entity_id: str, title: str) -> list[str]:
    found = [title] if isinstance(title, str) and title.strip() else []
    item = er.async_get(hass).async_get(entity_id)
    if item is None:
        return found
    for alias in item.aliases or ():
        if isinstance(alias, str) and alias.strip():
            found.append(alias)
    return found


def _score(said: list[str], names: list[str], remainder: str) -> tuple[int, str]:
    """Точное имя или псевдоним важнее длинного названия, где это слово просто встречается."""
    best = 0
    label = ""
    for name in names:
        words = _tokens(name)
        hit = [word for word in words if any(_same(word, item) for item in said)]
        if not hit:
            continue
        score = sum(len(word) for word in hit)
        if words and len(hit) == len(words):
            score += 5
        if remainder and _plain(name) == remainder:
            score += 20
        if score > best:
            best = score
            label = name
    return best, label


class AskView(HomeAssistantView):
    """Текст с колонки в Assist. Вход по ключу страницы колонки, токен HA не нужен."""

    url = "/api/yse_station/ask"
    name = "api:yse_station:ask"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.talks: dict[str, tuple[str, float]] = {}

    def entry_for(self, token: str):
        for entry in self.hass.config_entries.async_entries(DOMAIN):
            saved = entry.data.get(CONF_TOKEN) or ""
            if saved and token and hmac.compare_digest(saved, token):
                return entry
        return None

    async def post(self, request: web.Request) -> web.Response:
        entry = self.entry_for(request.headers.get("X-Token", ""))
        if entry is None:
            return self.json({"error": "token"}, status_code=401)
        try:
            data = await request.json()
        except ValueError:
            return self.json({"error": "json"}, status_code=400)
        text = str(data.get("text") or "").strip()[:400]
        if not text:
            return self.json({"error": "text"}, status_code=400)
        language = str(data.get("language") or "ru")
        agent_id = conversation.HOME_ASSISTANT_AGENT
        if data.get("agent") == "default":
            from homeassistant.components.assist_pipeline import async_get_pipeline

            agent_id = async_get_pipeline(self.hass).conversation_engine or agent_id
        device = dr.async_get(self.hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
        last = self.talks.get(entry.entry_id)
        talk_id = last[0] if last and time.monotonic() - last[1] < KEEP_SECONDS else None
        result = await conversation.async_converse(
            self.hass,
            text,
            talk_id,
            Context(),
            language=language,
            agent_id=agent_id,
            device_id=device.id if device else None,
        )
        if result.conversation_id:
            self.talks[entry.entry_id] = (result.conversation_id, time.monotonic())
        response = result.response
        # Дом забирает фразу, только если команда выполнена или это ответ о состоянии.
        # «Нет такого объекта» и «функция не поддерживается» — не дом: колонка отдаёт фразу музыке и облаку.
        # Ошибка сервиса и «несколько объектов» остаются дому, чтобы вместо света не включилась песня.
        home = response.response_type in (
            intent.IntentResponseType.ACTION_DONE,
            intent.IntentResponseType.QUERY_ANSWER,
            intent.IntentResponseType.PARTIAL_ACTION_DONE,
        )
        if response.response_type == intent.IntentResponseType.ERROR:
            home = response.error_code not in (
                intent.IntentResponseErrorCode.NO_INTENT_MATCH,
                intent.IntentResponseErrorCode.NO_VALID_TARGETS,
            )
        raw_speech = response.speech
        if isinstance(raw_speech, dict):
            speech = ((raw_speech.get("plain") or {}).get("speech") or "")
        else:
            speech = raw_speech or ""
        if home:
            return self.json({"matched": True, "speech": speech})
        named = await self.named_home(text)
        if named is not None:
            return self.json(named)
        return self.json({"matched": False, "speech": speech})

    async def named_home(self, text: str) -> dict | None:
        """Включить и выключить — у объекта, у которого есть такое действие.
        Автоматизация запускается, только если устройства с этим именем нет."""
        if not COMMAND.search(text.lower().replace("ё", "е")):
            return None
        said = _tokens(text)
        remainder = _remainder(text)
        if not said:
            return None
        off = any(OFF_WORDS.match(word) for word in _words(text))
        ranked: list[tuple[int, str, str]] = []
        for domain in NAMED:
            for state in self.hass.states.async_all(domain):
                if state.state in ("unavailable", "unknown"):
                    continue
                score, label = _score(said, _names(self.hass, state.entity_id, state.name), remainder)
                if score:
                    ranked.append((score, state.entity_id, label or state.name))
        if not ranked:
            return None
        ranked.sort(key=lambda row: row[0], reverse=True)
        top = ranked[0][0]
        winners = [row for row in ranked if row[0] == top]
        devices = [row for row in winners if row[1].split(".", 1)[0] not in RUN]
        if devices:
            winners = devices
        if len(winners) > 1:
            labels = ", ".join(name for _best, _eid, name in winners[:3])
            return {"matched": True, "speech": "Их несколько: %s. Скажите точнее." % labels}
        _best, entity_id, name = winners[0]
        domain = entity_id.split(".", 1)[0]
        if off and domain in RUN:
            return {"matched": True, "speech": "%s только запускается." % name}
        if domain == "automation":
            service, payload = "trigger", {"entity_id": entity_id, "skip_condition": True}
        elif domain == "script":
            service, payload = "turn_on", {"entity_id": entity_id}
        elif domain == "scene":
            service, payload = "turn_on", {"entity_id": entity_id}
        elif domain in ("button", "input_button"):
            service, payload = "press", {"entity_id": entity_id}
        else:
            service, payload = ("turn_off" if off else "turn_on"), {"entity_id": entity_id}
        try:
            await self.hass.services.async_call(domain, service, payload, blocking=True)
        except Exception:
            return {"matched": True, "speech": "%s не ответил." % name}
        return {"matched": True, "speech": name}
