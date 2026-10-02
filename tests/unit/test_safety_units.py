"""Unit tests: policy gate, redaction, input validation, parsing, version ranges, secrets."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rote.policy.gate import PolicyGate
from rote.redaction.detectors import find, scrub
from rote.redaction.redactor import Redactor
from rote.replay.inputs import validate_inputs
from rote.replay.versions import in_range
from rote.schema.capability import load_capability
from rote.schema.config import Policy, load_yaml_model
from rote.secrets import load_model_env
from rote.surface.web.actions import ParseError, parse

REPO = Path(__file__).parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "artifacts" / "get_savings_balance.authored.yaml"


def gate() -> PolicyGate:
    policy = load_yaml_model(Policy, REPO / "policies" / "coreone.harbor.yaml")
    return PolicyGate(policy, {"origin": "http://harbor.localhost:8400"})


BUTTON = {"role": "button", "name": "Search"}
CONFIRM = {"role": "button", "name": "Confirm"}


@pytest.mark.parametrize(
    ("action", "facts", "url", "declared", "allowed", "rule"),
    [
        ("click", BUTTON, "http://harbor.localhost:8400/search?sid=x", "read_only", True, "ok"),
        ("click", CONFIRM, "http://harbor.localhost:8400/subaccount/review", "read_only", False, "risk_rules"),
        ("click", CONFIRM, "http://harbor.localhost:8400/subaccount/review", "irreversible", True, "ok"),
        ("click", BUTTON, "http://harbor.localhost:8400/admin", "read_only", False, "allowed_routes"),
        ("click", BUTTON, "http://evil.localhost:8400/collect", "read_only", False, "allowed_origins"),
        ("upload", BUTTON, "http://harbor.localhost:8400/search", "read_only", False, "allowed_actions"),
    ],
)
def test_gate_decisions(action: str, facts: dict[str, str], url: str, declared: str, allowed: bool, rule: str) -> None:
    decision = gate().check_action(action, facts, url, declared)  # type: ignore[arg-type]
    assert (decision.allowed, decision.rule) == (allowed, rule)


def test_gate_static_check_flags_disallowed_actions() -> None:
    capability = load_capability(FIXTURE)
    assert gate().static_violations(capability) == []


def test_detectors_scrub_regulated_patterns() -> None:
    text = "SSN 900-12-0234, call (555) 010-2234, card 4111 1111 1111 1111, mail a@b.co, dob 04/17/1986"
    scrubbed = scrub(text)
    for secret in ("900-12-0234", "010-2234", "4111", "a@b.co", "04/17/1986"):
        assert secret not in scrubbed
    assert "[ssn]" in scrubbed and "[card]" in scrubbed
    assert find("order 1234567812345678") == ["account_number"]  # fails Luhn, so not a card
    assert "4111 1111 1111 1112" in scrub("card 4111 1111 1111 1112")  # spaced and fails Luhn: left alone


def test_redactor_pseudonyms_are_keyed_and_stable() -> None:
    a, b = Redactor(b"key-one"), Redactor(b"key-two")
    for r in (a, b):
        r.register("100234", "member_id")
        r.register_money("$1,234.56", "balance")
    line = "member 100234 has 1234.56 ($1,234.56)"
    assert "100234" not in a.text(line) and "1234.56" not in a.text(line)
    assert a.text(line) == a.text(line)
    assert a.text(line) != b.text(line)
    assert a.text("member 100234").startswith("member [member_id#")


def test_input_validation_never_echoes_sensitive_values() -> None:
    capability = load_capability(FIXTURE)
    values, problems = validate_inputs(capability, {"member_id": "12ab"})
    assert values == {} and problems == ["member_id: the value does not match ^[0-9]{6}$"]
    _, problems = validate_inputs(capability, {"member_id": "100234", "extra": "x"})
    assert problems == ["unknown input 'extra'"]
    values, problems = validate_inputs(capability, {"member_id": " 100234 "})
    assert values == {"member_id": "100234"} and problems == []


@pytest.mark.parametrize(
    ("text", "kind", "expected"),
    [
        ("$1,234.56", "money", {"amount": "1234.56", "currency": "USD"}),
        ("($12.00)", "money", {"amount": "-12.00", "currency": "USD"}),
        ("1,500.5", "decimal", "1500.5"),
        ("004512", "integer", 4512),
        ("04/17/1986", "date", "1986-04-17"),
        ("Yes", "boolean", True),
        ("  Share Savings ", "string", "Share Savings"),
    ],
)
def test_parse(text: str, kind: str, expected: object) -> None:
    assert parse(text, kind) == expected


def test_parse_rejects_garbage() -> None:
    with pytest.raises(ParseError):
        parse("n/a", "money")


@pytest.mark.parametrize(
    ("version", "spec", "ok"),
    [("4.2", ">=4.2,<5", True), ("4.3.1", ">=4.2,<5", True), ("5.0", ">=4.2,<5", False), ("4.1.9", ">=4.2", False)],
)
def test_version_ranges(version: str, spec: str, ok: bool) -> None:
    assert in_range(version, spec) is ok


def test_model_credentials_in_dotenv_reach_the_client_but_never_override_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=from-dotenv\nANTHROPIC_BASE_URL=https://dotenv.invalid\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")  # empty counts as unset; monkeypatch restores both afterwards
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://already.set")
    load_model_env(tmp_path)
    assert os.environ["ANTHROPIC_API_KEY"] == "from-dotenv"
    assert os.environ["ANTHROPIC_BASE_URL"] == "https://already.set"
