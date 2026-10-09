"""Approval outside the model for risky actions.

Research on computer-use agents shows that on-screen content (pop-ups, injected text) hijacks them easily and
that telling the model to be careful does not help; checks have to live outside the model. So clicks and menu
commands whose target reads like "send", "buy", "delete"... stop here and the server itself asks the user with
a system alert. The model cannot answer it: the tool call holding the server lock is the one waiting.
"""

from __future__ import annotations

import os
import re

import CoreFoundation as CF

from . import ax

ENABLED = os.environ.get("CU_CONFIRM", "1") != "0"
TIMEOUT = float(os.environ.get("CU_CONFIRM_TIMEOUT", "60"))

# Normalized (lowercase, no accents) words or phrases; matched as whole words.
DEFAULT_WORDS = [
    "send", "send now", "wyslij", "wyslij teraz",
    "buy", "buy now", "kup", "kup teraz", "purchase", "place order", "order now", "zamow", "zamawiam",
    "pay", "pay now", "zaplac", "platnosc", "checkout", "confirm purchase", "potwierdz zakup",
    "delete", "usun", "remove", "erase", "empty trash", "oproznij kosz", "move to trash", "przenies do kosza",
    "transfer", "przelej", "wykonaj przelew",
    "publish", "opublikuj", "post", "submit", "wyslij formularz",
    "uninstall", "odinstaluj", "format", "wipe", "factory reset",
]
WORDS = [w.strip() for w in os.environ.get("CU_CONFIRM_WORDS", ",".join(DEFAULT_WORDS)).split(",") if w.strip()]
_PATTERNS = [(w, re.compile(rf"(?<!\w){re.escape(ax.normalize(w))}(?!\w)")) for w in WORDS]

# Apps showing system alerts or password prompts: the agent never types or clicks there.
SYSTEM_PROMPT_APPS = {"usernotificationcenter", "securityagent", "coreauthd", "coreservicesuiagent"}


def risky_word(texts) -> str | None:
    """The first risky word found in any of the texts (labels of the target), or None."""
    for t in texts:
        if not t:
            continue
        n = ax.normalize(str(t))
        for word, pattern in _PATTERNS:
            if pattern.search(n):
                return word
    return None


def confirm(action: str, word: str) -> bool:
    """Ask the user with a system alert; True only on an explicit "Allow". Times out to "no"."""
    if not ENABLED:
        return True
    header = "Grok chce wykonać ryzykowną akcję"
    message = f"{action}\n\nRozpoznane słowo: „{word}”.\nPozwolić tylko na tę jedną akcję?"
    result, flags = CF.CFUserNotificationDisplayAlert(
        TIMEOUT, CF.kCFUserNotificationCautionAlertLevel, None, None, None,
        header, message, "Nie", "Pozwól", None, None)
    return result == 0 and (flags & 0x3) == CF.kCFUserNotificationAlternateResponse
