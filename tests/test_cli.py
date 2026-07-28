"""End-to-end tests that drive main() the way the CLI does.

The real message database, the real Contacts database and the clipboard are all
stubbed out — this suite never touches personal data. Run with:

    python3 tests/test_cli.py
"""

import io
import contextlib
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import imessage_export as m  # noqa: E402
from fixture import build_fixture  # noqa: E402

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def run(*argv):
    """Invoke main() with argv, returning (stdout, stderr, exit_code)."""
    out, err = io.StringIO(), io.StringIO()
    code = 0
    sys.argv = ["imessage_export.py", *argv]
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            m.main()
    except SystemExit as e:
        code = e.code or 0
    return out.getvalue(), err.getvalue(), code


@case
def test_list_shows_conversation():
    out, _, code = run("--list")
    assert code == 0
    assert "Fixture Chat" in out
    assert "past 7 days" in out


@case
def test_list_days_widens_window():
    out, _, _ = run("--list", "--list-days", "90")
    assert "past 90 days" in out


@case
def test_list_auto_widens_when_empty():
    """The picker is the only way to find a chat you cannot name."""
    out, err, _ = run("--list", "--list-days", "1")
    assert "widened to" in err, "did not widen an empty conversation list"
    assert "Fixture Chat" in out


@case
def test_stdout_export():
    out, err, code = run("--chat", "Fixture Chat", "--since", "30d", "--stdout")
    assert code == 0
    assert out.startswith("# iMessage Export")
    assert "[#1]" in out
    # Progress and size reporting must not pollute the piped export.
    assert "Exporting messages" in err
    assert "tokens" in err


@case
def test_last_n_limits_output():
    out, _, _ = run("--chat", "Fixture Chat", "--since", "30d", "--last", "2", "--stdout")
    assert len(re.findall(r"\[#\d+\]", out)) == 2


@case
def test_search_filters_output():
    out, _, _ = run("--chat", "Fixture Chat", "--since", "30d",
                    "--search", "labor", "--context", "0", "--stdout")
    assert "labor line" in out
    assert "here are the photos" not in out


@case
def test_search_with_no_hits_reports_clearly():
    out, _, code = run("--chat", "Fixture Chat", "--since", "30d",
                       "--search", "zzznotpresent", "--stdout")
    assert code == 0
    assert "No messages matching" in out


@case
def test_scrub_redacts_body_text():
    out, _, _ = run("--chat", "Fixture Chat", "--since", "30d", "--scrub", "--stdout")
    assert "555-123-9876" not in out
    assert "[phone-" in out


@case
def test_names_and_save_aliases(tmp: Path):
    _, err, _ = run("--chat", "Fixture Chat", "--since", "30d", "--last", "1",
                    "--names", "+15550000001=Mike", "--save-aliases", "--stdout")
    assert "Saved 1 alias" in err
    assert m.load_aliases() == {"+15550000001": "Mike"}
    # A later run should pick the alias up without --names.
    out, _, _ = run("--chat", "Fixture Chat", "--since", "30d", "--stdout")
    assert "Mike" in out and "+15550000001" not in out
    # ...and --no-aliases should ignore it.
    out, _, _ = run("--chat", "Fixture Chat", "--since", "30d",
                    "--no-aliases", "--stdout")
    assert "+15550000001" in out


@case
def test_output_to_file(tmp: Path):
    target = tmp / "out.txt"
    _, err, code = run("--chat", "Fixture Chat", "--since", "30d",
                       "-o", str(target))
    assert code == 0
    assert f"Exported to {target}" in err
    assert target.read_text(encoding="utf-8").startswith("# iMessage Export")


@case
def test_unknown_chat_exits_nonzero():
    _, err, code = run("--chat", "NoSuchConversation")
    assert code == 1
    assert "No conversation found" in err


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        # Point every external dependency at the sandbox.
        m.CHAT_DB = build_fixture(tmp / "fixture.db")
        m.ALIAS_PATH = tmp / "aliases.json"
        m.load_contacts = lambda: {}

        failures = []
        for fn in CASES:
            if m.ALIAS_PATH.exists():
                m.ALIAS_PATH.unlink()
            try:
                fn(tmp) if fn.__code__.co_argcount else fn()
                print(f"  pass  {fn.__name__}")
            except AssertionError as e:
                failures.append(fn.__name__)
                print(f"  FAIL  {fn.__name__}: {e}")
            except Exception as e:  # noqa: BLE001
                failures.append(fn.__name__)
                print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")

    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
