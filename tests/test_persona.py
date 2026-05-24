"""Persona + constitution sanity — the things that should never quietly drift."""

from __future__ import annotations

from constitution import CONSTITUTION, HARD_RULES, HOUSE_RULES
from persona import PERSONA_CORE, system_prompt


def test_constitution_contains_hard_rules() -> None:
    assert "HARD RULES" in CONSTITUTION
    assert "HOUSE RULES" in CONSTITUTION
    # Concrete checks for the non-negotiables — if these slip, that's the alarm.
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
    assert "140" in PERSONA_CORE  # length cap reminder


def test_system_prompt_composes_all_layers() -> None:
    sp = system_prompt()
    assert "HARD RULES" in sp
    assert "TOOTSIES HOUSE RULES" in sp
    assert "Toots" in sp
    assert "drake" in sp.lower()  # voice examples


def test_system_prompt_appends_extras() -> None:
    sp = system_prompt("TASK: testing extra")
    assert "TASK: testing extra" in sp


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
