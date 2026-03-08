import json
import logging
import asyncio
from typing import Dict, Any

from agents.base_agent import BaseAgent

logger = logging.getLogger("monitor_agent")

SYSTEM_PROMPT = """你是一位市场监控预警专家。你的任务是根据市场数据判断是否需要给用户发送预警通知。

## 监控规则

### 暴跌预警（可能加仓机会）
- 某标的单日跌幅 >= 5%
- 必须同时满足：该标的当前PE分位数 < 60%（低估或合理估值）
- 如果PE分位 > 80%（高估标的暴跌），不建议加仓，这是价值回归，不是机会
- 恐慌指数(QVIX)如果同时飙升，说明市场整体恐慌，是更好的加仓时机

### 暴涨预警（可能止盈机会）
- 某标的单日涨幅 >= 5%
- 如果该标的PE分位已经 > 70%，建议用户考虑部分止盈
- 如果涨幅连续多日，建议动态再平衡

### 输出格式

```json
{
  "has_alert": true/false,
  "alerts": [
    {
      "type": "crash_opportunity/crash_warning/surge_takeprofit",
      "etf_code": "代码",
      "etf_name": "名称",
      "change_pct": 涨跌幅百分比,
      "pe_percentile": PE分位(如有),
      "action": "建议操作",
      "reason": "理由",
      "urgency": "high/medium/low"
    }
  ],
  "market_sentiment": "恐慌/正常/贪婪",
  "qvix_level": QVIX数值(如有),
  "summary": "一段给用户看的总结"
}
```

注意：要温和、理性、安抚用户情绪。不要制造恐慌，也不要鼓励追涨。强调长期持有和理性投资。
"""


class MonitorAgent(BaseAgent):
    """监控预警Agent - 每日检测暴涨暴跌，推送邮件预警"""

    def __init__(self, llm_gateway, data_service, email_service, config: Dict[str, Any]):
        super().__init__(llm_gateway, config)
        self.data_service = data_service
        self.email_service = email_service

    def get_system_prompt(self) -> str:
        return SYSTEM_PROMPT

    async def run_daily_check(self):
        """执行每日监控检查"""
        logger.info("开始每日监控检查...")

        # 1. 刷新数据
        await asyncio.to_thread(self.data_service.ensure_fresh_data)

        # 2. 获取当日涨跌幅
        changes = self.data_service.get_daily_changes()

        # 3. 获取估值数据
        valuation_text = self.data_service.get_valuation_text_for_llm()

        # 4. 获取QVIX
        ts_data = self.data_service.get_time_series_summary()
        qvix_info = ts_data.get("qvix", {})

        # 5. 检查是否有异常波动
        abnormal = []
        for code, pct in changes.items():
            if abs(pct) >= 5:
                abnormal.append({"code": code, "change": pct})

        if not abnormal:
            logger.info("今日无异常波动，跳过预警")
            return

        # 6. 构建输入给大模型
        input_text = f"""## 今日异常波动标的
{json.dumps(abnormal, ensure_ascii=False)}

## 当前估值数据
{valuation_text}

## 恐慌指数(QVIX)
{json.dumps(qvix_info, ensure_ascii=False, default=str) if qvix_info else "暂无数据"}

请分析以上数据，判断是否需要发送预警通知。
"""

        messages = [
            {"role": "system", "content": self.get_system_prompt()},
            {"role": "user", "content": input_text},
        ]

        def _call():
            return self.llm.call(messages, stream=False)

        result = await asyncio.to_thread(_call)
        content = result.get("content", "")

        # 7. 解析结果
        alert_data = self._parse_analysis_output(result)

        if alert_data.get("has_alert"):
            summary = alert_data.get("summary", content)
            self.email_service.send_alert(
                subject="Agent Advisor 市场预警",
                body_text=summary,
            )
            logger.info(f"已发送预警邮件: {summary[:100]}...")
        else:
            logger.info("大模型判断无需预警")
