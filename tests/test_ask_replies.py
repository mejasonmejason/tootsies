"""_reply_quote: when does a reply count as addressing Toots?

Pure-function coverage of the reply-detection gate, no discord client needed,
just duck-typed stand-ins for Message / MessageReference / author.
"""

from __future__ import annotations

from types import SimpleNamespace

from cogs.ask import _reply_quote

ME = 999  # Toots' user id


def _msg(reference: object) -> SimpleNamespace:
    return SimpleNamespace(reference=reference)


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
