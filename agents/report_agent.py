import json
import logging
import asyncio
from typing import Dict, Any, AsyncGenerator, List

from agents.base_agent import BaseAgent

logger = logging.getLogger("report_agent")

SYSTEM_PROMPT = """你是一位专业的投资报告撰写专家。你的任务是将资产配置方案转化为用户友好的投资报告。

## 报告要求

1. 语言专业但通俗，不要有AI味道，不要用表情符号
2. 结构清晰：先总结，再详述各资产配置，最后给出买入节奏和风险提示
3. 如果用户是低认知用户，增加适当的投资教育内容
4. 重点说明为什么选择/不选择某些标的（结合估值数据）
5. 对高估值的标的明确说明风险

## 决策溯源要求（极其重要）

报告中必须有一个独立章节「为什么是这个方案」，用通俗的语言向用户解释：
- **股债配比**为什么是这个比例 — 引用用户回答的投资期限和回撤容忍度
- **每只标的**为什么选 — 结合估值数据 + 用户偏好
- **心态适配** — 引用用户在暴跌/暴涨场景中的选择，解释方案如何匹配
- **收益预期** — 引用用户的期望年化，说明方案预期收益是否匹配
- 不要说教，而是像朋友一样解释「因为你说了XX，所以我建议YY」

配置方案JSON中可能包含 `decision_rationale` 字段，请将其内容自然融入报告中，不要原样复制。

## 报告结构

1. 方案总览（一段话概括）
2. 为什么是这个方案（决策溯源，逐条解释每个关键决策与用户回答的关系）
3. 资产配比详情（Markdown表格，必须包含以下列：ETF名称、ETF代码、配比、金额、当前PE、PE分位、估值状态、配置理由。如果某标的没有PE数据则填"-"）
4. 估值分析（说明当前市场各标的估值情况）
5. 买入节奏建议
6. 风险提示
7. 如有投资教育内容，放在最后

请用Markdown格式输出。
"""


class ReportAgent(BaseAgent):
    """报告生成Agent - 格式化配置方案为用户友好的投资报告"""

    def get_system_prompt(self) -> str:
        return SYSTEM_PROMPT

    async def generate_report(
        self,
        allocation: Dict[str, Any],
        profile: Dict[str, Any],
        time_series: Dict[str, Any],
    ) -> AsyncGenerator[Dict, None]:
        """流式生成报告，thinking和content分别推送"""

        system_prompt = self.get_system_prompt()

        user_content = f"""请基于以下资产配置方案，生成一份完整的投资报告。

## 用户画像
{json.dumps(profile, ensure_ascii=False, indent=2, default=str)}

## 资产配置方案
{json.dumps(allocation, ensure_ascii=False, indent=2, default=str)}
"""

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

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

        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            if isinstance(chunk, dict):
                if chunk.get("type") == "thinking":
                    yield {"type": "thinking", "content": chunk["content"]}
                elif chunk.get("type") == "content":
                    yield {"type": "stream", "content": chunk["content"]}
                elif chunk.get("type") == "error":
                    logger.error(f"流式调用出错: {chunk['content']}")
            elif isinstance(chunk, str):
                yield {"type": "stream", "content": chunk}

        # 发送结构化数据（供前端绘图）
        charts_data = self._prepare_charts_data(allocation, time_series)
        yield {
            "type": "report_data",
            "allocation": allocation,
            "charts": charts_data,
        }

    def _prepare_charts_data(self, allocation: Dict, time_series: Dict) -> Dict:
        """准备前端图表数据 - 只展示最终选中标的的曲线"""
        charts = {}

        # 配比饼图数据
        alloc_list = allocation.get("allocation", [])
        if alloc_list:
            charts["pie"] = [
                {"name": item.get("etf_name", ""), "value": item.get("weight", 0)}
                for item in alloc_list
            ]

        # 从allocation中提取选中标的的 index_code 和 etf_code
        selected_index_codes = set()
        selected_etf_codes = set()
        for item in alloc_list:
            idx_code = item.get("index_code", "")
            etf_code = item.get("etf_code", "")
            if idx_code:
                selected_index_codes.add(idx_code)
            if etf_code:
                selected_etf_codes.add(etf_code)

        # PE走势 — 只展示选中标的
        pe_charts = {}
        for idx_code, pe_data in time_series.get("pe_history", {}).items():
            if idx_code not in selected_index_codes:
                continue
            data_list = pe_data.get("data", [])
            if data_list:
                pe_charts[idx_code] = data_list

        if pe_charts:
            charts["pe_trends"] = pe_charts

        # 价格走势 — 只展示选中标的（按 index_code 匹配）
        price_charts = {}
        for idx_code, price_data in time_series.get("price_history", {}).items():
            if idx_code not in selected_index_codes:
                continue
            data_list = price_data.get("data", [])
            if data_list:
                price_charts[idx_code] = data_list

        if price_charts:
            charts["price_trends"] = price_charts

        return charts
