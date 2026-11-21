"""The X tab: find the next post, reply to it, like it."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from playwright.sync_api import Page, TimeoutError as PWTimeout

from config import MIN_VIEWS, SEL, SELF_HANDLE
from database import Store
from utils import log, now_utc


_PERMALINK_RE = re.compile(r"^/([^/]+)/status/(\d+)")
_COUNT_RE = re.compile(r"(\d[\d.,]*)\s*([KMB])?", re.I)
_MULTIPLIER = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}


def parse_count(text: str | None) -> int | None:
    """'52345 views. View post analytics' -> 52345, '52K' -> 52000, '1.2M' -> 1200000,
    '1,234' -> 1234. Returns None if there is no number in the text."""
    m = _COUNT_RE.search(text or "")
    if not m:
        return None
    num, suffix = m.group(1), (m.group(2) or "").upper()
    try:
        if suffix:
            # '1.2K' (or '1,2K' in some locales); thousands separators are not used with a suffix
            num = num.replace(",", ".") if "." not in num else num.replace(",", "")
            return round(float(num) * _MULTIPLIER[suffix])
        return int(re.sub(r"[.,]", "", num))
    except ValueError:
        return None


def get_views(art) -> int | None:
    """View count of a post element, or None if X shows none (no counter, not rendered yet).
    Prefers the exact number in the counter's aria-label over the abbreviated text ('52K')."""
    link = art.locator(SEL["views"])
    try:
        if not link.count():
            return None
        label = link.first.get_attribute("aria-label", timeout=1000) or ""
        if re.search(r"view", label, re.I):
            n = parse_count(label)
            if n is not None:
                return n
        return parse_count(link.first.inner_text(timeout=1000))
    except PWTimeout:
        return None


def describe_views(page: Page, limit: int = 10) -> None:
    """--check helper: log the view count the bot reads for the first posts on screen."""
    log.info("[x] views filter: only posts with more than %s views are processed (MIN_VIEWS)", f"{MIN_VIEWS:,}")
    arts = page.locator(SEL["post"])
    for i in range(min(arts.count(), limit)):
        art = arts.nth(i)
        try:
            parsed = parse_permalink(art.locator(SEL["permalink"]).first.get_attribute("href", timeout=1500))
        except PWTimeout:
            parsed = None
        views = get_views(art)
        log.info("[x]   post %s: views=%s", parsed[1] if parsed else "?", f"{views:,}" if views is not None else "not found")


def parse_permalink(href: str | None) -> tuple[str, str] | None:
    """'/someuser/status/123/analytics' -> ('someuser', '123')"""
    m = _PERMALINK_RE.match(href or "")
    return (m.group(1), m.group(2)) if m else None


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
        if MIN_VIEWS:
            views = get_views(art)
            if views is None or views <= MIN_VIEWS:
                # Not stored in MongoDB: a post still gaining views can qualify on a later run.
                log.info("Skipping %s: %s (need more than %s views)", post.url,
                         f"{views:,} views" if views is not None else "no view count shown", f"{MIN_VIEWS:,}")
                seen.add(post.post_id)
                continue
        return post
    return None


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
    page.keyboard.type(reply, delay=2)  # was 15-45 ms per character, i.e. ~6s for a 200-char reply

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
