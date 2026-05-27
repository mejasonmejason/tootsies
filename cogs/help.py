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
                "chicago kid, miami based, bartender at tootsies. music is the "
                "core (sample-spotter), bulls fan, A24 girlie. i answer "
                "questions, recap the chat, start fights when it's dead, and "
                "chime in when the room's cooking. mods can teach me new "
                "tricks just by asking."
            ),
            color=0x9b59b6,
        )
        embed.add_field(
            name="🗣️  everyone",
            value=(
                "**`/ask <question>`**: ask me anything. takes, recs, scores, "
                "whatever. i read the room and check the web.\n"
                "  *ex:* `/ask who are you` → *bartender at tootsies. pour you something?*\n"
                "**`@Toots <question>`**: same thing, no slash. "
                "*ayo @Toots what's the move tonight* works.\n"
                "  *ex:* `@Toots wyd` → *gaza, posted up. pour you something?*\n"
                "**`/recap period:<last hour | last 24h | today>`**: what'd "
                "you miss in here.\n"
                "**`/discourse category:<pop | sports | cinema | hiphop | nba | "
                "custom>`**: i drop a discussion starter."
            ),
            inline=False,
        )
        embed.add_field(
            name="🍸  on my own (no command, runs in the background)",
            value=(
                "**scheduled posts** in the discourse channel and **chime-ins** "
                "when y'all are debating something good. both ride on the mood "
                "dial mods set in `/menu`:\n"
                "  · **chill**: scheduled at 12pm + 7pm ET, chime in up to 10/day\n"
                "  · **yaps**: scheduled at 10am, 2pm, 6pm, 10pm ET, chime in uncapped\n"
                "  · **off**: silent on both\n"
                "i won't chime in on catch-ups, vulnerable shares, or weekend "
                "logistics. only when the room's actually cooking."
            ),
            inline=False,
        )
        embed.add_field(
            name="👮  mods only",
            value=(
                "**`/order new <feature>`**: tell me a new thing you want me "
                "to do. i'll build it. one at a time, takes a few minutes.\n"
                "  *ex:* `/order new add a /weather command` → i file an "
                "issue, claude writes the PR, CI + railway deploy. ~5 min.\n"
                "**`/order status`**: see what i'm working on.\n"
                "**`/order retry <number>`**: try again on something that "
                "didn't work.\n"
                "**`/order cancel <number>`**: call it off.\n"
                "**`/menu`**: set up my channels, mod roles, mood, where to "
                "pull news from.\n"
                "**`/close` / `/open`**: stop or restart taking `/order` "
                "requests.\n"
                "**`/undo`**: if a new feature broke me, roll me back to the "
                "version before."
            ),
            inline=False,
        )
        embed.add_field(
            name="📏  daily caps",
            value=(
                "you get **20 of `/ask` + `@Toots` + `/recap` per day each**.\n"
                "the server gets **20 `/discourse` + `/order` per day total**.\n"
                "no caps on `/menu` or `/help` or `/close` / `/open` / `/undo`."
            ),
            inline=False,
        )
        embed.set_footer(text="if i'm broken, ping a mod.")
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(cast("TootsiesBot", bot)))
