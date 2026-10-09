"""Ready-made app actions through AppleScript: one call instead of a chain of clicks for scriptable apps.

Only scripts from this library run; the model supplies arguments, which reach AppleScript as `argv` data and
are never spliced into script text. The first use of an app asks the user once to let Cursor control it
(System Settings > Privacy & Security > Automation).
"""

from __future__ import annotations

import subprocess

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


def run(app: str, action: str, args=None) -> str:
    script, argv, name = resolve(app, action, args)
    proc = subprocess.run(["/usr/bin/osascript", "-", *argv], input=script, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        err = proc.stderr.strip()
        if "-1743" in err or "Not authorized" in err:
            raise PermissionError(f"macOS blocked controlling {name}. Ask the user to allow it: System Settings > "
                                  "Privacy & Security > Automation > Cursor > " + name)
        raise RuntimeError(f"{app}.{action} failed: {err}")
    return proc.stdout.strip() or "ok"
