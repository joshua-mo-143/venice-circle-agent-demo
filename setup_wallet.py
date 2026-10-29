"""One-off setup: create the agent's wallet, plus a second wallet to pay while testing.

Run it once, add CIRCLE_WALLET_ID to .env, then fund the agent's address from
https://faucet.circle.com (pick Arc Testnet).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from circle.web3 import developer_controlled_wallets as dcw
from circle.web3 import utils
from dotenv import load_dotenv

from wallet import BLOCKCHAIN, REQUEST_TIMEOUT

PAYEES = Path("payees.toml")


def main() -> None:
    load_dotenv()
    if os.environ.get("CIRCLE_WALLET_ID"):
        sys.exit("CIRCLE_WALLET_ID is already set in .env, so there's nothing to set up.")
    missing = [name for name in ("CIRCLE_API_KEY", "CIRCLE_ENTITY_SECRET") if not os.environ.get(name)]
    if missing:
        sys.exit(f"Set {' and '.join(missing)} in .env first (see the README).")
    if not os.environ["CIRCLE_API_KEY"].startswith("TEST_API_KEY:"):
        sys.exit("CIRCLE_API_KEY should be a whole testnet key, starting with TEST_API_KEY:")

    client = utils.init_developer_controlled_wallets_client(
        api_key=os.environ["CIRCLE_API_KEY"],
        entity_secret=os.environ["CIRCLE_ENTITY_SECRET"],
    )

    wallet_set = (
        dcw.WalletSetsApi(client)
        .create_wallet_set(
            dcw.CreateWalletSetRequest.from_dict({"name": "venice-payments-agent"}),
            _request_timeout=REQUEST_TIMEOUT,
        )
        .data.wallet_set.actual_instance
    )
    created = (
        dcw.WalletsApi(client)
        .create_wallet(
            dcw.CreateWalletRequest.from_dict(
                {
                    "walletSetId": wallet_set.id,
                    "blockchains": [BLOCKCHAIN],
                    "accountType": "EOA",
                    "count": 2,
                }
            ),
            _request_timeout=REQUEST_TIMEOUT,
        )
        .data.wallets
    )
    agent, alice = (w.actual_instance for w in created)

    if PAYEES.exists():
        print(f"{PAYEES} already exists, so Alice wasn't added. Her address is {alice.address}")
    else:
        PAYEES.write_text(
            "# People the agent is allowed to pay. The model only ever sees the names.\n\n"
            "[[payees]]\n"
            'name = "Alice"\n'
            f'address = "{alice.address}"\n'
            'note = "Test wallet created by setup_wallet.py"\n'
        )
        print(f"Wrote {PAYEES} with Alice at {alice.address}")

    print("\nAdd this line to .env:")
    print(f"  CIRCLE_WALLET_ID={agent.id}")
    print("\nThen fund the agent's wallet at https://faucet.circle.com (pick Arc Testnet):")
    print(f"  {agent.address}")


if __name__ == "__main__":
    main()
