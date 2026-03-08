import os
import json
import time
import logging
from typing import Generator, Optional, List, Dict, Any

import requests

logger = logging.getLogger("llm_gateway")


class LLMGateway:
    """统一大模型调用网关，支持 OpenAI 兼容接口 (GPT/DeepSeek/Claude 等)"""

    ENV_KEY_MAP = {
        "openai": "OPENAI_API_KEY",
        "deepseek": "DEEPSEEK_API_KEY",
        "claude": "ANTHROPIC_API_KEY",
    }

    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.providers = config.get("providers", {})
        self.current_provider = config.get("default_provider", "openai")

    def set_provider(self, provider: str):
        if provider in self.providers:
            self.current_provider = provider
            logger.info(f"切换 LLM provider: {provider}")
        else:
            raise ValueError(f"未知的 provider: {provider}, 可选: {list(self.providers.keys())}")

    def _get_provider_config(self, provider: Optional[str] = None) -> Dict[str, Any]:
        p = provider or self.current_provider
        if p not in self.providers:
            raise ValueError(f"未配置 provider: {p}")
        return self.providers[p], p

    def _get_api_key(self, provider: str) -> str:
        env_var = self.ENV_KEY_MAP.get(provider, f"{provider.upper()}_API_KEY")
        key = os.environ.get(env_var, "")
        if not key:
            logger.warning(f"未找到环境变量 {env_var}，请设置后重试")
        return key

    def call(
        self,
        messages: List[Dict],
        stream: bool = False,
        tools: Optional[List] = None,
        provider: Optional[str] = None,
    ) -> Any:
        cfg, p_name = self._get_provider_config(provider)
        return self._call_openai_compatible(messages, stream, tools, cfg, p_name)

    def call_stream(
        self,
        messages: List[Dict],
        tools: Optional[List] = None,
        provider: Optional[str] = None,
    ) -> Generator[str, None, None]:
        """流式调用，只返回正文content字符串（向后兼容）"""
        for chunk in self.call_stream_raw(messages, tools, provider):
            if isinstance(chunk, dict):
                if chunk.get("type") == "content":
                    yield chunk["content"]
            else:
                yield chunk

    def call_stream_raw(
        self,
        messages: List[Dict],
        tools: Optional[List] = None,
        provider: Optional[str] = None,
    ) -> Generator[Dict, None, None]:
        """流式调用，返回 {"type": "thinking"/"content", "content": "..."} 字典"""
        cfg, p_name = self._get_provider_config(provider)
        yield from self._stream_openai_compatible(messages, tools, cfg, p_name)

    # ==================== OpenAI 兼容 ====================

    def _call_openai_compatible(self, messages, stream, tools, cfg, provider_name):
        api_key = self._get_api_key(provider_name)
        headers = {
            "Content-Type": "application/json",
        }

        if provider_name == "claude":
            headers["x-api-key"] = api_key
            headers["anthropic-version"] = "2023-06-01"
            return self._call_claude(messages, stream, tools, cfg, headers)
        else:
            headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": cfg.get("model"),
            "messages": messages,
            "temperature": cfg.get("temperature", 0.3),
            "max_tokens": cfg.get("max_tokens", 8192),
            "stream": stream,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        resp = requests.post(
            cfg.get("api_url"),
            headers=headers,
            json=payload,
            stream=stream,
            timeout=120,
        )
        resp.raise_for_status()

        if stream:
            return resp
        else:
            data = resp.json()
            msg = data.get("choices", [{}])[0].get("message", {})
            return {
                "content": msg.get("content", ""),
                "tool_calls": msg.get("tool_calls", []),
            }

    def _call_claude(self, messages, stream, tools, cfg, headers):
        system_msg = ""
        clean_messages = []
        for m in messages:
            if m["role"] == "system":
                system_msg = m["content"]
            else:
                clean_messages.append(m)

        payload = {
            "model": cfg.get("model"),
            "max_tokens": cfg.get("max_tokens", 8192),
            "messages": clean_messages,
        }
        if system_msg:
            payload["system"] = system_msg
        if tools:
            payload["tools"] = [
                {"name": t["function"]["name"], "description": t["function"].get("description", ""), "input_schema": t["function"].get("parameters", {})}
                for t in tools if t.get("type") == "function"
            ]
        if stream:
            payload["stream"] = True

        resp = requests.post(
            cfg.get("api_url"),
            headers=headers,
            json=payload,
            stream=stream,
            timeout=120,
        )
        resp.raise_for_status()

        if stream:
            return resp
        else:
            data = resp.json()
            content_blocks = data.get("content", [])
            text = "".join(b.get("text", "") for b in content_blocks if b.get("type") == "text")
            return {"content": text, "tool_calls": []}

    def _stream_openai_compatible(self, messages, tools, cfg, provider_name) -> Generator[str, None, None]:
        if provider_name == "claude":
            yield from self._stream_claude(messages, tools, cfg)
            return

        resp = self._call_openai_compatible(messages, stream=True, tools=tools, cfg=cfg, provider_name=provider_name)
        for line in resp.iter_lines():
            if not line:
                continue
            line_str = line.decode("utf-8").strip()
            if line_str.startswith("data: "):
                line_str = line_str[6:]
            if line_str == "[DONE]":
                break
            try:
                chunk = json.loads(line_str)
                choices = chunk.get("choices", [])
                if choices:
                    delta = choices[0].get("delta", {})
                    content = delta.get("content", "")
                    reasoning = delta.get("reasoning_content", "") or delta.get("reasoning", "")
                    if reasoning:
                        yield {"type": "thinking", "content": reasoning}
                    if content:
                        yield {"type": "content", "content": content}
            except json.JSONDecodeError:
                continue

    def _stream_claude(self, messages, tools, cfg) -> Generator[str, None, None]:
        api_key = self._get_api_key("claude")
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }
        resp = self._call_claude(messages, stream=True, tools=None, cfg=cfg, headers=headers)
        for line in resp.iter_lines():
            if not line:
                continue
            line_str = line.decode("utf-8").strip()
            if line_str.startswith("data: "):
                line_str = line_str[6:]
            try:
                chunk = json.loads(line_str)
                if chunk.get("type") == "content_block_delta":
                    delta = chunk.get("delta", {})
                    text = delta.get("text", "")
                    if text:
                        yield {"type": "content", "content": text}
            except json.JSONDecodeError:
                continue

    # ==================== 便捷方法 ====================

    def chat(self, system_prompt: str, user_message: str, history: List[Dict] = None, provider: Optional[str] = None) -> str:
        messages = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})
        result = self.call(messages, stream=False, provider=provider)
        return result.get("content", "")

    def chat_stream(self, system_prompt: str, user_message: str, history: List[Dict] = None, provider: Optional[str] = None) -> Generator[str, None, None]:
        messages = [{"role": "system", "content": system_prompt}]
        if history:
            messages.extend(history)
        messages.append({"role": "user", "content": user_message})
        yield from self.call_stream(messages, provider=provider)

    def chat_with_messages(self, messages: List[Dict], stream: bool = False, provider: Optional[str] = None):
        if stream:
            return self.call_stream(messages, provider=provider)
        else:
            return self.call(messages, stream=False, provider=provider)
