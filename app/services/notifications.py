from app.platforms.telegram.bot import send_message as send_telegram_message
from app.platforms.vk.bot import send_message as send_vk_message
from app.services.error_logger import log_api_error


async def notify_user(platform: str, user_id: str, text: str):
    try:
        if platform == "telegram":
            await send_telegram_message(int(user_id), text)
            return True
        if platform == "vk":
            await send_vk_message(int(user_id), text)
            return True
        raise ValueError(f"Unsupported platform for notification: {platform}")
    except Exception as error:
        log_api_error(f"Payment notification error for {platform}:{user_id}: {error}")
        return False


async def notify_payment_success(
    platform: str,
    user_id: str,
    title: str,
    amount: float,
    requests_added: int,
    provider: str,
    duration_days: int | None = None,
    receipt_url: str | None = None,
):
    text = (
        f"Оплата подтверждена.\n"
        f"Тариф: {title}\n"
        f"Сумма: {amount} ₽\n"
        f"Начислено проверок: {requests_added}\n"
        f"Провайдер: {provider}"
    )
    if duration_days:
        text += f"\nСрок действия: {duration_days} дней"
    if receipt_url:
        text += f"\nЧек: {receipt_url}"
    return await notify_user(platform, user_id, text)