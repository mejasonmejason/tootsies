"""/close /open /undo — mod-only kitchen + rollback controls."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from utils import voice
from utils.events import emit
from utils.gates import require_configured
from utils.metrics import track_command
from utils.permissions import is_mod
from utils.railway import RailwayClient, RailwayError

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)


class Admin(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot

    @app_commands.command(name="close", description="close the kitchen. no new /order requests.")
    @track_command("close")
    async def close(self, interaction: discord.Interaction) -> None:
        if not await self._mod_gate(interaction):
            return
        assert interaction.guild is not None
        await self.bot.db.set_kitchen_open(interaction.guild.id, False)
        await self.bot.db.audit(
            interaction.guild.id, interaction.user.id, "kitchen_close"
        )
        await interaction.response.send_message("kitchen closed. no more orders tonight.")

    @app_commands.command(name="open", description="open the kitchen. accept /order again.")
    @track_command("open")
    async def open(self, interaction: discord.Interaction) -> None:
        if not await self._mod_gate(interaction):
            return
        assert interaction.guild is not None
        await self.bot.db.set_kitchen_open(interaction.guild.id, True)
        await self.bot.db.audit(
            interaction.guild.id, interaction.user.id, "kitchen_open"
        )
        await interaction.response.send_message("we're open. let's go.")

    @app_commands.command(name="undo", description="roll back to the previous successful deploy.")
    @track_command("undo")
    async def undo(self, interaction: discord.Interaction) -> None:
        if not await self._mod_gate(interaction):
            return
        assert interaction.guild is not None

        cfg = self.bot.config
        if not cfg.railway_api_token:
            await interaction.response.send_message(
                "no `RAILWAY_API_TOKEN` set, can't roll back from here. "
                "do it from the railway dashboard for now.",
                ephemeral=True,
            )
            return
        if not cfg.railway_service_id:
            await interaction.response.send_message(
                "no `RAILWAY_SERVICE_ID` set. usually auto-injected on railway. "
                "if you're running off-railway, set it explicitly.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        client = RailwayClient(cfg.railway_api_token, cfg.railway_service_id)
        try:
            target, new_id = await client.rollback_to_previous()
        except RailwayError as exc:
            log.warning("railway rollback rejected: %s", exc)
            emit(
                "error", source="undo", error="RailwayError",
                guild_id=interaction.guild.id, user_id=interaction.user.id,
            )
            await self.bot.db.audit(
                interaction.guild.id, interaction.user.id, "undo_failed",
                after={"error": str(exc)[:300]},
            )
            await interaction.followup.send(f"couldn't undo: {exc}")
            return
        except Exception as exc:
            log.exception("railway rollback crashed")
            emit(
                "error", source="undo", error=type(exc).__name__,
                guild_id=interaction.guild.id, user_id=interaction.user.id,
            )
            await self.bot.db.audit(
                interaction.guild.id, interaction.user.id, "undo_failed",
                after={"error": str(exc)[:300]},
            )
            await interaction.followup.send(voice.pick(voice.DB_ERROR))
            return

        await self.bot.db.audit(
            interaction.guild.id, interaction.user.id, "undo_invoked",
            after={
                "target_deployment": target.id,
                "target_created_at": target.created_at,
                "new_deployment": new_id,
            },
        )
        await interaction.followup.send(
            f"rolling back to the deploy from `{target.created_at}` "
            f"(`{target.id[:8]}` → `{new_id[:8]}`). be back in a minute."
        )

    async def _mod_gate(self, interaction: discord.Interaction) -> bool:
        if not await require_configured(interaction, self.bot.db):
            return False
        member = interaction.user
        if not isinstance(member, discord.Member):
            await interaction.response.send_message(voice.pick(voice.PERMISSION_DENIED), ephemeral=True)
            return False
        if not await is_mod(self.bot.db, member):
            await interaction.response.send_message(voice.pick(voice.PERMISSION_DENIED), ephemeral=True)
            return False
        return True


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(cast("TootsiesBot", bot)))
