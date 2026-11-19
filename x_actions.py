"""The X tab: find the next post, reply to it, like it."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from playwright.sync_api import Page, TimeoutError as PWTimeout

from config import SEL, SELF_HANDLE
from database import Store
from utils import log, now_utc


_PERMALINK_RE = re.compile(r"^/([^/]+)/status/(\d+)")


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
