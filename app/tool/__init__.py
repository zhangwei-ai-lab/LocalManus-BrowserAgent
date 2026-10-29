"""工具集合。

改动：可选依赖（crawl4ai / browser_use 等）改为按需导入。
上游在模块顶层硬导入 Crawl4aiTool，只要没装 crawl4ai 整个包就 import 失败，
导致连纯 Python 任务都跑不起来。这里降级为“缺依赖则跳过该工具”。
"""

from app.tool.base import BaseTool
from app.tool.bash import Bash
from app.tool.str_replace_editor import StrReplaceEditor
from app.tool.terminate import Terminate
from app.tool.tool_collection import ToolCollection
from app.tool.web_search import WebSearch

__all__ = [
    "BaseTool",
    "Bash",
    "BrowserUseTool",
    "Terminate",
    "StrReplaceEditor",
    "WebSearch",
    "ToolCollection",
    "CreateChatCompletion",
    "PlanningTool",
    "Crawl4aiTool",
]


def __getattr__(name: str):
    """惰性导入可选工具：缺依赖时返回 None 而不是让 import 崩溃。"""
    optional = {
        "BrowserUseTool": ("app.tool.browser_use_tool", "BrowserUseTool"),
        "Crawl4aiTool": ("app.tool.crawl4ai", "Crawl4aiTool"),
        "CreateChatCompletion": (
            "app.tool.create_chat_completion",
            "CreateChatCompletion",
        ),
        "PlanningTool": ("app.tool.planning", "PlanningTool"),
        "PythonExecute": ("app.tool.python_execute", "PythonExecute"),
        "AskHuman": ("app.tool.ask_human", "AskHuman"),
    }
    if name in optional:
        module_name, attr = optional[name]
        try:
            module = __import__(module_name, fromlist=[attr])
            value = getattr(module, attr)
        except Exception:  # pragma: no cover - 依赖缺失时的降级路径
            value = None
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
