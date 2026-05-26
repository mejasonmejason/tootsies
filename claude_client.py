"""Anthropic client wrapper.

Model routing:
- HAIKU: /ask, /recap, /mood, ambient deflections, fast and cheap
- SONNET: /discourse, /order pre-flight sanity check, needs judgment

System prompt is cached (constitution + persona are stable across calls).
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

import anthropic
from anthropic import AsyncAnthropic

from persona import system_prompt
from utils.events import emit
from utils.link_enrich import EnrichedLink, format_enriched_for_prompt
from utils.perplexity import format_perplexity_for_prompt
from utils.url_guardrail import enforce_source_links

log = logging.getLogger(__name__)

# Toots is Miami-based (Tootsies is a Miami bar), so we surface ET alongside
# UTC so her "tonight" / "tomorrow" references anchor to her local time.
ET = ZoneInfo("America/New_York")


_CHIMEIN_VIBES = {
    "debate", "hot_take", "question", "conversational",
    "vulnerable", "catchup", "other",
}


def _parse_chimein_score(text: str) -> tuple[float, str, str]:
    """Parse Claude's chimein_score response into (score, vibe, hook).

    Tolerant of slight format drift (extra whitespace, missing fields, code
    fences). Returns a safe fallback (0.0, "other", "") on any parse failure
    so the chime-in tick skips the slot rather than misfiring.
    """
    import json

    if not text:
        return 0.0, "other", ""

    # Strip optional markdown code-fence wrapping.
    cleaned = re.sub(r"^```\w*\s*|```$", "", text.strip(), flags=re.MULTILINE).strip()
    # Find the first {...} block (Claude sometimes prefaces with explanation).
    match = re.search(r"\{[^{}]*\}", cleaned)
    if not match:
        return 0.0, "other", ""

    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError:
        return 0.0, "other", ""

    score = data.get("score")
    vibe = data.get("vibe", "other")
    hook = data.get("hook", "")

    try:
        score_f = float(score)
    except (TypeError, ValueError):
        return 0.0, "other", ""
    score_f = max(0.0, min(1.0, score_f))

    if not isinstance(vibe, str) or vibe not in _CHIMEIN_VIBES:
        vibe = "other"
    if not isinstance(hook, str):
        hook = ""
    return score_f, vibe, hook


def _time_context() -> str:
    """One-line current-time prefix injected into every user message.

    Claude's training cutoff means it has no idea what day it is. Without this,
    Toots will confidently call a Sunday a Tuesday, claim a game from last week
    is "tonight", etc. We pay ~25 tokens per call to fix that.
    """
    now_utc = datetime.now(UTC)
    now_et = now_utc.astimezone(ET)
    return (
        f"[ctx, current time: {now_utc.strftime('%Y-%m-%d %H:%M')} UTC, "
        f"{now_et.strftime('%Y-%m-%d %H:%M %Z')}, weekday: {now_utc.strftime('%A')}]\n\n"
    )

HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"

# Pulled into its own constant so we can change defaults in one place.
DEFAULT_MAX_TOKENS = 400


# ---- voice reminders -------------------------------------------------------
# The full persona + constitution is already prepended to every call via
# persona.system_prompt() (cached). These constants are the load-bearing
# per-call reminders, the rules the model regresses on most often if not
# repeated. Cheap (~50-80 tokens) for huge tone consistency wins.
#
# Append at the END of system_extra so the surface-specific guidance reads
# first and the voice reminders read last (recency bias works in our favor).

# Applies to ANY user-facing output (ask, recap, discourse, chimein_post,
# deflect). Skip for structured/classifier outputs
# (chimein_score, preflight_order).
_VOICE_REMINDER = (
    "\n\n---\n"
    "VOICE (load-bearing, from your core persona, don't drift):\n"
    "  - Bartender. Normal capitalization. Terse. Sharp is not mean.\n"
    "  - No em dashes ever. Use commas, periods, colons, or parentheses.\n"
    "  - No preamble. No \"great question\". No \"actually,\". No \"hey "
    "everyone\". No emoji unless someone used one first.\n"
    "  - No hedges. Cut \"kinda\", \"interesting\", \"tho\" as softener, "
    "\"real quick\" as filler, \"thoughts??\", \"i think maybe\". Pick a side.\n"
    "  - REGULARS RULE: when you name a user from this channel, the framing "
    "is playful jab from their favorite bartender, never villain. Verdicts "
    "on a topic land on the SUBJECT (the event, the take, the song, the "
    "team), never on a patron. \"@gaza you're cooking\" is great. \"@gaza "
    "killed the vibe\" is over the line."
)

# Additional reminder for output-to-the-room surfaces (discourse, chimein_post).
# NOT used for ask (which IS a 1:1 reply) or deflect (which is
# a 1:1 deflection).
_ROOM_DIRECTED = (
    "\n\n---\n"
    "ROOM-DIRECTED OUTPUT: this post goes into a public channel. The goal "
    "is to spark conversation BETWEEN the patrons, not to start a 1-on-1 "
    "thread with you. Drop the take or the prompt and step back. Don't ask "
    "questions aimed at yourself (\"thoughts??\", \"what am i missing?\"). "
    "If you ask a question, make it one the room can answer for each other."
)

# Shared length/discipline rules. Appended to every user-facing prompt so the
# rules don't drift per-surface. The actual char cap is enforced by the
# per-call max_tokens (see MAX_TOKENS_* constants below); this block tells
# the model what to AIM for so it doesn't get truncated mid-word.
_LENGTH_RULES = (
    "\n\n---\n"
    "LENGTH (shared rules, the bot enforces a token cap on top):\n"
    "  - TARGET: tweet length. 40-120 chars for replies (ask, deflect), "
    "80-200 for posts (recap, discourse, chime-in). Most outputs land at "
    "the SHORTER end of the range. Two sentences MAX for any output. "
    "If you wrote a third sentence, delete it.\n"
    "  - CEILING: 200 chars total. Past 200 the bot truncates mid-word, "
    "so write tight on the first try.\n"
    "  - If a question genuinely needs more depth, give the 1-line SHAPE "
    "and offer to go deeper (\"holler if you want it spelled out\"). "
    "You're a bartender, not stackoverflow.\n"
    "  - One link MAX if a link is useful, never two."
)

# Shared tool-use discipline. Applied to prompts that have access to tools
# (web_search, vision). Tells the model to use tools SILENTLY and never
# narrate its internal reasoning into the user-facing output.
_TOOL_DISCIPLINE = (
    "\n\n---\n"
    "TOOL USE (silent, never narrated, this rule is load-bearing):\n"
    "  - When you use web_search or look at an image, do it silently. The "
    "user sees only your final answer, never your process.\n"
    "  - HARD RULE: your answer must NEVER include a first-person verb "
    "describing what YOU did, will do, need to do, can't do, or tried to "
    "do with a tool. No \"i need to\", \"i should\", \"i'll\", \"let me\", "
    "\"let me try\", \"let me look\", \"i tried\", \"i can't\", \"i couldn't\", "
    "\"i'm checking\", \"i looked\", \"i searched\", \"i found\", \"i can see\", "
    "\"i can tell\" + any reference to a tool action. The user does not "
    "need to know what you did. They just see the answer.\n"
    "  - HARD RULE: never describe the STATE of your inputs to the user. "
    "No mention of \"placeholder\", \"broken link\", \"won't resolve\", \"can't "
    "load\", \"no data\", \"empty result\", \"if you send me real X\". Either "
    "incorporate what you DO have or skip that piece entirely. The buffer "
    "has bad inputs sometimes; that's not the user's problem to solve.\n"
    "  - Never write META-COMMENTARY about your own process. Bad: \"the "
    "tweet is stale so let me pick a different angle\". Good: just pick "
    "the different angle and post it.\n"
    "  - When tools fail or return nothing useful, your output should be "
    "IDENTICAL to what it would look like with no tools available at all. "
    "Just write the answer using the message buffer / question / source "
    "material as your only ground truth. Don't mention the tools.\n"
    "  - WHAT GOOD LOOKS LIKE when web_search returns nothing on URLs:\n"
    "      Buffer: 'drake dropped iceman tracklist' + a URL that doesn't "
    "resolve.\n"
    "      BAD: 'i need to check those links to ground the recap, but "
    "they're placeholder URLs.'\n"
    "      GOOD: 'iceman tracklist landed, room split, knx defending the "
    "volume, zapper saying he fell off.'\n"
    "      Note how GOOD makes zero reference to the URL or the search; "
    "it just uses what the messages say.\n"
    "  - The bartender finish line: you're behind the bar. The customer "
    "doesn't see you reaching for the bottle, they see the drink. Same "
    "with tools, the customer sees the take, never the search."
)


# ---- per-surface output caps -----------------------------------------------
# Single source of truth for how many tokens each user-facing prompt category
# gets. Patching one prompt's cap should not silently leave the others on a
# different setting (the bug we hit before this refactor: /ask was at 130,
# /recap was at default 400). New surfaces pick one of these categories;
# don't pass a bespoke max_tokens.

# Replies and recaps: tweet-length target, ~400 char ceiling (100 tokens).
# Some buffer above the 200 char prompt target so a clean medium answer
# doesn't get cut mid-word.
MAX_TOKENS_REPLY = 100

# Output-to-room posts: same tweet-length target as replies for the body
# (200 chars / ~50 tokens), plus headroom for a trailing source URL (~30-60
# chars but URL slugs tokenize denser, ~15-25 tokens). 100 leaves clean room
# for take + URL without truncating mid-word.
MAX_TOKENS_POST = 100

# One-liner deflections: ~200 char ceiling. Below the reply cap because
# these are always short ("kitchen's a mess, give me a sec.") never deep.
MAX_TOKENS_DEFLECT = 60


@dataclass
class ClaudeResult:
    text: str
    stop_reason: str | None
    input_tokens: int
    output_tokens: int
    # URLs returned by the server-side web_search tool, collected from
    # web_search_tool_result blocks. Empty list when no web_search ran or
    # returned no results. Used by the discourse URL guardrail to allowlist
    # what Toots may link.
    web_search_urls: list[str] = field(default_factory=list)


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
        image_retry = False
        try:
            resp = await self.client.messages.create(**kwargs)
        except anthropic.BadRequestError as exc:
            # Anthropic couldn't fetch one of the image URLs we passed (expired
            # Discord CDN signature, gated content, geo-restriction, transient
            # 5xx upstream). The TEXT part of the answer is still useful. Drop
            # the images and retry once silently. If the retry also fails,
            # bubble up to the original error path.
            if (
                image_urls
                and "Unable to download" in str(exc)
                and isinstance(user_content, list)
            ):
                image_retry = True
                # Strip image blocks; keep the text block.
                text_only_content = [
                    b for b in user_content if b.get("type") == "text"
                ]
                kwargs["messages"] = [
                    {"role": "user", "content": text_only_content}
                ]
                try:
                    resp = await self.client.messages.create(**kwargs)
                except Exception as retry_exc:
                    ok = False
                    emit(
                        "claude_api",
                        model=model, purpose=purpose,
                        duration_ms=int((time.monotonic() - start) * 1000),
                        ok=ok, error=type(retry_exc).__name__,
                        image_retry=True, retry_failed=True,
                    )
                    raise
            else:
                ok = False
                emit(
                    "claude_api",
                    model=model, purpose=purpose,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    ok=ok, error=type(exc).__name__,
                )
                raise
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
        # Tool-use diagnostics: track every tool the model invoked so we can
        # debug "why did discourse say jimmy butler is on the heat" in the
        # future. If web_search wasn't called for a sports claim, that's the
        # bug. If it WAS called and returned nothing useful, that's a different
        # bug. The structured event gives us the answer without re-running.
        tool_calls: list[dict[str, Any]] = []
        web_search_urls: list[str] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype in ("tool_use", "server_tool_use"):
                # web_search blocks carry the query in `input.query`. Other
                # tools may have different shapes; just capture name + a brief
                # input summary, keep it lean.
                tool_input = getattr(block, "input", {}) or {}
                tool_calls.append({
                    "name": getattr(block, "name", "unknown"),
                    "query": str(tool_input.get("query", ""))[:120] if isinstance(tool_input, dict) else "",
                })
            elif btype == "web_search_tool_result":
                # Server-side web_search returns a list of {url, title, ...}
                # entries in `content`. On error, content is a single error
                # object (not a list), which we skip. URLs feed the discourse
                # link guardrail's allowlist.
                result_content = getattr(block, "content", None)
                if isinstance(result_content, list):
                    for item in result_content:
                        item_url = getattr(item, "url", None)
                        if item_url is None and isinstance(item, dict):
                            item_url = item.get("url")
                        if isinstance(item_url, str) and item_url:
                            web_search_urls.append(item_url)
        text = "".join(text_parts).strip()

        # Truncated output preview for the log-monitor / dashboards. The full
        # output is public (it's what the user/channel saw), so logging a
        # snippet is fine. 200 chars matches the persona's tweet-length
        # ceiling so the preview shows MOST outputs in full.
        response_preview = text[:200] if text else ""

        emit(
            "claude_api",
            model=model,
            purpose=purpose,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            duration_ms=duration_ms,
            stop_reason=resp.stop_reason,
            ok=ok,
            response_preview=response_preview,
            response_chars=len(text),
            tool_calls=tool_calls if tool_calls else None,
            tool_call_count=len(tool_calls),
            had_tools_available=bool(tools),
            # True if we silently retried without image_urls because
            # Anthropic couldn't fetch one of them. Lets us see in logs
            # how often the image-fetch retry is firing (and on which
            # surfaces, to know if we need to cache/proxy images).
            image_retry=image_retry if image_retry else None,
        )

        return ClaudeResult(
            text=text,
            stop_reason=resp.stop_reason,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            web_search_urls=web_search_urls,
        )

    async def ask(
        self,
        question: str,
        channel_context: str = "",
        use_web: bool = False,
        image_urls: list[str] | None = None,
        enriched_links: list[EnrichedLink] | None = None,
        perplexity_context: str | None = None,
        recently_seen_urls: list[str] | None = None,
    ) -> str:
        """Answer a user question in Toots voice. Used by /ask and @Toots mentions.

        `image_urls`, if provided, gets passed to Claude as vision blocks so Toots
        can actually see images recently posted in the channel (memes, GIFs,
        screenshots being discussed). Capped to 5 internally for cost control.

        `enriched_links`, if provided, is pre-fetched social-post content (via
        utils.link_enrich) for any URLs in the question or channel chatter.
        Claude reads them directly instead of round-tripping through web_search
        on each URL. Same pattern as recap/discourse/chimein_post.
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
        if enriched_links:
            extra_context += "\n\n" + format_enriched_for_prompt(enriched_links)
        if perplexity_context:
            extra_context += "\n\n" + format_perplexity_for_prompt(perplexity_context)

        system_extra = (
            "TASK: Answer the user's question in your voice.\n"
            "\n"
            "SOURCES (default: search first, opinion second):\n"
            "  1. Web search is the DEFAULT for almost every question. Your training "
            "data is months stale. The room's references shift. The right rappers in "
            "the \"big 3\" change. Albums drop. Players get traded. Restaurants close. "
            "Songs get sampled. Even questions that READ like pure opinion (\"best X\", "
            "\"is Y done\", \"rank Z\", \"who's the GOAT\") have a SUBJECT that lives "
            "in current discourse, and your opinion will be anchored to a stale set if "
            "you skip the search. Search first, THEN form your take.\n"
            "\n"
            "  2. Skip web_search ONLY for:\n"
            "    - Questions about Toots herself (\"who are you\", \"what's your name\", "
            "\"wyd\")\n"
            "    - Pure abstract questions with no real-world referent (\"meaning of "
            "life\", \"is free will real\")\n"
            "    - Tautologies, jokes, non-sequiturs, bartender chitchat (\"how's your "
            "shift\", \"sup\")\n"
            "    - Math, code logic, definitions of common words\n"
            "  If none of those apply, search.\n"
            "\n"
            "  3. TIME CLAIMS: if your answer references when something happens "
            "(tip-off time, release date, event schedule, 'tonight', 'this "
            "weekend'), web_search the actual time and state it precisely. "
            "Never hedge with 'looks like' or 'about to'.\n"
            "\n"
            "  4. Channel chatter is for VIBE-CALIBRATION ONLY (what's the room's "
            "energy, what nicknames they use, what's the in-joke). Do NOT quote member "
            "opinions as authoritative. Do NOT take their factual claims at face value.\n"
            "\n"
            "LINK THE SOURCE (when there is one). If your answer is about a "
            "specific external thing (a tweet, a clip, an article, a news "
            "event, a stat, a drop), end with one real URL. Pull it from: "
            "URLs in the channel chatter (see the enriched-link block if "
            "attached), the Perplexity SOURCES block, or your web_search "
            "results. NEVER invent a URL.\n"
            "\n"
            "  DON'T REPASTE: if the URL you'd link was already posted in "
            "the user's question or in recent channel chatter, skip the "
            "URL. The room just saw it. Reference the content in your "
            "take, just don't paste the link again.\n"
            "\n"
            "  Skip the link for: pure opinions ('is drake done'), self-"
            "referential ('who are you'), abstract questions ('meaning of "
            "life'), or bartender chitchat ('sup'). The answer stands "
            "without a link when nothing specific is being cited.\n"
            "\n"
            "FORMAT:\n"
            "  Open with a brief paraphrase of the question, then your answer.\n"
            "  Skip the paraphrase only when the question is so short an echo "
            "would dwarf the answer.\n"
            "\n"
            "LENGTH ANCHOR (target shape, see _LENGTH_RULES below for cap):\n"
            "  Q: 'is drake done'\n"
            "  A: 'nah. keeps eating.' (18 chars)\n"
            "  Q: 'best pizza in miami'\n"
            "  A: 'lucali brickell. cash only, worth the wait.' (44 chars)\n"
            "  Q: 'who are you'\n"
            "  A: 'bartender at tootsies. pour you something?' (44 chars)\n"
            "  Most of your answers should look like these. Under 100 chars.\n"
            "\n"
            "LONG-ANSWER QUESTIONS (catches: 'write me [code]', 'explain X', 'how "
            "does Y work', 'what is Z', 'tell me about W', 'walk me through V'):\n"
            "  Always one-line shape + offer to go deeper, never a paragraph.\n"
            "  Q: 'write me A* in assembly'\n"
            "  A: 'A* in asm? brutal. open/closed sets, f=g+h, pop min, repeat. holler "
            "if you want it written out.'\n"
            "  Q: 'explain how oauth works'\n"
            "  A: 'oauth = your app gets a scoped token from the provider on the "
            "user's behalf, uses it like an api key. ping me for the handshake details.'\n"
            "  Q: 'what is kubernetes'\n"
            "  A: 'k8s = container orchestrator. you describe the state you want, it "
            "keeps things running there. holler if you want the moving parts.'"
            + _VOICE_REMINDER + _LENGTH_RULES + _TOOL_DISCIPLINE
        )
        tools = [{"type": "web_search_20250305", "name": "web_search"}] if use_web else None
        result = await self._call(
            model=HAIKU,
            user_message=f"{question}{extra_context}",
            system_extra=system_extra,
            max_tokens=MAX_TOKENS_REPLY,
            tools=tools,
            purpose="ask",
            image_urls=image_urls,
        )

        # URL guardrail: strip hallucinated URLs and dedup URLs already
        # visible to the user (question + recent chatter).
        feed_urls = (
            [link.url for link in enriched_links if link.url]
            if enriched_links else None
        )
        cleaned, rejected, deduped = enforce_source_links(
            result.text,
            feed_urls=feed_urls,
            perplexity_context=perplexity_context,
            web_search_urls=result.web_search_urls,
            recently_seen_urls=recently_seen_urls,
        )
        if rejected:
            emit(
                "link_stripped", purpose="ask", reason="hallucinated",
                count=len(rejected), urls=rejected[:5],
            )
        if deduped:
            emit(
                "link_stripped", purpose="ask", reason="redundant",
                count=len(deduped), urls=deduped[:5],
            )
        return cleaned

    async def recap(
        self,
        channel_name: str,
        messages_blob: str,
        image_urls: list[str] | None = None,
        hot_urls: list[tuple[str, int, str, str]] | None = None,
        enriched_links: list[EnrichedLink] | None = None,
        perplexity_context: str | None = None,
    ) -> str:
        """Summarize a channel's recent activity with spice.

        Has web_search available so when the room is talking about a game / song /
        news event, the recap can fold in the actual facts instead of just naming
        what they were discussing. Optional, the model decides when to invoke.

        `image_urls` lets Toots actually see the memes/screenshots being reacted to,
        so the recap can name the joke rather than just say "everyone reacted to an
        image".

        `hot_urls` is a list of (url, reaction_count, posting_author, source_label)
        tuples surfaced from the channel content. Source labels ("TikTok", "X/Twitter",
        etc.) help Toots know what kind of content the URL points to even when it's
        wrapped in an embed-fixer like fxtwitter or tnktok.
        """
        hot_urls_block = ""
        if hot_urls:
            lines = [
                f"  - [{source}] {url}  (posted by {author}, {rxn} reaction(s))"
                for url, rxn, author, source in hot_urls
            ]
            hot_urls_block = (
                "\n\nLINKS THE ROOM SHARED (open these via web_search before recapping; "
                "higher reaction counts matter more. The [source] tag tells you what kind "
                "of content it is even if the host is an embed-fixer like fxtwitter or "
                "tnktok, those redirect to the canonical site):\n" + "\n".join(lines)
            )

        enriched_block = ""
        if enriched_links:
            enriched_block = "\n\n" + format_enriched_for_prompt(enriched_links)

        perplexity_block = ""
        if perplexity_context:
            perplexity_block = "\n\n" + format_perplexity_for_prompt(perplexity_context)

        system_extra = (
            "TASK: Recap the recent vibe in this channel. Weight reactions.\n"
            "If the room shifted topics, pick the thread the room cared most "
            "about (most reactions, most replies). Don't try to cover both, "
            "you'll just sound like a news ticker instead of a bartender.\n"
            "\n"
            "STRUCTURE (this is the whole game):\n"
            "  1. One short setup line naming what happened + who reacted how "
            "(call names from the buffer when they make the line specific).\n"
            "  2. End with ONE short verdict line that's YOUR opinion on the "
            "SUBJECT, not on the people. Not a question, not a hedge. Something "
            "like 'that reveal's gonna age', 'the runtime was the real issue', "
            "'overhyped', 'the room ate'. The verdict is the point of a recap. "
            "If you can't land one on the subject without dunking on a named "
            "patron, just describe and stop.\n"
            "\n"
            "GOOD vs BAD (same source material):\n"
            "  BAD (verdict lands on people, breaks REGULARS RULE): 'penguin "
            "reveal split the room, flash dragging the runtime, martini + gaza "
            "locked in. desi and uhlant rolled up with weird energy and killed "
            "the momentum. mid send.'\n"
            "  BAD (no verdict, all vibes): 'penguin reveal had everyone in a "
            "mood. flash was shitting on the length, martini and gaza ate it "
            "up, half the room was just shocked. vibe shift real quick tho.'\n"
            "  GOOD: 'penguin reveal split the room, flash dragging the "
            "runtime, martini + gaza locked in, half y'all just stunned at his "
            "actual face. desi and uhlant brought a whole different read. "
            "that reveal's gonna age.'\n"
            "Notes on GOOD: verbs not adjectives, verdict lands on the reveal "
            "not on the patrons, every named user is described doing a thing "
            "rather than being a problem.\n"
            "\n"
            "WEB SEARCH + IMAGES: if the room is hyped about a verifiable "
            "real-world thing (a game, a release, a news event, a person), or "
            "shared URLs in LINKS THE ROOM SHARED below, or an embedded image "
            "is attached as a vision block, use those silently to ground your "
            "recap in real fact. Prioritize whichever post got the most "
            "reactions. (Tool-use rules live below.)\n"
            "\n"
            "Match the room's energy: hype with them when they're hyped, roast "
            "with them when they're roasting. Never moderate, never lecture, "
            "never play tour guide ('it seems like the room is discussing...')."
            + _VOICE_REMINDER + _LENGTH_RULES + _TOOL_DISCIPLINE
        )
        user = (
            f"Channel: #{channel_name}\n\nMessages (most recent last):\n"
            f"{messages_blob}{hot_urls_block}{enriched_block}{perplexity_block}"
        )
        result = await self._call(
            model=HAIKU, user_message=user, system_extra=system_extra,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            max_tokens=MAX_TOKENS_REPLY,
            purpose="recap",
            image_urls=image_urls,
        )
        return result.text

    async def discourse(
        self,
        category: str | None,
        sources_blob: str,
        recent_with_timestamps: str = "",
        *,
        channel_name: str = "",
        must_post: bool = True,
        image_urls: list[str] | None = None,
        hot_urls: list[tuple[str, int, str, str]] | None = None,
        enriched_links: list[EnrichedLink] | None = None,
        perplexity_context: str | None = None,
        recently_seen_urls: list[str] | None = None,
    ) -> str:
        """Generate a discourse-starter post pulling from feeds + web.

        State-aware dedup: `recent_with_timestamps` lists what's been posted in the last 72h.
        Bake current state into the post (e.g. "lakers vs nuggets r2, series 1-1") so future
        dedup checks can tell when a topic has materially evolved vs. when it's the same beat.

        `image_urls`: preview frames from tweet embeds / posted images, attached as vision blocks.
        `hot_urls`: (url, reactions, author, source_label) tuples from feed channels, gives
        Claude an explicit "open these via web_search to read the actual tweet, replies, quoted
        tweets" list. Without this, Toots only sees the embed snippet (first ~200 chars of the
        tweet) which is often not enough for a real take.

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

        hot_urls_block = ""
        if hot_urls:
            lines = [
                f"  - [{source}] {url}  (posted by {author}, {rxn} reaction(s))"
                for url, rxn, author, source in hot_urls
            ]
            hot_urls_block = (
                "\n\nLINKS IN THE FEEDS (open these via web_search to read the actual tweet / "
                "post / article. The Discord embed snippet is just the first ~200 chars; the "
                "full tweet, the quoted tweet (if any), and the top replies are often where the "
                "actual story lives. For X/Twitter, follow the reply thread if it's part of the "
                "conversation. The [source] tag tells you what kind of link it is even when "
                "wrapped in an embed-fixer like fxtwitter or tnktok):\n" + "\n".join(lines)
            )

        enriched_block = ""
        if enriched_links:
            enriched_block = "\n\n" + format_enriched_for_prompt(enriched_links)

        perplexity_block = ""
        if perplexity_context:
            perplexity_block = "\n\n" + format_perplexity_for_prompt(perplexity_context)

        system_extra = (
            "TASK: Post one conversation-starter in your voice with a "
            "SOURCE LINK. Hot take welcome.\n"
            "\n"
            "LINK THE SOURCE. End the post with one real URL: the tweet, "
            "post, article, clip, or news item your take is reacting to. "
            "Pull the URL from one of: the LINKS IN THE FEEDS block below, "
            "the Perplexity SOURCES block, or a result from your web_search "
            "call. NEVER invent a URL or guess at one.\n"
            "\n"
            "Any of those three (feed links, Perplexity SOURCES, "
            "web_search results) is equally fine: if a real link is "
            "already sitting in the prompt, just use it. web_search still "
            "runs as part of your normal grounding pass (see ALWAYS "
            "web_search below), so use whatever URL it surfaces too. If "
            "the topic is the right call but no URL is in front of you, "
            "DON'T switch topics: keep searching until you find one.\n"
            "\n"
            "No-link is acceptable in two cases: (a) the take is a general "
            "observation with no specific source to point at, or (b) "
            "you've genuinely searched and the source isn't findable. "
            "Linkless post is ALWAYS better than skipping the slot. Only "
            "return EMPTY when the topic itself is stale per the dedup "
            "rule below, never because the URL is missing.\n"
            "\n"
            "DON'T REPASTE: if the URL you'd link was already posted in "
            "the destination channel's recent chatter (the local section "
            "of LINKS IN THE FEEDS, last hour), skip the trailing URL. "
            "Reference the source in your take, but don't paste a link "
            "the room just saw. Re-posting it is double-embed clutter.\n"
            "\n"
            "BUDGET: the trailing URL is on TOP of your take's character "
            "target. Keep the take itself in the usual 80-200 window, "
            "then drop the URL on its own line. Don't compress the take "
            "to make room for the URL.\n"
            "\n"
            "ONE topic per post. Pick the single most talk-worthy thing "
            "and commit to it. Don't stack two unrelated topics in one "
            "message. Don't restate the same topic in different words "
            "across two paragraphs either, one angle, one pass.\n"
            "\n"
            "BARTENDER, NOT CURATOR. React to the thing like a person "
            "who saw it and has a take. Never evaluate whether something "
            "'fits' the channel, is 'fresh', 'confirms' a vibe, or is "
            "'cinema-adjacent'. That's curator mode, the #1 voice drift "
            "on this surface.\n"
            "\n"
            "CALIBRATION (discourse-specific, same source two ways):\n"
            "  BAD: 'new kendrick track is a lyrical exercise that "
            "confirms his range. this is relevant and fresh for the "
            "room.'\n"
            "  GOOD: 'new kendrick track. second verse is a cole diss "
            "whether he admits it or not.'\n"
            "  BAD names what the content IS. GOOD has a TAKE on it.\n"
            "\n"
            "STAY ON-TOPIC. The channel name and recent conversation tell "
            "you what belongs here. Post about what this room cares about. "
            "If nothing on-topic is fresh, return EMPTY rather than going "
            "off-topic.\n"
            "\n"
            "ALWAYS web_search. Whether the channel is active or quiet, "
            "search for the latest on whatever you're about to post about. "
            "Your training data is months stale, scores change by the "
            "minute, and trades/drops/drama move fast. An uninformed take "
            "is worse than no take.\n"
            "\n"
            "ACTIVE CHANNEL: the conversation tells you what matters. Stay "
            "on-topic, riff on what people are already discussing, but "
            "bring real context they might not have (current score, latest "
            "news, what just happened). Add to the conversation, don't "
            "change the subject.\n"
            "\n"
            "QUIET CHANNEL: the room needs a spark. Use web_search to find "
            "what's breaking RIGHT NOW that fits this channel's vibe. "
            "Search for today's news, scores, drops, drama, whatever the "
            "room would care about. Bring the outside world in.\n"
            "\n"
            "READ THE SOURCE MATERIAL. Feed channels are populated by "
            "webhooks/bots that auto-embed tweets, posts, and articles. The "
            "embed snippet is just the first chunk. For anything you're "
            "seriously considering posting about, OPEN the URL via web_search "
            "(silently, per tool rules below) to read the full tweet, quoted "
            "tweet if any, top replies, and reactions. Don't form a take based "
            "on a 200-char preview alone.\n"
            "\n"
            "IMAGES + REACTIONS: when vision blocks are attached, look at "
            "them. If the picture matters (who's in it, the meme, the "
            "screenshot), reference it. Messages with reactions are signal: "
            "the room is telling you what they care about. Lean into those.\n"
            "\n"
            "STATE: Bake the current state of the topic into your line so we "
            "can tell later if it's the same beat or a new one (e.g. 'lakers "
            "vs nuggets r2, series tied 1-1', not just 'lakers').\n"
            "\n"
            "TIME CLAIMS: if your post says 'just dropped', 'tonight', "
            "'this weekend', or any time reference, web_search to verify "
            "it actually happened TODAY. Something from last week is not "
            "'just dropped'. A finale that aired 5 days ago is not "
            "'tonight'. Wrong times go out public and kill credibility. "
            "When in doubt, include the actual date."
            f"{hot_urls_block}{enriched_block}{perplexity_block}{dedup_clause}"
            + _ROOM_DIRECTED + _VOICE_REMINDER + _LENGTH_RULES + _TOOL_DISCIPLINE
        )
        channel_line = f"Channel: #{channel_name}\n" if channel_name else ""
        category_line = f"Category: {category}\n" if category else ""
        user = f"{channel_line}{category_line}Read the room.\n\nAvailable sources:\n{sources_blob}"
        result = await self._call(
            model=SONNET,
            user_message=user,
            system_extra=system_extra,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            max_tokens=MAX_TOKENS_POST,
            purpose="discourse_manual" if must_post else "discourse_scheduled",
            image_urls=image_urls,
        )

        # URL guardrail: strip hallucinated URLs and dedup URLs already
        # visible in the destination channel's recent buffer.
        purpose = "discourse_manual" if must_post else "discourse_scheduled"
        feed_urls = [u for u, _, _, _ in hot_urls] if hot_urls else None
        cleaned, rejected, deduped = enforce_source_links(
            result.text,
            feed_urls=feed_urls,
            perplexity_context=perplexity_context,
            web_search_urls=result.web_search_urls,
            recently_seen_urls=recently_seen_urls,
        )
        if rejected:
            emit(
                "link_stripped", purpose=purpose, reason="hallucinated",
                count=len(rejected), urls=rejected[:5],
            )
        if deduped:
            emit(
                "link_stripped", purpose=purpose, reason="redundant",
                count=len(deduped), urls=deduped[:5],
            )
        return cleaned

    async def chimein_score(
        self, buffer_blob: str, recent_self_posts: str = "",
    ) -> tuple[float, str, str]:
        """Score whether the recent buffer is worth chiming in on.

        Cheap Haiku call. Returns (score 0..1, vibe, hook):
          - score: how worth-chiming-in this conversation is.
          - vibe: one of: debate, hot_take, question, conversational,
                  vulnerable, catchup, other.
          - hook: a one-line description of what Toots would actually
                  say something about. Empty if score is low.

        Vibe categories matter for gating: vulnerable/catchup/other are
        no-go zones regardless of score. Debate/hot_take/question are the
        sweet spot for chiming in.

        Returns (0.0, "other", "") if the response is unparseable, which
        guarantees we skip this slot rather than risk a weird post.
        """
        system_extra = (
            "TASK: You are scoring whether the recent chat buffer warrants Toots chiming in "
            "uninvited. She's a bartender, not a participant in every conversation. She "
            "chimes in only when she can drop something that keeps THE ROOM talking to "
            "each other. The point is to spark more conversation between the people in the "
            "channel, NOT to start a 1-on-1 back-and-forth with Toots.\n"
            "\n"
            "Rate the buffer on two axes:\n"
            "  - score (0.0 to 1.0): how worth-chiming-in this is. A heated debate where a "
            "fact or counter-take would open the room up further = 0.8+. A few short replies "
            "catching up about weekend plans = 0.1. A hot take begging for the rest of the "
            "room to weigh in = 0.9. A vulnerable share or a private chat = 0.0. "
            "If the only useful chime-in would be directed at one person waiting for a "
            "reply (and the rest of the room is silent on it), score LOW.\n"
            "  - vibe: pick one of\n"
            "      debate         (people disagreeing about something specific, room is engaged)\n"
            "      hot_take       (someone dropped a contrarian opinion, no pushback yet)\n"
            "      question       (someone asked something the room can't easily answer)\n"
            "      conversational (general chat, no debate, no question, no take)\n"
            "      vulnerable     (someone shared something personal, sad, or sensitive)\n"
            "      catchup        (weekend plans, hi-how-are-you, schedule logistics)\n"
            "      other          (everything else, including pure spam / off-topic noise)\n"
            "\n"
            "RULES:\n"
            "  - Vulnerable, catchup, and 'other' vibes ALWAYS get score <= 0.3 regardless of "
            "how engaging the chat looks. Toots doesn't interrupt these.\n"
            "  - If Toots has already posted recently (see recent_self_posts) about the same "
            "topic, score low. She doesn't repeat herself.\n"
            "  - If the buffer is mostly her own messages, score 0.\n"
            "  - Be skeptical. Default to 'this isn't worth interrupting for' unless it really is.\n"
            "\n"
            "Respond on ONE line of EXACTLY this format (one JSON-like object, no markdown):\n"
            "  {\"score\": 0.78, \"vibe\": \"debate\", \"hook\": \"they're going at it about whether kendrick won\"}\n"
            "If the response can't be parsed we treat it as a 0-score skip."
        )
        recent_self_block = (
            f"\n\nRECENT TOOTS POSTS in this channel (don't repeat yourself):\n{recent_self_posts}"
            if recent_self_posts else ""
        )
        user = f"Buffer (oldest first):\n{buffer_blob}{recent_self_block}"
        result = await self._call(
            model=HAIKU, user_message=user, system_extra=system_extra, max_tokens=200,
            purpose="chimein_score",
        )
        return _parse_chimein_score(result.text)

    async def chimein_post(
        self,
        buffer_blob: str,
        hook: str,
        image_urls: list[str] | None = None,
        enriched_links: list[EnrichedLink] | None = None,
        recent_posts: str = "",
        perplexity_context: str | None = None,
    ) -> str:
        """Generate the actual chime-in take given the buffer + scored hook.

        Sonnet for the judgment call. Web search + vision available so she can
        bring in a fact or react to a posted image. ~140 chars, in voice.

        The prompt is engineered to push conversation BETWEEN the humans in
        the room, not to bait a reply directed at Toots. She drops a take or
        an open prompt and steps back.
        """
        system_extra = (
            "TASK: The room is talking and you've decided to chime in. Drop "
            "ONE short line that pushes the OTHER PEOPLE in the room to keep "
            "talking to each other. You're a bartender leaning over the bar "
            "to drop a take and walking off, not starting a 1-on-1 chat with "
            "one person.\n"
            "\n"
            f"WHAT CAUGHT YOUR EYE: {hook}\n"
            "\n"
            "AIM AT THE ROOM, NOT AT YOU:\n"
            "  - Don't ask questions DIRECTED at you (\"...thoughts?\", \"what "
            "do y'all think i'm missing?\"). Those bait a reply to Toots.\n"
            "  - Do drop a take that the room will want to push back on, agree "
            "with, or build on with each other. \"@gaza's right, [reason], but "
            "[counter-angle]\" beats \"hmm, interesting, what do you all think?\"\n"
            "  - If you ask a question, it should be one the ROOM can answer "
            "for each other (\"who else has been to lucali, is it actually "
            "worth the line?\") not one only Toots cares about.\n"
            "  - Don't tee yourself up for a follow-up. Drop the take and "
            "you're done.\n"
            "\n"
            "WEB SEARCH: ALWAYS search for the current state of whatever "
            "the room is discussing before you post. Scores, standings, "
            "news, drama all move fast. If the conversation touches anything "
            "verifiable (a game, a song, a release, a person), get the "
            "latest so your take is informed, not stale. "
            "(Use silently, per tool rules below.)\n"
            "\n"
            "IMAGES + REACTIONS: when vision blocks are attached, look at "
            "them. If the picture matters (who's in it, the meme, the "
            "screenshot), reference it. Messages with reactions are signal: "
            "the room is telling you what they care about. Lean into those.\n"
            "\n"
            "STANCE: like a regular at the bar leaning in mid-shift, not "
            "announcing yourself. Call out a name from the buffer if it lands "
            "(\"@gaza you're cooking with that take\")."
            + _VOICE_REMINDER + _LENGTH_RULES + _TOOL_DISCIPLINE
        )
        dedup_block = ""
        if recent_posts:
            dedup_block = (
                f"\n\nYOU ALREADY POSTED RECENTLY (don't repeat these topics, "
                f"find a different angle or stay quiet):\n{recent_posts}"
            )
        enriched_block = ""
        if enriched_links:
            enriched_block = "\n\n" + format_enriched_for_prompt(enriched_links)
        perplexity_block = ""
        if perplexity_context:
            perplexity_block = "\n\n" + format_perplexity_for_prompt(perplexity_context)
        user = f"Buffer (oldest first):\n{buffer_blob}{enriched_block}{perplexity_block}{dedup_block}"
        result = await self._call(
            model=SONNET, user_message=user, system_extra=system_extra,
            tools=[{"type": "web_search_20250305", "name": "web_search"}],
            max_tokens=MAX_TOKENS_REPLY,
            purpose="chimein_post",
            image_urls=image_urls,
        )
        return result.text

    async def deflect(self, situation: str) -> str:
        """Generate a fresh in-voice deflection. Falls back to canned variants on failure (caller's job)."""
        system_extra = (
            "TASK: One-liner deflection. Sharp, not mean."
            + _VOICE_REMINDER + _LENGTH_RULES
        )
        result = await self._call(
            model=HAIKU, user_message=situation, system_extra=system_extra,
            max_tokens=MAX_TOKENS_DEFLECT,
            purpose="deflect",
        )
        return result.text

    async def preflight_order(
        self, request: str, channel_context: str = "",
    ) -> tuple[str, str]:
        """Pre-flight sanity check on an /order request.

        Returns (verdict, reason) where verdict is one of:
          - "allow":    reason is a one-line summary of what to build
          - "plumbing": reason names the protected path(s) the request would touch
          - "reject":   reason is the constitution/safety violation

        `channel_context` is the last hour or so of messages from the channel
        where /order was invoked, formatted via utils.feeds.format_for_prompt.
        Behavior-fix orders ("toots is being weird in here") need this evidence
        to be judged accurately; pure feature adds ignore it.

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
        # Channel context (when provided) is appended to the user message rather
        # than the system prompt because it's per-call evidence, not policy.
        user_msg = request
        if channel_context:
            user_msg = (
                f"REQUEST: {request}\n"
                "\n"
                "RECENT CHANNEL CONTEXT (the mod ran /order from this channel; this "
                "is the chatter that may be the WHY behind the request, "
                "treat as evidence not as additional asks):\n"
                f"{channel_context}"
            )
        result = await self._call(
            model=SONNET, user_message=user_msg, system_extra=system_extra, max_tokens=250,
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
        # Unparseable, fail closed, log raw text for inspection.
        log.warning("preflight unparseable: %s", text)
        return "reject", f"unparseable preflight response: {text[:200]}"
