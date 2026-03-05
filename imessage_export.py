#!/usr/bin/env python3
"""iMessage Conversation Exporter CLI for macOS.

Reads ~/Library/Messages/chat.db (read-only) and exports conversations
to plain text files with inline reply threading preserved.

Requires Full Disk Access (System Settings → Privacy & Security).
"""

import argparse
import os
import re
import sqlite3
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"
APPLE_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01

# Tapback reaction types
TAPBACK_TYPES = {
    2000: "❤️",
    2001: "👍",
    2002: "👎",
    2003: "😂",
    2004: "‼️",
    2005: "❓",
    # 3000-3005 = remove tapback (we just skip these)
}

# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------


def apple_ts_to_datetime(apple_ns: int) -> datetime:
    """Convert Apple Core Data nanosecond timestamp to UTC datetime."""
    if apple_ns is None or apple_ns == 0:
        return None
    unix_ts = apple_ns / 1_000_000_000 + APPLE_EPOCH_OFFSET
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc)


def datetime_to_apple_ts(dt: datetime) -> int:
    """Convert datetime to Apple Core Data nanosecond timestamp."""
    unix_ts = dt.timestamp()
    return int((unix_ts - APPLE_EPOCH_OFFSET) * 1_000_000_000)


def format_relative_time(dt: datetime) -> str:
    """Format a datetime as a human-readable relative time string."""
    now = datetime.now(tz=timezone.utc)
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
    conn: sqlite3.Connection, days: int = 7
) -> list[dict]:
    """Fetch conversations with activity in the past N days."""
    handle_map = get_handle_map(conn)
    cutoff = datetime_to_apple_ts(datetime.now(tz=timezone.utc) - timedelta(days=days))

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

        # Determine display label
        if display_name:
            label = display_name
        elif style == 43:
            # Group chat without a name — list participants
            participants = get_chat_participants(conn, chat_id, handle_map)
            if participants:
                label = ", ".join(participants[:4])
                if len(participants) > 4:
                    label += f" +{len(participants) - 4} more"
            else:
                label = chat_identifier
        else:
            # DM — use the chat_identifier (phone/email)
            label = chat_identifier

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

    messages = []
    for row in rows:
        text = row["text"]
        if text is None and row["attributedBody"] is not None:
            text = extract_text_from_attributed_body(row["attributedBody"])

        dt = apple_ts_to_datetime(row["date"])
        handle_id = row["handle_id"]
        sender = "Me" if row["is_from_me"] else handle_map.get(handle_id, "Unknown")

        messages.append(
            {
                "rowid": row["ROWID"],
                "guid": row["guid"],
                "text": text,
                "date": dt,
                "sender": sender,
                "is_from_me": row["is_from_me"],
                "thread_originator_guid": row["thread_originator_guid"],
                "associated_message_guid": row["associated_message_guid"],
                "associated_message_type": row["associated_message_type"],
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

    result = {}
    for row in rows:
        text = row["text"]
        if text is None and row["attributedBody"] is not None:
            text = extract_text_from_attributed_body(row["attributedBody"])
        sender = "Me" if row["is_from_me"] else handle_map.get(row["handle_id"], "Unknown")
        result[row["guid"]] = {
            "text": text,
            "sender": sender,
            "date": apple_ts_to_datetime(row["date"]),
            "earlier": True,
        }
    return result


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
    """Format messages into export text with threading."""
    lines: list[str] = []

    # Collect tapbacks per message guid
    tapbacks: dict[str, list[str]] = {}
    normal_messages: list[dict] = []

    for msg in messages:
        assoc_type = msg["associated_message_type"] or 0
        if 2000 <= assoc_type <= 2005:
            # This is a tapback reaction
            target_guid = msg["associated_message_guid"] or ""
            # Clean the guid — sometimes prefixed with "p:0/" or "bp:"
            for prefix in ("p:0/", "p:1/", "bp:"):
                if target_guid.startswith(prefix):
                    target_guid = target_guid[len(prefix) :]
                    break
            emoji = TAPBACK_TYPES.get(assoc_type, "?")
            tapbacks.setdefault(target_guid, []).append(
                f"{emoji} by {msg['sender']}"
            )
        elif 3000 <= assoc_type <= 3005:
            # Remove tapback — skip
            continue
        elif msg["item_type"] and msg["item_type"] != 0:
            # System message
            if msg["group_title"]:
                lines.append(
                    f"[{msg['date']:%Y-%m-%d %H:%M}] [System: group name changed to \"{msg['group_title']}\"]"
                )
            continue
        else:
            normal_messages.append(msg)

    for msg in normal_messages:
        dt_str = f"{msg['date']:%Y-%m-%d %H:%M}" if msg["date"] else "unknown"

        # Determine content
        if msg["is_audio_message"]:
            content = "[Audio message]"
        elif msg["text"]:
            content = msg["text"]
        else:
            # No text at all — skip
            continue

        # Tapback annotations for this message
        reactions = tapbacks.get(msg["guid"], [])
        reaction_str = f"  ({', '.join(reactions)})" if reactions else ""

        originator_guid = msg["thread_originator_guid"]
        if originator_guid:
            # This is a threaded reply
            orig = guid_lookup.get(originator_guid)
            if orig:
                orig_text = orig.get("text", "")
                earlier_tag = " [earlier message]" if orig.get("earlier") else ""
                quote = truncate(orig_text) if orig_text else "[attachment]"
                lines.append(
                    f'  \u21b3 [{dt_str}] {msg["sender"]}: '
                    f'(replying to "{quote}"{earlier_tag}): {content}{reaction_str}'
                )
            else:
                lines.append(
                    f'  \u21b3 [{dt_str}] {msg["sender"]}: '
                    f"(replying to unknown message): {content}{reaction_str}"
                )
        else:
            lines.append(f'[{dt_str}] {msg["sender"]}: {content}{reaction_str}')

    return "\n".join(lines)


def build_export(
    conn: sqlite3.Connection,
    chat_info: dict,
    since: datetime,
    until: datetime | None,
) -> str:
    """Build the full export text for a conversation."""
    handle_map = get_handle_map(conn)
    chat_id = chat_info["chat_id"]

    # Get participants
    participants = chat_info.get("participants") or get_chat_participants(
        conn, chat_id, handle_map
    )
    participant_str = ", ".join(participants)
    if participant_str:
        participant_str += ", Me"
    else:
        participant_str = "Me"

    # Fetch messages
    messages = fetch_messages(conn, chat_id, since, until)

    if not messages:
        return ""

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

    # Format
    body = format_messages(messages, guid_lookup)
    msg_count = sum(
        1
        for m in messages
        if (m["associated_message_type"] or 0) < 2000
        and (m["item_type"] or 0) == 0
        and (m["text"] or m["is_audio_message"])
    )

    until_str = f"{until:%Y-%m-%d}" if until else "now"
    now_str = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    header = textwrap.dedent(f"""\
        # iMessage Export
        # Conversation: {chat_info['label']}
        # Participants: {participant_str}
        # Period: {since:%Y-%m-%d} to {until_str}
        # Exported: {now_str}
        # Message count: {msg_count}
        #
        # Legend:
        #   ↳ = reply to a previous message (thread)
        #   Messages are in chronological order
        #   Threaded replies are indented under their context
        # ---
    """)

    return header + "\n" + body + "\n"


# ---------------------------------------------------------------------------
# CLI — interactive mode
# ---------------------------------------------------------------------------


def interactive_select_conversation(conn: sqlite3.Connection) -> dict | None:
    """Show recent conversations and let the user pick one."""
    conversations = get_recent_conversations(conn)

    if not conversations:
        print("No conversations with activity in the past 7 days.")
        return None

    print("\nRecent conversations (past 7 days):\n")
    label_width = max(len(c["label"]) for c in conversations)
    label_width = min(label_width, 40)

    for i, c in enumerate(conversations, 1):
        label = truncate(c["label"], label_width)
        print(
            f"  {i:>3}. {label:<{label_width}}  — "
            f"{c['msg_count']} messages, last: {c['last_relative']}"
        )

    print()
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
            since = datetime.now(tz=timezone.utc) - periods[choice]
            return since, None

        if choice == "5":
            return interactive_custom_range()

        # Try parsing as a freeform duration
        delta = parse_duration(choice)
        if delta is not None:
            since = datetime.now(tz=timezone.utc) - delta
            return since, None

        print("  Please enter 1-5 or a duration like 3h, 30m, 2w, 10d.")


def interactive_custom_range() -> tuple[datetime, datetime | None]:
    """Prompt for custom start/end dates."""
    print("\n  Enter dates as YYYY-MM-DD")

    while True:
        try:
            start_str = input("  Start date: ").strip()
            start = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
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
                hour=23, minute=59, second=59, tzinfo=timezone.utc
            )
            return start, end
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(0)
        except ValueError:
            print("  Invalid date format. Use YYYY-MM-DD.")


def interactive_name_participants(participants: list[str]) -> dict[str, str]:
    """Prompt the user to assign display names to participants.

    Returns a mapping of identifier → display name. Identifiers the user
    skips (blank input) are kept as-is.
    """
    if not participants:
        return {}

    print("\nParticipants in this conversation:")
    for i, p in enumerate(participants, 1):
        print(f"  {i}. {p}")

    print(
        "\nYou can assign names to make the export easier to read."
    )
    print("Press Enter to keep the original identifier.\n")

    name_map: dict[str, str] = {}
    for p in participants:
        try:
            name = input(f"  Name for {p} [keep as-is]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if name:
            name_map[p] = name

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


def interactive_mode(conn: sqlite3.Connection) -> None:
    """Run the full interactive flow."""
    chat_info = interactive_select_conversation(conn)
    if not chat_info:
        return

    # Offer to assign names to participants
    participants = chat_info.get("participants", [])
    name_map = interactive_name_participants(participants)

    since, until = interactive_select_period()

    # Update label if a name was mapped
    label = chat_info["label"]
    if name_map:
        label = apply_name_map(label, name_map)

    default_path = default_output_path(label)
    try:
        out_path = input(f"\nOutput file [{default_path}]: ").strip() or default_path
    except (EOFError, KeyboardInterrupt):
        print()
        return

    print(f"\nExporting messages from '{label}'...")

    export_text = build_export(conn, chat_info, since, until)

    if not export_text:
        print("No messages found in the selected time period.")
        return

    if name_map:
        export_text = apply_name_map(export_text, name_map)

    Path(out_path).write_text(export_text, encoding="utf-8")
    print(f"Exported to {out_path}")


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
        return datetime.now(tz=timezone.utc) - delta

    try:
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)
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

    args = parser.parse_args()

    conn = open_db()

    try:
        if args.list:
            conversations = get_recent_conversations(conn)
            if not conversations:
                print("No conversations with activity in the past 7 days.")
                return
            print("\nRecent conversations (past 7 days):\n")
            label_width = max(len(c["label"]) for c in conversations)
            label_width = min(label_width, 40)
            for i, c in enumerate(conversations, 1):
                label = truncate(c["label"], label_width)
                print(
                    f"  {i:>3}. {label:<{label_width}}  — "
                    f"{c['msg_count']} messages, last: {c['last_relative']}"
                )
            print()
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
                datetime.now(tz=timezone.utc) - timedelta(days=7)
            )
            until = None
            if args.until:
                try:
                    until = datetime.strptime(args.until, "%Y-%m-%d").replace(
                        hour=23, minute=59, second=59, tzinfo=timezone.utc
                    )
                except ValueError:
                    print(
                        f"Error: Invalid --until date '{args.until}'. Use YYYY-MM-DD.",
                        file=sys.stderr,
                    )
                    sys.exit(1)

            # Parse --names mappings
            name_map: dict[str, str] = {}
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
                    name_map[ident.strip()] = name.strip()

            label = chat_info["label"]
            if name_map:
                label = apply_name_map(label, name_map)

            out_path = args.output or default_output_path(label)
            print(f"Exporting messages from '{label}'...")
            export_text = build_export(conn, chat_info, since, until)

            if not export_text:
                print("No messages found in the selected time period.")
                return

            if name_map:
                export_text = apply_name_map(export_text, name_map)

            Path(out_path).write_text(export_text, encoding="utf-8")
            print(f"Exported to {out_path}")
            return

        # No flags → interactive mode
        interactive_mode(conn)

    finally:
        conn.close()


if __name__ == "__main__":
    main()
