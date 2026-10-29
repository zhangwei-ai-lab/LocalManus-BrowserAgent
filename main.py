"""OpenManus CLI 入口。

本次改动：
1. 支持 `--local` / `--cloud` 在启动时切换模型来源（必须在导入 app 模块之前设置环境变量，
   否则 config 单例已经把配置读完了）；
2. 支持 `--model` 指定本地 Ollama 模型；
3. 支持 `--max-steps` 控制步数上限（云端贵、本地慢，步数需要可控）；
4. 支持 `--list-models` 直接列出本机可用模型，方便排查"到底有没有本地模型"。
"""

import argparse
import asyncio
import os


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="运行 OpenManus Agent")
    parser.add_argument("--prompt", type=str, help="输入给 agent 的提示（不填则交互式输入）")
    parser.add_argument(
        "--max-steps", type=int, default=None, help="最大执行步数（默认 20）"
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="指定模型名。带 --local 时表示本地 Ollama 模型",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--local", action="store_true", help="强制使用本机 Ollama 模型"
    )
    group.add_argument(
        "--cloud", action="store_true", help="强制使用 config.toml 中的云端模型"
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="无头模式跑浏览器（不弹窗，也不会留下可见的浏览器进程）",
    )
    parser.add_argument(
        "--list-models", action="store_true", help="列出本机 Ollama 模型后退出"
    )
    return parser.parse_args()


def apply_model_env(args: argparse.Namespace) -> None:
    """在导入 app 包之前把模型选择写进环境变量。"""
    if args.local:
        os.environ["OPENMANUS_USE_LOCAL"] = "1"
    elif args.cloud:
        os.environ["OPENMANUS_USE_LOCAL"] = "0"
    if args.model:
        os.environ["OPENMANUS_LOCAL_MODEL"] = args.model
    if args.headless:
        os.environ["OPENMANUS_HEADLESS"] = "1"


async def main() -> None:
    args = parse_args()
    apply_model_env(args)

    # 延迟导入：保证上面的环境变量先生效
    from app.agent.manus import Manus
    from app.logger import logger

    if args.list_models:
        from app.local_llm import DEFAULT_OLLAMA_BASE, probe_local_runtime, pick_text_model

        runtime = probe_local_runtime(DEFAULT_OLLAMA_BASE)
        if not runtime["available"]:
            logger.error(runtime["error"])
            return
        logger.info(f"本地 Ollama: {runtime['base']}")
        for m in runtime["models"]:
            caps = ",".join(m.get("capabilities") or []) or "-"
            logger.info(f"  - {m['name']:<40} {m.get('params', ''):<8} [{caps}]")
        best = pick_text_model(runtime["models"])
        logger.info(f"推荐作为 Agent 主脑: {best['name'] if best else '无（需支持 tool calling）'}")
        return

    agent = await Manus.create(
        **({"max_steps": args.max_steps} if args.max_steps else {})
    )
    try:
        prompt = args.prompt or input("请输入你的提示: ")
        if not prompt.strip():
            logger.warning("提供的提示为空。")
            return

        logger.warning("正在处理你的请求...")
        result = await agent.run(prompt)
        # 结果同时回显到终端，方便无 UI 场景直接复制
        print("\n" + "=" * 60)
        print(result)
        print("=" * 60)
        logger.info("请求处理完成。")
    except KeyboardInterrupt:
        logger.warning("操作被中断。")
    finally:
        await agent.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
