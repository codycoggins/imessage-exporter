"""Build a synthetic chat.db covering the edge cases the exporter has to handle.

Entirely fabricated data — the test suite never reads the real ~/Library/Messages
database, the real Contacts database, or the clipboard.
"""

import sqlite3
import time
from pathlib import Path

APPLE_EPOCH_OFFSET = 978307200

SCHEMA = """
CREATE TABLE handle (ROWID INTEGER PRIMARY KEY, id TEXT);
CREATE TABLE chat (ROWID INTEGER PRIMARY KEY, chat_identifier TEXT, display_name TEXT, style INT);
CREATE TABLE chat_handle_join (chat_id INT, handle_id INT);
CREATE TABLE chat_message_join (chat_id INT, message_id INT);
CREATE TABLE message (
  ROWID INTEGER PRIMARY KEY, guid TEXT, text TEXT, attributedBody BLOB, date INT,
  is_from_me INT, handle_id INT, thread_originator_guid TEXT,
  associated_message_guid TEXT, associated_message_type INT,
  associated_message_emoji TEXT, group_title TEXT, is_audio_message INT, item_type INT);
CREATE TABLE attachment (ROWID INTEGER PRIMARY KEY, mime_type TEXT, uti TEXT,
  transfer_name TEXT, is_sticker INT);
CREATE TABLE message_attachment_join (ROWID INTEGER PRIMARY KEY, message_id INT, attachment_id INT);
"""


def build_fixture(path: Path, days_ago: int = 3) -> Path:
    """Create the fixture database at `path`, anchored `days_ago` before now.

    Anchoring to the present keeps the conversation picker (which looks at
    recent activity) able to see the fixture chat.
    """
    path = Path(path)
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    conn.execute("INSERT INTO handle VALUES (1,'+15550000001')")
    conn.execute("INSERT INTO handle VALUES (2,'+15550000002')")
    conn.execute("INSERT INTO chat VALUES (1,'chat-fixture','Fixture Chat',43)")
    conn.executemany("INSERT INTO chat_handle_join VALUES (?,?)", [(1, 1), (1, 2)])

    base = int((time.time() - days_ago * 86400 - APPLE_EPOCH_OFFSET) * 10**9)

    def msg(rowid, guid, text, offset, from_me=0, handle=1, thread=None,
            assoc_guid=None, assoc_type=None, emoji=None, group_title=None,
            audio=0, item=0):
        conn.execute(
            "INSERT INTO message VALUES (?,?,?,NULL,?,?,?,?,?,?,?,?,?,?)",
            (rowid, guid, text, base + offset * 10**9, from_me, handle, thread,
             assoc_guid, assoc_type, emoji, group_title, audio, item))
        conn.execute("INSERT INTO chat_message_join VALUES (1,?)", (rowid,))

    def att(rowid, message_id, mime, uti, name, sticker=0):
        conn.execute("INSERT INTO attachment VALUES (?,?,?,?,?)",
                     (rowid, mime, uti, name, sticker))
        conn.execute("INSERT INTO message_attachment_join VALUES (?,?,?)",
                     (rowid, message_id, rowid))

    # A plain message that later collects reactions
    msg(1, "G1", "The quote came in at 12k", 0)
    # Attachment-only — must still appear as a turn
    msg(2, "G2", None, 60)
    att(1, 2, "application/pdf", "com.adobe.pdf", "quote.pdf")
    # Text plus several images, including a screenshot
    msg(3, "G3", "and here are the photos", 120, from_me=1)
    att(2, 3, "image/heic", "public.heic", "IMG_1.heic")
    att(3, 3, "image/jpeg", "public.jpeg", "IMG_2.jpeg")
    att(4, 3, "image/png", "public.png", "Screenshot 2024-01-01 at 1.00.00 PM.png")
    # Legacy tapback, and a custom-emoji tapback with a p:0/ prefixed target
    msg(4, "G4", None, 180, handle=2, assoc_guid="G1", assoc_type=2001)
    msg(5, "G5", None, 190, handle=2, assoc_guid="p:0/G1", assoc_type=2006, emoji="💯")
    msg(6, "G6", None, 200, from_me=1, assoc_guid="G2", assoc_type=2006, emoji="🙄")
    # Threaded reply that should cite its originator's ID
    msg(7, "G7", "12k is way over the estimate", 240, from_me=1, thread="G1")
    # Tapback removal — must render nothing
    msg(8, "G8", None, 250, handle=2, assoc_guid="G7", assoc_type=3006, emoji="💯")
    # System message, chronologically mid-conversation
    msg(9, "G9", None, 300, group_title="Reno Crew", item=2)
    msg(10, "G10", "which page is the labor line on?", 360, handle=2, thread="G2")
    # Link preview payload — must be suppressed, not labelled
    msg(11, "G11", "https://example.com/pricing", 420, handle=2)
    att(5, 11, None, "dyn.age81a5dzq7y066dbtf0g82peqf4hk2pdrb00n5xy",
        "x.pluginPayloadAttachment")
    # Reply whose originator is not in the chat at all
    msg(12, "G12", "as I said before", 480, from_me=1, thread="G-MISSING")
    msg(13, "G13", None, 540, handle=1, audio=1)
    msg(14, "G14", None, 600, handle=2)
    att(6, 14, "text/x-vlocation", "public.vlocation", "loc")
    att(7, 14, "text/vcard", "public.vcard", "card.vcf")
    # Control characters and an object-replacement marker leaking from parsing
    msg(15, "G15", "￼deal at 10k\x00\x01 final", 660, from_me=1)
    # Contact details typed into the body — what --names alone will not catch
    msg(16, "G16", "call me at 555-123-9876 or bob@example.com", 720, handle=2)

    conn.commit()
    conn.close()
    return path


if __name__ == "__main__":
    import sys
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("fixture.db")
    print(f"fixture written: {build_fixture(out)}")
