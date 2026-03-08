import os
import time
import logging
import datetime
import json
from typing import Dict, Any, List, Optional

import pandas as pd
import requests

logger = logging.getLogger("data_service")

# akshare 延迟导入，避免启动时慢
_ak = None

def _get_ak():
    global _ak
    if _ak is None:
        import akshare as ak
        _ak = ak
    return _ak


class DataService:
    """数据采集与缓存服务 - akshare + 本地CSV"""

    REQUEST_INTERVAL = 2.0  # 请求间隔(秒)，防止限频

    def __init__(self, data_config: Dict, etf_pool: Dict, base_dir: str):
        self.config = data_config
        self.etf_pool = etf_pool
        self.base_dir = base_dir

        self.cache_dir = os.path.join(base_dir, data_config.get("cache_dir", "data"))
        self.cross_section_dir = os.path.join(self.cache_dir, "cross_section")
        self.time_series_dir = os.path.join(self.cache_dir, "time_series")

        self.cs_expire_days = data_config.get("cross_section_expire_days", 1)
        self.ts_expire_days = data_config.get("time_series_expire_days", 1)

        for d in [self.cache_dir, self.cross_section_dir, self.time_series_dir]:
            os.makedirs(d, exist_ok=True)

    # ================================================================
    # 缓存管理
    # ================================================================

    def _cache_path(self, subdir: str, filename: str) -> str:
        return os.path.join(self.cache_dir, subdir, filename)

    def _is_cache_fresh(self, filepath: str, expire_days: int = 1) -> bool:
        if not os.path.exists(filepath):
            return False
        mtime = os.path.getmtime(filepath)
        age = (time.time() - mtime) / 86400
        return age < expire_days

    def _save_csv(self, df: pd.DataFrame, subdir: str, filename: str):
        path = self._cache_path(subdir, filename)
        df.to_csv(path, index=False, encoding="utf-8-sig")
        logger.info(f"已保存: {path} ({len(df)} 行)")

    def _load_csv(self, subdir: str, filename: str) -> Optional[pd.DataFrame]:
        path = self._cache_path(subdir, filename)
        if os.path.exists(path):
            return pd.read_csv(path, encoding="utf-8-sig")
        return None

    def _throttle(self):
        time.sleep(self.REQUEST_INTERVAL)

    def _ak_call_with_retry(self, func, *args, max_retries=2, **kwargs):
        """带重试的akshare调用，遇到限频自动等待重试"""
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
    # 腾讯财经API工具方法 — 免费、不限频、不需要注册
    # ================================================================

    @staticmethod
    def _qq_code_prefix(code: str) -> str:
        """根据代码判断腾讯API所需的sh/sz前缀
        ETF: 5xxxxx/6xxxxx -> sh, 1xxxxx -> sz
        指数: 000xxx -> sh(上证), 399xxx -> sz(深证), 93xxxx/99xxxx -> sh
        """
        if code.startswith("000") and len(code) == 6:
            return "sh"  # 上证指数: 000300沪深300, 000016上证50等
        if code.startswith(("5", "6", "9")):
            return "sh"
        return "sz"

    def _fetch_qq_kline(self, code: str, prefix: str = None, days: int = 800) -> Optional[pd.DataFrame]:
        """从腾讯财经获取日K线数据，返回DataFrame[date,open,close,high,low,volume]"""
        if prefix is None:
            prefix = self._qq_code_prefix(code)
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
            # 每条: [date, open, close, high, low, volume, ...]
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

    def _fetch_qq_realtime(self, codes: List[str]) -> Optional[pd.DataFrame]:
        """从腾讯财经获取实时行情，支持批量查询"""
        import urllib.request
        symbols = ",".join(f"{self._qq_code_prefix(c)}{c}" for c in codes)
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

    def get_cache_status(self) -> Dict:
        status = {"cross_section": {}, "time_series": {}}
        for subdir, category in [("cross_section", "cross_section"), ("time_series", "time_series")]:
            dirpath = os.path.join(self.cache_dir, subdir)
            if os.path.exists(dirpath):
                for f in os.listdir(dirpath):
                    if f.endswith(".csv"):
                        fp = os.path.join(dirpath, f)
                        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(fp))
                        status[category][f] = mtime.strftime("%Y-%m-%d %H:%M:%S")
        return status

    # ================================================================
    # 数据新鲜度检查
    # ================================================================

    def check_data_ready(self) -> dict:
        """快速预检：返回数据状态摘要 {total, fresh, stale, missing}"""
        critical_files = [
            ("cross_section", "danjuan_valuation.csv", self.cs_expire_days),
            ("cross_section", "etf_realtime.csv", self.cs_expire_days),
        ]
        # ETF 行情文件
        for category in self.etf_pool:
            for item in self.etf_pool[category]:
                critical_files.append(("time_series", f"etf_history_{item['code']}.csv", self.ts_expire_days))
        # 指数行情文件
        for cat in ["broad_base", "sector"]:
            for item in self.etf_pool.get(cat, []):
                idx = item.get("index_code", "")
                if idx:
                    critical_files.append(("time_series", f"price_history_{idx}.csv", self.ts_expire_days))

        result = {"total": len(critical_files), "fresh": 0, "stale": 0, "missing": 0}
        for subdir, filename, expire in critical_files:
            fp = self._cache_path(subdir, filename)
            if not os.path.exists(fp):
                result["missing"] += 1
            elif not self._is_cache_fresh(fp, expire):
                result["stale"] += 1
            else:
                result["fresh"] += 1
        return result

    def ensure_fresh_data(self):
        """确保所有数据是最新的，过期或缺失则重新拉取"""
        status = self.check_data_ready()
        logger.info(
            f"数据预检: 共{status['total']}项, "
            f"最新{status['fresh']}项, 过期{status['stale']}项, 缺失{status['missing']}项"
        )
        if status["stale"] == 0 and status["missing"] == 0:
            logger.info("所有数据均为最新，无需更新")
            return

        logger.info("开始更新过期/缺失数据...")
        self._fetch_danjuan_valuation()
        self._fetch_index_valuation_csindex()
        self._fetch_etf_realtime()
        self._fetch_index_pe_history()
        self._fetch_index_pb_history()
        self._fetch_index_price_history()
        self._fetch_etf_price_history()
        self._fetch_qvix()
        logger.info("数据更新完成")

    def refresh_all(self):
        """强制刷新所有数据"""
        logger.info("强制刷新所有数据...")
        self._fetch_danjuan_valuation(force=True)
        self._fetch_index_valuation_csindex(force=True)
        self._fetch_etf_realtime(force=True)
        self._fetch_index_pe_history(force=True)
        self._fetch_index_pb_history(force=True)
        self._fetch_index_price_history(force=True)
        self._fetch_etf_price_history(force=True)
        self._fetch_qvix(force=True)
        logger.info("强制刷新完成")

    # ================================================================
    # 横截面数据：韭圈儿(蛋卷基金)指数估值 — 整体法TTM PE，与雪球一致
    # ================================================================

    def _fetch_danjuan_valuation(self, force=False):
        """获取韭圈儿指数估值数据 — PE/PB/分位数/估值状态，整体法TTM口径"""
        filepath = self._cache_path("cross_section", "danjuan_valuation.csv")
        if not force and self._is_cache_fresh(filepath, self.cs_expire_days):
            logger.info("韭圈儿估值数据缓存有效，跳过")
            return

        try:
            logger.info("拉取韭圈儿(蛋卷基金)指数估值数据...")
            headers = {
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            }
            resp = requests.get(
                "https://danjuanfunds.com/djapi/index_eva/dj",
                headers=headers, timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data", {}).get("items", [])

            if not items:
                logger.warning("韭圈儿接口返回空数据")
                return

            # 构建 index_code -> etf映射
            idx_to_etf = {}
            for category in self.etf_pool:
                for item in self.etf_pool[category]:
                    icode = item.get("index_code", "")
                    if icode:
                        idx_to_etf[icode] = {"etf_code": item["code"], "etf_name": item["name"], "category": category}

            rows = []
            for item in items:
                code_raw = item.get("index_code", "")  # e.g. "SH000688"
                code = code_raw.replace("SH", "").replace("SZ", "")
                name = item.get("name", "")
                pe = item.get("pe", 0)
                pb = item.get("pb", 0)
                pe_pct = item.get("pe_percentile", 0)  # 0~1
                pb_pct = item.get("pb_percentile", 0)
                eva_type = item.get("eva_type", "")  # low/mid/high
                roe = item.get("roe", 0)
                yeild = item.get("yeild", 0)
                peg = item.get("peg", 0)

                rows.append({
                    "index_code": code,
                    "index_name": name,
                    "pe": pe,
                    "pb": pb,
                    "pe_percentile": round(pe_pct * 100, 2),
                    "pb_percentile": round(pb_pct * 100, 2),
                    "eva_type": eva_type,
                    "roe": roe,
                    "dividend_yield": yeild,
                    "peg": peg,
                    "source": "danjuan",
                })

            if rows:
                df = pd.DataFrame(rows)
                self._save_csv(df, "cross_section", "danjuan_valuation.csv")
                logger.info(f"韭圈儿估值数据已保存: {len(rows)} 个指数")

        except Exception as e:
            logger.warning(f"拉取韭圈儿估值数据失败: {e}")

    # ================================================================
    # 横截面数据：指数估值（中证指数）
    # ================================================================

    def _fetch_index_valuation_csindex(self, force=False):
        """获取中证指数估值数据 - 横截面(最新)+ 时序(全历史)"""
        filepath = self._cache_path("cross_section", "index_valuation_csindex.csv")
        if not force and self._is_cache_fresh(filepath, self.cs_expire_days):
            logger.info("中证指数估值数据缓存有效，跳过")
            return

        ak = _get_ak()
        all_items = []

        # 获取所有需要查询的国内指数代码
        domestic_indices = []
        for category in ["broad_base", "sector"]:
            for item in self.etf_pool.get(category, []):
                idx_code = item.get("index_code", "")
                if idx_code:
                    domestic_indices.append({"index_code": idx_code, "name": item["name"], "etf_code": item["code"]})

        for idx_info in domestic_indices:
            idx_code = idx_info["index_code"]
            try:
                logger.info(f"拉取中证指数估值: {idx_info['name']} ({idx_code})")
                df = ak.stock_zh_index_value_csindex(symbol=idx_code)
                if df is not None and not df.empty:
                    latest = df.iloc[-1].to_dict()
                    latest["index_code"] = idx_code
                    latest["index_name"] = idx_info["name"]
                    latest["etf_code"] = idx_info["etf_code"]
                    all_items.append(latest)

                    # 同时保存该指数的完整估值时序（供分位数计算）
                    if len(df) > 1:
                        df_ts = df.copy()
                        df_ts["index_code"] = idx_code
                        df_ts["index_name"] = idx_info["name"]
                        self._save_csv(df_ts, "time_series", f"valuation_csindex_{idx_code}.csv")

                self._throttle()
            except Exception as e:
                logger.warning(f"拉取 {idx_code} 估值失败: {e}")
                self._throttle()

        if all_items:
            df_result = pd.DataFrame(all_items)
            self._save_csv(df_result, "cross_section", "index_valuation_csindex.csv")

    # ================================================================
    # 横截面数据：ETF实时行情
    # ================================================================

    def _fetch_etf_realtime(self, force=False):
        """获取关注ETF实时行情 — 腾讯财经(免费、不限频)"""
        filepath = self._cache_path("cross_section", "etf_realtime.csv")
        if not force and self._is_cache_fresh(filepath, self.cs_expire_days):
            logger.info("ETF实时行情缓存有效，跳过")
            return

        try:
            logger.info("拉取ETF实时行情(腾讯财经)...")
            all_codes = []
            for category in self.etf_pool:
                for item in self.etf_pool[category]:
                    all_codes.append(item["code"])

            df = self._fetch_qq_realtime(all_codes)
            if df is not None and not df.empty:
                self._save_csv(df, "cross_section", "etf_realtime.csv")
        except Exception as e:
            logger.warning(f"拉取ETF实时行情失败: {e}")

    # ================================================================
    # 时序数据：指数历史PE
    # ================================================================

    def _fetch_index_pe_history(self, force=False):
        """拉取各指数历史PE数据 (乐咕) — 注意: 接口参数为中文名称"""
        ak = _get_ak()

        # 乐咕 stock_index_pe_lg 支持的指数（中文名 -> 存储用的代码）
        # 注意：乐咕不支持 科创50、创业板指、行业指数
        pe_index_map = {
            "沪深300": "000300",
            "中证500": "000905",
            "中证1000": "000852",
            "上证50": "000016",
            "创业板50": "399006",  # 乐咕用"创业板50"，对应创业板核心指数
        }

        for lg_name, idx_code in pe_index_map.items():
            filename = f"pe_history_{idx_code}.csv"
            filepath = self._cache_path("time_series", filename)
            if not force and self._is_cache_fresh(filepath, self.ts_expire_days):
                continue
            try:
                logger.info(f"拉取PE历史(乐咕): {lg_name} -> {idx_code}")
                df = ak.stock_index_pe_lg(symbol=lg_name)
                if df is not None and not df.empty:
                    df["index_code"] = idx_code
                    df["index_name"] = lg_name
                    self._save_csv(df, "time_series", filename)
                self._throttle()
            except Exception as e:
                logger.warning(f"拉取 {lg_name}({idx_code}) PE历史失败: {e}")
                self._throttle()

    # ================================================================
    # 时序数据：指数历史PB
    # ================================================================

    def _fetch_index_pb_history(self, force=False):
        """拉取各指数历史PB数据 (乐咕) — 注意: 接口参数为中文名称"""
        ak = _get_ak()

        # 乐咕 stock_index_pb_lg 支持的指数
        pb_index_map = {
            "沪深300": "000300",
            "中证500": "000905",
            "中证1000": "000852",
            "上证50": "000016",
            "创业板50": "399006",
        }

        for lg_name, idx_code in pb_index_map.items():
            filename = f"pb_history_{idx_code}.csv"
            filepath = self._cache_path("time_series", filename)
            if not force and self._is_cache_fresh(filepath, self.ts_expire_days):
                continue
            try:
                logger.info(f"拉取PB历史(乐咕): {lg_name} -> {idx_code}")
                df = ak.stock_index_pb_lg(symbol=lg_name)
                if df is not None and not df.empty:
                    df["index_code"] = idx_code
                    df["index_name"] = lg_name
                    self._save_csv(df, "time_series", filename)
                self._throttle()
            except Exception as e:
                logger.warning(f"拉取 {lg_name}({idx_code}) PB历史失败: {e}")
                self._throttle()

    # ================================================================
    # 时序数据：指数历史行情 (OHLCV)
    # ================================================================

    # 腾讯财经不覆盖的中证自编指数 -> 使用对应ETF代码获取行情
    _QQ_INDEX_ETF_PROXY = {
        "931151": "515790",  # 光伏指数 -> 光伏ETF
        "990001": "512480",  # 半导体指数 -> 半导体ETF
        "930901": "159869",  # 游戏指数 -> 游戏ETF
        "930851": "516510",  # 云计算指数 -> 云计算ETF
        "930633": "159766",  # 旅游指数 -> 旅游ETF
        "931590": "562500",  # 机器人指数 -> 机器人ETF
    }

    def _fetch_index_price_history(self, force=False):
        """拉取各指数历史行情 — 腾讯财经，6个自编指数用ETF行情代理"""
        domestic_indices = []
        for category in ["broad_base", "sector"]:
            for item in self.etf_pool.get(category, []):
                idx_code = item.get("index_code", "")
                if idx_code:
                    domestic_indices.append({"index_code": idx_code, "name": item["name"]})

        for idx_info in domestic_indices:
            idx_code = idx_info["index_code"]
            filename = f"price_history_{idx_code}.csv"
            filepath = self._cache_path("time_series", filename)
            if not force and self._is_cache_fresh(filepath, self.ts_expire_days):
                continue

            try:
                if idx_code in self._QQ_INDEX_ETF_PROXY:
                    # 腾讯不覆盖 -> 用对应ETF行情作为代理
                    etf_code = self._QQ_INDEX_ETF_PROXY[idx_code]
                    logger.info(f"拉取指数行情(ETF代理): {idx_info['name']} ({idx_code}) -> ETF {etf_code}")
                    df = self._fetch_qq_kline(etf_code)
                    if df is not None and not df.empty:
                        df["index_code"] = idx_code
                        df["index_name"] = idx_info["name"]
                        self._save_csv(df, "time_series", filename)
                else:
                    # 腾讯财经K线
                    logger.info(f"拉取指数行情(腾讯): {idx_info['name']} ({idx_code})")
                    df = self._fetch_qq_kline(idx_code)
                    if df is not None and not df.empty:
                        df["index_code"] = idx_code
                        df["index_name"] = idx_info["name"]
                        self._save_csv(df, "time_series", filename)
            except Exception as e:
                logger.warning(f"拉取 {idx_code} 行情失败: {e}")

    # ================================================================
    # 时序数据：ETF历史行情
    # ================================================================

    def _fetch_etf_price_history(self, force=False):
        """拉取各ETF历史行情 — 腾讯财经(免费、不限频)"""
        all_etfs = []
        for category in self.etf_pool:
            for item in self.etf_pool[category]:
                all_etfs.append({"code": item["code"], "name": item["name"], "category": category})

        for etf in all_etfs:
            filename = f"etf_history_{etf['code']}.csv"
            filepath = self._cache_path("time_series", filename)
            if not force and self._is_cache_fresh(filepath, self.ts_expire_days):
                continue
            try:
                logger.info(f"拉取ETF行情(腾讯): {etf['name']} ({etf['code']})")
                df = self._fetch_qq_kline(etf["code"])
                if df is not None and not df.empty:
                    df["etf_code"] = etf["code"]
                    df["etf_name"] = etf["name"]
                    df["category"] = etf["category"]
                    self._save_csv(df, "time_series", filename)
            except Exception as e:
                logger.warning(f"拉取 {etf['code']} ETF行情失败: {e}")

    # ================================================================
    # 时序数据：恐慌指数 QVIX
    # ================================================================

    def _fetch_qvix(self, force=False):
        """拉取50ETF期权波动率指数(QVIX)"""
        filepath = self._cache_path("time_series", "qvix.csv")
        if not force and self._is_cache_fresh(filepath, self.ts_expire_days):
            logger.info("QVIX缓存有效，跳过")
            return

        ak = _get_ak()
        try:
            logger.info("拉取QVIX恐慌指数...")
            df = ak.index_option_50etf_qvix()
            if df is not None and not df.empty:
                self._save_csv(df, "time_series", "qvix.csv")
        except Exception as e:
            logger.warning(f"拉取QVIX失败: {e}")

    # ================================================================
    # 数据读取接口（供Agent使用）
    # ================================================================

    def get_all_valuation_data(self) -> Dict[str, Any]:
        """获取所有横截面估值数据，整合为结构化字典"""
        result = {
            "index_valuation": None,
            "danjuan_valuation": None,
            "etf_realtime": None,
            "update_time": None,
        }

        # 韭圈儿估值（整体法TTM，与雪球一致，优先级最高）
        df_dj = self._load_csv("cross_section", "danjuan_valuation.csv")
        if df_dj is not None:
            result["danjuan_valuation"] = df_dj.to_dict(orient="records")
            filepath = self._cache_path("cross_section", "danjuan_valuation.csv")
            mtime = datetime.datetime.fromtimestamp(os.path.getmtime(filepath))
            result["update_time"] = mtime.strftime("%Y-%m-%d %H:%M:%S")

        # 中证指数估值
        df_val = self._load_csv("cross_section", "index_valuation_csindex.csv")
        if df_val is not None:
            result["index_valuation"] = df_val.to_dict(orient="records")
            if result["update_time"] is None:
                filepath = self._cache_path("cross_section", "index_valuation_csindex.csv")
                mtime = datetime.datetime.fromtimestamp(os.path.getmtime(filepath))
                result["update_time"] = mtime.strftime("%Y-%m-%d %H:%M:%S")

        # ETF实时行情
        df_etf = self._load_csv("cross_section", "etf_realtime.csv")
        if df_etf is not None:
            result["etf_realtime"] = df_etf.to_dict(orient="records")

        return result

    def get_time_series_summary(self) -> Dict[str, Any]:
        """获取时序数据摘要，用于传给大模型和前端绘图"""
        summary = {
            "pe_history": {},
            "pb_history": {},
            "price_history": {},
            "etf_history": {},
            "qvix": None,
        }

        # PE历史 - 取最近250个交易日
        ts_dir = os.path.join(self.cache_dir, "time_series")
        for f in os.listdir(ts_dir) if os.path.exists(ts_dir) else []:
            if f.startswith("pe_history_") and f.endswith(".csv"):
                idx_code = f.replace("pe_history_", "").replace(".csv", "")
                df = self._load_csv("time_series", f)
                if df is not None and not df.empty:
                    df_recent = df.tail(250)
                    summary["pe_history"][idx_code] = {
                        "data": df_recent.to_dict(orient="records"),
                        "current": df.iloc[-1].to_dict() if len(df) > 0 else {},
                        "count": len(df),
                    }

            elif f.startswith("pb_history_") and f.endswith(".csv"):
                idx_code = f.replace("pb_history_", "").replace(".csv", "")
                df = self._load_csv("time_series", f)
                if df is not None and not df.empty:
                    df_recent = df.tail(250)
                    summary["pb_history"][idx_code] = {
                        "data": df_recent.to_dict(orient="records"),
                        "current": df.iloc[-1].to_dict() if len(df) > 0 else {},
                        "count": len(df),
                    }

            elif f.startswith("price_history_") and f.endswith(".csv"):
                idx_code = f.replace("price_history_", "").replace(".csv", "")
                df = self._load_csv("time_series", f)
                if df is not None and not df.empty:
                    summary["price_history"][idx_code] = {
                        "data": df.to_dict(orient="records"),
                        "latest": df.iloc[-1].to_dict() if len(df) > 0 else {},
                    }

            elif f.startswith("etf_history_") and f.endswith(".csv"):
                etf_code = f.replace("etf_history_", "").replace(".csv", "")
                df = self._load_csv("time_series", f)
                if df is not None and not df.empty:
                    summary["etf_history"][etf_code] = {
                        "data": df.to_dict(orient="records"),
                        "latest": df.iloc[-1].to_dict() if len(df) > 0 else {},
                    }

        # QVIX
        df_qvix = self._load_csv("time_series", "qvix.csv")
        if df_qvix is not None and not df_qvix.empty:
            df_recent = df_qvix.tail(60)
            summary["qvix"] = {
                "data": df_recent.to_dict(orient="records"),
                "latest": df_qvix.iloc[-1].to_dict(),
            }

        return summary

    def get_time_series_for_charts(self) -> Dict[str, List[Dict]]:
        """获取适合前端绘图的时序数据（精简版）"""
        charts = {}

        ts_dir = os.path.join(self.cache_dir, "time_series")
        if not os.path.exists(ts_dir):
            return charts

        for f in os.listdir(ts_dir):
            if f.startswith("pe_history_") and f.endswith(".csv"):
                idx_code = f.replace("pe_history_", "").replace(".csv", "")
                df = self._load_csv("time_series", f)
                if df is not None and not df.empty:
                    df_recent = df.tail(120)
                    cols = [c for c in df_recent.columns if c not in ["index_code", "index_name"]]
                    charts[f"pe_{idx_code}"] = df_recent[cols].to_dict(orient="records")

            elif f.startswith("pb_history_") and f.endswith(".csv"):
                idx_code = f.replace("pb_history_", "").replace(".csv", "")
                df = self._load_csv("time_series", f)
                if df is not None and not df.empty:
                    df_recent = df.tail(120)
                    cols = [c for c in df_recent.columns if c not in ["index_code", "index_name"]]
                    charts[f"pb_{idx_code}"] = df_recent[cols].to_dict(orient="records")

            elif f.startswith("price_history_") and f.endswith(".csv"):
                idx_code = f.replace("price_history_", "").replace(".csv", "")
                df = self._load_csv("time_series", f)
                if df is not None and not df.empty:
                    df_recent = df.tail(120)
                    charts[f"price_{idx_code}"] = df_recent[["日期", "收盘"]].rename(
                        columns={"日期": "date", "收盘": "close"}
                    ).to_dict(orient="records")

        # QVIX
        df_qvix = self._load_csv("time_series", "qvix.csv")
        if df_qvix is not None and not df_qvix.empty:
            df_recent = df_qvix.tail(60)
            charts["qvix"] = df_recent.to_dict(orient="records")

        return charts

    def get_daily_changes(self) -> Dict[str, float]:
        """获取各ETF当日涨跌幅，用于监控预警"""
        changes = {}
        df = self._load_csv("cross_section", "etf_realtime.csv")
        if df is not None:
            for _, row in df.iterrows():
                code = str(row.get("代码", ""))
                pct = row.get("涨跌幅", 0)
                try:
                    changes[code] = float(pct)
                except (ValueError, TypeError):
                    pass
        return changes

    # 韭圈儿未直接覆盖的行业指数 -> 近似替代映射
    # key: config中的index_code, value: (韭圈儿中的index_code, 韭圈儿指数名, 说明)
    DANJUAN_FALLBACK_MAP = {
        "000933": ("000991", "全指医药", "近似：全指医药替代中证医药"),
        "399976": ("399417", "新能源车", "近似：中证新能源车替代国证新能车"),
        "990001": ("CSI930652", "中证电子", "近似：中证电子替代半导体"),
        "931151": ("000827", "中证环保", "近似：中证环保替代光伏"),
        "930851": ("000993", "全指信息", "近似：全指信息替代云计算"),
        "930901": ("399971", "中证传媒", "近似：中证传媒替代游戏"),
    }

    def get_valuation_text_for_llm(self) -> str:
        """生成适合大模型阅读的估值摘要文本，优先使用韭圈儿(整体法TTM PE)数据"""
        lines = ["## 当前各指数估值数据（数据来源：韭圈儿/蛋卷基金，整体法TTM口径，与雪球一致）\n"]

        # 构建 index_code -> etf 映射
        idx_to_etf = {}
        for category in self.etf_pool:
            for item in self.etf_pool[category]:
                icode = item.get("index_code", "")
                if icode:
                    idx_to_etf[icode] = {"etf_code": item["code"], "etf_name": item["name"], "category": category}

        # ===== 1. 韭圈儿估值数据（整体法TTM PE/PB + 分位数） =====
        df_dj = self._load_csv("cross_section", "danjuan_valuation.csv")
        dj_map = {}  # index_code -> row dict
        if df_dj is not None and not df_dj.empty:
            for _, row in df_dj.iterrows():
                code = str(row.get("index_code", ""))
                dj_map[code] = row.to_dict()

        processed_codes = set()

        # 按 etf_pool 顺序输出，确保覆盖所有配置的指数
        lines.append("### 指数估值概览（整体法TTM）\n")
        for category in ["broad_base", "sector"]:
            for item in self.etf_pool.get(category, []):
                idx_code = item.get("index_code", "")
                if not idx_code:
                    continue
                name = item.get("name", idx_code)

                if idx_code in dj_map:
                    # 韭圈儿直接命中
                    d = dj_map[idx_code]
                    pe = d.get("pe", 0)
                    pb = d.get("pb", 0)
                    pe_pct = d.get("pe_percentile", 0)
                    pb_pct = d.get("pb_percentile", 0)
                    eva = d.get("eva_type", "")
                    eva_cn = {"low": "低估", "mid": "适中", "high": "高估"}.get(eva, eva)
                    div_yield = d.get("dividend_yield", 0)

                    if pe > 0:
                        lines.append(
                            f"- {name}({idx_code}): PE(TTM)={pe:.2f}, PE分位={pe_pct:.1f}%, "
                            f"PB={pb:.2f}, PB分位={pb_pct:.1f}%, "
                            f"股息率={div_yield*100:.2f}%, 估值状态={eva_cn}"
                        )
                    else:
                        lines.append(
                            f"- {name}({idx_code}): PE不适用(亏损), "
                            f"PB={pb:.2f}, PB分位={pb_pct:.1f}%, 估值状态={eva_cn}"
                        )
                    processed_codes.add(idx_code)

                elif idx_code in self.DANJUAN_FALLBACK_MAP:
                    # 韭圈儿近似指数替代
                    alt_code, alt_name, note = self.DANJUAN_FALLBACK_MAP[idx_code]
                    if alt_code in dj_map:
                        d = dj_map[alt_code]
                        pe = d.get("pe", 0)
                        pb = d.get("pb", 0)
                        pe_pct = d.get("pe_percentile", 0)
                        pb_pct = d.get("pb_percentile", 0)
                        eva = d.get("eva_type", "")
                        eva_cn = {"low": "低估", "mid": "适中", "high": "高估"}.get(eva, eva)
                        div_yield = d.get("dividend_yield", 0)

                        if pe > 0:
                            lines.append(
                                f"- {name}({idx_code}): PE(TTM)={pe:.2f}, PE分位={pe_pct:.1f}%, "
                                f"PB={pb:.2f}, PB分位={pb_pct:.1f}%, "
                                f"股息率={div_yield*100:.2f}%, 估值状态={eva_cn} "
                                f"（{note}，参考{alt_name}({alt_code})）"
                            )
                        else:
                            lines.append(
                                f"- {name}({idx_code}): PE不适用(亏损), "
                                f"PB={pb:.2f}, PB分位={pb_pct:.1f}%, 估值状态={eva_cn} "
                                f"（{note}，参考{alt_name}({alt_code})）"
                            )
                        processed_codes.add(idx_code)

                if idx_code not in processed_codes:
                    # 韭圈儿完全无替代，回退到中证指数数据
                    df_cs = self._load_csv("time_series", f"valuation_csindex_{idx_code}.csv")
                    if df_cs is not None and not df_cs.empty:
                        pe_col = "市盈率2" if "市盈率2" in df_cs.columns else ("市盈率1" if "市盈率1" in df_cs.columns else None)
                        if pe_col:
                            pe_series = pd.to_numeric(df_cs[pe_col], errors="coerce").dropna()
                            if len(pe_series) > 0:
                                current_pe = pe_series.iloc[0]
                                lines.append(
                                    f"- {name}({idx_code}): PE(TTM)={current_pe:.2f} "
                                    f"（中证指数数据，仅{len(pe_series)}天，分位数不可靠）"
                                )
                                processed_codes.add(idx_code)

        # ===== 2. 乐咕PE历史分位（数千交易日，用于补充验证） =====
        lines.append("")
        lines.append("### 乐咕长期PE分位（数千交易日历史，5个宽基指数）\n")
        ts_dir = os.path.join(self.cache_dir, "time_series")
        if os.path.exists(ts_dir):
            for f in sorted(os.listdir(ts_dir)):
                if f.startswith("pe_history_") and f.endswith(".csv"):
                    idx_code = f.replace("pe_history_", "").replace(".csv", "")
                    df = self._load_csv("time_series", f)
                    if df is not None and not df.empty:
                        pe_col = None
                        for candidate in ["滚动市盈率", "静态市盈率", "等权滚动市盈率"]:
                            if candidate in df.columns:
                                pe_col = candidate
                                break
                        if pe_col:
                            pe_series = pd.to_numeric(df[pe_col], errors="coerce").dropna()
                            if len(pe_series) > 0:
                                current_pe = pe_series.iloc[-1]
                                percentile = (pe_series < current_pe).sum() / len(pe_series) * 100
                                name = df["index_name"].iloc[0] if "index_name" in df.columns else idx_code
                                lines.append(
                                    f"- {name}({idx_code}): 乐咕{pe_col}={current_pe:.2f}, "
                                    f"分位={percentile:.1f}%（{len(pe_series)}交易日）"
                                )

        lines.append("")

        # ===== 3. ETF实时行情 =====
        df_etf = self._load_csv("cross_section", "etf_realtime.csv")
        if df_etf is not None:
            lines.append("## ETF实时行情摘要\n")
            for _, row in df_etf.iterrows():
                name = row.get("名称", "")
                code = row.get("代码", "")
                price = row.get("最新价", "")
                pct = row.get("涨跌幅", "")
                vol = row.get("成交量", "")
                turnover = row.get("换手率", "")
                lines.append(f"- {name}({code}): 最新价={price}, 涨跌幅={pct}%, 成交量={vol}, 换手率={turnover}%")

        # ===== 4. QVIX =====
        df_qvix = self._load_csv("time_series", "qvix.csv")
        if df_qvix is not None and not df_qvix.empty:
            latest = df_qvix.iloc[-1]
            lines.append(f"\n## 恐慌指数(QVIX)\n")
            for col in df_qvix.columns:
                lines.append(f"- {col}: {latest[col]}")

        return "\n".join(lines)
