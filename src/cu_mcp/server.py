"""MCP server: Claude-style computer use for any vision model (built for Grok in Cursor).

Hybrid perception: screenshots for the model's eyes, the accessibility tree for exact targets and labels,
on-device OCR for everything accessibility cannot see. Actions report what changed, so the model needs
fewer turns.

Run: `cursor-computer-use` (stdio MCP server) or `cursor-computer-use --check` (self test, no input events).
"""

from __future__ import annotations

import base64
import dataclasses
import functools
import json
import math
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import AppKit
import Quartz
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, ImageContent, TextContent
from pydantic import Field

from . import apps, ax, inputs, jev, ocr, recipes, safety, screen
from .effects import effects
from .workspace import Workspace

SETTLE_MIN = float(os.environ.get("CU_SETTLE", "0.12"))  # always wait this long after an action
SETTLE_MAX = float(os.environ.get("CU_SETTLE_MAX", "1.5"))  # then until the screen stops moving, at most this
SETTLE_QUIET = float(os.environ.get("CU_SETTLE_QUIET", "0.3"))  # "stopped moving" = no change for this long
SETTLE_LATE = float(os.environ.get("CU_SETTLE_LATE", "2.5"))  # after an accessibility press, watch for a late effect
BG_LATE = float(os.environ.get("CU_BACKGROUND_LATE", "1.0"))  # same, for every press in background mode
SETTLE_SEARCH = float(os.environ.get("CU_SETTLE_SEARCH", "1.2"))  # after typing in a search field, wait for results
AUTO_SCREENSHOT = os.environ.get("CU_AUTO_SCREENSHOT", "1") != "0"
DEFAULT_GRID = os.environ.get("CU_GRID", "0") == "1"
# Typing into these is refused: they run commands or hold secrets (clicks are still allowed).
PROTECTED_APPS = {a.strip().lower() for a in os.environ.get(
    "CU_PROTECTED_APPS", "Cursor,Terminal,iTerm2,Ghostty,Warp,Passwords,Keychain Access").split(",") if a.strip()}
GLOBAL_COMBOS = {"cmd+tab", "cmd+shift+tab"}  # allowed even with a protected app in front
# The chat the agent runs in: clicks, scrolls and fills are refused there too (they could press its own buttons).
HOST_APPS = {a.strip().lower() for a in os.environ.get("CU_HOST_APPS", "Cursor").split(",") if a.strip()}
NEW_TASK_GAP = 120.0  # seconds without calls before a screenshot: a new task, not one to pull the old app back for
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
- run_goal(goal, texts) hands a short, clearly labeled sub-goal to Jev, a fast decision model (~1 s per click
  instead of a model turn); it stops when done or unsure and returns a screenshot. Needs OPENROUTER_API_KEY.
- Background mode (the user's "Work in background" button): the app runs on a hidden display while the user
  uses the Mac. Clicks press elements, typing goes into the focused field; no pointer moves, no drag.
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
_ws = Workspace()  # background mode: the hidden display and the app working on it


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
            if fn.__name__ not in CLICK_TOOLS | LOOK_TOOLS:
                _state["click_streak"] = 0
                if fn.__name__ == "run_goal":
                    _state["jev_tipped"] = True
            try:
                _bg_mark()
                result = fn(*args, **kwargs)
                ok = True
                return result
            except ToolError:
                raise
            except Exception as exc:  # noqa: BLE001 - every failure goes back to the model
                raise ToolError(f"{type(exc).__name__}: {exc}") from exc
            finally:
                _state["last_call_end"] = time.monotonic()
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


def _guard(refocus: bool = True) -> None:
    """Refuse input when permissions are missing or the user hit the failsafe corner."""
    if _state["aborted"]:
        raise ToolError("ABORTED: the user stopped computer use (mouse moved to the top-left corner). "
                        "Stop now and ask the user how to proceed. Restart the MCP server to re-enable input.")
    if not ax.is_trusted():
        raise ToolError("Accessibility permission is missing, so mouse/keyboard input would be ignored. "
                        "Tell the user: System Settings > Privacy & Security > Accessibility (called 'Device Control and Data "
                        "Access' on macOS 27+) > enable Cursor, then reload this MCP server in Cursor.")
    if _ws.active:
        return  # the pointer and the front app are the user's; nothing the agent does touches them
    front_pid, front = ax.frontmost_app()
    if front.strip().lower() in safety.SYSTEM_PROMPT_APPS:
        raise ToolError(f"A system dialog or password prompt ({front}) is in front. Only the user may answer it: "
                        "stop and ask them.")
    if refocus:
        _refocus(front_pid, front)
    x, y = inputs.cursor_pos()
    last = _state["last_move"]
    if x <= 2 and y <= 2 and not (last and last[0] <= 2 and last[1] <= 2):
        _state["aborted"] = True
        effects.hide()
        _guard()


WORK_APP_TTL = 600.0  # seconds: a work app older than this is a finished task, not one to return to (Grok can
# think for over 3 minutes between two steps, and the user often clicks into the chat meanwhile)


def _note_work_app(pid: int, name: str, chosen: bool = False) -> None:
    """Remember the app the agent works in (any app it opened; otherwise a non-protected front app)."""
    if chosen or name.strip().lower() not in PROTECTED_APPS | safety.SYSTEM_PROMPT_APPS:
        _state["work_app"] = (pid, name, time.monotonic())


def _refocus(front_pid: int, front: str) -> None:
    """Cursor (or another protected app) came to the front in the middle of a task, usually because the user
    clicked into the chat: bring the work app back instead of acting on Cursor, where a click could hit its
    buttons."""
    work = _state.get("work_app")
    if (not work or work[0] == front_pid or front.strip().lower() not in PROTECTED_APPS
            or work[1].strip().lower() in PROTECTED_APPS or time.monotonic() - work[2] > WORK_APP_TTL
            or ax.pid_for_app(work[1]) is None):
        return
    _do_open_app(work[1])
    _state["note"] = f"(Brought {work[1]} back to the front: {front} had taken focus.)"


def _not_in_host(query: str | None = None) -> None:
    """The chat the agent runs in (Cursor) is the user's: a click there can press its own buttons (run, accept,
    settings), and what the model looks for is almost never in it. Refuse, and point at the likely intent."""
    if _ws.active:
        return
    name = ax.frontmost_app()[1]
    if name.strip().lower() not in HOST_APPS:
        return
    app = apps.match_installed(query) if query else None
    hint = f' An installed app is named "{app}": open_app("{app}").' if app else ""
    raise ToolError(f"{name} is in front, and it is the chat you work in: the agent does not click, scroll or fill "
                    f"there. Bring the app you need forward with open_app(name) first (it finds installed apps by "
                    f"name).{hint}")


def _check_typing(combo: str | None = None) -> None:
    """Keys and text never go to apps that can run commands or hold secrets."""
    name = _target()[1]
    if name.strip().lower() not in PROTECTED_APPS:
        return
    if combo is not None and _canonical_combo(combo) == "cmd+space":
        raise ToolError(f"Spotlight would open, but {name} stays the front app and typing into it is blocked. Open "
                        "apps with open_app(name), which finds installed apps by name, and links with open_url.")
    if combo is not None and _canonical_combo(combo) in GLOBAL_COMBOS:
        return
    raise ToolError(f"Typing into {name} is blocked: it is a protected app (it can run commands or holds secrets). "
                    f"Bring the target app forward with open_app first (not Spotlight: {name} stays the front app).")


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
    if _state.get("stale_coords"):
        raise ToolError("The user switched between background and normal mode since your last screenshot, so its "
                        "coordinates point elsewhere now. Take a screenshot first (or use click_text/click_element).")
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
    pid, name = _target(required=False)
    head = [note] if note else []
    if _state.get("note"):
        head.insert(0, _state.pop("note"))
    if _ws.active:
        head.append(f"Background mode: working in {name or '(no app yet: open_app one)'} on a hidden display while "
                    "the user uses the Mac. Clicks press elements, typing goes into the focused field; no pointer, "
                    "no drag.")
    else:
        _note_work_app(pid, name)
        mx, my = geo.to_image(*inputs.cursor_pos())
        head.append(f"Front app: {name}. Mouse at ({mx}, {my}).")
    if ax.is_trusted() and pid:
        head.append(f"Focus: {ax.focused_summary(pid)}")
    if not screen.has_screen_permission():
        head.append("WARNING: Screen Recording permission missing; windows may be invisible. "
                    "Tell the user to enable Cursor in System Settings > Privacy & Security > Screen Recording.")
    draw_grid = DEFAULT_GRID if grid is None else grid
    small: list = []
    if after_action and screen.changed_fraction(thumb, _state["last_thumb"]) < 0.001:
        # The thumbnail averages small changes away (a moved map pin, a new number): check the full image before
        # telling the model its action did nothing, or it repeats an action that worked.
        small = screen.fine_changes(im, _state.get("last_image"))
        if not small and not marks and not draw_grid:
            head.append("No visible change on screen since your last screenshot (the action may have had no "
                        "effect, or changed something invisible). Image omitted: it is the same as the last one.")
            return _text_result("\n".join(head))
    if small:
        head.append("Small change only, at " + ", ".join(f"({x0}, {y0})-({x1}, {y1})" for x0, y0, x1, y1 in small)
                    + ": look there.")
    elif after_action and _state["last_thumb"] is not None:
        frac = screen.changed_fraction(thumb, _state["last_thumb"])
        box = screen.changed_bbox(thumb, _state["last_thumb"]) if frac < 0.6 else None
        if box:
            k = geo.img_w / screen.THUMB[0]
            head.append(f"Changed area: ({box[0] * k:.0f}, {box[1] * k:.0f})-({box[2] * k:.0f}, {box[3] * k:.0f}).")
        elif frac >= 0.6:
            head.append("Most of the screen changed.")
    _state["last_thumb"] = thumb
    _state["last_image"] = im
    _state["stale_coords"] = False
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
    _bg_tidy()
    _settle_once()
    _wait_late(float(_state.pop("late", 0.0)))
    _bg_tidy()


def _wait_late(late: float) -> None:
    if not late:
        return
    # An accessibility press in a web app often just closes the menu at first, and the real effect (a new playlist,
    # a new page) shows up after a network round trip. Without this the screenshot shows the old screen and the
    # model repeats the action, creating a duplicate.
    base = screen.live_thumb()
    end = time.monotonic() + late
    while base is not None and time.monotonic() < end:
        time.sleep(0.1)
        if screen.changed_fraction(base, screen.live_thumb()) >= 0.002:
            _settle_once()
            return


def _settle_once() -> None:
    time.sleep(SETTLE_MIN)
    start = time.monotonic()
    prev = screen.live_thumb()
    if prev is None:
        time.sleep(0.4)
        return
    still_since = time.monotonic()
    while time.monotonic() - start < SETTLE_MAX:
        time.sleep(0.08)
        cur = screen.live_thumb()
        if screen.changed_fraction(prev, cur) >= 0.002:
            still_since = time.monotonic()  # web apps often change again after a short pause (menu closes, page loads)
        elif time.monotonic() - still_since >= SETTLE_QUIET:
            return
        prev = cur


def _act(tool: str, fn, /, **args) -> CallToolResult:
    """Run an action, remember it for recipes, return the settled screenshot."""
    msg = fn(**args)
    _record(tool, args)
    _jev_tip(tool)
    return _done(msg)


CLICK_TOOLS = {"click", "click_text", "click_element"}
LOOK_TOOLS = {"screenshot", "zoom", "read_text", "find", "ui_elements", "list_apps", "wait", "wait_for",
              "recipe_list", "clipboard"}


def _jev_tip(tool: str) -> None:
    """The model tends to make every click itself, one turn (~8 s) each, and never hands a run of labeled clicks
    to Jev. After two single clicks in a row, remind it once per task, in the result it reads anyway."""
    if tool not in CLICK_TOOLS:
        return
    _state["click_streak"] = _state.get("click_streak", 0) + 1
    if _state["click_streak"] < 2 or _state.get("jev_tipped") or _ws.active or not jev.api_key():
        return
    _state["jev_tipped"] = True
    tip = ("(Tip: when the next steps are clicks on labeled controls you can name, hand them to Jev in one call, "
           'e.g. run_goal("open Metadata and click Pick on map"): about 1 s per click instead of a turn each.)')
    _state["note"] = "\n".join(filter(None, [_state.get("note"), tip]))


def _done(note: str) -> CallToolResult:
    if not AUTO_SCREENSHOT:
        return _text_result("\n".join(filter(None, [_state.pop("note", ""), note])))
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
    note: str = ""  # added to the action's result (which row near= picked)


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
    pid, app = _target()
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


def _inside(p: tuple[float, float], f) -> bool:
    return f[0] <= p[0] <= f[0] + f[2] and f[1] <= p[1] <= f[1] + f[3]


def _same_row(t: Target, a: Target, rows: list) -> bool:
    """t and a sit in one list row: an accessibility row holds both, or they are level with each other."""
    if any(_inside(t.center, r) and _inside(a.center, r) for r in rows):
        return True
    return abs(t.center[1] - a.center[1]) <= max(12.0, 0.6 * max(t.frame[3], a.frame[3]))


def _focused_field() -> tuple[float, float, float, float] | None:
    return ax.focused_input(_target()[0])[0] if ax.is_trusted() else None


NEAR_MAX = 160.0  # points (horizontal distance counts 0.3): how far a match outside the near text's row may be


def _pick_near(targets: list[Target], query: str, app: str, near: str) -> Target:
    """Among good matches for query, the one in the same row as a match for `near` (the "Add" button of the row
    that shows a given song), else the closest. Every good match of `near` counts, because the same text also
    shows up where no such button is (a song already in the playlist above, the text typed into the search
    field), and the best-scored one is often there. Matches the screenshot does not show are left out, and when
    several rows qualify equally the choice goes back to the model instead of a guess."""
    if not targets:
        return _pick(targets, query, app)
    anchors, _ = _candidates(near, reuse=True)
    field = _focused_field()
    anchors = [a for a in anchors if not (field and _inside(a.center, field))]
    pool = [t for t in targets if t.score >= targets[0].score - 25][:20]
    if not anchors:
        listed = "\n".join(_with_row(t) for t in pool[:8])
        raise ToolError(f'Found "{query}" but nothing matching near="{near}" in {app}. The matches for "{query}" '
                        f"(click_element(id) one, or use a text its row shows):\n{listed}")
    anchors = [a for a in anchors if a.score >= anchors[0].score - 10][:20]
    rows = [el.frame for el in _state["elements"].values() if el.role == "AXRow"]

    def cost(pair):
        t, a = pair
        d = math.hypot((t.center[0] - a.center[0]) * 0.3, t.center[1] - a.center[1])
        if _same_row(t, a, rows):
            return 0, 0, -a.score, a.center[1], -t.score, d  # the best, top-most row that has the target
        return 1, round(d / 40), -a.score, a.center[1], -t.score, d

    shown: dict[int, bool] = {}

    def seen(t: Target) -> bool:
        """On the screenshot: not under a header or scrolled out of its list (checked only when needed)."""
        if id(t) not in shown:
            shown[id(t)] = not _cover(t)
        return shown[id(t)]

    pairs = sorted(((t, a) for t in pool for a in anchors), key=cost)
    same = [p for p in pairs if cost(p)[0] == 0]
    if not same:
        # No match in the row of the near text: only something right next to it counts, never a far button that
        # happens to be the closest (a sidebar "Add" while the result row sits below the visible part).
        best, anchor = pairs[0]
        if cost(pairs[0])[-1] > NEAR_MAX:
            listed = "\n".join(_with_row(t) for t in pool[:8])
            raise ToolError(f'No "{query}" in the row of "{near}" in {app} (that row may be scrolled out of view: '
                            f"scroll_to it), and the nearest one is far from it. The matches:\n{listed}")
        row = _row_text(best)
        return dataclasses.replace(best, note=f' in the row "{row}"' if row else f" (next to {anchor.desc})")
    # Rows the screenshot shows win over hidden ones (a row under a header); a hidden row is still taken over any
    # other row, and the click scrolls it into view.
    shown_same = [(t, a) for t, a in same if seen(t) and seen(a)]
    candidates = shown_same or same
    best, anchor = candidates[0]
    # Several rows show the near text, each with its own match: which one is meant is a guess, and a wrong guess
    # adds, deletes or opens the wrong item. Hand the choice back with each row's text.
    per_anchor: dict[int, Target] = {}
    for t, a in candidates:
        if a.score >= anchor.score - 3 and t.score > best.score - 6:
            per_anchor.setdefault(id(a), t)
    rivals = list({id(t): t for t in per_anchor.values()}.values())
    if len(rivals) > 1:
        listed = "\n".join(_with_row(t) for t in rivals[:8])
        raise ToolError(f'"{near}" shows in {len(rivals)} rows that each have a "{query}" in {app}. Pick one with '
                        f"click_element(id), or use near= with a text only that row shows:\n{listed}")
    row = _row_text(best)
    return dataclasses.replace(best, note=f' in the row "{row}"' if row else f" (next to {anchor.desc})")


def _with_row(t: Target) -> str:
    row = _row_text(t)
    return t.desc + (f' in the row "{row}"' if row else "")


def _row_text(t: Target) -> str:
    """The text of the list row holding t ("" when it is not in one): the smallest accessibility row, else the
    row-shaped container around it (web lists are often plain groups)."""
    rows = [el for el in _state["elements"].values() if el.role == "AXRow" and el.label and _inside(t.center, el.frame)]
    if rows:
        label = min(rows, key=lambda el: el.frame[2] * el.frame[3]).label
        return label if len(label) <= 120 else label[:119] + "…"
    el = _state["elements"].get(t.el_id) if t.el_id is not None and ax.is_trusted() else None
    return ax.row_context(el.ref, el.label) if el is not None and el.ref is not None else ""


def _pick(targets: list[Target], query: str, app: str) -> Target:
    if not targets:
        named = apps.match_installed(query)
        hint = (f' If you meant the app, open it: an installed app is named "{named}" (open_app("{named}")).'
                if named and named.lower() != app.strip().lower() else "")
        raise ToolError(f'Nothing matching "{query}" is visible in {app} (checked accessibility and OCR). '
                        "It may be off screen (scroll), in another app, or worded differently; look at the "
                        f"screenshot.{hint}")
    close = [t for t in targets if t.score > targets[0].score - 6][:8]
    if len(close) > 1:
        shown = [t for t in close if not _cover(t)]
        if len(shown) == 1:
            return shown[0]  # the others are hidden under something or scrolled out of view
        listed = "\n".join(_with_row(t) for t in (shown or close))
        raise ToolError(f'Several things match "{query}" in {app}. Pick one with click_element(id) (accessibility) '
                        f"or click(x, y) (OCR), or use a longer, more specific text:\n{listed}")
    return targets[0]


def _click_target(t: Target, button="left", count=1, modifiers=None) -> str:
    if t.el_id is not None:
        return _do_click_element(t.el_id, button, count, modifiers)
    px, py = t.center
    if _ws.active:
        return _bg_click(px, py, button, count, modifiers, t.desc)
    hit, hit_desc, hit_texts = ax.element_at(px, py) if ax.is_trusted() else (None, "", [])
    _not_our_button(px, py)
    _approve([t.desc, *hit_texts], f"Kliknięcie: {t.desc}")
    effects.point()
    inputs.click(px, py, button, int(count), inputs.parse_modifiers(modifiers))
    _moved(px, py)
    effects.click(px, py, int(count))
    _lag_if_menu("", hit_desc)
    return f"clicked {t.desc}" + (f" (hit {hit_desc})" if hit_desc else "")


# ---------------------------------------------------------------- actions (shared by tools and batch)

def _do_click(x, y, button="left", count=1, modifiers=None) -> str:
    _not_in_host()
    px, py = _to_points(x, y)
    if _ws.active:
        return _bg_click(px, py, button, count, modifiers, f"({x:.0f}, {y:.0f})")
    _not_our_button(px, py)
    _, hit_desc, hit_texts = ax.element_at(px, py) if ax.is_trusted() else (None, "", [])
    _approve(hit_texts, f"Kliknięcie w {hit_desc or f'punkt ({x:.0f}, {y:.0f})'}")
    effects.point()
    inputs.click(px, py, button, int(count), inputs.parse_modifiers(modifiers))
    _moved(px, py)
    effects.click(px, py, int(count))
    _lag_if_menu("", hit_desc)
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
    _not_in_host(el.label)
    _approve([el.label, *(t for t, _ in el.texts)], f"Kliknięcie: {what}")
    if press:
        if not ax.press(el.ref):
            raise ToolError(f"AXPress on [{id}] failed; try a normal click_element.")
        _state["late"] = SETTLE_LATE
        effects.click(*el.center)
        return f'pressed [{id}] {what} via accessibility'
    if _ws.active:
        if ax.is_text_input(el.ref) and ax.set_attr(el.ref, "AXFocused", True):
            ax.note_focus(_target()[0], el.ref)
            return f"focused [{id}] {what} (background)"
        if button == "left" and int(count) == 1 and not modifiers and ax.press(el.ref):
            _state["late"] = max(float(_state.get("late", 0.0)), BG_LATE)
            return f"pressed [{id}] {what} (background, accessibility)"
        fx, fy, fw, fh = ax.element_frame(el.ref) or el.frame
        return _bg_click(fx + fw / 2, fy + fh / 2, button, count, modifiers, what, approved=True)
    frame = ax.element_frame(el.ref)  # re-read: the element may have moved since the snapshot
    if frame is None:
        raise ToolError(f"Element [{id}] no longer exists (the UI changed). Take a new screenshot(marks=true).")
    geo = screen.geometry()
    px, py = frame[0] + frame[2] / 2, frame[1] + frame[3] / 2
    reached, hit_desc = _reach(geo, px, py, el.ref)
    note = ""
    if reached is False and ax.perform(el.ref, "AXScrollToVisible"):
        # Under a sticky header, past the edge of its list or off screen: bring it into view and click it for real.
        time.sleep(0.3)
        f = ax.element_frame(el.ref) or frame
        qx, qy = f[0] + f[2] / 2, f[1] + f[3] / 2
        if _reach(geo, qx, qy, el.ref)[0] is not False:
            px, py, reached, note = qx, qy, True, " (scrolled it into view first)"
    if reached is False:
        # Something else sits on top (a pop-up, a header, another window, or the UI changed since the snapshot).
        simple = button == "left" and int(count) == 1 and not modifiers
        if simple and ax.is_text_input(el.ref) and ax.set_attr(el.ref, "AXFocused", True):
            ax.note_focus(_target()[0], el.ref)
            return f"[{id}] {what} was covered by {hit_desc}; put the keyboard focus into it through accessibility"
        if simple and ax.press(el.ref):
            _state["late"] = SETTLE_LATE
            effects.click(px, py)
            return (f'[{id}] {what} was covered by {hit_desc}; pressed it through accessibility instead (its effect '
                    f"can lag: if the screenshot does not show it, wait_for the result instead of pressing again)")
        if not geo.contains_point(px, py):
            raise ToolError(f"Element [{id}] is off screen now; scroll it into view and refresh.")
        note = f" (warning: the point was covered by {hit_desc})"
    effects.point()
    inputs.click(px, py, button, int(count), inputs.parse_modifiers(modifiers))
    _moved(px, py)
    effects.click(px, py, int(count))
    _lag_if_menu(el.role, hit_desc)
    return f"clicked [{id}] {what}{note}"


def _reach(geo: screen.Geometry, px: float, py: float, ref, describe: bool = True) -> tuple[bool | None, str]:
    """ax.reach, plus the two things it cannot see: the edge of the screen and the user's background button."""
    if not geo.contains_point(px, py):
        return False, "the edge of the screen"
    if _over_our_button(px, py):
        return False, "the user's background button"
    return ax.reach(px, py, ref, describe)


def _lag_if_menu(role: str, hit_desc: str) -> None:
    """A menu command in a web app (Remove from this playlist, Delete) shows its effect after a network round trip,
    while the menu closes at once: watch for the late change, so the model does not see the old list and retry."""
    if role == "AXMenuItem" or hit_desc.startswith("menu item"):
        _state["late"] = max(float(_state.get("late", 0.0)), SETTLE_LATE)


def _do_click_text(text, button="left", count=1, modifiers=None, near=None) -> str:
    _not_in_host(text)
    targets, app = _candidates(text)
    target = _pick_near(targets, text, app, near) if near else _pick(targets, text, app)
    return _click_target(target, button, count, modifiers) + target.note


def _do_fill(text, target=None, submit=False, clear=True, near=None) -> str:
    _not_in_host(target)
    done = []
    if target:
        targets, app = _candidates(target, prefer_inputs=True)
        t = _pick_near(targets, target, app, near) if near else _pick(targets, target, app)
        done.append(_click_target(t) + t.note)
        time.sleep(0.15)
        if not _ws.active:
            done[-1] += _ensure_focus(t)
    done.append(_type_into_focus(text, clear))
    if submit and _ws.active:
        done.append(_bg_key("return"))
    elif submit:
        inputs.key("return")
        done.append("pressed Return")
    return "; ".join(done)


def _ensure_focus(t: Target) -> str:
    """After fill clicked its field: the keyboard focus must be in it before anything is typed. A click can land on
    a header that scrolled over the field, or next to it, and the text would then go somewhere else."""
    el = _state["elements"].get(t.el_id) if t.el_id is not None and ax.is_trusted() else None
    if el is None or not ax.is_text_input(el.ref):
        return ""  # not known to be a text field: nothing to check against
    pid = _target()[0]
    focus = ax.focused_element(pid)
    if focus is None or ax.within(focus, el.ref):  # None: this app does not report focus, so trust the click
        return ""
    if ax.set_attr(el.ref, "AXFocused", True):
        time.sleep(0.1)
        focus = ax.focused_element(pid)
        if focus is None or ax.within(focus, el.ref):
            return " (the click did not reach the field; focused it through accessibility)"
    raise ToolError(f"Clicked {t.desc}, but the keyboard focus did not go into it, so nothing was typed. Make the "
                    "field visible (scroll_to its label), then retry.")


def _type_into_focus(text, clear=True) -> str:
    """Type into the focused element; with clear, a focused text field is emptied first (cmd+a)."""
    if _ws.active:
        return _bg_type(text, replace=clear)
    _check_typing()
    cleared = clear and ax.focused_is_text_input(ax.frontmost_app()[0])
    if cleared:
        inputs.key("cmd+a")
    msg = _do_type(text)
    return msg.replace("typed", "replaced the field with", 1).replace("pasted", "replaced the field with", 1) \
        if cleared else msg


def _shown(text: str) -> Target | None:
    """The best match anywhere except the focused text field: the query just typed there is not a search result."""
    targets, _ = _candidates(text)
    field = _focused_field()
    return next((t for t in targets if t.score >= 60 and not (field and _inside(t.center, field))), None)


def _present(text: str) -> bool:
    return _shown(text) is not None


def _cover(t: Target) -> str:
    """What sits on top of an accessibility match ("" when nothing does): a list row scrolled under a player bar
    or past the edge of its scroll area is still in the accessibility tree."""
    el = _state["elements"].get(t.el_id) if t.el_id is not None and ax.is_trusted() else None
    if el is None:
        return ""  # OCR read it, so it shows
    reached, desc = _reach(screen.geometry(), *t.center, el.ref, describe=False)
    return desc if reached is False else ""


def _expect_results() -> None:
    """Live search fields fill in results after a network round trip: make the next settle wait for them."""
    if ax.is_trusted() and ax.focused_input(_target()[0])[1]:
        _state["late"] = max(float(_state.get("late", 0.0)), SETTLE_SEARCH)


def _do_wait_for(text, timeout=10.0, gone=False) -> str:
    timeout = max(0.5, min(float(timeout), 60.0))
    start = time.monotonic()
    while True:
        if _present(text) != bool(gone):
            return f'"{text}" {"is gone" if gone else "appeared"} after {time.monotonic() - start:.1f}s'
        if time.monotonic() - start >= timeout:
            raise TimeoutError(f'"{text}" {"is still visible" if gone else "did not appear"} within {timeout:g}s')
        time.sleep(0.3)


def _front_window_frame() -> tuple[float, float, float, float]:
    """The front app's frontmost window, clipped to the main display (the whole display if it has none)."""
    geo = screen.geometry()
    pid = _target()[0]
    for w in ax.windows():
        if w["pid"] == pid:
            x, y, wd, ht = w["frame"]
            x0, y0 = max(x, geo.origin_x), max(y, geo.origin_y)
            x1, y1 = min(x + wd, geo.origin_x + geo.width_pt), min(y + ht, geo.origin_y + geo.height_pt)
            if x1 - x0 > 40 and y1 - y0 > 40:
                return x0, y0, x1 - x0, y1 - y0
    return geo.origin_x, geo.origin_y, geo.width_pt, geo.height_pt


def _front_window_center() -> tuple[float, float]:
    x, y, w, h = _front_window_frame()
    return x + w / 2, y + h / 2


def _do_scroll_to(text, x=None, y=None, direction="down", max_scrolls=12) -> str:
    _not_in_host(text)
    px, py = _to_points(x, y) if x is not None and y is not None else _front_window_center()
    for i in range(int(max_scrolls) + 1):
        t = _shown(text)
        cover = _cover(t) if t is not None else ""
        if cover and ax.perform(_state["elements"][t.el_id].ref, "AXScrollToVisible"):
            _settle()
            t = _shown(text)
            if t is not None and not _cover(t):
                return f'"{text}" is visible (it was under {cover}; scrolled it into view through accessibility)'
        if t is not None:
            if cover:
                return (f'"{text}" is there but hidden ({cover} is in the way, and it could not be scrolled into '
                        "view); click_text and click_element still reach it")
            return f'"{text}" is visible' + (f" after {i} scroll(s) {direction}" if i else " (no scrolling needed)")
        if i == int(max_scrolls):
            break
        before = screen.live_thumb()
        if _ws.active:
            _bg_scroll(px, py, direction, 4)
        else:
            effects.point()
            inputs.scroll(px, py, direction, 4)
            _moved(px, py)
        _settle()
        if screen.changed_fraction(before, screen.live_thumb()) < 0.002:
            raise TimeoutError(f'reached the end of the scrollable area without finding "{text}"')
    raise TimeoutError(f'"{text}" not found after {max_scrolls} scrolls {direction}')


def _do_move(x, y) -> str:
    _no_pointer_in_background("move_mouse (hover)")
    _not_in_host()
    px, py = _to_points(x, y)
    effects.point()
    inputs.glide(px, py)
    _moved(px, py)
    return f"mouse moved to ({x:.0f}, {y:.0f})"


def _do_drag(from_x, from_y, to_x, to_y, button="left", modifiers=None) -> str:
    _no_pointer_in_background("drag")
    _not_in_host()
    p0, p1 = _to_points(from_x, from_y), _to_points(to_x, to_y)
    effects.point()
    effects.click(*p0)
    inputs.drag(*p0, *p1, button=button, mods=inputs.parse_modifiers(modifiers))
    _moved(*p1)
    effects.click(*p1)
    return f"dragged ({from_x:.0f}, {from_y:.0f}) -> ({to_x:.0f}, {to_y:.0f})"


def _do_scroll(x, y, direction, amount=5, modifiers=None) -> str:
    _not_in_host()
    px, py = _to_points(x, y)
    if _ws.active:
        return _bg_scroll(px, py, direction, int(amount))
    effects.point()
    inputs.scroll(px, py, direction, int(amount), inputs.parse_modifiers(modifiers))
    _moved(px, py)
    effects.pulse()
    return f"scrolled {direction} {amount} at ({x:.0f}, {y:.0f})"


def _do_type(text, method="auto") -> str:
    if _ws.active:
        return _bg_type(text)
    _check_typing()
    effects.pulse()
    if method == "paste" or (method == "auto" and len(text) > 200):
        restored = inputs.paste_text(text)
        _expect_results()
        return f"pasted {len(text)} chars" + ("" if restored else " (clipboard left holding the text)")
    inputs.type_text(text)
    _expect_results()
    return f"typed {len(text)} chars"


def _do_key(keys, repeat=1) -> str:
    if _ws.active:
        return _bg_key(keys, int(repeat))
    _check_typing(keys)
    effects.pulse()
    inputs.key(keys, int(repeat))
    if _canonical_combo(keys) in ("return", "enter", "keypadenter"):
        _expect_results()
    return f"pressed {keys}" + (f" x{repeat}" if int(repeat) > 1 else "")


def _do_wait(seconds=1.0) -> str:
    seconds = max(0.0, min(float(seconds), 30.0))
    time.sleep(seconds)
    return f"waited {seconds:g}s"


def _do_open_app(name) -> str:
    if _ws.active:
        return _bg_open_app(name)
    args = ["-b", name] if name.count(".") >= 2 and " " not in name else ["-a", name]
    proc = subprocess.run(["/usr/bin/open", *args], capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        close = apps.similar_installed(name)
        raise ToolError(f"Could not open {name!r}: {proc.stderr.strip()}"
                        + (f" Installed apps with similar names: {', '.join(close)}." if close else ""))
    deadline = time.time() + 8
    front = ax.frontmost_app()[1]
    while time.time() < deadline and name.lower() not in front.lower():
        time.sleep(0.2)
        front = ax.frontmost_app()[1]
    _note_work_app(*ax.frontmost_app(), chosen=True)
    _state["late"] = max(float(_state.get("late", 0.0)), SETTLE_LATE)  # its window may paint a moment later
    return f"opened {name}; front app is now {front}"


def _do_open_url(url, app=None) -> str:
    url, scheme = _split_url(url)
    if scheme in BLOCKED_SCHEMES:
        raise ToolError(f"{scheme}: links are blocked here (they call, message, run code or open local files). "
                        "Ask the user to do this themselves.")
    if _ws.active:
        return _bg_open_url(url, scheme, app)
    args = ["-a", app, url] if app else [url]
    proc = subprocess.run(["/usr/bin/open", *args], capture_output=True, text=True, timeout=20)
    if proc.returncode != 0 and ("-10814" in proc.stderr or "No application knows" in proc.stderr):
        raise ToolError(f"No app on this Mac opens {scheme}: links (e.g. Spotify installed as a web app from Safari "
                        "has none). Do it in the app's own UI instead (its search field, buttons).")
    if proc.returncode != 0:
        raise ToolError(f"Could not open {url!r}: {proc.stderr.strip()}")
    time.sleep(0.4)
    return f"opened {url}" + (f" in {app}" if app else "") + f"; front app is now {ax.frontmost_app()[1]}"


def _do_menu(path, app=None) -> tuple[bool, str]:
    """(pressed, text): pressed is False when the path named a menu and its items were listed."""
    if not app:
        _not_in_host()
    pid = _app_pid(app)
    if app and not _ws.active and ax.frontmost_app()[0] != pid:
        _do_open_app(app)  # menus of background apps cannot open
    parts = [p.strip() for p in (path if isinstance(path, list) else path.replace("→", ">").split(">")) if p.strip()]
    if parts:
        _approve([parts[-1]], f"Polecenie menu: {' > '.join(parts)}")
    escape = (lambda n: inputs.key_to_pid(pid, "escape", n)) if _ws.active else None
    pressed, text = ax.menu_select(pid, parts, escape=escape)
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
        return _target()[0]
    pid = ax.pid_for_app(app)
    if pid is None:
        raise ToolError(f"App {app!r} is not running. Use open_app first.")
    return pid


# ---------------------------------------------------------------- background mode (hidden display)

def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _target(required: bool = True) -> tuple[int, str]:
    """The app the agent acts on: the front app, or in background mode the app on the hidden display."""
    if not _ws.active:
        return ax.frontmost_app()
    if _ws.app is not None and _alive(_ws.app[0]):
        return _ws.app
    if required:
        raise ToolError("Background mode has no app yet: open_app the app to work in (it opens on the hidden "
                        "display, out of the user's way).")
    return 0, ""


def _activate(pid: int) -> None:
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    if app is None:
        return
    app.activateWithOptions_(0)
    time.sleep(0.15)
    if ax.frontmost_app()[0] != pid and app.bundleURL() is not None:  # macOS may refuse a background activation
        subprocess.run(["/usr/bin/open", "-a", app.bundleURL().path()], capture_output=True, timeout=10)


def _our_windows() -> list[tuple[float, float, float, float]]:
    """Frames of the overlay's button panel (not the full-screen, click-through glow window)."""
    pid = effects.pid
    if pid is None:
        return []
    out = []
    opts = Quartz.kCGWindowListOptionOnScreenOnly
    for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
        b = w.get("kCGWindowBounds") or {}
        if w.get("kCGWindowOwnerPID") == pid and 0 < b.get("Width", 0) < 600 and 0 < b.get("Height", 0) < 200:
            out.append((b["X"], b["Y"], b["Width"], b["Height"]))
    return out


def _over_our_button(px: float, py: float) -> bool:
    return any(_inside((px, py), f) for f in _our_windows())


def _not_our_button(px: float, py: float) -> None:
    """The user's background button is left out of screenshots; a click that would land on it is refused."""
    if _over_our_button(px, py):
        raise ToolError("That point is covered by the user's 'Work in background' button (bottom right, not in "
                        "screenshots). Use click_text or click_element for the target instead.")


def _no_pointer_in_background(what: str) -> None:
    if _ws.active:
        raise ToolError(f"{what} needs the real pointer, which belongs to the user in background mode. Do the step "
                        "another way (click_text, menu, key), or ask the user to click 'Show' on the button at the "
                        "bottom right of the screen and then retry.")


def _bg_mark() -> None:
    """Before each tool call in background mode: who has the user's focus, and which work-app windows exist."""
    if _ws.active:
        before = _ws.windows_outside(_ws.app[0]) if _ws.app else set()
        _state["bg_pre"] = (ax.frontmost_app()[0], before)


def _bg_tidy() -> None:
    """Keep the user's screen theirs: windows the work app opened during the action go to the hidden display,
    and if the app took the focus, the user's app gets it back."""
    if not _ws.active or _ws.app is None or not _state.get("bg_pre"):
        return
    user_pid, before = _state["bg_pre"]
    pid, name = _ws.app
    new = _ws.windows_outside(pid) - before
    if new:
        _ws.adopt(pid, name, only=new)
    if user_pid != pid and ax.frontmost_app()[0] == pid:
        _activate(user_pid)


def _bg_click(px: float, py: float, button="left", count=1, modifiers=None, what: str = "",
              approved: bool = False) -> str:
    """A click without the pointer: press the control under the point through accessibility; otherwise post
    the click to the work app alone (the pointer stays with the user)."""
    pid, app = _target()
    node, desc, texts = ax.element_at(px, py)
    owner = ax.pid_of(node) if node is not None else None
    if owner is not None and owner != pid:
        raise ToolError(f"{what or 'That point'} is not inside {app} on the hidden display ({desc} is there).")
    label = desc or what or "the point"
    if not approved:
        _approve([what, *texts], f"Kliknięcie: {label}")
    if button == "left" and int(count) == 1 and not modifiers and node is not None:
        if ax.is_text_input(node) and ax.set_attr(node, "AXFocused", True):
            ax.note_focus(pid, node)
            return f"focused {label} (background)"
        target = ax.pressable(node)
        if target is not None and ax.press(target):
            _state["late"] = max(float(_state.get("late", 0.0)), BG_LATE)
            return f"pressed {label} (background, accessibility)"
    inputs.click_to_pid(pid, px, py, button, int(count), inputs.parse_modifiers(modifiers))
    return (f"{button} click on {label} sent to {app} only (background: an inactive app may ignore it; check the "
            "screenshot)")


def _bg_key(combo: str, repeat: int = 1) -> str:
    """A key without the keyboard: shortcuts run the matching menu command, Return/Escape use the focused
    element's confirm/cancel action, anything else is posted to the work app alone."""
    pid, app = _target()
    _check_typing(combo)
    mods, _ = inputs.parse_combo(combo)
    name = _canonical_combo(combo).split("+")[-1]
    field = ax.focused_element(pid)
    if name == "a" and set(mods) == {"cmd"} and field is not None and ax.is_text_input(field):
        how = "through accessibility" if _bg_select_all(pid, field) else "with caret keys (check the screenshot)"
        return f"cmd+a = selected all text in the focused field {how} (background)"
    if any(m in mods for m in ("cmd", "ctrl", "alt")):
        hit = ax.menu_item_for_shortcut(pid, mods, name)
        if hit is not None:
            item, path = hit
            _approve([path.rsplit(" > ", 1)[-1]], f"Polecenie menu: {path}")
            for _ in range(max(1, int(repeat))):
                if not ax.perform(item, "AXPress"):
                    break
            return f"{combo} = menu {path}, pressed through accessibility (background)"
    if name in ("return", "keypadenter") and ax.perform(field, "AXConfirm"):
        _expect_results()
        return "pressed Return (background, accessibility)"
    if name == "escape" and ax.perform(field, "AXCancel"):
        return "pressed Escape (background, accessibility)"
    inputs.key_to_pid(pid, combo, max(1, int(repeat)))
    if name in ("return", "keypadenter"):
        _expect_results()
    return f"sent {combo} to {app} only (background: check the screenshot that it worked)"


def _bg_type(text: str, replace: bool = False) -> str:
    """Typing without the keyboard: insert into the work app's focused text field through accessibility (the
    user's own typing is unaffected). A trailing newline presses Return."""
    pid, app = _target()
    _check_typing()
    body, submit = (text[:-1], True) if text.endswith("\n") else (text, False)
    field = ax.focused_element(pid)
    if field is not None and ax.is_text_input(field):
        msg = ("replaced the field with" if replace else "typed") + f" {len(body)} chars (background)"
        if body and not ax.insert_text(field, body, replace):
            if replace and ax.value_of(field):
                _bg_select_all(pid, field)
            inputs.type_to_pid(pid, body)
            value = ax.value_of(field)
            if body not in value:
                raise ToolError(f"{app} does not take typed text in the background. Ask the user to click 'Show' on "
                                "the button at the bottom right of the screen, then type normally.")
            if replace and value != body:
                msg = (f'typed {len(body)} chars (background), but the field now holds "{value[:80]}": the '
                       "old text was not replaced")
    else:
        if replace:
            _bg_select_all(pid, None)
        inputs.type_to_pid(pid, body)
        msg = (f"sent {len(body)} chars to {app} only (background: no text field has focus there; check the "
               "screenshot, or click the field first)")
    _expect_results()
    if submit:
        msg += "; " + _bg_key("return")
    return msg


def _bg_select_all(pid: int, field) -> bool:
    """Select the focused field's text before it is replaced. cmd+a is a menu shortcut, and a menu shortcut posted to
    an app in the background does nothing (Spotify's Safari web app), while caret moves posted to it work.
    True when accessibility confirmed the selection."""
    if field is not None and ax.select_all(field):
        return True
    for combo in ("cmd+up", "cmd+left", "shift+cmd+down", "shift+cmd+right"):  # start, then select to the end
        inputs.key_to_pid(pid, combo)
    return False


def _bg_scroll(px: float, py: float, direction: str, amount: int = 5) -> str:
    pid, app = _target()
    if ax.scroll_by(px, py, direction, amount):
        return f"scrolled {direction} {amount} (background, scroll bar)"
    inputs.scroll_to_pid(pid, px, py, direction, amount)
    return f"scroll {direction} {amount} sent to {app} only (background: check the screenshot)"


def _running(name: str) -> tuple[int, str] | None:
    if name.count(".") >= 2 and " " not in name:  # bundle id
        apps_ = AppKit.NSRunningApplication.runningApplicationsWithBundleIdentifier_(name)
        app = apps_[0] if apps_ else None
        return (int(app.processIdentifier()), str(app.localizedName())) if app is not None else None
    pid = ax.pid_for_app(name)
    return (pid, ax.app_name(pid)) if pid is not None else None


def _bg_open_app(name: str) -> str:
    """Launch without activating (open -g) and move the app's windows onto the hidden display."""
    args = ["-g", "-b", name] if name.count(".") >= 2 and " " not in name else ["-g", "-a", name]
    proc = subprocess.run(["/usr/bin/open", *args], capture_output=True, text=True, timeout=20)
    if proc.returncode != 0:
        raise ToolError(f"Could not open {name!r}: {proc.stderr.strip()}")
    app = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and (app is None or not ax.window_refs(app[0])):
        time.sleep(0.25)
        app = app or _running(name)
    if app is None:
        raise ToolError(f"{name} did not start.")
    moved = _ws.adopt(*app)
    if moved:
        _state["late"] = max(float(_state.get("late", 0.0)), SETTLE_LATE)  # a moved window paints a moment later
    pre = _state.get("bg_pre")
    if pre and pre[0] != app[0] and ax.frontmost_app()[0] == app[0]:
        _activate(pre[0])  # it grabbed the focus on launch: give it back to the user
    _state["last_thumb"] = None
    return (f"opened {app[1]} on the hidden display ({moved} window(s) moved there)" if moved
            else f"{app[1]} is running on the hidden display but shows no window yet")


def _bg_open_url(url: str, scheme: str, app: str | None) -> str:
    """Open in the background and move the window that appears onto the hidden display."""
    before = {w["number"] for w in ax.windows()}
    args = ["-g", "-a", app, url] if app else ["-g", url]
    proc = subprocess.run(["/usr/bin/open", *args], capture_output=True, text=True, timeout=20)
    if proc.returncode != 0 and ("-10814" in proc.stderr or "No application knows" in proc.stderr):
        raise ToolError(f"No app on this Mac opens {scheme}: links. Do it in the app's own UI instead.")
    if proc.returncode != 0:
        raise ToolError(f"Could not open {url!r}: {proc.stderr.strip()}")
    new: list[dict] = []
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline and not new:
        time.sleep(0.25)
        new = [w for w in ax.windows() if w["number"] not in before and w["pid"] != effects.pid]
    if new:
        pid, name = new[0]["pid"], new[0]["app"]
        _ws.adopt(pid, name, only={w["number"] for w in new if w["pid"] == pid})
        return f"opened {url} in {name} on the hidden display"
    if _ws.app and (not app or app.lower() in _ws.app[1].lower()):
        return f"opened {url} (in {_ws.app[1]} on the hidden display, no new window)"
    raise ToolError(f"Opened {url}, but no new window appeared: it probably went into a window on the user's screen. "
                    "In background mode open_app the app first, then open links inside it.")


def _enter_background(app: tuple[int, str] | None = None) -> str:
    """Create the hidden display, move the work app there and give the main screen back to the user."""
    if _ws.active:
        return "already in background mode"
    if app is None:
        work = _state.get("work_app")
        if work and _alive(work[0]) and work[1].strip().lower() not in PROTECTED_APPS:
            app = (work[0], work[1])
        else:
            front = ax.frontmost_app()
            if front[1].strip().lower() not in PROTECTED_APPS | safety.SYSTEM_PROMPT_APPS:
                app = front
    _ws.start()
    moved = _ws.adopt(*app) if app else 0
    effects.set_mode(True)
    _state["last_thumb"] = None
    _state["stale_coords"] = True
    if app and ax.frontmost_app()[0] == app[0]:
        mine = {app[0], effects.pid}
        nxt = next((w for w in ax.windows() if w["pid"] not in mine and not _ws.inside(w["frame"])), None)
        if nxt is not None:
            _activate(nxt["pid"])
    _state["note"] = (f"(The user switched to background mode: {app[1] if app else 'no app yet'} now runs on a "
                      "hidden display while they use the Mac. Keep working: clicks press elements through "
                      "accessibility, typing goes into the focused field, the pointer and drag are unavailable.)")
    return f"background mode on: {app[1] if app else 'no app yet'} ({moved} window(s)) is on the hidden display"


def _exit_background() -> str:
    """Bring the windows back where they were, remove the hidden display and show the work app."""
    app = _ws.stop()
    effects.set_mode(False)
    _state["last_thumb"] = None
    _state["stale_coords"] = True
    if app and _alive(app[0]):
        _activate(app[0])
        _note_work_app(*app, chosen=True)
    _state["note"] = ("(The user brought the work back to the front: the pointer and keyboard work normally again "
                      "and the user sees the screen.)")
    return "background mode off: " + (f"{app[1]} is back on the main display" if app else "nothing to bring back")


def _toggle_background() -> None:
    """The user clicked the button next to the Dock (runs on the overlay reader's thread)."""
    with _lock:
        try:
            _exit_background() if _ws.active else _enter_background()
        except Exception as exc:  # noqa: BLE001 - shown on the button instead
            effects.set_mode(_ws.active, note=f"Background mode failed: {exc}"[:90])


# ---------------------------------------------------------------- Jev: fast executor for short sub-goals

def _jev_screen(texts: list[str], typed: set[str]) -> tuple[str, str, list[jev.Option], list[str]]:
    """(app, focus, options, visible text) of the front app: accessibility controls, plus OCR when they are few."""
    geo = screen.geometry()
    pid, app = _target()
    elements, _ = ax.snapshot(pid, (geo.origin_x, geo.origin_y, geo.width_pt, geo.height_pt),
                              limit=jev.MAX_OPTIONS, include_text=True, text_limit=jev.MAX_TEXT_LINES)
    _state["elements"] = {i: el for i, el in enumerate(elements, 1)}
    _state["elements_pid"] = pid
    lines = None
    if sum(1 for el in elements if el.role != "AXStaticText") < 8:
        lines = [(b.text, (b.frame[0] + b.frame[2] / 2, b.frame[1] + b.frame[3] / 2))
                 for b in ocr.recognize(_front_window_frame())][:60]
    return (app, ax.focused_summary(pid), jev.build_options(_state["elements"], texts, typed, lines),
            jev.visible_text(_state["elements"], lines))


def _jev_execute(o: jev.Option) -> str:
    """Run the option Jev picked through the normal actions (approvals, hit-tests and recording included)."""
    if o.kind == "click":
        msg = _do_click_element(o.el_id)
        _record("click_element", {"id": o.el_id})
        return msg
    if o.kind == "fill":
        name = jev._name(_state["elements"][o.el_id])
        msg = _do_click_element(o.el_id)
        time.sleep(0.15)
        msg += "; " + _type_into_focus(o.text)
        if name:
            _recorder.record("fill", text=o.text, target=name)
        else:
            _record("click_element", {"id": o.el_id})
            _recorder.record("type_text", text=o.text)
        return msg
    if o.kind == "ocr":
        cx, cy = screen.geometry().to_image(*o.center)
        msg = _click_target(Target(100, o.center, (*o.center, 0, 0), f'text "{o.text}" at ({cx}, {cy}) [OCR]'))
        _recorder.record("click_text", text=o.text)
        return msg
    if o.kind == "key":
        msg = _do_key(o.keys)
        _record("key", {"keys": o.keys})
        return msg
    if o.kind == "scroll":
        cx, cy = screen.geometry().to_image(*_front_window_center())
        msg = _do_scroll(cx, cy, o.keys, 5)
        _record("scroll", {"x": cx, "y": cy, "direction": o.keys, "amount": 5})
        return msg
    raise ValueError(f"cannot execute {o.kind}")


def _run_goal(goal, texts=None, app=None, max_steps=8) -> tuple[bool, str]:
    """Let Jev click through a short sub-goal. (finished, log): it hands back on done, stuck, low confidence,
    two actions in a row without a visible change, an error, or max_steps."""
    if not jev.api_key():
        raise ToolError("Jev is not set up (no OPENROUTER_API_KEY in the env of the computer-use server in "
                        "~/.cursor/mcp.json). Do this step yourself with the other tools.")
    texts = [texts] if isinstance(texts, str) else [str(t) for t in texts or []]
    max_steps = max(1, min(int(max_steps), 25))
    if app and app.lower() not in _target(required=False)[1].lower():
        _do_open_app(app)
        _settle()
    front = _target()[1]
    if front.strip().lower() in PROTECTED_APPS:
        raise ToolError(f"run_goal does not drive {front} (a protected app). Bring the target app forward first "
                        "(app= or open_app).")
    log: list[str] = []
    history: list[str] = []
    typed: set[str] = set()
    cost, t0, still, last, repeats = 0.0, time.monotonic(), 0, None, 0
    finished, status = False, f"stopped after {max_steps} steps without Jev confirming the goal"
    for step in range(1, max_steps + 1):
        try:
            _guard()
            app_name, focus, options, seen = _jev_screen(texts, typed)
            d = jev.decide(jev.make_state(goal, app_name, focus, texts, history, seen), options)
        except ToolError as exc:
            status = str(exc)
            break
        except Exception as exc:  # noqa: BLE001 - the planner continues from the log
            status = f"Jev failed: {exc}"
            break
        cost += d.cost
        o, tag = d.option, f"(p={d.prob:.2f}, {d.ms} ms)"
        if o.kind == "done":
            finished, status = True, f"Jev says the goal is done {tag}"
            break
        if o.kind == "stuck":
            status = f"Jev is stuck {tag}: no listed action helps, or the next step is for you or the user"
            break
        if d.prob < jev.MIN_PROB:
            alts = "; ".join(f"{a.desc} (p={p:.2f})" for a, p in d.ranked[:3])
            status = f"Jev is unsure (best p={d.prob:.2f} < {jev.MIN_PROB:g}). Its top options: {alts}"
            break
        repeats = repeats + 1 if o.desc == last else 0
        last = o.desc
        if repeats >= 2:
            status = f"Jev picked the same action 3 times in a row ({o.desc})"
            break
        before = screen.live_thumb()
        try:
            msg = _jev_execute(o)
        except Exception as exc:  # noqa: BLE001 - DECLINED, ABORTED and blocked typing end the run
            log.append(f"{step}. FAILED: {o.desc}: {exc}")
            status = str(exc) if isinstance(exc, ToolError) else f"{type(exc).__name__}: {exc}"
            break
        if o.kind == "fill":
            typed.add(o.text)
        _settle()
        changed = before is None or screen.changed_fraction(before, screen.live_thumb()) >= 0.002
        still = 0 if changed else still + 1
        history.append(o.desc + ("" if changed else " (nothing visible changed)"))
        log.append(f"{step}. {msg} {tag}" + ("" if changed else " - no visible change"))
        if still >= 2:
            status = "two actions in a row changed nothing on screen"
            break
    text = (f'run_goal "{goal}": {status}.\n' + ("\n".join(log) or "(no actions taken)")
            + f"\n{len(log)} action(s) in {time.monotonic() - t0:.1f}s, Jev cost ${cost:.5f}."
            + ("" if finished else "\nCheck the screenshot and continue the task yourself."))
    return finished, text


def _batch_run_goal(goal, texts=None, app=None, max_steps=8) -> str:
    finished, text = _run_goal(goal, texts, app, max_steps)
    if not finished:
        raise RuntimeError(text)
    return text


_BATCH = {
    "click": _do_click, "click_text": _do_click_text, "click_element": _do_click_element, "fill": _do_fill,
    "move_mouse": _do_move, "drag": _do_drag, "scroll": _do_scroll, "type_text": _do_type, "key": _do_key,
    "wait": _do_wait, "wait_for": _do_wait_for, "scroll_to": _do_scroll_to, "open_app": _do_open_app, "open_url": _do_open_url,
    "menu": lambda path, app=None: _do_menu(path, app)[1],
    "app_action": _do_app_action, "run_goal": _batch_run_goal,
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
    if time.monotonic() - _state.get("last_call_end", 0.0) > NEW_TASK_GAP:
        _state.pop("work_app", None)  # a new task starts: do not pull the previous task's app back to the front
        _state.update(click_streak=0, jev_tipped=False)
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
    return f'Matches for "{text}" in {app} (best first):\n' + "\n".join(_with_row(t) for t in targets[:15])


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
    _guard(refocus=False)
    return _act("open_app", _do_open_app, name=name)


@_tool
def open_url(
    url: Annotated[str, Field(description='Web link or app deep link, e.g. "https://github.com", '
                                          '"spotify:search:take on me", "x-apple.systempreferences:"')],
    app: Annotated[str | None, Field(description='Open with this app, e.g. "Safari" or "Google Chrome"')] = None,
) -> CallToolResult:
    """Open a URL or an app deep link directly (often skips several clicks)."""
    _guard(refocus=False)
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
                                          '"?" lists every action')] = "?",
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
def run_goal(
    goal: Annotated[str, Field(description='A short sub-goal in one app, in words, e.g. "open the Privacy tab and '
                                           'turn on Block all cookies"')],
    texts: Annotated[list[str] | str | None, Field(description='Exact text to type on the way, e.g. ["Take On Me"]; '
                                                               "Jev never invents text")] = None,
    app: Annotated[str | None, Field(description="App to bring forward first")] = None,
    max_steps: Annotated[int, Field(description="1 to 25")] = 8,
) -> CallToolResult:
    """Hand a short sub-goal to Jev, a fast decision model (TypeSafe via OpenRouter), which clicks through the
    front app on its own: about 1 s per step instead of a full model turn per click. It chooses among the app's
    accessibility controls (clicks, typing your `texts`, Return, Escape, scrolling) and stops when the goal is
    done, when it is unsure, or when nothing changes; you get its log and a screenshot and continue from there.
    Best for several clicks through clearly labeled UI (settings, menus, lists, forms). Not for unlabeled icons,
    canvases, or decisions the goal does not spell out."""
    _guard()
    return _done(_run_goal(goal, texts, app, max_steps)[1])


@_tool
def background(on: Annotated[bool, Field(description="true: move to the hidden display; false: bring back")] = True
               ) -> CallToolResult:
    """Only when the user asks: on=true moves the app you work in to a hidden display and keeps working there
    while the user uses the Mac (the same as the user's "Work in background" button next to the Dock);
    on=false brings it back to the main display. Never switch on your own."""
    if not ax.is_trusted():
        _guard()
    return _done(_enter_background() if on else _exit_background())

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
                    "wait_for, open_app, open_url, menu, run_goal, app_action (as {\"action\": \"app_action\", "
                    "\"app\": \"notes\", \"do\": \"create_note\", \"args\": {...}}). Example: "
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
            _guard(refocus=name not in ("open_app", "open_url"))
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
    print(f"Jev (run_goal): {'key set, model ' + jev.MODEL if jev.api_key() else 'off (no OPENROUTER_API_KEY; optional)'}")
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
                    "accessibility": ax.is_trusted(), "screen_recording": screen.has_screen_permission(),
                    "jev": bool(os.environ.get("OPENROUTER_API_KEY")), "jev_model": jev.MODEL}
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


def check_jev() -> int:
    """One real Jev decision on a made-up screen (costs a fraction of a cent): key, model and latency."""
    if not jev.api_key():
        print(f"No OPENROUTER_API_KEY in the environment or in {jev.MCP_CONFIG} (mcpServers > computer-use > env).")
        return 1
    opts = [jev.Option("click_1", 'click tab "General"', "click"), jev.Option("click_2", 'click tab "Privacy"', "click"),
            jev.Option("click_3", 'click button "Help"', "click"), *jev.FIXED]
    state = jev.make_state("open the Privacy tab", "Settings", "nothing focused", [], [], ["General", "Privacy"])
    try:
        for _ in range(3):
            d = jev.decide(state, opts)
            print(f"{jev.MODEL}: {d.option.desc} (p={d.prob:.2f}) in {d.ms} ms, cost ${d.cost:.6f}")
    except jev.JevError as exc:
        print(f"Jev FAILED: {exc}")
        return 1
    return 0 if d.option.key == "click_2" else 1


def check_background(app_name: str | None = None) -> int:
    """Background mode self test. Run it from Cursor's terminal (Cursor's permissions are the ones that count):
    moves an app (default: a scratch TextEdit document) to the hidden display, types into it through
    accessibility and checks the result there, without the pointer or the keyboard. Prints each step and
    writes background_check.json next to status.json."""
    results: list[dict] = []

    def step(name: str, ok, detail: str = "") -> bool:
        results.append({"step": name, "ok": bool(ok), "detail": detail})
        print(f"{'OK  ' if ok else 'FAIL'} {name}" + (f": {detail}" if detail else ""))
        return bool(ok)

    if not step("Accessibility permission", ax.is_trusted(), "run this from Cursor's terminal"):
        return 1
    scratch = app_name is None
    if scratch and ax.pid_for_app("TextEdit"):
        print("TextEdit is open: close it first, or test another app: --check-background Spotify")
        return 1
    user_front = ax.frontmost_app()
    path, app = None, None
    try:
        _ws.start()
        step("hidden display created", True, "x={:.0f} y={:.0f} {:.0f}x{:.0f}".format(*_ws.bounds()))
        if scratch:
            fd, path = tempfile.mkstemp(suffix=".txt", prefix="cu_background_check_")
            os.close(fd)
            subprocess.run(["/usr/bin/open", "-g", "-a", "TextEdit", path], capture_output=True, timeout=20)
        else:
            subprocess.run(["/usr/bin/open", "-g", "-a", app_name], capture_output=True, timeout=20)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and (app is None or not ax.window_refs(app[0])):
            time.sleep(0.25)
            app = app or _running(app_name or "TextEdit")
        if not step("app running with a window", app is not None and ax.window_refs(app[0]), str(app)):
            return 1
        moved = _ws.adopt(*app)
        frames = [f for _, f, _ in ax.window_refs(app[0])]
        step("windows moved to the hidden display", moved and all(_ws.inside(f) for f in frames), f"{moved} moved")
        geo = screen.geometry()
        elements, _ = ax.snapshot(app[0], (geo.origin_x, geo.origin_y, geo.width_pt, geo.height_pt), limit=300)
        field = next((e for e in elements if e.role in ax.TEXT_ROLES or e.subrole == "AXSearchField"), None)
        if step("text field found through accessibility", field is not None, field.label if field else ""):
            before = ax.value_of(field.ref)
            ax.set_attr(field.ref, "AXFocused", True)
            ax.note_focus(app[0], field.ref)
            step("the focused field is known", ax.focused_element(app[0]) is not None)
            typed = ax.insert_text(field.ref, "background check", replace=True)
            step("typed into it without the keyboard", typed, repr(ax.value_of(field.ref))[:60])
            step("select all through accessibility", ax.select_all(field.ref))
            time.sleep(0.8)
            seen = any("background check" in b.text.lower() for b in ocr.recognize())
            step("the hidden display shows it (OCR)", seen)
            if not scratch:
                ax.set_attr(field.ref, "AXValue", before)
        hit = ax.menu_item_for_shortcut(app[0], ["cmd"], "a")
        step("shortcut cmd+a found in the app's menu", hit is not None, hit[1] if hit else "")
        front = ax.frontmost_app()
        step("the user's front app was left alone", front[0] != app[0] or user_front[0] == app[0], front[1])
    finally:
        _ws.stop()
        if scratch and app is not None:
            os.kill(app[0], signal.SIGTERM)  # the TextEdit this test started, with its scratch document
        if path:
            os.unlink(path)
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(os.path.join(CACHE_DIR, "background_check.json"), "w") as f:
                json.dump({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "app": app_name or "TextEdit",
                           "steps": results}, f, indent=1, ensure_ascii=False)
        except OSError:
            pass
    return 0 if all(r["ok"] for r in results) else 1


def main() -> None:
    if "--check" in sys.argv:
        raise SystemExit(check())
    if "--check-background" in sys.argv:
        i = sys.argv.index("--check-background")
        raise SystemExit(check_background(sys.argv[i + 1] if len(sys.argv) > i + 1 else None))
    if "--check-jev" in sys.argv:
        raise SystemExit(check_jev())
    if "--request-permissions" in sys.argv:
        ax.is_trusted(prompt=True)
        import Quartz
        Quartz.CGRequestScreenCaptureAccess()
        print("Requested. Approve in System Settings, then restart the app that runs this server.")
        return
    _write_status()
    effects.on_toggle = _toggle_background
    threading.Thread(target=_probe, daemon=True).start()
    mcp.run("stdio")


if __name__ == "__main__":
    main()
