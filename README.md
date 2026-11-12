# HALPER

HALPER: Heuristically programmed ALgorithmic computer Protocol Execution Relay.

A small reference implementation of four pieces of an LLM agent stack: a Claude Messages API tool-use loop with prompt caching and token accounting, a deterministic impact-tier guardrail for tool calls, an MCP server, and a Claude Agent SDK worker. The guardrail classifies every tool call into one of four tiers and applies an allow, ask or deny policy before the call runs, in the tool loop, in the MCP server's write path, and in the Agent SDK worker. A 42-case offline eval measures the classifier.

## Architecture

```mermaid
flowchart LR
    subgraph Loop["client.py: ToolLoop (Messages API)"]
        M[Claude model] -->|tool_use| G1{guardrail}
        G1 -->|allow, or ask and approved| H[tool handlers]
        G1 -->|deny, or ask not approved| E[error tool_result]
        H -->|tool_result| M
        E -->|tool_result| M
    end
    subgraph MCP["mcp_server.py: MCPServer, stdio"]
        T[search / read / append tools] --> P[path confinement] --> G2{guardrail on appends} --> F[(notes directory)]
    end
    subgraph SDK["sdk_worker.py: Agent SDK query()"]
        S[Claude Code runtime] -->|PreToolUse hook| G3{guardrail}
        S -.->|can_use_tool backstop| G3
    end
    G1 & G2 & G3 --- R[guardrail.py: classify then policy]
```

- `guardrail.py` is pure functions: no I/O, no model call.
- `client.py` runs the loop with a bounded turn count, retry with backoff, `cache_control` on the system prompt and the last tool definition, and totals for input, output, cache-write and cache-read tokens.
- `mcp_server.py` exposes `search_notes`, `read_note`, `append_note` and a `notes://index` resource over a notes directory.
- `sdk_worker.py` runs a task with a restricted built-in tool set and a guardrail hook.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python 3.12 or newer.

```sh
uv sync
uv run pytest -q              # offline, no API key needed
uv run python -m evals.run    # guardrail eval, prints accuracy and misses
uv run python -m halper.mcp_server   # MCP server over stdio
```

Run the Agent SDK worker (needs an API key and spends tokens):

```sh
cp .env.example .env          # then set ANTHROPIC_API_KEY, or export it in your shell
export ANTHROPIC_API_KEY=...
uv run python -m halper.sdk_worker "List the markdown files here" .
```

The MCP server reads notes from `AGENT_NOTES_DIR` (default `sample_notes`, synthetic examples). `ToolLoop` is a library class with no command-line entry point; see `tests/test_client.py` for usage. Settings and their environment variables are in `src/halper/config.py`.

## Impact tiers and default policy

| Tier | Meaning | Examples | Default decision |
|------|---------|----------|------------------|
| read | No side effects | `ls`, `cat`, `git status`, `Read`, `search_*` | allow |
| low-write | Local, easily reversed | `touch`, `mkdir`, `Write` to a safe path, `append_*` | allow |
| high-write | External or hard to undo, or unrecognised | `git push`, `docker run`, `sudo ...`, unknown tools, writes to `.env` or `/etc` | ask |
| destructive | Data loss | `rm`, `git reset --hard`, `DROP TABLE` via a SQL client, `delete_*`, `git push --force` | deny |

Anything the classifier does not recognise is high-write. "Ask" means a caller-supplied approval callback must return true; without one, the call is refused. A missing tier in a custom policy is denied.

## Eval result

`evals/cases.jsonl` holds 42 synthetic tool calls labelled by tier (21 adversarial, such as `rm` inside a pipeline or a write via redirect). Labels were written before the first run.

| Run | Accuracy | Unsafe misses | Over-cautious misses |
|-----|----------|---------------|----------------------|
| Baseline | 33/42 = 78.6% | 5 | 4 |
| After fixes | 40/42 = 95.2% | 0 | 2 |

The after-fix number is not held-out: the classifier was changed after seeing these same 42 cases, so 95.2% is not a measure of real-world accuracy. Unit tests in `tests/test_guardrail.py` cover the new rules on inputs outside the eval file.

Remaining misses, both over-cautious (rated riskier than labelled):

- `curl -s https://status.example.test/health` is rated high-write, labelled read. Left on purpose, since a GET can carry data in the URL.
- `remove_label` is rated destructive, labelled high-write. An earlier rule that downgraded names containing "label" was removed because it would also under-rate `delete_label_and_repo`.

Full output, confusion matrices and the baseline miss list are in `evals/RESULTS.md`.

## Security notes

- The MCP server resolves every path and requires it to stay inside the notes directory (symlinks are followed before the check). Only `.md` files are touched, and reads, appends and total note size are size-limited.
- The Agent SDK worker enforces the guardrail in a `PreToolUse` hook, which runs before allow rules and permission modes. The hook is registered with `on_failure="block"` so a crashing hook blocks the call instead of passing it through. `setting_sources=[]` stops user, project and local settings files from adding rules or hooks. `can_use_tool` repeats the same decision as a backstop.
- The tool loop wraps tool exceptions into error results and never runs a handler for a denied call.
- Secrets come only from environment variables; `.env` is git-ignored and CI runs gitleaks.

## Limits

- The classifier is heuristic. It matches shell text with tables and regular expressions rather than a real shell parser, so it can be fooled by constructs it does not model (for example scripts in files, shell functions, or compound keywords such as `then ... rm`). Unrecognised input is rated high-write, which fails towards asking, not towards allowing. Path checks cover a short list of sensitive locations, not an allow-list of directories.
- The guardrail checks the call, not intent or data. It is not a sandbox and does not replace OS-level isolation, least-privilege credentials, or review of what an agent may touch.
- In the MCP server the default policy allows every in-root append, because appends rate as low-write. The guardrail call there is a hook for a stricter policy, not a restriction by default.
- Nothing here has been run against the live Anthropic API or the Claude Agent SDK runtime. Tests use fakes, and the request shapes, hook output and options were checked against the current documentation and the installed SDK types only. The MCP server was started over stdio but is not covered by an in-process stdio test.
- Prompt caching only applies above the model's minimum cacheable length; shorter prompts run uncached without error, so a small system prompt will show zero cache tokens.
- Cost is reported as tokens in the tool loop. Dollar cost is available only from the SDK worker's result message.
- This is a small reference implementation, about 770 lines of source. It has no persistence, concurrency control or multi-provider support.

The MCP server uses the MCP Python SDK (`MCPServer`, formerly FastMCP), version 2.

## License

MIT, see `LICENSE`.
