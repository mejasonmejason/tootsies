"""/help, what Toots does, who can run what, and the daily caps.

Available to everyone (not mod-gated). Toots-voice but lightly formal since
it's reference text. Lives in its own cog so /order can later add categories
without bloating settings.py.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from utils.metrics import track_command

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)


class Help(commands.Cog):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="what toots can do.")
    @track_command("help")
    async def help(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="hey, i'm toots",
            description=(
                "chicago kid, miami based, bartender at tootsies. music head, "
                "bulls fan, A24 girlie. mods can teach me new tricks via "
                "`/order`.\n\n"
                "🗣️ **everyone**\n"
                "`/ask <q>` or `@Toots <q>`: ask me anything\n"
                "`/recap period:`: what'd you miss\n"
                "`/discourse category:`: drop a starter\n"
                "`/music`: a track rec, right now\n\n"
                "👮 **mods only**\n"
                "`/order new|status|retry|cancel`: ship new features\n"
                "`/menu`: channels, roles, mood\n"
                "`/squad`: name the girls i'm extra warm with\n"
                "`/logs`: order status + errors channel\n"
                "`/close` `/open`: pause/resume orders\n"
                "`/undo`: roll me back\n\n"
                "🍸 **on my own**: scheduled posts + chime-ins ride the "
                "`/menu` mood (chill / yaps / off).\n\n"
                "📏 **caps**: 20/day each for `/ask` + `@Toots` + `/recap`. "
                "server gets 20/day total of `/discourse` + `/order`."
            ),
            color=0x9b59b6,
        )
        embed.set_footer(text="if i'm broken, ping a mod.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(cast("TootsiesBot", bot)))
