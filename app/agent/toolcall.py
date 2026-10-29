import asyncio
import json
from typing import Any, List, Optional, Union

from pydantic import Field, PrivateAttr

from app.agent.react import ReActAgent
from app.exceptions import TokenLimitExceeded
from app.logger import logger
from app.prompt.toolcall import NEXT_STEP_PROMPT, SYSTEM_PROMPT
from app.schema import TOOL_CHOICE_TYPE, AgentState, Message, ToolCall, ToolChoice
from app.tool import CreateChatCompletion, Terminate, ToolCollection


TOOL_CALL_REQUIRED = "需要工具调用但未提供"


class ToolCallAgent(ReActAgent):
    """用于处理工具/函数调用的基础 agent 类，具有增强的抽象能力"""

    name: str = "toolcall"
    description: str = "可以执行工具调用的 agent。"

    system_prompt: str = SYSTEM_PROMPT
    next_step_prompt: str = NEXT_STEP_PROMPT

    available_tools: ToolCollection = ToolCollection(
        CreateChatCompletion(), Terminate()
    )
    tool_choices: TOOL_CHOICE_TYPE = ToolChoice.AUTO  # type: ignore
    special_tool_names: List[str] = Field(default_factory=lambda: [Terminate().name])

    tool_calls: List[ToolCall] = Field(default_factory=list)
    _current_base64_image: Optional[str] = None

    max_steps: int = 30
    max_observe: Optional[Union[int, bool]] = None

    # ---------------- 循环保护（本项目新增） ----------------
    # 小参数模型在真实网站上很容易"同一个 go_to_url 连点 5 次"，把步数全烧光。
    # 这里用"工具名 + 规范化参数"作为动作指纹：连续重复到 soft 阈值就注入一条
    # 换策略提示，到 hard 阈值直接收尾，避免把 max_steps 变成空转预算。
    loop_soft_limit: int = 3
    loop_hard_limit: int = 5
    _repeat_streak: int = PrivateAttr(default=0)
    _last_actions: List[str] = PrivateAttr(default_factory=list)

    @staticmethod
    def _action_signature(command: ToolCall) -> str:
        """把一次工具调用压成可比较的指纹（参数按键排序，避免顺序差异）。"""
        raw = command.function.arguments or "{}"
        try:
            normalized = json.dumps(
                json.loads(raw), sort_keys=True, ensure_ascii=False
            )
        except Exception:
            normalized = raw
        return f"{command.function.name}:{normalized}"

    def _note_action(self, signature: str) -> int:
        """记录动作并返回它连续重复了几次。"""
        if self._last_actions and self._last_actions[-1] == signature:
            self._repeat_streak += 1
        else:
            self._repeat_streak = 1
        self._last_actions = (self._last_actions + [signature])[-8:]
        return self._repeat_streak

    async def think(self) -> bool:
        """处理当前状态并使用工具决定下一步行动"""
        if self.next_step_prompt:
            user_msg = Message.user_message(self.next_step_prompt)
            self.messages += [user_msg]

        try:
            # 获取带有工具选项的响应
            response = await self.llm.ask_tool(
                messages=self.messages,
                system_msgs=(
                    [Message.system_message(self.system_prompt)]
                    if self.system_prompt
                    else None
                ),
                tools=self.available_tools.to_params(),
                tool_choice=self.tool_choices,
            )
        except ValueError:
            raise
        except Exception as e:
            # 检查这是否是包含 TokenLimitExceeded 的 RetryError
            if hasattr(e, "__cause__") and isinstance(e.__cause__, TokenLimitExceeded):
                token_limit_error = e.__cause__
                logger.error(
                    f"🚨 Token limit error (from RetryError): {token_limit_error}"
                )
                self.memory.add_message(
                    Message.assistant_message(
                        f"达到最大 token 限制，无法继续执行: {str(token_limit_error)}"
                    )
                )
                self.state = AgentState.FINISHED
                return False
            raise

        self.tool_calls = tool_calls = (
            response.tool_calls if response and response.tool_calls else []
        )
        content = response.content if response and response.content else ""

        # 记录响应信息
        logger.info(f"✨ {self.name}'s thoughts: {content}")
        logger.info(
            f"🛠️ {self.name} selected {len(tool_calls) if tool_calls else 0} tools to use"
        )
        if tool_calls:
            logger.info(
                f"🧰 Tools being prepared: {[call.function.name for call in tool_calls]}"
            )
            logger.info(f"🔧 Tool arguments: {tool_calls[0].function.arguments}")

        try:
            if response is None:
                raise RuntimeError("No response received from the LLM")

            # 处理不同的 tool_choices 模式
            if self.tool_choices == ToolChoice.NONE:
                if tool_calls:
                    logger.warning(
                        f"🤔 Hmm, {self.name} tried to use tools when they weren't available!"
                    )
                if content:
                    self.memory.add_message(Message.assistant_message(content))
                    return True
                return False

            # 创建并添加 assistant 消息
            assistant_msg = (
                Message.from_tool_calls(content=content, tool_calls=self.tool_calls)
                if self.tool_calls
                else Message.assistant_message(content)
            )
            self.memory.add_message(assistant_msg)

            if self.tool_choices == ToolChoice.REQUIRED and not self.tool_calls:
                return True  # 将在 act() 中处理

            # 对于 'auto' 模式，如果没有命令但存在内容，则继续处理内容
            if self.tool_choices == ToolChoice.AUTO and not self.tool_calls:
                return bool(content)

            return bool(self.tool_calls)
        except Exception as e:
            logger.error(f"🚨 Oops! The {self.name}'s thinking process hit a snag: {e}")
            self.memory.add_message(
                Message.assistant_message(
                    f"处理过程中遇到错误: {str(e)}"
                )
            )
            return False

    async def act(self) -> str:
        """执行工具调用并处理其结果"""
        if not self.tool_calls:
            if self.tool_choices == ToolChoice.REQUIRED:
                raise ValueError(TOOL_CALL_REQUIRED)

            # 如果没有工具调用，返回最后一条消息的内容
            return self.messages[-1].content or "No content or commands to execute"

        results = []
        for command in self.tool_calls:
            # 为每个工具调用重置 base64_image
            self._current_base64_image = None

            # 循环保护：同一个动作反复重试时，先提示换策略，再兜底收尾
            streak = self._note_action(self._action_signature(command))
            if streak >= self.loop_hard_limit:
                logger.warning(
                    f"🛑 连续 {streak} 次执行相同动作，触发循环保护，提前收尾"
                )
                self.memory.add_message(
                    Message.user_message(
                        f"你已经连续 {streak} 次执行完全相同的动作（同一工具、同一参数），"
                        "说明这条路径拿不到新信息。系统已终止本次任务。"
                        "请在最终回复里说明卡在哪里、已获得哪些信息。"
                    )
                )
                self.state = AgentState.FINISHED
                results.append(
                    f"Loop guard: identical action repeated {streak} times, task aborted."
                )
                break
            if streak >= self.loop_soft_limit:
                logger.warning(f"⚠️ 同一动作已重复 {streak} 次，注入换策略提示")
                self.memory.add_message(
                    Message.user_message(
                        f"提示：你已经连续 {streak} 次执行相同的动作，但没有得到新信息。"
                        "请立刻换一种策略（换工具、换参数、换入口页面），"
                        "或者直接基于已有信息作答、用 terminate 收尾。不要再重复这个动作。"
                    )
                )

            result = await self.execute_tool(command)

            if self.max_observe:
                result = result[: self.max_observe]

            logger.info(
                f"🎯 Tool '{command.function.name}' completed its mission! Result: {result}"
            )

            # 将工具响应添加到内存
            tool_msg = Message.tool_message(
                content=result,
                tool_call_id=command.id,
                name=command.function.name,
                base64_image=self._current_base64_image,
            )
            self.memory.add_message(tool_msg)
            results.append(result)

        return "\n\n".join(results)

    async def execute_tool(self, command: ToolCall) -> str:
        """执行单个工具调用，具有健壮的错误处理"""
        if not command or not command.function or not command.function.name:
            return "Error: Invalid command format"

        name = command.function.name
        if name not in self.available_tools.tool_map:
            return f"Error: Unknown tool '{name}'"

        try:
            # 解析参数
            args = json.loads(command.function.arguments or "{}")

            # 执行工具
            logger.info(f"🔧 Activating tool: '{name}'...")
            logger.debug(f"🔧 Tool arguments: {args}")
            result = await self.available_tools.execute(name=name, tool_input=args)

            # 处理特殊工具
            await self._handle_special_tool(name=name, result=result)

            # 检查结果是否是带有 base64_image 的 ToolResult
            if hasattr(result, "base64_image") and result.base64_image:
                image_size_kb = len(result.base64_image) * 3 / 4 / 1024
                logger.info(f"📷 Tool '{name}' returned screenshot: {image_size_kb:.2f} KB")
                # 存储 base64_image 以便稍后在 tool_message 中使用
                self._current_base64_image = result.base64_image
            else:
                logger.debug(f"📷 Tool '{name}' did not return screenshot")

            # 调试信息：显示工具执行结果
            if hasattr(result, "error") and result.error:
                logger.error(f"❌ Tool '{name}' failed: {result.error}")
            else:
                result_preview = str(result)[:200] + "..." if len(str(result)) > 200 else str(result)
                logger.debug(f"✅ Tool '{name}' result preview: {result_preview}")

            # 格式化结果以供显示（标准情况）
            observation = (
                f"Observed output of cmd `{name}` executed:\n{str(result)}"
                if result
                else f"Cmd `{name}` completed with no output"
            )

            return observation
        except json.JSONDecodeError:
            error_msg = f"Error parsing arguments for {name}: Invalid JSON format"
            logger.error(
                f"📝 Oops! The arguments for '{name}' don't make sense - invalid JSON, arguments:{command.function.arguments}"
            )
            return f"Error: {error_msg}"
        except Exception as e:
            error_msg = f"⚠️ Tool '{name}' encountered a problem: {str(e)}"
            logger.exception(error_msg)
            return f"Error: {error_msg}"

    async def _handle_special_tool(self, name: str, result: Any, **kwargs):
        """处理特殊工具执行和状态更改"""
        if not self._is_special_tool(name):
            return

        if self._should_finish_execution(name=name, result=result, **kwargs):
            # 将 agent 状态设置为已完成
            logger.info(f"🏁 Special tool '{name}' has completed the task!")
            self.state = AgentState.FINISHED

    @staticmethod
    def _should_finish_execution(**kwargs) -> bool:
        """确定工具执行是否应该结束 agent"""
        return True

    def _is_special_tool(self, name: str) -> bool:
        """检查工具名称是否在特殊工具列表中"""
        return name.lower() in [n.lower() for n in self.special_tool_names]

    async def cleanup(self):
        """清理 agent 工具使用的资源。"""
        logger.info(f"🧹 Cleaning up resources for agent '{self.name}'...")
        for tool_name, tool_instance in self.available_tools.tool_map.items():
            if hasattr(tool_instance, "cleanup") and asyncio.iscoroutinefunction(
                tool_instance.cleanup
            ):
                try:
                    logger.debug(f"🧼 Cleaning up tool: {tool_name}")
                    await tool_instance.cleanup()
                except Exception as e:
                    logger.error(
                        f"🚨 Error cleaning up tool '{tool_name}': {e}", exc_info=True
                    )
        logger.info(f"✨ Cleanup complete for agent '{self.name}'.")

    async def run(self, request: Optional[str] = None) -> str:
        """运行 agent，完成后进行清理。"""
        try:
            return await super().run(request)
        finally:
            await self.cleanup()
