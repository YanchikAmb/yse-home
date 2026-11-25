from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)

from .const import DOMAIN
from .entity import StationEntity

STATES = {"play": MediaPlayerState.PLAYING, "pause": MediaPlayerState.PAUSED, "stop": MediaPlayerState.IDLE}


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([StationPlayer(coordinator, entry.entry_id, entry.title, "player")])


class StationPlayer(StationEntity, MediaPlayerEntity):
    _attr_name = None
    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.PAUSE
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.NEXT_TRACK
        | MediaPlayerEntityFeature.PREVIOUS_TRACK
        | MediaPlayerEntityFeature.VOLUME_SET
        | MediaPlayerEntityFeature.VOLUME_STEP
        | MediaPlayerEntityFeature.SELECT_SOURCE
    )

    @property
    def music(self) -> dict:
        return self.device.get("music") or {}

    @property
    def state(self):
        return STATES.get(self.music.get("state"), MediaPlayerState.IDLE)

    @property
    def volume_level(self):
        volume = self.voice.get("volume")
        return None if volume is None else int(volume) / 100

    @property
    def media_title(self):
        return self.music.get("title") or None

    @property
    def media_artist(self):
        return self.music.get("artist") or None

    @property
    def media_playlist(self):
        return self.music.get("playlist") or None

    @property
    def source_list(self):
        return [p.get("title") for p in self.coordinator.data.get("playlists", []) if p.get("title")]

    @property
    def source(self):
        kind = self.music.get("kind")
        for p in self.coordinator.data.get("playlists", []):
            if str(p.get("kind")) == str(kind):
                return p.get("title")
        return None

    async def async_select_source(self, source):
        who = self.coordinator.data.get("who")
        for p in self.coordinator.data.get("playlists", []):
            if p.get("title") == source and who:
                await self.coordinator.send("/api/music/play", {"id": who, "kind": str(p.get("kind"))})
                return

    async def async_set_volume_level(self, volume):
        await self.coordinator.send("/api/ha/voice", {"volume": round(volume * 100)})

    async def async_volume_up(self):
        await self.async_set_volume_level(min(1.0, (self.volume_level or 0) + 0.05))

    async def async_volume_down(self):
        await self.async_set_volume_level(max(0.0, (self.volume_level or 0) - 0.05))

    async def async_media_play(self):
        await self.coordinator.send("/api/music/ctrl", {"cmd": "play"})

    async def async_media_pause(self):
        await self.coordinator.send("/api/music/ctrl", {"cmd": "pause"})

    async def async_media_stop(self):
        await self.coordinator.send("/api/music/ctrl", {"cmd": "stop"})

    async def async_media_next_track(self):
        await self.coordinator.send("/api/music/ctrl", {"cmd": "next"})

    async def async_media_previous_track(self):
        await self.coordinator.send("/api/music/ctrl", {"cmd": "prev"})
