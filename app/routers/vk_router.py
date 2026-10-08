from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from app.platforms.vk import handlers
from app.services import config
import logging

router = APIRouter()

@router.post('/webhook')
async def vk_webhook(req: Request):
    try:
        data = await req.json()
    except Exception as error:
        logging.warning("VK webhook invalid JSON: %s", error)
        raise HTTPException(status_code=400, detail="Invalid JSON") from error
    logging.info(
        "VK webhook incoming: type=%s event_id=%s group_id=%s",
        data.get("type"),
        data.get("event_id"),
        data.get("group_id"),
    )
    if data.get('type') == 'confirmation':
        logging.info("VK webhook confirmation requested")
        return PlainTextResponse(config.settings.VK_CONFIRMATION_TOKEN)
    if config.settings.VK_CALLBACK_SECRET and data.get("secret") != config.settings.VK_CALLBACK_SECRET:
        logging.warning("VK webhook rejected: invalid callback secret")
        raise HTTPException(status_code=403, detail="Invalid VK callback secret")
    await handlers.handle_event(data)
    return PlainTextResponse("ok")
