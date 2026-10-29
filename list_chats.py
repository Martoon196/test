"""Prints the ids of your groups/channels so you can fill in SIGNAL_CHATS:
    API_ID=... API_HASH=... SESSION_STRING=... python list_chats.py
"""
import os

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

with TelegramClient(StringSession(os.environ["SESSION_STRING"]), int(os.environ["API_ID"]),
                    os.environ["API_HASH"]) as client:
    for dialog in client.iter_dialogs():
        if dialog.is_group or dialog.is_channel:
            print(f"{dialog.id}\t{dialog.name}")
