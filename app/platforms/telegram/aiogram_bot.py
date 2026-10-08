import asyncio
import re
from urllib.parse import urlparse
from html import escape
from html.parser import HTMLParser
from aiogram import Bot, Dispatcher, types, F
from aiogram.client.default import DefaultBotProperties
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BufferedInputFile
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from app.services.config import settings
from app.services.billing import create_checkout_url, get_plan
from app.services.legal import (
    OFFER_URL,
    PAYMENT_ACCEPTANCE_NOTICE,
    build_html_offer_text,
)
from app.services.db import (
    add_user,
    add_history,
    consume_request_limit,
    get_recent_payments,
    get_recent_history,
    get_request_access,
    get_referral_stats,
    get_user_profile,
    has_active_pro_access,
    increment_requests,
    set_referred_by,
)
from app.services.prompts import get_system_prompt
from app.services.rate_limit import telegram_rate_limiter
from app.services.logger import logger
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
    format_price_comparison_html,
    format_price_comparison_teaser_html,
)
from app.services.error_logger import PUBLIC_TECHNICAL_ERROR_MESSAGE, log_api_error
import openai
import os
import httpx
from io import BytesIO

BOT_TOKEN = settings.TELEGRAM_BOT_TOKEN
bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()


def h(value) -> str:
    return escape("" if value is None else str(value), quote=False)


def wb_analysis_timeout_seconds() -> int:
    parse_timeout = max(5, int(settings.WB_MAX_PARSE_SECONDS or 45))
    ai_timeout = max(10, int(settings.PROXYAPI_TIMEOUT_SECONDS or 60))
    return max(75, parse_timeout + ai_timeout + 20)


class TelegramHTMLSanitizer(HTMLParser):
    allowed_tags = {"b", "strong", "i", "em", "u", "s", "code", "pre", "a"}

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        normalized_tag = tag.lower()
        if normalized_tag == "a":
            href = ""
            for name, value in attrs:
                if name.lower() == "href":
                    href = (value or "").strip()
                    break
            if href.startswith(("http://", "https://")):
                self.parts.append(f'<a href="{escape(href, quote=True)}">')
            else:
                self.parts.append(h(self.get_starttag_text() or ""))
            return
        if normalized_tag in self.allowed_tags:
            self.parts.append(f"<{normalized_tag}>")
        else:
            self.parts.append(h(self.get_starttag_text() or ""))

    def handle_endtag(self, tag):
        normalized_tag = tag.lower()
        if normalized_tag in self.allowed_tags:
            self.parts.append(f"</{normalized_tag}>")
        else:
            self.parts.append(h(f"</{tag}>"))

    def handle_startendtag(self, tag, attrs):
        self.parts.append(h(self.get_starttag_text() or ""))

    def handle_data(self, data):
        self.parts.append(h(data))

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_comment(self, data):
        self.parts.append(h(f"<!--{data}-->"))


def sanitize_telegram_html(value: str) -> str:
    value = re.sub(r"(?i)<br\s*/?>", "\n", value or "")
    value = re.sub(r"(?i)&lt;br\s*/?&gt;", "\n", value)
    sanitizer = TelegramHTMLSanitizer()
    sanitizer.feed(value)
    sanitizer.close()
    return "".join(sanitizer.parts) or ""


def split_telegram_message(text: str, limit: int = 3800) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current.rstrip())
                current = ""
            for start in range(0, len(line), limit):
                chunks.append(line[start:start + limit].rstrip())
            continue
        if len(current) + len(line) > limit:
            chunks.append(current.rstrip())
            current = line
        else:
            current += line
    if current:
        chunks.append(current.rstrip())
    return [chunk for chunk in chunks if chunk]


async def send_telegram_html_chunks(message: Message, text: str):
    for chunk in split_telegram_message(text):
        await message.answer(chunk)


async def download_telegram_photo(image_url: str) -> BufferedInputFile | None:
    if not image_url:
        return None

    candidates = [image_url]
    if image_url.lower().endswith(".webp"):
        candidates.append(f"{image_url[:-5]}.jpg")
    basket_match = re.search(r"https://basket-\d+\.wbbasket\.ru/(.+)", image_url, re.IGNORECASE)
    if basket_match:
        image_path = basket_match.group(1)
        for basket_number in range(1, 80):
            candidates.append(f"https://basket-{basket_number:02d}.wbbasket.ru/{image_path}")
        if image_path.lower().endswith(".webp"):
            jpg_path = f"{image_path[:-5]}.jpg"
            for basket_number in range(1, 80):
                candidates.append(f"https://basket-{basket_number:02d}.wbbasket.ru/{jpg_path}")
    candidates = list(dict.fromkeys(candidates))

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Referer": "https://www.wildberries.ru/",
    }
    last_error = None
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True, headers=headers) as client:
        for candidate in candidates:
            try:
                response = await client.get(candidate)
                response.raise_for_status()
                content_type = (response.headers.get("content-type") or "").lower()
                if not content_type.startswith("image/") or not response.content:
                    continue
                extension = "jpg"
                if "png" in content_type:
                    extension = "png"
                elif "webp" in content_type:
                    extension = "webp"
                if extension == "webp":
                    try:
                        from PIL import Image # type: ignore

                        image = Image.open(BytesIO(response.content)).convert("RGB")
                        output = BytesIO()
                        image.save(output, format="JPEG", quality=90)
                        return BufferedInputFile(output.getvalue(), filename="wb_product.jpg")
                    except Exception as error:
                        log_api_error(f"Telegram product photo convert failed: {error}")
                return BufferedInputFile(response.content, filename=f"wb_product.{extension}")
            except Exception as error:
                last_error = f"url={candidate}, error={error}"
    if last_error:
        log_api_error(f"Telegram product photo download failed: {last_error}")
    return None


def format_ai_reply_for_telegram(reply: str) -> str:
    lines = []
    for raw_line in escape(reply.strip()).splitlines():
        line = raw_line.strip()
        if not line:
            lines.append("")
            continue
        if line.startswith("### "):
            lines.append(f"<b>{line[4:].strip()}</b>")
            continue
        if line.startswith("## "):
            lines.append(f"<b>{line[3:].strip()}</b>")
            continue
        if line.startswith("# "):
            lines.append(f"<b>{line[2:].strip()}</b>")
            continue
        if re.match(r"^[-*]\s+", line):
            lines.append(f"• {line[2:].strip()}")
            continue
        lines.append(raw_line)

    formatted = "\n".join(lines)
    formatted = re.sub(r"(?<!\*)\*\*(.+?)\*\*(?!\*)", r"<b>\1</b>", formatted)
    formatted = re.sub(r"(?<!\*)\*(.+?)\*(?!\*)", r"<i>\1</i>", formatted)
    formatted = re.sub(r"`([^`]+)`", r"<code>\1</code>", formatted)
    return formatted or "Сервис временно не получил ответ. Попробуйте еще раз позже."


def normalize_optional_broadcast_value(value: str | None) -> str | None:
    cleaned = (value or "").strip()
    if cleaned.lower() in {"", "-", "нет", "пропустить", "skip"}:
        return None
    return cleaned


def is_public_http_url(value: str | None) -> bool:
    parsed = urlparse((value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def build_broadcast_preview_text(data: dict) -> str:
    text = escape(data.get("broadcast_text") or "")
    image_url = escape(data["broadcast_image_url"]) if data.get("broadcast_image_url") else "не задано"
    if data.get("broadcast_button_text") and data.get("broadcast_button_url"):
        button_info = f"{escape(data['broadcast_button_text'])} -> {escape(data['broadcast_button_url'])}"
    else:
        button_info = "не задана"
    return (
        "<b>Предпросмотр рассылки</b>\n\n"
        f"<b>Текст:</b>\n<code>{text}</code>\n\n"
        f"<b>Изображение:</b> {image_url}\n"
        f"<b>Кнопка:</b> {button_info}\n\n"
        "Подтвердить рассылку?"
    )


class AdminPanel(StatesGroup):
    waiting_broadcast_text = State()
    waiting_broadcast_image = State()
    waiting_broadcast_button_text = State()
    waiting_broadcast_button_url = State()
    confirm_broadcast = State()

main_keyboard = None

from aiogram.types import ReplyKeyboardMarkup, KeyboardButton
user_reply_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="👤 Кабинет")],
        [KeyboardButton(text="💳 Тарифы")],
        [KeyboardButton(text="📄 Публичная Оферта")]
    ],
    resize_keyboard=True
)

admin_reply_keyboard = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="/start"), KeyboardButton(text="/admin")]
    ],
    resize_keyboard=True
)

admin_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="broadcast")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton(text="Закрыть", callback_data="exit_admin")]
    ]
)

confirm_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="✅ Отправить", callback_data="confirm_broadcast")],
        [InlineKeyboardButton(text="✖️ Отменить", callback_data="cancel_broadcast")]
    ]
)

profile_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Купить PRO на 30 дней", url="__PRO_URL__")],
        [InlineKeyboardButton(text="Обновить кабинет", callback_data="refresh_profile")],
    ]
)


@dp.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    user_id = str(message.from_user.id)
    if user_id in settings.admin_ids:
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer("<b>Админ-панель</b>\nВыберите действие.", reply_markup=admin_keyboard)
        await state.clear()
    else:
        await message.answer("⛔️ У вас нет доступа к админ-панели.")

@dp.callback_query(lambda c: c.data == "admin_panel")
async def cb_admin_panel(callback: CallbackQuery, state: FSMContext):
    user_id = str(callback.from_user.id)
    if user_id in settings.admin_ids:
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer("<b>Админ-панель</b>\nВыберите действие.", reply_markup=admin_keyboard)
        await state.clear()
    else:
        await callback.answer("⛔️ Нет доступа", show_alert=True)

@dp.callback_query(lambda c: c.data == "exit_admin")
async def exit_admin(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.message.delete()
    except Exception:
        pass
    data = await state.get_data()
    prompt_msg_id = data.get("broadcast_prompt_msg_id")
    if prompt_msg_id:
        try:
            await callback.bot.delete_message(callback.message.chat.id, prompt_msg_id)
        except Exception:
            pass
    await callback.answer("Вы вышли из админ-панели.", show_alert=True)
    await state.clear()

@dp.callback_query(lambda c: c.data == "stats")
async def admin_stats(callback: CallbackQuery, state: FSMContext):
    user_id = str(callback.from_user.id)
    if user_id not in settings.admin_ids:
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    from app.services.db import get_price_compare_stats, get_stats
    rows = await get_stats()
    price_stats = await get_price_compare_stats()
    text = "<b>📊 Статистика сервиса</b>\n\n"
    total_users = 0
    total_requests = 0
    for platform, users, requests in rows:
        text += f"• {platform}: пользователей — <b>{users}</b>, запросов — <b>{requests or 0}</b>\n"
        total_users += users
        total_requests += requests or 0
    text += f"\n<b>Всего:</b> пользователей — {total_users}, запросов — {total_requests}"
    text += (
        "\n\n<b>💸 Сравнение цен</b>\n"
        f"• запросов — <b>{price_stats['total']}</b>\n"
        f"• успешно — <b>{price_stats['success']}</b>, частично — <b>{price_stats['partial']}</b>, ошибок — <b>{price_stats['errors']}</b>\n"
        f"• Ozon с результатом — <b>{price_stats['ozon_success_rate']}%</b>\n"
        f"• Яндекс Маркет с результатом — <b>{price_stats['yandex_success_rate']}%</b>\n"
        f"• среднее время — <b>{price_stats['avg_elapsed']} сек.</b>"
    )
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(text, reply_markup=admin_keyboard)

@dp.callback_query(lambda c: c.data == "broadcast")
async def admin_broadcast_start(callback: CallbackQuery, state: FSMContext):
    user_id = str(callback.from_user.id)
    if user_id not in settings.admin_ids:
        await callback.answer("⛔️ Нет доступа", show_alert=True)
        return
    try:
        await callback.message.delete()
    except Exception:
        pass
    msg = await callback.message.answer(
        "<b>📣 Новая рассылка</b>\n\n"
        "Отправьте текст сообщения. После этого я спрошу картинку и кнопку."
    )
    await state.update_data(broadcast_prompt_msg_id=msg.message_id)
    await state.set_state(AdminPanel.waiting_broadcast_text)

@dp.message(AdminPanel.waiting_broadcast_text)
async def admin_broadcast_text(message: Message, state: FSMContext):
    await state.update_data(broadcast_text=message.text)
    data = await state.get_data()
    prompt_msg_id = data.get("broadcast_prompt_msg_id")
    if prompt_msg_id:
        try:
            await message.bot.delete_message(message.chat.id, prompt_msg_id)
        except Exception:
            pass
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer("Картинка: отправьте URL изображения или '-' чтобы пропустить.")
    await state.set_state(AdminPanel.waiting_broadcast_image)


@dp.message(AdminPanel.waiting_broadcast_image)
async def admin_broadcast_image(message: Message, state: FSMContext):
    image_url = normalize_optional_broadcast_value(message.text)
    if image_url and not is_public_http_url(image_url):
        await message.answer("Похоже, это не URL. Отправьте ссылку вида https://... или '-' чтобы пропустить.")
        return
    await state.update_data(broadcast_image_url=image_url)
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer("Кнопка: отправьте текст кнопки или '-' чтобы не добавлять кнопку.")
    await state.set_state(AdminPanel.waiting_broadcast_button_text)


@dp.message(AdminPanel.waiting_broadcast_button_text)
async def admin_broadcast_button_text(message: Message, state: FSMContext):
    button_text = normalize_optional_broadcast_value(message.text)
    await state.update_data(broadcast_button_text=button_text)
    try:
        await message.delete()
    except Exception:
        pass
    if not button_text:
        await state.update_data(broadcast_button_url=None)
        data = await state.get_data()
        await message.answer(build_broadcast_preview_text(data), reply_markup=confirm_keyboard)
        await state.set_state(AdminPanel.confirm_broadcast)
        return
    await message.answer("Ссылка кнопки: отправьте URL или '-' чтобы убрать кнопку.")
    await state.set_state(AdminPanel.waiting_broadcast_button_url)


@dp.message(AdminPanel.waiting_broadcast_button_url)
async def admin_broadcast_button_url(message: Message, state: FSMContext):
    button_url = normalize_optional_broadcast_value(message.text)
    if not button_url:
        await state.update_data(broadcast_button_text=None, broadcast_button_url=None)
    elif not is_public_http_url(button_url):
        await message.answer("Ссылка кнопки должна начинаться с http:// или https://. Отправьте корректный URL или '-'.")
        return
    else:
        await state.update_data(broadcast_button_url=button_url)
    try:
        await message.delete()
    except Exception:
        pass
    data = await state.get_data()
    await message.answer(build_broadcast_preview_text(data), reply_markup=confirm_keyboard)
    await state.set_state(AdminPanel.confirm_broadcast)

@dp.callback_query(lambda c: c.data == "confirm_broadcast")
async def admin_broadcast_confirm(callback: CallbackQuery, state: FSMContext):
    import logging
    logging.info(f"[ADMIN] confirm_broadcast: user={callback.from_user.id} state={await state.get_state()}")
    if not await state.get_state():
        await callback.answer("Уже отправлено!", show_alert=False)
        return
    data = await state.get_data()
    text = data.get("broadcast_text")
    image_url = data.get("broadcast_image_url")
    button_text = data.get("broadcast_button_text")
    button_url = data.get("broadcast_button_url")
    await state.clear()
    try:
        await callback.message.delete()
    except Exception:
        pass
    from app.platforms.telegram.broadcast import broadcast as tg_broadcast
    from app.platforms.vk.broadcast import broadcast_vk
    logging.info("[ADMIN] Telegram broadcast start")
    tg_result = await tg_broadcast(text, bot, image_url=image_url, button_text=button_text, button_url=button_url)
    logging.info("[ADMIN] Telegram broadcast done")
    logging.info("[ADMIN] VK broadcast start")
    vk_result = await broadcast_vk(text, image_url=image_url, button_text=button_text, button_url=button_url)
    logging.info("[ADMIN] VK broadcast done")
    await callback.answer("Рассылка отправлена")
    await callback.message.answer(
        "📣 Рассылка завершена.\n"
        f"Telegram: {tg_result['sent']}/{tg_result['attempted']}\n"
        f"VK: {vk_result['sent']}/{vk_result['attempted']}\n"
        f"Ошибок: {tg_result['failed'] + vk_result['failed']}",
        reply_markup=admin_keyboard,
    )

@dp.callback_query(lambda c: c.data == "cancel_broadcast")
async def admin_broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer("Рассылка отменена.", reply_markup=admin_keyboard)
    await state.clear()

@dp.message(Command("start"))
async def cmd_start(message: Message):
    user_id = str(message.from_user.id)
    await add_user("telegram", user_id, message.from_user.username)
    start_parts = (message.text or "").split(maxsplit=1)
    referral_payload = start_parts[1].strip() if len(start_parts) > 1 else ""
    referral_attached = False
    if referral_payload:
        referral_attached = await set_referred_by("telegram", user_id, referral_payload)
    if user_id in settings.admin_ids:
        await message.answer(
            "Админ-панель на связи.\n\n"
            "Команды: /admin — управление, /profile — личный кабинет и лимиты.",
            reply_markup=admin_reply_keyboard
        )
    else:
        text = (
            "<b>SmartCheck уже здесь.</b>\n\n"
            "Пришлите ссылку на товар Wildberries или артикул. Я проверю карточку, отзывы и покажу, где есть риск накрутки или неприятный сюрприз.\n\n"
            "Бесплатно: 1 проверка в день.\n"
            "Кабинет, покупки и PRO: /profile или кнопка ниже.\n\n"
            "Публичная оферта и отказ от ответственности: /offer или кнопка ниже.\n\n"
            "Есть приглашение? Откройте реферальную ссылку или отправьте /start КОД."
        )
        if referral_attached:
            text += "\n\n✅ Реферальная ссылка сохранена. После первой оплаты пригласивший получит бонус."
        await message.answer(text, reply_markup=user_reply_keyboard)


def format_profile_text(profile: dict, referral_stats: dict, recent_payments: list[dict], referral_link: str) -> str:
    free_tester = bool(profile.get("free_tester"))
    tariff = "TEST" if free_tester else h((profile.get("tariff") or "free").upper())
    balance_requests = profile.get("balance_requests") or 0
    free_left = "безлимит" if free_tester else max(0, 1 - (profile.get("free_requests_used_today") or 0))
    pro_expires_at = h(profile.get("pro_expires_at") or "-")
    registered_at = h(profile.get("registered_at") or "-")
    payment_lines = []
    for payment in recent_payments[:3]:
        payment_lines.append(
            f"• {h(payment['payment_type'])} · {h(payment['amount'])} ₽ · {h(payment['status'])} · {h(payment['created_at'])}"
        )
    payments_text = "\n".join(payment_lines) if payment_lines else "• Оплат пока нет"
    return (
        "<b>👤 Кабинет SmartCheck</b>\n\n"
        f"Тариф: <b>{tariff}</b>\n"
        f"Платных проверок: <b>{balance_requests}</b>\n"
        f"Бесплатно сегодня: <b>{free_left}</b>\n"
        f"PRO активен до: <b>{pro_expires_at}</b>\n"
        f"Дата регистрации: <b>{registered_at}</b>\n\n"
        f"<b>🤝 Рефералка</b>\n"
        f"Код: <code>{h(referral_stats.get('referral_code') or '-')}</code>\n"
        f"Приглашено: <b>{referral_stats.get('invited_total', 0)}</b>\n"
        f"Бонусов начислено: <b>{referral_stats.get('rewarded_total', 0)}</b>\n"
        f"Ссылка:\n<code>{h(referral_link)}</code>\n\n"
        f"<b>💳 Последние оплаты</b>\n{payments_text}\n\n"
        f"<b>Перед оплатой:</b> {h(PAYMENT_ACCEPTANCE_NOTICE)}\n"
        f'<a href="{OFFER_URL}">Публичная оферта и Политика конфиденциальности</a>\n\n'
        f"PRO даёт 30 проверок и сравнение цен с Ozon и Яндекс Маркетом на 30 дней. "
        f"Бонус за первого оплатившего приглашённого: <b>{settings.REFERRAL_BONUS_REQUESTS}</b> проверки."
    )


async def build_profile_payload(user_id: str):
    profile = await get_user_profile("telegram", user_id)
    referral_stats = await get_referral_stats("telegram", user_id)
    recent_payments = await get_recent_payments("telegram", user_id, 5)
    bot_me = await bot.get_me()
    referral_code = referral_stats.get("referral_code") or f"telegram_{user_id}"
    referral_link = f"https://t.me/{bot_me.username}?start={referral_code}" if bot_me.username else referral_code
    return profile, referral_stats, recent_payments, referral_link


def build_profile_keyboard(user_id: str, profile: dict) -> InlineKeyboardMarkup:
    if profile.get("free_tester"):
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📄 Публичная Оферта", url=OFFER_URL)],
                [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_profile")],
            ]
        )
    pro_button_text = "⚡ Продлить PRO" if (profile.get("tariff") or "").lower() == "pro" else "⚡ Купить PRO"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🧾 Пакет 5 проверок",
                    url=create_checkout_url("telegram", user_id, "one_time"),
                )
            ],
            [
                InlineKeyboardButton(
                    text=pro_button_text,
                    url=create_checkout_url("telegram", user_id, "pro"),
                )
            ],
            [InlineKeyboardButton(text="📄 Публичная Оферта", url=OFFER_URL)],
            [InlineKeyboardButton(text="🔄 Обновить", callback_data="refresh_profile")],
        ]
    )


async def send_profile_message(message: Message):
    user_id = str(message.from_user.id)
    await add_user("telegram", user_id, message.from_user.username)
    profile, referral_stats, recent_payments, referral_link = await build_profile_payload(user_id)
    if not profile:
        await message.answer("Профиль пока не найден. Попробуйте ещё раз через несколько секунд.")
        return
    await message.answer(
        format_profile_text(profile, referral_stats, recent_payments, referral_link),
        reply_markup=build_profile_keyboard(user_id, profile),
    )


def build_tariff_keyboard(user_id: str) -> InlineKeyboardMarkup:
    one_time_plan = get_plan("one_time")
    pro_plan = get_plan("pro")
    one_time_price = one_time_plan.price_rub if one_time_plan else 99
    pro_price = pro_plan.price_rub if pro_plan else 349
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Пакет 5 проверок - {one_time_price} ₽",
                    url=create_checkout_url("telegram", user_id, "one_time"),
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"PRO на 30 дней - {pro_price} ₽",
                    url=create_checkout_url("telegram", user_id, "pro"),
                )
            ],
            [InlineKeyboardButton(text="📄 Публичная оферта", url=OFFER_URL)],
        ]
    )


def build_tariffs_text_html() -> str:
    one_time_plan = get_plan("one_time")
    pro_plan = get_plan("pro")
    one_time_price = one_time_plan.price_rub if one_time_plan else 99
    pro_price = pro_plan.price_rub if pro_plan else 349
    return (
        "<b>Тарифы SmartCheck</b>\n\n"
        "1. <b>Что покупают:</b> Пакет проверок или подписку на ИИ-анализатор отзывов SmartCheck.\n\n"
        f"2. <b>Пакет 5 проверок:</b> разовый доступ к 5 анализам отзывов WB без сравнения цен. Цена: <b>{one_time_price} рублей</b>.\n\n"
        f"3. <b>PRO на 30 дней:</b> 30 анализов WB и полное сравнение цен, рейтингов и рисков с Ozon и Яндекс Маркетом. Цена: <b>{pro_price} рублей</b>.\n\n"
        f'<a href="{OFFER_URL}">Условия оплаты и возврата (Публичная оферта)</a>'
    )


def build_price_compare_pro_offer_html() -> str:
    pro_plan = get_plan("pro")
    pro_price = pro_plan.price_rub if pro_plan else 349
    return (
        "🔒 <b>Сравнение цен доступно в SmartCheck PRO</b>\n\n"
        "PRO проверит предложения на Ozon и Яндекс Маркете и покажет только надежные совпадения по бренду, "
        "модели, цвету и комплектации. Затем сопоставит цену, рейтинг, отзывы и риск покупки.\n\n"
        "<b>В подписке:</b>\n"
        "• 30 анализов товаров;\n"
        "• сравнение цен и рисков;\n"
        "• доступ на 30 дней.\n\n"
        f"Стоимость: <b>{pro_price} ₽</b>."
    )


def build_price_compare_locked_cta_html() -> str:
    pro_plan = get_plan("pro")
    pro_price = pro_plan.price_rub if pro_plan else 349
    return (
        "⚖️ <b>Можно сравнить цену и риски</b>\n\n"
        "Для этого товара SmartCheck может проверить Ozon и Яндекс Маркет: совпадение по бренду, модели, цвету, "
        "комплектации, цену, рейтинг и риск покупки.\n\n"
        f"Сравнение входит в <b>SmartCheck PRO</b>: 30 дней доступа и 30 анализов за <b>{pro_price} ₽</b>."
    )


def build_price_compare_available_cta_html() -> str:
    return (
        "🔎 <b>Можно сравнить с Ozon и Яндекс Маркетом</b>\n\n"
        "Проверю не просто цену, а совпадение товара, рейтинг, объем отзывов и риск покупки. "
        "Если дешевле, но подозрительнее, SmartCheck так и скажет."
    )


def build_price_compare_action_keyboard(article: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚖️ Сравнить цену и риски",
                    callback_data=f"price_compare:{article}",
                )
            ]
        ]
    )


def build_price_compare_locked_keyboard(user_id: str, article: str) -> InlineKeyboardMarkup:
    pro_plan = get_plan("pro")
    pro_price = pro_plan.price_rub if pro_plan else 349
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="⚖️ Посмотреть сравнение",
                    callback_data=f"price_compare:{article}",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"⚡ Открыть PRO — {pro_price} ₽",
                    url=create_checkout_url("telegram", user_id, "pro"),
                )
            ],
            [InlineKeyboardButton(text="📄 Условия оплаты и возврата", url=OFFER_URL)],
        ]
    )


def build_price_compare_pro_keyboard(user_id: str) -> InlineKeyboardMarkup:
    pro_plan = get_plan("pro")
    pro_price = pro_plan.price_rub if pro_plan else 349
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"⚡ Открыть PRO — {pro_price} ₽ / 30 дней",
                    url=create_checkout_url("telegram", user_id, "pro"),
                )
            ],
            [InlineKeyboardButton(text="📄 Условия оплаты и возврата", url=OFFER_URL)],
        ]
    )


async def send_tariff_message(message: Message):
    user_id = str(message.from_user.id)
    await add_user("telegram", user_id, message.from_user.username)
    await message.answer(
        build_tariffs_text_html(),
        reply_markup=build_tariff_keyboard(user_id),
        disable_web_page_preview=True,
    )


@dp.message(Command("tariffs", "pay", "payment", "subscribe"))
async def cmd_tariffs(message: Message):
    await send_tariff_message(message)


@dp.message(F.text.in_({"💳 Тарифы", "Тарифы", "тарифы", "Оплата", "оплата", "Подписка", "подписка"}))
async def cmd_tariffs_button(message: Message):
    await send_tariff_message(message)


@dp.message(Command("offer"))
async def cmd_offer(message: Message):
    await message.answer(build_html_offer_text(), disable_web_page_preview=True)


@dp.message(Command("profile"))
async def cmd_profile(message: Message):
    try:
        await send_profile_message(message)
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        from app.services.error_logger import log_api_error
        log_api_error(f"/profile error: {e}\n{traceback.format_exc()}")
        await message.answer("Произошла ошибка при получении профиля. Попробуйте позже.")


@dp.message(F.text.in_({"Мой кабинет", "👤 Кабинет", "Кабинет"}))
async def cmd_profile_button(message: Message):
    try:
        await send_profile_message(message)
    except Exception as e:
        import traceback
        print(traceback.format_exc())
        from app.services.error_logger import log_api_error
        log_api_error(f"Мой кабинет error: {e}\n{traceback.format_exc()}")
        await message.answer("Произошла ошибка при получении профиля. Попробуйте позже.")


@dp.message(F.text.in_({"📄 Публичная Оферта", "📄 Публичная оферта", "Публичная Оферта", "Публичная оферта", "публичная оферта", "Оферта", "оферта"}))
async def cmd_offer_button(message: Message):
    await message.answer(build_html_offer_text(), disable_web_page_preview=True)


@dp.callback_query(lambda c: c.data == "refresh_profile")
async def refresh_profile(callback: CallbackQuery):
    user_id = str(callback.from_user.id)
    profile, referral_stats, recent_payments, referral_link = await build_profile_payload(user_id)
    if not profile:
        await callback.answer("Профиль пока недоступен", show_alert=True)
        return
    try:
        await callback.message.edit_text(
            format_profile_text(profile, referral_stats, recent_payments, referral_link),
            reply_markup=build_profile_keyboard(user_id, profile),
        )
    except Exception:
        await callback.message.answer(
            format_profile_text(profile, referral_stats, recent_payments, referral_link),
            reply_markup=build_profile_keyboard(user_id, profile),
        )
    await callback.answer("Кабинет обновлён")


@dp.callback_query(lambda c: bool(c.data and c.data.startswith("price_compare:")))
async def run_price_comparison(callback: CallbackQuery):
    article = (callback.data or "").split(":", 1)[-1].strip()
    if not article.isdigit():
        await callback.answer("Не удалось распознать товар", show_alert=True)
        return

    user_id = str(callback.from_user.id)
    if not await has_active_pro_access("telegram", user_id):
        await callback.answer("Сравнение цен доступно в SmartCheck PRO", show_alert=True)
        await callback.message.answer(
            build_price_compare_pro_offer_html(),
            reply_markup=build_price_compare_pro_keyboard(user_id),
            disable_web_page_preview=True,
        )
        return
    allowed_by_rate, retry_after = await telegram_rate_limiter.allow(
        key=f"telegram:price_compare:{user_id}",
        max_requests=2,
        window_seconds=max(10, settings.ANTIFLOOD_WINDOW_SECONDS),
    )
    if not allowed_by_rate:
        await callback.answer(f"Подождите примерно {retry_after} сек.", show_alert=True)
        return

    await callback.answer("Сравниваю цену и риски")
    status_message = await callback.message.answer(
        "⚖️ <b>Проверяю альтернативы</b>\nСверяю цену, рейтинг, отзывы и надежность совпадения..."
    )
    try:
        analysis = await asyncio.wait_for(
            analyze_wb_product(article),
            timeout=wb_analysis_timeout_seconds(),
        )
        comparison = await compare_wb_product_prices(
            analysis.product,
            user_input=article,
            platform="telegram",
            user_id=user_id,
        )
        wb_risk = analysis.review_analysis.risk_score if analysis.review_analysis else 25
        comparison = await enrich_price_comparison_with_risk(comparison, wb_risk)
        try:
            await status_message.delete()
        except Exception:
            pass
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await send_telegram_html_chunks(
            callback.message,
            sanitize_telegram_html(format_price_comparison_html(comparison)),
        )
    except PriceCompareProRequired:
        try:
            await status_message.delete()
        except Exception:
            pass
        await callback.message.answer(
            build_price_compare_pro_offer_html(),
            reply_markup=build_price_compare_pro_keyboard(user_id),
            disable_web_page_preview=True,
        )
    except PriceCompareError as error:
        await status_message.edit_text(
            f"⚖️ <b>Сравнение пока недоступно</b>\n{escape(str(error), quote=False)}"
        )
    except asyncio.TimeoutError:
        log_api_error(f"Telegram on-demand price compare timeout: article={article}")
        await status_message.edit_text(
            "⚖️ <b>Сравнение пока недоступно</b>\n"
            "WB или ИИ слишком долго отвечают по этому товару. Попробуйте еще раз чуть позже."
        )
    except Exception as error:
        log_api_error(f"Telegram on-demand price compare error: {error}")
        await status_message.edit_text(
            "⚖️ <b>Не удалось завершить сравнение</b>\n"
            "Одна из площадок временно не отдала надежные данные. Попробуйте еще раз позже."
        )


ADMIN_SERVICE_TEXTS = {
    "админ-панель",
    "рассылка пользователям",
    "статистика",
    "выйти из админ-панели",
    "подтвердить рассылку",
    "отклонить рассылку"
}


@dp.message(F.text)
async def handle_message(message: Message, state: FSMContext):
    if message.text.lower().strip() in ADMIN_SERVICE_TEXTS:
        return
    user_id = str(message.from_user.id)
    data = await state.get_data()
    last_broadcast_text = data.get("last_broadcast_text")
    if last_broadcast_text and message.text.strip() == last_broadcast_text and user_id in settings.admin_ids:
        return
    await add_user("telegram", user_id, message.from_user.username)
    allowed_by_rate, retry_after = await telegram_rate_limiter.allow(
        key=f"telegram:{user_id}",
        max_requests=settings.ANTIFLOOD_MAX_REQUESTS,
        window_seconds=settings.ANTIFLOOD_WINDOW_SECONDS,
    )
    if not allowed_by_rate:
        await message.answer(
            f"⏳ Слишком много запросов подряд. Подождите примерно {retry_after} сек. и попробуйте снова."
        )
        return
    allowed, reason, profile = await get_request_access("telegram", user_id)
    if not allowed:
        if reason.startswith("Лимит") and profile:
            await message.answer(
                "Лимит проверок исчерпан. Выберите пакет проверок или оформите PRO ниже.\n\n"
                f"<b>Перед оплатой:</b> {h(PAYMENT_ACCEPTANCE_NOTICE)}",
                reply_markup=build_profile_keyboard(user_id, profile),
            )
        else:
            await message.answer(reason)
        return
    prompt = message.text
    wb_article = extract_wb_article(prompt)
    system_prompt = get_system_prompt()
    history = await get_recent_history("telegram", user_id)
    messages = [{"role": "system", "content": system_prompt}]
    for q, a in history:
        if q:
            messages.append({"role": "user", "content": q})
        if a:
            messages.append({"role": "assistant", "content": a})
    messages.append({"role": "user", "content": prompt})

    msg = await message.answer("<i>Проверяю данные.</i>")
    loading = True
    reply = ""

    async def animate_loading():
        dots = 1
        while loading:
            text = f"<i>Проверяю данные{'.' * dots}</i>"
            try:
                await msg.edit_text(text)
            except Exception:
                pass
            dots = dots + 1 if dots < 3 else 1
            await asyncio.sleep(0.5)

    task = asyncio.create_task(animate_loading())

    async def finish_with_text(text: str):
        try:
            await msg.edit_text(text)
        except Exception:
            await message.answer(text)

    try:
        if wb_article:
            analysis = await asyncio.wait_for(
                analyze_wb_product(prompt),
                timeout=wb_analysis_timeout_seconds(),
            )
            if analysis.review_analysis:
                logger.info(
                    "Telegram WB analysis: user_id=%s, article=%s, loaded=%s, filtered=%s, risk_score=%s",
                    user_id,
                    analysis.product.article,
                    analysis.total_reviews_loaded,
                    analysis.review_analysis.stats.total_after_filter,
                    analysis.review_analysis.risk_score,
                )
            consumed, consume_reason = await consume_request_limit("telegram", user_id)
            if not consumed:
                loading = False
                await task
                await msg.edit_text(consume_reason)
                return
            await increment_requests("telegram", user_id)
            loading = False
            await task
            preview_text = format_product_preview(
                analysis.product,
                analysis.total_reviews_loaded,
                len(analysis.selected_reviews),
            )
            try:
                await msg.delete()
            except Exception:
                pass
            if analysis.product.image_url:
                try:
                    photo = await download_telegram_photo(analysis.product.image_url)
                    if photo:
                        await message.answer_photo(photo, caption=preview_text)
                    else:
                        await message.answer(preview_text)
                except Exception as photo_error:
                    log_api_error(f"Telegram product photo send failed: {photo_error}")
                    await message.answer(preview_text)
            else:
                await message.answer(preview_text)
            safe_summary_html = sanitize_telegram_html(analysis.summary_html)
            if safe_summary_html:
                await send_telegram_html_chunks(message, safe_summary_html)

            price_compare_text = ""
            if await has_active_pro_access("telegram", user_id):
                price_compare_text = build_price_compare_available_cta_html()
                await message.answer(
                    price_compare_text,
                    reply_markup=build_price_compare_action_keyboard(analysis.product.article),
                )
            else:
                price_compare_text = build_price_compare_locked_cta_html()
                await message.answer(
                    price_compare_text,
                    reply_markup=build_price_compare_locked_keyboard(user_id, analysis.product.article),
                    disable_web_page_preview=True,
                )
            history_response = "\n\n".join(part for part in (safe_summary_html, price_compare_text) if part)
            await add_history("telegram", user_id, prompt, history_response)
            return
        async for chunk in proxyapi_stream_with_context(messages):
            reply += chunk
        loading = False
        await task
        if not reply or not reply.strip():
            reply = "Сервис временно не получил ответ. Попробуйте еще раз позже."
        formatted_reply = format_ai_reply_for_telegram(reply)
        consumed, consume_reason = await consume_request_limit("telegram", user_id)
        if not consumed:
            await msg.edit_text(consume_reason)
            return
        await increment_requests("telegram", user_id)
        reply_chunks = split_telegram_message(formatted_reply)
        await msg.edit_text(reply_chunks[0])
        for chunk in reply_chunks[1:]:
            await message.answer(chunk)
        await add_history("telegram", user_id, prompt, reply)
    except WBTemporaryUnavailable as error:
        loading = False
        await task
        await finish_with_text(str(error))
    except WBNotFound as error:
        loading = False
        await task
        await finish_with_text(str(error))
    except WBError as error:
        loading = False
        await task
        log_api_error(f"WBError: {error}")
        await finish_with_text(str(error))
    except asyncio.TimeoutError:
        loading = False
        await task
        log_api_error(f"Telegram WB analysis timeout: user_id={user_id}, prompt={prompt[:120]}")
        await finish_with_text(
            "WB или ИИ слишком долго отвечают по этому товару. "
            "Я остановил проверку, чтобы бот не висел бесконечно. Попробуйте еще раз чуть позже."
        )
    except Exception as e:
        loading = False
        await task
        import traceback
        tb = traceback.format_exc()
        log_api_error(f"Telegram API error: {e}\n{tb}")
        await finish_with_text(PUBLIC_TECHNICAL_ERROR_MESSAGE)


async def proxyapi_stream_with_context(messages):
    import logging
    logging.warning(
        "TG proxyapi_stream_with_context: base_url=%s, messages_count=%s",
        settings.PROXYAPI_URL,
        len(messages),
    )
    client = openai.AsyncOpenAI(
        api_key=settings.PROXYAPI_KEY,
        base_url=settings.PROXYAPI_URL,
        timeout=settings.PROXYAPI_TIMEOUT_SECONDS,
    )
    response = await client.chat.completions.create(
        model="gpt-3.5-turbo",
        messages=messages,
        stream=False
    )
    yield response.choices[0].message.content or ""




def run():
    import sys
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        try:
            import uvloop  # type: ignore # uvloop работает только на Unix-системах
            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
        except ImportError:
            pass
    from app.services.db import init_db

    async def main():
        await init_db()
        await dp.start_polling(bot)

    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Бот остановлен")


if __name__ == "__main__":
    run()
