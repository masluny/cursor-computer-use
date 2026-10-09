"""Recipes: successful action sequences saved once and replayed in a single call.

Replaying validated runs is the biggest cost and time saver reported for recurring computer-use tasks. The
server records every successful semantic action (click_text, fill, menu, open_url, ...) of the current task;
recipe_save stores them with parameters ({{song}}), recipe_run replays them and hands control back to the
model at the first step that no longer matches the screen.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any

DIR = os.path.expanduser(os.environ.get("CU_RECIPES_DIR", "~/Library/Application Support/cursor-computer-use/recipes"))
TASK_GAP = 45.0  # seconds without tool calls that start a new task (new recording)
REPLAYABLE = {"open_app", "open_url", "menu", "click_text", "fill", "key", "type_text", "wait_for", "scroll_to",
              "click", "scroll", "drag", "wait", "app_action"}


class Recorder:
    """Successful replayable steps of the current task."""

    def __init__(self) -> None:
        self.steps: list[dict[str, Any]] = []
        self.last = 0.0
        self.paused = False

    def touch(self) -> None:
        """Called on every tool call; a long pause means the user started a new task."""
        now = time.monotonic()
        if now - self.last > TASK_GAP:
            self.steps = []
        self.last = now

    def record(self, action: str, **args) -> None:
        if self.paused or action not in REPLAYABLE:
            return
        step = {"action": action}
        step.update({k: v for k, v in args.items() if v is not None})
        self.steps.append(step)


def slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not s:
        raise ValueError("recipe name must contain letters or digits")
    return s[:60]


def templatize(steps: list[dict], params: dict[str, str]) -> list[dict]:
    """Replace each parameter's example value with {{name}} in every string argument (longest values first)."""
    order = sorted(params.items(), key=lambda kv: -len(str(kv[1])))
    used = {name: False for name in params}
    out = []
    for step in steps:
        new = {}
        for k, v in step.items():
            if k != "action" and isinstance(v, str):
                for name, value in order:
                    value = str(value)
                    if value and value in v:
                        v = v.replace(value, "{{" + name + "}}")
                        used[name] = True
            new[k] = v
        out.append(new)
    unused = [n for n, u in used.items() if not u]
    if unused:
        raise ValueError(f"parameter value(s) not found in any step: {', '.join(unused)}")
    return out


_PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


def params_of(steps: list[dict]) -> list[str]:
    found: list[str] = []
    for step in steps:
        for v in step.values():
            if isinstance(v, str):
                for name in _PLACEHOLDER.findall(v):
                    if name not in found:
                        found.append(name)
    return found


def render(steps: list[dict], values: dict[str, str]) -> list[dict]:
    missing = [p for p in params_of(steps) if p not in values]
    if missing:
        raise ValueError(f"missing parameter(s): {', '.join(missing)}")
    out = []
    for step in steps:
        out.append({k: (_PLACEHOLDER.sub(lambda m: str(values[m.group(1)]), v) if isinstance(v, str) else v)
                    for k, v in step.items()})
    return out


def _path(name: str) -> str:
    return os.path.join(DIR, slug(name) + ".json")


def save(name: str, description: str, steps: list[dict], params: dict[str, str] | None = None) -> dict:
    if not steps:
        raise ValueError("no steps to save")
    bad = [s.get("action") for s in steps if s.get("action") not in REPLAYABLE]
    if bad:
        raise ValueError(f"not replayable: {', '.join(map(str, bad))}")
    steps = templatize(steps, params or {})
    recipe = {"name": slug(name), "description": description.strip(), "params": params_of(steps), "steps": steps,
              "created": time.strftime("%Y-%m-%d %H:%M"), "runs": 0, "failures": 0}
    os.makedirs(DIR, exist_ok=True)
    with open(_path(name), "w") as f:
        json.dump(recipe, f, indent=1, ensure_ascii=False)
    return recipe


def load(name: str) -> dict:
    try:
        with open(_path(name)) as f:
            return json.load(f)
    except FileNotFoundError:
        raise ValueError(f"no recipe {slug(name)!r}; saved: {', '.join(r['name'] for r in list_all()) or 'none'}")


def list_all() -> list[dict]:
    if not os.path.isdir(DIR):
        return []
    out = []
    for fn in sorted(os.listdir(DIR)):
        if fn.endswith(".json"):
            try:
                with open(os.path.join(DIR, fn)) as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                continue
    return out


def note_run(name: str, ok: bool) -> None:
    try:
        r = load(name)
        r["runs"] = r.get("runs", 0) + 1
        if not ok:
            r["failures"] = r.get("failures", 0) + 1
        with open(_path(name), "w") as f:
            json.dump(r, f, indent=1, ensure_ascii=False)
    except (OSError, ValueError):
        pass


def summary(limit: int = 12) -> str:
    rs = list_all()
    if not rs:
        return ""
    lines = [f"- {r['name']}({', '.join(r.get('params', []))}): {r.get('description', '')}" for r in rs[:limit]]
    return "Saved recipes (replay with recipe_run(name, params) before doing the task by hand):\n" + "\n".join(lines)
