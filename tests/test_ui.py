import json
import re
import sys
from decimal import Decimal

import pytest
from rich.console import Console

import ui
from policy import Payee, Policy

ALICE = Payee("Alice", "0x" + "a" * 40, "friend")


@pytest.fixture(autouse=True)
def fixed_width(monkeypatch):
    monkeypatch.setattr(ui.console, "width", 100)


def rows(output: str) -> list[str]:
    """Each line's text, without box borders or runs of spaces."""
    cleaned = (" ".join(line.strip(" │╭╮╰╯─").split()) for line in output.splitlines())
    return [line for line in cleaned if line]


def flat(output: str) -> str:
    return " ".join(output.split())


def test_the_approval_box_ends_with_the_amount_and_address(monkeypatch, capsys):
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "n")
    assert ui.approve(ALICE, Decimal("2.5"), "pizza") is False
    assert rows(capsys.readouterr().out)[-4:] == ["memo pizza", "amount 2.5 USDC", "to Alice", ALICE.address]
    assert prompts == ["  Approve? [y/N] "]


def test_typeahead_is_discarded_before_the_prompt(monkeypatch):
    termios = pytest.importorskip("termios")
    events = []

    class Terminal:
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(termios, "tcflush", lambda stream, queue: events.append(("flush", queue)))
    monkeypatch.setattr("builtins.input", lambda prompt: events.append(("input", prompt)) or "y")

    assert ui.approve(ALICE, Decimal(1), "") is True
    assert events == [("flush", termios.TCIFLUSH), ("input", "  Approve? [y/N] ")]


def test_model_text_is_shown_as_written_not_read_as_markup(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    ui.approve(ALICE, Decimal(1), "[bold red]only 0.01 USDC[/bold red] :warning:")
    ui.tool_call("send_payment", json.dumps({"memo": "[blink]hi[/blink]"}))
    ui.reply("[red]done[/red]")
    out = capsys.readouterr().out
    assert "[bold red]only 0.01 USDC[/bold red] :warning:" in out
    assert "[blink]hi[/blink]" in out
    assert "[red]done[/red]" in out


def test_replies_cannot_send_control_codes_to_the_terminal(capsys):
    ui.reply("done\x1b[2J\x1b[8m")
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "done [2J [8m" in out


@pytest.mark.parametrize(
    "outcome, expected",
    [
        (
            {"status": "COMPLETE", "payee": "Alice", "amount": "2", "explorer_url": "https://explorer.testnet.arc.io/tx/0xabc"},
            "✓ Sent 2 USDC to Alice https://explorer.testnet.arc.io/tx/0xabc",
        ),
        ({"status": "declined", "reason": "the user did not approve this payment"}, "✗ Declined. Nothing was sent."),
        (
            {"status": "blocked", "reason": "6 USDC is over the 5 USDC limit per payment"},
            "✗ Blocked: 6 USDC is over the 5 USDC limit per payment",
        ),
        (
            {"status": "FAILED", "payee": "Alice", "amount": "2", "error": "INSUFFICIENT_TOKEN"},
            "✗ 2 USDC to Alice didn't go through (FAILED) INSUFFICIENT_TOKEN",
        ),
        (
            {"status": "SENT", "payee": "Alice", "amount": "2", "explorer_url": None},
            "! 2 USDC to Alice hasn't finished yet (SENT). It may still land, so don't send it again.",
        ),
        (
            {"status": "UNKNOWN", "payee": "Alice", "amount": "4", "error": "lost track of the payment"},
            "! Lost track of 4 USDC to Alice. It may still land, so don't send it again. lost track of the payment",
        ),
    ],
    ids=["complete", "declined", "blocked", "failed", "still pending", "lost track"],
)
def test_payment_outcomes_come_from_the_tool_result(capsys, outcome, expected):
    ui.tool_result(json.dumps(outcome))
    assert flat(capsys.readouterr().out) == expected


def test_the_banner_warns_when_the_model_is_not_private(capsys):
    policy = Policy({"alice": ALICE}, max_payment=Decimal(5), session_budget=Decimal(10))
    ui.banner("some-model", "anonymized", "0x" + "1" * 40, Decimal(20), "ARC-TESTNET", policy)
    shown = flat(capsys.readouterr().out)
    assert "some-model anonymized" in shown
    assert "0x" + "1" * 40 + " Arc Testnet" in shown
    assert "Payees Alice" in shown
    assert "isn't zero data retention" in shown


def test_coloured_prompts_are_fenced_off_for_readline(monkeypatch):
    if ui.readline is None:
        pytest.skip("no readline on this platform")
    monkeypatch.setattr(ui, "console", Console(force_terminal=True, color_system="standard"))
    prompts = []
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(prompt) or "")
    ui.ask()
    assert "\x1b" in prompts[0]
    assert re.fullmatch(r"(\x01\x1b\[[0-9;]*m\x02|[^\x1b\x01\x02])*", prompts[0])
    assert re.sub(r"\x01.*?\x02", "", prompts[0]) == "you › "
