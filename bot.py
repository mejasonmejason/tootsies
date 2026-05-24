"""Tootsies bot entrypoint.

Boots Discord client, opens DB pool, exposes /health, loads cogs, syncs slash commands.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
import sys

import discord
from discord.ext import commands, tasks

from claude_client import ClaudeClient
from config import Config
from db import DB
from utils import voice
from utils.github import GitHubClient
from utils.healthcheck import HealthServer
from utils.permissions import can_send_in

INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
INTENTS.guilds = True
INTENTS.messages = True

COGS = [
    "cogs.ask",
    "cogs.recap",
    "cogs.discourse",
    "cogs.order",
    "cogs.admin",
    "cogs.settings",
]


class TootsiesBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        super().__init__(command_prefix="!", intents=INTENTS, help_command=None)
        self.config = config
        self.db = DB(config.database_url)
        self.claude = ClaudeClient(config.anthropic_api_key)
        self.gh = GitHubClient(config.github_token, config.github_repo)
        self._ready_once = False

    async def setup_hook(self) -> None:
        await self.db.connect()
        for cog in COGS:
            await self.load_extension(cog)
            log.info("loaded %s", cog)

    async def on_ready(self) -> None:
        # Sync per guild — pushes commands fast (~10s) instead of the global ~1hr propagation.
        for guild in self.guilds:
            try:
                # Cogs register commands globally. Copy them onto this guild so the per-guild
                # sync actually has something to push — gives ~10s propagation instead of the
                # ~1h Discord takes for true global commands.
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info(
                    "synced %d commands to guild %s (%d): %s",
                    len(synced), guild.name, guild.id,
                    ", ".join(f"/{c.name}" for c in synced),
                )
                await self.db.ensure_server(guild.id)
            except Exception:
                log.exception("sync failed for guild %s", guild.id)
        if not self._ready_once:
            self._ready_once = True
            self._pruner.start()
        log.info("ready as %s · %d guild(s)", self.user, len(self.guilds))

    async def on_guild_join(self, guild: discord.Guild) -> None:
        await self.db.ensure_server(guild.id)
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except Exception:
            log.exception("sync on join failed")

        me = guild.me
        if me is None:
            return
        target = guild.system_channel
        if target is None or not can_send_in(target, me):
            for ch in guild.text_channels:
                if can_send_in(ch, me):
                    target = ch
                    break
        if target is not None:
            try:
                await target.send(
                    "hey, i'm toots. a mod needs to run `/menu` to get me set up before i can do anything."
                )
            except discord.DiscordException:
                log.exception("welcome post failed")

    async def on_error(self, event_method: str, /, *args, **kwargs) -> None:  # noqa: ARG002
        log.exception("unhandled error in %s", event_method)

    async def on_app_command_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        log.exception("app command error: %s", error)
        msg = voice.pick(voice.DB_ERROR)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.DiscordException:
            log.exception("error-response send failed")

    def is_healthy(self) -> bool:
        return self.is_ready() and self.db.pool is not None

    @tasks.loop(hours=24)
    async def _pruner(self) -> None:
        try:
            await self.db.prune_audit()
            await self.db.prune_discourse()
            log.info("pruned old audit + discourse history")
        except Exception:
            log.exception("prune failed")

    @_pruner.before_loop
    async def _before_prune(self) -> None:
        await self.wait_until_ready()


async def _main() -> None:
    cfg = Config.from_env()
    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    global log
    log = logging.getLogger("tootsies")

    bot = TootsiesBot(cfg)
    health = HealthServer(cfg.health_port, bot.is_healthy)

    # SIGTERM (Railway sends this on deploy) → close gracefully.
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Windows or restricted environments don't support add_signal_handler — skip silently.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    async def runner() -> None:
        try:
            await health.start()
            await bot.start(cfg.discord_token)
        finally:
            await health.stop()
            await bot.gh.close()
            await bot.db.close()

    bot_task = asyncio.create_task(runner())
    stop_task = asyncio.create_task(stop.wait())
    done, _ = await asyncio.wait(
        {bot_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if stop_task in done:
        log.info("shutdown signal received")
        await bot.close()
        await bot_task


log = logging.getLogger("tootsies")  # replaced in _main with configured root


if __name__ == "__main__":
    asyncio.run(_main())
