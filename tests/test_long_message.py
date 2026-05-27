"""Tests for utils.long_message: truncation and safe sending."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from utils.long_message import DISCORD_MAX, _truncate, send_long


class TestTruncate:
    def test_short_text_unchanged(self) -> None:
        assert _truncate("hello") == "hello"

    def test_cuts_at_last_newline(self) -> None:
        text = "line one\nline two\nline three"
        result = _truncate(text, limit=20)
        assert result == "line one\nline two"

    def test_cuts_at_last_space_when_no_newline(self) -> None:
        text = "word " * 50
        result = _truncate(text, limit=30)
        assert len(result) <= 30
        assert not result.endswith(" ")

    def test_hard_cut_when_no_break(self) -> None:
        text = "a" * 100
        result = _truncate(text, limit=50)
        assert result == "a" * 50


@pytest.mark.asyncio
class TestSendLong:
    async def test_short_message_sends_directly(self) -> None:
        followup = AsyncMock()
        await send_long("short msg", followup=followup)
        followup.send.assert_awaited_once_with("short msg")

    async def test_short_reply_sends_directly(self) -> None:
        msg = AsyncMock()
        await send_long("short msg", reply_to=msg)
        msg.reply.assert_awaited_once_with("short msg", mention_author=False)

    async def test_short_channel_sends_directly(self) -> None:
        channel = AsyncMock()
        await send_long("short msg", channel=channel)
        channel.send.assert_awaited_once_with("short msg")

    async def test_long_message_truncated(self) -> None:
        followup = AsyncMock()
        text = "line\n" * 600
        assert len(text) > DISCORD_MAX

        await send_long(text, followup=followup)
        sent_text = followup.send.call_args.args[0]
        assert len(sent_text) <= DISCORD_MAX

    async def test_long_reply_truncated(self) -> None:
        msg = AsyncMock()
        text = "line\n" * 600
        await send_long(text, reply_to=msg)
        sent_text = msg.reply.call_args.args[0]
        assert len(sent_text) <= DISCORD_MAX
