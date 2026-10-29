import asyncio
import base64
import json
from typing import Generic, Optional, TypeVar

from browser_use import Browser as BrowserUseBrowser
from browser_use import BrowserConfig
from browser_use.browser.context import BrowserContext, BrowserContextConfig
from browser_use.dom.service import DomService
from pydantic import Field, PrivateAttr, field_validator
from pydantic_core.core_schema import ValidationInfo

from app.config import config
from app.llm import LLM
from app.logger import logger
from app.tool.base import BaseTool, ToolResult
from app.tool.proc_guard import BrowserProcessGuard
from app.tool.web_search import WebSearch


_BROWSER_DESCRIPTION = """\
一个强大的浏览器自动化工具，允许通过各种操作与网页交互。
* 此工具提供用于控制浏览器会话、导航网页和提取信息的命令
* 它在调用之间保持状态，保持浏览器会话活动直到显式关闭
* 当你需要浏览网站、填写表单、点击按钮、提取内容或执行网页搜索时使用此工具
* 每个操作都需要工具依赖项中定义的特定参数

主要功能包括：
* 导航：转到特定 URL、返回、搜索网页或刷新页面
* 交互：点击元素、输入文本、从下拉菜单中选择、发送键盘命令
* 滚动：按像素量向上/向下滚动或滚动到特定文本
* 内容提取：根据特定目标从网页中提取和分析内容
* 标签页管理：在标签页之间切换、打开新标签页或关闭标签页

注意：使用元素索引时，请参考当前浏览器状态中显示的元素编号。
"""

Context = TypeVar("Context")

# 纯文本模式下每步喂给模型的页面正文上限（字符）。
# 太小模型抓不到关键信息，太大又挤占上下文，实测 1200 字符是平衡点。
PAGE_TEXT_LIMIT = 1200


class BrowserUseTool(BaseTool, Generic[Context]):
    name: str = "browser_use"
    description: str = _BROWSER_DESCRIPTION
    parameters: dict = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "go_to_url",
                    "click_element",
                    "input_text",
                    "scroll_down",
                    "scroll_up",
                    "scroll_to_text",
                    "send_keys",
                    "get_dropdown_options",
                    "select_dropdown_option",
                    "go_back",
                    "web_search",
                    "wait",
                    "extract_content",
                    "switch_tab",
                    "open_tab",
                    "close_tab",
                ],
                "description": "要执行的浏览器操作",
            },
            "url": {
                "type": "string",
                "description": "用于 'go_to_url' 或 'open_tab' 操作的 URL",
            },
            "index": {
                "type": "integer",
                "description": "用于 'click_element'、'input_text'、'get_dropdown_options' 或 'select_dropdown_option' 操作的元素索引",
            },
            "text": {
                "type": "string",
                "description": "用于 'input_text'、'scroll_to_text' 或 'select_dropdown_option' 操作的文本",
            },
            "scroll_amount": {
                "type": "integer",
                "description": "用于 'scroll_down' 或 'scroll_up' 操作的滚动像素数（正数向下，负数向上）",
            },
            "tab_id": {
                "type": "integer",
                "description": "用于 'switch_tab' 操作的标签页 ID",
            },
            "query": {
                "type": "string",
                "description": "用于 'web_search' 操作的搜索查询",
            },
            "goal": {
                "type": "string",
                "description": "用于 'extract_content' 操作的提取目标",
            },
            "keys": {
                "type": "string",
                "description": "用于 'send_keys' 操作要发送的按键",
            },
            "seconds": {
                "type": "integer",
                "description": "用于 'wait' 操作要等待的秒数",
            },
        },
        "required": ["action"],
        "dependencies": {
            "go_to_url": ["url"],
            "click_element": ["index"],
            "input_text": ["index", "text"],
            "switch_tab": ["tab_id"],
            "open_tab": ["url"],
            "scroll_down": ["scroll_amount"],
            "scroll_up": ["scroll_amount"],
            "scroll_to_text": ["text"],
            "send_keys": ["keys"],
            "get_dropdown_options": ["index"],
            "select_dropdown_option": ["index", "text"],
            "go_back": [],
            "web_search": ["query"],
            "wait": ["seconds"],
            "extract_content": ["goal"],
        },
    }

    lock: asyncio.Lock = Field(default_factory=asyncio.Lock)
    browser: Optional[BrowserUseBrowser] = Field(default=None, exclude=True)
    context: Optional[BrowserContext] = Field(default=None, exclude=True)
    dom_service: Optional[DomService] = Field(default=None, exclude=True)
    web_search_tool: WebSearch = Field(default_factory=WebSearch, exclude=True)

    # 浏览器进程守卫：启动前拍 PID 快照，清理时回收本次启动的 Chromium。
    # 见 app/tool/proc_guard.py 里对 Windows 孤儿进程的说明。
    _proc_guard: Optional[BrowserProcessGuard] = PrivateAttr(default=None)

    # Context for generic functionality
    tool_context: Optional[Context] = Field(default=None, exclude=True)

    llm: Optional[LLM] = Field(default_factory=LLM)

    @field_validator("parameters", mode="before")
    def validate_parameters(cls, v: dict, info: ValidationInfo) -> dict:
        if not v:
            raise ValueError("Parameters cannot be empty")
        return v

    async def _ensure_browser_initialized(self) -> BrowserContext:
        """确保浏览器和上下文已初始化。"""
        if self.browser is None:
            browser_config_kwargs = {"headless": False, "disable_security": True}

            if config.browser_config:
                from browser_use.browser.browser import ProxySettings

                # 处理代理设置。
                if config.browser_config.proxy and config.browser_config.proxy.server:
                    browser_config_kwargs["proxy"] = ProxySettings(
                        server=config.browser_config.proxy.server,
                        username=config.browser_config.proxy.username,
                        password=config.browser_config.proxy.password,
                    )

                browser_attrs = [
                    "headless",
                    "disable_security",
                    "extra_chromium_args",
                    "chrome_instance_path",
                    "wss_url",
                    "cdp_url",
                ]

                for attr in browser_attrs:
                    value = getattr(config.browser_config, attr, None)
                    if value is not None:
                        if not isinstance(value, list) or value:
                            browser_config_kwargs[attr] = value

            self.browser = BrowserUseBrowser(BrowserConfig(**browser_config_kwargs))

        if self.context is None:
            context_config = BrowserContextConfig()
            # 如果配置中有上下文配置，则使用它。
            if (
                config.browser_config
                and hasattr(config.browser_config, "new_context_config")
                and config.browser_config.new_context_config
            ):
                context_config = config.browser_config.new_context_config

            # new_context() 才是真正拉起 Chromium 的时刻：先拍一张进程快照，
            # 之后 cleanup 时只回收「快照之后新出现、且是本进程子孙」的浏览器。
            if self._proc_guard is None:
                self._proc_guard = BrowserProcessGuard()

            self.context = await self.browser.new_context(context_config)
            self.dom_service = DomService(await self.context.get_current_page())

        return self.context

    async def execute(
        self,
        action: str,
        url: Optional[str] = None,
        index: Optional[int] = None,
        text: Optional[str] = None,
        scroll_amount: Optional[int] = None,
        tab_id: Optional[int] = None,
        query: Optional[str] = None,
        goal: Optional[str] = None,
        keys: Optional[str] = None,
        seconds: Optional[int] = None,
        **kwargs,
    ) -> ToolResult:
        """
        执行指定的浏览器操作。

        Args:
            action: 要执行的浏览器操作
            url: 用于导航或新标签页的 URL
            index: 用于点击或输入操作的元素索引
            text: 用于输入操作或搜索查询的文本
            scroll_amount: 用于滚动操作的滚动像素数
            tab_id: 用于 switch_tab 操作的标签页 ID
            query: 用于 Google 搜索的搜索查询
            goal: 用于内容提取的提取目标
            keys: 用于键盘操作要发送的按键
            seconds: 要等待的秒数
            **kwargs: 其他参数

        Returns:
            包含操作输出或错误的 ToolResult
        """
        async with self.lock:
            try:
                context = await self._ensure_browser_initialized()

                # 从配置中获取最大内容长度
                max_content_length = getattr(
                    config.browser_config, "max_content_length", 2000
                )

                # 导航操作
                if action == "go_to_url":
                    if not url:
                        return ToolResult(
                            error="URL is required for 'go_to_url' action"
                        )
                    page = await context.get_current_page()
                    await page.goto(url)
                    await page.wait_for_load_state()
                    return ToolResult(output=f"Navigated to {url}")

                elif action == "go_back":
                    await context.go_back()
                    return ToolResult(output="Navigated back")

                elif action == "refresh":
                    await context.refresh_page()
                    return ToolResult(output="Refreshed current page")

                elif action == "web_search":
                    if not query:
                        return ToolResult(
                            error="Query is required for 'web_search' action"
                        )
                    # 执行网页搜索并直接返回结果，无需浏览器导航
                    search_response = await self.web_search_tool.execute(
                        query=query, fetch_content=True, num_results=1
                    )
                    # 导航到第一个搜索结果
                    first_search_result = search_response.results[0]
                    url_to_navigate = first_search_result.url

                    page = await context.get_current_page()
                    await page.goto(url_to_navigate)
                    await page.wait_for_load_state()

                    return search_response

                # 元素交互操作
                elif action == "click_element":
                    if index is None:
                        return ToolResult(
                            error="Index is required for 'click_element' action"
                        )
                    element = await context.get_dom_element_by_index(index)
                    if not element:
                        return ToolResult(error=f"Element with index {index} not found")
                    download_path = await context._click_element_node(element)
                    output = f"Clicked element at index {index}"
                    if download_path:
                        output += f" - Downloaded file to {download_path}"
                    return ToolResult(output=output)

                elif action == "input_text":
                    if index is None or not text:
                        return ToolResult(
                            error="Index and text are required for 'input_text' action"
                        )
                    element = await context.get_dom_element_by_index(index)
                    if not element:
                        return ToolResult(error=f"Element with index {index} not found")
                    await context._input_text_element_node(element, text)
                    return ToolResult(
                        output=f"Input '{text}' into element at index {index}"
                    )

                elif action == "scroll_down" or action == "scroll_up":
                    direction = 1 if action == "scroll_down" else -1
                    amount = (
                        scroll_amount
                        if scroll_amount is not None
                        else context.config.browser_window_size["height"]
                    )
                    await context.execute_javascript(
                        f"window.scrollBy(0, {direction * amount});"
                    )
                    return ToolResult(
                        output=f"Scrolled {'down' if direction > 0 else 'up'} by {amount} pixels"
                    )

                elif action == "scroll_to_text":
                    if not text:
                        return ToolResult(
                            error="Text is required for 'scroll_to_text' action"
                        )
                    page = await context.get_current_page()
                    try:
                        locator = page.get_by_text(text, exact=False)
                        await locator.scroll_into_view_if_needed()
                        return ToolResult(output=f"Scrolled to text: '{text}'")
                    except Exception as e:
                        return ToolResult(error=f"Failed to scroll to text: {str(e)}")

                elif action == "send_keys":
                    if not keys:
                        return ToolResult(
                            error="Keys are required for 'send_keys' action"
                        )
                    page = await context.get_current_page()
                    await page.keyboard.press(keys)
                    return ToolResult(output=f"Sent keys: {keys}")

                elif action == "get_dropdown_options":
                    if index is None:
                        return ToolResult(
                            error="Index is required for 'get_dropdown_options' action"
                        )
                    element = await context.get_dom_element_by_index(index)
                    if not element:
                        return ToolResult(error=f"Element with index {index} not found")
                    page = await context.get_current_page()
                    options = await page.evaluate(
                        """
                        (xpath) => {
                            const select = document.evaluate(xpath, document, null,
                                XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
                            if (!select) return null;
                            return Array.from(select.options).map(opt => ({
                                text: opt.text,
                                value: opt.value,
                                index: opt.index
                            }));
                        }
                    """,
                        element.xpath,
                    )
                    return ToolResult(output=f"Dropdown options: {options}")

                elif action == "select_dropdown_option":
                    if index is None or not text:
                        return ToolResult(
                            error="Index and text are required for 'select_dropdown_option' action"
                        )
                    element = await context.get_dom_element_by_index(index)
                    if not element:
                        return ToolResult(error=f"Element with index {index} not found")
                    page = await context.get_current_page()
                    await page.select_option(element.xpath, label=text)
                    return ToolResult(
                        output=f"Selected option '{text}' from dropdown at index {index}"
                    )

                # 内容提取操作
                elif action == "extract_content":
                    if not goal:
                        return ToolResult(
                            error="Goal is required for 'extract_content' action"
                        )

                    page = await context.get_current_page()
                    import markdownify

                    content = markdownify.markdownify(await page.content())

                    prompt = f"""\
Your task is to extract the content of the page. You will be given a page and a goal, and you should extract all relevant information around this goal from the page. If the goal is vague, summarize the page. Respond in json format.
Extraction goal: {goal}

Page content:
{content[:max_content_length]}
"""
                    messages = [{"role": "system", "content": prompt}]

                    # 定义提取函数模式
                    extraction_function = {
                        "type": "function",
                        "function": {
                            "name": "extract_content",
                            "description": "Extract specific information from a webpage based on a goal",
                            "parameters": {
                                "type": "object",
                                "properties": {
                                    "extracted_content": {
                                        "type": "object",
                                        "description": "The content extracted from the page according to the goal",
                                        "properties": {
                                            "text": {
                                                "type": "string",
                                                "description": "Text content extracted from the page",
                                            },
                                            "metadata": {
                                                "type": "object",
                                                "description": "Additional metadata about the extracted content",
                                                "properties": {
                                                    "source": {
                                                        "type": "string",
                                                        "description": "Source of the extracted content",
                                                    }
                                                },
                                            },
                                        },
                                    }
                                },
                                "required": ["extracted_content"],
                            },
                        },
                    }

                    # 使用 LLM 通过必需的函数调用来提取内容
                    response = await self.llm.ask_tool(
                        messages,
                        tools=[extraction_function],
                        tool_choice="required",
                    )

                    if response and response.tool_calls:
                        args = json.loads(response.tool_calls[0].function.arguments)
                        extracted_content = args.get("extracted_content", {})
                        return ToolResult(
                            output=f"Extracted from page:\n{extracted_content}\n"
                        )

                    return ToolResult(output="No content was extracted from the page.")

                # 标签页管理操作
                elif action == "switch_tab":
                    if tab_id is None:
                        return ToolResult(
                            error="Tab ID is required for 'switch_tab' action"
                        )
                    await context.switch_to_tab(tab_id)
                    page = await context.get_current_page()
                    await page.wait_for_load_state()
                    return ToolResult(output=f"Switched to tab {tab_id}")

                elif action == "open_tab":
                    if not url:
                        return ToolResult(error="URL is required for 'open_tab' action")
                    await context.create_new_tab(url)
                    return ToolResult(output=f"Opened new tab with {url}")

                elif action == "close_tab":
                    await context.close_current_tab()
                    return ToolResult(output="Closed current tab")

                # 实用操作
                elif action == "wait":
                    seconds_to_wait = seconds if seconds is not None else 3
                    await asyncio.sleep(seconds_to_wait)
                    return ToolResult(output=f"Waited for {seconds_to_wait} seconds")

                else:
                    return ToolResult(error=f"Unknown action: {action}")

            except Exception as e:
                return ToolResult(error=f"Browser action '{action}' failed: {str(e)}")

    async def get_current_state(
        self, context: Optional[BrowserContext] = None
    ) -> ToolResult:
        """
        获取当前浏览器状态作为 ToolResult。
        如果未提供 context，则使用 self.context。
        """
        try:
            # 使用提供的 context 或回退到 self.context
            ctx = context or self.context
            if not ctx:
                return ToolResult(error="Browser context not initialized")

            state = await ctx.get_state()

            # 如果不存在，创建 viewport_info 字典
            viewport_height = 0
            if hasattr(state, "viewport_info") and state.viewport_info:
                viewport_height = state.viewport_info.height
            elif hasattr(ctx, "config") and hasattr(ctx.config, "browser_window_size"):
                viewport_height = ctx.config.browser_window_size.get("height", 0)

            # 为状态拍摄截图
            page = await ctx.get_current_page()

            await page.bring_to_front()
            await page.wait_for_load_state()

            screenshot = await page.screenshot(
                full_page=True, animations="disabled", type="jpeg", quality=100
            )

            screenshot = base64.b64encode(screenshot).decode("utf-8")
            screenshot_size_kb = len(screenshot) * 3 / 4 / 1024  # 估算图片大小（KB）

            # 获取可交互元素信息
            interactive_elements_str = (
                state.element_tree.clickable_elements_to_string()
                if state.element_tree
                else ""
            )
            element_count = interactive_elements_str.count("[") if interactive_elements_str else 0

            # 纯文本模式的关键补充：抓一段页面正文。
            # 只有可交互元素列表时，模型看不到标题/段落，会误判"页面没打开"，
            # 于是反复 go_to_url 同一个地址。这里压成一行文本喂回去，
            # 既能让模型直接拿到 H1/正文，又不会像截图那样吃满上下文。
            page_text = ""
            try:
                raw_text = await page.inner_text("body")
                page_text = " ".join(raw_text.split())[:PAGE_TEXT_LIMIT]
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"Failed to read page text: {exc}")

            # 调试信息
            logger.info(f"🌐 Browser state captured: URL={state.url}, Title={state.title}")
            logger.info(f"📸 Screenshot size: {screenshot_size_kb:.2f} KB (base64)")
            logger.info(f"🔍 Interactive elements detected: {element_count}")
            if element_count == 0:
                logger.warning(f"⚠️ No interactive elements found - page may be empty or not loaded")
            if interactive_elements_str:
                # 显示前几个元素作为示例
                lines = interactive_elements_str.split("\n")[:5]
                preview = "\n".join(lines)
                logger.debug(f"🔍 Elements preview (first 5):\n{preview}")

            # 构建包含所有必需字段的状态信息
            state_info = {
                "url": state.url,
                "title": state.title,
                "tabs": [tab.model_dump() for tab in state.tabs],
                "help": "[0], [1], [2], etc., represent clickable indices corresponding to the elements listed. Clicking on these indices will navigate to or interact with the respective content behind them.",
                "interactive_elements": interactive_elements_str,
                "page_text": page_text,
                "scroll_info": {
                    "pixels_above": getattr(state, "pixels_above", 0),
                    "pixels_below": getattr(state, "pixels_below", 0),
                    "total_height": getattr(state, "pixels_above", 0)
                    + getattr(state, "pixels_below", 0)
                    + viewport_height,
                },
                "viewport_height": viewport_height,
            }

            return ToolResult(
                output=json.dumps(state_info, indent=4, ensure_ascii=False),
                base64_image=screenshot,
            )
        except Exception as e:
            return ToolResult(error=f"Failed to get browser state: {str(e)}")

    async def cleanup(self):
        """清理浏览器资源。

        顺序很重要：先按协议关 context/browser（让 Playwright 自己收尾），
        再扫一遍进程树兜底。因为关闭本身可能失败或卡住——那时如果只依赖
        ``browser.close()``，Chromium 就留在后台了。
        """
        async with self.lock:
            if self.context is not None:
                try:
                    await asyncio.wait_for(self.context.close(), timeout=15)
                except asyncio.TimeoutError:
                    logger.warning("⏱️ 关闭浏览器上下文超时，改为强制回收进程")
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"关闭浏览器上下文失败: {e}")
                finally:
                    self.context = None
                    self.dom_service = None

            if self.browser is not None:
                try:
                    await asyncio.wait_for(self.browser.close(), timeout=15)
                except asyncio.TimeoutError:
                    logger.warning("⏱️ 关闭浏览器超时，改为强制回收进程")
                except Exception as e:  # noqa: BLE001
                    logger.debug(f"关闭浏览器失败: {e}")
                finally:
                    self.browser = None

        self._sweep_processes()

    def _sweep_processes(self) -> None:
        """回收本次启动残留的 Chromium 进程（Windows 孤儿进程的兜底）。"""
        guard = self._proc_guard
        if guard is None:
            return
        try:
            killed = guard.sweep()
            if killed:
                logger.warning(
                    f"🧹 已强制回收 {len(killed)} 个残留浏览器进程: {killed}"
                )
            else:
                logger.debug("🧹 未发现残留浏览器进程")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"清理浏览器进程失败: {e}")
        finally:
            self._proc_guard = None

    def __del__(self):
        """对象被回收时只做进程兜底，不再尝试 asyncio.run()。

        原实现会 ``asyncio.run(self.cleanup())``：在自己的事件循环里跑必然抛
        ``RuntimeError``；退化成新建 loop 后，又去关属于旧 loop 的传输层，同样
        关不掉。与其装作能关，不如明确：**异步资源归 cleanup()，这里只保证不
        留孤儿进程**。
        """
        try:
            if self._proc_guard is not None:
                self._sweep_processes()
        except Exception:  # noqa: BLE001
            pass

    @classmethod
    def create_with_context(cls, context: Context) -> "BrowserUseTool[Context]":
        """创建具有特定上下文的 BrowserUseTool 的工厂方法。"""
        tool = cls()
        tool.tool_context = context
        return tool
