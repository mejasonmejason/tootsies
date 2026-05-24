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
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from anthropic import AsyncAnthropic

from persona import system_prompt
from utils.events import emit

log = logging.getLogger(__name__)

# Tootsies runs on PT-based schedules and the user base is US-leaning, so we also
# surface PT alongside UTC so Toots can talk about "tonight" / "tomorrow" sensibly.
PT = ZoneInfo("America/Los_Angeles")


def _time_context() -> str:
    """One-line current-time prefix injected into every user message.

    Claude's training cutoff means it has no idea what day it is. Without this,
    Toots will confidently call a Sunday a Tuesday, claim a game from last week
    is "tonight", etc. We pay ~25 tokens per call to fix that.
    """
    now_utc = datetime.now(UTC)
    now_pt = now_utc.astimezone(PT)
    return (
        f"[ctx — current time: {now_utc.strftime('%Y-%m-%d %H:%M')} UTC, "
        f"{now_pt.strftime('%Y-%m-%d %H:%M %Z')}, weekday: {now_utc.strftime('%A')}]\n\n"
    )

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
        image_urls: list[str] | None = None,
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

        # Build user content. Text always, plus optional image blocks for vision.
        # Time prefix keeps Toots's date/weekday references honest.
        full_text = _time_context() + user_message
        if image_urls:
            user_content: list[dict[str, Any]] | str = [{"type": "text", "text": full_text}]
            # Hard cap at 10 images. Per-image fixed overhead (~85 tokens) + variable
            # detail tokens add up fast, but we want to err on the side of seeing more
            # context to make Toots feel sharp rather than confused.
            for url in image_urls[:10]:
                cast("list[dict[str, Any]]", user_content).append({
                    "type": "image",
                    "source": {"type": "url", "url": url},
                })
        else:
            user_content = full_text

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user_content}],
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

    async def ask(
        self,
        question: str,
        channel_context: str = "",
        use_web: bool = False,
        image_urls: list[str] | None = None,
    ) -> str:
        """Answer a user question in Toots voice. Used by /ask and @Toots mentions.

        `image_urls`, if provided, gets passed to Claude as vision blocks so Toots
        can actually see images recently posted in the channel (memes, GIFs,
        screenshots being discussed). Capped to 5 internally for cost control.
        """
        extra_context = ""
        if channel_context:
            extra_context = f"\n\nRecent channel chatter (for vibe, don't quote it back):\n{channel_context}"
        if image_urls:
            extra_context += (
                f"\n\nThe last {len(image_urls)} image(s) posted in this channel are "
                "attached. Use them if the question is about one of them or if they're "
                "what the room is reacting to."
            )

        system_extra = (
            "TASK: Answer the user's question in your voice.\n"
            "\n"
            "SOURCES (in this order of trust):\n"
            "  1. Web search results, when the question is factual (artist discography, "
            "scores, news, releases, who-is-who). Use the web_search tool for ANY question "
            "that asks about real-world facts, even if you think you know the answer. "
            "Channel members get things wrong, and your training data is months stale.\n"
            "  2. Your own taste / opinion / hot take, for value judgments (best, worst, "
            "ranking, vibe checks).\n"
            "  3. Channel chatter, for VIBE-CALIBRATION ONLY (what's the room's energy, "
            "what nicknames do they use, what's the in-joke). Do NOT quote member opinions "
            "as authoritative. Do NOT take their factual claims at face value.\n"
            "\n"
            "FORMAT:\n"
            "  Open with a brief paraphrase of the question, then your answer.\n"
            "  The answer portion is ~140 chars; the paraphrase does not count toward that cap.\n"
            "  Skip the paraphrase only when the question is so short an echo would dwarf the answer.\n"
            "  One link MAX, only if it actually helps."
        )
        tools = [{"type": "web_search_20250305", "name": "web_search"}] if use_web else None
        result = await self._call(
            model=HAIKU,
            user_message=f"{question}{extra_context}",
            system_extra=system_extra,
            tools=tools,
            purpose="ask",
            image_urls=image_urls,
        )
        return result.text

    async def recap(
        self,
        channel_name: str,
        messages_blob: str,
        image_urls: list[str] | None = None,
        hot_urls: list[tuple[str, int, str]] | None = None,
    ) -> str:
        """Summarize a channel's recent activity with spice.

        Has web_search available so when the room is talking about a game / song /
        news event, the recap can fold in the actual facts instead of just naming
        what they were discussing. Optional, the model decides when to invoke.

        `image_urls` lets Toots actually see the memes/screenshots being reacted to,
        so the recap can name the joke rather than just say "everyone reacted to an
        image".

        `hot_urls` is a list of (url, reaction_count, posting_author) tuples
        surfaced from the channel content. Toots is explicitly instructed to OPEN
        those URLs via web_search rather than punt with "can't peep what's there"
        — that was the old failure mode in link-heavy channels.
        """
        hot_urls_block = ""
        if hot_urls:
            lines = [
                f"  - {url}  (posted by {author}, {rxn} reaction(s))"
                for url, rxn, author in hot_urls
            ]
            hot_urls_block = (
                "\n\nLINKS THE ROOM SHARED (open these via web_search before recapping; "
                "the higher the reaction count, the more it matters):\n" + "\n".join(lines)
            )

        system_extra = (
            "TASK: Recap the recent vibe in this channel. Weight reactions. Be spicy but kind. "
            "If it's dead, say so honestly with a quip. ~140 chars.\n"
            "\n"
            "WEB SEARCH: if the room is hyped about a specific real-world thing (a game, a "
            "release, a news event, a person), use web_search to pull the relevant fact and "
            "fold it into the recap. Example: 'room is buzzing about the lakers game' becomes "
            "'room is buzzing about the lakers losing 105-110 to denver, AD dropped 32'. "
            "Don't search for vibes or in-jokes, only for verifiable facts the room references.\n"
            "\n"
            "LINKS: when URLs are shared (see LINKS THE ROOM SHARED below if present), OPEN them "
            "by passing the URL to web_search as the query. Never tell the user 'i can't see "
            "what's at the link' — the tool is right there. If a link 404s or the content is "
            "unreachable, name what the URL points to (host, post id, etc.) and move on; don't "
            "give up the whole recap.\n"
            "\n"
            "IMAGES: if images are attached, the room may have been reacting to them. Reference "
            "what's IN the meme/screenshot when relevant, not just that an image exists."
        )
        user = (
            f"Channel: #{channel_name}\n\nMessages (most recent last):\n"
            f"{messages_blob}{hot_urls_block}"
        )
        result = await self._call(
            model=HAIKU, user_message=user, system_extra=system_extra,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            purpose="recap",
            image_urls=image_urls,
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
