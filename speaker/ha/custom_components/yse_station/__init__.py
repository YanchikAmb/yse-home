import logging
from datetime import timedelta

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID, CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import (
    area_registry as ar,
    config_validation as cv,
    device_registry as dr,
    discovery_flow,
    entity_registry as er,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .api import StationApi, StationError
from .ask import AskView
from .const import CONF_AREA, CONF_SERVER, CONF_TOKEN, DEFAULT_SERVER, DISCOVER_PORT, DOMAIN, PLATFORMS
from .coordinator import StationCoordinator
from .people import PeopleView
from .room import ClimateView, SttView

_LOGGER = logging.getLogger(__name__)
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
SAY_SCHEMA = vol.Schema({
    vol.Required("message"): vol.All(cv.string, vol.Length(min=1, max=300)),
    vol.Optional("sender", default=""): vol.All(cv.string, vol.Length(max=40)),
    vol.Optional(ATTR_DEVICE_ID, default=[]): vol.All(cv.ensure_list, [cv.string]),
})


async def say(hass: HomeAssistant, call: ServiceCall) -> None:
    """Сообщение вслух на колонках: сигнал и текст. Без устройств — на всех колонках."""
    devices = dr.async_get(hass)
    entry_ids = set()
    for device_id in call.data[ATTR_DEVICE_ID]:
        device = devices.async_get(device_id)
        if device is None:
            raise HomeAssistantError(f"нет устройства {device_id}")
        entry_ids.update(e for d, e in device.identifiers if d == DOMAIN)
    coordinators = hass.data.get(DOMAIN, {})
    targets = [c for e, c in coordinators.items() if not entry_ids or e in entry_ids]
    if not targets:
        raise HomeAssistantError("колонок yse_station нет")
    body = {"to": "here", "text": " ".join(call.data["message"].replace('"', "").replace("'", "").split()),
            "from": call.data["sender"].replace('"', "").replace("'", "")}
    failed = []
    for coordinator in targets:
        try:
            if not (await coordinator.api.post("/api/message", body)).get("ok"):
                failed.append(coordinator.name)
        except StationError:
            failed.append(coordinator.name)
    if failed:
        raise HomeAssistantError("не приняли: " + ", ".join(failed))


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    hass.http.register_view(AskView(hass))
    hass.http.register_view(ClimateView(hass))
    hass.http.register_view(SttView(hass))
    hass.http.register_view(PeopleView(hass))

    async def handle_say(call: ServiceCall) -> None:
        await say(hass, call)

    hass.services.async_register(DOMAIN, "say", handle_say, schema=SAY_SCHEMA)
    session = async_get_clientsession(hass)

    async def look(_now=None) -> None:
        servers = {e.data.get(CONF_SERVER, DEFAULT_SERVER) for e in hass.config_entries.async_entries(DOMAIN)}
        for server in (servers or {DEFAULT_SERVER}) - {""}:
            try:
                async with session.get(
                    f"http://{server}:{DISCOVER_PORT}/v1/found", timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    data = await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, ValueError):
                continue
            progress = {
                flow["context"].get("unique_id")
                for flow in hass.config_entries.flow.async_progress_by_handler(DOMAIN)
            }
            added = {entry.unique_id for entry in hass.config_entries.async_entries(DOMAIN)}
            for item in data.get("found", []):
                if item["ip"] in progress or item["ip"] in added:
                    continue
                discovery_flow.async_create_flow(
                    hass,
                    DOMAIN,
                    context={"source": SOURCE_INTEGRATION_DISCOVERY},
                    data={"ip": item["ip"], "name": item["name"], "kind": item.get("kind", "колонка"),
                          CONF_SERVER: server},
                )

    hass.async_create_background_task(look(), "yse_station discovery")
    async_track_time_interval(hass, look, timedelta(seconds=60))
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    api = StationApi(
        async_get_clientsession(hass), entry.data[CONF_HOST], entry.data[CONF_PORT], entry.data[CONF_TOKEN]
    )
    coordinator = StationCoordinator(hass, api, entry.title)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    drop_old_wake_text(hass, entry)
    place_in_area(hass, entry)
    link_page(hass, entry)
    return True


def drop_old_wake_text(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """«Слово» стало списком. Старое текстовое поле с тем же ключом убираем."""
    reg = er.async_get(hass)
    entity_id = reg.async_get_entity_id("text", DOMAIN, f"{entry.entry_id}_wake")
    if entity_id:
        reg.async_remove(entity_id)


def link_page(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Кнопка «Открыть» у устройства: страница колонки сразу с ключом (#key=…, на колонку не уходит)."""
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, entry.entry_id)})
    if device is None:
        return
    url = f"http://{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}/#key={entry.data[CONF_TOKEN]}"
    if device.configuration_url != url:
        dr.async_get(hass).async_update_device(device.id, configuration_url=url)


def place_in_area(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Комната из настройки: колонке и её спутнику Wyoming с тем же названием, если комната ещё не задана.
    По комнате колонка понимает, чью температуру называть «здесь»."""
    name = (entry.data.get(CONF_AREA) or "").strip()
    if not name:
        return
    area = ar.async_get(hass).async_get_area_by_name(name)
    if area is None:
        _LOGGER.warning("комнаты %s нет в HA", name)
        return
    devices = dr.async_get(hass)
    entries = [entry.entry_id] + [
        e.entry_id for e in hass.config_entries.async_entries("wyoming") if e.title == entry.title
    ]
    for entry_id in entries:
        for device in dr.async_entries_for_config_entry(devices, entry_id):
            if device.area_id is None:
                devices.async_update_device(device.id, area_id=area.id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return ok
