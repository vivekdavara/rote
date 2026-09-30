"""The indexer on the real mock: frames, inferred labels, table context, visibility."""

from __future__ import annotations

import pytest

from rote.devserver import MockServer
from rote.surface.web.observe import observe, render
from rote.surface.web.session import WebSession, call

from .conftest import sign_on

pytestmark = pytest.mark.integration


def controls(observation, frame_name):  # type: ignore[no-untyped-def]
    frame = next(f for f in observation.frames if f.name == frame_name)
    return frame.elements


async def test_frames_are_indexed_and_frameset_skipped(session: WebSession, mock: MockServer) -> None:
    await sign_on(session.page, mock.base_url())
    observation = await observe(session.page)
    assert [f.name for f in observation.frames] == ["banner", "nav", "main"]
    nav = [(e["role"], e["name"]) for e in controls(observation, "nav")]
    assert ("link", "Member Search") in nav
    main = next(f for f in observation.frames if f.name == "main")
    assert main.headings[0] == "Main Menu"
    assert observation.screenshot is not None and observation.screenshot[:2] == b"\xff\xd8"


async def test_labels_come_from_the_adjacent_cell(session: WebSession, mock: MockServer) -> None:
    await sign_on(session.page, mock.base_url())
    await session.page.frame(name="nav").get_by_role("link", name="Member Search").click()  # type: ignore[union-attr]
    await session.page.frame(name="main").get_by_text("Last Name").wait_for()  # type: ignore[union-attr]
    observation = await observe(session.page)
    elements = controls(observation, "main")
    textboxes = {e["label"]["text"]: e for e in elements if e["role"] == "textbox"}
    assert set(textboxes) >= {"Member Number", "Last Name"}
    assert textboxes["Member Number"]["label"]["source"] == "row"
    image_button = next(e for e in elements if e.get("type") == "image")
    assert image_button["name"] == "" and image_button["label"]["text"] == "Member Number"
    text = render(observation)
    assert '[e' in text and 'textbox label="Member Number"' in text and "(image button, no accessible name)" in text


async def test_hidden_validation_text_is_not_visible(session: WebSession, mock: MockServer) -> None:
    await sign_on(session.page, mock.base_url())
    main = session.page.frame(name="main")
    assert main is not None
    await session.page.frame(name="nav").get_by_role("link", name="Member Search").click()  # type: ignore[union-attr]
    await main.get_by_text("Last Name").wait_for()
    assert await main.locator("text=Member Number must be 6 digits.").count() == 1
    assert await call(main, "textVisible", "Member Number must be 6 digits") is False
    assert await call(main, "textVisible", "Last Name") is True


async def test_result_rows_carry_table_context(session: WebSession, mock: MockServer) -> None:
    await sign_on(session.page, mock.base_url())
    page = session.page
    await page.frame(name="nav").get_by_role("link", name="Member Search").click()  # type: ignore[union-attr]
    main = page.frame(name="main")
    assert main is not None
    await main.get_by_text("Last Name").wait_for()
    handles = await main.evaluate_handle("() => window.__rote.resolveLabel('Last Name', 'textbox')")
    first = (await handles.get_properties())["0"].as_element()
    assert first is not None
    await first.fill("P")  # Pike and Park
    await main.get_by_role("button", name="Search").click()
    await main.get_by_text("Search Results").wait_for()
    observation = await observe(page)
    views = [e for e in controls(observation, "main") if e["name"] == "View"]
    assert len(views) == 2
    rows = {e["table"]["row"][0] for e in views}
    assert rows == {"100517", "100666"}
    assert views[0]["table"]["headers"][:2] == ["Member #", "Name"]


async def test_readable_values_are_indexed_with_their_context(session: WebSession, mock: MockServer) -> None:
    await sign_on(session.page, mock.base_url())
    await session.page.goto(f"{mock.base_url()}/login")  # fresh page load keeps the session cookie
    await sign_on(session.page, mock.base_url())
    main = session.page.frame(name="main")
    assert main is not None
    sid = main.url.split("sid=")[1]
    await main.goto(f"{mock.base_url()}/member?mid=100234&sid={sid}")
    await main.get_by_text("Accounts").wait_for()
    observation = await observe(session.page)
    cells = [e for e in controls(observation, "main") if e["role"] == "cell"]
    balance = next(c for c in cells if (c.get("table") or {}).get("column") == "Balance" and "Share Savings" in c["table"]["row"])
    assert balance["text"] == "$1,234.56"
    member_number = next(c for c in cells if (c.get("label") or {}).get("text") == "Member #")
    assert member_number["text"] == "100234"
    assert "Readable values:" in render(observation)
