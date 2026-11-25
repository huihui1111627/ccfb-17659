# -*- coding: utf-8 -*-
"""pytest 测试：隐式转换规则引擎。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icconv.coercion import (
    RULE_COLLATION, RULE_NONE, RULE_NUMERIC_PROMOTION, RULE_STRING_TO_NUMBER,
    RULE_STRING_TO_TEMPORAL, Operand, decide,
)
from icconv.types import DataType


def col(kind, **kw):
    return Operand(DataType(kind, **kw), "column", "t", "c")


def lit(kind, **kw):
    return Operand(DataType(kind, **kw), "literal", coercibility=3)


class TestStringNumber:
    def test_string_column_vs_number_literal_blocks_index(self):
        r = decide(col("varchar", length=10), lit("bigint"))
        assert r.rule == RULE_STRING_TO_NUMBER
        assert r.blocks_left_index is True
        assert r.steps[0].on_column is True

    def test_number_column_vs_string_literal_keeps_index(self):
        r = decide(col("bigint"), lit("varchar", length=4))
        assert r.rule == RULE_STRING_TO_NUMBER
        assert r.blocks_left_index is False
        # 转换目标贴合列类型，常量被转
        assert r.right_target is not None and r.right_target.kind == "bigint"

    def test_two_columns_join_string_side_blocked(self):
        r = decide(col("varchar", length=10), col("bigint"))
        assert r.rule == RULE_STRING_TO_NUMBER
        assert r.blocks_left_index is True
        assert r.blocks_right_index is False


class TestNumericPromotion:
    def test_column_vs_literal_literal_yields(self):
        r = decide(col("tinyint"), lit("bigint"))
        assert r.rule == RULE_NUMERIC_PROMOTION
        assert r.blocks_left_index is False
        assert r.right_target is not None  # 常量迁就 tinyint 列

    def test_two_columns_lower_precision_blocked(self):
        r = decide(col("int"), col("bigint"))
        assert r.rule == RULE_NUMERIC_PROMOTION
        assert r.blocks_left_index is True

    def test_same_type_no_rule(self):
        r = decide(col("int"), lit("int"))
        assert r.rule == RULE_NONE


class TestTemporal:
    def test_date_column_string_literal(self):
        r = decide(col("datetime"), lit("varchar", length=19))
        assert r.rule == RULE_STRING_TO_TEMPORAL
        assert r.blocks_left_index is False
        # 常量侧转换不作为静态潜在风险
        assert r.potential_risks == []

    def test_string_column_vs_date_has_risk(self):
        r = decide(col("varchar", length=20), col("date"))
        assert r.rule == RULE_STRING_TO_TEMPORAL
        assert r.potential_risks  # 字符串列转日期有数据风险


class TestCollation:
    def test_literal_coercibility_column_wins(self):
        c = col("varchar", length=10, charset="utf8mb4",
                collation="utf8mb4_bin")
        l = Operand(DataType("varchar", length=5, collation="utf8mb4_general_ci"),
                    "literal", coercibility=3)
        r = decide(c, l)
        assert r.rule == RULE_COLLATION
        assert r.blocks_left_index is False  # 常量迁就列

    def test_explicit_collate_literal_forces_column_conversion(self):
        c = col("varchar", length=10, charset="utf8mb4",
                collation="utf8mb4_bin")
        l = Operand(DataType("varchar", length=5, collation="utf8mb4_general_ci"),
                    "literal", coercibility=0)
        r = decide(c, l)
        assert r.rule == RULE_COLLATION
        assert r.blocks_left_index is True  # 显式 COLLATE 最强，列被转换

    def test_charset_mismatch_two_columns(self):
        a = col("varchar", length=10, charset="utf8", collation="utf8_general_ci")
        b = col("varchar", length=10, charset="gbk", collation="gbk_chinese_ci")
        r = decide(a, b)
        assert r.rule == RULE_COLLATION
        assert any("字符集" in x for x in r.potential_risks)
