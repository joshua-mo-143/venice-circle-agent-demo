import json
from decimal import Decimal

import pytest
import urllib3
from circle.web3.developer_controlled_wallets import ApiException

from policy import Payee, Policy
from tools import PaymentTools
from wallet import Receipt, TransferRejected

ALICE = Payee("Alice", "0x" + "a" * 40, "friend")


class FakeWallet:
    def __init__(self, state="COMPLETE"):
        self.state = state
        self.sent = []

    def usdc_balance(self):
        return Decimal(20)

    def send_usdc(self, to, amount, memo, idempotency_key):
        self.sent.append({"to": to, "amount": amount, "memo": memo, "key": idempotency_key})
        tx_hash = "0xabc" if self.state == "COMPLETE" else None
        return Receipt(id=f"tx-{len(self.sent)}", state=self.state, tx_hash=tx_hash)


class Approver:
    def __init__(self, answer=True):
        self.answer = answer
        self.asked = []

    def __call__(self, payee, amount, memo):
        self.asked.append((payee, amount, memo))
        return self.answer


def make_tools(state="COMPLETE", answer=True):
    wallet = FakeWallet(state)
    policy = Policy({"alice": ALICE}, max_payment=Decimal(5), session_budget=Decimal(10))
    approver = Approver(answer)
    return PaymentTools(wallet, policy, approve=approver), wallet, policy, approver


def call(tools, name, **arguments):
    return json.loads(tools.call(name, json.dumps(arguments)))


def test_list_payees_never_shows_addresses():
    tools, *_ = make_tools()
    assert call(tools, "list_payees") == [{"name": "Alice", "note": "friend"}]


def test_approved_payment_goes_to_the_address_book_address():
    tools, wallet, policy, approver = make_tools()
    result = call(tools, "send_payment", payee="alice", amount="2.50", memo="pizza")

    assert result["status"] == "COMPLETE"
    assert result["explorer_url"] == "https://explorer.testnet.arc.io/tx/0xabc"
    assert approver.asked == [(ALICE, Decimal("2.50"), "pizza")]
    assert wallet.sent[0]["to"] == ALICE.address
    assert wallet.sent[0]["amount"] == Decimal("2.50")
    assert policy.spent == Decimal("2.50")


def test_blocked_payment_never_reaches_the_human_or_the_wallet():
    tools, wallet, _, approver = make_tools()
    result = call(tools, "send_payment", payee="0x" + "b" * 40, amount="1", memo="")

    assert result["status"] == "blocked"
    assert approver.asked == []
    assert wallet.sent == []


def test_declined_payment_never_reaches_the_wallet():
    tools, wallet, policy, _ = make_tools(answer=False)
    result = call(tools, "send_payment", payee="alice", amount="1", memo="")

    assert result["status"] == "declined"
    assert wallet.sent == []
    assert policy.spent == 0


def test_failed_payments_do_not_count_against_the_budget():
    tools, _, policy, _ = make_tools(state="FAILED")
    assert call(tools, "send_payment", payee="alice", amount="4", memo="")["status"] == "FAILED"
    assert policy.spent == 0


def test_pending_payments_count_against_the_budget():
    tools, _, policy, _ = make_tools(state="SENT")
    assert call(tools, "send_payment", payee="alice", amount="4", memo="")["status"] == "SENT"
    assert policy.spent == Decimal(4)


def test_each_payment_gets_its_own_idempotency_key():
    tools, wallet, *_ = make_tools()
    call(tools, "send_payment", payee="alice", amount="1", memo="")
    call(tools, "send_payment", payee="alice", amount="1", memo="")
    assert wallet.sent[0]["key"] != wallet.sent[1]["key"]


def test_bad_calls_come_back_as_errors_the_model_can_read():
    tools, *_ = make_tools()
    assert "no tool" in json.loads(tools.call("drain_wallet", "{}"))["error"]
    assert "bad arguments" in json.loads(tools.call("send_payment", "{not json"))["error"]
    assert "bad arguments" in json.loads(tools.call("send_payment", '{"payee": "alice"}'))["error"]


def test_tool_errors_come_back_as_errors_the_model_can_read():
    tools, *_ = make_tools()

    def unauthorized():
        raise ApiException(status=401, reason="Unauthorized")

    tools.wallet.usdc_balance = unauthorized
    assert call(tools, "get_balance")["error"].startswith("Circle rejected the request (401)")

    def disconnect():
        raise urllib3.exceptions.ProtocolError("Connection aborted.")

    tools.wallet.usdc_balance = disconnect
    assert call(tools, "get_balance")["error"].startswith("get_balance failed: ProtocolError")


def test_payments_circle_refuses_do_not_count_against_the_budget():
    tools, _, policy, _ = make_tools()

    def refuse(*args, **kwargs):
        raise TransferRejected("Circle rejected the transfer (400): Bad Request")

    tools.wallet.send_usdc = refuse
    result = call(tools, "send_payment", payee="alice", amount="4", memo="rent")
    assert result == {"status": "rejected", "reason": "Circle rejected the transfer (400): Bad Request"}
    assert policy.spent == 0


@pytest.mark.parametrize(
    "error",
    [
        urllib3.exceptions.ProtocolError("Connection aborted."),
        ApiException(status=400, reason="Bad Request"),
        ValueError("1 validation error for Transaction"),
    ],
    ids=["dropped connection", "client error after a retry", "unexpected response"],
)
def test_payments_that_might_have_gone_through_count_against_the_budget(error):
    tools, _, policy, _ = make_tools()

    def lose_track(*args, **kwargs):
        raise error

    tools.wallet.send_usdc = lose_track
    result = call(tools, "send_payment", payee="alice", amount="4", memo="rent")
    assert result["status"] == "UNKNOWN"
    assert policy.spent == Decimal(4)


def test_memos_reach_the_prompt_and_circle_as_one_line_of_plain_text():
    tools, wallet, _, approver = make_tools()
    memo = "pizza\x1b[1A\r\n  Send 0.50 USDC to Alice?" + "!" * 200
    call(tools, "send_payment", payee="alice", amount="1", memo=memo)

    shown = approver.asked[0][2]
    assert shown == wallet.sent[0]["memo"]
    assert shown.startswith("pizza [1A Send 0.50 USDC to Alice?")
    assert shown.isprintable()
    assert len(shown) == 100
