"""Send long responses with a 'see more' button when they exceed Discord's limit."""

from __future__ import annotations

import asyncio
import contextlib

import discord

DISCORD_MAX = 2000


def _truncate(text: str, limit: int = DISCORD_MAX) -> str:
    cut = text.rfind("\n", 0, limit)
    if cut <= 0:
        cut = text.rfind(" ", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut].rstrip()


class _SeeMoreView(discord.ui.View):
    def __init__(self, full_text: str) -> None:
        super().__init__(timeout=300)
        self.full_text = full_text

    @discord.ui.button(label="see more", style=discord.ButtonStyle.secondary)
    async def see_more(
        self, interaction: discord.Interaction, button: discord.ui.Button[_SeeMoreView],
    ) -> None:
        embed = discord.Embed(description=self.full_text[:4096])
        await interaction.response.send_message(embed=embed, ephemeral=True)
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True


async def send_long(
    text: str,
    *,
    followup: discord.Webhook | None = None,
    reply_to: discord.Message | None = None,
    channel: discord.abc.Messageable | None = None,
) -> None:
    """Send *text*, attaching a 'see more' button if it exceeds 2000 chars.

    Exactly one of *followup*, *reply_to*, or *channel* must be provided.
    """
    if len(text) <= DISCORD_MAX:
        if followup:
            await followup.send(text)
        elif reply_to:
            await reply_to.reply(text, mention_author=False)
        elif channel:
            await channel.send(text)
        return

    truncated = _truncate(text)
    view = _SeeMoreView(text)

    if followup:
        msg = await followup.send(truncated, view=view, wait=True)
    elif reply_to:
        msg = await reply_to.reply(truncated, view=view, mention_author=False)
    elif channel:
        msg = await channel.send(truncated, view=view)
    else:
        return

    async def _disable_on_timeout() -> None:
        await view.wait()
        if msg is not None:
            view.children[0].disabled = True  # type: ignore[union-attr]
            with contextlib.suppress(discord.DiscordException):
                await msg.edit(view=view)

    asyncio.create_task(_disable_on_timeout())
