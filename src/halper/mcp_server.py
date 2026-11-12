"""MCP server exposing a small notes directory, with writes gated by the guardrail policy.

Run over stdio:  uv run python -m halper.mcp_server
The notes directory comes from AGENT_NOTES_DIR (default: sample_notes).

Security: every path is resolved and must stay inside the notes directory; only `.md`
files are touched; reads and appends are size-limited.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer

from halper.config import load_settings
from halper.guardrail import DEFAULT_POLICY, Decision, evaluate

MAX_READ_BYTES = 64_000
MAX_APPEND_BYTES = 4_000
MAX_NOTE_BYTES = 256_000  # total size a note may reach via appends
MAX_RESULTS = 5

POLICY = DEFAULT_POLICY  # module-level so tests (or a host) can swap the policy

mcp = MCPServer("notes")


class NoteError(ValueError):
    """Raised for any refused request; the message is safe to show to the model."""


def notes_root() -> Path:
    return Path(load_settings().notes_dir).resolve()


def resolve_note(root: Path, name: str) -> Path:
    """Map a note name to a file inside `root`, or raise NoteError."""
    if not name or "\0" in name:
        raise NoteError("invalid note name")
    if Path(name).is_absolute() or name.startswith(("/", "\\")):
        raise NoteError("absolute paths are not allowed")
    if ".." in Path(name.replace("\\", "/")).parts:
        raise NoteError("path traversal is not allowed")
    if not name.endswith(".md"):
        raise NoteError("only .md notes are allowed")
    root = root.resolve()
    path = (root / name).resolve()  # follows symlinks, so a link out of root is caught here
    if not path.is_relative_to(root):
        raise NoteError("path escapes the notes directory")
    return path


def _note_files(root: Path) -> list[Path]:
    root = root.resolve()
    found = []
    for p in sorted(root.rglob("*.md")):
        if p.is_file() and p.resolve().is_relative_to(root):
            found.append(p.resolve())
    return found


def search(root: Path, query: str) -> str:
    terms = [t for t in query.lower().split() if t]
    if not terms:
        raise NoteError("query is empty")
    root = root.resolve()
    scored = []
    for p in _note_files(root):
        text = p.read_text(encoding="utf-8", errors="replace")
        low = text.lower()
        score = sum(low.count(t) for t in terms)
        if score:
            first = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
            scored.append((score, p.relative_to(root).as_posix(), first))
    scored.sort(key=lambda s: (-s[0], s[1]))
    if not scored:
        return "no matches"
    return "\n".join(f"{name} (score {s}): {first}" for s, name, first in scored[:MAX_RESULTS])


def read(root: Path, name: str) -> str:
    path = resolve_note(root, name)
    if not path.is_file():
        raise NoteError(f"note not found: {name}")
    if path.stat().st_size > MAX_READ_BYTES:
        raise NoteError(f"note is larger than {MAX_READ_BYTES} bytes")
    return path.read_text(encoding="utf-8", errors="replace")


def append(root: Path, name: str, text: str) -> str:
    path = resolve_note(root, name)
    verdict = evaluate("append_note", {"path": name}, POLICY)
    if verdict.decision is not Decision.ALLOW:
        raise NoteError(f"write refused by guardrail ({verdict.decision.value}): {verdict.reason}")
    data = text.encode("utf-8")
    if len(data) > MAX_APPEND_BYTES:
        raise NoteError(f"append is larger than {MAX_APPEND_BYTES} bytes")
    current = path.stat().st_size if path.exists() else 0
    if current + len(data) > MAX_NOTE_BYTES:
        raise NoteError(f"note would exceed {MAX_NOTE_BYTES} bytes")
    if not path.parent.is_dir():
        raise NoteError("parent directory does not exist")
    with path.open("ab") as f:
        f.write(data if data.endswith(b"\n") else data + b"\n")
    return f"appended {len(data)} bytes to {name}"


def index(root: Path) -> str:
    root = root.resolve()
    return "\n".join(p.relative_to(root).as_posix() for p in _note_files(root)) or "(no notes)"


@mcp.tool()
def search_notes(query: str) -> str:
    """Keyword search over the notes. Returns the best matches with a score and title."""
    return search(notes_root(), query)


@mcp.tool()
def read_note(name: str) -> str:
    """Read one note by relative path, for example `retry-backoff.md`."""
    return read(notes_root(), name)


@mcp.tool()
def append_note(name: str, text: str) -> str:
    """Append text to a note (creates it if missing). Refused unless the guardrail allows it."""
    return append(notes_root(), name, text)


@mcp.resource("notes://index")
def notes_index() -> str:
    """List of available note names, one per line."""
    return index(notes_root())


if __name__ == "__main__":
    mcp.run(transport="stdio")
