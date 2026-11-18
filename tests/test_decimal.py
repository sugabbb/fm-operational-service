from decimal import Decimal

from app.core.decimal import line_amount, money, quantity


def test_exact_decimal_and_idr_rounding() -> None:
    assert quantity("1.2349") == Decimal("1.234")
    assert quantity("-1.2349") == Decimal("-1.234")
    assert money("10.5") == Decimal("11")
    assert money("-10.5") == Decimal("-11")
    assert line_amount("3.111", "1000.5") == Decimal("3113")
