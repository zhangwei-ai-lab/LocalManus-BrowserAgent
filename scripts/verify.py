"""端到端验证脚本：把"能跑通"变成可重复、可留痕的检查。

用法：
    python scripts/verify.py                    # 跑全部用例并写报告
    python scripts/verify.py --case write       # 只跑离线写文件
    python scripts/verify.py --case browser     # 只跑浏览器取数
    python scripts/verify.py --no-report        # 只打印，不落盘

设计说明：
1. 用例刻意做得短（<= 6 步），一条用例只验证一件事，失败时容易定位；
2. 步数不读 `agent.current_step`（跑满 max_steps 时它会被重置为 0），
   而是挂一个 loguru sink 数 "Executing step N/M" 日志，保证数据真实；
3. 产物不靠模型自述，而是对比 workspace 运行前后的文件快照（mtime/size）；
4. 结果追加到 docs/verify_report.md，方便和 README 里的验证表对齐。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORKSPACE = ROOT / "workspace"
REPORT = ROOT / "docs" / "verify_report.md"

_STEP_RE = re.compile(r"Executing step (\d+)/(\d+)")

CASES = {
    "write": {
        "desc": "离线写文件（不联网，验证 思考→工具→落盘 闭环）",
        "max_steps": 6,
        "artifact": "verify_write.md",
        "prompt": (
            "在 workspace 下新建 verify_write.md，用不超过 80 字说明 ReAct 循环"
            "由哪三个阶段组成，最后回报该文件的绝对路径。"
        ),
    },
    "browser": {
        "desc": "浏览器取数 + 落盘（验证 Playwright 与页面元素解析）",
        "max_steps": 8,
        "artifact": "verify_browser.md",
        "prompt": (
            "打开 https://example.com ，提取页面 H1 标题文字，"
            "写入 workspace/verify_browser.md（用绝对路径、command=create），"
            "并回报文件路径。"
        ),
    },
}


def snapshot() -> dict[str, tuple[float, int]]:
    """记录 workspace 当前文件状态，用于判断哪些文件是本次运行新产出/被改写的。"""
    state: dict[str, tuple[float, int]] = {}
    if not WORKSPACE.exists():
        return state
    for path in WORKSPACE.rglob("*"):
        if path.is_file() and path.name != ".gitkeep":
            stat = path.stat()
            state[str(path.relative_to(WORKSPACE))] = (stat.st_mtime, stat.st_size)
    return state


def diff(before: dict, after: dict) -> list[str]:
    return sorted(
        name
        for name, meta in after.items()
        if name not in before or before[name] != meta
    )


async def run_case(name: str, settings: dict) -> dict:
    from app.agent.manus import Manus
    from app.config import config
    from app.logger import logger

    steps = {"count": 0, "limit": 0}

    def sink(message) -> None:
        match = _STEP_RE.search(str(message))
        if match:
            steps["count"] = int(match.group(1))
            steps["limit"] = int(match.group(2))

    sink_id = logger.add(sink, level="INFO", format="{message}")

    before = snapshot()
    status = config.model_status or {}
    print(f"\n[case] {name} — {settings['desc']}")
    print(f"[case] 模型 {status.get('source')} / {status.get('model')}")

    started = time.perf_counter()
    agent = await Manus.create(max_steps=settings["max_steps"])
    try:
        result = await agent.run(settings["prompt"])
        ok = True
        error = ""
    except Exception as exc:  # noqa: BLE001
        ok, error, result = False, f"{type(exc).__name__}: {exc}", ""
    finally:
        try:
            await agent.cleanup()
        finally:
            logger.remove(sink_id)

    elapsed = time.perf_counter() - started
    produced = diff(before, snapshot())
    return {
        "case": name,
        "desc": settings["desc"],
        "ok": ok,
        "error": error,
        "steps": steps["count"],
        "limit": steps["limit"] or settings["max_steps"],
        "elapsed": round(elapsed, 1),
        "model": status.get("model"),
        "source": status.get("source"),
        "produced": produced,
        "result": (result or "").strip(),
    }


def write_report(rows: list[dict]) -> None:
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = []
    if not REPORT.exists():
        lines += [
            "# 端到端验证报告",
            "",
            "> 由 `python scripts/verify.py` 自动生成/追加，每跑一次加一节，",
            "> 数据全部来自真实运行（步数取日志、耗时取 perf_counter、产物取文件快照）。",
            "",
        ]
    lines += [
        f"## {stamp}",
        "",
        "| 用例 | 结果 | 步数 | 耗时(s) | 模型 | 新产出文件 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        produced = ", ".join(row["produced"]) or "—"
        lines.append(
            f"| `{row['case']}` | {'✅ 通过' if row['ok'] else '❌ 失败'} "
            f"| {row['steps']}/{row['limit']} | {row['elapsed']} "
            f"| {row['source']} / {row['model']} | {produced} |"
        )
    lines.append("")
    for row in rows:
        if row["error"]:
            lines += [f"- `{row['case']}` 异常：`{row['error']}`", ""]
        elif row["result"]:
            tail = row["result"].splitlines()[-1]
            lines += [f"- `{row['case']}` 收尾：{tail}", ""]
    # 已存在则追加（保留历史记录），不存在则新建
    previous = REPORT.read_text(encoding="utf-8") + "\n" if REPORT.exists() else ""
    REPORT.write_text(previous + "\n".join(lines), encoding="utf-8")
    print(f"\n[report] 已追加到 {REPORT}")


async def main() -> int:
    parser = argparse.ArgumentParser(description="LocalManus 端到端验证")
    parser.add_argument("--case", choices=sorted(CASES), help="只跑指定用例")
    parser.add_argument("--no-report", action="store_true", help="不写报告文件")
    parser.add_argument("--cloud", action="store_true", help="改用 config.toml 的云端模型")
    args = parser.parse_args()

    os.environ["OPENMANUS_USE_LOCAL"] = "0" if args.cloud else "1"
    WORKSPACE.mkdir(exist_ok=True)

    selected = [args.case] if args.case else list(CASES)
    rows = [await run_case(name, CASES[name]) for name in selected]

    print("\n" + "=" * 78)
    print(f"{'用例':<10}{'结果':<8}{'步数':<8}{'耗时':<10}产物")
    print("-" * 78)
    for row in rows:
        steps_text = f"{row['steps']}/{row['limit']}"
        elapsed_text = f"{row['elapsed']}s"
        produced_text = ", ".join(row["produced"]) or "-"
        verdict = "PASS" if row["ok"] else "FAIL"
        print(
            f"{row['case']:<10}{verdict:<8}{steps_text:<8}"
            f"{elapsed_text:<10}{produced_text}"
        )
    print("=" * 78)

    if not args.no_report:
        write_report(rows)
    return 0 if all(row["ok"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
