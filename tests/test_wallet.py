import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from circle.web3.developer_controlled_wallets import TransactionState
from circle.web3.developer_controlled_wallets.exceptions import (
    BadRequestException,
    ServiceException,
)

import wallet as wallet_module
from wallet import TransferRejected, Wallet


def transaction(state, tx_hash=None):
    tx = SimpleNamespace(
        id="tx-1", state=TransactionState(state), tx_hash=tx_hash, network_fee="0.002", error_reason=None
    )
    return SimpleNamespace(data=SimpleNamespace(transaction=tx))


class FakeTransactions:
    def __init__(self, create_results, poll_results=()):
        self.create_results = list(create_results)
        self.poll_results = list(poll_results)
        self.requests = []
        self.timeouts = []

    def create_developer_transaction_transfer(self, request, _request_timeout=None):
        self.requests.append(request)
        self.timeouts.append(_request_timeout)
        result = self.create_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return SimpleNamespace(data=SimpleNamespace(id=result))

    def get_transaction(self, id, _request_timeout=None):
        self.timeouts.append(_request_timeout)
        result = self.poll_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FakeWallets:
    def __init__(self, balances):
        self.balances = list(balances)
        self.timeouts = []

    def get_wallet(self, id, _request_timeout=None):
        self.timeouts.append(_request_timeout)
        wallet = SimpleNamespace(actual_instance=SimpleNamespace(address="0x" + "1" * 40))
        return SimpleNamespace(data=SimpleNamespace(wallet=wallet))

    def list_wallet_balance(self, id, _request_timeout=None):
        self.timeouts.append(_request_timeout)
        return SimpleNamespace(data=SimpleNamespace(token_balances=self.balances))


def make_wallet(transactions=None, balances=()):
    wallet = Wallet.__new__(Wallet)
    wallet.wallet_id = "wallet-1"
    wallet.transactions = transactions
    wallet.wallets = FakeWallets(balances)
    return wallet


@pytest.fixture(autouse=True)
def no_sleeping(monkeypatch):
    monkeypatch.setattr(wallet_module.time, "sleep", lambda seconds: None)


def test_retries_reuse_the_idempotency_key_but_not_the_request():
    transactions = FakeTransactions([ServiceException(status=503, reason="Unavailable"), "tx-1"])
    wallet = make_wallet(transactions)

    assert wallet._create_transfer("0x" + "a" * 40, Decimal("2.5"), "pizza", "key-1") == "tx-1"
    first, second = transactions.requests
    assert first is not second
    body = json.loads(second.to_json())
    assert first.idempotency_key == body["idempotencyKey"] == "key-1"
    assert body["amounts"] == ["2.5"]
    assert body["refId"] == "pizza"
    assert body["blockchain"] == "ARC-TESTNET"
    assert body["tokenAddress"] == wallet_module.USDC_ADDRESS


def test_a_client_error_on_the_first_attempt_is_a_refusal_and_is_not_retried():
    transactions = FakeTransactions([BadRequestException(status=400, reason="Bad Request"), "tx-1"])
    with pytest.raises(TransferRejected, match=r"\(400\): Bad Request"):
        make_wallet(transactions)._create_transfer("0x" + "a" * 40, Decimal(1), "", "key-1")
    assert len(transactions.requests) == 1


def test_a_client_error_after_an_unclear_attempt_is_not_a_refusal():
    transactions = FakeTransactions(
        [ServiceException(status=503, reason="Unavailable"), BadRequestException(status=400, reason="Bad Request")]
    )
    with pytest.raises(BadRequestException):
        make_wallet(transactions)._create_transfer("0x" + "a" * 40, Decimal(1), "", "key-1")
    assert len(transactions.requests) == 2


def test_amounts_are_sent_without_scientific_notation():
    transactions = FakeTransactions(["tx-1"])
    make_wallet(transactions)._create_transfer("0x" + "a" * 40, Decimal("1E+1"), "", "key-1")
    assert transactions.requests[0].amounts == ["10"]


def test_wait_polls_through_errors_until_the_transfer_completes():
    transactions = FakeTransactions(
        [],
        [
            transaction("QUEUED"),
            ServiceException(status=502, reason="Bad Gateway"),
            transaction("COMPLETE", tx_hash="0xabc"),
        ],
    )
    receipt = make_wallet(transactions)._wait("tx-1")
    assert receipt.state == "COMPLETE"
    assert receipt.explorer_url == "https://explorer.testnet.arc.io/tx/0xabc"


def test_wait_reports_the_last_state_it_saw_when_it_times_out(monkeypatch):
    clock = iter([0, 0, 100])
    monkeypatch.setattr(wallet_module.time, "monotonic", lambda: next(clock))
    transactions = FakeTransactions([], [transaction("SENT")])
    assert make_wallet(transactions)._wait("tx-1", timeout=90).state == "SENT"


def test_usdc_balance_reads_the_usdc_row():
    balances = [
        SimpleNamespace(amount="3", token=SimpleNamespace(symbol="EURC")),
        SimpleNamespace(amount="17.25", token=SimpleNamespace(symbol="USDC")),
    ]
    assert make_wallet(balances=balances).usdc_balance() == Decimal("17.25")
    assert make_wallet(balances=[]).usdc_balance() == Decimal(0)


def test_usdc_balance_rounds_arcs_native_precision_down_to_what_can_be_sent():
    native = [SimpleNamespace(amount="17.9980390690676607", token=SimpleNamespace(symbol="USDC"))]
    assert f"{make_wallet(balances=native).usdc_balance():f}" == "17.998039"
    whole = [SimpleNamespace(amount="20", token=SimpleNamespace(symbol="USDC"))]
    assert f"{make_wallet(balances=whole).usdc_balance():f}" == "20"


def test_every_circle_call_has_a_timeout():
    transactions = FakeTransactions(["tx-1"], [transaction("COMPLETE", tx_hash="0xabc")])
    wallet = make_wallet(transactions)
    wallet.address()
    wallet.usdc_balance()
    wallet.send_usdc("0x" + "a" * 40, Decimal(1), "", "key-1")
    assert wallet.wallets.timeouts == transactions.timeouts == [wallet_module.REQUEST_TIMEOUT] * 2
