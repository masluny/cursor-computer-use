"""Feeds the overlay process (overlay.py). Best effort: an effect failing never breaks an action.

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

    def _send(self, msg: dict) -> None:
        if not ENABLED:
            return
        with self._lock:
            try:
                if self._proc is None or self._proc.poll() is not None:
                    self._proc = subprocess.Popen(
                        [sys.executable, "-m", "cu_mcp.overlay"], stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
                self._proc.stdin.write(json.dumps(msg) + "\n")
                self._proc.stdin.flush()
            except OSError:
                self._proc = None

    def active(self) -> None:
        """Show the border and halo; they fade out after CU_OVERLAY_IDLE seconds without calls."""
        self._send({"cmd": "active"})

    def point(self, hold: float = 1.0) -> None:
        """The agent is about to move the pointer: show the halo on it for `hold` seconds (plus a short linger)."""
        self._send({"cmd": "point", "hold": hold})

    def click(self, x: float, y: float, count: int = 1) -> None:
        self._send({"cmd": "click", "x": x, "y": y, "count": count})

    def pulse(self) -> None:
        """Typing, keys, scrolling, menu commands."""
        self._send({"cmd": "pulse"})

    def hide(self) -> None:
        self._send({"cmd": "hide"})


effects = Effects()
