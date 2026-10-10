"""Background mode: the agent keeps working on a hidden virtual display while the user uses the Mac.

macOS has no public API to create a desktop (Space) or to move another app's windows between Spaces, so the
"new desktop" is a virtual display: CGVirtualDisplay, the private CoreGraphics class that DeskPad and
BetterDisplay use. It sits diagonally off the bottom-right corner of the user's displays and touches them at a
single point, so the pointer practically never wanders onto it, yet windows there are on an active display and
keep rendering (a window that is merely covered by others may stop painting). The work app's windows are moved
there through accessibility, and the server acts on them without the pointer or the keyboard focus. The display
disappears together with this process; macOS then brings its windows back to the main display by itself.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import objc
import Quartz

from . import ax, screen

NAME = os.environ.get("CU_BACKGROUND_DISPLAY_NAME", "Agent workspace")
MENU_BAR = 30.0  # points kept free at the top of the hidden display for its menu bar


class WorkspaceError(RuntimeError):
    pass


def _wait(check, timeout: float) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if check():
            return True
        time.sleep(0.05)
    return check()


def _create_display(width: float, height: float):
    """(display object, display id) of a HiDPI virtual display; it lives as long as the object."""
    try:
        desc_cls = objc.lookUpClass("CGVirtualDisplayDescriptor")
        display_cls = objc.lookUpClass("CGVirtualDisplay")
        settings_cls = objc.lookUpClass("CGVirtualDisplaySettings")
        mode_cls = objc.lookUpClass("CGVirtualDisplayMode")
    except objc.nosuchclass_error as exc:
        raise WorkspaceError("this macOS has no virtual display support") from exc
    mm = Quartz.CGDisplayScreenSize(Quartz.CGMainDisplayID())  # same density as the built-in screen
    desc = desc_cls.alloc().init()
    desc.setName_(NAME)
    desc.setMaxPixelsWide_(int(width * 2))
    desc.setMaxPixelsHigh_(int(height * 2))
    desc.setSizeInMillimeters_(mm if mm.width > 0 else Quartz.CGSizeMake(width * 0.2, height * 0.2))
    desc.setProductID_(0xC0DE)
    desc.setVendorID_(0x3456)
    desc.setSerialNum_(1)
    display = display_cls.alloc().initWithDescriptor_(desc)
    if display is None:
        raise WorkspaceError("macOS refused to create the virtual display")
    settings = settings_cls.alloc().init()
    settings.setHiDPI_(1)
    settings.setModes_([mode_cls.alloc().initWithWidth_height_refreshRate_(int(width), int(height), 60.0)])
    if not display.applySettings_(settings):
        raise WorkspaceError("macOS refused the virtual display's mode")
    return display, int(display.displayID())


def _others_union(skip: int) -> tuple[float, float]:
    """Bottom-right corner of all other active displays together."""
    err, ids, n = Quartz.CGGetActiveDisplayList(16, None, None)
    right = bottom = 0.0
    for did in list(ids or [])[:n]:
        if did == skip:
            continue
        b = Quartz.CGDisplayBounds(did)
        right = max(right, b.origin.x + b.size.width)
        bottom = max(bottom, b.origin.y + b.size.height)
    return right, bottom


def _place_diagonally(did: int) -> None:
    """Touch the user's displays only at their bottom-right corner (for this process only: the arrangement is
    gone with the display)."""
    x, y = _others_union(did)
    err, cfg = Quartz.CGBeginDisplayConfiguration(None)
    if err != 0:
        return
    Quartz.CGConfigureDisplayOrigin(cfg, did, int(x), int(y))
    Quartz.CGCompleteDisplayConfiguration(cfg, Quartz.kCGConfigureForAppOnly)
    _wait(lambda: Quartz.CGDisplayBounds(did).origin.x == int(x), 2.0)


def same_frame(a, b, tol: float = 3.0) -> bool:
    return all(abs(p - q) <= tol for p, q in zip(a, b))


@dataclass
class _Moved:
    ref: Any
    frame: tuple[float, float, float, float]  # where it was on the user's screen


class Workspace:
    def __init__(self) -> None:
        self.display = None
        self.display_id: int | None = None
        self.app: tuple[int, str] | None = None
        self.moved: list[_Moved] = []

    @property
    def active(self) -> bool:
        return self.display is not None

    def bounds(self) -> tuple[float, float, float, float]:
        b = Quartz.CGDisplayBounds(self.display_id)
        return b.origin.x, b.origin.y, b.size.width, b.size.height

    def inside(self, frame) -> bool:
        """Is the center of a frame (global points) on the hidden display?"""
        if not self.active:
            return False
        x, y, w, h = self.bounds()
        cx, cy = frame[0] + frame[2] / 2, frame[1] + frame[3] / 2
        return x <= cx < x + w and y <= cy < y + h

    def area(self) -> tuple[float, float, float, float]:
        """Where moved windows go: the whole hidden display below its menu bar."""
        x, y, w, h = self.bounds()
        return x, y + MENU_BAR, w, h - MENU_BAR

    def start(self) -> None:
        if self.active:
            return
        main = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        display, did = _create_display(main.size.width, main.size.height)
        if not _wait(lambda: Quartz.CGDisplayBounds(did).size.width > 0, 3.0):
            raise WorkspaceError("the virtual display did not come up")
        self.display, self.display_id = display, did
        _place_diagonally(did)
        screen.set_work_display(did)

    def adopt(self, pid: int, name: str, only: set[int] | None = None) -> int:
        """Make pid the work app and move its windows (or only the CG window numbers in `only`) onto the hidden
        display, each filling it. Returns how many moved."""
        self.app = (pid, name)
        refs = ax.window_refs(pid)
        if only is not None:
            frames = [w["frame"] for w in ax.windows() if w["pid"] == pid and w["number"] in only]
            refs = [r for r in refs if any(same_frame(r[1], f) for f in frames)]
        moved = 0
        for ref, frame, _ in refs:
            if self.inside(frame):
                continue
            if ax.set_window_frame(ref, *self.area()):
                self.moved.append(_Moved(ref, frame))
                moved += 1
        return moved

    def windows_outside(self, pid: int) -> set[int]:
        """CG numbers of the app's on-screen windows that are not on the hidden display."""
        return {w["number"] for w in ax.windows() if w["pid"] == pid and not self.inside(w["frame"])}

    def stop(self) -> tuple[int, str] | None:
        """Put the moved windows back where they were (windows opened later land at the same spot on the main
        display), then remove the display. Returns the work app."""
        if not self.active:
            return None
        main = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        x0, y0, _, _ = self.bounds()
        for m in self.moved:
            ax.set_window_frame(m.ref, *m.frame)
        pids = {self.app[0]} if self.app else set()
        pids |= {ax.pid_of(m.ref) for m in self.moved} - {None}
        for pid in pids:
            for ref, frame, _ in ax.window_refs(pid):
                if self.inside(frame):
                    ax.set_window_frame(ref, main.origin.x + frame[0] - x0 + 40, main.origin.y + frame[1] - y0)
        app = self.app
        screen.set_work_display(None)
        self.display, self.display_id, self.app, self.moved = None, None, None, []
        return app
