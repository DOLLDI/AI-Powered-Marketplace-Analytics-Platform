from pathlib import Path

from urllib.parse import urlencode, urlparse
from html import escape

from fastapi import APIRouter, Body, Form, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.services.billing import apply_successful_payment, get_plan
from app.services.db import (
    activate_pro_subscription,
    get_payment_by_external_id,
    get_price_compare_stats,
    get_recent_payments,
    get_receipts_by_payment_ids,
    get_referral_stats,
    get_stats,
    get_user,
    grant_user_requests,
    list_referred_users,
    list_recent_payments,
    list_users,
    set_user_blocked,
)
from app.services.fiscal import create_receipt_for_payment
from app.platforms.telegram.broadcast import broadcast as tg_broadcast
from app.platforms.vk.broadcast import broadcast_vk
from app.services.config import settings
from app.services.price_compare import get_price_compare_health

router = APIRouter()

ADMIN_TOKEN_COOKIE = "proxyapi_admin_token"
ADMIN_ID_COOKIE = "proxyapi_admin_id"


def h(value) -> str:
    return escape("" if value is None else str(value), quote=True)


def safe_href(value: str | None) -> str:
    raw = (value or "").strip()
    if raw.startswith(("http://", "https://", "/")):
        return h(raw)
    return "#"


def is_public_http_url(value: str | None) -> bool:
    parsed = urlparse((value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

def check_admin_access(token: str, admin_id: str):
    if token != settings.ADMIN_TOKEN:
        raise HTTPException(status_code=403, detail="Forbidden (token)")
    if admin_id not in settings.admin_ids:
        raise HTTPException(status_code=403, detail="Forbidden (admin_id)")


def _set_admin_cookies(response: RedirectResponse, token: str, admin_id: str):
    public_url = settings.PUBLIC_WEBHOOK_BASE_URL or settings.APP_BASE_URL
    secure = public_url.lower().startswith("https://")
    response.set_cookie(ADMIN_TOKEN_COOKIE, token, httponly=True, samesite="lax", secure=secure)
    response.set_cookie(ADMIN_ID_COOKIE, admin_id, httponly=True, samesite="lax", secure=secure)


def require_admin_session(request: Request) -> str:
    token = request.cookies.get(ADMIN_TOKEN_COOKIE, "")
    admin_id = request.cookies.get(ADMIN_ID_COOKIE, "")
    check_admin_access(token, admin_id)
    return admin_id


def _login_page(error_text: str = "") -> HTMLResponse:
    error_block = f"<div class='error'>{escape(error_text)}</div>" if error_text else ""
    return HTMLResponse(
        f"""
        <html>
        <head>
            <meta charset='utf-8'>
            <title>Admin Login</title>
            <style>
                body {{ font-family: Arial, sans-serif; background: #f7f7f5; color: #222; margin: 0; }}
                .wrap {{ min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 24px; }}
                .card {{ width: 100%; max-width: 420px; background: #fff; border: 1px solid #ddd; border-radius: 16px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,.08); }}
                label {{ display: block; margin-bottom: 6px; font-weight: 600; }}
                input {{ width: 100%; padding: 10px; margin-bottom: 14px; box-sizing: border-box; }}
                button {{ padding: 10px 14px; }}
                .error {{ margin-bottom: 14px; padding: 10px; background: #fdecea; color: #a1260d; border: 1px solid #f5c2b7; border-radius: 10px; }}
                .note {{ color: #666; font-size: 14px; margin-bottom: 16px; }}
            </style>
        </head>
        <body>
            <div class='wrap'>
                <form class='card' method='post' action='/admin/login'>
                    <h1 style='margin-top:0;'>Admin Login</h1>
                    <div class='note'>Введите admin id и секретный токен админ-панели.</div>
                    {error_block}
                    <label for='admin_id'>Admin ID</label>
                    <input id='admin_id' type='text' name='admin_id' required>
                    <label for='token'>Admin Token</label>
                    <input id='token' type='password' name='token' required>
                    <button type='submit'>Войти</button>
                </form>
            </div>
        </body>
        </html>
        """
    )


def read_error_log_tail(limit: int = 50) -> str:
    log_path = Path(settings.ERROR_LOG_PATH).expanduser().resolve()
    if not log_path.exists():
        return "Лог-файл пока не создан."
    lines = log_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    tail = lines[-limit:] if lines else ["Лог пока пуст."]
    return "\n".join(tail)


def build_panel_url(
    notice: str | None = None,
    search_platform: str = "",
    search_user_id: str = "",
    search_payment_id: str = "",
) -> str:
    params = {}
    if notice:
        params["notice"] = notice
    if search_platform:
        params["search_platform"] = search_platform
    if search_user_id:
        params["search_user_id"] = search_user_id
    if search_payment_id:
        params["search_payment_id"] = search_payment_id
    return "/admin/panel" if not params else f"/admin/panel?{urlencode(params)}"


def format_broadcast_notice(*results: dict) -> str:
    total_attempted = sum(int(result.get("attempted", 0)) for result in results)
    total_sent = sum(int(result.get("sent", 0)) for result in results)
    total_failed = sum(int(result.get("failed", 0)) for result in results)
    parts = [
        f"Всего попыток: {total_attempted}",
        f"успешно: {total_sent}",
        f"ошибок: {total_failed}",
    ]
    for result in results:
        platform = str(result.get("platform") or "?")
        parts.append(
            f"{platform}: {int(result.get('sent', 0))}/{int(result.get('attempted', 0))}"
        )
    return "Rich-рассылка отправлена. " + ", ".join(parts)


@router.get('/stats')
async def stats(x_admin_token: str = Header(...), x_admin_id: str = Header(...)):
    check_admin_access(x_admin_token, x_admin_id)
    rows = await get_stats()
    price_compare = await get_price_compare_stats()
    price_compare_health = get_price_compare_health()
    result = {}
    total_users = 0
    total_requests = 0
    for platform, users, requests in rows:
        result[platform] = {'users': users, 'requests': requests or 0}
        total_users += users
        total_requests += requests or 0
    result['total'] = {'users': total_users, 'requests': total_requests}
    result['price_compare'] = price_compare
    result['price_compare_health'] = price_compare_health
    return result


@router.post('/broadcast')
async def do_broadcast(text: str = Body(...), x_admin_token: str = Header(...), x_admin_id: str = Header(...)):
    check_admin_access(x_admin_token, x_admin_id)
    from app.platforms.telegram.aiogram_bot import bot
    tg_result = await tg_broadcast(text, bot)
    vk_result = await broadcast_vk(text)
    return {
        "status": "ok",
        "results": {
            "telegram": tg_result,
            "vk": vk_result,
        },
    }


@router.post('/broadcast/rich')
async def do_rich_broadcast(
    request: Request,
    text: str = Form(...),
    image_url: str = Form(""),
    button_text: str = Form(""),
    button_url: str = Form(""),
):
    require_admin_session(request)
    from app.platforms.telegram.aiogram_bot import bot

    clean_image_url = image_url.strip() or None
    clean_button_text = button_text.strip() or None
    clean_button_url = button_url.strip() or None
    if clean_image_url and not is_public_http_url(clean_image_url):
        raise HTTPException(status_code=400, detail="Image URL must start with http:// or https://")
    if (clean_button_text and not clean_button_url) or (clean_button_url and not clean_button_text):
        raise HTTPException(status_code=400, detail="Button text and URL must be provided together")
    if clean_button_url and not is_public_http_url(clean_button_url):
        raise HTTPException(status_code=400, detail="Button URL must start with http:// or https://")

    tg_result = await tg_broadcast(
        text=text,
        bot=bot,
        image_url=clean_image_url,
        button_text=clean_button_text,
        button_url=clean_button_url,
    )
    vk_result = await broadcast_vk(
        text=text,
        image_url=clean_image_url,
        button_text=clean_button_text,
        button_url=clean_button_url,
    )
    return RedirectResponse(
        build_panel_url(format_broadcast_notice(tg_result, vk_result)),
        status_code=303,
    )


@router.post('/users/block')
async def block_user(
    request: Request,
    platform: str = Form(...),
    user_id: str = Form(...),
):
    require_admin_session(request)
    user = await get_user(platform, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await set_user_blocked(platform, user_id, True)
    return RedirectResponse(build_panel_url(f"Пользователь {platform}:{user_id} заблокирован"), status_code=303)


@router.post('/users/unblock')
async def unblock_user(
    request: Request,
    platform: str = Form(...),
    user_id: str = Form(...),
):
    require_admin_session(request)
    user = await get_user(platform, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await set_user_blocked(platform, user_id, False)
    return RedirectResponse(build_panel_url(f"Пользователь {platform}:{user_id} разблокирован"), status_code=303)


@router.post('/users/grant-requests')
async def admin_grant_requests(
    request: Request,
    platform: str = Form(...),
    user_id: str = Form(...),
    amount: int = Form(...),
):
    require_admin_session(request)
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount must be positive")
    user = await get_user(platform, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await grant_user_requests(platform, user_id, amount)
    return RedirectResponse(build_panel_url(f"Начислено {amount} проверок для {platform}:{user_id}"), status_code=303)


@router.post('/users/grant-pro')
async def admin_grant_pro(
    request: Request,
    platform: str = Form(...),
    user_id: str = Form(...),
    days: int = Form(30),
    requests: int = Form(30),
):
    require_admin_session(request)
    if days <= 0 or requests < 0:
        raise HTTPException(status_code=400, detail="Days and requests must be positive")
    user = await get_user(platform, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await activate_pro_subscription(platform, user_id, days=days, requests=requests)
    return RedirectResponse(build_panel_url(f"Выдан PRO для {platform}:{user_id} на {days} дн."), status_code=303)


@router.post('/payments/confirm')
async def admin_confirm_payment(
    request: Request,
    external_payment_id: str = Form(...),
):
    require_admin_session(request)
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
    return RedirectResponse(build_panel_url(f"Платёж {external_payment_id} подтверждён"), status_code=303)


@router.post('/payments/retry-receipt')
async def admin_retry_receipt(
    request: Request,
    external_payment_id: str = Form(...),
):
    require_admin_session(request)
    payment = await get_payment_by_external_id(external_payment_id)
    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")
    plan = get_plan(payment["payment_type"])
    receipt = await create_receipt_for_payment(
        payment_external_id=external_payment_id,
        platform=payment["platform"],
        user_id=payment["user_id"],
        provider=payment["provider"],
        title=plan.title if plan else payment["payment_type"],
        amount=payment["amount"],
        force_retry=True,
    )
    status = (receipt or {}).get("status", "unknown")
    return RedirectResponse(
        build_panel_url(f"Повторная отправка чека для {external_payment_id}: {status}"),
        status_code=303,
    )


@router.post('/logout')
async def admin_logout():
    response = RedirectResponse('/admin/login', status_code=303)
    response.delete_cookie(ADMIN_TOKEN_COOKIE)
    response.delete_cookie(ADMIN_ID_COOKIE)
    return response


@router.get('/login', response_class=HTMLResponse)
async def admin_login_page(request: Request):
    try:
        require_admin_session(request)
    except HTTPException:
        return _login_page()
    return RedirectResponse('/admin/panel', status_code=303)


@router.post('/login')
async def admin_login_submit(token: str = Form(...), admin_id: str = Form(...)):
    try:
        check_admin_access(token, admin_id)
    except HTTPException:
        return _login_page("Неверный admin id или токен.")

    response = RedirectResponse('/admin/panel', status_code=303)
    _set_admin_cookies(response, token, admin_id)
    return response


@router.get('/panel', response_class=HTMLResponse)
async def admin_panel(
    request: Request,
    token: str = Query(""),
    admin_id: str = Query(""),
    notice: str = Query(""),
    search_platform: str = Query(""),
    search_user_id: str = Query(""),
    search_payment_id: str = Query(""),
):
    if token and admin_id:
        check_admin_access(token, admin_id)
        response = RedirectResponse(
            build_panel_url(
                notice=notice,
                search_platform=search_platform,
                search_user_id=search_user_id,
                search_payment_id=search_payment_id,
            ),
            status_code=303,
        )
        _set_admin_cookies(response, token, admin_id)
        return response

    try:
        admin_id = require_admin_session(request)
    except HTTPException:
        return RedirectResponse('/admin/login', status_code=303)

    stats_rows = await get_stats()
    price_compare_stats = await get_price_compare_stats()
    price_compare_health = get_price_compare_health()
    users = await list_users(100)
    payments = await list_recent_payments(100)
    log_tail = read_error_log_tail(50)
    selected_user = None
    selected_user_note = ""
    selected_referral_stats = None
    selected_user_payments = []
    selected_referred_users = []
    selected_payment = None
    receipts_by_payment_id = {}
    selected_payment_receipt = None

    selected_user_platform = search_platform
    if search_user_id:
        if search_platform:
            selected_user = await get_user(search_platform, search_user_id)
        else:
            matched_users = []
            for candidate_platform in ("telegram", "vk"):
                candidate_user = await get_user(candidate_platform, search_user_id)
                if candidate_user:
                    matched_users.append(candidate_user)
            if len(matched_users) == 1:
                selected_user = matched_users[0]
                selected_user_platform = selected_user["platform"]
            elif len(matched_users) > 1:
                selected_user_note = "Найдено несколько пользователей с таким user id. Уточните платформу в фильтре выше."
        if selected_user:
            selected_user_platform = selected_user["platform"]
            selected_referral_stats = await get_referral_stats(selected_user_platform, search_user_id)
            selected_user_payments = await get_recent_payments(selected_user_platform, search_user_id, 20)
            referral_code = selected_referral_stats.get("referral_code") if selected_referral_stats else None
            if referral_code:
                selected_referred_users = await list_referred_users(selected_user_platform, referral_code, 20)

    if search_payment_id:
        selected_payment = await get_payment_by_external_id(search_payment_id)
        if selected_payment and selected_payment.get("external_payment_id"):
            receipts_by_payment_id = await get_receipts_by_payment_ids([selected_payment["external_payment_id"]])
            selected_payment_receipt = receipts_by_payment_id.get(selected_payment["external_payment_id"])

    if not receipts_by_payment_id:
        payment_ids = [row.get("external_payment_id") for row in payments if row.get("external_payment_id")]
        receipts_by_payment_id = await get_receipts_by_payment_ids(payment_ids)

    stats_html = "".join(
        f"<tr><td>{h(platform)}</td><td>{h(users_count)}</td><td>{h(requests_count or 0)}</td></tr>"
        for platform, users_count, requests_count in stats_rows
    ) or "<tr><td colspan='3'>Нет данных</td></tr>"

    price_top_products_html = "".join(
        "<tr>"
        f"<td>{h(row.get('article'))}</td>"
        f"<td>{h(row.get('wb_title'))}</td>"
        f"<td>{h(row.get('requests'))}</td>"
        "</tr>"
        for row in price_compare_stats.get("top_products", [])
    ) or "<tr><td colspan='3'>Нет данных</td></tr>"

    price_compare_health_html = "".join(
        "<tr>"
        f"<td>{h(key)}</td>"
        f"<td>{h(value)}</td>"
        "</tr>"
        for key, value in price_compare_health.items()
    )

    filtered_users = users
    if search_platform:
        filtered_users = [row for row in filtered_users if row["platform"] == search_platform]
    if search_user_id:
        filtered_users = [row for row in filtered_users if search_user_id.lower() in str(row["user_id"]).lower()]

    users_html = "".join(
        "<tr>"
        f"<td>{h(row['platform'])}</td>"
        f"<td>{h(row['user_id'])}</td>"
        f"<td>{h(row['username'] or '-')}</td>"
        f"<td>{h(row['tariff'])}</td>"
        f"<td>{h(row['balance_requests'])}</td>"
        f"<td>{h(row['free_requests_used_today'])}</td>"
        f"<td>{h(row['requests'])}</td>"
        f"<td>{'Да' if row['blocked'] else 'Нет'}</td>"
        f"<td>{h(row['pro_expires_at'] or '-')}</td>"
        f"<td>{h(row['registered_at'] or '-')}</td>"
        f"<td>{h(row['last_request_at'] or '-')}</td>"
        "<td>"
        f"<form method='post' action='/admin/users/{'unblock' if row['blocked'] else 'block'}' style='display:inline-block;margin:0 6px 6px 0;'>"
        f"<input type='hidden' name='platform' value='{h(row['platform'])}'>"
        f"<input type='hidden' name='user_id' value='{h(row['user_id'])}'>"
        f"<button type='submit'>{'Разблокировать' if row['blocked'] else 'Заблокировать'}</button>"
        "</form>"
        f"<form method='post' action='/admin/users/grant-requests' style='display:inline-block;margin:0 6px 6px 0;'>"
        f"<input type='hidden' name='platform' value='{h(row['platform'])}'>"
        f"<input type='hidden' name='user_id' value='{h(row['user_id'])}'>"
        "<input type='number' name='amount' value='5' min='1' style='width:70px'>"
        "<button type='submit'>+Проверки</button>"
        "</form>"
        f"<form method='post' action='/admin/users/grant-pro' style='display:inline-block;'>"
        f"<input type='hidden' name='platform' value='{h(row['platform'])}'>"
        f"<input type='hidden' name='user_id' value='{h(row['user_id'])}'>"
        "<input type='number' name='days' value='30' min='1' style='width:60px'>"
        "<input type='number' name='requests' value='30' min='0' style='width:70px'>"
        "<button type='submit'>Выдать PRO</button>"
        "</form>"
        "</td>"
        "</tr>"
        for row in filtered_users
    ) or "<tr><td colspan='12'>Нет пользователей</td></tr>"

    payments_html = "".join(
        "<tr>"
        f"<td>{h(row['platform'])}</td>"
        f"<td>{h(row['user_id'])}</td>"
        f"<td>{h(row['provider'])}</td>"
        f"<td>{h(row['payment_type'])}</td>"
        f"<td>{h(row['amount'])}</td>"
        f"<td>{h(row['requests_added'])}</td>"
        f"<td>{h(row['status'])}</td>"
        f"<td>{h((receipts_by_payment_id.get(row['external_payment_id']) or {}).get('status', '-'))}</td>"
        f"<td>{h(row['external_payment_id'] or '-')}</td>"
        f"<td>{h(row['created_at'])}</td>"
        "</tr>"
        for row in payments
    ) or "<tr><td colspan='8'>Нет оплат</td></tr>"

    selected_payments_html = "".join(
        "<tr>"
        f"<td>{h(row['provider'])}</td>"
        f"<td>{h(row['payment_type'])}</td>"
        f"<td>{h(row['amount'])}</td>"
        f"<td>{h(row['requests_added'])}</td>"
        f"<td>{h(row['status'])}</td>"
        f"<td>{h(row['created_at'])}</td>"
        f"<td>{h(row['paid_at'] or '-')}</td>"
        "</tr>"
        for row in selected_user_payments
    ) or "<tr><td colspan='7'>Оплат нет</td></tr>"

    referred_users_html = "".join(
        "<tr>"
        f"<td>{h(row['user_id'])}</td>"
        f"<td>{h(row['username'] or '-')}</td>"
        f"<td>{h(row['tariff'])}</td>"
        f"<td>{h(row['balance_requests'])}</td>"
        f"<td>{'Да' if row['referral_reward_granted'] else 'Нет'}</td>"
        f"<td>{h(row['registered_at'] or '-')}</td>"
        "</tr>"
        for row in selected_referred_users
    ) or "<tr><td colspan='6'>Приглашённых пользователей нет</td></tr>"

    selected_user_html = ""
    if selected_user:
        selected_user_html = f"""
        <h2>Карточка пользователя</h2>
        <table>
            <tr><th>Поле</th><th>Значение</th></tr>
            <tr><td>Платформа</td><td>{h(selected_user['platform'])}</td></tr>
            <tr><td>User ID</td><td>{h(selected_user['user_id'])}</td></tr>
            <tr><td>Username</td><td>{h(selected_user['username'] or '-')}</td></tr>
            <tr><td>Тариф</td><td>{h(selected_user['tariff'])}</td></tr>
            <tr><td>Остаток проверок</td><td>{h(selected_user['balance_requests'])}</td></tr>
            <tr><td>Бесплатно сегодня использовано</td><td>{h(selected_user['free_requests_used_today'])}</td></tr>
            <tr><td>Всего запросов</td><td>{h(selected_user['requests'])}</td></tr>
            <tr><td>Blocked</td><td>{'Да' if selected_user['blocked'] else 'Нет'}</td></tr>
            <tr><td>PRO до</td><td>{h(selected_user['pro_expires_at'] or '-')}</td></tr>
            <tr><td>Реферальный код</td><td>{h(selected_user['referral_code'] or '-')}</td></tr>
            <tr><td>Пришёл по рефке</td><td>{h(selected_user['referred_by'] or '-')}</td></tr>
            <tr><td>Бонус за него начислен</td><td>{'Да' if selected_user['referral_reward_granted'] else 'Нет'}</td></tr>
            <tr><td>Регистрация</td><td>{h(selected_user['registered_at'] or '-')}</td></tr>
            <tr><td>Последний запрос</td><td>{h(selected_user['last_request_at'] or '-')}</td></tr>
        </table>

        <h2>Реферальная статистика</h2>
        <table>
            <tr><th>Код</th><th>Приглашено</th><th>Бонусов начислено</th></tr>
            <tr>
                <td>{h(selected_referral_stats.get('referral_code') if selected_referral_stats else '-')}</td>
                <td>{h(selected_referral_stats.get('invited_total') if selected_referral_stats else 0)}</td>
                <td>{h(selected_referral_stats.get('rewarded_total') if selected_referral_stats else 0)}</td>
            </tr>
        </table>

        <h2>Последние оплаты пользователя</h2>
        <table>
            <tr><th>Провайдер</th><th>Тип</th><th>Сумма</th><th>Запросов</th><th>Статус</th><th>Создано</th><th>Оплачено</th></tr>
            {selected_payments_html}
        </table>

        <h2>Приглашённые пользователи</h2>
        <table>
            <tr><th>User ID</th><th>Username</th><th>Тариф</th><th>Баланс</th><th>Бонус начислен</th><th>Регистрация</th></tr>
            {referred_users_html}
        </table>
        """
    elif search_user_id:
        if selected_user_note:
            selected_user_html = f"<h2>Карточка пользователя</h2><div class='note'>{h(selected_user_note)}</div>"
        else:
            selected_user_html = "<h2>Карточка пользователя</h2><div class='note'>Пользователь не найден.</div>"

    selected_payment_html = ""
    if selected_payment:
        receipt_link = (selected_payment_receipt or {}).get("receipt_url")
        receipt_retry_form = ""
        if settings.MYTAX_API_URL and settings.MYTAX_API_TOKEN:
            receipt_retry_form = (
                f"<form method='post' action='/admin/payments/retry-receipt' style='background:#fff;padding:16px;margin-bottom:24px;border:1px solid #ddd;'>"
                f"<input type='hidden' name='external_payment_id' value='{h(selected_payment['external_payment_id'] or '')}'>"
                "<button type='submit'>Повторить отправку чека</button>"
                "</form>"
            )
        selected_payment_html = f"""
        <h2>Карточка платежа</h2>
        <table>
            <tr><th>Поле</th><th>Значение</th></tr>
            <tr><td>External ID</td><td>{h(selected_payment['external_payment_id'] or '-')}</td></tr>
            <tr><td>Платформа</td><td>{h(selected_payment['platform'])}</td></tr>
            <tr><td>User ID</td><td>{h(selected_payment['user_id'])}</td></tr>
            <tr><td>Провайдер</td><td>{h(selected_payment['provider'])}</td></tr>
            <tr><td>Тип</td><td>{h(selected_payment['payment_type'])}</td></tr>
            <tr><td>Сумма</td><td>{h(selected_payment['amount'])}</td></tr>
            <tr><td>Запросов</td><td>{h(selected_payment['requests_added'])}</td></tr>
            <tr><td>Статус</td><td>{h(selected_payment['status'])}</td></tr>
            <tr><td>Создан</td><td>{h(selected_payment['created_at'] or '-')}</td></tr>
            <tr><td>Оплачен</td><td>{h(selected_payment['paid_at'] or '-')}</td></tr>
            <tr><td>Статус чека</td><td>{h((selected_payment_receipt or {}).get('status', '-'))}</td></tr>
            <tr><td>Попыток фискализации</td><td>{h((selected_payment_receipt or {}).get('fiscal_attempts', 0))}</td></tr>
            <tr><td>Последняя ошибка</td><td>{h((selected_payment_receipt or {}).get('last_error', '-') or '-')}</td></tr>
            <tr><td>ID чека</td><td>{h((selected_payment_receipt or {}).get('external_receipt_id', '-'))}</td></tr>
            <tr><td>Ссылка на чек</td><td>{f"<a href='{safe_href(receipt_link)}' target='_blank' rel='noopener noreferrer'>Открыть чек</a>" if receipt_link else '-'}</td></tr>
        </table>
        {'' if selected_payment['status'] == 'paid' else f"<form method='post' action='/admin/payments/confirm' style='background:#fff;padding:16px;margin-bottom:24px;border:1px solid #ddd;'><input type='hidden' name='external_payment_id' value='{h(selected_payment['external_payment_id'] or '')}'><button type='submit'>Подтвердить платёж вручную</button></form>"}
        {receipt_retry_form}
        """
    elif search_payment_id:
        selected_payment_html = "<h2>Карточка платежа</h2><div class='note'>Платёж не найден.</div>"

    return HTMLResponse(f"""
    <html>
    <head>
        <meta charset='utf-8'>
        <title>ProxyApiBots Admin</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 24px; background: #f7f7f5; color: #222; }}
            h1, h2 {{ margin-bottom: 12px; }}
            table {{ border-collapse: collapse; width: 100%; margin-bottom: 24px; background: #fff; }}
            th, td {{ border: 1px solid #ddd; padding: 8px; text-align: left; font-size: 14px; }}
            th {{ background: #f0eadc; }}
            button {{ padding: 6px 10px; }}
            input {{ padding: 5px; }}
            pre {{ background: #111; color: #e8e8e8; padding: 16px; overflow: auto; white-space: pre-wrap; }}
            .note {{ margin-bottom: 16px; color: #555; }}
            .notice {{ margin-bottom: 16px; padding: 12px; background: #e6f4ea; border: 1px solid #c6e7cf; }}
        </style>
    </head>
    <body>
        <h1>Admin Panel</h1>
        <div class='note'>Открыто для admin_id={h(admin_id)}</div>
        <form method='post' action='/admin/logout' style='margin-bottom:16px;'>
            <button type='submit'>Выйти</button>
        </form>
        {f"<div class='notice'>{h(notice)}</div>" if notice else ''}

        <h2>Статистика</h2>
        <table>
            <tr><th>Платформа</th><th>Пользователи</th><th>Запросы</th></tr>
            {stats_html}
        </table>

        <h2>Сравнение цен</h2>
        <table>
            <tr>
                <th>Запросов</th><th>Успешно</th><th>Частично</th><th>Ошибок</th>
                <th>Ozon с результатом</th><th>Яндекс с результатом</th><th>Среднее время</th>
            </tr>
            <tr>
                <td>{h(price_compare_stats.get('total', 0))}</td>
                <td>{h(price_compare_stats.get('success', 0))}</td>
                <td>{h(price_compare_stats.get('partial', 0))}</td>
                <td>{h(price_compare_stats.get('errors', 0))}</td>
                <td>{h(price_compare_stats.get('ozon_success_rate', 0))}%</td>
                <td>{h(price_compare_stats.get('yandex_success_rate', 0))}%</td>
                <td>{h(price_compare_stats.get('avg_elapsed', 0))} сек.</td>
            </tr>
        </table>
        <h3>Часто проверяемые товары</h3>
        <table>
            <tr><th>Артикул</th><th>Название</th><th>Проверок</th></tr>
            {price_top_products_html}
        </table>
        <h3>Диагностика источников сравнения</h3>
        <table>
            <tr><th>Параметр</th><th>Значение</th></tr>
            {price_compare_health_html}
        </table>

        <h2>Поиск пользователя</h2>
        <form method='get' action='/admin/panel' style='background:#fff;padding:16px;margin-bottom:24px;border:1px solid #ddd;'>
            <input type='hidden' name='search_payment_id' value='{h(search_payment_id)}'>
            <div style='display:flex;gap:12px;flex-wrap:wrap;'>
                <div>
                    <label>Платформа</label><br>
                    <select name='search_platform' style='padding:6px;'>
                        <option value=''>Все</option>
                        <option value='telegram' {'selected' if search_platform == 'telegram' else ''}>telegram</option>
                        <option value='vk' {'selected' if search_platform == 'vk' else ''}>vk</option>
                    </select>
                </div>
                <div>
                    <label>User ID</label><br>
                    <input type='text' name='search_user_id' value='{h(search_user_id)}' placeholder='Например 123456789'>
                </div>
                <div style='align-self:end;'>
                    <button type='submit'>Найти</button>
                </div>
            </div>
        </form>

        {selected_user_html}

        <h2>Поиск платежа</h2>
        <form method='get' action='/admin/panel' style='background:#fff;padding:16px;margin-bottom:24px;border:1px solid #ddd;'>
            <input type='hidden' name='search_platform' value='{h(search_platform)}'>
            <input type='hidden' name='search_user_id' value='{h(search_user_id)}'>
            <div style='display:flex;gap:12px;flex-wrap:wrap;'>
                <div style='flex:1 1 420px;'>
                    <label>External payment id</label><br>
                    <input type='text' name='search_payment_id' value='{h(search_payment_id)}' placeholder='Например sandbox_order_123' style='width:100%;'>
                </div>
                <div style='align-self:end;'>
                    <button type='submit'>Найти платёж</button>
                </div>
            </div>
        </form>

        {selected_payment_html}

        <h2>Рассылка</h2>
        <form method='post' action='/admin/broadcast/rich' style='background:#fff;padding:16px;margin-bottom:24px;border:1px solid #ddd;'>
            <div style='margin-bottom:10px;'>
                <label>Текст сообщения</label><br>
                <textarea name='text' rows='5' style='width:100%;padding:8px;' required></textarea>
            </div>
            <div style='margin-bottom:10px;'>
                <label>URL картинки (необязательно)</label><br>
                <input type='url' name='image_url' placeholder='https://example.com/image.jpg' style='width:100%;'>
            </div>
            <div style='display:flex;gap:12px;flex-wrap:wrap;margin-bottom:10px;'>
                <div style='flex:1 1 260px;'>
                    <label>Текст кнопки (необязательно)</label><br>
                    <input type='text' name='button_text' placeholder='Открыть товар' style='width:100%;'>
                </div>
                <div style='flex:2 1 320px;'>
                    <label>URL кнопки (необязательно)</label><br>
                    <input type='url' name='button_url' placeholder='https://example.com' style='width:100%;'>
                </div>
            </div>
            <button type='submit'>Отправить rich-рассылку</button>
        </form>

        <h2>Пользователи</h2>
        <table>
            <tr>
                <th>Платформа</th><th>User ID</th><th>Username</th><th>Тариф</th>
                <th>Остаток проверок</th><th>Free today</th><th>Всего запросов</th>
                <th>Blocked</th><th>PRO до</th><th>Регистрация</th><th>Последний запрос</th><th>Управление</th>
            </tr>
            {users_html}
        </table>

        <h2>Оплаты</h2>
        <table>
            <tr>
                <th>Платформа</th><th>User ID</th><th>Провайдер</th><th>Тип</th>
                <th>Сумма</th><th>Запросов</th><th>Статус</th><th>Чек</th><th>External ID</th><th>Создано</th>
            </tr>
            {payments_html}
        </table>

        <h2>Хвост лога ошибок</h2>
        <pre>{h(log_tail)}</pre>
    </body>
    </html>
    """)
