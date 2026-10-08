import base64
import asyncio
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import quote, urlencode

import httpx

from app.services.config import settings
from app.services.db import (
    ONE_TIME_PACKAGE_REQUESTS,
    PRO_PLAN_DAYS,
    PRO_PLAN_REQUESTS,
    apply_payment_entitlements_if_needed,
    claim_payment_side_effects,
    complete_payment_side_effects,
    get_payment_by_external_id,
    insert_pending_payment_if_missing,
    list_payments_for_side_effect_retry,
    record_payment,
)
from app.services.error_logger import log_api_error
from app.services.fiscal import create_receipt_for_payment
from app.services.notifications import notify_payment_success


@dataclass(frozen=True)
class BillingPlan:
    code: str
    title: str
    price_rub: int
    request_balance: int
    duration_days: int | None = None


PLANS = {
    "one_time": BillingPlan(
        code="one_time",
        title="Разовый",
        price_rub=99,
        request_balance=ONE_TIME_PACKAGE_REQUESTS,
    ),
    "pro": BillingPlan(
        code="pro",
        title="PRO",
        price_rub=349,
        request_balance=PRO_PLAN_REQUESTS,
        duration_days=PRO_PLAN_DAYS,
    ),
}


def get_plan(plan_code: str) -> BillingPlan | None:
    return PLANS.get(plan_code)


def get_plan_description(plan_code: str) -> str:
    if plan_code == "one_time":
        return "Пакет 5 проверок SmartCheck"
    if plan_code == "pro":
        return "Подписка SmartCheck PRO на 30 дней"
    return "Услуга SmartCheck"


def _sign_payload(payload: str) -> str:
    signature = hmac.new(
        settings.BILLING_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return signature


def sign_webhook_payload(raw_body: bytes) -> str:
    return hmac.new(
        settings.BILLING_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()


def verify_webhook_signature(raw_body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    return hmac.compare_digest(sign_webhook_payload(raw_body), signature)


def build_external_payment_id(provider: str, source_token: str) -> str:
    digest = hashlib.sha256(source_token.encode("utf-8")).hexdigest()[:24]
    return f"{provider}_{digest}"


def create_checkout_token(
    platform: str,
    user_id: str,
    plan_code: str,
    ttl_seconds: int = 1800,
    external_payment_id: str | None = None,
) -> str:
    payload = {
        "platform": platform,
        "user_id": user_id,
        "plan_code": plan_code,
        "exp": int(time.time()) + ttl_seconds,
    }
    if external_payment_id:
        payload["external_payment_id"] = external_payment_id
    payload_json = json.dumps(payload, separators=(",", ":"), ensure_ascii=True)
    signature = _sign_payload(payload_json)
    raw = f"{payload_json}.{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("utf-8")


def parse_checkout_token(token: str) -> dict:
    try:
        decoded = base64.urlsafe_b64decode(token.encode("utf-8")).decode("utf-8")
        payload_json, signature = decoded.rsplit(".", 1)
    except Exception as error:
        raise ValueError("Invalid checkout token") from error

    if not hmac.compare_digest(_sign_payload(payload_json), signature):
        raise ValueError("Invalid checkout token signature")

    try:
        payload = json.loads(payload_json)
    except json.JSONDecodeError as error:
        raise ValueError("Invalid checkout token payload") from error
    if not isinstance(payload, dict):
        raise ValueError("Invalid checkout token payload")
    if payload.get("platform") not in {"telegram", "vk"}:
        raise ValueError("Invalid checkout platform")
    if not get_plan(str(payload.get("plan_code") or "")):
        raise ValueError("Invalid checkout plan")
    if not str(payload.get("user_id") or "").strip():
        raise ValueError("Invalid checkout user")
    if int(payload.get("exp", 0)) < int(time.time()):
        raise ValueError("Checkout token expired")
    return payload


def create_checkout_url(platform: str, user_id: str, plan_code: str) -> str:
    token = create_checkout_token(platform=platform, user_id=user_id, plan_code=plan_code)
    query = urlencode({"token": token})
    return f"{settings.APP_BASE_URL.rstrip('/')}/billing/checkout?{query}"


def format_price_value(price_rub: int) -> str:
    return str(Decimal(price_rub).quantize(Decimal("1.00")))


def format_robokassa_out_sum(price_rub: int) -> str:
    amount = Decimal(price_rub).quantize(Decimal("1.000000"), rounding=ROUND_HALF_UP)
    if settings.ROBOKASSA_TEST_MODE:
        return str(int(amount))
    return f"{amount:.6f}"


def build_robokassa_inv_id(external_payment_id: str) -> str:
    digest = hashlib.sha256(external_payment_id.encode("utf-8")).hexdigest()
    return str((int(digest[:16], 16) % 2_000_000_000) + 1)


def build_robokassa_receipt(plan: BillingPlan) -> str:
    receipt = {
        "items": [
            {
                "name": get_plan_description(plan.code),
                "quantity": 1,
                "sum": plan.price_rub,
                "payment_method": "full_payment",
                "payment_object": "service",
                "tax": "none",
            }
        ]
    }
    receipt_json = json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))
    return quote(receipt_json, safe="")


def _build_robokassa_signature(*parts: str) -> str:
    payload = ":".join(parts)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def create_robokassa_payment_url(
    platform: str,
    user_id: str,
    plan: BillingPlan,
    external_payment_id: str,
) -> str:
    if not settings.ROBOKASSA_MERCHANT_LOGIN or not settings.ROBOKASSA_PASSWORD1 or not settings.ROBOKASSA_PASSWORD2:
        raise ValueError("ROBOKASSA_MERCHANT_LOGIN, ROBOKASSA_PASSWORD1 and ROBOKASSA_PASSWORD2 are required for Robokassa checkout")

    out_sum = format_robokassa_out_sum(plan.price_rub)
    inv_id = build_robokassa_inv_id(external_payment_id)
    receipt = build_robokassa_receipt(plan)
    shp_params = {
        "Shp_external_payment_id": external_payment_id,
        "Shp_platform": platform,
        "Shp_user_id": user_id,
        "Shp_plan_code": plan.code,
    }
    signature_parts = [
        settings.ROBOKASSA_MERCHANT_LOGIN,
        out_sum,
        inv_id,
        receipt,
        settings.ROBOKASSA_PASSWORD1,
    ]
    for key in sorted(shp_params):
        signature_parts.append(f"{key}={shp_params[key]}")
    signature = _build_robokassa_signature(*signature_parts)

    params = {
        "MerchantLogin": settings.ROBOKASSA_MERCHANT_LOGIN,
        "OutSum": out_sum,
        "InvId": inv_id,
        "Description": get_plan_description(plan.code),
        "Receipt": receipt,
        "SignatureValue": signature,
        **shp_params,
    }
    if settings.ROBOKASSA_TEST_MODE:
        params["IsTest"] = 1
    return f"https://auth.robokassa.ru/Merchant/Index.aspx?{urlencode(params)}"


def verify_robokassa_result_signature(out_sum: str, inv_id: str, signature: str | None, shp_params: dict[str, str] | None = None) -> bool:
    if not signature:
        return False
    candidate_passwords = [
        password
        for password in (settings.ROBOKASSA_PASSWORD2, settings.ROBOKASSA_PASSWORD3)
        if password
    ]
    for password in candidate_passwords:
        signature_parts = [out_sum, inv_id, password]
        for key in sorted((shp_params or {}).keys()):
            signature_parts.append(f"{key}={shp_params[key]}")
        expected = _build_robokassa_signature(*signature_parts)
        if hmac.compare_digest(expected.lower(), str(signature).lower()):
            return True
    return False


def verify_robokassa_success_signature(out_sum: str, inv_id: str, signature: str | None, shp_params: dict[str, str] | None = None) -> bool:
    if not signature:
        return False
    signature_parts = [out_sum, inv_id, settings.ROBOKASSA_PASSWORD1]
    for key in sorted((shp_params or {}).keys()):
        signature_parts.append(f"{key}={shp_params[key]}")
    expected = _build_robokassa_signature(*signature_parts)
    return hmac.compare_digest(expected.lower(), str(signature).lower())


async def create_yookassa_payment(
    platform: str,
    user_id: str,
    plan: BillingPlan,
    external_payment_id: str,
) -> str:
    if not settings.YOOKASSA_SHOP_ID or not settings.YOOKASSA_SECRET_KEY:
        raise ValueError("YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY are required for YooKassa checkout")

    return_url = settings.YOOKASSA_RETURN_URL or f"{settings.APP_BASE_URL.rstrip('/')}/billing/return?external_payment_id={external_payment_id}"
    payload = {
        "amount": {
            "value": format_price_value(plan.price_rub),
            "currency": "RUB",
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": return_url,
        },
        "description": f"{plan.title} для {platform}:{user_id}",
        "metadata": {
            "platform": platform,
            "user_id": user_id,
            "plan_code": plan.code,
            "external_payment_id": external_payment_id,
        },
    }
    headers = {"Idempotence-Key": external_payment_id}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            "https://api.yookassa.ru/v3/payments",
            json=payload,
            headers=headers,
            auth=(settings.YOOKASSA_SHOP_ID, settings.YOOKASSA_SECRET_KEY),
        )
        response.raise_for_status()
        data = response.json()

    confirmation = data.get("confirmation") or {}
    confirmation_url = confirmation.get("confirmation_url")
    if not confirmation_url:
        raise ValueError("YooKassa did not return confirmation_url")
    return confirmation_url


def extract_webhook_payment_status(payload: dict) -> str:
    event = str(payload.get("event") or "").lower()
    object_status = str((payload.get("object") or {}).get("status") or payload.get("status") or "").lower()
    if event == "payment.succeeded" or object_status == "succeeded":
        return "paid"
    if event:
        return event
    return object_status


def extract_webhook_external_payment_id(payload: dict) -> str | None:
    obj = payload.get("object") or {}
    metadata = obj.get("metadata") or payload.get("metadata") or {}
    return (
        metadata.get("external_payment_id")
        or payload.get("external_payment_id")
        or payload.get("payment_id")
        or obj.get("id")
    )


async def verify_yookassa_webhook_payment(payload: dict, external_payment_id: str) -> bool:
    if not settings.YOOKASSA_SHOP_ID or not settings.YOOKASSA_SECRET_KEY:
        return False

    obj = payload.get("object") or {}
    yookassa_payment_id = str(obj.get("id") or payload.get("payment_id") or "").strip()
    if not yookassa_payment_id:
        return False

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"https://api.yookassa.ru/v3/payments/{yookassa_payment_id}",
            auth=(settings.YOOKASSA_SHOP_ID, settings.YOOKASSA_SECRET_KEY),
        )
        response.raise_for_status()
        data = response.json()

    metadata = data.get("metadata") or {}
    return (
        str(data.get("id") or "") == yookassa_payment_id
        and str(data.get("status") or "").lower() == "succeeded"
        and str(metadata.get("external_payment_id") or "") == external_payment_id
    )


async def ensure_pending_payment(
    platform: str,
    user_id: str,
    plan_code: str,
    provider: str,
    external_payment_id: str,
) -> BillingPlan:
    plan = get_plan(plan_code)
    if not plan:
        raise ValueError(f"Unknown billing plan: {plan_code}")

    await insert_pending_payment_if_missing(
        platform=platform,
        user_id=user_id,
        provider=provider,
        payment_type=plan.code,
        amount=plan.price_rub,
        requests_added=plan.request_balance,
        external_payment_id=external_payment_id,
    )
    return plan
async def handle_successful_payment_side_effects(
    platform: str,
    user_id: str,
    provider: str,
    plan: BillingPlan,
    payment_external_id: str,
):
    receipt = await create_receipt_for_payment(
        payment_external_id=payment_external_id,
        platform=platform,
        user_id=user_id,
        provider=provider,
        title=plan.title,
        amount=plan.price_rub,
    )
    notified = await notify_payment_success(
        platform=platform,
        user_id=user_id,
        title=plan.title,
        amount=plan.price_rub,
        requests_added=plan.request_balance,
        provider=provider,
        duration_days=plan.duration_days,
        receipt_url=(receipt or {}).get("receipt_url"),
    )
    if not notified:
        raise RuntimeError(f"Failed to deliver payment notification: {payment_external_id}")


async def apply_successful_payment(
    platform: str,
    user_id: str,
    plan_code: str,
    provider: str,
    external_payment_id: str | None = None,
):
    plan = get_plan(plan_code)
    if not plan:
        raise ValueError(f"Unknown billing plan: {plan_code}")

    payment_external_id = external_payment_id or build_external_payment_id(
        provider,
        f"{platform}:{user_id}:{plan_code}:{int(time.time())}",
    )

    if payment_external_id:
        existing_payment = await get_payment_by_external_id(payment_external_id)
        if not existing_payment:
            await record_payment(
                platform=platform,
                user_id=user_id,
                provider=provider,
                payment_type=plan.code,
                amount=plan.price_rub,
                requests_added=plan.request_balance,
                status="pending",
                external_payment_id=payment_external_id,
            )

        entitlements_state = await apply_payment_entitlements_if_needed(
            payment_external_id,
            pro_duration_days=plan.duration_days or PRO_PLAN_DAYS,
            referral_bonus_requests=settings.REFERRAL_BONUS_REQUESTS,
        )
        if entitlements_state == "missing":
            raise RuntimeError(f"Payment not found for entitlement application: {payment_external_id}")
        side_effects_state, _ = await claim_payment_side_effects(payment_external_id)
        if side_effects_state == "claimed":
            try:
                await handle_successful_payment_side_effects(platform, user_id, provider, plan, payment_external_id)
            except Exception as error:
                await complete_payment_side_effects(payment_external_id, success=False, error_text=str(error))
                raise
            await complete_payment_side_effects(payment_external_id, success=True)
        return plan

    await record_payment(
        platform=platform,
        user_id=user_id,
        provider=provider,
        payment_type=plan.code,
        amount=plan.price_rub,
        requests_added=plan.request_balance,
        status="pending",
        external_payment_id=payment_external_id,
    )
    entitlements_state = await apply_payment_entitlements_if_needed(
        payment_external_id,
        pro_duration_days=plan.duration_days or PRO_PLAN_DAYS,
        referral_bonus_requests=settings.REFERRAL_BONUS_REQUESTS,
    )
    if entitlements_state != "applied":
        raise RuntimeError(f"Failed to apply payment entitlements: {payment_external_id}")
    side_effects_state, _ = await claim_payment_side_effects(payment_external_id)
    if side_effects_state != "claimed":
        raise RuntimeError(f"Failed to claim payment side effects: {payment_external_id}")
    try:
        await handle_successful_payment_side_effects(platform, user_id, provider, plan, payment_external_id)
    except Exception as error:
        await complete_payment_side_effects(payment_external_id, success=False, error_text=str(error))
        raise
    await complete_payment_side_effects(payment_external_id, success=True)
    return plan


async def retry_payment_side_effects_once(limit: int = 50):
    payments = await list_payments_for_side_effect_retry(limit=limit)
    results = []
    for payment in payments:
        try:
            await apply_successful_payment(
                platform=payment["platform"],
                user_id=payment["user_id"],
                plan_code=payment["payment_type"],
                provider=payment["provider"],
                external_payment_id=payment["external_payment_id"],
            )
            results.append({
                "external_payment_id": payment["external_payment_id"],
                "status": "retried",
            })
        except Exception as error:
            log_api_error(f"Payment side effects retry error for {payment['external_payment_id']}: {error}")
            results.append({
                "external_payment_id": payment["external_payment_id"],
                "status": "failed",
                "error": str(error),
            })
    return results


async def payment_side_effects_retry_worker():
    while True:
        try:
            await retry_payment_side_effects_once()
            await asyncio.sleep(settings.PAYMENT_SIDE_EFFECT_RETRY_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_api_error(f"Payment side effects worker error: {error}")
            await asyncio.sleep(settings.PAYMENT_SIDE_EFFECT_RETRY_INTERVAL_SECONDS)
