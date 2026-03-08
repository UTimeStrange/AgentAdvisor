"""PDF 报告生成工具 - 使用 fpdf2 的 write_html 能力直接渲染 HTML"""
import markdown
from fpdf import FPDF
from datetime import datetime
import logging
import os
import platform

logger = logging.getLogger("pdf_generator")

# ----------------------------------------------------------------
# 中文字体探测
# ----------------------------------------------------------------

def get_chinese_font_path():
    """获取系统中文字体路径"""
    system = platform.system()
    if system == "Darwin":
        candidates = [
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
            "/System/Library/Fonts/Supplemental/Songti.ttc",
            "/System/Library/Fonts/PingFang.ttc",
        ]
    elif system == "Windows":
        candidates = [
            "C:/Windows/Fonts/simhei.ttf",
            "C:/Windows/Fonts/simsun.ttc",
            "C:/Windows/Fonts/msyh.ttc",
        ]
    else:
        candidates = [
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        ]
    for p in candidates:
        if os.path.exists(p):
            return p
    return None

# ----------------------------------------------------------------
# PDF 报告类
# ----------------------------------------------------------------

class ReportPDF(FPDF):
    """支持中文的 PDF 报告"""

    GOLD = (201, 168, 76)
    DARK = (26, 26, 26)
    BODY = (51, 51, 51)
    MUTED = (120, 120, 120)
    WHITE = (255, 255, 255)
    LIGHT_BG = (245, 245, 245)

    def __init__(self):
        super().__init__()
        self.set_auto_page_break(auto=True, margin=20)
        self._font = "Helvetica"
        font_path = get_chinese_font_path()
        if font_path:
            try:
                self.add_font("CN", "", font_path)
                self._font = "CN"
            except Exception as e:
                logger.warning(f"中文字体加载失败: {e}")

    def _set(self, size=10, color=None, style=""):
        """快捷设置字体"""
        if self._font == "CN":
            style = ""  # 自定义字体不支持 B/I
        self.set_font(self._font, style, size)
        if color:
            self.set_text_color(*color)

    # ---- 页眉/页脚 ----
    def header(self):
        self._set(8, self.MUTED)
        self.cell(0, 8, "AI 投资顾问报告", align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*self.GOLD)
        self.set_line_width(0.3)
        self.line(10, self.get_y(), 200, self.get_y())
        self.ln(4)

    def footer(self):
        self.set_y(-15)
        self._set(8, self.MUTED)
        self.cell(0, 8, f"— 第 {self.page_no()} 页 —", align="C")

    # ---- 高层元素 ----
    def title_block(self, title):
        self.ln(5)
        self._set(22, self.GOLD)
        self.cell(0, 14, title, align="C", new_x="LMARGIN", new_y="NEXT")
        self._set(10, self.MUTED)
        self.cell(0, 8, datetime.now().strftime("%Y 年 %m 月 %d 日"), align="C", new_x="LMARGIN", new_y="NEXT")
        self.set_draw_color(*self.GOLD)
        self.set_line_width(0.6)
        y = self.get_y() + 4
        self.line(30, y, 180, y)
        self.ln(12)

    def section(self, text):
        """二级标题"""
        self.ln(6)
        # 金色色块
        self.set_fill_color(*self.GOLD)
        self.rect(10, self.get_y(), 3, 8, "F")
        self.set_x(16)
        self._set(13, self.GOLD)
        self.cell(0, 8, text, new_x="LMARGIN", new_y="NEXT")
        self.ln(4)

    def subsection(self, text):
        """三级标题"""
        self.ln(3)
        self._set(11, self.DARK)
        self.cell(0, 7, text, new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def body_text(self, text):
        """正文段落（支持自动换行）"""
        self._set(10, self.BODY)
        w = self.w - self.l_margin - self.r_margin
        self.multi_cell(w, 6, text)
        self.ln(2)

    def bullet(self, text):
        """无序列表项 — 整体缩进，换行自动对齐"""
        self._set(10, self.BODY)
        indent = 6  # mm
        orig_margin = self.l_margin
        # 画圆点符号
        dot_x = orig_margin + 1
        dot_y = self.get_y() + 2.2
        self.set_fill_color(*self.BODY)
        self.ellipse(dot_x, dot_y, 1.5, 1.5, style="F")
        # 临时增大左边距 → multi_cell 每行都从 indent 位置开始
        self.set_left_margin(orig_margin + indent)
        self.set_x(orig_margin + indent)
        w = self.w - self.l_margin - self.r_margin
        self.multi_cell(w, 6, text)
        # 恢复原始边距
        self.set_left_margin(orig_margin)

    def ordered_item(self, num, text):
        """有序列表项 — 整体缩进，换行自动对齐"""
        self._set(10, self.BODY)
        indent = 10  # mm
        orig_margin = self.l_margin
        # 输出序号
        self.cell(indent, 6, f"  {num}.", new_x="END")
        # 临时增大左边距
        self.set_left_margin(orig_margin + indent)
        self.set_x(orig_margin + indent)
        w = self.w - self.l_margin - self.r_margin
        self.multi_cell(w, 6, text)
        # 恢复
        self.set_left_margin(orig_margin)

    # ---- 表格 ----
    def render_table(self, headers, rows):
        """
        渲染带自动列宽的表格。
        headers: list[str]
        rows: list[list[str]]
        """
        if not headers:
            return

        col_count = len(headers)
        page_w = 190  # mm

        # 计算每列最大字符宽度，按内容比例分配
        col_max = []
        for ci in range(col_count):
            max_len = len(headers[ci])
            for row in rows:
                if ci < len(row):
                    max_len = max(max_len, len(row[ci]))
            col_max.append(max(max_len, 2))

        total = sum(col_max)
        col_widths = [max(page_w * c / total, 12) for c in col_max]
        # 归一化使总和 = page_w
        scale = page_w / sum(col_widths)
        col_widths = [w * scale for w in col_widths]

        row_h = 7

        # ---- 表头 ----
        self.set_fill_color(*self.GOLD)
        self._set(8, self.WHITE)
        for ci, h in enumerate(headers):
            self.cell(col_widths[ci], row_h + 1, h, border=1, fill=True, align="C")
        self.ln()

        # ---- 表体 ----
        self._set(8, self.BODY)
        for ri, row in enumerate(rows):
            if ri % 2 == 0:
                self.set_fill_color(*self.LIGHT_BG)
            else:
                self.set_fill_color(*self.WHITE)

            # 检查是否需要换页
            if self.get_y() + row_h > self.h - 25:
                self.add_page()
                # 重绘表头
                self.set_fill_color(*self.GOLD)
                self._set(8, self.WHITE)
                for ci, h in enumerate(headers):
                    self.cell(col_widths[ci], row_h + 1, h, border=1, fill=True, align="C")
                self.ln()
                self._set(8, self.BODY)
                if ri % 2 == 0:
                    self.set_fill_color(*self.LIGHT_BG)
                else:
                    self.set_fill_color(*self.WHITE)

            for ci in range(col_count):
                val = row[ci] if ci < len(row) else ""
                self.cell(col_widths[ci], row_h, val, border=1, fill=True, align="C")
            self.ln()

        self.ln(5)


# ----------------------------------------------------------------
# Markdown → PDF 渲染引擎
# ----------------------------------------------------------------

import re

def _strip_md_bold(text):
    """移除 **bold** 标记，返回纯文本"""
    return re.sub(r'\*\*(.*?)\*\*', r'\1', text)

def _strip_md_italic(text):
    return re.sub(r'\*(.*?)\*', r'\1', text)

def _clean(text):
    return _strip_md_italic(_strip_md_bold(text))


def _parse_table(lines, start):
    """
    从 lines[start] 开始解析 Markdown 表格，返回 (headers, rows, next_index)。
    """
    headers = []
    rows = []
    i = start

    # 第一行是表头
    if '|' in lines[i]:
        headers = [c.strip() for c in lines[i].split('|') if c.strip()]
        i += 1
    else:
        return [], [], start

    # 跳过分隔行
    if i < len(lines) and re.match(r'^[\s|:\-]+$', lines[i].strip()):
        i += 1

    # 读取数据行
    while i < len(lines):
        line = lines[i].strip()
        if not line or '|' not in line:
            break
        if re.match(r'^[\s|:\-]+$', line):
            i += 1
            continue
        cells = [c.strip() for c in line.split('|') if c.strip()]
        rows.append(cells)
        i += 1

    return headers, rows, i


def generate_pdf_report(
    markdown_content: str,
    output_path: str,
    title: str = "投资顾问报告",
) -> str:
    """将 Markdown 报告转换为 PDF"""
    try:
        pdf = ReportPDF()
        pdf.add_page()
        pdf.title_block(title)

        lines = markdown_content.split('\n')
        n = len(lines)
        i = 0

        while i < n:
            raw = lines[i]
            line = raw.strip()

            # 空行
            if not line:
                i += 1
                continue

            # ---- 标题 ----
            if line.startswith('### '):
                pdf.subsection(_clean(line[4:]))
                i += 1; continue
            if line.startswith('## '):
                pdf.section(_clean(line[3:]))
                i += 1; continue
            if line.startswith('# '):
                pdf.section(_clean(line[2:]))
                i += 1; continue

            # ---- 表格 ----
            if ('|' in line
                    and i + 1 < n
                    and '|' in lines[i + 1]
                    and re.search(r'[\-:]{2,}', lines[i + 1])):
                headers, rows, next_i = _parse_table(lines, i)
                if headers:
                    pdf.render_table(headers, rows)
                i = next_i
                continue

            # ---- 无序列表 ----
            if line.startswith('- ') or line.startswith('* '):
                pdf.bullet(_clean(line[2:]))
                i += 1; continue

            # ---- 有序列表 ----
            m = re.match(r'^(\d+)\.\s+(.*)', line)
            if m:
                pdf.ordered_item(m.group(1), _clean(m.group(2)))
                i += 1; continue

            # ---- 分隔线 ----
            if re.match(r'^[-*]{3,}$', line):
                pdf.set_draw_color(200, 200, 200)
                pdf.line(10, pdf.get_y() + 3, 200, pdf.get_y() + 3)
                pdf.ln(8)
                i += 1; continue

            # ---- 普通段落 ----
            pdf.body_text(_clean(line))
            i += 1

        pdf.output(output_path)
        logger.info(f"PDF 报告生成成功: {output_path}")
        return output_path

    except Exception as e:
        logger.error(f"PDF 生成失败: {e}", exc_info=True)
        raise
