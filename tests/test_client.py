from types import SimpleNamespace as NS

import anthropic
import httpx2 as httpx
import pytest

from halper.client import ToolLoop
from halper.config import Settings

TOOLS = [
    {"name": "read_note", "description": "Read a note", "input_schema": {"type": "object"}},
    {"name": "delete_note", "description": "Delete a note", "input_schema": {"type": "object"}},
]


def usage(i=10, o=5, cw=0, cr=0):
    return NS(
        input_tokens=i, output_tokens=o, cache_creation_input_tokens=cw, cache_read_input_tokens=cr
    )


def tool_use(id_, tool, **inp):
    return NS(type="tool_use", id=id_, name=tool, input=inp)


def reply(blocks, stop, u=None):
    return NS(content=blocks, stop_reason=stop, usage=u or usage())


def text(s):
    return NS(type="text", text=s)


class FakeClient:
    """Plays back scripted responses (or raises scripted exceptions) and records requests."""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.messages = self

    def create(self, **request):
        self.requests.append({**request, "messages": list(request["messages"])})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def status_error(cls, code):
    req = httpx.Request("POST", "https://example.test/v1/messages")
    return cls("boom", response=httpx.Response(code, request=req), body=None)


def loop(script, **settings):
    sleeps = []
    fake = FakeClient(script)
    return ToolLoop(fake, Settings(**settings), sleep=sleeps.append), fake, sleeps


def test_multi_turn_tool_loop_and_usage_totals():
    tl, fake, _ = loop(
        [
            reply(
                [text("checking"), tool_use("t1", "read_note", name="a")],
                "tool_use",
                usage(i=100, o=10, cw=500),
            ),
            reply([tool_use("t2", "read_note", name="b")], "tool_use", usage(i=20, o=8, cr=500)),
            reply([text("done")], "end_turn", usage(i=30, o=4, cr=500)),
        ]
    )
    seen = []
    handlers = {"read_note": lambda args: seen.append(args["name"]) or f"body-{args['name']}"}
    result = tl.run("go", system="sys", tools=TOOLS, handlers=handlers)

    assert result.text == "done" and result.stop_reason == "end_turn" and result.turns == 3
    assert seen == ["a", "b"]
    last_user = fake.requests[1]["messages"][-1]
    assert last_user["role"] == "user"
    assert last_user["content"] == [
        {"type": "tool_result", "tool_use_id": "t1", "content": "body-a"}
    ]
    assert result.usage.input_tokens == 150
    assert result.usage.output_tokens == 22
    assert result.usage.cache_creation_input_tokens == 500
    assert result.usage.cache_read_input_tokens == 1000
    assert result.usage.total_input_tokens == 1650


def test_cache_control_on_system_and_last_tool_only():
    tl, fake, _ = loop([reply([text("hi")], "end_turn")])
    tl.run("go", system="sys", tools=TOOLS, handlers={})
    req = fake.requests[0]
    assert req["system"] == [
        {"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}
    ]
    assert "cache_control" not in req["tools"][0]
    assert req["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert "cache_control" not in TOOLS[-1]  # caller's definitions are not mutated


def test_max_turns_stops_a_looping_model():
    script = [reply([tool_use(f"t{i}", "read_note", name="x")], "tool_use") for i in range(5)]
    tl, fake, _ = loop(script, max_turns=3)
    result = tl.run("go", system="s", tools=TOOLS, handlers={"read_note": lambda a: "ok"})
    assert result.stop_reason == "max_turns" and result.turns == 3
    assert len(fake.requests) == 3


def test_retries_rate_limit_then_succeeds():
    err = status_error(anthropic.RateLimitError, 429)
    over = status_error(anthropic.InternalServerError, 529)
    tl, fake, sleeps = loop([err, over, reply([text("ok")], "end_turn")])
    result = tl.run("go", system="s")
    assert result.text == "ok" and len(fake.requests) == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] - 1  # backoff grows (plus jitter)


def test_retry_gives_up_and_non_retryable_raises_immediately():
    errs = [status_error(anthropic.RateLimitError, 429) for _ in range(3)]
    tl, _, sleeps = loop(errs, max_retries=2)
    with pytest.raises(anthropic.RateLimitError):
        tl.run("go", system="s")
    assert len(sleeps) == 2

    tl, fake, sleeps = loop([status_error(anthropic.BadRequestError, 400)])
    with pytest.raises(anthropic.BadRequestError):
        tl.run("go", system="s")
    assert sleeps == [] and len(fake.requests) == 1


def test_guardrail_blocks_destructive_and_asks_for_high_write():
    script = [
        reply([tool_use("t1", "Bash", command="rm -rf /tmp/x")], "tool_use"),
        reply([tool_use("t2", "Bash", command="curl https://example.test")], "tool_use"),
        reply([text("done")], "end_turn"),
    ]
    tl, fake, _ = loop(script)
    ran = []
    handlers = {"Bash": lambda a: ran.append(a["command"]) or "ran"}
    bash_tool = [{"name": "Bash", "input_schema": {}}]
    result = tl.run("go", system="s", tools=bash_tool, handlers=handlers)
    assert ran == []  # deny never runs; ask without an approver does not run
    blocked = [
        m["content"][0]
        for m in result.messages
        if m["role"] == "user" and isinstance(m["content"], list)
    ]
    assert all(b["is_error"] for b in blocked) and len(blocked) == 2

    curl = tool_use("t", "Bash", command="curl https://example.test")
    tl, _, _ = loop([reply([curl], "tool_use"), reply([text("ok")], "end_turn")])
    tl.run("go", system="s", handlers=handlers, approve=lambda n, i, v: True)
    assert ran == ["curl https://example.test"]


def test_handler_error_and_unknown_tool_become_error_results():
    def boom(args):
        raise ValueError("bad input")

    script = [
        reply([tool_use("t1", "read_note"), tool_use("t2", "mystery_read")], "tool_use"),
        reply([text("ok")], "end_turn"),
    ]
    tl, fake, _ = loop(script)
    tl.run("go", system="s", handlers={"read_note": boom})
    results = fake.requests[1]["messages"][-1]["content"]
    assert results[0]["is_error"] and "ValueError: bad input" in results[0]["content"]
    assert results[1]["is_error"] and "Unknown tool" in results[1]["content"]


def test_connection_errors_and_timeouts_are_retried():
    req = httpx.Request("POST", "https://example.test/v1/messages")
    script = [
        anthropic.APIConnectionError(request=req),
        anthropic.APITimeoutError(request=req),
        reply([text("ok")], "end_turn"),
    ]
    tl, fake, sleeps = loop(script)
    assert tl.run("go", system="s").text == "ok"
    assert len(fake.requests) == 3 and len(sleeps) == 2
