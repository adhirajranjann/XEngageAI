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
  6. Test first:   py main.py --dry-run --max-posts 3
     (does everything except post/like, and does not write to MongoDB)
     Real run:     py main.py        (Ctrl+C to stop)
"""

from __future__ import annotations

import argparse
import re
import sys

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

from browser import check_setup, describe_tab, pick_tabs
from chatgpt import find_chat_input
from config import CDP_URL, MONGODB_DB, MONGODB_URI, SEL
from database import Store
from ocr import configure_tesseract
from pipeline import run
from utils import log, setup_logging


def main() -> None:
    parser = argparse.ArgumentParser(description="XEngageAI")
    parser.add_argument("--dry-run", action="store_true",
                        help="run the whole flow but don't post, like, or write to MongoDB")
    parser.add_argument("--max-posts", type=int, default=0, help="stop after N posts (0 = no limit)")
    parser.add_argument("--check", action="store_true",
                        help="show what the bot sees in each tab (URLs, selector matches, screenshots) and exit")
    args = parser.parse_args()

    setup_logging()
    configure_tesseract()

    store = Store(MONGODB_URI, MONGODB_DB)
    store.readonly = args.dry_run

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            sys.exit(f"Could not connect to Chrome at {CDP_URL}: {e}\n"
                     "Start Chrome with --remote-debugging-port=9222 first (see the top of main.py).")
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
                         "py main.py --check  and look at debug_chatgpt.png.")
            x_page.bring_to_front()

            run(x_page, gpt_page, store, args.dry_run, args.max_posts)
        except KeyboardInterrupt:
            log.info("Stopped by user")
        finally:
            browser.close()  # only disconnects Playwright; Chrome stays open


if __name__ == "__main__":
    main()
