#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浮动隐蔽股票行情 HUD (macOS / Windows / Linux 均可运行) —— 每秒刷新关注股票 + 信号提示 + 通知/CSV。

自成一体的单文件工具(不再依赖 stock_monitor.py):
- 数据: 腾讯财经实时行情(qt.gtimg.cn) + 历史日K(ifzq.gtimg.cn); 备用免费源(新浪/东财)兜底
- 指标: MA5/10/20, RSI(14), MACD(12,26,9); 可扩展 KDJ / 布林 / 量; 盘中把实时价并入指标序列(可关)
- 信号: 多空打分 -> 五档信号(带滞回, 吸收边界抖动); 打分指标由 settings.indicators 动态决定(默认 MA/RSI/MACD)
- 界面: 原生 tkinter 无边框 + 置顶 + 半透明; 上半部分行情(含 sparkline), 下半部分信号提示; 支持暂停/频率切换/暗色
- 通知: 省流规则(信号变动/阈值破位/支撑压力穿越)时弹系统通知(可配声音, 带冷却); 跨平台分发(mac AppleScript / linux notify-send / win 蜂鸣+闪烁)
- 落盘: 同规则写 signals CSV(可按天滚动; 可关闭; 可按 csv_dedup_sec 去重); 支持 --stats 聚合与 --review 过滤
- 日K按 代码+日期 缓存(30min), 实时行情并发拉取

配置从同级目录读取(自动寻找):
  设置: config.toml (只放 [settings]); 兼容旧版 stocks.toml 内含 [settings]
  股票: stocks.toml (放 [settings] 监控引擎配置 + [[stocks]] 股票列表, 可选 support/resistance 支撑压力价)

用法:
  python3 stock_float.py                 # 启动浮窗(默认)
  python3 stock_float.py --review        # 回看历史信号CSV(最近30条)
  python3 stock_float.py --stats         # 当日信号聚合统计
  python3 stock_float.py --review --code hk01810 --date 2026-07-15
双击同级 启动浮窗.command (mac) / 启动浮窗.bat (win) 亦可运行。
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import threading
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Tuple, Optional, Callable, List, Dict, Any

try:
    import tkinter as tk
except Exception:
    tk = None

try:
    import tomllib  # Python 3.11+
except Exception:
    tomllib = None

try:
    import tkinter.simpledialog as simpledialog  # 运行时添加自选的输入对话框
except Exception:
    simpledialog = None

try:
    from tkinter import messagebox  # 删除自选确认弹框
except Exception:
    messagebox = None

try:
    from zoneinfo import ZoneInfo
    _ZI = True
except Exception:
    _ZI = False


# ================= ① 引擎: 常量 =================
HKT = timezone(timedelta(hours=8))   # 参考时区: 港股/内地均为 GMT+8
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# 配置文件(只放 [settings]) 候选; 也可只保留一个文件(如 config.toml)
SETTINGS_CANDIDATES = ["config.toml", "config.json", "settings.toml",
                       "settings.json", "配置.toml", "配置.json"]
# 股票列表 + [settings](监控引擎配置) 候选; 也可只保留一个文件(如 stocks.toml)
STOCKS_CANDIDATES = ["stocks.toml", "stocks.json", "stocks.yaml", "stocks.yml",
                     "watchlist.toml", "watchlist.json", "自选股.toml"]

# 历史信号落盘(单文件模式)
SIGNAL_CSV = os.path.join(SCRIPT_DIR, "signals.csv")
# CSV 字段(顺序即列顺序): 既有字段在前, 新增指标字段追加在尾部(向后兼容旧 CSV)
CSV_FIELDS = ["datetime", "code", "name", "price", "chg_pct", "open", "prev_close",
              "ma5", "ma10", "ma20", "rsi", "macd_dif", "macd_dea", "macd_hist",
              "bull", "bear", "net", "signal", "reasons",
              "k", "d", "j", "boll_mid", "boll_up", "boll_low", "volume", "vol_ma5"]

# 日K缓存: code -> (日期, 收盘价序列, 成交量序列, 抓取时间); 盘中日K变化极小, 30min 复用
_KLINE_CACHE: Dict[str, Tuple[str, List[float], List[float], datetime]] = {}
_KLINE_TTL = 1800

# 盘中灵敏: 把实时价作为"当日收盘"并入指标序列(替换最后一根收盘), 默认开启。
# 关闭(live_indicators=false)则只用日K收盘算指标(金叉/死叉仅日界变)。
LIVE_INDICATORS = True

# 默认监控列表 (未找到任何配置文件时使用)
DEFAULT_STOCKS = [
    {"code": "hk01810", "name": "小米集团-W"},
]

# 各市场交易时段(当地时间的分钟区间)
HK_RANGES = [(9 * 60 + 30, 12 * 60), (13 * 60, 16 * 60)]
A_RANGES  = [(9 * 60 + 30, 11 * 60 + 30), (13 * 60, 15 * 60)]
US_RANGES = [(9 * 60 + 30, 16 * 60)]

# 免费行情源(qt.gtimg.cn)对以下市场延时: 港股约15分钟, 美股约15分钟; A股为实时
DELAYED_MARKETS = {"hk", "us"}

# 可用指标 & 默认打分指标(默认与改造前 MA/RSI/MACD 权重逐字一致)
INDICATORS_AVAILABLE = ["MA", "RSI", "MACD", "KDJ", "BOLL", "VOLUME"]
DEFAULT_INDICATORS = ["MA", "RSI", "MACD"]
# 默认启用的行情源(全部为免费公开源, 顺序即兜底顺序; 主源=索引0)
DEFAULT_SOURCES = ["tencent", "sina", "eastmoney"]

# 实时行情返回元组: (price, prev_close, open_px, ts, high, low, volume, delayed)
RT = Tuple[float, float, float, str, Optional[float], Optional[float], Optional[float], bool]

# 过滤窗口(功能②): 信号档位发生变化后, 在窗口内(默认 300 秒)仍可见
SIG_CHANGE_WINDOW_SEC = 300


# ================= ③ 引擎: 工具 =================
def _to_float(x) -> Optional[float]:
    """安全转 float, 失败返回 None。"""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def fmt_chg(chg) -> str:
    """把涨跌幅格式化为字符串(纯文本, 供通知/回看用)。"""
    v = _to_float(chg)
    if v is None:
        return "NA" if chg is None else str(chg)
    return f"{v:+.2f}%"


def fmt_ts(ts) -> str:
    """把腾讯接口时间戳归一化为 HH:MM 显示; 解析失败回退空串。
    A股: 20260714094802 -> 09:48 ; 港股: 2026/07/14 09:33:00 -> 09:33
    """
    if not ts:
        return ""
    s = str(ts).strip()
    if len(s) == 14 and s.isdigit():          # A股紧凑格式
        return f"{s[8:10]}:{s[10:12]}"
    m = re.search(r"(\d{1,2}):(\d{2})(?::\d{2})?\s*$", s)  # 港股带分隔符格式
    if m:
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    return ""


def fetch(url, timeout=8) -> str:
    """GET 一个 URL 返回解码后的文本; 失败抛异常由上层处理。"""
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def market_of(code) -> str:
    """从代码前缀识别市场: hk / sh / sz / us, 未知返回空串。"""
    pre = code[:2].lower()
    if pre in ("hk", "sh", "sz"):
        return pre
    if code[:2].lower() == "us":
        return "us"
    return ""


def is_delayed(code) -> bool:
    """该代码是否走延时行情源(免费源港股/美股延时, A股实时)。"""
    return market_of(code) in DELAYED_MARKETS


def market_local_now(now_hkt, market) -> datetime:
    """把 HKT 参考时间换算成该市场的本地时间。"""
    if market in ("hk", "sh", "sz"):
        return now_hkt.astimezone(HKT)
    if market == "us":
        if _ZI:
            return now_hkt.astimezone(ZoneInfo("America/New_York"))
        return now_hkt.astimezone(timezone(timedelta(hours=-4)))
    return now_hkt


def _in_session(h, m, ranges) -> bool:
    t = h * 60 + m
    return any(a <= t <= b for a, b in ranges)


def is_trading_time(now_hkt, market) -> bool:
    """该市场当前是否处于交易时段(当地工作日 + 时段内)。"""
    local = market_local_now(now_hkt, market)
    if local.weekday() >= 5:
        return False
    h, m = local.hour, local.minute
    if market == "hk":
        return _in_session(h, m, HK_RANGES)
    if market in ("sh", "sz"):
        return _in_session(h, m, A_RANGES)
    if market == "us":
        return _in_session(h, m, US_RANGES)
    return False


def _safe_float(p, i) -> Optional[float]:
    """安全取数组元素转 float, 越界/非法返回 None。"""
    try:
        if i < len(p):
            return float(p[i])
    except (ValueError, TypeError):
        pass
    return None


def _cross(prev: Optional[float], cur: Optional[float], level: float) -> int:
    """穿越判定: 返回 -1 表示下穿(跌破, prev>level>=cur), +1 表示上穿(突破, prev<level<=cur), 0 未穿越。"""
    if prev is None or cur is None:
        return 0
    if prev > level >= cur:
        return -1
    if prev < level <= cur:
        return 1
    return 0


# ================= ② 引擎: 数据源层 =================
class RealTimeSource:
    """实时行情源抽象基类; fetch 失败抛异常(由 DataSource 兜底)。"""

    name = "base"

    def fetch(self, code: str) -> RT:
        raise NotImplementedError


class TencentSource(RealTimeSource):
    """主源: 腾讯财经 qt.gtimg.cn; 免费、A股实时、港股/美股延时。"""

    name = "tencent"

    def fetch(self, code: str) -> RT:
        raw = fetch(f"https://qt.gtimg.cn/q={code}")
        s = raw.split('"')[1]
        p = s.split("~")
        # 腾讯各市场通用字段: [3]现价 [4]昨收 [5]今开 [33]最高 [34]最低 [6]成交量(手, 需实测确认)
        price = float(p[3])
        prev_close = float(p[4])
        open_px = float(p[5])
        high = _safe_float(p, 33)
        low = _safe_float(p, 34)
        volume = _safe_float(p, 6)          # 先按 p[6](手数)实测; 失败回退 None 不报错
        ts = p[-1] if len(p[-1]) > 8 else (p[30] if len(p) > 30 else "")
        delayed = is_delayed(code)
        return (price, prev_close, open_px, ts, high, low, volume, delayed)


class SinaSource(RealTimeSource):
    """备用免费源: 新浪 hq.sinajs.cn; 需带 Referer。字段映射以常见 A股/港股为准, 解析失败抛异常走下一源。"""

    name = "sina"

    @staticmethod
    def _to_sina_symbol(code: str) -> str:
        m = market_of(code)
        num = code[2:]
        if m in ("sh", "sz"):
            return f"{m}{num}"
        if m == "hk":
            return f"hk{num}"
        if m == "us":
            return f"gb{num.lower()}"
        return code

    def fetch(self, code: str) -> RT:
        sym = self._to_sina_symbol(code)
        url = f"https://hq.sinajs.cn/list={sym}"
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://finance.sina.com.cn",
        })
        with urllib.request.urlopen(req, timeout=8) as r:
            raw = r.read().decode("gbk", "ignore")
        # var hq_str_xxx="名,今开,昨收,现价,最高,最低,...,成交量(股),..."
        m = re.search(r'=["\']([^"\'\\]+)["\']', raw)
        if not m:
            raise ValueError("新浪返回格式异常")
        parts = m.group(1).split(",")
        if len(parts) < 6:
            raise ValueError("新浪字段不足")
        open_px = _to_float(parts[1])
        prev_close = _to_float(parts[2])
        price = _to_float(parts[3])
        high = _to_float(parts[4])
        low = _to_float(parts[5])
        if price is None or prev_close is None:
            raise ValueError("新浪关键字段缺失")
        # 成交量(股) -> 转手; A股[8], 港股[7]; 取其一
        vol_shares = _safe_float(parts, 8) if _safe_float(parts, 8) is not None else _safe_float(parts, 7)
        volume = vol_shares / 100.0 if vol_shares is not None else None
        delayed = is_delayed(code)
        return (price, prev_close, open_px, "", high, low, volume, delayed)


class EastmoneySource(RealTimeSource):
    """备用免费源: 东财 push2.eastmoney.com; 需按市场构造 secid。价格/100, 成交量(手)。"""

    name = "eastmoney"
    _SECID_PREFIX = {"hk": "116", "sh": "1", "sz": "0", "us": "105"}

    @staticmethod
    def _secid(code: str) -> Optional[str]:
        m = re.match(r"^(hk|sh|sz|us)([0-9A-Za-z]+)$", code)
        if not m:
            return None
        prefix = EastmoneySource._SECID_PREFIX.get(m.group(1))
        return f"{prefix}.{m.group(2)}" if prefix else None

    def fetch(self, code: str) -> RT:
        secid = self._secid(code)
        if secid is None:
            raise ValueError(f"无法构造东财 secid: {code}")
        url = (f"https://push2.eastmoney.com/api/qt/stock/get"
               f"?secid={secid}&fields=f43,f44,f45,f46,f47,f57,f58,f86")
        data = json.loads(fetch(url, timeout=8))
        node = data.get("data")
        if not isinstance(node, dict):
            raise ValueError("东财返回空 data")
        gv = lambda k: _to_float(node.get(k))
        price = gv("f43")
        if price is None:
            raise ValueError("东财缺 f43")
        price = price / 100.0
        high = gv("f44")
        low = gv("f45")
        open_px = gv("f46")
        prev_close = gv("f60")
        volume = gv("f47")                       # 手
        high = high / 100.0 if high is not None else None
        low = low / 100.0 if low is not None else None
        open_px = open_px / 100.0 if open_px is not None else None
        prev_close = prev_close / 100.0 if prev_close is not None else None
        ts = ""
        tsec = gv("f86")
        if tsec is not None:
            try:
                ts = datetime.fromtimestamp(tsec, HKT).strftime("%H:%M")
            except Exception:
                ts = ""
        delayed = is_delayed(code)
        return (price, prev_close, open_px, ts, high, low, volume, delayed)


_SOURCE_REGISTRY = {
    "tencent": TencentSource,
    "sina": SinaSource,
    "eastmoney": EastmoneySource,
}


def build_sources(names: Optional[List[str]]) -> List[RealTimeSource]:
    """按配置名构造行情源列表; 未知名跳过; 全空则回退仅腾讯主源。"""
    sources: List[RealTimeSource] = []
    for n in (names or DEFAULT_SOURCES):
        cls = _SOURCE_REGISTRY.get(n)
        if cls is not None:
            sources.append(cls())
    return sources or [TencentSource()]


class DataSource:
    """统一行情入口: 按 sources 顺序兜底; 主源(索引0)异常经 alert_fn 仅告警一次(非静默跳过)。"""

    def __init__(self, sources: Optional[List[RealTimeSource]] = None,
                 alert_fn: Optional[Callable[[str], None]] = None):
        self.sources = sources if sources is not None else [
            TencentSource(), SinaSource(), EastmoneySource()
        ]
        self.alert_fn = alert_fn
        self._alerted = False

    def fetch(self, code: str) -> RT:
        last_err: Optional[BaseException] = None
        for i, src in enumerate(self.sources):
            try:
                return src.fetch(code)
            except Exception as e:          # noqa: BLE001 - 任何源异常都尝试下一源
                last_err = e
                # 主源(索引0)异常: 仅经 alert_fn 告警一次
                if i == 0 and self.alert_fn is not None and not self._alerted:
                    self._alerted = True
                    try:
                        self.alert_fn(f"主源({src.name})异常, 已切备用源: {e}")
                    except Exception:
                        pass
                continue
        raise last_err if last_err is not None else RuntimeError("无可用行情源")


# 模块级默认数据源(主函数会按配置重建并注入 alert_fn)
DATA_SOURCE = DataSource()


def get_realtime(code: str) -> RT:
    """兼容别名: 委托 DATA_SOURCE.fetch 取实时行情(返回8元组)。"""
    return DATA_SOURCE.fetch(code)


def _fetch_kline_full(code: str) -> Tuple[List[float], List[float]]:
    """拉取日K收盘序列与成交量序列(手); 返回 (closes, volumes)。"""
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,60,qfq"
    data = json.loads(fetch(url))
    node = data["data"][code]
    bars = node.get("qfqday") or node.get("day")
    closes = [float(b[2]) for b in bars]
    volumes = []
    for b in bars:
        v = _safe_float(b, 5)
        volumes.append(v if v is not None else 0.0)
    return closes, volumes


def get_kline(code: str, force: bool = False) -> List[float]:
    """拉取日K收盘序列; 按 代码+日期 缓存, 同日期 30min 内复用。抓取失败且当天有缓存时回退缓存。"""
    today = datetime.now(HKT).strftime("%Y-%m-%d")
    cached = _KLINE_CACHE.get(code)
    if cached and not force:
        cdate, closes, _vols, ts = cached
        age = (datetime.now(HKT) - ts).total_seconds()
        if cdate == today and age < _KLINE_TTL:
            return closes
    try:
        closes, volumes = _fetch_kline_full(code)
        _KLINE_CACHE[code] = (today, closes, volumes, datetime.now(HKT))
        return closes
    except Exception:
        if cached:
            return cached[1]
        raise


def get_volume_hist(code: str, force: bool = False) -> Optional[List[float]]:
    """返回历史每日成交量(手)序列; 仅当 indicators 含 'VOLUME' 时调用(默认关闭, 不增加请求)。"""
    today = datetime.now(HKT).strftime("%Y-%m-%d")
    cached = _KLINE_CACHE.get(code)
    if cached and not force:
        cdate, _closes, volumes, ts = cached
        age = (datetime.now(HKT) - ts).total_seconds()
        if cdate == today and age < _KLINE_TTL:
            return volumes
    try:
        _closes, volumes = _fetch_kline_full(code)
        _KLINE_CACHE[code] = (today, _closes, volumes, datetime.now(HKT))
        return volumes
    except Exception:
        if cached:
            return cached[2]
        return None


def fetch_realtime_batch(codes: List[str]) -> Dict[str, Any]:
    """并发拉取多只实时行情, 返回 {code: (price,prev,open,ts,high,low,vol,delayed) 或 Exception}。"""
    results: Dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=min(len(codes), 8)) as ex:
        futs = {ex.submit(get_realtime, c): c for c in codes}
        for f in as_completed(futs):
            c = futs[f]
            try:
                results[c] = f.result()
            except Exception as e:
                results[c] = e
    return results


def warm_klines(stocks: List[dict]) -> None:
    """启动前并发预热日K缓存, 避免首个刷新周期逐只抓取卡顿。"""
    codes = [st["code"] for st in stocks]
    with ThreadPoolExecutor(max_workers=min(len(codes), 8)) as ex:
        futs = [ex.submit(get_kline, c) for c in codes]
        for f in futs:
            try:
                f.result()
            except Exception:
                pass


# ================= ④ 引擎: 指标 (Indicator 纯函数命名空间) =================
def sma(vals: List[float], n: int) -> Optional[float]:
    """简单移动平均; 数据不足返回 None。"""
    return sum(vals[-n:]) / n if len(vals) >= n else None


def ema_series(vals: List[float], n: int) -> List[Optional[float]]:
    """指数移动平均序列; 前 n-1 个为 None, 之后逐根递推。"""
    start = next((i for i, v in enumerate(vals) if v is not None), 0)
    k = 2 / (n + 1)
    out = [None] * start
    prev = sum(vals[start:start + n]) / n
    out.extend([None] * (n - 1))
    out.append(prev)
    for i in range(start + n, len(vals)):
        if vals[i] is None:
            out.append(None)
            continue
        prev = vals[i] * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(closes: List[float], n: int = 14) -> Optional[float]:
    """相对强弱指标(默认14); 数据不足返回 None。"""
    if len(closes) < n + 1:
        return None
    gains = [max(closes[i] - closes[i - 1], 0) for i in range(-n, 0)]
    losses = [max(closes[i - 1] - closes[i], 0) for i in range(-n, 0)]
    ag, al = sum(gains) / n, sum(losses) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def macd(closes: List[float], fast: int = 12, slow: int = 26, sig: int = 9):
    """MACD; 返回 (dif, dea, hist, prev_hist) 或 (None, None, None, None)(数据不足)。"""
    if len(closes) < slow + sig:
        return None, None, None, None
    ef = ema_series(closes, fast)
    es = ema_series(closes, slow)
    dif = [ef[i] - es[i] if ef[i] is not None and es[i] is not None else None
           for i in range(len(closes))]
    dea = ema_series(dif, sig)
    hist = [(dif[i] - dea[i]) * 2 if dif[i] is not None and dea[i] is not None else None
            for i in range(len(closes))]
    return dif[-1], dea[-1], hist[-1], hist[-2]


def kdj(closes: List[float], n: int = 9, k_period: int = 3, d_period: int = 3):
    """简化 KDJ: 用收盘价版 RSV=(C-min(Cn))/(max(Cn)-min(Cn))*100 递推 K/D/J。
    返回 (k, d, j); 数据不足返回 (None, None, None)。注: 非严格 high/low 版(用户已拍板简化口径)。
    """
    if len(closes) < n:
        return None, None, None
    a_k = 1.0 / k_period
    a_d = 1.0 / d_period
    k, d = 50.0, 50.0
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1: i + 1]
        lo, hi = min(w), max(w)
        rsv = 50.0 if hi == lo else (closes[i] - lo) / (hi - lo) * 100.0
        k = (1 - a_k) * k + a_k * rsv
        d = (1 - a_d) * d + a_d * k
    j = 3 * k - 2 * d
    return k, d, j


def bollinger(closes: List[float], n: int = 20, k: float = 2):
    """布林带; 返回 (mid, upper, lower); 数据不足返回 (None, None, None)。"""
    if len(closes) < n:
        return None, None, None
    window = closes[-n:]
    mid = sum(window) / n
    var = sum((x - mid) ** 2 for x in window) / n
    sd = var ** 0.5
    return mid, mid + k * sd, mid - k * sd


def _vol_ma5(code: str, volume: Optional[float]) -> Optional[float]:
    """量MA5: 历史量序列 + 当日实时量, 取近5根均值; 无历史量返回 None。"""
    vols = get_volume_hist(code)
    if vols is None:
        return None
    seq = list(vols)
    if volume is not None:
        seq = seq + [volume]
    return sma(seq, 5)


# ================= ⑤ 引擎: 信号引擎 (SignalEngine / monitor) =================
# 信号档位滞回(hysteresis): 吸收实时价在边界附近的高频抖动, 避免反复横跳。
_SIG_LEVELS = {
    "🔴 买入(偏强)": 2,
    "🟠 轻仓/偏多": 1,
    "⚪ 持有/观望": 0,
    "🔵 减仓/偏空": -1,
    "🟢 卖出(偏弱)": -2,
}
_LEVEL_SIG = {v: k for k, v in _SIG_LEVELS.items()}


def map_signal(net: int, prev_sig: Optional[str] = None) -> str:
    """把净力(net)映射成信号档位; 有 prev_sig 时应用滞回, 否则用原始阈值。"""
    if prev_sig is None:
        if net >= 3: return "🔴 买入(偏强)"
        if net >= 1: return "🟠 轻仓/偏多"
        if net <= -3: return "🟢 卖出(偏弱)"
        if net <= -1: return "🔵 减仓/偏空"
        return "⚪ 持有/观望"
    lvl = _SIG_LEVELS.get(prev_sig, 0)
    if lvl == 0:
        if net >= 2: lvl = 1
        elif net <= -2: lvl = -1
    elif lvl == 1:
        if net >= 3: lvl = 2
        elif net <= 0: lvl = 0
    elif lvl == 2:
        if net <= 2: lvl = 1
    elif lvl == -1:
        if net <= -3: lvl = -2
        elif net >= 0: lvl = 0
    elif lvl == -2:
        if net >= -2: lvl = -1
    return _LEVEL_SIG[lvl]


# 打分函数注册表: 每个返回 (delta_bull, delta_bear, reasons:list[str])
def _score_ma(price: float, vals: dict):  # noqa: ANN001
    bull = bear = 0
    reasons: List[str] = []
    ma5, ma10, ma20 = vals.get("ma5"), vals.get("ma10"), vals.get("ma20")
    if ma5 and price > ma5: bull += 1; reasons.append("价在MA5上")
    else: bear += 1; reasons.append("价在MA5下")
    if ma5 and ma10 and ma5 > ma10: bull += 1; reasons.append("MA5>MA10(短多)")
    else: bear += 1; reasons.append("MA5<MA10(短空)")
    if ma10 and ma20 and ma10 > ma20: bull += 1; reasons.append("MA10>MA20(中多)")
    else: bear += 1; reasons.append("MA10<MA20(中空)")
    return bull, bear, reasons


def _score_rsi(price: float, vals: dict):  # noqa: ANN001
    r = vals.get("rsi")
    if r is None:
        return 0, 0, []
    if r < 35: return 1, 0, [f"RSI{r:.0f}超卖"]
    elif r > 65: return 0, 1, [f"RSI{r:.0f}超买"]
    return 0, 0, []


def _score_macd(price: float, vals: dict):  # noqa: ANN001
    hist = vals.get("hist")
    prev_hist = vals.get("prev_hist")
    if hist is None:
        return 0, 0, []
    if hist > 0: bull, bear, reasons = 1, 0, ["MACD红柱"]
    else: bull, bear, reasons = 0, 1, ["MACD绿柱"]
    if prev_hist is not None:
        if prev_hist <= 0 < hist: bull += 2; reasons.append("MACD金叉")
        elif prev_hist >= 0 > hist: bear += 2; reasons.append("MACD死叉")
    return bull, bear, reasons


def _score_kdj(price: float, vals: dict):  # noqa: ANN001
    k, d, j = vals.get("k"), vals.get("d"), vals.get("j")
    if k is None or d is None:
        return 0, 0, []
    bull = bear = 0
    reasons: List[str] = []
    if k > d: bull += 1; reasons.append("KDJ金叉区(K>D)")
    else: bear += 1; reasons.append("KDJ死叉区(K<D)")
    if j is not None:
        if j < 0: bull += 1; reasons.append("J<0超卖")
        elif j > 100: bear += 1; reasons.append("J>100超买")
    return bull, bear, reasons


def _score_boll(price: float, vals: dict):  # noqa: ANN001
    up = vals.get("boll_up")
    low = vals.get("boll_low")
    if up is None or low is None or price is None:
        return 0, 0, []
    if price < low: return 1, 0, ["价破布林下轨(超卖)"]
    elif price > up: return 0, 1, ["价破布林上轨(超买)"]
    return 0, 0, []


def _score_volume(price: float, vals: dict):  # noqa: ANN001
    vol = vals.get("vol")
    vol_ma5 = vals.get("vol_ma5")
    if vol is None or vol_ma5 is None or vol_ma5 <= 0:
        return 0, 0, []
    ratio = vol / vol_ma5
    if ratio > 1.5: return 1, 0, [f"放量{ratio:.1f}×MA5"]
    elif ratio < 0.5: return 0, 1, [f"缩量{ratio:.1f}×MA5"]
    return 0, 0, []


SCORERS = {
    "MA": _score_ma,
    "RSI": _score_rsi,
    "MACD": _score_macd,
    "KDJ": _score_kdj,
    "BOLL": _score_boll,
    "VOLUME": _score_volume,
}


def monitor(stock: dict, prev_sig: Optional[str] = None, rt: Optional[RT] = None,
            settings: Optional[dict] = None):
    """算单只股票的指标/信号; 返回 (text, sig, rec)。rt 为预取实时行情元组(可选)。

    settings["indicators"] 控制参与打分的指标(默认 ["MA","RSI","MACD"], 与改造前权重逐字一致);
    rec 新增 k/d/j/boll_*/volume/vol_ma5 字段(追加到 CSV 尾部), 以及仅运行时的 kline(sparkline 用)。
    """
    code = stock["code"]
    name = stock["name"]
    market = market_of(code)
    settings = settings or {}

    now = datetime.now(HKT)
    delayed = is_delayed(code)            # 免费源港股/美股延时, A股实时
    trading = is_trading_time(now, market)
    if rt is None:
        rt = get_realtime(code)
    price, prev_close, open_px, ts, high, low, volume, delayed = rt
    closes = get_kline(code)

    # 盘中灵敏: 实时价替换最后一根(今日)收盘并入指标序列(用 list 拷贝, 不污染缓存)。
    if LIVE_INDICATORS and closes:
        ind = list(closes[:-1]) + [price]
    else:
        ind = list(closes) if closes else [price]

    chg = (price - prev_close) / prev_close * 100
    swing = (high - low) / prev_close * 100 if (high is not None and low is not None and prev_close) else None

    # —— 按 indicators 动态计算指标(默认 MA/RSI/MACD) ——
    indicators = settings.get("indicators") or DEFAULT_INDICATORS
    indicators = [x for x in indicators if x in SCORERS]   # 过滤未知项
    vals: Dict[str, Any] = {}
    if "MA" in indicators:
        vals["ma5"], vals["ma10"], vals["ma20"] = sma(ind, 5), sma(ind, 10), sma(ind, 20)
    if "RSI" in indicators:
        vals["rsi"] = rsi(ind)
    if "MACD" in indicators:
        dif, dea, hist, prev_hist = macd(ind)
        vals["dif"], vals["dea"] = dif, dea
        vals["hist"], vals["prev_hist"] = hist, prev_hist
    if "KDJ" in indicators:
        vals["k"], vals["d"], vals["j"] = kdj(ind)
    if "BOLL" in indicators:
        vals["boll_mid"], vals["boll_up"], vals["boll_low"] = bollinger(ind)
    if "VOLUME" in indicators:
        vals["vol"] = volume
        vals["vol_ma5"] = _vol_ma5(code, volume)

    # —— 动态累加打分 ——
    bull = bear = 0
    reasons: List[str] = []
    for name_i in indicators:
        db, dbear, rs = SCORERS[name_i](price, vals)
        bull += db
        bear += dbear
        reasons += rs

    net = bull - bear
    sig = map_signal(net, prev_sig)

    # —— 终端/回看文本(默认指标时与改造前逐字一致) ——
    out: List[str] = []
    out.append(f"=== {name} ({code.upper()}) ===")
    out.append(f"时间: {ts or now.strftime('%Y-%m-%d %H:%M')}  {'[交易中]' if trading else '[闭市]'}")
    out.append(f"现价: {price:.3f}  涨跌: {fmt_chg(chg)}  今开: {open_px:.3f}  昨收: {prev_close:.3f}"
               + (f"  日内波动: {swing:.2f}%" if swing is not None else ""))
    if "MA" in indicators or "RSI" in indicators:
        rsi_str = f"{vals['rsi']:.1f}" if vals.get("rsi") is not None else "NA"
        def _f3(v):  # noqa: ANN001
            return f"{v:.3f}" if isinstance(v, (int, float)) else "NA"
        ma5v, ma10v, ma20v = vals.get("ma5"), vals.get("ma10"), vals.get("ma20")
        out.append(f"MA5:{_f3(ma5v)}  MA10:{_f3(ma10v)}  MA20:{_f3(ma20v)}  RSI:{rsi_str}")
    if "MACD" in indicators and vals.get("dif") is not None:
        out.append(f"MACD DIF:{vals['dif']:.3f} DEA:{vals['dea']:.3f} 柱:{vals['hist']:+.3f}")
    if "KDJ" in indicators and vals.get("k") is not None:
        out.append(f"KDJ K:{vals['k']:.1f} D:{vals['d']:.1f} J:{vals['j']:.1f}")
    if "BOLL" in indicators and vals.get("boll_mid") is not None:
        out.append(f"布林 中:{vals['boll_mid']:.3f} 上:{vals['boll_up']:.3f} 下:{vals['boll_low']:.3f}")
    if "VOLUME" in indicators and vals.get("vol") is not None:
        vma = vals.get("vol_ma5")
        out.append(f"量:{vals['vol']:.0f} 量MA5:{vma:.0f}" if vma is not None else f"量:{vals['vol']:.0f}")
    out.append(f"信号: {sig}  (多{bull}/空{bear})")
    out.append("依据: " + ", ".join(reasons))
    text = "\n".join(out)

    ma5, ma10, ma20 = vals.get("ma5"), vals.get("ma10"), vals.get("ma20")
    rec = {
        "datetime": now.strftime("%Y-%m-%d %H:%M:%S"),
        "code": code,
        "name": name,
        "price": round(price, 3),
        "chg_pct": round(chg, 2),
        "swing_pct": round(swing, 2) if swing is not None else "",
        "open": round(open_px, 3),
        "prev_close": round(prev_close, 3),
        "ma5": round(ma5, 3) if ma5 is not None else "",
        "ma10": round(ma10, 3) if ma10 is not None else "",
        "ma20": round(ma20, 3) if ma20 is not None else "",
        "rsi": round(vals.get("rsi"), 1) if vals.get("rsi") is not None else "",
        "macd_dif": round(vals.get("dif"), 3) if vals.get("dif") is not None else "",
        "macd_dea": round(vals.get("dea"), 3) if vals.get("dea") is not None else "",
        "macd_hist": round(vals.get("hist"), 3) if vals.get("hist") is not None else "",
        "bull": bull,
        "bear": bear,
        "net": net,
        "signal": sig,
        "reasons": reasons,
        "delayed": delayed,
        "ts": ts,                 # 接口返回的数据时间(港股为延时时间)
        # 新增指标字段(追加到 CSV 尾部, 向后兼容)
        "k": round(vals["k"], 2) if vals.get("k") is not None else "",
        "d": round(vals["d"], 2) if vals.get("d") is not None else "",
        "j": round(vals["j"], 2) if vals.get("j") is not None else "",
        "boll_mid": round(vals["boll_mid"], 3) if vals.get("boll_mid") is not None else "",
        "boll_up": round(vals["boll_up"], 3) if vals.get("boll_up") is not None else "",
        "boll_low": round(vals["boll_low"], 3) if vals.get("boll_low") is not None else "",
        "volume": round(vals["vol"], 0) if vals.get("vol") is not None else "",
        "vol_ma5": round(vals["vol_ma5"], 0) if vals.get("vol_ma5") is not None else "",
        # 仅运行时, 不入 CSV(供 sparkline)
        "kline": (list(closes[-30:]) + [price]) if closes else [price],
    }
    return text, sig, rec


# ================= ⑥ 引擎: 配置加载 =================
def _normalize(raw) -> List[dict]:
    """把配置原始结构统一成 [{'code','name', ...}, ...]; 透传 chg_alert/swing_alert/support/resistance。"""
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, dict):
        items = raw.get("stocks") or raw.get("Stocks")
        if items is None:
            items = [raw] if "code" in raw else []
    else:
        items = []
    stocks: List[dict] = []
    for it in items:
        if isinstance(it, dict):
            d = {"code": it["code"], "name": it.get("name", it["code"])}
            for k in ("chg_alert", "swing_alert"):
                if k in it:
                    d[k] = it[k]
            # 支撑/压力预警价(可多个价位, 可选)
            if "support" in it:
                d["support"] = [x for x in (_to_float(v) for v in it["support"]) if x is not None]
            if "resistance" in it:
                d["resistance"] = [x for x in (_to_float(v) for v in it["resistance"]) if x is not None]
            stocks.append(d)
        elif isinstance(it, (list, tuple)) and it:
            stocks.append({"code": str(it[0]), "name": str(it[1]) if len(it) > 1 else str(it[0])})
    return stocks


def _load_first(cands: List[str]):
    """在脚本同级目录按候选列表顺序找到第一个存在的文件并解析成 (path, raw_dict)。"""
    for fname in cands:
        path = os.path.join(SCRIPT_DIR, fname)
        if not os.path.exists(path):
            continue
        try:
            if fname.endswith(".toml"):
                if tomllib is None:
                    print(f"[警告] 当前 Python 不支持 tomllib(<3.11), 跳过 {fname}", file=sys.stderr)
                    continue
                with open(path, "rb") as f:
                    raw = tomllib.load(f)
            elif fname.endswith((".yaml", ".yml")):
                import yaml  # 惰性导入, 非硬性依赖
                with open(path, "r", encoding="utf-8") as f:
                    raw = yaml.safe_load(f)
            else:  # json
                with open(path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
            return path, (raw if isinstance(raw, dict) else {})
        except Exception as e:
            print(f"[配置读取失败] {path}: {e}", file=sys.stderr)
    return None


def load_settings() -> dict:
    """读取 [settings]; 合并 config.toml 与 stocks.toml 两处的 [settings], 股票文件优先。
    约定: 全局外观/通知等放 config.toml; 变动阈值/冷却/盘中灵敏等放 stocks.toml。同一键两处都有则 stocks.toml 覆盖。
    """
    merged: dict = {}
    res_cfg = _load_first(SETTINGS_CANDIDATES)
    if res_cfg is not None and isinstance(res_cfg[1].get("settings"), dict):
        merged.update(res_cfg[1]["settings"])
    res_st = _load_first(STOCKS_CANDIDATES)
    if res_st is not None and isinstance(res_st[1].get("settings"), dict):
        merged.update(res_st[1]["settings"])   # 股票文件优先
    return merged


def load_stocks(settings: Optional[dict] = None) -> List[dict]:
    """从股票列表文件读取 [[stocks]]; 找不到回退默认。个股阈值/支撑压力缺省由全局兜底。"""
    res = _load_first(STOCKS_CANDIDATES)
    if res is None:
        return list(DEFAULT_STOCKS)
    stocks = _normalize(res[1])
    if not stocks:
        return list(DEFAULT_STOCKS)
    if settings is None:
        settings = load_settings()
    dca = _to_float(settings.get("chg_alert")) or 0.0
    dsa = _to_float(settings.get("swing_alert")) or 0.0
    for st in stocks:
        st.setdefault("chg_alert", dca)
        st.setdefault("swing_alert", dsa)
    return stocks


# ================= ⑥b 引擎: 运行时增删自选 / 信号变动过滤(纯函数) =================
def parse_add_input(s: str) -> dict:
    """解析『添加自选』输入, 返回 {"code", "name"}。

    支持格式:
      - "code,name"  -> code=code, name=name
      - "code"      -> code=code, name 回退为 code
    校验: code 前缀必须是 hk/sh/sz/us(沿用 market_of 识别); 空串或非法前缀 -> 抛 ValueError。
    """
    if s is None:
        raise ValueError("输入为空")
    s = s.strip()
    if not s:
        raise ValueError("输入为空")
    if "," in s:
        code, _, name = s.partition(",")
        code = code.strip()
        name = name.strip()
    else:
        code = s
        name = ""
    if not code:
        raise ValueError("代码为空")
    if market_of(code) == "":
        raise ValueError(f"非法代码前缀: {code} (仅支持 hk/sh/sz/us)")
    if not name:
        name = code
    return {"code": code, "name": name}


def is_row_visible(filter_on: bool, sig_changed_at: Optional[float],
                   window_sec: float = SIG_CHANGE_WINDOW_SEC) -> bool:
    """功能② 信号行(下半)可见性判定纯函数。

    - filter_on=False: 信号行(下半)永远可见。
    - filter_on=True 且 sig_changed_at 为 None: 信号行(下半)不可见。
    - filter_on=True 且 sig_changed_at 非 None: (now - sig_changed_at) <= window_sec 信号行(下半)可见, 否则不可见。
    注意: 行情行(上半)始终可见, 不受本判定影响。
    """
    if not filter_on:
        return True
    if sig_changed_at is None:
        return False
    return (time.time() - sig_changed_at) <= window_sec


def _extract_toml_prefix(raw: str) -> str:
    """返回第一个 [[stocks]] 之前的所有内容(头注释 + [settings] 段, 含段内注释), 用于保留式重写。"""
    lines = raw.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("[[stocks]]"):
            return "\n".join(lines[:i])
    return raw


def _toml_str(v: Any) -> str:
    """TOML 字符串字面量转义。"""
    return str(v).replace("\\", "\\\\").replace('"', '\\"')


def _toml_num(v: Any) -> str:
    """把数值/布尔格式化为 TOML 字面量(float 整数值写为 x.0 以保类型)。"""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v) if v != int(v) else f"{int(v)}.0"
    return str(v)


def _serialize_stocks(stocks: List[dict]) -> str:
    """把股票列表序列化为 [[stocks]] TOML 文本(含 code/name/support/resistance/chg_alert/swing_alert)。"""
    blocks: List[str] = []
    for st in stocks:
        lines = [
            "[[stocks]]",
            f'code = "{_toml_str(st.get("code", ""))}"',
            f'name = "{_toml_str(st.get("name", st.get("code", "")))}"',
        ]
        if st.get("support"):
            lines.append("support = [" + ", ".join(_toml_num(x) for x in st["support"]) + "]")
        if st.get("resistance"):
            lines.append("resistance = [" + ", ".join(_toml_num(x) for x in st["resistance"]) + "]")
        if st.get("chg_alert") is not None:
            lines.append(f'chg_alert = {_toml_num(st["chg_alert"])}')
        if st.get("swing_alert") is not None:
            lines.append(f'swing_alert = {_toml_num(st["swing_alert"])}')
        blocks.append("\n".join(lines))
    return ("\n\n".join(blocks) + "\n") if blocks else ""


def rewrite_stocks_toml(path: str, add: Optional[dict] = None,
                        remove: Optional[str] = None,
                        reorder: Optional[List[str]] = None) -> List[dict]:
    """功能① 保留式重写 stocks.toml: 保留文件头注释与 [settings] 段, 仅重写 [[stocks]] 区块。

    - add: parse_add_input 返回的 stock dict(或含 code/name 的 dict); 若 code 已存在则忽略(去重)。
    - remove: 要删除的股票 code。
    - reorder: 可选, 给定 code 顺序列表, 在序列化前据此重排 stocks
      (不在列表中的 code 保持原相对序追加于末尾)。add/remove 调用方不传, 保持向后兼容。
    返回最终生效的股票列表(List[dict], 经 _normalize)。文件不存在 / 无法解析 -> 抛异常。
    """
    if tomllib is None:
        raise RuntimeError("需要 tomllib (Python >= 3.11) 才能重写 TOML")
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    parsed = tomllib.loads(raw)
    stocks = _normalize(parsed)
    if remove:
        stocks = [s for s in stocks if s.get("code") != remove]
    if add:
        code = add.get("code")
        if code and not any(s.get("code") == code for s in stocks):
            new_stock = {"code": code, "name": add.get("name", code)}
            if add.get("support"):
                new_stock["support"] = list(add["support"])
            if add.get("resistance"):
                new_stock["resistance"] = list(add["resistance"])
            if add.get("chg_alert") is not None:
                new_stock["chg_alert"] = add["chg_alert"]
            if add.get("swing_alert") is not None:
                new_stock["swing_alert"] = add["swing_alert"]
            stocks.append(new_stock)
    if reorder is not None:
        stocks = _reorder_stocks(stocks, reorder)
    prefix = _extract_toml_prefix(raw)
    body = _serialize_stocks(stocks)
    content = prefix.rstrip("\n")
    if content:
        content += "\n\n"
    content += body
    if not content.endswith("\n"):
        content += "\n"
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return stocks


def remove_stock_from_memory(stocks: List[dict], code: str,
                             bookkeeping: Optional[Dict[str, dict]] = None) -> Optional[dict]:
    """功能① 纯逻辑: 从内存列表 stocks 移除指定 code(原地), 并清理 bookkeeping 各字典的 code 键(原地)。

    - stocks: 股票列表(原地 pop 匹配项)
    - bookkeeping: {name: dict}; 每个 dict 原地 pop(code)(用于清理 quote_frames / sig_frames /
      rows / sig_rows / row_vis / last_sig_change / last_sigs 等, 防止刷新循环访问已销毁 widget)
    返回被移除的 stock dict; 不存在返回 None。本函数不触碰 Tk, 可直接无头单测。
    """
    removed: Optional[dict] = None
    for i, st in enumerate(stocks):
        if st.get("code") == code:
            removed = stocks.pop(i)
            break
    if bookkeeping:
        for d in bookkeeping.values():
            d.pop(code, None)
    return removed


def move_stock_in_order(order: List[str], code: str, direction: str) -> List[str]:
    """在 code 顺序列表 order 中, 把 code 上移一步(direction='up')或下移一步(direction='down')。

    边界: code 不在 order / 已是首元素却上移 / 已是末元素却下移 -> 返回原 order 副本(不变)。
    返回新列表, 不原地修改入参。

    本函数为纯计算, 不触碰 Tk / 文件, 可直接无头单测。
    """
    if code not in order:
        return list(order)
    i = order.index(code)
    if direction == "up":
        if i == 0:
            return list(order)
        j = i - 1
    elif direction == "down":
        if i == len(order) - 1:
            return list(order)
        j = i + 1
    else:
        return list(order)
    new_order = list(order)
    new_order[i], new_order[j] = new_order[j], new_order[i]
    return new_order


def _reorder_stocks(stocks: List[dict], order: List[str]) -> List[dict]:
    """按 order(code 列表)重排 stocks; 不在 order 中的 code 保持原相对序追加于末尾。

    纯函数: 不原地修改入参, 返回新列表。供 rewrite_stocks_toml 的 reorder 参数使用,
    便于无头单测。
    """
    by_code: Dict[str, dict] = {}
    for s in stocks:
        by_code.setdefault(s.get("code"), s)
    ordered = [by_code[c] for c in order if c in by_code]
    seen = {id(s) for s in ordered}
    rest = [s for s in stocks if id(s) not in seen]
    return ordered + rest


# ================= ⑦ 引擎: CSV 落盘 / 回看 / 统计 =================
# 模块级去重缓存: code -> (写入时间戳秒, signal, price)
_LAST_CSV: Dict[str, Tuple[float, Any, Any]] = {}


def signals_path(mode: str, date: Optional[str] = None) -> str:
    """按 csv_mode 决定落盘文件名: daily -> signals_YYYY-MM-DD.csv(可按 date 指定), 其他 -> signals.csv。"""
    if mode == "daily":
        d = date or datetime.now(HKT).strftime("%Y-%m-%d")
        return os.path.join(SCRIPT_DIR, f"signals_{d}.csv")
    return SIGNAL_CSV


def append_signal(rec: dict, path: str, dedup_sec: float = 0.0) -> None:
    """把单次观测追加写入指定 CSV 文件 (首次自动写表头)。
    dedup_sec>0 时: 同 code 且 signal/price 未变且在窗口内则跳过(避免重复落盘)。
    """
    if dedup_sec:
        code = rec.get("code")
        sig = rec.get("signal")
        price = rec.get("price")
        cached = _LAST_CSV.get(code)
        if cached is not None:
            lt, lsig, lprice = cached
            if (time.time() - lt) < dedup_sec and lsig == sig and lprice == price:
                return  # 去重跳过
        _LAST_CSV[code] = (time.time(), sig, price)

    new_file = not os.path.exists(path)
    try:
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            if new_file:
                w.writeheader()
            row = {k: rec.get(k, "") for k in CSV_FIELDS}
            rs = rec.get("reasons", [])
            row["reasons"] = ";".join(rs) if isinstance(rs, list) else rs
            w.writerow(row)
    except Exception as e:
        print(f"[CSV写入失败] {e}", file=sys.stderr)


def review_csv(n: int = 30, path: Optional[str] = None,
               code: Optional[str] = None, date: Optional[str] = None) -> None:
    """回看指定 CSV 最近 n 条, 标出信号变动; 支持按 code / date 过滤。"""
    path = path or SIGNAL_CSV
    if not os.path.exists(path):
        print(f"暂无历史记录: {path}")
        return
    rows: List[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if code and row.get("code") != code:
                continue
            if date and not str(row.get("datetime", "")).startswith(date):
                continue
            rows.append(row)
    if not rows:
        print("CSV 为空或无匹配记录。")
        return
    rows = rows[-n:]
    print(f"=== 历史信号回看 (最近 {len(rows)} 条, {path}) ===")
    cur: Dict[str, Any] = {}
    for r in rows:
        c = r["code"]
        changed = cur.get(c) is not None and cur.get(c) != r["signal"]
        mark = "⚡" if changed else "  "
        print(f"{mark} {r['datetime']}  {r['name']}({c})  价{r['price']} {fmt_chg(r['chg_pct'])}  {r['signal']}")
        cur[c] = r["signal"]


def stats_csv(path: str, code: Optional[str] = None, date: Optional[str] = None) -> None:
    """当日信号聚合统计: 各档位出现次数、最高/最低 net、信号切换次数; 支持 code/date 过滤。"""
    if not os.path.exists(path):
        print(f"暂无历史记录: {path}")
        return
    rows: List[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if code and row.get("code") != code:
                continue
            if date and not str(row.get("datetime", "")).startswith(date):
                continue
            rows.append(row)
    if not rows:
        print("CSV 为空或无匹配记录。")
        return
    cnt = Counter(r["signal"] for r in rows)
    nets = [x for x in (_to_float(r.get("net")) for r in rows) if x is not None]
    max_net = max(nets) if nets else None
    min_net = min(nets) if nets else None
    switches = 0
    last: Dict[str, Any] = {}
    for r in rows:
        c = r["code"]
        s = r["signal"]
        if c in last and last[c] != s:
            switches += 1
        last[c] = s
    print(f"=== 当日信号聚合 ({path}) ===")
    print(f"总记录数: {len(rows)}")
    print("各档位出现次数:")
    for lvl in ["🔴 买入(偏强)", "🟠 轻仓/偏多", "⚪ 持有/观望", "🔵 减仓/偏空", "🟢 卖出(偏弱)"]:
        print(f"  {lvl}: {cnt.get(lvl, 0)}")
    print(f"最高 net: {max_net}   最低 net: {min_net}")
    print(f"信号切换次数: {switches}")


# ================= ⑧ 通知 (Notifier, 跨平台) =================
def _applescript_escape(s: str) -> str:
    s = str(s)
    if len(s) > 200:
        s = s[:197] + "..."
    return s.replace('"', '\\"')


def notify_mac(msg: str, sound: bool = False) -> None:
    """macOS 系统通知(单行消息, 无粗体标题, 更低调)。非 macOS 静默跳过。"""
    if sys.platform != "darwin":
        return
    try:
        scpt = f'display notification "{_applescript_escape(msg)}"'
        if sound:
            scpt += ' sound name "Glass"'
        subprocess.run(["osascript", "-e", scpt], check=False, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


class Notifier:
    """跨平台通知分发: mac→AppleScript; linux→notify-send(subprocess); win→winsound.Beep + 闪烁(flash_fn)。
    Windows 闪烁必须经 flash_fn(GUI 注入, 内部 root.after 回主线程), 本类不持有 tk 对象。
    """

    def __init__(self, enabled: bool = False, sound: bool = False,
                 flash_fn: Optional[Callable[[], None]] = None):
        self.enabled = enabled     # settings.notify
        self.sound = sound         # settings.notify_sound
        self.flash_fn = flash_fn   # Windows 闪烁: 由 GUI 注入

    def notify(self, msg: str) -> None:
        if not self.enabled:
            return
        if sys.platform == "darwin":
            notify_mac(msg, self.sound)
        elif sys.platform.startswith("linux"):
            self._notify_linux(msg)
        else:
            self._notify_windows(msg)

    def _notify_linux(self, msg: str) -> None:
        try:
            subprocess.run(["notify-send", "股票HUD", msg], check=False, timeout=5,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass

    def _notify_windows(self, msg: str) -> None:
        # 系统蜂鸣(后台线程直跑, 阻塞 ~0.2s 可接受)
        try:
            import winsound
            winsound.Beep(880, 200)
        except Exception:
            pass
        # 闪烁必须经 flash_fn 回到 GUI 主线程(由 GUI 注入)
        if self.flash_fn is not None:
            try:
                self.flash_fn()
            except Exception:
                pass


# ================= ⑨ 样式 (build_style / detect_system_theme) =================
ALPHA_DEFAULT = 0.94     # 浮窗默认透明度(0~1, 1=完全不透明)

LIGHT_PALETTE = {"bg": "#f7f7f7", "fg": "#1a1a1a", "fg_dim": "#6b7280", "up": "#d93025",
                 "down": "#188038", "flat": "#8a8f98", "dl": "#b06a2c", "header": "#e9e9ec",
                 "sep": "#d8dadf"}
DARK_PALETTE = {"bg": "#1e1e22", "fg": "#e8e8ea", "fg_dim": "#9aa0a6", "up": "#ff6b5e",
                "down": "#4cc38a", "flat": "#7b818a", "dl": "#d99a4e", "header": "#2a2a30",
                "sep": "#3a3a42"}

# 信号档位 -> 颜色(左侧色点, 暗/亮均可用)
SIG_COLORS = {
    "🔴 买入(偏强)": "#d93025",
    "🟠 轻仓/偏多": "#e8730a",
    "⚪ 持有/观望": "#8a8f98",
    "🔵 减仓/偏空": "#1a66c0",
    "🟢 卖出(偏弱)": "#188038",
}
# 信号档位 -> 去掉 emoji 的中短名(下半部分/通知展示用)
SIG_SHORT = {k: (k.split(" ", 1)[1] if " " in k else k) for k in SIG_COLORS}
SEP_COLOR = "#d8dadf"           # 分界横线颜色(浅灰)


def _hex_color(s: Any, default: str) -> str:
    return s if isinstance(s, str) and re.fullmatch(r"#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})", s) else default


def desaturate(hex_str: str, amount: float) -> str:
    """按比例去饱和(去色), 用于「界面颜色灰色程度」配置。

    - hex_str: '#rrggbb' 或 '#rgb'; 非法输入直接返回原串(不抛异常)。
    - amount:  0.0~1.0, 越界自动 clamp; 0=原色逐字不变, 1=纯灰阶(三通道相等)。
    - 灰度用 Rec.601 luma: g = round(0.299*r + 0.587*g + 0.114*b)。
    - 逐通道混合: out = round(orig*(1-amount) + gray*amount)。
    - 返回 '#rrggbb'(两位小写十六进制, 不足补零)。
    """
    if not isinstance(hex_str, str):
        return hex_str
    m6 = re.fullmatch(r"#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})", hex_str)
    if m6:
        r, g, b = (int(m6.group(i), 16) for i in (1, 2, 3))
    else:
        m3 = re.fullmatch(r"#([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])", hex_str)
        if not m3:
            return hex_str
        r = int(m3.group(1) * 2, 16)
        g = int(m3.group(2) * 2, 16)
        b = int(m3.group(3) * 2, 16)
    amount = max(0.0, min(1.0, float(amount)))
    gray = round(0.299 * r + 0.587 * g + 0.114 * b)
    out_r = round(r * (1 - amount) + gray * amount)
    out_g = round(g * (1 - amount) + gray * amount)
    out_b = round(b * (1 - amount) + gray * amount)
    return "#{:02x}{:02x}{:02x}".format(out_r, out_g, out_b)


def detect_system_theme() -> str:
    """探测系统外观: 'light' | 'dark'。mac 读 defaults; win 读注册表 AppsUseLightTheme; 其它回退 light。"""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["defaults", "read", "Apple Global Domain", "AppleInterfaceStyle"],
                                 capture_output=True, text=True, timeout=5)
            if out.returncode == 0 and "Dark" in out.stdout:
                return "dark"
            return "light"
        if sys.platform.startswith("win"):
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
                val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return "light" if val == 1 else "dark"   # 0 = dark, 1 = light
    except Exception:
        pass
    return "light"


def build_style(settings: dict) -> dict:
    """根据 settings 生成样式集(亮/暗/自动 + 字体 + 信号色)。"""
    theme = settings.get("float_theme", "light")
    if theme == "auto":
        theme = detect_system_theme()
    pal = DARK_PALETTE if theme == "dark" else LIGHT_PALETTE
    font = settings.get("float_font") or "Menlo"
    try:
        size = int(settings.get("float_font_size") or 7)
    except (TypeError, ValueError):
        size = 7
    size = max(5, size)
    # 界面颜色灰色程度(强调色去饱和); 0.0=原色, 1.0=纯灰阶; 越界/缺省兜底 0.0
    grayness = max(0.0, min(1.0, float(settings.get("grayness") or 0.0)))
    sig_colors = {}
    for k, m in {
        "🔴 买入(偏强)": "sig_buy",
        "🟠 轻仓/偏多": "sig_long",
        "⚪ 持有/观望": "sig_hold",
        "🔵 减仓/偏空": "sig_short",
        "🟢 卖出(偏弱)": "sig_sell",
    }.items():
        sig_colors[k] = desaturate(_hex_color(settings.get(m), SIG_COLORS[k]), grayness)
    return {
        "bg": pal["bg"], "fg": pal["fg"], "fg_dim": pal["fg_dim"],
        "up": desaturate(_hex_color(settings.get("float_up_color"), pal["up"]), grayness),
        "down": desaturate(_hex_color(settings.get("float_down_color"), pal["down"]), grayness),
        "flat": pal["flat"], "dl": pal["dl"], "header": pal["header"], "sep": pal["sep"],
        "sig_colors": sig_colors,
        "FONT": (font, size), "FONT_SM": (font, max(5, size - 1)), "ROW_H": 16,
    }


# ================= ⑩ GUI (Hud / run_hud) =================
def _draw_sparkline(canvas: "tk.Canvas", kline: List[float], price: Any,
                    prev_close: Any, style: dict) -> None:
    """在 Canvas 上画折线(sparkline); 用 rec.kline(日K近30根+实时价), 不在 refresh 内联网。"""
    try:
        w = canvas.winfo_width() or 38
        h = canvas.winfo_height() or 14
        canvas.delete("all")
        vals = [v for v in kline if isinstance(v, (int, float))]
        if len(vals) < 2:
            return
        lo, hi = min(vals), max(vals)
        rng = hi - lo
        if rng <= 0:
            rng = 1.0
        pad = 1
        n = len(vals)
        col = style["up"] if (isinstance(price, (int, float)) and price >= (prev_close or price)) else style["down"]
        coords: List[float] = []
        for i, v in enumerate(vals):
            x = pad + i * (w - 2 * pad) / (n - 1)
            y = h - pad - (v - lo) / rng * (h - 2 * pad)
            coords.append(x)
            coords.append(y)
        canvas.create_line(*coords, fill=col, width=1)
    except Exception:
        pass


# sparkline 默认展开宽度(像素); 隐藏时折叠到 0 宽度(不依赖 pack_forget, 杜绝第二次隐藏失效)
SPARK_W = 38


def apply_sparkline_state(spark: "tk.Canvas", on: bool,
                          snap: Optional[dict], style: dict) -> None:
    """按开关显示/隐藏单只 sparkline; 幂等、可重入、健壮。

    采用「清空白板 + 折叠宽度」而非 pack_forget/re-pack:
      - on:  恢复宽度(SPARK_W)并按快照重绘(若快照含 kline); 刷新循环会持续重绘。
      - off: 先清空已有折线(delete), 再把宽度折叠到 0 —— 几何上收起且关→开→关→开序列稳定。

    该纯函数不依赖 Tk 主线程, 可直接无头单测(用桩对象记录 config/delete 调用)。
    """
    if on:
        spark.config(width=SPARK_W)
        if isinstance(snap, dict) and snap.get("kline"):
            _draw_sparkline(spark, snap["kline"], snap.get("price"),
                            snap.get("prev_close"), style)
    else:
        # 先清空折线, 再折叠宽度 -> 稳定隐藏(避免 pack_forget/re-pack 脆弱切换)
        spark.delete("all")
        spark.config(width=0)


def apply_sig_visibility(sf_, visible, sig_pack):
    """仅控制信号行(下半)显隐; 行情行(上半)由调用方保证始终可见, 本函数不碰。

    仅在真实状态变化时操作 pack, 操作后由调用处 update_idletasks 刷新几何。
    幂等、可重入: 重复调用同状态不会产生多余的 pack/pack_forget。
    该纯函数不依赖 Tk 主线程, 可直接无头单测(用桩对象记录 pack/pack_forget 调用)。
    """
    if sf_ is None:
        return
    if visible:
        if not sf_.winfo_ismapped():
            sf_.pack(**sig_pack)
    else:
        if sf_.winfo_ismapped():
            sf_.pack_forget()


def refresh_requires_ban_warning(nxt_sec: int) -> bool:
    """切到 1 秒刷新时需弹确认框警告数据源可能被限流/封禁。

    Args:
        nxt_sec: 即将切换到的刷新周期(秒)。

    Returns:
        仅当切到 1 秒刷新时返回 True(需警告), 其余周期返回 False。
    """
    return nxt_sec == 1


def run_hud(stocks: List[dict], settings: dict, log_fn: Optional[Callable[[dict], None]] = None,
            notifier: Optional[Notifier] = None, cooldown: float = 0.0,
            stocks_path: Optional[str] = None) -> None:
    """构建并运行浮窗; 后台线程按 refresh_sec 取数, 满足省流规则时写CSV/弹通知。
    新增: 暂停/频率控件、每行 sparkline、暗色样式、Windows 闪烁(flash_fn 注入 notifier)。
    """
    style = build_style(settings)
    # 涨跌幅颜色(浮窗专用)
    up_color = style["up"]
    down_color = style["down"]
    sig_colors = style["sig_colors"]
    # 透明度(0~1, 非法/越界回退默认)
    _a = settings.get("float_alpha", ALPHA_DEFAULT)
    alpha = _a if isinstance(_a, (int, float)) and 0 < _a <= 1 else ALPHA_DEFAULT
    # 刷新周期(默认5s; 频率控件在 1/3/5/10 间循环)
    try:
        refresh_sec = max(1, int(_to_float(settings.get("refresh_sec")) or 1))
    except (TypeError, ValueError):
        refresh_sec = 1

    root = tk.Tk()
    root.title("")
    root.overrideredirect(True)           # 去标题栏/边框 -> 浮动
    root.attributes("-topmost", True)     # 置顶
    root.attributes("-alpha", alpha)       # 透明度, 由 float_alpha 配置控制
    root.configure(bg=style["bg"])
    root.resizable(False, False)

    # ---- 顶部拖拽条 ----
    header = tk.Frame(root, bg=style["header"], height=14)
    header.pack(fill="x")
    htitle = tk.Label(header, text="  行情", bg=style["header"], fg=style["fg_dim"],
                      font=style["FONT_SM"], anchor="w")
    htitle.pack(side="left", padx=(3, 0))

    def _quit():
        """静默直接退出, 不弹任何确认框。"""
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", _quit)

    close_btn = tk.Label(header, text=" × ", bg=style["header"], fg=style["fg_dim"],
                         font=style["FONT_SM"], cursor="hand2")
    close_btn.pack(side="right", padx=(0, 3))
    close_btn.bind("<Button-1>", lambda e: _quit())

    # 频率控件(1/3/5s 循环)
    freq_btn = tk.Label(header, text=f" {refresh_sec}s ", bg=style["header"], fg=style["fg_dim"],
                        font=style["FONT_SM"], cursor="hand2")
    freq_btn.pack(side="right", padx=(0, 2))
    freq_btn.bind("<Button-1>", lambda e: _cycle_freq())

    # 暂停/继续按钮
    pause_btn = tk.Label(header, text=" ⏸ ", bg=style["header"], fg=style["fg_dim"],
                         font=style["FONT_SM"], cursor="hand2")
    pause_btn.pack(side="right", padx=(0, 2))
    pause_btn.bind("<Button-1>", lambda e: _toggle_pause())

    # 运行时增删自选: ＋ 按钮(功能①)
    add_btn = tk.Label(header, text=" ＋ ", bg=style["header"], fg=style["fg_dim"],
                       font=style["FONT_SM"], cursor="hand2")
    add_btn.pack(side="right", padx=(0, 2))
    add_btn.bind("<Button-1>", lambda e: _add_stock_dialog())

    # 只看信号变动股过滤开关: 🔎 按钮(功能②)
    filter_btn = tk.Label(header, text=" 🔍 ", bg=style["header"], fg=style["fg_dim"],
                          font=style["FONT_SM"], cursor="hand2")
    filter_btn.pack(side="right", padx=(0, 2))
    filter_btn.bind("<Button-1>", lambda e: _toggle_filter())

    # 迷你 sparkline 走势图显示/隐藏开关: 📈 按钮(功能③)
    spark_btn = tk.Label(header, text=" 📈 ", bg=style["header"], fg=style["fg_dim"],
                         font=style["FONT_SM"], cursor="hand2")
    spark_btn.pack(side="right", padx=(0, 2))
    spark_btn.bind("<Button-1>", lambda e: _toggle_sparkline())

    drag = {"x": 0, "y": 0}

    def start_drag(e):
        drag["x"] = e.x_root - root.winfo_x()
        drag["y"] = e.y_root - root.winfo_y()

    def do_drag(e):
        root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")

    for w in (header, htitle):
        w.bind("<ButtonPress-1>", start_drag)
        w.bind("<B1-Motion>", do_drag)

    # ---- 行情行(含 sparkline) ----
    body = tk.Frame(root, bg=style["bg"])
    body.pack(fill="x")
    rows: Dict[str, tuple] = {}
    quote_frames: Dict[str, "tk.Frame"] = {}
    sig_frames: Dict[str, "tk.Frame"] = {}
    row_vis: Dict[str, bool] = {}
    last_sig_change: Dict[str, float] = {}
    # 手动排序: 记录每行行情行右侧的上移/下移箭头部件, 用于边界灰显
    move_btns: Dict[str, tuple] = {}
    ui = {"filter_on": bool(settings.get("filter_on", True)), "show_sparkline": bool(settings.get("show_sparkline", False))}
    QUOTE_PACK = dict(fill="x", padx=3, pady=1)
    SIG_PACK = dict(fill="x", padx=5, pady=(0, 1))

    # ---- 运行时增删自选 / 信号变动过滤: 行构建与 UI 回调(功能①②) ----
    # 以下嵌套函数引用的 body/sigpane/status/filter_btn 等均在 run_hud 后续创建,
    # 调用发生在运行时(构建循环或用户交互), 闭包按调用时解析, 故此处定义安全。
    def _build_quote_row(st):
        """构建一只股票的行情行(含 sparkline); 存入 rows/quote_frames/row_vis, 绑定右键删除菜单。"""
        code = st["code"]
        f = tk.Frame(body, bg=style["bg"], height=style["ROW_H"])
        f.pack(**QUOTE_PACK)
        sig_l = tk.Label(f, text="●", bg=style["bg"], fg=style["flat"], font=style["FONT"], width=2, anchor="w")
        sig_l.pack(side="left")
        name_l = tk.Label(f, text=st["name"], bg=style["bg"], fg=style["fg"], font=style["FONT"], width=6, anchor="w")
        name_l.pack(side="left")
        price_l = tk.Label(f, text="--", bg=style["bg"], fg=style["fg"], font=style["FONT"], width=5, anchor="e")
        price_l.pack(side="left", padx=(0, 4))
        chg_l = tk.Label(f, text="", bg=style["bg"], fg=style["flat"], font=style["FONT"], width=6, anchor="e")
        chg_l.pack(side="left")
        dl_l = tk.Label(f, text="", bg=style["bg"], fg=style["dl"], font=style["FONT_SM"], width=3, anchor="w")
        dl_l.pack(side="left")
        spark = tk.Canvas(f, width=38, height=14, bg=style["bg"], highlightthickness=0)
        # 可见删除按钮(功能①): 先 pack 故位于最右角, 独立于 sparkline,
        # spark 折叠(width=0)时也不受影响; 左键直接删除。
        del_btn = tk.Label(f, text=" 🗑 ", bg=style["bg"], fg=style["fg_dim"],
                           font=style["FONT_SM"], cursor="hand2")
        del_btn.bind("<Button-1>", lambda e, c=code: _confirm_remove(c))
        del_btn.pack(side="right", padx=(0, 2))
        # 手动排序: 上移/下移箭头(位于删除按钮左侧, 紧贴其左)
        up_btn = tk.Label(f, text=" ▲ ", bg=style["bg"], fg=style["fg_dim"],
                          font=style["FONT_SM"], cursor="hand2")
        up_btn.pack(side="right", padx=(0, 1))
        up_btn.bind("<Button-1>", lambda e, c=code: _move_stock(c, "up"))
        down_btn = tk.Label(f, text=" ▼ ", bg=style["bg"], fg=style["fg_dim"],
                            font=style["FONT_SM"], cursor="hand2")
        down_btn.pack(side="right", padx=(0, 1))
        down_btn.bind("<Button-1>", lambda e, c=code: _move_stock(c, "down"))
        move_btns[code] = (up_btn, down_btn)
        spark.pack(side="right", padx=(2, 0))
        rows[code] = (sig_l, name_l, price_l, chg_l, dl_l, spark)
        quote_frames[code] = f
        row_vis[code] = True
        # 右键菜单: 删除该自选(功能①)——保留, 非 macOS 用户仍可用
        f.bind("<Button-3>", lambda e, c=code: _show_remove_menu(e, c))

    def _refresh_move_buttons():
        """刷新上移/下移箭头灰显: 首行 up_btn / 末行 down_btn 置为禁用色(与背景同色)。

        纯 UI 态同步, 不触碰内存顺序与文件; 每轮手动排序或构建完成后调用一次即可。
        """
        order = [s["code"] for s in stocks]
        n = len(order)
        for i, c in enumerate(order):
            btns = move_btns.get(c)
            if not btns:
                continue
            up_btn, down_btn = btns
            up_btn.config(fg=(style["bg"] if i == 0 else style["fg_dim"]))
            down_btn.config(fg=(style["bg"] if i == n - 1 else style["fg_dim"]))

    def _move_stock(code, direction):
        """上移/下移一只股票(自定义手动排序)。

        流程: 边界检查 -> 纯函数 move_stock_in_order 计算新顺序 -> 重建内存 stocks
        -> 按新顺序重排 UI 显示(pack 追加到父容器末尾) -> 协调过滤可见性
        -> 刷新箭头灰显 -> 回写 stocks.toml(reorder)。仅内存模式(stocks_path 为空)则跳过写回。
        """
        # 边界: code 不存在直接返回
        if not any(s["code"] == code for s in stocks):
            return
        idx = next(i for i, s in enumerate(stocks) if s["code"] == code)
        n = len(stocks)
        if direction == "up" and idx == 0:
            return
        if direction == "down" and idx == n - 1:
            return
        new_order = move_stock_in_order([s["code"] for s in stocks], code, direction)
        # 顺序未变(理论上已在上一步拦截, 双保险)则不操作
        if new_order == [s["code"] for s in stocks]:
            return
        # 按新顺序重建内存 stocks(保留各 dict 原对象; 原地修改以便所有闭包共享同一列表)
        by_code = {s["code"]: s for s in stocks}
        stocks[:] = [by_code[c] for c in new_order]
        # 重排 UI 显示顺序: 先全部 pack_forget 再按 new_order 依次 pack(确定性重排,
        # 规避 Tk 对已 pack 控件重复 pack 不改变顺序的坑)
        for c in new_order:
            qf = quote_frames.get(c)
            if qf is not None:
                qf.pack_forget()
            sf_ = sig_frames.get(c)
            if sf_ is not None:
                sf_.pack_forget()
        for c in new_order:
            qf = quote_frames.get(c)
            if qf is not None:
                qf.pack(**QUOTE_PACK)
            sf_ = sig_frames.get(c)
            if sf_ is not None:
                sf_.pack(**SIG_PACK)
        # 上面的 pack 会让原本隐藏的 sig 行重新显示, 需按 row_vis 还原过滤可见性(无论过滤开/关)
        for c in new_order:
            _apply_visibility(c, row_vis.get(c, True))
        root.update_idletasks()
        # 更新箭头灰显(首行 up / 末行 down 禁用)
        _refresh_move_buttons()
        # 持久化: 把新顺序写回 stocks.toml
        if stocks_path:
            try:
                rewrite_stocks_toml(stocks_path, reorder=new_order)
            except Exception as e:
                status.config(text=f"写入 stocks.toml 失败: {e}")

    def _build_sig_row(st):
        """构建一只股票的下方信号提示行; 存入 sig_rows/sig_frames。"""
        code = st["code"]
        sf = tk.Frame(sigpane, bg=style["bg"])
        sf.pack(**SIG_PACK)
        line1 = tk.Frame(sf, bg=style["bg"])
        line1.pack(fill="x")
        sdot = tk.Label(line1, text="●", bg=style["bg"], fg=style["flat"], font=style["FONT"], width=2, anchor="w")
        sdot.pack(side="left")
        snm = tk.Label(line1, text=st["name"], bg=style["bg"], fg=style["fg"], font=style["FONT_SM"], width=5, anchor="w")
        snm.pack(side="left")
        slv = tk.Label(line1, text="—", bg=style["bg"], fg=style["flat"], font=style["FONT_SM"], width=9, anchor="w")
        slv.pack(side="left")
        sbb = tk.Label(line1, text="", bg=style["bg"], fg=style["fg_dim"], font=style["FONT_SM"], anchor="e")
        sbb.pack(side="right")
        sig_rows[code] = (sdot, slv, sbb)
        sig_frames[code] = sf

    def _apply_visibility(code, visible):
        """功能②: 仅控制信号行(下半)显隐; 行情行(上半)始终可见, 不受过滤影响。

        委托模块级纯函数 apply_sig_visibility 操作 pack, 仅在真实状态变化时生效,
        操作后刷新几何, 杜绝 pack_forget/re-pack 来回切换的脆弱性(与 sparkline 稳健思路一致)。
        """
        # 行情行(上半)始终可见: 构建时一次性 pack, 仅 _remove_stock 删除时才销毁,
        # 本函数不再对上半行情行做任何 pack/pack_forget。
        sf_ = sig_frames.get(code)
        apply_sig_visibility(sf_, visible, SIG_PACK)
        # 刷新几何: 确保 pack_forget 立即生效, 不被后续 pack 覆盖而「粘住」。
        # 刷新循环已用 row_vis != visible 守卫, 仅在真实状态变化时才调用本函数,
        # 故高频刷新不会每次 flush; _toggle_filter 遍历各调一次, 开销可接受。
        root.update_idletasks()

    def _show_remove_menu(event, code):
        """右键弹出删除菜单(功能①)。"""
        menu = tk.Menu(root, tearoff=0)
        menu.add_command(label=f"删除 {code}", command=lambda: _confirm_remove(code))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _toggle_filter():
        """切换『只看信号变动股』过滤(功能②)。"""
        ui["filter_on"] = not ui["filter_on"]
        on = ui["filter_on"]
        filter_btn.config(text="🔎" if on else "🔍",
                          fg=(style["fg"] if on else style["fg_dim"]))
        status.config(text=("🔎 只看信号变动股" if on else "● 实时"))
        # 立即重算所有行可见性
        with lock:
            sig_changes = dict(last_sig_change)
        for st in stocks:
            c = st["code"]
            vis = is_row_visible(on, sig_changes.get(c), SIG_CHANGE_WINDOW_SEC)
            _apply_visibility(c, vis)
            row_vis[c] = vis

    def _apply_sparkline(on):
        """按开关应用 sparkline 显示/隐藏(功能③, 主线程执行)。"""
        spark_btn.config(text="📈" if on else "📉",
                         fg=(style["fg"] if on else style["fg_dim"]))
        status.config(text=("📈 走势图开" if on else "📉 走势图关"))
        with lock:
            snap = dict(data)
        for st in stocks:
            code = st["code"]
            spark = rows[code][5]
            # 幂等、可重入的显示/隐藏(折叠宽度而非 pack_forget, 杜绝第二次隐藏失效)
            apply_sparkline_state(spark, on, snap.get(code), style)

    def _toggle_sparkline():
        """切换迷你 sparkline 走势图显示(功能③); root.after(0) 回主线程执行 UI 操作。"""
        ui["show_sparkline"] = not ui["show_sparkline"]
        root.after(0, _apply_sparkline, ui["show_sparkline"])

    def _add_stock_dialog():
        """弹输入对话框解析并添加自选(功能①)。"""
        if simpledialog is None:
            return
        s = simpledialog.askstring(
            "添加自选", "输入 code,name (如 sh600519,贵州茅台)\n前缀需为 hk/sh/sz/us")
        if not s:
            return
        try:
            parsed = parse_add_input(s)
        except ValueError as e:
            status.config(text=f"添加失败: {e}")
            return
        _add_stock(parsed)

    def _add_stock(parsed):
        """把解析后的股票加入内存列表 + GUI + 回写 stocks.toml(功能①)。"""
        code = parsed["code"]
        if any(st["code"] == code for st in stocks):
            status.config(text=f"{code} 已在自选, 忽略")
            return
        stocks.append(parsed)
        warm_klines([parsed])
        _build_quote_row(parsed)
        _build_sig_row(parsed)
        last_sig_change[code] = time.time()   # 立即可见(无论过滤是否开启)
        row_vis[code] = True
        if stocks_path:
            try:
                rewrite_stocks_toml(stocks_path, add=parsed)
            except Exception as e:
                status.config(text=f"写入 stocks.toml 失败: {e}")
                return
        status.config(text=f"已添加 {parsed['name']} ({code})")

    def _confirm_remove(code):
        """删除前弹出确认框(功能①): 点『是/Yes』才执行 _remove_stock, 否则取消(不删)。

        仅做一层确认包装, 不改动 _remove_stock 内部逻辑; 无头测试不直接覆盖本函数。
        """
        st = next((s for s in stocks if s.get("code") == code), None)
        name = st.get("name", code) if st else code
        if messagebox.askyesno("删除自选", f"确定删除 {name}（{code}）？", parent=root):
            _remove_stock(code)

    def _remove_stock(code):
        """从内存列表 + GUI + stocks.toml 删除该自选(功能①)。"""
        if not any(st["code"] == code for st in stocks):
            return
        # 先取出待销毁的 frame(字典清理会 pop 这两个键, 需提前拿引用)
        qf = quote_frames.get(code)
        sf_ = sig_frames.get(code)
        # 内存列表 + 全部 bookkeeping 字典(含 last_sigs)原地清理,
        # 杜绝刷新循环 / worker 访问已销毁 widget 或残留过期状态
        remove_stock_from_memory(stocks, code, {
            "quote_frames": quote_frames,
            "sig_frames": sig_frames,
            "rows": rows,
            "sig_rows": sig_rows,
            "row_vis": row_vis,
            "last_sig_change": last_sig_change,
            "last_sigs": last_sigs,
        })
        if qf is not None:
            qf.destroy()
        if sf_ is not None:
            sf_.destroy()
        if stocks_path:
            try:
                rewrite_stocks_toml(stocks_path, remove=code)
            except Exception as e:
                status.config(text=f"写入 stocks.toml 失败: {e}")
                return
        status.config(text=f"已删除 {code}")

    for st in stocks:
        _build_quote_row(st)
    # 初始同步上移/下移箭头灰显(首行 up / 末行 down 禁用)
    _refresh_move_buttons()

    # ---- 分隔线 + 下半部分: 信号提示 ----
    sep = tk.Frame(root, bg=style["sep"], height=1)
    sep.pack(fill="x", padx=5, pady=2)
    sighead = tk.Label(root, text="信号提示", bg=style["bg"], fg=style["fg_dim"],
                       font=style["FONT_SM"], anchor="w")
    sighead.pack(fill="x", padx=5, pady=(0, 1))

    sigpane = tk.Frame(root, bg=style["bg"])
    sigpane.pack(fill="x")
    sig_rows: Dict[str, tuple] = {}
    for st in stocks:
        _build_sig_row(st)

    # ---- 底部状态 ----
    status = tk.Label(root, text="连接中…", bg=style["bg"], fg=style["fg_dim"], font=style["FONT_SM"], anchor="w")
    status.pack(fill="x", padx=3, pady=(0, 1))

    # 初始定位到右上角(为 sparkline 留出更多空间)
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    root.geometry(f"+{max(0, sw - 205)}+20")

    # ---- 数据层 ----
    data: Dict[str, dict] = {}                 # code -> rec(最近一次成功)
    last_sigs: Dict[str, dict] = {}            # code -> {sig, 阈值状态, last_price, ...}
    lock = threading.Lock()
    # 共享状态(后台线程读/主线程写; 单键原子操作在 CPython 下安全)
    state = {"paused": False, "refresh_sec": refresh_sec}

    def _toggle_pause():
        state["paused"] = not state["paused"]
        pause_btn.config(text=" ▶ " if state["paused"] else " ⏸ ")
        if state["paused"]:
            status.config(text="⏸ 已暂停")

    def _cycle_freq():
        cur = state["refresh_sec"]
        nxt = {1: 3, 3: 5, 5: 10, 10: 1}.get(cur, 1)
        # 切到 1 秒刷新频率极可能触发免费源限流/封禁, 需用户确认
        if refresh_requires_ban_warning(nxt):
            ok = messagebox.askyesno(
                "频率警告",
                "1 秒刷新会非常频繁地请求行情数据源，可能被限流甚至封禁 IP。\n\n"
                "确定仍要使用 1 秒刷新吗？",
                parent=root,
            )
            if not ok:
                return  # 用户取消, 保持当前频率不变
        state["refresh_sec"] = nxt
        freq_btn.config(text=f" {nxt}s ")

    # ---- Windows 闪烁(经 root.after 回主线程, 由 notifier 注入) ----
    _flash = {"n": 0, "orig": alpha}

    def _flash_window():
        try:
            s = _flash
            s["n"] += 1
            if s["n"] > 6:                       # 约 6*160ms ≈ 1s
                root.attributes("-alpha", s["orig"])
                s["n"] = 0
                return
            root.attributes("-alpha", 1.0 if (s["n"] % 2 == 0) else max(0.3, s["orig"] - 0.4))
            root.after(160, _flash_window)
        except Exception:
            pass

    def flash_fn():
        root.after(0, _flash_window)

    if notifier is not None:
        notifier.flash_fn = flash_fn

    def worker():
        while True:
            if state.get("paused"):
                time.sleep(state.get("refresh_sec", 1))
                continue
            # 运行时增删: 每轮快照 stocks, 支持新增/删除自选(功能①)
            cur_stocks = list(stocks)
            codes = [s["code"] for s in cur_stocks]
            try:
                rt_map = fetch_realtime_batch(codes)
            except Exception:
                rt_map = {}
            new: Dict[str, dict] = {}
            now_t = time.time()
            cd = cooldown * 60.0
            for st in cur_stocks:
                code = st["code"]
                rt = rt_map.get(code)
                if isinstance(rt, Exception) or rt is None:
                    continue
                prev = last_sigs.get(code)
                prev_sig = prev.get("sig") if isinstance(prev, dict) else None
                try:
                    _, sig, rec = monitor(st, prev_sig, rt=rt, settings=settings)
                except Exception:
                    continue
                new[code] = rec
                price = rec.get("price")

                # ---- 通知 + CSV 触发(省流规则, 与原终端监控一致) ----
                st_ca = _to_float(st.get("chg_alert")) or 0.0
                st_sa = _to_float(st.get("swing_alert")) or 0.0
                cv = _to_float(rec.get("chg_pct"))
                sv = _to_float(rec.get("swing_pct"))
                ca = st_ca > 0 and cv is not None and abs(cv) >= st_ca
                sa = st_sa > 0 and sv is not None and sv >= st_sa
                prev_ca = prev.get("chg_alert", False) if isinstance(prev, dict) else False
                prev_sa = prev.get("swing_alert", False) if isinstance(prev, dict) else False
                ca_cool = (cd > 0 and isinstance(prev, dict)
                           and prev.get("chg_alert_at") is not None
                           and (now_t - prev.get("chg_alert_at")) < cd)
                sa_cool = (cd > 0 and isinstance(prev, dict)
                           and prev.get("swing_alert_at") is not None
                           and (now_t - prev.get("swing_alert_at")) < cd)
                cross_ca = (not prev_ca and ca) and not ca_cool
                cross_sa = (not prev_sa and sa) and not sa_cool
                # 信号档位变动也走冷却: 价格卡在指标边界反复横跳时, 同一只冷却期内只提示一次
                sig_changed = (prev_sig != sig)
                if sig_changed:
                    last_sig_change[code] = time.time()  # 功能②: 记录信号变动时间戳
                sig_cool = (cd > 0 and isinstance(prev, dict)
                            and prev.get("sig_alert_at") is not None
                            and (now_t - prev.get("sig_alert_at")) < cd)
                notify_on_sig = sig_changed and not sig_cool

                # ---- 支撑/压力穿越检测(复用 cross + 冷却) ----
                last_price = prev.get("last_price") if isinstance(prev, dict) else None
                sr_triggered: List[str] = []
                sr_cool = (cd > 0 and isinstance(prev, dict)
                           and prev.get("sr_alert_at") is not None
                           and (now_t - prev.get("sr_alert_at")) < cd)
                if not sr_cool:
                    for lvl in (st.get("support") or []):
                        if _cross(last_price, price, lvl) == -1:
                            sr_triggered.append(f"跌破支撑{lvl}")
                    for lvl in (st.get("resistance") or []):
                        if _cross(last_price, price, lvl) == 1:
                            sr_triggered.append(f"突破阻力{lvl}")
                if sr_triggered:
                    rec["reasons"] = list(rec.get("reasons", [])) + sr_triggered
                sr_notify = bool(sr_triggered) and not sr_cool

                trigger = (prev is None or cross_ca or cross_sa or notify_on_sig or sr_notify)
                if trigger:
                    # 信号档位用于通知时去掉 emoji 圆点, 只保留中文名
                    sig_text = SIG_SHORT.get(sig, sig)
                    prev_sig_text = SIG_SHORT.get(prev_sig, prev_sig) if prev_sig else ""
                    if prev is None:
                        line = f"{st['name']}({code}): 信号 {sig_text}  现价{rec.get('price','')}  {fmt_chg(rec.get('chg_pct'))}"
                    else:
                        notes = []
                        if prev_sig != sig:
                            notes.append(f"信号 {prev_sig_text} → {sig_text}")
                        if cross_ca:
                            notes.append(f"涨跌 {cv:+.2f}% ≥ {st_ca}% 阈值")
                        if cross_sa:
                            notes.append(f"日内波动 {sv:.2f}% ≥ {st_sa}% 阈值")
                        if sr_triggered:
                            notes.append("; ".join(sr_triggered))
                        line = f"{st['name']}({code}): " + "; ".join(notes) \
                               + f"  现价{rec.get('price','')}  {fmt_chg(rec.get('chg_pct'))}"
                    if log_fn is not None:
                        log_fn(rec)
                    # 系统通知: 仅"真实变动"(非首屏)才弹; 信号变动同样受冷却约束, 避免边界横跳刷屏
                    if notifier is not None and prev is not None and (cross_ca or cross_sa or notify_on_sig or sr_notify):
                        notifier.notify(line)
                last_sigs[code] = {
                    "sig": sig,
                    "chg_alert": bool(ca),
                    "swing_alert": bool(sa),
                    "chg_alert_at": (now_t if cross_ca else (prev.get("chg_alert_at") if isinstance(prev, dict) else None)),
                    "swing_alert_at": (now_t if cross_sa else (prev.get("swing_alert_at") if isinstance(prev, dict) else None)),
                    "sig_alert_at": (now_t if notify_on_sig else (prev.get("sig_alert_at") if isinstance(prev, dict) else None)),
                    "sr_alert_at": (now_t if sr_notify else (prev.get("sr_alert_at") if isinstance(prev, dict) else None)),
                    "last_price": price,
                }
            with lock:
                data.update(new)
            time.sleep(state.get("refresh_sec", 1))

    def refresh():
        with lock:
            snap = dict(data)
            sig_changes = dict(last_sig_change)
        offline = not snap
        filter_on = ui["filter_on"]
        for st in stocks:
            code = st["code"]
            # 功能②: 过滤模式下按信号变动时间戳隐藏/显示信号行(下半); 行情行始终可见(仅在状态变化时操作 pack, 避免抖动)
            visible = is_row_visible(filter_on, sig_changes.get(code), SIG_CHANGE_WINDOW_SEC)
            if row_vis.get(code, True) != visible:
                _apply_visibility(code, visible)
                row_vis[code] = visible
            r = snap.get(code)
            sig_l, name_l, price_l, chg_l, dl_l, spark = rows[code]
            if not r:
                continue
            price = r.get("price")
            chg = r.get("chg_pct")
            sig = r.get("signal")
            delayed = bool(r.get("delayed"))
            price_l.config(text=f"{price:.2f}" if isinstance(price, (int, float)) else "--")
            if isinstance(chg, (int, float)):
                col = up_color if chg > 0 else (down_color if chg < 0 else style["flat"])
                sign = "+" if chg > 0 else ""
                chg_l.config(text=f"{sign}{chg:.2f}%", fg=col)
            else:
                chg_l.config(text="", fg=style["flat"])
            sig_l.config(text="●", fg=sig_colors.get(sig, style["flat"]))
            # 延时标记: 港股/美股免费源约15分钟延时, 标"延时"提醒数据非实时
            dl_l.config(text="延时" if delayed else "", fg=style["dl"])
            # sparkline(功能③: 隐藏开关关闭时跳过绘制)
            kline = r.get("kline")
            if kline and ui["show_sparkline"]:
                _draw_sparkline(spark, kline, price, r.get("prev_close"), style)
            # 下半部分: 信号提示
            sdot, slv, sbb = sig_rows[code]
            bull = r.get("bull")
            bear = r.get("bear")
            sdot.config(fg=sig_colors.get(sig, style["flat"]))
            slv.config(text=SIG_SHORT.get(sig, "—"), fg=sig_colors.get(sig, style["flat"]))
            if delayed:
                # 延时股: 右侧显示接口数据时间, 直观看到"慢了多少"
                sbb.config(text=f"{fmt_ts(r.get('ts',''))}数据", fg=style["dl"])
            else:
                sbb.config(text=f"多{bull}空{bear}" if isinstance(bull, int) else "", fg=style["flat"])
        if not state.get("paused"):
            status.config(text=("● 实时  ·  " + str(state.get("refresh_sec", 1)) + "s" if not offline else "○ 离线(显示上次)  ·  重试中"))
        root.after(250, refresh)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    root.after(250, refresh)
    root.mainloop()


# ================= ⑪ main() =================
def main() -> None:
    ap = argparse.ArgumentParser(
        description="浮动股票行情 HUD + 信号/通知/CSV (配置见同级 config.toml / stocks.toml)")
    ap.add_argument("--review", nargs="?", const=30, type=int, metavar="N",
                    help="回看历史信号CSV(最近N条, 默认30), 不启动浮窗")
    ap.add_argument("--stats", action="store_true", help="当日信号聚合统计(配合 --code/--date 过滤)")
    ap.add_argument("--code", type=str, default=None, help="--review/--stats 按代码过滤")
    ap.add_argument("--date", type=str, default=None, help="--review/--stats 按日期(YYYY-MM-DD)过滤")
    ap.add_argument("--no-log", action="store_true", help="不把信号写入 signals.csv")
    args = ap.parse_args()

    settings = load_settings()
    global LIVE_INDICATORS
    LIVE_INDICATORS = bool(settings.get("live_indicators", True))

    # 数据源: 按 sources 配置构建多源兜底(主源失败顺序尝试备用, 主源异常仅告警一次)
    sources = build_sources(settings.get("sources"))
    global DATA_SOURCE
    DATA_SOURCE = DataSource(
        sources=sources,
        alert_fn=lambda m: print(f"[数据源] {m}", file=sys.stderr),
    )

    csv_mode = "daily" if settings.get("csv_mode") == "daily" else "single"
    csv_path = signals_path(csv_mode, args.date)

    # 回看/统计模式: 打印后退出, 不需要 GUI
    if args.review is not None:
        review_csv(args.review, csv_path, code=args.code, date=args.date)
        return
    if args.stats:
        stats_csv(csv_path, code=args.code, date=args.date)
        return

    if tk is None:
        print("未检测到 tkinter, 无法启动浮窗。", file=sys.stderr)
        sys.exit(1)

    stocks = load_stocks(settings)
    if not stocks:
        print("未配置任何股票，无法启动。")
        sys.exit(1)

    # 解析 stocks.toml 实际路径(供运行时增删回写; 无文件则仅内存生效, 不自动创建)
    res_st = _load_first(STOCKS_CANDIDATES)
    stocks_path = res_st[0] if res_st is not None else None

    # 阈值冷却(分钟, 默认 15; 0 关闭)
    cooldown = _to_float(settings.get("alert_cooldown", 15)) or 0.0

    # 系统通知: 信号变动/阈值破位/支撑压力穿越时弹窗, 可配声音; 跨平台(mac/linux/win)
    notify_enabled = bool(settings.get("notify", False))
    notify_snd = bool(settings.get("notify_sound", False))
    notifier = Notifier(enabled=notify_enabled, sound=notify_snd)

    # CSV 落盘(省流规则: 首屏/信号变动/阈值穿越/支撑压力穿越才写); --no-log 关闭
    log = not args.no_log
    csv_dedup_sec = _to_float(settings.get("csv_dedup_sec")) or 0.0

    def do_log(rec):
        append_signal(rec, csv_path, csv_dedup_sec)

    # 启动前并发预热日K缓存, 避免首个刷新周期卡顿
    warm_klines(stocks)
    run_hud(stocks, settings,
            log_fn=(do_log if log else None),
            notifier=(notifier if notify_enabled else None),
            cooldown=cooldown,
            stocks_path=stocks_path)


if __name__ == "__main__":
    main()
