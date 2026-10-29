"""A terminal payments agent: Venice decides what to do, a Circle wallet moves the money."""

from __future__ import annotations

import os
import sys
from decimal import Decimal
from pathlib import Path

import httpx
from dotenv import load_dotenv
from openai import OpenAI

import ui
from policy import Policy, load_payees
from tools import TOOLS, PaymentTools
from wallet import BLOCKCHAIN, Wallet

VENICE_URL = "https://api.venice.ai/api/v1"

SYSTEM = """You are a payments assistant. You control a USDC wallet on Arc Testnet through three tools.

- You can only pay people in the address book. Call list_payees if you're not sure who is in it.
- Amounts are in USDC. If the payee or the amount is unclear, ask instead of guessing.
- Every payment is checked against spending limits, then the user approves or declines it.
  If a payment comes back "blocked", "declined" or "rejected", explain why and stop. Never retry
  with a different payee or amount, or split a payment up, to get around a limit.
- Only say a payment went through if its status is COMPLETE. FAILED, CANCELLED and DENIED mean it
  didn't happen. Any other status, including UNKNOWN, means it may still land: say so, and don't
  send it again.
- Keep replies short. The terminal already shows each payment's outcome and explorer link, so you
  don't need to repeat the link."""


def pick_model(api_key: str) -> tuple[str, str]:
    """Return the model to use and its privacy mode ("private" or "anonymized")."""
    headers = {"Authorization": f"Bearer {api_key}"}
    model = os.environ.get("VENICE_MODEL")
    if not model:
        traits = httpx.get(f"{VENICE_URL}/models/traits", headers=headers, timeout=30)
        model = traits.raise_for_status().json()["data"]["function_calling_default"]

    models = httpx.get(f"{VENICE_URL}/models", params={"type": "text"}, headers=headers, timeout=30)
    spec = next((m["model_spec"] for m in models.raise_for_status().json()["data"] if m["id"] == model), None)
    if spec is None:
        sys.exit(f"Venice has no text model called {model!r}. If you set VENICE_MODEL, check the spelling.")
    if spec.get("capabilities", {}).get("supportsFunctionCalling") is False:
        sys.exit(f"{model} doesn't support tool calling, which this agent needs. Pick another VENICE_MODEL.")
    return model, spec.get("privacy", "unknown")


def run_turn(client: OpenAI, model: str, messages: list, tools: PaymentTools, max_rounds: int = 8) -> str:
    for _ in range(max_rounds):
        with ui.working("Thinking"):
            response = client.chat.completions.create(
                model=model,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0,
                extra_body={"venice_parameters": {"include_venice_system_prompt": False}},
            )
        message = response.choices[0].message
        messages.append(message.model_dump(exclude_none=True))

        if not message.tool_calls:
            return message.content or ""

        for call in message.tool_calls:
            ui.tool_call(call.function.name, call.function.arguments)
            result = tools.call(call.function.name, call.function.arguments)
            ui.tool_result(result)
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result})

    return f"I stopped after {max_rounds} rounds of tool calls without an answer."


def main() -> None:
    load_dotenv()
    if not os.environ.get("CIRCLE_WALLET_ID") or not Path("payees.toml").exists():
        sys.exit("No wallet yet. Run `uv run python setup_wallet.py` first (see the README).")

    policy = Policy(
        payees=load_payees("payees.toml"),
        max_payment=Decimal(os.environ.get("MAX_PAYMENT_USDC", "5")),
        session_budget=Decimal(os.environ.get("SESSION_BUDGET_USDC", "10")),
    )
    venice_key = os.environ["VENICE_API_KEY"]
    with ui.working("Connecting to Venice and Circle"):
        model, privacy = pick_model(venice_key)
        wallet = Wallet(
            api_key=os.environ["CIRCLE_API_KEY"],
            entity_secret=os.environ["CIRCLE_ENTITY_SECRET"],
            wallet_id=os.environ["CIRCLE_WALLET_ID"],
        )
        address, balance = wallet.address(), wallet.usdc_balance()
    ui.banner(model, privacy, address, balance, BLOCKCHAIN, policy)

    venice = OpenAI(api_key=venice_key, base_url=VENICE_URL)
    tools = PaymentTools(wallet, policy)
    messages = [{"role": "system", "content": SYSTEM}]
    while True:
        try:
            text = ui.ask().strip()
        except EOFError:
            ui.console.print()
            return
        if text.lower() in {"q", "quit", "exit"}:
            return
        if not text:
            continue

        messages.append({"role": "user", "content": text})
        try:
            reply = run_turn(venice, model, messages, tools)
        except Exception as error:  # noqa: BLE001
            # Keep the turn: it may hold the result of a payment that went through.
            ui.error(f"{type(error).__name__}: {error}")
            continue
        ui.reply(reply)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
