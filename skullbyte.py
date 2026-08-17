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
import json
import logging
import os
import re
from datetime import datetime, timezone

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

# Самообучение: своя история, отдельная от gold_signal_history.json старого бота.
# Тот же принцип — логируем каждый прогноз, через EVAL_HORIZON_HOURS сверяем
# с реальным движением цены, считаем точность по последним ACCURACY_WINDOW
# проверенным прогнозам и сообщаем её и пользователю, и самому Claude (чтобы
# он калибровал уверенность формулировок под свой реальный track record).
_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "skullbyte_history.json")
_EVAL_HORIZON_HOURS = 4
_MOVE_THRESHOLD_PCT = 0.1
_ACCURACY_WINDOW = 20

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


def pivot_points(candles_1d: list[Candle], min_range: float = 15.0) -> dict | None:
    """Классические дневные пивоты (PP/R1/R2/S1/S2) от последнего ПОЛНОЦЕННОГО
    торгового дня — конкретные внутридневные уровни для входов и стопов.

    candles_1d[-1] — сегодняшняя (ещё формирующаяся) свеча, пропускаем её всегда.
    Дальше идём назад и пропускаем "мёртвые" сессии (суббота, вечер воскресенья
    перед открытием) — у них диапазон high-low в разы уже нормального дня для
    золота, пивоты от них получаются бессмысленно узкими. Берём первый день
    с нормальным диапазоном — обычно это пятница, если сейчас выходные/понедельник.
    """
    for i in range(len(candles_1d) - 2, -1, -1):
        c = candles_1d[i]
        if (c.high - c.low) >= min_range:
            pp = (c.high + c.low + c.close) / 3
            span = c.high - c.low
            return {
                "r2": round(pp + span, 2),
                "r1": round(2 * pp - c.low, 2),
                "pp": round(pp, 2),
                "s1": round(2 * pp - c.high, 2),
                "s2": round(pp - span, 2),
            }
    return None


def current_session() -> str:
    """Текущая торговая сессия по UTC — золото по-разному волатильно в разные сессии."""
    from datetime import datetime, timezone

    hour = datetime.now(timezone.utc).hour
    in_london = 7 <= hour < 16
    in_ny = 12 <= hour < 21
    if in_london and in_ny:
        return "перекрытие Лондон/Нью-Йорк (12:00-16:00 UTC, максимальная ликвидность и волатильность)"
    if in_london:
        return "Лондонская сессия (07:00-16:00 UTC)"
    if in_ny:
        return "Нью-Йоркская сессия (12:00-21:00 UTC)"
    if 0 <= hour < 9:
        return "Азиатская сессия (00:00-09:00 UTC, обычно ниже волатильность по золоту)"
    return "затишье между сессиями (Нью-Йорк закрылся, Лондон/Азия ещё не открылись)"


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


# ---------- Самообучение (своя история, отдельная от старого бота) ----------

def _load_history() -> list:
    if not os.path.exists(_HISTORY_FILE):
        return []
    try:
        with open(_HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"[skullbyte] Ошибка чтения истории прогнозов: {e}")
        return []


def _save_history(history: list):
    try:
        with open(_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history[-500:], f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"[skullbyte] Ошибка сохранения истории прогнозов: {e}")


def _evaluate_pending(history: list, current_price: float):
    """Сверяет прогнозы старше _EVAL_HORIZON_HOURS с реальным движением цены."""
    now = datetime.now(timezone.utc)
    for entry in history:
        if entry["evaluated"]:
            continue
        ts = datetime.fromisoformat(entry["timestamp"])
        hours_passed = (now - ts).total_seconds() / 3600
        if hours_passed < _EVAL_HORIZON_HOURS:
            continue
        entry["evaluated"] = True
        move_pct = (current_price - entry["price"]) / entry["price"] * 100
        if entry["direction"] == "бычье":
            entry["correct"] = move_pct >= _MOVE_THRESHOLD_PCT
        elif entry["direction"] == "медвежье":
            entry["correct"] = move_pct <= -_MOVE_THRESHOLD_PCT
        else:
            entry["correct"] = abs(move_pct) < _MOVE_THRESHOLD_PCT


def _recent_accuracy(history: list) -> float | None:
    evaluated = [e for e in history if e["evaluated"] and e["correct"] is not None]
    if not evaluated:
        return None
    recent = evaluated[-_ACCURACY_WINDOW:]
    return sum(1 for e in recent if e["correct"]) / len(recent)


def _extract_direction(analysis_text: str) -> tuple[str, str]:
    """Достаёт машиночитаемую метку SIGNAL: ... из ответа Claude и убирает
    её из текста, который увидит пользователь. Возвращает (направление, чистый_текст).
    """
    match = re.search(r"\n?SIGNAL:\s*(BULLISH|BEARISH|NEUTRAL)\s*$", analysis_text.strip())
    if not match:
        return "нейтральное", analysis_text
    direction_map = {"BULLISH": "бычье", "BEARISH": "медвежье", "NEUTRAL": "нейтральное"}
    direction = direction_map[match.group(1)]
    clean_text = analysis_text[: match.start()].strip()
    return direction, clean_text


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
    signal_15m: dict, signal_1h: dict, signal_1d: dict, real_yield: dict, metals: dict,
    macd_15m: dict | None = None, atr_15m: float | None = None,
    macd_1h: dict | None = None, atr_1h: float | None = None,
    macd_1d: dict | None = None, atr_1d: float | None = None,
    pivots: dict | None = None, session: str = "",
) -> str:
    """Собирает сырые данные всех слоёв в текст для промпта Claude."""
    parts = []

    parts.append(
        f"15-минутный график (для точности входа): направление {signal_15m.get('direction')}, "
        f"цена {signal_15m.get('price')}, RSI(14)={signal_15m.get('rsi')}, "
        f"основания: {', '.join(signal_15m.get('reasons', [])) or 'нет'}."
        + _macd_atr_note(macd_15m, atr_15m)
    )
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

    if pivots:
        parts.append(
            f"Внутридневные уровни (пивоты от вчерашнего дня): "
            f"R2={pivots['r2']}, R1={pivots['r1']}, PP={pivots['pp']}, S1={pivots['s1']}, S2={pivots['s2']} "
            f"(классические уровни для входов/стопов дневного трейдера)."
        )

    if session:
        parts.append(f"Текущая торговая сессия: {session}.")

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


def ask_claude_synthesis(data_summary: str, accuracy: float | None) -> str:
    """Просит Claude (с доступом к веб-поиску) свести всё в единый разбор для пользователя."""
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    accuracy_note = (
        f"\n\nСправка: точность твоих последних {_ACCURACY_WINDOW} прогнозов (сверено с реальным "
        f"движением цены через {_EVAL_HORIZON_HOURS}ч) — {accuracy:.0%}. "
        + ("Точность просела — формулируй направление осторожнее, явно проговаривай неопределённость."
           if accuracy is not None and accuracy < 0.5 else "")
    ) if accuracy is not None else ""

    user_prompt = (
        "Ты аналитик по золоту (XAU/USD) для дневного трейдера (до 5 сделок в день, "
        "внутри дня, не позиционная торговля). Вот сырые данные, собранные только что:\n\n"
        f"{data_summary}"
        f"{accuracy_note}\n\n"
        "Проверь через веб-поиск текущий новостной и макроэкономический фон (ставки ФРС, "
        "инфляционная статистика, геополитика). Отдельно найди экономический календарь "
        "на СЕГОДНЯ — важно узнать ТОЧНОЕ время публикаций (укажи по МСК), а не только "
        "какой день недели. Сведи всё в единый разбор на русском. Структура ответа:\n"
        "1. Итоговое направление одним словом (бычье/медвежье/нейтральное) и коротко почему.\n"
        "2. Что говорит техника (согласуются ли 15-минутный, часовой и дневной графики).\n"
        "3. Что говорит макро (доходность, золото/серебро).\n"
        "4. Конкретные уровни для входа/стопа — используй присланные пивоты и текущую "
        "сессию, дай практические цифры (например 'вход от S1, стоп ниже S2'), а не общие слова.\n"
        "5. Риски сегодня — события с ТОЧНЫМ временем по МСК, если оно есть в календаре.\n"
        "Пиши сжато, по существу, без лишних вступлений.\n\n"
        "В САМОМ КОНЦЕ ответа, отдельной последней строкой, добавь СТРОГО в этом формате "
        "(это для автоматической обработки, не для пользователя, но её всё равно нужно написать): "
        "SIGNAL: BULLISH — если направление из пункта 1 бычье, SIGNAL: BEARISH — если медвежье, "
        "SIGNAL: NEUTRAL — если нейтральное."
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
    candles_15m, candles_1h, candles_1d, real_yield, metals = await asyncio.gather(
        asyncio.to_thread(fetch_candles, "15min", 110),
        asyncio.to_thread(fetch_candles, "1h", 110),
        asyncio.to_thread(fetch_candles, "1day", 110),
        asyncio.to_thread(fetch_real_yield),
        asyncio.to_thread(fetch_gold_silver),
    )

    if len(candles_15m) < 60 or len(candles_1h) < 60 or len(candles_1d) < 60:
        return "Недостаточно данных по свечам XAU/USD, попробуй чуть позже."

    signal_15m = build_signal(candles_15m)
    signal_1h = build_signal(candles_1h)
    signal_1d = build_signal(candles_1d)

    macd_15m, atr_15m = macd([c.close for c in candles_15m]), atr(candles_15m)
    macd_1h, atr_1h = macd([c.close for c in candles_1h]), atr(candles_1h)
    macd_1d, atr_1d = macd([c.close for c in candles_1d]), atr(candles_1d)

    pivots = pivot_points(candles_1d)
    session = current_session()

    # Самообучение: сверяем прошлые прогнозы с текущей ценой ДО того, как спросим
    # Claude — так свежая точность попадает в промпт этого же запроса.
    current_price = signal_1h["price"]
    history = _load_history()
    _evaluate_pending(history, current_price)
    accuracy = _recent_accuracy(history)

    data_summary = build_data_summary(
        signal_15m, signal_1h, signal_1d, real_yield, metals,
        macd_15m, atr_15m, macd_1h, atr_1h, macd_1d, atr_1d,
        pivots, session,
    )
    raw_analysis = await asyncio.to_thread(ask_claude_synthesis, data_summary, accuracy)
    direction, analysis_text = _extract_direction(raw_analysis)

    history.append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "direction": direction,
        "price": current_price,
        "evaluated": False,
        "correct": None,
    })
    _save_history(history)

    footer = f"\n\n📊 Точность последних прогнозов: {accuracy:.0%}" if accuracy is not None else (
        "\n\n📊 Точность прогнозов: пока недостаточно данных (нужно время)"
    )
    return f"🪙 Разбор по XAU/USD\n\n{analysis_text}{footer}"


# ---------- Telegram-обработчики ----------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я SkullByte — агент глубокого анализа по золоту (XAU/USD) для дневного трейдера.\n\n"
        "Напиши что угодно (или /gold) — соберу технику на 15-минутном, часовом и дневном "
        "графике, пивот-уровни на сегодня, текущую сессию, реальную доходность облигаций, "
        "золото/серебро и точный новостной календарь на сегодня, и пришлю разбор."
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
