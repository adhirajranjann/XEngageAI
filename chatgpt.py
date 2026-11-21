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


# Boilerplate ChatGPT shows around a conversation (footer disclaimer etc.). In the structural
# reader, everything from the first match to the end is not part of the reply.
_UI_TAIL_RE = re.compile(
    r"(ChatGPT\s+(is\s+AI\s+and\s+)?can\s+make\s+mistakes|Check\s+important\s+info"
    r"|See\s+Cookie\s+Preferences|Cookie\s+Preferences|Ask\s+anything|Message\s+ChatGPT).*\Z",
    re.I | re.S)
# If any of this is still inside a "reply", it is page chrome, not an answer - never post it.
_UI_JUNK_RE = re.compile(r"can\s+make\s+mistakes|ChatGPT\s+said|Check\s+important\s+info|You\s+said:", re.I)
_PLACEHOLDER_RE = re.compile(r"(thinking|searching|reasoning|working)[ .…]*", re.I)


def looks_like_ui_junk(reply: str) -> bool:
    return bool(_UI_JUNK_RE.search(reply))


# Structure-based reply reader. It does not rely on any ChatGPT class name or data attribute:
# it finds the message we just sent (the last text node containing the end of it), then walks up
# and takes the first following element that has visible text - the assistant's turn. It stops
# if it reaches the message box, so the composer and page footer are never mistaken for a reply.
# Returns null if our message isn't on the page, '' if no reply has appeared yet.
_JS_HELPERS = r"""
const norm = s => (s || '').replace(/\s+/g, ' ').trim();
const COMPOSER = 'textarea, [contenteditable="true"], input';
const findSent = needles => {
    for (const needle of needles) {
        if (!needle) continue;
        const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
        let hit = null;
        for (let n = walker.nextNode(); n; n = walker.nextNode()) {
            const p = n.parentElement;
            if (!p || p.closest('textarea, [contenteditable="true"], script, style')) continue;
            if (norm(n.nodeValue).includes(needle)) hit = n;   // last one = the newest message
        }
        if (hit) return hit;
    }
    return null;
};
"""

_JS_REPLY_AFTER = "(needles) => {\n" + _JS_HELPERS + r"""
    const hit = findSent(needles);
    if (!hit) return null;
    const textOf = root => {
        const w = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        const blocks = [];
        for (let n = w.nextNode(); n; n = w.nextNode()) {
            const p = n.parentElement;   // whitespace-only nodes are kept: they separate inline words
            if (!p || p.closest('button, svg, nav, [role="toolbar"], [aria-hidden="true"], .sr-only')) continue;
            const block = p.closest('p, li, pre, blockquote, h1, h2, h3, h4, h5, h6, div');
            const last = blocks[blocks.length - 1];
            if (last && last.block === block) last.text += n.nodeValue;
            else blocks.push({block, text: n.nodeValue});
        }
        return blocks.map(b => norm(b.text)).filter(Boolean).join('\n');
    };
    let cur = hit.parentElement;
    for (let hops = 0; cur && cur !== document.body && hops < 14; hops++) {
        if (cur.querySelector(COMPOSER)) return '';              // climbed past the conversation
        for (let sib = cur.nextElementSibling; sib; sib = sib.nextElementSibling) {
            if (sib.matches(COMPOSER) || sib.querySelector(COMPOSER)) return '';   // reached the message box
            const t = textOf(sib);
            if (t) return t;
        }
        cur = cur.parentElement;
    }
    return '';
}
"""

# Outline of the markup around our sent message, written to debug_chatgpt_dom.txt when no reply is found.
_JS_OUTLINE = "(needles) => {\n" + _JS_HELPERS + r"""
    const hit = findSent(needles);
    if (!hit) return 'The message that was sent was not found anywhere on the page text.';
    const desc = el => el.tagName.toLowerCase() + ' ' + Array.from(el.attributes)
        .filter(a => a.name === 'id' || a.name === 'role' || a.name === 'class' || a.name.startsWith('data-') || a.name.startsWith('aria-'))
        .map(a => a.name + '=' + a.value.slice(0, 40)).join(' ');
    const out = [];
    let cur = hit.parentElement;
    for (let i = 0; cur && cur !== document.body && i < 10; i++, cur = cur.parentElement) {
        const pad = '  '.repeat(i);
        out.push(pad + 'UP   ' + desc(cur));
        for (let s = cur.nextElementSibling; s; s = s.nextElementSibling)
            out.push(pad + '  NEXT ' + desc(s) + ' | text=' + JSON.stringify(norm(s.innerText).slice(0, 60)));
    }
    return out.join('\n');
}
"""


def _needles(text: str) -> list[str]:
    """Strings to look for on the page to locate the message we sent: the end of the whole
    text, and its last line (in case the page splits the message into separate nodes)."""
    flat = " ".join(text.split())
    lines = [" ".join(ln.split()) for ln in text.splitlines() if ln.strip()]
    out = [flat[-80:]] if flat else []
    if lines and lines[-1][-60:] not in out:
        out.append(lines[-1][-60:])
    return out


_turn_error_logged = False


def reply_via_turn(page: Page, text: str) -> str | None:
    """Reply read by page structure (see _JS_REPLY_AFTER). None = the message we sent could not
    be found on the page; '' = found, but no reply has appeared yet."""
    global _turn_error_logged
    try:
        raw = page.evaluate(_JS_REPLY_AFTER, _needles(text))
    except Exception as e:
        if not _turn_error_logged:  # never fail silently: this would hide a broken reader
            _turn_error_logged = True
            log.warning("Reading the reply by page structure failed: %s", e)
        return None
    if raw is None:
        return None
    reply = re.sub(r"^\s*ChatGPT\s+said:?\s*", "", raw, flags=re.I)
    reply = _UI_TAIL_RE.sub("", reply)
    reply = re.sub(r"\n{3,}", "\n\n", reply).strip()
    return "" if _PLACEHOLDER_RE.fullmatch(reply) else reply


def dump_dom_outline(page: Page, text: str) -> None:
    """Write the markup outline around our sent message to debug_chatgpt_dom.txt and the log."""
    try:
        outline = page.evaluate(_JS_OUTLINE, _needles(text))
    except Exception as e:
        outline = f"could not read the page structure: {e}"
    try:
        with open("debug_chatgpt_dom.txt", "w", encoding="utf-8") as f:
            f.write(outline + "\n")
    except OSError:
        pass
    log.info("[chatgpt] markup around the sent message (also saved to debug_chatgpt_dom.txt):\n%s", outline)


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
    # If the reply selectors matched nothing even before we sent (before_n == 0), they don't fit this
    # page (e.g. logged in vs logged out), so start reading by page structure almost immediately.
    fallback_after = GPT_FALLBACK_AFTER if before_n else min(GPT_FALLBACK_AFTER, 1.5)
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
        elif elapsed >= fallback_after:
            # Selector-free fallback: read the conversation turn that follows the message we sent.
            # (None = our message isn't on the page yet; treated like "no reply yet")
            cur = reply_via_turn(page, text) or ""
            how = "page text"
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
            # No reliable "still writing" signal in this mode, so wait for the text to hold still
            done = not streaming and same >= 12
            if done:
                log.warning("Reply was read from the page structure (selector fallback)")
                _log_reply_location(page, cur)
        if done:
            return cur

    describe_tab(page, "chatgpt")  # log what the bot can see, plus debug_chatgpt.png
    dump_dom_outline(page, text)   # plus debug_chatgpt_dom.txt: the markup around our sent message
    raise TimeoutError(
        f"ChatGPT's reply was not detected within {GPT_TIMEOUT}s. If the reply is visible in the "
        "ChatGPT tab, the page markup has changed - see the [chatgpt] lines above, debug_chatgpt.png "
        "and debug_chatgpt_dom.txt.")
