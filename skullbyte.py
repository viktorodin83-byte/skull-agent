# -*- coding: utf-8 -*-
"""
SkullByte — отдельный Telegram-агент, только для глубокого анализа XAU/USD.

Полностью самостоятельный скрипт: не импортирует и не трогает bot.py,
использует свой собственный токен Telegram (SKULLBYTE_TOKEN). Общий модуль
только analysis.py (чистые функции индикаторов, без побочных эффектов).

Работает по запросу — никакого расписания. Пользователь пишет боту что угодно
(или /gold) — в ответ приходит разбор: технический сигнал на часовом и дневном
графике, реальная доходность облигаций США (FRED), золото и серебро (UniRateAPI),
и синтез от Claude с учётом текущего новостного фона.
"""

import asyncio
import logging
import os

import requests
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from analysis import Candle, build_signal

load_dotenv()

TELEGRAM_TOKEN = os.getenv("SKULLBYTE_TOKEN")
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
FRED_API_KEY = os.getenv("FRED_API_KEY")
UNIRATE_API_KEY = os.getenv("UNIRATE_API_KEY")

ADVISOR_MODEL = "claude-opus-5"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


# ---------- Дополнительные индикаторы (только для этого агента) ----------
#
# RSI/MA/Фибоначчи/канал уже даёт analysis.py (общий для обоих ботов) — они
# видят "перекупленность" и "тренд по пересечению". Тут добавлены две вещи,
# которых там нет: MACD (импульс/ускорение движения) и ATR (волатильность,
# чтобы оценивать движения относительно текущей нормы, а не в вакууме).

def _ema(values: list[float], period: int) -> list[float]:
    """Экспоненциальная скользящая средняя. Первое значение — простая SMA."""
    if len(values) < period:
        return []
    ema_values = [sum(values[:period]) / period]
    multiplier = 2 / (period + 1)
    for price in values[period:]:
        ema_values.append((price - ema_values[-1]) * multiplier + ema_values[-1])
    return ema_values


def macd(closes: list[float], fast: int = 12, slow: int = 26, signal: int = 9) -> dict | None:
    """MACD: импульс тренда. Возвращает линию MACD, сигнальную линию и гистограмму."""
    if len(closes) < slow + signal:
        return None
    ema_fast = _ema(closes, fast)
    ema_slow = _ema(closes, slow)
    offset = len(ema_fast) - len(ema_slow)
    macd_line = [ema_fast[i + offset] - ema_slow[i] for i in range(len(ema_slow))]
    signal_line = _ema(macd_line, signal)
    if not signal_line:
        return None
    histogram = macd_line[-1] - signal_line[-1]
    return {
        "macd": round(macd_line[-1], 2),
        "signal": round(signal_line[-1], 2),
        "histogram": round(histogram, 2),
        "momentum": "растёт" if histogram > 0 else "падает",
    }


def atr(candles: list[Candle], period: int = 14) -> float | None:
    """Средний истинный диапазон — мера текущей волатильности в долларах."""
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for i in range(1, len(candles)):
        high, low, prev_close = candles[i].high, candles[i].low, candles[i - 1].close
        true_ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
    return round(sum(true_ranges[-period:]) / period, 2)


# ---------- Сбор данных ----------

def fetch_candles(interval: str, outputsize: int = 110) -> list[Candle]:
    """Свечи XAU/USD с Twelve Data для произвольного таймфрейма."""
    url = "https://api.twelvedata.com/time_series"
    params = {
        "symbol": "XAU/USD",
        "interval": interval,
        "outputsize": outputsize,
        "apikey": TWELVE_DATA_API_KEY,
    }
    try:
        resp = requests.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") != "ok":
            logger.error(f"[skullbyte] Twelve Data ({interval}) вернул ошибку: {data}")
            return []
        return [
            Candle(high=float(v["high"]), low=float(v["low"]), close=float(v["close"]))
            for v in reversed(data.get("values", []))
        ]
    except Exception as e:
        logger.error(f"[skullbyte] Ошибка получения свечей XAU/USD ({interval}): {e}")
        return []


def fetch_real_yield() -> dict:
    """Реальная доходность 10-летних облигаций США (FRED, ряд DFII10)."""
    url = "https://api.stlouisfed.org/fred/series/observations"
    params = {
        "series_id": "DFII10",
        "api_key": FRED_API_KEY,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 30,
    }
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        obs = [o for o in resp.json().get("observations", []) if o["value"] != "."]
        if not obs:
            return {}
        latest = obs[0]
        oldest = obs[-1]
        latest_val = float(latest["value"])
        oldest_val = float(oldest["value"])
        return {
            "latest_value": latest_val,
            "latest_date": latest["date"],
            "change_since": round(latest_val - oldest_val, 2),
            "window_start_date": oldest["date"],
        }
    except Exception as e:
        logger.error(f"[skullbyte] Ошибка получения реальной доходности с FRED: {e}")
        return {}


def fetch_gold_silver() -> dict:
    """Текущие цены золота и серебра + их соотношение (UniRateAPI)."""
    url = "https://api.unirateapi.com/api/commodities/rates"
    params = {"api_key": UNIRATE_API_KEY, "symbols": "XAU,XAG"}
    try:
        resp = requests.get(url, params=params, timeout=20)
        resp.raise_for_status()
        rates = resp.json().get("rates", {})
        xau, xag = rates.get("XAU"), rates.get("XAG")
        if not xau or not xag:
            return {}
        return {
            "gold": round(xau, 2),
            "silver": round(xag, 2),
            "gold_silver_ratio": round(xau / xag, 1),
        }
    except Exception as e:
        logger.error(f"[skullbyte] Ошибка получения золота/серебра с UniRateAPI: {e}")
        return {}


# ---------- Синтез ----------

def _macd_atr_note(macd_data: dict | None, atr_value: float | None) -> str:
    if not macd_data and not atr_value:
        return ""
    bits = []
    if macd_data:
        bits.append(f"MACD: гистограмма {macd_data['histogram']}, импульс {macd_data['momentum']}")
    if atr_value:
        bits.append(f"ATR(14): ${atr_value} (текущий размах волатильности)")
    return " " + "; ".join(bits) + "."


def build_data_summary(
    signal_1h: dict, signal_1d: dict, real_yield: dict, metals: dict,
    macd_1h: dict | None = None, atr_1h: float | None = None,
    macd_1d: dict | None = None, atr_1d: float | None = None,
) -> str:
    """Собирает сырые данные всех слоёв в текст для промпта Claude."""
    parts = []

    parts.append(
        f"Часовой график: направление {signal_1h.get('direction')}, "
        f"цена {signal_1h.get('price')}, RSI(14)={signal_1h.get('rsi')}, "
        f"основания: {', '.join(signal_1h.get('reasons', [])) or 'нет'}."
        + _macd_atr_note(macd_1h, atr_1h)
    )
    parts.append(
        f"Дневной график: направление {signal_1d.get('direction')}, "
        f"цена {signal_1d.get('price')}, RSI(14)={signal_1d.get('rsi')}, "
        f"основания: {', '.join(signal_1d.get('reasons', [])) or 'нет'}."
        + _macd_atr_note(macd_1d, atr_1d)
    )

    if real_yield:
        trend = "выросла" if real_yield["change_since"] > 0 else "снизилась" if real_yield["change_since"] < 0 else "не изменилась"
        parts.append(
            f"Реальная доходность 10-летних облигаций США: {real_yield['latest_value']}% "
            f"на {real_yield['latest_date']}, за последний месяц {trend} на {abs(real_yield['change_since'])} п.п. "
            f"(рост реальной доходности обычно давит на золото, снижение — поддерживает)."
        )
    else:
        parts.append("Реальная доходность облигаций: данные недоступны.")

    if metals:
        parts.append(
            f"Золото: ${metals['gold']}, серебро: ${metals['silver']}, "
            f"соотношение золото/серебро: {metals['gold_silver_ratio']} "
            f"(историческая норма примерно 60-90; сильное отклонение — сигнал о характере движения)."
        )
    else:
        parts.append("Данные по золоту/серебру: недоступны.")

    return "\n".join(parts)


def ask_claude_synthesis(data_summary: str) -> str:
    """Просит Claude (с доступом к веб-поиску) свести всё в единый разбор для пользователя."""
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    user_prompt = (
        "Ты аналитик по золоту (XAU/USD). Вот сырые данные, собранные только что:\n\n"
        f"{data_summary}\n\n"
        "Кратко проверь через веб-поиск текущий новостной и макроэкономический фон "
        "(ставки ФРС, инфляционная статистика, геополитика, крупные данные в ближайшие часы). "
        "Сведи всё это (техника на двух таймфреймах + реальная доходность + золото/серебро + новости) "
        "в единый разбор на русском для трейдера. Структура ответа:\n"
        "1. Итоговое направление одним словом (бычье/медвежье/нейтральное) и коротко почему.\n"
        "2. Что говорит техника (согласны ли часовой и дневной графики между собой).\n"
        "3. Что говорит макро (доходность, золото/серебро).\n"
        "4. Риски в ближайшие часы-дни (важные события).\n"
        "Пиши сжато, по существу, без лишних вступлений."
    )
    payload = {
        "model": ADVISOR_MODEL,
        "max_tokens": 4096,
        "tools": [{"type": "web_search_20260209", "name": "web_search", "max_uses": 3}],
        "messages": [{"role": "user", "content": user_prompt}],
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=150)
        resp.raise_for_status()
        data = resp.json()
        text_parts = [b["text"] for b in data.get("content", []) if b.get("type") == "text"]
        text = "\n".join(part.strip() for part in text_parts).strip()
        return text or "Не удалось получить разбор от ИИ."
    except Exception as e:
        logger.error(f"[skullbyte] Ошибка синтеза Claude: {e}")
        return "Не удалось получить разбор от ИИ (ошибка запроса)."


async def run_full_analysis() -> str:
    """Собирает все слои данных и возвращает готовый текст ответа."""
    candles_1h, candles_1d, real_yield, metals = await asyncio.gather(
        asyncio.to_thread(fetch_candles, "1h", 110),
        asyncio.to_thread(fetch_candles, "1day", 110),
        asyncio.to_thread(fetch_real_yield),
        asyncio.to_thread(fetch_gold_silver),
    )

    if len(candles_1h) < 60 or len(candles_1d) < 60:
        return "Недостаточно данных по свечам XAU/USD, попробуй чуть позже."

    signal_1h = build_signal(candles_1h)
    signal_1d = build_signal(candles_1d)

    closes_1h = [c.close for c in candles_1h]
    closes_1d = [c.close for c in candles_1d]
    macd_1h, atr_1h = macd(closes_1h), atr(candles_1h)
    macd_1d, atr_1d = macd(closes_1d), atr(candles_1d)

    data_summary = build_data_summary(
        signal_1h, signal_1d, real_yield, metals,
        macd_1h, atr_1h, macd_1d, atr_1d,
    )
    analysis_text = await asyncio.to_thread(ask_claude_synthesis, data_summary)

    return f"🪙 Разбор по XAU/USD\n\n{analysis_text}"


# ---------- Telegram-обработчики ----------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я SkullByte — агент глубокого анализа по золоту (XAU/USD).\n\n"
        "Напиши что угодно (или /gold) — соберу технику на часовом и дневном "
        "графике, реальную доходность облигаций, золото/серебро и текущий "
        "новостной фон, и пришлю разбор."
    )


async def analyze_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thinking_msg = await update.message.reply_text("Собираю данные и анализирую, это может занять минуту...")
    try:
        answer = await run_full_analysis()
    except Exception as e:
        logger.error(f"[skullbyte] Ошибка анализа: {e}")
        answer = "Что-то пошло не так при сборе анализа, попробуй ещё раз."
    await thinking_msg.edit_text(answer)


def main():
    if not TELEGRAM_TOKEN:
        raise RuntimeError("SKULLBYTE_TOKEN не задан в .env")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("gold", analyze_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, analyze_command))

    logger.info("SkullByte запущен...")
    app.run_polling()


if __name__ == "__main__":
    main()
