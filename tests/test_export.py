"""Regression tests for the exporter's formatting and filtering.

Standard library only, matching the project's no-dependencies rule. Run with:

    python3 tests/test_export.py
"""

import re
import sqlite3
import sys
import tempfile
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import imessage_export as m  # noqa: E402
from fixture import build_fixture  # noqa: E402

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


def export(conn, **kwargs):
    """Export the whole fixture conversation, with optional filters applied."""
    info = m.find_chat_by_identifier(conn, "Fixture Chat")
    since = m.datetime.now(tz=m.TZ) - timedelta(days=365)
    return m.build_export(conn, info, since, None, **kwargs)


# --- attachments ------------------------------------------------------------

@case
def test_attachment_only_message_is_not_dropped(conn):
    out = export(conn)
    assert "[PDF: quote.pdf]" in out, "attachment-only message vanished"


@case
def test_attachment_kinds(conn):
    out = export(conn)
    for expected in ("[Photo ×2]", "[Screenshot]", "[Audio message]",
                     "[Shared location]", "[Contact card]"):
        assert expected in out, f"missing {expected}"


@case
def test_link_preview_payload_suppressed(conn):
    out = export(conn)
    assert "[Attachment]" not in out, "plugin payload rendered as an attachment"
    assert "pluginPayload" not in out


@case
def test_attachment_label_classification():
    def row(mime, uti, name, sticker=0):
        return {"mime_type": mime, "uti": uti, "transfer_name": name,
                "is_sticker": sticker}
    assert m.attachment_label(row("image/jpeg", "public.jpeg", "IMG_1.jpg"))[0] == "Photo"
    assert m.attachment_label(row("image/png", "public.png", "Screenshot x.png"))[0] == "Screenshot"
    assert m.attachment_label(row("image/gif", "com.compuserve.gif", "a.gif"))[0] == "GIF"
    assert m.attachment_label(row("application/pdf", "com.adobe.pdf", "q.pdf")) == ("PDF", "q.pdf")
    assert m.attachment_label(row(None, None, "s", sticker=1))[0] == "Sticker"


@case
def test_attachment_grouping():
    f = m.format_attachments
    assert f([("Photo", None)]) == "[Photo]"
    assert f([("Photo", None)] * 3) == "[Photo ×3]"
    assert f([("PDF", "a.pdf")]) == "[PDF: a.pdf]"
    assert f([("PDF", "a.pdf"), ("PDF", "b.pdf")]) == "[PDF: a.pdf, b.pdf]"
    assert f([]) == ""


# --- tapbacks ---------------------------------------------------------------

@case
def test_custom_emoji_tapback_rendered(conn):
    out = export(conn)
    assert "💯 by" in out, "custom-emoji tapback (type 2006) was dropped"
    assert "🙄 by" in out, "custom tapback on an attachment-only message was dropped"


@case
def test_legacy_tapback_rendered(conn):
    assert "👍 by" in export(conn)


@case
def test_tapback_removal_not_rendered(conn):
    out = export(conn)
    # G8 removes a 💯 from G7; G7 must carry no reaction annotation.
    reply_line = [l for l in out.splitlines() if "way over the estimate" in l][0]
    assert "(" not in reply_line.split("): ")[-1], "removal type 3006 leaked into output"


# --- message IDs and threading ----------------------------------------------

@case
def test_ids_are_sequential(conn):
    out = export(conn)
    ids = [int(n) for n in re.findall(r"\[#(\d+)\]", out)]
    assert ids == list(range(1, len(ids) + 1)), f"ids not sequential: {ids}"


@case
def test_reply_cites_originator_id(conn):
    out = export(conn)
    assert re.search(r'replying to #\d+ "The quote came in at 12k"', out), \
        "threaded reply did not cite its originator's ID"


@case
def test_reply_to_out_of_window_originator(conn):
    """An originator outside the window is quoted but carries no ID."""
    info = m.find_chat_by_identifier(conn, "Fixture Chat")
    rows = conn.execute("SELECT date FROM message WHERE guid='G7'").fetchone()
    since = m.apple_ts_to_datetime(rows[0] - 10 * 10**9)
    out = m.build_export(conn, info, since, None)
    assert "[earlier message]" in out
    assert not re.search(r'replying to #\d+ "The quote', out), \
        "cited an ID for a message that is not in the export"


@case
def test_unknown_originator(conn):
    assert "(replying to unknown message)" in export(conn)


@case
def test_system_message_is_chronological(conn):
    """System lines used to be hoisted to the top of the export."""
    lines = export(conn).splitlines()
    sys_idx = next(i for i, l in enumerate(lines) if "[System:" in l)
    first_msg = next(i for i, l in enumerate(lines) if l.startswith("[#1]"))
    assert sys_idx > first_msg, "system message rendered before the conversation"


# --- text cleaning and timezone ---------------------------------------------

@case
def test_control_characters_stripped(conn):
    out = export(conn)
    assert "\x00" not in out and "\x01" not in out, "control characters leaked"
    assert "￼" not in out, "object-replacement marker leaked"
    assert "deal at 10k final" in out


@case
def test_zero_width_joiner_preserved():
    assert "‍" in m.clean_text("a‍b")


@case
def test_timezone_is_fixed_est():
    dt = m.apple_ts_to_datetime(700000000 * 10**9)
    assert dt.utcoffset().total_seconds() == -5 * 3600
    summer = m.parse_since("2024-07-15")
    assert summer.utcoffset().total_seconds() == -5 * 3600, "offset shifted for DST"


# --- filters ----------------------------------------------------------------

@case
def test_last_n(conn):
    out = export(conn, limit=3)
    assert len(re.findall(r"\[#\d+\]", out)) == 3
    assert "most recent 3 messages only" in out


@case
def test_search_filters_and_marks_gaps(conn):
    out = export(conn, search="labor", context=0)
    assert "labor line" in out
    assert "and here are the photos" not in out, "search returned unrelated messages"
    assert 'matching "labor"' in out


@case
def test_search_matches_attachment_filenames(conn):
    out = export(conn, search="quote.pdf", context=0)
    assert "[PDF: quote.pdf]" in out, "search did not match an attachment filename"


@case
def test_search_gap_marker(conn):
    out = export(conn, search="12k", context=0)
    assert "messages omitted" in out, "no gap marker between disjoint matches"


@case
def test_search_no_match_returns_empty(conn):
    assert export(conn, search="zzzznotpresent") == ""


# --- aliases and scrubbing --------------------------------------------------

@case
def test_aliases_applied(conn):
    out = export(conn, aliases={"+15550000001": "Mike"})
    assert "Mike:" in out and "+15550000001" not in out


@case
def test_scrub_tokenizes_body_text(conn):
    """--names only rewrites listed identifiers; --scrub catches the rest."""
    plain = export(conn)
    assert "555-123-9876" in plain and "bob@example.com" in plain
    scrubbed = export(conn, scrub=True)
    assert "555-123-9876" not in scrubbed, "phone number in body text survived --scrub"
    assert "bob@example.com" not in scrubbed, "email in body text survived --scrub"
    assert "[phone-" in scrubbed and "[email-" in scrubbed


@case
def test_scrub_tokens_are_stable():
    s = m.Scrubber()
    first = s.scrub("call 555-123-9876")
    second = s.scrub("still 555-123-9876")
    token = re.search(r"\[phone-\d+\]", first).group(0)
    assert token in second, "same number produced different tokens"


@case
def test_scrub_leaves_timestamps_and_ids_alone():
    probe = "[#3] [2023-03-08 15:28] Me: meet 2024-01-15 at 10:30"
    assert m.Scrubber().scrub(probe) == probe


@case
def test_scrub_preserves_chosen_aliases(conn):
    out = export(conn, aliases={"+15550000001": "Mike"}, scrub=True)
    assert "Mike:" in out, "scrubbing clobbered a user-assigned alias"


@case
def test_alias_store_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        original = m.ALIAS_PATH
        try:
            m.ALIAS_PATH = Path(tmp) / "aliases.json"
            m.save_aliases({"+15550000001": "Mike"})
            m.save_aliases({"+15550000002": "Dana"})
            assert m.load_aliases() == {"+15550000001": "Mike", "+15550000002": "Dana"}, \
                "saving aliases did not merge with existing entries"
        finally:
            m.ALIAS_PATH = original


# --- size -------------------------------------------------------------------

@case
def test_estimate_tokens():
    assert m.estimate_tokens("x" * 400) == 100


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        db = build_fixture(Path(tmp) / "fixture.db")
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row

        failures = []
        for fn in CASES:
            try:
                if fn.__code__.co_argcount:
                    fn(conn)
                else:
                    fn()
                print(f"  pass  {fn.__name__}")
            except AssertionError as e:
                failures.append((fn.__name__, str(e) or "assertion failed"))
                print(f"  FAIL  {fn.__name__}: {e}")
            except Exception as e:  # noqa: BLE001
                failures.append((fn.__name__, f"{type(e).__name__}: {e}"))
                print(f"  ERROR {fn.__name__}: {type(e).__name__}: {e}")
        conn.close()

    print(f"\n{len(CASES) - len(failures)}/{len(CASES)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
