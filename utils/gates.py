"""Common pre-checks shared across cogs."""

from __future__ import annotations

import discord

from db import DB
from utils import voice


async def require_configured(interaction: discord.Interaction, db: DB) -> bool:
    """If the guild hasn't run /menu yet, respond with the pre-setup quip and return False."""
    if interaction.guild is None:
        await interaction.response.send_message(
            "guild-only, regular.", ephemeral=True
        )
        return False
    if not await db.is_configured(interaction.guild.id):
        await interaction.response.send_message(voice.PRE_SETUP, ephemeral=True)
        return False
    return True
