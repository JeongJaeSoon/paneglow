from __future__ import annotations

import io
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from paneglow import cli, pad, ui
from tests.test_cli import identity, paths_for, snapshot_for


@pytest.fixture(autouse=True)
def no_hardware(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("ui must never open the pad")

    monkeypatch.setattr(pad.Pad, "open", refuse)


def test_ui_data_payload_running(tmp_path: Path, monkeypatch):
    paths = paths_for(tmp_path)
    item = identity()
    snap = snapshot_for(item)
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: True)
    monkeypatch.setattr(
        cli, "_runtime_identity", lambda _paths, **_kwargs: (item, snap)
    )

    data = ui.build_data(paths)

    assert data["status"] == "running"
    assert data["snapshot"] == snap
    assert data["palette"] == {
        "idle": "#FFFFFF",
        "working": "#304FFE",
        "waiting": "#FF6D00",
        "done": "#00FF4C",
        "error": "#FF0033",
    }
    assert data["reason_labels"] == cli._REASON_LABELS
    assert isinstance(data["config_warnings"], list)


def test_ui_data_payload_stopped(tmp_path: Path, monkeypatch):
    paths = paths_for(tmp_path)
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: False)

    data = ui.build_data(paths)

    assert data["status"] == "stopped"
    assert data["snapshot"] is None


def test_ui_data_payload_degraded(tmp_path: Path, monkeypatch):
    paths = paths_for(tmp_path)
    monkeypatch.setattr(cli, "_lock_is_held", lambda _path: True)

    def unavailable(_paths, **_kwargs):
        raise cli.RuntimeDataError("runtime snapshot is stale")

    monkeypatch.setattr(cli, "_runtime_identity", unavailable)

    data = ui.build_data(paths)

    assert data["status"] == "degraded"
    assert "stale" in data["detail"]
    assert data["snapshot"] is None


def test_ui_data_payload_degraded_on_invalid_lock(tmp_path: Path, monkeypatch):
    paths = paths_for(tmp_path)

    def broken(_path):
        raise cli.RuntimeDataError("daemon lock cannot be inspected")

    monkeypatch.setattr(cli, "_lock_is_held", broken)

    data = ui.build_data(paths)

    assert data["status"] == "degraded"
    assert data["snapshot"] is None


def test_ui_serves_html_and_json_over_http(tmp_path: Path):
    paths = paths_for(tmp_path)
    server = ui.make_server(paths, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    try:
        with urllib.request.urlopen(f"{base}/{server.token}/") as response:
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/html")
            body = response.read()
            assert b"Paneglow" in body
            assert b"setInterval" in body

        with urllib.request.urlopen(f"{base}/{server.token}/data") as response:
            payload = json.loads(response.read())
        assert payload["status"] == "stopped"
        assert payload["palette"]["error"] == "#FF0033"

        for bad in ("/", "/data", f"/{server.token}/nope"):
            with pytest.raises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(base + bad)
            assert caught.value.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("open_browser", [False, True])
def test_ui_serve_prints_url_and_honours_open_browser(
    tmp_path: Path, monkeypatch, open_browser: bool
):
    paths = paths_for(tmp_path)
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url))
    stop = threading.Event()
    stop.set()
    stdout = io.StringIO()

    assert ui.serve(paths, port=0, open_browser=open_browser, stdout=stdout,
                    stop_event=stop) == 0

    assert "http://127.0.0.1:" in stdout.getvalue()
    if open_browser:
        assert len(opened) == 1
        assert opened[0].startswith("http://127.0.0.1:")
    else:
        assert opened == []


def test_ui_cli_dispatch(monkeypatch):
    calls: list[dict] = []

    def fake_serve(paths, *, port, open_browser, stdout=None, stderr=None,
                   stop_event=None):
        calls.append({"port": port, "open_browser": open_browser})
        return 0

    monkeypatch.setattr(ui, "serve", fake_serve)

    assert cli.main(["ui", "--no-open", "--port", "8123"]) == 0
    assert calls == [{"port": 8123, "open_browser": False}]
