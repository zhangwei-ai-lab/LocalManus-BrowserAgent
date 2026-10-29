"""案例运行器：把验证过的任务固化成可复用的 prompt。

用法：
    python cases/run_case.py --case seo --url https://example.com
    python cases/run_case.py --case competitor --topic "中国新能源车出海欧洲"
    python cases/run_case.py --case flight --from 上海 --to 北京 --date 2026-11-05
    python cases/run_case.py --list

注意：默认走本地模型（--local）；想用云端加 --cloud。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def build_prompt(case: str, args: argparse.Namespace) -> str:
    if case == "seo":
        url = args.url or "https://example.com"
        return (
            f"请对 {url} 做一次完整的 SEO 审查：检查标题、描述、关键词、H 标签结构、"
            f"图片 alt、内链外链、移动端友好度、加载性能相关线索，给出问题清单与可执行的优化建议，"
            f"最后用 str_replace_editor 写入 workspace/seo_report.md，并在结尾回报文件绝对路径。"
        )
    if case == "competitor":
        topic = args.topic or "比亚迪与小鹏在欧洲市场的竞争格局"
        return (
            f"帮我写一份关于「{topic}」的竞品分析报告："
            f"先用 web_search 或浏览器获取最近的真实数据（销量、车型、定价、渠道、政策），"
            f"再按「市场概况 → 双方产品矩阵 → 价格与定位 → 渠道与本地化 → 风险与机会」组织，"
            f"写入 workspace/competitor_report.md，并回报文件路径。"
        )
    if case == "flight":
        dep = args.from_city or "上海"
        arr = args.to_city or "北京"
        date = args.date or (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
        return (
            f"查询 {date} 从{dep}到{arr}的机票。"
            f"注意：如果日历控件点不到，请直接构造带出发地、目的地、出发日期参数的搜索结果页 URL；"
            f"并且先访问网站首页建立会话，再跳转深链，避免被反爬拦截。"
            f"最终把 5 条航班（航班号、起飞时间、价格）写入 workspace/flight_result.md。"
        )
    raise ValueError(f"未知案例: {case}")


async def main() -> None:
    parser = argparse.ArgumentParser(description="OpenManus 案例运行器")
    parser.add_argument("--case", choices=["seo", "competitor", "flight"], help="案例名")
    parser.add_argument("--url", help="SEO 案例的目标网址")
    parser.add_argument("--topic", help="竞品分析主题")
    parser.add_argument("--from", dest="from_city", help="出发城市")
    parser.add_argument("--to", dest="to_city", help="到达城市")
    parser.add_argument("--date", help="出发日期 YYYY-MM-DD")
    parser.add_argument("--max-steps", type=int, default=12)
    parser.add_argument("--cloud", action="store_true", help="改用云端模型")
    parser.add_argument("--list", action="store_true", help="列出可用案例")
    args = parser.parse_args()

    cases = {
        "seo": "对指定网址做 SEO 审查，产出 workspace/seo_report.md",
        "competitor": "写竞品分析报告，产出 workspace/competitor_report.md",
        "flight": "查机票并处理日历/反爬，产出 workspace/flight_result.md",
    }
    if args.list or not args.case:
        print("可用案例：")
        for name, desc in cases.items():
            print(f"  {name:<12} {desc}")
        return

    if args.cloud:
        os.environ["OPENMANUS_USE_LOCAL"] = "0"
    os.environ.setdefault("OPENMANUS_USE_LOCAL", "1")

    from app.agent.manus import Manus
    from app.logger import logger

    prompt = build_prompt(args.case, args)
    logger.info(f"案例 [{args.case}] 启动")
    logger.info(f"任务：{prompt}")

    agent = await Manus.create(max_steps=args.max_steps)
    try:
        result = await agent.run(prompt)
        print("\n" + "=" * 60)
        print(result)
        print("=" * 60)
    finally:
        await agent.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
