"""进程守卫的纯逻辑测试（不启动浏览器，跑得很快）。

真正的「杀进程」行为在 Windows 上手工验证过：
启动 4 个 Chromium 子进程 → 守卫识别出属于本进程的 4 个 → sweep 后剩余 0。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.tool.proc_guard import ProcInfo, _descendants  # noqa: E402


def test_playwright_browser_is_recognized():
    proc = ProcInfo(
        pid=100,
        ppid=1,
        name="headless_shell.exe",
        path=r"C:\Users\x\AppData\Local\ms-playwright\chromium_headless_shell-1161\chrome-win\headless_shell.exe",
    )
    assert proc.is_playwright_browser


def test_user_chrome_is_not_touched():
    proc = ProcInfo(
        pid=200,
        ppid=1,
        name="chrome.exe",
        path=r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    )
    assert not proc.is_playwright_browser


def test_other_binaries_under_cache_are_ignored():
    """缓存目录下的 node.exe 不是浏览器进程，不应该被当成回收目标。"""
    proc = ProcInfo(
        pid=300,
        ppid=1,
        name="node.exe",
        path=r"C:\Users\x\AppData\Local\ms-playwright\driver\node.exe",
    )
    assert not proc.is_playwright_browser


def test_descendants_walks_the_tree():
    processes = [
        ProcInfo(1, 0, "python.exe", ""),
        ProcInfo(2, 1, "node.exe", ""),
        ProcInfo(3, 2, "headless_shell.exe", ""),
        ProcInfo(4, 3, "headless_shell.exe", ""),
        ProcInfo(5, 0, "unrelated.exe", ""),
    ]
    assert _descendants(1, processes) == {2, 3, 4}
    assert _descendants(5, processes) == set()
