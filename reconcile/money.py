"""十进制金额工具：禁止 float 参与任何金额运算。"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

ZERO = Decimal("0")


def parse_amount(raw) -> Decimal:
    """把 CSV 中的金额文本解析为 Decimal，拒绝浮点与非法值。"""
    if isinstance(raw, float):
        # 显式拒绝 float：调用方应传字符串
        raise TypeError("金额不能使用 float，请以字符串传入")
    text = str(raw).strip().replace(",", "")
    if not text:
        raise ValueError("金额为空")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"非法金额: {raw!r}") from exc
    if not value.is_finite():
        raise ValueError(f"金额必须是有限数: {raw!r}")
    return value


def quantize(value: Decimal, scale: int) -> Decimal:
    """按币种小数位量化（四舍五入），返回 Decimal。"""
    quantum = Decimal(1).scaleb(-int(scale))
    return value.quantize(quantum, rounding=ROUND_HALF_UP)


def format_amount(value: Decimal, scale: int) -> str:
    """量化后转成定长小数字符串，用于存储与展示。"""
    return str(quantize(value, scale))


def semantic_key_amount(value: Decimal) -> str:
    """语义化金额表示：100.0 与 100.00 视为同一内容。"""
    return str(value.normalize())
