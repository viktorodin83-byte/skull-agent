# -*- coding: utf-8 -*-
"""
Telegram-бот "Универсальный помощник"
- Отвечает на вопросы
- Ищет информацию в интернете (DuckDuckGo, бесплатно)
- Анализирует найденное через Claude API
- Резерв: Google Custom Search (100 запросов/день бесплатно)

Автор: собран для Кубы
"""

import asyncio
import io
import logging
import os
import tempfile
import time

import requests
from dotenv import load_dotenv
from duckduckgo_search import DDGS
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

load_dotenv()

# ========== НАСТРОЙКИ (значения берутся из .env, см. .env.example) ==========

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")

# Ключ для распознавания голосовых сообщений через OpenAI Whisper API
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Резервный поиск через Google (можно оставить пустым, если не настроено)
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.getenv("GOOGLE_CSE_ID", "")

# Сколько результатов поиска брать
MAX_SEARCH_RESULTS = 5

# Apify: поиск цен на маркетплейсах через actor ru-marketplaces-price-monitor
APIFY_API_TOKEN = os.getenv("APIFY_API_TOKEN")
APIFY_ACTOR_ID = "isolovyev/ru-marketplaces-price-monitor"
APIFY_MAX_ITEMS = 8
APIFY_MAX_WAIT_SECONDS = 180
APIFY_POLL_INTERVAL_SECONDS = 5

# Слова-триггеры, по которым сообщение считается поиском товара/цены
PRICE_INTENT_KEYWORDS = [
    "цена", "цены", "цену", "цен", "стоит", "стоимость", "почём", "почем",
    "купить", "дешевле", "дешёвый", "дешевый", "озон", "ozon", "авито", "avito",
]

# Слова, при которых "цена"/"стоимость" — не про товар на маркетплейсе, а про
# биржевой/финансовый актив (золото, валюта, крипта, акции и т.п.). При них
# на Ozon/Avito не идём, даже если PRICE_INTENT_KEYWORDS сработали.
PRICE_INTENT_ASSET_EXCLUDE_KEYWORDS = [
    "золот", "xau", "серебр", "нефт", "биткоин", "bitcoin", "btc", "эфир", "ethereum",
    "курс валют", "валютный курс", "акци", "фондов", "график цен",
]

# Слова-триггеры, по которым сообщение считается запросом на поиск и отправку картинки
IMAGE_INTENT_VERBS = [
    "покажи", "пришли", "пришлите", "скинь", "скиньте", "найди", "найдите",
    "отправь", "отправьте", "хочу увидеть", "хочу посмотреть",
]
IMAGE_INTENT_NOUNS = ["картинк", "фото", "фотк", "изображени", "снимок", "снимки"]

MAX_IMAGE_RESULTS = 5

# =============================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def search_duckduckgo(query: str, max_results: int = MAX_SEARCH_RESULTS) -> list:
    """Ищет информацию через DuckDuckGo. Возвращает список результатов."""
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results, region="ru-ru"):
                results.append({
                    "title": r.get("title", ""),
                    "body": r.get("body", ""),
                    "href": r.get("href", ""),
                })
    except Exception as e:
        logger.error(f"Ошибка поиска DuckDuckGo: {e}")
    return results


def search_google(query: str, max_results: int = MAX_SEARCH_RESULTS) -> list:
    """Резервный поиск через Google Custom Search API."""
    if not GOOGLE_API_KEY or not GOOGLE_CSE_ID:
        return []

    results = []
    try:
        url = "https://www.googleapis.com/customsearch/v1"
        params = {
            "key": GOOGLE_API_KEY,
            "cx": GOOGLE_CSE_ID,
            "q": query,
            "num": min(max_results, 10),
        }
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        for item in data.get("items", []):
            results.append({
                "title": item.get("title", ""),
                "body": item.get("snippet", ""),
                "href": item.get("link", ""),
            })
    except Exception as e:
        logger.error(f"Ошибка поиска Google: {e}")
    return results


def get_search_results(query: str) -> list:
    """Сначала пробуем DuckDuckGo, если пусто — Google (если настроен)."""
    results = search_duckduckgo(query)
    if not results:
        logger.info("DuckDuckGo пустой, пробуем Google...")
        results = search_google(query)
    return results


def format_search_context(results: list) -> str:
    """Форматирует результаты поиска в текст для передачи модели."""
    if not results:
        return "Результаты поиска не найдены."

    parts = []
    for i, r in enumerate(results, 1):
        parts.append(
            f"[{i}] {r['title']}\n{r['body']}\nИсточник: {r['href']}"
        )
    return "\n\n".join(parts)


def looks_like_price_search(text: str) -> bool:
    """Эвристика: похоже ли сообщение на поиск товара/цены на маркетплейсе."""
    lowered = text.lower()
    if any(keyword in lowered for keyword in PRICE_INTENT_ASSET_EXCLUDE_KEYWORDS):
        return False
    return any(keyword in lowered for keyword in PRICE_INTENT_KEYWORDS)


def looks_like_image_request(text: str) -> bool:
    """Эвристика: просит ли пользователь найти и прислать картинку."""
    lowered = text.lower()
    has_verb = any(v in lowered for v in IMAGE_INTENT_VERBS)
    has_noun = any(n in lowered for n in IMAGE_INTENT_NOUNS)
    return has_verb and has_noun


def extract_image_query(text: str) -> str:
    """Убирает слова-триггеры из фразы, оставляя предмет поиска картинки.

    Слова-стемы (например "картинк" для картинка/картинку) сравниваются
    с целым словом, а не ищутся как подстрока — иначе можно случайно
    откусить кусок неродственного слова (например "фото" внутри
    "фотографию" -> осталось бы бессмысленное "графию"). Разрешаем
    короткий "хвост" (падежное окончание, до 3 символов), чтобы стем
    всё ещё ловил склонения ("картинк" -> "картинку", "картинки").
    """
    MAX_SUFFIX = 3
    lowered = text.lower()

    # Многословные фразы-триггеры безопасно снимаем как подстроку — пробел
    # внутри не даёт случайно задеть середину другого слова.
    for phrase in IMAGE_INTENT_VERBS:
        if " " in phrase:
            lowered = lowered.replace(phrase, " ")

    single_triggers = [t for t in IMAGE_INTENT_VERBS if " " not in t] + IMAGE_INTENT_NOUNS
    kept_words = []
    for word in lowered.split():
        stripped = word.strip(".,!?:;\"'()")
        is_trigger = any(
            stripped == trig or (stripped.startswith(trig) and len(stripped) - len(trig) <= MAX_SUFFIX)
            for trig in single_triggers
        )
        if not is_trigger:
            kept_words.append(word)

    return " ".join(kept_words).strip()


def refine_image_query(raw_query: str) -> dict:
    """Просит Claude превратить сырой запрос в точный поисковый запрос на английском,
    достроив разумные умолчания (как понял бы обычный человек), и список
    слов-исключений для нежелательных вариантов, которые пользователь не просил.
    Возвращает {"query": str, "exclude": list[str]}. При сбое — {"query": raw_query, "exclude": []}.
    """
    import json
    import re

    fallback = {"query": raw_query, "exclude": []}
    if not raw_query:
        return fallback

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    system_prompt = (
        "Пользователь просит найти картинку. Он мог не уточнить детали — тогда "
        "выбирай то, что предположил бы обычный человек по умолчанию: приятная, "
        "красивая, дневная сцена в тёплое время года, без снега/льда/руин/разрухи "
        "и других неожиданных вариаций, если только пользователь явно не попросил "
        "именно их. Переведи запрос на английский язык (фотобанки лучше индексируют "
        "английские теги).\n\n"
        "Ответь СТРОГО в формате JSON без пояснений и без markdown-разметки:\n"
        '{"query": "краткий точный запрос на английском, 3-6 слов", '
        '"exclude": ["0-5 английских слов для нежелательных вариантов"]}'
    )

    payload = {
        "model": "claude-sonnet-5",
        "max_tokens": 200,
        "system": system_prompt,
        "messages": [{"role": "user", "content": raw_query}],
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        text_parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        raw_text = "\n".join(text_parts)
        match = re.search(r"\{.*\}", raw_text, re.DOTALL)
        if not match:
            return fallback
        parsed = json.loads(match.group(0))
        query = str(parsed.get("query") or raw_query).strip()
        exclude = [str(w).lower() for w in parsed.get("exclude", []) if str(w).strip()]
        return {"query": query or raw_query, "exclude": exclude}
    except Exception as e:
        logger.warning(f"Не удалось уточнить запрос картинки через Claude: {e}")
        return fallback


def search_images_openverse(query: str, max_results: int = MAX_IMAGE_RESULTS) -> list:
    """Ищет картинки через Openverse (бесплатные CC-лицензированные изображения, без ключа)."""
    results = []
    try:
        resp = requests.get(
            "https://api.openverse.org/v1/images/",
            params={"q": query, "page_size": max_results},
            timeout=15,
        )
        resp.raise_for_status()
        for item in resp.json().get("results", [])[:max_results]:
            url = item.get("url")
            if url:
                results.append({"url": url, "title": item.get("title", "")})
    except Exception as e:
        logger.error(f"Ошибка поиска картинок Openverse: {e}")
    return results


def search_images_ddg(query: str, max_results: int = MAX_IMAGE_RESULTS) -> list:
    """Резервный поиск картинок через DuckDuckGo (иногда упирается в rate limit)."""
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.images(query, region="ru-ru", max_results=max_results):
                url = r.get("image")
                if url:
                    results.append({"url": url, "title": r.get("title", "")})
    except Exception as e:
        logger.error(f"Ошибка поиска картинок DuckDuckGo: {e}")
    return results


def _filter_excluded(results: list, exclude: list) -> list:
    """Отбрасывает результаты, чьё название содержит нежелательные слова."""
    if not exclude:
        return results
    filtered = [
        r for r in results
        if not any(word in r.get("title", "").lower() for word in exclude)
    ]
    return filtered or results  # если отфильтровали всё подчистую — лучше что-то, чем ничего


def get_image_results(query: str, exclude: list | None = None) -> list:
    """Сначала пробуем Openverse (стабильнее), если пусто — DuckDuckGo.
    Берём с запасом (вдвое больше) и отсеиваем нежелательные варианты по exclude.
    """
    fetch_count = MAX_IMAGE_RESULTS * 2
    results = search_images_openverse(query, max_results=fetch_count)
    if not results:
        logger.info("Openverse пустой, пробуем DuckDuckGo Images...")
        results = search_images_ddg(query, max_results=fetch_count)
    return _filter_excluded(results, exclude or [])[:MAX_IMAGE_RESULTS]


def search_marketplaces(query: str) -> list:
    """Ищет товары на Ozon и Avito через Apify actor ru-marketplaces-price-monitor.

    Блокирующая функция (использует requests) — вызывать через asyncio.to_thread,
    так как actor может скрапить несколько минут.
    """
    if not APIFY_API_TOKEN:
        return []

    actor_path = APIFY_ACTOR_ID.replace("/", "~")
    params = {"token": APIFY_API_TOKEN}
    payload = {
        "mode": "search",
        "platforms": ["ozon", "avito"],
        "queries": [query],
        "maxPagesPerQuery": 1,
        "maxItemsPerQuery": APIFY_MAX_ITEMS,
        "proxyConfiguration": {
            "useApifyProxy": True,
            "apifyProxyGroups": ["RESIDENTIAL"],
            "apifyProxyCountry": "RU",
        },
    }

    try:
        resp = requests.post(
            f"https://api.apify.com/v2/acts/{actor_path}/runs",
            params=params,
            json=payload,
            timeout=30,
        )
        resp.raise_for_status()
        run_id = resp.json()["data"]["id"]
    except Exception as e:
        logger.error(f"Ошибка запуска Apify actor: {e}")
        return []

    status_url = f"https://api.apify.com/v2/actor-runs/{run_id}"
    dataset_id = None
    elapsed = 0
    while elapsed < APIFY_MAX_WAIT_SECONDS:
        time.sleep(APIFY_POLL_INTERVAL_SECONDS)
        elapsed += APIFY_POLL_INTERVAL_SECONDS
        try:
            resp = requests.get(status_url, params=params, timeout=15)
            resp.raise_for_status()
            run_data = resp.json()["data"]
        except Exception as e:
            logger.error(f"Ошибка проверки статуса Apify run: {e}")
            continue

        dataset_id = run_data.get("defaultDatasetId")
        if run_data.get("status") in ("SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"):
            break
    else:
        logger.warning("Apify actor не успел завершиться за отведённое время.")

    if not dataset_id:
        return []

    try:
        resp = requests.get(
            f"https://api.apify.com/v2/datasets/{dataset_id}/items",
            params={**params, "limit": APIFY_MAX_ITEMS},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.error(f"Ошибка получения результатов Apify: {e}")
        return []


def format_marketplace_results(items: list) -> str:
    """Форматирует товары с Ozon/Avito в текст для передачи модели."""
    if not items:
        return "Товары не найдены."

    parts = []
    for i, item in enumerate(items, 1):
        price = item.get("price")
        currency = item.get("currency", "RUB")
        price_str = f"{price} {currency}" if price is not None else "цена не указана"
        parts.append(
            f"[{i}] {item.get('title', '')}\n"
            f"Цена: {price_str} ({item.get('platform', '')})\n"
            f"Ссылка: {item.get('url', '')}"
        )
    return "\n\n".join(parts)


def ask_claude(user_question: str, search_context: str) -> str:
    """Отправляет вопрос + контекст поиска в Claude API, возвращает ответ."""
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    system_prompt = (
        "Ты — полезный помощник в Telegram. Отвечай на русском языке, "
        "кратко и по делу. Используй предоставленные результаты поиска, "
        "чтобы дать точный и актуальный ответ. Если информации в результатах "
        "недостаточно, скажи об этом честно и ответь по своим знаниям."
    )

    user_message = (
        f"Вопрос пользователя: {user_question}\n\n"
        f"Результаты поиска в интернете:\n{search_context}"
    )

    payload = {
        "model": "claude-sonnet-5",
        "max_tokens": 1024,
        "system": system_prompt,
        "messages": [{"role": "user", "content": user_message}],
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        content_blocks = data.get("content", [])
        text_parts = [b["text"] for b in content_blocks if b.get("type") == "text"]
        return "\n".join(text_parts) if text_parts else "Не удалось получить ответ от модели."
    except requests.exceptions.HTTPError as e:
    	logger.error(f"Ошибка запроса к Claude API: {e}. Ответ сервера: {resp.text}")
    	return f"Ошибка при обращении к Claude API: {resp.text}"
    except Exception as e:
    	logger.error(f"Ошибка запроса к Claude API: {e}")
    	return f"Ошибка при обращении к Claude API: {e}"

def ask_claude_vision(image_bytes: bytes, media_type: str, caption: str = "") -> str:
    """Отправляет фото в Claude API (vision) и просит описать, что на нём изображено."""
    import base64

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    system_prompt = (
        "Ты — помощник, который распознаёт объекты на фотографиях. Отвечай на "
        "русском языке, кратко. Опиши, что изображено на фото, и явно укажи, "
        "дерево это, метла или что-то другое."
    )

    user_text = caption.strip() or "Что изображено на этом фото? Это дерево или метла?"

    payload = {
        "model": "claude-sonnet-5",
        "max_tokens": 512,
        "system": system_prompt,
        "messages": [{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    },
                },
                {"type": "text", "text": user_text},
            ],
        }],
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        content_blocks = data.get("content", [])
        text_parts = [b["text"] for b in content_blocks if b.get("type") == "text"]
        return "\n".join(text_parts) if text_parts else "Не удалось распознать изображение."
    except requests.exceptions.HTTPError as e:
        logger.error(f"Ошибка запроса к Claude Vision: {e}. Ответ сервера: {resp.text}")
        return f"Ошибка при распознавании фото: {resp.text}"
    except Exception as e:
        logger.error(f"Ошибка запроса к Claude Vision: {e}")
        return f"Ошибка при распознавании фото: {e}"


def transcribe_voice(file_path: str) -> str:
    """Распознаёт речь в аудиофайле через OpenAI Whisper API."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан — распознавание голоса недоступно.")

    url = "https://api.openai.com/v1/audio/transcriptions"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}"}

    with open(file_path, "rb") as f:
        files = {"file": (os.path.basename(file_path), f, "audio/ogg")}
        data = {"model": "whisper-1"}
        resp = requests.post(url, headers=headers, files=files, data=data, timeout=60)
    resp.raise_for_status()
    return resp.json().get("text", "").strip()


def synthesize_speech(text: str) -> bytes:
    """Озвучивает текст через OpenAI TTS API, возвращает аудио в формате OGG/Opus."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY не задан — синтез речи недоступен.")

    url = "https://api.openai.com/v1/audio/speech"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "tts-1",
        "voice": "nova",
        "input": text[:4000],
        "response_format": "opus",
    }
    resp = requests.post(url, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    return resp.content


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет, Куба! Я твой помощник. Пиши любой вопрос, либо отправь "
        "голосовое сообщение — найду информацию в интернете и дам развёрнутый ответ."
    )


async def get_context_for_question(update: Update, user_question: str) -> str:
    """Определяет источник контекста: маркетплейсы (Ozon/Avito) или обычный веб-поиск."""
    if looks_like_price_search(user_question):
        await update.message.reply_text("Ищу цены на Ozon и Avito, это может занять пару минут...")
        items = await asyncio.to_thread(search_marketplaces, user_question)
        if items:
            return format_marketplace_results(items)
        logger.info("Apify не нашёл товаров, переключаюсь на обычный поиск.")

    results = await asyncio.to_thread(get_search_results, user_question)
    return format_search_context(results)


async def answer_question(update: Update, chat_id: int, user_question: str, reply_as_voice: bool = False):
    """Ищет информацию по вопросу и отправляет ответ Claude пользователю."""
    # 1. Ищем информацию (маркетплейсы или обычный веб-поиск)
    search_context = await get_context_for_question(update, user_question)

    # 2. Отправляем в Claude для анализа и формулировки ответа
    answer = await asyncio.to_thread(ask_claude, user_question, search_context)

    # 3. Отправляем ответ пользователю (голосом, если вопрос тоже был голосовым)
    if reply_as_voice:
        try:
            audio_bytes = synthesize_speech(answer)
            voice_file = io.BytesIO(audio_bytes)
            voice_file.name = "reply.ogg"
            await update.message.reply_voice(voice=voice_file)
            return
        except Exception as e:
            logger.error(f"Ошибка синтеза речи: {e}")
            await update.message.reply_text(
                "Не удалось озвучить ответ, отправляю текстом.\n\n" + answer
            )
            return

    await update.message.reply_text(answer)


async def handle_image_request(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str):
    """Ищет картинку в интернете по запросу пользователя и отправляет в чат.

    Перед поиском прогоняет сырой запрос через Claude, чтобы достроить разумные
    умолчания (человек говорит "озеро" — подразумевает красивое летнее озеро, а не
    первое попавшееся, включая замёрзшее) и получить более точный поисковый запрос
    на английском.
    """
    raw_query = extract_image_query(text)
    if not raw_query:
        await update.message.reply_text("Что именно найти на картинке? Уточни запрос.")
        return

    refined = await asyncio.to_thread(refine_image_query, raw_query)
    query, exclude = refined["query"], refined["exclude"]

    await update.message.reply_text(f"Ищу картинку: {raw_query}...")
    await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")

    results = await asyncio.to_thread(get_image_results, query, exclude)
    if not results:
        await update.message.reply_text("Не нашёл подходящих картинок, попробуй другой запрос.")
        return

    for item in results:
        try:
            # Telegram сам скачивает картинку по URL перед ответом — стандартный
            # read_timeout в 5с у PTB может не хватить для крупных файлов.
            await update.message.reply_photo(
                photo=item["url"], caption=item.get("title") or query, read_timeout=25,
            )
            return
        except Exception as e:
            logger.warning(f"Не удалось отправить картинку {item['url']}: {e}")
            continue

    await update.message.reply_text("Нашёл картинки, но ни одну не удалось отправить. Попробуй ещё раз.")


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_question = update.message.text
    chat_id = update.effective_chat.id

    reply_to = update.message.reply_to_message
    if reply_to and reply_to.photo:
        edit_ops = parse_edit_ops(user_question)
        if edit_ops:
            await apply_and_send_edit(update, context, chat_id, reply_to.photo[-1], edit_ops)
            return

    if looks_like_image_request(user_question):
        await handle_image_request(update, context, chat_id, user_question)
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    await answer_question(update, chat_id, user_question)


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    voice = update.message.voice or update.message.audio

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")

    tg_file = await context.bot.get_file(voice.file_id)

    with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        await tg_file.download_to_drive(tmp_path)
        try:
            user_question = transcribe_voice(tmp_path)
        except Exception as e:
            logger.error(f"Ошибка распознавания голоса: {e}")
            await update.message.reply_text(
                "Не удалось распознать голосовое сообщение. Попробуй ещё раз или напиши текстом."
            )
            return
    finally:
        os.remove(tmp_path)

    if not user_question:
        await update.message.reply_text("Не удалось разобрать речь в сообщении.")
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="record_voice")
    await answer_question(update, chat_id, user_question, reply_as_voice=True)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    caption = update.message.caption or ""

    edit_ops = parse_edit_ops(caption)
    if edit_ops:
        await apply_and_send_edit(update, context, chat_id, update.message.photo[-1], edit_ops)
        return

    await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")

    photo = update.message.photo[-1]  # самое большое разрешение
    tg_file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await tg_file.download_as_bytearray())

    answer = await asyncio.to_thread(ask_claude_vision, image_bytes, "image/jpeg", caption)
    await update.message.reply_text(answer)


# ======================================================================
# IMAGE EDIT NODE — полностью независимый блок, не связан с остальным ботом.
# Всё редактирование делается локально через Pillow — бесплатно, без внешних
# платных API и без скачивания ML-моделей. Можно вырезать целиком (вместе
# со строкой регистрации в main() и вызовами из handle_photo/handle_message)
# — ничего больше в файле не сломается.
#
# Как пользоваться:
#   - Отправить фото с подписью-инструкцией ("сделай чёрно-белым", "поверни
#     на 90", "восстанови качество" и т.д.) — бот пришлёт отредактированную
#     версию.
#   - Или ответить (reply) текстом-инструкцией на уже отправленное в чат
#     фото (своё или от бота).
#
# "Восстановление" (restore) — это автокоррекция контраста, лёгкое
# устранение шума и повышение резкости плюс апскейл мелких фото, а не
# нейросетевая реконструкция (снятие царапин, колоризация) — та требует
# тяжёлых ML-моделей и здесь сознательно не подключена, чтобы не тянуть
# гигабайты зависимостей.
# ======================================================================

_EDIT_CANONICAL_ORDER = [
    "restore", "grayscale", "sepia", "invert", "mirror", "flip_vertical",
    "rotate", "upscale", "brighten", "darken", "contrast", "sharpen", "blur",
]

_EDIT_OP_TRIGGERS = [
    ("grayscale", ["черно-бел", "чёрно-бел", "черно белы", "чёрно белы", "оттенки серого", " чб ", "чб фото"]),
    ("invert", ["негатив", "инверт"]),
    ("sepia", ["сепия"]),
    ("mirror", ["зеркал", "отрази", "отражение"]),
    ("flip_vertical", ["переверни", "вверх ногами"]),
    ("brighten", ["увеличь яркост", "повысь яркост", "добавь яркост", "ярче"]),
    ("darken", ["убавь яркост", "уменьши яркост", "темнее"]),
    ("contrast", ["контраст"]),
    ("sharpen", ["резкост", "четч", "чётк"]),
    ("blur", ["размыт", "блюр", "расфокус"]),
    ("rotate", ["поверни", "разверни", "вращ"]),
    ("upscale", ["увеличь размер", "увеличь фото", "апскейл", "укрупни", "растян", "в 2 раза", "в 3 раза", "в 4 раза"]),
    ("restore", ["восстанов", "реставра", "почини", "улучши", "убери шум", "исправь фото", "качеств"]),
]

_EDIT_OP_LABELS = {
    "restore": "восстановление/улучшение",
    "grayscale": "чёрно-белое",
    "sepia": "сепия",
    "invert": "негатив",
    "mirror": "зеркальное отражение",
    "flip_vertical": "переворот",
    "rotate": "поворот",
    "upscale": "увеличение размера",
    "brighten": "ярче",
    "darken": "темнее",
    "contrast": "контраст",
    "sharpen": "резкость",
    "blur": "размытие",
}


def parse_edit_ops(text: str) -> list:
    """Разбирает текст-инструкцию на список операций редактирования (в каноническом порядке применения)."""
    if not text:
        return []
    lowered = f" {text.lower()} "
    matched = set()
    for op, triggers in _EDIT_OP_TRIGGERS:
        if any(t in lowered for t in triggers):
            matched.add(op)
    return [op for op in _EDIT_CANONICAL_ORDER if op in matched]


def apply_edit_ops(image_bytes: bytes, ops: list, instruction_text: str = "") -> bytes:
    """Применяет операции редактирования к изображению через Pillow. Блокирующая функция."""
    import re
    from PIL import Image, ImageOps, ImageEnhance, ImageFilter

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    for op in ops:
        if op == "restore":
            img = ImageOps.autocontrast(img, cutoff=1)
            img = img.filter(ImageFilter.MedianFilter(size=3))
            img = ImageEnhance.Sharpness(img).enhance(1.5)
            if max(img.width, img.height) < 1000:
                img = img.resize((img.width * 2, img.height * 2), Image.LANCZOS)
        elif op == "grayscale":
            img = ImageOps.grayscale(img).convert("RGB")
        elif op == "sepia":
            gray = ImageOps.grayscale(img)
            img = ImageOps.colorize(gray, black="#3a2410", white="#d9c8a9").convert("RGB")
        elif op == "invert":
            img = ImageOps.invert(img)
        elif op == "mirror":
            img = ImageOps.mirror(img)
        elif op == "flip_vertical":
            img = ImageOps.flip(img)
        elif op == "rotate":
            match = re.search(r"\d+", instruction_text)
            angle = int(match.group()) if match else 90
            img = img.rotate(-angle, expand=True)
        elif op == "upscale":
            match = re.search(r"\bв\s*(\d+)\s*раза?\b", instruction_text.lower())
            factor = max(1, min(int(match.group(1)) if match else 2, 4))
            img = img.resize((img.width * factor, img.height * factor), Image.LANCZOS)
        elif op == "brighten":
            img = ImageEnhance.Brightness(img).enhance(1.4)
        elif op == "darken":
            img = ImageEnhance.Brightness(img).enhance(0.7)
        elif op == "contrast":
            img = ImageEnhance.Contrast(img).enhance(1.4)
        elif op == "sharpen":
            img = ImageEnhance.Sharpness(img).enhance(2.0)
        elif op == "blur":
            img = img.filter(ImageFilter.GaussianBlur(4))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=92)
    return buf.getvalue()


async def apply_and_send_edit(update: Update, context: ContextTypes.DEFAULT_TYPE, chat_id: int, photo, ops: list):
    """Скачивает фото, применяет операции редактирования и отправляет результат."""
    await context.bot.send_chat_action(chat_id=chat_id, action="upload_photo")

    tg_file = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await tg_file.download_as_bytearray())

    instruction_text = update.message.text or update.message.caption or ""
    edited_bytes = await asyncio.to_thread(apply_edit_ops, image_bytes, ops, instruction_text)

    labels = ", ".join(_EDIT_OP_LABELS.get(op, op) for op in ops)
    result_file = io.BytesIO(edited_bytes)
    result_file.name = "edited.jpg"
    await update.message.reply_photo(photo=result_file, caption=f"Готово: {labels}", read_timeout=25)


# ======================================================================


# ======================================================================
# GOLD SIGNAL NODE — полностью независимый блок, не связан с остальным ботом.
# Всё, что ему нужно: analysis.py и модуль requests/asyncio/logging/json из stdlib.
# Можно вырезать целиком (вместе со строкой регистрации в main()) — ничего
# больше в файле не сломается.
#
# Самообучение: каждый отправленный (и подавленный) сигнал пишется в
# gold_signal_history.json. Через _GOLD_EVAL_HORIZON_HOURS часов нода сверяет
# предсказанное направление с реальным движением цены и считает точность
# последних сигналов. Если точность просела ниже _GOLD_STRUGGLING_ACCURACY —
# нода начинает требовать более сильного подтверждения (больше оснований)
# перед отправкой следующего сигнала.
#
# ИИ-советник (Claude + веб-поиск) отсюда убран — глубокий, многослойный анализ
# (мультитаймфрейм, макро, золото/серебро, новости) теперь делает отдельный
# агент SkullByte (skullbyte.py) по запросу. Эта нода отдаёт только быстрый
# механический сигнал по индикаторам, без вызовов ИИ.
# ======================================================================

_GOLD_TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
_GOLD_CHAT_ID = 7684813527

_GOLD_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gold_signal_history.json")
_GOLD_EVAL_HORIZON_HOURS = 4        # через сколько часов проверяем, сбылся ли сигнал
_GOLD_MOVE_THRESHOLD_PCT = 0.1      # минимальное движение цены (%), чтобы считать сигнал сбывшимся
_GOLD_ACCURACY_WINDOW = 20          # по скольким последним проверенным сигналам считаем точность
_GOLD_STRUGGLING_ACCURACY = 0.5     # ниже этой точности включаем более строгий фильтр
_GOLD_MIN_REASONS_WHEN_STRUGGLING = 2  # сколько оснований нужно сигналу, когда точность просела


def _gold_load_history() -> list:
    import json

    if not os.path.exists(_GOLD_HISTORY_FILE):
        return []
    try:
        with open(_GOLD_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[gold] Ошибка чтения истории сигналов: {e}")
        return []


def _gold_save_history(history: list):
    import json

    try:
        with open(_GOLD_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history[-500:], f, ensure_ascii=False, indent=2)  # не даём файлу расти бесконечно
    except Exception as e:
        logger.error(f"[gold] Ошибка сохранения истории сигналов: {e}")


def _gold_evaluate_pending(history: list, current_price: float):
    """Проверяет прошлые сигналы старше _GOLD_EVAL_HORIZON_HOURS и помечает, сбылись они или нет."""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    for record in history:
        if record.get("evaluated"):
            continue
        signal_time = datetime.fromisoformat(record["timestamp"])
        hours_passed = (now - signal_time).total_seconds() / 3600
        if hours_passed < _GOLD_EVAL_HORIZON_HOURS:
            continue

        move_pct = (current_price - record["price"]) / record["price"] * 100
        if record["direction"] == "бычий":
            correct = move_pct >= _GOLD_MOVE_THRESHOLD_PCT
        else:  # медвежий
            correct = move_pct <= -_GOLD_MOVE_THRESHOLD_PCT

        record["evaluated"] = True
        record["correct"] = correct
        record["move_pct"] = round(move_pct, 3)


def _gold_recent_accuracy(history: list):
    """Точность (0..1) по последним _GOLD_ACCURACY_WINDOW проверенным сигналам, либо None, если данных ещё нет."""
    evaluated = [r for r in history if r.get("evaluated")]
    if not evaluated:
        return None
    recent = evaluated[-_GOLD_ACCURACY_WINDOW:]
    return sum(1 for r in recent if r["correct"]) / len(recent)


def _gold_fetch_candles(outputsize: int = 110) -> list:
    """Получает часовые свечи XAU/USD через Twelve Data API. Блокирующая функция."""
    from analysis import Candle

    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": "XAU/USD",
        "interval": "1h",
        "outputsize": outputsize,
        "apikey": _GOLD_TWELVE_DATA_API_KEY,
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok":
            logger.error(f"[gold] Twelve Data вернул ошибку: {data}")
            return []
        # Twelve Data отдаёт свечи от новых к старым — разворачиваем в хронологический порядок
        return [
            Candle(high=float(v["high"]), low=float(v["low"]), close=float(v["close"]))
            for v in reversed(data.get("values", []))
        ]
    except Exception as e:
        logger.error(f"[gold] Ошибка получения свечей XAU/USD: {e}")
        return []


async def check_gold_signal(context: ContextTypes.DEFAULT_TYPE):
    """Фоновая задача JobQueue: раз в час проверяет технический сигнал по золоту и шлёт его в чат.

    Полностью самостоятельная нода — не использует и не трогает состояние
    остального бота (кроме context.bot_data под своим собственным ключом).
    Самообучается: сверяет старые сигналы с реальным движением цены и,
    если недавняя точность просела, требует более сильного подтверждения.
    """
    from datetime import datetime, timezone
    from analysis import is_market_likely_open, build_signal, format_signal_message

    if not is_market_likely_open():
        return

    candles = await asyncio.to_thread(_gold_fetch_candles)
    if len(candles) < 60:
        logger.warning("[gold] Недостаточно свечей XAU/USD для анализа.")
        return

    signal = build_signal(candles)

    history = _gold_load_history()
    _gold_evaluate_pending(history, signal["price"])
    accuracy = _gold_recent_accuracy(history)

    should_send = signal["direction"] != "нейтрально"
    if should_send and accuracy is not None and accuracy < _GOLD_STRUGGLING_ACCURACY:
        should_send = len(signal["reasons"]) >= _GOLD_MIN_REASONS_WHEN_STRUGGLING
        if not should_send:
            logger.info(
                f"[gold] Сигнал подавлен: точность последних сигналов {accuracy:.0%}, "
                f"оснований мало ({len(signal['reasons'])} < {_GOLD_MIN_REASONS_WHEN_STRUGGLING})."
            )

    if signal["direction"] != "нейтрально":
        history.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signal_id": signal["signal_id"],
            "direction": signal["direction"],
            "price": signal["price"],
            "reasons": signal["reasons"],
            "sent": should_send,
            "evaluated": False,
            "correct": None,
        })
    _gold_save_history(history)

    if not should_send:
        return
    if context.bot_data.get("_gold_last_signal_id") == signal["signal_id"]:
        return  # сигнал не изменился с прошлой проверки
    context.bot_data["_gold_last_signal_id"] = signal["signal_id"]

    message = format_signal_message(signal)
    if accuracy is not None:
        message += f"\n\nТочность последних сигналов: {accuracy:.0%}"

    await context.bot.send_message(chat_id=_GOLD_CHAT_ID, text=message)


# ======================================================================


def main():
    pid_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.pid")
    with open(pid_file, "w") as f:
        f.write(str(os.getpid()))

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.job_queue.run_repeating(check_gold_signal, interval=3600, first=10)  # gold signal node

    logger.info("Бот запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
