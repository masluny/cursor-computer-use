"""macOS Accessibility (AX): frontmost app, clickable-element discovery, menu bar commands.

Everything here returns frames in global points; the server maps them to screenshot pixels.
"""

from __future__ import annotations

import re
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from typing import Any

import AppKit
import ApplicationServices as AS
import Quartz

# Roles worth showing the model. Containers (groups, scroll areas) are walked, not listed.
INTERACTIVE_ROLES = {
    "AXButton", "AXCheckBox", "AXRadioButton", "AXTextField", "AXTextArea", "AXComboBox",
    "AXPopUpButton", "AXMenuButton", "AXMenuItem", "AXMenuBarItem", "AXLink", "AXSlider",
    "AXIncrementor", "AXDisclosureTriangle", "AXRow", "AXTab", "AXColorWell", "AXDockItem",
    "AXSegmentedControl", "AXStepper", "AXSwitch", "AXToggle",
}
# Listed only when they expose AXPress (clickable divs in web views, icon tiles, etc.).
PRESSABLE_ROLES = {"AXGroup", "AXImage", "AXCell", "AXGenericElement"}
# Do not descend into these; their children are rarely separately useful.
LEAF_ROLES = {
    "AXButton", "AXCheckBox", "AXRadioButton", "AXTextField", "AXTextArea", "AXComboBox",
    "AXPopUpButton", "AXMenuButton", "AXSlider", "AXIncrementor", "AXLink", "AXDisclosureTriangle",
    "AXStaticText", "AXImage", "AXValueIndicator", "AXSwitch", "AXToggle", "AXColumn",
}
# Scrolled-away rows/cells are skipped with their whole subtree (big tables would eat the budget).
OFFSCREEN_PRUNE_ROLES = {"AXRow", "AXCell"}
TEXT_ROLES = {"AXTextField", "AXTextArea", "AXComboBox", "AXSearchField"}
# Roles a person would call "a control": they win ties against groups and plain text.
ACTION_ROLES = {
    "AXButton", "AXLink", "AXMenuItem", "AXMenuBarItem", "AXCheckBox", "AXRadioButton", "AXTab",
    "AXPopUpButton", "AXMenuButton", "AXDisclosureTriangle", "AXComboBox", "AXTextField", "AXTextArea",
    "AXSwitch", "AXToggle", "AXSegmentedControl", "AXDockItem", "AXSlider",
}
CLIPPING_ROLES = {"AXWindow", "AXScrollArea", "AXSheet", "AXDrawer", "AXPopover"}

_ATTRS = ["AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXPosition", "AXSize",
          "AXEnabled", "AXFocused", "AXHelp", "AXPlaceholderValue"]


def is_trusted(prompt: bool = False) -> bool:
    if prompt:
        return bool(AS.AXIsProcessTrustedWithOptions({AS.kAXTrustedCheckOptionPrompt: True}))
    return bool(AS.AXIsProcessTrusted())


# ---------------------------------------------------------------- apps & windows

def app_name(pid: int) -> str:
    app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
    return str(app.localizedName()) if app is not None and app.localizedName() else f"pid {pid}"


def frontmost_app() -> tuple[int, str]:
    """Fresh on every call (NSWorkspace caches state without a main run loop)."""
    try:
        asn = subprocess.run(["/usr/bin/lsappinfo", "front"], capture_output=True, text=True, timeout=3).stdout.strip()
        info = subprocess.run(["/usr/bin/lsappinfo", "info", "-only", "pid", asn],
                              capture_output=True, text=True, timeout=3).stdout
        m = re.search(r"pid\s*=\s*(\d+)", info)
        if m:
            pid = int(m.group(1))
            return pid, app_name(pid)
    except (OSError, subprocess.SubprocessError):
        pass
    app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
    return int(app.processIdentifier()), str(app.localizedName())


def windows() -> list[dict]:
    """Normal on-screen windows, front to back."""
    opts = Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements
    out = []
    for w in Quartz.CGWindowListCopyWindowInfo(opts, Quartz.kCGNullWindowID) or []:
        if w.get("kCGWindowLayer") != 0:
            continue
        b = w.get("kCGWindowBounds") or {}
        if b.get("Width", 0) < 40 or b.get("Height", 0) < 40:
            continue
        out.append({
            "number": int(w.get("kCGWindowNumber") or 0),
            "app": str(w.get("kCGWindowOwnerName") or ""),
            "pid": int(w.get("kCGWindowOwnerPID") or 0),
            "title": str(w.get("kCGWindowName") or ""),
            "frame": (b.get("X", 0), b.get("Y", 0), b.get("Width", 0), b.get("Height", 0)),
        })
    return out


def pid_for_app(name: str) -> int | None:
    """Running app by (case-insensitive) name, preferring exact matches."""
    name_l = name.strip().lower()
    candidates: dict[int, str] = {}
    for app in AppKit.NSWorkspace.sharedWorkspace().runningApplications():
        if app.activationPolicy() != AppKit.NSApplicationActivationPolicyRegular:
            continue
        candidates[int(app.processIdentifier())] = str(app.localizedName() or "")
    for w in windows():
        candidates.setdefault(w["pid"], w["app"])
    for pid, n in candidates.items():
        if n.lower() == name_l:
            return pid
    for pid, n in candidates.items():
        if name_l in n.lower():
            return pid
    return None


# ---------------------------------------------------------------- raw AX access

def _attr(el, name: str):
    err, v = AS.AXUIElementCopyAttributeValue(el, name, None)
    return v if err == 0 else None


def _attrs(el, names: list[str]) -> dict[str, Any]:
    err, vals = AS.AXUIElementCopyMultipleAttributeValues(el, names, 0, None)
    if err != 0 or vals is None:
        return {n: _attr(el, n) for n in names}
    out = {}
    for n, v in zip(names, vals):
        if isinstance(v, AS.AXValueRef) and AS.AXValueGetType(v) == AS.kAXValueAXErrorType:
            v = None
        out[n] = v
    return out


def _children(el) -> list:
    v = _attr(el, "AXChildren")
    return list(v) if v else []


def _actions(el) -> list[str]:
    err, names = AS.AXUIElementCopyActionNames(el, None)
    return [str(n) for n in names] if err == 0 and names else []


def _xy(v, kind) -> tuple[float, float] | None:
    if v is None or not isinstance(v, AS.AXValueRef):
        return None
    ok, p = AS.AXValueGetValue(v, kind, None)
    if not ok:
        return None
    return (p.x, p.y) if kind == AS.kAXValueCGPointType else (p.width, p.height)


def _frame(pos, size) -> tuple[float, float, float, float] | None:
    p = _xy(pos, AS.kAXValueCGPointType)
    s = _xy(size, AS.kAXValueCGSizeType)
    if p is None or s is None:
        return None
    return p[0], p[1], s[0], s[1]


def _text(v, limit: int = 80) -> str:
    if v is None or isinstance(v, bool):
        return ""
    if isinstance(v, (str, int, float)):
        s = " ".join(str(v).split())
        return s if len(s) <= limit else s[: limit - 1] + "…"
    return ""


def element_frame(ref) -> tuple[float, float, float, float] | None:
    a = _attrs(ref, ["AXPosition", "AXSize"])
    return _frame(a["AXPosition"], a["AXSize"])


def press(ref) -> bool:
    return AS.AXUIElementPerformAction(ref, "AXPress") == 0


# ---------------------------------------------------------------- element snapshot

@dataclass
class Element:
    ref: Any
    role: str
    subrole: str
    label: str
    value: str
    frame: tuple[float, float, float, float]  # x, y, w, h in points
    enabled: bool
    focused: bool
    texts: tuple[tuple[str, int], ...] = ()  # (text, score offset) used for matching by label

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.frame
        return x + w / 2, y + h / 2

    @property
    def kind(self) -> str:
        role = (self.subrole if self.subrole in ("AXSearchField", "AXCloseButton", "AXMinimizeButton",
                                                  "AXZoomButton", "AXFullScreenButton", "AXTabButton")
                else self.role)
        return re.sub(r"(?<!^)(?=[A-Z])", " ", role.removeprefix("AX")).lower()


def _intersect(a, b):
    x0, y0 = max(a[0], b[0]), max(a[1], b[1])
    x1, y1 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (x0, y0, x1 - x0, y1 - y0) if x1 > x0 and y1 > y0 else None


def _inner_text(el, depth: int = 3, limit: int = 3) -> str:
    """Text of descendant static texts, for rows/cells/links without their own label."""
    found: list[str] = []
    stack = [(c, 1) for c in reversed(_children(el))]
    while stack and len(found) < limit:
        node, d = stack.pop()
        a = _attrs(node, ["AXRole", "AXValue", "AXTitle", "AXDescription"])
        if a["AXRole"] in ("AXStaticText", "AXTextField", "AXImage", "AXButton"):
            t = _text(a["AXValue"]) or _text(a["AXTitle"]) or _text(a["AXDescription"])
            if t:
                found.append(t)
        if d < depth:
            stack.extend((c, d + 1) for c in reversed(_children(node)))
    return _text(" · ".join(found), 100)


def _roots(app) -> list:
    """Focused window (with its sheets/popovers) plus open context menus and floating panels."""
    roots = []
    focused = _attr(app, "AXFocusedWindow") or _attr(app, "AXMainWindow")
    if focused is not None:
        roots.append(focused)
    for child in _children(app):
        role = _attr(child, "AXRole")
        if role == "AXMenu":
            roots.append(child)
        elif role == "AXWindow" and child != focused:
            if _attr(child, "AXSubrole") in ("AXFloatingWindow", "AXSystemFloatingWindow",
                                             "AXDialog", "AXSystemDialog"):
                roots.append(child)
    if not roots:
        roots = [c for c in _children(app) if _attr(c, "AXRole") != "AXMenuBar"]
    return roots


_ax_enabled: set[int] = set()  # pids already asked to expose their accessibility tree
_UNBOUNDED = (-1e6, -1e6, 2e6, 2e6)


def snapshot(pid: int, screen: tuple[float, float, float, float], limit: int = 150,
             max_nodes: int = 5000, budget_s: float = 5.0, include_text: bool = False,
             text_limit: int = 800) -> tuple[list[Element], bool]:
    """Visible interactive elements of the app's front window, in reading order; with include_text, visible
    static text too (appended after the controls), so plain text like list suggestions can be targeted.

    Returns (elements, truncated)."""
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, 1.0)
    if pid not in _ax_enabled:
        # Electron/Chromium apps only build their AX tree when asked to, and need a moment to do it.
        if AS.AXUIElementSetAttributeValue(app, "AXManualAccessibility", True) == 0:
            time.sleep(0.35)
        _ax_enabled.add(pid)

    deadline = time.monotonic() + budget_s
    elements: list[Element] = []
    statics: list[Element] = []
    seen_frames: dict[tuple[int, int, int, int], int] = {}  # frame -> index in elements
    visited = 0
    truncated = False
    # An open menu lists all its items, also those past the screen edge: they can still be chosen (AXPress), and
    # a long list (countries) would otherwise need blind scrolling.
    stack = [(r, _UNBOUNDED if _attr(r, "AXRole") == "AXMenu" else screen) for r in reversed(_roots(app))]
    while stack:
        if len(elements) >= limit or visited >= max_nodes or time.monotonic() > deadline:
            truncated = True
            break
        el, clip = stack.pop()
        visited += 1
        a = _attrs(el, _ATTRS)
        role = str(a["AXRole"] or "")
        frame = _frame(a["AXPosition"], a["AXSize"])
        visible = frame is not None and frame[2] >= 2 and frame[3] >= 2
        if visible:
            cx, cy = frame[0] + frame[2] / 2, frame[1] + frame[3] / 2
            visible = clip[0] <= cx < clip[0] + clip[2] and clip[1] <= cy < clip[1] + clip[3]
        if frame is not None and role in CLIPPING_ROLES:
            clip = _intersect(clip, frame) or clip
            if role != "AXWindow" and not visible:
                continue  # a scrolled-away or hidden pane
        if frame is not None and not visible and role in OFFSCREEN_PRUNE_ROLES:
            continue
        if include_text and visible and role == "AXStaticText" and len(statics) < text_limit:
            t = _text(a["AXValue"], 120) or _text(a["AXTitle"], 120) or _text(a["AXDescription"], 120)
            if t:
                statics.append(Element(el, role, "", t, "", frame, True, False, ((t, 0),)))

        listed = role in INTERACTIVE_ROLES or (role in PRESSABLE_ROLES and visible and "AXPress" in _actions(el))
        if listed and visible:
            key = tuple(round(v) for v in frame)
            same = seen_frames.get(key)
            # A control replaces a generic wrapper of the same size: web search fields sit in a clickable group
            # that loses its only text (the placeholder) once something is typed, so keep the field itself.
            if same is None or (elements[same].role in PRESSABLE_ROLES and role not in PRESSABLE_ROLES):
                label = (_text(a["AXTitle"]) or _text(a["AXDescription"])
                         or ("" if role in TEXT_ROLES else _text(a["AXValue"]))
                         or _text(a["AXHelp"]) or _text(a["AXPlaceholderValue"]))
                if not label and role in ("AXRow", "AXCell", "AXLink", "AXGroup", "AXButton", "AXMenuItem"):
                    label = _inner_text(el)
                value = ""
                if role in TEXT_ROLES:
                    value = _text(a["AXValue"], 60) or (f"(placeholder: {_text(a['AXPlaceholderValue'], 40)})"
                                                        if a["AXPlaceholderValue"] else "")
                elif role in ("AXCheckBox", "AXRadioButton", "AXSwitch", "AXToggle") and a["AXValue"] is not None:
                    value = "on" if str(a["AXValue"]) in ("1", "True") else "off"
                elif role in ("AXSlider", "AXIncrementor"):
                    value = _text(a["AXValue"])
                item = Element(el, role, str(a["AXSubrole"] or ""), label, value, frame,
                               a["AXEnabled"] is not False, a["AXFocused"] is True, match_texts(a, role, label))
                if same is None:
                    seen_frames[key] = len(elements)
                    elements.append(item)
                else:
                    elements[same] = item
        if role in LEAF_ROLES:
            continue
        stack.extend((c, clip) for c in reversed(_children(el)))
    return elements + statics, truncated


def match_texts(a: dict, role: str, label: str) -> tuple[tuple[str, int], ...]:
    """(text, score offset) pairs an element can be found by: its names, placeholder and help; a pop-up's selected
    item; a text field's current content ("the field that says My Playlist #45"). Content ranks low enough that only
    fill (which prefers inputs) picks a field by it; click_text keeps choosing visible text and links."""
    texts = [(t, off) for t, off in ((_text(a.get("AXTitle"), 120), 0), (_text(a.get("AXDescription"), 120), 0),
                                      (_text(a.get("AXPlaceholderValue"), 120), 0),
                                      (_text(a.get("AXHelp"), 120), -10)) if t]
    value = _text(a.get("AXValue"), 120)
    if value:
        texts.append((value, -20 if role in TEXT_ROLES else 0))
    if label and not texts:
        texts.append((label, -3))  # from descendant text
    return tuple(texts)


# pid -> the element we focused through AXFocused. An app in the background (hidden display) often reports no
# AXFocusedUIElement although the field itself still says AXFocused (seen in Safari web apps).
_focus_hint: dict[int, Any] = {}


def note_focus(pid: int, ref) -> None:
    _focus_hint[pid] = ref


def focused_element(pid: int):
    """The app's focused element, or the field we focused in it when the app does not report one."""
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, 0.5)
    el = _attr(app, "AXFocusedUIElement")
    if el is None:
        hint = _focus_hint.get(pid)
        if hint is not None and _attr(hint, "AXFocused") is True:
            return hint
    return el


def focused_summary(pid: int) -> str:
    """Where typed text would go right now, e.g. 'text field "Search" = "abc"'."""
    el = focused_element(pid)
    if el is None:
        return "nothing focused"
    a = _attrs(el, ["AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue"])
    role = str(a["AXSubrole"] if a["AXSubrole"] in ("AXSearchField",) else a["AXRole"] or "element")
    kind = re.sub(r"(?<!^)(?=[A-Z])", " ", role.removeprefix("AX")).lower()
    name = _text(a["AXTitle"], 40) or _text(a["AXDescription"], 40) or _text(a["AXPlaceholderValue"], 40)
    out = kind + (f' "{name}"' if name else "")
    if role in TEXT_ROLES or role == "AXSearchField":
        out += f' = "{_text(a["AXValue"], 60)}"'
    return out


def focused_input(pid: int) -> tuple[tuple[float, float, float, float] | None, bool]:
    """(frame, is_search) of the focused text field; (None, False) when focus is not in a text field."""
    el = focused_element(pid)
    if el is None:
        return None, False
    a = _attrs(el, ["AXRole", "AXSubrole", "AXPosition", "AXSize"])
    if a["AXRole"] not in TEXT_ROLES and a["AXSubrole"] != "AXSearchField":
        return None, False
    return _frame(a["AXPosition"], a["AXSize"]), a["AXSubrole"] == "AXSearchField" or a["AXRole"] == "AXComboBox"


def focused_is_text_input(pid: int) -> bool:
    el = focused_element(pid)
    if el is None:
        return False
    a = _attrs(el, ["AXRole", "AXSubrole"])
    return a["AXRole"] in TEXT_ROLES or a["AXSubrole"] == "AXSearchField"


# ---------------------------------------------------------------- menu bar

def normalize(s: str) -> str:
    """Lowercase words only: 'Save As…' -> 'save as', '› · Metadata · time' -> 'metadata time'."""
    s = unicodedata.normalize("NFKD", s.lower().replace("…", " ").replace("ł", "l"))
    s = "".join(c for c in s if not unicodedata.combining(c))  # OCR often drops diacritics: ą == a
    return " ".join(re.sub(r"[^\w]+", " ", s).split())


def match_score(query: str, text: str) -> float:
    """How well normalized `text` matches normalized `query`: 100 exact, 85 word prefix, 70 prefix,
    65 whole words inside, 45 substring, 0 none; longer texts lose a little."""
    if not query or not text:
        return 0.0
    if text == query:
        return 100.0
    if text.startswith(query):
        base = 85.0 if text[len(query)] == " " else 70.0
    elif f" {query} " in f" {text} ":
        base = 65.0
    elif query in text:
        base = 45.0
    else:
        return 0.0
    return base - min(10.0, 0.15 * (len(text) - len(query)))


def role_bonus(role: str) -> float:
    if role in ACTION_ROLES:
        return 8.0
    if role in ("AXRow", "AXCell"):
        return 4.0
    if role == "AXStaticText":
        return -4.0
    return 0.0


def rank(query: str, elements: dict[int, Element], prefer_inputs: bool = False) -> list[tuple[float, int]]:
    """(score, id) of elements matching query, best first."""
    q = normalize(query)
    raw = query.strip().lower()
    out = []
    for i, el in elements.items():
        best = 0.0
        for t, off in el.texts:
            sc = match_score(q, normalize(t)) if q else (100.0 if t.strip().lower() == raw else 0.0)
            if sc:
                best = max(best, sc + off)
        if best <= 0:
            continue
        best += role_bonus(el.role)
        if not el.enabled:
            best -= 30
        if prefer_inputs and (el.role in TEXT_ROLES or el.subrole == "AXSearchField"):
            best += 30
        out.append((best, i))
    out.sort(key=lambda t: -t[0])
    return out


def _match(items: list, name: str):
    titled = [(it, str(_attr(it, "AXTitle") or "")) for it in items]
    want = normalize(name)
    available = [t for _, t in titled if t]
    # exact, then prefix, then substring; the first item in menu order wins
    for test in (lambda t: normalize(t) == want, lambda t: normalize(t).startswith(want), lambda t: want in normalize(t)):
        hits = [(it, t) for it, t in titled if t and test(t)]
        if hits:
            return hits[0], available
    return None, available


def _submenu(item):
    return next((c for c in _children(item) if _attr(c, "AXRole") == "AXMenu"), None)


def _listing(items: list) -> str:
    """Titles of menu items, submenus marked with a trailing ▸."""
    out = []
    for it in items:
        t = str(_attr(it, "AXTitle") or "")
        if t:
            out.append(t + (" ▸" if _submenu(it) is not None else ""))
    return ", ".join(out) or "(empty)"


def menu_select(pid: int, path: list[str], escape=None) -> tuple[bool, str]:
    """Press a menu command by path, e.g. ["File", "Export", "PDF…"].

    A path that ends at a menu (not a command) lists that menu instead; an empty path lists the menu bar.
    Menus opened only to read them are closed with escape(n) (default: n global Escape presses).
    Returns (pressed, description)."""
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, 2.0)
    bar = _attr(app, "AXMenuBar")
    if bar is None:
        raise RuntimeError("this app exposes no menu bar (is Accessibility permission granted?)")
    level = _children(bar)
    if not path:
        return False, "Menu bar: " + _listing(level)
    opened = []
    titles: list[str] = []
    try:
        for i, name in enumerate(path):
            hit, available = _match(level, name)
            if hit is None:
                raise ValueError(f"menu item {name!r} not found under {' > '.join(titles) or 'menu bar'}. "
                                 f"Available: {', '.join(available[:60])}")
            item, title = hit
            titles.append(title)
            submenu = _submenu(item)
            if submenu is None:
                if i < len(path) - 1:
                    raise ValueError(f"{' > '.join(titles)!r} is a command, not a menu")
                if _attr(item, "AXEnabled") is False:
                    raise ValueError(f"menu item {' > '.join(titles)!r} is disabled right now")
                if not press(item):
                    raise RuntimeError(f"pressing {' > '.join(titles)!r} failed")
                opened.clear()  # the press closes the menus
                return True, " > ".join(titles)
            level = _children(submenu)
            if not level:  # lazily-populated menu: open it so the app fills it in
                press(item)
                opened.append(item)
                time.sleep(0.35)
                submenu = _submenu(item)
                level = _children(submenu) if submenu is not None else []
        return False, f"{' > '.join(titles)} contains: {_listing(level)}"
    finally:
        if opened:
            from . import inputs
            (escape or (lambda n: inputs.key("escape", repeat=n)))(len(opened) + 1)


def find_by_label(labels: dict[int, str], text: str) -> tuple[list[int], str]:
    """Ids whose label matches text: exact first, then prefix, then substring. Returns (ids, match kind)."""
    want = normalize(text)
    if not want:
        return [], ""
    for how, test in (("exact", lambda lab: lab == want), ("prefix", lambda lab: lab.startswith(want)),
                      ("substring", lambda lab: want in lab)):
        hits = [i for i, lab in labels.items() if lab and test(normalize(lab))]
        if hits:
            return hits, how
    return [], ""


# ---------------------------------------------------------------- what is under a point

_CONTROL_ROLES = ACTION_ROLES | {"AXRow", "AXCell", "AXMenuItem", "AXMenuBarItem", "AXLink", "AXDockItem", "AXImage"}


def element_at(x: float, y: float) -> tuple[Any, str, list[str]]:
    """(element, description, texts) of what a click at global point (x, y) would hit, walking up from plain
    text to its control (a label inside a button reports the button). (None, "", []) when unknown."""
    sw = AS.AXUIElementCreateSystemWide()
    AS.AXUIElementSetMessagingTimeout(sw, 0.5)
    err, hit = AS.AXUIElementCopyElementAtPosition(sw, float(x), float(y), None)
    if err != 0 or hit is None:
        return None, "", []
    names = ["AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue"]
    first = _attrs(hit, names)
    node, a = hit, first
    for _ in range(4):
        if str(a["AXRole"] or "") in _CONTROL_ROLES:
            break
        parent = _attr(node, "AXParent")
        if parent is None:
            node, a = hit, first
            break
        node, a = parent, _attrs(parent, names)
    else:
        node, a = hit, first
    role = str(a["AXRole"] or "element")
    texts = [t for t in (_text(a["AXTitle"]), _text(a["AXDescription"]),
                         "" if role in TEXT_ROLES else _text(a["AXValue"]),
                         _text(first["AXValue"]) or _text(first["AXTitle"])) if t]
    kind = re.sub(r"(?<!^)(?=[A-Z])", " ", role.removeprefix("AX")).lower()
    label = texts[0] if texts else ""
    return node, kind + (f' "{label}"' if label else ""), texts


def within(inner, outer, depth: int = 12) -> bool:
    """inner is outer or sits inside it."""
    node = inner
    for _ in range(depth):
        if node is None:
            return False
        if node == outer:
            return True
        node = _attr(node, "AXParent")
    return False


def reach(x: float, y: float, ref, describe: bool = True) -> tuple[bool | None, str]:
    """Whether a click at global point (x, y) lands on ref. True when the deepest element there is ref, inside it,
    or a control wrapping it (the button around a label). False, with what is there, when something sits on top
    (a sticky header, a pop-up) or when only a container of ref is there: ref is then scrolled out of the visible
    part of its list, although the accessibility tree still places it on screen. None when untestable. The
    description is of the control at the point (skipped on a hit when describe is False)."""
    sw = AS.AXUIElementCreateSystemWide()
    AS.AXUIElementSetMessagingTimeout(sw, 0.5)
    err, hit = AS.AXUIElementCopyElementAtPosition(sw, float(x), float(y), None)
    if err != 0 or hit is None:
        return None, ""
    if within(hit, ref) or within(ref, hit, depth=3) and str(_attr(hit, "AXRole") or "") in _CONTROL_ROLES:
        return True, element_at(x, y)[1] if describe else ""
    return False, element_at(x, y)[1] or "something else"


_NOT_A_ROW = {"AXWindow", "AXWebArea", "AXApplication", "AXScrollArea", "AXList", "AXTable", "AXOutline",
              "AXMenu", "AXMenuBar", "AXSheet", "AXDrawer"}


def row_context(ref, own: str = "", budget_s: float = 0.12) -> str:
    """The other texts of the list row (or toolbar strip) around ref, e.g. 'Peggy Sue · Buddy Holly', so that
    identical controls ("Add", "Delete", "Edit") in different rows can be told apart. "" when there is none."""
    deadline = time.monotonic() + budget_s
    own_n = normalize(own)
    node = _attr(ref, "AXParent")
    for _ in range(6):
        if node is None or time.monotonic() > deadline:
            return ""
        a = _attrs(node, ["AXRole", "AXPosition", "AXSize"])
        role = str(a["AXRole"] or "")
        if role in _NOT_A_ROW:
            return ""
        f = _frame(a["AXPosition"], a["AXSize"])
        if role == "AXRow" or (f is not None and f[3] <= 140 and f[2] >= 2.5 * f[3]):
            texts = [t for t in _texts_below(node, deadline) if normalize(t) != own_n]
            if texts:
                out = " · ".join(dict.fromkeys(texts))
                return out if len(out) <= 120 else out[:119] + "…"
        node = _attr(node, "AXParent")
    return ""


def _texts_below(node, deadline: float, limit: int = 40) -> list[str]:
    out, stack, seen = [], [node], 0
    while stack and seen < limit and time.monotonic() < deadline:
        n = stack.pop()
        seen += 1
        a = _attrs(n, ["AXRole", "AXValue", "AXTitle"])
        if a["AXRole"] == "AXStaticText":
            t = _text(a["AXValue"]) or _text(a["AXTitle"])
            if t:
                out.append(t)
        else:
            stack.extend(reversed(_children(n)))
    return out


# ---------------------------------------------------------------- background work (no pointer, no keyboard focus)

def pid_of(ref) -> int | None:
    err, pid = AS.AXUIElementGetPid(ref, None)
    return int(pid) if err == 0 else None


def perform(ref, action: str) -> bool:
    return ref is not None and AS.AXUIElementPerformAction(ref, action) == 0


def set_attr(ref, name: str, value) -> bool:
    return ref is not None and AS.AXUIElementSetAttributeValue(ref, name, value) == 0


def window_refs(pid: int) -> list[tuple[Any, tuple[float, float, float, float], str]]:
    """(window, frame, title) of the app's normal, not minimized windows."""
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, 1.0)
    out = []
    for w in _attr(app, "AXWindows") or []:
        a = _attrs(w, ["AXPosition", "AXSize", "AXTitle", "AXMinimized", "AXSubrole"])
        frame = _frame(a["AXPosition"], a["AXSize"])
        if frame and a["AXMinimized"] is not True:
            out.append((w, frame, _text(a["AXTitle"], 200)))
    return out


def set_window_frame(ref, x: float, y: float, w: float | None = None, h: float | None = None) -> bool:
    """Move (and optionally resize) another app's window. Position first: a window keeps its size limits."""
    ok = set_attr(ref, "AXPosition", AS.AXValueCreate(AS.kAXValueCGPointType, Quartz.CGPointMake(x, y)))
    if ok and w and h:
        set_attr(ref, "AXSize", AS.AXValueCreate(AS.kAXValueCGSizeType, Quartz.CGSizeMake(w, h)))
    return ok


def is_text_input(ref) -> bool:
    a = _attrs(ref, ["AXRole", "AXSubrole"])
    return a["AXRole"] in TEXT_ROLES or a["AXSubrole"] == "AXSearchField"


def value_of(ref) -> str:
    v = _attr(ref, "AXValue")
    return v if isinstance(v, str) else ""


def insert_text(ref, text: str, replace: bool = False) -> bool:
    """Type into a text element without the keyboard. AXSelectedText replaces the selection (= inserts at the
    caret) and goes through the app's normal editing path, so web pages see input events; AXValue rewriting is
    the fallback. True when reading the element back shows the text."""
    before = value_of(ref)
    if replace and before:
        set_attr(ref, "AXSelectedTextRange", AS.AXValueCreate(AS.kAXValueCFRangeType, (0, _utf16_len(before))))
    if set_attr(ref, "AXSelectedText", text):
        after = value_of(ref)
        if text in after and (after != before or before == text):
            return True
    target = text if replace else before + text
    return set_attr(ref, "AXValue", target) and value_of(ref) == target


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2  # accessibility text ranges count UTF-16 units (an emoji is 2)


def select_all(ref) -> bool:
    """Select all text of a text element through accessibility, without cmd+a (a menu shortcut posted to an app in
    the background does nothing). True when reading the selection back confirms it."""
    n = _utf16_len(value_of(ref))
    if not set_attr(ref, "AXSelectedTextRange", AS.AXValueCreate(AS.kAXValueCFRangeType, (0, n))):
        return False
    got = _attr(ref, "AXSelectedTextRange")
    if got is None or not isinstance(got, AS.AXValueRef):
        return False
    ok, r = AS.AXValueGetValue(got, AS.kAXValueCFRangeType, None)
    return bool(ok) and (r[0], r[1]) == (0, n)


def pressable(ref, depth: int = 6):
    """The element itself or its nearest ancestor that supports AXPress."""
    node = ref
    for _ in range(depth):
        if node is None:
            return None
        if "AXPress" in _actions(node):
            return node
        node = _attr(node, "AXParent")
    return None


# AXMenuItemCmdModifiers: Command is implied unless bit 8 is set.
_MENU_MODS = {"shift": 1, "alt": 2, "ctrl": 4}


def menu_item_for_shortcut(pid: int, mods: list[str], key: str, budget_s: float = 1.5) -> tuple[Any, str] | None:
    """(menu item, "File > New Window") whose keyboard shortcut is mods+key, so a shortcut can be run through
    the app's menu while it is in the background. None when no menu item has that shortcut."""
    want = sum(v for k, v in _MENU_MODS.items() if k in mods) | (0 if "cmd" in mods else 8)
    char = key.upper() if len(key) == 1 else None
    if char is None:
        return None
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, 1.0)
    bar = _attr(app, "AXMenuBar")
    deadline = time.monotonic() + budget_s
    stack = [(c, []) for c in reversed(_children(bar))] if bar is not None else []
    while stack and time.monotonic() < deadline:
        node, path = stack.pop()
        a = _attrs(node, ["AXRole", "AXTitle", "AXMenuItemCmdChar", "AXMenuItemCmdModifiers", "AXEnabled"])
        title = _text(a["AXTitle"], 60)
        if a["AXRole"] == "AXMenuItem" and str(a["AXMenuItemCmdChar"] or "").upper() == char \
                and int(a["AXMenuItemCmdModifiers"] or 0) == want and a["AXEnabled"] is not False:
            return node, " > ".join(path + [title])
        for child in reversed(_children(node)):
            stack.append((child, path + [title] if title else path))
    return None


def scroll_by(x: float, y: float, direction: str, amount: int = 5) -> bool:
    """Scroll the scroll area under a point by moving its scroll bar: no wheel events, no pointer.
    amount is in wheel notches (~80 points of content each)."""
    node, _, _ = element_at(x, y)
    for _ in range(12):
        if node is None or _attr(node, "AXRole") == "AXScrollArea":
            break
        node = _attr(node, "AXParent")
    if node is None:
        return False
    vertical = direction in ("up", "down")
    bar = _attr(node, "AXVerticalScrollBar" if vertical else "AXHorizontalScrollBar")
    value = _attr(bar, "AXValue") if bar is not None else None
    if not isinstance(value, (int, float)):
        return False
    view = element_frame(node)
    contents = next((element_frame(c) for c in _children(node) if _attr(c, "AXRole") != "AXScrollBar"), None)
    if not view or not contents:
        return False
    hidden = (contents[3] - view[3]) if vertical else (contents[2] - view[2])
    if hidden <= 1:
        return False
    step = amount * 80 / hidden * (1 if direction in ("down", "right") else -1)
    return set_attr(bar, "AXValue", float(min(1.0, max(0.0, value + step))))

