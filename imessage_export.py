#!/usr/bin/env python3
"""iMessage Conversation Exporter CLI for macOS.

Reads ~/Library/Messages/chat.db (read-only) and exports conversations
to plain text files with inline reply threading preserved.

Requires Full Disk Access (System Settings → Privacy & Security).
"""

import argparse
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
APPLE_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01

# All timestamps (display, date parsing, "now") are EST — a fixed UTC-5 offset
# that does not shift for daylight saving.
TZ = timezone(timedelta(hours=-5), "EST")

# Placeholder character iMessage embeds in text where an attachment sits.
OBJECT_REPLACEMENT = "￼"

# Tapback reaction types
TAPBACK_TYPES = {
    2000: "❤️",
    2001: "👍",
    2002: "👎",
    2003: "😂",
    2004: "‼️",
    2005: "❓",
    # 2006 = arbitrary emoji, carried in message.associated_message_emoji
    # 3000-3006 = removal of the corresponding tapback (skipped)
}
CUSTOM_TAPBACK_TYPE = 2006

# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def apple_ts_to_datetime(apple_ns: int) -> datetime:
    """Convert Apple Core Data nanosecond timestamp to an EST datetime."""
    if apple_ns is None or apple_ns == 0:
        return None
    unix_ts = apple_ns / 1_000_000_000 + APPLE_EPOCH_OFFSET
    return datetime.fromtimestamp(unix_ts, tz=TZ)


def datetime_to_apple_ts(dt: datetime) -> int:
    """Convert datetime to Apple Core Data nanosecond timestamp."""
    unix_ts = dt.timestamp()
    return int((unix_ts - APPLE_EPOCH_OFFSET) * 1_000_000_000)


def format_relative_time(dt: datetime) -> str:
    """Format a datetime as a human-readable relative time string."""
    now = datetime.now(tz=TZ)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    if days == 1:
        return "1d ago"
    return f"{days}d ago"


# ---------------------------------------------------------------------------
# attributedBody extraction
# ---------------------------------------------------------------------------


def extract_text_from_attributed_body(blob: bytes) -> str | None:
    """Extract plain text from an NSAttributedString typedstream blob."""
    if blob is None:
        return None
    NSSTRING_MARKER = b"NSString"
    idx = blob.find(NSSTRING_MARKER)
    if idx == -1:
        return None

    pos = idx + len(NSSTRING_MARKER)
    # Skip type-descriptor bytes until '+' (0x2B)
    while pos < len(blob) and blob[pos] != 0x2B:
        pos += 1
    pos += 1  # skip the '+' itself
    if pos >= len(blob):
        return None

    # Read length
    length_byte = blob[pos]
    pos += 1
    if length_byte < 0x80:
        text_len = length_byte
    else:
        num_extra = length_byte & 0x7F
        if pos + num_extra > len(blob):
            return None
        text_len = int.from_bytes(blob[pos : pos + num_extra], "little")
        pos += num_extra

    if pos + text_len > len(blob):
        return None

    return blob[pos : pos + text_len].decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Attachments
# ---------------------------------------------------------------------------


def clean_text(text: str | None) -> str | None:
    """Normalize message text for pasting into a plain-text tool.

    Removes the object-replacement chars iMessage uses as attachment markers,
    plus control/format characters — the typedstream parser leaks NULs and other
    binary noise that makes exports register as binary files. Zero-width joiners
    are kept so multi-codepoint emoji survive intact.
    """
    if text is None:
        return None
    text = text.replace(OBJECT_REPLACEMENT, "")
    cleaned = []
    for ch in text:
        if ch in ("\n", "\t"):
            cleaned.append(ch)
            continue
        category = unicodedata.category(ch)
        if category == "Cc" or (category == "Cf" and ch != "‍"):
            continue
        cleaned.append(ch)
    return "".join(cleaned).strip()


def attachment_label(row: sqlite3.Row) -> tuple[str, str | None]:
    """Classify an attachment as (kind, filename). Filename is None when noise."""
    mime = (row["mime_type"] or "").lower()
    uti = (row["uti"] or "").lower()
    name = row["transfer_name"] or ""

    if row["is_sticker"]:
        return "Sticker", None
    if mime.startswith("image/"):
        if name.lower().startswith("screenshot"):
            return "Screenshot", None
        if mime == "image/gif":
            return "GIF", None
        # Camera-roll names (IMG_1234.heic) carry no meaning — drop them.
        return "Photo", None
    if mime.startswith("video/"):
        return "Video", None
    if mime.startswith("audio/") or uti == "com.apple.coreaudio-format":
        return "Audio", None
    if mime == "application/pdf":
        return "PDF", name or None
    if "vcard" in mime or "vcard" in uti:
        return "Contact card", None
    if "vlocation" in mime or "vlocation" in uti:
        return "Shared location", None
    if name:
        return "File", name
    return "Attachment", None


def get_attachments(
    conn: sqlite3.Connection, rowids: list[int]
) -> dict[int, list[tuple[str, str | None]]]:
    """Map message ROWID → list of (kind, filename) for its real attachments.

    Link previews and app payloads (`.pluginPayloadAttachment` / `dyn.*` UTIs)
    are excluded: their URL is already in the message text, and they outnumber
    real attachments roughly 2:1.
    """
    if not rowids:
        return {}

    result: dict[int, list[tuple[str, str | None]]] = {}
    # Chunk to stay under SQLite's variable limit on large exports.
    for i in range(0, len(rowids), 500):
        chunk = rowids[i : i + 500]
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT
                maj.message_id,
                a.mime_type, a.uti, a.transfer_name, a.is_sticker
            FROM message_attachment_join maj
            JOIN attachment a ON a.ROWID = maj.attachment_id
            WHERE maj.message_id IN ({placeholders})
              AND COALESCE(a.uti, '') NOT LIKE 'dyn.%'
              AND COALESCE(a.transfer_name, '') NOT LIKE '%.pluginPayloadAttachment'
            ORDER BY maj.ROWID
            """,
            chunk,
        ).fetchall()
        for row in rows:
            result.setdefault(row["message_id"], []).append(attachment_label(row))

    return result


def format_attachments(attachments: list[tuple[str, str | None]]) -> str:
    """Render attachments as inline placeholders, e.g. '[Photo ×3] [PDF: quote.pdf]'."""
    if not attachments:
        return ""

    # Group by kind, preserving first-seen order.
    grouped: dict[str, list[str]] = {}
    for kind, name in attachments:
        grouped.setdefault(kind, [])
        if name:
            grouped[kind].append(name)

    parts = []
    for kind, names in grouped.items():
        count = sum(1 for k, _ in attachments if k == kind)
        if count == 1:
            parts.append(f"[{kind}: {names[0]}]" if names else f"[{kind}]")
        elif names and len(names) == count and count <= 3:
            parts.append(f"[{kind}: {', '.join(names)}]")
        else:
            parts.append(f"[{kind} ×{count}]")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------


def open_db() -> sqlite3.Connection:
    """Open chat.db read-only. Exits with a helpful message on failure."""
    if not CHAT_DB.exists():
        print(f"Error: iMessage database not found at {CHAT_DB}", file=sys.stderr)
        print(
            "This tool only works on macOS with an active iMessage account.",
            file=sys.stderr,
        )
        sys.exit(1)

    db_uri = f"file:{CHAT_DB}?mode=ro"
    try:
        conn = sqlite3.connect(db_uri, uri=True)
        conn.row_factory = sqlite3.Row
        # Quick test query
        conn.execute("SELECT 1 FROM chat LIMIT 1")
        return conn
    except sqlite3.OperationalError as e:
        if "unable to open" in str(e) or "authorization denied" in str(e):
            print(
                "Error: Cannot read iMessage database. Full Disk Access is required.",
                file=sys.stderr,
            )
            print(
                "Grant it in: System Settings → Privacy & Security → Full Disk Access",
                file=sys.stderr,
            )
            print(
                "Add your terminal app (Terminal, iTerm, etc.) to the list.",
                file=sys.stderr,
            )
        else:
            print(f"Error opening database: {e}", file=sys.stderr)
        sys.exit(1)


def has_column(conn: sqlite3.Connection, table: str, column: str) -> bool:
    """Check a column exists — chat.db schema varies across macOS versions."""
    return any(
        row[1] == column for row in conn.execute(f"PRAGMA table_info({table})")
    )


def get_handle_map(conn: sqlite3.Connection) -> dict[int, str]:
    """Return a mapping of handle ROWID → contact identifier (phone/email)."""
    rows = conn.execute("SELECT ROWID, id FROM handle").fetchall()
    return {row["ROWID"]: row["id"] for row in rows}


def get_chat_participants(
    conn: sqlite3.Connection, chat_id: int, handle_map: dict[int, str]
) -> list[str]:
    """Return list of participant identifiers for a chat."""
    rows = conn.execute(
        "SELECT handle_id FROM chat_handle_join WHERE chat_id = ?", (chat_id,)
    ).fetchall()
    return [handle_map.get(row["handle_id"], "Unknown") for row in rows]


def get_recent_conversations(
    conn: sqlite3.Connection, days: int = 7, contacts: dict[str, str] | None = None
) -> list[dict]:
    """Fetch conversations with activity in the past N days."""
    contacts = contacts if contacts is not None else {}
    handle_map = get_handle_map(conn)
    cutoff = datetime_to_apple_ts(datetime.now(tz=TZ) - timedelta(days=days))

    query = """
        SELECT
            c.ROWID AS chat_id,
            c.chat_identifier,
            c.display_name,
            c.style,
            COUNT(m.ROWID) AS msg_count,
            MAX(m.date) AS last_date
        FROM chat c
        JOIN chat_message_join cmj ON cmj.chat_id = c.ROWID
        JOIN message m ON m.ROWID = cmj.message_id
        WHERE m.date > ?
        GROUP BY c.ROWID
        ORDER BY last_date DESC
    """
    rows = conn.execute(query, (cutoff,)).fetchall()

    conversations = []
    for row in rows:
        chat_id = row["chat_id"]
        style = row["style"]
        display_name = row["display_name"]
        chat_identifier = row["chat_identifier"]

        # Determine display label, preferring real names from Contacts
        if display_name:
            label = display_name
        elif style == 43:
            # Group chat without a name — list participants
            participants = get_chat_participants(conn, chat_id, handle_map)
            if participants:
                named = [resolve_name(p, contacts) or p for p in participants]
                label = ", ".join(named[:4])
                if len(named) > 4:
                    label += f" +{len(named) - 4} more"
            else:
                label = chat_identifier
        else:
            # DM — a contact name if we have one, else the phone/email
            label = resolve_name(chat_identifier, contacts) or chat_identifier

        last_dt = apple_ts_to_datetime(row["last_date"])
        conversations.append(
            {
                "chat_id": chat_id,
                "label": label,
                "chat_identifier": chat_identifier,
                "display_name": display_name,
                "style": style,
                "msg_count": row["msg_count"],
                "last_date": last_dt,
                "last_relative": format_relative_time(last_dt) if last_dt else "unknown",
            }
        )

    return conversations


def find_chat_by_identifier(
    conn: sqlite3.Connection, identifier: str
) -> dict | None:
    """Find a chat by display_name or chat_identifier (partial match)."""
    handle_map = get_handle_map(conn)
    # Normalize phone: strip formatting
    normalized = re.sub(r"[\s\-\(\)]", "", identifier)

    rows = conn.execute(
        """
        SELECT ROWID AS chat_id, chat_identifier, display_name, style
        FROM chat
        """
    ).fetchall()

    for row in rows:
        display_name = row["display_name"] or ""
        chat_id_str = row["chat_identifier"] or ""
        chat_id_normalized = re.sub(r"[\s\-\(\)]", "", chat_id_str)

        if (
            display_name.lower() == identifier.lower()
            or chat_id_str == identifier
            or chat_id_normalized == normalized
            or (normalized.startswith("+") and chat_id_normalized.endswith(normalized))
            or (
                not normalized.startswith("+")
                and normalized.isdigit()
                and chat_id_normalized.endswith(normalized)
            )
        ):
            participants = get_chat_participants(conn, row["chat_id"], handle_map)
            label = display_name or chat_id_str
            if not label and row["style"] == 43:
                label = ", ".join(participants[:4])
            return {
                "chat_id": row["chat_id"],
                "label": label or chat_id_str,
                "chat_identifier": chat_id_str,
                "display_name": display_name,
                "style": row["style"],
                "participants": participants,
            }
    return None


def fetch_messages(
    conn: sqlite3.Connection,
    chat_id: int,
    since: datetime,
    until: datetime | None = None,
) -> list[dict]:
    """Fetch messages for a chat within a time range."""
    handle_map = get_handle_map(conn)
    since_ts = datetime_to_apple_ts(since)
    until_ts = datetime_to_apple_ts(until) if until else None

    params: list = [chat_id, since_ts]
    until_clause = ""
    if until_ts is not None:
        until_clause = "AND m.date <= ?"
        params.append(until_ts)

    # Custom-emoji tapbacks only exist on newer macOS builds.
    emoji_col = (
        "m.associated_message_emoji"
        if has_column(conn, "message", "associated_message_emoji")
        else "NULL AS associated_message_emoji"
    )

    query = f"""
        SELECT
            m.ROWID,
            m.guid,
            m.text,
            m.attributedBody,
            m.date,
            m.is_from_me,
            m.handle_id,
            m.thread_originator_guid,
            m.associated_message_guid,
            m.associated_message_type,
            {emoji_col},
            m.group_title,
            m.is_audio_message,
            m.item_type
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        WHERE cmj.chat_id = ?
          AND m.date > ?
          {until_clause}
        ORDER BY m.date ASC
    """
    rows = conn.execute(query, params).fetchall()

    attachments = get_attachments(conn, [row["ROWID"] for row in rows])

    messages = []
    for row in rows:
        text = row["text"]
        if text is None and row["attributedBody"] is not None:
            text = extract_text_from_attributed_body(row["attributedBody"])
        text = clean_text(text)

        dt = apple_ts_to_datetime(row["date"])
        handle_id = row["handle_id"]
        sender = "Me" if row["is_from_me"] else handle_map.get(handle_id, "Unknown")

        messages.append(
            {
                "rowid": row["ROWID"],
                "attachments": attachments.get(row["ROWID"], []),
                "guid": row["guid"],
                "text": text,
                "date": dt,
                "sender": sender,
                "is_from_me": row["is_from_me"],
                "thread_originator_guid": row["thread_originator_guid"],
                "associated_message_guid": row["associated_message_guid"],
                "associated_message_type": row["associated_message_type"],
                "associated_message_emoji": row["associated_message_emoji"],
                "group_title": row["group_title"],
                "is_audio_message": row["is_audio_message"],
                "item_type": row["item_type"],
            }
        )

    return messages


def fetch_thread_originator_messages(
    conn: sqlite3.Connection,
    chat_id: int,
    guids: set[str],
    already_fetched: set[str],
) -> dict[str, dict]:
    """Fetch messages by GUID that are outside the time range (for thread context)."""
    missing = guids - already_fetched
    if not missing:
        return {}

    handle_map = get_handle_map(conn)
    placeholders = ",".join("?" for _ in missing)
    query = f"""
        SELECT
            m.ROWID, m.guid, m.text, m.attributedBody, m.date,
            m.is_from_me, m.handle_id
        FROM message m
        JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
        WHERE cmj.chat_id = ?
          AND m.guid IN ({placeholders})
    """
    params = [chat_id] + list(missing)
    rows = conn.execute(query, params).fetchall()

    attachments = get_attachments(conn, [row["ROWID"] for row in rows])

    result = {}
    for row in rows:
        text = row["text"]
        if text is None and row["attributedBody"] is not None:
            text = extract_text_from_attributed_body(row["attributedBody"])
        sender = "Me" if row["is_from_me"] else handle_map.get(row["handle_id"], "Unknown")
        result[row["guid"]] = {
            "text": clean_text(text),
            "sender": sender,
            "date": apple_ts_to_datetime(row["date"]),
            "attachments": attachments.get(row["ROWID"], []),
            "earlier": True,
        }
    return result


# ---------------------------------------------------------------------------
# Contacts resolution
# ---------------------------------------------------------------------------


ADDRESSBOOK_GLOB = (
    "Library/Application Support/AddressBook/Sources/*/AddressBook-v22.abcddb"
)


def phone_key(identifier: str) -> str:
    """Normalize a phone number for matching: last 10 digits, or the raw string."""
    digits = re.sub(r"\D", "", identifier or "")
    if len(digits) >= 10:
        return digits[-10:]
    return (identifier or "").strip().lower()


def load_contacts() -> dict[str, str]:
    """Map normalized phone/email → contact name from the macOS Contacts DB.

    Returns an empty dict when Contacts is unavailable; name resolution is a
    convenience, never a hard requirement.
    """
    names: dict[str, str] = {}
    for db in sorted(Path.home().glob(ADDRESSBOOK_GLOB)):
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            for table, column in (
                ("ZABCDPHONENUMBER", "ZFULLNUMBER"),
                ("ZABCDEMAILADDRESS", "ZADDRESS"),
            ):
                try:
                    rows = conn.execute(
                        f"""
                        SELECT r.ZFIRSTNAME, r.ZLASTNAME, r.ZORGANIZATION, t.{column}
                        FROM {table} t
                        JOIN ZABCDRECORD r ON r.Z_PK = t.ZOWNER
                        WHERE t.{column} IS NOT NULL
                        """
                    ).fetchall()
                except sqlite3.Error:
                    continue
                for first, last, org, value in rows:
                    name = " ".join(p for p in (first, last) if p) or org
                    if not name:
                        continue
                    key = phone_key(value) if column == "ZFULLNUMBER" else value.strip().lower()
                    names.setdefault(key, name.strip())
        finally:
            conn.close()
    return names


def resolve_name(identifier: str, contacts: dict[str, str]) -> str | None:
    """Look up an identifier in the contacts map, trying phone and email forms."""
    if not identifier:
        return None
    return contacts.get(phone_key(identifier)) or contacts.get(identifier.strip().lower())


# ---------------------------------------------------------------------------
# Alias store
# ---------------------------------------------------------------------------


ALIAS_PATH = Path.home() / ".config" / "imessage-exporter" / "aliases.json"


def load_aliases() -> dict[str, str]:
    """Load the persisted identifier → alias map."""
    try:
        return json.loads(ALIAS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_aliases(aliases: dict[str, str]) -> None:
    """Persist the identifier → alias map, merging with what's already stored."""
    merged = load_aliases()
    merged.update(aliases)
    try:
        ALIAS_PATH.parent.mkdir(parents=True, exist_ok=True)
        ALIAS_PATH.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    except OSError as e:
        print(f"Warning: could not save aliases: {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Scrubbing
# ---------------------------------------------------------------------------


EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
# 10+ digit phone numbers in common formats. Requires 3-3-4 grouping so it
# cannot swallow dates or times.
PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?\d{1,3}[ .\-]?)?\(?\d{3}\)?[ .\-]?\d{3}[ .\-]?\d{4}(?!\d)"
)
# Long digit runs — card, account, policy numbers.
LONG_DIGITS_RE = re.compile(r"(?<!\d)\d{12,}(?!\d)")


class Scrubber:
    """Replaces identifiers with stable tokens so the AI can still track people.

    The same number always maps to the same token within an export, which keeps
    the conversation followable without exposing the real value.
    """

    def __init__(self) -> None:
        self.seen: dict[str, str] = {}
        self.counts: dict[str, int] = {}

    def _token(self, kind: str, value: str) -> str:
        if value not in self.seen:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            self.seen[value] = f"[{kind}-{self.counts[kind]}]"
        return self.seen[value]

    def scrub(self, text: str | None) -> str | None:
        if not text:
            return text
        text = EMAIL_RE.sub(lambda m: self._token("email", m.group(0)), text)
        text = PHONE_RE.sub(lambda m: self._token("phone", m.group(0)), text)
        text = LONG_DIGITS_RE.sub(lambda m: self._token("number", m.group(0)), text)
        return text


# ---------------------------------------------------------------------------
# Message filters
# ---------------------------------------------------------------------------


def clean_target_guid(guid: str) -> str:
    """Strip the p:0/ / bp: prefixes iMessage puts on tapback target guids."""
    guid = guid or ""
    for prefix in ("p:0/", "p:1/", "bp:"):
        if guid.startswith(prefix):
            return guid[len(prefix) :]
    return guid


def is_tapback(msg: dict) -> bool:
    assoc = msg["associated_message_type"] or 0
    return 2000 <= assoc <= CUSTOM_TAPBACK_TYPE or 3000 <= assoc <= 3006


def limit_messages(messages: list[dict], limit: int) -> list[dict]:
    """Keep only the most recent `limit` content messages, plus their tapbacks."""
    content = [m for m in messages if not is_tapback(m)]
    if len(content) <= limit:
        return messages
    kept = content[-limit:]
    kept_guids = {m["guid"] for m in kept}
    return [
        m
        for m in messages
        if (not is_tapback(m) and m["guid"] in kept_guids)
        or (is_tapback(m) and clean_target_guid(m["associated_message_guid"]) in kept_guids)
    ]


def search_messages(
    messages: list[dict], term: str, context: int = 5
) -> list[dict]:
    """Keep messages matching `term` plus `context` messages either side.

    Gaps are annotated on the following message so the transcript can show
    how much was skipped rather than silently splicing.
    """
    term_lower = term.lower()
    content = [m for m in messages if not is_tapback(m)]

    def matches(msg: dict) -> bool:
        if msg["text"] and term_lower in msg["text"].lower():
            return True
        # Attachment filenames are searchable too — "quote" should find quote.pdf.
        return any(
            name and term_lower in name.lower()
            for _, name in (msg.get("attachments") or [])
        )

    keep_idx: set[int] = set()
    for i, msg in enumerate(content):
        if matches(msg):
            keep_idx.update(range(max(0, i - context), min(len(content), i + context + 1)))

    if not keep_idx:
        return []

    kept = []
    previous = None
    for i in sorted(keep_idx):
        msg = dict(content[i])
        if previous is not None and i > previous + 1:
            msg["_gap_before"] = i - previous - 1
        kept.append(msg)
        previous = i

    kept_guids = {m["guid"] for m in kept}
    tapbacks = [
        m
        for m in messages
        if is_tapback(m)
        and clean_target_guid(m["associated_message_guid"]) in kept_guids
    ]
    return sorted(kept + tapbacks, key=lambda m: m["rowid"] if "rowid" in m else 0)


# ---------------------------------------------------------------------------
# Size estimate and output delivery
# ---------------------------------------------------------------------------


TOKEN_WARN_THRESHOLD = 150_000


def estimate_tokens(text: str) -> int:
    """Rough token estimate — ~4 characters per token is close enough to warn on."""
    return len(text) // 4


def report_size(text: str, stream=None) -> None:
    """Print size of the export, warning when it is likely to blow a context window."""
    # Resolved at call time so redirected streams are honoured.
    stream = stream if stream is not None else sys.stderr
    tokens = estimate_tokens(text)
    lines = text.count("\n")
    print(f"  {lines:,} lines, ~{tokens:,} tokens", file=stream)
    if tokens > TOKEN_WARN_THRESHOLD:
        print(
            f"  Warning: ~{tokens:,} tokens may exceed the model's context window.\n"
            f"  Narrow it with --since, --last N, or --search TERM.",
            file=stream,
        )


def copy_to_clipboard(text: str) -> bool:
    """Copy text to the macOS clipboard. Returns False if pbcopy is unavailable."""
    try:
        proc = subprocess.run(
            ["pbcopy"], input=text.encode("utf-8"), check=True
        )
        return proc.returncode == 0
    except (OSError, subprocess.CalledProcessError):
        return False


def deliver(text: str, out_path: str | None, to_stdout: bool, to_clipboard: bool) -> None:
    """Write the export to wherever the user asked for it."""
    if to_stdout:
        sys.stdout.write(text)
    if to_clipboard:
        if copy_to_clipboard(text):
            print("Copied to clipboard.", file=sys.stderr)
        else:
            print("Error: could not copy to clipboard (pbcopy failed).", file=sys.stderr)
    if out_path:
        Path(out_path).write_text(text, encoding="utf-8")
        print(f"Exported to {out_path}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Formatting / export
# ---------------------------------------------------------------------------


def truncate(text: str, max_len: int = 40) -> str:
    """Truncate text with ellipsis."""
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def format_messages(
    messages: list[dict],
    guid_lookup: dict[str, dict],
) -> str:
    """Format messages into export text with threading and stable message IDs."""
    # Pass 1: separate tapbacks from renderable content, preserving chronology.
    tapbacks: dict[str, list[str]] = {}
    entries: list[dict] = []

    for msg in messages:
        assoc_type = msg["associated_message_type"] or 0
        if 2000 <= assoc_type <= 2005 or assoc_type == CUSTOM_TAPBACK_TYPE:
            # This is a tapback reaction
            target_guid = msg["associated_message_guid"] or ""
            # Clean the guid — sometimes prefixed with "p:0/" or "bp:"
            for prefix in ("p:0/", "p:1/", "bp:"):
                if target_guid.startswith(prefix):
                    target_guid = target_guid[len(prefix) :]
                    break
            if assoc_type == CUSTOM_TAPBACK_TYPE:
                # Arbitrary emoji; fall back if the column is unavailable.
                emoji = msg.get("associated_message_emoji") or "emoji"
            else:
                emoji = TAPBACK_TYPES.get(assoc_type, "?")
            tapbacks.setdefault(target_guid, []).append(
                f"{emoji} by {msg['sender']}"
            )
            continue

        if 3000 <= assoc_type <= 3006:
            # Remove tapback — skip
            continue

        if msg["item_type"] and msg["item_type"] != 0:
            # System message — kept in place chronologically
            if msg["group_title"]:
                entries.append(
                    {
                        "kind": "system",
                        "date": msg["date"],
                        "content": f'[System: group name changed to "{msg["group_title"]}"]',
                    }
                )
            continue

        # Determine content
        att_str = format_attachments(msg.get("attachments") or [])
        if msg["is_audio_message"]:
            content = "[Audio message]"
        elif msg["text"] and att_str:
            content = f"{msg['text']}  {att_str}"
        elif msg["text"]:
            content = msg["text"]
        elif att_str:
            # Attachment-only message — previously dropped entirely.
            content = att_str
        else:
            # Nothing renderable — skip
            continue

        if msg.get("_gap_before"):
            entries.append(
                {
                    "kind": "gap",
                    "content": f"[… {msg['_gap_before']} messages omitted …]",
                }
            )
        entries.append({"kind": "msg", "msg": msg, "content": content})

    # Pass 2: number renderable messages so replies can cite them by ID.
    guid_to_id: dict[str, int] = {}
    next_id = 1
    for entry in entries:
        if entry["kind"] == "msg":
            entry["id"] = next_id
            guid_to_id[entry["msg"]["guid"]] = next_id
            next_id += 1

    # Pass 3: render
    lines: list[str] = []
    for entry in entries:
        if entry["kind"] == "gap":
            lines.append(entry["content"])
            continue

        if entry["kind"] == "system":
            dt_str = f"{entry['date']:%Y-%m-%d %H:%M}" if entry["date"] else "unknown"
            lines.append(f"[{dt_str}] {entry['content']}")
            continue

        msg = entry["msg"]
        content = entry["content"]
        msg_id = f"[#{entry['id']}]"
        dt_str = f"{msg['date']:%Y-%m-%d %H:%M}" if msg["date"] else "unknown"

        # Tapback annotations for this message
        reactions = tapbacks.get(msg["guid"], [])
        reaction_str = f"  ({', '.join(reactions)})" if reactions else ""

        originator_guid = msg["thread_originator_guid"]
        if originator_guid:
            # This is a threaded reply
            orig = guid_lookup.get(originator_guid)
            if orig:
                orig_text = orig.get("text", "")
                if orig_text:
                    quote = truncate(orig_text)
                else:
                    # Quote the attachment itself rather than a bare "[attachment]".
                    quote = format_attachments(orig.get("attachments") or []) or "[attachment]"
                # Cite the ID when the original is in this export; flag it when not.
                orig_id = guid_to_id.get(originator_guid)
                ref = f"#{orig_id} " if orig_id else ""
                earlier_tag = "" if orig_id else " [earlier message]"
                lines.append(
                    f'  \u21b3 {msg_id} [{dt_str}] {msg["sender"]}: '
                    f'(replying to {ref}"{quote}"{earlier_tag}): {content}{reaction_str}'
                )
            else:
                lines.append(
                    f'  \u21b3 {msg_id} [{dt_str}] {msg["sender"]}: '
                    f"(replying to unknown message): {content}{reaction_str}"
                )
        else:
            lines.append(f'{msg_id} [{dt_str}] {msg["sender"]}: {content}{reaction_str}')

    return "\n".join(lines)


def build_export(
    conn: sqlite3.Connection,
    chat_info: dict,
    since: datetime,
    until: datetime | None,
    aliases: dict[str, str] | None = None,
    scrub: bool = False,
    limit: int | None = None,
    search: str | None = None,
    context: int = 5,
) -> str:
    """Build the full export text for a conversation.

    Aliases are applied at the message level (senders and body text) rather than
    to the finished string, so scrubbing sees the aliased form and does not
    tokenize names the user has deliberately chosen.
    """
    aliases = aliases or {}
    handle_map = get_handle_map(conn)
    chat_id = chat_info["chat_id"]

    # Get participants
    participants = chat_info.get("participants") or get_chat_participants(
        conn, chat_id, handle_map
    )

    # Fetch messages
    messages = fetch_messages(conn, chat_id, since, until)

    if not messages:
        return ""

    if search:
        messages = search_messages(messages, search, context)
        if not messages:
            return ""
    if limit:
        messages = limit_messages(messages, limit)

    # Build guid lookup from fetched messages
    guid_lookup: dict[str, dict] = {}
    for msg in messages:
        guid_lookup[msg["guid"]] = msg

    # Find thread originators outside the time range
    originator_guids = set()
    for msg in messages:
        if msg["thread_originator_guid"]:
            originator_guids.add(msg["thread_originator_guid"])

    extra = fetch_thread_originator_messages(
        conn, chat_id, originator_guids, set(guid_lookup.keys())
    )
    guid_lookup.update(extra)

    # Apply aliases, then scrub whatever identifiers remain.
    scrubber = Scrubber() if scrub else None

    def rewrite(value: str | None) -> str | None:
        value = apply_name_map(value, aliases) if value else value
        return scrubber.scrub(value) if scrubber else value

    for record in list(messages) + list(extra.values()):
        record["sender"] = rewrite(record.get("sender"))
        record["text"] = rewrite(record.get("text"))

    participants = [rewrite(p) for p in participants]
    label = rewrite(chat_info["label"]) or chat_info["label"]

    participant_str = ", ".join(p for p in participants if p)
    if participant_str:
        participant_str += ", Me"
    else:
        participant_str = "Me"

    # Format
    body = format_messages(messages, guid_lookup)
    msg_count = sum(
        1
        for m in messages
        if (m["associated_message_type"] or 0) < 2000
        and (m["item_type"] or 0) == 0
        and (m["text"] or m["is_audio_message"] or m["attachments"])
    )

    until_str = f"{until:%Y-%m-%d}" if until else "now"
    now_str = datetime.now(tz=TZ).strftime("%Y-%m-%d %H:%M %Z")

    # Tell the reader when the transcript is deliberately partial or redacted.
    notes = []
    if search:
        notes.append(
            f'# Filter: only messages matching "{search}" '
            f"(+/- {context} for context); gaps marked inline"
        )
    if limit:
        notes.append(f"# Filter: most recent {limit} messages only")
    if scrub:
        notes.append(
            "# Redacted: phone numbers, emails and long digit runs are replaced\n"
            "#   with stable tokens ([phone-1], [email-1]); the same token always\n"
            "#   means the same value"
        )
    notes_str = ("\n".join(notes) + "\n") if notes else ""

    header = textwrap.dedent(f"""\
        # iMessage Export
        # Conversation: {label}
        # Participants: {participant_str}
        # Period: {since:%Y-%m-%d} to {until_str}
        # Exported: {now_str}
        # Message count: {msg_count}
        #
        # Legend:
        #   [#N] = message ID — cite these to refer to a specific message
        #   ↳ = reply to a previous message (thread); "replying to #N" names the target
        #   (emoji by X) = tapback reaction on that message
        #   [Photo] / [PDF: name] etc. = attachment sent in that message
        #   Messages are in chronological order
        #   Threaded replies are indented under their context
        # ---
    """)

    # Spliced in after dedent: these lines are already at column 0, and
    # interpolating them earlier would defeat textwrap.dedent entirely.
    if notes_str:
        header = header.replace("#\n# Legend:", notes_str + "#\n# Legend:")

    return header + "\n" + body + "\n"


# ---------------------------------------------------------------------------
# CLI — interactive mode
# ---------------------------------------------------------------------------


def list_conversations(
    conn: sqlite3.Connection, days: int, contacts: dict[str, str]
) -> tuple[list[dict], int]:
    """Fetch conversations, widening the window when the default turns up nothing.

    The picker is the only way to discover a chat you can't name, so an empty
    list is a dead end rather than a useful answer.
    """
    conversations = get_recent_conversations(conn, days, contacts)
    if conversations:
        return conversations, days
    for wider in (30, 90, 365):
        if wider <= days:
            continue
        conversations = get_recent_conversations(conn, wider, contacts)
        if conversations:
            print(
                f"No conversations in the past {days} days — "
                f"widened to {wider}.",
                file=sys.stderr,
            )
            return conversations, wider
    return [], days


def print_conversations(conversations: list[dict], days: int) -> None:
    """Print the numbered conversation picker list."""
    print(f"\nRecent conversations (past {days} days):\n")
    label_width = max(len(c["label"]) for c in conversations)
    label_width = min(label_width, 40)
    for i, c in enumerate(conversations, 1):
        label = truncate(c["label"], label_width)
        print(
            f"  {i:>3}. {label:<{label_width}}  — "
            f"{c['msg_count']} messages, last: {c['last_relative']}"
        )
    print()


def interactive_select_conversation(
    conn: sqlite3.Connection, days: int, contacts: dict[str, str]
) -> dict | None:
    """Show recent conversations and let the user pick one."""
    conversations, days = list_conversations(conn, days, contacts)

    if not conversations:
        print("No conversations found in the past year.")
        return None

    print_conversations(conversations, days)
    while True:
        try:
            choice = input("Select a conversation (number): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return None
        if not choice:
            continue
        try:
            idx = int(choice)
            if 1 <= idx <= len(conversations):
                selected = conversations[idx - 1]
                # Attach participants
                handle_map = get_handle_map(conn)
                selected["participants"] = get_chat_participants(
                    conn, selected["chat_id"], handle_map
                )
                return selected
            else:
                print(f"  Please enter a number between 1 and {len(conversations)}.")
        except ValueError:
            print("  Please enter a valid number.")


def interactive_select_period() -> tuple[datetime, datetime | None]:
    """Let the user pick a time period. Returns (since, until)."""
    print("\nExport messages from:")
    print("  1. Past 24 hours")
    print("  2. Past 3 days")
    print("  3. Past 7 days")
    print("  4. Past 30 days")
    print("  5. Custom date range")
    print()
    print("  Or type a duration: e.g. 3h, 30m, 2w, 10d")
    print()

    periods = {
        "1": timedelta(hours=24),
        "2": timedelta(days=3),
        "3": timedelta(days=7),
        "4": timedelta(days=30),
    }

    while True:
        try:
            choice = input("Select time period (number or duration): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)

        if choice in periods:
            since = datetime.now(tz=TZ) - periods[choice]
            return since, None

        if choice == "5":
            return interactive_custom_range()

        # Try parsing as a freeform duration
        delta = parse_duration(choice)
        if delta is not None:
            since = datetime.now(tz=TZ) - delta
            return since, None

        print("  Please enter 1-5 or a duration like 3h, 30m, 2w, 10d.")


def interactive_custom_range() -> tuple[datetime, datetime | None]:
    """Prompt for custom start/end dates."""
    print("\n  Enter dates as YYYY-MM-DD")

    while True:
        try:
            start_str = input("  Start date: ").strip()
            start = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=TZ)
            break
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        except ValueError:
            print("  Invalid date format. Use YYYY-MM-DD.")

    while True:
        try:
            end_str = input("  End date (leave blank for now): ").strip()
            if not end_str:
                return start, None
            end = datetime.strptime(end_str, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, tzinfo=TZ
            )
            return start, end
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        except ValueError:
            print("  Invalid date format. Use YYYY-MM-DD.")


def interactive_name_participants(
    participants: list[str],
    aliases: dict[str, str],
    contacts: dict[str, str],
) -> dict[str, str]:
    """Prompt the user to assign display names to participants.

    Saved aliases are offered as the default, and Contacts supplies a suggestion
    when there is no alias yet, so the same person keeps the same name across
    exports without retyping.
    """
    if not participants:
        return {}

    print("\nParticipants in this conversation:")
    for i, p in enumerate(participants, 1):
        known = aliases.get(p) or resolve_name(p, contacts)
        print(f"  {i}. {p}" + (f"  → {known}" if known else ""))

    print("\nYou can assign names to make the export easier to read.")
    print("Press Enter to accept the suggestion (or keep the identifier).\n")

    name_map: dict[str, str] = {}
    for p in participants:
        suggestion = aliases.get(p) or resolve_name(p, contacts)
        prompt = f"  Name for {p} [{suggestion or 'keep as-is'}]: "
        try:
            name = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        chosen = name or suggestion
        if chosen:
            name_map[p] = chosen

    return name_map


def apply_name_map(text: str, name_map: dict[str, str]) -> str:
    """Replace all participant identifiers in text with their assigned names."""
    for identifier, name in name_map.items():
        text = text.replace(identifier, name)
    return text


def default_output_path(label: str) -> str:
    """Generate a default output filename from the conversation label."""
    safe = re.sub(r"[^\w\s\-]", "", label).strip()
    safe = re.sub(r"\s+", "_", safe)
    if not safe:
        safe = "conversation"
    return f"{safe}_export.txt"


def interactive_mode(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    """Run the full interactive flow."""
    contacts = {} if args.no_contacts else load_contacts()
    stored = {} if args.no_aliases else load_aliases()

    chat_info = interactive_select_conversation(conn, args.list_days, contacts)
    if not chat_info:
        return

    # Offer to assign names to participants
    participants = chat_info.get("participants", [])
    name_map = interactive_name_participants(participants, stored, contacts)

    since, until = interactive_select_period()

    label = apply_name_map(chat_info["label"], name_map)
    default_path = default_output_path(label)
    print("\nOutput: a path, 'clip' for the clipboard, or '-' for stdout")
    try:
        out_choice = input(f"Output file [{default_path}]: ").strip() or default_path
    except (EOFError, KeyboardInterrupt):
        print()
        return

    print(f"\nExporting messages from '{label}'...", file=sys.stderr)

    export_text = build_export(
        conn, chat_info, since, until,
        aliases=name_map, scrub=args.scrub,
        limit=args.last, search=args.search, context=args.context,
    )

    if not export_text:
        print("No messages found in the selected time period.")
        return

    if name_map and not args.no_aliases:
        save_aliases(name_map)

    report_size(export_text)
    to_stdout = out_choice == "-"
    to_clip = out_choice.lower() == "clip"
    deliver(
        export_text,
        None if (to_stdout or to_clip) else out_choice,
        to_stdout,
        to_clip,
    )


# ---------------------------------------------------------------------------
# CLI — argument parsing
# ---------------------------------------------------------------------------


def parse_duration(value: str) -> timedelta | None:
    """Parse a duration string like '3h', '7d', '2w', '30m'. Returns None if not a duration."""
    match = re.match(r"^(\d+)(m|h|d|w)$", value)
    if not match:
        return None
    amount = int(match.group(1))
    unit = match.group(2)
    if unit == "m":
        return timedelta(minutes=amount)
    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    return None


def parse_since(value: str) -> datetime:
    """Parse --since value: '30m', '3h', '7d', '2w', or 'YYYY-MM-DD'."""
    delta = parse_duration(value)
    if delta is not None:
        return datetime.now(tz=TZ) - delta

    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=TZ)
    except ValueError:
        print(
            f"Error: Invalid --since value '{value}'. "
            "Use '30m', '3h', '7d', '2w', or 'YYYY-MM-DD'.",
            file=sys.stderr,
        )
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export iMessage conversations to text files.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            examples:
              %(prog)s                                          Interactive mode
              %(prog)s --list                                   List recent conversations
              %(prog)s --chat "Family Chat" --since 7d          Export last 7 days
              %(prog)s --chat "Family Chat" --since 3h          Export last 3 hours
              %(prog)s --chat "+15551234567" --since 2024-01-01 --until 2024-01-31
              %(prog)s --chat "Family Chat" --since 7d --names "+15551234567=Alice" "+15559876543=Bob"
              %(prog)s --chat "Mike" --last 200 --copy          Recent 200 messages to clipboard
              %(prog)s --chat "Mike" --search "deadline"        Just the deadline argument
              %(prog)s --chat "Mike" --since 30d --scrub        Redact numbers/emails
              %(prog)s --list --list-days 90                    Widen the conversation list

            duration formats: 30m (minutes), 3h (hours), 7d (days), 2w (weeks)
        """),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List recent conversations and exit",
    )
    parser.add_argument(
        "--chat",
        metavar="NAME_OR_NUMBER",
        help="Conversation to export (display name, phone number, or email)",
    )
    parser.add_argument(
        "--since",
        metavar="PERIOD",
        help="Start of export period: 30m, 3h, 7d, 2w, or YYYY-MM-DD",
    )
    parser.add_argument(
        "--until",
        metavar="DATE",
        help="End of export period: 'YYYY-MM-DD' (default: now)",
    )
    parser.add_argument(
        "--output", "-o",
        metavar="FILE",
        help="Output file path (default: auto-generated from conversation name)",
    )
    parser.add_argument(
        "--names",
        metavar="MAPPING",
        nargs="+",
        help='Replace identifiers with names: "+15551234567=Alice" "user@example.com=Bob"',
    )
    parser.add_argument(
        "--last",
        metavar="N",
        type=int,
        help="Export only the most recent N messages",
    )
    parser.add_argument(
        "--search",
        metavar="TERM",
        help="Export only messages matching TERM, with surrounding context",
    )
    parser.add_argument(
        "--context",
        metavar="N",
        type=int,
        default=5,
        help="Messages of context on each side of a --search hit (default: 5)",
    )
    parser.add_argument(
        "--scrub",
        action="store_true",
        help="Replace phone numbers, emails and long digit runs with stable tokens",
    )
    parser.add_argument(
        "--list-days",
        metavar="N",
        type=int,
        default=7,
        help="How far back the conversation list looks (default: 7)",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="Write the export to stdout instead of a file",
    )
    parser.add_argument(
        "--copy",
        action="store_true",
        help="Copy the export to the clipboard instead of a file",
    )
    parser.add_argument(
        "--save-aliases",
        action="store_true",
        help="Persist the --names mappings for reuse in later exports",
    )
    parser.add_argument(
        "--no-aliases",
        action="store_true",
        help="Ignore saved aliases for this run",
    )
    parser.add_argument(
        "--no-contacts",
        action="store_true",
        help="Do not resolve names from the macOS Contacts database",
    )

    args = parser.parse_args()

    conn = open_db()

    try:
        contacts = {} if args.no_contacts else load_contacts()

        if args.list:
            conversations, days = list_conversations(conn, args.list_days, contacts)
            if not conversations:
                print("No conversations found in the past year.")
                return
            print_conversations(conversations, days)
            return

        if args.chat:
            # Non-interactive export
            chat_info = find_chat_by_identifier(conn, args.chat)
            if not chat_info:
                print(
                    f"Error: No conversation found matching '{args.chat}'.",
                    file=sys.stderr,
                )
                print("Use --list to see available conversations.", file=sys.stderr)
                sys.exit(1)

            since = parse_since(args.since) if args.since else (
                datetime.now(tz=TZ) - timedelta(days=7)
            )
            until = None
            if args.until:
                try:
                    until = datetime.strptime(args.until, "%Y-%m-%d").replace(
                        hour=23, minute=59, second=59, tzinfo=TZ
                    )
                except ValueError:
                    print(
                        f"Error: Invalid --until date '{args.until}'. Use YYYY-MM-DD.",
                        file=sys.stderr,
                    )
                    sys.exit(1)

            # Saved aliases first, then Contacts, then explicit --names wins.
            name_map: dict[str, str] = {} if args.no_aliases else load_aliases()
            for participant in chat_info.get("participants", []):
                if participant not in name_map:
                    resolved = resolve_name(participant, contacts)
                    if resolved:
                        name_map[participant] = resolved
            explicit: dict[str, str] = {}
            if args.names:
                for mapping in args.names:
                    if "=" not in mapping:
                        print(
                            f"Error: Invalid --names format '{mapping}'. "
                            'Use "identifier=Name".',
                            file=sys.stderr,
                        )
                        sys.exit(1)
                    ident, name = mapping.split("=", 1)
                    explicit[ident.strip()] = name.strip()
            name_map.update(explicit)

            label = apply_name_map(chat_info["label"], name_map)

            to_file = not (args.stdout or args.copy)
            out_path = (args.output or default_output_path(label)) if to_file else None
            print(f"Exporting messages from '{label}'...", file=sys.stderr)
            export_text = build_export(
                conn, chat_info, since, until,
                aliases=name_map, scrub=args.scrub,
                limit=args.last, search=args.search, context=args.context,
            )

            if not export_text:
                if args.search:
                    print(f"No messages matching '{args.search}' in that period.")
                else:
                    print("No messages found in the selected time period.")
                return

            if args.save_aliases and explicit:
                save_aliases(explicit)
                print(f"Saved {len(explicit)} alias(es) to {ALIAS_PATH}", file=sys.stderr)

            report_size(export_text)
            deliver(export_text, out_path, args.stdout, args.copy)
            return

        # No flags → interactive mode
        interactive_mode(conn, args)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
