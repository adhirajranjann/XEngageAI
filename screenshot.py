"""Step 2: screenshot one post as post_saved.png (same file every time)."""

from __future__ import annotations

from playwright.sync_api import Page

from config import SCREENSHOT_PATH
from utils import pause
from x_actions import locate_post


def screenshot_post(page: Page, post_id: str) -> None:
    """STEP 2: screenshot the post as post_saved.png (same file every time)."""
    page.bring_to_front()
    art = locate_post(page, post_id)
    art.wait_for(state="visible", timeout=10000)
    art.evaluate("el => el.scrollIntoView({block: 'center'})")  # keeps it clear of the sticky header
    pause(0.8, 1.5)  # let images finish loading
    art.screenshot(path=str(SCREENSHOT_PATH), animations="disabled")
