"""One post start to finish (process_post) and the main loop over the timeline (run)."""

from __future__ import annotations

import time

from playwright.sync_api import Page

from chatgpt import ask_chatgpt, clean_reply, is_skip, looks_like_ui_junk
from config import (LIKE_ON_REPLY, LIKE_ON_SKIP, MAX_CONSECUTIVE_FAILURES, MAX_EMPTY_SCROLLS,
                    MAX_REPLIES_PER_DAY, MAX_REPLIES_PER_RUN, MAX_REPLY_CHARS, MIN_OCR_CHARS,
                    REFRESH_EVERY, REQUIRE_APPROVAL, SCREENSHOT_PATH, SEL)
from database import Store
from ocr import ocr_image
from screenshot import screenshot_post
from utils import log, now_utc, pause
from x_actions import Post, close_dialogs, like_post, next_unprocessed, post_reply


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
