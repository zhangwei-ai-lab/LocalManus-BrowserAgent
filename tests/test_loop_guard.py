"""循环保护的单元测试。

背景：小参数模型在真实站点上会「同一个 go_to_url 连点 5 次」，把步数耗光。
这里只测机制本身（指纹归一 + 连续计数），不依赖 LLM，跑得快也稳定。

运行：
    python -m pytest tests/test_loop_guard.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agent.toolcall import ToolCallAgent  # noqa: E402
from app.schema import Function, ToolCall  # noqa: E402


def call(name: str, arguments: str) -> ToolCall:
    return ToolCall(id="call_1", function=Function(name=name, arguments=arguments))


def test_signature_ignores_key_order():
    """参数键顺序不同但语义相同 → 视为同一个动作。"""
    agent = ToolCallAgent()
    a = agent._action_signature(call("go_to_url", '{"url": "https://a.com", "x": 1}'))
    b = agent._action_signature(call("go_to_url", '{"x": 1, "url": "https://a.com"}'))
    assert a == b


def test_streak_counts_consecutive_repeats():
    agent = ToolCallAgent()
    sig = agent._action_signature(call("go_to_url", '{"url": "https://a.com"}'))
    assert [agent._note_action(sig) for _ in range(4)] == [1, 2, 3, 4]


def test_streak_resets_when_action_changes():
    agent = ToolCallAgent()
    first = agent._action_signature(call("go_to_url", '{"url": "https://a.com"}'))
    second = agent._action_signature(call("extract_content", '{"goal": "h1"}'))
    agent._note_action(first)
    agent._note_action(first)
    assert agent._note_action(second) == 1
    assert agent._note_action(second) == 2


def test_thresholds_are_sane():
    """soft 必须小于 hard，否则"提示换策略"永远等不到。"""
    agent = ToolCallAgent()
    assert 1 < agent.loop_soft_limit < agent.loop_hard_limit
