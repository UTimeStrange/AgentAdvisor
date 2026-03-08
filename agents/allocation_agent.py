import json
import logging
import asyncio
from typing import Dict, Any, AsyncGenerator

from agents.base_agent import BaseAgent

logger = logging.getLogger("allocation_agent")

SYSTEM_PROMPT = """你是一位资深的AI资产配置专家，负责根据用户画像和当前市场估值数据，制定个性化的场内ETF资产配置方案。

## 核心原则

1. **估值驱动** — 绝不让用户在严重高估的标的上重仓。PE/PB历史分位数超过80%的标的，大幅降低配置比例或不配；超过90%的绝对不配。
2. **投资期限决定股债比** — 1年内基本只配债券和货币基金；3年可以30-40%股票；5年可以50-60%；10年以上可以70-80%。
3. **低认知用户保护** — 如果用户认知水平低，降低股票比例，增加债券和货币比例，保护用户。
4. **分散化** — 不在单一行业或主题上超配，宽基指数为主，行业主题为辅。
5. **黄金压舱石** — 黄金配置5-10%作为对冲工具。
6. **货币基金** — 保留5-10%作为再平衡和资金中转。

## 标的选择规则

- 只选场内ETF，不选个股，不选主动基金
- 优先选择市值大、流动性好的ETF
- 覆盖范围：国内宽基指数ETF、行业主题ETF、美股QDII(纳斯达克100/标普500)、港股QDII(恒生科技/恒生指数)、债券ETF、黄金ETF、货币基金
- 当前严重高估的行业/指数（PE分位>80%）降低权重或剔除
- 低估值（PE分位<30%）的标的可以适当超配

## 估值判断标准

- PE分位 < 20%：极度低估，可重点配置
- PE分位 20-40%：低估，正常配置偏多
- PE分位 40-60%：合理，标准配置
- PE分位 60-80%：偏高，减少配置
- PE分位 > 80%：严重高估，极低配或不配
- PE分位 > 90%：泡沫区域，绝对不配

## 决策溯源要求（极其重要）

你的每一个配置决策，都必须显式关联到用户画像中的具体回答。在JSON输出的 `decision_rationale` 字段中，逐条解释：

1. **股债配比** — 为什么是这个比例？必须引用用户的「投资期限」和「最大回撤容忍度」。例如："你表示这笔钱可以放3-5年，且最多能接受20%的回撤，所以我建议股票占45%、债券占35%。"
2. **标的选择** — 每只ETF为什么选/不选？必须结合估值数据和用户偏好。例如："半导体ETF当前PE分位72%偏高，考虑到你风险偏好稳健，暂不配置。"
3. **心态适配** — 必须引用用户在暴跌/暴涨场景的回答。例如："你在面对15%下跌时选择了'先不看了等它回来'，说明你心态尚可但不够坚定，所以我没有让高波动资产占比过高。"
4. **收益预期校准** — 必须引用用户的「期望年化收益」。例如："你期望年化6-10%，按当前组合的历史回测，长期年化大约在7-9%之间，与你的预期匹配。"
5. **经验适配** — 如果用户经验少，解释为什么选了更简单/稳健的标的。

## 输出格式

你必须输出以下JSON格式（不要有其他内容）：

```json
{
  "summary": "一段简洁的配置方案总结（100字以内）",
  "risk_level": "保守/稳健/平衡/积极",
  "stock_bond_ratio": "如 40:60 表示40%股票60%债券",
  "total_amount": 用户投资总额(万元),
  "decision_rationale": {
    "stock_bond_reason": "股债配比的理由，引用用户的投资期限、回撤容忍度、风险偏好",
    "drawdown_adaptation": "如何适配用户的回撤容忍度，引用用户的具体回答",
    "return_calibration": "收益预期校准说明，引用用户的期望年化",
    "mental_adaptation": "心态适配说明，引用用户在暴跌/暴涨场景的选择",
    "experience_adaptation": "经验适配说明，引用用户的投资经验水平"
  },
  "allocation": [
    {
      "category": "资产类别（宽基/行业/QDII/债券/黄金/货币）",
      "etf_code": "ETF代码",
      "etf_name": "ETF名称",
      "index_code": "对应指数代码（如有）",
      "weight": 配比百分比(如15表示15%),
      "amount": 配置金额(万元),
      "reason": "配置理由（必须结合估值数据 + 关联用户画像中的某个具体回答）",
      "valuation_status": "低估/合理/偏高/高估",
      "current_pe": 当前市盈率数值(如有，无则填null),
      "pe_percentile": PE分位数(如有，无则填null)
    }
  ],
  "buy_rhythm": {
    "strategy": "一次性买入/分3次/分5次等",
    "schedule": [
      {"batch": 1, "ratio": 占比百分比, "timing": "时间建议"},
      {"batch": 2, "ratio": 占比百分比, "timing": "时间建议"}
    ],
    "reason": "买入节奏的理由（需关联用户心态和经验）"
  },
  "warnings": ["风险提示列表"],
  "education": "如果是低认知用户，提供投资教育内容，否则为空字符串"
}
```
"""


class AllocationAgent(BaseAgent):
    """资产配置Agent - 估值驱动的动态配比决策"""

    def __init__(self, llm_gateway, data_service, config: Dict[str, Any]):
        super().__init__(llm_gateway, config)
        self.data_service = data_service

    def get_system_prompt(self) -> str:
        return SYSTEM_PROMPT

    def _format_analysis_input(self, data: Dict[str, Any]) -> str:
        """构建给大模型的输入文本，包含用户画像原始回答"""
        profile = data.get("profile", {})
        valuation = data.get("valuation", {})
        time_series = data.get("time_series", {})

        lines = []

        # 用户画像 — 结构化字段
        lines.append("# 用户画像\n")
        field_labels = {
            "investment_amount": "投资金额(万元)",
            "investment_horizon": "投资期限",
            "annual_income": "年收入(万元)",
            "max_drawdown_tolerance": "最大回撤容忍度(%)",
            "expected_annual_return": "期望年化收益(%)",
            "investment_experience": "投资经验",
            "risk_preference": "风险偏好(系统推断)",
            "cognitive_level": "认知水平(系统推断)",
            "mental_stability": "心态稳定性(系统推断)",
            "scenario_response": "场景反应类型(系统推断)",
            "special_notes": "特别说明",
        }
        for k, label in field_labels.items():
            v = profile.get(k, "")
            if v:
                lines.append(f"- {label}: {v}")

        # 用户原始回答 — 让LLM可以直接引用
        raw = profile.get("raw_answers", {})
        if raw:
            lines.append("\n## 用户原始回答（请在决策溯源中直接引用这些回答）\n")
            answer_labels = {
                "investment_amount": "资金规模",
                "investment_horizon": "投资期限",
                "annual_income": "年收入",
                "max_drawdown_tolerance": "最大回撤容忍",
                "expected_annual_return": "期望年化收益",
                "investment_experience": "投资经验",
                "scenario_crash": "暴跌场景反应",
                "scenario_surge": "暴涨场景反应",
            }
            for field, label in answer_labels.items():
                ans = raw.get(field, "")
                if ans:
                    lines.append(f"- 问：{label} → 用户回答：「{ans}」")

        # 估值数据
        lines.append("\n# 当前市场估值数据\n")
        valuation_text = self.data_service.get_valuation_text_for_llm()
        lines.append(valuation_text)

        # PE/PB分位数摘要
        if time_series:
            lines.append("\n# 各指数PE/PB历史分位总结\n")
            for idx_code, pe_data in time_series.get("pe_history", {}).items():
                current = pe_data.get("current", {})
                if current:
                    lines.append(f"- {idx_code}: {json.dumps(current, ensure_ascii=False, default=str)}")

        return "\n".join(lines)

    async def analyze(self, data: Dict[str, Any]) -> Dict[str, Any]:
        system_prompt = self.get_system_prompt()
        user_content = self._format_analysis_input(data)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        def _call():
            return self.llm.call(messages, stream=False)

        result = await asyncio.to_thread(_call)
        parsed = self._parse_analysis_output(result)

        if "raw_content" in parsed and "allocation" not in parsed:
            logger.warning("配置方案解析失败，返回原始内容")
            return {"raw_content": parsed["raw_content"], "allocation": [], "summary": "方案生成中出现解析问题，请参考以下原始分析"}

        return parsed

    async def analyze_stream(self, data: Dict[str, Any]) -> AsyncGenerator[Dict, None]:
        """流式分析，thinking内容实时推送，最终返回结构化结果"""
        system_prompt = self.get_system_prompt()
        user_content = self._format_analysis_input(data)

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
                elif chunk.get("type") == "error":
                    logger.error(f"流式调用出错: {chunk['content']}")
            elif isinstance(chunk, str):
                full_content += chunk

        # 解析最终的JSON结果
        parsed = self._parse_analysis_output({"content": full_content})
        if "raw_content" in parsed and "allocation" not in parsed:
            logger.warning("配置方案解析失败，返回原始内容")
            parsed = {"raw_content": parsed.get("raw_content", full_content), "allocation": [], "summary": "方案解析出现问题，请参考原始分析"}

        yield {"type": "result", "data": parsed}
