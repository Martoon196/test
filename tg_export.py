"""Reads Telegram Desktop chat exports.

In Telegram Desktop: open the group -> ⋮ -> Export chat history -> Format: JSON -> Export.
That gives a folder with result.json. You can also use Settings -> Advanced -> Export Telegram data
to get every chat in one result.json. Point replay.py / backtest.py at the file or the folder.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

CHANNEL_TYPES = ("public_supergroup", "private_supergroup", "public_channel", "private_channel")


def flatten_text(text):
    """Export text is either a string or a list of strings and {"type":..., "text":...} entities."""
    if isinstance(text, str):
        return text
    return "".join(part if isinstance(part, str) else part.get("text", "") for part in text)


def telethon_chat_id(chat):
    """The export uses bare ids; Telethon (and SIGNAL_CHATS) use -100<id> for channels/supergroups."""
    cid = chat.get("id")
    if cid is None:
        return None
    kind = chat.get("type", "")
    if kind in CHANNEL_TYPES:
        return int(f"-100{cid}")
    if kind == "private_group":
        return -cid
    return cid


def message_time(message):
    unix = message.get("date_unixtime")
    if unix:
        return datetime.fromtimestamp(int(unix), tz=timezone.utc)
    # "date" is in the exporting computer's local time, without a zone; treated as UTC
    return datetime.fromisoformat(message["date"]).replace(tzinfo=timezone.utc)


def load_file(path):
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    chats = data["chats"]["list"] if "chats" in data else [data]
    for chat in chats:
        name = chat.get("name") or "Unknown"
        chat_id = telethon_chat_id(chat)
        for message in chat.get("messages", []):
            if message.get("type", "message") != "message":
                continue
            text = flatten_text(message.get("text", ""))
            if not text.strip():
                continue
            yield {
                "chat_id": chat_id,
                "chat_name": name,
                "message_id": message.get("id"),
                "time": message_time(message),
                "text": text,
                "reply_to": message.get("reply_to_message_id"),
                "from": message.get("from") or message.get("actor") or "",
            }


def load_text_file(path):
    """Plain text file with messages separated by blank lines - handy for pasting a few examples."""
    name = Path(path).stem
    blocks = [b.strip() for b in Path(path).read_text(encoding="utf-8").split("\n\n") if b.strip()]
    base = datetime.now(timezone.utc)
    for i, block in enumerate(blocks, start=1):
        yield {"chat_id": None, "chat_name": name, "message_id": i, "time": base, "text": block,
               "reply_to": None, "from": ""}


def load(path):
    """Yields messages (dicts) from a result.json, a .txt file, or a folder containing either."""
    path = Path(path)
    if path.is_dir():
        files = sorted(list(path.rglob("result.json")) + list(path.glob("*.txt")))
        if not files:
            raise SystemExit(f"No result.json or .txt files found under {path}")
    else:
        files = [path]
    for file in files:
        loader = load_text_file if file.suffix == ".txt" else load_file
        yield from loader(file)
