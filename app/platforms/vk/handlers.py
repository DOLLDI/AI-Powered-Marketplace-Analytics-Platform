import re
import json
from html import unescape

from app.shared.types import VKCallback
from app.platforms.vk.vk_utils import edit_message, send_message, upload_message_photo
from app.services.billing import create_checkout_url, get_plan
from app.services.db import (
    add_history,
    add_user,
    consume_request_limit,
    get_recent_payments,
    get_referral_stats,
    get_request_access,
    get_user_profile,
    has_active_pro_access,
    increment_requests,
    set_referred_by,
)
from app.services.config import settings
from app.services.error_logger import log_api_error
from app.services.legal import (
    OFFER_URL,
    PAYMENT_ACCEPTANCE_NOTICE,
    build_plain_offer_text,
)
from app.services.logger import logger
from app.services.prompts import get_vk_system_prompt
from app.services.rate_limit import vk_rate_limiter
from app.services.wb import (
    WBError,
    WBNotFound,
    WBTemporaryUnavailable,
    analyze_wb_product,
    extract_wb_article,
    format_product_preview,
)
from app.services.price_compare import (
    PriceCompareError,
    PriceCompareProRequired,
    compare_wb_product_prices,
    enrich_price_comparison_with_risk,
    format_price_comparison_plain,
    format_price_comparison_teaser_plain,
)
import httpx


def _normalize_command(text: str) -> str:
    return (text or "").strip().lower()


def _extract_referral_code(text: str) -> str | None:
    stripped = (text or "").strip()
    lower = stripped.lower()
    prefixes = ("реф ", "ref ", "/ref ", "/start ")
    for prefix in prefixes:
        if lower.startswith(prefix):
            value = stripped[len(prefix):].strip()
            return value or None
    return None


def _build_start_message() -> str:
    return (
        "SmartCheck уже здесь.\n\n"
        "Пришлите ссылку на товар Wildberries или артикул. Я проверю карточку, отзывы и покажу, где есть риск накрутки или неприятный сюрприз.\n\n"
        "Бесплатно: 1 проверка в день.\n"
        "Кабинет, покупки и PRO: команда «профиль» или кнопка ниже.\n\n"
        "Публичная оферта и отказ от ответственности: команда «оферта» или кнопка ниже.\n\n"
        "Команды: «помощь», «профиль», «тарифы», «оферта» или используйте меню ниже.\n\n"
        "Есть приглашение? Отправьте: реф КОД."
    )


def build_start_keyboard() -> str:
    return json.dumps(
        {
            "inline": False,
            "one_time": False,
            "buttons": [
                [
                    {
                        "action": {
                            "type": "text",
                            "label": "👤 Кабинет",
                            "payload": json.dumps({"cmd": "profile"}, ensure_ascii=False),
                        },
                        "color": "primary",
                    },
                    {
                        "action": {
                            "type": "text",
                            "label": "💳 Тарифы",
                            "payload": json.dumps({"cmd": "tariffs"}, ensure_ascii=False),
                        },
                        "color": "secondary",
                    },
                ],
                [
                    {
                        "action": {
                            "type": "text",
                            "label": "ℹ️ Помощь",
                            "payload": json.dumps({"cmd": "help"}, ensure_ascii=False),
                        },
                        "color": "secondary",
                    },
                    {
                        "action": {
                            "type": "text",
                            "label": "📄 Оферта",
                            "payload": json.dumps({"cmd": "offer"}, ensure_ascii=False),
                        },
                        "color": "secondary",
                    },
                ],
            ],
        },
        ensure_ascii=False,
    )


def format_profile_text(profile: dict, referral_stats: dict, recent_payments: list[dict], user_id: str) -> str:
    tariff = (profile.get("tariff") or "free").upper()
    balance_requests = profile.get("balance_requests") or 0
    free_left = max(0, 1 - (profile.get("free_requests_used_today") or 0))
    pro_expires_at = profile.get("pro_expires_at") or "-"
    registered_at = profile.get("registered_at") or "-"
    payment_lines = []
    for payment in recent_payments[:3]:
        payment_lines.append(
            f"• {payment['payment_type']} · {payment['amount']} ₽ · {payment['status']} · {payment['created_at']}"
        )
    payments_text = "\n".join(payment_lines) if payment_lines else "• Оплат пока нет"
    referral_code = referral_stats.get("referral_code") or f"vk_{user_id}"
    one_time_url = create_checkout_url("vk", user_id, "one_time")
    pro_url = create_checkout_url("vk", user_id, "pro")
    return (
        "👤 Кабинет SmartCheck\n\n"
        f"Тариф: {tariff}\n"
        f"Платных проверок: {balance_requests}\n"
        f"Бесплатно сегодня: {free_left}\n"
        f"PRO активен до: {pro_expires_at}\n"
        f"Дата регистрации: {registered_at}\n\n"
        f"🤝 Рефералка\n"
        f"Код: {referral_code}\n"
        f"Приглашено: {referral_stats.get('invited_total', 0)}\n"
        f"Бонусов начислено: {referral_stats.get('rewarded_total', 0)}\n"
        f"Чтобы привязать код, отправьте: реф {referral_code}\n\n"
        f"💳 Последние оплаты\n{payments_text}\n\n"
        f"Перед оплатой: {PAYMENT_ACCEPTANCE_NOTICE}\n"
        f"Публичная оферта: {OFFER_URL}\n\n"
        f"🧾 Пакет 5 проверок: {one_time_url}\n"
        f"⚡ PRO на 30 дней: {pro_url}\n\n"
        f"PRO даёт 30 проверок и сравнение цен с Ozon и Яндекс Маркетом на 30 дней. "
        f"Бонус за первого оплатившего приглашённого: {settings.REFERRAL_BONUS_REQUESTS} проверки."
    )


def build_price_compare_keyboard(article: str) -> str:
    return json.dumps(
        {
            "inline": True,
            "buttons": [
                [
                    {
                        "action": {
                            "type": "text",
                            "label": "⚖️ Сравнить цену и риски",
                            "payload": json.dumps(
                                {"cmd": "price_compare", "article": str(article)},
                                ensure_ascii=False,
                            ),
                        },
                        "color": "primary",
                    }
                ]
            ],
        },
        ensure_ascii=False,
    )


def build_price_compare_locked_keyboard(user_id: str, article: str) -> str:
    pro_plan = get_plan("pro")
    pro_price = pro_plan.price_rub if pro_plan else 349
    return json.dumps(
        {
            "inline": True,
            "buttons": [
                [
                    {
                        "action": {
                            "type": "text",
                            "label": "⚖️ Посмотреть сравнение",
                            "payload": json.dumps(
                                {"cmd": "price_compare", "article": str(article)},
                                ensure_ascii=False,
                            ),
                        },
                        "color": "primary",
                    }
                ],
                [
                    {
                        "action": {
                            "type": "open_link",
                            "link": create_checkout_url("vk", user_id, "pro"),
                            "label": f"⚡ PRO — {pro_price} ₽",
                        }
                    }
                ],
                [
                    {
                        "action": {
                            "type": "open_link",
                            "link": OFFER_URL,
                            "label": "📄 Условия оплаты",
                        }
                    }
                ],
            ],
        },
        ensure_ascii=False,
    )


def format_price_compare_available_cta_plain() -> str:
    return (
        "🔎 Можно сравнить с Ozon и Яндекс Маркетом\n\n"
        "Проверю не просто цену, а совпадение товара, рейтинг, объем отзывов и риск покупки. "
        "Если дешевле, но подозрительнее, SmartCheck так и скажет."
    )


def format_price_compare_locked_cta_plain(include_payment_link: bool = False, user_id: str | None = None) -> str:
    pro_plan = get_plan("pro")
    pro_price = pro_plan.price_rub if pro_plan else 349
    text = (
        "⚖️ Можно сравнить цену и риски\n\n"
        "Для этого товара SmartCheck может проверить Ozon и Яндекс Маркет: совпадение по бренду, модели, цвету, "
        "комплектации, цену, рейтинг и риск покупки.\n\n"
        f"Сравнение входит в SmartCheck PRO: 30 дней доступа и 30 анализов за {pro_price} ₽."
    )
    if include_payment_link and user_id:
        text += (
            "\n\n"
            f"Оплатить PRO: {create_checkout_url('vk', user_id, 'pro')}\n"
            f"Условия оплаты и возврата: {OFFER_URL}"
        )
    return text


async def send_message_with_keyboard_fallback(
    peer_id: int,
    text: str,
    keyboard: str | None = None,
    fallback_text: str | None = None,
    attachment: str | None = None,
):
    try:
        return await send_message(peer_id, text, keyboard=keyboard, attachment=attachment)
    except Exception as error:
        if not keyboard:
            raise
        log_api_error(f"VK keyboard message fallback: peer_id={peer_id}, error={error}")
        return await send_message(peer_id, fallback_text or text, attachment=attachment)


def format_price_compare_pro_offer_plain() -> str:
    pro_plan = get_plan("pro")
    pro_price = pro_plan.price_rub if pro_plan else 349
    return (
        "🔒 Сравнение цен доступно в SmartCheck PRO\n\n"
        "PRO проверит предложения на Ozon и Яндекс Маркете и покажет только надежные совпадения по бренду, "
        "модели, цвету и комплектации. Затем сопоставит цену, рейтинг, отзывы и риск покупки.\n\n"
        "В подписке:\n"
        "• 30 анализов товаров;\n"
        "• сравнение цен и рисков;\n"
        "• доступ на 30 дней.\n\n"
        f"Стоимость: {pro_price} ₽."
    )


def build_price_compare_pro_keyboard(user_id: str) -> str:
    pro_plan = get_plan("pro")
    pro_price = pro_plan.price_rub if pro_plan else 349
    return json.dumps(
        {
            "inline": True,
            "buttons": [
                [
                    {
                        "action": {
                            "type": "open_link",
                            "link": create_checkout_url("vk", user_id, "pro"),
                            "label": f"⚡ Открыть PRO — {pro_price} ₽",
                        }
                    }
                ],
                [
                    {
                        "action": {
                            "type": "open_link",
                            "link": OFFER_URL,
                            "label": "📄 Условия оплаты и возврата",
                        }
                    }
                ],
            ],
        },
        ensure_ascii=False,
    )


def build_profile_keyboard(user_id: str, profile: dict) -> str:
    pro_button_text = "⚡ Продлить PRO" if (profile.get("tariff") or "").lower() == "pro" else "⚡ Купить PRO"
    return json.dumps(
        {
            "inline": True,
            "buttons": [
                [
                    {
                        "action": {
                            "type": "open_link",
                            "link": create_checkout_url("vk", user_id, "one_time"),
                            "label": "🧾 Пакет 5 проверок",
                        }
                    }
                ],
                [
                    {
                        "action": {
                            "type": "open_link",
                            "link": create_checkout_url("vk", user_id, "pro"),
                            "label": pro_button_text,
                        }
                    }
                ],
                [
                    {
                        "action": {
                            "type": "open_link",
                            "link": OFFER_URL,
                            "label": "📄 Публичная Оферта",
                        }
                    }
                ],
                [
                    {
                        "action": {
                            "type": "text",
                            "label": "💳 Тарифы",
                            "payload": json.dumps({"cmd": "tariffs"}, ensure_ascii=False),
                        }
                    }
                ],
                [
                    {
                        "action": {
                            "type": "text",
                            "label": "🔄 Обновить",
                            "payload": json.dumps({"cmd": "profile"}, ensure_ascii=False),
                        }
                    }
                ],
            ],
        },
        ensure_ascii=False,
    )


def build_tariff_keyboard(user_id: str) -> str:
    one_time_plan = get_plan("one_time")
    pro_plan = get_plan("pro")
    one_time_price = one_time_plan.price_rub if one_time_plan else 99
    pro_price = pro_plan.price_rub if pro_plan else 349
    return json.dumps(
        {
            "inline": True,
            "buttons": [
                [
                    {
                        "action": {
                            "type": "open_link",
                            "link": create_checkout_url("vk", user_id, "one_time"),
                            "label": f"Пакет 5 проверок - {one_time_price} ₽",
                        }
                    }
                ],
                [
                    {
                        "action": {
                            "type": "open_link",
                            "link": create_checkout_url("vk", user_id, "pro"),
                            "label": f"PRO на 30 дней - {pro_price} ₽",
                        }
                    }
                ],
                [
                    {
                        "action": {
                            "type": "open_link",
                            "link": OFFER_URL,
                            "label": "Условия оплаты и возврата",
                        }
                    }
                ],
            ],
        },
        ensure_ascii=False,
    )


def format_tariff_text() -> str:
    one_time_plan = get_plan("one_time")
    pro_plan = get_plan("pro")
    one_time_price = one_time_plan.price_rub if one_time_plan else 99
    pro_price = pro_plan.price_rub if pro_plan else 349
    return (
        "Тарифы SmartCheck\n\n"
        "1. Что покупают: пакет проверок или подписку на ИИ-анализатор отзывов SmartCheck.\n\n"
        f"2. Пакет 5 проверок: разовый доступ к 5 анализам отзывов WB без сравнения цен. Цена: {one_time_price} рублей.\n\n"
        f"3. PRO на 30 дней: 30 анализов WB и полное сравнение цен, рейтингов и рисков с Ozon и Яндекс Маркетом. Цена: {pro_price} рублей.\n\n"
        f"Условия оплаты и возврата (Публичная оферта): {OFFER_URL}"
    )


def format_limit_exhausted_text(user_id: str) -> str:
    return (
        "Лимит проверок исчерпан.\n\n"
        f"Перед оплатой: {PAYMENT_ACCEPTANCE_NOTICE}\n"
        f"Публичная оферта: {OFFER_URL}\n\n"
        "🧾 Пакет 5 проверок:\n"
        f"{create_checkout_url('vk', user_id, 'one_time')}\n\n"
        "⚡ PRO на 30 дней:\n"
        f"{create_checkout_url('vk', user_id, 'pro')}"
    )


async def handle_profile_command(peer_id: str):
    profile = await get_user_profile("vk", peer_id)
    referral_stats = await get_referral_stats("vk", peer_id)
    recent_payments = await get_recent_payments("vk", peer_id, 5)
    if not profile:
        return "Профиль пока не найден. Попробуйте ещё раз через несколько секунд.", None
    return (
        format_profile_text(profile, referral_stats, recent_payments, peer_id),
        build_profile_keyboard(peer_id, profile),
    )


async def handle_referral_command(peer_id: str, text: str):
    referral_code = _extract_referral_code(text)
    if not referral_code:
        return "Не удалось прочитать реферальный код. Используйте формат: реф КОД"
    attached = await set_referred_by("vk", peer_id, referral_code)
    if attached:
        return (
            "Реферальный код сохранён. Когда вы впервые оплатите пакет или PRO, "
            "пригласивший пользователь получит бонус."
        )
    return "Не удалось привязать код. Возможно, он неверный, уже привязан или принадлежит вам."


def _strip_html(text: str) -> str:
    value = text or ""
    value = re.sub(
        r'<a\s+[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        lambda match: f"{match.group(2)}: {match.group(1)}",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    value = re.sub(r"<br\s*/?>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"</(p|div|li)\s*>", "\n", value, flags=re.IGNORECASE)
    value = re.sub(r"</?(b|strong|i|em|code|pre|u|s)>", "", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", "", value)
    value = unescape(value)
    value = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1: \2", value)
    value = re.sub(r"`([^`]+)`", r"\1", value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"\1", value)
    value = re.sub(r"__([^_]+)__", r"\1", value)
    value = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", value)
    value = re.sub(r"(?<!_)_([^_\n]+)_(?!_)", r"\1", value)
    value = re.sub(r"^\s{0,3}#{1,6}\s*", "", value, flags=re.MULTILINE)
    value = re.sub(r"[ \t]+\n", "\n", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return value.strip()


def format_wb_preview_text(analysis) -> str:
    preview = _strip_html(
        format_product_preview(
            analysis.product,
            analysis.total_reviews_loaded,
            len(analysis.selected_reviews),
        )
    )
    return preview

async def call_proxy(prompt: str) -> str:
    url = settings.PROXYAPI_URL.rstrip("/") + "/chat/completions"
    system_prompt = get_vk_system_prompt()
    payload = {
        "model": "gpt-3.5-turbo",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt}
        ]
    }
    headers = {"Authorization": f"Bearer {settings.PROXYAPI_KEY}"} if settings.PROXYAPI_KEY else {}
    import logging
    logging.warning(
        "VK call_proxy: url=%s, has_api_key=%s, system_prompt_len=%s, user_prompt_len=%s",
        url,
        bool(settings.PROXYAPI_KEY),
        len(system_prompt),
        len(prompt or ""),
    )
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            j = r.json()
            if "choices" in j and j["choices"]:
                return j["choices"][0]["message"]["content"]
            fallback_reply = j.get('reply') or j.get('response') or j.get('text')
            if fallback_reply:
                return fallback_reply
            log_api_error(f"VK ProxyAPI unexpected payload: {j}")
            return None
    except Exception as e:
        log_api_error(f"VK ProxyAPI error: {e}")
        logging.warning(f"VK call_proxy error: {e}")
        return None


async def handle_price_compare_action(peer_id: str, article: str):
    if not await has_active_pro_access("vk", peer_id):
        await send_message_with_keyboard_fallback(
            int(peer_id),
            format_price_compare_pro_offer_plain(),
            keyboard=build_price_compare_pro_keyboard(peer_id),
            fallback_text=(
                f"{format_price_compare_pro_offer_plain()}\n\n"
                f"Оплатить PRO: {create_checkout_url('vk', peer_id, 'pro')}\n"
                f"Условия оплаты и возврата: {OFFER_URL}"
            ),
        )
        return
    status = await send_message(
        int(peer_id),
        "⚖️ Проверяю альтернативы\nСверяю цену, рейтинг, отзывы и надежность совпадения...",
    )
    status_id = status.get("response") if isinstance(status, dict) else None
    if isinstance(status_id, dict):
        status_id = status_id.get("message_id")
    try:
        analysis = await analyze_wb_product(article)
        comparison = await compare_wb_product_prices(
            analysis.product,
            user_input=article,
            platform="vk",
            user_id=peer_id,
        )
        wb_risk = analysis.review_analysis.risk_score if analysis.review_analysis else 25
        comparison = await enrich_price_comparison_with_risk(comparison, wb_risk)
        if status_id:
            try:
                await edit_message(int(peer_id), int(status_id), "✅ Сравнение готово")
            except Exception:
                pass
        await send_message(int(peer_id), format_price_comparison_plain(comparison))
    except PriceCompareProRequired:
        if status_id:
            try:
                await edit_message(int(peer_id), int(status_id), "🔒 Для сравнения цен нужен SmartCheck PRO")
            except Exception:
                pass
        await send_message(
            int(peer_id),
            format_price_compare_pro_offer_plain(),
            keyboard=build_price_compare_pro_keyboard(peer_id),
        )
    except PriceCompareError as error:
        error_text = f"⚖️ Сравнение пока недоступно\n{error}"
        if status_id:
            await edit_message(int(peer_id), int(status_id), error_text)
        else:
            await send_message(int(peer_id), error_text)
    except Exception as error:
        log_api_error(f"VK on-demand price compare error: {error}")
        error_text = (
            "⚖️ Не удалось завершить сравнение\n"
            "Одна из площадок временно не отдала надежные данные. Попробуйте еще раз позже."
        )
        if status_id:
            try:
                await edit_message(int(peer_id), int(status_id), error_text)
            except Exception:
                await send_message(int(peer_id), error_text)
        else:
            await send_message(int(peer_id), error_text)

async def handle_event(data: dict):
    cb = VKCallback(**data)
    if cb.type == 'message_new':
        import logging
        from app.services.db import is_vk_message_processed, mark_vk_message_processed
        obj = cb.object
        msg = obj.get('message') or obj
        peer_id = msg.get('peer_id') or msg.get('from_id')
        text = msg.get('text')
        message_id = msg.get('id')
        conversation_message_id = msg.get("conversation_message_id")
        processed_key = None
        if peer_id is not None and conversation_message_id is not None:
            processed_key = f"{peer_id}:{conversation_message_id}"
        elif message_id not in (None, 0):
            processed_key = message_id
        else:
            processed_key = cb.event_id
        logging.warning(
            "VK EVENT: peer_id=%s, message_id=%s, conversation_message_id=%s, event_id=%s, text_len=%s",
            peer_id,
            message_id,
            conversation_message_id,
            cb.event_id,
            len(text or ""),
        )
        if not text:
            return
        if processed_key is not None and await is_vk_message_processed(processed_key):
            logging.warning(f"VK DUPLICATE: processed_key={processed_key} already processed, skip")
            return
        peer_id = str(peer_id)
        await add_user("vk", peer_id, None)

        payload = {}
        raw_payload = msg.get("payload")
        if isinstance(raw_payload, str):
            try:
                payload = json.loads(raw_payload)
            except json.JSONDecodeError:
                payload = {}
        elif isinstance(raw_payload, dict):
            payload = raw_payload
        payload_command = str(payload.get("cmd") or "").strip().lower()
        if payload_command == "price_compare":
            article = str(payload.get("article") or "").strip()
            if processed_key is not None:
                await mark_vk_message_processed(processed_key)
            if not article.isdigit():
                await send_message(int(peer_id), "Не удалось распознать товар для сравнения.")
                return
            await handle_price_compare_action(peer_id, article)
            return

        normalized = _normalize_command(text)
        if payload_command in {"start", "help", "menu"} or normalized in {
            "/start", "start", "начать", "/help", "help", "помощь", "команды", "меню", "ℹ️ помощь"
        }:
            if processed_key is not None:
                await mark_vk_message_processed(processed_key)
            await send_message(int(peer_id), _build_start_message(), keyboard=build_start_keyboard())
            return

        if payload_command == "profile" or normalized in {"/profile", "profile", "профиль", "кабинет", "👤 кабинет", "обновить кабинет", "🔄 обновить"}:
            profile_text, profile_keyboard = await handle_profile_command(peer_id)
            if processed_key is not None:
                await mark_vk_message_processed(processed_key)
            await send_message(int(peer_id), profile_text, keyboard=profile_keyboard)
            return

        if payload_command == "offer" or normalized in {"/offer", "offer", "оферта", "публичная оферта", "📄 публичная оферта", "📄 оферта"}:
            if processed_key is not None:
                await mark_vk_message_processed(processed_key)
            await send_message(int(peer_id), build_plain_offer_text(), keyboard=build_start_keyboard())
            return

        if payload_command == "tariffs" or normalized in {"/tariffs", "/pay", "/payment", "/subscribe", "tariffs", "pay", "payment", "subscribe", "тарифы", "оплата", "подписка", "💳 тарифы"}:
            if processed_key is not None:
                await mark_vk_message_processed(processed_key)
            await send_message(int(peer_id), format_tariff_text(), keyboard=build_tariff_keyboard(peer_id))
            return

        if _extract_referral_code(text):
            referral_text = await handle_referral_command(peer_id, text)
            if processed_key is not None:
                await mark_vk_message_processed(processed_key)
            if (text or "").strip().lower().startswith("/start "):
                await send_message(
                    int(peer_id),
                    f"{referral_text}\n\n{_build_start_message()}",
                    keyboard=build_start_keyboard(),
                )
            else:
                await send_message(int(peer_id), referral_text)
            return

        allowed_by_rate, retry_after = await vk_rate_limiter.allow(
            key=f"vk:{peer_id}",
            max_requests=settings.ANTIFLOOD_MAX_REQUESTS,
            window_seconds=settings.ANTIFLOOD_WINDOW_SECONDS,
        )
        if not allowed_by_rate:
            if processed_key is not None:
                await mark_vk_message_processed(processed_key)
            await send_message(int(peer_id), f"⏳ Слишком много запросов подряд. Подождите примерно {retry_after} сек. и попробуйте снова.")
            return

        allowed, reason, profile = await get_request_access("vk", peer_id)
        if not allowed:
            if processed_key is not None:
                await mark_vk_message_processed(processed_key)
            if reason.startswith("Лимит") and profile:
                await send_message(
                    int(peer_id),
                    format_limit_exhausted_text(peer_id),
                    keyboard=build_profile_keyboard(peer_id, profile),
                )
            else:
                await send_message(int(peer_id), reason)
            return
        if processed_key is not None:
            await mark_vk_message_processed(processed_key)
        import asyncio
        thinking_msgs = ["Проверяю данные.", "Проверяю данные..", "Проверяю данные..."]
        msg_send = await send_message(int(peer_id), thinking_msgs[0])
        logging.warning(f"VK SEND_MESSAGE: resp={msg_send}")
        sent_id = None
        if isinstance(msg_send, dict):
            sent_id = msg_send.get('response')
            if isinstance(sent_id, dict):
                sent_id = sent_id.get('message_id')
            elif isinstance(sent_id, list) and sent_id:
                sent_id = sent_id[0]
        for anim in thinking_msgs[1:]:
            await asyncio.sleep(0.7)
            if sent_id:
                try:
                    await edit_message(int(peer_id), sent_id, anim)
                except Exception as e:
                    logging.warning(f"VK EDIT_MESSAGE error: {e}")

        wb_article = extract_wb_article(text)
        try:
            reply = None
            wb_preview = None
            wb_attachment = None
            price_compare_reply = None
            price_compare_keyboard = None
            price_compare_fallback = None
            show_price_compare_pro_offer = False
            if wb_article:
                analysis = await analyze_wb_product(text)
                if analysis.review_analysis:
                    logger.info(
                        "VK WB analysis: user_id=%s, article=%s, loaded=%s, filtered=%s, risk_score=%s",
                        peer_id,
                        analysis.product.article,
                        analysis.total_reviews_loaded,
                        analysis.review_analysis.stats.total_after_filter,
                        analysis.review_analysis.risk_score,
                    )
                wb_preview = format_wb_preview_text(analysis)
                if analysis.product.image_url:
                    try:
                        wb_attachment = await upload_message_photo(analysis.product.image_url)
                    except Exception:
                        wb_preview = f"{wb_preview}\nФото: {analysis.product.image_url}"
                if await has_active_pro_access("vk", peer_id):
                    price_compare_reply = format_price_compare_available_cta_plain()
                    price_compare_keyboard = build_price_compare_keyboard(analysis.product.article)
                else:
                    show_price_compare_pro_offer = True
                    price_compare_reply = format_price_compare_locked_cta_plain()
                    price_compare_keyboard = build_price_compare_locked_keyboard(peer_id, analysis.product.article)
                    price_compare_fallback = format_price_compare_locked_cta_plain(
                        include_payment_link=True,
                        user_id=peer_id,
                    )
                reply = _strip_html(analysis.summary_html)
            else:
                reply = await asyncio.wait_for(call_proxy(text), timeout=30)
        except asyncio.TimeoutError:
            reply = None
            wb_preview = None
        except WBTemporaryUnavailable as error:
            error_text = str(error)
            if sent_id:
                try:
                    await edit_message(int(peer_id), sent_id, error_text)
                except Exception:
                    await send_message(int(peer_id), error_text)
            else:
                await send_message(int(peer_id), error_text)
            return
        except WBNotFound as error:
            error_text = str(error)
            if sent_id:
                try:
                    await edit_message(int(peer_id), sent_id, error_text)
                except Exception:
                    await send_message(int(peer_id), error_text)
            else:
                await send_message(int(peer_id), error_text)
            return
        except WBError as error:
            error_text = str(error)
            if sent_id:
                try:
                    await edit_message(int(peer_id), sent_id, error_text)
                except Exception:
                    await send_message(int(peer_id), error_text)
            else:
                await send_message(int(peer_id), error_text)
            return

        has_wb_result = bool(wb_article) and reply is not None
        if (reply and reply.strip()) or has_wb_result:
            consumed, consume_reason = await consume_request_limit("vk", peer_id)
            if not consumed:
                if sent_id:
                    try:
                        await edit_message(int(peer_id), sent_id, consume_reason)
                    except Exception:
                        await send_message(int(peer_id), consume_reason)
                else:
                    await send_message(int(peer_id), consume_reason)
                return
            await increment_requests("vk", peer_id)

        if reply or has_wb_result:
            history_reply = reply or ""
            if wb_article:
                history_reply = "\n\n".join(part for part in (reply, price_compare_reply) if part)
                await add_history("vk", peer_id, text, history_reply)
                if sent_id:
                    try:
                        await edit_message(int(peer_id), sent_id, "Анализ товара готов.")
                    except Exception as e:
                        logging.warning(f"VK FINAL EDIT_MESSAGE error: {e}")
                if wb_preview:
                    await send_message(int(peer_id), wb_preview, attachment=wb_attachment)
                if reply and reply.strip():
                    await send_message(int(peer_id), reply, keyboard=build_start_keyboard())
                if price_compare_reply:
                    await send_message_with_keyboard_fallback(
                        int(peer_id),
                        price_compare_reply,
                        keyboard=price_compare_keyboard,
                        fallback_text=price_compare_fallback or price_compare_reply,
                    )
                elif show_price_compare_pro_offer:
                    await send_message_with_keyboard_fallback(
                        int(peer_id),
                        format_price_compare_pro_offer_plain(),
                        keyboard=build_price_compare_pro_keyboard(peer_id),
                        fallback_text=(
                            f"{format_price_compare_pro_offer_plain()}\n\n"
                            f"Оплатить PRO: {create_checkout_url('vk', peer_id, 'pro')}\n"
                            f"Условия оплаты и возврата: {OFFER_URL}"
                        ),
                    )
            else:
                await add_history("vk", peer_id, text, history_reply)
                if sent_id:
                    try:
                        await edit_message(int(peer_id), sent_id, reply)
                    except Exception as e:
                        logging.warning(f"VK FINAL EDIT_MESSAGE error: {e}")
                        await send_message(int(peer_id), reply)
                else:
                    await send_message(int(peer_id), reply, keyboard=build_start_keyboard())
        else:
            error_text = "Извините, сервис временно недоступен. Попробуйте позже."
            if sent_id:
                try:
                    await edit_message(int(peer_id), sent_id, error_text)
                except Exception as e:
                    logging.warning(f"VK ERROR EDIT_MESSAGE: {e}")
                    await send_message(int(peer_id), error_text)
            else:
                await send_message(int(peer_id), error_text)
