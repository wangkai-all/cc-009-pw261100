"""运行配置：币种小数位等。

金额一律使用 decimal.Decimal，按本配置中各币种的小数位做量化；
不存在的币种使用 default_scale，绝不做自动换汇。
"""
from __future__ import annotations

import json
from pathlib import Path

# 常用币种小数位（ISO 4217）
DEFAULT_CURRENCY_SCALES = {
    "CNY": 2,
    "HKD": 2,
    "USD": 2,
    "EUR": 2,
    "GBP": 2,
    "SGD": 2,
    "JPY": 0,
    "KRW": 0,
}


class Config:
    def __init__(self, currency_scales: dict | None = None, default_scale: int = 2):
        scales = dict(DEFAULT_CURRENCY_SCALES)
        if currency_scales:
            scales.update({k.upper(): int(v) for k, v in currency_scales.items()})
        self.currency_scales = scales
        self.default_scale = int(default_scale)

    def scale(self, currency: str) -> int:
        """返回某币种保留的小数位数。"""
        return self.currency_scales.get(str(currency).upper(), self.default_scale)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        if path and Path(path).exists():
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            return cls(
                currency_scales=data.get("currency_scales"),
                default_scale=int(data.get("default_scale", 2)),
            )
        return cls()
