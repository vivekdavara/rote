"""Screenshots with regulated data masked before they touch disk."""

from __future__ import annotations

from collections.abc import Iterable

from playwright.async_api import Error, Locator, Page

from rote.surface.web.session import call


async def masked_screenshot(
    page: Page,
    *,
    mask_labels: Iterable[str] = (),
    mask_columns: Iterable[str] = (),
    sensitive_values: Iterable[str] = (),
) -> bytes:
    spec = {"labels": list(mask_labels), "columns": list(mask_columns), "values": list(sensitive_values)}
    masks: list[Locator] = []
    for frame in page.frames:
        try:
            count = await call(frame, "markForMasking", spec)
        except Error:
            continue
        if not count:
            continue
        if frame == page.main_frame:
            masks.append(page.locator("[data-rote-mask]"))
        elif frame.name:
            masks.append(page.frame_locator(f'frame[name="{frame.name}"], iframe[name="{frame.name}"]').locator("[data-rote-mask]"))
    try:
        return await page.screenshot(type="png", mask=masks, mask_color="#20242a")
    finally:
        for frame in page.frames:
            try:
                await call(frame, "clearMasks")
            except Error:
                continue
