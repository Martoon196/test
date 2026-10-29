"""Run this once on your own computer to get SESSION_STRING for Railway:
    API_ID=... API_HASH=... python make_session.py
"""
import os

from telethon.sessions import StringSession
from telethon.sync import TelegramClient

with TelegramClient(StringSession(), int(os.environ["API_ID"]), os.environ["API_HASH"]) as client:
    print("\nSESSION_STRING (keep this secret - it is a login to your Telegram account):\n")
    print(client.session.save())
