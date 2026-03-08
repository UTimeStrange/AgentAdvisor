import json
import logging
import asyncio
from typing import Dict, Any, AsyncGenerator

from agents.base_agent import BaseAgent

logger = logging.getLogger("consult_agent")

SYSTEM_PROMPT = """你是一位专业的AI投资顾问，用户已经完成了投资画像问答并获得了资产配置方案。现在用户进入自由咨询阶段，你需要：

## 你的职责

1. **解答疑问** — 回答用户关于配置方案的任何问题，解释为什么这样配置
2. **调整方案** — 如果用户要求调整某个标的的比例、替换某个ETF、增减仓位，你应该给出调整建议
3. **投资教育** — 耐心解释投资相关概念，如PE分位、估值、资产配置原理
4. **风险提示** — 对用户不合理的要求（如全仓单一标的），需要温和但明确地提示风险

## 回复风格

- 专业但不晦涩，避免AI腔调
- 回复用Markdown格式，结构清晰
- 如果涉及具体数字和比例调整，用表格展示
- 不要用表情符号

## 上下文

你可以引用以下信息来回答用户：
- 用户画像（风险偏好、投资期限等）
- 当前配置方案（各ETF的配比和理由）
- 估值数据（PE/PB分位等）

请基于上下文信息回答用户的问题。
"""


class ConsultAgent(BaseAgent):
    """咨询Agent - 方案生成后的自由对话，支持咨询和调整配置"""

    def __init__(self, llm_gateway, data_service, config: Dict[str, Any]):
        super().__init__(llm_gateway, config)
        self.data_service = data_service

    def get_system_prompt(self) -> str:
        return SYSTEM_PROMPT

    async def chat_stream(
        self, user_message: str, session: Dict
    ) -> AsyncGenerator[Dict, None]:
        """流式对话，带thinking"""

        system_prompt = self.get_system_prompt()

        # 构建上下文信息
        context_parts = []

        # 用户画像
        profile = session.get("profile", {})
        if profile:
            context_parts.append(f"## 用户画像\n{json.dumps(profile, ensure_ascii=False, indent=2, default=str)}")

        # 当前配置方案
        allocation = session.get("allocation", {})
        if allocation:
            context_parts.append(f"## 当前配置方案\n{json.dumps(allocation, ensure_ascii=False, indent=2, default=str)}")

        # 估值数据摘要
        valuation_text = self.data_service.get_valuation_text_for_llm()
        if valuation_text:
            context_parts.append(f"## 市场估值数据\n{valuation_text}")

        context_info = "\n\n".join(context_parts)

        # 构建messages
        messages = [{"role": "system", "content": system_prompt}]

        # 加入对话历史（最近10轮）
        history = session.get("history", [])
        recent_history = history[-20:] if len(history) > 20 else history

        # 第一次咨询时注入上下文
        if not session.get("consult_context_sent"):
            messages.append({
                "role": "user",
                "content": f"以下是我的投资背景信息，请记住：\n\n{context_info}",
            })
            messages.append({
                "role": "assistant",
                "content": "好的，我已了解你的投资画像和当前配置方案。有任何问题都可以问我，我会帮你分析和调整。",
            })
            session["consult_context_sent"] = True

        messages.extend(recent_history)
        messages.append({"role": "user", "content": user_message})

        queue: asyncio.Queue = asyncio.Queue()

        def _stream_worker():
            try:
                for chunk in self.llm.call_stream_raw(messages):
                    queue.put_nowait(chunk)
            except Exception as e:
                queue.put_nowait({"type": "error", "content": str(e)})
            finally:
                queue.put_nowait(None)  # sentinel

        loop = asyncio.get_event_loop()
        loop.run_in_executor(None, _stream_worker)

        full_content = ""
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            if isinstance(chunk, dict):
                if chunk.get("type") == "thinking":
                    yield {"type": "thinking", "content": chunk["content"]}
                elif chunk.get("type") == "content":
                    full_content += chunk["content"]
                    yield {"type": "stream", "content": chunk["content"]}
                elif chunk.get("type") == "error":
                    logger.error(f"流式调用出错: {chunk['content']}")
            elif isinstance(chunk, str):
                full_content += chunk
                yield {"type": "stream", "content": chunk}

        # 记录到对话历史
        session["history"].append({"role": "user", "content": user_message})
        session["history"].append({"role": "assistant", "content": full_content})
