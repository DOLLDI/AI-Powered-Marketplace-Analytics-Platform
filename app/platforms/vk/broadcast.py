import json
import logging

from app.services.db import list_platform_user_ids, set_user_blocked
from app.platforms.vk.vk_utils import VKAPIError, send_message, upload_message_photo


async def broadcast_vk(
    text: str,
    image_url: str | None = None,
    button_text: str | None = None,
    button_url: str | None = None,
):
    final_text = text
    attempted = 0
    sent = 0
    failed = 0
    keyboard = None
    if button_text and button_url:
        final_text = f"{final_text}\n\n{button_text}: {button_url}"
        keyboard = json.dumps(
            {
                "inline": True,
                "buttons": [[
                    {
                        "action": {
                            "type": "open_link",
                            "link": button_url,
                            "label": button_text,
                        }
                    }
                ]],
            },
            ensure_ascii=False,
        )
    attachment = None
    if image_url:
        try:
            attachment = await upload_message_photo(image_url)
        except Exception as error:
            logging.warning(f"[VK BROADCAST] image upload fallback for {image_url}: {error}")
            final_text = f"{final_text}\n\nИзображение: {image_url}"

    for peer_id in await list_platform_user_ids("vk"):
        attempted += 1
        try:
            resp = await send_message(
                int(peer_id),
                final_text,
                keyboard=keyboard,
                attachment=attachment,
            )
            sent += 1
            logging.info(f"[VK BROADCAST] sent to {peer_id}: {resp}")
        except Exception as error:
            if isinstance(error, VKAPIError):
                error_code = int(error.error.get("error_code") or 0)
                if error_code in {901, 902}:
                    await set_user_blocked("vk", str(peer_id), True)
                    logging.warning(f"[VK BROADCAST] marked {peer_id} as blocked after VK error {error_code}")
            failed += 1
            logging.error(f"[VK BROADCAST] error for {peer_id}: {error}")
    return {
        "platform": "vk",
        "attempted": attempted,
        "sent": sent,
        "failed": failed,
    }
