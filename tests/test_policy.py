from decimal import Decimal
from pathlib import Path

import pytest

from policy import Payee, Policy, PolicyError, load_payees, parse_usdc

ALICE = Payee("Alice", "0x" + "a" * 40, "friend")


def write_payees(tmp_path, *entries) -> Path:
    path = tmp_path / "payees.toml"
    path.write_text("".join(f'[[payees]]\nname = "{name}"\naddress = "{address}"\n' for name, address in entries))
    return path


def make_policy(**overrides) -> Policy:
    settings = {
        "payees": {"alice": ALICE},
        "max_payment": Decimal(5),
        "session_budget": Decimal(10),
    }
    return Policy(**(settings | overrides))


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2", "2"),
        ("2.50", "2.5"),
        (" 0.000001 ", "0.000001"),
        (3, "3"),
        (1.5, "1.5"),
        ("1.5000000", "1.5"),
        ("1E+1", "10"),
    ],
)
def test_parse_usdc_accepts_amounts_and_writes_them_the_way_circle_gets_them(raw, expected):
    assert f"{parse_usdc(raw):f}" == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "abc",
        "2 USDC",
        "$2",
        "0",
        "-1",
        "NaN",
        "sNaN",
        "Infinity",
        "0.0000001",
        "4.0000000000000000000000000001",
        "1" * 30,
        True,
    ],
)
def test_parse_usdc_rejects_everything_else(raw):
    with pytest.raises(PolicyError):
        parse_usdc(raw)


def test_payee_lookup_ignores_case_and_whitespace():
    payee, amount = make_policy().check("  ALICE ", "1")
    assert payee == ALICE
    assert amount == Decimal(1)


def test_unknown_payee_is_blocked_and_lists_who_is_allowed():
    with pytest.raises(PolicyError, match="payees: Alice"):
        make_policy().check("0x" + "b" * 40, "1")


def test_payment_over_the_cap_is_blocked():
    with pytest.raises(PolicyError, match="limit per payment"):
        make_policy().check("alice", "5.000001")


def test_session_budget_runs_out():
    policy = make_policy()
    policy.record(Decimal(8))
    with pytest.raises(PolicyError, match="2 USDC left"):
        policy.check("alice", "3")
    assert policy.check("alice", "2")[1] == Decimal(2)


def test_load_payees(tmp_path):
    path = tmp_path / "payees.toml"
    path.write_text(f'[[payees]]\nname = "Alice"\naddress = "{ALICE.address}"\nnote = "friend"\n')
    assert load_payees(path) == {"alice": ALICE}


def test_load_payees_strips_names(tmp_path):
    policy = make_policy(payees=load_payees(write_payees(tmp_path, ("  Bob ", "0x" + "b" * 40))))
    assert policy.check("bob", "1")[0].name == "Bob"


@pytest.mark.parametrize(
    "address",
    ["0x1234", "0x" + "g" * 40, " 0x" + "a" * 40, "0x" + "a" * 40 + "\\n"],
    ids=["too short", "not hex", "leading space", "trailing newline"],
)
def test_load_payees_rejects_bad_addresses(tmp_path, address):
    with pytest.raises(ValueError, match="Bob has an invalid address"):
        load_payees(write_payees(tmp_path, ("Bob", address)))


def test_load_payees_rejects_the_zero_address(tmp_path):
    with pytest.raises(ValueError, match="Bob has the zero address"):
        load_payees(write_payees(tmp_path, ("Bob", "0x" + "0" * 40)))


def test_the_example_address_book_has_to_be_edited_before_use():
    with pytest.raises(ValueError, match="Alice has the zero address"):
        load_payees(Path(__file__).parent.parent / "payees.example.toml")


def test_load_payees_rejects_names_that_only_differ_by_case(tmp_path):
    path = write_payees(tmp_path, ("Alice", "0x" + "a" * 40), ("alice", "0x" + "b" * 40))
    with pytest.raises(ValueError, match="more than one payee called 'alice'"):
        load_payees(path)


def test_load_payees_rejects_blank_names(tmp_path):
    with pytest.raises(ValueError, match="has no name"):
        load_payees(write_payees(tmp_path, ("   ", "0x" + "a" * 40)))
