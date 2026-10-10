"""Mouse and keyboard via Quartz CGEvents. All coordinates here are global points."""

from __future__ import annotations

import math
import os
import time

import AppKit
import Quartz

GLIDE = float(os.environ.get("CU_GLIDE", "0.2"))  # seconds the pointer takes to travel to a target; 0 = jump

# macOS virtual key codes (ANSI layout = physical positions).
KEYCODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
    "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31,
    "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
    "return": 36, "enter": 36, "tab": 48, "space": 49, "backspace": 51, "delete": 51,
    "escape": 53, "esc": 53, "forwarddelete": 117, "del": 117,
    "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    "capslock": 57, "help": 114, "keypadenter": 76,
    "minus": 27, "equal": 24, "plus": 24, "comma": 43, "period": 47, "slash": 44,
    "semicolon": 41, "quote": 39, "backslash": 42, "grave": 50, "leftbracket": 33, "rightbracket": 30,
}

# name -> (keycode, flag mask)
MODIFIERS: dict[str, tuple[int, int]] = {
    "cmd": (55, Quartz.kCGEventFlagMaskCommand),
    "shift": (56, Quartz.kCGEventFlagMaskShift),
    "alt": (58, Quartz.kCGEventFlagMaskAlternate),
    "ctrl": (59, Quartz.kCGEventFlagMaskControl),
    "fn": (63, Quartz.kCGEventFlagMaskSecondaryFn),
}
MOD_ALIASES = {"command": "cmd", "super": "cmd", "meta": "cmd", "win": "cmd", "option": "alt",
               "opt": "alt", "control": "ctrl", "ctl": "ctrl"}
KEY_ALIASES = {"ret": "return", "cr": "return", "esc": "escape", "pgup": "pageup", "pgdn": "pagedown",
               "arrowleft": "left", "arrowright": "right", "arrowup": "up", "arrowdown": "down",
               "back_space": "backspace", "page_up": "pageup", "page_down": "pagedown"}

_BUTTONS = {
    "left": (Quartz.kCGEventLeftMouseDown, Quartz.kCGEventLeftMouseUp,
             Quartz.kCGEventLeftMouseDragged, Quartz.kCGMouseButtonLeft),
    "right": (Quartz.kCGEventRightMouseDown, Quartz.kCGEventRightMouseUp,
              Quartz.kCGEventRightMouseDragged, Quartz.kCGMouseButtonRight),
    "middle": (Quartz.kCGEventOtherMouseDown, Quartz.kCGEventOtherMouseUp,
               Quartz.kCGEventOtherMouseDragged, Quartz.kCGMouseButtonCenter),
}


def parse_modifiers(mods: list[str] | str | None) -> list[str]:
    if not mods:
        return []
    if isinstance(mods, str):
        mods = [m for m in mods.replace(",", "+").split("+") if m.strip()]
    out = []
    for m in mods:
        name = MOD_ALIASES.get(m.strip().lower(), m.strip().lower())
        if name not in MODIFIERS:
            raise ValueError(f"unknown modifier {m!r}; use cmd, shift, alt, ctrl, fn")
        if name not in out:
            out.append(name)
    return out


def parse_combo(combo: str) -> tuple[list[str], int]:
    """'cmd+shift+t' -> (['cmd', 'shift'], keycode of t). 'cmd++' is cmd + the '=' key."""
    combo = combo.strip()
    if not combo:
        raise ValueError("empty key combo")
    if combo.endswith("++"):
        parts, key = combo[:-2].split("+"), "plus"
    elif combo == "+":
        parts, key = [], "plus"
    else:
        *parts, key = combo.split("+")
    mods = parse_modifiers([p for p in parts if p])
    k = key.strip().lower()
    k = KEY_ALIASES.get(k, k)
    if k in MOD_ALIASES or k in MODIFIERS:  # a bare modifier press, e.g. "shift"
        return mods, MODIFIERS[MOD_ALIASES.get(k, k)][0]
    if k not in KEYCODES:
        raise ValueError(f"unknown key {key!r} in {combo!r}")
    return mods, KEYCODES[k]


def _flags(mods: list[str]) -> int:
    f = 0
    for m in mods:
        f |= MODIFIERS[m][1]
    return f


_SOURCE = None


def _src():
    """Our own event source. By default macOS ignores the real mouse and keyboard for 0.25 s after every
    synthetic event, which makes the user's mouse feel stuck while the agent works; this source turns that off."""
    global _SOURCE
    if _SOURCE is None:
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
        if src is not None:
            Quartz.CGEventSourceSetLocalEventsSuppressionInterval(src, 0.0)
            for state in (Quartz.kCGEventSuppressionStateSuppressionInterval,
                          Quartz.kCGEventSuppressionStateRemoteMouseDrag):
                Quartz.CGEventSourceSetLocalEventsFilterDuringSuppressionState(
                    src, Quartz.kCGEventFilterMaskPermitAllEvents, state)
        _SOURCE = src
    return _SOURCE


def _post(ev) -> None:
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def cursor_pos() -> tuple[float, float]:
    p = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return p.x, p.y


def move(x: float, y: float, prev: tuple[float, float] | None = None) -> None:
    ev = Quartz.CGEventCreateMouseEvent(_src(), Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft)
    if prev is not None:  # real mice report relative motion too; some apps and hover logic read it
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventDeltaX, int(round(x - prev[0])))
        Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventDeltaY, int(round(y - prev[1])))
    _post(ev)


def glide(x: float, y: float, duration: float = GLIDE) -> None:
    """Move the pointer along an eased path at ~120 Hz so the user can follow it. Short hops take less time;
    tiny ones jump."""
    x0, y0 = cursor_pos()
    dist = math.hypot(x - x0, y - y0)
    if duration <= 0 or dist < 4:
        move(x, y, (x0, y0))
        return
    duration = min(duration, 0.06 + dist / 2500)
    start = time.monotonic()
    prev = (x0, y0)
    frame = 1 / 120
    while True:
        t = (time.monotonic() - start) / duration
        if t >= 1:
            break
        e = t * t * (3 - 2 * t)
        pt = (x0 + (x - x0) * e, y0 + (y - y0) * e)
        move(*pt, prev)
        prev = pt
        time.sleep(max(0.0, start + (math.floor(t * duration / frame) + 1) * frame - time.monotonic()))
    move(x, y, prev)


class _held:
    """Press modifier keys for the duration of a mouse/key action."""

    def __init__(self, mods: list[str]):
        self.mods = mods

    def __enter__(self):
        flags = 0
        for m in self.mods:
            code, mask = MODIFIERS[m]
            flags |= mask
            ev = Quartz.CGEventCreateKeyboardEvent(_src(), code, True)
            Quartz.CGEventSetFlags(ev, flags)
            _post(ev)
            time.sleep(0.01)
        return self

    def __exit__(self, *exc):
        flags = _flags(self.mods)
        for m in reversed(self.mods):
            code, mask = MODIFIERS[m]
            flags &= ~mask
            ev = Quartz.CGEventCreateKeyboardEvent(_src(), code, False)
            Quartz.CGEventSetFlags(ev, flags)
            _post(ev)
            time.sleep(0.01)


def click(x: float, y: float, button: str = "left", count: int = 1, mods: list[str] | None = None) -> None:
    if button not in _BUTTONS:
        raise ValueError("button must be left, right or middle")
    down, up, _, btn = _BUTTONS[button]
    mods = mods or []
    flags = _flags(mods)
    glide(x, y)
    time.sleep(0.06)
    with _held(mods):
        for i in range(1, max(1, count) + 1):
            for etype in (down, up):
                ev = Quartz.CGEventCreateMouseEvent(_src(), etype, (x, y), btn)
                Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, i)
                Quartz.CGEventSetFlags(ev, flags)
                _post(ev)
                time.sleep(0.02)
            time.sleep(0.04)


def drag(x0: float, y0: float, x1: float, y1: float, button: str = "left",
         mods: list[str] | None = None, duration: float = 0.4) -> None:
    down, up, dragged, btn = _BUTTONS[button]
    mods = mods or []
    flags = _flags(mods)
    glide(x0, y0)
    time.sleep(0.06)
    with _held(mods):
        ev = Quartz.CGEventCreateMouseEvent(_src(), down, (x0, y0), btn)
        Quartz.CGEventSetFlags(ev, flags)
        _post(ev)
        time.sleep(0.08)
        steps = max(8, int(duration / 0.02))
        for i in range(1, steps + 1):
            t = i / steps
            t = t * t * (3 - 2 * t)  # ease in/out; some apps ignore instant jumps
            p = (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
            ev = Quartz.CGEventCreateMouseEvent(_src(), dragged, p, btn)
            Quartz.CGEventSetFlags(ev, flags)
            _post(ev)
            time.sleep(duration / steps)
        time.sleep(0.08)
        ev = Quartz.CGEventCreateMouseEvent(_src(), up, (x1, y1), btn)
        Quartz.CGEventSetFlags(ev, flags)
        _post(ev)


def scroll(x: float, y: float, direction: str, amount: int = 5, mods: list[str] | None = None) -> None:
    """amount = wheel notches; one notch is ~80 px of content."""
    direction = direction.lower()
    if direction not in ("up", "down", "left", "right"):
        raise ValueError("direction must be up, down, left or right")
    glide(x, y)
    time.sleep(0.05)
    flags = _flags(mods or [])
    dy = {"up": 80, "down": -80}.get(direction, 0)
    dx = {"left": 80, "right": -80}.get(direction, 0)
    for _ in range(max(1, amount)):
        ev = Quartz.CGEventCreateScrollWheelEvent(_src(), Quartz.kCGScrollEventUnitPixel, 2, dy, dx)
        Quartz.CGEventSetFlags(ev, flags)
        _post(ev)
        time.sleep(0.03)


def key(combo: str, repeat: int = 1) -> None:
    mods, code = parse_combo(combo)
    flags = _flags(mods)
    for _ in range(max(1, repeat)):
        with _held(mods):
            for is_down in (True, False):
                ev = Quartz.CGEventCreateKeyboardEvent(_src(), code, is_down)
                Quartz.CGEventSetFlags(ev, flags)
                _post(ev)
                time.sleep(0.015)
        time.sleep(0.03)


def type_text(text: str, delay: float = 0.012) -> None:
    """Types any Unicode (Polish letters, emoji) independent of keyboard layout.
    Newlines are sent as Return, tabs as Tab."""
    for ch in text:
        if ch == "\n":
            key("return")
            continue
        if ch == "\t":
            key("tab")
            continue
        units = len(ch.encode("utf-16-le")) // 2
        for is_down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(_src(), 0, is_down)
            Quartz.CGEventSetFlags(ev, 0)
            Quartz.CGEventKeyboardSetUnicodeString(ev, units, ch)
            _post(ev)
        time.sleep(delay)


def paste_text(text: str) -> bool:
    """Paste via clipboard, then restore the previous text clipboard. Returns True if restored."""
    pb = AppKit.NSPasteboard.generalPasteboard()
    previous = pb.stringForType_(AppKit.NSPasteboardTypeString)
    pb.clearContents()
    pb.setString_forType_(text, AppKit.NSPasteboardTypeString)
    time.sleep(0.05)
    key("cmd+v")
    time.sleep(0.35)
    if previous is not None:
        pb.clearContents()
        pb.setString_forType_(previous, AppKit.NSPasteboardTypeString)
        return True
    return False


def read_clipboard() -> str:
    s = AppKit.NSPasteboard.generalPasteboard().stringForType_(AppKit.NSPasteboardTypeString)
    return "" if s is None else str(s)


def write_clipboard(text: str) -> None:
    pb = AppKit.NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, AppKit.NSPasteboardTypeString)


# ---------------------------------------------------------------- background: events for one app only
# CGEventPostToPid hands an event straight to one process: the pointer does not move and the user's keyboard
# focus stays where it is. Whether an inactive app acts on such events is up to the app, so callers check the
# result (read the field back, look at the screenshot) instead of assuming it worked.

def key_to_pid(pid: int, combo: str, repeat: int = 1) -> None:
    mods, code = parse_combo(combo)
    flags = _flags(mods)
    for _ in range(max(1, repeat)):
        for is_down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(_src(), code, is_down)
            Quartz.CGEventSetFlags(ev, flags)
            Quartz.CGEventPostToPid(pid, ev)
            time.sleep(0.015)
        time.sleep(0.03)


def type_to_pid(pid: int, text: str, delay: float = 0.012) -> None:
    for ch in text:
        if ch in "\n\t":
            key_to_pid(pid, "return" if ch == "\n" else "tab")
            continue
        units = len(ch.encode("utf-16-le")) // 2
        for is_down in (True, False):
            ev = Quartz.CGEventCreateKeyboardEvent(_src(), 0, is_down)
            Quartz.CGEventSetFlags(ev, 0)
            Quartz.CGEventKeyboardSetUnicodeString(ev, units, ch)
            Quartz.CGEventPostToPid(pid, ev)
        time.sleep(delay)


def click_to_pid(pid: int, x: float, y: float, button: str = "left", count: int = 1,
                 mods: list[str] | None = None) -> None:
    if button not in _BUTTONS:
        raise ValueError("button must be left, right or middle")
    down, up, _, btn = _BUTTONS[button]
    flags = _flags(mods or [])
    for i in range(1, max(1, count) + 1):
        for etype in (down, up):
            ev = Quartz.CGEventCreateMouseEvent(_src(), etype, (x, y), btn)
            Quartz.CGEventSetIntegerValueField(ev, Quartz.kCGMouseEventClickState, i)
            Quartz.CGEventSetFlags(ev, flags)
            Quartz.CGEventPostToPid(pid, ev)
            time.sleep(0.02)
        time.sleep(0.04)


def scroll_to_pid(pid: int, x: float, y: float, direction: str, amount: int = 5) -> None:
    direction = direction.lower()
    if direction not in ("up", "down", "left", "right"):
        raise ValueError("direction must be up, down, left or right")
    dy = {"up": 80, "down": -80}.get(direction, 0)
    dx = {"left": 80, "right": -80}.get(direction, 0)
    for _ in range(max(1, amount)):
        ev = Quartz.CGEventCreateScrollWheelEvent(_src(), Quartz.kCGScrollEventUnitPixel, 2, dy, dx)
        Quartz.CGEventSetLocation(ev, (x, y))
        Quartz.CGEventPostToPid(pid, ev)
        time.sleep(0.03)
