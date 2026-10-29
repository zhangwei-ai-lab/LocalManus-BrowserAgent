"""本地模型优先的模型适配层。

设计目标（相对上游 OpenManus 的改动）：

1. 启动时自动探测本机 Ollama（默认 ``http://localhost:11434``）。
2. 命中本地模型时优先使用本地模型，不消耗云端 token；
   未命中时给出明确的"需要 API Key"提示，而不是抛一堆 SDK 异常。
3. 视觉模型单独判断：本地没有多模态模型时自动降级为"纯文本浏览器模式"
   （本项目 browser 工具本身已不把截图塞给 LLM，因此纯文本模型可完整跑通）。
4. 对外只暴露 :func:`resolve_llm_endpoint` 与 :func:`probe_local_runtime`，
   不依赖 app.config，避免循环导入。
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

DEFAULT_OLLAMA_BASE = os.getenv("OLLAMA_HOST", "http://localhost:11434")
PROBE_TIMEOUT = float(os.getenv("OLLAMA_PROBE_TIMEOUT", "1.5"))

# 视觉能力的模型族关键字（用于判断本地是否有多模态模型）
_VISION_HINTS = ("vl", "vision", "llava", "minicpm-v", "moondream", "gemma3")

# 用于给本地候选模型打分的关键字（越大越优先）
_MODEL_PRIORITY = (
    ("qwen3", 30),
    ("qwen2.5", 20),
    ("qwen", 15),
    ("deepseek", 12),
    ("llama", 8),
    ("mistral", 6),
    ("coder", -5),  # 纯代码模型排后面：系统提示词里工具描述很长
)


def _http_json(url: str, timeout: float = PROBE_TIMEOUT) -> Optional[dict]:
    """极简 GET JSON，失败一律返回 None（探测场景不抛异常）。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode("utf-8", errors="ignore"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def probe_local_runtime(base_url: str = DEFAULT_OLLAMA_BASE) -> Dict[str, Any]:
    """探测本地 Ollama 运行时。

    Returns:
        dict: ``{"available": bool, "base": str, "models": [...], "error": str|None}``
    """
    result: Dict[str, Any] = {
        "available": False,
        "base": base_url,
        "models": [],
        "error": None,
    }
    data = _http_json(f"{base_url.rstrip('/')}/api/tags")
    if not data:
        result["error"] = "未检测到本地 Ollama 服务（11434 端口无响应）"
        return result

    models: List[Dict[str, Any]] = []
    for item in data.get("models", []) or []:
        details = item.get("details") or {}
        models.append(
            {
                "name": item.get("name", ""),
                "size": item.get("size", 0),
                "params": details.get("parameter_size", ""),
                "capabilities": item.get("capabilities") or [],
            }
        )
    result["available"] = True
    result["models"] = models
    return result


def _supports_tools(model: Dict[str, Any]) -> bool:
    caps = [str(c).lower() for c in model.get("capabilities") or []]
    return "tools" in caps or "completion" in caps


def _is_vision_model(model: Dict[str, Any]) -> bool:
    name = model.get("name", "").lower()
    return any(h in name for h in _VISION_HINTS)


def _score(model: Dict[str, Any]) -> float:
    name = model.get("name", "").lower()
    score = sum(weight for key, weight in _MODEL_PRIORITY if key in name)
    # 参数规模：从 "8.2B" / "7.6B" 这类字符串里取数字
    match = re.search(r"([\d.]+)\s*B", model.get("params", "") or "")
    if match:
        try:
            score += float(match.group(1)) * 0.5
        except ValueError:
            pass
    # 工具能力是硬需求
    if not _supports_tools(model):
        score -= 100
    return score


def pick_text_model(models: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """在本地模型里挑一个最适合做 Agent 主脑的（需支持 tool calling）。"""
    candidates = [m for m in models if _supports_tools(m) and not _is_vision_model(m)]
    if not candidates:
        return None
    return max(candidates, key=_score)


def pick_vision_model(models: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    candidates = [m for m in models if _is_vision_model(m)]
    if not candidates:
        return None
    return max(candidates, key=_score)


def resolve_llm_endpoint(
    preferred_model: Optional[str] = None,
    base_url: str = DEFAULT_OLLAMA_BASE,
) -> Optional[Dict[str, Any]]:
    """解析出一个可直接喂给 OpenAI SDK 的本地端点配置。

    Args:
        preferred_model: 用户显式指定的本地模型名，存在则优先使用。
        base_url: Ollama 服务地址。

    Returns:
        ``{"model", "base_url", "api_key", "params", "vision_model"}`` 或 ``None``。
    """
    runtime = probe_local_runtime(base_url)
    if not runtime["available"]:
        return None

    models = runtime["models"]
    chosen = None
    if preferred_model:
        chosen = next(
            (m for m in models if m["name"] == preferred_model or m["name"].split(":")[0] == preferred_model.split(":")[0]),
            None,
        )
    if chosen is None:
        chosen = pick_text_model(models)
    if chosen is None:
        return None

    vision = pick_vision_model(models)
    return {
        "model": chosen["name"],
        "base_url": f"{base_url.rstrip('/')}/v1",
        "api_key": "ollama",  # Ollama 的 OpenAI 兼容层不校验 key
        "params": chosen.get("params", ""),
        "vision_model": vision["name"] if vision else None,
    }


def missing_api_key_hint(provider: str = "DashScope / OpenAI 兼容服务") -> str:
    """本地模型缺失时给用户的可执行提示。"""
    return (
        "\n"
        "==================== 需要 API Key ====================\n"
        f"本机未检测到可用于 Agent 的本地大模型（需要支持 tool calling）。\n"
        f"请任选其一后重试：\n"
        f"  1) 本地拉取模型：  ollama pull qwen3:8b\n"
        f"  2) 配置云端 Key：  在 config/config.toml 的 [llm] 中填写 api_key，\n"
        f"     或设置环境变量 DASHSCOPE_API_KEY（{provider}）\n"
        "=====================================================\n"
    )
