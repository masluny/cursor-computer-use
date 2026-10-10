import json

import pytest

from cu_mcp import ax, jev


def el(role, label="", value="", enabled=True, subrole="", texts=()):
    return ax.Element(None, role, subrole, label, value, (10, 10, 50, 20), enabled, False, tuple(texts))


def test_options_cover_labeled_controls_and_one_fill_per_text_left():
    elements = {
        1: el("AXButton", "Privacy"),
        2: el("AXButton", "Gone", enabled=False),
        3: el("AXStaticText", "Welcome"),
        4: el("AXButton"),  # unlabeled icon
        5: el("AXTextField", "Search", subrole="AXSearchField"),
        6: el("AXCheckBox", "Block all cookies", value="off"),
        7: el("AXButton", texts=[("Settings", 0)]),
    }
    opts = jev.element_options(elements, ["Take On Me", "Hey Jude"], typed={"Hey Jude"})
    descs = {o.key: o.desc for o in opts}
    assert descs == {
        "click_1": 'click button "Privacy"',
        "fill_5_0": 'type "Take On Me" into search field "Search" (empty)',
        "click_6": 'click check box "Block all cookies" (off)',
        "click_7": 'click button "Settings"',
    }
    fill = next(o for o in opts if o.kind == "fill")
    assert (fill.el_id, fill.text) == (5, "Take On Me")


def test_build_options_caps_the_list_and_always_offers_the_fixed_moves(monkeypatch):
    monkeypatch.setattr(jev, "MAX_OPTIONS", 10)
    elements = {i: el("AXButton", f"Item {i}") for i in range(1, 30)}
    opts = jev.build_options(elements, [], set(), ocr_lines=[("Item 1", (0, 0)), ("Canvas word", (5, 5))])
    assert len(opts) == 10
    assert [o.key for o in opts[-len(jev.FIXED):]] == [o.key for o in jev.FIXED]
    assert len({o.key for o in opts}) == len(opts)


def test_ocr_options_skip_text_accessibility_already_covers():
    elements = {1: el("AXButton", "Save")}
    opts = jev.build_options(elements, [], set(), ocr_lines=[("save", (1, 1)), ("Untitled", (2, 2))])
    assert [o.desc for o in opts if o.kind == "ocr"] == ['click the text "Untitled"']


def test_state_mentions_text_to_type_only_when_given():
    s = jev.make_state("search", "Spotify", "nothing focused", [], [], ["Home"])
    assert "text_to_type" not in s and s["actions_taken"] == ["none yet"]
    assert jev.make_state("search", "Spotify", "x", ["Take On Me"], ["clicked"], [])["text_to_type"] == ["Take On Me"]


def test_parse_reads_choice_probability_and_ranking():
    opts = [jev.Option("click_1", "a", "click"), jev.Option("click_2", "b", "click"), *jev.FIXED]
    data = {"answers": {"next_action": {"type": "choice", "choice": "click_2", "confidence": 0.6,
                                        "probabilities": {"click_1": 0.2, "click_2": 0.75, "done": 0.05}}},
            "usage": {"input_tokens": 300, "output_tokens": 10, "cost": 0.0000126}}
    d = jev.parse(data, opts)
    assert d.option.key == "click_2" and d.prob == 0.75 and d.confidence == 0.6 and d.cost == 0.0000126
    assert [o.key for o, _ in d.ranked] == ["click_2", "click_1", "done"]
    with pytest.raises(jev.JevError):
        jev.parse({"answers": {"next_action": {"choice": "nope"}}}, opts)


def test_decide_sends_one_choice_question_and_explains_http_errors(monkeypatch):
    sent = {}

    def fake_post(body, key):
        sent.update(body=body, key=key)
        return 402, {"error": {"message": "Insufficient credits"}}

    monkeypatch.setattr(jev, "api_key", lambda: "sk-or-test")
    monkeypatch.setattr(jev, "_post", fake_post)
    opts = [jev.Option("click_1", 'click tab "Privacy"', "click"), *jev.FIXED]
    with pytest.raises(jev.JevError, match="no credits"):
        jev.decide({"goal": "x"}, opts)
    q = sent["body"]["questions"]["next_action"]
    assert sent["key"] == "sk-or-test" and sent["body"]["model"] == jev.MODEL
    assert q["type"] == "choice" and q["criteria"]["click_1"] == 'click tab "Privacy"' and "done" in q["criteria"]


def test_api_key_falls_back_to_cursor_mcp_config(tmp_path, monkeypatch):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"computer-use": {"command": "x", "env": {"OPENROUTER_API_KEY": "sk-or-1"}}}}))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr(jev, "MCP_CONFIG", str(cfg))
    assert jev.api_key() == "sk-or-1"
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-env")
    assert jev.api_key() == "sk-or-env"
    monkeypatch.delenv("OPENROUTER_API_KEY")
    monkeypatch.setattr(jev, "MCP_CONFIG", str(tmp_path / "missing.json"))
    assert jev.api_key() is None


@pytest.fixture
def loop(monkeypatch):
    """server._run_goal with the screen, Jev and the actions replaced by fakes."""
    from cu_mcp import screen, server

    click = jev.Option("click_1", 'click tab "Privacy"', "click", el_id=1)
    fill = jev.Option("fill_2_0", 'type "abc" into text field "Name"', "fill", el_id=2, text="abc")
    done = jev.FIXED[0]
    state = {"answers": [], "screens": [], "done": []}

    def fake_screen(texts, typed):
        state["screens"].append(set(typed))
        return "Settings", "nothing focused", [click, fill, *jev.FIXED], ["General"]

    def fake_decide(st, options):
        option, prob = state["answers"].pop(0)
        return jev.Decision(option, prob, prob, [(option, prob)], ms=5, cost=0.00001)

    def fake_execute(o):
        state["done"].append(o.key)
        return f"did {o.key}"

    monkeypatch.setattr(jev, "api_key", lambda: "sk-or-test")
    monkeypatch.setattr(jev, "decide", fake_decide)
    monkeypatch.setattr(server, "_jev_screen", fake_screen)
    monkeypatch.setattr(server, "_jev_execute", fake_execute)
    monkeypatch.setattr(server, "_guard", lambda **k: None)
    monkeypatch.setattr(server, "_settle", lambda: None)
    monkeypatch.setattr(server.ax, "frontmost_app", lambda: (1, "Settings"))
    monkeypatch.setattr(screen, "live_thumb", lambda: object())
    monkeypatch.setattr(screen, "changed_fraction", lambda a, b: 0.1)
    return server, state, click, fill, done


def test_run_goal_runs_until_jev_says_done(loop):
    server, state, click, fill, done = loop
    state["answers"] = [(click, 0.9), (fill, 0.8), (done, 0.95)]
    finished, text = server._run_goal("rename it", texts=["abc"])
    assert finished and state["done"] == ["click_1", "fill_2_0"]
    assert state["screens"][-1] == {"abc"}  # a typed text is not offered again
    assert "goal is done" in text and "2 action(s)" in text


def test_run_goal_hands_back_when_unsure_or_stuck_in_place(loop, monkeypatch):
    server, state, click, fill, done = loop
    state["answers"] = [(click, 0.3)]
    finished, text = server._run_goal("x")
    assert not finished and state["done"] == [] and "unsure" in text and "continue the task yourself" in text

    monkeypatch.setattr(server.screen, "changed_fraction", lambda a, b: 0.0)
    state["answers"] = [(click, 0.9), (fill, 0.9), (done, 0.9)]
    finished, text = server._run_goal("x", texts="abc")
    assert not finished and state["done"] == ["click_1", "fill_2_0"] and "changed nothing" in text


def test_run_goal_refuses_protected_apps_and_batch_fails_when_not_done(loop, monkeypatch):
    from mcp.server.mcpserver.exceptions import ToolError

    server, state, click, fill, done = loop
    monkeypatch.setattr(server.ax, "frontmost_app", lambda: (1, "Cursor"))
    with pytest.raises(ToolError, match="protected"):
        server._run_goal("x")
    monkeypatch.setattr(server.ax, "frontmost_app", lambda: (1, "Settings"))
    state["answers"] = [(jev.FIXED[1], 0.9)]  # stuck
    with pytest.raises(RuntimeError, match="stuck"):
        server._BATCH["run_goal"](goal="x")


def test_two_single_clicks_in_a_row_bring_one_jev_tip_per_task(monkeypatch):
    # Grok never called run_goal in eight runs: it made every labeled click itself, one turn each.
    from cu_mcp import server

    monkeypatch.setattr(jev, "api_key", lambda: "sk-or-test")
    monkeypatch.setitem(server._state, "click_streak", 0)
    monkeypatch.setitem(server._state, "jev_tipped", False)
    monkeypatch.setitem(server._state, "note", "")
    server._jev_tip("click_text")
    assert not server._state["note"]
    server._jev_tip("click_element")
    assert "run_goal(" in server._state["note"]
    server._state["note"] = ""
    server._jev_tip("click_text")  # once per task
    assert not server._state["note"]

    monkeypatch.setattr(jev, "api_key", lambda: None)  # no key: no tip
    server._state.update(click_streak=0, jev_tipped=False)
    server._jev_tip("click_text"), server._jev_tip("click_text")
    assert not server._state["note"]
