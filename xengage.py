#!/usr/bin/env python3
r"""
XEngageAI - X timeline engagement bot

Flow (per post, in this exact order):
  1. find the next unprocessed post on the X timeline tab
  2. screenshot it -> post_saved.png (overwritten every time)
  3. OCR the screenshot
  4. switch to the ChatGPT tab and send the OCR text
  5. read ChatGPT's reply ("skip" = do not reply)
  6. switch back to the X tab, post the reply, like the post if needed
  7. next post

Setup (Windows):
  1. py -m pip install -r requirements.txt
     (no "playwright install" needed - this uses your installed Chrome)
  2. Install Tesseract OCR: https://github.com/UB-Mannheim/tesseract/wiki
  3. Copy .env.example to .env and fill it in
  4. Start Chrome with a dedicated profile and remote debugging:
       "C:\Program Files\Google\Chrome\Application\chrome.exe" --remote-debugging-port=9222 --user-data-dir="%LOCALAPPDATA%\ChromeAutomation"
  5. In that Chrome: log in to x.com (home timeline) in one tab, and open chatgpt.com in
     another tab, inside the conversation that already holds your instruction
     ("return the corresponding reply, if there is no need to reply, reply with 'skip'").
  6. Test first:   py xengage.py --dry-run --max-posts 3
     (does everything except post/like, and does not write to MongoDB)
     Real run:     py xengage.py        (Ctrl+C to stop)
"""

from __future__ import annotations

import argparse
import logging
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytesseract
from dotenv import load_dotenv
from PIL import Image, ImageOps, ImageStat
from playwright.sync_api import Page, TimeoutError as PWTimeout, sync_playwright

load_dotenv()


# --------------------------------------------------------------------------- config
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
MAX_REPLIES_PER_RUN = int(input("How many posts to process? "))  #_int("MAX_REPLIES_PER_RUN", 25)
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

log = logging.getLogger("xengage")


def setup_logging() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # emoji-safe on Windows
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler("xengage.log", encoding="utf-8"),
        ],
    )


def pause(lo: float, hi: float) -> None:
    time.sleep(random.uniform(lo, hi))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- pure helpers
_PERMALINK_RE = re.compile(r"^/([^/]+)/status/(\d+)")


def parse_permalink(href: str | None) -> tuple[str, str] | None:
    """'/someuser/status/123/analytics' -> ('someuser', '123')"""
    m = _PERMALINK_RE.match(href or "")
    return (m.group(1), m.group(2)) if m else None


def is_skip(reply: str) -> bool:
    """True for 'skip', 'Skip.', "'skip'", etc. - but not 'skip this one because...'."""
    return re.sub(r"[^a-z]", "", reply.lower()) == "skip"


def clean_reply(reply: str) -> str:
    """Strip one pair of wrapping quotes, e.g. "Nice!" or curly quotes around the whole reply."""
    reply = reply.strip()
    pairs = {'"': '"', "'": "'", "`": "`", "\u201c": "\u201d", "\u2018": "\u2019"}
    if len(reply) > 1 and pairs.get(reply[0]) == reply[-1]:
        reply = reply[1:-1].strip()
    return reply


def clean_ocr(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def ocr_image(path: Path) -> str:
    """OCR the screenshot. Dark-mode screenshots are inverted and upscaled first,
    because Tesseract reads dark text on a light background much better."""
    img = Image.open(path).convert("L")
    if ImageStat.Stat(img).mean[0] < 128:
        img = ImageOps.invert(img)
    img = img.resize((img.width * 2, img.height * 2), Image.Resampling.LANCZOS)
    return clean_ocr(pytesseract.image_to_string(img, config=f"--psm {OCR_PSM}"))


# --------------------------------------------------------------------------- database
class Store:
    """MongoDB Atlas store of processed posts (falls back to memory if no URI is set)."""

    def __init__(self, uri: str, db_name: str):
        self.readonly = False
        self._mem: dict[str, dict] = {}
        self._coll = None
        if uri:
            import certifi
            from pymongo import ASCENDING, MongoClient

            client = MongoClient(uri, serverSelectionTimeoutMS=8000, tlsCAFile=certifi.where())
            client.admin.command("ping")
            self._coll = client[db_name]["posts"]
            self._coll.create_index([("post_id", ASCENDING)], unique=True)
            log.info("Connected to MongoDB (db=%s)", db_name)
        else:
            log.warning("MONGODB_URI not set - duplicate tracking is in memory only")

    def seen(self, post_id: str) -> bool:
        if self._coll is not None:
            return self._coll.count_documents({"post_id": post_id}, limit=1) > 0
        return post_id in self._mem

    def save(self, doc: dict) -> None:
        if self.readonly:
            return
        if self._coll is not None:
            self._coll.update_one({"post_id": doc["post_id"]}, {"$set": doc}, upsert=True)
        else:
            self._mem[doc["post_id"]] = doc

    def replies_today(self) -> int:
        start = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
        start = start.astimezone(timezone.utc)
        if self._coll is not None:
            return self._coll.count_documents({"status": "replied", "processed_at": {"$gte": start}})
        return sum(
            1 for d in self._mem.values() if d.get("status") == "replied" and d["processed_at"] >= start
        )


# --------------------------------------------------------------------------- X (tab 1)
@dataclass
class Post:
    post_id: str
    handle: str
    url: str


def locate_post(page: Page, post_id: str):
    return page.locator(f'{SEL["post"]}:has(a[href*="/status/{post_id}"])').first


def is_ad(art) -> bool:
    try:
        lines = [ln.strip() for ln in art.inner_text(timeout=1500).splitlines()]
    except PWTimeout:
        return False
    return "Ad" in lines or "Promoted" in lines


def _ignore(store: Store, seen: set, post: Post, status: str) -> None:
    log.info("Ignoring %s (%s)", post.url, status)
    seen.add(post.post_id)
    store.save({"post_id": post.post_id, "post_url": post.url, "status": status,
                "liked": False, "processed_at": now_utc()})


def next_unprocessed(page: Page, store: Store, seen: set) -> Post | None:
    """STEP 1: first post in the loaded timeline that we haven't handled yet."""
    arts = page.locator(SEL["post"])
    for i in range(arts.count()):
        art = arts.nth(i)
        try:
            href = art.locator(SEL["permalink"]).first.get_attribute("href", timeout=1500)
        except PWTimeout:
            continue
        parsed = parse_permalink(href)
        if not parsed:
            continue
        handle, pid = parsed
        if pid in seen:
            continue
        if store.seen(pid):
            seen.add(pid)
            continue
        post = Post(pid, handle, f"https://x.com/{handle}/status/{pid}")
        if SELF_HANDLE and handle.lower() == SELF_HANDLE:
            _ignore(store, seen, post, "ignored_own")
            continue
        if is_ad(art):
            _ignore(store, seen, post, "ignored_ad")
            continue
        return post
    return None


def screenshot_post(page: Page, post_id: str) -> None:
    """STEP 2: screenshot the post as post_saved.png (same file every time)."""
    page.bring_to_front()
    art = locate_post(page, post_id)
    art.wait_for(state="visible", timeout=10000)
    art.evaluate("el => el.scrollIntoView({block: 'center'})")  # keeps it clear of the sticky header
    pause(0.8, 1.5)  # let images finish loading
    art.screenshot(path=str(SCREENSHOT_PATH), animations="disabled")


def close_dialogs(page: Page) -> None:
    """Best-effort cleanup if a reply dialog is left open after an error."""
    try:
        page.keyboard.press("Escape")
        time.sleep(0.6)
        confirm = page.locator(SEL["discard_confirm"])
        if confirm.count():
            confirm.first.click()
    except Exception:
        pass


def post_reply(page: Page, post_id: str, reply: str) -> None:
    """STEP 8-9a: back on the X tab, open the reply box for this post and publish."""
    page.bring_to_front()
    art = locate_post(page, post_id)
    art.wait_for(state="visible", timeout=10000)
    art.scroll_into_view_if_needed()
    art.locator(SEL["reply"]).first.click()

    composer = page.locator(SEL["dialog_composer"]).first
    composer.wait_for(state="visible", timeout=10000)
    composer.click()
    page.keyboard.type(reply, delay=random.randint(15, 45))
    pause(0.6, 1.2)

    page.locator(SEL["dialog_post"]).first.click(timeout=10000)  # waits until the button is enabled
    page.locator(SEL["dialog_composer"]).first.wait_for(state="detached", timeout=15000)


def like_post(page: Page, post_id: str) -> bool:
    """STEP 9b: like the post. Returns True if the post is liked afterwards."""
    art = locate_post(page, post_id)
    if art.locator(SEL["unlike"]).count():
        return True  # already liked
    btn = art.locator(SEL["like"])
    if not btn.count():
        return False
    btn.first.click()
    try:
        art.locator(SEL["unlike"]).first.wait_for(state="visible", timeout=5000)
        return True
    except PWTimeout:
        return False


# --------------------------------------------------------------------------- ChatGPT (tab 2)
def find_chat_input(page: Page, wait: float = 10.0):
    """Return a locator for the visible ChatGPT message box, or None if there isn't one."""
    deadline = time.time() + wait
    while True:
        for sel in GPT_INPUT_CANDIDATES:
            loc = page.locator(f"{sel}:visible")
            if loc.count():
                return loc.first
        if time.time() >= deadline:
            return None
        time.sleep(1.0)


def describe_tab(page: Page, label: str) -> None:
    """Log what a tab looks like (URL, title, selector matches) and save a screenshot.
    Used when a tab isn't what the bot expects, and by --check."""
    try:
        page.bring_to_front()
    except Exception:
        pass
    try:
        log.info("[%s] url=%s title=%r", label, page.url, page.title())
    except Exception as e:
        log.info("[%s] could not read url/title: %s", label, e)
    try:
        body = " ".join(page.inner_text("body", timeout=3000).split())[:300]
        log.info("[%s] page text starts: %r", label, body)
    except Exception as e:
        log.info("[%s] could not read page text: %s", label, e)
    names = (GPT_INPUT_CANDIDATES + [SEL["gpt_send"]] + GPT_ASSISTANT_CANDIDATES + GPT_STOP_CANDIDATES
             if label == "chatgpt"
             else [SEL["post"], SEL["permalink"], SEL["like"], SEL["reply"]])
    for sel in names:
        try:
            log.info("[%s] %-48s matches=%d visible=%d", label, sel,
                     page.locator(sel).count(), page.locator(f"{sel}:visible").count())
        except Exception as e:
            log.info("[%s] %-48s error: %s", label, sel, e)
    try:
        path = f"debug_{label}.png"
        page.screenshot(path=path)
        log.info("[%s] screenshot saved to %s", label, path)
    except Exception as e:
        log.info("[%s] could not take a screenshot: %s", label, e)


def check_setup(context) -> None:
    """--check: show what the bot sees in Chrome, without touching anything."""
    log.info("Tabs open in this Chrome:")
    for pg in context.pages:
        try:
            log.info("  %s | %s", pg.url, pg.title())
        except Exception:
            log.info("  %s", pg.url)
    x_page, gpt_page = pick_tabs(context)
    for label, pg in (("x", x_page), ("chatgpt", gpt_page)):
        if pg is None:
            log.warning("[%s] no matching tab found", label)
        else:
            describe_tab(pg, label)


# Boilerplate ChatGPT shows around a conversation (footer disclaimer etc.). In the page-text
# fallback, everything from the first match to the end is not part of the reply.
_UI_TAIL_RE = re.compile(
    r"(ChatGPT\s+(is\s+AI\s+and\s+)?can\s+make\s+mistakes|Check\s+important\s+info"
    r"|See\s+Cookie\s+Preferences|Cookie\s+Preferences|Ask\s+anything|Message\s+ChatGPT).*\Z",
    re.I | re.S)
# If any of this is still inside a "reply", it is page chrome, not an answer - never post it.
_UI_JUNK_RE = re.compile(r"can\s+make\s+mistakes|ChatGPT\s+said|Check\s+important\s+info|You\s+said:", re.I)
_PLACEHOLDER_RE = re.compile(r"(thinking|searching|reasoning|working)[ .…]*", re.I)


def looks_like_ui_junk(reply: str) -> bool:
    return bool(_UI_JUNK_RE.search(reply))


def _page_text(page: Page) -> str:
    """Visible text of the ChatGPT conversation ('' if unreadable)."""
    for sel in ("main", "body"):
        try:
            return page.inner_text(sel, timeout=3000)
        except Exception:
            continue
    return ""


def _end_of(raw: str, words: list[str]) -> int | None:
    """Index just after the LAST place in raw where these words appear in order."""
    pattern = r"\s+".join(re.escape(w) for w in words)
    end = None
    for m in re.finditer(pattern, raw):
        end = m.end()
    return end


def reply_after_text(raw: str, sent: str) -> str:
    """Selector-free fallback: whatever the page shows after the message we just sent,
    minus ChatGPT's own labels and footer. Line breaks in the reply are kept.
    Returns '' if the sent message isn't on the page or nothing follows it yet."""
    words = sent.split()
    if not words:
        return ""
    end = _end_of(raw, words)
    if end is None and len(words) > 8:
        end = _end_of(raw, words[-8:])  # the page may render the start of a long message differently
    if end is None:
        return ""
    reply = re.sub(r"^\s*ChatGPT\s+said:?\s*", "", raw[end:], flags=re.I)
    reply = _UI_TAIL_RE.sub("", reply)
    reply = re.sub(r"\n{3,}", "\n\n", reply).strip()
    return "" if _PLACEHOLDER_RE.fullmatch(reply) else reply


_reply_location_logged = False


def _log_reply_location(page: Page, reply: str) -> None:
    """Once per run: log which page elements hold the reply, so the selectors can be fixed."""
    global _reply_location_logged
    if _reply_location_logged:
        return
    _reply_location_logged = True
    try:
        chain = page.evaluate(r"""(reply) => {
            const norm = s => (s || '').replace(/\s+/g, ' ').trim();
            const target = norm(reply);
            let hit = null;
            for (const el of document.querySelectorAll('main *')) {
                if (norm(el.innerText) === target) hit = el;  // last match = deepest element
            }
            const out = [];
            for (let el = hit, i = 0; el && i < 6; el = el.parentElement, i++) {
                const attrs = Array.from(el.attributes)
                    .filter(a => a.name === 'role' || a.name === 'class' || a.name.startsWith('data-'))
                    .map(a => a.name + '=' + a.value.slice(0, 50));
                out.push(el.tagName.toLowerCase() + ' ' + attrs.join(' '));
            }
            return out;
        }""", reply)
        log.info("[chatgpt] the reply sits in (innermost first): %s", " < ".join(chain) or "not found")
    except Exception as e:
        log.info("[chatgpt] could not locate the reply element: %s", e)


def _assistant_state(page: Page) -> tuple[str | None, int, str]:
    """(selector, number of reply elements, text of the last one), using the first
    selector in GPT_ASSISTANT_CANDIDATES that matches anything on the page."""
    for sel in GPT_ASSISTANT_CANDIDATES:
        loc = page.locator(sel)
        n = loc.count()
        if n:
            try:
                return sel, n, loc.last.inner_text(timeout=2000).strip()
            except PWTimeout:
                return sel, n, ""
    return None, 0, ""


def _is_streaming(page: Page) -> bool:
    return any(page.locator(sel).count() for sel in GPT_STOP_CANDIDATES)


def ask_chatgpt(page: Page, text: str) -> str:
    """STEPS 4-7: send the OCR text to the ChatGPT tab and return its reply."""
    page.bring_to_front()
    _, before_n, before_text = _assistant_state(page)

    box = find_chat_input(page, wait=10)
    if box is None:
        raise RuntimeError("Could not find the ChatGPT message box (is the tab on a chat page and logged in?)")
    box.click()
    box.fill(text)
    send = page.locator(SEL["gpt_send"]).first
    try:
        send.click(timeout=200)  # waits until the button is enabled
    except PWTimeout:
        page.keyboard.press("Enter")
    log.info("Sent to ChatGPT, waiting for its reply (up to %ds)...", GPT_TIMEOUT)

    start = time.time()
    next_log = start + 10
    last, same = None, 0
    while time.time() - start < GPT_TIMEOUT:
        time.sleep(1.0)
        elapsed = time.time() - start
        sel, n, txt = _assistant_state(page)
        streaming = _is_streaming(page)
        if elapsed >= next_log - start:
            next_log += 10
            log.info("  still waiting (%ds): reply elements=%d via %s, ChatGPT still writing=%s",
                     elapsed, n, sel, streaming)

        if n > before_n or (n and txt != before_text):
            cur, how = txt, "dom"
        elif elapsed >= GPT_FALLBACK_AFTER:
            # Selector-free fallback: read whatever the page shows after the message we sent.
            cur, how = reply_after_text(_page_text(page), text), "page text"
        else:
            continue  # the new answer hasn't appeared yet

        if not cur:
            last, same = None, 0
            continue
        if cur == last:
            same += 1
        else:
            last, same = cur, 0

        if how == "dom":
            done = (not streaming and same >= 3) or (streaming and same >= 12)
            if done and streaming:
                log.warning("ChatGPT's stop button is still showing but the reply has not changed for "
                            "12s - taking it as the final reply")
        else:
            done = not streaming and same >= 3
            if done:
                log.warning("Reply was read from the page text (selector fallback)")
                _log_reply_location(page, cur)
        if done:
            return cur

    describe_tab(page, "chatgpt")  # log what the bot can see, plus debug_chatgpt.png
    raise TimeoutError(
        f"ChatGPT's reply was not detected within {GPT_TIMEOUT}s. If the reply is visible in the "
        "ChatGPT tab, the page markup has changed - see the [chatgpt] lines above and debug_chatgpt.png.")


# --------------------------------------------------------------------------- one post, start to finish
def process_post(x_page: Page, gpt_page: Page, post: Post, dry_run: bool) -> dict:
    doc = {"post_id": post.post_id, "post_url": post.url, "ocr_text": "", "ai_response": "",
           "status": "failed", "liked": False}
    try:
        screenshot_post(x_page, post.post_id)  # step 2
        text = ocr_image(SCREENSHOT_PATH)  # step 3
        doc["ocr_text"] = text
        log.info("OCR: %r", text[:120])
        if len(text) < MIN_OCR_CHARS:
            doc["status"] = "no_text"
            return doc

        reply = clean_reply(ask_chatgpt(gpt_page, text))  # steps 4-7
        doc["ai_response"] = reply
        log.info("ChatGPT: %r", reply[:160])
        if not reply:
            raise RuntimeError("ChatGPT returned an empty answer")
        if looks_like_ui_junk(reply):
            log.warning("The captured 'reply' contains ChatGPT page text, not an answer - not posting: %r",
                        reply[:200])
            doc["status"] = "bad_reply"
            return doc

        if is_skip(reply):
            log.info("ChatGPT said skip - leaving this post alone")
            doc["status"] = "skipped"
            if LIKE_ON_SKIP and not dry_run:
                x_page.bring_to_front()
                doc["liked"] = like_post(x_page, post.post_id)
            return doc

        if len(reply) > MAX_REPLY_CHARS:
            log.warning("Reply is %d chars (limit %d) - not posting", len(reply), MAX_REPLY_CHARS)
            doc["status"] = "too_long"
            return doc

        if REQUIRE_APPROVAL:
            print(f"\n--- Post: {post.url}\n--- Reply:\n{reply}\n")
            if input("Post this reply? [y/N] ").strip().lower() != "y":
                doc["status"] = "declined"
                return doc

        if dry_run:
            log.info("DRY RUN: this reply would be posted (and the post liked), but --dry-run is on. "
                     "Run without --dry-run to post for real.")
            doc["status"] = "dry_run"
            return doc

        post_reply(x_page, post.post_id, reply)  # steps 8-9
        doc["status"] = "replied"
        if LIKE_ON_REPLY:
            try:
                doc["liked"] = like_post(x_page, post.post_id)
            except Exception as e:  # the reply already went out; don't mark the whole post failed
                log.warning("Reply posted but liking failed: %s", e)
        return doc

    except Exception as e:
        log.exception("Failed on %s", post.url)
        doc["status"] = "failed"
        doc["error"] = str(e)
        close_dialogs(x_page)
        return doc
    finally:
        try:
            x_page.bring_to_front()
        except Exception:
            pass


# --------------------------------------------------------------------------- main loop
def run(x_page: Page, gpt_page: Page, store: Store, dry_run: bool, max_posts: int) -> None:
    seen: set[str] = set()
    stats = {"replied": 0, "skipped": 0, "liked": 0, "failed": 0, "other": 0}
    replied_today = store.replies_today()
    processed = empty_scrolls = consecutive_failures = 0
    log.info("Started%s. Replies already posted today: %d (daily cap %d)",
             " [DRY RUN]" if dry_run else "", replied_today, MAX_REPLIES_PER_DAY)

    while True:
        if max_posts and processed >= max_posts:
            log.info("Reached --max-posts %d", max_posts)
            break
        if stats["replied"] >= MAX_REPLIES_PER_RUN:
            log.info("Reached MAX_REPLIES_PER_RUN (%d)", MAX_REPLIES_PER_RUN)
            break
        if replied_today + stats["replied"] >= MAX_REPLIES_PER_DAY:
            log.info("Reached MAX_REPLIES_PER_DAY (%d)", MAX_REPLIES_PER_DAY)
            break
        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            log.error("%d failures in a row - stopping. Check the selectors / tabs.", consecutive_failures)
            break

        post = next_unprocessed(x_page, store, seen)  # step 1 / step 10
        if post is None:
            empty_scrolls += 1
            if empty_scrolls > MAX_EMPTY_SCROLLS:
                log.info("No new posts after scrolling. Waiting 60s, then refreshing the timeline.")
                time.sleep(60)
                x_page.reload()
                x_page.wait_for_selector(SEL["post"], timeout=30000)
                empty_scrolls = 0
            else:
                x_page.bring_to_front()
                x_page.evaluate("window.scrollBy(0, window.innerHeight * 0.8)")
                pause(1.5, 2.5)
            continue
        empty_scrolls = 0

        log.info("Post %s by @%s", post.post_id, post.handle)
        doc = process_post(x_page, gpt_page, post, dry_run)
        doc["processed_at"] = now_utc()
        store.save(doc)
        seen.add(post.post_id)
        processed += 1

        status = doc["status"]
        if status == "replied":
            stats["replied"] += 1
        elif status == "skipped":
            stats["skipped"] += 1
        elif status == "failed":
            stats["failed"] += 1
        else:
            stats["other"] += 1
        stats["liked"] += 1 if doc["liked"] else 0
        consecutive_failures = consecutive_failures + 1 if status == "failed" else 0
        log.info("-> %s%s | %s", status, " + liked" if doc["liked"] else "", stats)

        if REFRESH_EVERY and processed % REFRESH_EVERY == 0:
            log.info("Processed %d posts - refreshing the timeline", processed)
            x_page.bring_to_front()
            x_page.reload()
            x_page.wait_for_selector(SEL["post"], timeout=30000)
            empty_scrolls = 0

    log.info("Done. %s", stats)


def pick_tabs(context) -> tuple[Page | None, Page | None]:
    x_page = gpt_page = None
    for pg in context.pages:
        url = pg.url
        if x_page is None and re.match(r"https?://(www\.)?(x|twitter)\.com", url):
            x_page = pg
        elif gpt_page is None and re.match(r"https?://(chatgpt\.com|chat\.openai\.com)", url):
            gpt_page = pg
    return x_page, gpt_page


def main() -> None:
    parser = argparse.ArgumentParser(description="XEngageAI")
    parser.add_argument("--dry-run", action="store_true",
                        help="run the whole flow but don't post, like, or write to MongoDB")
    parser.add_argument("--max-posts", type=int, default=0, help="stop after N posts (0 = no limit)")
    parser.add_argument("--check", action="store_true",
                        help="show what the bot sees in each tab (URLs, selector matches, screenshots) and exit")
    args = parser.parse_args()

    setup_logging()
    if TESSERACT_CMD and Path(TESSERACT_CMD).exists():
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD

    store = Store(MONGODB_URI, MONGODB_DB)
    store.readonly = args.dry_run

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            sys.exit(f"Could not connect to Chrome at {CDP_URL}: {e}\n"
                     "Start Chrome with --remote-debugging-port=9222 first (see the top of this file).")
        try:
            context = browser.contexts[0]
            if args.check:
                check_setup(context)
                return
            x_page, gpt_page = pick_tabs(context)
            if x_page is None:
                sys.exit("No x.com tab found. Open your X home timeline in the debugging Chrome.")
            if gpt_page is None:
                sys.exit("No chatgpt.com tab found. Open your instructed ChatGPT conversation in a second tab.")

            if not re.search(r"/home/?(\?|$)", x_page.url):
                log.info("Opening the X home timeline")
                x_page.goto("https://x.com/home")
            try:
                x_page.wait_for_selector(SEL["post"], timeout=30000)
            except PWTimeout:
                describe_tab(x_page, "x")
                sys.exit("The X tab shows no posts. Are you logged in on the home timeline? "
                         "See the log lines above and debug_x.png.")
            gpt_page.bring_to_front()
            if find_chat_input(gpt_page, wait=20) is None:
                describe_tab(gpt_page, "chatgpt")
                sys.exit("The ChatGPT tab has no visible message box. Open your instructed chat there and "
                         "make sure you are logged in (no login or 'verify you are human' page), then run "
                         "py xengage.py --check  and look at debug_chatgpt.png.")
            x_page.bring_to_front()

            run(x_page, gpt_page, store, args.dry_run, args.max_posts)
        except KeyboardInterrupt:
            log.info("Stopped by user")
        finally:
            browser.close()  # only disconnects Playwright; Chrome stays open


if __name__ == "__main__":
    main()