# -*- coding: utf-8 -*-
"""pytest 测试：运行时转换的 MySQL 行为与告警。"""
import os
import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from icconv.types import DataType
from icconv.values import Val, compare, convert


class TestStringToNumber:
    def test_leading_zero_warns(self):
        out = convert(Val("007", DataType("varchar", length=8)), DataType("bigint"))
        assert out.value == 7
        assert any(w.code == "leading_zero_lost" for w in out.warnings)

    def test_numeric_suffix_truncated(self):
        out = convert(Val("13800001234x", DataType("varchar", length=20)),
                      DataType("double"))
        assert out.value == 13800001234.0
        assert any(w.code == "trunc_numeric_prefix" for w in out.warnings)

    def test_non_numeric_becomes_zero(self):
        out = convert(Val("abc", DataType("varchar", length=8)), DataType("bigint"))
        assert out.value == 0
        assert any(w.code == "str_to_num_zero" for w in out.warnings)

    def test_empty_string_zero(self):
        out = convert(Val("", DataType("varchar", length=4)), DataType("bigint"))
        assert out.value == 0

    def test_scientific_notation(self):
        out = convert(Val("1.5e3", DataType("varchar", length=8)), DataType("bigint"))
        assert out.value == 1500


class TestNumberToString:
    def test_int_roundtrip(self):
        out = convert(Val(42, DataType("bigint")), DataType("varchar", length=10))
        assert out.value == "42" and not out.warnings

    def test_decimal_display(self):
        out = convert(Val(Decimal("12.30"), DataType("decimal", precision=6, scale=2)),
                      DataType("varchar", length=10))
        assert out.value == "12.30"


class TestDate:
    def test_string_formats(self):
        for s, want in [("2025-03-01", date(2025, 3, 1)),
                        ("2025/03/01", date(2025, 3, 1)),
                        ("20250301", date(2025, 3, 1))]:
            out = convert(Val(s, DataType("varchar", length=10)), DataType("date"))
            assert out.value == want

    def test_bad_date_null_with_warning(self):
        out = convert(Val("not-a-date", DataType("varchar", length=12)),
                      DataType("date"))
        assert out.value is None
        assert any(w.code == "bad_date" for w in out.warnings)


class TestCharset:
    def test_rare_char_latin1_loss(self):
        out = convert(Val("𠮷", DataType("varchar", length=4, charset="utf8mb4")),
                      DataType("varchar", length=4, charset="latin1",
                               collation="latin1_swedish_ci"))
        assert any(w.code == "charset_loss" for w in out.warnings)

    def test_ascii_utf8mb4_to_latin1_ok(self):
        out = convert(Val("abc", DataType("varchar", length=4, charset="utf8mb4")),
                      DataType("varchar", length=4, charset="latin1",
                               collation="latin1_swedish_ci"))
        assert not any(w.code == "charset_loss" for w in out.warnings)


class TestCompare:
    def test_three_valued_logic_null(self):
        assert compare(Val(None, DataType("bigint")), "=",
                       Val(1, DataType("bigint"))) is None

    def test_case_insensitive_collation(self):
        a = Val("AbC", DataType("varchar", length=8,
                                collation="utf8mb4_general_ci"))
        b = Val("aBc", DataType("varchar", length=8,
                                collation="utf8mb4_general_ci"))
        assert compare(a, "=", b) is True

    def test_case_sensitive_collation(self):
        a = Val("AbC", DataType("varchar", length=8, collation="utf8mb4_bin"))
        b = Val("aBc", DataType("varchar", length=8, collation="utf8mb4_bin"))
        assert compare(a, "=", b) is False
