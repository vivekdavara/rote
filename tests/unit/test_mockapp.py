"""Smoke tests for the CoreOne mock over HTTP (no browser)."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi.testclient import TestClient

from mockapp.app import create_app
from mockapp.state import field_names

PASSWORD = "training-only-2026"


def client_for(tenant: str = "harbor", seed: int = 7) -> tuple[TestClient, dict[str, str]]:
    app = create_app(seed=seed, faults_enabled=True)
    client = TestClient(app, base_url=f"http://{tenant}.localhost")
    return client, app.state.coreone.tenants[tenant].names


def sign_on(client: TestClient, names: dict[str, str]) -> str:
    response = client.post(
        "/login",
        data={names["login_user"]: "svc_rote", names["login_pass"]: PASSWORD},
        follow_redirects=False,
    )
    assert response.status_code == 302, response.text
    return parse_qs(urlsplit(response.headers["location"]).query)["sid"][0]


def search(client: TestClient, names: dict[str, str], sid: str, member: str, **extra: str) -> str:
    data = {names["search_member"]: member, names["search_last"]: "", **extra}
    return client.post(f"/search?sid={sid}", data=data).text


def test_login_rejects_bad_password() -> None:
    client, names = client_for()
    page = client.post("/login", data={names["login_user"]: "svc_rote", names["login_pass"]: "nope"}).text
    assert "Invalid user ID or password." in page


def test_frameset_and_banner_fingerprint() -> None:
    client, names = client_for()
    sid = sign_on(client, names)
    frameset = client.get(f"/main?sid={sid}").text
    assert '<frame name="main"' in frameset and '<frame name="nav"' in frameset
    assert "Member Servicing v4.2.7" in client.get(f"/banner?sid={sid}").text


def test_search_found_not_found_and_invalid() -> None:
    client, names = client_for()
    sid = sign_on(client, names)
    found = search(client, names, sid, "100234")
    assert "Search Results" in found and "Avery Quill" in found and "__doPostBack" in found
    assert "No members found matching the search criteria." in search(client, names, sid, "999999")
    invalid = search(client, names, sid, "12ab")
    assert 'style="display:inline;"><font color="#cc0000">Member Number must be 6 digits.' in invalid


def test_hidden_validation_message_is_in_dom_but_hidden() -> None:
    client, names = client_for()
    sid = sign_on(client, names)
    page = client.get(f"/search?sid={sid}").text
    assert 'style="display:none;"><font color="#cc0000">Member Number must be 6 digits.' in page


def test_postback_select_opens_member_detail() -> None:
    client, names = client_for()
    sid = sign_on(client, names)
    search(client, names, sid, "100234")
    response = client.post(f"/search/select?sid={sid}", data={"__EVENTARGUMENT": "Select$0"}, follow_redirects=False)
    assert response.headers["location"] == f"/member?mid=100234&sid={sid}"
    detail = client.get(response.headers["location"]).text
    assert "Member Detail" in detail and "$1,234.56" in detail and "XXX-XX-0234" in detail
    assert "900-12-0234" not in detail


def test_restricted_member() -> None:
    client, names = client_for()
    sid = sign_on(client, names)
    assert "Access to this member record is restricted." in client.get(f"/member?mid=100900&sid={sid}").text


def test_session_id_must_match_cookie() -> None:
    client, names = client_for()
    sign_on(client, names)
    response = client.get("/search?sid=forged", follow_redirects=False)
    assert response.headers["location"] == "/login?expired=1"


def open_review(client: TestClient, names: dict[str, str], sid: str, deposit: str) -> str:
    data = {
        names["sub_type"]: "Holiday Club",
        names["sub_nick"]: "Holiday 2026",
        names["sub_deposit"]: deposit,
        names["sub_funding"]: "S00",
        names["sub_disclosure"]: "1",
    }
    return client.post(f"/subaccount/review?mid=100234&sid={sid}", data=data).text


def test_sub_account_validation_and_single_commit() -> None:
    client, names = client_for()
    sid = sign_on(client, names)
    assert "Initial deposit must be at least $5.00." in open_review(client, names, sid, "1.00")
    assert "exceeds the available balance" in open_review(client, names, sid, "5000")

    review = open_review(client, names, sid, "25.00")
    assert "Please review and confirm." in review
    token = re.search(r'name="reviewToken" value="([0-9a-f]+)"', review)
    assert token is not None
    done = client.post(f"/subaccount/confirm?mid=100234&sid={sid}", data={"reviewToken": token.group(1)}).text
    assert "Sub-Account Opened" in done and "Confirmation #" in done
    again = client.post(f"/subaccount/confirm?mid=100234&sid={sid}", data={"reviewToken": token.group(1)}).text
    assert "This request has already been processed." in again
    assert client.get("/__state").json()["harbor"]["commits"] == 1


@pytest.mark.parametrize(
    ("fault", "page", "expected", "status"),
    [
        ("interstitial", "search", "Security Notice", 200),
        ("unknown_modal", "search", "Fraud Alert", 200),
        ("js_dialog", "search", "pending mail items", 200),
        ("app_error", "search", "Server Error in '/CoreOne' Application.", 500),
        ("app_unavailable", "search", "Service Unavailable", 503),
    ],
)
def test_faults_fire_once_on_their_page(fault: str, page: str, expected: str, status: int) -> None:
    client, names = client_for()
    sid = sign_on(client, names)
    assert client.post("/__faults", json={"fault": fault, "page": page}).json() == {"ok": True}
    first = client.get(f"/search?sid={sid}")
    assert first.status_code == status and expected in first.text
    assert expected not in client.get(f"/search?sid={sid}").text


def test_session_expire_fault_redirects_to_login() -> None:
    client, names = client_for()
    sid = sign_on(client, names)
    client.post("/__faults", json={"fault": "session_expire", "page": "search"})
    response = client.get(f"/search?sid={sid}", follow_redirects=False)
    assert response.headers["location"] == "/login?expired=1"
    assert "Your session has expired. Please sign on again." in client.get("/login?expired=1").text


def test_summit_is_the_same_product_configured_differently() -> None:
    client, names = client_for("summit")
    sid = sign_on(client, names)
    assert "Find Member" in client.get(f"/nav?sid={sid}").text
    form = client.get(f"/search?sid={sid}").text
    assert "Account Holder No.:" in form and "Branch:" in form
    assert "Branch is required." in search(client, names, sid, "100234")
    assert "Search Results" in search(client, names, sid, "100234", **{names["search_branch"]: "Main Office"})


def test_control_names_change_with_the_seed() -> None:
    assert field_names(1, "harbor") != field_names(2, "harbor")
    assert field_names(1, "harbor") != field_names(1, "summit")
    assert field_names(3, "harbor") == field_names(3, "harbor")


def test_unknown_host_is_not_served() -> None:
    client = TestClient(create_app(seed=1, faults_enabled=False), base_url="http://evil.localhost")
    assert client.get("/login").status_code == 404
