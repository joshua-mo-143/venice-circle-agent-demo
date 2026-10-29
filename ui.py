"""Everything the agent shows in the terminal, and the two places it reads from the keyboard.

Text from the model, Circle or payees.toml only reaches the screen through plain(): control characters
become spaces, and rich never parses a Text as markup, so none of it can restyle the terminal or fake
the approval box.
"""

from __future__ import annotations

import json
import re
import sys
from decimal import Decimal

from rich.console import Console
from rich.markdown import Markdown
from rich.padding import Padding
from rich.panel import Panel
from rich.status import Status
from rich.style import Style
from rich.table import Table
from rich.text import Text

from policy import Payee, Policy
from wallet import DID_NOT_HAPPEN

try:
    import readline  # importing it gives input() arrow keys and history
except ImportError:  # Windows
    readline = None

try:
    import termios
except ImportError:  # Windows
    termios = None

VENICE_BLUE = "#125DA3"

console = Console(highlight=False)


def terminal_safe(text: str) -> str:
    """Swap control characters for spaces, so model text can't move the cursor or restyle the terminal."""
    return "".join(ch if ch.isprintable() or ch in "\n\t" else " " for ch in text)


def plain(text: object, style: str | Style = "") -> Text:
    return Text(terminal_safe(str(text)), style=style)


def discard_typeahead() -> None:
    """Drop anything typed or pasted before the prompt appeared, so it can't answer the prompt."""
    if termios and sys.stdin.isatty():
        termios.tcflush(sys.stdin, termios.TCIFLUSH)


def read_line(prompt: Text) -> str:
    """input() with a styled prompt. readline can only measure the prompt if its colour codes are fenced off."""
    with console.capture() as capture:
        console.print(prompt, end="")
    rendered = capture.get()
    if readline:
        rendered = re.sub("(\x1b\\[[0-9;]*m)", "\x01\\1\x02", rendered)
    return input(rendered)


def ask() -> str:
    return read_line(Text("you › ", style="bold cyan"))


def working(message: str) -> Status:
    """A spinner for while we wait on Venice or Circle. It's cleared when the block ends."""
    return console.status(plain(f"{message}…", "dim"))


def banner(model: str, privacy: str, address: str, balance: Decimal, network: str, policy: Policy) -> None:
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style="dim")
    rows.add_column()
    privacy_style = "green" if privacy == "private" else "yellow"
    rows.add_row("Model", Text.assemble(plain(model, "bold"), "  ", plain(privacy, privacy_style)))
    rows.add_row("Wallet", Text.assemble(plain(address, "cyan"), "  ", plain(network.replace("-", " ").title(), "dim")))
    rows.add_row("Balance", plain(f"{balance:f} USDC", "bold green"))
    rows.add_row("Payees", plain(", ".join(payee.name for payee in policy.payees.values())))
    rows.add_row("Limits", plain(f"{policy.max_payment:f} USDC per payment, {policy.session_budget:f} USDC per session"))
    rows.add_row("", plain("and you approve every payment"))

    console.print()
    console.print(
        Panel(
            rows,
            title=Text("Venice × Circle payments agent", style="bold"),
            title_align="left",
            border_style=VENICE_BLUE,
            padding=(1, 2),
            expand=False,
        )
    )
    if privacy != "private":
        _note("!", "yellow", "This model isn't zero data retention. Set VENICE_MODEL to a private model if that matters.")
    console.print(plain("Type a request, or q to quit.", "dim"))
    console.print()


def tool_call(name: str, arguments: str) -> None:
    try:
        shown = json.loads(arguments or "{}").items()
        args = ", ".join(f"{key}={json.dumps(value, ensure_ascii=False)}" for key, value in shown)
    except (json.JSONDecodeError, AttributeError):
        args = arguments
    console.print(Text.assemble("  ", ("→ ", "dim"), plain(name, "bold"), plain(f"({args})", "dim")))


def tool_result(result: str) -> None:
    """Say what a tool call did, from its result rather than from the model's account of it."""
    outcome = json.loads(result)
    if not isinstance(outcome, dict):
        return
    if "status" in outcome:
        _payment(outcome)
    elif outcome.get("error"):
        _note("✗", "red", outcome["error"])


def _payment(outcome: dict) -> None:
    status = outcome["status"]
    what = f"{outcome.get('amount')} USDC to {outcome.get('payee')}"
    if status == "COMPLETE":
        _note("✓", "green", f"Sent {what}")
    elif status == "declined":
        _note("✗", "yellow", "Declined. Nothing was sent.")
    elif status == "blocked":
        _note("✗", "red", f"Blocked: {outcome.get('reason')}")
    elif status == "rejected":
        _note("✗", "red", outcome.get("reason"))
    elif status in DID_NOT_HAPPEN:
        _note("✗", "red", f"{what} didn't go through ({status})", outcome.get("error"))
    elif status == "UNKNOWN":
        _note("!", "yellow", f"Lost track of {what}. It may still land, so don't send it again.", outcome.get("error"))
    else:
        _note("!", "yellow", f"{what} hasn't finished yet ({status}). It may still land, so don't send it again.")

    if outcome.get("explorer_url"):
        url = terminal_safe(outcome["explorer_url"])
        # Soft wrap leaves the URL as one line, so it still copies and clicks when the terminal wraps it.
        console.print(Text.assemble("    ", (url, Style(color="cyan", link=url))), soft_wrap=True)


def _note(mark: str, style: str, message: object, detail: object = None) -> None:
    console.print(Text.assemble("  ", (mark, f"bold {style}"), " ", plain(message)))
    if detail:
        console.print(Text.assemble("    ", plain(detail, "dim")))


def approve(payee: Payee, amount: Decimal, memo: str) -> bool:
    rows = Table.grid(padding=(0, 2))
    rows.add_column(style="dim")
    rows.add_column()
    if memo:
        rows.add_row("memo", plain(memo, "italic"))
    rows.add_row("amount", plain(f"{amount:f} USDC", "bold green"))
    rows.add_row("to", plain(payee.name, "bold"))
    rows.add_row("", plain(payee.address, "cyan"))
    box = Panel(
        rows,
        title=Text("Approve this payment?", style="bold"),
        title_align="left",
        border_style="yellow",
        expand=False,
    )

    console.print()
    console.print(Padding(box, (0, 0, 0, 2), expand=False))
    discard_typeahead()
    answer = read_line(Text.assemble("  ", ("Approve? ", "bold yellow"), ("[y/N] ", "dim")))
    return answer.strip().lower() in {"y", "yes"}


def reply(text: str) -> None:
    rows = Table.grid(padding=(0, 1))
    rows.add_column(no_wrap=True)
    rows.add_column()
    rows.add_row(Text("agent ›", style="bold blue"), Markdown(terminal_safe(text), justify="left", hyperlinks=False))
    console.print()
    console.print(rows)
    console.print()


def error(message: str) -> None:
    _note("✗", "red", message)
    console.print()
