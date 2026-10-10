import io
import json
from types import SimpleNamespace

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from cu_mcp import ax, effects as effects_mod, server, workspace


class FakeWorkspace:
    def __init__(self, app=(42, "Spotify")):
        self.active, self.app = True, app
        self.adopted = []
        self.stopped = False

    def windows_outside(self, pid):
        return set()

    def adopt(self, pid, name, only=None):
        self.adopted.append((pid, name, only))
        self.app = (pid, name)
        return 1

    def inside(self, frame):
        return frame[0] >= 2000

    def start(self):
        self.active = True

    def stop(self):
        self.stopped, self.active = True, False
        app, self.app = self.app, None
        return app


@pytest.fixture
def bg(monkeypatch):
    ws = FakeWorkspace()
    monkeypatch.setattr(server, "_ws", ws)
    monkeypatch.setattr(server, "_alive", lambda pid: True)
    monkeypatch.setattr(server, "_approve", lambda texts, action: None)
    monkeypatch.setattr(server.ax, "frontmost_app", lambda: (7, "Notes"))  # the user's app
    sent = []
    for name in ("click_to_pid", "key_to_pid", "type_to_pid", "scroll_to_pid"):
        monkeypatch.setattr(server.inputs, name, lambda *a, _n=name, **k: sent.append((_n, a)))
    for name in ("click", "key", "type_text", "scroll", "glide", "drag"):  # the real pointer/keyboard: never
        monkeypatch.setattr(server.inputs, name, lambda *a, _n=name, **k: pytest.fail(f"inputs.{_n} used"))
    server._state.pop("late", None)
    return ws, sent


def test_background_targets_the_app_on_the_hidden_display(bg):
    ws, _ = bg
    assert server._target() == (42, "Spotify")
    ws.app = None
    with pytest.raises(ToolError, match="open_app"):
        server._target()
    assert server._target(required=False) == (0, "")


def test_background_click_presses_the_control_under_the_point(bg, monkeypatch):
    ws, sent = bg
    pressed = []
    monkeypatch.setattr(server.ax, "element_at", lambda x, y: ("label", 'static text "Playlist"', ["Playlist"]))
    monkeypatch.setattr(server.ax, "pid_of", lambda ref: 42)
    monkeypatch.setattr(server.ax, "is_text_input", lambda ref: False)
    monkeypatch.setattr(server.ax, "pressable", lambda ref: "menu item")
    monkeypatch.setattr(server.ax, "press", lambda ref: pressed.append(ref) or True)
    assert "pressed" in server._bg_click(2100, 300, what="Playlist")
    assert pressed == ["menu item"] and sent == [] and server._state["late"] == server.BG_LATE

    monkeypatch.setattr(server.ax, "pressable", lambda ref: None)  # nothing pressable: post to the app only
    assert "sent to Spotify only" in server._bg_click(2100, 300)
    assert sent[-1][0] == "click_to_pid" and sent[-1][1][0] == 42

    monkeypatch.setattr(server.ax, "pid_of", lambda ref: 7)  # the point shows another app
    with pytest.raises(ToolError, match="not inside Spotify"):
        server._bg_click(2100, 300)


def test_background_typing_goes_into_the_field_and_return_through_accessibility(bg, monkeypatch):
    ws, sent = bg
    field = {"value": ""}
    monkeypatch.setattr(server.ax, "focused_element", lambda pid: "field")
    monkeypatch.setattr(server.ax, "is_text_input", lambda ref: True)
    monkeypatch.setattr(server.ax, "focused_input", lambda pid: (None, False))
    monkeypatch.setattr(server.ax, "insert_text",
                        lambda ref, text, replace=False: field.update(value=text) or True)
    monkeypatch.setattr(server.ax, "perform", lambda ref, action: action == "AXConfirm")
    msg = server._do_type("Paint It Black\n")
    assert field["value"] == "Paint It Black" and "background" in msg and "Return" in msg and sent == []

    monkeypatch.setattr(server.ax, "insert_text", lambda ref, text, replace=False: False)
    monkeypatch.setattr(server.ax, "value_of", lambda ref: "")
    with pytest.raises(ToolError, match="Show"):
        server._bg_type("abc")
    assert [n for n, _ in sent] == ["type_to_pid"]  # tried posting to the app before giving up


def test_background_shortcuts_run_the_menu_command(bg, monkeypatch):
    ws, sent = bg
    monkeypatch.setattr(server.ax, "menu_item_for_shortcut",
                        lambda pid, mods, key: ("item", "File > New Playlist") if (mods, key) == (["cmd"], "n") else None)
    monkeypatch.setattr(server.ax, "perform", lambda ref, action: True)
    assert "File > New Playlist" in server._do_key("cmd+n")
    monkeypatch.setattr(server.ax, "focused_element", lambda pid: None)
    monkeypatch.setattr(server.ax, "perform", lambda ref, action: False)
    assert "sent tab" in server._do_key("tab") and sent[-1][0] == "key_to_pid"


def test_background_refuses_pointer_moves_and_protected_apps(bg):
    ws, _ = bg
    with pytest.raises(ToolError, match="Show"):
        server._do_drag(1, 1, 5, 5)
    with pytest.raises(ToolError, match="pointer"):
        server._do_move(3, 3)
    ws.app = (9, "Terminal")
    with pytest.raises(ToolError, match="protected"):
        server._bg_type("rm -rf ~")


def test_button_toggles_background_mode(monkeypatch):
    ws = FakeWorkspace()
    ws.active, ws.app = False, None
    modes = []
    monkeypatch.setattr(server, "_ws", ws)
    monkeypatch.setattr(server, "_alive", lambda pid: True)
    monkeypatch.setattr(server, "_activate", lambda pid: None)
    monkeypatch.setattr(server.effects, "set_mode", lambda background, note="": modes.append((background, note)))
    monkeypatch.setattr(server.ax, "frontmost_app", lambda: (42, "Spotify"))
    monkeypatch.setattr(server.ax, "windows", lambda: [])
    server._state["work_app"] = (42, "Spotify", 0.0)
    server._toggle_background()
    assert ws.active and ws.adopted == [(42, "Spotify", None)] and modes[-1] == (True, "")
    assert "background mode" in server._state.pop("note")
    with pytest.raises(ToolError, match="Take a screenshot first"):  # old screenshot, new layout
        server._to_points(100, 100)
    server._toggle_background()
    assert ws.stopped and modes[-1] == (False, "") and "back to the front" in server._state.pop("note")
    server._state["stale_coords"] = False

    def broken():
        raise workspace.WorkspaceError("no virtual display")

    ws2 = FakeWorkspace()
    ws2.active, ws2.app, ws2.start = False, None, broken
    monkeypatch.setattr(server, "_ws", ws2)
    server._toggle_background()
    assert modes[-1][0] is False and "no virtual display" in modes[-1][1]


def test_overlay_click_reaches_the_server_callback(monkeypatch):
    fx = effects_mod.Effects()
    got = []
    fx.on_toggle = lambda: got.append("toggle")

    class Proc:
        stdout = io.StringIO(json.dumps({"event": "toggle"}) + "\nnot json\n")

    monkeypatch.setattr(effects_mod.threading, "Thread",
                        lambda target, daemon=True, args=(): type("T", (), {"start": lambda self: target(*args)})())
    fx._read_events(Proc())
    assert got == ["toggle"]
    fx.background = True
    fx._send = lambda msg: pytest.fail("no pointer effects in background mode")
    fx.point(), fx.click(1, 2), fx.pulse()


def test_insert_text_prefers_selected_text_and_checks_the_result(monkeypatch):
    state = {"value": "abc", "calls": []}

    def set_attr(ref, name, value):
        state["calls"].append(name)
        if name == "AXSelectedText":
            state["value"] += value
        elif name == "AXValue":
            state["value"] = value
        return True

    monkeypatch.setattr(ax, "set_attr", set_attr)
    monkeypatch.setattr(ax, "value_of", lambda ref: state["value"])
    assert ax.insert_text("f", "def") and state["value"] == "abcdef" and state["calls"] == ["AXSelectedText"]
    monkeypatch.setattr(ax, "set_attr", lambda ref, name, value: name == "AXValue" and state.update(value=value) is None)
    assert ax.insert_text("f", "xyz", replace=True) and state["value"] == "xyz"


def test_menu_shortcut_lookup_matches_modifier_bits(monkeypatch):
    tree = {"app": ["bar"], "bar": ["File"], "File": ["menu"], "menu": ["New", "NewWin"]}
    attrs = {
        "File": {"AXRole": "AXMenuBarItem", "AXTitle": "File"},
        "menu": {"AXRole": "AXMenu", "AXTitle": ""},
        "New": {"AXRole": "AXMenuItem", "AXTitle": "New Playlist", "AXMenuItemCmdChar": "N",
                "AXMenuItemCmdModifiers": 0, "AXEnabled": True},
        "NewWin": {"AXRole": "AXMenuItem", "AXTitle": "New Window", "AXMenuItemCmdChar": "N",
                   "AXMenuItemCmdModifiers": 1, "AXEnabled": True},
    }
    monkeypatch.setattr(ax.AS, "AXUIElementCreateApplication", lambda pid: "app")
    monkeypatch.setattr(ax.AS, "AXUIElementSetMessagingTimeout", lambda el, t: 0)
    monkeypatch.setattr(ax, "_attr", lambda el, name: "bar" if name == "AXMenuBar" else None)
    monkeypatch.setattr(ax, "_children", lambda el: tree.get(el, []))
    monkeypatch.setattr(ax, "_attrs", lambda el, names: {n: attrs.get(el, {}).get(n) for n in names})
    assert ax.menu_item_for_shortcut(1, ["cmd"], "n") == ("New", "File > New Playlist")
    assert ax.menu_item_for_shortcut(1, ["cmd", "shift"], "n") == ("NewWin", "File > New Window")
    assert ax.menu_item_for_shortcut(1, ["ctrl"], "n") is None


def test_workspace_moves_windows_in_and_puts_them_back(monkeypatch):
    ws = workspace.Workspace()
    ws.display, ws.display_id = object(), 4
    monkeypatch.setattr(ws, "bounds", lambda: (1470.0, 956.0, 1470.0, 956.0))
    frames = {"w1": (100.0, 50.0, 800.0, 600.0), "w2": (200.0, 80.0, 500.0, 400.0)}
    monkeypatch.setattr(workspace.ax, "window_refs", lambda pid: [(r, f, "") for r, f in frames.items()])
    monkeypatch.setattr(workspace.ax, "windows",
                        lambda: [{"pid": 42, "number": 7, "frame": frames["w2"]}])
    monkeypatch.setattr(workspace.ax, "pid_of", lambda ref: 42)
    monkeypatch.setattr(workspace.ax, "set_window_frame",
                        lambda ref, x, y, w=None, h=None: frames.__setitem__(ref, (x, y, w or frames[ref][2],
                                                                                    h or frames[ref][3])) or True)
    monkeypatch.setattr(workspace.screen, "set_work_display", lambda did: None)
    assert ws.adopt(42, "Spotify", only={7}) == 1  # only the new window
    assert ws.inside(frames["w2"]) and not ws.inside(frames["w1"])
    assert ws.adopt(42, "Spotify") == 1 and ws.inside(frames["w1"])
    assert ws.stop() == (42, "Spotify") and not ws.active
    assert frames == {"w1": (100.0, 50.0, 800.0, 600.0), "w2": (200.0, 80.0, 500.0, 400.0)}


def test_button_leaves_with_the_glow_and_shortly_after_the_user_takes_the_mouse():
    from cu_mcp.overlay import BUTTON_GRACE, IDLE_HIDE, button_wanted

    assert button_wanted(False, True, False, 1.0, 99, False)  # agent working, glow on
    assert button_wanted(True, False, False, 999, 999, False)  # background mode: always
    assert button_wanted(False, False, False, 3.0, 1.0, False)  # just took the mouse: still reachable
    assert not button_wanted(False, False, False, 6.0, BUTTON_GRACE + 1, False)  # took it a while ago: gone
    assert button_wanted(False, False, False, 6.0, BUTTON_GRACE + 1, True)  # unless heading for the button
    assert not button_wanted(False, False, False, IDLE_HIDE + 1, 1.0, True)  # session over: gone with the glow


def test_focus_hint_stands_in_when_a_background_app_reports_no_focus(monkeypatch):
    attrs = {("app", "AXFocusedUIElement"): None, ("field", "AXFocused"): True}
    monkeypatch.setattr(ax.AS, "AXUIElementCreateApplication", lambda pid: "app")
    monkeypatch.setattr(ax.AS, "AXUIElementSetMessagingTimeout", lambda el, t: 0)
    monkeypatch.setattr(ax, "_attr", lambda el, name: attrs.get((el, name)))
    monkeypatch.setattr(ax, "_focus_hint", {})
    assert ax.focused_element(42) is None
    ax.note_focus(42, "field")
    assert ax.focused_element(42) == "field" and ax.focused_element(7) is None
    attrs[("field", "AXFocused")] = False  # the focus moved on
    assert ax.focused_element(42) is None
    attrs[("app", "AXFocusedUIElement")] = "other"
    assert ax.focused_element(42) == "other"


def test_background_click_on_a_field_remembers_it_as_focused(bg, monkeypatch):
    el = ax.Element("field", "AXTextField", "", "Name", "My Playlist #47", (2100, 300, 200, 30), True, False, ())
    monkeypatch.setitem(server._state, "elements", {72: el})
    monkeypatch.setattr(server.ax, "is_text_input", lambda ref: True)
    monkeypatch.setattr(server.ax, "set_attr", lambda ref, name, value: name == "AXFocused")
    monkeypatch.setattr(ax, "_focus_hint", {})
    assert "focused [72]" in server._do_click_element(72)
    assert ax._focus_hint == {42: "field"}


def test_background_replace_selects_with_caret_keys_not_cmd_a(bg, monkeypatch):
    ws, sent = bg
    field = {"value": "My Playlist #47"}
    monkeypatch.setattr(server.ax, "focused_element", lambda pid: "field")
    monkeypatch.setattr(server.ax, "is_text_input", lambda ref: True)
    monkeypatch.setattr(server.ax, "focused_input", lambda pid: (None, False))
    monkeypatch.setattr(server.ax, "insert_text", lambda ref, text, replace=False: False)
    monkeypatch.setattr(server.ax, "select_all", lambda ref: False)
    monkeypatch.setattr(server.ax, "value_of", lambda ref: field["value"])
    monkeypatch.setattr(server.inputs, "type_to_pid", lambda pid, text: field.update(value=text))
    assert "replaced the field with 9 chars" in server._type_into_focus("Pop 2000s")
    # cmd+a posted to a background Safari web app did nothing; caret moves did work
    assert [a[1] for n, a in sent] == ["cmd+up", "cmd+left", "shift+cmd+down", "shift+cmd+right"]
    monkeypatch.setattr(server.inputs, "type_to_pid", lambda pid, text: field.update(value="M" + text))
    assert 'now holds "MPop 2000s"' in server._type_into_focus("Pop 2000s")


def test_background_cmd_a_selects_the_focused_field_through_accessibility(bg, monkeypatch):
    ws, sent = bg
    selected = []
    monkeypatch.setattr(server.ax, "focused_element", lambda pid: "field")
    monkeypatch.setattr(server.ax, "is_text_input", lambda ref: True)
    monkeypatch.setattr(server.ax, "select_all", lambda ref: selected.append(ref) or True)
    monkeypatch.setattr(server.ax, "menu_item_for_shortcut", lambda *a, **k: pytest.fail("no menu lookup"))
    assert "through accessibility" in server._do_key("cmd+a") and selected == ["field"] and sent == []


def test_cover_names_what_sits_on_an_accessibility_match(monkeypatch):
    monkeypatch.setattr(server.ax, "is_trusted", lambda: True)
    monkeypatch.setitem(server._state, "elements", {5: SimpleNamespace(ref="link")})
    monkeypatch.setattr(server.screen, "geometry", lambda: None)
    t = server.Target(90, (400, 800), (380, 790, 40, 20), 'link "Kelly Clarkson"', 5)
    monkeypatch.setattr(server, "_reach", lambda geo, x, y, ref, describe=True: (False, 'group "Now playing bar"'))
    assert server._cover(t) == 'group "Now playing bar"'
    monkeypatch.setattr(server, "_reach", lambda geo, x, y, ref, describe=True: (True, 'link "Kelly Clarkson"'))
    assert server._cover(t) == ""
    assert server._cover(server.Target(80, (1, 1), (0, 0, 2, 2), 'text "x" [OCR]')) == ""


def test_reach_tells_a_scrolled_out_element_from_one_under_the_point(monkeypatch):
    parents = {"label": "button", "button": "row", "row": "list", "list": "window", "header": "window"}
    roles = {"label": "AXStaticText", "button": "AXButton", "row": "AXRow", "list": "AXGroup", "window": "AXWindow",
             "header": "AXGroup"}
    at = {"hit": "label"}
    monkeypatch.setattr(ax.AS, "AXUIElementCreateSystemWide", lambda: "sw")
    monkeypatch.setattr(ax.AS, "AXUIElementSetMessagingTimeout", lambda el, t: 0)
    monkeypatch.setattr(ax.AS, "AXUIElementCopyElementAtPosition", lambda sw, x, y, _: (0, at["hit"]))
    monkeypatch.setattr(ax, "_attr", lambda el, name: parents.get(el) if name == "AXParent" else
                        roles.get(el) if name == "AXRole" else None)
    monkeypatch.setattr(ax, "element_at", lambda x, y: (at["hit"], f'thing "{at["hit"]}"', []))
    assert ax.reach(1, 2, "button") == (True, 'thing "label"')  # the label inside the button
    at["hit"] = "button"
    assert ax.reach(1, 2, "label")[0] is True  # the button wrapping the label
    at["hit"] = "list"
    assert ax.reach(1, 2, "button") == (False, 'thing "list"')  # only its list: scrolled out of the visible part
    at["hit"] = "header"
    assert ax.reach(1, 2, "button")[0] is False  # a header on top


def test_row_context_reads_the_texts_of_the_row_around_a_control(monkeypatch):
    tree = {"list": ["row"], "row": ["cell", "wrap"], "cell": ["title", "artist"], "wrap": ["btn"], "btn": ["btntext"]}
    parent = {c: p for p, cs in tree.items() for c in cs}
    attrs = {"list": {"AXRole": "AXGroup", "AXPosition": (0, 0, 700, 600)},
             "row": {"AXRole": "AXGroup", "AXPosition": (0, 100, 700, 56)},
             "cell": {"AXRole": "AXGroup", "AXPosition": (10, 100, 400, 56)},
             "wrap": {"AXRole": "AXGroup", "AXPosition": (600, 110, 80, 32)},
             "btn": {"AXRole": "AXButton"}, "btntext": {"AXRole": "AXStaticText", "AXValue": "Add"},
             "title": {"AXRole": "AXStaticText", "AXValue": "Peggy Sue"},
             "artist": {"AXRole": "AXStaticText", "AXValue": "Buddy Holly, The Crickets"}}
    monkeypatch.setattr(ax, "_attr", lambda el, name: parent.get(el) if name == "AXParent" else
                        tree.get(el) if name == "AXChildren" else attrs.get(el, {}).get(name))
    monkeypatch.setattr(ax, "_attrs", lambda el, names: {n: attrs.get(el, {}).get(n) for n in names})
    monkeypatch.setattr(ax, "_frame", lambda pos, size: pos)
    assert ax.row_context("btn", "Add") == "Peggy Sue · Buddy Holly, The Crickets"


def test_scroll_to_reveals_a_match_under_a_bar_through_accessibility(monkeypatch):
    t = server.Target(90, (400, 800), (380, 790, 40, 20), 'link "Kelly Clarkson"', 5)
    covers = ['group "Now playing bar"', ""]
    performed = []
    monkeypatch.setattr(server, "_front_window_center", lambda: (0, 0))
    monkeypatch.setattr(server, "_shown", lambda text: t)
    monkeypatch.setattr(server, "_cover", lambda target: covers.pop(0) if covers else "")
    monkeypatch.setattr(server, "_settle", lambda: None)
    monkeypatch.setattr(server.ax, "perform", lambda ref, action: performed.append((ref, action)) or True)
    monkeypatch.setitem(server._state, "elements", {5: SimpleNamespace(ref="link")})
    assert "scrolled it into view" in server._do_scroll_to("Kelly Clarkson")
    assert performed == [("link", "AXScrollToVisible")]
    covers[:] = ["the player bar", "the player bar"]  # cannot be revealed: say so instead of scrolling away
    monkeypatch.setattr(server.ax, "perform", lambda ref, action: False)
    assert "hidden (the player bar is in the way" in server._do_scroll_to("Kelly Clarkson")
