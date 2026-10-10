"""Ready-made app actions through AppleScript: one call instead of a chain of clicks for scriptable apps.

Only scripts from this library run; the model supplies arguments, which reach AppleScript as `argv` data and
are never spliced into script text. The first use of an app asks the user once to let Cursor control it
(System Settings > Privacy & Security > Automation).
"""

from __future__ import annotations

import difflib
import os
import re
import subprocess
import time

# app key -> (application name, {action: (script body using argv, [argument names], description)})
LIBRARY: dict[str, tuple[str, dict[str, tuple[str, list[str], str]]]] = {
    "music": ("Music", {
        "play": ('tell application "Music" to play', [], "resume playback"),
        "pause": ('tell application "Music" to pause', [], "pause"),
        "next": ('tell application "Music" to next track', [], "next track"),
        "previous": ('tell application "Music" to previous track', [], "previous track"),
        "play_song": ('tell application "Music" to play (first track of library playlist 1 whose name contains '
                      '(item 1 of argv))', ["query"], "play the first library song whose title contains query"),
        "now_playing": ('tell application "Music"\nif player state is not playing then return "nothing playing"\n'
                        'return (name of current track) & " - " & (artist of current track)\nend tell', [],
                        "title and artist of the current track"),
    }),
    "spotify": ("Spotify", {
        "play": ('tell application "Spotify" to play', [], "resume playback"),
        "pause": ('tell application "Spotify" to pause', [], "pause"),
        "next": ('tell application "Spotify" to next track', [], "next track"),
        "previous": ('tell application "Spotify" to previous track', [], "previous track"),
        "play_uri": ('tell application "Spotify" to play track (item 1 of argv)', ["uri"],
                     "play a spotify:track:/album:/playlist: URI (find one via open_url spotify:search:...)"),
        "now_playing": ('tell application "Spotify"\nif player state is not playing then return "nothing playing"\n'
                        'return (name of current track) & " - " & (artist of current track) & " | " & '
                        '(spotify url of current track)\nend tell', [], "title, artist and URI of the current track"),
    }),
    "notes": ("Notes", {
        "create_note": ('tell application "Notes"\nmake new note with properties {body:"<h1>" & (item 1 of argv) & '
                        '"</h1>" & (item 2 of argv)}\nend tell\nreturn "created"', ["title", "body"],
                        "create a note (body may use simple HTML: <br>, <ul><li>)"),
    }),
    "reminders": ("Reminders", {
        "create_reminder": ('tell application "Reminders" to make new reminder with properties {name:(item 1 of argv)}'
                            '\nreturn "created"', ["title"], "add a reminder to the default list"),
    }),
    "safari": ("Safari", {
        "current_page": ('tell application "Safari" to return (URL of current tab of front window) & linefeed & '
                         '(name of current tab of front window)', [], "URL and title of the front tab"),
    }),
    "finder": ("Finder", {
        "reveal": ('tell application "Finder"\nreveal (POSIX file (item 1 of argv) as alias)\nactivate\nend tell',
                   ["path"], "show a file or folder in Finder"),
    }),
    "mail": ("Mail", {
        "compose": ('tell application "Mail"\nset m to make new outgoing message with properties '
                    '{subject:(item 2 of argv), content:(item 3 of argv), visible:true}\n'
                    'tell m to make new to recipient at end of to recipients with properties {address:(item 1 of argv)}'
                    '\nactivate\nend tell\nreturn "draft opened (not sent)"', ["to", "subject", "body"],
                    "open a new email draft; it is NOT sent, the user sends it"),
    }),
    "system": ("System", {
        "get_volume": ("return output volume of (get volume settings)", [], "output volume 0-100"),
        "set_volume": ("set volume output volume ((item 1 of argv) as integer)\nreturn \"ok\"", ["level"],
                       "set output volume 0-100"),
    }),
}


def catalog() -> str:
    lines = []
    for key, (name, actions) in LIBRARY.items():
        for action, (_, params, desc) in actions.items():
            lines.append(f"- {key}.{action}({', '.join(params)}): {desc}")
    return "\n".join(lines)


def resolve(app: str, action: str, args) -> tuple[str, list[str], str]:
    """(script, argv, app name) for a library action; args may be a list or a dict by parameter name."""
    key = app.strip().lower()
    if key not in LIBRARY:
        raise ValueError(f"no actions for {app!r}. Available:\n{catalog()}")
    name, actions = LIBRARY[key]
    if action not in actions:
        raise ValueError(f"{app} has no action {action!r}. Available:\n{catalog()}")
    body, params, _ = actions[action]
    if isinstance(args, dict):
        missing = [p for p in params if p not in args]
        if missing:
            raise ValueError(f"{key}.{action} needs {', '.join(params)}; missing {', '.join(missing)}")
        values = [str(args[p]) for p in params]
    else:
        values = [str(a) for a in (args or [])]
        if len(values) != len(params):
            raise ValueError(f"{key}.{action} takes {len(params)} argument(s): {', '.join(params) or 'none'}")
    return f"on run argv\n{body}\nend run\n", values, name


def web_app(name: str) -> bool:
    """True for browser web apps (Safari or Chrome "Add to Dock"): they carry a real app's name, but have no
    AppleScript dictionary and handle no app links."""
    from AppKit import NSBundle, NSWorkspace

    path = NSWorkspace.sharedWorkspace().fullPathForApplication_(name)
    bundle = NSBundle.bundleWithPath_(path) if path else None
    bid = str(bundle.bundleIdentifier() or "") if bundle else ""
    return bid.startswith(("com.apple.Safari.WebApp", "com.google.Chrome.app."))


def run(app: str, action: str, args=None) -> str:
    script, argv, name = resolve(app, action, args)
    if web_app(name):
        raise RuntimeError(f"{name} on this Mac is a web app installed from a browser, not the native app: it has no "
                           "AppleScript actions and opens no app links. Use its own UI (search field, buttons) instead.")
    proc = subprocess.run(["/usr/bin/osascript", "-", *argv], input=script, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        err = proc.stderr.strip()
        if "-1743" in err or "Not authorized" in err:
            raise PermissionError(f"macOS blocked controlling {name}. Ask the user to allow it: System Settings > "
                                  "Privacy & Security > Automation > Cursor > " + name)
        raise RuntimeError(f"{app}.{action} failed: {err}")
    return proc.stdout.strip() or "ok"


# ---------------------------------------------------------------- installed apps (for "did you mean an app?" hints)

APP_DIRS = ["/Applications", "/Applications/Utilities", "/System/Applications", "/System/Applications/Utilities",
            os.path.expanduser("~/Applications")]
_installed: tuple[float, list[str]] = (0.0, [])


def installed() -> list[str]:
    """Names of the apps in the usual app folders (and one level of subfolders), cached for a minute."""
    global _installed
    if time.monotonic() - _installed[0] < 60:
        return _installed[1]
    names: set[str] = set()
    for d in APP_DIRS:
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for e in entries:
            if e.endswith(".app"):
                names.add(e[:-4])
            elif d == "/Applications" and os.path.isdir(os.path.join(d, e)):
                try:
                    names.update(x[:-4] for x in os.listdir(os.path.join(d, e)) if x.endswith(".app"))
                except OSError:
                    pass
    _installed = (time.monotonic(), sorted(names))
    return _installed[1]


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z]+", "", s.lower())


def match_installed(query: str, cutoff: float = 0.85) -> str | None:
    """The installed app the text most likely names ("conpress" -> "Conpress", "system setings" -> "System
    Settings"), or None."""
    q = _norm(query or "")
    if len(q) < 3:
        return None
    names = installed()
    by_norm = {_norm(n): n for n in names}
    if q in by_norm:
        return by_norm[q]
    close = difflib.get_close_matches(q, list(by_norm), n=1, cutoff=cutoff)
    return by_norm[close[0]] if close else None


def similar_installed(query: str, n: int = 3) -> list[str]:
    by_norm = {_norm(x): x for x in installed()}
    return [by_norm[k] for k in difflib.get_close_matches(_norm(query or ""), list(by_norm), n=n, cutoff=0.6)]
