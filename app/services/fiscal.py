import json
import time
import asyncio
from urllib.parse import urlencode

import httpx

from app.services.config import settings
from app.services.db import get_receipt_by_payment_id, list_receipts_for_retry, upsert_receipt
from app.services.error_logger import log_api_error


def _build_local_receipt_url(payment_external_id: str) -> str:
    query = urlencode({"payment_id": payment_external_id})
    return f"{settings.APP_BASE_URL.rstrip('/')}/billing/receipt?{query}"


def _should_return_existing_receipt(existing: dict | None, force_retry: bool) -> bool:
    if not existing or force_retry:
        return False
    return (existing.get("status") or "") in {"sent", "succeeded", "success", "sandbox", "pending", "created"}


async def create_receipt_for_payment(
    payment_external_id: str,
    platform: str,
    user_id: str,
    provider: str,
    title: str,
    amount: float,
    force_retry: bool = False,
):
    existing = await get_receipt_by_payment_id(payment_external_id)
    if _should_return_existing_receipt(existing, force_retry):
        return existing

    attempts = int((existing or {}).get("fiscal_attempts") or 0)

    payload = {
        "payment_external_id": payment_external_id,
        "platform": platform,
        "user_id": user_id,
        "provider": provider,
        "title": title,
        "amount": amount,
        "seller_inn": settings.MYTAX_SELLER_INN,
    }

    if settings.MYTAX_API_URL and settings.MYTAX_API_TOKEN:
        attempts += 1
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    settings.MYTAX_API_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {settings.MYTAX_API_TOKEN}"},
                )
                response.raise_for_status()
                data = response.json() if response.content else {}
            receipt_url = data.get("receipt_url") or _build_local_receipt_url(payment_external_id)
            await upsert_receipt(
                payment_external_id=payment_external_id,
                platform=platform,
                user_id=user_id,
                provider="mytax",
                amount=amount,
                title=title,
                status=str(data.get("status") or "sent"),
                receipt_url=receipt_url,
                external_receipt_id=str(data.get("receipt_id") or data.get("id") or payment_external_id),
                payload=json.dumps(data, ensure_ascii=False),
                sent=True,
                fiscal_attempts=attempts,
                last_error=None,
            )
            return await get_receipt_by_payment_id(payment_external_id)
        except Exception as error:
            error_text = str(error)
            log_api_error(f"Fiscalization error for {payment_external_id}: {error_text}")
            await upsert_receipt(
                payment_external_id=payment_external_id,
                platform=platform,
                user_id=user_id,
                provider="mytax",
                amount=amount,
                title=title,
                status="error",
                receipt_url=_build_local_receipt_url(payment_external_id),
                external_receipt_id=(existing or {}).get("external_receipt_id"),
                payload=json.dumps({"request": payload, "error": error_text}, ensure_ascii=False),
                sent=False,
                fiscal_attempts=attempts,
                last_error=error_text,
            )
            return await get_receipt_by_payment_id(payment_external_id)

    local_payload = {
        "receipt_id": f"local-{payment_external_id}-{int(time.time())}",
        "status": "sandbox",
        "provider": provider,
        "title": title,
        "amount": amount,
    }
    await upsert_receipt(
        payment_external_id=payment_external_id,
        platform=platform,
        user_id=user_id,
        provider="local",
        amount=amount,
        title=title,
        status="sandbox",
        receipt_url=_build_local_receipt_url(payment_external_id),
        external_receipt_id=local_payload["receipt_id"],
        payload=json.dumps(local_payload, ensure_ascii=False),
        sent=False,
        fiscal_attempts=attempts,
        last_error=None,
    )
    return await get_receipt_by_payment_id(payment_external_id)


async def retry_failed_receipts_once(limit: int = 50):
    if not (settings.MYTAX_API_URL and settings.MYTAX_API_TOKEN):
        return []

    receipts = await list_receipts_for_retry(limit=limit, max_attempts=settings.FISCAL_MAX_ATTEMPTS)
    results = []
    for receipt in receipts:
        refreshed = await create_receipt_for_payment(
            payment_external_id=receipt["payment_external_id"],
            platform=receipt["platform"],
            user_id=receipt["user_id"],
            provider=receipt.get("provider") or "mytax",
            title=receipt["title"],
            amount=receipt["amount"],
            force_retry=True,
        )
        results.append(refreshed)
    return results


async def fiscal_retry_worker():
    if not (settings.MYTAX_API_URL and settings.MYTAX_API_TOKEN):
        return

    while True:
        try:
            await asyncio.sleep(max(1, settings.FISCAL_RETRY_INTERVAL_SECONDS))
            await retry_failed_receipts_once()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log_api_error(f"Fiscal retry worker error: {error}")