"""Tiny aiohttp server exposing /health for Railway's healthcheck.

Reports OK only when the bot is connected to Discord AND the DB pool is alive.
"""

from __future__ import annotations

import logging

from aiohttp import web

log = logging.getLogger(__name__)


class HealthServer:
    def __init__(self, port: int, is_healthy):  # is_healthy: () -> bool
        self.port = port
        self.is_healthy = is_healthy
        self.runner: web.AppRunner | None = None

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/health", self._handle)
        app.router.add_get("/", self._handle)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "0.0.0.0", self.port)
        await site.start()
        log.info("health server on :%d", self.port)

    async def stop(self) -> None:
        if self.runner is not None:
            await self.runner.cleanup()

    async def _handle(self, _request: web.Request) -> web.Response:
        if self.is_healthy():
            return web.Response(text="ok")
        return web.Response(text="not ready", status=503)
