import os
import json
import asyncio
import logging
import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi import HTTPException
from pydantic import BaseModel
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("agent_advisor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("main")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_DIR = os.path.join(BASE_DIR, "temp")

# 确保临时目录存在
os.makedirs(TEMP_DIR, exist_ok=True)

with open(os.path.join(BASE_DIR, "config.yaml"), "r", encoding="utf-8") as f:
    CONFIG = yaml.safe_load(f)

from core.llm_gateway import LLMGateway
from core.data_service import DataService
from core.email_service import EmailService
from core.pdf_generator import generate_pdf_report
from agents.profile_agent import ProfileAgent
from agents.allocation_agent import AllocationAgent
from agents.report_agent import ReportAgent
from agents.monitor_agent import MonitorAgent
from agents.consult_agent import ConsultAgent

app = FastAPI(title="Agent Advisor - 多智能体投顾系统")

llm_gateway = LLMGateway(CONFIG["llm"])
data_service = DataService(CONFIG["data"], CONFIG["etf_pool"], BASE_DIR)
email_service = EmailService(CONFIG.get("email", {}))

profile_agent = ProfileAgent(llm_gateway, CONFIG)
allocation_agent = AllocationAgent(llm_gateway, data_service, CONFIG)
report_agent = ReportAgent(llm_gateway, CONFIG)
monitor_agent = MonitorAgent(llm_gateway, data_service, email_service, CONFIG)
consult_agent = ConsultAgent(llm_gateway, data_service, CONFIG)

scheduler = AsyncIOScheduler()

app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


class ExportRequest(BaseModel):
    """导出请求模型"""
    content: str
    title: str = "投资顾问报告"
    allocation: dict = None
    charts: dict = None


@app.post("/api/export/pdf")
async def export_pdf(request: ExportRequest):
    """将 Markdown 报告导出为 PDF"""
    try:
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"investment_report_{timestamp}.pdf"
        output_path = os.path.join(TEMP_DIR, filename)

        pdf_path = generate_pdf_report(
            markdown_content=request.content,
            output_path=output_path,
            title=request.title,
        )

        return FileResponse(
            path=pdf_path,
            media_type="application/pdf",
            filename=filename,
        )
    except Exception as e:
        logger.error(f"PDF 导出失败: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"PDF 导出失败: {str(e)}")


@app.get("/")
async def index():
    return FileResponse(os.path.join(BASE_DIR, "static", "index.html"))


@app.get("/api/config")
async def get_config():
    """返回前端需要的配置信息，包含 code->name 映射"""
    providers = list(CONFIG["llm"]["providers"].keys())

    # 构建 index_code / etf_code -> name 的映射表
    code_name_map = {}
    for category in CONFIG.get("etf_pool", {}):
        for item in CONFIG["etf_pool"][category]:
            code = item.get("code", "")
            name = item.get("name", "")
            idx_code = item.get("index_code", "")
            if code:
                code_name_map[code] = name
            if idx_code:
                code_name_map[idx_code] = name

    return {
        "providers": providers,
        "default_provider": CONFIG["llm"]["default_provider"],
        "code_name_map": code_name_map,
    }


@app.get("/api/data/status")
async def data_status():
    """返回数据缓存状态"""
    status = data_service.get_cache_status()
    return status


@app.get("/api/data/refresh")
async def data_refresh():
    """手动刷新数据"""
    try:
        await asyncio.to_thread(data_service.refresh_all)
        return {"status": "ok", "message": "数据刷新完成"}
    except Exception as e:
        logger.error(f"数据刷新失败: {e}")
        return {"status": "error", "message": str(e)}


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()
    session = {
        "stage": "profiling",
        "profile": {},
        "allocation": {},
        "history": [],
        "provider": CONFIG["llm"]["default_provider"],
        "profile_step": 0,
        "profile_answers": {},
        "consult_context_sent": False,
    }

    try:
        # 等待前端的第一条消息，决定是新会话还是重连恢复
        init_raw = await websocket.receive_text()
        init_msg = json.loads(init_raw)

        if init_msg.get("type") == "resume" and init_msg.get("stage") == "done":
            # 重连恢复：跳过 profiling，直接进入自由咨询阶段
            session["stage"] = "done"
            await websocket.send_json({"type": "status", "content": "方案已生成"})
            await websocket.send_json({"type": "system", "content": "连接已恢复，可以继续咨询"})
        else:
            # 新会话：发送欢迎消息，开始 profiling
            welcome = await profile_agent.get_welcome_message()
            session["profile_questions"] = welcome.get("questions", [])
            await websocket.send_json({
                "type": "assistant",
                "content": welcome["content"],
                "quick_options": welcome.get("quick_options", []),
                "stage": "profiling",
            })

        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)

            if msg.get("type") == "switch_provider":
                session["provider"] = msg["provider"]
                llm_gateway.set_provider(msg["provider"])
                await websocket.send_json({"type": "system", "content": f"已切换到 {msg['provider']}"})
                continue

            user_text = msg.get("content", "")

            # profiling阶段记录history，done阶段由consult_agent管理history
            if session["stage"] != "done":
                session["history"].append({"role": "user", "content": user_text})

            if session["stage"] == "profiling":
                await websocket.send_json({"type": "status", "content": "画像采集中"})

                full_response = ""
                quick_options = []
                profile_complete = False

                async for chunk in profile_agent.chat(user_text, session):
                    if chunk.get("type") == "stream":
                        await websocket.send_json({"type": "stream", "content": chunk["content"]})
                        full_response += chunk["content"]
                    elif chunk.get("type") == "quick_options":
                        quick_options = chunk["options"]
                    elif chunk.get("type") == "profile_complete":
                        profile_complete = True
                        session["profile"] = chunk["profile"]

                await websocket.send_json({
                    "type": "stream_end",
                    "quick_options": quick_options,
                })

                session["history"].append({"role": "assistant", "content": full_response})

                if profile_complete:
                    session["stage"] = "allocating"
                    await websocket.send_json({"type": "status", "content": "正在读取市场数据"})

                    # 只读本地缓存，不实时拉取。数据由定时任务或离线脚本更新
                    valuation_data = await asyncio.to_thread(data_service.get_all_valuation_data)
                    time_series_data = await asyncio.to_thread(data_service.get_time_series_summary)

                    await websocket.send_json({"type": "status", "content": "AI配置方案生成中"})

                    # allocation_agent 改为流式调用，思考过程也推送给前端
                    allocation = None
                    async for chunk in allocation_agent.analyze_stream({
                        "profile": session["profile"],
                        "valuation": valuation_data,
                        "time_series": time_series_data,
                    }):
                        if chunk.get("type") == "thinking":
                            await websocket.send_json({"type": "thinking", "content": chunk["content"]})
                        elif chunk.get("type") == "result":
                            allocation = chunk["data"]

                    # 思考过程结束，发一条总结到chat bubble
                    summary_text = ""
                    if allocation and allocation.get("summary"):
                        summary_text = f"**配置方案已生成** — {allocation['summary']}"
                    else:
                        summary_text = "配置方案已生成，正在撰写详细报告..."
                    await websocket.send_json({"type": "stream", "content": summary_text})
                    await websocket.send_json({"type": "stream_end", "quick_options": []})

                    if allocation is None:
                        allocation = {"allocation": [], "summary": "方案生成失败，请重试"}

                    # 存入session供后续咨询引用
                    session["allocation"] = allocation

                    await websocket.send_json({"type": "status", "content": "报告生成中"})

                    async for chunk in report_agent.generate_report(allocation, session["profile"], time_series_data):
                        if chunk.get("type") == "thinking":
                            await websocket.send_json({"type": "report_thinking", "content": chunk["content"]})
                        elif chunk.get("type") == "stream":
                            await websocket.send_json({"type": "report_stream", "content": chunk["content"]})
                        elif chunk.get("type") == "report_data":
                            await websocket.send_json({
                                "type": "report_data",
                                "allocation": chunk["allocation"],
                                "charts": chunk.get("charts", {}),
                            })

                    await websocket.send_json({"type": "report_end"})
                    await websocket.send_json({"type": "status", "content": "方案已生成"})
                    session["stage"] = "done"

            elif session["stage"] == "done":
                # 自由聊天模式：用户可以咨询和调整配置方案
                await websocket.send_json({"type": "status", "content": "AI思考中"})

                async for chunk in consult_agent.chat_stream(
                    user_text,
                    session,
                ):
                    if chunk.get("type") == "thinking":
                        await websocket.send_json({"type": "thinking", "content": chunk["content"]})
                    elif chunk.get("type") == "stream":
                        await websocket.send_json({"type": "stream", "content": chunk["content"]})

                await websocket.send_json({"type": "stream_end", "quick_options": []})
                await websocket.send_json({"type": "status", "content": "方案已生成"})

    except WebSocketDisconnect:
        logger.info("WebSocket 断开连接")
    except Exception as e:
        logger.error(f"WebSocket 错误: {e}", exc_info=True)
        try:
            await websocket.send_json({"type": "error", "content": f"系统错误: {str(e)}"})
        except:
            pass


async def run_daily_monitor():
    """每日监控任务"""
    try:
        logger.info("执行每日监控任务...")
        await monitor_agent.run_daily_check()
        logger.info("每日监控任务完成")
    except Exception as e:
        logger.error(f"每日监控任务失败: {e}", exc_info=True)


@app.on_event("startup")
async def startup():
    # ---- 数据新鲜度检查 ----
    status = data_service.check_data_ready()
    logger.info(
        "数据预检: 共%d项, 最新%d项, 过期%d项, 缺失%d项",
        status["total"], status["fresh"], status["stale"], status["missing"],
    )
    if status["stale"] > 0 or status["missing"] > 0:
        logger.info("启动后台数据更新...")
        asyncio.create_task(asyncio.to_thread(data_service.ensure_fresh_data))

    # ---- 定时任务 ----
    sched_cfg = CONFIG.get("scheduler", {})

    # 定时任务: 每日监控预警
    scheduler.add_job(
        run_daily_monitor,
        "cron",
        hour=sched_cfg.get("monitor_hour", 18),
        minute=sched_cfg.get("monitor_minute", 0),
    )

    # 定时任务: 每日数据刷新（开盘后拉取最新数据）
    scheduler.add_job(
        lambda: asyncio.ensure_future(asyncio.to_thread(data_service.refresh_all)),
        "cron",
        hour=9,
        minute=35,
        id="daily_data_refresh",
    )

    scheduler.start()
    logger.info("定时任务调度器已启动 (监控: %02d:%02d, 数据刷新: 09:35)",
                sched_cfg.get("monitor_hour", 18), sched_cfg.get("monitor_minute", 0))


@app.on_event("shutdown")
async def shutdown():
    scheduler.shutdown()


if __name__ == "__main__":
    import uvicorn
    server_cfg = CONFIG.get("server", {})
    uvicorn.run(
        "main:app",
        host=server_cfg.get("host", "0.0.0.0"),
        port=server_cfg.get("port", 8000),
        reload=True,
    )
