import pytest

from halper.config import load_settings
from halper.guardrail import (
    Decision,
    Tier,
    classify_shell,
    classify_tool_call,
    decide,
    evaluate,
)

R, L, H, D = Tier.READ, Tier.LOW_WRITE, Tier.HIGH_WRITE, Tier.DESTRUCTIVE


@pytest.mark.parametrize(
    "command,tier",
    [
        ("ls -la", R),
        ("cat notes.md | grep todo | wc -l", R),
        ("git status", R),
        ("git log --oneline", R),
        ("echo hi > out.txt", L),
        ("echo hi >> out.txt", L),
        ("echo hi 2>&1", R),
        ("ls > /dev/null", R),
        ("mkdir -p a/b && touch a/b/c", L),
        ("sed -i s/a/b/ f.txt", L),
        ("sed s/a/b/ f.txt", R),
        ("git commit -m x", H),
        ("git push origin main", H),
        ("curl https://example.test", H),
        ("pip install requests", H),
        ("some-unknown-tool --flag", H),
        ("rm file.txt", D),
        ("rm -rf /", D),
        ("ls && rm x", D),  # destructive hidden after a read
        ("cat f | xargs rm", D),  # destructive hidden behind xargs
        ("echo $(rm x)", D),  # destructive in a substitution
        ("echo `rm x`", D),
        ("sudo rm -rf /var/x", D),
        ("sudo ls", H),
        ("bash -c 'rm x'", D),
        ("find . -name '*.tmp' -delete", D),
        ("find . -name x -exec rm {} ;", D),
        ("find . -name x", R),
        ("git push --force origin main", D),
        ("git reset --hard HEAD~1", D),
        ("git clean -fd", D),
        ("git branch -D old", D),
        ("git branch", R),
        ("psql -c 'DROP TABLE users'", D),
        ("psql -c 'select 1'", H),
        ("echo 'a;b' | cat", R),  # separator inside quotes is not a separator
        ("echo 'unterminated", H),
        ("FOO=1 rm x", D),
        ("dd if=/dev/zero of=/dev/sda", D),
        ("mkfs.ext4 /dev/sda1", D),
    ],
)
def test_classify_shell(command, tier):
    assert classify_shell(command) is tier


@pytest.mark.parametrize(
    "command,tier",
    [
        # Redirect and tee targets use the same sensitive-path check as path arguments.
        ("echo x > /etc/hosts", H),
        ("echo x >> /usr/local/bin/tool", H),
        ("printf a > ~/.ssh/authorized_keys", H),
        ('echo x > "$HOME/.aws/credentials"', H),
        ("echo x > config/.env.production", H),
        ("echo x > ../outside.txt", H),
        ("echo x 2> /etc/err.log", H),
        ("ls | tee /boot/grub.cfg", H),
        ("cat a | tee -a .env", H),
        ("cat a | tee notes/out.txt", L),
        ("echo x > notes/out.txt", L),
        ("echo x > /dev/null", R),
        # Inline interpreter code that deletes files is destructive.
        ("python3 -c 'import os; os.remove(\"x.txt\")'", D),
        ("python -c \"import shutil; shutil.rmtree('build')\"", D),
        ("python3.12 -c 'from pathlib import Path; Path(\"a\").unlink()'", D),
        ("perl -e 'unlink \"x.txt\"'", D),
        ("ruby -e 'File.delete(\"x\")'", D),
        ("ruby -e 'FileUtils.rm_rf(\"d\")'", D),
        ('node -e \'require("fs").unlinkSync("x")\'', D),
        ("node -e 'fs.rmSync(\"d\", {recursive:true})'", D),
        ("python3 -c 'import os; os.system(\"rm -rf d\")'", D),
        ("sudo python3 -c 'import os; os.remove(\"x\")'", D),
        # Unknown inline code and script files stay at high-write.
        ("python3 -c 'print(1)'", H),
        ("perl -e 'print 1'", H),
        ("node -e 'console.log(1)'", H),
        ("python3 script.py", H),
        # docker: read-only subcommands only.
        ("docker ps -a", R),
        ("docker images", R),
        ("docker logs web", R),
        ("docker run --rm alpine rm -rf /", H),
        ("docker rm web", H),
        ("docker", H),
        # cp: plain two-operand copy between safe relative paths only.
        ("cp a.txt b.txt", L),
        ("cp src/a.md docs/b.md", L),
        ("cp -f a.txt b.txt", H),
        ("cp -r dir other", H),
        ("cp a b c", H),
        ("cp a.txt /etc/hosts", H),
        ("cp .env backup", H),
        ("cp a.txt ../b.txt", H),
        ("cp a.txt ~/x", H),
        # curl is deliberately left cautious.
        ("curl -s https://example.test", H),
    ],
)
def test_classify_shell_gap_rules(command, tier):
    assert classify_shell(command) is tier


@pytest.mark.parametrize(
    "name,args,tier",
    [
        ("remove_label", {}, D),  # over-cautious on purpose: no name-based downgrades
        ("removeAssignee", {}, D),
        ("delete_label_and_repo", {}, D),
        ("remove_file", {}, D),
    ],
)
def test_destructive_verbs_are_not_downgraded_by_nouns(name, args, tier):
    assert classify_tool_call(name, args) is tier


@pytest.mark.parametrize(
    "name,args,tier",
    [
        ("Read", {"file_path": "a.md"}, R),
        ("Glob", {"pattern": "*.md"}, R),
        ("Write", {"file_path": "notes/a.md"}, L),
        ("Write", {"file_path": "../../etc/passwd"}, H),
        ("Edit", {"file_path": ".env"}, H),
        ("Bash", {"command": "ls"}, R),
        ("Bash", {"command": "rm x"}, D),
        ("Bash", {}, H),
        ("search_notes", {"q": "x"}, R),
        ("read_note", {}, R),
        ("append_note", {"path": "a.md"}, L),
        ("sendEmail", {}, H),
        ("delete_note", {}, D),
        ("dropTable", {}, D),
        ("get_or_delete_item", {}, D),  # highest tier among name verbs wins
        ("frobnicate", {}, H),  # unknown fails closed
    ],
)
def test_classify_tool_call(name, args, tier):
    assert classify_tool_call(name, args) is tier


def test_default_policy_and_custom_policy():
    assert [decide(t) for t in (R, L, H, D)] == [
        Decision.ALLOW,
        Decision.ALLOW,
        Decision.ASK,
        Decision.DENY,
    ]
    strict = {R: Decision.ALLOW}
    assert decide(L, strict) is Decision.DENY  # missing tiers are denied
    v = evaluate("Bash", {"command": "rm x"})
    assert v.tier is D and v.decision is Decision.DENY and "destructive" in v.reason


def test_settings_from_env():
    s = load_settings({"AGENT_MODEL": "m", "AGENT_MAX_TURNS": "3"})
    assert s.model == "m" and s.max_turns == 3 and s.max_tokens == 4096


def test_awk_is_not_read_only():
    assert classify_shell("awk 'BEGIN{system(\"rm -rf x\")}'") is H


def test_empty_policy_denies_everything():
    assert decide(R, {}) is Decision.DENY
