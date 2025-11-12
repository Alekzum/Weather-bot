import time
import httpx
import asyncio
import hashlib
from aiogram import types, Router, Bot, F
from aiogram.enums import ParseMode
from aiogram.utils.keyboard import InlineKeyboardBuilder
from utils.weather_utils import get_current_weather_async, get_location_async
from utils.image_utils import create_weather_card_async
from utils.file_utils import (
    generate_random_filename,
    cleanup_files,
    upload_to_website,
    generate_random_ip,
)
from io import BytesIO
from config import IMGBB_API_KEY
from structlog import get_logger


rt = Router(name=__name__)
logger = get_logger(__name__)


@rt.inline_query(
    F.func(lambda i: i.query and i.query.strip().lower() or "").as_("location")
)
async def inline_weather_query(
    query: types.InlineQuery, bot: Bot, location: str
):
    """Асинхронный обработчик инлайн запросов погоды"""
    logger.debug(
        "Пришёл инлайн-запрос",
        query=repr(query.query),
        from_user_id=query.from_user.id,
        from_user_name=repr(query.from_user.full_name),
    )

    bot_me = await bot.get_me()
    bot_username = bot_me.username
    if bot_username is None:
        raise RuntimeError("Почему у бота нет псевдонима?")  # just for IDE :(

    if not location:
        results = generate_article(
            id="help",
            title="Как использовать бота?",
            description=f"Введите @{bot_username} локация",
            message_text=HELP_MESSAGE.format(bot_username=bot_username),
        )
        await query.answer(results, cache_time=30)  # type: ignore[arg-type]
        return

    try:
        await _inline_weather_query(
            query=query, bot_username=bot_username, location=location
        )

    except Exception as ex:
        logger.error(
            "Неизвестная ошибка.",
            caught_exception=(repr(ex), str(ex)),
            exc_info=True,
        )
        results = generate_article(
            id="fallback",
            title="Погода",
            description=location,
            message_text=f"<b>@{bot_username}</b>",
        )
        await query.answer(results, cache_time=1)  # type: ignore[arg-type]


@rt.chosen_inline_result(
    F.result_id.split(":")[0] == "gw",
    F.result_id.split(":")[1].as_("country_code_"),
    F.result_id.split(":")[2].as_("city"),
)
async def chosen_inline_weather_result(
    cir: types.ChosenInlineResult,
    bot: Bot,
    country_code_: str,
    city: str,
):
    country_code = country_code_ if country_code_ != "." else None
    logger.debug(
        "Getting weather from CIR", city=city, country_code=country_code
    )
    start_time = time.time()
    query = cir.query

    weather_data = await get_current_weather_async(city, country_code)
    if not weather_data:
        logger.warn(
            "Не удалось получить погоду",
            city=city,
            country_code=country_code,
        )
        await bot.edit_message_caption(
            inline_message_id=cir.inline_message_id,
            caption="Не удалось получить погоду",
        )
        return

    image_url = await generate_image(weather_data=weather_data)

    if query.strip().lower() == "random":
        title = f"Случайная погода в {weather_data['city']}"
        description = f"Случайный IP | {weather_data['temp']:+.1f}°C, {weather_data['description']}"
    else:
        title = f"Погода в {weather_data['city']}"
        description = (
            f"{weather_data['temp']:+.1f}°C, {weather_data['description']}"
        )

    await bot.edit_message_media(
        inline_message_id=cir.inline_message_id,
        media=types.InputMediaPhoto(media=image_url),
    )
    await bot.edit_message_caption(
        inline_message_id=cir.inline_message_id,
        caption=title + "\n" + description,
    )

    elapsed_time = time.time() - start_time
    if query.strip().lower() == "random":
        logger.info(
            "Случайная погода обработана.",
            elapsed_time=elapsed_time,
            city=weather_data["city"],
            country=weather_data["country"],
            temperature=weather_data["temp"],
            city_type="random",
        )
    else:
        logger.info(
            "Запрос обработан",
            elapsed_time=elapsed_time,
            city=weather_data["city"],
            country=weather_data.get("country"),
            temperature=weather_data["temp"],
            city_type="specified",
        )
    pass


@rt.chosen_inline_result(
    F.result_id.split(":")[0] == "gw",
)
async def chosen_inline_weather_skip(
    cir: types.ChosenInlineResult,
):
    logger.warn("Skipped CIR with gw", result_id=cir.result_id)


async def _inline_weather_query(
    query: types.InlineQuery, bot_username: str, location: str
):
    # Обработка команды random
    start_time = time.time()
    user_query = query.query.strip().lower()
    is_ip = user_query.count(".") == 4
    city = country_code = None

    if user_query == "random":
        i = 0
        while i < 3:
            random_ip = generate_random_ip()
            logger.debug(
                f"{'Повторно с' if i != 0 else 'С'}генерирован случайный IP",
                random_ip=random_ip,
            )
            city, country_code = await get_location_async(random_ip)

            if city:
                break
            i += 1
        else:
            results = generate_article(
                id="random_error",
                title="Случайная погода",
                description="Не удалось найти случайную локацию, попробуйте еще раз",
                message_text=(
                    "Не удалось найти случайную локацию\n\n"
                    "Попробуйте еще раз: <code>@{bot_username} random</code></b>"
                ),
            )
            await query.answer(results, cache_time=1)  # type: ignore[arg-type]
            elapsed_time = time.time() - start_time
            logger.warn(
                "Ошибка генерации IP отправлена", elapsed_time=elapsed_time
            )
            return

        user_query = random_ip
        is_ip = True

    if is_ip:
        city, country_code = await get_location_async(user_query)

        if not city:
            results = generate_article(
                id="ip_error",
                title="Ошибка определения местоположения",
                description=f"IP {user_query} не найден",
                message_text=(
                    "❌ IP <code>{}</code> не найден\n\n"
                    "Проверьте IP адрес и повторите попытку\n\n<b>@{}</b>"
                ).format(user_query, bot_username),
            )
            await query.answer(results, cache_time=1)  # type: ignore[arg-type]
            elapsed_time = time.time() - start_time
            logger.warn("Ошибка IP отправлена", elapsed_time=elapsed_time)
            return

    else:
        city = user_query

    result_id = ":".join(
        (
            "gw",
            country_code if country_code is not None else ".",
            city,
            # generate_result_id(
            #     city,
            #     int(time.time()),
            # ),
        )
    )
    results = [
        types.InlineQueryResultPhoto(
            id=result_id,
            thumbnail_url="https://chuhan.lol/icon.jpg",
            photo_url="https://chuhan.lol/icon.jpg",
            title=f'Запросить погоду для "{city}"',
            description="При выборе бот начнёт получать погоду для города, затем обновит сообщение",
            caption=f'Загрузка погоды для "{city}"',
            parse_mode=ParseMode.HTML,
            photo_width=1600,
            photo_height=1000,
            reply_markup=InlineKeyboardBuilder()
            .button(text="...", callback_data="unhandled")
            .as_markup(),
        )
    ]

    elapsed_time = time.time() - start_time
    logger.debug(
        "Обрабатывается первичный запрос на погоду...",
        elapsed_time=elapsed_time,
        result_id=result_id,
    )
    await query.answer(results, cache_time=3)  # type: ignore[arg-type]

    elapsed_time = time.time() - start_time
    logger.debug(
        "Обработан первичный запрос на погоду...",
        elapsed_time=elapsed_time,
        result_id=result_id,
    )
    return


def generate_result_id(city: str, timestamp: float):
    """Генерация ID для инлайна"""
    base_string = f"{city}_{timestamp}"
    return hashlib.md5(base_string.encode()).hexdigest()[:64]


async def upload_to_imgbb(image_io: BytesIO):  # well... why not :shrug:
    """Асинхронная загрузка на imgbb"""
    try:
        async with httpx.AsyncClient() as client:
            url = "https://api.imgbb.com/1/upload"
            response = await client.post(
                url, data=dict(key=IMGBB_API_KEY), files=dict(image=image_io)
            )
            if response.status_code == 200:
                result = response.json()
                return result["data"]["url"]
            else:
                logger.error(
                    "Ошибка от imgbb",
                    response=response,
                    status_code=response.status_code,
                    content=response.content,
                )
                return None
    except Exception as ex:
        logger.error(
            "Ошибка загрузки на imgbb",
            caught_exception=(repr(ex), str(ex)),
            exc_info=True,
        )
        return None


def generate_article(id: str, title: str, description: str, message_text: str):
    result_id = generate_result_id(id, int(time.time()))
    return (
        types.InlineQueryResultArticle(
            id=result_id,
            title=title,
            description=description,
            input_message_content=types.InputTextMessageContent(
                message_text=message_text,
                parse_mode=ParseMode.HTML,
            ),
            thumb_url="https://chuhan.lol/icon.jpg",
            thumb_width=64,
            thumb_height=64,
        ),
    )


async def generate_image(weather_data: dict):
    # Генерируем файлы
    timestamp = int(time.time())
    local_filename = generate_random_filename(prefix=f"weather_{timestamp}")
    website_filename = local_filename
    # local_filepath = f"templates/{local_filename}"

    # Асинхронное создание карточки
    card_created, card_io = await create_weather_card_async(weather_data)

    if not card_created:
        raise RuntimeError("Didn't created card!")
    elif card_io is None:
        raise RuntimeError("Didn't created card's BytesIO!")

    imgbb_task = asyncio.create_task(upload_to_imgbb(card_io))
    await upload_to_website(card_io, website_filename)

    image_url = await imgbb_task

    if not image_url:
        image_url = f"https://chuhan.lol/{website_filename}"
    cleanup_files(website_filename)
    return image_url


HELP_MESSAGE = (
    "🌤️ <b>Погодник</b>\n\n"
    "Чтобы узнать погоду, введите:\n"
    "<code>@{bot_username} локация</code>\n"
    "<code>@{bot_username} IP</code>\n"
    "<code>@{bot_username} random</code>\n\n"
    "Пример: <code>@{bot_username} Москва</code>"
)
