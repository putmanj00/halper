import pytest

from halper import mcp_server as ms
from halper.guardrail import Decision, Tier


@pytest.fixture
def root(tmp_path):
    (tmp_path / "alpha.md").write_text("# Alpha\nretry retry backoff\n")
    (tmp_path / "beta.md").write_text("# Beta\nbackoff only once\n")
    return tmp_path


def test_search_ranks_by_score(root):
    out = ms.search(root, "retry backoff").splitlines()
    assert out[0].startswith("alpha.md (score 3)")
    assert out[1].startswith("beta.md (score 1)")


def test_search_no_match_and_empty_query(root):
    assert ms.search(root, "zzzz") == "no matches"
    with pytest.raises(ms.NoteError):
        ms.search(root, "   ")


def test_read(root):
    assert "backoff only once" in ms.read(root, "beta.md")


def test_read_missing(root):
    with pytest.raises(ms.NoteError, match="not found"):
        ms.read(root, "nope.md")


def test_append_allowed(root):
    assert "appended" in ms.append(root, "alpha.md", "new line")
    assert (root / "alpha.md").read_text().endswith("new line\n")


def test_append_creates_new_note(root):
    ms.append(root, "fresh.md", "hello")
    assert (root / "fresh.md").read_text() == "hello\n"


@pytest.mark.parametrize("name", ["../x.md", "a/../../x.md", "sub/../../x.md"])
def test_traversal_rejected(root, name):
    with pytest.raises(ms.NoteError, match="traversal"):
        ms.read(root, name)
    with pytest.raises(ms.NoteError):
        ms.append(root, name, "x")


def test_absolute_path_rejected(root, tmp_path_factory):
    other = tmp_path_factory.mktemp("other") / "secret.md"
    other.write_text("secret")
    with pytest.raises(ms.NoteError, match="absolute"):
        ms.read(root, str(other))


def test_non_markdown_rejected(root):
    (root / "data.txt").write_text("x")
    with pytest.raises(ms.NoteError, match=r"\.md"):
        ms.read(root, "data.txt")


def test_symlink_escape_rejected(root, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "secret.md").write_text("secret")
    (root / "link.md").symlink_to(outside / "secret.md")
    (root / "linkdir").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ms.NoteError, match="escapes"):
        ms.read(root, "link.md")
    with pytest.raises(ms.NoteError, match="escapes"):
        ms.append(root, "linkdir/secret.md", "x")
    assert "secret" not in ms.index(root)
    assert (outside / "secret.md").read_text() == "secret"


def test_guardrail_refuses_write(root, monkeypatch):
    monkeypatch.setattr(ms, "POLICY", {Tier.LOW_WRITE: Decision.DENY})
    with pytest.raises(ms.NoteError, match="guardrail"):
        ms.append(root, "alpha.md", "x")
    assert (root / "alpha.md").read_text() == "# Alpha\nretry retry backoff\n"


def test_guardrail_ask_tier_refused(root, monkeypatch):
    monkeypatch.setattr(ms, "POLICY", {Tier.LOW_WRITE: Decision.ASK})
    with pytest.raises(ms.NoteError, match="ask"):
        ms.append(root, "alpha.md", "x")


def test_append_size_limit(root):
    with pytest.raises(ms.NoteError, match="larger"):
        ms.append(root, "alpha.md", "x" * (ms.MAX_APPEND_BYTES + 1))


def test_total_note_size_limit(root, monkeypatch):
    monkeypatch.setattr(ms, "MAX_NOTE_BYTES", 50)
    with pytest.raises(ms.NoteError, match="exceed"):
        ms.append(root, "alpha.md", "x" * 40)


def test_read_size_limit(root, monkeypatch):
    monkeypatch.setattr(ms, "MAX_READ_BYTES", 5)
    with pytest.raises(ms.NoteError, match="larger"):
        ms.read(root, "alpha.md")


def test_tools_use_configured_dir(root, monkeypatch):
    monkeypatch.setenv("AGENT_NOTES_DIR", str(root))
    assert "alpha.md" in ms.notes_index()
    assert "Beta" in ms.read_note("beta.md")
    assert "alpha.md" in ms.search_notes("retry")
