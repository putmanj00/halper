"""Claude Agent SDK worker whose every tool call goes through the impact-tier guardrail.

Enforcement point: a PreToolUse hook. Per the SDK permission docs, hooks run first and
also run for calls that an allow rule or the permission mode would auto-approve, whereas
`can_use_tool` only sees calls that fall through to a prompt. The same decision function
is also installed as `can_use_tool` as a backstop for anything that reaches a prompt.

Policy: allow -> proceed; deny -> refused with a reason; ask -> refused unless the
caller-supplied `approve` callback returns True.

Run: `uv run python -m halper.sdk_worker "task"` (needs ANTHROPIC_API_KEY).
"""

from __future__ import annotations

import asyncio
import inspect
import sys
from collections.abc import Awaitable, Callable
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    query,
)

from halper.config import load_settings
from halper.guardrail import Decision, Verdict, evaluate

DEFAULT_TOOLS = ["Read", "Glob", "Grep", "Write", "Edit", "Bash"]

Approver = Callable[[str, dict[str, Any], Verdict], bool | Awaitable[bool]]


def deny_all(tool_name: str, tool_input: dict[str, Any], verdict: Verdict) -> bool:
    """Default approver: never approve ask-tier calls (safe for unattended runs)."""
    return False


async def check_call(
    tool_name: str, tool_input: dict[str, Any], approve: Approver = deny_all
) -> tuple[bool, str]:
    """Return (allowed, reason) for one tool call."""
    verdict = evaluate(tool_name, tool_input)
    if verdict.decision is Decision.ALLOW:
        return True, verdict.reason
    if verdict.decision is Decision.ASK:
        ok = approve(tool_name, tool_input, verdict)
        if inspect.isawaitable(ok):
            ok = await ok
        if ok:
            return True, f"{verdict.reason} (approved by caller)"
        return False, f"{verdict.reason}: approval required and not granted"
    return False, f"{verdict.reason}: blocked by policy"


def make_hook(approve: Approver = deny_all):
    """PreToolUse hook callback: returns a permissionDecision for every tool call."""

    async def pre_tool_use(input_data, tool_use_id, context):
        allowed, reason = await check_call(
            input_data["tool_name"], input_data.get("tool_input") or {}, approve
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow" if allowed else "deny",
                "permissionDecisionReason": reason,
            }
        }

    return pre_tool_use


def make_can_use_tool(approve: Approver = deny_all):
    """`can_use_tool` backstop with the same decision logic as the hook."""

    async def can_use_tool(tool_name, tool_input, context):
        allowed, reason = await check_call(tool_name, tool_input, approve)
        if allowed:
            return PermissionResultAllow(updated_input=tool_input)
        return PermissionResultDeny(message=reason)

    return can_use_tool


def build_options(
    cwd: str,
    approve: Approver = deny_all,
    tools: list[str] | None = None,
    model: str | None = None,
    max_turns: int | None = None,
) -> ClaudeAgentOptions:
    """Options: restricted built-in tool set, fixed cwd, guardrail hook + callback."""
    s = load_settings()
    return ClaudeAgentOptions(
        tools=list(tools if tools is not None else DEFAULT_TOOLS),
        allowed_tools=[],  # nothing pre-approved: the guardrail decides each call
        cwd=cwd,
        model=model or s.model,
        max_turns=max_turns or s.max_turns,
        permission_mode="default",
        # Ignore user/project/local settings files so no allow rule, hook or default mode
        # from the working directory can change how this worker behaves.
        setting_sources=[],
        can_use_tool=make_can_use_tool(approve),
        # on_failure="block": by default a hook that raises lets the call continue to the
        # normal permission flow; a guard must fail closed instead.
        hooks={"PreToolUse": [HookMatcher(hooks=[make_hook(approve)], on_failure="block")]},
    )


async def run_task(task: str, cwd: str, approve: Approver = deny_all, **kw: Any) -> dict[str, Any]:
    """Run one task; return final text, cost and usage from the ResultMessage."""
    text: list[str] = []
    out: dict[str, Any] = {"text": "", "cost_usd": None, "usage": None, "is_error": False}
    async for msg in query(prompt=task, options=build_options(cwd, approve, **kw)):
        if isinstance(msg, AssistantMessage):
            text += [b.text for b in msg.content if isinstance(b, TextBlock)]
        elif isinstance(msg, ResultMessage):
            out.update(cost_usd=msg.total_cost_usd, usage=msg.usage, is_error=msg.is_error)
    out["text"] = "\n".join(text)
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print('usage: python -m halper.sdk_worker "task" [cwd]', file=sys.stderr)
        return 2
    result = asyncio.run(run_task(args[0], args[1] if len(args) > 1 else "."))
    print(result["text"])
    print(f"cost_usd={result['cost_usd']} usage={result['usage']}", file=sys.stderr)
    return 1 if result["is_error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
