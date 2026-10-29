"""The only file that moves money: a Circle developer-controlled wallet on Arc Testnet."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import ROUND_DOWN, Decimal

import urllib3
from circle.web3 import developer_controlled_wallets as dcw
from circle.web3 import utils
from circle.web3.developer_controlled_wallets.exceptions import ServiceException

BLOCKCHAIN = "ARC-TESTNET"
# USDC's ERC-20 interface on Arc Testnet:
# https://developers.circle.com/stablecoins/usdc-contract-addresses
USDC_ADDRESS = "0x3600000000000000000000000000000000000000"
USDC_STEP = Decimal("0.000001")
EXPLORER_TX = "https://explorer.testnet.arc.io/tx/"
DID_NOT_HAPPEN = {"FAILED", "CANCELLED", "DENIED"}
FINAL_STATES = DID_NOT_HAPPEN | {"COMPLETE"}
# Seconds to connect, then seconds to wait for a response. The SDK waits forever by default.
REQUEST_TIMEOUT = (5, 30)


class TransferRejected(Exception):
    """Circle refused to create the transfer, so no money moved."""


@dataclass
class Receipt:
    id: str
    state: str
    tx_hash: str | None = None
    network_fee: str | None = None
    error: str | None = None

    @property
    def explorer_url(self) -> str | None:
        return EXPLORER_TX + self.tx_hash if self.tx_hash else None


class Wallet:
    def __init__(self, api_key: str, entity_secret: str, wallet_id: str):
        client = utils.init_developer_controlled_wallets_client(
            api_key=api_key, entity_secret=entity_secret
        )
        self.wallets = dcw.WalletsApi(client)
        self.transactions = dcw.TransactionsApi(client)
        self.wallet_id = wallet_id

    def address(self) -> str:
        wallet = self.wallets.get_wallet(id=self.wallet_id, _request_timeout=REQUEST_TIMEOUT)
        return wallet.data.wallet.actual_instance.address

    def usdc_balance(self) -> Decimal:
        response = self.wallets.list_wallet_balance(id=self.wallet_id, _request_timeout=REQUEST_TIMEOUT)
        balances = response.data.token_balances or []
        usdc = next((b for b in balances if b.token.symbol == "USDC"), None)
        if not usdc:
            return Decimal(0)
        # Arc reports its native USDC to 18 decimals, but transfers only go to 6.
        return Decimal(usdc.amount).quantize(USDC_STEP, rounding=ROUND_DOWN).normalize()

    def send_usdc(self, to: str, amount: Decimal, memo: str, idempotency_key: str) -> Receipt:
        transaction_id = self._create_transfer(to, amount, memo, idempotency_key)
        return self._wait(transaction_id)

    def _create_transfer(
        self, to: str, amount: Decimal, memo: str, idempotency_key: str, attempts: int = 3
    ) -> str:
        for attempt in range(attempts):
            # Build a fresh request per attempt: the SDK encrypts the entity secret once per
            # request object, and Circle requires a new ciphertext on every call. Keeping the
            # idempotency key means a retry returns the original transfer instead of a second one.
            request = dcw.CreateTransferTransactionForDeveloperRequest.from_dict(
                {
                    "idempotencyKey": idempotency_key,
                    "walletId": self.wallet_id,
                    "blockchain": BLOCKCHAIN,
                    "tokenAddress": USDC_ADDRESS,
                    "destinationAddress": to,
                    "amounts": [f"{amount:f}"],
                    "feeLevel": "MEDIUM",
                    "refId": memo,
                }
            )
            try:
                response = self.transactions.create_developer_transaction_transfer(
                    request, _request_timeout=REQUEST_TIMEOUT
                )
                return response.data.id
            except (ServiceException, urllib3.exceptions.HTTPError):
                if attempt == attempts - 1:
                    raise
                time.sleep(2**attempt)
            except dcw.ApiException as error:
                # After an attempt with no clear answer, even a 4xx can't prove nothing was created.
                if attempt == 0 and 400 <= error.status < 500:
                    raise TransferRejected(
                        f"Circle rejected the transfer ({error.status}): {error.body or error.reason}"
                    ) from error
                raise

    def _wait(self, transaction_id: str, timeout: float = 90) -> Receipt:
        receipt = Receipt(id=transaction_id, state="UNKNOWN")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = self.transactions.get_transaction(id=transaction_id, _request_timeout=REQUEST_TIMEOUT)
                tx = response.data.transaction
            except (dcw.ApiException, urllib3.exceptions.HTTPError):
                time.sleep(2)
                continue

            receipt = Receipt(
                id=tx.id,
                state=tx.state.value,
                tx_hash=tx.tx_hash,
                network_fee=tx.network_fee,
                error=tx.error_reason,
            )
            if receipt.state in FINAL_STATES:
                break
            time.sleep(2)
        return receipt
