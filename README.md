# iMessage Exporter

A macOS CLI tool that exports iMessage conversations to plain text files with inline reply threading preserved. Designed to produce clean output you can share as context with an AI assistant.

## Quick Install

```bash
curl -fsSL https://raw.githubusercontent.com/vishoo7/imessage-exporter/main/install.sh | bash
```

This clones the repo to `~/.imessage-exporter` and creates the `imessage-export` command. Then just run:

```bash
imessage-export
```

> **Important:** Your terminal app needs **Full Disk Access** to read iMessage data.
> Grant it in: System Settings → Privacy & Security → Full Disk Access

### Uninstall

```bash
bash ~/.imessage-exporter/uninstall.sh
```

## Requirements

- macOS (Apple Silicon or Intel)
- Python 3.11+
- **Full Disk Access** for your terminal app
- No external dependencies — uses only the Python standard library

## Manual Setup

If you prefer not to use the installer:

1. Clone or download this repo
2. Grant Full Disk Access to your terminal (Terminal.app, iTerm2, Ghostty, etc.)
3. Run it:

```bash
python3 imessage_export.py
```

## Usage

### Interactive mode

```bash
imessage-export
```

Walks you through:
1. Selecting a conversation from recent activity
2. Assigning friendly names to participants (suggested from Contacts and saved aliases)
3. Choosing a time period
4. Saving to a file, the clipboard (`clip`), or stdout (`-`)

### CLI flags

```bash
# List recent conversations
imessage-export --list

# Export a conversation by name or phone number
imessage-export --chat "Family Group Chat" --since 7d
imessage-export --chat "+15551234567" --since 3h

# Custom date range
imessage-export --chat "+15551234567" --since 2024-01-01 --until 2024-01-31

# Replace phone numbers with names in the output
imessage-export --chat "Family Group Chat" --since 7d \
  --names "+15551234567=Alice" "+15559876543=Bob"

# Specify output file
imessage-export --chat "Family Group Chat" --since 7d -o chat.txt
```

### Narrowing a long conversation

Time windows are a poor proxy for "the part I actually care about". Two
alternatives, both of which keep tapbacks attached to the messages they react to:

```bash
# The most recent N messages, regardless of date
imessage-export --chat "Mike" --last 200

# Only messages mentioning a term, plus surrounding context
imessage-export --chat "Mike" --search "deadline"
imessage-export --chat "Mike" --search "invoice" --context 10
```

`--search` matches message text and attachment filenames, and marks what it
skipped inline as `[… 34 messages omitted …]` so the transcript never looks
continuous when it isn't.

Every export prints a line count and rough token estimate, with a warning when
the result is large enough to risk overflowing a model's context window.

### Sending it somewhere

```bash
imessage-export --chat "Mike" --last 200 --copy      # to the clipboard
imessage-export --chat "Mike" --last 200 --stdout    # to stdout, for piping
```

Progress messages go to stderr, so `--stdout` pipes cleanly.

### Names, aliases and redaction

Names resolve automatically from the macOS Contacts database, so the
conversation list shows people instead of phone numbers. Aliases you assign are
remembered in `~/.config/imessage-exporter/aliases.json` and reused on later
exports, which keeps the same person under the same name across sessions.

```bash
# Assign names and remember them for next time
imessage-export --chat "Mike" --since 7d \
  --names "+15551234567=Mike" --save-aliases

# Replace phone numbers, emails and long digit runs with stable tokens
imessage-export --chat "Mike" --since 30d --scrub

# Opt out
imessage-export --chat "Mike" --no-contacts --no-aliases
```

`--scrub` exists because `--names` only rewrites the identifiers you list —
a phone number or email typed *inside* a message body passes through untouched.
Scrubbing replaces each distinct value with a stable token (`[phone-1]`,
`[email-2]`), so the conversation stays followable while the real values do not
leave your machine.

### Finding an older conversation

The conversation list defaults to the past 7 days. If nothing turns up it widens
automatically (30 → 90 → 365 days); `--list-days` sets it explicitly.

```bash
imessage-export --list --list-days 90
```

Note this affects *discovery* only — `--chat` works on any conversation
regardless of how long it has been quiet.

### Duration formats

| Format | Meaning        |
|--------|----------------|
| `30m`  | Past 30 minutes|
| `3h`   | Past 3 hours   |
| `7d`   | Past 7 days    |
| `2w`   | Past 2 weeks   |

You can also use `YYYY-MM-DD` for exact dates.

## Output format

All timestamps — message times, `--since`/`--until` dates, and relative times
in the conversation list — are EST (a fixed UTC-5 offset). This offset does not
shift for daylight saving, so summer timestamps read one hour earlier than what
the Messages app shows.

```
# iMessage Export
# Conversation: Family Group Chat
# Participants: Alice, Bob, Me
# Period: 2024-01-10 to now
# Exported: 2024-01-17 14:30 EST
# Message count: 128
#
# Legend:
#   ↳ = reply to a previous message (thread)
#   Messages are in chronological order
#   Threaded replies are indented under their context
# ---

[2024-01-15 10:30] Alice: Does anyone want to grab lunch today?
[2024-01-15 10:31] Bob: I'm thinking sushi  [Photo]
[2024-01-15 10:32] Me: Sure, what time?
  ↳ [2024-01-15 10:35] Alice: (replying to "Does anyone want to..."): How about 12:30?
  ↳ [2024-01-15 10:36] Me: (replying to "Does anyone want to..."): Works for me!
[2024-01-15 10:33] Charlie: Can't today, sorry
  ↳ [2024-01-15 10:37] Bob: (replying to "Can't today, sorry"): Next time!
[2024-01-15 10:40] Alice: Let's meet at the lobby
[2024-01-15 10:41] Bob: [PDF: menu.pdf]
```

### Attachments

Photos, videos, PDFs and similar appear as inline placeholders, so a message
that contained *only* an attachment still shows up as a turn in the transcript
instead of vanishing. No media files are written to disk.

`[Photo]` `[Photo ×3]` `[Screenshot]` `[Video]` `[GIF]` `[PDF: quote.pdf]`
`[Contact card]` `[Shared location]` `[Sticker]` `[Audio]` `[File: notes.txt]`

Link previews and app payloads are deliberately omitted — their URL is already
in the message text, and they outnumber real attachments roughly 2:1.

## What it handles

- **Reply threads** — indented with `↳` and a truncated quote of the original message
- **Tapback reactions** — collapsed as emoji annotations (e.g. `(❤️ by Alice)`)
- **Group chats** — shows display name or participant list
- **Audio messages** — shown as `[Audio message]`
- **System events** — group name changes shown as `[System: ...]`
- **attributedBody fallback** — extracts text when the `text` column is NULL
- **Thread originators outside time range** — fetched for context, tagged `[earlier message]`
- **Name mapping** — replace phone numbers/emails with real names in the export

## How it works

Reads `~/Library/Messages/chat.db` in **read-only mode** (never writes to it). The database is SQLite and contains all iMessage/SMS history on the device.

## Troubleshooting

**"Cannot read iMessage database. Full Disk Access is required."**
Your terminal app needs Full Disk Access. Go to System Settings → Privacy & Security → Full Disk Access and add your terminal.

**"iMessage database not found"**
This tool only works on macOS with an active iMessage account. The database lives at `~/Library/Messages/chat.db`.

**Empty conversation list**
The default view shows conversations with activity in the past 7 days. Use `--since 30d` for a wider window.
