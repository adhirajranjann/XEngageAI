"""Chrome connection helpers: pick the X and ChatGPT tabs, and diagnostics (--check)."""

from __future__ import annotations

import re

from playwright.sync_api import Page

from config import GPT_ASSISTANT_CANDIDATES, GPT_INPUT_CANDIDATES, GPT_STOP_CANDIDATES, SEL
from utils import log
from x_actions import describe_views


def pick_tabs(context) -> tuple[Page | None, Page | None]:
    x_page = gpt_page = None
    for pg in context.pages:
        url = pg.url
        if x_page is None and re.match(r"https?://(www\.)?(x|twitter)\.com", url):
            x_page = pg
        elif gpt_page is None and re.match(r"https?://(chatgpt\.com|chat\.openai\.com)", url):
            gpt_page = pg
    return x_page, gpt_page


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
             else [SEL["post"], SEL["permalink"], SEL["views"], SEL["like"], SEL["reply"]])
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
            if label == "x":
                describe_views(pg)
