from __future__ import annotations

from datetime import date

from sqlalchemy.sql import func

from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()




def _request_shop_domain() -> str:
    try:
        from flask import g
        return str(getattr(g, "shopify_shop", "") or "").strip().lower()
    except RuntimeError:
        return ""

def _request_shop_domain() -> str:
    """Default tenant for records created during an authenticated request."""
    try:
        from flask import g

        return str(getattr(g, "shopify_shop", "") or "").strip().lower()
    except RuntimeError:
        return ""


class TenantMixin:
    """Adds mandatory per-Shopify-store isolation to analytics records."""

    shop_domain = db.Column(
        db.String(255),
        nullable=False,
        index=True,
        default=_request_shop_domain,
    )


class ShopifyPricesRecord(TenantMixin, db.Model):
    __tablename__ = "shopify_prices_record"

    __table_args__ = (
        db.UniqueConstraint("shop_domain", "date", "currency", name="uq_shopify_prices_record_shop_date_currency"),
    )

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, default=date.today)

    total_count = db.Column(db.Integer, nullable=False, default=0)
    total_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)
    shipping_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    prepaid_count = db.Column(db.Integer, nullable=False, default=0)
    prepaid_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    tcs_count = db.Column(db.Integer, nullable=False, default=0)
    tcs_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    postex_count = db.Column(db.Integer, nullable=False, default=0)
    postex_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    dispatch_count = db.Column(db.Integer, nullable=False, default=0)
    dispatch_fulfillments_created_count = db.Column(db.Integer, nullable=False, default=0)
    cod_shipped_count = db.Column(db.Integer, nullable=False, default=0)
    cod_realised_count = db.Column(db.Integer, nullable=False, default=0)
    first_order_count = db.Column(db.Integer, nullable=False, default=0)
    repeat_order_count = db.Column(db.Integer, nullable=False, default=0)
    realised_first_order_count = db.Column(db.Integer, nullable=False, default=0)
    realised_repeat_order_count = db.Column(db.Integer, nullable=False, default=0)

    returns_count = db.Column(db.Integer, nullable=False, default=0)
    refunded_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    voided_count = db.Column(db.Integer, nullable=False, default=0)
    voided_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    # Orders cancelled before dispatch ("cancelled on-call beforehand").
    # This is separate from voided_fulfilled_* which is fulfilled then later voided.
    voided_pre_dispatch_count = db.Column(db.Integer, nullable=False, default=0)
    voided_pre_dispatch_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    # Orders that were fulfilled (shipped) but later voided (returned/cancelled after fulfillment).
    voided_fulfilled_count = db.Column(db.Integer, nullable=False, default=0)
    voided_fulfilled_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    voided_fulfilled_tcs_count = db.Column(db.Integer, nullable=False, default=0)
    voided_fulfilled_tcs_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    voided_fulfilled_postex_count = db.Column(db.Integer, nullable=False, default=0)
    voided_fulfilled_postex_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    voided_fulfilled_mnp_count = db.Column(db.Integer, nullable=False, default=0)
    voided_fulfilled_mnp_amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)

    currency = db.Column(db.String(10), nullable=False, default="")


class ShopifyProductSalesRecord(TenantMixin, db.Model):
    __tablename__ = "shopify_product_sales_record"

    __table_args__ = (
        db.UniqueConstraint("shop_domain", "date", "product_title", name="uq_shopify_product_sales_record_shop_date_product_title"),
    )

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, default=date.today)

    product_title = db.Column(db.String(255), nullable=False, default="")
    quantity = db.Column(db.Integer, nullable=False, default=0)


class ShopifyProductValueRecord(TenantMixin, db.Model):
    __tablename__ = "shopify_product_value_record"

    __table_args__ = (
        db.UniqueConstraint("shop_domain", "date", "product_title", name="uq_shopify_product_value_record_shop_date_product_title"),
    )

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, default=date.today)

    product_title = db.Column(db.String(255), nullable=False, default="")
    amount = db.Column(db.Numeric(12, 2), nullable=False, default=0)


class ShopifyProductCityRecord(TenantMixin, db.Model):
    """Units sold for a product in a city on a given order-created day."""

    __tablename__ = "shopify_product_city_record"

    __table_args__ = (
        db.UniqueConstraint("shop_domain", "date", "product_title", "city", name="uq_shopify_product_city_record_shop_date_product_city"),
    )

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, default=date.today)
    product_title = db.Column(db.String(255), nullable=False, default="")
    city = db.Column(db.String(128), nullable=False, default="")
    quantity = db.Column(db.Integer, nullable=False, default=0)


class ShopifyCitySalesRecord(TenantMixin, db.Model):
    __tablename__ = "shopify_city_sales_record"

    __table_args__ = (
        db.UniqueConstraint("shop_domain", "date", "city", name="uq_shopify_city_sales_record_shop_date_city"),
    )

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, default=date.today)

    city = db.Column(db.String(128), nullable=False, default="")
    orders = db.Column(db.Integer, nullable=False, default=0)


class ShopifyReturnDetailRecord(TenantMixin, db.Model):
    __tablename__ = "shopify_return_detail_record"

    __table_args__ = (
        db.UniqueConstraint("shop_domain", "date", "order_no", name="uq_shopify_return_detail_record_shop_date_order_no"),
        db.Index("ix_shopify_return_detail_record_date", "date"),
    )

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, default=date.today)

    order_no = db.Column(db.String(64), nullable=False, default="")
    client_no = db.Column(db.String(64), nullable=False, default="")
    city = db.Column(db.String(128), nullable=False, default="")
    service_used = db.Column(db.String(32), nullable=False, default="")


class AdminUser(db.Model):
    __tablename__ = "admin_users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False, default="")


class AppSetting(db.Model):
    __tablename__ = "app_settings"

    shop_domain = db.Column(db.String(255), primary_key=True)
    key = db.Column(db.String(128), primary_key=True)
    value = db.Column(db.Text, nullable=False, default="")
    updated_at = db.Column(db.DateTime, nullable=False, server_default=func.now(), onupdate=func.now())
