"""Decimal indicators for one request's ordered, available closing prices."""

from decimal import ROUND_HALF_UP, Decimal, localcontext

from app.schemas.analysis import TechnicalIndicators


def rounded(value: Decimal | None) -> str | None:
    if value is None:
        return None
    return format(value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f")


def ema(values: list[Decimal], size: int) -> list[Decimal | None]:
    """Seed with an SMA; keep unrounded state and nulls before the seed."""
    result: list[Decimal | None] = [None] * len(values)
    if len(values) < size:
        return result
    current = sum(values[:size], Decimal(0)) / size
    result[size - 1] = current
    alpha = Decimal(2) / (size + 1)
    for index in range(size, len(values)):
        current += alpha * (values[index] - current)
        result[index] = current
    return result


def technical_indicators(closes: list[Decimal]) -> list[TechnicalIndicators]:
    with localcontext() as context:
        context.prec = 64
        fast, slow = ema(closes, 12), ema(closes, 26)
        differences = [
            short - long
            for short, long in zip(fast, slow, strict=True)
            if short is not None and long is not None
        ]
        signals = ema(differences, 9)
        result = []
        gain = loss = Decimal(0)
        for index, close in enumerate(closes):
            rsi = None
            if index:
                change = close - closes[index - 1]
                up, down = max(change, Decimal(0)), max(-change, Decimal(0))
                if index <= 14:
                    gain += up
                    loss += down
                    if index == 14:
                        gain /= 14
                        loss /= 14
                else:
                    gain = (gain * 13 + up) / 14
                    loss = (loss * 13 + down) / 14
                if index >= 14:
                    rsi = (
                        Decimal(50)
                        if gain == loss == 0
                        else Decimal(100)
                        if loss == 0
                        else 100 - 100 / (1 + gain / loss)
                    )
            middle = upper = lower = None
            if index >= 19:
                window = closes[index - 19 : index + 1]
                middle = sum(window, Decimal(0)) / 20
                deviation = (sum(((v - middle) ** 2 for v in window), Decimal(0)) / 20).sqrt()
                upper, lower = middle + 2 * deviation, middle - 2 * deviation
            macd = differences[index - 25] if index >= 25 else None
            signal = signals[index - 25] if index >= 25 else None
            result.append(
                TechnicalIndicators(
                    ma20=rounded(middle),
                    ma60=rounded(sum(closes[index - 59 : index + 1], Decimal(0)) / 60)
                    if index >= 59
                    else None,
                    rsi14=rounded(rsi),
                    macd=rounded(macd),
                    macd_signal=rounded(signal),
                    macd_histogram=rounded(macd - signal)
                    if macd is not None and signal is not None
                    else None,
                    bollinger_middle=rounded(middle),
                    bollinger_upper=rounded(upper),
                    bollinger_lower=rounded(lower),
                )
            )
        return result
