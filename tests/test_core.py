import pytest

from cu_mcp import inputs, screen


def test_fit_size_downscales_long_edge_and_never_upscales():
    assert screen.fit_size(1470, 956, 1280) == (1280, 832)
    assert screen.fit_size(1280, 800, 1280) == (1280, 800)
    assert screen.fit_size(1024, 768, 1280) == (1024, 768)
    assert screen.fit_size(956, 1470, 1280) == (832, 1280)


def test_geometry_round_trip():
    geo = screen.Geometry(0, 0, 1470, 956, 1280, 832)
    px, py = geo.to_points(640, 416)
    assert (round(px), round(py)) == (735, 478)
    assert geo.to_image(px, py) == (640, 416)


def test_geometry_clamps_and_offsets():
    geo = screen.Geometry(-1920, 0, 1920, 1080, 1280, 720)
    assert geo.to_points(-50, 99999) == (-1920, 719 / geo.scale)
    assert geo.contains_point(-1, 5) and not geo.contains_point(0, 5)


@pytest.mark.parametrize("combo, mods, code", [
    ("cmd+c", ["cmd"], 8),
    ("Cmd+Shift+T", ["cmd", "shift"], 17),
    ("command+option+escape", ["cmd", "alt"], 53),
    ("return", [], 36),
    ("ctrl+left", ["ctrl"], 123),
    ("cmd++", ["cmd"], 24),
    ("cmd+-", ["cmd"], 27),
    ("pgdn", [], 121),
])
def test_parse_combo(combo, mods, code):
    assert inputs.parse_combo(combo) == (mods, code)


def test_parse_combo_rejects_unknown():
    with pytest.raises(ValueError):
        inputs.parse_combo("cmd+banana")
    with pytest.raises(ValueError):
        inputs.parse_modifiers(["hyper"])


def test_parse_modifiers_accepts_strings_and_dedupes():
    assert inputs.parse_modifiers("cmd+shift") == ["cmd", "shift"]
    assert inputs.parse_modifiers(["Option", "alt"]) == ["alt"]
    assert inputs.parse_modifiers(None) == []


def test_overlays_keep_size():
    from PIL import Image
    im = Image.new("RGB", (400, 300), "white")
    assert screen.draw_grid(im).size == (400, 300)
    assert screen.draw_marks(im, [(1, (10, 10, 60, 40)), (12, (100, 0, 150, 20))]).size == (400, 300)


def test_find_by_label_prefers_exact_then_prefix_then_substring():
    from cu_mcp import ax
    labels = {1: "Save As…", 2: "Save", 3: "Autosave", 4: ""}
    assert ax.find_by_label(labels, "save") == ([2], "exact")
    assert ax.find_by_label(labels, "Save as...") == ([1], "exact")
    assert ax.find_by_label({1: "Pick on map", 2: "Map"}, "pick") == ([1], "prefix")
    assert ax.find_by_label(labels, "auto") == ([3], "prefix")
    assert ax.find_by_label({1: "Open in new tab", 2: "Open"}, "new tab") == ([1], "substring")
    assert ax.find_by_label(labels, "missing") == ([], "")
    assert ax.find_by_label(labels, "  ") == ([], "")


def test_find_by_label_reports_ambiguity():
    from cu_mcp import ax
    hits, how = ax.find_by_label({1: "OK", 2: "Cancel", 3: "ok"}, "OK")
    assert hits == [1, 3] and how == "exact"


def test_normalize_ignores_case_punctuation_glyphs_and_accents():
    from cu_mcp import ax
    assert ax.normalize("Save As…") == "save as"
    assert ax.normalize("› · Metadata · location") == "metadata location"
    assert ax.normalize("Wyślij wiadomość") == "wyslij wiadomosc"
    assert ax.normalize("Łódź") == "lodz"


def _el(role, *texts, enabled=True):
    from cu_mcp import ax
    return ax.Element(None, role, "", texts[0] if texts else "", "", (0, 0, 10, 10), enabled, False,
                      tuple((t, 0) for t in texts))


def test_rank_reproduces_the_conpress_cases():
    from cu_mcp import ax
    # "Metadata": the section's disclosure triangle beats the "Keep metadata" checkbox
    els = {17: _el("AXCheckBox", "Keep metadata"), 37: _el("AXGroup", "› · Metadata · location · time"),
           38: _el("AXDisclosureTriangle", "› Metadata location · time")}
    ranked = ax.rank("Metadata", els)
    assert ranked[0][1] == 38 and ranked[0][0] - ranked[-1][0] > 15
    # "Choose...": the pop-up whose current value is "Choose…" beats fields that merely contain the word
    els = {26: _el("AXTextField", "Cover music Choose Clear"), 28: _el("AXButton", "Cover music Choose"),
           29: _el("AXPopUpButton", "Country", "Choose…")}
    assert ax.rank("Choose...", els)[0][1] == 29
    # fill(): a text field wins over a button with the same label
    els = {1: _el("AXButton", "Search"), 2: _el("AXTextField", "Search")}
    assert ax.rank("Search", els, prefer_inputs=True)[0][1] == 2
    # disabled controls sink
    els = {1: _el("AXButton", "Save", enabled=False), 2: _el("AXButton", "Save as")}
    assert ax.rank("Save", els)[0][1] == 2


def test_dedupe_and_pick():
    from mcp.server.mcpserver.exceptions import ToolError
    from cu_mcp.server import Target, _dedupe, _pick
    row = Target(101, (300, 200), (100, 190, 400, 20), "row")
    child_text = Target(96, (180, 200), (150, 192, 60, 16), "text in row")
    other = Target(70, (300, 500), (250, 490, 100, 20), "elsewhere")
    kept = _dedupe([row, child_text, other])
    assert [t.desc for t in kept] == ["row", "elsewhere"]
    assert _pick(kept, "x", "App").desc == "row"
    with pytest.raises(ToolError, match="Several things match"):
        _pick([Target(100, (10, 10), (0, 0, 20, 20), "a"), Target(98, (400, 400), (390, 390, 20, 20), "b")], "x", "App")
    with pytest.raises(ToolError, match="Nothing matching"):
        _pick([], "x", "App")


def test_split_url_and_combo_canonical_form():
    from cu_mcp.server import _canonical_combo, _split_url
    assert _split_url("github.com") == ("https://github.com", "https")
    assert _split_url("localhost:3000/x") == ("http://localhost:3000/x", "http")
    assert _split_url("spotify:search:take on me") == ("spotify:search:take%20on%20me", "spotify")
    assert _split_url("tel:123")[1] == "tel"
    assert _split_url("facetime:12345")[1] == "facetime"
    assert _split_url("127.0.0.1:8000")[1] == "http"
    assert _canonical_combo("Shift+Command+Tab") == "cmd+shift+tab"
    assert _canonical_combo("cmd+space") == "cmd+space"


def test_pick_near_prefers_the_match_closest_to_the_anchor(monkeypatch):
    from cu_mcp import server
    from cu_mcp.server import Target
    adds = [Target(100, (900, 200), (880, 190, 40, 20), "add row 1"),
            Target(100, (900, 400), (880, 390, 40, 20), "add row 2"),
            Target(60, (100, 405), (90, 395, 20, 20), "weak match next to the anchor")]
    anchor = [Target(100, (300, 400), (250, 390, 100, 20), "Take on Me")]
    monkeypatch.setattr(server, "_candidates", lambda q, reuse=False, **k: (anchor, "Spotify"))
    assert server._pick_near(adds, "Add", "Spotify", "Take on Me").desc == "add row 2"


def test_recipe_templating_round_trip(tmp_path, monkeypatch):
    from cu_mcp import recipes
    monkeypatch.setattr(recipes, "DIR", str(tmp_path))
    steps = [{"action": "open_url", "url": "spotify:search:Take On Me"},
             {"action": "wait_for", "text": "Songs"},
             {"action": "click_text", "text": "Take On Me", "near": "a-ha"},
             {"action": "click_text", "text": "Add to Liked Songs"}]
    r = recipes.save("Spotify like song!", "Like a song on Spotify", steps, {"song": "Take On Me"})
    assert r["name"] == "spotify-like-song" and r["params"] == ["song"]
    assert r["steps"][0]["url"] == "spotify:search:{{song}}"
    again = recipes.render(recipes.load("spotify-like-song")["steps"], {"song": "Hey Jude"})
    assert again[2]["text"] == "Hey Jude" and again[3]["text"] == "Add to Liked Songs"
    assert "spotify-like-song(song)" in recipes.summary()
    with pytest.raises(ValueError, match="missing parameter"):
        recipes.render(r["steps"], {})
    with pytest.raises(ValueError, match="not found in any step"):
        recipes.save("x", "y", steps, {"artist": "Queen"})
    with pytest.raises(ValueError, match="not replayable"):
        recipes.save("x", "y", [{"action": "screenshot"}])


def test_recorder_starts_a_new_task_after_a_pause(monkeypatch):
    from cu_mcp import recipes
    rec = recipes.Recorder()
    clock = [1000.0]
    monkeypatch.setattr(recipes.time, "monotonic", lambda: clock[0])
    rec.touch(); rec.record("open_app", name="Notes"); rec.record("zoom", x0=1)
    assert [s["action"] for s in rec.steps] == ["open_app"]
    clock[0] += recipes.TASK_GAP + 1
    rec.touch()
    assert rec.steps == []


def test_risky_words_match_whole_words_in_both_languages():
    from cu_mcp import safety
    assert safety.risky_word(["Wyślij wiadomość"]) == "wyslij"
    assert safety.risky_word(["", "Kup teraz"]) == "kup"
    assert safety.risky_word(["Move to Trash"]) == "move to trash"
    assert safety.risky_word(["Sending options", "Postaw kawę", "Deleted items"]) is None


def test_app_action_library_validates_and_passes_args_as_data():
    from cu_mcp import apps
    script, argv, name = apps.resolve("Notes", "create_note", {"title": 'a "b"', "body": "c"})
    assert name == "Notes" and argv == ['a "b"', "c"] and 'a "b"' not in script
    with pytest.raises(ValueError, match="needs title, body"):
        apps.resolve("notes", "create_note", {"title": "x"})
    with pytest.raises(ValueError, match="no actions"):
        apps.resolve("terminal", "run", [])
    assert "spotify.play_uri(uri)" in apps.catalog()


def test_zoom_grid_labels_full_screenshot_coordinates():
    from PIL import Image
    from cu_mcp import screen
    assert screen.nice_step(400) == 50 and screen.nice_step(60) == 10
    im = Image.new("RGB", (1280, 640), "white")
    assert screen.draw_grid_mapped(im, 600, 300, 700, 350).size == (1280, 640)


def test_click_element_is_recorded_as_click_text(monkeypatch):
    from cu_mcp import ax, server
    el = ax.Element(None, "AXButton", "", "Save", "", (100, 100, 40, 20), True, False, (("Save", 0),))
    monkeypatch.setitem(server._state, "elements", {7: el})
    server._recorder.steps = []
    server._record("click_element", {"id": 7, "button": "left", "count": 1})
    assert server._recorder.steps == [{"action": "click_text", "text": "Save", "button": "left", "count": 1}]


def test_tools_whose_argument_is_called_name_reach_the_action(monkeypatch):
    from cu_mcp import server

    opened = []
    monkeypatch.setattr(server, "_guard", lambda **k: None)
    monkeypatch.setattr(server, "_done", lambda note: note)
    monkeypatch.setattr(server, "_log_call", lambda *a: None)
    monkeypatch.setattr(server.effects, "active", lambda: None)
    monkeypatch.setattr(server, "_do_open_app", lambda name: opened.append(name) or f"opened {name}")
    server._recorder.steps = []
    assert server.open_app(name="Spotify") == "opened Spotify" and opened == ["Spotify"]
    assert server._recorder.steps[-1] == {"action": "open_app", "name": "Spotify"}


def test_open_url_explains_links_no_app_handles(monkeypatch):
    import subprocess

    from mcp.server.mcpserver.exceptions import ToolError
    from cu_mcp import server

    failed = subprocess.CompletedProcess([], 1, "", "No application knows how to open URL spotify: (Code=-10814)")
    monkeypatch.setattr(server.subprocess, "run", lambda *a, **k: failed)
    with pytest.raises(ToolError, match="No app on this Mac opens spotify: links"):
        server._do_open_url("spotify:search:the doors")


def test_app_actions_refuse_browser_web_apps(monkeypatch):
    from cu_mcp import apps

    monkeypatch.setattr(apps, "web_app", lambda name: name == "Spotify")
    with pytest.raises(RuntimeError, match="web app"):
        apps.run("spotify", "play")


def test_settle_waits_for_a_quiet_period_not_one_still_frame(monkeypatch):
    from cu_mcp import server

    clock = [0.0]
    monkeypatch.setattr(server.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    # still for 2 samples, then the page changes once more (a menu closed, then the new page loaded), then still
    frames = iter([0, 0, 0, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 1])
    monkeypatch.setattr(server.screen, "live_thumb", lambda: next(frames))
    monkeypatch.setattr(server.screen, "changed_fraction", lambda a, b: float(a != b))
    server._settle()
    # the late change at sample 3 is caught, and the call returns once the screen stayed still for SETTLE_QUIET
    assert server.SETTLE_MIN + 3 * 0.08 + server.SETTLE_QUIET <= clock[0] < server.SETTLE_MIN + server.SETTLE_MAX


def test_settle_after_an_accessibility_press_waits_for_the_late_effect(monkeypatch):
    from cu_mcp import server

    clock = [0.0]
    monkeypatch.setattr(server.time, "sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(server.screen, "changed_fraction", lambda a, b: float(a != b))
    # the menu closes (frame 1), the screen stays still, then ~1 s later the new playlist page shows up (frame 2)
    late = lambda: 1 if clock[0] < 1.5 else 2
    monkeypatch.setattr(server.screen, "live_thumb", late)
    server._state["late"] = server.SETTLE_LATE
    server._settle()
    assert "late" not in server._state
    assert 1.5 <= clock[0] < 1.5 + server.SETTLE_MAX  # waited for frame 2 and settled on it

    clock[0] = 0.0
    monkeypatch.setattr(server.screen, "live_thumb", lambda: 1)
    server._settle()  # no press: returns after the quiet period, no extra wait
    assert clock[0] < 1.0


def _fake_ax_tree(monkeypatch, root):
    """Run ax.snapshot over plain dicts instead of live accessibility elements."""
    from cu_mcp import ax

    monkeypatch.setattr(ax, "_roots", lambda app: [root])
    monkeypatch.setattr(ax, "_attrs", lambda el, names: {n: el.get(n) for n in names})
    monkeypatch.setattr(ax, "_attr", lambda el, n: el.get(n))
    monkeypatch.setattr(ax, "_children", lambda el: el.get("children", []))
    monkeypatch.setattr(ax, "_actions", lambda el: el.get("actions", []))
    monkeypatch.setattr(ax, "_frame", lambda p, s: (*p, *s) if p and s else None)
    monkeypatch.setattr(ax.AS, "AXUIElementCreateApplication", lambda pid: {})
    monkeypatch.setattr(ax.AS, "AXUIElementSetMessagingTimeout", lambda el, t: 0)
    ax._ax_enabled.add(4242)
    return ax


def test_snapshot_keeps_a_search_field_over_its_same_size_wrapper(monkeypatch):
    # Spotify's "Search for songs or episodes": a pressable group and the field inside share one frame; once a
    # song title is typed the group's text is the title, so only the field still carries the placeholder.
    field = {"AXRole": "AXTextField", "AXSubrole": "AXSearchField", "AXValue": "Come Together",
             "AXPlaceholderValue": "Search for songs or episodes", "AXPosition": (300, 400), "AXSize": (280, 32)}
    text = {"AXRole": "AXStaticText", "AXValue": "Come Together", "AXPosition": (310, 405), "AXSize": (100, 20)}
    group = {"AXRole": "AXGroup", "actions": ["AXPress"], "AXPosition": (300, 400), "AXSize": (280, 32),
             "children": [field, text]}
    window = {"AXRole": "AXWindow", "AXPosition": (0, 0), "AXSize": (1470, 956), "children": [group]}
    ax = _fake_ax_tree(monkeypatch, window)
    els, _ = ax.snapshot(4242, (0, 0, 1470, 956), include_text=True)
    controls = [e for e in els if e.role != "AXStaticText"]
    assert [(e.role, e.subrole) for e in controls] == [("AXTextField", "AXSearchField")]
    elements = dict(enumerate(els, 1))
    assert elements[ax.rank("Search for songs or episodes", elements, prefer_inputs=True)[0][1]] is controls[0]


def test_fill_target_can_name_what_a_field_currently_says():
    from cu_mcp import ax

    field = ax.Element(None, "AXTextField", "", "Name", "My Playlist #45", (500, 300, 300, 30), True, False,
                       ax.match_texts({"AXTitle": "Name", "AXValue": "My Playlist #45"}, "AXTextField", "Name"))
    row = ax.Element(None, "AXRow", "", "My Playlist #45 Playlist • Kacper", "", (10, 380, 230, 50), True, False,
                     ax.match_texts({}, "AXRow", "My Playlist #45 Playlist • Kacper"))
    title = ax.Element(None, "AXStaticText", "", "My Playlist #45", "", (390, 140, 300, 60), True, False,
                       (("My Playlist #45", 0),))
    elements = {1: row, 2: title, 3: field}
    (top, i), (second, _) = ax.rank("My Playlist #45", elements, prefer_inputs=True)[:2]
    assert i == 3 and top - second > 6  # clear winner, not "several things match"
    plain = {i: sc for sc, i in ax.rank("My Playlist #45", elements)}
    assert max(plain, key=plain.get) == 2 and plain[3] < plain[2] - 6  # click_text: the title, and no new ambiguity


def test_near_picks_the_button_in_the_row_of_the_named_text(monkeypatch):
    # Spotify playlist page: "+ Add" in the header, "Paranoid · Black Sabbath" already in the playlist, and
    # search results below, each row with its own "Add". near="Black Sabbath" must not take the header button
    # just because the playlist row is the first "Black Sabbath" on screen.
    from mcp.server.mcpserver.exceptions import ToolError

    from cu_mcp import server
    from cu_mcp.server import Target

    T = lambda score, x, y, desc, h=20: Target(score, (x, y), (x - 20, y - h / 2, 40, h), desc)
    adds = [T(108, 360, 254, "header + Add"), T(94, 912, 565, "Add row Iron Man"), T(94, 912, 621, "Add row War Pigs")]
    anchors = [T(100, 420, 300, "playlist: Black Sabbath"), T(100, 420, 574, "result: Iron Man · Black Sabbath"),
               T(100, 420, 630, "result: War Pigs · Black Sabbath"), T(94, 499, 470, "typed: Black Sabbath")]
    monkeypatch.setattr(server, "_candidates", lambda q, reuse=False, **k: (anchors, "Spotify"))
    monkeypatch.setattr(server, "_focused_field", lambda: (330, 455, 340, 32))  # the search field holding the query
    monkeypatch.setitem(server._state, "elements", {})
    # Iron Man and War Pigs both show "Black Sabbath": guessing could add the wrong song, so hand the choice back
    with pytest.raises(ToolError, match="2 rows") as e:
        server._pick_near(adds, "Add", "Spotify", "Black Sabbath")
    assert "Add row Iron Man" in str(e.value) and "Add row War Pigs" in str(e.value) and "header" not in str(e.value)
    monkeypatch.setattr(server, "_candidates", lambda q, reuse=False, **k: ([anchors[1]], "Spotify"))
    assert server._pick_near(adds, "Add", "Spotify", "Iron Man").desc == "Add row Iron Man"


def test_near_leaves_out_rows_hidden_under_a_header(monkeypatch):
    # A row scrolled under a sticky header is still in the accessibility tree; the model cannot see it, so its
    # text and its button must not compete with the visible row (any app with a scrolling list).
    from cu_mcp import server
    from cu_mcp.server import Target

    hidden_btn = Target(94, (858, 100), (838, 90, 40, 20), "[71] row menu button", 71)
    shown_btn = Target(94, (913, 500), (893, 490, 40, 20), "[117] button \"Add\"", 117)
    hidden_anchor = Target(100, (600, 100), (500, 90, 200, 20), "old row: Golden Records", 5)
    shown_anchor = Target(100, (600, 500), (500, 490, 200, 20), "result: Golden Records", 6)
    monkeypatch.setattr(server, "_candidates", lambda q, reuse=False, **k: ([hidden_anchor, shown_anchor], "App"))
    monkeypatch.setattr(server, "_focused_field", lambda: None)
    monkeypatch.setattr(server, "_cover", lambda t: 'group "Top bar"' if t.el_id in (71, 5) else "")
    monkeypatch.setattr(server, "_row_text", lambda t: "")
    monkeypatch.setitem(server._state, "elements", {})
    assert server._pick_near([hidden_btn, shown_btn], "Add", "App", "Golden Records").el_id == 117


def test_pick_skips_hidden_duplicates_and_lists_rows_when_unsure(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from cu_mcp import server
    from cu_mcp.server import Target

    a = Target(100, (900, 60), (880, 50, 40, 20), '[71] button "Add"', 71)
    b = Target(100, (900, 400), (880, 390, 40, 20), '[90] button "Add"', 90)
    monkeypatch.setattr(server, "_cover", lambda t: 'group "Top bar"' if t.el_id == 71 else "")
    assert server._pick([a, b], "Add", "App") is b
    monkeypatch.setattr(server, "_cover", lambda t: "")
    monkeypatch.setattr(server, "_row_text", lambda t: {71: "Jailhouse Rock · Elvis", 90: "Peggy Sue · Buddy Holly"}[t.el_id])
    with pytest.raises(ToolError) as e:
        server._pick([a, b], "Add", "App")
    assert '[90] button "Add" in the row "Peggy Sue · Buddy Holly"' in str(e.value)


def test_click_element_brings_a_hidden_element_into_view_before_clicking(monkeypatch):
    from cu_mcp import ax, server

    el = ax.Element("btn", "AXButton", "", "Add", "", (900, 50, 40, 20), True, False, (("Add", 0),))
    monkeypatch.setitem(server._state, "elements", {7: el})
    monkeypatch.setattr(server, "_approve", lambda *a: None)
    monkeypatch.setattr(server.screen, "geometry", lambda: None)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    for name in ("point", "click"):
        monkeypatch.setattr(server.effects, name, lambda *a, **k: None)
    monkeypatch.setattr(server, "_moved", lambda *a: None)
    clicks = []
    monkeypatch.setattr(server.inputs, "click", lambda x, y, *a: clicks.append((x, y)))
    frames = [(900, 50, 40, 20), (900, 400, 40, 20)]
    monkeypatch.setattr(server.ax, "element_frame", lambda ref: frames[0])
    reach = {(920.0, 60.0): (False, 'group "Top bar"'), (920.0, 410.0): (True, 'button "Add"')}
    monkeypatch.setattr(server, "_reach", lambda geo, x, y, ref, describe=True: reach[(x, y)])
    monkeypatch.setattr(server.ax, "perform", lambda ref, action: action == "AXScrollToVisible" and bool(frames.pop(0)))
    msg = server._do_click_element(7)
    assert clicks == [(920.0, 410.0)] and "scrolled it into view first" in msg

    # a text field that cannot be revealed gets the keyboard focus instead of a click on whatever covers it
    field = ax.Element("field", "AXTextField", "AXSearchField", "Search", "", (300, 50, 300, 30), True, False, ())
    monkeypatch.setitem(server._state, "elements", {8: field})
    monkeypatch.setattr(server.ax, "element_frame", lambda ref: field.frame)
    monkeypatch.setattr(server, "_reach", lambda geo, x, y, ref, describe=True: (False, 'group "Top bar"'))
    monkeypatch.setattr(server.ax, "perform", lambda ref, action: False)
    monkeypatch.setattr(server.ax, "set_attr", lambda ref, name, value: name == "AXFocused")
    monkeypatch.setattr(server.ax, "is_text_input", lambda ref: ref == "field")
    monkeypatch.setattr(server.ax, "note_focus", lambda pid, ref: None)
    monkeypatch.setattr(server, "_target", lambda required=True: (42, "App"))
    assert "put the keyboard focus into it" in server._do_click_element(8) and len(clicks) == 1


def test_fill_types_nothing_when_the_focus_did_not_go_into_the_field(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from cu_mcp import ax, server
    from cu_mcp.server import Target

    field = ax.Element("field", "AXTextField", "AXSearchField", "Search", "", (300, 50, 300, 30), True, False, ())
    monkeypatch.setitem(server._state, "elements", {53: field})
    t = Target(100, (450, 65), field.frame, '[53] search field "Search"', 53)
    monkeypatch.setattr(server, "_candidates", lambda q, **k: ([t], "App"))
    monkeypatch.setattr(server, "_click_target", lambda target, *a: "clicked [53]")
    monkeypatch.setattr(server, "_target", lambda required=True: (42, "App"))
    monkeypatch.setattr(server.ax, "is_trusted", lambda: True)
    monkeypatch.setattr(server.ax, "is_text_input", lambda ref: True)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    typed = []
    monkeypatch.setattr(server, "_type_into_focus", lambda text, clear=True: typed.append(text) or "typed")
    focus = {"el": "web area"}  # the click landed on a header that had scrolled over the field
    monkeypatch.setattr(server.ax, "focused_element", lambda pid: focus["el"])
    monkeypatch.setattr(server.ax, "within", lambda inner, outer: inner == outer)
    monkeypatch.setattr(server.ax, "set_attr", lambda ref, name, value: False)
    with pytest.raises(ToolError, match="nothing was typed"):
        server._do_fill("Summertime Blues", target="Search")
    assert typed == []
    monkeypatch.setattr(server.ax, "set_attr", lambda ref, name, value: bool(focus.update(el=ref)) or True)
    assert "focused it through accessibility" in server._do_fill("Summertime Blues", target="Search")
    assert typed == ["Summertime Blues"]


def test_near_names_the_row_it_used_and_lists_rows_when_the_anchor_is_missing(monkeypatch):
    # 90s classical run: near="Lento e Largo" matched two search results, and the Add of the wrong one (a video)
    # was clicked without the result saying which row it was.
    from mcp.server.mcpserver.exceptions import ToolError

    from cu_mcp import ax, server
    from cu_mcp.server import Target

    row = lambda y, label: ax.Element(None, "AXRow", "", label, "", (320, y - 20, 640, 40), True, False, ())
    monkeypatch.setitem(server._state, "elements", {
        1: row(362, "Symphony No. 3, Op. 36: II. Lento e Largo · Dawn Upshaw · Górecki: Symphony No. 3"),
        2: row(558, "Górecki: Symphony No. 3 (Excerpt) · Ernest Bour · Contemplation")})
    adds = [Target(94, (913, 362), (893, 352, 40, 20), "[71] button \"Add to Playlist\"", 71),
            Target(94, (913, 558), (893, 548, 40, 20), "[90] button \"Add to Playlist\"", 90)]
    anchors = [Target(90, (420, 356), (330, 346, 180, 16), "title Dawn Upshaw", 3)]
    monkeypatch.setattr(server, "_candidates", lambda q, reuse=False, **k: (anchors, "Spotify"))
    monkeypatch.setattr(server, "_focused_field", lambda: None)
    picked = server._pick_near(adds, "Add", "Spotify", "Dawn Upshaw")
    assert picked.el_id == 71 and picked.note == ' in the row "Symphony No. 3, Op. 36: II. Lento e Largo · Dawn ' \
        'Upshaw · Górecki: Symphony No. 3"'

    anchors.clear()
    with pytest.raises(ToolError) as e:
        server._pick_near(adds, "Add", "Spotify", "Three Tenors")
    assert '[90] button "Add to Playlist" in the row "Górecki: Symphony No. 3 (Excerpt)' in str(e.value)


def test_menu_commands_wait_for_their_late_effect(monkeypatch):
    from cu_mcp import server
    monkeypatch.setitem(server._state, "late", 0.0)
    server._lag_if_menu("AXStaticText", 'menu item "Remove from this playlist"')
    assert server._state["late"] == server.SETTLE_LATE
    server._state["late"] = 0.0
    server._lag_if_menu("AXButton", 'button "Add to Playlist"')
    assert server._state["late"] == 0.0


def test_wait_for_ignores_the_text_just_typed_into_the_focused_field(monkeypatch):
    from cu_mcp import server
    from cu_mcp.server import Target

    typed = Target(94, (420, 470), (340, 460, 160, 20), 'text "Iron Man Black Sabbath" [OCR]')
    monkeypatch.setattr(server, "_focused_field", lambda: (330, 455, 340, 32))
    monkeypatch.setattr(server, "_candidates", lambda q, **k: ([typed], "Spotify"))
    assert not server._present("Iron Man")
    result = Target(100, (450, 570), (400, 560, 100, 20), 'static text "Iron Man"')
    monkeypatch.setattr(server, "_candidates", lambda q, **k: ([typed, result], "Spotify"))
    assert server._present("Iron Man")


def test_typing_into_a_search_field_makes_the_next_settle_wait_for_results(monkeypatch):
    from cu_mcp import server

    monkeypatch.setattr(server, "_check_typing", lambda *a: None)
    monkeypatch.setattr(server.effects, "pulse", lambda: None)
    monkeypatch.setattr(server.inputs, "type_text", lambda text: None)
    monkeypatch.setattr(server.ax, "is_trusted", lambda: True)
    monkeypatch.setattr(server.ax, "frontmost_app", lambda: (7, "Spotify"))
    server._state.pop("late", None)
    monkeypatch.setattr(server.ax, "focused_input", lambda pid: ((0, 0, 100, 30), False))
    server._do_type("Rename me")
    assert "late" not in server._state  # a plain text field: no extra wait
    monkeypatch.setattr(server.ax, "focused_input", lambda pid: ((0, 0, 100, 30), True))
    server._do_type("Iron Man")
    assert server._state.pop("late") == server.SETTLE_SEARCH


def test_actions_bring_the_work_app_back_when_cursor_took_the_front(monkeypatch):
    from cu_mcp import server

    opened = []
    monkeypatch.setattr(server, "_do_open_app", lambda name: opened.append(name) or f"opened {name}")
    monkeypatch.setattr(server.ax, "pid_for_app", lambda name: 7)
    monkeypatch.setitem(server._state, "work_app", (7, "Spotify", server.time.monotonic()))
    server._state.pop("note", None)
    server._refocus(1, "Cursor")
    assert opened == ["Spotify"] and "Spotify back" in server._state.pop("note")
    server._refocus(9, "Finder")  # the agent moved on to an unprotected app on purpose: leave it
    monkeypatch.setitem(server._state, "work_app", (7, "Spotify", server.time.monotonic() - 999))
    server._refocus(1, "Cursor")  # an old task: the user is just using Cursor
    assert opened == ["Spotify"]


def test_app_action_lists_actions_without_arguments(monkeypatch):
    import inspect

    from cu_mcp import server

    assert inspect.signature(server.app_action).parameters["app"].default == "?"


def test_the_chat_app_is_never_clicked_and_app_names_get_an_open_app_hint(monkeypatch):
    # "otwórz conpress": Grok clicked a "conpress" folder in Cursor's sidebar and Cursor's own Browser button.
    from mcp.server.mcpserver.exceptions import ToolError

    from cu_mcp import apps, server

    monkeypatch.setattr(server, "HOST_APPS", {"cursor"})
    monkeypatch.setattr(server.ax, "frontmost_app", lambda: (9, "Cursor"))
    monkeypatch.setattr(apps, "installed", lambda: ["Conpress", "Safari", "System Settings"])
    with pytest.raises(ToolError, match=r'open_app\("Conpress"\)'):
        server._do_click_text("conpress")
    with pytest.raises(ToolError, match="the chat you work in"):
        server._do_click(100, 100)
    monkeypatch.setattr(server.ax, "frontmost_app", lambda: (5, "Conpress"))
    server._not_in_host("Metadata")  # any other app: fine


def test_spotlight_is_refused_while_typing_would_go_to_a_protected_app(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from cu_mcp import server

    monkeypatch.setattr(server, "_target", lambda required=True: (9, "Cursor"))
    with pytest.raises(ToolError, match="open_app"):
        server._check_typing("cmd+space")
    server._check_typing("cmd+tab")  # switching apps stays allowed


def test_nothing_found_suggests_the_app_with_that_name(monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    from cu_mcp import apps, server

    monkeypatch.setattr(apps, "installed", lambda: ["Conpress", "Safari"])
    with pytest.raises(ToolError, match=r'installed app is named "Conpress"'):
        server._pick([], "conpres", "Finder")


def test_a_new_task_does_not_pull_the_previous_tasks_app_back(monkeypatch):
    from cu_mcp import server

    monkeypatch.setattr(server, "_screenshot", lambda **k: server._text_result("shot"))
    monkeypatch.setattr(server.recipes, "summary", lambda: "")
    monkeypatch.setattr(server.effects, "active", lambda: None)
    monkeypatch.setattr(server, "_log_call", lambda *a: None)
    monkeypatch.setitem(server._state, "work_app", (5, "Spotify", server.time.monotonic()))
    monkeypatch.setitem(server._state, "last_call_end", server.time.monotonic() - 30)
    server.screenshot()  # a screenshot within a task keeps the work app
    assert server._state["work_app"][1] == "Spotify"
    server._state["last_call_end"] = server.time.monotonic() - server.NEW_TASK_GAP - 1
    server.screenshot()  # the first screenshot after a long pause starts a new task
    assert "work_app" not in server._state


def test_fine_changes_find_a_pin_and_a_number_but_not_a_caret():
    from PIL import Image, ImageDraw

    from cu_mcp import screen

    a = Image.new("RGB", (1280, 832), (40, 60, 80))
    b = a.copy()
    d = ImageDraw.Draw(b)
    d.ellipse((530, 395, 540, 405), fill=(255, 0, 0))  # same brightness as the map, other colour
    d.text((397, 642), "46.5676 N", fill=(255, 255, 255))
    assert screen.changed_fraction(screen.thumb_of(a), screen.thumb_of(b)) < 0.001  # the thumbnail misses both
    assert sorted(screen.fine_changes(a, b)) == [(384, 640, 448, 672), (512, 384, 544, 416)]
    caret = a.copy()
    ImageDraw.Draw(caret).line((700, 300, 700, 316), fill=(255, 255, 255), width=2)
    assert screen.fine_changes(a, caret) == [] and screen.fine_changes(a, a.copy()) == []


def test_a_small_change_comes_with_the_image_instead_of_no_visible_change(monkeypatch):
    # Conpress run: clicking the map moved the pin and the coordinates, but the result said "No visible change",
    # so the model clicked again elsewhere.
    from types import SimpleNamespace

    from PIL import Image, ImageDraw

    from cu_mcp import server

    a = Image.new("RGB", (1280, 832), (40, 60, 80))
    b = a.copy()
    ImageDraw.Draw(b).ellipse((530, 395, 540, 405), fill=(255, 0, 0))
    frames = [a, b, b.copy()]
    geo = SimpleNamespace(img_w=1280, img_h=832, to_image=lambda x, y: (int(x), int(y)))
    monkeypatch.setattr(server.screen, "geometry", lambda: geo)
    monkeypatch.setattr(server.screen, "capture", lambda g: frames.pop(0))
    monkeypatch.setattr(server.screen, "has_screen_permission", lambda: True)
    monkeypatch.setattr(server, "_target", lambda required=True: (1, "Maps"))
    monkeypatch.setattr(server.ax, "is_trusted", lambda: False)
    monkeypatch.setattr(server.inputs, "cursor_pos", lambda: (0, 0))
    monkeypatch.setattr(server, "_note_work_app", lambda *a, **k: None)
    monkeypatch.setitem(server._state, "last_thumb", None)
    monkeypatch.setitem(server._state, "note", "")
    server._screenshot()
    r = server._screenshot(after_action=True)
    assert "Small change only, at (512, 384)-(544, 416)" in r.content[0].text and len(r.content) == 2
    r = server._screenshot(after_action=True)
    assert "No visible change" in r.content[0].text and len(r.content) == 1


def test_near_keeps_to_the_anchors_row_even_when_that_row_is_scrolled_out_of_view(monkeypatch):
    # Polish pop run: the result row "Szarówka · Daria Zawiałow" sat below the visible part of the list, and
    # near="Zawialow" clicked a visible "Add to playlist" button far away in the sidebar instead.
    from mcp.server.mcpserver.exceptions import ToolError

    from cu_mcp import server
    from cu_mcp.server import Target

    sidebar_btn = Target(94, (215, 738), (195, 728, 40, 20), "[40] sidebar button", 40)
    row_btn = Target(94, (913, 790), (893, 780, 40, 20), "[92] row button", 92)
    anchor = Target(100, (420, 790), (330, 780, 180, 20), "row text: Daria Zawiałow", 7)
    monkeypatch.setattr(server, "_candidates", lambda q, reuse=False, **k: ([anchor], "App"))
    monkeypatch.setattr(server, "_focused_field", lambda: None)
    monkeypatch.setattr(server, "_cover", lambda t: 'group "Player controls"' if t.el_id in (92, 7) else "")
    monkeypatch.setattr(server, "_row_text", lambda t: "")
    monkeypatch.setitem(server._state, "elements", {})
    assert server._pick_near([sidebar_btn, row_btn], "Add", "App", "Zawialow").el_id == 92

    far = Target(100, (420, 300), (330, 290, 180, 20), "a label far above", 8)  # no button in its row
    monkeypatch.setattr(server, "_candidates", lambda q, reuse=False, **k: ([far], "App"))
    with pytest.raises(ToolError, match="far from it"):
        server._pick_near([sidebar_btn], "Add", "App", "Zawialow")
