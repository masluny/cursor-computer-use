"""Feeds the overlay process (overlay.py) and hears back from its button. Best effort: an effect failing never
breaks an action.

Disable with CU_EFFECTS=0. Coordinates are global points, like inputs.py.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading

ENABLED = os.environ.get("CU_EFFECTS", "1") != "0"


class Effects:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self.background = False
        self.on_toggle = None  # set by the server; called in a new thread when the user clicks the button

    @property
    def pid(self) -> int | None:
        """The overlay process (its button must never be clicked by the agent)."""
        proc = self._proc
        return proc.pid if proc is not None and proc.poll() is None else None

    def _send(self, msg: dict) -> None:
        if not ENABLED:
            return
        with self._lock:
            try:
                if self._proc is None or self._proc.poll() is not None:
                    self._proc = subprocess.Popen(
                        [sys.executable, "-m", "cu_mcp.overlay"], stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
                    threading.Thread(target=self._read_events, args=(self._proc,), daemon=True).start()
                    if self.background and msg.get("cmd") != "mode":  # a restarted overlay must know the mode
                        self._proc.stdin.write(json.dumps({"cmd": "mode", "background": True}) + "\n")
                self._proc.stdin.write(json.dumps(msg) + "\n")
                self._proc.stdin.flush()
            except OSError:
                self._proc = None

    def _read_events(self, proc: subprocess.Popen) -> None:
        for line in proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if isinstance(msg, dict) and msg.get("event") == "toggle" and self.on_toggle is not None:
                threading.Thread(target=self.on_toggle, daemon=True).start()

    def active(self) -> None:
        """Show the border and halo (in background mode just the button); they fade out after CU_OVERLAY_IDLE
        seconds without calls."""
        self._send({"cmd": "active"})

    def set_mode(self, background: bool, note: str = "") -> None:
        """Switch the button between "Work in background" and "Show"; note is shown on it for a few seconds."""
        self.background = background
        self._send({"cmd": "mode", "background": background, "note": note})

    def point(self, hold: float = 1.0) -> None:
        """The agent is about to move the pointer: show the halo on it for `hold` seconds (plus a short linger)."""
        if not self.background:
            self._send({"cmd": "point", "hold": hold})

    def click(self, x: float, y: float, count: int = 1) -> None:
        if not self.background:
            self._send({"cmd": "click", "x": x, "y": y, "count": count})

    def pulse(self) -> None:
        """Typing, keys, scrolling, menu commands."""
        if not self.background:
            self._send({"cmd": "pulse"})

    def hide(self) -> None:
        self._send({"cmd": "hide"})


effects = Effects()
