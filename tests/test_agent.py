import copy
import json
from types import SimpleNamespace

import pytest
from openai.types.chat import ChatCompletion

import agent
from agent import run_turn


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        return self

    def json(self):
        return {"data": self.data}


@pytest.fixture
def fake_venice(monkeypatch):
    models = [
        {"id": "glm", "model_spec": {"privacy": "private", "capabilities": {"supportsFunctionCalling": True}}},
        {"id": "chat-only", "model_spec": {"privacy": "private", "capabilities": {"supportsFunctionCalling": False}}},
    ]

    def get(url, **kwargs):
        return FakeResponse({"function_calling_default": "glm"} if url.endswith("/traits") else models)

    monkeypatch.setattr(agent.httpx, "get", get)
    monkeypatch.delenv("VENICE_MODEL", raising=False)


def test_pick_model_defaults_to_venices_tool_calling_model(fake_venice):
    assert agent.pick_model("key") == ("glm", "private")


def test_a_misspelled_model_stops_the_agent_at_startup(fake_venice, monkeypatch):
    monkeypatch.setenv("VENICE_MODEL", "glm-typo")
    with pytest.raises(SystemExit, match="no text model called 'glm-typo'"):
        agent.pick_model("key")


def test_a_model_without_tool_calling_stops_the_agent_at_startup(fake_venice, monkeypatch):
    monkeypatch.setenv("VENICE_MODEL", "chat-only")
    with pytest.raises(SystemExit, match="doesn't support tool calling"):
        agent.pick_model("key")


def completion(message: dict) -> ChatCompletion:
    return ChatCompletion.model_validate(
        {
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "test-model",
            "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
        }
    )


def tool_call(call_id: str, name: str, arguments: dict) -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = SimpleNamespace(completions=self)

    def create(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs))
        return self.replies.pop(0)


class FakeTools:
    def __init__(self):
        self.calls = []

    def call(self, name, arguments):
        self.calls.append((name, json.loads(arguments)))
        return json.dumps({"ok": name})


def test_tool_results_follow_the_assistant_message_that_asked_for_them():
    client = FakeClient(
        [
            completion(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        tool_call("call_1", "list_payees", {}),
                        tool_call("call_2", "get_balance", {}),
                    ],
                }
            ),
            completion({"role": "assistant", "content": "Alice is the only payee."}),
        ]
    )
    tools = FakeTools()
    messages = [{"role": "user", "content": "who can I pay?"}]

    assert run_turn(client, "test-model", messages, tools) == "Alice is the only payee."
    assert tools.calls == [("list_payees", {}), ("get_balance", {})]

    second_request = client.requests[1]["messages"]
    assert [m["role"] for m in second_request] == ["user", "assistant", "tool", "tool"]
    assert second_request[1]["tool_calls"][0]["id"] == "call_1"
    assert [m["tool_call_id"] for m in second_request[2:]] == ["call_1", "call_2"]
    assert client.requests[0]["extra_body"]["venice_parameters"]["include_venice_system_prompt"] is False


def test_extra_fields_on_the_assistant_message_survive_the_round_trip():
    client = FakeClient(
        [
            completion(
                {
                    "role": "assistant",
                    "content": None,
                    "reasoning_content": "check the address book first",
                    "tool_calls": [tool_call("call_1", "list_payees", {})],
                }
            ),
            completion({"role": "assistant", "content": "done"}),
        ]
    )
    run_turn(client, "test-model", [{"role": "user", "content": "hi"}], FakeTools())
    assert client.requests[1]["messages"][1]["reasoning_content"] == "check the address book first"


def test_tool_calls_are_shown_without_control_characters(capsys):
    client = FakeClient(
        [
            completion(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call("call_1", "list_payees\x1b[2J\x1b[8m", {})],
                }
            ),
            completion({"role": "assistant", "content": "done"}),
        ]
    )
    run_turn(client, "test-model", [{"role": "user", "content": "hi"}], FakeTools())
    shown = capsys.readouterr().out
    assert "\x1b" not in shown
    assert "list_payees [2J [8m" in shown


def test_the_loop_gives_up_after_max_rounds():
    looping = completion(
        {"role": "assistant", "content": None, "tool_calls": [tool_call("call_1", "get_balance", {})]}
    )
    client = FakeClient([looping] * 3)
    reply = run_turn(client, "test-model", [{"role": "user", "content": "hi"}], FakeTools(), max_rounds=3)
    assert "stopped after 3 rounds" in reply
