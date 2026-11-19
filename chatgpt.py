"""The ChatGPT tab (steps 4-7): send the post text, capture the reply, decide if it is "skip"."""

from __future__ import annotations

import re
import time

from playwright.sync_api import Page, TimeoutError as PWTimeout

from browser import describe_tab
from config import (GPT_ASSISTANT_CANDIDATES, GPT_FALLBACK_AFTER, GPT_INPUT_CANDIDATES,
                    GPT_STOP_CANDIDATES, GPT_TIMEOUT, SEL)
from utils import log


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
        send.click(timeout=200)
    except PWTimeout:
        page.keyboard.press("Enter")
    log.info("Sent to ChatGPT, waiting for its reply (up to %ds)...", GPT_TIMEOUT)

    # Poll fast and take the reply the moment ChatGPT stops writing. The old 1s polling plus
    # "3 unchanged polls" cost 3-4 seconds after every answer.
    poll = 0.1
    start = time.time()
    next_log = start + 10
    last, same = None, 0
    seen_streaming = False
    while time.time() - start < GPT_TIMEOUT:
        time.sleep(poll)
        elapsed = time.time() - start
        streaming = _is_streaming(page)  # read BEFORE the text, so the text is never older than this flag
        sel, n, txt = _assistant_state(page)
        if elapsed >= next_log - start:
            next_log += 10
            log.info("  still waiting (%ds): reply elements=%d via %s, ChatGPT still writing=%s",
                     elapsed, n, sel, streaming)
        if streaming:
            seen_streaming = True

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
            if seen_streaming and not streaming:
                return cur  # ChatGPT just finished writing: take the reply right away
            done = (not streaming and same >= 5) or (streaming and same >= 120)
            if done and streaming:
                log.warning("ChatGPT's stop button is still showing but the reply has not changed for "
                            "12s - taking it as the final reply")
        else:
            done = not streaming and same >= 5
            if done:
                log.warning("Reply was read from the page text (selector fallback)")
                _log_reply_location(page, cur)
        if done:
            return cur

    describe_tab(page, "chatgpt")  # log what the bot can see, plus debug_chatgpt.png
    raise TimeoutError(
        f"ChatGPT's reply was not detected within {GPT_TIMEOUT}s. If the reply is visible in the "
        "ChatGPT tab, the page markup has changed - see the [chatgpt] lines above and debug_chatgpt.png.")
