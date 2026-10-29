"""环境自检脚本。

用法：
    python scripts/check_env.py

逐项检查 Python 版本、关键依赖、Ollama 本地模型、配置文件、工作目录与工具集，
每项给出 OK/WARN/FAIL，最后给出建议。写这个脚本是因为调试过程中
"少一个可选依赖" 和 "本地模型没起来" 是最常见的两类卡点。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OK, WARN, FAIL = "OK  ", "WARN", "FAIL"
_results: list[tuple[str, str, str]] = []


def record(status: str, name: str, detail: str = "") -> None:
    _results.append((status, name, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def check_python() -> None:
    ver = sys.version_info
    status = OK if (3, 11) <= (ver.major, ver.minor) <= (3, 13) else WARN
    record(status, "Python 版本", f"{ver.major}.{ver.minor}.{ver.micro}")


def check_packages() -> None:
    required = [
        "pydantic",
        "openai",
        "tenacity",
        "loguru",
        "fastapi",
        "uvicorn",
        "tiktoken",
        "playwright",
        "browser_use",
        "mcp",
        "bs4",
        "requests",
        "boto3",
        "docker",
        "daytona_sdk",
    ]
    missing = []
    for pkg in required:
        try:
            importlib.import_module(pkg)
        except Exception:
            missing.append(pkg)
    if missing:
        record(FAIL, "核心依赖", "缺失: " + ", ".join(missing))
    else:
        record(OK, "核心依赖", f"{len(required)} 个全部可导入")

    try:
        import pydantic

        if pydantic.VERSION.startswith("2.10"):
            record(OK, "pydantic 版本", pydantic.VERSION)
        else:
            record(
                WARN,
                "pydantic 版本",
                f"{pydantic.VERSION}（mcp 1.5.0 需要 2.10.x，否则 eval_type_backport 报错）",
            )
    except Exception:
        pass

    for optional in ("crawl4ai", "browsergym", "datasets"):
        try:
            importlib.import_module(optional)
        except Exception:
            record(WARN, f"可选依赖 {optional}", "未安装（不影响主流程）")


def check_playwright() -> None:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
            if path and Path(path).exists():
                record(OK, "Playwright Chromium", path)
            else:
                record(WARN, "Playwright Chromium", "未安装内核，执行 playwright install chromium")
    except Exception as exc:  # noqa: BLE001
        record(WARN, "Playwright Chromium", f"检测失败: {exc}")


def check_ollama() -> None:
    from app.local_llm import DEFAULT_OLLAMA_BASE, pick_text_model, probe_local_runtime

    runtime = probe_local_runtime(DEFAULT_OLLAMA_BASE)
    if not runtime["available"]:
        record(WARN, "本地 Ollama", runtime["error"] or "未启动")
        return
    models = runtime["models"]
    record(OK, "本地 Ollama", f"{runtime['base']} 共 {len(models)} 个模型")
    for m in models:
        caps = ",".join(m.get("capabilities") or []) or "-"
        print(f"        - {m['name']:<38} {m.get('params', ''):<8} [{caps}]")
    best = pick_text_model(models)
    if best:
        record(OK, "推荐 Agent 主脑", best["name"])
    else:
        record(WARN, "推荐 Agent 主脑", "没有支持 tool calling 的模型，需 ollama pull qwen3:8b")


def check_config() -> None:
    from app.config import config

    status = getattr(config, "model_status", {}) or {}
    record(
        OK if status.get("source") == "local" else WARN,
        "模型来源",
        f"{status.get('source')} / {status.get('model')}",
    )
    record(OK, "视觉策略", str(status.get("vision", "未知")))

    if not (ROOT / "config" / "config.toml").exists():
        record(FAIL, "config/config.toml", "缺失，请从 config.example.toml 复制")
    else:
        record(OK, "config/config.toml", "存在")

    ws = config.workspace_root
    record(OK if ws.exists() else WARN, "workspace 目录", str(ws))


def check_tools() -> None:
    try:
        from app.agent.manus import Manus

        agent = Manus()
        names = [t.name for t in agent.available_tools.tools]
        record(OK, "工具集", ", ".join(names))
    except Exception as exc:  # noqa: BLE001
        record(FAIL, "工具集", f"{type(exc).__name__}: {exc}")


def main() -> int:
    print("=" * 66)
    print(" LocalManus 环境自检")
    print(" 项目根目录:", ROOT)
    print("=" * 66)
    check_python()
    check_packages()
    check_playwright()
    check_ollama()
    check_config()
    check_tools()

    fails = sum(1 for s, _, _ in _results if s == FAIL)
    warns = sum(1 for s, _, _ in _results if s == WARN)
    print("=" * 66)
    print(f" 结果：FAIL={fails}  WARN={warns}  OK={len(_results) - fails - warns}")
    if fails:
        print(" 建议：先按 FAIL 项补齐依赖/配置，再运行 python main.py")
    elif warns:
        print(" 建议：WARN 项不影响主流程，但会影响浏览器/视觉类任务")
    else:
        print(" 一切就绪：python main.py --local --prompt \"你的任务\"")
    print("=" * 66)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
