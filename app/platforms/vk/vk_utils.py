from urllib.parse import urlparse
import random

import httpx

from app.services.config import settings
from app.services.error_logger import log_api_error

VK_API = "https://api.vk.com/method"
VK_MESSAGE_LIMIT = 3800


class VKAPIError(RuntimeError):
    def __init__(self, method: str, error: dict):
        self.method = method
        self.error = error
        super().__init__(f"VK API {method} error: {error}")


async def _call_vk_method(client: httpx.AsyncClient, method: str, data: dict):
    response = await client.post(
        f"{VK_API}/{method}",
        data={
            "access_token": settings.VK_GROUP_TOKEN,
            "v": "5.131",
            **data,
        },
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise VKAPIError(method, payload["error"])
    return payload.get("response")


def _guess_image_filename(image_url: str, content_type: str | None) -> str:
    parsed = urlparse(image_url)
    name = parsed.path.rsplit("/", 1)[-1] if parsed.path else ""
    if name:
        return name
    if content_type == "image/png":
        return "broadcast.png"
    if content_type == "image/webp":
        return "broadcast.webp"
    if content_type == "image/gif":
        return "broadcast.gif"
    return "broadcast.jpg"


def _build_vk_photo_attachment(photo_payload: list[dict] | dict) -> str:
    photo = photo_payload[0] if isinstance(photo_payload, list) else photo_payload
    owner_id = photo["owner_id"]
    photo_id = photo["id"]
    access_key = photo.get("access_key")
    attachment = f"photo{owner_id}_{photo_id}"
    if access_key:
        attachment = f"{attachment}_{access_key}"
    return attachment


async def upload_message_photo(image_url: str) -> str:
    async with httpx.AsyncClient(timeout=20.0) as client:
        upload_server = await _call_vk_method(client, "photos.getMessagesUploadServer", {})
        upload_url = upload_server.get("upload_url")
        if not upload_url:
            raise RuntimeError("VK upload server did not return upload_url")

        image_response = await client.get(image_url)
        image_response.raise_for_status()
        content_type = image_response.headers.get("content-type", "").split(";", 1)[0].strip().lower() or None
        if content_type and not content_type.startswith("image/"):
            raise RuntimeError(f"Unsupported image content type: {content_type}")

        upload_response = await client.post(
            upload_url,
            files={
                "photo": (
                    _guess_image_filename(image_url, content_type),
                    image_response.content,
                    content_type or "image/jpeg",
                )
            },
        )
        upload_response.raise_for_status()
        upload_payload = upload_response.json()
        saved = await _call_vk_method(
            client,
            "photos.saveMessagesPhoto",
            {
                "photo": upload_payload["photo"],
                "server": upload_payload["server"],
                "hash": upload_payload["hash"],
            },
        )
        return _build_vk_photo_attachment(saved)


async def send_message(
    peer_id: int,
    text: str,
    keyboard: str | None = None,
    attachment: str | None = None,
):
    chunks = []
    text = text or ""
    while len(text) > VK_MESSAGE_LIMIT:
        split_at = text.rfind("\n", 0, VK_MESSAGE_LIMIT)
        if split_at < 500:
            split_at = VK_MESSAGE_LIMIT
        chunks.append(text[:split_at].rstrip())
        text = text[split_at:].lstrip()
    chunks.append(text)

    first_response = None
    first = True
    async with httpx.AsyncClient(timeout=10.0) as client:
        for chunk in chunks:
            params = {
                'peer_id': peer_id,
                'message': chunk,
                'random_id': random.randint(1, 2_147_483_647),
            }
            if first and keyboard:
                params['keyboard'] = keyboard
            if first and attachment:
                params['attachment'] = attachment
            try:
                response = await _call_vk_method(client, "messages.send", params)
            except VKAPIError as error:
                if first and keyboard:
                    log_api_error(
                        "VK keyboard send failed: "
                        f"peer_id={peer_id}, error_code={error.error.get('error_code')}, "
                        f"error_msg={error.error.get('error_msg')}"
                    )
                raise
            if first_response is None:
                first_response = response
            first = False
        return {"response": first_response}

async def edit_message(peer_id: int, message_id: int, text: str):
    params = {
        'peer_id': peer_id,
        'message_id': message_id,
        'message': text
    }
    async with httpx.AsyncClient(timeout=10.0) as client:
        return await _call_vk_method(client, "messages.edit", params)
