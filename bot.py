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
from models import ORDER_STATUS_EMOJI, ORDER_STATUS_LABEL
from utils import bot_logs, voice
from utils.events import emit, emit_error
from utils.github import GitHubClient
from utils.healthcheck import HealthServer
from utils.link_enrich import close_session as close_link_enrich_session
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
    "cogs.help",
    "cogs.chimein",
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
        # Sync per guild, pushes commands fast (~10s) instead of the global ~1hr propagation.
        for guild in self.guilds:
            try:
                # Cogs register commands globally. Copy them onto this guild so the per-guild
                # sync actually has something to push, gives ~10s propagation instead of the
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
            await self._announce_completed_orders()
        log.info("ready as %s · %d guild(s)", self.user, len(self.guilds))
        emit("deploy_event", kind="boot", guilds=len(self.guilds))

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
        emit_error(
                source="app_command_handler", exc=error, recoverable=False,
                guild_id=interaction.guild_id, user_id=interaction.user.id,
            command=(
                interaction.command.qualified_name
                if interaction.command else None,
            ),
        )
        await bot_logs.maybe_post_db_error(
            self, self.db, interaction.guild_id, error,
            source="app_command_handler", user_id=interaction.user.id,
            verbosity=self.config.bot_logs_verbosity,
        )
        await bot_logs.maybe_post_prompt_error(
            self, self.db, interaction.guild_id, error,
            source="app_command_handler", user_id=interaction.user.id,
            verbosity=self.config.bot_logs_verbosity,
        )
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

    async def _announce_completed_orders(self) -> None:
        """Post to #bot-logs for any orders that reached a terminal state
        since the last boot. Idempotent: each order is only announced once
        (tracked via the announced_at column)."""
        try:
            orders = await self.db.unannounced_terminal_orders()
        except Exception:
            log.exception("failed to fetch unannounced orders")
            return
        for order in orders:
            emoji = ORDER_STATUS_EMOJI[order.status]
            label = ORDER_STATUS_LABEL[order.status]
            ref = f"issue #{order.issue_number}" if order.issue_number else f"order {order.id}"
            msg = f"{emoji} **{label}**: {ref} · {order.summary[:80]}"
            try:
                await bot_logs.post(
                    self, self.db, order.guild_id, msg,
                    level="milestones", verbosity=self.config.bot_logs_verbosity,
                )
                await self.db.mark_announced(order.id)
            except Exception:
                log.exception("failed to announce order %d", order.id)

    @tasks.loop(hours=24)
    async def _pruner(self) -> None:
        try:
            await self.db.prune_audit()
            await self.db.prune_discourse()
            await self.db.prune_command_metrics()
            await self.db.prune_chimein_history()
            log.info("pruned old audit + discourse + command_metrics + chimein_history")
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
        # Windows or restricted environments don't support add_signal_handler, skip silently.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    async def runner() -> None:
        try:
            await health.start()
            await bot.start(cfg.discord_token)
        finally:
            await health.stop()
            await bot.gh.close()
            await close_link_enrich_session()
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
