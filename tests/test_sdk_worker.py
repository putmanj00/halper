"""Offline tests: permission hook/callback logic and option construction. No live calls."""

import asyncio

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from halper.sdk_worker import (
    DEFAULT_TOOLS,
    build_options,
    check_call,
    make_can_use_tool,
    make_hook,
)


def run(coro):
    return asyncio.run(coro)


def hook_decision(tool, tool_input, approve=None):
    hook = make_hook(approve) if approve else make_hook()
    out = run(hook({"tool_name": tool, "tool_input": tool_input}, "id1", None))
    spec = out["hookSpecificOutput"]
    assert spec["hookEventName"] == "PreToolUse"
    return spec["permissionDecision"], spec["permissionDecisionReason"]


def test_hook_allows_read_tier():
    assert hook_decision("Read", {"file_path": "a.txt"})[0] == "allow"
    assert hook_decision("Bash", {"command": "ls -la"})[0] == "allow"


def test_hook_denies_destructive_with_reason():
    decision, reason = hook_decision("Bash", {"command": "echo hi | xargs rm -rf build"})
    assert decision == "deny"
    assert "destructive" in reason and "blocked" in reason


def test_hook_refuses_ask_tier_by_default():
    decision, reason = hook_decision("Bash", {"command": "git push origin main"})
    assert decision == "deny"
    assert "approval required" in reason


def test_hook_ask_tier_approved_by_sync_and_async_callback():
    seen = []

    def approve(name, tool_input, verdict):
        seen.append((name, verdict.tier.name))
        return True

    async def approve_async(name, tool_input, verdict):
        return True

    cmd = {"command": "git push origin main"}
    assert hook_decision("Bash", cmd, approve)[0] == "allow"
    assert seen == [("Bash", "HIGH_WRITE")]
    assert hook_decision("Bash", cmd, approve_async)[0] == "allow"


def test_approver_not_consulted_for_deny_tier():
    calls = []
    allowed, _ = run(
        check_call("Bash", {"command": "rm -rf /tmp/x"}, lambda *a: calls.append(a) or True)
    )
    assert not allowed and calls == []


def test_can_use_tool_results():
    cb = make_can_use_tool()
    ok = run(cb("Read", {"file_path": "a.txt"}, None))
    assert isinstance(ok, PermissionResultAllow) and ok.updated_input == {"file_path": "a.txt"}
    bad = run(cb("Bash", {"command": "rm -rf x"}, None))
    assert isinstance(bad, PermissionResultDeny) and "blocked by policy" in bad.message
    ask = run(cb("Bash", {"command": "git push origin main"}, None))
    assert isinstance(ask, PermissionResultDeny)
    approved = run(make_can_use_tool(lambda *a: True)("Bash", {"command": "git push"}, None))
    assert isinstance(approved, PermissionResultAllow)


def test_unknown_tool_is_not_silently_allowed():
    assert hook_decision("SomeNewTool", {})[0] == "deny"


def test_build_options():
    opts = build_options("/work/dir", model="m-test", max_turns=3)
    assert opts.tools == DEFAULT_TOOLS
    assert opts.allowed_tools == []
    assert str(opts.cwd) == "/work/dir"
    assert opts.model == "m-test" and opts.max_turns == 3
    assert opts.permission_mode == "default"
    assert callable(opts.can_use_tool)
    matchers = opts.hooks["PreToolUse"]
    assert len(matchers) == 1 and len(matchers[0].hooks) == 1
    assert matchers[0].on_failure == "block"  # a crashing guard must block, not pass
    assert opts.setting_sources == []


def test_build_options_custom_tools_and_default_model():
    opts = build_options(".", tools=["Read"])
    assert opts.tools == ["Read"]
    assert opts.model.startswith("claude-")
