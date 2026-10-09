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
    seen_frames: set[tuple[int, int, int, int]] = set()
    visited = 0
    truncated = False
    stack = [(r, screen) for r in reversed(_roots(app))]
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
            if key not in seen_frames:
                seen_frames.add(key)
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
                texts = [(t, off) for t, off in ((_text(a["AXTitle"], 120), 0), (_text(a["AXDescription"], 120), 0),
                                                  (_text(a["AXPlaceholderValue"], 120), 0),
                                                  (_text(a["AXHelp"], 120), -10)) if t]
                if role not in TEXT_ROLES and _text(a["AXValue"], 120):
                    texts.append((_text(a["AXValue"], 120), 0))  # e.g. the selected item of a pop-up
                if label and not texts:
                    texts.append((label, -3))  # from descendant text
                elements.append(Element(el, role, str(a["AXSubrole"] or ""), label, value, frame,
                                        a["AXEnabled"] is not False, a["AXFocused"] is True, tuple(texts)))
        if role in LEAF_ROLES:
            continue
        stack.extend((c, clip) for c in reversed(_children(el)))
    return elements + statics, truncated


def focused_summary(pid: int) -> str:
    """Where typed text would go right now, e.g. 'text field "Search" = "abc"'."""
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, 0.5)
    el = _attr(app, "AXFocusedUIElement")
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


def focused_is_text_input(pid: int) -> bool:
    app = AS.AXUIElementCreateApplication(pid)
    AS.AXUIElementSetMessagingTimeout(app, 0.5)
    el = _attr(app, "AXFocusedUIElement")
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


def menu_select(pid: int, path: list[str]) -> tuple[bool, str]:
    """Press a menu command by path, e.g. ["File", "Export", "PDF…"].

    A path that ends at a menu (not a command) lists that menu instead; an empty path lists the menu bar.
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
            inputs.key("escape", repeat=len(opened) + 1)


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


def related(a, b, depth: int = 8) -> bool:
    """True when a and b are the same element or one contains the other."""
    if a is None or b is None:
        return False
    for start, other in ((a, b), (b, a)):
        node = start
        for _ in range(depth):
            if node is None:
                break
            if node == other:
                return True
            node = _attr(node, "AXParent")
    return False
