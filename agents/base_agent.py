import json
import logging
from abc import ABC, abstractmethod
from typing import Dict, Any, AsyncGenerator, List, Optional, Generator

logger = logging.getLogger("agent")


class BaseAgent(ABC):
    """Agent基类"""

    def __init__(self, llm_gateway, config: Dict[str, Any]):
        self.llm = llm_gateway
        self.config = config
        self.name = self.__class__.__name__

    @abstractmethod
    def get_system_prompt(self) -> str:
        """返回该Agent的system prompt"""
        ...

    async def chat(self, user_message: str, context: Dict) -> AsyncGenerator[Dict, None]:
        """流式对话，yield字典形式的chunk"""
        import asyncio

        system_prompt = self.get_system_prompt()
        history = context.get("history", [])
        messages = [{"role": "system", "content": system_prompt}]
        messages.extend(history)
        messages.append({"role": "user", "content": user_message})

        def _stream():
            return list(self.llm.call_stream(messages))

        chunks = await asyncio.to_thread(_stream)
        for chunk_text in chunks:
            yield {"type": "stream", "content": chunk_text}

    async def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """非流式分析，返回结构化结果"""
        import asyncio

        system_prompt = self.get_system_prompt()
        user_content = self._format_analysis_input(data)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        def _call():
            return self.llm.call(messages, stream=False)

        result = await asyncio.to_thread(_call)
        return self._parse_analysis_output(result)

    def _format_analysis_input(self, data: Dict[str, Any]) -> str:
        """将输入数据格式化为大模型可读的文本"""
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)

    def _parse_analysis_output(self, result: Dict) -> Dict[str, Any]:
        """解析大模型返回的结果"""
        content = result.get("content", "")
        # 尝试提取JSON
        try:
            if "```json" in content:
                json_str = content.split("```json")[1].split("```")[0].strip()
                return json.loads(json_str)
            elif "```" in content:
                json_str = content.split("```")[1].split("```")[0].strip()
                return json.loads(json_str)
            else:
                return json.loads(content)
        except (json.JSONDecodeError, IndexError):
            return {"raw_content": content}
