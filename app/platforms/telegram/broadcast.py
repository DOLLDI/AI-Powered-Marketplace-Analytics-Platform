import logging
import asyncio

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramNotFound, TelegramRetryAfter
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.services.db import list_platform_user_ids, set_user_blocked


TELEGRAM_BROADCAST_TEXT_LIMIT = 3900
TELEGRAM_BROADCAST_CAPTION_LIMIT = 1000


def _split_message(text: str, limit: int = TELEGRAM_BROADCAST_TEXT_LIMIT) -> list[str]:
    text = text or ""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
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
    return [chunk for chunk in chunks if chunk] or ["Рассылка без текста."]


def _should_block_recipient(error: Exception) -> bool:
    if isinstance(error, (TelegramForbiddenError, TelegramNotFound)):
        return True
    if isinstance(error, TelegramBadRequest):
        message = str(error).lower()
        return any(
            marker in message
            for marker in (
                "chat not found",
                "user is deactivated",
                "bot was blocked",
                "bot can't initiate conversation",
            )
        )
    return False


async def broadcast(
    text: str,
    bot,
    image_url: str | None = None,
    button_text: str | None = None,
    button_url: str | None = None,
):
    bot_id = (await bot.get_me()).id
    attempted = 0
    sent = 0
    failed = 0
    reply_markup = None
    if button_text and button_url:
        reply_markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=button_text, url=button_url)]]
        )
    text_chunks = _split_message(text)
    for raw_user_id in await list_platform_user_ids("telegram"):
        user_id = int(raw_user_id)
        if user_id == bot_id:
            continue
        attempted += 1
        try:
            if image_url:
                caption = text_chunks[0]
                if len(caption) > TELEGRAM_BROADCAST_CAPTION_LIMIT:
                    caption = caption[:TELEGRAM_BROADCAST_CAPTION_LIMIT - 1].rstrip() + "…"
                try:
                    await bot.send_photo(
                        user_id,
                        image_url,
                        caption=caption,
                        reply_markup=reply_markup,
                        parse_mode=None,
                    )
                    for chunk in text_chunks[1:]:
                        await bot.send_message(user_id, chunk, parse_mode=None)
                except TelegramBadRequest as photo_error:
                    logging.warning(f"[TG BROADCAST] photo fallback for {user_id}: {photo_error}")
                    fallback_chunks = _split_message(f"{text}\n\nИзображение: {image_url}")
                    await bot.send_message(
                        user_id,
                        fallback_chunks[0],
                        reply_markup=reply_markup,
                        parse_mode=None,
                    )
                    for chunk in fallback_chunks[1:]:
                        await bot.send_message(user_id, chunk, parse_mode=None)
            else:
                await bot.send_message(user_id, text_chunks[0], reply_markup=reply_markup, parse_mode=None)
                for chunk in text_chunks[1:]:
                    await bot.send_message(user_id, chunk, parse_mode=None)
            sent += 1
            logging.info(f"[TG BROADCAST] sent to {user_id}")
        except TelegramRetryAfter as error:
            await asyncio.sleep(float(error.retry_after) + 0.5)
            failed += 1
            logging.warning(f"[TG BROADCAST] retry-after for {user_id}: {error.retry_after}s")
        except Exception as e:
            if _should_block_recipient(e):
                await set_user_blocked("telegram", str(user_id), True)
                logging.warning(f"[TG BROADCAST] marked {user_id} as blocked after Telegram error: {e}")
            failed += 1
            logging.warning(f"[TG BROADCAST] error for {user_id}: {e}")
    return {
        "platform": "telegram",
        "attempted": attempted,
        "sent": sent,
        "failed": failed,
    }
