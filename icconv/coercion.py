# -*- coding: utf-8 -*-
"""隐式类型转换规则引擎（参照 MySQL 8.0 行为的简化模型）。

关键规则（与主流数据库真实行为对齐）：

1. 数值 vs 数值：低精度提升到高精度（tinyint<...<bigint<decimal<float<double）。
2. 字符串 vs 数值：**字符串一侧转为数值**（MySQL 铁律），与字符串在哪一侧无关——
   字符串是列时，该列每行都被转换，索引失效；字符串是常量时，只转一次，列索引可用。
3. 日期 vs 字符串：字符串转日期/日期时间。
4. 日期 vs 数值：两侧都转 double（日期变 YYYYMMDD 数字，MySQL 行为），日期列索引失效。
5. 字符串 vs 字符串（不同字符集/排序规则）：
   - 列 vs 常量：常量迁就列（coercibility 规则），列索引可用；
   - 列 vs 列：按排序规则/字符集优先级，低优先级一侧被 CONVERT，其索引失效；
   - 字符集转换可能产生乱码（运行时由 values.convert_charset 告警）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .types import (
    CHARSET_PRECEDENCE, COLLATIONS, NUMERIC_RANK, DataType,
)
from .values import Val, convert

# 规则编号
RULE_NONE = "NONE"
RULE_NUMERIC_PROMOTION = "NUMERIC_PROMOTION"
RULE_STRING_TO_NUMBER = "STRING_TO_NUMBER"
RULE_STRING_TO_TEMPORAL = "STRING_TO_TEMPORAL"
RULE_TEMPORAL_TO_NUMBER = "RULE_TEMPORAL_TO_NUMBER"
RULE_COLLATION = "COLLATION_MISMATCH"
RULE_SAME_FAMILY = "SAME_FAMILY"


@dataclass
class Operand:
    """参与比较/连接的一侧操作数。"""

    dtype: DataType
    kind: str = "expr"            # column / literal / expr
    table: str | None = None
    column: str | None = None
    # 字符串可强制度：列=2，常量=3，显式 COLLATE=0；数字越小越"强"
    coercibility: int = 2

    @property
    def is_column(self) -> bool:
        return self.kind == "column"

    def label(self) -> str:
        if self.is_column:
            return f"{self.table}.{self.column}"
        if self.kind == "literal":
            return "常量"
        return "表达式"


@dataclass
class CoercionStep:
    side: str                     # left / right
    operand: str                  # 展示名
    from_type: str
    to_type: str
    on_column: bool
    index_blocked: bool
    reason: str


@dataclass
class CoercionResult:
    rule: str
    rule_text: str
    left_target: DataType | None
    right_target: DataType | None
    common_type: DataType
    steps: list = field(default_factory=list)
    blocks_left_index: bool = False
    blocks_right_index: bool = False
    # 静态可预见的运行时风险（前导零、截断、乱码、非法日期…）
    potential_risks: list = field(default_factory=list)

    @property
    def has_conversion(self) -> bool:
        return self.left_target is not None or self.right_target is not None

    @property
    def blocked_sides(self) -> list:
        out = []
        if self.blocks_left_index:
            out.append("left")
        if self.blocks_right_index:
            out.append("right")
        return out

    def apply(self, left: Val, right: Val) -> tuple:
        """执行期：按决定把两侧 Val 转成目标类型，返回 (left', right') 与告警。"""
        warns = []
        if self.left_target is not None and not left.is_null:
            if left.dtype != self.left_target:
                left = convert(left, self.left_target)
                warns.extend(left.warnings)
        if self.right_target is not None and not right.is_null:
            if right.dtype != self.right_target:
                right = convert(right, self.right_target)
                warns.extend(right.warnings)
        return left, right, warns


def _step(side: str, op: Operand, target: DataType, reason: str) -> CoercionStep:
    blocked = op.is_column  # 转换落在列上 => 该列索引失效
    return CoercionStep(
        side=side, operand=op.label(),
        from_type=op.dtype.display, to_type=target.display,
        on_column=op.is_column, index_blocked=blocked, reason=reason,
    )


def decide(left: Operand, right: Operand) -> CoercionResult:
    """决定两个操作数之间的隐式转换方案（按类型语义签名缓存）。

    缓存键只含类型族/精度/字符集/排序规则/可强制度/操作数角色，
    不含具体表名列名——同型语义的决策结果天然一致，可跨查询安全复用。
    """
    cache = decide._cache
    key = (left.dtype, right.dtype, left.kind, right.kind,
           left.coercibility, right.coercibility,
           left.dtype.collation if left.dtype.is_string else None,
           right.dtype.collation if right.dtype.is_string else None)
    hit = cache.get(key)
    if hit is not None:
        return hit
    res = _decide_uncached(left, right)
    cache[key] = res
    return res


decide._cache = {}


def decide_cache_clear():
    decide._cache.clear()


def _decide_uncached(left: Operand, right: Operand) -> CoercionResult:
    lt, rt = left.dtype, right.dtype

    # ---------- 完全一致（字符串还要求排序规则一致）
    if lt == rt and (not lt.is_string or lt.collation == rt.collation):
        return CoercionResult(
            RULE_NONE, "两侧类型与排序规则完全一致，无需转换",
            None, None, lt,
        )

    # ---------- 数值 vs 数值
    if lt.is_numeric and rt.is_numeric:
        if lt == rt:
            return CoercionResult(RULE_NONE, "两侧数值类型一致，无需转换", None, None, lt)
        # 列 vs 常量：常量永远迁就列，列侧不发生转换（索引可用）
        if left.is_column and not right.is_column:
            target, lo_side, lo_op = lt, "right", right
        elif right.is_column and not left.is_column:
            target, lo_side, lo_op = rt, "left", left
        else:
            # 列 vs 列（或表达式）：按精度提升，低精度一侧被转换
            if NUMERIC_RANK[lt.kind] >= NUMERIC_RANK[rt.kind]:
                target, lo_side, lo_op = lt, "right", right
            else:
                target, lo_side, lo_op = rt, "left", left
        reason = (f"数值精度协调规则：{lo_op.label()} 的 {lo_op.dtype.display} "
                  f"转为 {target.display} 后比较"
                  + ("" if lo_op.is_column else "（转换只发生在常量上，列索引可用）"))
        step = _step(lo_side, lo_op, target, reason)
        res = CoercionResult(
            RULE_NUMERIC_PROMOTION, reason,
            target if lo_side == "left" else None,
            target if lo_side == "right" else None,
            target, [step],
        )
        res.blocks_left_index = step.index_blocked and lo_side == "left"
        res.blocks_right_index = step.index_blocked and lo_side == "right"
        return res

    # ---------- 字符串 vs 数值（MySQL：字符串转数值）
    if (lt.is_string and rt.is_numeric) or (lt.is_numeric and rt.is_string):
        if lt.is_string:
            str_op, num_op, str_side = left, right, "left"
        else:
            str_op, num_op, str_side = right, left, "right"
        # 字符串侧转数值：常量侧转换时目标贴合列的数值类型（保证索引探测键同型），
        # 列侧转换时按 MySQL 规则转 double
        if str_op.is_column:
            target = DataType("double")
            common = DataType("double")
        else:
            target = num_op.dtype
            common = num_op.dtype
        reason = (f"字符串与数值比较：MySQL 规则把字符串一侧 {str_op.label()} "
                  f"({str_op.dtype.display}) 转为 {target.display} 再比较，"
                  + ("转换落在列上，该列索引失效" if str_op.is_column
                     else "转换只发生在常量上，列侧索引不受影响"))
        steps = [_step(str_side, str_op, target, reason)]
        risks = [
            "字符串中的前导零在数值化后丢失（'007'→7），可能改变匹配结果",
            "含非数字后缀的字符串只取前导数字（'13800001234x'→13800001234）",
            "空串或纯非数字字符串变为 0，可能意外匹配到数值 0 的行",
        ]
        res = CoercionResult(
            RULE_STRING_TO_NUMBER, reason,
            target if str_side == "left" else None,
            target if str_side == "right" else None,
            common, steps, potential_risks=risks,
        )
        res.blocks_left_index = str_op.is_column and str_side == "left"
        res.blocks_right_index = str_op.is_column and str_side == "right"
        return res

    # ---------- 日期 vs 数值：两侧都转 double
    if (lt.is_temporal and rt.is_numeric) or (lt.is_numeric and rt.is_temporal):
        if lt.is_temporal:
            t_op, t_side, n_op, n_side = left, "left", right, "right"
        else:
            t_op, t_side, n_op, n_side = right, "right", left, "left"
        dbl = DataType("double")
        reason = ("日期与数值比较：MySQL 把日期转为 YYYYMMDD(HHMMSS) 形式的 double，"
                  "数值一侧也按 double 比较；日期列上的转换使其索引失效")
        steps = [
            _step(t_side, t_op, dbl, "日期列/值数值化（YYYYMMDD 数字）"),
            _step(n_side, n_op, dbl, "数值统一为 double"),
        ]
        res = CoercionResult(
            RULE_TEMPORAL_TO_NUMBER, reason,
            dbl if t_side == "left" or n_side == "left" else None,
            dbl if t_side == "right" or n_side == "right" else None,
            dbl, steps,
        )
        # 两侧都被转；列侧索引失效
        res.blocks_left_index = (t_op.is_column and t_side == "left") or \
                                (n_op.is_column and n_side == "left")
        res.blocks_right_index = (t_op.is_column and t_side == "right") or \
                                 (n_op.is_column and n_side == "right")
        return res

    # ---------- 日期 vs 字符串：字符串转日期
    if (lt.is_temporal and rt.is_string) or (lt.is_string and rt.is_temporal):
        if lt.is_temporal:
            t_dtype, s_op, s_side = lt, right, "right"
        else:
            t_dtype, s_op, s_side = rt, left, "left"
        target = DataType(t_dtype.kind)
        reason = (f"日期与字符串比较：字符串一侧 {s_op.label()} 转为 {t_dtype.kind}；"
                  + ("转换落在字符串列上，该列索引失效" if s_op.is_column
                     else "转换只发生在常量上，日期列索引可用"))
        steps = [_step(s_side, s_op, target, reason)]
        # 常量侧转换是标准写法，仅当执行中真有坏日期才告警，不作为静态潜在风险；
        # 字符串列转日期则确有数据风险
        risks = (["不符合日期格式的字符串转为 NULL，对应行不会被匹配（与预期可能不符）"]
                 if s_op.is_column else [])
        res = CoercionResult(
            RULE_STRING_TO_TEMPORAL, reason,
            target if s_side == "left" else None,
            target if s_side == "right" else None,
            target, steps, potential_risks=risks,
        )
        res.blocks_left_index = s_op.is_column and s_side == "left"
        res.blocks_right_index = s_op.is_column and s_side == "right"
        return res

    # ---------- 字符串 vs 字符串：字符集/排序规则协调
    if lt.is_string and rt.is_string:
        return _decide_collation(left, right)

    raise TypeError(f"暂不支持的类型组合：{lt.display} vs {rt.display}")


def _decide_collation(left: Operand, right: Operand) -> CoercionResult:
    lt, rt = left.dtype, right.dtype
    same_coll = lt.collation == rt.collation
    same_charset = lt.charset == rt.charset

    if same_coll:
        return CoercionResult(
            RULE_NONE, "字符串类型不同但排序规则一致，按相同排序规则比较，无需转换",
            None, None, lt,
        )

    # 决定谁"输"：先比 coercibility（数字大者输），平手再比优先级
    if left.coercibility != right.coercibility:
        loser_side = "left" if left.coercibility > right.coercibility else "right"
        winner_side = "right" if loser_side == "left" else "left"
        why = "可强制度规则（列强于常量，常量迁就列）"
    else:
        lp = COLLATIONS[lt.collation]["precedence"]
        rp = COLLATIONS[rt.collation]["precedence"]
        if lp != rp:
            loser_side = "left" if lp > rp else "right"
        else:
            # 同优先级不同字符集：按字符集优先级
            lc = CHARSET_PRECEDENCE[lt.charset]
            rc = CHARSET_PRECEDENCE[rt.charset]
            loser_side = "left" if lc > rc else "right"
        winner_side = "right" if loser_side == "left" else "left"
        why = "排序规则/字符集优先级"

    loser = left if loser_side == "left" else right
    winner = right if winner_side == "right" else left
    # 输方转为赢方的字符集+排序规则，保持自身字符串大类与长度
    target = DataType(
        kind=loser.dtype.kind, length=loser.dtype.length,
        charset=winner.dtype.charset, collation=winner.dtype.collation,
    )
    charset_change = loser.dtype.charset != winner.dtype.charset
    reason = (
        f"排序规则不一致（{lt.collation} vs {rt.collation}）：{why}，"
        f"{loser.label()} 被 CONVERT 到 {winner.dtype.charset}/"
        f"{winner.dtype.collation}"
        + ("，并伴随字符集转换" if charset_change else "")
        + ("；转换落在列上，该列索引失效" if loser.is_column
           else "；转换只发生在常量上，列侧索引不受影响")
    )
    risks = []
    if charset_change:
        risks.append(
            f"字符集 {loser.dtype.charset}→{winner.dtype.charset} 转换中，"
            "目标字符集无法编码的字符会变成乱码/问号")
    steps = [_step(loser_side, loser, target, reason)]
    res = CoercionResult(
        RULE_COLLATION, reason,
        target if loser_side == "left" else None,
        target if loser_side == "right" else None,
        target, steps, potential_risks=risks,
    )
    res.blocks_left_index = loser.is_column and loser_side == "left"
    res.blocks_right_index = loser.is_column and loser_side == "right"
    return res
