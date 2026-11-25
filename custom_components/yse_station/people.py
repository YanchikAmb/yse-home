import hmac
import time

from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import CONF_TOKEN, DOMAIN

SIGNAL_SPEAKER = f"{DOMAIN}_speaker"


class PeopleView(HomeAssistantView):
    """Люди HA для привязки голосов и «кто говорит» с колонки. Вход по ключу страницы колонки."""

    url = "/api/yse_station/people"
    name = "api:yse_station:people"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass

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
            data = {}
        speaker = data.get("speaker") if isinstance(data, dict) else None
        if isinstance(speaker, dict):
            state = {
                "name": str(speaker.get("name") or "")[:60],
                "person": str(speaker.get("person") or "")[:80],
                "score": float(speaker.get("score") or 0),
                "at": time.time(),
            }
            self.hass.data.setdefault(DOMAIN + "_speaker", {})[entry.entry_id] = state
            async_dispatcher_send(self.hass, f"{SIGNAL_SPEAKER}_{entry.entry_id}")
        persons = [
            {"id": st.entity_id, "name": st.attributes.get("friendly_name") or st.entity_id}
            for st in self.hass.states.async_all("person")
        ]
        return self.json({"persons": persons})
