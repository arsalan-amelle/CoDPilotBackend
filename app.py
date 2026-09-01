from __future__ import annotations

import os
import json
import hmac
import hashlib
import threading
import time
from datetime import date
from decimal import Decimal

# Local development convenience: load variables from a .env file if present.
# On Vercel you should configure Environment Variables in the Vercel dashboard.
try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass

from flask import Flask, abort, g, jsonify, request
from flask_cors import CORS
from flask_migrate import Migrate
from sqlalchemy import event, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, with_loader_criteria
from werkzeug.security import check_password_hash, generate_password_hash
import requests

try:
    # Preferred for this repo: flat module layout.
    from models import (
        AdminUser,
        AppSetting,
        ShopifyCitySalesRecord,
        ShopifyProductCityRecord,
        ShopifyPricesRecord,
        ShopifyProductSalesRecord,
        ShopifyProductValueRecord,
        ShopifyReturnDetailRecord,
        db,
        TenantMixin,
    )
    from shopify_sync import (
        ShopifySyncError,
        can_sync_shopify,
        fetch_markets,
        get_shopify_credentials,
        get_max_refresh_range_days,
        get_sync_days,
        get_sync_interval_seconds,
        sync_last_n_days,
        sync_shopify_range,
    )
except ModuleNotFoundError:
    # Fallback for alternate packaging layouts.
    from backend.models import (  # type: ignore[import-not-found]
        AdminUser,
        AppSetting,
        ShopifyCitySalesRecord,
        ShopifyProductCityRecord,
        ShopifyPricesRecord,
        ShopifyProductSalesRecord,
        ShopifyProductValueRecord,
        ShopifyReturnDetailRecord,
        db,
        TenantMixin,
    )
    from backend.shopify_sync import (  # type: ignore[import-not-found]
        ShopifySyncError,
        can_sync_shopify,
        fetch_markets,
        get_shopify_credentials,
        get_max_refresh_range_days,
        get_sync_days,
        get_sync_interval_seconds,
        sync_last_n_days,
        sync_shopify_range,
    )

migrate = Migrate()


def create_app() -> Flask:
    app = Flask(__name__, instance_relative_config=True)

    # Browser traffic only reaches this service through the authenticated
    # Shopify app server. Keep CORS limited to that deployment.
    app_origin = str(os.getenv("SHOPIFY_APP_URL") or "").strip()
    CORS(
        app,
        origins=[app_origin] if app_origin else [],
        methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "X-Codpilot-Shop", "X-Codpilot-Shopify-Token", "X-Codpilot-Timestamp", "X-Codpilot-Signature"],
    )

    is_serverless = os.getenv("VERCEL") == "1" or bool(os.getenv("VERCEL_ENV"))
    if is_serverless:
        # Serverless filesystems are commonly read-only except for /tmp.
        app.instance_path = os.path.join("/tmp", "amelle-instance")

    os.makedirs(app.instance_path, exist_ok=True)
    default_db_path = os.path.join(app.instance_path, "amelle.sqlite")
    app.config.from_mapping(
        SQLALCHEMY_DATABASE_URI=os.getenv("DATABASE_URL", f"sqlite:///{default_db_path}"),
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
    )

    db.init_app(app)
    migrate.init_app(app, db)

    @app.before_request
    def require_trusted_shopify_context():
        """Accept API calls only from the authenticated Shopify app server."""
        if request.method == "OPTIONS" or request.path == "/":
            return None

        secret = str(os.getenv("CODPILOT_BACKEND_SHARED_SECRET") or "")
        shop = str(request.headers.get("X-Codpilot-Shop") or "").strip().lower()
        token = str(request.headers.get("X-Codpilot-Shopify-Token") or "").strip()
        timestamp = str(request.headers.get("X-Codpilot-Timestamp") or "").strip()
        supplied_signature = str(request.headers.get("X-Codpilot-Signature") or "").strip()

        try:
            timestamp_value = int(timestamp)
        except ValueError:
            abort(401)
        if not secret or not shop or not token or abs(int(time.time()) - timestamp_value) > 300:
            abort(401)

        expected_signature = hmac.new(
            secret.encode("utf-8"),
            f"{timestamp}.{shop}.{token}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            abort(401)

        g.shopify_shop = shop
        g.shopify_access_token = token
        return None

    @event.listens_for(Session, "do_orm_execute")
    def add_tenant_criteria(execute_state):
        """Filter all ORM reads/updates/deletes to the authenticated merchant."""
        if not (execute_state.is_select or execute_state.is_update or execute_state.is_delete):
            return
        try:
            shop = str(getattr(g, "shopify_shop", "") or "").strip()
        except RuntimeError:
            shop = ""
        if not shop:
            return
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(TenantMixin, lambda cls: cls.shop_domain == shop, include_aliases=True)
        )

    @event.listens_for(Session, "before_flush")
    def stamp_tenant_on_new_records(session, flush_context, instances):
        try:
            shop = str(getattr(g, "shopify_shop", "") or "").strip()
        except RuntimeError:
            shop = ""
        if not shop:
            return
        for instance in session.new:
            if isinstance(instance, TenantMixin) and not instance.shop_domain:
                instance.shop_domain = shop

    def _ensure_sqlite_column(table: str, column: str, ddl_fragment: str) -> None:
        uri: str = str(app.config.get("SQLALCHEMY_DATABASE_URI") or "")
        if not uri.startswith("sqlite:"):
            return

        cols = db.session.execute(text(f"PRAGMA table_info({table});")).fetchall()
        existing = {str(r[1]) for r in cols}  # row[1] == name
        if column in existing:
            return

        db.session.execute(text(f"ALTER TABLE {table} ADD COLUMN {ddl_fragment};"))
        db.session.commit()

    def _ensure_default_admin_user() -> None:
        """Create the default admin user if it doesn't exist.

        Note: password is stored as a hash (not plaintext) for safety.
        """

        username = str(os.getenv("AMELLE_ADMIN_USERNAME") or "").strip()
        password = str(os.getenv("AMELLE_ADMIN_PASSWORD") or "")
        if not username or not password:
            raise RuntimeError(
                "AMELLE_ADMIN_USERNAME and AMELLE_ADMIN_PASSWORD must be configured."
            )

        existing = AdminUser.query.filter_by(username=username).first()
        if existing and existing.password_hash:
            return

        if existing is None:
            existing = AdminUser(username=username)
            db.session.add(existing)

        existing.password_hash = generate_password_hash(password)
        db.session.commit()

    # Dev-friendly: make sure the local SQLite file has the tables.
    with app.app_context():
        db.create_all()
        # Lightweight schema drift handling for SQLite (create_all doesn't ALTER existing tables).
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="voided_count",
            ddl_fragment="voided_count INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="shipping_amount",
            ddl_fragment="shipping_amount NUMERIC(12, 2) NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="cod_shipped_count",
            ddl_fragment="cod_shipped_count INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="cod_realised_count",
            ddl_fragment="cod_realised_count INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="first_order_count",
            ddl_fragment="first_order_count INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="repeat_order_count",
            ddl_fragment="repeat_order_count INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="realised_first_order_count",
            ddl_fragment="realised_first_order_count INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="realised_repeat_order_count",
            ddl_fragment="realised_repeat_order_count INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="voided_amount",
            ddl_fragment="voided_amount NUMERIC(12, 2) NOT NULL DEFAULT 0",
        )

        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="voided_pre_dispatch_count",
            ddl_fragment="voided_pre_dispatch_count INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="voided_pre_dispatch_amount",
            ddl_fragment="voided_pre_dispatch_amount NUMERIC(12, 2) NOT NULL DEFAULT 0",
        )

        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="voided_fulfilled_count",
            ddl_fragment="voided_fulfilled_count INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="voided_fulfilled_amount",
            ddl_fragment="voided_fulfilled_amount NUMERIC(12, 2) NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="voided_fulfilled_tcs_count",
            ddl_fragment="voided_fulfilled_tcs_count INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="voided_fulfilled_tcs_amount",
            ddl_fragment="voided_fulfilled_tcs_amount NUMERIC(12, 2) NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="voided_fulfilled_postex_count",
            ddl_fragment="voided_fulfilled_postex_count INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="voided_fulfilled_postex_amount",
            ddl_fragment="voided_fulfilled_postex_amount NUMERIC(12, 2) NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="voided_fulfilled_mnp_count",
            ddl_fragment="voided_fulfilled_mnp_count INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_sqlite_column(
            table=ShopifyPricesRecord.__tablename__,
            column="voided_fulfilled_mnp_amount",
            ddl_fragment="voided_fulfilled_mnp_amount NUMERIC(12, 2) NOT NULL DEFAULT 0",
        )

    # Shopify Admin supplies the merchant identity for the public app. The old
    # dashboard password is intentionally opt-in for local legacy support only.
    legacy_login_enabled = str(os.getenv("ENABLE_LEGACY_ADMIN_LOGIN") or "").lower() in {"1", "true", "yes"}
    if legacy_login_enabled:
        _ensure_default_admin_user()

    SETTINGS_SELECTED_MARKET_KEY = "selected_shopify_market_id"
    ai_requests_by_ip: dict[str, list[float]] = {}

    def _current_shop() -> str:
        shop = str(getattr(g, "shopify_shop", "") or "").strip()
        if not shop:
            raise RuntimeError("Missing authenticated Shopify shop context")
        return shop

    def _get_setting(key: str) -> str | None:
        try:
            row = AppSetting.query.filter_by(shop_domain=_current_shop(), key=key).first()
        except Exception:
            return None
        if not row:
            return None
        v = str(row.value or "").strip()
        return v or None

    def _set_setting(key: str, value: str | None) -> None:
        try:
            shop = _current_shop()
            row = AppSetting.query.filter_by(shop_domain=shop, key=key).first()
        except Exception:
            row = None
        if row is None:
            row = AppSetting(shop_domain=shop, key=key)
            db.session.add(row)

        row.value = str(value or "")
        db.session.commit()

    def _choose_default_market_id(markets: list[dict]) -> str | None:
        # Prefer PRIMARY market when available; otherwise first.
        for m in markets:
            if str(m.get("type") or "").upper() == "PRIMARY":
                mid = str(m.get("id") or "").strip()
                if mid:
                    return mid
        for m in markets:
            mid = str(m.get("id") or "").strip()
            if mid:
                return mid
        return None

    @app.get("/api/markets")
    def get_markets():
        """Return Shopify Markets + the currently selected market (dashboard selection).

        Note: This selection is only used for display unless other endpoints
        explicitly apply it.
        """

        # Always return something; frontend will render best-effort.
        selection = _get_setting(SETTINGS_SELECTED_MARKET_KEY)
        markets: list[dict] = []
        shop, token = get_shopify_credentials()

        if can_sync_shopify() and shop and token:
            try:
                markets = fetch_markets(shop=shop, access_token=token, first=50)
            except ShopifySyncError as e:
                return (
                    jsonify(
                        {
                            "ok": False,
                            "error": str(e),
                            "selected_market_id": selection,
                            "current_market": None,
                            "markets": [],
                        }
                    ),
                    200,
                )

        if not selection and markets:
            selection = _choose_default_market_id(markets)
            if selection:
                _set_setting(SETTINGS_SELECTED_MARKET_KEY, selection)

        current_market = None
        if selection and markets:
            current_market = next((m for m in markets if str(m.get("id")) == selection), None)

        return jsonify(
            {
                "ok": True,
                "selected_market_id": selection,
                "current_market": current_market,
                "markets": markets,
            }
        )

    @app.post("/api/markets/select")
    def select_market():
        from flask import request

        payload = request.get_json(silent=True) or {}
        market_id = str(payload.get("market_id") or "").strip()
        if not market_id:
            return jsonify({"ok": False, "error": "Missing market_id"}), 400

        _set_setting(SETTINGS_SELECTED_MARKET_KEY, market_id)
        return jsonify({"ok": True, "selected_market_id": market_id})

    @app.cli.command("init-db")
    def init_db_command() -> None:
        with app.app_context():
            db.create_all()
            if legacy_login_enabled:
                _ensure_default_admin_user()

    @app.cli.command("reset-db")
    def reset_db_command() -> None:
        with app.app_context():
            db.drop_all()
            db.create_all()
            if legacy_login_enabled:
                _ensure_default_admin_user()

    @app.get("/")
    def index():
        return jsonify(
            {
                "status": "ok",
                "message": "Amelle backend running",
                "environment": os.getenv("VERCEL_ENV") or os.getenv("FLASK_ENV") or "local",
            }
        )

    @app.get("/health")
    def health():
        return jsonify(
            {
                "status": "healthy",
                "shopify": {
                    "credentials_configured": can_sync_shopify(),
                    "sync_days": get_sync_days(),
                    "sync_interval_seconds": get_sync_interval_seconds(),
                },
            }
        )

    def _parse_date(value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None

    def serialize_daily_record(r: ShopifyPricesRecord) -> dict:
        def as_float(v):
            if v is None:
                return 0
            if isinstance(v, Decimal):
                return float(v)
            return v

        total_count = int(r.total_count or 0)
        dispatch_count = int(r.dispatch_count or 0)
        cancelled_pre_dispatch_count = int(getattr(r, "voided_pre_dispatch_count", 0) or 0)
        voided_fulfilled_count = int(getattr(r, "voided_fulfilled_count", 0) or 0)
        # Pending is derived rather than stored: it is the remaining order-created
        # cohort after the three terminal outcomes below.
        pending_count = max(0, total_count - dispatch_count - cancelled_pre_dispatch_count - voided_fulfilled_count)

        return {
            "id": r.id,
            "date": r.date.isoformat(),
            "total_count": total_count,
            "total_amount": as_float(r.total_amount),
            "shipping_amount": as_float(getattr(r, "shipping_amount", 0)),
            "prepaid_count": r.prepaid_count,
            "prepaid_amount": as_float(r.prepaid_amount),
            "tcs_count": r.tcs_count,
            "tcs_amount": as_float(r.tcs_amount),
            "postex_count": r.postex_count,
            "postex_amount": as_float(r.postex_amount),
            "dispatch_count": dispatch_count,
            "cod_shipped_count": getattr(r, "cod_shipped_count", 0),
            "cod_realised_count": getattr(r, "cod_realised_count", 0),
            "first_order_count": getattr(r, "first_order_count", 0),
            "repeat_order_count": getattr(r, "repeat_order_count", 0),
            "realised_first_order_count": getattr(r, "realised_first_order_count", 0),
            "realised_repeat_order_count": getattr(r, "realised_repeat_order_count", 0),
            "pending_count": pending_count,
            "dispatch_fulfillments_created_count": r.dispatch_fulfillments_created_count,
            "returns_count": r.returns_count,
            "refunded_amount": as_float(r.refunded_amount),
            "voided_count": getattr(r, "voided_count", 0),
            "voided_amount": as_float(getattr(r, "voided_amount", 0)),
            "voided_pre_dispatch_count": cancelled_pre_dispatch_count,
            "voided_pre_dispatch_amount": as_float(getattr(r, "voided_pre_dispatch_amount", 0)),
            "voided_fulfilled_count": voided_fulfilled_count,
            "voided_fulfilled_amount": as_float(getattr(r, "voided_fulfilled_amount", 0)),
            "voided_fulfilled_tcs_count": getattr(r, "voided_fulfilled_tcs_count", 0),
            "voided_fulfilled_tcs_amount": as_float(getattr(r, "voided_fulfilled_tcs_amount", 0)),
            "voided_fulfilled_postex_count": getattr(r, "voided_fulfilled_postex_count", 0),
            "voided_fulfilled_postex_amount": as_float(getattr(r, "voided_fulfilled_postex_amount", 0)),
            "voided_fulfilled_mnp_count": getattr(r, "voided_fulfilled_mnp_count", 0),
            "voided_fulfilled_mnp_amount": as_float(getattr(r, "voided_fulfilled_mnp_amount", 0)),
            "currency": r.currency,
        }

    @app.get("/api/daily-records")
    def get_daily_records():
        # Optional query params:
        # - start=YYYY-MM-DD
        # - end=YYYY-MM-DD
        # - refresh=1 (sync Shopify first for the intersecting recent range)
        from flask import request

        start = _parse_date(request.args.get("start"))
        end = _parse_date(request.args.get("end"))
        refresh = request.args.get("refresh") in ("1", "true", "yes")

        if refresh and can_sync_shopify() and start and end:
            # Refresh the selected range (bounded to avoid huge backfills).
            max_days = get_max_refresh_range_days()
            today = date.today()
            req_end = min(end, today)
            req_start = min(start, req_end)

            # Bound range length to max_days (keep the most recent portion of the selection).
            # Inclusive day count: (end-start)+1
            if (req_end - req_start).days + 1 > max_days:
                req_start = req_end.fromordinal(req_end.toordinal() - (max_days - 1))

            if req_start <= req_end:
                try:
                    sync_shopify_range(app, start=req_start, end=req_end)
                except ShopifySyncError:
                    # Ignore sync errors here; UI will still show DB contents.
                    pass
                except Exception:
                    # Defensive: don't 500 the dashboard if an unexpected error occurs during sync.
                    pass

        try:
            q = ShopifyPricesRecord.query
            if start:
                q = q.filter(ShopifyPricesRecord.date >= start)
            if end:
                q = q.filter(ShopifyPricesRecord.date <= end)
            rows = q.order_by(ShopifyPricesRecord.date.desc()).all()
        except OperationalError:
            return jsonify({"records": []})

        return jsonify({"records": [serialize_daily_record(r) for r in rows]})

    @app.get("/api/return-details")
    def get_return_details():
        """Per-order return details for a given day.

        Query params:
        - date=YYYY-MM-DD (required)
        """

        from flask import request

        d = _parse_date(request.args.get("date"))
        if not d:
            return jsonify({"rows": [], "error": "Missing or invalid date"}), 400

        try:
            rows = (
                ShopifyReturnDetailRecord.query.filter(ShopifyReturnDetailRecord.date == d)
                .order_by(ShopifyReturnDetailRecord.order_no.asc())
                .all()
            )
        except OperationalError:
            return jsonify({"rows": []})

        return jsonify(
            {
                "rows": [
                    {
                        "date": r.date.isoformat(),
                        "order_no": r.order_no,
                        "client_no": r.client_no,
                        "city": r.city,
                        "service_used": r.service_used,
                    }
                    for r in rows
                ]
            }
        )

    @app.post("/api/ai-assistant")
    def ai_assistant():
        """Answer dashboard questions with a Gemini model using selected-period data."""
        forwarded_for = str(request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
        client_ip = forwarded_for or str(request.remote_addr or "unknown")
        now = time.time()
        recent_requests = [t for t in ai_requests_by_ip.get(client_ip, []) if now - t < 600]
        if len(recent_requests) >= 15:
            return jsonify({"ok": False, "error": "AI request limit reached. Please try again in a few minutes."}), 429
        recent_requests.append(now)
        ai_requests_by_ip[client_ip] = recent_requests

        def format_dashboard_date(value: date | None) -> str | None:
            if value is None:
                return None
            day = value.day
            suffix = "th" if 11 <= day % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
            return f"{value.strftime('%B')} {day}{suffix}, {value.year}"

        payload = request.get_json(silent=True) or {}
        question = str(payload.get("question") or "").strip()
        if not question:
            return jsonify({"ok": False, "error": "Please enter a question."}), 400
        if len(question) > 2000:
            return jsonify({"ok": False, "error": "Please keep questions under 2,000 characters."}), 400

        api_key = str(os.getenv("GEMINI_API_KEY") or "").strip()
        if not api_key:
            return jsonify({"ok": False, "error": "Gemini is not configured on the backend."}), 503

        start = _parse_date(str(payload.get("start") or ""))
        end = _parse_date(str(payload.get("end") or ""))
        if start and end and start > end:
            return jsonify({"ok": False, "error": "Invalid date range."}), 400

        try:
            from sqlalchemy import func

            daily_query = ShopifyPricesRecord.query
            product_query = db.session.query(
                ShopifyProductSalesRecord.product_title.label("product"),
                func.sum(ShopifyProductSalesRecord.quantity).label("units"),
            )
            city_query = db.session.query(
                ShopifyCitySalesRecord.city.label("city"),
                func.sum(ShopifyCitySalesRecord.orders).label("orders"),
            )
            if start:
                daily_query = daily_query.filter(ShopifyPricesRecord.date >= start)
                product_query = product_query.filter(ShopifyProductSalesRecord.date >= start)
                city_query = city_query.filter(ShopifyCitySalesRecord.date >= start)
            if end:
                daily_query = daily_query.filter(ShopifyPricesRecord.date <= end)
                product_query = product_query.filter(ShopifyProductSalesRecord.date <= end)
                city_query = city_query.filter(ShopifyCitySalesRecord.date <= end)

            daily_rows = daily_query.order_by(ShopifyPricesRecord.date.asc()).limit(62).all()
            product_rows = product_query.group_by(ShopifyProductSalesRecord.product_title).order_by(func.sum(ShopifyProductSalesRecord.quantity).desc()).limit(25).all()
            city_rows = city_query.group_by(ShopifyCitySalesRecord.city).order_by(func.sum(ShopifyCitySalesRecord.orders).desc()).limit(25).all()
        except OperationalError:
            return jsonify({"ok": False, "error": "Dashboard data is temporarily unavailable."}), 503

        # Calculator values are computed in the browser from the same selected data.
        # Accepting them here lets Gemini explain exactly what the user sees.
        calculator = payload.get("calculator") if isinstance(payload.get("calculator"), dict) else {}
        raw_history = payload.get("history") if isinstance(payload.get("history"), list) else []
        history: list[dict[str, str]] = []
        for item in raw_history[-10:]:
            if not isinstance(item, dict):
                continue
            role = str(item.get("role") or "").strip().lower()
            text = str(item.get("text") or "").strip()
            if role in {"user", "assistant"} and text:
                history.append({"role": role, "text": text[:2000]})
        selected_period_label = (
            f"{format_dashboard_date(start)} to {format_dashboard_date(end)}"
            if start and end
            else "the selected dashboard period"
        )
        context = {
            "selected_period": selected_period_label,
            "daily_metrics": [serialize_daily_record(row) for row in daily_rows],
            "top_products": [{"product": str(row.product or ""), "units": int(row.units or 0)} for row in product_rows],
            "top_cities": [{"city": str(row.city or ""), "orders": int(row.orders or 0)} for row in city_rows],
            "calculator": calculator,
        }
        conversation = "\n".join(f"{item['role'].upper()}: {item['text']}" for item in history) or "No earlier messages."
        instructions = (
            "You are the Amelle COD Dashboard assistant. Answer only from the supplied dashboard data. "
            "Be concise, explain calculations when asked, use the selected period, and never invent a figure. "
            "If the data cannot answer, say so and name the missing data. Do not reveal personal customer data or secrets. "
            "Write in clean plain text: do not use Markdown, asterisks, backticks, hashes, or bullet symbols. "
            f"When mentioning the date range, write it exactly as: {selected_period_label}.\n\n"
            f"CONVERSATION SO FAR:\n{conversation}\n\n"
            f"DASHBOARD DATA:\n{json.dumps(context, default=str, separators=(',', ':'))}\n\n"
            f"USER QUESTION:\n{question}"
        )
        # Flash-Lite is intentionally used for a responsive dashboard chat and
        # is available on Gemini's free tier. Override only when needed.
        model = str(os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite").strip()
        try:
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json={
                    "contents": [{"parts": [{"text": instructions}]}],
                    "generationConfig": {
                        "maxOutputTokens": 350,
                        "thinkingConfig": {"thinkingLevel": "MINIMAL"},
                    },
                },
                # Gemini's free tier can queue a dashboard analysis briefly. This
                # function has a longer Vercel lifetime; do not cut the request
                # off at the previous 25-second client timeout.
                timeout=(5, 75),
            )
            if response.status_code >= 400:
                try:
                    provider_error = str((response.json().get("error") or {}).get("message") or "").strip()
                except Exception:
                    provider_error = ""
                detail = f": {provider_error}" if provider_error else ""
                return jsonify({"ok": False, "error": f"Gemini request failed ({response.status_code}){detail}"}), 502
            result = response.json() if response.content else {}
            answer = ""
            for candidate in result.get("candidates") or []:
                content = candidate.get("content") if isinstance(candidate, dict) else {}
                for part in (content or {}).get("parts") or []:
                    if isinstance(part, dict) and part.get("text"):
                        answer += str(part["text"])
            if not answer:
                return jsonify({"ok": False, "error": "Gemini returned no answer."}), 502
            return jsonify({"ok": True, "answer": answer})
        except requests.RequestException as exc:
            app.logger.warning("Gemini request failed: %s", exc)
            return jsonify({"ok": False, "error": "Could not reach Gemini. Check the Vercel Function Logs."}), 502

    @app.post("/api/login")
    def login():
        from flask import request

        if not legacy_login_enabled:
            abort(404)

        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username") or "").strip()
        password = str(payload.get("password") or "")

        if not username or not password:
            return jsonify({"ok": False, "error": "Missing username or password"}), 400

        user = AdminUser.query.filter_by(username=username).first()
        if not user or not user.password_hash or not check_password_hash(user.password_hash, password):
            return jsonify({"ok": False, "error": "Invalid username or password"}), 401

        return jsonify({"ok": True, "username": user.username})

    @app.post("/api/change-password")
    def change_password():
        from flask import request

        if not legacy_login_enabled:
            abort(404)

        payload = request.get_json(silent=True) or {}
        username = str(payload.get("username") or "").strip()
        old_password = str(payload.get("old_password") or "")
        new_password = str(payload.get("new_password") or "")

        if not username or not old_password or not new_password:
            return jsonify({"ok": False, "error": "Missing username, old password, or new password"}), 400

        user = AdminUser.query.filter_by(username=username).first()
        if not user or not user.password_hash or not check_password_hash(user.password_hash, old_password):
            return jsonify({"ok": False, "error": "Old password is incorrect"}), 401

        user.password_hash = generate_password_hash(new_password)
        db.session.commit()

        return jsonify({"ok": True})

    @app.get("/api/product-sales")
    def get_product_sales():
        """Return product quantities and value aggregated over the selected range."""

        from flask import request
        from sqlalchemy import func

        start = _parse_date(request.args.get("start"))
        end = _parse_date(request.args.get("end"))
        refresh = request.args.get("refresh") in ("1", "true", "yes")

        if refresh and can_sync_shopify() and start and end:
            max_days = get_max_refresh_range_days()
            today = date.today()
            req_end = min(end, today)
            req_start = min(start, req_end)

            if (req_end - req_start).days + 1 > max_days:
                req_start = req_end.fromordinal(req_end.toordinal() - (max_days - 1))

            if req_start <= req_end:
                try:
                    sync_shopify_range(app, start=req_start, end=req_end)
                except ShopifySyncError:
                    pass

        q_qty = db.session.query(
            ShopifyProductSalesRecord.product_title.label("product_title"),
            func.sum(ShopifyProductSalesRecord.quantity).label("quantity"),
        )
        if start:
            q_qty = q_qty.filter(ShopifyProductSalesRecord.date >= start)
        if end:
            q_qty = q_qty.filter(ShopifyProductSalesRecord.date <= end)

        qty_rows = q_qty.group_by(ShopifyProductSalesRecord.product_title).all()
        qty_by_title = {
            str(r.product_title): int(r.quantity or 0)
            for r in qty_rows
            if getattr(r, "product_title", None)
        }

        q_amt = db.session.query(
            ShopifyProductValueRecord.product_title.label("product_title"),
            func.sum(ShopifyProductValueRecord.amount).label("amount"),
        )
        if start:
            q_amt = q_amt.filter(ShopifyProductValueRecord.date >= start)
        if end:
            q_amt = q_amt.filter(ShopifyProductValueRecord.date <= end)

        amt_rows = q_amt.group_by(ShopifyProductValueRecord.product_title).all()
        amt_by_title = {
            str(r.product_title): float(r.amount or 0)
            for r in amt_rows
            if getattr(r, "product_title", None)
        }

        titles = set(qty_by_title.keys()) | set(amt_by_title.keys())
        merged = [
            {
                "product_title": t,
                "quantity": int(qty_by_title.get(t, 0) or 0),
                "amount": float(amt_by_title.get(t, 0) or 0),
            }
            for t in titles
            if t
        ]
        merged.sort(key=lambda r: r.get("quantity", 0), reverse=True)

        return jsonify({"rows": merged})

    @app.get("/api/product-report")
    def get_product_report():
        """Return the full product report, including each product's top city by units sold."""

        from flask import request
        from sqlalchemy import func

        start = _parse_date(request.args.get("start"))
        end = _parse_date(request.args.get("end"))
        refresh = request.args.get("refresh") in ("1", "true", "yes")

        if refresh and can_sync_shopify() and start and end:
            max_days = get_max_refresh_range_days()
            today = date.today()
            req_end = min(end, today)
            req_start = min(start, req_end)
            if (req_end - req_start).days + 1 > max_days:
                req_start = req_end.fromordinal(req_end.toordinal() - (max_days - 1))
            if req_start <= req_end:
                try:
                    sync_shopify_range(app, start=req_start, end=req_end)
                except ShopifySyncError:
                    pass

        q_qty = db.session.query(
            ShopifyProductSalesRecord.product_title.label("product_title"),
            func.sum(ShopifyProductSalesRecord.quantity).label("quantity"),
        )
        q_amt = db.session.query(
            ShopifyProductValueRecord.product_title.label("product_title"),
            func.sum(ShopifyProductValueRecord.amount).label("amount"),
        )
        q_city = db.session.query(
            ShopifyProductCityRecord.product_title.label("product_title"),
            ShopifyProductCityRecord.city.label("city"),
            func.sum(ShopifyProductCityRecord.quantity).label("quantity"),
        )
        if start:
            q_qty = q_qty.filter(ShopifyProductSalesRecord.date >= start)
            q_amt = q_amt.filter(ShopifyProductValueRecord.date >= start)
            q_city = q_city.filter(ShopifyProductCityRecord.date >= start)
        if end:
            q_qty = q_qty.filter(ShopifyProductSalesRecord.date <= end)
            q_amt = q_amt.filter(ShopifyProductValueRecord.date <= end)
            q_city = q_city.filter(ShopifyProductCityRecord.date <= end)

        quantities = {str(r.product_title): int(r.quantity or 0) for r in q_qty.group_by(ShopifyProductSalesRecord.product_title).all() if r.product_title}
        amounts = {str(r.product_title): float(r.amount or 0) for r in q_amt.group_by(ShopifyProductValueRecord.product_title).all() if r.product_title}
        top_cities: dict[str, tuple[str, int]] = {}
        for r in q_city.group_by(ShopifyProductCityRecord.product_title, ShopifyProductCityRecord.city).all():
            title, city, quantity = str(r.product_title or ""), str(r.city or ""), int(r.quantity or 0)
            if title and city and (title not in top_cities or quantity > top_cities[title][1]):
                top_cities[title] = (city, quantity)

        rows = []
        for title in set(quantities) | set(amounts):
            city, city_quantity = top_cities.get(title, ("—", 0))
            rows.append(
                {
                    "product_title": title,
                    "quantity": quantities.get(title, 0),
                    "amount": amounts.get(title, 0),
                    "top_city": city,
                    "top_city_quantity": city_quantity,
                }
            )
        rows.sort(key=lambda r: (-r["amount"], r["product_title"].lower()))
        return jsonify({"rows": rows})

    @app.get("/api/city-sales")
    def get_city_sales():
        """Return order counts aggregated by city over the selected range.

        This is computed from Shopify orders (shipping address city; falls back to billing city).
        Excludes voided and cancelled orders.

        Query params:
        - start=YYYY-MM-DD
        - end=YYYY-MM-DD
        - refresh=1 (accepted for API parity; city counts are computed live)
        """

        from flask import request
        from sqlalchemy import func

        start = _parse_date(request.args.get("start"))
        end = _parse_date(request.args.get("end"))
        refresh = request.args.get("refresh") in ("1", "true", "yes")

        if not start or not end:
            return jsonify({"rows": []})

        if refresh and can_sync_shopify() and start and end:
            max_days = get_max_refresh_range_days()
            today = date.today()
            req_end = min(end, today)
            req_start = min(start, req_end)

            if (req_end - req_start).days + 1 > max_days:
                req_start = req_end.fromordinal(req_end.toordinal() - (max_days - 1))

            if req_start <= req_end:
                try:
                    sync_shopify_range(app, start=req_start, end=req_end)
                except ShopifySyncError:
                    pass

        q = db.session.query(
            ShopifyCitySalesRecord.city.label("city"),
            func.sum(ShopifyCitySalesRecord.orders).label("orders"),
        )
        if start:
            q = q.filter(ShopifyCitySalesRecord.date >= start)
        if end:
            q = q.filter(ShopifyCitySalesRecord.date <= end)

        rows = q.group_by(ShopifyCitySalesRecord.city).order_by(func.sum(ShopifyCitySalesRecord.orders).desc()).all()
        return jsonify({"rows": [{"city": r.city, "orders": int(r.orders or 0)} for r in rows if getattr(r, "city", None)]})

    @app.post("/api/refresh")
    def refresh_shopify():
        if not can_sync_shopify():
            return jsonify({"ok": False, "error": "Shopify credentials not configured"}), 400
        try:
            res = sync_last_n_days(app)
            return jsonify({"ok": True, **res})
        except ShopifySyncError as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @app.get("/api/sync-range")
    def sync_range():
        """Sync Shopify data for a specific date range.

        This endpoint is intentionally lightweight: it triggers a sync and returns summary metadata,
        avoiding returning the (potentially large) daily-records payload.

        Query params:
        - start=YYYY-MM-DD
        - end=YYYY-MM-DD
        """

        from flask import request

        if not can_sync_shopify():
            return jsonify({"ok": False, "error": "Shopify credentials not configured"}), 400

        start = _parse_date(request.args.get("start"))
        end = _parse_date(request.args.get("end"))
        if not start or not end:
            return jsonify({"ok": False, "error": "Missing start or end"}), 400

        max_days = get_max_refresh_range_days()
        today = date.today()
        req_end = min(end, today)
        req_start = min(start, req_end)

        # Bound range length to max_days (keep the most recent portion of the selection).
        if (req_end - req_start).days + 1 > max_days:
            req_start = req_end.fromordinal(req_end.toordinal() - (max_days - 1))

        if req_start > req_end:
            return jsonify({"ok": True, "skipped": True, "reason": "Empty range"})

        try:
            res = sync_shopify_range(app, start=req_start, end=req_end)
            return jsonify({"ok": True, **res})
        except ShopifySyncError as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    def _start_scheduler_once() -> None:
        # Avoid running twice under Flask debug reloader.
        if app.debug and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
            return

        # Vercel runs Python as a request/response serverless function.
        # Background threads can keep the invocation alive and/or behave unpredictably.
        if os.getenv("VERCEL") == "1" or os.getenv("VERCEL_ENV"):
            return

        if os.getenv("ENABLE_SHOPIFY_SCHEDULER", "1").lower() in ("0", "false", "no"):
            return

        def loop():
            interval = get_sync_interval_seconds()
            while True:
                try:
                    if can_sync_shopify():
                        sync_last_n_days(app)
                except Exception:
                    # Keep loop alive; inspect logs while developing.
                    pass
                time.sleep(interval)

        t = threading.Thread(target=loop, daemon=True)
        t.start()

    _start_scheduler_once()

    return app


# Export a module-level WSGI app for Vercel (and other WSGI servers).
app = create_app()


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5010"))
    app.run(host="0.0.0.0", port=port, debug=True)
