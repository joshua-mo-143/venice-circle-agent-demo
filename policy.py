"""The rules a payment has to pass before anyone is asked to approve it.

Nothing in here talks to the model or the network, so these checks hold no
matter what the model says.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

USDC_DECIMALS = 6
USDC_STEP = Decimal(1).scaleb(-USDC_DECIMALS)
EVM_ADDRESS = re.compile(r"0x[0-9a-fA-F]{40}")
ZERO_ADDRESS = "0x" + "0" * 40


class PolicyError(Exception):
    """A payment the agent is not allowed to make."""


@dataclass(frozen=True)
class Payee:
    name: str
    address: str
    note: str = ""


def load_payees(path: str | Path) -> dict[str, Payee]:
    with open(path, "rb") as f:
        entries = tomllib.load(f).get("payees", [])

    payees = {}
    for entry in entries:
        payee = Payee(str(entry["name"]).strip(), str(entry["address"]), entry.get("note", ""))
        if not payee.name:
            raise ValueError(f"a payee in {path} has no name")
        if not EVM_ADDRESS.fullmatch(payee.address):
            raise ValueError(f"{payee.name} has an invalid address: {payee.address!r}")
        if payee.address == ZERO_ADDRESS:
            raise ValueError(f"{payee.name} has the zero address. Replace it with their real address.")
        if payee.name.lower() in payees:
            raise ValueError(f"{path} has more than one payee called {payee.name!r} (names ignore case)")
        payees[payee.name.lower()] = payee
    return payees


def parse_usdc(raw: object) -> Decimal:
    try:
        amount = Decimal(str(raw).strip())
    except InvalidOperation:
        raise PolicyError(f"{raw!r} is not a number") from None

    if not amount.is_finite() or amount <= 0:
        raise PolicyError("the amount has to be more than zero")
    try:
        rounded = amount.quantize(USDC_STEP)
    except InvalidOperation:
        raise PolicyError(f"{raw!r} is too large") from None
    if rounded != amount:
        raise PolicyError("USDC only goes to 6 decimal places")
    return rounded.normalize()


@dataclass
class Policy:
    payees: dict[str, Payee]
    max_payment: Decimal
    session_budget: Decimal
    spent: Decimal = Decimal(0)

    @property
    def remaining(self) -> Decimal:
        return self.session_budget - self.spent

    def check(self, payee: object, amount: object) -> tuple[Payee, Decimal]:
        who = self.payees.get(str(payee).strip().lower())
        if who is None:
            names = ", ".join(p.name for p in self.payees.values())
            raise PolicyError(f"{payee!r} is not in the address book (payees: {names})")

        value = parse_usdc(amount)
        if value > self.max_payment:
            raise PolicyError(
                f"{value:f} USDC is over the {self.max_payment:f} USDC limit per payment"
            )
        if value > self.remaining:
            raise PolicyError(
                f"{value:f} USDC is more than the {self.remaining:f} USDC "
                "left in this session's budget"
            )
        return who, value

    def record(self, amount: Decimal) -> None:
        self.spent += amount
