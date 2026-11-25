import asyncio

import aiohttp


class StationError(Exception):
    pass


class StationApi:
    def __init__(self, session: aiohttp.ClientSession, host: str, port: int, token: str) -> None:
        self._session = session
        self._base = f"http://{host}:{port}"
        self._headers = {"X-Token": token}

    async def get(self, path: str) -> dict:
        return await self._call("GET", path, None)

    async def post(self, path: str, body: dict) -> dict:
        return await self._call("POST", path, body)

    async def _call(self, method: str, path: str, body: dict | None) -> dict:
        try:
            async with asyncio.timeout(15):
                async with self._session.request(
                    method, self._base + path, json=body, headers=self._headers
                ) as resp:
                    if resp.status == 401:
                        raise StationError("auth")
                    if resp.status >= 400:
                        raise StationError(f"http {resp.status}")
                    return await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise StationError(str(err)) from err
