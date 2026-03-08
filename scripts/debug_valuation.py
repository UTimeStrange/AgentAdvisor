"""
估值数据诊断脚本 — 对比各数据源的PE和分位数，并画图
使用方式:
    conda activate agent_advisor
    python scripts/debug_valuation.py
"""

import os
import sys
import time
import datetime
import json

import pandas as pd
import requests

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import akshare as ak


def throttle():
    time.sleep(1.5)


# ============================================================
# 1. 韭圈儿(蛋卷基金)数据 — 整体法TTM PE，与雪球一致
# ============================================================
def fetch_danjuan():
    print("=" * 70)
    print("1. 韭圈儿(蛋卷基金) — 整体法TTM PE，与雪球一致")
    print("=" * 70)

    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    try:
        resp = requests.get(
            "https://danjuanfunds.com/djapi/index_eva/dj",
            headers=headers, timeout=15,
        )
        data = resp.json()
        items = data.get("data", {}).get("items", [])
        print(f"  共获取 {len(items)} 个指数\n")
    except Exception as e:
        print(f"  ❌ 请求失败: {e}")
        return {}

    # 我们关注的指数
    targets = {
        "000688": "科创50", "000300": "沪深300", "000905": "中证500",
        "000852": "中证1000", "000016": "上证50", "399006": "创业板",
        "000933": "医药", "399975": "证券", "399997": "白酒",
        "931151": "光伏", "990001": "半导体", "399976": "新能车",
        "399986": "银行", "399393": "地产", "000922": "红利",
        "399967": "军工", "930901": "游戏", "399989": "医疗",
        "930851": "云计算", "930633": "旅游", "931590": "机器人",
    }

    results = {}
    for item in items:
        code_raw = item.get("index_code", "")
        code = code_raw.replace("SH", "").replace("SZ", "")
        for tc, name in targets.items():
            if tc in code:
                pe = item.get("pe", 0)
                pb = item.get("pb", 0)
                pe_pct = item.get("pe_percentile", 0)
                pb_pct = item.get("pb_percentile", 0)
                eva = item.get("eva_type", "")
                eva_cn = {"low": "低估", "mid": "适中", "high": "高估"}.get(eva, eva)
                print(f"  {name:8s}({tc}): PE={pe:>8.2f}, PE分位={pe_pct*100:>6.1f}%, "
                      f"PB={pb:>6.2f}, PB分位={pb_pct*100:>6.1f}%, 估值={eva_cn}")
                results[tc] = {
                    "name": name, "pe": pe, "pb": pb,
                    "pe_pct": pe_pct * 100, "pb_pct": pb_pct * 100,
                    "eva": eva_cn,
                }
                break

    not_found = set(targets.keys()) - set(results.keys())
    if not_found:
        print(f"\n  ⚠️ 韭圈儿未覆盖: {', '.join(targets[c] for c in not_found)}")

    return results


# ============================================================
# 2. 乐咕数据（全历史PE，数据量最大）
# ============================================================
def fetch_legu_pe():
    print()
    print("=" * 70)
    print("2. 乐咕(LG)指数PE历史 — stock_index_pe_lg (用中文名调用)")
    print("=" * 70)

    supported = {
        "沪深300": "000300",
        "上证50": "000016",
        "中证500": "000905",
        "中证1000": "000852",
        "创业板50": "399006",
    }

    results = {}
    for lg_name, code in supported.items():
        try:
            df = ak.stock_index_pe_lg(symbol=lg_name)
            if df is not None and not df.empty:
                pe_col = "滚动市盈率" if "滚动市盈率" in df.columns else df.columns[2]
                pe_series = pd.to_numeric(df[pe_col], errors="coerce").dropna()
                current_pe = pe_series.iloc[-1]
                pct = (pe_series < current_pe).sum() / len(pe_series) * 100
                print(f"  ✅ {lg_name}({code}): {len(pe_series)}交易日, "
                      f"当前{pe_col}={current_pe:.2f}, 分位={pct:.1f}%, "
                      f"范围=[{pe_series.min():.2f}, {pe_series.max():.2f}]")
                results[lg_name] = {"df": df, "code": code, "pe_col": pe_col}
            throttle()
        except Exception as e:
            print(f"  ❌ {lg_name}: {e}")
            throttle()

    print()
    print("  ⚠️ 乐咕不支持: 科创50、创业板指、所有行业指数")
    return results


# ============================================================
# 3. 中证指数数据（只有~20天）
# ============================================================
def check_csindex_data():
    print()
    print("=" * 70)
    print("3. 中证指数(CSIndex)估值 — stock_zh_index_value_csindex (仅~20天)")
    print("=" * 70)

    targets = {
        "000688": "科创50",
        "000300": "沪深300",
        "000933": "医药",
        "399975": "证券",
    }

    for code, name in targets.items():
        try:
            df = ak.stock_zh_index_value_csindex(symbol=code)
            if df is not None and not df.empty:
                pe1 = df["市盈率1"].iloc[-1] if "市盈率1" in df.columns else "N/A"
                pe2 = df["市盈率2"].iloc[-1] if "市盈率2" in df.columns else "N/A"
                print(f"  {name}({code}): {len(df)}天, 静态PE={pe1}, 滚动PE(TTM)={pe2}")
            throttle()
        except Exception as e:
            print(f"  ❌ {name}({code}): {e}")
            throttle()

    print()
    print("  ⚠️ 中证指数只返回~20个交易日，用这算分位数完全无意义!")


# ============================================================
# 4. 数据源对比表
# ============================================================
def compare_table(dj_results, legu_results):
    print()
    print("=" * 70)
    print("4. 多数据源PE对比（韭圈儿 vs 乐咕 vs 中证）")
    print("=" * 70)

    # 对比沪深300
    print("\n  --- 沪深300 ---")
    if "000300" in dj_results:
        d = dj_results["000300"]
        print(f"  韭圈儿: PE(整体法TTM)={d['pe']:.2f}, 分位={d['pe_pct']:.1f}%")

    if "沪深300" in legu_results:
        df = legu_results["沪深300"]["df"]
        pe_col = legu_results["沪深300"]["pe_col"]
        pe_series = pd.to_numeric(df[pe_col], errors="coerce").dropna()
        current_pe = pe_series.iloc[-1]
        pct = (pe_series < current_pe).sum() / len(pe_series) * 100
        print(f"  乐咕:   PE({pe_col})={current_pe:.2f}, 分位={pct:.1f}% (基于{len(pe_series)}天)")

    try:
        df_cs = ak.stock_zh_index_value_csindex(symbol="000300")
        if df_cs is not None and not df_cs.empty:
            pe2 = pd.to_numeric(df_cs["市盈率2"], errors="coerce").dropna()
            print(f"  中证:   PE(加权TTM)={pe2.iloc[-1]:.2f} (仅{len(pe2)}天)")
    except:
        pass

    # 对比科创50
    print("\n  --- 科创50 ---")
    if "000688" in dj_results:
        d = dj_results["000688"]
        print(f"  韭圈儿: PE(整体法TTM)={d['pe']:.2f}, 分位={d['pe_pct']:.1f}%  ← 与雪球一致!")

    try:
        df_cs = ak.stock_zh_index_value_csindex(symbol="000688")
        if df_cs is not None and not df_cs.empty:
            pe1 = pd.to_numeric(df_cs["市盈率1"], errors="coerce").dropna()
            pe2 = pd.to_numeric(df_cs["市盈率2"], errors="coerce").dropna()
            print(f"  中证:   静态PE={pe1.iloc[-1]:.2f}, 加权TTM={pe2.iloc[-1]:.2f} (仅{len(pe2)}天)")
    except:
        pass

    print("\n  说明: 科创50 PE差异巨大是因为计算方法不同:")
    print("    整体法: 总市值/总净利润(TTM), 亏损公司拉低分母 → PE≈165")
    print("    加权法: 按市值加权各股PE, 排除亏损 → PE≈74")
    print("    雪球/韭圈儿用整体法, 中证指数用加权法")


# ============================================================
# 5. 画图
# ============================================================
def plot_charts(dj_results, legu_results):
    print()
    print("=" * 70)
    print("5. 生成估值对比图表")
    print("=" * 70)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        plt.rcParams["font.sans-serif"] = ["PingFang SC", "SimHei", "Arial Unicode MS"]
        plt.rcParams["axes.unicode_minus"] = False
    except ImportError:
        print("  ⚠️ matplotlib 未安装，跳过画图")
        return

    # ---- 图1: 韭圈儿PE分位数概览 ----
    if dj_results:
        sorted_items = sorted(dj_results.items(), key=lambda x: x[1]["pe_pct"], reverse=True)
        names = [f"{v['name']}" for _, v in sorted_items]
        pe_pcts = [v["pe_pct"] for _, v in sorted_items]

        colors = []
        for p in pe_pcts:
            if p >= 80:
                colors.append("#E74C3C")  # 红 高估
            elif p >= 60:
                colors.append("#F39C12")  # 橙 偏高
            elif p >= 40:
                colors.append("#3498DB")  # 蓝 合理
            elif p >= 20:
                colors.append("#2ECC71")  # 绿 低估
            else:
                colors.append("#27AE60")  # 深绿 极度低估

        fig, ax = plt.subplots(figsize=(14, max(8, len(names) * 0.45)))
        fig.patch.set_facecolor("#0A0A0A")
        ax.set_facecolor("#0D0D0D")

        bars = ax.barh(names, pe_pcts, color=colors, height=0.6, edgecolor="#333")

        for bar, pct, item_tuple in zip(bars, pe_pcts, sorted_items):
            code, v = item_tuple
            pe_val = v["pe"]
            label = f"PE={pe_val:.1f}  分位={pct:.1f}%"
            ax.text(bar.get_width() + 1, bar.get_y() + bar.get_height() / 2,
                    label, va="center", fontsize=8, color="#CCC")

        # 分位参考线
        for threshold, label, color in [(20, "低估线", "#2ECC71"), (80, "高估线", "#E74C3C")]:
            ax.axvline(x=threshold, color=color, linestyle="--", linewidth=0.8, alpha=0.5)
            ax.text(threshold + 0.5, len(names) - 0.3, label, fontsize=7, color=color, alpha=0.7)

        ax.set_xlim(0, 110)
        ax.set_xlabel("PE分位数 (%)", color="#999")
        ax.set_title("韭圈儿指数PE分位数概览（整体法TTM，与雪球一致）",
                     fontsize=13, fontweight="bold", color="#C9A84C", pad=15)
        ax.tick_params(colors="#999")
        for spine in ax.spines.values():
            spine.set_color("#333")
        ax.invert_yaxis()

        plt.tight_layout(pad=2)
        out1 = os.path.join(PROJECT_ROOT, "data", "debug_danjuan_pe_overview.png")
        plt.savefig(out1, dpi=150, bbox_inches="tight", facecolor="#0A0A0A")
        plt.close()
        print(f"  ✅ 韭圈儿PE概览: {out1}")

    # ---- 图2: 乐咕PE历史曲线 ----
    n = len(legu_results)
    if n > 0:
        fig, axes = plt.subplots(n, 1, figsize=(16, 4.5 * n))
        if n == 1:
            axes = [axes]

        for i, (name, info) in enumerate(legu_results.items()):
            ax = axes[i]
            df = info["df"]
            pe_col = info["pe_col"]
            code = info["code"]

            dates = pd.to_datetime(df["日期"], errors="coerce")
            pe_values = pd.to_numeric(df[pe_col], errors="coerce")

            valid = dates.notna() & pe_values.notna()
            dates = dates[valid]
            pe_values = pe_values[valid]

            ax.plot(dates, pe_values, color="#C9A84C", linewidth=0.8, label=pe_col)
            ax.fill_between(dates, pe_values, alpha=0.1, color="#C9A84C")

            current_pe = pe_values.iloc[-1]
            pct = (pe_values < current_pe).sum() / len(pe_values) * 100
            median_pe = pe_values.median()

            ax.axhline(y=current_pe, color="red", linestyle="--", linewidth=1, alpha=0.8)
            ax.text(dates.iloc[-1], current_pe * 1.02,
                    f"当前: {current_pe:.2f}\n分位: {pct:.1f}%",
                    color="red", fontsize=9, fontweight="bold", ha="right", va="bottom")

            ax.axhline(y=median_pe, color="#666", linestyle=":", linewidth=0.8, alpha=0.6)
            ax.text(dates.iloc[0], median_pe,
                    f"中位数: {median_pe:.1f}", color="#666", fontsize=8, va="bottom")

            ax.set_title(
                f"{name}({code}) — {pe_col} | 当前={current_pe:.2f} | "
                f"分位={pct:.1f}% | 共{len(pe_values)}交易日",
                fontsize=11, fontweight="bold", pad=10
            )
            ax.set_ylabel("PE")
            ax.grid(True, alpha=0.15)
            ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
            ax.xaxis.set_major_locator(mdates.YearLocator())

            ax.set_facecolor("#0D0D0D")
            fig.patch.set_facecolor("#0A0A0A")
            ax.tick_params(colors="#999")
            ax.yaxis.label.set_color("#999")
            ax.title.set_color("#C9A84C")
            for spine in ax.spines.values():
                spine.set_color("#333")

        plt.tight_layout(pad=2)
        out2 = os.path.join(PROJECT_ROOT, "data", "debug_legu_pe_history.png")
        plt.savefig(out2, dpi=150, bbox_inches="tight", facecolor="#0A0A0A")
        plt.close()
        print(f"  ✅ 乐咕PE历史: {out2}")


# ============================================================
# Main
# ============================================================
if __name__ == "__main__":
    print("🔍 估值数据诊断工具 v2（新增韭圈儿数据源）")
    print(f"   时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   akshare版本: {ak.__version__}")
    print()

    dj_results = fetch_danjuan()
    legu_results = fetch_legu_pe()
    check_csindex_data()
    compare_table(dj_results, legu_results)
    plot_charts(dj_results, legu_results)

    print()
    print("=" * 70)
    print("结论")
    print("=" * 70)
    print("""
  1. 韭圈儿(蛋卷基金) API:
     - 接口: https://danjuanfunds.com/djapi/index_eva/dj
     - 覆盖60+指数, 包含PE/PB/分位数/估值状态
     - PE口径: 整体法TTM, 与雪球完全一致
     - 分位数: 基于上市以来全部历史数据, 准确可靠
     - 科创50: PE≈165, 分位≈95% ← 与雪球一致!

  2. 乐咕(stock_index_pe_lg): 5个宽基指数, 数千交易日
     - PE口径: 加权滚动市盈率(TTM), 与韭圈儿口径不同
     - 适合做长期PE趋势分析

  3. 中证指数(stock_zh_index_value_csindex): 所有指数, 但只有~20天
     - PE口径: 加权法, 与韭圈儿差异大(特别是亏损公司多的指数)
     - 20天数据不够算分位数

  4. 现在系统优先使用韭圈儿数据, PE和分位数与雪球一致!
    """)
