"""清理 Playwright 留下来的浏览器进程（Windows 孤儿进程救援工具）。

用法：
    python scripts/cleanup_browsers.py                # 只查看，不动手（默认）
    python scripts/cleanup_browsers.py --kill         # 结束所有 ms-playwright 浏览器
    python scripts/cleanup_browsers.py --orphans      # 只结束「父进程已消失」的
    python scripts/cleanup_browsers.py --watch 60     # 监控 60 秒，有新进程就报出父子链

安全边界：只认 ``ms-playwright`` 缓存目录下的 chrome / chromium / headless_shell，
**不会碰你自己在用的 Chrome**（它在 ``C:\\Program Files\\Google\\Chrome``）。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.tool.proc_guard import (  # noqa: E402
    OrphanScanner,
    kill_process_tree,
    list_browser_processes,
    list_processes,
)


def describe() -> None:
    processes = list_processes()
    alive = {p.pid for p in processes}
    browsers = [p for p in processes if p.is_playwright_browser]

    print("=" * 78)
    print(" Playwright 浏览器进程扫描")
    print("=" * 78)
    if not browsers:
        print(" 没有发现 ms-playwright 目录下的浏览器进程。")
        print("=" * 78)
        return

    print(f" 共 {len(browsers)} 个浏览器进程：\n")
    print(f" {'PID':>7}  {'PPID':>7}  {'父进程':<8} {'进程名':<20} 路径")
    print("-" * 78)
    for proc in browsers:
        parent_state = "存活" if proc.ppid in alive else "已退出"
        path = proc.path or "<无法读取>"
        if len(path) > 62:
            path = "..." + path[-59:]
        print(f" {proc.pid:>7}  {proc.ppid:>7}  {parent_state:<8} {proc.name:<20} {path}")
    print("=" * 78)

    orphans = OrphanScanner().orphans()
    print(f" 其中父进程已退出的孤儿进程：{len(orphans)} 个")
    print(" 处理：python scripts/cleanup_browsers.py --kill   （全部）")
    print("       python scripts/cleanup_browsers.py --orphans（只清孤儿）")
    print("=" * 78)


def watch(seconds: int) -> None:
    """盯一段时间，只要有新的浏览器进程冒出来就打印父子链。"""
    known = {p.pid for p in list_browser_processes()}
    print(f"监控 {seconds}s，查找新出现的 Playwright 浏览器进程（Ctrl+C 结束）...")
    deadline = time.time() + seconds
    while time.time() < deadline:
        processes = list_processes()
        by_pid = {p.pid: p for p in processes}
        for proc in processes:
            if proc.pid in known or not proc.is_playwright_browser:
                continue
            known.add(proc.pid)
            chain = []
            current = proc
            for _ in range(6):
                chain.append(f"{current.name}({current.pid})")
                parent = by_pid.get(current.ppid)
                if parent is None:
                    chain.append(f"<已退出:{current.ppid}>")
                    break
                current = parent
            print(f"\n[{time.strftime('%H:%M:%S')}] 新浏览器进程 {proc.pid}")
            print("  链路: " + " ← ".join(chain))
            print(f"  路径: {proc.path}")
        time.sleep(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="清理 Playwright 残留浏览器进程")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--kill", action="store_true", help="结束所有 ms-playwright 浏览器进程")
    group.add_argument("--orphans", action="store_true", help="只结束父进程已退出的进程")
    parser.add_argument("--watch", type=int, default=0, help="监控指定秒数，报告新冒出来的进程")
    args = parser.parse_args()

    if args.watch:
        watch(args.watch)
        return 0

    if args.kill:
        targets = list_browser_processes()
        if not targets:
            print("没有需要清理的浏览器进程。")
            return 0
        killed = [p.pid for p in targets if kill_process_tree(p.pid)]
        print(f"已结束 {len(killed)} 个进程: {killed}")
        return 0 if len(killed) == len(targets) else 1

    if args.orphans:
        targets = OrphanScanner().orphans()
        if not targets:
            print("没有孤儿浏览器进程。")
            return 0
        killed = [p.pid for p in targets if kill_process_tree(p.pid)]
        print(f"已结束 {len(killed)} 个孤儿进程: {killed}")
        return 0

    describe()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
