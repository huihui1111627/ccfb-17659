# -*- coding: utf-8 -*-
"""带逻辑类型的运行时值，以及参照 MySQL 行为实现的转换函数。

每次转换都可能产生告警（warning），告警正是"结果与预期不同"类问题的来源，
例如：
  * '007' -> 7，前导零丢失，再转回字符串得到 '7' 而非 '007'；
  * '13800001234abc' -> 13800001234，尾部非数字被静默截断；
  * '' / 'abc' -> 0（MySQL 真实行为，会产生 warning）；
  * 字符串按 utf8mb4 存的生僻字转 latin1/gbk 时无法编码。
"""
from __future__ import annotations

import codecs
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .types import COLLATIONS, DataType

NULL = None

_NUM_PREFIX = re.compile(r"^\s*([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)")
_DATE_PATTERNS = ("%Y-%m-%d", "%Y/%m/%d", "%Y%m%d")
_DT_PATTERNS = (
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S",
    "%Y%m%d%H%M%S", "%Y-%m-%d %H:%i:%s",
)


@dataclass
class ConversionWarning:
    code: str
    message: str
    detail: dict = field(default_factory=dict)


@dataclass
class Val:
    """运行时值：Python 原生值 + 逻辑类型 + 转换告警。"""

    value: object          # int / float / Decimal / str / date / datetime / None
    dtype: DataType
    warnings: list = field(default_factory=list)

    @property
    def is_null(self) -> bool:
        return self.value is NULL

    # ------------------------------------------------------------ JSON 落盘
    def to_json(self) -> dict:
        v = self.value
        if isinstance(v, datetime):
            v = {"__dt__": v.strftime("%Y-%m-%d %H:%M:%S")}
        elif isinstance(v, date):
            v = {"__date__": v.isoformat()}
        elif isinstance(v, Decimal):
            v = {"__dec__": str(v)}
        return {"v": v, "t": self.dtype.to_dict()}

    @staticmethod
    def from_json(d: dict) -> "Val":
        v, t = d["v"], DataType.from_dict(d["t"])
        if isinstance(v, dict):
            if "__dt__" in v:
                v = datetime.strptime(v["__dt__"], "%Y-%m-%d %H:%M:%S")
            elif "__date__" in v:
                v = date.fromisoformat(v["__date__"])
            elif "__dec__" in v:
                v = Decimal(v["__dec__"])
        return Val(v, t)


# ================================================================ 转换函数

def _warn(code: str, message: str, **detail) -> ConversionWarning:
    return ConversionWarning(code, message, detail)


def to_int(v: Val, dtype: DataType) -> Val:
    """字符串转整数：取前导数字，忽略前导空格，无法解析得 0（MySQL 行为）。"""
    warns = list(v.warnings)
    if v.is_null:
        return Val(NULL, dtype, warns)
    x = v.value
    if isinstance(x, bool):
        x = int(x)
    if isinstance(x, int):
        num = x
    elif isinstance(x, float) or isinstance(x, Decimal):
        num = int(x)  # MySQL: 向零截断
        if float(x) != num:
            warns.append(_warn("trunc_decimal", "小数部分在转整数时被截断", original=str(x)))
    elif isinstance(x, str):
        m = _NUM_PREFIX.match(x)
        if not m:
            num = 0
            warns.append(_warn(
                "str_to_num_zero", f"字符串 {x!r} 没有前导数字，转换结果为 0", original=x))
        else:
            token = m.group(1)
            num = int(Decimal(token))  # 先走 Decimal 以支持科学计数法
            if m.end() != len(x.rstrip()):
                warns.append(_warn(
                    "trunc_numeric_prefix",
                    f"字符串 {x!r} 仅前导数字部分 {token!r} 被转换，其余被丢弃",
                    original=x, used=token))
            if x.lstrip().startswith(("0", "+0", "-0")) and token.lstrip("+-").lstrip("0") not in ("",):
                warns.append(_warn(
                    "leading_zero_lost",
                    f"字符串 {x!r} 的前导零在数值化后丢失", original=x, converted=num))
    elif isinstance(x, (date, datetime)):
        # MySQL: 日期转数字得到 YYYYMMDD / YYYYMMDDHHMMSS
        if isinstance(x, datetime):
            num = int(x.strftime("%Y%m%d%H%M%S"))
        else:
            num = int(x.strftime("%Y%m%d"))
    else:
        raise TypeError(f"无法把 {type(x)} 转成整数")
    return Val(num, dtype, warns)


def to_float(v: Val, dtype: DataType) -> Val:
    warns = list(v.warnings)
    if v.is_null:
        return Val(NULL, dtype, warns)
    x = v.value
    if isinstance(x, (int, float)):
        return Val(float(x), dtype, warns)
    if isinstance(x, Decimal):
        return Val(float(x), dtype, warns)
    if isinstance(x, str):
        m = _NUM_PREFIX.match(x)
        if not m:
            warns.append(_warn(
                "str_to_num_zero", f"字符串 {x!r} 没有前导数字，转换结果为 0", original=x))
            return Val(0.0, dtype, warns)
        token = m.group(1)
        f = float(token)
        if m.end() != len(x.rstrip()):
            warns.append(_warn(
                "trunc_numeric_prefix",
                f"字符串 {x!r} 仅前导数字部分 {token!r} 被转换，其余被丢弃",
                original=x, used=token))
        if x.lstrip().startswith(("0", "+0", "-0")) and token.lstrip("+-").lstrip("0") not in ("",):
            warns.append(_warn("leading_zero_lost",
                               f"字符串 {x!r} 的前导零在数值化后丢失", original=x, converted=f))
        return Val(f, dtype, warns)
    if isinstance(x, (date, datetime)):
        return Val(float(to_int(v, dtype).value), dtype, warns)
    raise TypeError(f"无法把 {type(x)} 转成浮点")


def to_decimal(v: Val, dtype: DataType) -> Val:
    warns = list(v.warnings)
    if v.is_null:
        return Val(NULL, dtype, warns)
    x = v.value
    prec, scale = dtype.precision, dtype.scale
    if isinstance(x, Decimal):
        d = x
    elif isinstance(x, (int, float)):
        d = Decimal(str(x))
    elif isinstance(x, str):
        m = _NUM_PREFIX.match(x)
        if not m:
            warns.append(_warn(
                "str_to_num_zero", f"字符串 {x!r} 没有前导数字，转换结果为 0", original=x))
            d = Decimal(0)
        else:
            token = m.group(1)
            d = Decimal(token)
            if m.end() != len(x.rstrip()):
                warns.append(_warn(
                    "trunc_numeric_prefix",
                    f"字符串 {x!r} 仅前导数字部分 {token!r} 被转换，其余被丢弃",
                    original=x, used=token))
            if x.lstrip().startswith(("0", "+0", "-0")) and token.lstrip("+-").lstrip("0") not in ("",):
                warns.append(_warn("leading_zero_lost",
                                   f"字符串 {x!r} 的前导零在数值化后丢失", original=x))
    elif isinstance(x, (date, datetime)):
        d = Decimal(to_int(v, DataType("bigint")).value)
    else:
        raise TypeError(f"无法把 {type(x)} 转成 decimal")
    if scale is not None:
        quant = Decimal(10) ** -scale
        rounded = d.quantize(quant)
        if rounded != d:
            warns.append(_warn("decimal_round",
                               f"{d} 按 decimal({prec},{scale}) 四舍五入为 {rounded}",
                               original=str(d), rounded=str(rounded)))
        d = rounded
    return Val(d, dtype, warns)


def to_string(v: Val, dtype: DataType) -> Val:
    warns = list(v.warnings)
    if v.is_null:
        return Val(NULL, dtype, warns)
    x = v.value
    if isinstance(x, str):
        s = x
    elif isinstance(x, float):
        # MySQL 风格：尽量不用科学计数法
        s = repr(x) if not math.isfinite(x) else ("%f" % x).rstrip("0").rstrip(".")
        if "." not in s:
            s += ".0" if x != int(x) else ""
    elif isinstance(x, Decimal):
        s = format(x, "f")
    elif isinstance(x, datetime):
        s = x.strftime("%Y-%m-%d %H:%M:%S")
    elif isinstance(x, date):
        s = x.isoformat()
    else:
        s = str(x)
    if dtype.length is not None and len(s) > dtype.length:
        warns.append(_warn("string_trunc",
                           f"字符串 {s!r} 超过 {dtype.kind}({dtype.length}) 长度被截断",
                           original=s, length=dtype.length))
        s = s[: dtype.length]
    return Val(s, dtype, warns)


def convert_charset(v: Val, target: DataType) -> Val:
    """字符集转换：尝试真实编码，不能编码即数据丢失（与 MySQL 报 Illegal mix 对照）。"""
    warns = list(v.warnings)
    if v.is_null or not target.is_string:
        return Val(v.value, target, warns)
    enc = target.charset
    try:
        encoded = str(v.value).encode(enc, errors="strict")
        # 再解码回来确认可逆（gbk/latin1 各自能编但字符集语义不同）
        encoded.decode(enc, errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError, LookupError):
        warns.append(_warn(
            "charset_loss",
            f"字符 {v.value!r} 无法无损转为字符集 {enc}，存在乱码/丢失风险",
            original=str(v.value), charset=enc))
    return Val(str(v.value), target, warns)


def to_date(v: Val, dtype: DataType) -> Val:
    warns = list(v.warnings)
    if v.is_null:
        return Val(NULL, dtype, warns)
    x = v.value
    if isinstance(x, datetime):
        return Val(x.date(), dtype, warns)
    if isinstance(x, date):
        return Val(x, dtype, warns)
    if isinstance(x, (int, float, Decimal)):
        # MySQL: 数字 20240101 -> 日期
        s = str(int(x))
        try:
            return Val(datetime.strptime(s, "%Y%m%d").date(), dtype, warns)
        except ValueError:
            warns.append(_warn("bad_date", f"{x!r} 不是合法日期", original=str(x)))
            return Val(NULL, dtype, warns)
    if isinstance(x, str):
        for pat in _DATE_PATTERNS:
            try:
                return Val(datetime.strptime(x.strip(), pat).date(), dtype, warns)
            except ValueError:
                continue
        warns.append(_warn("bad_date", f"字符串 {x!r} 不是合法日期，转换结果为 NULL", original=x))
        return Val(NULL, dtype, warns)
    raise TypeError(f"无法把 {type(x)} 转成 date")


def to_datetime(v: Val, dtype: DataType) -> Val:
    warns = list(v.warnings)
    if v.is_null:
        return Val(NULL, dtype, warns)
    x = v.value
    if isinstance(x, datetime):
        return Val(x, dtype, warns)
    if isinstance(x, date):
        return Val(datetime(x.year, x.month, x.day), dtype, warns)
    if isinstance(x, (int, float, Decimal)):
        s = str(int(x))
        try:
            return Val(datetime.strptime(s.ljust(14, "0"), "%Y%m%d%H%M%S"), dtype, warns)
        except ValueError:
            warns.append(_warn("bad_datetime", f"{x!r} 不是合法日期时间", original=str(x)))
            return Val(NULL, dtype, warns)
    if isinstance(x, str):
        s = x.strip()
        for pat in _DT_PATTERNS + _DATE_PATTERNS:
            try:
                return Val(datetime.strptime(s, pat), dtype, warns)
            except ValueError:
                continue
        warns.append(_warn("bad_datetime",
                           f"字符串 {x!r} 不是合法日期时间，转换结果为 NULL", original=x))
        return Val(NULL, dtype, warns)
    raise TypeError(f"无法把 {type(x)} 转成 datetime")


def convert(v: Val, target: DataType) -> Val:
    """按目标类型分派的统一转换入口。"""
    if v.is_null:
        return Val(NULL, target, list(v.warnings))
    if target.is_integer:
        out = to_int(v, target)
    elif target.is_float:
        out = to_float(v, target)
    elif target.is_decimal:
        out = to_decimal(v, target)
    elif target.is_string:
        out = to_string(v, DataType(kind=target.kind, length=target.length,
                                    charset=v.dtype.charset or target.charset,
                                    collation=v.dtype.collation or target.collation))
        if target.charset and v.dtype.is_string and v.dtype.charset != target.charset:
            out = convert_charset(out, target)
        else:
            out = Val(out.value, target, out.warnings)
    elif target.kind == "date":
        out = to_date(v, target)
    elif target.kind in ("datetime", "timestamp"):
        out = to_datetime(v, target)
    elif target.kind == "time":
        raise NotImplementedError("time 类型转换暂未实现")
    else:
        raise TypeError(f"未知目标类型 {target}")
    return out


# ================================================================ 比较

def _numeric_key(v: Val):
    """把数值族统一成可比较的 Python 值（Decimal 承载，避免浮点噪音）。"""
    x = v.value
    if isinstance(x, Decimal):
        return x
    return Decimal(str(x))


def compare(a: Val, op: str, b: Val) -> bool:
    """三值逻辑比较，返回 True/False/None(=UNKNOWN)。"""
    if a.is_null or b.is_null:
        return None
    ta, tb = a.dtype, b.dtype
    if ta.is_string and tb.is_string:
        coll = ta.collation
        cs = COLLATIONS[coll]["case_sensitive"]
        x, y = str(a.value), str(b.value)
        if not cs:  # _ci：大小写不敏感
            x, y = x.lower(), y.lower()
        if op == "=":
            return x == y
        if op == "<>":
            return x != y
        if op == "<":
            return x < y
        if op == "<=":
            return x <= y
        if op == ">":
            return x > y
        if op == ">=":
            return x >= y
        if op == "like":
            return _like(x, y)
        if op == "in":
            return x in [z.lower() if not cs else z for z in b.value]
    if ta.is_numeric and tb.is_numeric:
        x, y = _numeric_key(a), _numeric_key(b)
    elif ta.is_temporal and tb.is_temporal:
        x, y = a.value, b.value
    else:
        # 走到这里说明 coercion 层没处理好，兜底按字符串
        x, y = str(a.value), str(b.value)
    if op == "=":
        return x == y
    if op == "<>":
        return x != y
    if op == "<":
        return x < y
    if op == "<=":
        return x <= y
    if op == ">":
        return x > y
    if op == ">=":
        return x >= y
    if op == "in":
        return x in y
    raise ValueError(f"不支持的比较操作 {op}")


def _like(text: str, pattern: str) -> bool:
    """SQL LIKE：% 任意串，_ 单字符。"""
    rx = "^"
    for ch in pattern:
        if ch == "%":
            rx += ".*"
        elif ch == "_":
            rx += "."
        else:
            rx += re.escape(ch)
    rx += "$"
    return re.match(rx, text, flags=re.DOTALL) is not None
