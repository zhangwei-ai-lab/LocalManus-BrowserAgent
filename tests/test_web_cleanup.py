"""Web 任务的资源释放测试：报错时也必须清理浏览器。

背景：`app.py` 的 `run_task()` 原本把 `agent.cleanup()` 写在 `try` 的最后一行，
下面只要有任何一步抛异常（模型 401 / 超时 / 工具异常），清理就被跳过，Windows
下 Chromium 会变成常驻后台的孤儿进程。这个测试锁住「无论成败都清理」。
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def load_webapp():
    """按路径加载项目根目录的 app.py（它与 app/ 包同名，不能直接 import）。"""
    spec = importlib.util.spec_from_file_location("webapp_under_test", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cleanup_runs_when_agent_run_raises(monkeypatch):
    webapp = load_webapp()
    calls = {"cleanup": 0}

    class FakeAgent:
        async def run(self, prompt):  # noqa: ANN001
            raise RuntimeError("模拟模型 401 / 超时")

        async def cleanup(self):
            calls["cleanup"] += 1

    class FakeManus:
        @classmethod
        async def create(cls, **kwargs):  # noqa: ANN003
            return FakeAgent()

    monkeypatch.setattr(webapp, "Manus", FakeManus)

    task = webapp.task_manager.create_task("清理测试")
    asyncio.run(webapp.run_task(task.id, "清理测试"))

    assert calls["cleanup"] == 1, "报错路径没有调用 agent.cleanup()，浏览器会泄漏"
    assert webapp.task_manager.tasks[task.id].status.startswith("failed")


def test_cleanup_runs_on_success(monkeypatch):
    webapp = load_webapp()
    calls = {"cleanup": 0}

    class FakeAgent:
        async def run(self, prompt):  # noqa: ANN001
            return "ok"

        async def cleanup(self):
            calls["cleanup"] += 1

    class FakeManus:
        @classmethod
        async def create(cls, **kwargs):  # noqa: ANN003
            return FakeAgent()

    monkeypatch.setattr(webapp, "Manus", FakeManus)
    monkeypatch.setattr(webapp.asyncio, "sleep", _no_sleep)

    task = webapp.task_manager.create_task("成功路径")
    asyncio.run(webapp.run_task(task.id, "成功路径"))

    assert calls["cleanup"] == 1
    assert webapp.task_manager.tasks[task.id].status == "completed"


async def _no_sleep(*args, **kwargs):  # noqa: ANN002, ANN003
    return None
