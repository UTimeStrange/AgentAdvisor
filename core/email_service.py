import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Dict, List, Optional

logger = logging.getLogger("email_service")


class EmailService:
    """邮件推送服务"""

    def __init__(self, config: Dict):
        self.smtp_host = config.get("smtp_host", "smtp.qq.com")
        self.smtp_port = config.get("smtp_port", 465)
        self.sender = config.get("sender", "")
        self.password = os.environ.get("EMAIL_PASSWORD", "")
        self.receivers = config.get("receivers", [])

    def send_html_email(self, subject: str, html_content: str, receivers: Optional[List[str]] = None):
        """发送HTML格式邮件"""
        to_list = receivers or self.receivers
        if not to_list:
            logger.warning("未配置收件人列表，跳过邮件发送")
            return
        if not self.sender or not self.password:
            logger.warning("未配置发件邮箱或密码，跳过邮件发送")
            return

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = subject
            msg["From"] = self.sender
            msg["To"] = ", ".join(to_list)

            html_part = MIMEText(html_content, "html", "utf-8")
            msg.attach(html_part)

            with smtplib.SMTP_SSL(self.smtp_host, self.smtp_port) as server:
                server.login(self.sender, self.password)
                server.sendmail(self.sender, to_list, msg.as_string())

            logger.info(f"邮件发送成功: {subject} -> {to_list}")
        except Exception as e:
            logger.error(f"邮件发送失败: {e}")

    def send_alert(self, subject: str, body_text: str, receivers: Optional[List[str]] = None):
        """发送预警邮件（黑金风格HTML模板）"""
        html = f"""
        <html>
        <body style="background-color: #0D0D0D; color: #E8E8E8; font-family: 'DIN Next', Arial, sans-serif; padding: 24px;">
            <div style="max-width: 600px; margin: 0 auto; background: #141414; border-radius: 12px; padding: 32px; border: 1px solid #C9A84C33;">
                <h1 style="color: #C9A84C; font-size: 22px; margin-top: 0; border-bottom: 1px solid #C9A84C33; padding-bottom: 16px;">
                    Agent Advisor 市场预警
                </h1>
                <div style="font-size: 14px; line-height: 1.8; color: #E8E8E8;">
                    {body_text}
                </div>
                <div style="margin-top: 24px; padding-top: 16px; border-top: 1px solid #333; color: #666; font-size: 12px;">
                    此邮件由 Agent Advisor 多智能体投顾系统自动发送
                </div>
            </div>
        </body>
        </html>
        """
        self.send_html_email(subject, html, receivers)
