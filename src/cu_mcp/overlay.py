"""On-screen effects while the agent controls the Mac: a glowing border around the screen, a halo with a
label that follows the mouse, ripples on clicks and a status pill under the menu bar.

Runs as its own process because AppKit needs a main thread with a run loop; the MCP server writes JSON
lines to stdin (see effects.py) and the process exits when that pipe closes. The window is click-through,
never takes focus and uses NSWindowSharingNone, so the screenshots taken for the model never contain it.
"""

from __future__ import annotations

import json
import math
import os
import sys
import threading
import time

import AppKit
import objc
import Quartz
from PyObjCTools import AppHelper

IDLE_HIDE = float(os.environ.get("CU_OVERLAY_IDLE", "15"))  # seconds without tool calls before fading out
USER_TAKEOVER_PT = 40.0  # the user moving the mouse this far (while the agent is not pointing) hides everything
POINTER_LINGER = 1.2  # the cursor halo shows while the agent moves/clicks and this long after, never on the user's own moves
LABEL = os.environ.get("CU_OVERLAY_LABEL", "Grok steruje komputerem · mysz w lewy górny róg = stop")
CURSOR_LABEL = os.environ.get("CU_CURSOR_LABEL", "Grok")
COLOR = os.environ.get("CU_OVERLAY_COLOR", "#8B5CF6")
# Debug only: let screencapture see the overlay so its look can be checked from a screenshot.
CAPTURABLE = os.environ.get("CU_OVERLAY_CAPTURABLE") == "1"


def _rgb(hexstr: str) -> tuple[float, float, float]:
    h = hexstr.lstrip("#")
    if len(h) != 6:
        h = "8B5CF6"
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


ACCENT = _rgb(COLOR)


def _cg(rgb: tuple[float, float, float], alpha: float = 1.0):
    return Quartz.CGColorCreateGenericRGB(rgb[0], rgb[1], rgb[2], alpha)


def _circle(radius: float):
    return Quartz.CGPathCreateWithEllipseInRect(Quartz.CGRectMake(-radius, -radius, 2 * radius, 2 * radius), None)


def _notch(screen) -> tuple[float, float, float] | None:
    """(left x, right x, height) of the camera housing in points, from the public macOS 12+ API."""
    try:
        left, right = screen.auxiliaryTopLeftArea(), screen.auxiliaryTopRightArea()
        top = screen.safeAreaInsets().top
    except AttributeError:
        return None
    if not top or left.size.width <= 0 or right.size.width <= 0:
        return None
    x0, x1 = left.origin.x + left.size.width, right.origin.x
    return (x0, x1, top) if x1 - x0 > 20 else None


def _border_path(w: float, h: float, notch):
    """Outline of the visible display in layer coordinates (origin bottom-left).

    Corners stay square on purpose: the panel's rounded corners clip the glow themselves, so it follows
    whatever radius the hardware has. With a notch the outline dips around it (rounded bottom corners,
    small concave shoulders), like the cutout itself."""
    p = Quartz.CGPathCreateMutable()
    Quartz.CGPathMoveToPoint(p, None, 0, 0)
    Quartz.CGPathAddLineToPoint(p, None, w, 0)
    Quartz.CGPathAddLineToPoint(p, None, w, h)
    if notch:
        nx0, nx1, nh = notch
        ny = h - nh
        r, f = min(9.0, nh / 2, (nx1 - nx0) / 4), 4.0  # bottom corner radius, shoulder fillet
        Quartz.CGPathAddLineToPoint(p, None, nx1 + f, h)
        Quartz.CGPathAddQuadCurveToPoint(p, None, nx1, h, nx1, h - f)
        Quartz.CGPathAddArcToPoint(p, None, nx1, ny, nx1 - r, ny, r)
        Quartz.CGPathAddArcToPoint(p, None, nx0, ny, nx0, ny + r, r)
        Quartz.CGPathAddLineToPoint(p, None, nx0, h - f)
        Quartz.CGPathAddQuadCurveToPoint(p, None, nx0, h, nx0 - f, h)
    Quartz.CGPathAddLineToPoint(p, None, 0, h)
    Quartz.CGPathCloseSubpath(p)
    return p


def _text_layer(text: str, size: float, scale: float, weight=AppKit.NSFontWeightMedium):
    """Rounded dark pill with white text; returns (layer, width, height)."""
    font = AppKit.NSFont.systemFontOfSize_weight_(size, weight)
    attrs = {AppKit.NSFontAttributeName: font}
    tw, th = AppKit.NSAttributedString.alloc().initWithString_attributes_(text, attrs).size()
    pad_x, pad_y = size * 0.9, size * 0.45
    w, h = tw + 2 * pad_x, th + 2 * pad_y
    pill = Quartz.CALayer.layer()
    pill.setBounds_(Quartz.CGRectMake(0, 0, w, h))
    pill.setCornerRadius_(h / 2)
    pill.setBackgroundColor_(_cg((0.07, 0.07, 0.09), 0.88))
    pill.setBorderColor_(_cg(ACCENT, 0.9))
    pill.setBorderWidth_(1.0)
    label = Quartz.CATextLayer.layer()
    label.setString_(text)
    label.setFont_(font)
    label.setFontSize_(size)
    label.setForegroundColor_(_cg((1, 1, 1), 1))
    label.setAlignmentMode_(Quartz.kCAAlignmentCenter)
    label.setContentsScale_(scale)
    label.setFrame_(Quartz.CGRectMake(0, pad_y - 1, w, th + 2))
    pill.addSublayer_(label)
    return pill, w, h


def _cursor():
    return Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))


class _Ticker(AppKit.NSObject):
    def initWithCallback_(self, callback):
        self = objc.super(_Ticker, self).init()
        if self is not None:
            self.callback = callback
        return self

    def tick_(self, _timer):
        self.callback()


class Overlay:
    def __init__(self):
        screen = AppKit.NSScreen.screens()[0]  # the main display, the only one the server acts on
        frame = screen.frame()
        visible = screen.visibleFrame()
        self.w, self.h = frame.size.width, frame.size.height
        scale = screen.backingScaleFactor()
        menubar = (frame.origin.y + frame.size.height) - (visible.origin.y + visible.size.height)

        win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            frame, AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
        win.setOpaque_(False)
        win.setBackgroundColor_(AppKit.NSColor.clearColor())
        win.setHasShadow_(False)
        win.setIgnoresMouseEvents_(True)
        win.setLevel_(Quartz.CGWindowLevelForKey(Quartz.kCGOverlayWindowLevelKey))
        win.setCollectionBehavior_(
            AppKit.NSWindowCollectionBehaviorCanJoinAllSpaces | AppKit.NSWindowCollectionBehaviorStationary
            | AppKit.NSWindowCollectionBehaviorFullScreenAuxiliary | AppKit.NSWindowCollectionBehaviorIgnoresCycle)
        win.setSharingType_(AppKit.NSWindowSharingReadOnly if CAPTURABLE else AppKit.NSWindowSharingNone)
        view = win.contentView()
        view.setWantsLayer_(True)
        bounds = Quartz.CGRectMake(0, 0, self.w, self.h)

        # Non-root container so opacity changes animate implicitly (fade in/out).
        self.stage = Quartz.CALayer.layer()
        self.stage.setFrame_(bounds)
        self.stage.setOpacity_(0.0)
        view.layer().addSublayer_(self.stage)

        # Glowing border: stacked bands fading inward (half of each stroke is off screen) plus a crisp
        # line with a bloom; the whole thing breathes.
        glow = Quartz.CALayer.layer()
        glow.setFrame_(bounds)
        path = _border_path(self.w, self.h, _notch(screen))
        # Smooth falloff comes from Gaussian shadows of the edge strokes (stacked flat bands would show steps).
        # Half of every stroke lies off screen, so only the inward half and its bloom are visible.
        for width, alpha, bloom, strength in ((10, 0.5, 30, 1.0), (4, 1.0, 9, 0.9)):
            edge = Quartz.CAShapeLayer.layer()
            edge.setFrame_(bounds)
            edge.setPath_(path)
            edge.setFillColor_(None)
            edge.setStrokeColor_(_cg(ACCENT, alpha))
            edge.setLineWidth_(width)
            edge.setLineJoin_(Quartz.kCALineJoinRound)
            edge.setShadowColor_(_cg(ACCENT))
            edge.setShadowOpacity_(strength)
            edge.setShadowRadius_(bloom)
            edge.setShadowOffset_(Quartz.CGSizeMake(0, 0))
            edge.setShouldRasterize_(True)  # static shape: draw the blur once, then only fade it
            edge.setRasterizationScale_(scale)
            glow.addSublayer_(edge)
        breathe = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
        breathe.setFromValue_(0.55)
        breathe.setToValue_(1.0)
        breathe.setDuration_(1.6)
        breathe.setAutoreverses_(True)
        breathe.setRepeatCount_(1e9)
        breathe.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseInEaseOut))
        glow.addAnimation_forKey_(breathe, "breathe")
        self.stage.addSublayer_(glow)

        # Status pill under the menu bar.
        pill, pw, ph = _text_layer(LABEL, 12.5, scale)
        pill.setPosition_(Quartz.CGPointMake(self.w / 2, self.h - menubar - 16 - ph / 2))
        self.stage.addSublayer_(pill)

        # Halo + name tag that follow the mouse.
        self.halo = Quartz.CALayer.layer()
        self.halo.setBounds_(Quartz.CGRectMake(0, 0, 1, 1))
        self.ring = Quartz.CAShapeLayer.layer()
        self.ring.setPath_(_circle(15))
        self.ring.setFillColor_(_cg(ACCENT, 0.16))
        self.ring.setStrokeColor_(_cg(ACCENT, 0.95))
        self.ring.setLineWidth_(2.5)
        self.ring.setShadowColor_(_cg(ACCENT))
        self.ring.setShadowOpacity_(0.9)
        self.ring.setShadowRadius_(8)
        self.ring.setShadowOffset_(Quartz.CGSizeMake(0, 0))
        self.halo.addSublayer_(self.ring)
        tag, tw, th = _text_layer(CURSOR_LABEL, 11, scale, AppKit.NSFontWeightSemibold)
        tag.setPosition_(Quartz.CGPointMake(18 + tw / 2, -20 - th / 2))  # below-right of the pointer
        self.halo.addSublayer_(tag)
        self.halo.setOpacity_(0.0)
        self.stage.addSublayer_(self.halo)
        self.pointer_until = 0.0

        win.orderFrontRegardless()
        self.win = win
        self.visible = False
        self.last_active = 0.0
        self.last_pos: tuple[float, float] | None = None
        self.user_moved = 0.0
        self._ticker = _Ticker.alloc().initWithCallback_(self._tick)
        AppKit.NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            1 / 60, self._ticker, "tick:", None, True)

    # -- coordinates: messages carry global CG points (origin top-left, y down) on the main display

    def _local(self, x: float, y: float):
        return Quartz.CGPointMake(x, self.h - y)

    def _tick(self) -> None:
        """60 Hz: follow the pointer, and get out of the way as soon as the session is over or the user takes
        the mouse (the agent announces its own pointer moves with "point", so any other movement is the user's)."""
        if not self.visible:
            return
        now = time.monotonic()
        p = _cursor()
        if now - self.last_active > IDLE_HIDE or (p.x <= 2 and p.y <= 2):
            self._set_visible(False)
            return
        pointing = now < self.pointer_until
        if pointing:
            self.user_moved = 0.0
        elif self.last_pos is not None:
            self.user_moved += math.hypot(p.x - self.last_pos[0], p.y - self.last_pos[1])
            if self.user_moved > USER_TAKEOVER_PT:
                self._set_visible(False)
                return
        self.last_pos = (p.x, p.y)
        if pointing != (self.halo.opacity() > 0.5):
            Quartz.CATransaction.begin()
            Quartz.CATransaction.setAnimationDuration_(0.15 if pointing else 0.5)
            self.halo.setOpacity_(1.0 if pointing else 0.0)
            Quartz.CATransaction.commit()
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        self.halo.setPosition_(self._local(p.x, p.y))
        Quartz.CATransaction.commit()

    def _set_visible(self, on: bool) -> None:
        self.visible = on
        self.last_pos, self.user_moved = None, 0.0
        if not on:
            self.pointer_until = 0.0
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setAnimationDuration_(0.35 if on else 0.4)
        self.stage.setOpacity_(1.0 if on else 0.0)
        Quartz.CATransaction.commit()

    def _ripple(self, x: float, y: float, delay: float = 0.0) -> None:
        dot = Quartz.CAShapeLayer.layer()
        dot.setPath_(_circle(22))
        dot.setFillColor_(_cg(ACCENT, 0.18))
        dot.setStrokeColor_(_cg(ACCENT, 1.0))
        dot.setLineWidth_(3)
        dot.setOpacity_(0.0)
        Quartz.CATransaction.begin()
        Quartz.CATransaction.setDisableActions_(True)
        dot.setPosition_(self._local(x, y))
        Quartz.CATransaction.commit()
        self.stage.addSublayer_(dot)
        grow = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
        grow.setFromValue_(0.25)
        grow.setToValue_(1.7)
        fade = Quartz.CABasicAnimation.animationWithKeyPath_("opacity")
        fade.setFromValue_(1.0)
        fade.setToValue_(0.0)
        group = Quartz.CAAnimationGroup.animation()
        group.setAnimations_([grow, fade])
        group.setDuration_(0.55)
        group.setBeginTime_(Quartz.CACurrentMediaTime() + delay)
        group.setFillMode_(Quartz.kCAFillModeBackwards)
        group.setTimingFunction_(Quartz.CAMediaTimingFunction.functionWithName_(Quartz.kCAMediaTimingFunctionEaseOut))
        dot.addAnimation_forKey_(group, "ripple")
        AppHelper.callLater(0.7 + delay, dot.removeFromSuperlayer)

    def _pulse(self) -> None:
        bump = Quartz.CABasicAnimation.animationWithKeyPath_("transform.scale")
        bump.setFromValue_(1.0)
        bump.setToValue_(1.45)
        bump.setDuration_(0.14)
        bump.setAutoreverses_(True)
        self.ring.addAnimation_forKey_(bump, "pulse")

    def handle(self, msg: dict) -> None:
        cmd = msg.get("cmd")
        if cmd == "hide":
            self.last_active = 0.0
            self._set_visible(False)
            return
        self.last_active = time.monotonic()
        if not self.visible:
            self._set_visible(True)
            self._tick()
        if cmd in ("point", "click"):
            self.pointer_until = time.monotonic() + float(msg.get("hold", 0)) + POINTER_LINGER
            self._tick()
        if cmd == "click":
            for i in range(max(1, min(int(msg.get("count", 1)), 3))):
                self._ripple(float(msg["x"]), float(msg["y"]), delay=0.12 * i)
        elif cmd == "pulse":
            self._pulse()


def _read_commands(overlay: Overlay) -> None:
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict):
            AppHelper.callAfter(overlay.handle, msg)
    os._exit(0)  # the server went away


def main() -> None:
    app = AppKit.NSApplication.sharedApplication()
    app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)  # no Dock icon, never active
    overlay = Overlay()
    threading.Thread(target=_read_commands, args=(overlay,), daemon=True).start()
    # A plain run loop, not NSApp.run(): finishLaunching would activate this process and steal keyboard
    # focus from the app the agent is typing into. Drawing and timers only need the run loop.
    AppHelper.runConsoleEventLoop(installInterrupt=False)


if __name__ == "__main__":
    main()
