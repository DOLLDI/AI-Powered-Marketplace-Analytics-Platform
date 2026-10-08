import json
from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.services.billing import (
    apply_successful_payment,
    build_external_payment_id,
    create_robokassa_payment_url,
    create_yookassa_payment,
    create_checkout_token,
    ensure_pending_payment,
    extract_webhook_external_payment_id,
    extract_webhook_payment_status,
    get_plan,
    parse_checkout_token,
    verify_robokassa_result_signature,
    verify_robokassa_success_signature,
    verify_webhook_signature,
    verify_yookassa_webhook_payment,
)
from app.services.config import settings
from app.services.db import get_payment_by_external_id, get_receipt_by_payment_id
from app.services.error_logger import (
    PUBLIC_CHECKOUT_TOKEN_ERROR_DETAIL,
    PUBLIC_PAYMENT_PROVIDER_ERROR_DETAIL,
    PUBLIC_PAYMENT_REQUEST_ERROR_DETAIL,
    PUBLIC_PAYMENT_TARGET_NOT_FOUND_DETAIL,
    log_api_error,
)
from app.services.legal import OFFER_URL, PAYMENT_ACCEPTANCE_NOTICE, DISCLAIMER_TEXT
from app.services.logger import logger

router = APIRouter()


def h(value) -> str:
    return escape("" if value is None else str(value), quote=True)


def _html_page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        f"""
        <html>
        <head>
            <meta charset='utf-8'>
            <title>{h(title)}</title>
            <style>
                body {{ font-family: Arial, sans-serif; background: #f6f3ec; color: #222; margin: 40px; }}
                .card {{ max-width: 640px; background: white; border-radius: 16px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,.08); }}
                .button {{ display: inline-block; padding: 12px 18px; background: #1f6f5f; color: white; text-decoration: none; border-radius: 10px; }}
                .muted {{ color: #666; margin-top: 12px; }}
            </style>
        </head>
        <body>
            <div class='card'>
                {body}
            </div>
        </body>
        </html>
        """
    )


async def _read_robokassa_payload(request: Request) -> dict[str, str]:
    data: dict[str, str] = {key: str(value) for key, value in request.query_params.items()}
    if request.method.upper() == "POST":
        form = await request.form()
        data.update({key: str(value) for key, value in form.items()})
    return data


def _extract_shp_params(data: dict[str, str]) -> dict[str, str]:
    return {key: str(value) for key, value in data.items() if key.lower().startswith("shp_")}


def _robokassa_value(data: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if value:
            return str(value)
    lower_map = {key.lower(): str(value) for key, value in data.items()}
    for key in keys:
        value = lower_map.get(key.lower())
        if value:
            return str(value)
    return ""


def _shp_value(shp_params: dict[str, str], key: str) -> str:
    for param_key, value in shp_params.items():
        if param_key.lower() == key.lower():
            return str(value)
    return ""


async def _apply_robokassa_payment_by_payload(data: dict[str, str]) -> tuple[str, str]:
    shp_params = _extract_shp_params(data)
    inv_id = _robokassa_value(data, "InvId", "InvID", "inv_id")
    external_payment_id = _shp_value(shp_params, "Shp_external_payment_id") or inv_id
    payment = await get_payment_by_external_id(external_payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    await apply_successful_payment(
        platform=payment["platform"],
        user_id=payment["user_id"],
        plan_code=payment["payment_type"],
        provider=payment["provider"],
        external_payment_id=external_payment_id,
    )
    return inv_id, external_payment_id


@router.get("/checkout", response_class=HTMLResponse)
async def checkout(token: str = Query(...), accepted: bool = Query(False)):
    try:
        payload = parse_checkout_token(token)
    except ValueError:
        raise HTTPException(status_code=400, detail=PUBLIC_CHECKOUT_TOKEN_ERROR_DETAIL)

    external_payment_id = payload.get("external_payment_id") or build_external_payment_id(settings.PAYMENT_PROVIDER, token)
    try:
        plan = await ensure_pending_payment(
            platform=payload["platform"],
            user_id=payload["user_id"],
            plan_code=payload["plan_code"],
            provider=settings.PAYMENT_PROVIDER,
            external_payment_id=external_payment_id,
        )
    except ValueError:
        raise HTTPException(status_code=404, detail=PUBLIC_PAYMENT_TARGET_NOT_FOUND_DETAIL)

    confirm_token = create_checkout_token(
        platform=payload["platform"],
        user_id=payload["user_id"],
        plan_code=payload["plan_code"],
        ttl_seconds=1800,
        external_payment_id=external_payment_id,
    )

    if not accepted:
        continue_url = f"{settings.APP_BASE_URL.rstrip('/')}/billing/checkout?{urlencode({'token': token, 'accepted': '1'})}"
        disclaimer_html = h(DISCLAIMER_TEXT).replace("\n", "<br>")
        body = f"""
            <h1>Перед оплатой</h1>
            <p>Тариф: <b>{h(plan.title)}</b></p>
            <p>Стоимость: <b>{h(plan.price_rub)} ₽</b></p>
            <p><b>{h(PAYMENT_ACCEPTANCE_NOTICE)}</b></p>
            <p><a href="{h(OFFER_URL)}" target="_blank" rel="noopener">Публичная оферта и Политика конфиденциальности</a></p>
            <div class='muted'>{disclaimer_html}</div>
            <p style='margin-top:18px;'><a class='button' href='{h(continue_url)}'>Перейти к оплате</a></p>
        """
        return _html_page("Перед оплатой", body)

    if settings.PAYMENT_PROVIDER.lower() == "yookassa":
        try:
            confirmation_url = await create_yookassa_payment(
                platform=payload["platform"],
                user_id=payload["user_id"],
                plan=plan,
                external_payment_id=external_payment_id,
            )
        except ValueError as error:
            log_api_error(f"YooKassa checkout validation error: {error}")
            raise HTTPException(status_code=400, detail=PUBLIC_PAYMENT_REQUEST_ERROR_DETAIL)
        except Exception as error:
            log_api_error(f"YooKassa checkout error: {error}")
            raise HTTPException(status_code=502, detail=PUBLIC_PAYMENT_PROVIDER_ERROR_DETAIL)
        return RedirectResponse(confirmation_url, status_code=303)

    if settings.PAYMENT_PROVIDER.lower() == "robokassa":
        try:
            confirmation_url = create_robokassa_payment_url(
                platform=payload["platform"],
                user_id=payload["user_id"],
                plan=plan,
                external_payment_id=external_payment_id,
            )
        except ValueError as error:
            log_api_error(f"Robokassa checkout validation error: {error}")
            raise HTTPException(status_code=400, detail=PUBLIC_PAYMENT_REQUEST_ERROR_DETAIL)
        return RedirectResponse(confirmation_url, status_code=303)

    confirm_url = f"{settings.APP_BASE_URL.rstrip('/')}/billing/confirm?token={confirm_token}"

    plan_effect_html = f"<p>Что будет начислено: <b>{h(plan.request_balance)}</b> проверок</p>"
    if plan.duration_days:
        plan_effect_html += f"<p>Срок действия: <b>{h(plan.duration_days)}</b> дней</p>"

    body = f"""
        <h1>Тестовая оплата {h(plan.title)}</h1>
        <p>Провайдер: <b>{h(settings.PAYMENT_PROVIDER)}</b></p>
        <p>Стоимость: <b>{h(plan.price_rub)} ₽</b></p>
        {plan_effect_html}
        <p>Пользователь: <code>{h(payload['platform'])}:{h(payload['user_id'])}</code></p>
        <p>External payment id: <code>{h(external_payment_id)}</code></p>
        <a class='button' href='{h(confirm_url)}'>Подтвердить тестовую оплату</a>
        <div class='muted'>Это sandbox-режим. Pending-платёж уже создан, а для реального провайдера сюда подключается webhook с тем же external payment id.</div>
    """
    return _html_page("Sandbox checkout", body)


@router.get("/confirm", response_class=HTMLResponse)
async def confirm_checkout(token: str = Query(...)):
    try:
        payload = parse_checkout_token(token)
    except ValueError:
        raise HTTPException(status_code=400, detail=PUBLIC_CHECKOUT_TOKEN_ERROR_DETAIL)

    plan = await apply_successful_payment(
        platform=payload["platform"],
        user_id=payload["user_id"],
        plan_code=payload["plan_code"],
        provider=settings.PAYMENT_PROVIDER,
        external_payment_id=payload.get("external_payment_id") or token,
    )

    plan_effect_html = f"<p>Начислено проверок: <b>{h(plan.request_balance)}</b></p>"
    if plan.duration_days:
        plan_effect_html += f"<p>Срок действия: <b>{h(plan.duration_days)}</b> дней</p>"

    body = f"""
        <h1>Оплата подтверждена</h1>
        <p>Тариф: <b>{h(plan.title)}</b></p>
        {plan_effect_html}
        <p>Вернитесь в бот и откройте команду <b>/profile</b>, чтобы увидеть обновлённый баланс.</p>
        <div class='muted'>Для реальной интеграции сюда подключается ЮKassa/Robokassa sandbox webhook.</div>
    """
    return _html_page("Payment confirmed", body)


@router.get("/return", response_class=HTMLResponse)
async def billing_return(external_payment_id: str = Query("")):
    body = f"""
        <h1>Оплата ожидает подтверждения</h1>
        <p>Если платёж прошёл успешно, лимиты будут начислены автоматически после webhook от провайдера.</p>
        <p>External payment id: <code>{h(external_payment_id or '-')}</code></p>
        <div class='muted'>Вернитесь в бот и откройте /profile или напишите 'профиль' во VK через несколько секунд.</div>
    """
    return _html_page("Payment return", body)


@router.get("/receipt", response_class=HTMLResponse)
async def billing_receipt(payment_id: str = Query(...)):
    receipt = await get_receipt_by_payment_id(payment_id)
    if not receipt:
        raise HTTPException(status_code=404, detail="Receipt not found")

    body = f"""
        <h1>Чек по оплате</h1>
        <p>Платёж: <code>{h(receipt['payment_external_id'])}</code></p>
        <p>Платформа: <b>{h(receipt['platform'])}</b></p>
        <p>Пользователь: <b>{h(receipt['user_id'])}</b></p>
        <p>Провайдер чека: <b>{h(receipt['provider'])}</b></p>
        <p>Товар: <b>{h(receipt['title'])}</b></p>
        <p>Сумма: <b>{h(receipt['amount'])} ₽</b></p>
        <p>Статус: <b>{h(receipt['status'])}</b></p>
        <p>Создан: <b>{h(receipt['created_at'])}</b></p>
        <div class='muted'>Если используется sandbox или локальный режим, этот чек является тестовым представлением.</div>
    """
    return _html_page("Receipt", body)


@router.api_route("/robokassa/success", methods=["GET", "POST"], response_class=HTMLResponse)
async def robokassa_success(request: Request):
    data = await _read_robokassa_payload(request)
    out_sum = _robokassa_value(data, "OutSum", "out_summ")
    inv_id = _robokassa_value(data, "InvId", "InvID", "inv_id")
    signature = _robokassa_value(data, "SignatureValue", "crc")
    shp_params = _extract_shp_params(data)
    apply_error = ""

    if out_sum and inv_id and signature:
        if verify_robokassa_success_signature(out_sum, inv_id, signature, shp_params):
            try:
                _, external_payment_id = await _apply_robokassa_payment_by_payload(data)
                logger.info("Robokassa SuccessURL applied payment: inv_id=%s external_payment_id=%s", inv_id, external_payment_id)
                body = f"""
                    <h1>Оплата подтверждена</h1>
                    <p>Подписка или пакет проверок начислены. Вернитесь в бот и обновите профиль.</p>
                    <p>InvId: <code>{h(inv_id or '-')}</code></p>
                """
                return _html_page("Robokassa success", body)
            except Exception as error:
                apply_error = str(error)
                log_api_error(f"Robokassa SuccessURL apply error: inv_id={inv_id}, error={error}")
        else:
            apply_error = "Invalid Robokassa success signature"
            log_api_error(f"Robokassa SuccessURL invalid signature: inv_id={inv_id}, shp_keys={sorted(shp_params.keys())}")

    body = f"""
        <h1>Оплата отправлена на подтверждение</h1>
        <p>Robokassa приняла платёж и должна прислать серверный callback для начисления лимитов.</p>
        <p>InvId: <code>{h(inv_id or '-')}</code></p>
        {f"<p>Ошибка обработки: <code>{h(apply_error)}</code></p>" if apply_error else ""}
        <div class='muted'>Вернитесь в бот и обновите кабинет через несколько секунд.</div>
    """
    return _html_page("Robokassa success", body)


@router.get("/robokassa/fail", response_class=HTMLResponse)
async def robokassa_fail(InvId: str = Query("")):
    body = f"""
        <h1>Оплата не завершена</h1>
        <p>Платёж в Robokassa был отменён или не завершился.</p>
        <p>External payment id: <code>{h(InvId or '-')}</code></p>
        <div class='muted'>Можно вернуться в бот и попробовать оплату позже.</div>
    """
    return _html_page("Robokassa fail", body)


@router.api_route("/robokassa/result", methods=["GET", "POST", "HEAD"])
async def robokassa_result(request: Request):
    if request.method.upper() == "HEAD":
        return HTMLResponse("")

    data = await _read_robokassa_payload(request)
    out_sum = _robokassa_value(data, "OutSum", "out_summ")
    inv_id = _robokassa_value(data, "InvId", "InvID", "inv_id")
    signature = _robokassa_value(data, "SignatureValue", "crc")
    shp_params = _extract_shp_params(data)
    logger.info(
        "Robokassa ResultURL received: method=%s inv_id=%s out_sum=%s shp_keys=%s has_signature=%s",
        request.method,
        inv_id,
        out_sum,
        sorted(shp_params.keys()),
        bool(signature),
    )
    if not out_sum or not inv_id:
        log_api_error(f"Robokassa ResultURL missing required fields: method={request.method}, keys={sorted(data.keys())}")
        raise HTTPException(status_code=400, detail="OutSum and InvId are required")
    if not verify_robokassa_result_signature(out_sum, inv_id, signature, shp_params):
        log_api_error(f"Robokassa ResultURL invalid signature: inv_id={inv_id}, shp_keys={sorted(shp_params.keys())}")
        raise HTTPException(status_code=403, detail="Invalid Robokassa signature")

    inv_id, external_payment_id = await _apply_robokassa_payment_by_payload(data)
    logger.info("Robokassa ResultURL applied payment: inv_id=%s external_payment_id=%s", inv_id, external_payment_id)
    return HTMLResponse(f"OK{inv_id}")


@router.post("/webhook")
async def billing_webhook(request: Request, x_billing_signature: str = Header(default="")):
    raw_body = await request.body()

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except json.JSONDecodeError as error:
        raise HTTPException(status_code=400, detail="Invalid JSON payload") from error

    provider = settings.PAYMENT_PROVIDER.lower()
    if provider == "robokassa":
        raise HTTPException(status_code=400, detail="Use /billing/robokassa/result for Robokassa callbacks")

    if provider != "yookassa" and not verify_webhook_signature(raw_body, x_billing_signature):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    external_payment_id = extract_webhook_external_payment_id(payload)
    if not external_payment_id:
        raise HTTPException(status_code=400, detail="external_payment_id is required")

    if provider == "yookassa":
        try:
            verified = await verify_yookassa_webhook_payment(payload, external_payment_id)
        except Exception as error:
            log_api_error(f"YooKassa webhook verification error: {error}")
            raise HTTPException(status_code=502, detail=PUBLIC_PAYMENT_PROVIDER_ERROR_DETAIL)
        if not verified:
            raise HTTPException(status_code=403, detail="Invalid YooKassa webhook")

    status = extract_webhook_payment_status(payload)
    payment = await get_payment_by_external_id(external_payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    if status not in {"paid", "succeeded", "success"}:
        return {"status": "ignored", "payment_status": payment.get("status"), "event_status": status or "unknown"}

    plan = await apply_successful_payment(
        platform=payment["platform"],
        user_id=payment["user_id"],
        plan_code=payment["payment_type"],
        provider=payment["provider"],
        external_payment_id=external_payment_id,
    )
    return {
        "status": "ok",
        "external_payment_id": external_payment_id,
        "plan": plan.code,
        "user": f"{payment['platform']}:{payment['user_id']}",
    }
