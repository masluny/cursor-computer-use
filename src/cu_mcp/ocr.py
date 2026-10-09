"""On-device text recognition (Apple Vision): the fallback eyes for things accessibility cannot see
(canvas apps, custom-drawn lists, web content without labels, games).

Fast mode reads the whole Retina screen in ~100-200 ms. Frames are global points, like ax.py.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any

import Quartz
import Vision
from Foundation import NSMakeRange

from . import ax, screen

LANGUAGES = [s.strip() for s in os.environ.get("CU_OCR_LANGUAGES", "pl-PL,en-US").split(",") if s.strip()]


@dataclass
class TextBox:
    text: str
    frame: tuple[float, float, float, float]  # x, y, w, h in points
    candidate: object  # VNRecognizedText, for sub-string boxes

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.frame
        return x + w / 2, y + h / 2


_memo: dict[str, Any] = {"t": 0.0, "region": None, "boxes": []}


def recognize(region: tuple[float, float, float, float] | None = None) -> list[TextBox]:
    """Text lines on the main display (or a region of it, in points), top to bottom."""
    if _memo["region"] == region and time.monotonic() - _memo["t"] < 0.25:
        return _memo["boxes"]
    boxes = _recognize(region)
    _memo.update(t=time.monotonic(), region=region, boxes=boxes)
    return boxes


def _recognize(region) -> list[TextBox]:
    if region is None:
        b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        region = (b.origin.x, b.origin.y, b.size.width, b.size.height)
    rx, ry, rw, rh = region
    img = screen.cg_image(Quartz.CGRectMake(rx, ry, rw, rh), best=True)
    if img is None:
        return []
    req = Vision.VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelFast)
    req.setUsesLanguageCorrection_(False)
    try:
        req.setRecognitionLanguages_(LANGUAGES)
    except Exception:  # noqa: BLE001 - unsupported language list: keep Vision's default
        pass
    handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(img, None)
    ok, _err = handler.performRequests_error_([req], None)
    boxes = []
    for obs in (req.results() or []) if ok else []:
        cands = obs.topCandidates_(1)
        if not cands:
            continue
        cand = cands[0]
        boxes.append(TextBox(str(cand.string()), _to_points(obs.boundingBox(), region), cand))
    boxes.sort(key=lambda b: (round(b.frame[1] / 6), b.frame[0]))
    return boxes


def _to_points(bb, region) -> tuple[float, float, float, float]:
    """Vision's normalized box (origin bottom-left) -> global points (origin top-left)."""
    rx, ry, rw, rh = region
    return (rx + bb.origin.x * rw, ry + (1 - bb.origin.y - bb.size.height) * rh,
            bb.size.width * rw, bb.size.height * rh)


def substring_frame(box: TextBox, start: int, length: int, region) -> tuple[float, float, float, float]:
    """Frame of text[start:start+length] inside a recognized line; the whole line if Vision cannot say."""
    try:
        obs, _err = box.candidate.boundingBoxForRange_error_(NSMakeRange(start, length), None)
        if obs is not None:
            return _to_points(obs.boundingBox(), region)
    except Exception:  # noqa: BLE001
        pass
    return box.frame


@dataclass
class Hit:
    score: float
    text: str  # the whole recognized line
    frame: tuple[float, float, float, float]  # the matched words when possible

    @property
    def center(self) -> tuple[float, float]:
        x, y, w, h = self.frame
        return x + w / 2, y + h / 2


def locate(query: str, region=None) -> list[Hit]:
    """Places on screen where query is written, best first; the frame covers just the matched words."""
    if region is None:
        b = Quartz.CGDisplayBounds(Quartz.CGMainDisplayID())
        region = (b.origin.x, b.origin.y, b.size.width, b.size.height)
    q = ax.normalize(query)
    raw = query.strip().lower()
    hits = []
    for box in recognize(region):
        sc = ax.match_score(q, ax.normalize(box.text)) if q else 0.0
        if not sc:
            continue
        frame = box.frame
        i = box.text.lower().find(raw)
        if i >= 0 and len(raw) < len(box.text):
            frame = substring_frame(box, i, len(raw), region)
        hits.append(Hit(sc, box.text, frame))
    hits.sort(key=lambda h: -h.score)
    return hits
