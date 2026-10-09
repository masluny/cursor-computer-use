"""MCP server: Claude-style computer use for any vision model (built for Grok in Cursor).

Hybrid perception: screenshots for the model's eyes, the accessibility tree for exact targets and labels,
on-device OCR for everything accessibility cannot see. Actions report what changed, so the model needs
fewer turns.

Run: `cursor-computer-use` (stdio MCP server) or `cursor-computer-use --check` (self test, no input events).
"""

from __future__ import annotations

import base64
import functools
import json
import math
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import Field

from . import apps, ax, inputs, ocr, recipes, safety, screen
from .effects import effects

SETTLE_MIN = float(os.environ.get("CU_SETTLE", "0.12"))  # always wait this long after an action
SETTLE_MAX = float(os.environ.get("CU_SETTLE_MAX", "1.5"))  # then until the screen stops moving, at most this
AUTO_SCREENSHOT = os.environ.get("CU_AUTO_SCREENSHOT", "1") != "0"
DEFAULT_GRID = os.environ.get("CU_GRID", "0") == "1"
# Typing into these is refused: they run commands or hold secrets (clicks are still allowed).
PROTECTED_APPS = {a.strip().lower() for a in os.environ.get(
    "CU_PROTECTED_APPS", "Cursor,Terminal,iTerm2,Ghostty,Warp,Passwords,Keychain Access").split(",") if a.strip()}
GLOBAL_COMBOS = {"cmd+space", "cmd+tab", "cmd+shift+tab"}  # allowed even with a protected app in front
# URL schemes that call, message, run code or open local files.
BLOCKED_SCHEMES = {"tel", "facetime", "facetime-audio", "facetime-group", "sms", "imessage", "javascript", "file",
                   "shortcuts", "workflow", "ssh", "telnet", "vnc", "applescript", "x-apple-terminal"}

INSTRUCTIONS = """Computer use for macOS. You see the main display through screenshots and act with mouse and keyboard.
- Coordinates are always pixels in the LATEST screenshot image (not the real screen); the server rescales them.
- Every action waits for the screen to settle and returns a fresh screenshot plus where keyboard focus is;
  "No visible change" means the action did nothing visible. Never take an extra screenshot after an action.
- Fewest calls first: click_text("Label") for anything with visible text (controls via accessibility, anything
  else via OCR), fill(text, target) for fields, wait_for(text) instead of wait, menu("File > Save"), key()
  shortcuts, open_url for links and app deep links, batch for predictable sequences; click(x, y) last.
- Work on the screen only: do not read source code or app files to figure out a UI.
- If a result says ABORTED, stop immediately and tell the user. Never type passwords or payment data."""

mcp = MCPServer("computer-use", instructions=INSTRUCTIONS)

CACHE_DIR = os.path.expanduser("~/Library/Caches/cursor-computer-use")
CALLS_LOG = os.path.join(CACHE_DIR, "calls.log")  # gaps between calls = model thinking time
STATUS_PATH = os.path.join(CACHE_DIR, "status.json")

_lock = threading.RLock()
_recorder = recipes.Recorder()
_state: dict[str, Any] = {"elements": {}, "elements_pid": None, "aborted": False, "last_move": None,
                          "last_thumb": None}


# ---------------------------------------------------------------- plumbing

def _tool(fn):
    """Register a tool: serialize calls, turn any exception into an error the model can read."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _lock:
            t0, ok = time.monotonic(), False
            _recorder.touch()
            if not _state["aborted"]:
                effects.active()
            try:
                result = fn(*args, **kwargs)
                ok = True
                return result
            except ToolError:
                raise
            except Exception as exc:  # noqa: BLE001 - every failure goes back to the model
                raise ToolError(f"{type(exc).__name__}: {exc}") from exc
            finally:
                _log_call(fn.__name__, time.monotonic() - t0, ok)

    return mcp.tool(structured_output=False)(wrapper)


def _log_call(name: str, seconds: float, ok: bool) -> None:
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        if os.path.exists(CALLS_LOG) and os.path.getsize(CALLS_LOG) > 1_000_000:
            os.remove(CALLS_LOG)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S") + f".{int(time.time() * 1000) % 1000:03d}"
        with open(CALLS_LOG, "a") as f:
            f.write(f"{stamp} {name} {seconds:.2f}s {'ok' if ok else 'error'}\n")
    except OSError:
        pass


def _guard() -> None:
    """Refuse input when permissions are missing or the user hit the failsafe corner."""
    if _state["aborted"]:
        raise ToolError("ABORTED: the user stopped computer use (mouse moved to the top-left corner). "
                        "Stop now and ask the user how to proceed. Restart the MCP server to re-enable input.")
    if not ax.is_trusted():
        raise ToolError("Accessibility permission is missing, so mouse/keyboard input would be ignored. "
                        "Tell the user: System Settings > Privacy & Security > Accessibility (called 'Device Control and Data "
                        "Access' on macOS 27+) > enable Cursor, then reload this MCP server in Cursor.")
    front = ax.frontmost_app()[1]
    if front.strip().lower() in safety.SYSTEM_PROMPT_APPS:
        raise ToolError(f"A system dialog or password prompt ({front}) is in front. Only the user may answer it: "
                        "stop and ask them.")
    x, y = inputs.cursor_pos()
    last = _state["last_move"]
    if x <= 2 and y <= 2 and not (last and last[0] <= 2 and last[1] <= 2):
        _state["aborted"] = True
        effects.hide()
        _guard()


def _check_typing(combo: str | None = None) -> None:
    """Keys and text never go to apps that can run commands or hold secrets."""
    name = ax.frontmost_app()[1]
    if name.strip().lower() not in PROTECTED_APPS:
        return
    if combo is not None and _canonical_combo(combo) in GLOBAL_COMBOS:
        return
    raise ToolError(f"Typing into {name} is blocked: it is a protected app (it can run commands or holds secrets). "
                    "Bring the target app forward with open_app first.")


def _canonical_combo(combo: str) -> str:
    """'Shift+Command+Tab' -> 'cmd+shift+tab'."""
    mods, code = inputs.parse_combo(combo)
    order = ["cmd", "shift", "alt", "ctrl", "fn"]
    name = next((k for k, v in inputs.KEYCODES.items() if v == code), str(code))
    return "+".join(sorted(mods, key=order.index) + [name])


def _split_url(url: str) -> tuple[str, str]:
    """(url to open, scheme); bare hosts like 'github.com' or 'localhost:3000' become https/http links."""
    url = url.strip()
    if re.match(r"^(localhost|[\w-]+(\.[\w-]+)+):\d+(/|$)", url):  # host:port, never "tel:123"
        url = ("http://" if url.startswith(("localhost", "127.")) else "https://") + url
    elif not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
        url = "https://" + url
    scheme = url.split(":", 1)[0].lower()
    return url.replace(" ", "%20"), scheme


def _approve(texts, action: str) -> None:
    """Risky targets (send, buy, delete...) need the user's OK in a system alert the model cannot click."""
    word = safety.risky_word(texts)
    if word and not safety.confirm(action, word):
        raise ToolError(f"DECLINED by the user: {action}. Do not retry or work around it; tell the user and ask "
                        "how to continue.")


def _record(name: str, args: dict) -> None:
    """Remember a successful step for recipe_save, as replayable as possible (ids become labels)."""
    args = {k: v for k, v in args.items() if k != "action"}
    if name == "click_element":
        el = _state["elements"].get(int(args.get("id", -1)))
        if el is not None and el.label:
            _recorder.record("click_text", text=el.label, button=args.get("button"), count=args.get("count"),
                             modifiers=args.get("modifiers"))
        elif el is not None:
            cx, cy = screen.geometry().to_image(*el.center)
            _recorder.record("click", x=cx, y=cy, button=args.get("button"), count=args.get("count"))
        return
    _recorder.record(name, **args)


def _to_points(x: float, y: float) -> tuple[float, float]:
    geo = screen.geometry()
    if not (0 <= x <= geo.img_w and 0 <= y <= geo.img_h):
        raise ToolError(f"({x}, {y}) is outside the screenshot ({geo.img_w}x{geo.img_h}).")
    return geo.to_points(x, y)


def _moved(px: float, py: float) -> None:
    _state["last_move"] = (px, py)


def _image(im) -> ImageContent:
    data, fmt = screen.encode(im)
    return ImageContent(type="image", data=base64.b64encode(data).decode(), mime_type=f"image/{fmt}")


def _text_result(text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)])


def _describe(el: ax.Element, geo: screen.Geometry, i: int) -> str:
    cx, cy = geo.to_image(*el.center)
    parts = [f"[{i}] {el.kind}"]
    if el.label:
        parts.append(f'"{el.label}"')
    if el.value:
        parts.append(f"= {el.value}")
    if not el.enabled:
        parts.append("(disabled)")
    if el.focused:
        parts.append("(focused)")
    parts.append(f"at ({cx}, {cy})")
    return " ".join(parts)


def _elements_snapshot(pid: int, geo: screen.Geometry, limit: int) -> tuple[list[str], list]:
    if not ax.is_trusted():
        return ["(UI elements unavailable: Accessibility permission missing)"], []
    elements, truncated = ax.snapshot(pid, (geo.origin_x, geo.origin_y, geo.width_pt, geo.height_pt), limit=limit)
    _state["elements"] = {i: el for i, el in enumerate(elements, 1)}
    _state["elements_pid"] = pid
    lines = [_describe(el, geo, i) for i, el in _state["elements"].items()]
    if not lines:
        lines = ["(no interactive elements exposed by this app; use click_text (OCR) or click(x, y). "
                 "Electron/web apps sometimes need a second call after a moment.)"]
    elif truncated:
        lines.append(f"(list truncated at {len(lines)}; pass a higher limit if needed)")
    marks = []
    for i, el in _state["elements"].items():
        x, y, w, h = el.frame
        x0, y0 = geo.to_image(x, y)
        x1, y1 = geo.to_image(x + w, y + h)
        marks.append((i, (x0, y0, x1, y1)))
    return lines, marks


def _screenshot(note: str = "", grid: bool | None = None, marks: bool = False, limit: int = 150,
                after_action: bool = False) -> CallToolResult:
    geo = screen.geometry()
    im = screen.capture(geo)
    thumb = screen.thumb_of(im)
    pid, name = ax.frontmost_app()
    mx, my = geo.to_image(*inputs.cursor_pos())
    head = [note] if note else []
    head.append(f"Front app: {name}. Mouse at ({mx}, {my}).")
    if ax.is_trusted():
        head.append(f"Focus: {ax.focused_summary(pid)}")
    if not screen.has_screen_permission():
        head.append("WARNING: Screen Recording permission missing; windows may be invisible. "
                    "Tell the user to enable Cursor in System Settings > Privacy & Security > Screen Recording.")
    draw_grid = DEFAULT_GRID if grid is None else grid
    if (after_action and not marks and not draw_grid
            and screen.changed_fraction(thumb, _state["last_thumb"]) < 0.001):
        head.append("No visible change on screen since your last screenshot (the action may have had no "
                    "effect, or changed something invisible). Image omitted: it is the same as the last one.")
        return _text_result("\n".join(head))
    if after_action and _state["last_thumb"] is not None:
        frac = screen.changed_fraction(thumb, _state["last_thumb"])
        box = screen.changed_bbox(thumb, _state["last_thumb"]) if frac < 0.6 else None
        if box:
            k = geo.img_w / screen.THUMB[0]
            head.append(f"Changed area: ({box[0] * k:.0f}, {box[1] * k:.0f})-({box[2] * k:.0f}, {box[3] * k:.0f}).")
        elif frac >= 0.6:
            head.append("Most of the screen changed.")
    _state["last_thumb"] = thumb
    head.append(f"Screenshot {geo.img_w}x{geo.img_h}px (click/scroll/drag coordinates are pixels of this image).")
    if marks:
        lines, boxes = _elements_snapshot(pid, geo, limit)
        im = screen.draw_marks(im, boxes)
        head.append(f"UI elements of {name} (numbered boxes on the image; use click_element(id)):")
        head.extend(lines)
    if draw_grid:
        im = screen.draw_grid(im)
    return CallToolResult(content=[TextContent(type="text", text="\n".join(head)), _image(im)])


def _settle() -> None:
    """Wait until the screen stops changing (animations, page loads), between SETTLE_MIN and SETTLE_MAX."""
    time.sleep(SETTLE_MIN)
    start = time.monotonic()
    prev = screen.live_thumb()
    if prev is None:
        time.sleep(0.4)
        return
    while time.monotonic() - start < SETTLE_MAX:
        time.sleep(0.08)
        cur = screen.live_thumb()
        if screen.changed_fraction(prev, cur) < 0.002:
            return
        prev = cur


def _act(name: str, fn, **args) -> CallToolResult:
    """Run an action, remember it for recipes, return the settled screenshot."""
    msg = fn(**args)
    _record(name, args)
    return _done(msg)


def _done(note: str) -> CallToolResult:
    if not AUTO_SCREENSHOT:
        return _text_result(note)
    _settle()
    return _screenshot(note, after_action=True)


Coord = Annotated[float, Field(description="Pixel in the latest screenshot image")]
Mods = Annotated[list[str] | str | None,
                 Field(description='Modifier keys held during the action, e.g. ["cmd"] or "cmd+shift". '
                                   "Names: cmd, shift, alt (option), ctrl, fn")]


# ---------------------------------------------------------------- finding things by text (accessibility + OCR)

@dataclass
class Target:
    score: float
    center: tuple[float, float]  # global points
    frame: tuple[float, float, float, float]
    desc: str
    el_id: int | None = None  # set when found through accessibility


def _dedupe(targets: list[Target]) -> list[Target]:
    """Drop lower-scored targets that are the same thing as a better one (same spot, child or container)."""
    def inside(p, f):
        return f[0] <= p[0] <= f[0] + f[2] and f[1] <= p[1] <= f[1] + f[3]

    kept: list[Target] = []
    for t in targets:
        same = any(abs(t.center[0] - k.center[0]) < 14 and abs(t.center[1] - k.center[1]) < 14
                   or (k.frame[2] * k.frame[3] < 250_000 and inside(t.center, k.frame))
                   or (t.frame[2] * t.frame[3] < 250_000 and inside(k.center, t.frame))
                   for k in kept)
        if not same:
            kept.append(t)
    return kept


def _candidates(query: str, prefer_inputs: bool = False, force_ocr: bool = False,
                reuse: bool = False) -> tuple[list[Target], str]:
    """Everything on screen matching query, best first: accessibility elements and static text, plus OCR when
    accessibility has no strong match. reuse=True ranks the elements of the previous scan (ids stay valid)."""
    geo = screen.geometry()
    pid, app = ax.frontmost_app()
    targets: list[Target] = []
    if ax.is_trusted():
        if not (reuse and _state["elements_pid"] == pid):
            elements, _ = ax.snapshot(pid, (geo.origin_x, geo.origin_y, geo.width_pt, geo.height_pt),
                                      limit=400, include_text=True)
            _state["elements"] = {i: el for i, el in enumerate(elements, 1)}
            _state["elements_pid"] = pid
        for score, i in ax.rank(query, _state["elements"], prefer_inputs):
            el = _state["elements"][i]
            targets.append(Target(score, el.center, el.frame, _describe(el, geo, i), i))
    if force_ocr or not targets or targets[0].score < 80:
        for hit in ocr.locate(query)[:20]:
            cx, cy = geo.to_image(*hit.center)
            targets.append(Target(hit.score - 6, hit.center, hit.frame, f'text "{hit.text}" at ({cx}, {cy}) [OCR]'))
    targets.sort(key=lambda t: -t.score)
    return _dedupe(targets), app


def _pick_near(targets: list[Target], query: str, app: str, near: str) -> Target:
    """Among good matches for query, the one closest to the best match for `near` (e.g. the "Add" button in
    the row of a given song)."""
    if not targets:
        return _pick(targets, query, app)
    anchors, _ = _candidates(near, reuse=True)
    if not anchors:
        raise ToolError(f'Found "{query}" but nothing matching near="{near}" in {app}.')
    ax_, ay_ = anchors[0].center
    pool = [t for t in targets if t.score >= targets[0].score - 25]
    return min(pool, key=lambda t: math.hypot(t.center[0] - ax_, t.center[1] - ay_))


def _pick(targets: list[Target], query: str, app: str) -> Target:
    if not targets:
        raise ToolError(f'Nothing matching "{query}" is visible in {app} (checked accessibility and OCR). '
                        "It may be off screen (scroll), in another app, or worded differently; look at the screenshot.")
    if len(targets) > 1 and targets[1].score > targets[0].score - 6:
        listed = "\n".join(t.desc for t in targets[:8])
        raise ToolError(f'Several things match "{query}" in {app}. Pick one with click_element(id) (accessibility) '
                        f"or click(x, y) (OCR), or use a longer, more specific text:\n{listed}")
    return targets[0]


def _click_target(t: Target, button="left", count=1, modifiers=None) -> str:
    if t.el_id is not None:
        return _do_click_element(t.el_id, button, count, modifiers)
    px, py = t.center
    _, hit_desc, hit_texts = ax.element_at(px, py) if ax.is_trusted() else (None, "", [])
    _approve([t.desc, *hit_texts], f"Kliknięcie: {t.desc}")
    effects.point()
    inputs.click(px, py, button, int(count), inputs.parse_modifiers(modifiers))
    _moved(px, py)
    effects.click(px, py, int(count))
    return f"clicked {t.desc}" + (f" (hit {hit_desc})" if hit_desc else "")


# ---------------------------------------------------------------- actions (shared by tools and batch)

def _do_click(x, y, button="left", count=1, modifiers=None) -> str:
    px, py = _to_points(x, y)
    _, hit_desc, hit_texts = ax.element_at(px, py) if ax.is_trusted() else (None, "", [])
    _approve(hit_texts, f"Kliknięcie w {hit_desc or f'punkt ({x:.0f}, {y:.0f})'}")
    effects.point()
    inputs.click(px, py, button, int(count), inputs.parse_modifiers(modifiers))
    _moved(px, py)
    effects.click(px, py, int(count))
    what = {1: "click", 2: "double-click", 3: "triple-click"}.get(int(count), f"{count}x click")
    mods = "+".join(inputs.parse_modifiers(modifiers))
    return (f"{(mods + '+') if mods else ''}{button} {what} at ({x:.0f}, {y:.0f})"
            + (f" on {hit_desc}" if hit_desc else " (nothing identifiable there; check the screenshot)"))


def _do_click_element(id, button="left", count=1, modifiers=None, press=False) -> str:
    el = _state["elements"].get(int(id))
    if el is None:
        raise ToolError(f"No element [{id}]. Call screenshot(marks=true), ui_elements() or find() first; "
                        "ids change on every call.")
    what = f'{el.kind} "{el.label}"' if el.label else el.kind
    _approve([el.label, *(t for t, _ in el.texts)], f"Kliknięcie: {what}")
    if press:
        if not ax.press(el.ref):
            raise ToolError(f"AXPress on [{id}] failed; try a normal click_element.")
        effects.click(*el.center)
        return f'pressed [{id}] {what} via accessibility'
    frame = ax.element_frame(el.ref)  # re-read: the element may have moved since the snapshot
    if frame is None:
        raise ToolError(f"Element [{id}] no longer exists (the UI changed). Take a new screenshot(marks=true).")
    x, y, w, h = frame
    px, py = x + w / 2, y + h / 2
    if not screen.geometry().contains_point(px, py):
        raise ToolError(f"Element [{id}] is off screen now; scroll it into view and refresh.")
    hit, hit_desc, _ = ax.element_at(px, py)
    if hit is not None and not ax.related(hit, el.ref):
        # Something else sits on top (a pop-up, another window, or the UI changed since the snapshot).
        if button == "left" and int(count) == 1 and not modifiers and ax.press(el.ref):
            effects.click(px, py)
            return f'[{id}] {what} was covered by {hit_desc}; pressed it through accessibility instead'
        note = f" (warning: the point was covered by {hit_desc})"
    else:
        note = ""
    effects.point()
    inputs.click(px, py, button, int(count), inputs.parse_modifiers(modifiers))
    _moved(px, py)
    effects.click(px, py, int(count))
    return f"clicked [{id}] {what}{note}"


def _do_click_text(text, button="left", count=1, modifiers=None, near=None) -> str:
    targets, app = _candidates(text)
    target = _pick_near(targets, text, app, near) if near else _pick(targets, text, app)
    return _click_target(target, button, count, modifiers)


def _do_fill(text, target=None, submit=False, clear=True, near=None) -> str:
    done = []
    if target:
        targets, app = _candidates(target, prefer_inputs=True)
        done.append(_click_target(_pick_near(targets, target, app, near) if near else _pick(targets, target, app)))
        time.sleep(0.15)
    _check_typing()
    pid = ax.frontmost_app()[0]
    cleared = False
    if clear and ax.focused_is_text_input(pid):
        inputs.key("cmd+a")
        cleared = True
    done.append(_do_type(text))
    if cleared:
        done[-1] = done[-1].replace("typed", "replaced the field with", 1).replace("pasted", "replaced the field with", 1)
    if submit:
        inputs.key("return")
        done.append("pressed Return")
    return "; ".join(done)


def _present(text: str) -> bool:
    targets, _ = _candidates(text)
    return any(t.score >= 60 for t in targets)


def _do_wait_for(text, timeout=10.0, gone=False) -> str:
    timeout = max(0.5, min(float(timeout), 60.0))
    start = time.monotonic()
    while True:
        if _present(text) != bool(gone):
            return f'"{text}" {"is gone" if gone else "appeared"} after {time.monotonic() - start:.1f}s'
        if time.monotonic() - start >= timeout:
            raise TimeoutError(f'"{text}" {"is still visible" if gone else "did not appear"} within {timeout:g}s')
        time.sleep(0.3)


def _front_window_center() -> tuple[float, float]:
    pid = ax.frontmost_app()[0]
    for w in ax.windows():
        if w["pid"] == pid:
            x, y, wd, ht = w["frame"]
            return x + wd / 2, y + ht / 2
    geo = screen.geometry()
    return geo.origin_x + geo.width_pt / 2, geo.origin_y + geo.height_pt / 2


def _do_scroll_to(text, x=None, y=None, direction="down", max_scrolls=12) -> str:
    px, py = _to_points(x, y) if x is not None and y is not None else _front_window_center()
    for i in range(int(max_scrolls) + 1):
        if _present(text):
            return f'"{text}" is visible' + (f" after {i} scroll(s) {direction}" if i else " (no scrolling needed)")
        if i == int(max_scrolls):
            break
        before = screen.live_thumb()
        effects.point()
        inputs.scroll(px, py, direction, 4)
        _moved(px, py)
        _settle()
        if screen.changed_fraction(before, screen.live_thumb()) < 0.002:
            raise TimeoutError(f'reached the end of the scrollable area without finding "{text}"')
    raise TimeoutError(f'"{text}" not found after {max_scrolls} scrolls {direction}')


def _do_move(x, y) -> str:
    px, py = _to_points(x, y)
    effects.point()
    inputs.glide(px, py)
    _moved(px, py)
    return f"mouse moved to ({x:.0f}, {y:.0f})"


def _do_drag(from_x, from_y, to_x, to_y, button="left", modifiers=None) -> str:
    p0, p1 = _to_points(from_x, from_y), _to_points(to_x, to_y)
    effects.point()
    effects.click(*p0)
    inputs.drag(*p0, *p1, button=button, mods=inputs.parse_modifiers(modifiers))
    _moved(*p1)
    effects.click(*p1)
    return f"dragged ({from_x:.0f}, {from_y:.0f}) -> ({to_x:.0f}, {to_y:.0f})"


def _do_scroll(x, y, direction, amount=5, modifiers=None) -> str:
    px, py = _to_points(x, y)
    effects.point()
    inputs.scroll(px, py, direction, int(amount), inputs.parse_modifiers(modifiers))
    _moved(px, py)
    effects.pulse()
    return f"scrolled {direction} {amount} at ({x:.0f}, {y:.0f})"


def _do_type(text, method="auto") -> str:
    _check_typing()
    effects.pulse()
    if method == "paste" or (method == "auto" and len(text) > 200):
        restored = inputs.paste_text(text)
        return f"pasted {len(text)} chars" + ("" if restored else " (clipboard left holding the text)")
    inputs.type_text(text)
    return f"typed {len(text)} chars"


def _do_key(keys, repeat=1) -> str:
    _check_typing(keys)
    effects.pulse()
    inputs.key(keys, int(repeat))
    return f"pressed {keys}" + (f" x{repeat}" if int(repeat) > 1 else "")


def _do_wait(seconds=1.0) -> str:
    seconds = max(0.0, min(float(seconds), 30.0))
    time.sleep(seconds)
    return f"waited {seconds:g}s"


def _do_open_app(name) -> str:
    args = ["-b", name] if name.count(".") >= 2 and " " not in name else ["-a", name]
    proc = subprocess.run(["/usr/bin/open", *args], capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise ToolError(f"Could not open {name!r}: {proc.stderr.strip()}")
    deadline = time.time() + 8
    front = ax.frontmost_app()[1]
    while time.time() < deadline and name.lower() not in front.lower():
        time.sleep(0.2)
        front = ax.frontmost_app()[1]
    return f"opened {name}; front app is now {front}"


def _do_open_url(url, app=None) -> str:
    url, scheme = _split_url(url)
    if scheme in BLOCKED_SCHEMES:
        raise ToolError(f"{scheme}: links are blocked here (they call, message, run code or open local files). "
                        "Ask the user to do this themselves.")
    args = ["-a", app, url] if app else [url]
    proc = subprocess.run(["/usr/bin/open", *args], capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise ToolError(f"Could not open {url!r}: {proc.stderr.strip()}")
    time.sleep(0.4)
    return f"opened {url}" + (f" in {app}" if app else "") + f"; front app is now {ax.frontmost_app()[1]}"


def _do_menu(path, app=None) -> tuple[bool, str]:
    """(pressed, text): pressed is False when the path named a menu and its items were listed."""
    pid = _app_pid(app)
    if app and ax.frontmost_app()[0] != pid:
        _do_open_app(app)  # menus of background apps cannot open
    parts = [p.strip() for p in (path if isinstance(path, list) else path.replace("→", ">").split(">")) if p.strip()]
    if parts:
        _approve([parts[-1]], f"Polecenie menu: {' > '.join(parts)}")
    pressed, text = ax.menu_select(pid, parts)
    if pressed:
        effects.pulse()
    return pressed, ("selected menu " + text) if pressed else text


READ_ONLY_APP_ACTIONS = {"now_playing", "current_page", "get_volume"}


def _do_app_action(app, do, args=None) -> str:
    """`do` is the app's action name (in batch/recipe steps "action" already names the step type)."""
    effects.pulse()
    return f"{app}.{do}: {apps.run(app, do, args)}"


def _app_pid(app: str | None) -> int:
    if not app:
        return ax.frontmost_app()[0]
    pid = ax.pid_for_app(app)
    if pid is None:
        raise ToolError(f"App {app!r} is not running. Use open_app first.")
    return pid


_BATCH = {
    "click": _do_click, "click_text": _do_click_text, "click_element": _do_click_element, "fill": _do_fill,
    "move_mouse": _do_move, "drag": _do_drag, "scroll": _do_scroll, "type_text": _do_type, "key": _do_key,
    "wait": _do_wait, "wait_for": _do_wait_for, "scroll_to": _do_scroll_to, "open_app": _do_open_app, "open_url": _do_open_url,
    "menu": lambda path, app=None: _do_menu(path, app)[1],
    "app_action": _do_app_action,
}


# ---------------------------------------------------------------- tools: looking

@_tool
def screenshot(
    marks: Annotated[bool, Field(description="Overlay numbered boxes on the front app's clickable elements and list "
                                             "them; then use click_element(id). Best for small or dense targets.")] = False,
    grid: Annotated[bool | None, Field(description="Overlay a labeled 100px coordinate grid to help estimate "
                                                   "positions (default from CU_GRID)")] = None,
    limit: Annotated[int, Field(description="Max elements listed when marks=true")] = 150,
) -> CallToolResult:
    """Capture the main display. Start a task with this. The returned image defines the coordinate space for
    every click/scroll/drag/zoom. Not needed after actions: they return a screenshot themselves."""
    result = _screenshot(grid=grid, marks=marks, limit=limit)
    listing = recipes.summary()
    if listing:
        result.content[0].text += "\n" + listing
    return result


@_tool
def zoom(
    x0: Coord, y0: Coord, x1: Coord, y1: Coord,
    grid: Annotated[bool, Field(description="Draw grid lines labeled in full-screenshot coordinates")] = True,
) -> CallToolResult:
    """High-resolution view of the rectangle (x0, y0)-(x1, y1) of the latest screenshot, for small targets and
    icons (to read text, read_text is cheaper). The grid labels are full-screenshot coordinates: read the exact
    spot from them and click(x, y) directly. The zoomed image's own pixels are not coordinates."""
    geo = screen.geometry()
    (ax0, ay0), (ax1, ay1) = geo.to_points(min(x0, x1), min(y0, y1)), geo.to_points(max(x0, x1), max(y0, y1))
    if ax1 - ax0 < 4 or ay1 - ay0 < 4:
        raise ToolError("Zoom region is too small; give a rectangle at least ~10px each side.")
    im = screen.capture_region(ax0, ay0, ax1 - ax0, ay1 - ay0)
    lo_x, hi_x, lo_y, hi_y = min(x0, x1), max(x0, x1), min(y0, y1), max(y0, y1)
    if grid:
        im = screen.draw_grid_mapped(im, lo_x, lo_y, hi_x, hi_y)
    note = (f"Zoom of screenshot region ({lo_x:.0f}, {lo_y:.0f})-({hi_x:.0f}, {hi_y:.0f}), shown at {im.width}x{im.height}. "
            + ("Grid labels are full-screenshot coordinates: click(x, y) with them directly."
               if grid else "Do not click using this image's pixels."))
    return CallToolResult(content=[TextContent(type="text", text=note), _image(im)])


@_tool
def read_text(
    x0: Annotated[float | None, Field(description="Optional region (screenshot pixels); default whole screen")] = None,
    y0: float | None = None, x1: float | None = None, y1: float | None = None,
) -> str:
    """Read the text on screen with OCR: one line per text line with its center in screenshot pixels. Much
    cheaper than an image when you need to read results, messages or lists."""
    geo = screen.geometry()
    region = None
    if None not in (x0, y0, x1, y1):
        (ax0, ay0), (ax1, ay1) = geo.to_points(min(x0, x1), min(y0, y1)), geo.to_points(max(x0, x1), max(y0, y1))
        region = (ax0, ay0, max(ax1 - ax0, 4), max(ay1 - ay0, 4))
    boxes = ocr.recognize(region)
    if not boxes:
        return "(no text found)"
    lines = []
    for b in boxes[:400]:
        cx, cy = geo.to_image(*b.center)
        lines.append(f"({cx}, {cy}) {b.text}")
    return "\n".join(lines)


@_tool
def find(text: Annotated[str, Field(description="Visible text or label to look for")]) -> str:
    """List everything on screen matching text (accessibility elements with ids, plus OCR hits with
    coordinates), best first, without clicking."""
    geo = screen.geometry()
    targets, app = _candidates(text, force_ocr=True)
    if not targets:
        return f'Nothing matching "{text}" is visible in {app}.'
    return f'Matches for "{text}" in {app} (best first):\n' + "\n".join(t.desc for t in targets[:15])


@_tool
def ui_elements(
    app: Annotated[str | None, Field(description="App name; default is the frontmost app")] = None,
    limit: Annotated[int, Field(description="Max elements")] = 150,
) -> str:
    """List the clickable/editable elements of an app's front window (accessibility tree) with their centers in
    screenshot pixels. Text only, cheaper than screenshot(marks=true). Ids work with click_element."""
    geo = screen.geometry()
    pid = _app_pid(app)
    lines, _ = _elements_snapshot(pid, geo, limit)
    return f"UI elements of {ax.app_name(pid)}:\n" + "\n".join(lines)


@_tool
def list_apps() -> str:
    """List visible windows (front to back) with app name, title and bounds in screenshot pixels."""
    geo = screen.geometry()
    pid, name = ax.frontmost_app()
    lines = [f"Front app: {name}", "Windows (front to back):"]
    for w in ax.windows():
        x, y, wd, ht = w["frame"]
        x0, y0 = geo.to_image(x, y)
        x1, y1 = geo.to_image(x + wd, y + ht)
        title = f' "{w["title"]}"' if w["title"] else ""
        lines.append(f"- {w['app']}{title} ({x0}, {y0})-({x1}, {y1})")
    return "\n".join(lines)


# ---------------------------------------------------------------- tools: acting

@_tool
def click(
    x: Coord, y: Coord,
    button: Literal["left", "right", "middle"] = "left",
    count: Annotated[int, Field(description="1 = click, 2 = double-click, 3 = triple-click (select line)")] = 1,
    modifiers: Mods = None,
) -> CallToolResult:
    """Click at (x, y) in screenshot pixels. Aim at the center of the target. Prefer click_text for anything
    that has visible text."""
    _guard()
    return _act("click", _do_click, x=x, y=y, button=button, count=count, modifiers=modifiers)


@_tool
def click_text(
    text: Annotated[str, Field(description="Visible text: a button/tab/link/menu item label, a field's placeholder, "
                                           "a list item, any word on screen. Case, punctuation and accents are ignored.")],
    button: Literal["left", "right", "middle"] = "left",
    count: int = 1,
    modifiers: Mods = None,
    near: Annotated[str | None, Field(description="Disambiguate: pick the match closest to this other text, e.g. "
                                                  'click_text("Add", near="Take on Me")')] = None,
) -> CallToolResult:
    """Click whatever shows this text. Looks through accessibility first (controls, values, plain text), then OCR
    for canvas/custom UIs, and clicks the best match. One call, no aiming. If several things match equally,
    the error lists them with ids/coordinates."""
    _guard()
    return _act("click_text", _do_click_text, text=text, button=button, count=count, modifiers=modifiers, near=near)


@_tool
def click_element(
    id: Annotated[int, Field(description="Element id from the latest screenshot(marks=true), ui_elements() or find()")],
    button: Literal["left", "right", "middle"] = "left",
    count: int = 1,
    modifiers: Mods = None,
    press: Annotated[bool, Field(description="Use the accessibility AXPress action instead of a mouse click "
                                             "(works even if covered; does not move the mouse)")] = False,
) -> CallToolResult:
    """Click a UI element by its numbered id. More precise than click(x, y)."""
    _guard()
    return _act("click_element", _do_click_element, id=id, button=button, count=count, modifiers=modifiers, press=press)


@_tool
def fill(
    text: Annotated[str, Field(description="Text to enter")],
    target: Annotated[str | None, Field(description="Label, placeholder or nearby text of the field to click first; "
                                                    "omit to type into the focused field")] = None,
    submit: Annotated[bool, Field(description="Press Return afterwards")] = False,
    clear: Annotated[bool, Field(description="Replace the field's current content (only when a text field "
                                             "has focus)")] = True,
    near: Annotated[str | None, Field(description="Pick the field closest to this text when several match")] = None,
) -> CallToolResult:
    """Fill a text field in one call: find and click it, replace its content, type, optionally press Return.
    The result reports the field's new content."""
    _guard()
    return _act("fill", _do_fill, text=text, target=target, submit=submit, clear=clear, near=near)


@_tool
def move_mouse(x: Coord, y: Coord) -> CallToolResult:
    """Move the mouse without clicking (hover menus, tooltips)."""
    _guard()
    return _done(_do_move(x, y))


@_tool
def drag(
    from_x: Coord, from_y: Coord, to_x: Coord, to_y: Coord,
    button: Literal["left", "right", "middle"] = "left",
    modifiers: Mods = None,
) -> CallToolResult:
    """Press at (from_x, from_y), drag to (to_x, to_y), release. For sliders, selections, moving files/windows."""
    _guard()
    return _act("drag", _do_drag, from_x=from_x, from_y=from_y, to_x=to_x, to_y=to_y, button=button, modifiers=modifiers)


@_tool
def scroll(
    x: Coord, y: Coord,
    direction: Literal["up", "down", "left", "right"],
    amount: Annotated[int, Field(description="Wheel notches, ~80px each. 5 is about half a window")] = 5,
    modifiers: Mods = None,
) -> CallToolResult:
    """Scroll the area under (x, y)."""
    _guard()
    return _act("scroll", _do_scroll, x=x, y=y, direction=direction, amount=amount, modifiers=modifiers)


@_tool
def type_text(
    text: Annotated[str, Field(description="Text to type. \\n presses Return, \\t presses Tab. Any Unicode works.")],
    method: Annotated[Literal["auto", "type", "paste"],
                      Field(description="type = keystrokes; paste = via clipboard (fast for long text); "
                                        "auto = paste above 200 chars")] = "auto",
) -> CallToolResult:
    """Type text into the focused field (fill does click + replace + type in one call)."""
    _guard()
    return _act("type_text", _do_type, text=text, method=method)


@_tool
def key(
    keys: Annotated[str, Field(description='Key or combo: "return", "escape", "tab", "cmd+c", "cmd+shift+t", '
                                           '"alt+left", "pagedown", "f5". Letters by US keyboard position.')],
    repeat: int = 1,
) -> CallToolResult:
    """Press a key or keyboard shortcut."""
    _guard()
    return _act("key", _do_key, keys=keys, repeat=repeat)


@_tool
def wait(seconds: Annotated[float, Field(description="0 to 30")] = 1.0) -> CallToolResult:
    """Wait a fixed time, then take a screenshot. Prefer wait_for(text) when you know what should appear."""
    return _act("wait", _do_wait, seconds=seconds)


@_tool
def wait_for(
    text: Annotated[str, Field(description="Text or label that should appear (or disappear with gone=true)")],
    timeout: Annotated[float, Field(description="Seconds, up to 60")] = 10.0,
    gone: bool = False,
) -> CallToolResult:
    """Wait until text appears on screen (or disappears), checking accessibility and OCR every ~0.3 s; returns as
    soon as it does, with a screenshot. Use after anything that loads."""
    try:
        note = _do_wait_for(text, timeout, gone)
        _record("wait_for", {"text": text, "timeout": timeout, "gone": gone or None})
    except TimeoutError as exc:
        note = f"TIMEOUT: {exc}."
    return _screenshot(note, after_action=True)


@_tool
def scroll_to(
    text: Annotated[str, Field(description="Text to bring into view")],
    x: Annotated[float | None, Field(description="Where to scroll (screenshot pixels); default: middle of the "
                                                 "front window")] = None,
    y: float | None = None,
    direction: Literal["up", "down", "left", "right"] = "down",
    max_scrolls: int = 12,
) -> CallToolResult:
    """Scroll until text is visible, checking after every step; stops at the end of the list. Replaces
    repeated scroll + look turns."""
    _guard()
    try:
        note = _do_scroll_to(text, x, y, direction, max_scrolls)
        _record("scroll_to", {"text": text, "x": x, "y": y, "direction": direction, "max_scrolls": max_scrolls})
    except TimeoutError as exc:
        note = f"NOT FOUND: {exc}."
    return _screenshot(note, after_action=True)


@_tool
def open_app(name: Annotated[str, Field(description='App name ("Safari", "System Settings") or bundle id')]) -> CallToolResult:
    """Launch an app or bring it to the front."""
    _guard()
    return _act("open_app", _do_open_app, name=name)


@_tool
def open_url(
    url: Annotated[str, Field(description='Web link or app deep link, e.g. "https://github.com", '
                                          '"spotify:search:take on me", "x-apple.systempreferences:"')],
    app: Annotated[str | None, Field(description='Open with this app, e.g. "Safari" or "Google Chrome"')] = None,
) -> CallToolResult:
    """Open a URL or an app deep link directly (often skips several clicks)."""
    _guard()
    return _act("open_url", _do_open_url, url=url, app=app)


@_tool
def menu(
    path: Annotated[str, Field(description='Menu path separated by ">", e.g. "File > Export > PDF". A path that ends '
                                           'at a menu ("View") lists its items; "" lists the menu bar.')],
    app: Annotated[str | None, Field(description="App name; default is the frontmost app")] = None,
) -> CallToolResult:
    """Choose a menu bar command through accessibility (no pixel hunting), or list a menu's items.
    On a typo the error lists the available items."""
    _guard()
    pressed, text = _do_menu(path, app)
    if not pressed:
        return _text_result(text)
    _record("menu", {"path": path, "app": app})
    return _done(text)


@_tool
def app_action(
    app: Annotated[str, Field(description='App key: music, spotify, notes, reminders, safari, finder, mail, system; '
                                          '"?" lists every action')],
    action: Annotated[str, Field(description='Action name, e.g. "create_note", "play_uri", "now_playing"')] = "?",
    args: Annotated[dict[str, Any] | list[Any] | None, Field(description='Arguments by name, e.g. {"title": "Zakupy", '
                                                                         '"body": "mleko<br>chleb"}')] = None,
) -> CallToolResult:
    """Do a common task in a scriptable app with one ready-made AppleScript action instead of clicking:
    music/spotify play, pause, next, previous, play_song(query) / play_uri(uri), now_playing; notes.create_note(title,
    body); reminders.create_reminder(title); safari.current_page; finder.reveal(path); mail.compose(to, subject,
    body) (draft only, never sent); system.get_volume / set_volume(level). First use of an app asks the user to
    allow Automation once."""
    if app.strip() == "?" or action.strip() == "?":
        return _text_result("App actions:\n" + apps.catalog())
    _guard()
    if action in READ_ONLY_APP_ACTIONS:
        return _text_result(_do_app_action(app, action, args))
    return _act("app_action", _do_app_action, app=app, do=action, args=args)


@_tool
def recipe_list() -> str:
    """List saved recipes (name, parameters, description). The first screenshot of a task lists them too."""
    return recipes.summary(limit=100) or "No recipes saved yet."


@_tool
def recipe_save(
    name: Annotated[str, Field(description='Short name, e.g. "spotify-like-song"')],
    description: Annotated[str, Field(description="What it does and when to use it")],
    params: Annotated[dict[str, str] | None, Field(description='Parameter name -> the value used in this run, e.g. '
                                                               '{"song": "Take On Me"}; that text becomes {{song}}')] = None,
    steps: Annotated[list[dict[str, Any]] | None, Field(description="Steps in batch format; omit to save the steps "
                                                                    "recorded during this task")] = None,
) -> str:
    """Save the actions that just completed a task as a reusable recipe. Do it when the user asks, or when the
    task is clearly recurring. Recorded steps include detours; pass `steps` to keep only the clean path."""
    chosen = [dict(st) for st in (steps or _recorder.steps)]
    if not chosen:
        raise ToolError("Nothing recorded for this task yet. Pass `steps` explicitly.")
    r = recipes.save(name, description, chosen, params)
    lines = [f"{i}. {json.dumps(st, ensure_ascii=False)}" for i, st in enumerate(r["steps"], 1)]
    return (f"Saved recipe {r['name']}({', '.join(r['params'])}) with {len(r['steps'])} steps:\n"
            + "\n".join(lines) + f"\nFile: {recipes.DIR}/{r['name']}.json")


@_tool
def recipe_run(
    name: Annotated[str, Field(description="Recipe name from the list")],
    params: Annotated[dict[str, str] | None, Field(description='Values for its parameters, e.g. {"song": "Hey Jude"}')] = None,
) -> CallToolResult:
    """Replay a saved recipe in one call. Before each click/fill it waits up to 6 s for the target to appear.
    If a step no longer matches the screen it stops there and you continue the task yourself."""
    _guard()
    r = recipes.load(name)
    steps = recipes.render(r["steps"], params or {})
    log, ok = [], True
    _recorder.paused = True
    try:
        for i, step in enumerate(steps, 1):
            step = dict(step)
            act = step.pop("action")
            try:
                _guard()
                target = step.get("text") if act == "click_text" else step.get("target") if act == "fill" else None
                if target:
                    _do_wait_for(target, timeout=6)
                log.append(f"{i}. {_BATCH[act](**step)}")
            except Exception as exc:  # noqa: BLE001
                ok = False
                log.append(f"{i}. FAILED ({act}): {exc}")
                log.append(f"Recipe stopped at step {i}: the screen no longer matches. Continue the task yourself.")
                break
            if act not in ("wait", "wait_for"):
                _settle()
    finally:
        _recorder.paused = False
    recipes.note_run(name, ok)
    return _done(f"recipe {r['name']} {'done' if ok else 'stopped'}:\n" + "\n".join(log))


@_tool
def clipboard(
    action: Literal["read", "write"],
    text: Annotated[str | None, Field(description="Text to put on the clipboard (write)")] = None,
) -> str:
    """Read or write the clipboard as plain text."""
    if action == "read":
        return inputs.read_clipboard() or "(clipboard has no text)"
    _guard()
    inputs.write_clipboard(text or "")
    return f"clipboard now holds {len(text or '')} chars"


@_tool
def batch(
    actions: Annotated[list[dict[str, Any]], Field(
        description='Steps run in order, each {"action": <name>, ...same arguments as the tool}. Actions: click, '
                    "click_text, click_element, fill, move_mouse, drag, scroll, scroll_to, type_text, key, wait, "
                    "wait_for, open_app, open_url, menu, app_action (as {\"action\": \"app_action\", \"app\": \"notes\", "
                    "\"do\": \"create_note\", \"args\": {...}}). Example: "
                    '[{"action": "open_url", "url": "spotify:search:take on me"}, '
                    '{"action": "wait_for", "text": "Songs"}, {"action": "click_text", "text": "Take on Me"}]')],
) -> CallToolResult:
    """Run several actions with one screenshot at the end. wait_for steps act as checkpoints. Stops at the first
    failing step and says which one."""
    _guard()
    log = []
    for i, step in enumerate(actions, 1):
        step = dict(step)
        name = step.pop("action", None)
        fn = _BATCH.get(name)
        if fn is None:
            raise ToolError(f"step {i}: unknown action {name!r}; valid: {', '.join(_BATCH)}")
        try:
            _guard()
            log.append(f"{i}. {fn(**step)}")
            _record(name, step)
        except Exception as exc:  # noqa: BLE001
            log.append(f"{i}. FAILED ({name}): {exc}")
            log.append("Remaining steps skipped.")
            break
        if name not in ("wait", "wait_for"):
            _settle()
    return _done("\n".join(log))


# ---------------------------------------------------------------- CLI

def check() -> int:
    """Self test without sending any input events. Run it from Cursor's terminal so the permissions checked are Cursor's."""
    ok = True
    print(f"Python {sys.version.split()[0]} at {sys.executable}")
    trusted = ax.is_trusted()
    rec = screen.has_screen_permission()
    print(f"Accessibility permission: {'OK' if trusted else 'MISSING'}")
    print(f"Screen Recording permission: {'OK' if rec else 'MISSING'}")
    ok &= trusted and rec
    geo = screen.geometry()
    print(f"Main display {geo.width_pt:.0f}x{geo.height_pt:.0f} pt -> screenshots {geo.img_w}x{geo.img_h} px "
          f"(scale {geo.scale:.3f})")
    try:
        t = time.time()
        im = screen.capture(geo)
        path = os.path.join(os.environ.get("TMPDIR", "/tmp"), "cu_check.jpg")
        im.save(path, quality=80)
        print(f"Screenshot OK in {time.time() - t:.2f}s -> {path}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"Screenshot FAILED: {exc}")
    pid, name = ax.frontmost_app()
    print(f"Front app: {name} (pid {pid})")
    if trusted:
        t = time.time()
        lines, _ = _elements_snapshot(pid, geo, 40)
        print(f"Accessibility snapshot in {time.time() - t:.2f}s, first elements:")
        for line in lines[:15]:
            print("   ", line)
    t = time.time()
    print(f"OCR: {len(ocr.recognize())} text lines in {time.time() - t:.2f}s")
    print("All good." if ok else "Fix the MISSING items: System Settings > Privacy & Security, enable the app "
                                 "that runs this (Cursor), then restart it.")
    return 0 if ok else 1


def _write_status(extra: dict | None = None) -> None:
    """Record what the server can do from inside the app that launched it (Cursor), readable from outside."""
    try:
        os.makedirs(CACHE_DIR, exist_ok=True)
        data = {}
        if extra and os.path.exists(STATUS_PATH):
            with open(STATUS_PATH) as f:
                data = json.load(f)
        else:
            data = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "pid": os.getpid(), "ppid": os.getppid(),
                    "accessibility": ax.is_trusted(), "screen_recording": screen.has_screen_permission()}
        data.update(extra or {})
        with open(STATUS_PATH, "w") as f:
            json.dump(data, f, indent=1, ensure_ascii=False)
    except (OSError, ValueError):
        pass


def _probe() -> None:
    """Background health check right after start: capture, accessibility and OCR speed (no input events)."""
    time.sleep(1.0)
    info: dict[str, Any] = {}
    with _lock:
        try:
            geo = screen.geometry()
            t = time.monotonic()
            screen.capture(geo)
            info["capture_ms"] = round((time.monotonic() - t) * 1000)
            pid, name = ax.frontmost_app()
            info["front_app"] = name
            if ax.is_trusted():
                t = time.monotonic()
                els, _ = ax.snapshot(pid, (geo.origin_x, geo.origin_y, geo.width_pt, geo.height_pt),
                                     limit=400, include_text=True)
                info["ax_ms"] = round((time.monotonic() - t) * 1000)
                info["ax_controls"] = sum(1 for e in els if e.role != "AXStaticText")
                info["ax_texts"] = sum(1 for e in els if e.role == "AXStaticText")
                info["focus"] = ax.focused_summary(pid)
                info["under_mouse"] = ax.element_at(*inputs.cursor_pos())[1]
                info["menu_bar"] = ax.menu_select(pid, [])[1][:160]
            t = time.monotonic()
            info["ocr_lines"] = len(ocr.recognize())
            info["ocr_ms"] = round((time.monotonic() - t) * 1000)
        except Exception as exc:  # noqa: BLE001
            info["probe_error"] = f"{type(exc).__name__}: {exc}"
    _write_status(info)


def main() -> None:
    if "--check" in sys.argv:
        raise SystemExit(check())
    if "--request-permissions" in sys.argv:
        ax.is_trusted(prompt=True)
        import Quartz
        Quartz.CGRequestScreenCaptureAccess()
        print("Requested. Approve in System Settings, then restart the app that runs this server.")
        return
    _write_status()
    threading.Thread(target=_probe, daemon=True).start()
    mcp.run("stdio")


if __name__ == "__main__":
    main()
