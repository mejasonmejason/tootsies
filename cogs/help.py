"""/help — what Toots does, who can run what, and the daily caps.

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
            title="what i do",
            description=(
                "i'm toots, the bot. ask me stuff, summarize chat, drop discourse "
                "starters. mods can ship new features by ordering them."
            ),
            color=0x9b59b6,
        )
        embed.add_field(
            name="🗣️  everyone",
            value=(
                "**`/ask <question>`** — i answer in my voice. i read recent chat "
                "for vibe and search the web for facts. you can also `@Toots` me "
                "in any message (same backend).\n"
                "**`/recap period:<1h | 1d | today>`** — what'd you miss in this "
                "channel.\n"
                "**`/discourse category:<pop | sports | cinema | hiphop | nba | custom>`** — "
                "i drop a discourse starter into this channel."
            ),
            inline=False,
        )
        embed.add_field(
            name="👮  mods only",
            value=(
                "**`/order new <feature>`** — file a feature request. i'll write "
                "the PR, CI runs, auto-merges if green, railway redeploys. one "
                "order at a time.\n"
                "**`/order status [filter]`** — see what's cooking.\n"
                "**`/order retry <issue#>`** — retry a failed order.\n"
                "**`/order cancel <issue#>`** — kill an in-flight order.\n"
                "**`/menu`** — channels, mod roles, feed channels. re-run anytime "
                "to see current settings.\n"
                "**`/discourse mood:<chill | yaps | off | status>`** — control "
                "the scheduled posting cadence.\n"
                "**`/close` / `/open`** — kitchen open or closed for new orders.\n"
                "**`/undo`** — roll back to the previous successful deploy."
            ),
            inline=False,
        )
        embed.add_field(
            name="📏  daily caps",
            value=(
                "**20/day per user** for `/ask` (+ @mentions) and `/recap`.\n"
                "**20/day server-wide** for `/discourse` (manual posts) and `/order`.\n"
                "`/order` also has a 15-minute per-user cooldown. mood changes are unlimited."
            ),
            inline=False,
        )
        embed.set_footer(text="see the source: github.com/mejasonmejason/tootsies")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(cast("TootsiesBot", bot)))
