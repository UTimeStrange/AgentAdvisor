"""
一次性离线下载全部数据脚本
使用方式:
    conda activate agent_advisor
    python scripts/download_all_data.py

数据源:
    - 韭圈儿(蛋卷基金): 指数估值(整体法TTM PE/PB/分位数)
    - 腾讯财经: ETF实时行情、ETF/指数历史K线(免费、不限频)
    - 中证指数(akshare): 指数估值横截面+时序
    - 乐咕(akshare): 宽基指数历史PE/PB(数千交易日)
    - akshare: QVIX恐慌指数、国债收益率、市场整体PE
    - 对于腾讯不覆盖的6个中证自编指数(93xxxx/99xxxx)，回退akshare
"""

import os
import sys
import time
import datetime
import logging
import traceback
import json
from typing import List, Optional

import pandas as pd
import yaml
import requests

# 添加项目根目录到 path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("download_all_data")

# 请求间隔(秒)，akshare回退时使用
REQUEST_INTERVAL = 2.0

# 腾讯不覆盖的中证自编指数 -> 用对应ETF代码获取行情
QQ_INDEX_ETF_PROXY = {
    "931151": "515790",  # 光伏指数 -> 光伏ETF
    "990001": "512480",  # 半导体指数 -> 半导体ETF
    "930901": "159869",  # 游戏指数 -> 游戏ETF
    "930851": "516510",  # 云计算指数 -> 云计算ETF
    "930633": "159766",  # 旅游指数 -> 旅游ETF
    "931590": "562500",  # 机器人指数 -> 机器人ETF
}


def throttle():
    time.sleep(REQUEST_INTERVAL)


def load_config():
    config_path = os.path.join(PROJECT_ROOT, "config.yaml")
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_dirs(base_dir):
    dirs = [
        os.path.join(base_dir, "cross_section"),
        os.path.join(base_dir, "time_series"),
    ]
    for d in dirs:
        os.makedirs(d, exist_ok=True)
    return dirs[0], dirs[1]


def save_csv(df: pd.DataFrame, filepath: str, desc: str = ""):
    df.to_csv(filepath, index=False, encoding="utf-8-sig")
    logger.info(f"已保存: {filepath} ({len(df)} 行) {desc}")


# ================================================================
# 腾讯财经 API 工具函数
# ================================================================

def qq_code_prefix(code: str) -> str:
    """根据代码判断腾讯API所需的sh/sz前缀
    ETF: 5xxxxx/6xxxxx -> sh, 1xxxxx -> sz
    指数: 000xxx -> sh(上证), 399xxx -> sz(深证), 93xxxx/99xxxx -> sh
    """
    if code.startswith("000") and len(code) == 6:
        return "sh"  # 上证指数
    if code.startswith(("5", "6", "9")):
        return "sh"
    return "sz"


def fetch_qq_kline(code: str, prefix: str = None, days: int = 800) -> Optional[pd.DataFrame]:
    """从腾讯财经获取日K线数据"""
    if prefix is None:
        prefix = qq_code_prefix(code)
    symbol = f"{prefix}{code}"
    start = (datetime.datetime.now() - datetime.timedelta(days=365 * 3)).strftime("%Y-%m-%d")
    url = f"http://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,{start},,{days},qfq"
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
        kdata = data.get("data", {}).get(symbol, {})
        day_list = kdata.get("day", []) or kdata.get("qfqday", [])
        if not day_list:
            return None
        rows = []
        for d in day_list:
            if len(d) >= 6:
                rows.append({
                    "日期": d[0],
                    "开盘": float(d[1]),
                    "收盘": float(d[2]),
                    "最高": float(d[3]),
                    "最低": float(d[4]),
                    "成交量": int(float(d[5])) if d[5] else 0,
                })
        return pd.DataFrame(rows) if rows else None
    except Exception as e:
        logger.warning(f"腾讯K线获取失败 {symbol}: {e}")
        return None


def fetch_qq_realtime(codes: List[str]) -> Optional[pd.DataFrame]:
    """从腾讯财经获取实时行情，支持批量查询"""
    import urllib.request
    symbols = ",".join(f"{qq_code_prefix(c)}{c}" for c in codes)
    url = f"http://qt.gtimg.cn/q={symbols}"
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        content = urllib.request.urlopen(req, timeout=15).read().decode("gbk")
        rows = []
        for line in content.strip().split("\n"):
            parts = line.split("~")
            if len(parts) < 50:
                continue
            rows.append({
                "名称": parts[1],
                "代码": parts[2],
                "最新价": float(parts[3]) if parts[3] else 0,
                "昨收": float(parts[4]) if parts[4] else 0,
                "今开": float(parts[5]) if parts[5] else 0,
                "成交量": int(parts[6]) if parts[6] else 0,
                "涨跌": float(parts[31]) if parts[31] else 0,
                "涨跌幅": float(parts[32]) if parts[32] else 0,
                "最高": float(parts[33]) if parts[33] else 0,
                "最低": float(parts[34]) if parts[34] else 0,
                "成交额": float(parts[37]) if parts[37] else 0,
                "换手率": float(parts[38]) if parts[38] else 0,
            })
        return pd.DataFrame(rows) if rows else None
    except Exception as e:
        logger.warning(f"腾讯实时行情获取失败: {e}")
        return None


def ak_call_with_retry(func, *args, max_retries=2, **kwargs):
    """带重试的akshare调用"""
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except Exception as e:
            err_str = str(e)
            if ("Connection aborted" in err_str or "RemoteDisconnected" in err_str) and attempt < max_retries:
                wait = 15 * (attempt + 1)
                logger.warning(f"东方财富限频，等待{wait}秒后重试 (第{attempt+1}次)...")
                time.sleep(wait)
                continue
            raise


# ================================================================
# 1. 韭圈儿(蛋卷基金)指数估值 — 整体法TTM PE
# ================================================================
def download_danjuan_valuation(cs_dir):
    """韭圈儿指数估值 — 63个指数"""
    logger.info("=" * 60)
    logger.info("开始下载: 韭圈儿指数估值 (整体法TTM)")
    logger.info("=" * 60)

    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    try:
        resp = requests.get(
            "https://danjuanfunds.com/djapi/index_eva/dj",
            headers=headers, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        items = data.get("data", {}).get("items", [])
        if not items:
            logger.warning("韭圈儿接口返回空数据")
            return 0, 1

        rows = []
        for item in items:
            code_raw = item.get("index_code", "")
            code = code_raw.replace("SH", "").replace("SZ", "")
            pe = item.get("pe", 0)
            pb = item.get("pb", 0)
            pe_pct = item.get("pe_percentile", 0)
            pb_pct = item.get("pb_percentile", 0)
            rows.append({
                "index_code": code,
                "index_name": item.get("name", ""),
                "pe": pe,
                "pb": pb,
                "pe_percentile": round(pe_pct * 100, 2),
                "pb_percentile": round(pb_pct * 100, 2),
                "eva_type": item.get("eva_type", ""),
                "roe": item.get("roe", 0),
                "dividend_yield": item.get("yeild", 0),
                "peg": item.get("peg", 0),
                "source": "danjuan",
            })

        if rows:
            df = pd.DataFrame(rows)
            save_csv(df, os.path.join(cs_dir, "danjuan_valuation.csv"),
                     f"[韭圈儿估值 {len(rows)}个指数]")
            return 1, 0

    except Exception as e:
        logger.warning(f"韭圈儿估值下载失败: {e}")
    return 0, 1


# ================================================================
# 2. 中证指数估值 (akshare)
# ================================================================
def download_index_valuation_csindex(ak, etf_pool, cs_dir, ts_dir):
    """中证指数官方估值数据"""
    logger.info("=" * 60)
    logger.info("开始下载: 中证指数估值数据")
    logger.info("=" * 60)

    domestic_indices = []
    for category in ["broad_base", "sector"]:
        for item in etf_pool.get(category, []):
            idx_code = item.get("index_code", "")
            if idx_code:
                domestic_indices.append({
                    "index_code": idx_code, "name": item["name"], "etf_code": item["code"],
                })

    all_items = []
    success_count = 0
    fail_count = 0

    for idx_info in domestic_indices:
        idx_code = idx_info["index_code"]
        try:
            logger.info(f"  拉取: {idx_info['name']} ({idx_code})")
            df = ak.stock_zh_index_value_csindex(symbol=idx_code)
            if df is not None and not df.empty:
                latest = df.iloc[-1].to_dict()
                latest["index_code"] = idx_code
                latest["index_name"] = idx_info["name"]
                latest["etf_code"] = idx_info["etf_code"]
                all_items.append(latest)
                if len(df) > 1:
                    df_ts = df.copy()
                    df_ts["index_code"] = idx_code
                    df_ts["index_name"] = idx_info["name"]
                    save_csv(df_ts, os.path.join(ts_dir, f"valuation_csindex_{idx_code}.csv"),
                             f"[{idx_info['name']}估值时序]")
                success_count += 1
            throttle()
        except Exception as e:
            logger.warning(f"  失败: {idx_code} - {e}")
            fail_count += 1
            throttle()

    if all_items:
        df_result = pd.DataFrame(all_items)
        save_csv(df_result, os.path.join(cs_dir, "index_valuation_csindex.csv"),
                 "[中证指数估值横截面]")

    logger.info(f"中证指数估值: 成功 {success_count}, 失败 {fail_count}")
    return success_count, fail_count


# ================================================================
# 3. ETF 实时行情 (腾讯财经)
# ================================================================
def download_etf_realtime(etf_pool, cs_dir):
    """ETF实时行情 — 腾讯财经"""
    logger.info("=" * 60)
    logger.info("开始下载: ETF实时行情 (腾讯财经)")
    logger.info("=" * 60)

    all_codes = []
    for category in etf_pool:
        for item in etf_pool[category]:
            all_codes.append(item["code"])

    try:
        df = fetch_qq_realtime(all_codes)
        if df is not None and not df.empty:
            save_csv(df, os.path.join(cs_dir, "etf_realtime.csv"),
                     f"[ETF实时行情 {len(df)}只]")
            return 1, 0
    except Exception as e:
        logger.warning(f"ETF实时行情失败: {e}")
    return 0, 1


# ================================================================
# 4. 指数历史PE (乐咕)
# ================================================================
def download_index_pe_history(ak, ts_dir):
    """乐咕 — 宽基指数历史PE"""
    logger.info("=" * 60)
    logger.info("开始下载: 指数历史PE (乐咕)")
    logger.info("=" * 60)

    pe_index_map = {
        "沪深300": "000300",
        "中证500": "000905",
        "中证1000": "000852",
        "上证50": "000016",
        "创业板50": "399006",
    }

    success_count = 0
    fail_count = 0

    for lg_name, idx_code in pe_index_map.items():
        try:
            logger.info(f"  拉取: {lg_name} ({idx_code})")
            df = ak.stock_index_pe_lg(symbol=lg_name)
            if df is not None and not df.empty:
                df["index_code"] = idx_code
                df["index_name"] = lg_name
                save_csv(df, os.path.join(ts_dir, f"pe_history_{idx_code}.csv"),
                         f"[{lg_name} PE历史 {len(df)}天]")
                success_count += 1
            throttle()
        except Exception as e:
            logger.warning(f"  失败: {lg_name} - {e}")
            fail_count += 1
            throttle()

    logger.info(f"指数PE历史: 成功 {success_count}, 失败 {fail_count}")
    return success_count, fail_count


# ================================================================
# 5. 指数历史PB (乐咕)
# ================================================================
def download_index_pb_history(ak, ts_dir):
    """乐咕 — 宽基指数历史PB"""
    logger.info("=" * 60)
    logger.info("开始下载: 指数历史PB (乐咕)")
    logger.info("=" * 60)

    pb_index_map = {
        "沪深300": "000300",
        "中证500": "000905",
        "中证1000": "000852",
        "上证50": "000016",
        "创业板50": "399006",
    }

    success_count = 0
    fail_count = 0

    for lg_name, idx_code in pb_index_map.items():
        try:
            logger.info(f"  拉取: {lg_name} ({idx_code})")
            df = ak.stock_index_pb_lg(symbol=lg_name)
            if df is not None and not df.empty:
                df["index_code"] = idx_code
                df["index_name"] = lg_name
                save_csv(df, os.path.join(ts_dir, f"pb_history_{idx_code}.csv"),
                         f"[{lg_name} PB历史 {len(df)}天]")
                success_count += 1
            throttle()
        except Exception as e:
            logger.warning(f"  失败: {lg_name} - {e}")
            fail_count += 1
            throttle()

    logger.info(f"指数PB历史: 成功 {success_count}, 失败 {fail_count}")
    return success_count, fail_count


# ================================================================
# 6. 指数历史行情 (腾讯财经 + akshare回退)
# ================================================================
def download_index_price_history(ak, etf_pool, ts_dir):
    """指数近3年日K — 腾讯财经，6个自编指数用ETF行情代理"""
    logger.info("=" * 60)
    logger.info("开始下载: 指数历史行情 (腾讯财经)")
    logger.info("=" * 60)

    domestic_indices = []
    for category in ["broad_base", "sector"]:
        for item in etf_pool.get(category, []):
            idx_code = item.get("index_code", "")
            if idx_code:
                domestic_indices.append({"index_code": idx_code, "name": item["name"]})

    success_count = 0
    fail_count = 0

    for idx_info in domestic_indices:
        idx_code = idx_info["index_code"]
        try:
            if idx_code in QQ_INDEX_ETF_PROXY:
                # 用对应ETF行情代理
                etf_code = QQ_INDEX_ETF_PROXY[idx_code]
                logger.info(f"  拉取(ETF代理): {idx_info['name']} ({idx_code}) -> ETF {etf_code}")
                df = fetch_qq_kline(etf_code)
            else:
                # 腾讯财经
                logger.info(f"  拉取(腾讯): {idx_info['name']} ({idx_code})")
                df = fetch_qq_kline(idx_code)

            if df is not None and not df.empty:
                df["index_code"] = idx_code
                df["index_name"] = idx_info["name"]
                save_csv(df, os.path.join(ts_dir, f"price_history_{idx_code}.csv"),
                         f"[{idx_info['name']} 行情]")
                success_count += 1
            else:
                logger.warning(f"  无数据: {idx_code}")
                fail_count += 1
        except Exception as e:
            logger.warning(f"  失败: {idx_code} - {e}")
            fail_count += 1

    logger.info(f"指数行情: 成功 {success_count}, 失败 {fail_count}")
    return success_count, fail_count


# ================================================================
# 7. ETF历史行情 (腾讯财经)
# ================================================================
def download_etf_price_history(etf_pool, ts_dir):
    """各ETF近3年日K (前复权) — 腾讯财经"""
    logger.info("=" * 60)
    logger.info("开始下载: ETF历史行情 (腾讯财经)")
    logger.info("=" * 60)

    all_etfs = []
    for category in etf_pool:
        for item in etf_pool[category]:
            all_etfs.append({
                "code": item["code"], "name": item["name"], "category": category,
            })

    success_count = 0
    fail_count = 0

    for etf in all_etfs:
        try:
            logger.info(f"  拉取(腾讯): {etf['name']} ({etf['code']})")
            df = fetch_qq_kline(etf["code"])
            if df is not None and not df.empty:
                df["etf_code"] = etf["code"]
                df["etf_name"] = etf["name"]
                df["category"] = etf["category"]
                save_csv(df, os.path.join(ts_dir, f"etf_history_{etf['code']}.csv"),
                         f"[{etf['name']} ETF行情(腾讯)]")
                success_count += 1
            else:
                logger.warning(f"  腾讯无数据: {etf['code']}")
                fail_count += 1
        except Exception as e:
            logger.warning(f"  失败: {etf['code']} - {e}")
            fail_count += 1

    logger.info(f"ETF行情: 成功 {success_count}, 失败 {fail_count}")
    return success_count, fail_count


# ================================================================
# 8. QVIX 恐慌指数
# ================================================================
def download_qvix(ak, ts_dir):
    """50ETF期权波动率指数(QVIX)"""
    logger.info("=" * 60)
    logger.info("开始下载: QVIX 恐慌指数")
    logger.info("=" * 60)

    try:
        df = ak.index_option_50etf_qvix()
        if df is not None and not df.empty:
            save_csv(df, os.path.join(ts_dir, "qvix.csv"), "[QVIX恐慌指数]")
            return 1, 0
    except Exception as e:
        logger.warning(f"  失败: {e}")
    return 0, 1


# ================================================================
# 9. 市场整体PE
# ================================================================
def download_market_overview(ak, ts_dir):
    """市场整体PE"""
    logger.info("=" * 60)
    logger.info("开始下载: 市场整体PE")
    logger.info("=" * 60)

    success_count = 0
    fail_count = 0

    for market, label in [("sh", "上证"), ("sz", "深证")]:
        try:
            logger.info(f"  拉取: {label}整体PE")
            df = ak.stock_a_pe(market=market)
            if df is not None and not df.empty:
                save_csv(df, os.path.join(ts_dir, f"market_pe_{market}.csv"), f"[{label}整体PE]")
                success_count += 1
            throttle()
        except Exception as e:
            logger.warning(f"  失败 ({label}PE): {e}")
            fail_count += 1
            throttle()

    logger.info(f"市场PE: 成功 {success_count}, 失败 {fail_count}")
    return success_count, fail_count


# ================================================================
# 10. 国债收益率
# ================================================================
def download_bond_yield(ak, ts_dir):
    """中国国债收益率"""
    logger.info("=" * 60)
    logger.info("开始下载: 国债收益率")
    logger.info("=" * 60)

    try:
        df = ak.bond_china_yield(start_date="20200101")
        if df is not None and not df.empty:
            save_csv(df, os.path.join(ts_dir, "bond_china_yield.csv"), "[国债收益率]")
            return 1, 0
    except Exception as e:
        logger.warning(f"  失败: {e}")
    return 0, 1


# ================================================================
# Main
# ================================================================
def main():
    logger.info("=" * 60)
    logger.info("AI投顾系统 - 离线数据下载 (腾讯财经+akshare)")
    logger.info(f"时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logger.info("=" * 60)

    config = load_config()
    etf_pool = config.get("etf_pool", {})
    data_dir = os.path.join(PROJECT_ROOT, config.get("data", {}).get("cache_dir", "data"))
    cs_dir, ts_dir = ensure_dirs(data_dir)

    # 导入 akshare (乐咕/中证/QVIX等仍需要)
    logger.info("导入 akshare ...")
    import akshare as ak
    logger.info(f"akshare 版本: {ak.__version__}")

    total_success = 0
    total_fail = 0
    start_time = time.time()

    tasks = [
        ("韭圈儿估值", lambda: download_danjuan_valuation(cs_dir)),
        ("中证指数估值", lambda: download_index_valuation_csindex(ak, etf_pool, cs_dir, ts_dir)),
        ("ETF实时行情(腾讯)", lambda: download_etf_realtime(etf_pool, cs_dir)),
        ("指数PE历史(乐咕)", lambda: download_index_pe_history(ak, ts_dir)),
        ("指数PB历史(乐咕)", lambda: download_index_pb_history(ak, ts_dir)),
        ("指数行情(腾讯)", lambda: download_index_price_history(ak, etf_pool, ts_dir)),
        ("ETF行情(腾讯)", lambda: download_etf_price_history(etf_pool, ts_dir)),
        ("QVIX恐慌指数", lambda: download_qvix(ak, ts_dir)),
        ("市场整体PE", lambda: download_market_overview(ak, ts_dir)),
        ("国债收益率", lambda: download_bond_yield(ak, ts_dir)),
    ]

    for task_name, task_func in tasks:
        try:
            result = task_func()
            if result and isinstance(result, tuple):
                total_success += result[0]
                total_fail += result[1]
        except Exception as e:
            logger.error(f"任务 [{task_name}] 整体失败: {e}")
            traceback.print_exc()
            total_fail += 1

    elapsed = time.time() - start_time

    # 统计文件
    csv_count = 0
    total_size = 0
    for root, dirs, files in os.walk(data_dir):
        for f in files:
            if f.endswith(".csv"):
                csv_count += 1
                total_size += os.path.getsize(os.path.join(root, f))

    logger.info("")
    logger.info("=" * 60)
    logger.info("下载完成!")
    logger.info(f"  耗时: {elapsed:.1f} 秒 ({elapsed / 60:.1f} 分钟)")
    logger.info(f"  成功请求: {total_success}")
    logger.info(f"  失败请求: {total_fail}")
    logger.info(f"  CSV文件数: {csv_count}")
    logger.info(f"  总数据量: {total_size / 1024 / 1024:.2f} MB")
    logger.info(f"  存储目录: {data_dir}")
    logger.info("=" * 60)

    # 列出所有文件
    logger.info("\n已下载的文件列表:")
    for root, dirs, files in os.walk(data_dir):
        level = root.replace(data_dir, "").count(os.sep)
        indent = "  " * level
        folder = os.path.basename(root)
        logger.info(f"{indent}{folder}/")
        for f in sorted(files):
            if f.endswith(".csv"):
                fpath = os.path.join(root, f)
                fsize = os.path.getsize(fpath) / 1024
                logger.info(f"{indent}  {f} ({fsize:.1f} KB)")


if __name__ == "__main__":
    main()
