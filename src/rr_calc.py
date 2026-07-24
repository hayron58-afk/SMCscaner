"""Расчёт соотношения риск/профит (R:R)."""

DEPOSITS = [1000, 5000, 10000, 50000, 100000]


def calculate_rr(entry: float, stop: float, target: float) -> float:
    """
    Считает R:R для сделки.

    Args:
        entry: цена входа
        stop: стоп-лосс
        target: цель (take profit)

    Returns:
        Отношение reward/risk. 0 если риск равен нулю.
    """
    risk = abs(entry - stop)
    reward = abs(target - entry)
    if risk == 0:
        return 0.0
    return reward / risk


def calc_position_sizes(entry: float, stop: float, risk_pct: float = 0.0025) -> dict:
    """
    Считает размер позиции для нескольких депозитов при фиксированном риске на сделку.

    deposits: 1000, 5000, 10000, 50000, 100000
    risk_pct: доля депозита (0.0025 = 0.25%)

    Returns:
        dict с ключами-депозитами и значениями {"risk_usd": ..., "qty": ...}
    """
    stop_distance = abs(entry - stop)
    result: dict = {}

    for deposit in DEPOSITS:
        risk_usd = deposit * risk_pct
        qty = risk_usd / stop_distance if stop_distance > 0 else 0.0
        result[deposit] = {"risk_usd": risk_usd, "qty": qty}

    return result
