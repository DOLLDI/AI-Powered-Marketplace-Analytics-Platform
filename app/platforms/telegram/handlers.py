from aiogram.types import Update

from app.platforms.telegram.aiogram_bot import bot, dp

async def handle_update(data: dict):
    update = Update.model_validate(data)
    await dp.feed_update(bot, update)
