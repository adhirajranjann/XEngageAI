"""Step 3: OCR the post screenshot."""

from __future__ import annotations

import re
from pathlib import Path

import pytesseract
from PIL import Image, ImageOps, ImageStat

from config import OCR_PSM, TESSERACT_CMD


def configure_tesseract() -> None:
    """Point pytesseract at the Tesseract executable if it is installed at TESSERACT_CMD."""
    if TESSERACT_CMD and Path(TESSERACT_CMD).exists():
        pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


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
