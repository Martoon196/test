import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import dashboard  # noqa: E402
import db  # noqa: E402


class FakeTrader:
    paused = True
    kill_switch_tripped = False


def get(url):
    with urllib.request.urlopen(url, timeout=5) as res:
        return res.status, res.read().decode()


def test_dashboard_serves_state_and_page(tmp_path):
    config.DB_PATH = str(tmp_path / "d.db")
    config.DASHBOARD_TOKEN, config.PORT, config.PUBLIC_DOMAIN = "secret", 0, ""
    db.reset()
    db.init_db()
    db.add_equity(10000, 10050)
    db.add_equity(10000, 10120)
    db.day_start_balance("2025-01-01", 10000)
    db.log_action("OPEN", "test")

    url = dashboard.start(FakeTrader())
    port = dashboard.server.server_address[1]
    assert url == f"http://localhost:{port}/?token=secret"
    try:
        status, body = get(f"http://127.0.0.1:{port}/api/state?token=secret")
        state = json.loads(body)
        assert status == 200 and state["paused"] is True and state["equity"] == 10120
        assert [p["equity"] for p in state["equity_curve"]] == [10050, 10120]
        assert state["actions"][0]["action"] == "OPEN" and state["open"] == []

        status, page = get(f"http://127.0.0.1:{port}/?token=secret")
        assert status == 200 and "<title>Signal Bot</title>" in page and "prefers-color-scheme: dark" in page

        assert get(f"http://127.0.0.1:{port}/health")[0] == 200
        try:
            get(f"http://127.0.0.1:{port}/api/state?token=wrong")
            assert False, "expected 401"
        except urllib.error.HTTPError as e:
            assert e.code == 401
    finally:
        dashboard.server.shutdown()
        dashboard.server.server_close()
