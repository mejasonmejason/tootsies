"""Persona + constitution sanity, the things that should never quietly drift."""

from __future__ import annotations

from constitution import CONSTITUTION, HARD_RULES, HOUSE_RULES
from persona import PERSONA_CORE, system_prompt


def test_constitution_contains_hard_rules() -> None:
    assert "HARD RULES" in CONSTITUTION
    assert "HOUSE RULES" in CONSTITUTION
    # Concrete checks for the non-negotiables, if these slip, that's the alarm.
    for needle in (
        "doxxing",
        "NSFW",
        "fabricated quotes",
        "impersonation",
        "moderation actions",
        "DMs initiated",
        "Minors",
        "Crisis",
    ):
        assert needle in HARD_RULES, f"missing hard rule: {needle}"


def test_house_rules_all_present() -> None:
    for rule_num in range(1, 11):
        assert f"{rule_num}." in HOUSE_RULES


def test_persona_voice_markers() -> None:
    """The persona should still describe Toots's distinctive voice."""
    assert "Toots" in PERSONA_CORE
    assert "bartender" in PERSONA_CORE.lower()
    assert "280" in PERSONA_CORE  # length cap reminder (was ~140 chars,
    # tightened to "280 chars total" with hard token-budget enforcement)


def test_system_prompt_composes_all_layers() -> None:
    sp = system_prompt()
    assert "HARD RULES" in sp
    assert "TOOTSIES HOUSE RULES" in sp
    assert "Toots" in sp
    assert "drake" in sp.lower()  # voice examples


def test_system_prompt_appends_extras() -> None:
    sp = system_prompt("TASK: testing extra")
    assert "TASK: testing extra" in sp


def test_user_facing_prompts_share_voice_reminder() -> None:
    """Every user-facing prompt should append _VOICE_REMINDER.

    Persona is already prepended via system_prompt() (cached), but the
    load-bearing per-call reminders (REGULARS RULE, hedge ban, em-dash ban)
    are appended at the end of each system_extra so recency keeps them top
    of mind for the model. If a new user-facing prompt skips it, this test
    catches the drift instead of letting it slip into prod.

    Classifier/structured outputs (chimein_score, preflight_order) intentionally
    skip the voice reminder, they return JSON / fixed-token verdicts, not Toots
    voice.
    """
    import inspect

    from claude_client import _VOICE_REMINDER, ClaudeClient

    expected_voice = {
        "ask", "recap", "discourse", "mood_post",
        "chimein_post", "deflect",
    }
    expected_no_voice = {
        "chimein_score",         # JSON classifier
        "preflight_order",       # ALLOW/PLUMBING/REJECT classifier
    }

    for name in expected_voice:
        method = getattr(ClaudeClient, name)
        src = inspect.getsource(method)
        assert "_VOICE_REMINDER" in src, (
            f"user-facing prompt {name!r} should append _VOICE_REMINDER to "
            "its system_extra so persona reminders stay top of mind"
        )

    for name in expected_no_voice:
        method = getattr(ClaudeClient, name)
        src = inspect.getsource(method)
        assert "_VOICE_REMINDER" not in src, (
            f"classifier prompt {name!r} should NOT include _VOICE_REMINDER, "
            "it returns structured output not Toots voice"
        )
    # Sanity: the constant itself is non-empty.
    assert _VOICE_REMINDER.strip()


def test_room_directed_prompts_have_room_framing() -> None:
    """Output-to-room prompts must push the room to talk to each other, either
    via the shared _ROOM_DIRECTED constant or via richer surface-specific wording.

    discourse + mood_post use the shared constant. chimein_post has its own
    more detailed AIM AT THE ROOM block (with examples) because it's the
    highest-risk surface for misfiring (unprompted, into an active conversation).
    ask/deflect are 1:1 replies and skip both.
    """
    import inspect

    from claude_client import ClaudeClient

    uses_shared = {"discourse", "mood_post"}
    has_own_room_block = {"chimein_post"}
    one_to_one = {"ask", "deflect"}

    for name in uses_shared:
        method = getattr(ClaudeClient, name)
        src = inspect.getsource(method)
        assert "_ROOM_DIRECTED" in src, (
            f"room-output prompt {name!r} should append _ROOM_DIRECTED"
        )

    for name in has_own_room_block:
        method = getattr(ClaudeClient, name)
        src = inspect.getsource(method)
        # Must have SOME room-direction wording even if not the shared constant.
        assert "AIM AT THE ROOM" in src or "_ROOM_DIRECTED" in src, (
            f"chime-in prompt {name!r} needs room-direction guidance"
        )

    for name in one_to_one:
        method = getattr(ClaudeClient, name)
        src = inspect.getsource(method)
        assert "_ROOM_DIRECTED" not in src, (
            f"1:1-reply prompt {name!r} should NOT include _ROOM_DIRECTED"
        )


def test_no_em_dashes_in_persona_constitution_or_voice() -> None:
    """Toots never uses em dashes (see plan §2). This test fails loudly if one slips into
    a place that affects her output: the constitution, persona, voice examples, or any
    canned variant pool."""
    from utils import voice

    surfaces = {
        "CONSTITUTION": CONSTITUTION,
        "PERSONA_CORE": PERSONA_CORE,
        "system_prompt()": system_prompt(),
    }
    for pool_name in dir(voice):
        if pool_name.startswith("_"):
            continue
        attr = getattr(voice, pool_name)
        if isinstance(attr, list) and all(isinstance(x, str) for x in attr):
            surfaces[f"voice.{pool_name}"] = "\n".join(attr)
        elif isinstance(attr, str):
            surfaces[f"voice.{pool_name}"] = attr

    offenders = {name: text for name, text in surfaces.items() if "—" in text}
    assert not offenders, (
        "em dashes found in Toots-output surfaces: "
        + ", ".join(offenders.keys())
        + ". Use commas, periods, or parentheses instead."
    )


def test_no_em_dashes_anywhere_in_repo() -> None:
    """Stricter than the persona test: NO em dashes anywhere in shipped code,
    docs, or config. Reasoning: the original rule was about Toots's spoken
    output, but em dashes look ugly in code comments and docs too, and they
    creep back in if we only test the persona surfaces.

    The test allow-lists:
      - this file (it has the literal `"—"` character as the search string)
      - EXECUTION_PLAN.md (frozen v1 design artifact, intentionally untouched)
      - the .venv / __pycache__ build artifacts
      - the docs/assets/banner.jpg binary
    """
    import subprocess
    from pathlib import Path

    repo_root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        ["grep", "-rln", "—", str(repo_root)],
        capture_output=True, text=True, check=False,
    )
    paths = [
        line for line in result.stdout.strip().splitlines()
        if line.strip()
        and "/.venv/" not in line
        and "/__pycache__/" not in line
        and "/.git/" not in line
        and not line.endswith(".jpg")
        and not line.endswith(".png")
        and not line.endswith(".webp")
        and not line.endswith("EXECUTION_PLAN.md")
        and not line.endswith("tests/test_persona.py")  # this file holds the search char
    ]
    assert not paths, (
        "em dashes found in:\n  " + "\n  ".join(paths)
        + "\nUse commas, periods, colons, or parentheses instead."
    )
