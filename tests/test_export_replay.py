import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import replay  # noqa: E402
import tg_export  # noqa: E402

EXPORT = {
    "name": "Gold Kings", "type": "public_supergroup", "id": 1234567890,
    "messages": [
        {"id": 1, "type": "service", "date": "2025-10-01T09:00:00", "actor": "Admin", "action": "create_group", "text": ""},
        {"id": 2, "type": "message", "date": "2025-10-01T10:00:00", "date_unixtime": "1759309200", "from": "Admin",
         "text": ["XAUUSD BUY 2040\nSL 2030\n", {"type": "bold", "text": "TP1 2045"}, "\nTP2 2050"]},
        {"id": 3, "type": "message", "date": "2025-10-01T10:10:00", "date_unixtime": "1759309800", "from": "Admin",
         "text": "Close half here ✅"},
        {"id": 4, "type": "message", "date": "2025-10-01T10:20:00", "date_unixtime": "1759310400", "from": "Admin",
         "reply_to_message_id": 2, "text": "Close now"},
        {"id": 5, "type": "message", "date": "2025-10-01T11:00:00", "date_unixtime": "1759312800", "from": "Admin",
         "text": "Gold buy now, sl and tp coming"},
        {"id": 6, "type": "message", "date": "2025-10-01T11:05:00", "date_unixtime": "1759313100", "from": "Bob",
         "text": "good morning all"},
        {"id": 7, "type": "message", "date": "2025-10-01T11:06:00", "date_unixtime": "1759313160", "from": "Bob",
         "text": [{"type": "sticker", "text": ""}]},
    ],
}


def write_export(tmp_path):
    path = tmp_path / "result.json"
    path.write_text(json.dumps(EXPORT), encoding="utf-8")
    return path


def test_load_flattens_text_and_maps_ids(tmp_path):
    messages = list(tg_export.load(write_export(tmp_path)))
    assert [m["message_id"] for m in messages] == [2, 3, 4, 5, 6]      # service + empty messages dropped
    first = messages[0]
    assert first["text"] == "XAUUSD BUY 2040\nSL 2030\nTP1 2045\nTP2 2050"
    assert first["chat_id"] == -1001234567890 and first["chat_name"] == "Gold Kings"
    assert first["time"] == datetime(2025, 10, 1, 9, 0, tzinfo=timezone.utc)   # date_unixtime wins over local date
    assert messages[2]["reply_to"] == 2


def test_load_full_account_export_and_folder(tmp_path):
    (tmp_path / "all").mkdir()
    (tmp_path / "all" / "result.json").write_text(json.dumps({"chats": {"list": [EXPORT, {**EXPORT, "name": "Other", "id": 99,
                                                                                            "type": "private_group"}]}}))
    messages = list(tg_export.load(tmp_path / "all"))
    assert len(messages) == 10
    assert {m["chat_id"] for m in messages} == {-1001234567890, -99}


def test_text_file_loader(tmp_path):
    path = tmp_path / "pasted.txt"
    path.write_text("GOLD BUY 2040\nSL 2030\nTP 2050\n\nclose all now\n", encoding="utf-8")
    messages = list(tg_export.load(path))
    assert [m["text"] for m in messages] == ["GOLD BUY 2040\nSL 2030\nTP 2050", "close all now"]


def test_classify(tmp_path):
    messages = list(tg_export.load(write_export(tmp_path)))
    kinds, signal_ids = [], set()
    for m in messages:
        kind, detail = replay.classify(m, signal_ids)
        if kind == "SIGNAL":
            signal_ids.add(m["message_id"])
        kinds.append((kind, detail if kind != "SIGNAL" else replay.describe(detail)))
    assert kinds == [
        ("SIGNAL", "BUY XAUUSD @ 2040.0 SL 2030.0 TP 2045/2050"),
        ("PARTIAL", 0.5),
        ("CLOSE", "reply"),
        ("CHECK", None),
        ("IGNORED", None),
    ]


def test_replay_main_writes_report(tmp_path, monkeypatch, capsys):
    path = write_export(tmp_path)
    out = tmp_path / "report.md"
    monkeypatch.setattr(sys, "argv", ["replay.py", str(path), "--out", str(out)])
    replay.main()
    printed = capsys.readouterr().out
    assert "1 signal, 1 close, 1 partial, 0 breakeven, 1 check, 1 ignored" in printed
    assert "1 message(s) look like signals but didn't parse" in printed
    report = out.read_text(encoding="utf-8")
    assert "Gold Kings (-1001234567890)" in report
    assert "Not parsed - full text" in report and "Gold buy now, sl and tp coming" in report
