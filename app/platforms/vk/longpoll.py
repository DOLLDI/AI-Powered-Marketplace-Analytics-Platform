import asyncio

import httpx

from app.platforms.vk import handlers
from app.services.config import settings
from app.services.error_logger import log_api_error
from app.services.logger import logger

VK_API = "https://api.vk.com/method"
VK_API_VERSION = "5.131"


async def _call_vk(method: str, data: dict) -> dict:
    payload = {
        "access_token": settings.VK_GROUP_TOKEN,
        "v": VK_API_VERSION,
        **data,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.post(f"{VK_API}/{method}", data=payload)
        response.raise_for_status()
        result = response.json()
    if result.get("error"):
        raise RuntimeError(f"VK {method} error: {result['error']}")
    return result.get("response") or {}


async def _enable_longpoll_events() -> None:
    await _call_vk(
        "groups.setLongPollSettings",
        {
            "group_id": settings.VK_GROUP_ID,
            "enabled": 1,
            "api_version": VK_API_VERSION,
            "message_new": 1,
        },
    )


async def _get_longpoll_server() -> dict:
    return await _call_vk("groups.getLongPollServer", {"group_id": settings.VK_GROUP_ID})


async def _backfill_recent_incoming() -> None:
    response = await _call_vk("messages.getConversations", {"count": 20, "filter": "all"})
    items = response.get("items") or []
    for item in reversed(items):
        message = item.get("last_message") or {}
        if message.get("out") != 0 or not message.get("text"):
            continue
        logger.info(
            "VK long poll backfill incoming: peer_id=%s message_id=%s",
            message.get("peer_id"),
            message.get("id"),
        )
        await handlers.handle_event(
            {
                "type": "message_new",
                "object": {"message": message},
                "group_id": int(settings.VK_GROUP_ID),
                "event_id": f"backfill:{message.get('id')}",
            }
        )


async def _poll_once(server_state: dict) -> dict:
    wait_seconds = max(1, int(settings.VK_LONGPOLL_WAIT_SECONDS or 25))
    params = {
        "act": "a_check",
        "key": server_state["key"],
        "ts": server_state["ts"],
        "wait": wait_seconds,
        "mode": 2,
        "version": 3,
    }
    async with httpx.AsyncClient(timeout=wait_seconds + 10.0) as client:
        response = await client.get(server_state["server"], params=params)
        response.raise_for_status()
        return response.json()


async def vk_longpoll_worker() -> None:
    if not settings.VK_GROUP_TOKEN or not settings.VK_GROUP_ID:
        logger.info("VK long poll skipped: VK_GROUP_TOKEN or VK_GROUP_ID is empty")
        return

    logger.info("VK long poll worker starting")
    server_state: dict | None = None

    while True:
        try:
            if server_state is None:
                await _enable_longpoll_events()
                server_state = await _get_longpoll_server()
                logger.info("VK long poll server acquired")
                await _backfill_recent_incoming()

            payload = await _poll_once(server_state)
            failed = payload.get("failed")
            if failed:
                if failed == 1:
                    server_state["ts"] = payload.get("ts", server_state["ts"])
                    continue
                server_state = None
                logger.warning("VK long poll server refresh required: failed=%s", failed)
                continue

            server_state["ts"] = payload.get("ts", server_state["ts"])
            for update in payload.get("updates") or []:
                if update.get("type") != "message_new":
                    continue
                logger.info("VK long poll incoming: type=message_new event_id=%s", update.get("event_id"))
                await handlers.handle_event(
                    {
                        "type": "message_new",
                        "object": update.get("object") or {},
                        "group_id": int(settings.VK_GROUP_ID),
                        "event_id": update.get("event_id"),
                    }
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("VK long poll error: %s", error)
            log_api_error(f"VK long poll error: {error}")
            server_state = None
            await asyncio.sleep(5)
