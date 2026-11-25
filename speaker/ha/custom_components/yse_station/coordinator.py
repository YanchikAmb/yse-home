import logging
import time
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import StationApi, StationError
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)
PLAYLISTS_EVERY = 600


class StationCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, api: StationApi, name: str) -> None:
        super().__init__(hass, _LOGGER, name=f"{DOMAIN} {name}", update_interval=timedelta(seconds=10))
        self.api = api
        self._lists_at = 0.0
        self._lists: dict = {"who": "", "playlists": []}

    async def _async_update_data(self) -> dict:
        try:
            voice = await self.api.get("/api/ha/voice")
            device = await self.api.get("/api/ha/device")
            try:
                ble = await self.api.get("/api/ble")
            except StationError:
                ble = {"total": device.get("ble_count"), "sensors": 0, "devices": []}
            if time.monotonic() - self._lists_at > PLAYLISTS_EVERY:
                await self._load_lists()
        except StationError as err:
            raise UpdateFailed(str(err)) from err
        return {"voice": voice, "device": device, "ble": ble, **self._lists}

    async def _load_lists(self) -> None:
        people = await self.api.get("/api/people")
        who = people.get("who") or ""
        playlists = []
        if who:
            try:
                playlists = (await self.api.get(f"/api/playlists?id={who}")).get("playlists") or []
            except StationError:
                playlists = self._lists.get("playlists", [])
        self._lists = {"who": who, "playlists": playlists}
        self._lists_at = time.monotonic()

    async def send(self, path: str, body: dict) -> None:
        try:
            await self.api.post(path, body)
        except StationError as err:
            raise HomeAssistantError(f"Колонка не приняла команду: {err}") from err
        await self.async_request_refresh()
