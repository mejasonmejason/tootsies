"""Command instrumentation.

Wraps a cog's slash-command callback with `@track_command()`. Every invocation:
- emits a single structured log line for log-aggregation tools to filter on
- writes a row to the `command_metrics` table for time-series queries (latency,
  success rate, per-user volume) without needing external telemetry infra.

Failures in the metrics path never block the command — we log and swallow.
"""

from __future__ import annotations

import functools
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar, cast

import discord

# Re-exported into this module's globals so discord.py's get_type_hints() can resolve
# forward references in wrapped-cog annotations (e.g. `app_commands.Choice[str]`).
# Without these imports, get_type_hints raises NameError at cog-load time.
from discord import app_commands  # noqa: F401

from utils.events import emit

log = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Awaitable[Any]])


def track_command(command_name: str | None = None) -> Callable[[F], F]:
    """Decorator for cog command callbacks.

    `command_name` is optional; if not given, we read it from the interaction at call time.
    Pass it explicitly for command-group subcommands so the recorded name is the qualified
    form (e.g. `order new`) rather than just `new`.
    """

    def decorator(func: F) -> F:
        # Capture the wrapped function's signature so discord.py's app_commands.command()
        # introspection sees the real parameters (`question: str`, etc.) instead of our
        # wrapper's `*args, **kwargs`. Without this, slash command registration crashes
        # with "NameError: name 'app_commands' is not defined" when it tries to resolve
        # forward references on the wrapper.
        original_sig = inspect.signature(func)

        @functools.wraps(func)
        async def wrapper(
            self: Any, interaction: discord.Interaction, *args: Any, **kwargs: Any
        ) -> Any:
            start = time.monotonic()
            ok = True
            error_class: str | None = None
            try:
                return await func(self, interaction, *args, **kwargs)
            except Exception as exc:
                ok = False
                error_class = type(exc).__name__
                raise
            finally:
                duration_ms = int((time.monotonic() - start) * 1000)
                name = command_name or _interaction_command_name(interaction)
                emit(
                    "command",
                    cmd=name,
                    user_id=interaction.user.id,
                    guild_id=interaction.guild_id,
                    duration_ms=duration_ms,
                    ok=ok,
                    error=error_class,
                )
                # DB write is best-effort. If the metrics table or pool is unavailable,
                # we don't want a metrics failure to bubble into the user's response.
                db = getattr(self.bot, "db", None)
                if db is not None:
                    try:
                        await db.record_command(
                            guild_id=interaction.guild_id,
                            user_id=interaction.user.id,
                            command=name,
                            duration_ms=duration_ms,
                            ok=ok,
                            error_class=error_class,
                        )
                    except Exception:
                        log.exception("metrics write failed for cmd=%s", name)

        wrapper.__signature__ = original_sig  # type: ignore[attr-defined]
        return cast(F, wrapper)

    return decorator


def _interaction_command_name(interaction: discord.Interaction) -> str:
    cmd = interaction.command
    if cmd is None:
        return "?"
    # qualified_name walks parent groups so subcommands log as e.g. "order new" not "new".
    return getattr(cmd, "qualified_name", cmd.name)
