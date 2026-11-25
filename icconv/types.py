# -*- coding: utf-8 -*-
"""逻辑类型系统：数据类型、字符集、排序规则。

规则参照 MySQL 8.0 的行为做了简化（见 coercion.py 的规则表），
但类型族、精度层级、字符集/排序规则优先级都按真实数据库建模。
"""
from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------- 字符集/排序规则

# 每个排序规则: (所属字符集, 是否大小写敏感, 权重优先级)
# 优先级数字越小优先级越高（参照 MySQL collation precedence 的思路建模）
COLLATIONS = {
    "utf8mb4_general_ci": {"charset": "utf8mb4", "case_sensitive": False, "precedence": 10},
    "utf8mb4_bin":        {"charset": "utf8mb4", "case_sensitive": True,  "precedence": 9},
    "utf8_general_ci":    {"charset": "utf8",    "case_sensitive": False, "precedence": 20},
    "utf8_bin":           {"charset": "utf8",    "case_sensitive": True,  "precedence": 19},
    "latin1_swedish_ci":  {"charset": "latin1",  "case_sensitive": False, "precedence": 30},
    "latin1_bin":         {"charset": "latin1",  "case_sensitive": True,  "precedence": 29},
    "gbk_chinese_ci":     {"charset": "gbk",     "case_sensitive": False, "precedence": 31},
    "gbk_bin":            {"charset": "gbk",     "case_sensitive": True,  "precedence": 30},
}

DEFAULT_COLLATION = {
    "utf8mb4": "utf8mb4_general_ci",
    "utf8": "utf8_general_ci",
    "latin1": "latin1_swedish_ci",
    "gbk": "gbk_chinese_ci",
}

# 字符集优先级（数字小 = 优先级高，参照 MySQL coercibility 中 charset 选择）
CHARSET_PRECEDENCE = {"utf8mb4": 1, "utf8": 2, "gbk": 3, "latin1": 4}


def charset_of(collation: str) -> str:
    return COLLATIONS[collation]["charset"]


# ---------------------------------------------------------------- 数据类型

# 数值类型按精度从低到高（真实数据库聚合类型推导的简化模型）
INT_TYPES = ["tinyint", "smallint", "mediumint", "int", "bigint"]
FLOAT_TYPES = ["float", "double"]
NUMERIC_TYPES = INT_TYPES + ["decimal"] + FLOAT_TYPES
STRING_TYPES = ["char", "varchar", "text"]
TEMPORAL_TYPES = ["date", "datetime", "timestamp", "time"]

# 数值族内部的精度序号
NUMERIC_RANK = {t: i for i, t in enumerate(NUMERIC_TYPES)}


@dataclass(frozen=True)
class DataType:
    """列/字面量的逻辑类型。

    kind      : 基础类型名
    length    : 字符串长度或数值显示宽度（可空）
    precision : decimal 的总位数（可空）
    scale     : decimal 的小数位数（可空）
    charset   : 字符串类型的字符集（数值/日期为 None）
    collation : 字符串类型的排序规则（可空，None 表示取字符集默认）
    unsigned  : 数值是否无符号
    """

    kind: str
    length: int | None = None
    precision: int | None = None
    scale: int | None = None
    charset: str | None = None
    collation: str | None = None
    unsigned: bool = False

    def __post_init__(self):
        if self.kind in STRING_TYPES:
            cs = self.charset or "utf8mb4"
            object.__setattr__(self, "charset", cs)
            if not self.collation:
                object.__setattr__(self, "collation", DEFAULT_COLLATION[cs])
        else:
            object.__setattr__(self, "charset", None)
            object.__setattr__(self, "collation", None)

    # ------------------------------------------------------------ 类型族判断
    @property
    def is_numeric(self) -> bool:
        return self.kind in NUMERIC_TYPES

    @property
    def is_integer(self) -> bool:
        return self.kind in INT_TYPES

    @property
    def is_float(self) -> bool:
        return self.kind in FLOAT_TYPES

    @property
    def is_string(self) -> bool:
        return self.kind in STRING_TYPES

    @property
    def is_temporal(self) -> bool:
        return self.kind in TEMPORAL_TYPES

    @property
    def is_decimal(self) -> bool:
        return self.kind == "decimal"

    # ------------------------------------------------------------ 展示
    @property
    def display(self) -> str:
        if self.kind == "decimal" and self.precision is not None:
            s = f"decimal({self.precision},{self.scale or 0})"
        elif self.length is not None and self.kind in STRING_TYPES + INT_TYPES:
            s = f"{self.kind}({self.length})"
        else:
            s = self.kind
        if self.unsigned:
            s += " unsigned"
        if self.is_string:
            s += f" {self.charset}/{self.collation}"
        return s

    def with_collation(self, collation: str) -> "DataType":
        return DataType(
            kind=self.kind, length=self.length, precision=self.precision,
            scale=self.scale, charset=charset_of(collation), collation=collation,
            unsigned=self.unsigned,
        )

    def with_charset(self, charset: str) -> "DataType":
        return DataType(
            kind=self.kind, length=self.length, precision=self.precision,
            scale=self.scale, charset=charset,
            collation=DEFAULT_COLLATION[charset], unsigned=self.unsigned,
        )

    def to_dict(self) -> dict:
        return {
            "kind": self.kind, "length": self.length, "precision": self.precision,
            "scale": self.scale, "charset": self.charset,
            "collation": self.collation, "unsigned": self.unsigned,
        }

    @staticmethod
    def from_dict(d: dict) -> "DataType":
        return DataType(**d)


def parse_type(text: str) -> DataType:
    """解析简化的类型声明，例如 ``VARCHAR(32) CHARACTER SET utf8 COLLATE utf8_bin``。"""
    s = text.strip().lower()
    unsigned = "unsigned" in s
    s = s.replace("unsigned", "")
    charset = collation = None
    if "collate" in s:
        head, after = s.split("collate", 1)
        collation = after.strip().split()[0]
        s = head.strip()
    if "character set" in s:
        head, after = s.split("character set", 1)
        charset = after.strip().split()[0]
        s = head.strip()
    s = s.strip().rstrip(",").strip()
    if "(" in s:
        kind, rest = s.split("(", 1)
        kind = kind.strip()
        nums = [int(x) for x in rest.rstrip(")").split(",") if x.strip()]
        length = nums[0] if len(nums) == 1 else None
        precision = nums[0] if len(nums) == 2 else None
        scale = nums[1] if len(nums) == 2 else None
    else:
        kind = s.strip()
        length = precision = scale = None
    if collation and not charset:
        charset = charset_of(collation)
    return DataType(
        kind=kind, length=length, precision=precision, scale=scale,
        charset=charset, collation=collation, unsigned=unsigned,
    )
