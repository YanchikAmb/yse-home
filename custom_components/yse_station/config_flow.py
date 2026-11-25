import asyncio
import logging

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import SOURCE_USER, ConfigFlow
from homeassistant.const import CONF_HOST, CONF_NAME, CONF_PORT
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import StationApi, StationError
from .const import CONF_AREA, CONF_SERVER, CONF_TOKEN, DEFAULT_PORT, DISCOVER_PORT, DOMAIN

_LOGGER = logging.getLogger(__name__)
INSTALL_MINUTES = 40


class StationConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._found: dict = {}
        self._task: asyncio.Task | None = None
        self._result: dict = {}

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(user_input[CONF_HOST])
            self._abort_if_unique_id_configured()
            api = StationApi(
                async_get_clientsession(self.hass),
                user_input[CONF_HOST],
                user_input[CONF_PORT],
                user_input[CONF_TOKEN],
            )
            try:
                await api.get("/api/ha/voice")
            except StationError as err:
                errors["base"] = "invalid_auth" if str(err) == "auth" else "cannot_connect"
            else:
                return self.async_create_entry(title=user_input[CONF_NAME], data=user_input)
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default="Колонка"): str,
                vol.Required(CONF_HOST): str,
                vol.Required(CONF_PORT, default=DEFAULT_PORT): int,
                vol.Required(CONF_TOKEN): str,
                vol.Optional(CONF_AREA, default=""): str,
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_integration_discovery(self, discovery_info):
        await self.async_set_unique_id(discovery_info["ip"])
        self._abort_if_unique_id_configured()
        self._found = dict(discovery_info)
        self.context["title_placeholders"] = {"name": self._found["name"]}
        return await self.async_step_confirm()

    def is_matching(self, other_flow: dict) -> bool:
        """Одно предложение на адрес: повторный опрос раз в минуту не плодит окна."""
        return other_flow.get("ip") == self._found.get("ip")

    async def async_step_confirm(self, user_input=None):
        if user_input is None:
            return self.async_show_form(
                step_id="confirm",
                description_placeholders={"name": self._found["name"], "ip": self._found["ip"],
                                          "kind": self._found.get("kind", "колонка")},
            )
        return await self.async_step_install()

    async def async_step_install(self, user_input=None):
        if self._task is None:
            self._task = self.hass.async_create_task(self._install())
        if not self._task.done():
            return self.async_show_progress(step_id="install", progress_action="install", progress_task=self._task)
        try:
            self._result = self._task.result()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("установка на %s не удалась: %s", self._found.get("ip"), err)
            return self.async_show_progress_done(next_step_id="failed")
        return self.async_show_progress_done(next_step_id="finish")

    async def async_step_failed(self, user_input=None):
        return self.async_abort(reason="install_failed")

    async def async_step_finish(self, user_input=None):
        result = self._result
        try:
            await self.hass.config_entries.flow.async_init(
                "wyoming",
                context={"source": SOURCE_USER},
                data={"host": result["server"], "port": int(result["sat_port"])},
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("голос колонки в Wyoming не добавился: %s", err)
        return self.async_create_entry(
            title=result["name"],
            data={
                CONF_NAME: result["name"],
                CONF_HOST: self._found["ip"],
                CONF_PORT: DEFAULT_PORT,
                CONF_TOKEN: result["token"],
                CONF_SERVER: self._found[CONF_SERVER],
            },
        )

    async def _install(self) -> dict:
        session = async_get_clientsession(self.hass)
        base = f"http://{self._found[CONF_SERVER]}:{DISCOVER_PORT}"
        timeout = aiohttp.ClientTimeout(total=20)
        async with session.post(f"{base}/v1/install", json={"ip": self._found["ip"]}, timeout=timeout) as resp:
            start = await resp.json(content_type=None)
        if start.get("state") != "running":
            raise RuntimeError(start.get("error") or "не стартовала")
        for _ in range(INSTALL_MINUTES * 12):
            await asyncio.sleep(5)
            try:
                async with session.get(f"{base}/v1/job", params={"ip": self._found["ip"]}, timeout=timeout) as resp:
                    job = await resp.json(content_type=None)
            except (aiohttp.ClientError, TimeoutError, ValueError):
                continue
            if job.get("state") == "done" and job.get("token"):
                return job
            if job.get("state") == "error":
                raise RuntimeError(job.get("step") or "ошибка")
        raise RuntimeError("слишком долго")
