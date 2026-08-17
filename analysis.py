"""
Технический анализ XAU/USD: RSI, MA, уровни Фибоначчи, пробитие канала.
Вызывается раз в час фоновой задачей (JobQueue) в bot.py.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Candle:
    high: float
    low: float
    close: float


# ---------- Проверка рабочих дней рынка ----------

def is_market_likely_open(now: datetime | None = None) -> bool:
    """
    Проверка по реальным часам рынка золота (XAU/USD): открыт с воскресенья
    ~22:00 UTC (открытие сессии в Сиднее) до пятницы ~22:00 UTC (закрытие
    в Нью-Йорке), в субботу закрыт полностью.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    else:
        now = now.astimezone(timezone.utc)

    weekday = now.weekday()  # Пн=0 ... Вс=6
    if weekday == 5:  # суббота — закрыто весь день
        return False
    if weekday == 6:  # воскресенье — открывается вечером
        return now.hour >= 22
    if weekday == 4:  # пятница — закрывается вечером
        return now.hour < 22
    return True


# ---------- Базовые индикаторы ----------

def sma(closes: list[float], period: int) -> float | None:
    if len(closes) < period:
        return None
    return sum(closes[-period:]) / period


def rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(-period, 0):
        diff = closes[i] - closes[i - 1]
        gains.append(max(diff, 0))
        losses.append(max(-diff, 0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)


# ---------- Уровни Фибоначчи ----------

FIB_LEVELS = (0.236, 0.382, 0.5, 0.618)


def fibonacci_levels(candles: list[Candle], lookback: int = 100) -> dict:
    """
    Находит последний значимый максимум и минимум за период lookback
    и строит от них уровни retracement.
    Возвращает словарь: {swing_high, swing_low, levels: {0.236: price, ...}, trend: 'up'|'down'}
    """
    window = candles[-lookback:] if len(candles) >= lookback else candles
    if len(window) < 2:
        return {}

    highs = [c.high for c in window]
    lows = [c.low for c in window]
    swing_high = max(highs)
    swing_low = min(lows)

    if swing_high == swing_low:
        # Диапазона нет (совсем плоский рынок) — уровни Фибоначчи не имеют смысла
        return {}

    high_idx = highs.index(swing_high)
    low_idx = lows.index(swing_low)
    # Если хай случился позже лоу — считаем, что тренд был вверх (retracement от хая вниз)
    trend = "up" if high_idx > low_idx else "down"

    diff = swing_high - swing_low
    levels = {}
    for level in FIB_LEVELS:
        if trend == "up":
            # retracement вниз от хая
            levels[level] = round(swing_high - diff * level, 2)
        else:
            # retracement вверх от лоу
            levels[level] = round(swing_low + diff * level, 2)

    return {
        "swing_high": round(swing_high, 2),
        "swing_low": round(swing_low, 2),
        "levels": levels,
        "trend": trend,
    }


def price_near_fib_level(current_price: float, fib: dict, tolerance_pct: float = 0.15) -> float | None:
    """
    Проверяет, находится ли текущая цена рядом с одним из уровней Фибоначчи.
    tolerance_pct — допуск в процентах от цены (0.15% по умолчанию).
    Возвращает ближайший уровень (0.236/0.382/0.5/0.618), если попадание есть, иначе None.
    """
    if not fib or "levels" not in fib:
        return None
    tolerance = current_price * (tolerance_pct / 100)
    for level, price in fib["levels"].items():
        if abs(current_price - price) <= tolerance:
            return level
    return None


# ---------- Пробитие канала ----------

def channel_breakout(candles: list[Candle], lookback: int = 25) -> dict:
    """
    Строит простой канал по локальным хаям/лоу последних `lookback` свечей
    (без учёта самой последней свечи — она проверяется на пробитие),
    и определяет, пробила ли последняя цена этот канал вверх/вниз.
    """
    if len(candles) < lookback + 1:
        return {"breakout": None}

    window = candles[-(lookback + 1):-1]  # канал строим БЕЗ последней свечи
    last_candle = candles[-1]

    channel_high = max(c.high for c in window)
    channel_low = min(c.low for c in window)

    breakout = None
    if last_candle.close > channel_high:
        breakout = "up"
    elif last_candle.close < channel_low:
        breakout = "down"

    return {
        "channel_high": round(channel_high, 2),
        "channel_low": round(channel_low, 2),
        "breakout": breakout,
    }


# ---------- Объединённый сигнал ----------

def build_signal(candles: list[Candle]) -> dict:
    """
    Собирает все индикаторы вместе и формирует итоговый сигнал.
    Возвращает словарь с полным разбором + короткий 'signal_id' для сравнения с предыдущим.
    """
    closes = [c.close for c in candles]
    current_price = closes[-1]

    r = rsi(closes, 14)
    ma20 = sma(closes, 20)
    ma50 = sma(closes, 50)
    fib = fibonacci_levels(candles, lookback=100)
    fib_hit = price_near_fib_level(current_price, fib)
    channel = channel_breakout(candles, lookback=25)

    reasons = []
    direction = "нейтрально"

    # Логика комбинирования сигналов
    if channel.get("breakout") == "up":
        reasons.append(f"пробитие канала вверх (>{channel['channel_high']})")
        direction = "бычий"
    elif channel.get("breakout") == "down":
        reasons.append(f"пробитие канала вниз (<{channel['channel_low']})")
        direction = "медвежий"

    if fib_hit is not None:
        pct = int(fib_hit * 1000) / 10  # 0.618 -> 61.8
        reasons.append(f"цена у уровня Фибоначчи {pct}% ({fib['levels'][fib_hit]})")
        if r is not None and r < 35 and direction != "медвежий":
            direction = "бычий"
            reasons.append(f"RSI={r} (перепроданность)")
        elif r is not None and r > 65 and direction != "бычий":
            direction = "медвежий"
            reasons.append(f"RSI={r} (перекупленность)")

    if ma20 is not None and ma50 is not None:
        if direction == "нейтрально":
            if ma20 > ma50 and r is not None and r < 70:
                direction = "бычий"
                reasons.append("MA20 > MA50")
            elif ma20 < ma50 and r is not None and r > 30:
                direction = "медвежий"
                reasons.append("MA20 < MA50")

    # signal_id — компактный идентификатор состояния для сравнения "изменился ли сигнал"
    signal_id = f"{direction}|{channel.get('breakout')}|{fib_hit}"

    return {
        "price": round(current_price, 2),
        "rsi": r,
        "ma20": ma20,
        "ma50": ma50,
        "fib": fib,
        "fib_hit": fib_hit,
        "channel": channel,
        "direction": direction,
        "reasons": reasons,
        "signal_id": signal_id,
    }


def format_signal_message(signal: dict) -> str:
    """Короткое сообщение: только вывод — покупать, продавать, или ничего не менять."""
    direction = signal["direction"]

    if direction == "бычий":
        return f"🟢 XAU/USD: сигнал на ПОКУПКУ (цена {signal['price']})"
    elif direction == "медвежий":
        return f"🔴 XAU/USD: сигнал на ПРОДАЖУ (цена {signal['price']})"
    else:
        return None  # нейтрально — сообщение вообще не отправляем


def format_signal_message_detailed(signal: dict) -> str:
    """Подробное сообщение с разбором — для команды /gold по запросу."""
    emoji = {"бычий": "🟢", "медвежий": "🔴", "нейтрально": "⚪"}.get(signal["direction"], "⚪")
    lines = [
        f"{emoji} Сигнал по XAU/USD",
        f"Цена: {signal['price']}",
    ]
    if signal["rsi"] is not None:
        lines.append(f"RSI(14): {signal['rsi']}")
    if signal["reasons"]:
        lines.append("Основание: " + ", ".join(signal["reasons"]))
    lines.append(f"→ Направление: {signal['direction']}")
    return "\n".join(lines)
