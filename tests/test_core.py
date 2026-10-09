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
