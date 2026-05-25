"""/order family, the mod-only feature pipeline.

/order <feature>            file a new feature request (pre-flight checked)
/order status [filter]      list orders
/order retry <issue#>       restart a failed order
/order cancel <issue#>      kill an in-flight order
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, cast

import discord
from discord import app_commands
from discord.ext import commands

from models import ORDER_STATUS_EMOJI, ORDER_STATUS_LABEL, TERMINAL_STATUSES, OrderStatus
from utils import bot_logs, voice
from utils.events import emit, emit_error
from utils.feeds import format_for_prompt, recent_messages
from utils.gates import require_configured
from utils.github import issue_body_for_order
from utils.metrics import track_command
from utils.permissions import is_mod
from utils.rate_limits import check_cooldown, check_server_limit, consume_server

ORDER_CONTEXT_MSG_LIMIT = 100

if TYPE_CHECKING:
    from bot import TootsiesBot

log = logging.getLogger(__name__)


class Order(commands.GroupCog, name="order"):
    def __init__(self, bot: TootsiesBot) -> None:
        self.bot = bot
        super().__init__()

    # ---- /order <feature> --------------------------------------------------------

    @app_commands.command(name="new", description="file a new feature request. (mods only)")
    @app_commands.describe(feature="what should toots learn how to do?")
    @track_command("order new")
    async def new(self, interaction: discord.Interaction, feature: str) -> None:
        if not await self._mod_gate(interaction):
            return
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        user_id = interaction.user.id

        if not await self.bot.db.is_kitchen_open(guild_id):
            await interaction.response.send_message(voice.pick(voice.KITCHEN_CLOSED), ephemeral=True)
            return

        # Pipeline-red: last order is burnt → refuse.
        red = await self.bot.db.last_failed_deploy(guild_id)
        if red is not None:
            await interaction.response.send_message(voice.pick(voice.PIPELINE_RED), ephemeral=True)
            return

        # One-at-a-time: refuse if anything is in flight.
        in_flight = await self.bot.db.in_flight_orders(guild_id)
        if in_flight:
            current = in_flight[0]
            ref = f"#{current.issue_number}" if current.issue_number else f"order {current.id}"
            await interaction.response.send_message(voice.order_in_flight(ref), ephemeral=True)
            return

        # Duplicate / cooldown / rate-limit checks all need DB; bundle them up.
        cd_ok, _ = await check_cooldown(self.bot.db, user_id, guild_id, "order")
        if not cd_ok:
            await interaction.response.send_message(
                "easy, regular. cool off a minute.", ephemeral=True
            )
            return
        srv_ok, _, _ = await check_server_limit(self.bot.db, guild_id, "order")
        if not srv_ok:
            await interaction.response.send_message(
                voice.pick(voice.RATE_LIMIT_HIT), ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)

        # Pull recent chatter from the channel where /order was invoked. For
        # behavior-complaint orders ("toots is being weird in #discourse"),
        # this lets preflight + claude-code-action see the actual evidence
        # instead of working from the mod's one-line description alone. For
        # straight "add a /weather command" orders the context is just noise
        # the model ignores. Fail-open: if we can't pull context, ship the
        # order anyway with no context.
        channel_context = ""
        me = interaction.guild.me
        ch = interaction.channel
        if me is not None and isinstance(ch, discord.TextChannel | discord.Thread):
            try:
                msgs = await recent_messages(
                    ch, me, limit=ORDER_CONTEXT_MSG_LIMIT,
                    include_bots=True,  # Toots's own posts are often the evidence
                )
                if msgs:
                    channel_context = format_for_prompt(msgs, include_reactions=True)
            except Exception:
                log.exception("order context fetch failed (continuing without context)")

        # Pre-flight sanity check via Sonnet. Three-way: allow / plumbing / reject.
        try:
            verdict, reason = await self.bot.claude.preflight_order(
                feature, channel_context=channel_context,
            )
        except Exception as exc:
            log.exception("preflight failed")
            emit_error(
                source="order_preflight", exc=exc, recoverable=False,
                guild_id=guild_id, user_id=user_id,
            )
            await bot_logs.maybe_post_db_error(
                self.bot, self.bot.db, guild_id, exc,
                source="order_preflight", user_id=user_id,
                verbosity=self.bot.config.bot_logs_verbosity,
            )
            await bot_logs.maybe_post_prompt_error(
                self.bot, self.bot.db, guild_id, exc,
                source="order_preflight", user_id=user_id,
                verbosity=self.bot.config.bot_logs_verbosity,
            )
            await interaction.followup.send(voice.pick(voice.DB_ERROR))
            return

        if verdict != "allow":
            # Plumbing = protected path; reject = constitution/safety. Different deflection,
            # same audit treatment.
            user_msg = (
                voice.pick(voice.PLUMBING_TOUCHED)
                if verdict == "plumbing"
                else voice.pick(voice.ORDER_REFUSED)
            )
            await interaction.followup.send(user_msg)
            await self.bot.db.audit(
                guild_id, user_id, f"order_{verdict}", before={"request": feature},
                after={"reason": reason},
            )
            emoji = "🔧" if verdict == "plumbing" else "🚫"
            await bot_logs.post(
                self.bot, self.bot.db, guild_id,
                f"{emoji} **{verdict.title()}**: {interaction.user.mention}'s order rejected.\n"
                f"> {feature[:200]}\n**Reason:** {reason}",
                level="milestones", verbosity=self.bot.config.bot_logs_verbosity,
            )
            emit(
                "order_state",
                guild_id=guild_id, user_id=user_id, **{"from": None, "to": verdict},
            )
            return

        # File the order, DB row first so we have the ID for the issue body.
        order = await self.bot.db.create_order(
            guild_id=guild_id, requester_id=user_id, request_text=feature, summary=reason,
        )
        title = f"[order #{order.id}] {reason[:80]}"
        body = issue_body_for_order(
            feature, reason, interaction.user.mention,
            channel_context=channel_context,
            channel_name=getattr(ch, "name", None) if ch else None,
        )

        try:
            issue = await self.bot.gh.create_issue(title, body, labels=["order", "claude"])
            issue_number = int(issue["number"])
        except Exception as exc:
            log.exception("github issue file failed")
            emit_error(
                source="order_github_create", exc=exc, recoverable=False,
                guild_id=guild_id, user_id=user_id, order_id=order.id,
            )
            await self.bot.db.update_order(order.id, status=OrderStatus.BURNT,
                                           error_log="failed to file GitHub issue")
            await interaction.followup.send(voice.pick(voice.DB_ERROR))
            return

        await self.bot.db.update_order(order.id, issue_number=issue_number)
        await consume_server(self.bot.db, guild_id, "order")
        await self.bot.db.set_cooldown(user_id, guild_id, "order")
        await self.bot.db.audit(
            guild_id, user_id, "order_filed",
            after={"order_id": order.id, "issue": issue_number, "summary": reason},
        )

        await interaction.followup.send(
            f"🟡 order #{order.id} filed. tracking: `{reason}` (issue #{issue_number})"
        )
        await bot_logs.post(
            self.bot, self.bot.db, guild_id,
            f"🟡 **Prepping**: order #{order.id} (issue #{issue_number}) from {interaction.user.mention}\n> {feature[:200]}",
            level="milestones", verbosity=self.bot.config.bot_logs_verbosity,
        )
        emit(
            "order_state",
            order_id=order.id, issue_number=issue_number,
            guild_id=guild_id, user_id=user_id,
            **{"from": None, "to": "prepping"},
        )

    # ---- /order status ----------------------------------------------------------

    @app_commands.command(name="status", description="see what's cooking. (mods only)")
    @app_commands.describe(filter="which orders to show")
    @app_commands.choices(
        filter=[
            app_commands.Choice(name="mine", value="mine"),
            app_commands.Choice(name="all (last 30d)", value="all"),
            app_commands.Choice(name="in-progress", value="in-progress"),
            app_commands.Choice(name="failed", value="failed"),
        ]
    )
    @track_command("order status")
    async def status(
        self,
        interaction: discord.Interaction,
        filter: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await self._mod_gate(interaction):
            return
        assert interaction.guild is not None
        guild_id = interaction.guild.id
        kind = filter.value if filter else "all"

        orders = await self.bot.db.recent_orders(guild_id, since_days=30, limit=50)
        if kind == "mine":
            orders = [o for o in orders if o.requester_id == interaction.user.id]
        elif kind == "in-progress":
            orders = [o for o in orders if o.status not in TERMINAL_STATUSES]
        elif kind == "failed":
            orders = [o for o in orders if o.status in {OrderStatus.BURNT, OrderStatus.SENT_BACK}]

        if not orders:
            await interaction.response.send_message("nothing on the rail.", ephemeral=True)
            return

        lines = []
        for o in orders[:15]:
            emoji = ORDER_STATUS_EMOJI[o.status]
            label = ORDER_STATUS_LABEL[o.status]
            ref = f"issue #{o.issue_number}" if o.issue_number else f"order {o.id}"
            lines.append(f"{emoji} **{label}** · {ref} · {o.summary[:60]}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ---- /order retry -----------------------------------------------------------

    @app_commands.command(name="retry", description="retry a failed order. (mods only)")
    @app_commands.describe(issue_number="github issue number")
    @track_command("order retry")
    async def retry(self, interaction: discord.Interaction, issue_number: int) -> None:
        if not await self._mod_gate(interaction):
            return
        assert interaction.guild is not None
        order = await self.bot.db.get_order_by_issue(issue_number)
        if order is None or order.guild_id != interaction.guild.id:
            await interaction.response.send_message("don't know that one.", ephemeral=True)
            return
        if order.status not in {OrderStatus.BURNT, OrderStatus.SENT_BACK}:
            await interaction.response.send_message(
                f"that one's {ORDER_STATUS_LABEL[order.status].lower()}, can't retry.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        # Cancel original on GitHub, file fresh.
        try:
            await self.bot.gh.close_issue(issue_number)
        except Exception:
            log.exception("close old issue failed")

        new_order = await self.bot.db.create_order(
            guild_id=order.guild_id,
            requester_id=interaction.user.id,
            request_text=order.request_text,
            summary=order.summary,
        )
        title = f"[order #{new_order.id}] {order.summary[:80]} (retry of #{issue_number})"
        body = issue_body_for_order(order.request_text, order.summary, interaction.user.mention)
        try:
            issue = await self.bot.gh.create_issue(title, body, labels=["order", "claude", "retry"])
            new_issue = int(issue["number"])
        except Exception:
            log.exception("file retry issue failed")
            await self.bot.db.update_order(new_order.id, status=OrderStatus.BURNT,
                                           error_log="retry filing failed")
            await interaction.followup.send(voice.pick(voice.DB_ERROR))
            return

        await self.bot.db.update_order(new_order.id, issue_number=new_issue)
        await self.bot.db.audit(
            order.guild_id, interaction.user.id, "order_retried",
            before={"old_issue": issue_number}, after={"new_issue": new_issue},
        )
        await interaction.followup.send(
            f"🟡 retried as order #{new_order.id} (issue #{new_issue})."
        )

    # ---- /order cancel ----------------------------------------------------------

    @app_commands.command(name="cancel", description="kill an in-flight order. (mods only)")
    @app_commands.describe(issue_number="github issue number")
    @track_command("order cancel")
    async def cancel(self, interaction: discord.Interaction, issue_number: int) -> None:
        if not await self._mod_gate(interaction):
            return
        assert interaction.guild is not None
        order = await self.bot.db.get_order_by_issue(issue_number)
        if order is None or order.guild_id != interaction.guild.id:
            await interaction.response.send_message("don't know that one.", ephemeral=True)
            return
        if order.status in TERMINAL_STATUSES:
            await interaction.response.send_message(
                f"already {ORDER_STATUS_LABEL[order.status].lower()}, nothing to cancel.",
                ephemeral=True,
            )
            return
        try:
            await self.bot.gh.close_issue(issue_number)
        except Exception:
            log.exception("close issue failed")
        await self.bot.db.update_order(
            order.id, status=OrderStatus.SENT_BACK, error_log="canceled by mod"
        )
        await self.bot.db.audit(
            order.guild_id, interaction.user.id, "order_canceled",
            after={"issue": issue_number},
        )
        await interaction.response.send_message(f"🚫 order #{order.id} canceled.")


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
    await bot.add_cog(Order(cast("TootsiesBot", bot)))
