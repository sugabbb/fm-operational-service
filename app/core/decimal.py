from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

QTY = Decimal("0.001")
MONEY = Decimal("1")


def quantity(value: object) -> Decimal:
    return Decimal(str(value)).quantize(QTY, rounding=ROUND_DOWN)


def money(value: object) -> Decimal:
    return Decimal(str(value)).quantize(MONEY, rounding=ROUND_HALF_UP)


def line_amount(quantity_value: object, price_value: object) -> Decimal:
    return money(Decimal(str(quantity_value)) * Decimal(str(price_value)))


def decimal_string(value: Decimal | int | str | None, scale: int = 3) -> str | None:
    if value is None:
        return None
    return f"{Decimal(str(value)):.{scale}f}"
