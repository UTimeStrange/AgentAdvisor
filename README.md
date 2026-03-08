# AgentAdvisor - AI多智能体投顾系统

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

一个基于多智能体协作的智能投顾系统，通过AI对话采集用户画像，结合实时市场估值数据，动态生成个性化的场内ETF资产配置方案。

## 核心特点

- **多智能体协作** — 用户画像Agent、数据采集Agent、资产配置Agent、报告生成Agent、监控预警Agent 五大智能体流水线协作
- **估值驱动配置** — 基于标的实时估值水平（PE/PB分位数等）动态调整仓位，拒绝让用户在最高点接盘
- **全场内ETF** — 仅配置场内被动ETF基金（宽基指数、行业主题、QDII跨境、债券、黄金、货币），流动性好、费率低
- **AI对话画像** — 通过自然对话+情景问答深度了解用户风险偏好，而非传统机械选择题
- **兼容多模型** — 支持 OpenAI GPT、Claude、DeepSeek 等主流大模型，一键切换
- **实时监控预警** — 每日自动检测市场恐慌指数和异常波动，智能推送邮件预警
- **PDF报告导出** — 支持导出格式精美的中文PDF投资建议报告

## 系统架构

```
┌──────────────────────────────────────────────────┐
│                  前端 (黑金风格)                    │
│  ┌─────────────────┐  ┌────────────────────────┐ │
│  │  AI 对话面板     │  │  报告展示面板           │ │
│  │  · 用户画像问答  │  │  · 资产配比环形图       │ │
│  │  · 快捷选项     │  │  · 时序走势图           │ │
│  │  · 流式对话     │  │  · 配置明细表           │ │
│  └─────────────────┘  │  · 买入节奏建议         │ │
│                       └────────────────────────┘ │
└──────────────────────────────────────────────────┘
                        │ WebSocket
┌──────────────────────────────────────────────────┐
│                  后端 (FastAPI)                    │
│  ┌──────────────────────────────────────────────┐│
│  │              多智能体层                        ││
│  │  画像Agent → 数据Agent → 配置Agent → 报告Agent ││
│  │                     监控Agent (定时)          ││
│  ├──────────────────────────────────────────────┤│
│  │              核心服务层                        ││
│  │  LLM Gateway │ DataService │ EmailService    ││
│  └──────────────────────────────────────────────┘│
└──────────────────────────────────────────────────┘
```

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
```

> **注意**: 如果使用 conda 环境，请先激活环境：
> ```bash
> conda activate agent_advisor
> pip install -r requirements.txt
> ```

### 2. 配置大模型

编辑 `config.yaml`，选择你要使用的大模型 provider（默认 `openai`）：

```yaml
llm:
  default_provider: "openai"  # 可选: openai / deepseek / claude
```

设置对应的 API Key 环境变量：

```bash
# 选择你使用的模型，设置对应的 Key（至少设置一个）
export OPENAI_API_KEY="your-openai-key"
# export DEEPSEEK_API_KEY="your-deepseek-key"
# export ANTHROPIC_API_KEY="your-claude-key"
```

### 3. 启动

```bash
python main.py
```

浏览器访问 `http://localhost:8000`

## 配置说明

`config.yaml` 中包含：

- **llm** — 大模型配置（provider切换、API地址、模型名称）
- **email** — 邮件推送配置（SMTP信息）
- **scheduler** — 定时监控任务配置
- **etf_pool** — ETF标的池（宽基、行业、QDII、债券、黄金、货币）
- **data** — 数据缓存路径与过期策略

## 技术栈

| 模块 | 技术 |
|------|------|
| 后端框架 | Python FastAPI |
| 前端 | 原生HTML/CSS/JS，黑金扁平风格 |
| 数据源 | akshare（免费） |
| 数据缓存 | 本地CSV |
| 大模型 | GPT / Claude / DeepSeek |
| 定时任务 | APScheduler |
| 邮件 | smtplib |

## 项目结构

```
agent-Advisor/
├── main.py              # FastAPI 入口
├── config.yaml          # 全局配置
├── requirements.txt     # 依赖
├── core/
│   ├── llm_gateway.py   # 统一大模型网关
│   ├── data_service.py  # 数据采集与缓存
│   └── email_service.py # 邮件推送
├── agents/
│   ├── base_agent.py    # Agent基类
│   ├── profile_agent.py # 用户画像Agent
│   ├── allocation_agent.py  # 资产配置Agent
│   ├── report_agent.py  # 报告生成Agent
│   └── monitor_agent.py # 监控预警Agent
├── static/
│   └── index.html       # 前端页面
└── data/
    ├── cross_section/   # 横截面数据
    └── time_series/     # 时序数据
```
