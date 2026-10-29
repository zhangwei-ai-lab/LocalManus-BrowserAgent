"""工具层文本归一的单元测试。

运行：
    python -m pytest tests/test_text_normalize.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.tool.str_replace_editor import normalize_multiline_text  # noqa: E402


def test_literal_newlines_are_restored():
    """多条字面量 \\n 且没有真实换行 → 还原成多行。"""
    assert normalize_multiline_text(r"第一行\n第二行\n第三行") == "第一行\n第二行\n第三行"


def test_real_newlines_are_untouched():
    """已经是真实换行 → 一个字都不改。"""
    text = "第一行\n第二行"
    assert normalize_multiline_text(text) == text


def test_single_literal_is_untouched():
    """只出现一次字面量 \\n（典型是写代码/正则）→ 不猜，保持原样。"""
    text = r'print("hello\n")'
    assert normalize_multiline_text(text) == text


def test_mixed_escapes():
    assert normalize_multiline_text(r"a\r\nb\r\nc") == "a\nb\nc"
