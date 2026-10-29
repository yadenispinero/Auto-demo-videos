"""Record the video clips of an Odoo module from a local demo, with headless Chrome (CDP screencast) and ffmpeg.

Usage: python3 record.py <spec.json> --base http://127.0.0.1:8069 --db demo [--only v1-03-config] [--frames]
Usually run through video.py (python3 video.py record <spec.json>), which reads base and db from the spec's
"demo" block and validates the spec first. Guide: README.md next to this file.

The spec lives in the workspace (presentacion/<repo>/<version>/videos/<video>.json, never in the repo):
  {"out": "clips",                                  # relative to the spec
   "scenes": [{"name": "v1-01-lista", "path": "/odoo/action-...", "wait": 6,
               "setup": [<steps run before recording>], "steps": [<recorded steps>], "post": [<steps after>]}]}
Steps (one key each, plus options):
  {"wait": 1.5}                                     pause (the clip keeps recording)
  {"goto": "/odoo/...", "load": 5}                  open another page; the loading is cut from the clip
  {"click": "Nuevo"} / {"click_css": ".o_form_button_save"}   move the cursor there and click
      options: "within": css of the container, "nth": n-th match (0 by default)
  {"hover": "Technician"} / {"hover_css": "..."}       move the cursor there
  {"type": "Fuga de aceite", "delay": 0.06}         type in the focused field, key by key
  {"key": "Enter"}                                  Enter, Escape, Tab
  {"highlight": "Technician"} / {"highlight_css": "..."}   red box around the element (and its value in a form)
  {"unhighlight": true}                             remove the boxes
  {"scroll": 300} / {"scroll_to": "<css>"}          smooth scroll of the main content / to an element
  {"js": "..."}                                     any JavaScript
  {"call": ["res.users", "write", [[2], {...}], {}]}   JSON-RPC (resets between takes)
Visual marks (silent videos). The element: its exact text ({"arrow": "Consolidar"}) or {"arrow": true, "at_css": ...};
"within"/"nth" as in click. They stay until "clear" or the end of the scene:
  {"arrow": ..., "from": "left"}                    animated arrow coming from left/right/top/bottom
  {"spotlight": ...} / {"unspotlight": true}        darken the page except the element (a new one moves there)
  {"badge": ...}                                    step number in a circle; numbered through the whole video
  {"note": {"es": "...", "en": "..."}, "at": "Acciones", "place": "left"}   bubble next to the element in the
      recording language ("lang" of the spec); without "at"/"at_css" a caption at the bottom. One note at a time;
      without "after" it stays max(2.5, characters / 15) s. Timings go to <out>/<name>.notes.json (for subtitles).
  {"zoom": ..., "factor": 1.6} / {"unzoom": true}   smooth zoom on the element and back
  {"clear": true}                                   remove every mark and the zoom
Output: <out>/<name>.mp4, 1920x1080 (1280x720 CSS at 1.5x), 30 fps, H.264, no audio.
Only for local demo databases (admin/admin by default).
Environment: CHROME (default google-chrome), FFMPEG (default bin/ffmpeg next to this file, then the one on PATH).
"""

import argparse
import base64
import json
import os
import queue
import shutil
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request

WIDTH, HEIGHT, SCALE, FPS = 1280, 720, 1.5, 30
HERE = os.path.dirname(os.path.abspath(__file__))


class RecordError(Exception):
    """A recording problem with a message meant for the person or agent running it."""


def find_ffmpeg():
    """FFMPEG env var, then the bundled bin/ffmpeg, then the one on PATH."""
    for candidate in (os.environ.get("FFMPEG"), os.path.join(HERE, "bin", "ffmpeg"), shutil.which("ffmpeg")):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


# Set by record() for the helpers below.
BASE = ""
FFMPEG = None
LANG = "es"  # recording language: picks the texts given per language


class CDP:
    """WebSocket client with a reader thread: screencast frames arrive while the steps run."""

    def __init__(self, url):
        host, path = url[len("ws://"):].split("/", 1)
        h, p = host.split(":")
        self.sock = socket.create_connection((h, int(p)))
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.send(
            f"GET /{path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        resp = b""
        while b"\r\n\r\n" not in resp:
            resp += self.sock.recv(4096)
        self.buf = resp.split(b"\r\n\r\n", 1)[1]
        self.id = 0
        self.lock = threading.Lock()
        self.waiting = {}
        self.on_event = None
        threading.Thread(target=self._reader, daemon=True).start()

    def _send(self, data):
        payload = data.encode()
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        with self.lock:
            self.sock.sendall(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise EOFError
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def _reader(self):
        try:
            while True:
                message = b""
                while True:
                    b1, b2 = self._read(2)
                    n = b2 & 0x7F
                    if n == 126:
                        n = struct.unpack(">H", self._read(2))[0]
                    elif n == 127:
                        n = struct.unpack(">Q", self._read(8))[0]
                    message += self._read(n)
                    if b1 & 0x80:
                        break
                msg = json.loads(message)
                if "id" in msg and msg["id"] in self.waiting:
                    self.waiting.pop(msg["id"]).put(msg)
                elif "method" in msg and self.on_event:
                    self.on_event(msg)
        except (EOFError, OSError):
            pass

    def call(self, method, wait=True, **params):
        with self.lock:
            self.id += 1
            mid = self.id
        box = queue.Queue()
        if wait:
            self.waiting[mid] = box
        self._send(json.dumps({"id": mid, "method": method, "params": params}))
        if wait:
            msg = box.get(timeout=60)
            return msg.get("result", msg)

    def js(self, expression):
        r = self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if r.get("exceptionDetails"):
            raise RecordError("JS error: %s" % r["exceptionDetails"])
        return r.get("result", {}).get("value")


def rpc(path, params, sid=None):
    req = urllib.request.Request(
        BASE + path, data=json.dumps({"params": params}).encode(),
        headers={"Content-Type": "application/json", **({"Cookie": f"session_id={sid}"} if sid else {})},
    )
    with urllib.request.urlopen(req) as r:
        body = json.load(r)
    if body.get("error"):
        raise RecordError("Odoo: " + body["error"]["data"]["message"])
    return r, body.get("result")  # Odoo leaves "result" out when the method returns None


# Fake mouse pointer (headless Chrome draws none), red boxes and the visual marks of the silent videos
# (arrow, spotlight, step badge, note, zoom). They live in a layer outside <body>, so zooming the page does not
# move them, and they follow their element on every frame (scroll, zoom, re-render).
HELPERS = r"""
(() => {
  if (window.REC) return;
  // ===== Look of the marks: colors and sizes in one place. CSS px; the video is 1.5x (20px -> 30px at 1080p).
  const CSS = `
    #rec-layer { position: fixed; left: 0; top: 0; width: 100vw; height: 100vh; z-index: 2147483646; pointer-events: none; }
    #rec-layer > * { position: fixed; left: 0; top: 0; opacity: 0; transition: opacity .35s ease; }
    #rec-layer > .rec-on { opacity: 1; }
    #rec-cursor { opacity: 1 !important; z-index: 3; transition: left .7s ease-in-out, top .7s ease-in-out, transform .15s !important; }
    .rec-box { border: 3px solid #e03131; border-radius: 6px; }
    .rec-spot { border-radius: 8px; box-shadow: 0 0 0 4px #ffd43b, 0 0 0 9999px rgba(20, 24, 33, .62); }
    .rec-arrow { width: 84px; height: 84px; color: #e03131; filter: drop-shadow(0 2px 3px rgba(0, 0, 0, .35)); }
    .rec-arrow svg { width: 100%; height: 100%; animation: rec-nudge 1s ease-in-out infinite; }
    @keyframes rec-nudge { 0%, 100% { transform: translateX(0); } 50% { transform: translateX(10px); } }
    .rec-badge { width: 38px; height: 38px; border-radius: 50%; background: #e03131; color: #fff; text-align: center;
                 font: 700 21px/38px system-ui, sans-serif; box-shadow: 0 0 0 3px #fff, 0 2px 6px rgba(0, 0, 0, .35); }
    .rec-note { max-width: 430px; padding: 14px 18px; border-radius: 10px; background: #fff; color: #1d2433;
                font: 500 20px/1.35 system-ui, sans-serif; white-space: pre-line;
                box-shadow: 0 6px 24px rgba(0, 0, 0, .28); border-left: 6px solid #e03131; }
    .rec-note.rec-caption { max-width: 960px; text-align: center; }
    .o_menu_systray .badge, .o_menu_systray .o-mail-ActivityMenu-counter { display: none !important; }
  `;
  // Captions stay above the bottom sixth of the screen, where YouTube draws its subtitles.
  const GAP = 10, ARROW = 84, BADGE = 38, ZOOM_MS = 900, CAPTION_BOTTOM = 130;
  // =====
  const style = document.createElement('style');
  style.textContent = CSS;
  document.head.appendChild(style);
  const layer = document.createElement('div');
  layer.id = 'rec-layer';
  document.documentElement.appendChild(layer);
  const cursor = document.createElement('div');
  cursor.id = 'rec-cursor';
  cursor.innerHTML = '<svg width="26" height="26" viewBox="0 0 24 24"><path d="M3 2l7 19 2.6-7.6L20 11z" fill="#111" stroke="#fff" stroke-width="1.5"/></svg>';
  cursor.style.left = '640px'; cursor.style.top = '420px';
  layer.appendChild(cursor);

  const visible = (e) => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0 &&
    getComputedStyle(e).visibility !== 'hidden'; };
  const W = () => window.innerWidth, H = () => window.innerHeight;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const union = (els) => { let l = 1e9, t = 1e9, r = -1e9, b = -1e9;
    for (const e of els) { const q = e.getBoundingClientRect();
      l = Math.min(l, q.left); t = Math.min(t, q.top); r = Math.max(r, q.right); b = Math.max(b, q.bottom); }
    return {left: l, top: t, right: r, bottom: b, width: r - l, height: b - t}; };
  const at = (n, x, y, w, h) => { n.style.left = x + 'px'; n.style.top = y + 'px';
    if (w !== undefined) { n.style.width = w + 'px'; n.style.height = h + 'px'; } };

  // Each mark: {kind, node, els, rect}. rect is kept from the last frame its elements were on the page.
  const marks = [];
  const PLACE = {
    box(m, r) { at(m.node, r.left - 6, r.top - 4, r.width + 12, r.height + 8); },
    spot(m, r) { at(m.node, r.left - 8, r.top - 6, r.width + 16, r.height + 12); },
    badge(m, r) { at(m.node, clamp(r.left - BADGE + 2, 4, W() - BADGE - 4), clamp(r.top - BADGE + 4, 4, H() - BADGE - 4)); },
    arrow(m, r) {
      const cx = r.left + r.width / 2 - ARROW / 2, cy = r.top + r.height / 2 - ARROW / 2;
      const p = {left: [r.left - ARROW - GAP, cy, 0], right: [r.right + GAP, cy, 180],
                 top: [cx, r.top - ARROW - GAP, 90], bottom: [cx, r.bottom + GAP, -90]}[m.from];
      at(m.node, p[0], p[1]); m.node.style.transform = `rotate(${p[2]}deg)`;
    },
    note(m, r) {
      const w = m.node.offsetWidth, h = m.node.offsetHeight;
      if (!r) { at(m.node, (W() - w) / 2, H() - h - CAPTION_BOTTOM); return; }
      if (m.place === 'left' || m.place === 'right') {
        const x = m.place === 'right' ? r.right + 14 : r.left - 14 - w;
        at(m.node, clamp(x, 12, W() - w - 12), clamp(r.top + r.height / 2 - h / 2, 10, H() - h - 10));
        return;
      }
      const below = r.bottom + 14 + h < H() - 10, above = r.top - 14 - h > 10;
      const y = m.place === 'above' || (m.place !== 'below' && !below && above) ? r.top - 14 - h
        : below || m.place === 'below' ? r.bottom + 14 : clamp(r.top, 10, H() - h - 10);
      const side = !below && !above && m.place !== 'above' && m.place !== 'below';
      const x = side ? (r.right + 14 + w < W() - 10 ? r.right + 14 : r.left - 14 - w) : r.left + r.width / 2 - w / 2;
      at(m.node, clamp(x, 12, W() - w - 12), clamp(y, 10, H() - h - 10));
    },
  };
  const tick = () => {
    for (const m of marks) {
      if (m.els.length && m.els.every(e => e.isConnected)) m.rect = union(m.els);
      PLACE[m.kind](m, m.rect);
    }
    requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
  const add = (kind, els, extra) => {
    const node = document.createElement('div');
    node.className = 'rec-' + kind;
    const m = Object.assign({kind, node, els, rect: els.length ? union(els) : null}, extra || {});
    layer.appendChild(node);
    marks.push(m);
    PLACE[kind](m, m.rect);
    requestAnimationFrame(() => requestAnimationFrame(() => node.classList.add('rec-on')));
    return m;
  };
  const drop = (kinds) => {
    for (const m of marks.filter(m => kinds.includes(m.kind))) {
      marks.splice(marks.indexOf(m), 1);
      m.node.classList.remove('rec-on');
      setTimeout(() => m.node.remove(), 400);
    }
  };
  const zoomState = {f: 1, tx: 0, ty: 0};
  const setZoom = (f, tx, ty) => {
    const b = document.body;
    b.style.transformOrigin = '0 0';
    b.style.transition = `transform ${ZOOM_MS}ms ease-in-out`;
    b.style.transform = f === 1 ? '' : `translate(${tx}px, ${ty}px) scale(${f})`;
    Object.assign(zoomState, {f, tx, ty});
  };

  window.REC = {
    find(text, within, nth) {
      const root = within ? document.querySelector(within) : document;
      if (!root) return null;
      const all = [...root.querySelectorAll('button, a, label, span, div, th, td, li, input, .o_field_widget, h1, h2, p')]
        .filter(e => visible(e) && (e.value === text || e.textContent.replace(/\s+/g, ' ').replace('?', '').trim() === text));
      // Innermost matches only.
      const inner = all.filter(e => !all.some(o => o !== e && e.contains(o)));
      return inner[nth || 0] || null;
    },
    el(text, css, within, nth) {
      return css ? [...(within ? document.querySelector(within) || document : document).querySelectorAll(css)].filter(visible)[nth || 0]
        : this.find(text, within, nth);
    },
    center(e) { e.scrollIntoView({block: 'nearest'}); const r = e.getBoundingClientRect();
      return [r.left + r.width / 2, r.top + r.height / 2]; },
    move(x, y) { cursor.style.left = (x - 4) + 'px'; cursor.style.top = (y - 2) + 'px'; },
    press() { cursor.style.transform = 'scale(.8)'; setTimeout(() => cursor.style.transform = '', 160); },
    box(e) {
      // A form field: its label and its value.
      const cell = e.closest('.o_cell');
      const prev = cell && cell.previousElementSibling, next = cell && cell.nextElementSibling;
      const parts = !cell ? [e] : cell.classList.contains('o_wrap_label') && next ? [cell, next] :
        prev && prev.classList.contains('o_wrap_label') ? [prev, cell] : [e];
      add('box', parts);
    },
    clear() { drop(['box']); },
    // The marks below take (text, css, within, nth) and return false while the element is not on the page.
    arrow(text, css, within, nth, from) {
      const e = this.el(text, css, within, nth); if (!e) return false;
      const m = add('arrow', [e], {from: from || 'left'});
      m.node.innerHTML = '<svg viewBox="0 0 24 24"><path d="M2 10h13V5l7 7-7 7v-5H2z" fill="currentColor" stroke="#fff" stroke-width="1.2" stroke-linejoin="round"/></svg>';
      return true;
    },
    spot(text, css, within, nth) {
      const e = this.el(text, css, within, nth); if (!e) return false;
      const old = marks.find(m => m.kind === 'spot');
      if (old) { old.els = [e]; old.node.style.transition = 'opacity .35s ease, left .45s ease, top .45s ease, width .45s ease, height .45s ease'; }
      else add('spot', [e]);
      return true;
    },
    unspot() { drop(['spot']); },
    badge(text, css, within, nth, n) {
      const e = this.el(text, css, within, nth); if (!e) return false;
      add('badge', [e]).node.textContent = String(n);
      return true;
    },
    note(message, text, css, within, nth, place) {
      const e = text || css ? this.el(text, css, within, nth) : null;
      if ((text || css) && !e) return false;
      drop(['note']);
      const m = add('note', e ? [e] : [], {place});
      // Non-breaking hyphen inside codes (SRV-MANT), so the line never breaks there.
      m.node.textContent = message.replace(/(\w)-(?=\w)/g, '$1\u2011');
      if (!e) m.node.classList.add('rec-caption');
      PLACE.note(m, m.rect);
      return true;
    },
    endNote() { drop(['note']); },
    zoom(text, css, within, nth, f) {
      const e = this.el(text, css, within, nth); if (!e) return false;
      const r = e.getBoundingClientRect(), z = zoomState;
      // Page point under the element's center (undoing the current zoom), then bring it to the screen center.
      const cx = (r.left + r.width / 2 - z.tx) / z.f, cy = (r.top + r.height / 2 - z.ty) / z.f;
      setZoom(f, clamp(W() / 2 - f * cx, W() - f * W(), 0), clamp(H() / 2 - f * cy, H() - f * H(), 0));
      return true;
    },
    unzoom() { setZoom(1, 0, 0); },
    clearAll() { drop(['box', 'spot', 'arrow', 'badge', 'note']); if (zoomState.f !== 1) setZoom(1, 0, 0); },
  };
})()
"""


def element_point(ws, step, key, css_key):
    expr = "(() => { const e = REC.el(%s, %s, %s, %d); return e ? REC.center(e) : null; })()" % (
        json.dumps(screen_text(step.get(key), key)), json.dumps(step.get(css_key)), json.dumps(step.get("within")),
        step.get("nth", 0))
    for _ in range(20):
        point = ws.js(expr)
        if point:
            return point
        time.sleep(0.25)
    raise RecordError("element not found: %s" % json.dumps(step, ensure_ascii=False))


# Screencast state: frames are dropped while paused, and the pause is taken out of their timestamps.
STATE = {"paused": False, "offset": 0.0, "skews": []}
MARKS = ("arrow", "spotlight", "badge", "zoom")
SIDES = ("left", "right", "top", "bottom")


def clip_time():
    """Now, on the clip's clock: the frame timestamps' clock (Chrome's, which is not exactly time.time()),
    with the pauses taken out. The difference between both clocks is measured on every frame received."""
    skew = min(STATE["skews"]) if STATE["skews"] else 0.0
    return time.time() - skew - STATE["offset"]


def localized(value, lang, what):
    """A spec text: a plain string, or {"es": ..., "en": ...} from which the recording language is taken."""
    if isinstance(value, dict):
        if lang not in value:
            raise RecordError(f"{what}: no text in the recording language '{lang}': {json.dumps(value, ensure_ascii=False)}")
        return value[lang]
    return value


def screen_text(value, what):
    """A text as it appears on screen: a string (any language) or {"es": ..., "en": ...} (the recording language)."""
    if isinstance(value, dict):
        return localized(value, LANG, what)
    return value


def note_seconds(text):
    """How long a note stays when the step has no "after": enough to read it."""
    return max(2.5, len(text) / 15)


def target_args(step, key):
    """JS arguments (text, css, within, nth) of the element a mark points at: {"arrow": "Text"} or {"arrow": true, "at_css": ...}."""
    value = step.get(key)
    text = screen_text(value if value is not True else step.get("at"), key)
    return "%s, %s, %s, %d" % (json.dumps(text), json.dumps(step.get("at_css")), json.dumps(step.get("within")),
                               step.get("nth", 0))


def js_until(ws, expression, step):
    """Run a mark until its element is on the page (it returns true)."""
    for _ in range(20):
        if ws.js(expression):
            return
        time.sleep(0.25)
    raise RecordError("element not found: %s" % json.dumps(step, ensure_ascii=False))


def end_note(ctx):
    if ctx["notes"] and ctx["notes"][-1]["end"] is None:
        ctx["notes"][-1]["end"] = clip_time()


def run(ws, step, sid, ctx):
    """Run one step. ctx: {"lang": recording language, "notes": [{"text", "start", "end"}] of the scene}."""
    ws.js(HELPERS)
    if "goto" in step:
        end_note(ctx)  # the page reload takes the marks away
        STATE["paused"], start = True, time.time()
        ws.call("Page.navigate", url=BASE + step["goto"])
        time.sleep(step.get("load", 5))
        ws.js(HELPERS)
        STATE["offset"] += time.time() - start
        STATE["paused"] = False
    elif "wait" in step:
        time.sleep(step["wait"])
    elif "click" in step or "click_css" in step or "hover" in step or "hover_css" in step:
        clicking = "click" in step or "click_css" in step
        x, y = element_point(ws, step, "click" if clicking else "hover", "click_css" if clicking else "hover_css")
        ws.js("REC.move(%f, %f)" % (x, y))
        time.sleep(step.get("move", 0.8))
        ws.call("Input.dispatchMouseEvent", type="mouseMoved", x=x, y=y)
        if clicking:
            ws.js("REC.press()")
            for kind in ("mousePressed", "mouseReleased"):
                ws.call("Input.dispatchMouseEvent", type=kind, x=x, y=y, button="left", clickCount=1)
        time.sleep(step.get("after", 0.6))
    elif "type" in step:
        for char in screen_text(step["type"], "type"):
            ws.call("Input.insertText", text=char)
            time.sleep(step.get("delay", 0.06))
        time.sleep(step.get("after", 0.8))
    elif "key" in step:
        codes = {"Enter": 13, "Escape": 27, "Tab": 9}
        for kind in ("keyDown", "keyUp"):
            ws.call("Input.dispatchKeyEvent", type=kind, key=step["key"], code=step["key"],
                    windowsVirtualKeyCode=codes[step["key"]], text="\r" if step["key"] == "Enter" and kind == "keyDown" else "")
        time.sleep(step.get("after", 0.8))
    elif "highlight" in step or "highlight_css" in step:
        x, y = element_point(ws, step, "highlight", "highlight_css")
        ws.js("REC.box(REC.el(%s, %s, %s, %d))" % (json.dumps(screen_text(step.get("highlight"), "highlight")), json.dumps(step.get("highlight_css")),
                                                   json.dumps(step.get("within")), step.get("nth", 0)))
        if step.get("point", True):
            ws.js("REC.move(%f, %f)" % (x + 30, y + 14))
        time.sleep(step.get("after", 1.2))
    elif "unhighlight" in step:
        ws.js("REC.clear()")
        time.sleep(step.get("after", 0.3))
    elif "arrow" in step:
        side = step.get("from", "left")
        if side not in SIDES:
            raise RecordError(f"arrow 'from' must be one of {', '.join(SIDES)}: {json.dumps(step)}")
        js_until(ws, "REC.arrow(%s, %s)" % (target_args(step, "arrow"), json.dumps(side)), step)
        time.sleep(step.get("after", 1.5))
    elif "spotlight" in step:
        js_until(ws, "REC.spot(%s)" % target_args(step, "spotlight"), step)
        time.sleep(step.get("after", 1.5))
    elif "unspotlight" in step:
        ws.js("REC.unspot()")
        time.sleep(step.get("after", 0.5))
    elif "badge" in step:
        js_until(ws, "REC.badge(%s, %d)" % (target_args(step, "badge"), step["_number"]), step)
        time.sleep(step.get("after", 1.0))
    elif "note" in step:
        text = localized(step["note"], LANG, "note")
        has_target = "at" in step or "at_css" in step
        args = "%s, %s, %s, %d" % (json.dumps(screen_text(step.get("at"), "at")), json.dumps(step.get("at_css")),
                                   json.dumps(step.get("within")), step.get("nth", 0)) if has_target else "null, null, null, 0"
        js_until(ws, "REC.note(%s, %s, %s)" % (json.dumps(text), args, json.dumps(step.get("place", "auto"))), step)
        end_note(ctx)
        ctx["notes"].append({"text": step["note"], "start": clip_time(), "end": None})
        time.sleep(step.get("after", note_seconds(text)))
    elif "zoom" in step:
        js_until(ws, "REC.zoom(%s, %s)" % (target_args(step, "zoom"), float(step.get("factor", 1.6))), step)
        time.sleep(step.get("after", 1.5))
    elif "unzoom" in step:
        ws.js("REC.unzoom()")
        time.sleep(step.get("after", 1.2))
    elif "clear" in step:
        ws.js("REC.clearAll()")
        end_note(ctx)
        time.sleep(step.get("after", 0.5))
    elif "scroll_to" in step:
        ws.js("document.querySelector(%s)?.scrollIntoView({behavior: 'smooth', block: 'center'})"
              % json.dumps(step["scroll_to"]))
        time.sleep(step.get("after", 1.2))
    elif "scroll" in step:
        ws.js("document.querySelector('.o_content')?.scrollBy({top: %d, behavior: 'smooth'})" % step["scroll"])
        time.sleep(step.get("after", 1.2))
    elif "js" in step:
        ws.js(step["js"])
        time.sleep(step.get("after", 0.5))
    elif "call" in step:
        model, method, a, kw = step["call"]
        rpc(f"/web/dataset/call_kw/{model}/{method}", {"model": model, "method": method, "args": a, "kwargs": kw}, sid)
        time.sleep(step.get("after", 0.2))
    else:
        raise RecordError("unknown step: %s" % json.dumps(step, ensure_ascii=False))


def encode(frames, path):
    """Variable-rate screencast frames -> constant 30 fps H.264 MP4, with the real timing.

    Each frame lasts until the next one; the fps filter then picks one frame per 1/30 s. (A minimum of 1/30 s per
    frame would stretch the busy parts, where Chrome sends ~60 frames per second, and the clip would play slower.)
    """
    if not frames:
        raise RecordError("no frames for " + path)
    folder = os.path.dirname(frames[0][1])
    with open(os.path.join(folder, "list.txt"), "w") as f:
        for (ts, frame), nxt in zip(frames, frames[1:] + [(frames[-1][0] + 1 / FPS, None)]):
            f.write(f"file '{frame}'\nduration {max(nxt[0] - ts, 0.001):.4f}\n")
        f.write(f"file '{frames[-1][1]}'\n")
    subprocess.run(
        [FFMPEG, "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", os.path.join(folder, "list.txt"),
         "-vf", f"fps={FPS},scale={int(WIDTH * SCALE)}:{int(HEIGHT * SCALE)}:flags=lanczos:in_range=pc:out_range=tv,format=yuv420p",
         "-pix_fmt", "yuv420p", "-color_range", "tv",
         "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-movflags", "+faststart", "-an", path],
        check=True,
    )
    expected = frames[-1][0] - frames[0][0]
    got = media_seconds(path)
    if got is not None and abs(got - expected) > 0.3:
        raise RecordError(f"{path}: the mp4 lasts {got:.2f} s but the recording {expected:.2f} s")


def media_seconds(path):
    """Duration of a media file, read from ffmpeg's header output (None if it cannot be read)."""
    out = subprocess.run([FFMPEG, "-hide_banner", "-i", path], capture_output=True, text=True).stderr
    for line in out.splitlines():
        line = line.strip()
        if line.startswith("Duration:"):
            h, m, sec = line.split()[1].rstrip(",").split(":")
            return int(h) * 3600 + int(m) * 60 + float(sec)
    return None


def record(spec_path, base, db, login="admin", password="admin", only=None, keep_frames=False, port=9334, log=print,
           lang=None, out=None):
    """Record the scenes of a spec. Returns [{"scene", "file", "seconds", "frames", "notes"}] per recorded clip.

    lang / out override the spec's "lang" and "out" (e.g. a test take in another language in another folder)."""
    global BASE, FFMPEG, LANG
    BASE = base.rstrip("/")
    FFMPEG = find_ffmpeg()
    spec = json.load(open(spec_path))
    out_dir = os.path.join(os.path.dirname(os.path.abspath(spec_path)), out or spec.get("out", "clips"))
    os.makedirs(out_dir, exist_ok=True)
    names = [scene["name"] for scene in spec["scenes"]]
    lang = LANG = lang or spec.get("lang", "es")
    # Step badges are numbered through the whole video, so recording one scene keeps its numbers.
    number = 0
    for scene in spec["scenes"]:
        for step in scene.get("steps", []):
            if "badge" in step:
                number += 1
                step["_number"] = number
    unknown = [n for n in (only or []) if n not in names]
    if unknown:
        raise RecordError("unknown scene(s): %s (scenes: %s)" % (", ".join(unknown), ", ".join(names)))
    if not FFMPEG:
        log("ffmpeg not found (sudo apt install ffmpeg or set FFMPEG): only the frames are kept")
    clips = []
    profile = tempfile.mkdtemp()
    try:
        chrome = subprocess.Popen(
            [os.environ.get("CHROME", "google-chrome"), "--headless=new", f"--remote-debugging-port={port}",
             f"--user-data-dir={profile}", "--no-first-run", "--hide-scrollbars", "--force-device-scale-factor=1.5",
             "--window-size=1280,720", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RecordError("Chrome not found: install google-chrome or set CHROME")
    try:
        target = None
        for _ in range(50):
            try:
                pages = json.load(urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list"))
                target = next(p for p in pages if p["type"] == "page")
                break
            except Exception:
                time.sleep(0.2)
        if not target:
            raise RecordError(f"Chrome did not open its debugging port {port} (is another recording running?)")
        ws = CDP(target["webSocketDebuggerUrl"])
        ws.call("Network.enable")
        ws.call("Page.enable")
        try:
            r, _user = rpc("/web/session/authenticate", {"db": db, "login": login, "password": password})
        except urllib.error.URLError as e:
            raise RecordError(f"Odoo is not answering at {BASE} ({e.reason}): start the demo first")
        sid = r.headers.get("Set-Cookie").split("session_id=")[1].split(";")[0]
        ws.call("Network.setCookie", name="session_id", value=sid, url=BASE)
        ws.call("Emulation.setDeviceMetricsOverride", width=WIDTH, height=HEIGHT, deviceScaleFactor=SCALE, mobile=False)
        for scene in spec["scenes"]:
            if only and scene["name"] not in only:
                continue
            ws.call("Page.navigate", url=BASE + scene["path"])
            time.sleep(scene.get("load", 6))
            ws.js(HELPERS)
            ctx = {"lang": lang, "notes": []}
            for step in scene.get("setup", []):
                run(ws, step, sid, ctx)
            frames_dir = os.path.join(out_dir, ".frames", scene["name"])
            shutil.rmtree(frames_dir, ignore_errors=True)
            os.makedirs(frames_dir)
            frames = []
            STATE.update(paused=False, offset=0.0, skews=[])

            def on_event(msg, frames=frames, frames_dir=frames_dir):
                if msg["method"] != "Page.screencastFrame":
                    return
                p = msg["params"]
                if STATE["paused"]:
                    ws.call("Page.screencastFrameAck", wait=False, sessionId=p["sessionId"])
                    return
                # Lowest delivery delay = closest to the real clock difference; keep a few recent ones.
                STATE["skews"] = (STATE["skews"] + [time.time() - p["metadata"]["timestamp"]])[-60:]
                path = os.path.join(frames_dir, "%05d.jpg" % len(frames))
                open(path, "wb").write(base64.b64decode(p["data"]))
                frames.append((p["metadata"]["timestamp"] - STATE["offset"], path))
                ws.call("Page.screencastFrameAck", wait=False, sessionId=p["sessionId"])

            ws.on_event = on_event
            ws.call("Page.startScreencast", format="jpeg", quality=92,
                    maxWidth=int(WIDTH * SCALE), maxHeight=int(HEIGHT * SCALE), everyNthFrame=1)
            # The page only sends frames when it changes: a tiny repaint gives the first one.
            ws.js("document.body.style.outline = '0px solid transparent'")
            time.sleep(scene.get("lead", 0.5))
            ctx["notes"] = []  # notes from setup are not in the clip
            for step in scene.get("steps", []):
                run(ws, step, sid, ctx)
            time.sleep(scene.get("tail", 1.0))
            end_note(ctx)
            # Last frame held until the end of the clip.
            frames.append((clip_time(), frames[-1][1])) if frames else None
            ws.call("Page.stopScreencast")
            ws.on_event = None
            if not frames:
                raise RecordError("no frames for scene " + scene["name"])
            out = os.path.join(out_dir, scene["name"] + ".mp4")
            keep = keep_frames
            if FFMPEG:
                encode(frames, out)
            else:
                out, keep = frames_dir, True
            seconds = frames[-1][0] - frames[0][0]
            log("saved", out, "%.1fs" % seconds, "frames:", len(frames), "(%s)" % frames_dir if keep else "")
            # Note timings on the clip's own clock: the subtitles and chapters are built from them.
            t0, t_end = frames[0][0], frames[-1][0]
            notes = [{"text": n["text"], "start": round(max(0.0, n["start"] - t0), 2),
                      "end": round(min(t_end, n["end"]) - t0, 2)} for n in ctx["notes"]]
            with open(os.path.join(out_dir, scene["name"] + ".notes.json"), "w") as f:
                json.dump({"scene": scene["name"], "lang": lang, "seconds": round(seconds, 2), "notes": notes},
                          f, ensure_ascii=False, indent=1)
            clips.append({"scene": scene["name"], "file": out, "seconds": round(seconds, 2), "frames": len(frames),
                          "notes": len(notes)})
            if not keep:
                shutil.rmtree(frames_dir, ignore_errors=True)
            for step in scene.get("post", []):
                run(ws, step, sid, ctx)
    finally:
        chrome.terminate()
        shutil.rmtree(profile, ignore_errors=True)
    return clips


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("spec")
    parser.add_argument("--base", required=True, help="URL of the local Odoo, e.g. http://127.0.0.1:8093")
    parser.add_argument("--db", required=True)
    parser.add_argument("--login", default="admin")
    parser.add_argument("--password", default="admin")
    parser.add_argument("--only", action="append", help="name of a scene to record (repeatable)")
    parser.add_argument("--frames", action="store_true", help="keep the frames folder (debug)")
    parser.add_argument("--port", type=int, default=9334, help="Chrome debugging port")
    args = parser.parse_args()
    try:
        record(args.spec, args.base, args.db, args.login, args.password, args.only, args.frames, args.port)
    except RecordError as e:
        raise SystemExit(str(e))


if __name__ == "__main__":
    main()
