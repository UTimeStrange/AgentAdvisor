import json
import logging
import asyncio
import random
from typing import Dict, Any, AsyncGenerator, List, Optional

from agents.base_agent import BaseAgent

logger = logging.getLogger("profile_agent")


# ================================================================
# 每个问题维度多个变体，每次会话随机选一种问法
# 保持: options 和 field 不变，只变 question 的措辞/背景/案例
# ================================================================

QUESTION_VARIANTS = {
    "investment_amount": {
        "field": "investment_amount",
        "options": ["10万以内", "10-50万", "50-100万", "100-300万", "300万以上"],
        "variants": [
            "你好，我是你的AI投资顾问。在制定配置方案之前，先了解一下你的情况。\n\n这次打算用多少资金来做投资？可以直接选择区间，也可以输入具体金额。",
            "你好！在开始之前，我需要了解几个关键信息。\n\n首先最重要的——你这次计划投入的资金规模大概是多少？这会直接影响到我给你的方案。",
            "你好，欢迎来到AI投顾。我会通过几个问题快速了解你，然后给你一份量身定制的ETF配置方案。\n\n第一个问题：你手头准备用来投资的资金大概有多少？",
            "嗨，我是你的专属AI投资顾问。给你做方案之前，先聊几个问题。\n\n你计划拿出多少钱来做这次投资？比如是闲置的存款，还是攒了一段时间准备理财的钱？",
        ],
    },
    "investment_horizon": {
        "field": "investment_horizon",
        "options": ["1年以内", "1-3年", "3-5年", "5-10年", "10年以上"],
        "variants": [
            "好的，已记录。\n\n这笔钱大概可以放多长时间不用？投资期限对可选的资产类别影响很大——短期只能配固收，长期才能加权益。",
            "收到。\n\n接下来很关键：这笔钱你多久之内可能需要用到？比如买房、结婚、子女教育这些大额开支在多久以后？",
            "了解了。\n\n你这笔钱是短期理财还是长期投资？如果3年内可能要用来买房或者应急，那方案会完全不同。",
            "明白了。\n\n假设不出意外，这笔钱你打算投多久？举个例子：如果是5年不动的钱，我可以多配一些权益资产来追求更高收益。",
        ],
    },
    "annual_income": {
        "field": "annual_income",
        "options": ["10万以下", "10-30万", "30-50万", "50-100万", "100万以上"],
        "variants": [
            "了解了。\n\n方便透露一下你目前的年收入水平吗？这有助于我判断投资金额占你总收入的比重，避免过度集中。",
            "好的。\n\n为了评估这笔投资在你整体财务中的比例，请问你的年收入大概在什么水平？放心，这不会影响方案本身，只是帮我做风险评估。",
            "收到。\n\n另一个参考信息——你目前一年的收入大概多少？知道这个我才好判断你承受波动的底气有多大。",
            "明白了。\n\n简单聊一下收入情况吧。你现在年收入大概在哪个区间？这有助于我判断这笔投资对你来说是'小试牛刀'还是'重注出击'。",
        ],
    },
    "max_drawdown": {
        "field": "max_drawdown_tolerance",
        "options": ["最多亏5%", "最多亏10%", "最多亏20%", "最多亏30%", "亏多少都能扛"],
        "variants": [
            "明白了。\n\n投资过程中难免有波动，你能接受的最大亏损幅度是多少？比如投入100万，最多能承受亏多少？",
            "了解。\n\n直接问：你能忍受账户缩水多少？比如你投了50万，如果有一天打开账户发现变成了45万，你是什么感受？变成40万呢？35万呢？你的底线在哪？",
            "好的。\n\n想象一个场景：你把钱投进去之后，市场大跌，你的账户每天都在亏。你心里的那根弦在亏多少的时候会绷断？",
            "收到。\n\n2024年10月份有一波很猛的行情，但之前A股曾经一路阴跌。如果你当时满仓，可能面临20-30%的浮亏。你能扛住多大的回撤？",
        ],
    },
    "expected_return": {
        "field": "expected_annual_return",
        "options": ["1-3%，跑赢通胀就行", "4-6%就够了", "6-10%", "10-15%", "15-20%", "20%以上"],
        "variants": [
            "收到。\n\n你对这笔投资的年化收益预期大概是多少？",
            "了解了。\n\n你希望这笔钱每年大概能赚多少？给你一个参考：银行大额存单一年大概2%左右，沪深300长期年化大概8-10%。",
            "好的。\n\n聊一下你的收益目标吧。如果把钱存银行每年大概2%，你对投资的期望要比这高多少？",
            "明白了。\n\n你对年化收益的预期是什么水平？坦率说，长期年化10%已经是很优秀的成绩了，巴菲特也就20%左右。你的目标呢？",
        ],
    },
    "investment_experience": {
        "field": "investment_experience",
        "options": ["没有任何投资经验", "买过银行理财或余额宝", "买过基金", "炒过股票", "有丰富的投资经验"],
        "variants": [
            "好的。\n\n你之前有过哪些投资经验？",
            "收到。\n\n在投资这件事上，你算是新手还是老手？之前接触过股票、基金、理财产品这些吗？",
            "了解了。\n\n聊一下你的投资履历吧——是完全的小白，还是已经在市场里摸爬滚打过一阵了？",
            "好的。\n\n你之前碰过哪些理财方式？比如余额宝、银行理财、基金定投、炒股票……或者说这是你第一次认真做投资？",
        ],
    },
    "scenario_crash": {
        "field": "scenario_crash",
        "options": ["马上卖掉止损", "先不看了，等它回来", "正好便宜，再买一些", "可能会焦虑睡不好"],
        "variants": [
            "了解。下面通过一个场景来看看你的投资心态。\n\n假设你刚买入一只基金，一周之内跌了15%，你会怎么做？",
            "好的。来做个小测试，帮我了解你面对亏损时的真实反应。\n\n假如你前天刚买了10万块的沪深300ETF，今天一看已经跌了1.5万，而且还在跌。你的第一反应是什么？",
            "收到。接下来想了解一下你面对市场波动时的心态。\n\n想象一下：你上周刚入手了一只ETF，这周市场突然暴跌，你的持仓一下子亏了15%。你会怎么处理？",
            "了解了。来一道情景题。\n\n2024年初A股曾经连续大跌，假如你那时候满仓在手，打开账户发现比买入时亏了15%，你会怎么做？",
        ],
    },
    "scenario_surge": {
        "field": "scenario_surge",
        "options": ["全部卖出锁定利润", "卖掉一半，留一半", "继续持有不动", "再追加投入"],
        "variants": [
            "最后一个问题。\n\n假设你持有的一只基金3个月涨了40%，你会怎么操作？",
            "最后一道题。\n\n反过来想：你买了一只ETF，3个月就赚了40%——收益远超你的预期。你会怎么操作？",
            "再测一道。\n\n假设你买了半导体ETF，赶上一波大行情，3个月账户上涨了40%。你打算怎么处理这笔收益？",
            "最后一个场景。\n\n想象一下：你持仓3个月赚了40%，身边的人都在说'牛市来了'。这时候你会选择落袋为安，还是继续持有甚至加码？",
        ],
    },
}

# 固定的问题ID顺序
QUESTION_ORDER = [
    "investment_amount",
    "investment_horizon",
    "annual_income",
    "max_drawdown",
    "expected_return",
    "investment_experience",
    "scenario_crash",
    "scenario_surge",
]

# 对高收益预期的教育回复
HIGH_RETURN_EDUCATION = """坦率讲，长期稳定年化20%以上在正规投资渠道是极难实现的。巴菲特过去几十年的年化收益大约在20%左右，普通投资者很难超越这个水平。

过高的收益预期往往意味着需要承担极高的风险，甚至可能落入一些不合规的投资陷阱。我会基于你的实际情况给出一个合理且可持续的方案。

"""


class ProfileAgent(BaseAgent):
    """用户画像Agent - 预设问答流程，每次会话随机选取问题变体"""

    def __init__(self, llm_gateway, config: Dict[str, Any]):
        super().__init__(llm_gateway, config)

    @staticmethod
    def _select_variants() -> List[Dict]:
        """从每个问题维度中随机选一个变体，组成本次问卷"""
        questions = []
        for qid in QUESTION_ORDER:
            qdef = QUESTION_VARIANTS[qid]
            question_text = random.choice(qdef["variants"])
            questions.append({
                "id": qid,
                "question": question_text,
                "options": qdef["options"],
                "field": qdef["field"],
            })
        return questions

    def get_system_prompt(self) -> str:
        return ""

    async def get_welcome_message(self) -> Dict:
        """返回第一个问题及本次随机选取的问题列表（存入session用）"""
        questions = self._select_variants()
        first_q = questions[0]
        return {
            "content": first_q["question"],
            "quick_options": first_q["options"],
            "questions": questions,  # 由 main.py 存入 session
        }

    async def chat(self, user_message: str, context: Dict) -> AsyncGenerator[Dict, None]:
        """处理用户回答，返回下一个问题或完成画像"""
        step = context.get("profile_step", 0)
        answers = context.get("profile_answers", {})
        questions = context.get("profile_questions", [])
        if not questions:
            questions = self._select_variants()
            context["profile_questions"] = questions

        # 记录当前回答
        if step < len(questions):
            current_q = questions[step]
            answers[current_q["field"]] = user_message.strip()

        # 检查是否需要对高收益预期做教育
        education_text = ""
        if step < len(questions) and questions[step]["id"] == "expected_return":
            education_text = self._check_return_expectation(user_message)

        # 下一步
        next_step = step + 1

        if next_step < len(questions):
            # 还有下一个问题
            next_q = questions[next_step]
            reply_text = education_text + next_q["question"]

            async for chunk in self._fake_stream(reply_text):
                yield chunk

            yield {"type": "quick_options", "options": next_q["options"]}

            context["profile_step"] = next_step
            context["profile_answers"] = answers

        else:
            # 所有问题回答完毕，生成画像
            profile = self._build_profile(answers)

            complete_text = education_text + "所有问题已经了解完毕，正在为你分析并生成专属资产配置方案，请稍候..."

            async for chunk in self._fake_stream(complete_text):
                yield chunk

            yield {"type": "profile_complete", "profile": profile}
            context["profile_step"] = next_step
            context["profile_answers"] = answers

    async def _fake_stream(self, text: str):
        """模拟流式输出，逐段发送文本"""
        segments = []
        buf = ""
        for ch in text:
            buf += ch
            if ch in "，。！？、；\n：" or len(buf) >= 12:
                segments.append(buf)
                buf = ""
        if buf:
            segments.append(buf)

        for seg in segments:
            yield {"type": "stream", "content": seg}
            delay = 0.02 + random.random() * 0.04
            await asyncio.sleep(delay)

    def _check_return_expectation(self, user_input: str) -> str:
        """检查用户收益预期是否过高"""
        text = user_input.lower().replace("%", "").replace("％", "")

        import re
        numbers = re.findall(r'(\d+)', text)

        is_high = False
        if numbers:
            max_num = max(int(n) for n in numbers)
            if max_num >= 20:
                is_high = True
        elif "20" in text or "以上" in text or "越高越好" in text or "越多越好" in text:
            is_high = True

        if is_high:
            return HIGH_RETURN_EDUCATION

        return ""

    def _build_profile(self, answers: Dict) -> Dict:
        """根据所有回答构建用户画像"""
        profile = {
            "investment_amount": self._parse_amount(answers.get("investment_amount", "")),
            "investment_horizon": answers.get("investment_horizon", ""),
            "annual_income": self._parse_income(answers.get("annual_income", "")),
            "max_drawdown_tolerance": self._parse_drawdown(answers.get("max_drawdown_tolerance", "")),
            "expected_annual_return": self._parse_return(answers.get("expected_annual_return", "")),
            "investment_experience": self._map_experience(answers.get("investment_experience", "")),
            "risk_preference": "",
            "cognitive_level": "",
            "mental_stability": "",
            "scenario_response": "",
            "special_notes": "",
            "raw_answers": answers,
        }

        # 推断认知水平
        ret = profile["expected_annual_return"]
        if ret > 20:
            profile["cognitive_level"] = "低"
            profile["special_notes"] = "用户期望年化收益过高，已进行教育引导，后续配置需保守处理"
        elif ret > 15:
            profile["cognitive_level"] = "中"
        else:
            profile["cognitive_level"] = "高"

        # 推断风险偏好
        dd = profile["max_drawdown_tolerance"]
        if dd <= 5:
            profile["risk_preference"] = "保守"
        elif dd <= 10:
            profile["risk_preference"] = "稳健"
        elif dd <= 20:
            profile["risk_preference"] = "平衡"
        elif dd <= 30:
            profile["risk_preference"] = "积极"
        else:
            profile["risk_preference"] = "激进"

        # 推断心态稳定性(基于暴跌场景)
        crash_answer = answers.get("scenario_crash", "").lower()
        if "卖" in crash_answer or "止损" in crash_answer:
            profile["mental_stability"] = "脆弱"
            profile["scenario_response"] = "止损型"
        elif "焦虑" in crash_answer or "睡不好" in crash_answer or "失眠" in crash_answer:
            profile["mental_stability"] = "脆弱"
            profile["scenario_response"] = "焦虑型"
        elif "不看" in crash_answer or "等" in crash_answer or "不管" in crash_answer:
            profile["mental_stability"] = "一般"
            profile["scenario_response"] = "忽略型"
        elif "买" in crash_answer or "加" in crash_answer or "便宜" in crash_answer:
            profile["mental_stability"] = "强韧"
            profile["scenario_response"] = "加仓型"
        else:
            profile["mental_stability"] = "一般"
            profile["scenario_response"] = "未知"

        return profile

    def _parse_amount(self, text: str) -> float:
        """解析资金量(万元)"""
        import re
        text = text.replace(",", "").replace("，", "")
        numbers = re.findall(r'(\d+)', text)
        if not numbers:
            if "10万以内" in text:
                return 8
            return 30

        val = int(numbers[-1])
        if "万" in text:
            return val
        elif "百万" in text:
            return val * 100
        elif val > 1000:
            return val / 10000
        return val

    def _parse_income(self, text: str) -> float:
        """解析年收入(万元)"""
        import re
        numbers = re.findall(r'(\d+)', text)
        if numbers:
            return int(numbers[-1])
        return 20

    def _parse_drawdown(self, text: str) -> float:
        """解析最大回撤(百分比数字)"""
        import re
        if "无所谓" in text or "都能扛" in text or "不在乎" in text:
            return 50
        numbers = re.findall(r'(\d+)', text)
        if numbers:
            return int(numbers[-1])
        return 10

    def _parse_return(self, text: str) -> float:
        """解析期望年化(百分比数字)"""
        import re
        numbers = re.findall(r'(\d+)', text)
        if numbers:
            return max(int(n) for n in numbers)
        if "越高越好" in text or "越多越好" in text:
            return 30
        return 8

    def _map_experience(self, text: str) -> str:
        """映射投资经验等级"""
        if "没有" in text or "无" in text or "从未" in text:
            return "无经验"
        elif "理财" in text or "余额宝" in text:
            return "初级"
        elif "基金" in text:
            return "中级"
        elif "股票" in text or "炒" in text:
            return "中级"
        elif "丰富" in text:
            return "资深"
        return "初级"
