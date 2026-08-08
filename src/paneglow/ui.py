"""Read-only local dashboard: ``paneglow ui``.

Serves one inline HTML page over loopback that polls ``/data`` every second
and answers "why is the pad this colour right now" from the same runtime
snapshot ``paneglow status`` reads.  The URL carries a random token so the
snapshot's 0600 privacy model (other local users cannot read it) survives
the move to a TCP port; the socket itself only binds 127.0.0.1.
"""
from __future__ import annotations

import json
import secrets
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TextIO

from paneglow import cli
from paneglow.render import PALETTE


def build_data(paths: "cli.RuntimePaths") -> dict:
    """Assemble the ``/data`` payload.  Data problems never raise -- they
    degrade, so the polling client always has something to render."""
    cfg, warnings = cli._load_config(paths)
    data: dict = {
        "status": "degraded",
        "detail": "",
        "snapshot": None,
        "palette": {state.value: f"#{colour:06X}" for state, colour in PALETTE.items()},
        "reason_labels": cli._REASON_LABELS,
        "config_warnings": warnings,
    }
    try:
        held = cli._lock_is_held(paths.lock_path)
    except cli.RuntimeDataError as error:
        data["detail"] = str(error)
        return data
    if not held:
        data["status"] = "stopped"
        data["detail"] = "daemon is not running"
        return data
    try:
        _identity, snapshot = cli._runtime_identity(
            paths, status_poll_ms=getattr(cfg, "status_poll_ms", 1000)
        )
    except cli.RuntimeDataError as error:
        data["detail"] = str(error)
        return data
    data["status"] = "running"
    data["snapshot"] = snapshot
    return data


class _UIServer(ThreadingHTTPServer):
    def __init__(self, paths: "cli.RuntimePaths", port: int):
        self.paths = paths
        self.token = secrets.token_urlsafe(16)
        super().__init__(("127.0.0.1", port), _Handler)


class _Handler(BaseHTTPRequestHandler):
    server: _UIServer

    def log_message(self, *_args) -> None:
        pass

    def _reply(self, content_type: str, body: bytes) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        prefix = f"/{self.server.token}"
        if self.path in (prefix, prefix + "/"):
            self._reply("text/html; charset=utf-8", PAGE_HTML.encode())
        elif self.path == prefix + "/data":
            try:
                payload = build_data(self.server.paths)
            except Exception as error:  # never 500 the poll loop
                payload = {"status": "degraded", "detail": str(error),
                           "snapshot": None}
            self._reply("application/json", json.dumps(payload).encode())
        else:
            self.send_error(404)


def make_server(paths: "cli.RuntimePaths", *, port: int = 0) -> _UIServer:
    return _UIServer(paths, port)


def serve(paths: "cli.RuntimePaths", *, port: int = 0, open_browser: bool = True,
          stdout: TextIO | None = None, stderr: TextIO | None = None,
          stop_event: threading.Event | None = None) -> int:
    stdout = sys.stdout if stdout is None else stdout
    stderr = sys.stderr if stderr is None else stderr
    try:
        server = make_server(paths, port=port)
    except OSError as error:
        print(f"paneglow: ui server could not bind 127.0.0.1:{port}: {error}",
              file=stderr)
        return 1
    url = f"http://127.0.0.1:{server.server_address[1]}/{server.token}/"
    print(f"paneglow ui: {url}", file=stdout, flush=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    event = threading.Event() if stop_event is None else stop_event
    try:
        with cli._signal_stop_event(event):
            event.wait()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    return 0


PAGE_HTML = """<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paneglow</title>
<style>
  :root {
    --ink:#14161C; --ink-soft:#4A505E; --ink-faint:#7C8496;
    --ground:#F6F7FA; --card:#FFFFFF; --rule:#DFE3EB; --rule-soft:#ECEFF4;
    --accent:#304FFE;
    --mono: ui-monospace, "SF Mono", SFMono-Regular, Menlo, Consolas, monospace;
    --sans: -apple-system, BlinkMacSystemFont, "Apple SD Gothic Neo",
            "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --ink:#E8EBF2; --ink-soft:#A6AEC0; --ink-faint:#6E778A;
      --ground:#0D0F14; --card:#161A22; --rule:#262C38; --rule-soft:#1D222B;
      --accent:#7C8CFF;
    }
  }
  * { margin:0; box-sizing:border-box; }
  body { background:var(--ground); color:var(--ink); font-family:var(--sans);
         font-size:.95rem; line-height:1.6; max-width:44rem; margin:0 auto;
         padding:2rem 1.25rem 4rem; }
  h1 { font-size:1.3rem; font-weight:600; letter-spacing:-.01em; }
  header { display:flex; align-items:baseline; gap:.8rem; margin-bottom:1.2rem; }
  .chips { display:flex; flex-wrap:wrap; gap:.4rem; margin-bottom:1.2rem; }
  .chip { font-family:var(--mono); font-size:.74rem; padding:.15rem .55rem;
          border:1px solid var(--rule); border-radius:99px; background:var(--card);
          color:var(--ink-soft); }
  .chip b { font-weight:600; color:var(--ink); }
  .chip.bad b { color:#FF0033; }
  .grid { display:grid; grid-template-columns:repeat(3,1fr); gap:.6rem;
          margin-bottom:1rem; }
  .key { background:var(--card); border:1px solid var(--rule); border-radius:8px;
         padding:.6rem .7rem; }
  .key .led { height:.55rem; border-radius:4px; background:var(--rule-soft);
              border:1px solid var(--rule-soft); margin-bottom:.45rem; }
  .key .name { font-family:var(--mono); font-size:.74rem; color:var(--ink-faint); }
  .key .state { font-weight:600; font-size:.85rem; }
  .key .who { font-family:var(--mono); font-size:.72rem; color:var(--ink-soft);
              overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .key .why { font-size:.72rem; color:var(--ink-faint); }
  .legend { display:flex; flex-wrap:wrap; gap:.9rem; font-size:.76rem;
            color:var(--ink-soft); margin-bottom:1.4rem; }
  .legend span { display:inline-flex; align-items:center; gap:.35rem; }
  .legend i { width:.7rem; height:.7rem; border-radius:3px; display:inline-block;
              border:1px solid var(--rule); }
  h2 { font-family:var(--mono); font-size:.74rem; letter-spacing:.12em;
       text-transform:uppercase; color:var(--ink-faint); font-weight:500;
       margin:1.3rem 0 .4rem; }
  ul { list-style:none; font-family:var(--mono); font-size:.78rem;
       color:var(--ink-soft); padding:0; }
  li { padding:.1rem 0; }
  .warn { border-left:3px solid #FF6D00; background:var(--card);
          padding:.5rem .8rem; border-radius:0 4px 4px 0; font-size:.8rem;
          margin:.3rem 0; }
  #detail { color:var(--ink-faint); font-size:.8rem; }
</style>
<body>
<header><h1>Paneglow</h1><span class="chip" id="daemon">…</span></header>
<div id="detail"></div>
<div class="chips" id="chips"></div>
<div class="grid" id="keys"></div>
<div class="legend" id="legend"></div>
<h2>Border</h2><ul id="border"></ul>
<h2>Last causes</h2><ul id="causes"></ul>
<div id="diagnostics"></div>
<div id="warnings"></div>
<script>
"use strict";
const $ = id => document.getElementById(id);
const esc = value => { const d = document.createElement("div");
  d.textContent = String(value); return d.innerHTML; };
const chip = (label, value, bad) =>
  `<span class="chip${bad ? " bad" : ""}">${esc(label)} <b>${esc(value)}</b></span>`;

function render(data) {
  const daemon = $("daemon");
  daemon.textContent = data.status;
  daemon.className = "chip" + (data.status === "running" ? "" : " bad");
  $("detail").textContent = data.detail || "";

  const palette = data.palette || {};
  $("legend").innerHTML = Object.entries(palette).map(([state, hex]) =>
    `<span><i style="background:${esc(hex)}"></i>${esc(state)}</span>`
  ).join("") + `<span><i></i>off</span>`;

  const snap = data.snapshot;
  if (!snap) {
    $("chips").innerHTML = "";
    $("keys").innerHTML = Array.from({length: 6}, (_ignored, index) =>
      `<div class="key"><div class="led"></div>
       <div class="name">A${index + 1}</div><div class="state">–</div></div>`
    ).join("");
    $("border").innerHTML = ""; $("causes").innerHTML = "";
    $("diagnostics").innerHTML = "";
    renderWarnings(data);
    return;
  }

  const pad = snap.pad;
  const front = snap.frontmost.ok ? (snap.frontmost.bundle_id || "none") : "unknown";
  $("chips").innerHTML =
    chip("owner", snap.owner, snap.owner === "none") +
    chip("frontmost", front) +
    (pad.connected && pad.status_verified
      ? chip("pad", `${pad.transport} · layer ${pad.layer_index}`)
      : chip("pad", pad.error_code || "unavailable", true)) +
    chip("sessions", `${snap.session_scan.count}` +
      (snap.session_scan.authoritative ? "" : " (partial)"),
      !snap.session_scan.authoritative);

  $("keys").innerHTML = snap.slots.map((slot, index) => {
    const state = slot.effective_state;
    const hex = state ? palette[state] : null;
    const reason = (data.reason_labels || {})[slot.reason] || slot.reason;
    return `<div class="key">
      <div class="led"${hex ? ` style="background:${esc(hex)};border-color:${esc(hex)}"` : ""}></div>
      <div class="name">A${index + 1}</div>
      <div class="state">${esc(state || "off")}</div>
      <div class="who">${esc(slot.session_id || "–")}</div>
      <div class="why">${esc(reason)}</div></div>`;
  }).join("");

  const ambient = snap.zones.ambient;
  const colour = ambient.color === null ? "off"
    : "#" + ambient.color.toString(16).padStart(6, "0").toUpperCase();
  $("border").innerHTML =
    `<li>${esc(colour)} ${esc(ambient.effect || "off")} (${esc(ambient.reason)})</li>` +
    (snap.last_input_result
      ? `<li>last input: ${esc(snap.last_input_result)}</li>` : "");
  $("causes").innerHTML = (snap.last_causes || []).map(cause =>
    `<li>${esc(cause)}</li>`).join("") || "<li>–</li>";
  $("diagnostics").innerHTML = (snap.session_scan.diagnostics || []).map(item =>
    `<div class="warn">${esc(item)}</div>`).join("");
  renderWarnings(data);
}

function renderWarnings(data) {
  $("warnings").innerHTML = (data.config_warnings || []).map(warning =>
    `<div class="warn">config: ${esc(warning)}</div>`).join("");
}

async function tick() {
  try {
    const response = await fetch("data", {cache: "no-store"});
    render(await response.json());
  } catch (_error) {
    const daemon = $("daemon");
    daemon.textContent = "ui server stopped";
    daemon.className = "chip bad";
  }
}
tick();
setInterval(tick, 1000);
</script>
</body>
</html>
"""
