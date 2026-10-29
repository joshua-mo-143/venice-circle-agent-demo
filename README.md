# A Venice payments agent with a Circle wallet

A terminal agent that pays people in USDC when you ask it to. Venice reads your request and decides which tools to call. A Circle developer-controlled wallet on Arc Testnet sends the money. In between, ordinary Python decides what's allowed: the model only ever sees names from an address book, spending limits live in code it can't argue with, and nothing is signed until you approve it.

Everything runs on testnet with free USDC from Circle's faucet.

## What we're building

| Piece | What it does | File |
| --- | --- | --- |
| Venice | Reads your request and picks tool calls (`function_calling_default` model) | `agent.py` |
| Tools | `get_balance`, `list_payees`, `send_payment`: the only things the model can ask for | `tools.py` |
| Policy | Address book, per-payment cap, session budget | `policy.py` |
| Circle wallet | Holds the USDC, sends transfers, reports their state | `wallet.py` |
| Terminal | Shows tool calls and payment outcomes, and asks you to approve each payment | `ui.py` |

```text
.
├── agent.py               # prompt loop and the Venice tool-calling loop
├── tools.py               # what the model sees, and what runs when it calls a tool
├── policy.py              # address book and spending limits (no network)
├── wallet.py              # the Circle calls: balance, transfer, wait
├── ui.py                  # everything the terminal shows, including the approval box
├── setup_wallet.py        # one-off: create the agent's wallet and a test payee
├── payees.example.toml
├── tests/
├── .env.example
└── pyproject.toml
```

## Pre-requisites

- Python 3.11+ and [uv](https://docs.astral.sh/uv/)
- A Venice API key from [venice.ai](https://venice.ai)
- A [Circle Developer Console](https://console.circle.com/signup) account. The first step below covers the testnet API key and entity secret you need from it.

## Getting started

### 1. Get your Circle credentials

1. In the [Circle Developer Console](https://console.circle.com), select **API & Client Keys** in the sidebar, then **Create a key** → **API Key**, and pick **Standard** access. Select **Show** and copy the whole key. Testnet keys look like `TEST_API_KEY:<id>:<secret>`.
2. Open the Console's [entity secret page](https://console.circle.com/wallets/dev/configurator/entity-secret) and follow its steps to create and register an entity secret. Keep the secret and the recovery file it gives you. Store the recovery file somewhere safe outside this repo, like a password manager: you can only download it once, and it's the only way to reset a lost secret.

Circle's guide has more detail, including a script that registers the secret for you: [Generate and register your entity secret](https://developers.circle.com/wallets/dev-controlled/register-entity-secret).

### 2. Configure the agent

```bash
cp .env.example .env
```

Fill in `VENICE_API_KEY`, and `CIRCLE_API_KEY` with the whole testnet key. Uncomment `CIRCLE_ENTITY_SECRET` and paste the secret in. If you used Circle's script instead of the Console, it adds that line for you.

### 3. Create and fund the wallets

```bash
uv sync
uv run python setup_wallet.py
```

This creates two Arc Testnet wallets: one for the agent, and one called Alice so you have someone to pay. It writes `payees.toml` and prints a `CIRCLE_WALLET_ID` line. Add that line to `.env`, then paste the agent's address into the [Circle faucet](https://faucet.circle.com) and pick Arc Testnet. Arc uses USDC for gas, so one faucet request covers the payments and the fees.

### 4. Run it

```bash
uv run python agent.py
```

```text
╭─ Venice × Circle payments agent ───────────────────╮
│                                                    │
│  Model    zai-org-glm-5-2  private                 │
│  Wallet   0x…  Arc Testnet                         │
│  Balance  20 USDC                                  │
│  Payees   Alice                                    │
│  Limits   5 USDC per payment, 10 USDC per session  │
│           and you approve every payment            │
│                                                    │
╰────────────────────────────────────────────────────╯
Type a request, or q to quit.

you › Pay Alice 2 USDC for Friday's pizza
  → send_payment(payee="Alice", amount="2", memo="Friday's pizza")

  ╭─ Approve this payment? ─╮
  │ memo    Friday's pizza  │
  │ amount  2 USDC          │
  │ to      Alice           │
  │         0x…             │
  ╰─────────────────────────╯
  Approve? [y/N] y
  ✓ Sent 2 USDC to Alice
    https://explorer.testnet.arc.io/tx/0x…

agent › Done. 2 USDC went to Alice for Friday's pizza, and you have 8 USDC left
        in this session's budget.
```

Tool calls print as they happen. Each payment's outcome line comes from Circle's result, not from the model's summary, so you can check one against the other. Nothing is sent until you approve it.

## Things to try

- **"Pay Alice 2 USDC for Friday's pizza"**: approve it, and you get a ✓ and an explorer link.
- **"Pay Alice 6 USDC"**: blocked by the 5 USDC cap before you're asked.
- **Answer `N` to a payment**: declined, and nothing is sent.
- **Keep paying**: blocked once the 10 USDC session budget runs out.
- **"Pay 0x1234… 1 USDC"**: the model can't, because it only knows names from the address book.

## Configuration

| Variable | Default | What it does |
| --- | --- | --- |
| `VENICE_MODEL` | `function_calling_default` trait | Pin a specific Venice model |
| `MAX_PAYMENT_USDC` | `5` | Largest single payment |
| `SESSION_BUDGET_USDC` | `10` | Total the agent can send before you restart it |

Add people by editing `payees.toml`. See `payees.example.toml` for the format. Names ignore case and have to be unique, and the agent won't start if an address is malformed or still the zero-address placeholder.

## How the guardrails work

1. **The model never handles addresses.** `send_payment` takes a payee name. `policy.py` resolves it to an address from `payees.toml`, so a made-up, mistyped or injected address has nowhere to go.
2. **Limits are code, not prompt.** Amounts are parsed as `Decimal`, checked against the per-payment cap and the remaining session budget, and rejected with a reason the model can read. Note that the cap applies to each call, so nothing in code stops the model splitting a big payment into smaller ones. The system prompt tells it not to, but what actually holds the line is the session budget and your approval.
3. **You approve every payment.** The terminal shows the payee, full address, amount and memo in a box before the wallet signs. The model can't redraw that box: its text is shown exactly as written, with terminal control codes stripped and nothing read as styling markup. The memo is cut to a single line, and the amount and address are always the last thing above `Approve?`. Anything typed or pasted before the prompt appears is thrown away, so it can't answer for you.
4. **Retries can't double-pay.** Each payment gets one idempotency key. If the request to Circle fails and is retried, Circle returns the original transfer instead of creating a second one. If there's still no clear answer after three tries, the payment comes back as `UNKNOWN` rather than failed, and the model is told not to send it again.
5. **Pending counts as spent.** A payment that hasn't reached a final state still counts against the budget, because it can still land. So does one the agent lost track of, for example because the connection to Circle dropped. Only payments Circle refused outright, or that ended `FAILED`, `CANCELLED` or `DENIED`, are left out.

## Tests

```bash
uv run pytest
```

The tests cover the policy, the tools, the tool-calling loop, the wallet's retry and polling logic, and what the terminal shows. They use fakes, so they don't need any keys.

## Notes

- The memo is stored by Circle as the transfer's `refId`, trimmed to one line of up to 100 characters. It isn't written onchain, but the amount and both addresses are public on Arc.
- On Arc, gas is paid in USDC, so the balance drops by slightly more than the amount sent. The budget only counts amounts.
- Dev-controlled wallets mean your server holds the entity secret. For wallets your users own, with spending policies enforced by Circle, look at [Circle Agent Wallets](https://developers.circle.com/agent-stack/agent-wallets).

## License

MIT. See [LICENSE](LICENSE).
