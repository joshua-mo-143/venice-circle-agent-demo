"""What the model sees, and the code that runs when it asks for something."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from decimal import Decimal

from circle.web3.developer_controlled_wallets import ApiException

import ui
from policy import Payee, Policy, PolicyError
from wallet import DID_NOT_HAPPEN, TransferRejected, Wallet

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_balance",
            "description": "Get the wallet's USDC balance and how much of this session's budget is left.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_payees",
            "description": "List the people this wallet is allowed to pay, by name.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_payment",
            "description": (
                "Send USDC to someone in the address book. The payment is checked against "
                "spending limits and the user has to approve it before anything is sent."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "payee": {
                        "type": "string",
                        "description": "The payee's name, exactly as list_payees returns it.",
                    },
                    "amount": {
                        "type": "string",
                        "description": 'Amount in USDC as a decimal string, for example "2.50".',
                    },
                    "memo": {
                        "type": "string",
                        "description": "A short note saying what the payment is for.",
                    },
                },
                "required": ["payee", "amount", "memo"],
            },
        },
    },
]

MEMO_LIMIT = 100

Approver = Callable[[Payee, Decimal, str], bool]


def clean_memo(memo: object) -> str:
    """One short line of plain text, so what you approve is exactly what Circle stores."""
    return " ".join(ui.terminal_safe(str(memo or "")).split())[:MEMO_LIMIT]


class PaymentTools:
    def __init__(self, wallet: Wallet, policy: Policy, approve: Approver = ui.approve):
        self.wallet = wallet
        self.policy = policy
        self.approve = approve

    def get_balance(self) -> dict:
        return {
            "usdc": f"{self.wallet.usdc_balance():f}",
            "session_budget_left": f"{self.policy.remaining:f}",
        }

    def list_payees(self) -> list[dict]:
        return [{"name": p.name, "note": p.note} for p in self.policy.payees.values()]

    def send_payment(self, payee: str, amount: str, memo: str = "") -> dict:
        try:
            who, value = self.policy.check(payee, amount)
        except PolicyError as error:
            return {"status": "blocked", "reason": str(error)}

        memo = clean_memo(memo)
        if not self.approve(who, value, memo):
            return {"status": "declined", "reason": "the user did not approve this payment"}

        try:
            with ui.working(f"Sending {value:f} USDC to {who.name}"):
                receipt = self.wallet.send_usdc(who.address, value, memo, idempotency_key=str(uuid.uuid4()))
        except TransferRejected as error:
            return {"status": "rejected", "reason": str(error)}
        except Exception as error:  # noqa: BLE001
            # The transfer may exist even though we lost track of it, so it counts as spent.
            self.policy.record(value)
            return {
                "status": "UNKNOWN",
                "payee": who.name,
                "amount": f"{value:f}",
                "error": f"lost track of the payment: {type(error).__name__}: {error}",
            }

        if receipt.state not in DID_NOT_HAPPEN:
            # Pending payments count too: they can still land after we stop waiting.
            self.policy.record(value)

        return {
            "status": receipt.state,
            "payee": who.name,
            "amount": f"{value:f}",
            "transaction_id": receipt.id,
            "explorer_url": receipt.explorer_url,
            "network_fee": receipt.network_fee,
            "error": receipt.error,
        }

    def call(self, name: str, arguments: str) -> str:
        """Run one tool call and return the result as JSON.

        Failures come back as results too: a tool call left without a result breaks the next request.
        """
        handlers = {
            "get_balance": self.get_balance,
            "list_payees": self.list_payees,
            "send_payment": self.send_payment,
        }
        handler = handlers.get(name)
        if handler is None:
            return json.dumps({"error": f"there is no tool called {name!r}"})

        try:
            result = handler(**json.loads(arguments or "{}"))
        except (json.JSONDecodeError, TypeError) as error:
            result = {"error": f"bad arguments for {name}: {error}"}
        except ApiException as error:
            result = {"error": f"Circle rejected the request ({error.status}): {error.body or error.reason}"}
        except Exception as error:  # noqa: BLE001
            result = {"error": f"{name} failed: {type(error).__name__}: {error}"}
        return json.dumps(result)
