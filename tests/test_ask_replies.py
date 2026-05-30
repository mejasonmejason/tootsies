"""_reply_quote: when does a reply count as addressing Toots?

Pure-function coverage of the reply-detection gate, no discord client needed,
just duck-typed stand-ins for Message / MessageReference / author.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest

from cogs.ask import _reply_quote, _safe_reply

ME = 999  # Toots' user id


def _msg(reference: object) -> discord.Message:
    # _reply_quote only duck-types (getattr) off the message; cast so the call
    # sites type-check against the real discord.Message signature.
    return cast(discord.Message, SimpleNamespace(reference=reference))


def _ref(resolved: object) -> SimpleNamespace:
    return SimpleNamespace(resolved=resolved)


def _resolved(author_id: int, content: str) -> SimpleNamespace:
    return SimpleNamespace(author=SimpleNamespace(id=author_id), content=content)


def test_no_reference_returns_none() -> None:
    assert _reply_quote(_msg(None), ME) is None


def test_reply_to_toots_returns_her_text() -> None:
    msg = _msg(_ref(_resolved(ME, "drake is done")))
    assert _reply_quote(msg, ME) == "drake is done"


def test_reply_to_someone_else_returns_none() -> None:
    msg = _msg(_ref(_resolved(123, "some human said this")))
    assert _reply_quote(msg, ME) is None


def test_reply_to_toots_empty_body_returns_empty_string() -> None:
    # Distinct from None: it IS a reply to her, just with no text to quote.
    msg = _msg(_ref(_resolved(ME, "")))
    assert _reply_quote(msg, ME) == ""


def test_deleted_referenced_message_returns_none() -> None:
    # DeletedReferencedMessage has no .author attribute.
    msg = _msg(_ref(SimpleNamespace(id=1)))
    assert _reply_quote(msg, ME) is None


def test_uncached_reference_returns_none() -> None:
    msg = _msg(_ref(None))
    assert _reply_quote(msg, ME) is None


# ---- _safe_reply -------------------------------------------------------------


def _reply_message() -> tuple[Any, Any]:
    """A Message whose channel.send is an AsyncMock and whose to_reference
    records the fail_if_not_exists flag it was called with."""
    channel = SimpleNamespace(send=AsyncMock())
    msg = MagicMock(spec=discord.Message)
    msg.channel = channel
    msg.to_reference = MagicMock(return_value="REF")
    return msg, channel


async def test_safe_reply_sends_non_failing_reference() -> None:
    # The whole point of #146: the reference must be built with
    # fail_if_not_exists=False so a deleted source message degrades to a plain
    # send instead of raising 50035.
    msg, channel = _reply_message()
    await _safe_reply(msg, "here's your answer")
    msg.to_reference.assert_called_once_with(fail_if_not_exists=False)
    channel.send.assert_awaited_once_with(
        "here's your answer", reference="REF", mention_author=False,
    )


async def test_safe_reply_propagates_other_http_errors() -> None:
    # A non-reference failure (e.g. over-long body, missing perms) must still
    # surface, the guard is scoped to the deleted-message race only.
    msg, channel = _reply_message()
    channel.send.side_effect = discord.HTTPException(
        cast(Any, SimpleNamespace(status=400, reason="Bad Request")),
        {"code": 50035, "message": "Invalid Form Body"},
    )
    with pytest.raises(discord.HTTPException):
        await _safe_reply(msg, "x" * 5000)


# ---- _format_memory_hits -----------------------------------------------------


def test_format_memory_hits_empty():
    from cogs.ask import _format_memory_hits
    assert "nothing specific" in _format_memory_hits([])


def test_format_memory_hits_renders_tier_and_date_range():
    from datetime import UTC, datetime

    from cogs.ask import _format_memory_hits
    hits = [(
        "weekly", "alex drove the drake debate",
        datetime(2026, 5, 1, tzinfo=UTC), datetime(2026, 5, 7, tzinfo=UTC),
    )]
    out = _format_memory_hits(hits)
    assert "[weekly | May 01-May 07]" in out
    assert "alex drove the drake debate" in out
