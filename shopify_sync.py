from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Literal

try:
    from zoneinfo import ZoneInfo  # py3.9+
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore[assignment]

import requests
from flask import Flask

from models import (
    ShopifyCitySalesRecord,
    ShopifyProductCityRecord,
    ShopifyPricesRecord,
    ShopifyProductSalesRecord,
    ShopifyProductValueRecord,
    ShopifyReturnDetailRecord,
    db,
)


class ShopifySyncError(RuntimeError):
    pass


# Heuristics (match scripts/shopify_export_sample.py)
# NOTE: COD detection should be driven by Shopify's COD-specific gateway text
# (not generic/offline signals like "manual"), otherwise prepaid/offline payment
# methods can get misclassified as COD and prepaid revenue will be undercounted.
COD_KEYWORDS = ["cod", "cash on delivery"]
PREPAID_KEYWORDS = ["prepaid", "paid"]
# Match the exact tags/pills you use in Shopify Admin.
# Keep these specific to avoid accidental matches from unrelated tags/notes.
TCS_KEYWORDS = [
    "shipped by tcs courier",
    "shipped by tcs",
    "ship by tcs",
    "tcs courier",
    "tcs",
]
POSTEX_KEYWORDS = [
    "shipped by postex courier",
    "shipped by postex",
    "postex courier",
    "postex",
]

# MnP return tags observed in Shopify Admin include variants like:
# - "MnP_Return to Origin"
# - "MnP_Return to Shipper"
# Keep the matching fairly broad but still MnP-specific.
MNP_KEYWORDS = [
    "mnp",
    "m&p",
    "mn&p",
    "mnp_",
    "mnp-",
    "mnp return",
    "mnp_return",
    "mnp_return to origin",
    "mnp_return to shipper",
]


def _get_credentials() -> tuple[str | None, str | None]:
    # A public app receives a merchant-scoped access token only from the
    # authenticated Shopify application server. Never fall back to global
    # environment credentials: that would expose one merchant's data to another.
    try:
        from flask import g

        shop_from_request = str(getattr(g, "shopify_shop", "") or "").strip()
        token_from_request = str(getattr(g, "shopify_access_token", "") or "").strip()
        if shop_from_request and token_from_request:
            return shop_from_request, token_from_request
    except RuntimeError:
        return None, None
    return None, None


def get_sync_days() -> int:
    try:
        return max(1, int(os.getenv("SHOPIFY_SYNC_DAYS", "10")))
    except ValueError:
        return 10


def get_sync_interval_seconds() -> int:
    try:
        return max(60, int(os.getenv("SHOPIFY_SYNC_INTERVAL_SECONDS", str(30 * 60))))
    except ValueError:
        return 30 * 60


def get_shopify_api_version() -> str:
    # You can change this later via env var.
    return os.getenv("SHOPIFY_API_VERSION", "2025-10")


def get_shopify_report_tz_name() -> str | None:
    """Timezone used to bucket 'daily' metrics.

    Shopify analytics uses the shop's reporting timezone, not UTC. If we bucket by UTC,
    rows near midnight often shift by 1.

    You can force a timezone with SHOPIFY_REPORT_TZ (e.g. 'Asia/Karachi').
    """

    v = os.getenv("SHOPIFY_REPORT_TZ", "").strip()
    return v or None


def fetch_shop_iana_timezone(shop: str, access_token: str) -> str | None:
    """Fetch the shop's IANA timezone (best-effort)."""

    shop = _normalize_shop_domain(shop)
    api_version = get_shopify_api_version()
    url = f"https://{shop}/admin/api/{api_version}/shop.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Accept": "application/json",
    }

    resp = requests.get(url, headers=headers, timeout=30)
    if resp.status_code >= 400:
        return None
    payload = resp.json() if resp.content else {}
    shop_obj = payload.get("shop") if isinstance(payload, dict) else None
    if not isinstance(shop_obj, dict):
        return None
    tz = (shop_obj.get("iana_timezone") or shop_obj.get("timezone"))
    tz = str(tz).strip() if tz else ""
    return tz or None


def shopify_graphql(shop: str, access_token: str, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    shop = _normalize_shop_domain(shop)
    api_version = get_shopify_api_version()
    url = f"https://{shop}/admin/api/{api_version}/graphql.json"
    headers = {
        "X-Shopify-Access-Token": access_token,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {"query": query}
    if variables is not None:
        payload["variables"] = variables

    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    if resp.status_code == 401 or resp.status_code == 403:
        raise ShopifySyncError("Shopify auth failed for GraphQL (check token scopes)")
    if resp.status_code >= 400:
        raise ShopifySyncError(f"Shopify GraphQL error: {resp.status_code} {resp.text[:200]}")
    data = resp.json() if resp.content else {}
    if isinstance(data, dict) and data.get("errors"):
        # Keep message short; do not include full response.
        first = None
        try:
            first = data.get("errors")[0]
        except Exception:
            first = None
        msg = first.get("message") if isinstance(first, dict) else None
        raise ShopifySyncError(f"Shopify GraphQL returned errors: {msg or 'unknown'}")
    return data


def _extract_shopifyql_table_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Best-effort extraction of rows from shopifyqlQuery response."""

    # Response shapes vary by API version; handle common formats.
    node = (((result.get("data") or {}).get("shopifyqlQuery")) or {})
    if not isinstance(node, dict):
        return []

    # Newer: { tableData: { columns: [...], rows: [{values:[...]}, ...] } }
    table = node.get("tableData")
    if isinstance(table, dict):
        columns = table.get("columns")
        rows = table.get("rows")
        if isinstance(columns, list) and isinstance(rows, list):
            col_names: list[str] = []
            for c in columns:
                if isinstance(c, dict) and c.get("name"):
                    col_names.append(str(c.get("name")))
                else:
                    col_names.append(str(c))

            out: list[dict[str, Any]] = []
            for r in rows:
                if isinstance(r, dict) and isinstance(r.get("values"), list):
                    vals = r.get("values")
                    out.append({col_names[i]: vals[i] for i in range(min(len(col_names), len(vals)))})
            return out

    # Older: { tableData: { rowData: [[...], ...], columns: [...] } }
    if isinstance(table, dict) and isinstance(table.get("rowData"), list) and isinstance(table.get("columns"), list):
        cols = table.get("columns")
        col_names = [str((c.get("name") if isinstance(c, dict) else c)) for c in cols]
        out = []
        for row in table.get("rowData"):
            if isinstance(row, list):
                out.append({col_names[i]: row[i] for i in range(min(len(col_names), len(row)))})
        return out

    return []


def fetch_fulfilled_counts_shopifyql(
    *,
    shop: str,
    access_token: str,
    start: date,
    end: date,
    currency: str,
) -> dict[date, int]:
    """Fetch orders_fulfilled per day using ShopifyQL (matches Shopify Analytics)."""

    query_str = (
        "FROM fulfillments "
        "SHOW orders_fulfilled "
        "TIMESERIES day WITH TOTALS, CURRENCY '{currency}' "
        "SINCE {since} UNTIL {until}"
    ).format(currency=currency.replace("'", ""), since=start.isoformat(), until=end.isoformat())

    gql = """
    query ShopifyQL($query: String!) {
      shopifyqlQuery(query: $query) {
        __typename
        ... on ShopifyqlQueryResponse {
          tableData {
            columns { name }
            rows { values }
          }
        }
      }
    }
    """

    result = shopify_graphql(shop, access_token, gql, variables={"query": query_str})
    rows = _extract_shopifyql_table_rows(result)

    out: dict[date, int] = {}
    for r in rows:
        # Typically contains a date column + orders_fulfilled column
        day_val = r.get("day") or r.get("date") or r.get("Day") or r.get("Date")
        fulfilled_val = r.get("orders_fulfilled") or r.get("Orders Fulfilled") or r.get("ordersFulfilled")
        if not day_val:
            continue
        try:
            d = date.fromisoformat(str(day_val)[:10])
        except Exception:
            continue
        try:
            n = int(float(fulfilled_val)) if fulfilled_val is not None else 0
        except Exception:
            n = 0
        out[d] = n

    return out


def _normalize_shop_domain(shop: str) -> str:
    shop = shop.strip()
    shop = re.sub(r"^https?://", "", shop)
    shop = shop.strip("/")
    return shop


def _parse_next_link(link_header: str | None) -> str | None:
    if not link_header:
        return None

    # Example: <https://...page_info=...>; rel="next", <...>; rel="previous"
    parts = [p.strip() for p in link_header.split(",")]
    for part in parts:
        if 'rel="next"' in part or "rel=next" in part:
            m = re.search(r"<([^>]+)>", part)
            if m:
                return m.group(1)
    return None


def _iso_min_dt(d: date) -> str:
    # Shopify expects ISO-8601; use UTC day boundaries.
    return datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc).isoformat()


def _iso_max_dt(d: date) -> str:
    return datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=timezone.utc).isoformat()


def _iso_min_dt_in_tz(d: date, report_tz: Any) -> str:
    # Interpret the date boundary in the shop/reporting timezone, then convert to UTC.
    local = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=report_tz)
    return local.astimezone(timezone.utc).isoformat()


def _iso_max_dt_in_tz(d: date, report_tz: Any) -> str:
    local = datetime(d.year, d.month, d.day, 23, 59, 59, tzinfo=report_tz)
    return local.astimezone(timezone.utc).isoformat()


def _to_date(created_at: str | None, report_tz: Any | None = None) -> date | None:
    if not created_at:
        return None
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        if report_tz is not None:
            return dt.astimezone(report_tz).date()
        return dt.date()
    except ValueError:
        return None


def _dec(v: Any) -> Decimal:
    if v is None:
        return Decimal("0")
    try:
        return Decimal(str(v))
    except Exception:
        return Decimal("0")


def _lower_list(values: list[Any]) -> list[str]:
    return [str(v).strip().lower() for v in values if v is not None]


def _contains_any(haystack: list[Any], keywords: list[str]) -> bool:
    hs = " ".join(_lower_list(haystack))
    return any(k.lower() in hs for k in keywords)


def _has_phrase(values: list[str], phrase: str) -> bool:
    p = phrase.strip().lower()
    return any(p in str(v).strip().lower() for v in values if v is not None)


def _matches_cod_gateway_value(value: str) -> bool:
    v = value.strip().lower()
    if not v:
        return False
    if "cash on delivery" in v:
        return True
    # Token/word match for "cod" to avoid catching unrelated substrings.
    return bool(re.search(r"\bcod\b", v))


def _order_is_cod(*, gateways: list[Any], processing_method: Any = None, tags: list[str] | None = None) -> bool:
    gateway_values = _lower_list(gateways)
    if any(_matches_cod_gateway_value(g) for g in gateway_values):
        return True

    # Fallback (best-effort): allow explicit COD tags if you use them.
    if tags:
        return _contains_any(list(tags), COD_KEYWORDS)
    return False


def _order_is_prepaid(*, gateways: list[Any], financial_status: Any, processing_method: Any = None, tags: list[str] | None = None) -> bool:
    fin = str(financial_status or "").strip().lower()
    if fin not in {"paid", "partially paid", "partially_paid"}:
        return False
    return not _order_is_cod(gateways=gateways, processing_method=processing_method, tags=tags)


@dataclass
class DailyAggregate:
    date: date
    currency: str

    total_count: int = 0
    total_amount: Decimal = Decimal("0")
    shipping_amount: Decimal = Decimal("0")

    prepaid_count: int = 0
    prepaid_amount: Decimal = Decimal("0")

    tcs_count: int = 0
    tcs_amount: Decimal = Decimal("0")

    postex_count: int = 0
    postex_amount: Decimal = Decimal("0")

    dispatch_count: int = 0
    dispatch_fulfillments_created_count: int = 0
    cod_shipped_count: int = 0
    cod_realised_count: int = 0
    first_order_count: int = 0
    repeat_order_count: int = 0
    realised_first_order_count: int = 0
    realised_repeat_order_count: int = 0

    returns_count: int = 0
    refunded_amount: Decimal = Decimal("0")

    voided_count: int = 0
    voided_amount: Decimal = Decimal("0")

    voided_pre_dispatch_count: int = 0
    voided_pre_dispatch_amount: Decimal = Decimal("0")

    voided_fulfilled_count: int = 0
    voided_fulfilled_amount: Decimal = Decimal("0")

    voided_fulfilled_tcs_count: int = 0
    voided_fulfilled_tcs_amount: Decimal = Decimal("0")

    voided_fulfilled_postex_count: int = 0
    voided_fulfilled_postex_amount: Decimal = Decimal("0")

    voided_fulfilled_mnp_count: int = 0
    voided_fulfilled_mnp_amount: Decimal = Decimal("0")


def fetch_orders(shop: str, access_token: str, start: date, end: date, report_tz: Any | None = None) -> list[dict[str, Any]]:
    return fetch_orders_by(
        shop=shop,
        access_token=access_token,
        start=start,
        end=end,
        date_filter="created_at",
        report_tz=report_tz,
    )


def fetch_orders_by(
    *,
    shop: str,
    access_token: str,
    start: date,
    end: date,
    date_filter: Literal["created_at", "updated_at"],
    report_tz: Any | None = None,
) -> list[dict[str, Any]]:
    # New public Shopify apps must use the GraphQL Admin API.  The dashboard
    # keeps its existing calculation code by normalizing the GraphQL response
    # into the legacy internal order shape at this boundary.
    field = "created_at" if date_filter == "created_at" else "updated_at"
    search = f"status:any {field}:>={start.isoformat()} {field}:<={end.isoformat()}"
    query = """
    query CodPilotOrders($after: String, $query: String!) {
      orders(first: 250, after: $after, query: $query, sortKey: UPDATED_AT) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id name createdAt processedAt updatedAt cancelledAt cancelReason currencyCode tags phone
          displayFinancialStatus displayFulfillmentStatus paymentGatewayNames
          totalPriceSet { shopMoney { amount currencyCode } }
          currentTotalPriceSet { shopMoney { amount currencyCode } }
          totalShippingPriceSet { shopMoney { amount } }
          shippingAddress { city phone }
          billingAddress { city phone }
          customer { id numberOfOrders }
          shippingLines(first: 100) { nodes { title } }
          lineItems(first: 250) { nodes { title quantity originalUnitPriceSet { shopMoney { amount } } } }
          fulfillments(first: 100) { nodes { status createdAt updatedAt trackingInfo(first: 10) { company } } }
          refunds { createdAt transactions(first: 100) { nodes { kind status amountSet { shopMoney { amount } } } } }
        }
      }
    }
    """
    orders: list[dict[str, Any]] = []
    after: str | None = None
    while True:
        result = shopify_graphql(shop, access_token, query, {"after": after, "query": search})
        root = ((result.get("data") or {}).get("orders") or {}) if isinstance(result, dict) else {}
        nodes = root.get("nodes") or []
        if not isinstance(nodes, list):
            raise ShopifySyncError("Shopify GraphQL returned an invalid orders payload")
        orders.extend(_normalize_graphql_order(item) for item in nodes if isinstance(item, dict))
        page = root.get("pageInfo") or {}
        if not page.get("hasNextPage"):
            break
        after = str(page.get("endCursor") or "") or None
        if not after:
            break
    return orders


def _money(node: Any) -> str:
    if not isinstance(node, dict):
        return "0"
    shop_money = node.get("shopMoney") if isinstance(node.get("shopMoney"), dict) else node
    return str(shop_money.get("amount") or "0")


def _nodes(connection: Any) -> list[dict[str, Any]]:
    if not isinstance(connection, dict):
        return []
    nodes = connection.get("nodes") or []
    return [node for node in nodes if isinstance(node, dict)] if isinstance(nodes, list) else []


def _normalize_graphql_order(order: dict[str, Any]) -> dict[str, Any]:
    fulfillments = []
    for item in _nodes(order.get("fulfillments")):
        tracking = _nodes(item.get("trackingInfo"))
        fulfillments.append({
            "status": str(item.get("status") or "").lower(),
            "shipment_status": str(item.get("status") or "").lower(),
            "created_at": item.get("createdAt"),
            "updated_at": item.get("updatedAt"),
            "tracking_company": next((str(t.get("company") or "") for t in tracking if t.get("company")), ""),
        })
    refunds = []
    for refund in order.get("refunds") or []:
        if not isinstance(refund, dict):
            continue
        refunds.append({
            "created_at": refund.get("createdAt"),
            "transactions": [
                {"kind": t.get("kind"), "status": t.get("status"), "amount": _money(t.get("amountSet"))}
                for t in _nodes(refund.get("transactions"))
            ],
        })
    total = order.get("totalPriceSet") or {}
    return {
        "id": order.get("id"), "name": order.get("name"), "created_at": order.get("createdAt"),
        "processed_at": order.get("processedAt"), "updated_at": order.get("updatedAt"),
        "cancelled_at": order.get("cancelledAt"), "cancel_reason": order.get("cancelReason"),
        "currency": ((total.get("shopMoney") or {}).get("currencyCode") or order.get("currencyCode") or ""),
        "tags": ",".join(str(tag) for tag in (order.get("tags") or [])), "phone": order.get("phone"),
        "shipping_address": order.get("shippingAddress") or {}, "billing_address": order.get("billingAddress") or {},
        "payment_gateway_names": order.get("paymentGatewayNames") or [],
        "financial_status": str(order.get("displayFinancialStatus") or "").lower(),
        "fulfillment_status": str(order.get("displayFulfillmentStatus") or "").lower(),
        "total_price": _money(total), "current_total_price": _money(order.get("currentTotalPriceSet")),
        "shipping_lines": [{"title": line.get("title")} for line in _nodes(order.get("shippingLines"))],
        "fulfillments": fulfillments, "refunds": refunds,
        "line_items": [{"title": line.get("title"), "quantity": line.get("quantity"), "price": _money(line.get("originalUnitPriceSet"))} for line in _nodes(order.get("lineItems"))],
        "customer": ({"id": (order.get("customer") or {}).get("id"), "orders_count": (order.get("customer") or {}).get("numberOfOrders")} if isinstance(order.get("customer"), dict) else {}),
    }


def fetch_orders_updated(
    shop: str,
    access_token: str,
    start: date,
    end: date,
    report_tz: Any | None = None,
) -> list[dict[str, Any]]:
    """Fetch changed orders so the order-created cohort uses current outcome data."""

    return fetch_orders_by(
        shop=shop,
        access_token=access_token,
        start=start,
        end=end,
        date_filter="updated_at",
        report_tz=report_tz,
    )


def aggregate_orders_to_daily(orders: list[dict[str, Any]], report_tz: Any | None = None) -> list[DailyAggregate]:
    buckets: dict[tuple[date, str], DailyAggregate] = {}

    def _get_bucket(bucket_date: date, bucket_currency: str) -> DailyAggregate:
        k = (bucket_date, bucket_currency)
        if k not in buckets:
            buckets[k] = DailyAggregate(date=bucket_date, currency=bucket_currency)
        return buckets[k]

    for o in orders:
        d = _to_date(o.get("created_at"), report_tz=report_tz)
        if not d:
            continue

        currency = (o.get("currency") or "").strip() or "PKR"
        agg = _get_bucket(d, currency)

        amount = _dec(o.get("current_total_price") or o.get("total_price"))
        shipping_amount = sum((_dec(sl.get("price") or 0) for sl in (o.get("shipping_lines") or []) if isinstance(sl, dict)), Decimal("0"))
        refunded = _dec(o.get("total_refunded"))
        fin = (o.get("financial_status") or "").lower()
        fulfillment_status = (o.get("fulfillment_status") or "").lower()

        agg.total_count += 1
        agg.total_amount += amount
        agg.shipping_amount += shipping_amount

        gateways = o.get("payment_gateway_names") or []
        if not isinstance(gateways, list):
            gateways = [gateways]

        processing_method = o.get("processing_method")

        tags_raw = o.get("tags") or ""
        tags = [t.strip() for t in str(tags_raw).split(",") if t.strip()]

        shipping_lines = [sl for sl in (o.get("shipping_lines") or []) if isinstance(sl, dict)]
        shipping_titles = [str(sl.get("title") or "").strip() for sl in shipping_lines if sl.get("title")]

        fulfillments = o.get("fulfillments") or []
        if not isinstance(fulfillments, list):
            fulfillments = []

        tracking_companies: list[str] = []
        for f in fulfillments:
            if not isinstance(f, dict):
                continue
            tc = f.get("tracking_company")
            if tc:
                tracking_companies.append(str(tc))

        all_text_sources = list(gateways) + list(tags) + list(shipping_titles) + list(tracking_companies)

        is_cod = _order_is_cod(gateways=gateways, processing_method=processing_method, tags=tags)
        is_prepaid = _order_is_prepaid(
            gateways=gateways,
            financial_status=fin,
            processing_method=processing_method,
            tags=tags,
        ) or ((not is_cod) and _contains_any(all_text_sources, PREPAID_KEYWORDS))
        is_tcs = _contains_any(all_text_sources, TCS_KEYWORDS)
        is_postex = _contains_any(all_text_sources, POSTEX_KEYWORDS)

        if is_prepaid:
            agg.prepaid_count += 1
            agg.prepaid_amount += amount

        # Match the exporter script: tcs/postex amount is the ORDER total for orders
        # classified as tcs/postex (not the shipping-line price).
        if is_tcs:
            agg.tcs_count += 1
            agg.tcs_amount += amount

        if is_postex:
            agg.postex_count += 1
            agg.postex_amount += amount

        # A dispatched order belongs to its order-created day.  Orders fulfilled and
        # subsequently voided are deliberately excluded here: they have their own
        # mutually-exclusive "voided fulfilled" outcome.
        is_cancelled = bool(o.get("cancelled_at"))
        is_fulfilled = fulfillment_status == "fulfilled" or (isinstance(fulfillments, list) and len(fulfillments) > 0)
        is_voided_like = fin == "voided" or is_cancelled
        if is_fulfilled and not is_voided_like:
            agg.dispatch_count += 1
            if is_cod and _order_is_realised(o):
                agg.cod_realised_count += 1

        if is_fulfilled and is_cod:
            agg.cod_shipped_count += 1

        if refunded > 0:
            agg.returns_count += 1
            agg.refunded_amount += refunded

        # Voided orders: Shopify marks these as financial_status == 'voided'.
        # Track count + total value for visibility.
        if fin == "voided":
            agg.voided_count += 1
            # Shopify can set current_total_price to 0 for voided orders; use original total_price.
            voided_amount = _dec(o.get("total_price") or o.get("current_total_price") or 0)
            agg.voided_amount += voided_amount

        # Cancelled on-call beforehand (pre-dispatch): cancelled_at set and not fulfilled.
        # Stored separately so it doesn't interfere with existing voided_fulfilled logic.
        if is_voided_like and not is_fulfilled:
            agg.voided_pre_dispatch_count += 1
            cancelled_amount = _dec(o.get("total_price") or o.get("current_total_price") or 0)
            agg.voided_pre_dispatch_amount += cancelled_amount
        elif is_voided_like and is_fulfilled:
            voided_amount = _dec(o.get("total_price") or o.get("current_total_price") or 0)
            agg.voided_fulfilled_count += 1
            agg.voided_fulfilled_amount += voided_amount

    return sorted(buckets.values(), key=lambda x: (x.date, x.currency))


def upsert_daily_aggregates(aggregates: list[DailyAggregate]) -> None:
    for a in aggregates:
        existing = (
            ShopifyPricesRecord.query.filter(ShopifyPricesRecord.date == a.date)
            .filter(ShopifyPricesRecord.currency == a.currency)
            .one_or_none()
        )

        if existing is None:
            existing = ShopifyPricesRecord(date=a.date, currency=a.currency)
            db.session.add(existing)

        existing.total_count = a.total_count
        existing.total_amount = a.total_amount
        existing.shipping_amount = getattr(a, "shipping_amount", Decimal("0"))
        existing.prepaid_count = a.prepaid_count
        existing.prepaid_amount = a.prepaid_amount
        existing.tcs_count = a.tcs_count
        existing.tcs_amount = a.tcs_amount
        existing.postex_count = a.postex_count
        existing.postex_amount = a.postex_amount
        existing.dispatch_count = a.dispatch_count
        existing.cod_shipped_count = getattr(a, "cod_shipped_count", 0)
        existing.cod_realised_count = getattr(a, "cod_realised_count", 0)
        existing.first_order_count = getattr(a, "first_order_count", 0)
        existing.repeat_order_count = getattr(a, "repeat_order_count", 0)
        existing.realised_first_order_count = getattr(a, "realised_first_order_count", 0)
        existing.realised_repeat_order_count = getattr(a, "realised_repeat_order_count", 0)
        existing.returns_count = a.returns_count
        existing.refunded_amount = a.refunded_amount
        existing.voided_count = getattr(a, "voided_count", 0)
        existing.voided_amount = getattr(a, "voided_amount", Decimal("0"))

        existing.voided_pre_dispatch_count = getattr(a, "voided_pre_dispatch_count", 0)
        existing.voided_pre_dispatch_amount = getattr(a, "voided_pre_dispatch_amount", Decimal("0"))

        existing.voided_fulfilled_count = getattr(a, "voided_fulfilled_count", 0)
        existing.voided_fulfilled_amount = getattr(a, "voided_fulfilled_amount", Decimal("0"))
        existing.voided_fulfilled_tcs_count = getattr(a, "voided_fulfilled_tcs_count", 0)
        existing.voided_fulfilled_tcs_amount = getattr(a, "voided_fulfilled_tcs_amount", Decimal("0"))
        existing.voided_fulfilled_postex_count = getattr(a, "voided_fulfilled_postex_count", 0)
        existing.voided_fulfilled_postex_amount = getattr(a, "voided_fulfilled_postex_amount", Decimal("0"))
        existing.voided_fulfilled_mnp_count = getattr(a, "voided_fulfilled_mnp_count", 0)
        existing.voided_fulfilled_mnp_amount = getattr(a, "voided_fulfilled_mnp_amount", Decimal("0"))

    db.session.commit()


def _order_is_realised(order: dict[str, Any]) -> bool:
    """Return the best realised-order signal available in the current Shopify data.

    Shopify reliably exposes fulfillment, while final carrier delivery events are
    often absent. A fulfilled order that was not later voided/cancelled is the
    realised operational outcome already tracked by this dashboard.
    """
    financial_status = str(order.get("financial_status") or "").strip().lower()
    if financial_status == "voided" or bool(order.get("cancelled_at")):
        return False
    fulfillments = order.get("fulfillments") or []
    if not isinstance(fulfillments, list):
        return False
    return any(
        isinstance(fulfillment, dict)
        and (
            str(fulfillment.get("status") or "").strip().lower() in {"success", "fulfilled"}
            or str(fulfillment.get("shipment_status") or "").strip().lower() == "delivered"
        )
        for fulfillment in fulfillments
    )


def apply_repeat_customer_metrics(
    *,
    aggregates: dict[tuple[date, str], DailyAggregate],
    orders: list[dict[str, Any]],
    latest_orders_by_id: dict[str, dict[str, Any]],
    shop: str,
    access_token: str,
    start: date,
    end: date,
    report_tz: Any | None,
) -> None:
    """Add the requested repeat-order / first-order ratios for the reporting period."""
    by_customer: dict[str, list[dict[str, Any]]] = {}
    for order in orders:
        customer = order.get("customer") if isinstance(order.get("customer"), dict) else {}
        customer_id = str(customer.get("id") or "").strip()
        if not customer_id:
            continue
        created = _to_date(order.get("created_at"), report_tz=report_tz)
        if created and start <= created <= end:
            by_customer.setdefault(customer_id, []).append(order)

    for customer_id, customer_orders in by_customer.items():
        # Shopify includes each customer's lifetime orders_count on the order.
        # Using it avoids one extra API request per customer, which can otherwise
        # make a normal dashboard sync exceed a serverless timeout.
        lifetime_orders = max(
            int(((item.get("customer") or {}).get("orders_count") or 0))
            for item in customer_orders
            if isinstance(item.get("customer"), dict)
        )
        has_prior_order = lifetime_orders > len(customer_orders)
        for original in sorted(customer_orders, key=lambda item: str(item.get("created_at") or "")):
            order_id = str(original.get("id") or original.get("name") or original.get("order_number") or "")
            order = latest_orders_by_id.get(order_id, original)
            created = _to_date(original.get("created_at"), report_tz=report_tz)
            if not created:
                continue
            currency = str(original.get("currency") or "").strip() or "PKR"
            aggregate = aggregates.setdefault((created, currency), DailyAggregate(date=created, currency=currency))

            if has_prior_order:
                aggregate.repeat_order_count += 1
            else:
                aggregate.first_order_count += 1

            if _order_is_realised(order):
                if has_prior_order:
                    aggregate.realised_repeat_order_count += 1
                else:
                    aggregate.realised_first_order_count += 1

            # Every following selected-period order from this customer is repeat.
            has_prior_order = True


def _infer_service_used(order: dict[str, Any]) -> str:
    tags_raw = order.get("tags") or ""
    tags = [t.strip() for t in str(tags_raw).split(",") if t.strip()]

    shipping_lines = [sl for sl in (order.get("shipping_lines") or []) if isinstance(sl, dict)]
    shipping_titles = [str(sl.get("title") or "").strip() for sl in shipping_lines if sl.get("title")]

    fulfillments = order.get("fulfillments") or []
    if not isinstance(fulfillments, list):
        fulfillments = []

    tracking_companies: list[str] = []
    for f in fulfillments:
        if not isinstance(f, dict):
            continue
        tc = f.get("tracking_company")
        if tc:
            tracking_companies.append(str(tc))

    gateways = order.get("payment_gateway_names") or []
    if not isinstance(gateways, list):
        gateways = [gateways]

    carrier_sources = list(tags) + list(shipping_titles) + list(tracking_companies) + list(gateways)
    if _contains_any(carrier_sources, TCS_KEYWORDS):
        return "TCS"
    if _contains_any(carrier_sources, POSTEX_KEYWORDS):
        return "PostEx"
    if _contains_any(carrier_sources, MNP_KEYWORDS):
        return "MnP"
    return ""


def _infer_client_no(order: dict[str, Any]) -> str:
    shipping = order.get("shipping_address") if isinstance(order.get("shipping_address"), dict) else {}
    billing = order.get("billing_address") if isinstance(order.get("billing_address"), dict) else {}

    candidates = [
        shipping.get("phone"),
        billing.get("phone"),
        order.get("phone"),
    ]
    for c in candidates:
        s = str(c or "").strip()
        if s:
            return s
    return ""


def _infer_city(order: dict[str, Any]) -> str:
    shipping = order.get("shipping_address") if isinstance(order.get("shipping_address"), dict) else {}
    billing = order.get("billing_address") if isinstance(order.get("billing_address"), dict) else {}
    for c in [shipping.get("city"), billing.get("city")]:
        s = str(c or "").strip()
        if s:
            return s
    return ""


def _infer_order_no(order: dict[str, Any]) -> str:
    # Prefer Shopify's human-friendly name like "#1234".
    name = str(order.get("name") or "").strip()
    if name:
        return name
    num = order.get("order_number")
    if num is not None:
        return str(num).strip()
    return ""


def upsert_daily_return_details(start: date, end: date, orders: list[dict[str, Any]], report_tz: Any | None = None) -> None:
    # Rebuild detail rows for the range (simple + deterministic).
    (
        db.session.query(ShopifyReturnDetailRecord)
        .filter(ShopifyReturnDetailRecord.date >= start)
        .filter(ShopifyReturnDetailRecord.date <= end)
        .delete(synchronize_session=False)
    )

    for o in orders:
        fin = str(o.get("financial_status") or "").strip().lower()
        is_cancelled = bool(o.get("cancelled_at"))
        is_voided_like = fin == "voided" or is_cancelled

        fulfillments = o.get("fulfillments") or []
        if not isinstance(fulfillments, list):
            fulfillments = []

        fulfillment_status = (o.get("fulfillment_status") or "").lower()
        is_fulfilled = fulfillment_status == "fulfilled" or any(
            isinstance(f, dict) and str(f.get("status") or "").lower() in {"success", "fulfilled"}
            for f in fulfillments
        )

        if not (is_voided_like and is_fulfilled):
            continue

        created_d = _to_date(o.get("created_at"), report_tz=report_tz)
        if not created_d or not (start <= created_d <= end):
            continue
        order_no = _infer_order_no(o)
        if not order_no:
            continue

        rec = ShopifyReturnDetailRecord(
            date=created_d,
            order_no=order_no,
            client_no=_infer_client_no(o),
            city=_infer_city(o),
            service_used=_infer_service_used(o),
        )
        db.session.add(rec)

    db.session.commit()


def upsert_daily_product_sales(*, start: date, end: date, orders: list[dict[str, Any]], report_tz: Any | None = None) -> None:
    """Rebuild per-day product quantities for the selected range.

    This stores units sold by order created_at day (simple + fast).
    We exclude financially voided orders so cancelled-after-order-creation doesn't inflate quantities.
    """

    totals: dict[tuple[date, str], int] = {}

    for o in orders:
        d = _to_date(o.get("created_at"), report_tz=report_tz)
        if not d or not (start <= d <= end):
            continue

        fin = str(o.get("financial_status") or "").strip().lower()
        if fin == "voided":
            continue

        line_items = o.get("line_items") or []
        if not isinstance(line_items, list):
            continue

        for li in line_items:
            if not isinstance(li, dict):
                continue
            title = str(li.get("title") or "").strip()
            if not title:
                continue
            try:
                qty = int(li.get("quantity") or 0)
            except Exception:
                qty = 0
            if qty <= 0:
                continue
            k = (d, title)
            totals[k] = totals.get(k, 0) + qty

    # Rebuild the window to keep results consistent when tags/statuses change.
    (
        db.session.query(ShopifyProductSalesRecord)
        .filter(ShopifyProductSalesRecord.date >= start)
        .filter(ShopifyProductSalesRecord.date <= end)
        .delete(synchronize_session=False)
    )

    for (d, title), qty in totals.items():
        db.session.add(ShopifyProductSalesRecord(date=d, product_title=title, quantity=qty))

    db.session.commit()


def upsert_daily_product_value(*, start: date, end: date, orders: list[dict[str, Any]], report_tz: Any | None = None) -> None:
    """Rebuild per-day product gross value totals for the selected range.

    Value is computed as sum(line_item.price * line_item.quantity) by order created_at day.
    We exclude financially voided orders so cancelled-after-order-creation doesn't inflate totals.
    """

    totals: dict[tuple[date, str], Decimal] = {}

    for o in orders:
        d = _to_date(o.get("created_at"), report_tz=report_tz)
        if not d or not (start <= d <= end):
            continue

        fin = str(o.get("financial_status") or "").strip().lower()
        if fin == "voided":
            continue

        line_items = o.get("line_items") or []
        if not isinstance(line_items, list):
            continue

        for li in line_items:
            if not isinstance(li, dict):
                continue

            title = str(li.get("title") or "").strip()
            if not title:
                continue

            try:
                qty = int(li.get("quantity") or 0)
            except Exception:
                qty = 0
            if qty <= 0:
                continue

            raw_price = li.get("price")
            try:
                unit_price = Decimal(str(raw_price or "0"))
            except Exception:
                unit_price = Decimal("0")

            line_total = unit_price * qty
            if line_total <= 0:
                continue

            k = (d, title)
            totals[k] = totals.get(k, Decimal("0")) + line_total

    # Rebuild the window to keep results consistent when tags/statuses change.
    (
        db.session.query(ShopifyProductValueRecord)
        .filter(ShopifyProductValueRecord.date >= start)
        .filter(ShopifyProductValueRecord.date <= end)
        .delete(synchronize_session=False)
    )

    for (d, title), amt in totals.items():
        try:
            amt = amt.quantize(Decimal("0.01"))
        except Exception:
            pass
        db.session.add(ShopifyProductValueRecord(date=d, product_title=title, amount=amt))

    db.session.commit()


def upsert_daily_product_cities(*, start: date, end: date, orders: list[dict[str, Any]], report_tz: Any | None = None) -> None:
    """Rebuild per-day product unit totals by destination city."""

    totals: dict[tuple[date, str, str], int] = {}

    for o in orders:
        d = _to_date(o.get("created_at"), report_tz=report_tz)
        if not d or not (start <= d <= end):
            continue
        if str(o.get("financial_status") or "").strip().lower() == "voided":
            continue

        city = _infer_city(o) or "Unknown"
        line_items = o.get("line_items") or []
        if not isinstance(line_items, list):
            continue

        for li in line_items:
            if not isinstance(li, dict):
                continue
            title = str(li.get("title") or "").strip()
            if not title:
                continue
            try:
                qty = int(li.get("quantity") or 0)
            except Exception:
                qty = 0
            if qty > 0:
                k = (d, title, city)
                totals[k] = totals.get(k, 0) + qty

    (
        db.session.query(ShopifyProductCityRecord)
        .filter(ShopifyProductCityRecord.date >= start)
        .filter(ShopifyProductCityRecord.date <= end)
        .delete(synchronize_session=False)
    )
    for (d, title, city), quantity in totals.items():
        db.session.add(ShopifyProductCityRecord(date=d, product_title=title, city=city, quantity=quantity))
    db.session.commit()


def upsert_daily_city_sales(*, start: date, end: date, orders: list[dict[str, Any]], report_tz: Any | None = None) -> None:
    """Rebuild per-day order counts grouped by city for the selected range.

    City is taken from shipping_address.city, falling back to billing_address.city.
    We exclude voided and cancelled orders to stay consistent with other aggregates.
    """

    totals: dict[tuple[date, str], int] = {}

    for o in orders:
        d = _to_date(o.get("created_at"), report_tz=report_tz)
        if not d or not (start <= d <= end):
            continue

        fin = str(o.get("financial_status") or "").strip().lower()
        if fin == "voided":
            continue

        if o.get("cancelled_at"):
            continue

        addr = o.get("shipping_address") or o.get("billing_address") or {}
        if not isinstance(addr, dict):
            addr = {}
        city = str(addr.get("city") or "").strip()
        if not city:
            continue

        city_norm = " ".join(city.split())
        k = (d, city_norm)
        totals[k] = totals.get(k, 0) + 1

    (
        db.session.query(ShopifyCitySalesRecord)
        .filter(ShopifyCitySalesRecord.date >= start)
        .filter(ShopifyCitySalesRecord.date <= end)
        .delete(synchronize_session=False)
    )

    for (d, city), n in totals.items():
        db.session.add(ShopifyCitySalesRecord(date=d, city=city, orders=n))

    db.session.commit()


def can_sync_shopify() -> bool:
    shop, token = _get_credentials()
    return bool(shop and token)


def get_shopify_credentials() -> tuple[str | None, str | None]:
        """Return (shop_domain, access_token) from environment/config."""

        return _get_credentials()


def fetch_markets(*, shop: str, access_token: str, first: int = 50) -> list[dict[str, Any]]:
        """Fetch Shopify Markets (best-effort) including region countries.

        Requires the access token to have `read_markets` scope.
        """

        gql = """
        query Markets($first: Int!) {
            markets(first: $first) {
                nodes {
                    id
                    name
                    handle
                    status
                    type
                    conditions {
                        regionsCondition {
                            regions(first: 250) {
                                nodes {
                                    __typename
                                    ... on MarketRegionCountry {
                                        id
                                        code
                                        name
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
        """

        data = shopify_graphql(shop, access_token, gql, variables={"first": int(first)})
        nodes = (((data.get("data") or {}).get("markets") or {}).get("nodes"))
        if not isinstance(nodes, list):
                return []

        out: list[dict[str, Any]] = []
        for m in nodes:
                if not isinstance(m, dict):
                        continue

                conditions = m.get("conditions") if isinstance(m.get("conditions"), dict) else {}
                regions_condition = conditions.get("regionsCondition") if isinstance(conditions.get("regionsCondition"), dict) else {}
                regions = regions_condition.get("regions") if isinstance(regions_condition.get("regions"), dict) else {}
                region_nodes = regions.get("nodes") if isinstance(regions.get("nodes"), list) else []

                countries: list[dict[str, str]] = []
                for r in region_nodes:
                        if not isinstance(r, dict):
                                continue
                        if r.get("__typename") != "MarketRegionCountry":
                                continue
                        code = str(r.get("code") or "").strip()
                        name = str(r.get("name") or "").strip()
                        if not code and not name:
                                continue
                        countries.append({"code": code, "name": name})

                out.append(
                        {
                                "id": str(m.get("id") or ""),
                                "name": str(m.get("name") or ""),
                                "handle": str(m.get("handle") or ""),
                                "status": str(m.get("status") or ""),
                                "type": str(m.get("type") or ""),
                                "countries": countries,
                        }
                )

        return out


def sync_shopify_range(app: Flask, start: date, end: date) -> dict[str, Any]:
    shop, token = _get_credentials()
    if not shop or not token:
        raise ShopifySyncError(
            "Missing authenticated Shopify merchant context"
        )

    # Use Shopify's reporting timezone for daily bucketing (fixes +/-1 drift near midnight).
    tz_name = get_shopify_report_tz_name() or fetch_shop_iana_timezone(shop, token)
    report_tz = None
    if tz_name and ZoneInfo is not None:
        try:
            report_tz = ZoneInfo(tz_name)
        except Exception:
            report_tz = None

    with app.app_context():
        # Main daily metrics are based on orders created in the range.
        orders = fetch_orders(shop=shop, access_token=token, start=start, end=end, report_tz=report_tz)
        aggregates = aggregate_orders_to_daily(orders, report_tz=report_tz)

        # Product analytics: units sold per product per day (created_at day).
        upsert_daily_product_sales(start=start, end=end, orders=orders, report_tz=report_tz)

        # Product analytics: gross value per product per day (created_at day).
        upsert_daily_product_value(start=start, end=end, orders=orders, report_tz=report_tz)

        # Product analytics: units sold per product and destination city per day.
        upsert_daily_product_cities(start=start, end=end, orders=orders, report_tz=report_tz)

        # City analytics: orders per city per day (created_at day).
        upsert_daily_city_sales(start=start, end=end, orders=orders, report_tz=report_tz)

        agg_by_key: dict[tuple[date, str], DailyAggregate] = {(a.date, a.currency): a for a in aggregates}

        # Every daily outcome is assigned to the Shopify order-created day. This
        # makes the daily order total reconcilable with its outcome breakdown.
        now_dt = datetime.now(timezone.utc)
        if report_tz is not None:
            now_dt = now_dt.astimezone(report_tz)
        today_local = now_dt.date()

        # Fetch updated orders for fulfillment/refund/void signals that can happen after creation.
        ops_lookahead_days = 30
        ops_fetch_end = min(today_local, end + timedelta(days=ops_lookahead_days))
        updated_orders_for_ops = fetch_orders_updated(
            shop=shop,
            access_token=token,
            start=start,
            end=ops_fetch_end,
            report_tz=report_tz,
        )
        orders_fetched_updated_for_ops = len(updated_orders_for_ops)

        orders_for_ops_by_id: dict[str, dict[str, Any]] = {}
        for o in updated_orders_for_ops + orders:
            order_key = str(o.get("id") or o.get("name") or o.get("order_number") or "")
            if order_key:
                orders_for_ops_by_id[order_key] = o

        # Return details: per-order rows for the Shopify view:
        # Date = order date, Payment status = Voided, Fulfillment status = Fulfilled.
        upsert_daily_return_details(
            start=start,
            end=end,
            orders=list(orders_for_ops_by_id.values()),
            report_tz=report_tz,
        )

        # Establish currencies we will upsert for.
        currencies = sorted(
            ({a.currency or "PKR" for a in aggregates} | {str((o.get("currency") or "")).strip() or "PKR" for o in updated_orders_for_ops})
            or {"PKR"}
        )

        # Ensure rows exist for every day/currency and reset operational fields in-range.
        for cur in currencies:
            d = start
            while d <= end:
                key = (d, cur)
                if key not in agg_by_key:
                    agg_by_key[key] = DailyAggregate(date=d, currency=cur)
                # Recompute selected operational metrics in-range.
                agg_by_key[key].prepaid_count = 0
                agg_by_key[key].prepaid_amount = Decimal("0")
                agg_by_key[key].dispatch_count = 0
                agg_by_key[key].cod_shipped_count = 0
                agg_by_key[key].cod_realised_count = 0
                agg_by_key[key].tcs_count = 0
                agg_by_key[key].tcs_amount = Decimal("0")
                agg_by_key[key].postex_count = 0
                agg_by_key[key].postex_amount = Decimal("0")

                # Returns should be attributed to the day the refund happened (not order day).
                agg_by_key[key].returns_count = 0
                agg_by_key[key].refunded_amount = Decimal("0")

                # Returned/cancelled-after-fulfillment metrics (fulfilled + voided) are operational.
                agg_by_key[key].voided_fulfilled_count = 0
                agg_by_key[key].voided_fulfilled_amount = Decimal("0")
                agg_by_key[key].voided_fulfilled_tcs_count = 0
                agg_by_key[key].voided_fulfilled_tcs_amount = Decimal("0")
                agg_by_key[key].voided_fulfilled_postex_count = 0
                agg_by_key[key].voided_fulfilled_postex_amount = Decimal("0")
                agg_by_key[key].voided_fulfilled_mnp_count = 0
                agg_by_key[key].voided_fulfilled_mnp_amount = Decimal("0")
                agg_by_key[key].voided_pre_dispatch_count = 0
                agg_by_key[key].voided_pre_dispatch_amount = Decimal("0")
                agg_by_key[key].first_order_count = 0
                agg_by_key[key].repeat_order_count = 0
                agg_by_key[key].realised_first_order_count = 0
                agg_by_key[key].realised_repeat_order_count = 0
                d = d + timedelta(days=1)

        counted_returns: set[tuple[str, date, str]] = set()

        # Apply operational metrics. Order outcomes, prepaid and courier data use
        # order created_at. Returns continue to use refund.created_at.
        for o in orders_for_ops_by_id.values():
            currency = (o.get("currency") or "").strip() or "PKR"
            amount = _dec(o.get("current_total_price") or o.get("total_price"))
            # For TCS load, the ops sheet typically uses the original order total.
            amount_tcs = _dec(o.get("total_price") or o.get("current_total_price") or 0)
            refunded = _dec(o.get("total_refunded"))

            created_d = _to_date(o.get("created_at"), report_tz=report_tz)

            fin = str(o.get("financial_status") or "").strip().lower()
            is_unpaid = fin not in {"paid", "partially_paid", "partially paid"}

            tags_raw = o.get("tags") or ""
            tags = [t.strip() for t in str(tags_raw).split(",") if t.strip()]

            gateways = o.get("payment_gateway_names") or []
            if not isinstance(gateways, list):
                gateways = [gateways]

            processing_method = o.get("processing_method")
            is_cod = _order_is_cod(gateways=gateways, processing_method=processing_method, tags=tags)

            # Prepaid (per ops/load sheet) means "money received".
            # IMPORTANT: include COD orders that later become financially "paid".
            is_prepaid = fin in {"paid", "partially_paid", "partially paid"}
            prepaid_net_amount = amount - refunded
            if prepaid_net_amount < 0:
                prepaid_net_amount = Decimal("0")

            shipping_lines = [sl for sl in (o.get("shipping_lines") or []) if isinstance(sl, dict)]
            shipping_titles = [str(sl.get("title") or "").strip() for sl in shipping_lines if sl.get("title")]

            fulfillments = o.get("fulfillments") or []
            if not isinstance(fulfillments, list):
                fulfillments = []

            fulfillment_status = (o.get("fulfillment_status") or "").lower()
            is_fulfilled = fulfillment_status == "fulfilled" or any(
                isinstance(f, dict) and str(f.get("status") or "").lower() in {"success", "fulfilled"}
                for f in fulfillments
            )
            is_realised = _order_is_realised(o)

            tracking_companies: list[str] = []
            for f in fulfillments:
                if not isinstance(f, dict):
                    continue
                tc = f.get("tracking_company")
                if tc:
                    tracking_companies.append(str(tc))

            # Primary signal is tags (matches Shopify Admin pills);
            # tracking_company/shipping title are fallbacks.
            carrier_sources = list(tags) + list(shipping_titles) + list(tracking_companies) + list(gateways)
            # IMPORTANT: For TCS load sheets we keep stricter matching elsewhere.
            # For returns (voided+fulfilled), we allow broader keyword matching so tags like
            # "Shipped by TCS Courier" / "Ship by TCS" / tracking_company signals are counted.
            is_tcs = _contains_any(carrier_sources, TCS_KEYWORDS)
            is_postex = _contains_any(carrier_sources, POSTEX_KEYWORDS)
            is_mnp = _contains_any(carrier_sources, MNP_KEYWORDS)

            # Returns: attribute to the day the refund happened.
            refunds = o.get("refunds") or []
            if isinstance(refunds, list):
                order_id = str(o.get("id") or o.get("name") or "")
                for r in refunds:
                    if not isinstance(r, dict):
                        continue
                    r_date = _to_date(r.get("created_at"), report_tz=report_tz)
                    if not r_date or not (start <= r_date <= end):
                        continue

                    key = (r_date, currency)
                    if key not in agg_by_key:
                        agg_by_key[key] = DailyAggregate(date=r_date, currency=currency)

                    # Count each order once per refund day.
                    count_key = (order_id, r_date, currency)
                    if count_key not in counted_returns:
                        counted_returns.add(count_key)
                        agg_by_key[key].returns_count += 1

                    txs = r.get("transactions") or []
                    if isinstance(txs, list) and txs:
                        for t in txs:
                            if not isinstance(t, dict):
                                continue
                            kind = str(t.get("kind") or "").strip().lower()
                            status = str(t.get("status") or "").strip().lower()
                            if kind and kind != "refund":
                                continue
                            if status and status not in {"success", "pending"}:
                                continue
                            agg_by_key[key].refunded_amount += _dec(t.get("amount") or 0)

            # Fulfilled + voided-like: shipped out, then voided/cancelled.
            # Shopify sometimes expresses this as:
            # - financial_status == 'voided'
            # - or order status 'canceled' (cancelled_at is set)
            #
            # Match the Shopify Returns view from Admin:
            # Date = order date, Payment status = Voided, Fulfillment status = Fulfilled.
            is_cancelled = bool(o.get("cancelled_at"))
            is_voided_like = fin == "voided" or is_cancelled
            if is_voided_like and is_fulfilled:
                created_d = _to_date(o.get("created_at"), report_tz=report_tz)
                if created_d and start <= created_d <= end:
                    key = (created_d, currency)
                    if key not in agg_by_key:
                        agg_by_key[key] = DailyAggregate(date=created_d, currency=currency)

                    voided_amount = _dec(o.get("total_price") or o.get("current_total_price") or 0)
                    agg_by_key[key].voided_fulfilled_count += 1
                    agg_by_key[key].voided_fulfilled_amount += voided_amount

                    if is_tcs:
                        agg_by_key[key].voided_fulfilled_tcs_count += 1
                        agg_by_key[key].voided_fulfilled_tcs_amount += voided_amount
                    if is_postex:
                        agg_by_key[key].voided_fulfilled_postex_count += 1
                        agg_by_key[key].voided_fulfilled_postex_amount += voided_amount
                    if is_mnp:
                        agg_by_key[key].voided_fulfilled_mnp_count += 1
                        agg_by_key[key].voided_fulfilled_mnp_amount += voided_amount

            order_bucket_dates: set[date] = set()
            if created_d and start <= created_d <= end:
                order_bucket_dates.add(created_d)

            postex_bucket_dates: set[date] = set()
            if is_postex:
                postex_bucket_dates = set(order_bucket_dates)

            # Prepaid: bucket to the order date (count once per order).
            prepaid_bucket_dates: set[date] = set()
            if is_prepaid and prepaid_net_amount > 0:
                prepaid_bucket_dates = set(order_bucket_dates)

            # TCS load: bucket unpaid orders with the explicit TCS tag to the order date.
            tcs_bucket_dates: set[date] = set()
            if is_tcs and is_fulfilled and is_unpaid:
                tcs_bucket_dates = set(order_bucket_dates)

            bucket_dates = order_bucket_dates | postex_bucket_dates | prepaid_bucket_dates | tcs_bucket_dates

            for b_date in sorted(bucket_dates):
                key = (b_date, currency)
                if key not in agg_by_key:
                    agg_by_key[key] = DailyAggregate(date=b_date, currency=currency)

                # Outcomes are mutually exclusive and all use the order-created
                # date. A fulfilled order later voided is counted only in
                # voided_fulfilled_count, never as a dispatch as well.
                if b_date in order_bucket_dates and is_fulfilled and not is_voided_like:
                    agg_by_key[key].dispatch_count += 1
                elif b_date in order_bucket_dates and is_voided_like and not is_fulfilled:
                    agg_by_key[key].voided_pre_dispatch_count += 1
                    agg_by_key[key].voided_pre_dispatch_amount += _dec(o.get("total_price") or o.get("current_total_price") or 0)

                if b_date in order_bucket_dates and is_cod and is_fulfilled:
                    agg_by_key[key].cod_shipped_count += 1
                if b_date in order_bucket_dates and is_cod and is_fulfilled and is_realised and not is_voided_like:
                    agg_by_key[key].cod_realised_count += 1

                if is_tcs and b_date in tcs_bucket_dates:
                    agg_by_key[key].tcs_count += 1
                    agg_by_key[key].tcs_amount += amount_tcs
                if is_postex and b_date in postex_bucket_dates:
                    agg_by_key[key].postex_count += 1
                    agg_by_key[key].postex_amount += amount
                if is_prepaid and b_date in prepaid_bucket_dates:
                    agg_by_key[key].prepaid_count += 1
                    agg_by_key[key].prepaid_amount += prepaid_net_amount

        apply_repeat_customer_metrics(
            aggregates=agg_by_key,
            orders=orders,
            latest_orders_by_id=orders_for_ops_by_id,
            shop=shop,
            access_token=token,
            start=start,
            end=end,
            report_tz=report_tz,
        )
        aggregates = sorted(agg_by_key.values(), key=lambda x: (x.date, x.currency))
        upsert_daily_aggregates(aggregates)
        return {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "orders_fetched": len(orders),
            "orders_fetched_updated_for_ops": orders_fetched_updated_for_ops,
            "daily_rows_upserted": len(aggregates),
            "report_timezone": tz_name or "UTC",
        }


def sync_last_n_days(app: Flask, days: int | None = None) -> dict[str, Any]:
    n = days if days is not None else get_sync_days()
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=n - 1)
    return sync_shopify_range(app, start=start, end=today)
