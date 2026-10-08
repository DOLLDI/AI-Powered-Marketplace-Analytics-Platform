import json
import traceback
from fastapi import APIRouter, Request, HTTPException
from aiogram.types import Update
from pathlib import Path
from app.platforms.telegram.aiogram_bot import bot, dp
from app.services.config import settings
from app.services.error_logger import log_api_error, PUBLIC_WEBHOOK_ERROR_DETAIL
from app.services.logger import logger
router = APIRouter()

TELEGRAM_WEBHOOK_LOG_PATH = Path("logs/telegram_webhook_in.log")


def append_telegram_webhook_log(line: str):
    TELEGRAM_WEBHOOK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TELEGRAM_WEBHOOK_LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line)

@router.post('/webhook')
async def telegram_webhook(req: Request):
    if settings.TELEGRAM_WEBHOOK_DEBUG_LOG:
        try:
            append_telegram_webhook_log("[HANDLER ENTERED]\n")
        except Exception as log_exc:
            logger.warning("Telegram webhook debug log error: %s", log_exc)
    try:
        try:
            data = await req.json()
        except Exception as json_exc:
            tb = traceback.format_exc()
            log_api_error(f"Telegram webhook JSON error: {json_exc}\n{tb}")
            raise HTTPException(status_code=400, detail="Invalid JSON")
        if settings.TELEGRAM_WEBHOOK_DEBUG_LOG:
            try:
                append_telegram_webhook_log(f"[INCOMING UPDATE] {json.dumps(data, ensure_ascii=False)}\n")
            except Exception as log_exc:
                logger.warning("Telegram webhook debug log error: %s", log_exc)
        logger.info("Telegram webhook update received: update_id=%s", data.get("update_id"))
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
    except Exception as e:
        tb = traceback.format_exc()
        log_api_error(f"Telegram webhook error: {e}\n{tb}")
        raise HTTPException(status_code=500, detail=PUBLIC_WEBHOOK_ERROR_DETAIL)
    return {"ok": True}
