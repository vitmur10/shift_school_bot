import asyncio
from aiogram import Bot
from aiogram.types import InputMediaVideo
import os
from dotenv import load_dotenv

load_dotenv()


async def test():
    bot = Bot(token=os.getenv("BOT_TOKEN"))
    file_id_1 = "BAACAgIAAxkBAAObajvVJ5GKrP0ytSGdoU_5JIfwDhoAAqGvAAITa-BJdDlWddpYWhI8BA"
    file_id_2 = "BAACAgIAAxkBAAO3ajvp92roxRT5YZNyT5CCncC4ficAAmWwAAITa-BJfBdg0quqjnw8BA"
    chat_id = 558530054

    try:
        result = await bot.send_media_group(
            chat_id=chat_id,
            media=[
                InputMediaVideo(media=file_id_1, caption="тест"),
                InputMediaVideo(media=file_id_2),
            ]
        )
        print("✅ Успішно:", result)
    except Exception as e:
        print("❌ Помилка:", e)
    finally:
        await bot.session.close()


asyncio.run(test())