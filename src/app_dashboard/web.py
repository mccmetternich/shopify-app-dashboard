import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

# Uvicorn only configures its own loggers; without this, app INFO lines
# (run_sync summaries, Slack skips) never reach the host's logs.
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

import httpx
from datetime import date
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app_dashboard.auth import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    STATE_COOKIE,
    allowed_principals,
    authorize_url,
    email_is_allowed,
    display_name,
    exchange_code,
    issue_session,
    new_state,
    read_session,
)
from app_dashboard import annotations as anno
from app_dashboard import export as json_export
from app_dashboard.config import get_settings
from app_dashboard.db import connect
from app_dashboard.faq import FAQ
from app_dashboard.markdown_export import PAGES as MD_PAGES
from app_dashboard.markdown_export import render_page
from app_dashboard.metrics import COMPARE_LABEL, METRICS, signed
from app_dashboard.ops import sync_health
from app_dashboard.ranges import (
    MONEY_MONTHS,
    choice,
)
from app_dashboard.scheduler import start_scheduler
from app_dashboard.security import RateLimiter, SecurityHeadersMiddleware, client_key
from app_dashboard.stats import (
    collected_revenue,
    customer_cohorts,
    days_of_cover,
    monthly_activity,
    mrr_movements,
    mrr_trend,
    overview_comparison,
    overview_stats,
    recent_events,
    revenue_by_month,
    subscription_retention,
    survey_tally,
    funnel_stats,
    funnel_by_source,
    abandoned_checkout_stats,
    discount_usage,
    revenue_by_sku,
    repeat_purchase_rate,
    refund_rate,
    omnisend_summary,
    generate_summary,
    kpi_sparklines,
    meta_channel_vitals,
    meta_campaign_breakdown,
    meta_top_ads,
)
from app_dashboard.usage import (
    MAX_BODY_BYTES,
    UsageError,
    has_usage_data,
    parse_batch,
)
from app_dashboard.usage import ingest as ingest_usage_events

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

# Header the app sends its shared secret in. A dedicated header rather than
# Authorization, so it can never collide with the Basic auth path.
USAGE_TOKEN_HEADER = "X-Usage-Token"

# auto_error=False so a browser with no Authorization header falls through to
# the Google redirect instead of getting a Basic auth popup.
security = HTTPBasic(auto_error=False)

# Valid window sizes for the overview time-range picker.
WINDOW_CHOICES = [7, 30, 90]


def _same_secret(supplied: str | None, expected: str | None) -> bool:
    """Constant-time compare that survives non-ASCII input."""
    if not supplied or not expected:
        return False
    return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


# Hosts where a weak session secret is tolerated, so local development works
# without ceremony.
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", ""}
MIN_SESSION_SECRET_BYTES = 32

LOGIN_LIMIT, LOGIN_WINDOW = 10, 300
INGEST_LIMIT, INGEST_WINDOW = 20, 60


def create_app(conn_factory) -> FastAPI:
    settings = get_settings()
    hostname = (urlparse(settings.public_base_url).hostname or "").lower()
    if hostname not in LOCAL_HOSTS and len(settings.session_secret.strip()) < MIN_SESSION_SECRET_BYTES:
        raise RuntimeError(
            f"SESSION_SECRET is missing or too short while PUBLIC_BASE_URL is "
            f"{settings.public_base_url!r}. Set at least "
            f"{MIN_SESSION_SECRET_BYTES} characters before serving: "
            'python -c "import secrets; print(secrets.token_urlsafe(32))"'
        )

    templates = Jinja2Templates(directory=TEMPLATES_DIR)

    def render_ms(request: Request) -> str:
        started = getattr(request.state, "started", None)
        if started is None:
            return ""
        ms = (time.perf_counter() - started) * 1000
        return f"{ms:.1f}" if ms < 10 else f"{ms:.0f}"

    templates.env.globals["render_ms"] = render_ms
    templates.env.globals["METRICS"] = METRICS
    templates.env.globals["COMPARE_LABEL"] = COMPARE_LABEL
    templates.env.globals["signed"] = signed
    templates.env.globals["GA4_PROPERTY_ID"] = None
    templates.env.globals["APP_NAME"] = settings.app_name
    templates.env.globals["DASHBOARD_NAME"] = settings.dashboard_name
    templates.env.globals["APP_LISTING_URL"] = settings.app_listing_url

    allowed = allowed_principals(settings.google_allowed_domains)
    sso_enabled = bool(settings.google_client_id and settings.google_client_secret)
    login_limiter = RateLimiter(LOGIN_LIMIT, LOGIN_WINDOW)
    ingest_limiter = RateLimiter(INGEST_LIMIT, INGEST_WINDOW)

    def _basic_user(credentials: HTTPBasicCredentials | None) -> str | None:
        if credentials is None:
            return None
        stored = settings.dashboard_users_map.get(credentials.username)
        pass_ok = _same_secret(credentials.password, stored or "\0invalid")
        return credentials.username if stored is not None and pass_ok else None

    def verify_creds(
        request: Request,
        credentials: HTTPBasicCredentials | None = Depends(security),
    ) -> str:
        email = read_session(settings.session_secret,
                             request.cookies.get(SESSION_COOKIE), allowed)
        if email:
            return email

        key = client_key(request)
        if credentials is not None and not login_limiter.check(key):
            raise HTTPException(status_code=429, detail="Too many attempts")

        user = _basic_user(credentials)
        if user:
            login_limiter.reset(key)
            return user
        if credentials is not None:
            login_limiter.record(key)

        if sso_enabled and credentials is None:
            code = 303 if request.method not in ("GET", "HEAD") else 307
            raise HTTPException(status_code=code, detail="sso",
                                headers={"Location": "/auth/login"})
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )

    def _display(request: Request, user: str) -> str:
        return display_name(settings.session_secret,
                            request.cookies.get(SESSION_COOKIE), user)

    def _session_email(request: Request) -> str | None:
        return read_session(settings.session_secret,
                            request.cookies.get(SESSION_COOKIE), allowed)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scheduler = None
        if not os.environ.get("NO_SCHEDULER"):
            scheduler = start_scheduler(conn_factory, settings)
        yield
        if scheduler is not None:
            scheduler.shutdown()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(SecurityHeadersMiddleware)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/robots.txt", include_in_schema=False)
    def robots():
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    RENDERED_STATUSES = (401, 403, 404)

    def _error_page(request: Request, exc: StarletteHTTPException,
                    signed_in: bool):
        if exc.status_code == 401:
            return ("Those credentials were not accepted",
                    "Check the username and password, or sign in with Google instead.",
                    "/auth/login", "Go to sign-in")
        if exc.status_code == 403:
            return ("Not on the list", str(exc.detail),
                    "/auth/login", "Try another account")
        if signed_in:
            return ("Page not found",
                    "The link may be stale. Every page in this scoreboard is in the sidebar.",
                    "/", "Back to Overview")
        return ("Page not found",
                "The link may be stale, or the page is one of the ones behind sign-in.",
                "/auth/login", "Go to sign-in")

    @app.exception_handler(StarletteHTTPException)
    async def render_http_exception(request: Request,
                                    exc: StarletteHTTPException):
        wants_html = "text/html" in request.headers.get("accept", "")
        if exc.status_code not in RENDERED_STATUSES or not wants_html:
            return await http_exception_handler(request, exc)
        signed_in = bool(read_session(settings.session_secret,
                                      request.cookies.get(SESSION_COOKIE),
                                      allowed))
        title, body, href, text = _error_page(request, exc, signed_in)
        response = templates.TemplateResponse(
            request, "error.html",
            {"user": display_name(settings.session_secret,
                                  request.cookies.get(SESSION_COOKIE), ""),
             "active": None, "signed_in": signed_in,
             "art": f"/static/error-{exc.status_code}.webp",
             "title": title, "body": body,
             "link_href": href, "link_text": text},
            status_code=exc.status_code,
        )
        for key, value in (exc.headers or {}).items():
            response.headers[key] = value
        return response

    @app.get("/healthz")
    def healthz():
        return {"status": "ok"}

    def _check_usage_token(request: Request) -> None:
        key = client_key(request)
        if not ingest_limiter.check(key):
            raise HTTPException(status_code=429, detail="Too many requests")
        if not _same_secret(request.headers.get(USAGE_TOKEN_HEADER),
                            settings.usage_ingest_token):
            ingest_limiter.record(key)
            raise HTTPException(status_code=401, detail="Unauthorized")

    async def _read_capped(request: Request) -> bytes:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Payload too large")
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Payload too large")
            chunks.append(chunk)
        return b"".join(chunks)

    @app.post("/ingest/usage")
    async def ingest_usage(request: Request):
        _check_usage_token(request)
        raw = await _read_capped(request)
        try:
            events = parse_batch(raw)
        except UsageError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from None
        conn = conn_factory()
        try:
            result = ingest_usage_events(conn, events)
        finally:
            conn.close()
        return result

    redirect_uri = settings.public_base_url.rstrip("/") + "/auth/callback"

    @app.get("/auth/login")
    def auth_login(request: Request):
        return templates.TemplateResponse(
            request, "login.html",
            {"user": None, "active": None, "signed_in": False,
             "art": "/static/login.webp", "sso_enabled": sso_enabled},
        )

    @app.get("/auth/google")
    def auth_google():
        if not sso_enabled:
            raise HTTPException(status_code=404, detail="SSO not configured")
        state = new_state()
        response = RedirectResponse(
            authorize_url(settings.google_client_id, redirect_uri, state)
        )
        response.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True,
                            secure=True, samesite="lax")
        return response

    @app.get("/auth/callback")
    def auth_callback(request: Request, code: str | None = None,
                      state: str | None = None):
        if not sso_enabled:
            raise HTTPException(status_code=404, detail="SSO not configured")
        if not code or not _same_secret(state, request.cookies.get(STATE_COOKIE)):
            raise HTTPException(status_code=400, detail="Invalid OAuth state")

        email, name = exchange_code(settings.google_client_id,
                                    settings.google_client_secret,
                                    redirect_uri, code, post=httpx.post, get=httpx.get)
        if not email_is_allowed(email, allowed):
            logger.warning("rejected Google sign-in for %r", email)
            raise HTTPException(
                status_code=403,
                detail=f"{email or 'That account'} is not allowed. Sign in with "
                       f"an authorized email address.",
            )

        response = RedirectResponse("/", status_code=303)
        response.set_cookie(SESSION_COOKIE,
                            issue_session(settings.session_secret, email, name),
                            max_age=SESSION_MAX_AGE, httponly=True,
                            secure=True, samesite="lax")
        response.delete_cookie(STATE_COOKIE)
        logger.info("signed in %s", email)
        return response

    @app.get("/auth/logout")
    def auth_logout():
        response = RedirectResponse("/auth/login" if sso_enabled else "/", status_code=303)
        response.delete_cookie(SESSION_COOKIE)
        return response

    # ── Overview (/): Phase C rewrite ─────────────────────────────────────────
    @app.get("/")
    def overview(request: Request, window: int = 7,
                 user: str = Depends(verify_creds)):
        window = window if window in WINDOW_CHOICES else 7
        conn = conn_factory()
        try:
            stats = overview_stats(conn, window_days=window)
            prior_stats = overview_stats(conn, window_days=window * 2)
            # For prior period we want just the previous window, not double window.
            # Recompute prior as window immediately before current window.
            from datetime import datetime, timedelta, timezone as tz
            from app_dashboard.stats import _utcnow
            now = _utcnow()
            window_start = now - timedelta(days=window)
            prior_end = window_start
            prior_start = prior_end - timedelta(days=window)

            # Build prior dict by running overview_stats equivalent over prior slice
            def prior_scalar(sql, params=()):
                row = conn.execute(sql, params).fetchone()
                return row[0] if row else None

            from decimal import Decimal as D
            prior_rev = prior_scalar(
                "select coalesce(sum(total - refunded), null) from orders "
                "where created_at >= %s and created_at < %s",
                (prior_start, prior_end),
            )
            prior_new_cust = prior_scalar(
                "select count(distinct customer_id) from orders "
                "where is_new_customer = true and created_at >= %s and created_at < %s",
                (prior_start, prior_end),
            ) or 0
            prior_spend = prior_scalar(
                "select coalesce(sum(spend), null) from ad_spend "
                "where date >= %s::date and date < %s::date",
                (prior_start, prior_end),
            )
            prior_order_count = prior_scalar(
                "select count(*) from orders where created_at >= %s and created_at < %s",
                (prior_start, prior_end),
            ) or 0

            prior_cac = (prior_spend / D(prior_new_cust)
                         if prior_spend and prior_new_cust else None)
            prior_mer = (prior_rev / prior_spend
                         if prior_rev and prior_spend and prior_spend > 0 else None)
            prior_subs = prior_scalar(
                "select count(distinct customer_id) from subscription_revenue "
                "where converted_at >= %s and converted_at < %s",
                (prior_start, prior_end),
            ) or 0
            prior_sub_share = (
                D("100") * D(prior_subs) / D(prior_new_cust)
                if prior_new_cust else None
            )
            prior_aov = (
                prior_rev / D(prior_order_count)
                if prior_rev and prior_order_count else None
            )

            prior = {
                "revenue": prior_rev,
                "new_customers": prior_new_cust,
                "blended_cac": prior_cac,
                "mer": prior_mer,
                "subscription_share": prior_sub_share,
                "aov": prior_aov,
                "days_of_cover": None,
            }

            comparison = overview_comparison(stats, prior)

            # Days of cover is a point metric, computed separately
            doc = days_of_cover(conn, settings.serum_sku)
            stats["days_of_cover"] = doc

            health = sync_health(conn)
            # Format last_synced_at as "2:14 pm" for the banner
            if health["last_synced_at"]:
                health["last_synced_at_fmt"] = health["last_synced_at"].strftime("%-I:%M %p").lower()
            else:
                health["last_synced_at_fmt"] = None

            notes = anno.recent(conn)
            notes_by_month = anno.by_month(conn)
            months_val = 12
            trend = mrr_trend(conn, months_val)
            movements = mrr_movements(conn, months_val)
            revenue = revenue_by_month(conn, months_val)
            activity = monthly_activity(conn)
            funnel = funnel_stats(conn, window)
            # Identify weakest funnel step
            steps = {
                "atc": funnel.get("atc_rate"),
                "checkout": funnel.get("checkout_rate"),
                "purchase": funnel.get("purchase_rate"),
            }
            valid_steps = {k: v for k, v in steps.items() if v is not None}
            funnel["weakest_step"] = min(valid_steps, key=valid_steps.get) if valid_steps else None
            funnel_sources = funnel_by_source(conn, window)
            cart = abandoned_checkout_stats(conn, window)
            discounts = discount_usage(conn, window)
            sku_revenue = revenue_by_sku(conn, window)
            repeat_rate = repeat_purchase_rate(conn, window)
            refunds = refund_rate(conn, window)
            omnisend = omnisend_summary(conn, window, stats.get("revenue"))
            meta = meta_channel_vitals(conn, window)
            meta_campaigns = meta_campaign_breakdown(conn, window)
            sparklines = kpi_sparklines(conn, days=window)
            summary_line = generate_summary(stats, comparison, window)
        finally:
            conn.close()

        trend_max = max([m["mrr"] for m in trend] + [1])
        activity_max = max(
            [m["installs"] for m in activity] + [m["uninstalls"] for m in activity] + [1]
        )
        movement_scale = max(
            [m["new"] + m["reactivation"] + m["expansion"] for m in movements]
            + [-(m["contraction"] + m["churned"]) for m in movements] + [1]
        )
        revenue_max = max([m["revenue"] for m in revenue] + [1])

        return templates.TemplateResponse(
            request,
            "overview.html",
            {
                "user": _display(request, user),
                "active": "overview",
                "stats": stats,
                "comparison": comparison,
                "window": window,
                "window_choices": WINDOW_CHOICES,
                "health": health,
                "notes": notes,
                "notes_by_month": notes_by_month,
                "note_max": anno.NOTE_MAX,
                "today": date.today().isoformat(),
                "can_annotate": bool(_session_email(request)),
                "note_error": request.query_params.get("note_error"),
                "trend": trend,
                "trend_max": trend_max,
                "movements": movements,
                "movement_scale": movement_scale,
                "revenue": revenue,
                "revenue_max": revenue_max,
                "activity": activity,
                "activity_max": activity_max,
                "months": months_val,
                "month_choices": MONEY_MONTHS,
                "funnel": funnel,
                "funnel_sources": funnel_sources,
                "cart": cart,
                "discounts": discounts,
                "sku_revenue": sku_revenue,
                "repeat_rate": repeat_rate,
                "refunds": refunds,
                "omnisend": omnisend,
                "meta": meta,
                "meta_campaigns": meta_campaigns,
                "sparklines": sparklines,
                "summary_line": summary_line,
                "launch_date": settings.launch_date,
            },
        )

    # ── Annotations (kept from original) ──────────────────────────────────────

    async def _annotation_form(request: Request) -> tuple[str, dict]:
        email = _session_email(request)
        if not email:
            raise HTTPException(
                status_code=403,
                detail="Changing a note needs a browser session. Sign in with "
                       "Google rather than a username and password.",
            )
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != settings.public_base_url.rstrip("/"):
            raise HTTPException(status_code=403, detail="Cross-origin write refused")
        content_type = request.headers.get("content-type", "")
        if not content_type.startswith("application/x-www-form-urlencoded"):
            raise HTTPException(status_code=415, detail="Send a form body")
        body = await _read_capped(request)
        return email, parse_qs(body.decode("utf-8", "replace"))

    def _back_to_notes(error: str | None = None) -> RedirectResponse:
        if error:
            return RedirectResponse(
                "/?" + urlencode({"note_error": error}) + "#annotations",
                status_code=303)
        return RedirectResponse("/#annotations", status_code=303)

    @app.post("/annotations")
    async def add_annotation(request: Request,
                             user: str = Depends(verify_creds)):
        email, form = await _annotation_form(request)
        on_date = (form.get("on_date") or [""])[0]
        note = (form.get("note") or [""])[0]
        conn = conn_factory()
        try:
            anno.add(conn, on_date=on_date, note=note, author=email)
        except anno.AnnotationError as exc:
            return _back_to_notes(str(exc))
        finally:
            conn.close()
        return _back_to_notes()

    @app.post("/annotations/delete")
    async def delete_annotation(request: Request,
                                user: str = Depends(verify_creds)):
        _, form = await _annotation_form(request)
        annotation_id = (form.get("id") or [""])[0]
        conn = conn_factory()
        try:
            gone = anno.remove(conn, annotation_id=annotation_id)
        except anno.AnnotationError as exc:
            return _back_to_notes(str(exc))
        finally:
            conn.close()
        if gone is None:
            return _back_to_notes("That note was already gone.")
        return _back_to_notes()

    # ── FAQ ───────────────────────────────────────────────────────────────────

    @app.get("/faq")
    def faq(request: Request, user: str = Depends(verify_creds)):
        return templates.TemplateResponse(
            request, "faq.html",
            {"user": _display(request, user), "active": "faq", "faq": FAQ},
        )

    # ── Export JSON ───────────────────────────────────────────────────────────

    @app.get("/export.json")
    def export_json(request: Request, user: str = Depends(verify_creds)):
        conn = conn_factory()
        try:
            body = json_export.render(conn, settings)
        finally:
            conn.close()
        return Response(
            body,
            media_type="application/json",
            headers={"content-disposition":
                     f'attachment; filename="{json_export.filename(slug=settings.slug)}"'},
        )

    # ── Cohorts page (new: Phase C) ───────────────────────────────────────────

    @app.get("/cohorts")
    def cohorts(request: Request, user: str = Depends(verify_creds)):
        conn = conn_factory()
        try:
            cohort_data = customer_cohorts(conn)
            sub_retention = subscription_retention(conn)
        finally:
            conn.close()
        return templates.TemplateResponse(
            request, "cohorts.html",
            {
                "user": _display(request, user),
                "active": "cohorts",
                "cohorts": cohort_data,
                "sub_retention": sub_retention,
            },
        )

    # ── Survey page (new: Phase C) ────────────────────────────────────────────

    @app.get("/survey")
    def survey(request: Request, window: str = "90",
               user: str = Depends(verify_creds)):
        # window: "30", "90", or "all"
        if window == "all":
            w_days = 0
        elif window == "30":
            w_days = 30
        else:
            window = "90"
            w_days = 90
        conn = conn_factory()
        try:
            tally = survey_tally(conn, window_days=w_days)
        finally:
            conn.close()
        total = sum(r["count"] for r in tally)
        return templates.TemplateResponse(
            request, "survey.html",
            {
                "user": _display(request, user),
                "active": "survey",
                "tally": tally,
                "total": total,
                "window": window,
            },
        )

    # ── Meta Ads page ─────────────────────────────────────────────────────────

    @app.get("/ads")
    def ads(request: Request, window: int = 7, user: str = Depends(verify_creds)):
        window = window if window in WINDOW_CHOICES else 7
        conn = conn_factory()
        try:
            meta = meta_channel_vitals(conn, window)
            campaigns = meta_campaign_breakdown(conn, window)
            top_ads = meta_top_ads(conn, window)
        finally:
            conn.close()
        return templates.TemplateResponse(
            request, "ads.html",
            {
                "user": _display(request, user),
                "active": "ads",
                "window": window,
                "window_choices": WINDOW_CHOICES,
                "meta": meta,
                "campaigns": campaigns,
                "top_ads": top_ads,
            },
        )

    # ── Markdown mirrors (.md twins) ──────────────────────────────────────────

    MD_SLUGS = {slug: page for page, (slug, _, _) in MD_PAGES.items()}

    def _markdown(request: Request, slug: str) -> PlainTextResponse:
        page = MD_SLUGS.get(slug)
        if page is None:
            raise HTTPException(status_code=404, detail="No such page")
        conn = conn_factory()
        try:
            text = render_page(conn, page, settings, dict(request.query_params))
        finally:
            conn.close()
        return PlainTextResponse(text, media_type="text/markdown; charset=utf-8")

    @app.get("/{slug}.md")
    def page_markdown(request: Request, slug: str, user: str = Depends(verify_creds)):
        return _markdown(request, slug)

    @app.get("/reports/{slug}.md")
    def report_markdown(request: Request, slug: str, user: str = Depends(verify_creds)):
        return _markdown(request, f"reports/{slug}")

    return app


# Production entrypoint for `uvicorn app_dashboard.web:app`.
app = create_app(connect)
