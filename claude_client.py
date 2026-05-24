"""Anthropic client wrapper.

Model routing:
- HAIKU: /ask, /recap, /mood, ambient deflections — fast and cheap
- SONNET: /discourse, /order pre-flight sanity check — needs judgment

System prompt is cached (constitution + persona are stable across calls).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Any

from anthropic import AsyncAnthropic

from persona import system_prompt
from utils.events import emit

log = logging.getLogger(__name__)

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"

# Pulled into its own constant so we can change defaults in one place.
DEFAULT_MAX_TOKENS = 400


@dataclass
class ClaudeResult:
    text: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int


class ClaudeClient:
    def __init__(self, api_key: str | None = None) -> None:
        self.client = AsyncAnthropic(api_key=api_key or os.environ["ANTHROPIC_API_KEY"])

    async def _call(
        self,
        *,
        model: str,
        user_message: str,
        system_extra: str = "",
        max_tokens: int = DEFAULT_MAX_TOKENS,
        tools: list[dict[str, Any]] | None = None,
        purpose: str = "unknown",
    ) -> ClaudeResult:
        # System prompt is a list with a cache_control marker on the persona block so
        # repeat calls hit the prompt cache (the persona is ~1k tokens and stable).
        system = [
            {
                "type": "text",
                "text": system_prompt(system_extra),
                "cache_control": {"type": "ephemeral"},
            }
        ]

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_message}],
        }
        if tools:
            kwargs["tools"] = tools

        start = time.monotonic()
        ok = True
        try:
            resp = await self.client.messages.create(**kwargs)
        except Exception as exc:
            ok = False
            emit(
                "claude_api",
                model=model, purpose=purpose,
                duration_ms=int((time.monotonic() - start) * 1000),
                ok=ok, error=type(exc).__name__,
            )
            raise

        duration_ms = int((time.monotonic() - start) * 1000)
        text_parts: list[str] = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                text_parts.append(block.text)
        text = "".join(text_parts).strip()

        emit(
            "claude_api",
            model=model,
            purpose=purpose,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            duration_ms=duration_ms,
            stop_reason=resp.stop_reason,
            ok=ok,
        )

        return ClaudeResult(
            text=text,
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )

    async def ask(self, question: str, channel_context: str = "", use_web: bool = False) -> str:
        """Answer a user question in Toots voice. Used by /ask and @Toots mentions."""
        extra_context = ""
        if channel_context:
            extra_context = f"\n\nRecent channel chatter (for vibe, don't quote it back):\n{channel_context}"

        system_extra = (
            "TASK: Answer the user's question in your voice. Use channel chatter for vibe, not quotes. "
            "Open with a brief paraphrase of the question, then your answer. "
            "The answer portion is ~140 chars; the paraphrase does not count toward that cap. "
            "Skip the paraphrase only when the question is so short an echo would dwarf the answer. "
            "One link MAX, only if it actually helps."
        )
        tools = [{"type": "web_search_20250305", "name": "web_search"}] if use_web else None
        result = await self._call(
            model=HAIKU,
            user_message=f"{question}{extra_context}",
            system_extra=system_extra,
            tools=tools,
            purpose="ask",
        )
        return result.text

    async def recap(self, channel_name: str, messages_blob: str) -> str:
        """Summarize a channel's recent activity with spice."""
        system_extra = (
            "TASK: Recap the recent vibe in this channel. Weight reactions. Be spicy but kind. "
            "If it's dead, say so honestly with a quip. ~140 chars."
        )
        user = f"Channel: #{channel_name}\n\nMessages (most recent last):\n{messages_blob}"
        result = await self._call(
            model=HAIKU, user_message=user, system_extra=system_extra, purpose="recap",
        )
        return result.text

    async def discourse(
        self,
        category: str,
        sources_blob: str,
        recent_with_timestamps: str = "",
        *,
        must_post: bool = True,
    ) -> str:
        """Generate a discourse-starter post pulling from feeds + web.

        State-aware dedup: `recent_with_timestamps` lists what's been posted in the last 72h.
        Bake current state into the post (e.g. "lakers vs nuggets r2, series 1-1") so future
        dedup checks can tell when a topic has materially evolved vs. when it's the same beat.

        must_post=True  (manual /discourse): always produce a post. If the obvious topic is
                                              stale, pick a fresher angle. Never return EMPTY.
        must_post=False (scheduled mood tick): may return literal "EMPTY" to skip the slot.
        """
        dedup_clause = (
            f"\n\nRECENTLY POSTED (last 72h, with timestamps):\n{recent_with_timestamps}\n"
            "If a topic has materially evolved since the last post (new score, new news, new beef), "
            "going again is fine. If nothing's changed, "
            + (
                "pick a DIFFERENT angle or category. the user asked, you must post something."
                if must_post
                else "return EMPTY (literally the word EMPTY, nothing else) so we skip this slot."
            )
            if recent_with_timestamps
            else ""
        )
        system_extra = (
            "TASK: Pick the freshest, most talk-worthy thread from these sources and post one starter "
            "in your voice. Hot take welcome. ~140 chars, optional 1 link if it's the source.\n"
            "STATE: Bake the current state of the topic into your line so we can tell later if it's "
            "the same beat or a new one (e.g. 'lakers vs nuggets r2, series tied 1-1', not just 'lakers')."
            f"{dedup_clause}"
        )
        user = f"Category: {category}\n\nAvailable sources:\n{sources_blob}"
        result = await self._call(
            model=SONNET,
            user_message=user,
            system_extra=system_extra,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            purpose="discourse_manual" if must_post else "discourse_scheduled",
        )
        return result.text

    async def mood_post(self, recent_with_timestamps: str = "") -> str:
        """Ambient post for the scheduled mood ticker.

        Returns literal "EMPTY" if recent topics cover the field and nothing has evolved —
        the scheduler treats that as "skip this slot cleanly."
        """
        dedup_clause = (
            f"\n\nRECENTLY POSTED (last 72h):\n{recent_with_timestamps}\n"
            "If a topic listed above has materially evolved (score change, news drop, new beef), "
            "going again is fine. Otherwise pick a DIFFERENT subject. If you genuinely can't think "
            "of anything fresh that isn't a repeat, return EMPTY (literally the word EMPTY) and "
            "we'll skip this slot."
            if recent_with_timestamps
            else ""
        )
        system_extra = (
            "TASK: Drop one short conversation-starter into the chat. Pop culture, sports, music, "
            "movies, food. Your voice. ~140 chars. No question stack, one prompt.\n"
            "STATE: Bake the current state of the topic into your line (e.g. 'lakers vs nuggets r2, "
            "series 1-1', not just 'lakers')."
            f"{dedup_clause}"
        )
        user = "Anything good. Surprise me."
        result = await self._call(
            model=HAIKU, user_message=user, system_extra=system_extra,
            purpose="mood_scheduled",
        )
        return result.text

    async def deflect(self, situation: str) -> str:
        """Generate a fresh in-voice deflection. Falls back to canned variants on failure (caller's job)."""
        system_extra = (
            "TASK: One-liner deflection in your voice. Sharp, not mean. <100 chars. No emoji."
        )
        result = await self._call(
            model=HAIKU, user_message=situation, system_extra=system_extra, max_tokens=80,
            purpose="deflect",
        )
        return result.text

    async def preflight_order(self, request: str) -> tuple[str, str]:
        """Pre-flight sanity check on an /order request.

        Returns (verdict, reason) where verdict is one of:
          - "allow":    reason is a one-line summary of what to build
          - "plumbing": reason names the protected path(s) the request would touch
          - "reject":   reason is the constitution/safety violation

        Fails closed (returns "reject") if the model output is unparseable.
        """
        system_extra = (
            "TASK: You are reviewing a /order request before it goes to the build pipeline.\n"
            "Classify into ONE of three buckets:\n"
            "\n"
            "REJECT: drop the request entirely. Use when:\n"
            "  - it asks for moderation actions (kick/ban/mute/delete messages/role changes)\n"
            "  - it asks the bot to DM users or post outside this Discord\n"
            "  - it violates the constitution (NSFW, hate, doxxing, fabricated quotes, impersonation)\n"
            "  - it's incoherent or has no actionable code change\n"
            "  - it asks for medical / legal / financial advice features\n"
            "\n"
            "PLUMBING: the request would require editing a protected path. Use when it would touch:\n"
            "  - constitution.py (the constitution itself)\n"
            "  - persona.py CORE voice (the system prompt that defines Toots)\n"
            "    EXCEPTION: voice-library *additions* in utils/voice.py are FINE, that's an ALLOW.\n"
            "    Example: 'add a new quip for when someone asks toots to dance' → ALLOW.\n"
            "  - .github/ (CI/CD workflows)\n"
            "  - Dockerfile, railway.toml, Procfile\n"
            "  - db.py connection/pool setup\n"
            "    EXCEPTION: adding new tables / models / migrations is FINE → ALLOW.\n"
            "  - bot.py boot logic\n"
            "    EXCEPTION: registering a new cog is FINE → ALLOW.\n"
            "  - deleting from requirements.txt or removing required vars from .env.example\n"
            "    EXCEPTION: adding new deps / new optional vars is FINE → ALLOW.\n"
            "\n"
            "ALLOW: anything else, including '/order remove /commandname' and the exceptions above.\n"
            "\n"
            "Respond on ONE line in EXACTLY this format:\n"
            "  ALLOW: <one-line summary of what to build>\n"
            "  PLUMBING: <which protected path(s) it would touch and why>\n"
            "  REJECT: <one-line reason>"
        )
        result = await self._call(
            model=SONNET, user_message=request, system_extra=system_extra, max_tokens=250,
            purpose="order_preflight",
        )
        text = result.text.strip()
        upper = text.upper()
        if upper.startswith("ALLOW"):
            _, _, reason = text.partition(":")
            return "allow", reason.strip() or "ok"
        if upper.startswith("PLUMBING"):
            _, _, reason = text.partition(":")
            return "plumbing", reason.strip() or "protected path"
        if upper.startswith("REJECT"):
            _, _, reason = text.partition(":")
            return "reject", reason.strip() or "no reason given"
        # Unparseable — fail closed, log raw text for inspection.
        log.warning("preflight unparseable: %s", text)
        return "reject", f"unparseable preflight response: {text[:200]}"
