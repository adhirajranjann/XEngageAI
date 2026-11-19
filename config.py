"""Settings (read from .env / environment) and every CSS selector the bot relies on.

X and ChatGPT change their markup now and then; if a step stops working, the selectors
below are the first place to look."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


CDP_URL = os.getenv("CDP_URL", "http://localhost:9222")
MONGODB_URI = os.getenv("MONGODB_URI", "").strip()
MONGODB_DB = os.getenv("MONGODB_DB", "xengageai")
TESSERACT_CMD = os.getenv("TESSERACT_CMD", r"C:\Program Files\Tesseract-OCR\tesseract.exe")
SCREENSHOT_PATH = Path(os.getenv("SCREENSHOT_PATH", "post_saved.png"))
SELF_HANDLE = os.getenv("X_HANDLE", "").strip().lstrip("@").lower()

OCR_PSM = _int("OCR_PSM", 3)
MIN_OCR_CHARS = _int("MIN_OCR_CHARS", 5)
GPT_FALLBACK_AFTER = _int("GPT_FALLBACK_AFTER", 12)  # seconds before reading the reply from page text
GPT_TIMEOUT = _int("GPT_TIMEOUT", 180)  # seconds to wait for ChatGPT's answer

REQUIRE_APPROVAL = _bool("REQUIRE_APPROVAL", False)  # ask in the terminal before posting
LIKE_ON_REPLY = _bool("LIKE_ON_REPLY", True)  # like the post whenever a reply is posted
LIKE_ON_SKIP = _bool("LIKE_ON_SKIP", False)  # also like posts ChatGPT says to skip

MAX_REPLY_CHARS = _int("MAX_REPLY_CHARS", 280)
MAX_REPLIES_PER_RUN = _int("MAX_REPLIES_PER_RUN", 25)
MAX_REPLIES_PER_DAY = _int("MAX_REPLIES_PER_DAY", 50)
MAX_CONSECUTIVE_FAILURES = _int("MAX_CONSECUTIVE_FAILURES", 5)
MAX_EMPTY_SCROLLS = _int("MAX_EMPTY_SCROLLS", 8)
REFRESH_EVERY = _int("REFRESH_EVERY", 25)  # reload the timeline after this many posts (0 = never)

# X and ChatGPT change their markup now and then; if something stops working, fix it here.
SEL = {
    "post": 'article[data-testid="tweet"]',
    "permalink": 'a[href*="/status/"]:has(time)',
    "like": '[data-testid="like"]',
    "unlike": '[data-testid="unlike"]',
    "reply": '[data-testid="reply"]',
    "dialog_composer": 'div[role="dialog"] [data-testid="tweetTextarea_0"]',
    "dialog_post": 'div[role="dialog"] [data-testid="tweetButton"]',
    "discard_confirm": '[data-testid="confirmationSheetConfirm"]',
    "gpt_send": '[data-testid="send-button"]',
}

# ChatGPT's replies: the first selector that matches anything is used.
GPT_ASSISTANT_CANDIDATES = [
    "[data-assistant-markdown-root]",  # what the reply container looks like on this user's ChatGPT page
    '[data-message-author-role="assistant"]',
    'article[data-turn="assistant"]',
    "div.markdown",
]
# Present while ChatGPT is still writing its answer.
GPT_STOP_CANDIDATES = [
    '[data-testid="stop-button"]',
    'button[aria-label*="Stop" i]',
]

# Tried in order; the first one that is visible on the ChatGPT tab is used as the message box.
GPT_INPUT_CANDIDATES = [
    "#prompt-textarea",
    '[data-testid="prompt-textarea"]',
    'div.ProseMirror[contenteditable="true"]',
    'div[contenteditable="true"][role="textbox"]',
    "textarea",
]
