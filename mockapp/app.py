"""CoreOne Member Servicing: a mock back-office app built to be hostile to automation.

It is the proxy target for rote. It is modeled on legacy core-banking screens:

* a nested ``<frameset>`` (banner / nav / main) with ``target=`` links
* table layouts, ``<font>`` tags, labels in the adjacent cell (no ``<label>`` elements)
* ASP.NET-style control names regenerated from ``COREONE_SEED`` (no stable ids, no test ids)
* ``javascript:__doPostBack`` links and a random ``__VIEWSTATE`` per page
* the session id repeated in the query string

Tenants are routed by host (``harbor.localhost``, ``summit.localhost``). They run the
same product at different versions and configurations.

With ``COREONE_FAULTS=1``, ``/__faults`` injects deterministic, counted faults so every
error path in rote can be exercised on demand. See ``FAULT_NAMES`` in ``state.py``.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import secrets
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from mockapp.state import HERE, AppState, Session, TenantState, money

COOKIE = "CoreOneSession"
GO_GIF = base64.b64decode("R0lGODlhAQABAIAAAMDAwAAAACH5BAEAAAAALAAAAAABAAEAAAICRAEAOw==")
MIN_DEPOSIT = Decimal("5.00")
SHARE_TYPES = ("Regular Savings", "Holiday Club", "Money Market")


def autolink(text: str) -> Markup:
    """Old servicing screens often linkified URLs in free-text notes."""
    return Markup(re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', str(escape(text))))


def create_app(seed: int | None = None, faults_enabled: bool | None = None) -> FastAPI:
    if seed is None:
        seed = int(os.environ.get("COREONE_SEED") or secrets.randbelow(10**6))
    if faults_enabled is None:
        faults_enabled = os.environ.get("COREONE_FAULTS") == "1"
    state = AppState(seed, faults_enabled)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    templates.env.globals["money"] = money
    templates.env.filters["autolink"] = autolink

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state.coreone = state

    # ------------------------------------------------------------------ helpers

    def tenant_of(request: Request) -> TenantState | None:
        host = request.headers.get("host", "").split(":")[0].lower()
        if host in ("localhost", "127.0.0.1", "testserver"):
            return state.tenants["harbor"]
        if host.endswith(".localhost"):
            return state.tenants.get(host.removesuffix(".localhost"))
        return None

    def unknown_site() -> HTMLResponse:
        return HTMLResponse("<html><body><h3>Unknown site</h3></body></html>", status_code=404)

    def session_for(request: Request, tenant: TenantState, page: str) -> Session | None:
        session = state.sessions.get(request.cookies.get(COOKIE, ""))
        if session is None or session.tenant != tenant.config["id"]:
            return None
        if state.faults_enabled and state.take_fault(tenant.config["id"], "session_expire", page):
            session.expired = True
        if session.expired:
            return None
        sid = request.query_params.get("sid")
        if sid is not None and sid != session.sid:
            return None
        return session

    def expired() -> RedirectResponse:
        return RedirectResponse("/login?expired=1", status_code=302)

    async def render(
        request: Request,
        tenant: TenantState,
        page: str,
        template: str,
        context: dict[str, Any],
        session: Session | None = None,
        status: int = 200,
    ) -> Response:
        tid = tenant.config["id"]
        ctx: dict[str, Any] = {
            "tenant": tenant.config,
            "labels": tenant.config["labels"],
            "f": tenant.names,
            "sid": session.sid if session else "",
            "user": session.user if session else "",
            "viewstate": secrets.token_urlsafe(24),
            "overlay": None,
            "alert": None,
            **context,
        }
        if state.faults_enabled:
            latency = state.take_fault(tid, "latency", page)
            if latency:
                await asyncio.sleep(latency.ms / 1000)
            if state.take_fault(tid, "app_error", page):
                return templates.TemplateResponse(request, "error500.html", ctx, status_code=500)
            if state.take_fault(tid, "app_unavailable", page):
                return templates.TemplateResponse(request, "error503.html", ctx, status_code=503)
            if state.take_fault(tid, "interstitial", page):
                ctx["overlay"] = "security"
            elif state.take_fault(tid, "unknown_modal", page):
                ctx["overlay"] = "fraud"
            if state.take_fault(tid, "js_dialog", page):
                ctx["alert"] = "This member has pending mail items."
        return templates.TemplateResponse(request, template, ctx, status_code=status)

    def member_or_none(tenant: TenantState, mid: str | None) -> dict[str, Any] | None:
        return tenant.members.get(mid or "")

    # --------------------------------------------------------------- static bits

    @app.get("/favicon.ico")
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/static/go.gif")
    async def go_gif() -> Response:
        return Response(GO_GIF, media_type="image/gif")

    @app.get("/")
    async def root() -> Response:
        return RedirectResponse("/login", status_code=302)

    # ------------------------------------------------------------------ sign on

    @app.get("/login")
    async def login_page(request: Request) -> Response:
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        return await render(
            request,
            tenant,
            "login",
            "login.html",
            {"expired": request.query_params.get("expired") == "1", "failed": False},
        )

    @app.post("/login")
    async def login_submit(request: Request) -> Response:
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        form = await request.form()
        user = str(form.get(tenant.names["login_user"], "")).strip()
        password = str(form.get(tenant.names["login_pass"], ""))
        operator = state.operators.get(user)
        if operator is None or operator["password"] != password:
            return await render(request, tenant, "login", "login.html", {"expired": False, "failed": True})
        session = state.new_session(tenant.config["id"], user)
        response = RedirectResponse(f"/main?sid={session.sid}", status_code=302)
        response.set_cookie(COOKIE, session.sid, httponly=True, path="/")
        return response

    @app.get("/logout")
    async def logout(request: Request) -> Response:
        session = state.sessions.pop(request.cookies.get(COOKIE, ""), None)
        del session
        response = RedirectResponse("/login", status_code=302)
        response.delete_cookie(COOKIE, path="/")
        return response

    # ------------------------------------------------------------------- frames

    @app.get("/main")
    async def main_frameset(request: Request) -> Response:
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        session = session_for(request, tenant, "main")
        if session is None:
            return expired()
        return await render(request, tenant, "main", "frameset.html", {}, session)

    @app.get("/banner")
    async def banner(request: Request) -> Response:
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        session = session_for(request, tenant, "banner")
        if session is None:
            return expired()
        return await render(request, tenant, "banner", "banner.html", {}, session)

    @app.get("/nav")
    async def nav(request: Request) -> Response:
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        session = session_for(request, tenant, "nav")
        if session is None:
            return expired()
        return await render(request, tenant, "nav", "nav.html", {}, session)

    @app.get("/home")
    async def home(request: Request) -> Response:
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        session = session_for(request, tenant, "home")
        if session is None:
            return expired()
        return await render(request, tenant, "home", "home.html", {}, session)

    @app.get("/admin")
    async def admin(request: Request) -> Response:
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        session = session_for(request, tenant, "admin")
        if session is None:
            return expired()
        return await render(
            request,
            tenant,
            "admin",
            "message.html",
            {"title_text": "Administration", "error": "You are not authorized to access this function."},
            session,
            status=403,
        )

    @app.get("/inquiry")
    @app.get("/reports")
    async def unavailable(request: Request) -> Response:
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        session = session_for(request, tenant, "other")
        if session is None:
            return expired()
        return await render(
            request,
            tenant,
            "other",
            "message.html",
            {"title_text": "Unavailable", "error": "This function is not available in training mode."},
            session,
        )

    # ------------------------------------------------------------ member search

    def search_context(tenant: TenantState, **extra: Any) -> dict[str, Any]:
        return {
            "branch_required": tenant.config.get("branch_required", False),
            "branches": tenant.config.get("branches", []),
            "values": {},
            "errors": [],
            "invalid_member_number": False,
            "results": None,
            **extra,
        }

    @app.get("/search")
    async def search_form(request: Request) -> Response:
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        session = session_for(request, tenant, "search")
        if session is None:
            return expired()
        return await render(request, tenant, "search", "search.html", search_context(tenant), session)

    @app.post("/search")
    async def search_submit(request: Request) -> Response:
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        session = session_for(request, tenant, "results")
        if session is None:
            return expired()
        form = await request.form()
        member_number = str(form.get(tenant.names["search_member"], "")).strip()
        last_name = str(form.get(tenant.names["search_last"], "")).strip()
        branch = str(form.get(tenant.names["search_branch"], "")).strip()
        values = {"member": member_number, "last": last_name, "branch": branch}
        errors: list[str] = []
        invalid_number = bool(member_number) and not (member_number.isdigit() and len(member_number) == 6)
        if tenant.config.get("branch_required") and not branch:
            errors.append("Branch is required.")
        if not member_number and not last_name:
            errors.append("Enter a member number or a last name.")
        if errors or invalid_number:
            ctx = search_context(tenant, values=values, errors=errors, invalid_member_number=invalid_number)
            return await render(request, tenant, "results", "search.html", ctx, session)

        members = list(tenant.members.values())
        if member_number:
            found = [m for m in members if m["member_id"] == member_number]
        else:
            found = [m for m in members if m["name"].split()[-1].lower().startswith(last_name.lower())]
        session.last_results = [m["member_id"] for m in found]
        ctx = search_context(tenant, values=values, results=found)
        return await render(request, tenant, "results", "search.html", ctx, session)

    @app.post("/search/select")
    async def search_select(request: Request) -> Response:
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        session = session_for(request, tenant, "select")
        if session is None:
            return expired()
        form = await request.form()
        argument = str(form.get("__EVENTARGUMENT", ""))
        try:
            index = int(argument.removeprefix("Select$"))
            member_id = session.last_results[index]
        except (ValueError, IndexError):
            return RedirectResponse(f"/search?sid={session.sid}", status_code=302)
        return RedirectResponse(f"/member?mid={member_id}&sid={session.sid}", status_code=302)

    @app.get("/member")
    async def member_detail(request: Request) -> Response:
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        session = session_for(request, tenant, "member")
        if session is None:
            return expired()
        member = member_or_none(tenant, request.query_params.get("mid"))
        if member is None:
            return await render(
                request, tenant, "member", "message.html",
                {"title_text": "Member Detail", "error": "Member record not found."}, session,
            )
        return await render(request, tenant, "member", "member.html", {"m": member}, session)

    # -------------------------------------------------------------- sub-account

    def funding_options(member: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            a for a in member["accounts"]
            if Decimal(a["available"]) > 0 and a["type"] in ("Share Savings", "Share Draft Checking")
        ]

    def subaccount_context(member: dict[str, Any], **extra: Any) -> dict[str, Any]:
        return {"m": member, "share_types": SHARE_TYPES, "funding": funding_options(member), "values": {},
                "errors": [], **extra}

    @app.get("/subaccount/new")
    async def subaccount_form(request: Request) -> Response:
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        session = session_for(request, tenant, "subaccount_form")
        if session is None:
            return expired()
        member = member_or_none(tenant, request.query_params.get("mid"))
        if member is None or member.get("restricted"):
            return RedirectResponse(f"/search?sid={session.sid}", status_code=302)
        return await render(request, tenant, "subaccount_form", "subaccount_form.html",
                            subaccount_context(member), session)

    @app.post("/subaccount/review")
    async def subaccount_review(request: Request) -> Response:
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        session = session_for(request, tenant, "subaccount_review")
        if session is None:
            return expired()
        member = member_or_none(tenant, request.query_params.get("mid"))
        if member is None or member.get("restricted"):
            return RedirectResponse(f"/search?sid={session.sid}", status_code=302)
        form = await request.form()
        values = {
            "type": str(form.get(tenant.names["sub_type"], "")),
            "nick": str(form.get(tenant.names["sub_nick"], "")).strip(),
            "deposit": str(form.get(tenant.names["sub_deposit"], "")).strip(),
            "funding": str(form.get(tenant.names["sub_funding"], "")),
            "disclosure": form.get(tenant.names["sub_disclosure"]) is not None,
        }
        errors: list[str] = []
        funding = next((a for a in funding_options(member) if a["suffix"] == values["funding"]), None)
        if values["type"] not in SHARE_TYPES:
            errors.append("Select a share type.")
        try:
            deposit = Decimal(values["deposit"].replace("$", "").replace(",", ""))
        except InvalidOperation:
            deposit = None
            errors.append("Initial deposit must be a dollar amount.")
        if deposit is not None and deposit < MIN_DEPOSIT:
            errors.append("Initial deposit must be at least $5.00.")
        if funding is None:
            errors.append("Select a funding account.")
        elif deposit is not None and deposit > Decimal(funding["available"]):
            errors.append("Initial deposit exceeds the available balance in the funding account.")
        if not values["disclosure"]:
            errors.append("Truth-in-Savings disclosure must be provided before opening a sub-account.")
        if len(values["nick"]) > 20:
            errors.append("Nickname must be 20 characters or fewer.")
        if errors or deposit is None or funding is None:
            return await render(request, tenant, "subaccount_review", "subaccount_form.html",
                                subaccount_context(member, values=values, errors=errors), session)
        token = secrets.token_hex(8)
        session.reviews[token] = {
            "member_id": member["member_id"],
            "type": values["type"],
            "nick": values["nick"],
            "deposit": str(deposit.quantize(Decimal("0.01"))),
            "funding": funding["suffix"],
            "funding_label": f'{funding["suffix"]} - {funding["description"]}',
        }
        return await render(request, tenant, "subaccount_review", "subaccount_review.html",
                            {"m": member, "r": session.reviews[token], "token": token}, session)

    @app.post("/subaccount/confirm")
    async def subaccount_confirm(request: Request) -> Response:
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        session = session_for(request, tenant, "subaccount_confirm")
        if session is None:
            return expired()
        form = await request.form()
        token = str(form.get("reviewToken", ""))
        review = session.reviews.get(token)
        if review is None or token in tenant.processed_reviews:
            return await render(
                request, tenant, "subaccount_confirm", "message.html",
                {"title_text": "Open Sub-Account", "error": "This request has already been processed."}, session,
            )
        tenant.processed_reviews.add(token)
        member = tenant.members[review["member_id"]]
        used = {a["suffix"] for a in member["accounts"]}
        suffix = next(f"S{n}" for n in range(12, 80) if f"S{n}" not in used)
        deposit = Decimal(review["deposit"])
        source = next(a for a in member["accounts"] if a["suffix"] == review["funding"])
        source["balance"] = str(Decimal(source["balance"]) - deposit)
        source["available"] = str(Decimal(source["available"]) - deposit)
        member["accounts"].append({
            "type": "Share Savings", "suffix": suffix, "description": review["nick"] or review["type"],
            "balance": str(deposit), "available": str(deposit),
        })
        tenant.commits += 1
        confirmation = f"{tenant.next_confirmation:06d}"
        tenant.next_confirmation += 1
        if state.faults_enabled:
            stall = state.take_fault(tenant.config["id"], "confirm_timeout", "subaccount_confirm")
            if stall:
                await asyncio.sleep((stall.ms or 30_000) / 1000)
        return await render(
            request, tenant, "subaccount_confirm", "subaccount_done.html",
            {"m": member, "r": review, "suffix": suffix, "confirmation": confirmation}, session,
        )

    # ---------------------------------------------------------- fault injection

    def local_only(request: Request) -> JSONResponse | None:
        if not state.faults_enabled:
            return JSONResponse({"error": "fault injection disabled; set COREONE_FAULTS=1"}, status_code=404)
        client = request.client.host if request.client else ""
        if client not in ("127.0.0.1", "::1", "testclient"):
            return JSONResponse({"error": "localhost only"}, status_code=403)
        return None

    @app.post("/__faults")
    async def add_fault(request: Request) -> Response:
        if (denied := local_only(request)) is not None:
            return denied
        tenant = tenant_of(request)
        if tenant is None:
            return unknown_site()
        body = await request.json()
        try:
            state.add_fault(tenant.config["id"], body["fault"], body.get("page"), int(body.get("times", 1)),
                            int(body.get("ms", 0)))
        except (KeyError, ValueError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return JSONResponse({"ok": True})

    @app.delete("/__faults")
    async def clear_faults(request: Request) -> Response:
        if (denied := local_only(request)) is not None:
            return denied
        for tenant in state.tenants.values():
            tenant.faults.clear()
        return JSONResponse({"ok": True})

    @app.post("/__reset")
    async def reset(request: Request) -> Response:
        if (denied := local_only(request)) is not None:
            return denied
        state.reset()
        return JSONResponse({"ok": True})

    @app.get("/__state")
    async def inspect(request: Request) -> Response:
        if (denied := local_only(request)) is not None:
            return denied
        return JSONResponse({
            tid: {
                "commits": t.commits,
                "faults": [f.__dict__ for f in t.faults if f.remaining > 0],
                "sessions": sum(1 for s in state.sessions.values() if s.tenant == tid),
            }
            for tid, t in state.tenants.items()
        })

    return app
