"""Deterministic impact-tier classifier for tool calls, and a policy over the tiers.

Pure functions: no I/O, no model call. The classifier is conservative. Anything it
does not recognise is treated as high-write, so unknown tools and commands get a
human check instead of silently running.

# simple: allowlist/denylist heuristics over shell text; upgrade to a real shell
# parser (e.g. bashlex) if adversarial inputs show up that the eval cannot cover.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from enum import IntEnum, StrEnum


class Tier(IntEnum):
    READ = 0
    LOW_WRITE = 1
    HIGH_WRITE = 2
    DESTRUCTIVE = 3


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


DEFAULT_POLICY: dict[Tier, Decision] = {
    Tier.READ: Decision.ALLOW,
    Tier.LOW_WRITE: Decision.ALLOW,
    Tier.HIGH_WRITE: Decision.ASK,
    Tier.DESTRUCTIVE: Decision.DENY,
}


@dataclass(frozen=True)
class Verdict:
    tier: Tier
    decision: Decision
    reason: str


# --- shell command tables -------------------------------------------------------------

_READ_CMDS = set(
    "ls cat head tail grep egrep fgrep rg wc pwd echo printf stat file du df diff sort uniq cut tr "
    "which whoami date printenv true false test [ tree less more basename dirname realpath "
    "uname id ps jq find sed cd".split()
)
_LOW_WRITE_CMDS = {"touch", "mkdir", "tee", "ln"}
_DESTRUCTIVE_CMDS = set(
    "rm rmdir shred dd truncate unlink wipefs fdisk parted shutdown reboot halt poweroff".split()
)
_WRAPPERS = {"xargs", "nohup", "time", "nice", "env", "command", "exec", "timeout"}
_SQL_CLIENTS = {"psql", "sqlite3", "mysql", "mariadb", "mongosh", "redis-cli"}
_SQL_DESTRUCTIVE = re.compile(
    r"\b(drop\s+(table|database|schema|index)|truncate\s+table|delete\s+from)\b", re.I
)

_GIT_READ = set(
    "status log diff show ls-files rev-parse blame describe grep shortlog remote config".split()
)
_GIT_LOW = {"add", "fetch", "stash"}
_DOCKER_READ = {"ps", "images", "inspect", "logs", "version", "info", "top", "stats", "diff"}
_INTERPRETERS = re.compile(r"(python[\d.]*|perl|ruby|node|nodejs|php|deno|bun)")
_INLINE_FLAGS = {"-c", "-e", "-E", "-r", "--eval", "-p"}
# File-deletion calls across python, perl, ruby, node, php, shell-in-string.
_DELETE_CALL = re.compile(
    r"\b(unlink\w*|rmtree|rmdir|removedirs|remove_tree|os\.remove|fs\.rm\w*|"
    r"File\.delete|FileUtils\.(rm|remove)\w*|rm\s+-)|\.unlink\(|\brm\s"
)
# Sensitive paths: dotfile secrets and system dirs. A `..` component also counts as risky.
_SENSITIVE_PATH = re.compile(
    r"(^|/)(\.env[^/]*|\.ssh|\.aws|\.gnupg|id_rsa[^/]*)(/|$)|^/(etc|usr|boot|bin|sbin)/"
)

_SEPARATORS = {";", "&&", "||", "|", "&", "|&", "(", ")"}
_SUBSHELL = re.compile(r"\$\(([^()]*)\)|`([^`]*)`")

_SHELL_TOOLS = {"bash", "shell", "sh", "run_command", "execute_command", "exec", "terminal"}


def _risky_path(path: str) -> bool:
    return ".." in path.split("/") or bool(_SENSITIVE_PATH.search(path))


def _git_tier(args: list[str]) -> Tier:
    flags = [a for a in args if a.startswith("-")]
    sub = next((a for a in args if not a.startswith("-")), "")
    if sub == "push":
        forced = any(
            f in ("-f", "--force", "--delete", "-d") or f.startswith("--force") for f in flags
        )
        deleting_ref = any(a.startswith(":") for a in args)
        return Tier.DESTRUCTIVE if forced or deleting_ref else Tier.HIGH_WRITE
    if sub == "reset":
        return Tier.DESTRUCTIVE if "--hard" in flags else Tier.HIGH_WRITE
    if sub == "clean":
        return Tier.DESTRUCTIVE
    if sub == "branch":
        return Tier.DESTRUCTIVE if any(f in ("-d", "-D", "--delete") for f in flags) else Tier.READ
    if sub in _GIT_READ:
        return Tier.READ
    if sub in _GIT_LOW:
        return Tier.LOW_WRITE
    return Tier.HIGH_WRITE  # includes _GIT_HIGH and anything unrecognised


def _command_tier(tokens: list[str], raw: str) -> Tier:
    """Classify one simple command (already split on pipes, ;, &&, etc.)."""
    floor = Tier.READ
    while tokens:
        head = tokens[0]
        if re.fullmatch(r"[A-Za-z_]\w*=.*", head):  # VAR=value prefix
            tokens = tokens[1:]
        elif head in _WRAPPERS or head == "sudo":
            if head == "sudo":
                floor = Tier.HIGH_WRITE
            tokens = tokens[1:]
            # Drop the wrapper's own options (crude: `-u user` is handled, others are not).
            while tokens and (tokens[0].startswith("-") or re.fullmatch(r"\d+[smhd]?", tokens[0])):
                skip = 2 if tokens[0] in ("-u", "-g") else 1
                tokens = tokens[skip:]
        else:
            break
    if not tokens:
        return floor
    cmd, args = tokens[0].rsplit("/", 1)[-1], tokens[1:]

    if cmd in {"sh", "bash", "zsh", "dash"}:
        if "-c" in args and args.index("-c") + 1 < len(args):
            return max(floor, Tier.HIGH_WRITE, classify_shell(args[args.index("-c") + 1]))
        return max(floor, Tier.HIGH_WRITE)
    if cmd in _DESTRUCTIVE_CMDS or cmd.startswith("mkfs"):
        tier = Tier.DESTRUCTIVE
    elif cmd == "git":
        tier = _git_tier(args)
    elif cmd == "docker":
        sub = next((a for a in args if not a.startswith("-")), "")
        tier = Tier.READ if sub in _DOCKER_READ else Tier.HIGH_WRITE
    elif _INTERPRETERS.fullmatch(cmd):
        code = [args[i + 1] for i, a in enumerate(args[:-1]) if a in _INLINE_FLAGS]
        deleting = any(_DELETE_CALL.search(c) for c in code)
        tier = Tier.DESTRUCTIVE if deleting else Tier.HIGH_WRITE  # unknown code: fail closed
    elif cmd == "cp":
        # Only a plain two-operand copy between safe paths; flags (-f, -r, -t) stay cautious.
        paths = [a for a in args if not a.startswith("-")]
        plain = len(paths) == len(args) == 2 and not any(
            _risky_path(a) or a.startswith(("/", "~")) for a in paths
        )
        tier = Tier.LOW_WRITE if plain else Tier.HIGH_WRITE
    elif cmd == "find":
        tier = Tier.READ
        if "-delete" in args:
            tier = Tier.DESTRUCTIVE
        for flag in ("-exec", "-execdir", "-ok"):
            if flag in args and args.index(flag) + 1 < len(args):
                tier = max(tier, _command_tier(args[args.index(flag) + 1 :], raw))
    elif cmd == "sed":
        in_place = any(a.startswith("-i") or a == "--in-place" for a in args)
        tier = Tier.LOW_WRITE if in_place else Tier.READ
    elif cmd in _SQL_CLIENTS:
        tier = Tier.DESTRUCTIVE if _SQL_DESTRUCTIVE.search(raw) else Tier.HIGH_WRITE
    elif cmd in _LOW_WRITE_CMDS:
        risky = cmd == "tee" and any(_risky_path(a) for a in args if not a.startswith("-"))
        tier = Tier.HIGH_WRITE if risky else Tier.LOW_WRITE
    elif cmd in _READ_CMDS:
        tier = Tier.READ
    else:
        tier = Tier.HIGH_WRITE
    return max(floor, tier)


def classify_shell(command: str) -> Tier:
    """Return the highest tier of any command, redirect or substitution in `command`."""
    tier = Tier.READ
    for m in _SUBSHELL.finditer(command):
        tier = max(tier, classify_shell(m.group(1) or m.group(2) or ""))
    stripped = _SUBSHELL.sub(" ", command).replace("\n", " ; ")
    try:
        lexer = shlex.shlex(stripped, posix=True, punctuation_chars=True)
        lexer.whitespace_split = False
        tokens = list(lexer)
    except ValueError:
        return Tier.HIGH_WRITE  # unbalanced quotes: do not guess

    segment: list[str] = []
    i = 0
    while i <= len(tokens):
        tok = tokens[i] if i < len(tokens) else ";"
        if tok in _SEPARATORS:
            if segment:
                tier = max(tier, _command_tier(segment, stripped))
            segment = []
        elif tok.startswith(">"):
            target = tokens[i + 1] if i + 1 < len(tokens) else ""
            if not tok.endswith("&") and target != "/dev/null":
                tier = max(tier, Tier.HIGH_WRITE if _risky_path(target) else Tier.LOW_WRITE)
        elif tok.startswith("<"):
            pass
        else:
            segment.append(tok)
        i += 1
    return tier


# --- generic (non-shell) tools --------------------------------------------------------

_KNOWN_TOOLS = {
    # Claude Code / Agent SDK built-in tool names.
    "read": Tier.READ,
    "glob": Tier.READ,
    "grep": Tier.READ,
    "websearch": Tier.READ,
    "webfetch": Tier.READ,
    "write": Tier.LOW_WRITE,
    "edit": Tier.LOW_WRITE,
    "multiedit": Tier.LOW_WRITE,
    "notebookedit": Tier.LOW_WRITE,
    "todowrite": Tier.READ,
}
_VERB_TIERS: dict[str, Tier] = {}
for _tier, _verbs in {
    Tier.READ: "read get list search find view show fetch query describe count stat",
    Tier.LOW_WRITE: "write create append add edit update set save put",
    Tier.HIGH_WRITE: "send post publish deploy install commit push move rename run exec "
    "execute invoke call email message upload merge",
    Tier.DESTRUCTIVE: "delete remove drop destroy purge truncate wipe erase kill reset "
    "format uninstall revoke",
}.items():
    for _v in _verbs.split():
        _VERB_TIERS[_v] = _tier

_PATH_KEYS = ("path", "file_path", "filepath", "filename", "file", "dest", "destination")


def _name_tokens(name: str) -> list[str]:
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)
    return [t for t in re.split(r"[^A-Za-z0-9]+", spaced.lower()) if t]


def classify_tool_call(tool_name: str, tool_input: dict | None = None) -> Tier:
    """Classify a tool call by name and input. Unknown tools are high-write."""
    tool_input = tool_input or {}
    lowered = tool_name.lower()
    if lowered in _SHELL_TOOLS:
        cmd = tool_input.get("command", tool_input.get("cmd"))
        return classify_shell(cmd) if isinstance(cmd, str) else Tier.HIGH_WRITE

    if lowered in _KNOWN_TOOLS:
        tier = _KNOWN_TOOLS[lowered]
    else:
        tiers = [_VERB_TIERS[t] for t in _name_tokens(tool_name) if t in _VERB_TIERS]
        tier = max(tiers) if tiers else Tier.HIGH_WRITE

    if tier is Tier.LOW_WRITE:
        for key in _PATH_KEYS:
            path = tool_input.get(key)
            if isinstance(path, str) and _risky_path(path):
                return Tier.HIGH_WRITE
    return tier


# --- policy ---------------------------------------------------------------------------


def decide(tier: Tier, policy: dict[Tier, Decision] | None = None) -> Decision:
    """Map a tier to allow / ask / deny. Missing tiers are denied."""
    return (DEFAULT_POLICY if policy is None else policy).get(tier, Decision.DENY)


def evaluate(
    tool_name: str, tool_input: dict | None = None, policy: dict[Tier, Decision] | None = None
) -> Verdict:
    """Classify a tool call and apply the policy."""
    tier = classify_tool_call(tool_name, tool_input)
    decision = decide(tier, policy)
    return Verdict(tier, decision, f"{tool_name}: tier {tier.name.lower()} -> {decision.value}")
