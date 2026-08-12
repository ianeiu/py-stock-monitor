#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""浮动隐蔽股票行情 HUD (macOS / Windows / Linux 均可运行) —— 每秒刷新关注股票 + 信号提示 + 通知/CSV。

自成一体的单文件工具(不再依赖 stock_monitor.py):
- 数据: 腾讯财经实时行情(qt.gtimg.cn) + 历史日K(ifzq.gtimg.cn); 备用免费源(新浪/东财)兜底
- 指标: MA5/10/20, RSI(14), MACD(12,26,9); 可扩展 KDJ / 布林 / 量; 盘中把实时价并入指标序列(可关)
- 信号: 多空打分 -> 五档信号(带滞回, 吸收边界抖动); 打分指标由 settings.indicators 动态决定(默认 MA/RSI/MACD)
- 界面: 原生 tkinter 无边框 + 置顶 + 半透明; 上半部分行情, 下半部分信号提示; 支持暂停/频率切换/暗色
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
from urllib.parse import quote
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from typing import Tuple, Optional, Callable, List, Dict, Any
from pathlib import Path

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
    from tkinter import colorchooser  # 设置面板: 涨/跌色配置弹系统颜色选择器(macOS 即 NSColorPanel)
except Exception:
    colorchooser = None

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

# 热重载守卫: 记录本程序自身最近一次写文件的时间戳, 用于区分"自己写的"与"外部编辑的"改动,
# 避免配置热重载线程把自身的持久化写回误判为外部修改而重复重载(详见 run_hud 的 _check_reload)。
LAST_CONFIG_WRITE_T = 0.0
LAST_STOCKS_WRITE_T = 0.0

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


def parse_levels_txt(s: str) -> Optional[List[float]]:
    """解析支撑/压力位输入(逗号分隔数值列表)。空白 = None(不配置); 全部非法 = None; 部分非法只取合法项。"""
    if not s or not s.strip():
        return None
    vals = [x for x in (_to_float(p) for p in s.split(",")) if x is not None]
    return vals or None


def parse_pct_txt(s: str) -> Optional[float]:
    """解析变动/波动提示输入(%)。空白 = None(不配置, 回退全局); 非法或负数 = None。"""
    if not s or not s.strip():
        return None
    v = _to_float(s)
    return v if v is not None and v >= 0 else None


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


def _parse_search_response(text: str) -> List[dict]:
    """解析腾讯 smartbox 搜索响应为 {"code","name"} 列表(代码带市场前缀)。

    腾讯 smartbox 接口返回形如:
        v_hint="sh~600519~贵州茅台~gzmt~GP-A^sz~000001~平安银行~payh~GP-A^..."
    每条记录以 ^ 分隔, 字段以 ~ 分隔: {market}~{code}~{name}~{pinyin}~{type}。
    市场前缀: sh=上海, sz=深圳, hk=港股, us=美股; 未知市场项跳过。
    响应为空 / 无 v_hint / 无有效记录 → 返回 [] (优雅降级)。结果按 code 去重。
    """
    if not text:
        return []
    m = re.search(r'v_hint="([^"]*)"', text)
    if not m:
        return []
    content = m.group(1)
    if not content:
        return []

    known_markets = {"sh", "sz", "hk", "us"}
    results: List[dict] = []
    seen: set = set()
    for rec in content.split("^"):
        if not rec:
            continue
        parts = rec.split("~")
        if len(parts) < 3:
            continue
        market, code, name = parts[0], parts[1], parts[2]
        # 腾讯 smartbox 返回的 name 可能包含字面 \uXXXX 转义序列(如 'TCL\\u79d1\\u6280'),
        # 需解码为明文中文; 若 name 已是明文中文(非 ASCII 字节, 无法 latin1 编码),
        # 则保持原样, 避免 latin1 编码抛 UnicodeEncodeError。
        try:
            name = name.encode("latin1").decode("unicode_escape")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        if not market or not code or not name:
            continue
        if market not in known_markets:
            continue
        full_code = f"{market}{code}"
        if full_code in seen:
            continue
        seen.add(full_code)
        results.append({"code": full_code, "name": name})
    return results


def search_stocks(query: str, limit: int = 10) -> List[dict]:
    """按名称/代码模糊搜索股票, 复用腾讯 smartbox 搜索接口(无需 token)。

    返回 [{"code","name"}] (代码带市场前缀)。空查询 / 网络异常 / 解析异常
    → 返回 [] (调用方据此显示"无匹配结果", 不让搜索错误拖垮主程序)。
    """
    if not query or not query.strip():
        return []
    q = quote(query.strip())
    url = f"https://smartbox.gtimg.cn/s3/?q={q}&t=all"
    try:
        resp = fetch(url)
        return _parse_search_response(resp)[:limit]
    except Exception:
        return []


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


def signal_became_changed(prev_sig, sig) -> bool:
    """Return True only when sig actually changed AND we have a prior reading.

    首次观测(prev_sig 为 None)不计为"变动" —— 否则每只股票都会在启动瞬间
    被标记"刚变动", 进而在信号面板展示 SIG_CHANGE_WINDOW_SEC 秒(启动瞬时全显 bug)。
    """
    return prev_sig is not None and prev_sig != sig


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
    rec 新增 k/d/j/boll_*/volume/vol_ma5 字段(追加到 CSV 尾部)。
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


def _save_config_key(key: str, value, path: str = None) -> None:
    """向 config.toml 写入单个 [settings] 键值对（保留式）。用于设置面板实时持久化。

    - path: 目标文件; 缺省按「SCRIPT_DIR(与 _load_first 读取端一致, 打包后 boot.py 重定向为
      发布目录) → 当前工作目录」顺序遍历 SETTINGS_CANDIDATES 取第一个已存在者, 都不存在则静默跳过。
      此前仅用 CWD 相对路径 os.path.isfile(c), 打包后双击 .app 时 CWD ≠ 发布目录,
      导致面板所有写盘静默失败(2026-08-04 真机: hide_param 不持久化)。
    - bool 值落为裸 TOML 布尔(如 True/False, 无引号), 重启可正确回读; float 值用 repr 落为合法 TOML 数值(如 0.94, 无引号); 其它值用 repr(str(value)) 落为字符串。
    - 保留其它段与注释; 异常静默吞掉, 不阻塞 UI。
    """
    try:
        target = path
        if not target:
            # 与读取端 _load_first 对齐: 优先 SCRIPT_DIR(打包后=发布目录), 再回退 CWD(旧行为兼容)
            for base in (SCRIPT_DIR, os.getcwd()):
                for c in SETTINGS_CANDIDATES:
                    cand = os.path.join(base, c)
                    if os.path.isfile(cand):
                        target = cand
                        break
                if target:
                    break
        if not target:
            return
        text = Path(target).read_text('utf-8', errors='replace')
        # 确保 [settings] 段存在
        if '[settings]' not in text:
            text = '[settings]\n' + text
        key_pat = re.compile(r'^\s*' + re.escape(key) + r'\s*=')
        # bool -> 裸布尔(TOML 要求小写 true/false, 与 Python str(False)='False' 不同);
        # float -> 裸数值; 其余 -> 字符串
        if isinstance(value, bool):
            new_line = f"{key} = {'true' if value else 'false'}"
        elif isinstance(value, float):
            new_line = f"{key} = {repr(value)}"
        else:
            new_line = f"{key} = {repr(str(value))}"
        lines = text.split('\n')
        found = False
        out: List[str] = []
        in_settings = False
        for line in lines:
            stripped = line.strip()
            if stripped == '[settings]':
                in_settings = True
                out.append(line)
                continue
            if stripped.startswith('[') and stripped != '[settings]':
                in_settings = False
            if in_settings and key_pat.match(line):
                out.append(new_line)
                found = True
            else:
                out.append(line)
        if not found:
            # 在 [settings] 后第一行插入
            for i, line in enumerate(out):
                if line.strip() == '[settings]':
                    out.insert(i + 1, new_line)
                    break
        global LAST_CONFIG_WRITE_T
        LAST_CONFIG_WRITE_T = time.time()
        Path(target).write_text('\n'.join(out), 'utf-8')
    except Exception:
        # 异常静默吞掉, 不阻塞 UI
        pass


def set_topmost(root, on: bool) -> None:
    """设置窗口是否置顶(always-on-top)。薄封装 root.attributes, 便于无头桩测试。

    约定: 置顶为运行时态(启动从 config 读默认, 运行中切换, 不回写文件)。
    """
    root.attributes("-topmost", bool(on))


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


def is_row_visible(sig_changed_at: Optional[float],
                   window_sec: float = SIG_CHANGE_WINDOW_SEC) -> bool:
    """信号行(下半)可见性判定纯函数。

    默认只展示有信号变动的股票:
    - sig_changed_at 为 None: 信号行(下半)不可见。
    - sig_changed_at 非 None: (now - sig_changed_at) <= window_sec 信号行(下半)可见, 否则不可见。
    注意: 行情行(上半)始终可见, 不受本判定影响。
    """
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
                        reorder: Optional[List[str]] = None,
                        update_code: Optional[str] = None,
                        update_data: Optional[dict] = None) -> List[dict]:
    """功能① 保留式重写 stocks.toml: 保留文件头注释与 [settings] 段, 仅重写 [[stocks]] 区块。

    - add: parse_add_input 返回的 stock dict(或含 code/name 的 dict); 若 code 已存在则忽略(去重)。
    - remove: 要删除的股票 code。
    - reorder: 可选, 给定 code 顺序列表, 在序列化前据此重排 stocks
      (不在列表中的 code 保持原相对序追加于末尾)。add/remove 调用方不传, 保持向后兼容。
    - update_code + update_data: 可选, 更新指定 code 的个股参数(support/resistance/chg_alert/
      swing_alert)。update_data 中值为 None 的键 = 移除该字段(不写入, 重启后回退全局/无配置);
      值非 None 则写入。
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
    if update_code is not None and update_data:
        for s in stocks:
            if s.get("code") != update_code:
                continue
            for k in ("support", "resistance", "chg_alert", "swing_alert"):
                if k not in update_data:
                    continue
                if update_data[k] is None:
                    s.pop(k, None)          # 清空 = 移除字段(重启回退全局/无配置)
                else:
                    s[k] = update_data[k]
            break
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
    global LAST_STOCKS_WRITE_T
    LAST_STOCKS_WRITE_T = time.time()
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

# 重入守卫哨兵: _run_with_guard 在持锁期间被嵌套调用时返回该值(表示实质工作被跳过)
_GUARD_SKIPPED = object()


def _run_with_guard(guard: dict, fn: Callable, *args, **kwargs):
    """通用重入守卫包装器。

    用于 macOS Tk 上 Scale 的 -command 回调经 root.update_idletasks() 重入事件循环、
    再次触发同一滑块 command 造成的无限递归(RecursionError)防护。

    guard 必须是可变 dict(形如 {"v": False}); 若 guard["v"] 为 True(说明正处在另一次
    调用内部, 对当前回调的重入), 直接跳过 fn 的执行并返回 _GUARD_SKIPPED; 否则执行 fn,
    并于 finally 中复位 guard["v"]=False, 确保外层调用照常完成、标志可靠还原。
    即使 fn 内部(经 update_idletasks 等)重入触发同一回调, 嵌套调用也会因守卫立即返回,
    递归被彻底切断, 且视觉样式已由外层调用正确应用。

    返回: fn 的执行结果; 被守卫跳过时返回 _GUARD_SKIPPED。
    """
    if guard.get("v"):
        return _GUARD_SKIPPED
    guard["v"] = True
    try:
        return fn(*args, **kwargs)
    finally:
        guard["v"] = False


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
        # 异动闪动高亮色: 与背景形成可辨对比但不过刺; 暗色用暖黄、亮色用浅黄
        "flash": ("#3a3a1e" if theme == "dark" else "#fff3b0"),
        "FONT": (font, size), "FONT_SM": (font, max(5, size - 1)), "ROW_H": 14,
    }


# ================= ⑩ GUI (Hud / run_hud) =================


def apply_sig_visibility(sf_, visible, sig_pack):
    """仅控制信号行(下半)显隐; 行情行(上半)由调用方保证始终可见, 本函数不碰。

    按目标态无条件 apply: 显示则 pack、隐藏则 pack_forget(对已处该状态的 widget 是安全的幂等操作),
    不依赖 winfo_ismapped()(macOS Tk 上该值对可见控件常返回 False, 会导致隐藏失效)。
    调用方 _apply_visibility 已用 row_vis != visible 守卫, 仅在真实状态变化时才调用本函数,
    故不会每轮重复 pack。该纯函数不依赖 Tk 主线程, 可直接无头单测(用桩对象记录 pack/pack_forget 调用)。
    """
    if sf_ is None:
        return
    if visible:
        sf_.pack(**sig_pack)
    else:
        sf_.pack_forget()


def refresh_requires_ban_warning(nxt_sec: int) -> bool:
    """切到 1 秒刷新时需弹确认框警告数据源可能被限流/封禁。

    Args:
        nxt_sec: 即将切换到的刷新周期(秒)。

    Returns:
        仅当切到 1 秒刷新时返回 True(需警告), 其余周期返回 False。
    """
    return nxt_sec == 1


def format_stock_name(name: str, delayed: bool) -> str:
    """股票名显示: 延时源在名后追加（延时）提示。纯函数, 便于无头单测。"""
    return f"{name}（延时）" if delayed else name


def run_hud(stocks: List[dict], settings: dict, log_fn: Optional[Callable[[dict], None]] = None,
            notifier: Optional[Notifier] = None, cooldown: float = 0.0,
            stocks_path: Optional[str] = None, config_path: Optional[str] = None) -> None:
    """构建并运行浮窗; 后台线程按 refresh_sec 取数, 满足省流规则时写CSV/弹通知。
    新增: 暂停/频率控件、暗色样式、Windows 闪烁(flash_fn 注入 notifier)。
    """
    style = build_style(settings)
    # 涨跌幅颜色(浮窗专用)
    up_color = style["up"]
    down_color = style["down"]
    sig_colors = style["sig_colors"]
    # 透明度(0~1, 非法/越界回退默认)
    _a = settings.get("float_alpha", ALPHA_DEFAULT)
    alpha = _a if isinstance(_a, (int, float)) and 0 < _a <= 1 else ALPHA_DEFAULT
    # 刷新周期(默认5s; 频率控件在 1/5/10 间循环; 历史残留 3 归一化到 5)
    try:
        refresh_sec = max(1, int(_to_float(settings.get("refresh_sec")) or 1))
    except (TypeError, ValueError):
        refresh_sec = 1
    # 归一化到合法频率集合 {1,5,10}; config 残留 refresh_sec=3 等无效值映射到最近合法值
    if refresh_sec not in (1, 5, 10):
        # 就近映射: <=2 ->1, <=5 ->5(覆盖历史 3/4), 其余 ->10
        refresh_sec = 1 if refresh_sec <= 2 else (5 if refresh_sec <= 5 else 10)

    root = tk.Tk()
    root.title("")
    root.overrideredirect(True)           # 去标题栏/边框 -> 浮动
    set_topmost(root, bool(settings.get("topmost", True)))  # 置顶(启动默认由 config 决定)
    root.attributes("-alpha", alpha)       # 透明度, 由 float_alpha 配置控制
    root.configure(bg=style["bg"])
    root.resizable(False, False)
    # macOS: 禁用绿色放大按钮(macOS Tk 无直接 API; 本文依赖 overrideredirect 去标题栏 +
    # resizable 禁边缘缩放 + 下文的 refresh 宽度守卫 三管齐下。若窗口仍被放大(全屏手势等),
    # refresh 守卫每轮强制恢复原始宽度——这是唯一兜底, 不依赖平台专有属性)。

    # ---- 顶部拖拽条 ----
    header = tk.Frame(root, bg=style["header"], height=14)
    header.pack(fill="x")
    htitle = tk.Label(header, text="", bg=style["header"], fg=style["fg_dim"],
                      font=style["FONT_SM"], anchor="w")
    htitle.pack(side="left", padx=(2, 0))

    def _quit():
        """静默直接退出, 不弹任何确认框。"""
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", _quit)

    # header 按钮左→右顺序: ↺刷新 → 🔔显示变动 → ＋新增 → ⚙️设置
    # 刷新频率切换已移至设置面板(⚙️ 内)。用 side="left" 打包, 先 pack 的在左。
    # (窗口置顶 📌 已并入设置面板, 不再保留按钮)

    # 立即刷新: ↺ 按钮(点击触发后台立即取数)
    refresh_btn = tk.Label(header, text=" ↺ ", bg=style["header"], fg=style["fg_dim"],
                           font=style["FONT_SM"], cursor="hand2")
    refresh_btn.pack(side="left", padx=(0, 1))
    refresh_btn.bind("<Button-1>", lambda e: _force_refresh())

    # 频率控件(1/5/10s 循环)
    # 刷新频率按钮(已移至设置面板, 此处保留引用占位供 _cycle_freq / _reapply_style 使用;
    # 设置面板构建后 freq_btn["w"] 指向实际 Button 控件)
    freq_btn = {"w": None}

    # 信号提示显隐开关: 🔔/🔕 按钮(运行时态, 不落盘) —— header 左起第 2 位(紧挨 ↺刷新)
    # 注意: 此处 ui 字典尚未定义, 故直接读 settings 默认(与 ui["show_signal"] 同源, 默认 True)
    _sig_on = bool(settings.get("show_signal", True))
    sig_btn = tk.Label(header, text=("🔔" if _sig_on else "🔕"),
                       bg=style["header"], fg=(style["fg"] if _sig_on else style["fg_dim"]),
                       font=style["FONT_SM"], cursor="hand2")
    sig_btn.pack(side="left", padx=(0, 1))

    def _toggle_signal_label():
        """点击后切换信号显隐, 并同步按钮文案与高亮。"""
        _toggle_signal()
        on = ui["show_signal"]
        sig_btn.config(text=("🔔" if on else "🔕"),
                       fg=(style["fg"] if on else style["fg_dim"]))
    sig_btn.bind("<Button-1>", lambda e: _toggle_signal_label())

    # 运行时增删自选: ＋ 按钮(header 左起第 3 位)
    add_btn = tk.Label(header, text=" ＋ ", bg=style["header"], fg=style["fg_dim"],
                       font=style["FONT_SM"], cursor="hand2")
    add_btn.pack(side="left", padx=(0, 1))
    add_btn.bind("<Button-1>", lambda e: _add_stock_dialog())

    # 设置面板: ⚙️ 按钮(透明度+灰度+变动消息), 点开实时调节并持久化
    set_btn = tk.Label(header, text=" ⚙ ", bg=style["header"], fg=style["fg_dim"],
                       font=style["FONT_SM"], cursor="hand2")
    set_btn.pack(side="left", padx=(0, 1))
    set_btn.bind("<Button-1>", lambda e: _open_settings_panel())

    drag = {"x": 0, "y": 0}

    def start_drag(e):
        drag["x"] = e.x_root - root.winfo_x()
        drag["y"] = e.y_root - root.winfo_y()

    def do_drag(e):
        root.geometry(f"+{e.x_root - drag['x']}+{e.y_root - drag['y']}")

    for w in (header, htitle):
        w.bind("<ButtonPress-1>", start_drag)
        w.bind("<B1-Motion>", do_drag)

    # 鼠标移开自动淡出, 悬停恢复清晰(B1): 离开窗口降到淡出透明度, 进入恢复用户设定基值。
    # 注意: 不经由 _apply_alpha(它会持久化并覆盖基值), 直接改 -alpha 属性, 无几何抖动。
    root.bind("<Enter>", lambda e: root.attributes("-alpha", ui["_alpha_base"]))
    root.bind("<Leave>", lambda e: root.attributes("-alpha", 0.35))

    # ---- 主窗口布局容器: content(顶层 wrapper) 内含 center(中央竖向区) ----
    # 原 left_pane/right_pane 侧列已移除; 设置/添加面板改为 center 内 status 之下的
    # 内联面板, 默认隐藏, 由 ⚙️/＋ 切换显隐, 按 header 图标顺序(⚙️上 / ＋下)竖向堆叠。
    # body/sep/sighead/sigpane/status 以及 settings_inline/add_inline 均为 center 子 Frame。
    content = tk.Frame(root, bg=style["bg"])
    content.pack(fill="x")
    center = tk.Frame(content, bg=style["bg"])         # 中央竖向区
    center.pack(side="left", fill="both", expand=True)

    # 以下两内联面板为 center 子 Frame, 默认隐藏, 打包于 status 之下(添加面板在设置面板之下)。
    settings_inline = tk.Frame(center, bg=style["bg"])   # 设置面板(默认隐藏, 置于 status 之下)
    add_inline = tk.Frame(center, bg=style["bg"])         # 添加面板(默认隐藏, 置于 status 之下)
    settings_inline.pack_forget()
    add_inline.pack_forget()

    # ---- 行情行(固定 5 行可视 + 右侧滚动条, 鼠标滚轮/拖条浏览更多股票) ----
    # 外层 body_wrap 含 Canvas + 纵向 Scrollbar; 内部 body 是 Canvas 上的子 Frame,
    # 行情行仍在 body 内 pack(原有 _build_quote_row / _reload_stocks 重排逻辑不变)。
    body_wrap = tk.Frame(center, bg=style["bg"])
    body_wrap.pack(fill="x")
    # 可视行数: 固定 5 行; 行高按 20px 估算(Menlo 7 号实际渲染高于 ROW_H=14,
    # 5 行取 5×20=100px), 超出部分走滚动。
    QUOTE_VISIBLE_ROWS = 5
    QUOTE_ROW_H_EST = 20
    body_canvas = tk.Canvas(body_wrap, bg=style["bg"], height=QUOTE_VISIBLE_ROWS * QUOTE_ROW_H_EST,
                            highlightthickness=0, bd=0, width=1,
                            yscrollincrement=QUOTE_ROW_H_EST)  # 滚轮每格 = 恰好一行
    body_canvas.pack(side="left", fill="both", expand=True)
    # 右侧栏: ▲ 滚动按钮 / 滚动条 / ▼ 滚动按钮(macOS Tk 只把 scrollWheel 投递给可滚动控件,
    # 行情区 Label/Frame 收不到滚轮——▲▼ 按钮是不依赖滚轮事件的硬滚动入口, 点击逐行滚动)
    side_bar = tk.Frame(body_wrap, bg=style["bg"])
    side_bar.pack(side="right", fill="y")
    up_arrow = tk.Label(side_bar, text="▲", bg=style["bg"], fg=style["fg_dim"],
                        font=style["FONT_SM"], cursor="hand2")
    up_arrow.pack(side="top", fill="x")
    up_arrow.bind("<Button-1>", lambda e: body_canvas.yview_scroll(-1, "units"))
    down_arrow = tk.Label(side_bar, text="▼", bg=style["bg"], fg=style["fg_dim"],
                          font=style["FONT_SM"], cursor="hand2")
    down_arrow.pack(side="bottom", fill="x")
    down_arrow.bind("<Button-1>", lambda e: body_canvas.yview_scroll(1, "units"))
    body_scroll = tk.Scrollbar(side_bar, orient="vertical", command=body_canvas.yview,
                               width=14, bd=0, relief="flat")
    body_scroll.pack(side="top", fill="both", expand=True)
    body = tk.Frame(body_canvas, bg=style["bg"])
    body_id = body_canvas.create_window((0, 0), window=body, anchor="nw")
    body_canvas.configure(yscrollcommand=body_scroll.set)
    # 内部 body 尺寸变化时刷新滚动区域(新增/删除/重排行后滚动条范围正确)
    body.bind("<Configure>",
              lambda e: body_canvas.configure(scrollregion=body_canvas.bbox("all")))
    # body 宽度跟随画布(否则行内容只占 body 自身请求宽度, 右侧留白)
    body_canvas.bind("<Configure>",
                     lambda e: body_canvas.itemconfigure(body_id, width=e.width))
    # 滚轮驱动 canvas 滚动(统一出口): step 转 int(Tk 9 平滑滚动 delta 是浮点),
    # 任何异常静默吞掉(如越界/已销毁), 不阻塞 UI。
    def _scroll_canvas(step):
        try:
            body_canvas.yview_scroll(int(step), "units")
        except Exception:
            pass

    # 鼠标滚轮: 任何 MouseWheel/Button-4/5 事件到达即滚动(不拦截、不判坐标)。
    # 说明: macOS Tk9 只把 scrollWheel 投递给可滚动控件, 行情区 Label/Frame 收不到,
    # 故 mac 上主要靠右侧 ▲▼ 按钮 / 滚动条拖动 / 滚动条上滚轮; 本绑定对 Linux/Windows 有效。
    def _on_quote_wheel(e):
        d = getattr(e, "delta", 0) or 0
        num = getattr(e, "num", None)
        if num == 4:
            _scroll_canvas(-1)
        elif num == 5:
            _scroll_canvas(1)
        elif d:
            # Tk 9(macOS) 平滑滚动 delta 可能为浮点, 只取符号方向: d>0 内容上移
            step = -1 if d > 0 else 1
            if sys.platform == "win32":
                step = -(int(d) // 120)
            _scroll_canvas(step)
        else:
            # delta/num 都拿不到(Tk9 下 %d 可能为 0) → 默认向下滚一行, 保证有响应
            _scroll_canvas(1)
    body_canvas.bind("<MouseWheel>", _on_quote_wheel)
    body.bind("<MouseWheel>", _on_quote_wheel)
    root.bind_all("<MouseWheel>", _on_quote_wheel)
    # macOS 第三方外接鼠标滚轮在 Tk 中常触发 <Button-4>/<Button-5>(模拟 X11)而非 <MouseWheel>,
    # 故 darwin 也一并绑定(仅 Linux 绑会漏掉 mac 外接鼠)。行内子控件另在 _build_quote_row 补绑。
    if sys.platform.startswith("linux") or sys.platform == "darwin":
        body_canvas.bind("<Button-4>", _on_quote_wheel)
        body_canvas.bind("<Button-5>", _on_quote_wheel)
        body.bind("<Button-4>", _on_quote_wheel)
        body.bind("<Button-5>", _on_quote_wheel)
        root.bind_all("<Button-4>", _on_quote_wheel)
        root.bind_all("<Button-5>", _on_quote_wheel)
    # Tk 9(macOS) 平滑滚动专用: 滚轮增量走 %D(浮点), tkinter Event.delta 映射的是旧 %d
    # (Tk9 下多为 0), 故在 Tcl 侧直接用 %D 驱动 yview, 绕开 tkinter 事件对象。
    # 仅绑定到 body_canvas 本身: 鼠标悬停于内部子控件时事件经 bindtags 冒泡到 toplevel,
    # 但 canvas 的绑定不会被子控件触发——故同时 bind_all(下方 _wheel_tcl_all)。
    _cv = str(body_canvas)
    _tcl_wheel = (
        "if {[string is double -strict %D] && %D != 0} {"
        f"  if {{%D < 0}} {{{_cv} yview scroll 1 units}} "
        f"  else {{{_cv} yview scroll -1 units}}"
        "} elseif {[string is double -strict %d] && %d != 0} {"
        f"  if {{%d < 0}} {{{_cv} yview scroll 1 units}} "
        f"  else {{{_cv} yview scroll -1 units}}"
        "} else {"
        f"  {{{_cv} yview scroll 1 units}}"   # %D/%d 均不可用 → 默认向下滚一行
        "}"
    )
    body_canvas.bind("<MouseWheel>", _tcl_wheel, add="+")
    root.bind_all("<MouseWheel>", _tcl_wheel, add="+")
    # macOS 将 scrollWheel 投递给「键盘焦点」视图: 启动后把焦点给 body,
    # 否则 overrideredirect 窗口可能收不到滚轮事件(滚动条拖动不受影响)。
    root.after(300, lambda: (_silent_focus(root), _silent_focus(body)))

    def _silent_focus(w):
        try:
            w.focus_set()
            w.focus_force()
        except Exception:
            pass
    rows: Dict[str, tuple] = {}
    quote_frames: Dict[str, "tk.Frame"] = {}
    sig_frames: Dict[str, "tk.Frame"] = {}
    row_vis: Dict[str, bool] = {}
    last_sig_change: Dict[str, float] = {}
    # 手动排序: 记录每行行情行右侧的上移/下移箭头部件, 用于边界灰显
    move_btns: Dict[str, tuple] = {}
    # 删除按钮引用(供「隐藏删除」开关按需 pack_forget/重 pack)
    del_btns: Dict[str, "tk.Label"] = {}
    # 个股参数按钮引用(⚙, 打开该股 support/resistance/chg_alert/swing_alert 配置面板)
    param_btns: Dict[str, "tk.Label"] = {}
    # 行情行异动闪动待触发标记: worker 线程写入(异步事件), refresh 主线程消费并真正改背景色
    # (Tk 非线程安全, 所有 widget.config 必须发生在主线程, 故经 refresh 的 root.after 循环执行)
    flash_pending: Dict[str, bool] = {}
    ui = {"topmost": bool(settings.get("topmost", True)), "show_signal": True}
    # 淡出基值: 用户设定的透明度, 鼠标移开淡出后以此还原(不被淡出值覆盖)
    ui["_alpha_base"] = alpha
    # 内联面板展开态(显式布尔, 不依赖 winfo_ismapped——macOS Tk 上该值不可信,
    # 会导致"点 ＋/⚙️ 收不起/重复展开")。_add_stock_dialog/_open_settings_panel/_close_settings 读写;
    # param 存当前打开参数面板的股票 code(无则 None)。
    panel_state = {"settings": False, "add": False, "param": None}
    QUOTE_PACK = dict(fill="x", padx=2, pady=0)
    SIG_PACK = dict(fill="x", padx=4, pady=(0, 0))
    # 窗口宽度锁定: 首轮 refresh 捕获折叠态(收起面板)宽度并 minsize=maxsize 锁死,
    # 使设置/添加面板展开时窗口不再被内部控件撑大(始终等于主题窗口宽)。
    # target: 锁定宽度值。不锁高度——高度由内容驱动(面板展开/加股票/信号行变化),
    #   通过 refresh 周期守卫兜底(1~10s)检测高度异常。如果在这里锁高度, 面板一展开就被截断。
    width_locked = {"v": False, "target": None}

    # 防缩放守卫(实时): 监听 root 的 <Configure> 事件, **仅**在宽度偏离锁值时强制恢复。
    # 高度完全不干预(内容自然撑高/收缩, 由 refresh 周期守卫兜底处理 zoom 残留的异常高度)。
    # 不调用 wm_state("normal")——那会把置顶(topmost)也干掉, 导致窗口沉底。
    def _enforce_window(e):
        if not width_locked["v"] or not width_locked["target"]:
            return
        if e.widget is not root:
            return
        if e.width != width_locked["target"]:
            # 宽度锁死; 高度保留当前值(不干涉内容高度, 避免截断面板)
            root.geometry(f"{width_locked['target']}x{e.height}")
            root.update_idletasks()
    root.bind("<Configure>", _enforce_window)
    # 配置热重载: 记录已处理过的文件 mtime, 仅当 mtime 超过"自身写时间戳 + 缓冲"才视为外部改动
    cfg_poll = {"stocks_mt": 0.0, "config_mt": 0.0}

    # ---- 运行时增删自选 / 信号行可见性控制: 行构建与 UI 回调 ----
    # 以下嵌套函数引用的 body/sigpane/status 等均在 run_hud 后续创建,
    # 调用发生在运行时(构建循环或用户交互), 闭包按调用时解析, 故此处定义安全。
    def _build_quote_row(st):
        """构建一只股票的行情行; 存入 rows/quote_frames/row_vis, 绑定右键删除菜单。"""
        code = st["code"]
        f = tk.Frame(body, bg=style["bg"], height=style["ROW_H"])
        f.pack(**QUOTE_PACK)
        sig_l = tk.Label(f, text="●", bg=style["bg"], fg=style["flat"], font=style["FONT"], width=2, anchor="w")
        sig_l.pack(side="left")
        name_l = tk.Label(f, text=st["name"], bg=style["bg"], fg=style["fg"], font=style["FONT"], width=9, anchor="w")
        name_l.pack(side="left")
        price_l = tk.Label(f, text="--", bg=style["bg"], fg=style["fg"], font=style["FONT"], width=4, anchor="e")
        price_l.pack(side="left", padx=(0, 3))
        chg_l = tk.Label(f, text="", bg=style["bg"], fg=style["flat"], font=style["FONT"], width=6, anchor="e")
        chg_l.pack(side="left", padx=(3, 0))
        dl_l = tk.Label(f, text="", bg=style["bg"], fg=style["dl"], font=style["FONT_SM"], width=2, anchor="w")
        dl_l.pack(side="left")
        # 可见删除按钮(功能①): 先 pack 故位于最右角; 左键直接删除。
        del_btn = tk.Label(f, text="🗑", bg=style["bg"], fg=style["fg_dim"],
                           font=style["FONT_SM"], cursor="hand2")
        del_btn.bind("<Button-1>", lambda e, c=code: _confirm_remove(c))
        del_btn.pack(side="right", padx=(0, 1))
        # 记录原始文本/内边距, 供「宽度折叠」显隐方案还原(见 _apply_row_tools_visibility)
        del_btn._orig_text = del_btn.cget("text")   # "🗑"
        del_btn._orig_padx = del_btn.cget("padx")   # (0, 1)
        del_btns[code] = del_btn
        # 手动排序: 仅上移箭头 ▲(位于删除按钮左侧, 紧贴其左); 下移 ▼ 已移除(2026-08-12)
        up_btn = tk.Label(f, text="▲", bg=style["bg"], fg=style["fg_dim"],
                          font=style["FONT_SM"], cursor="hand2")
        up_btn.pack(side="right", padx=(0, 0))
        # 记录原始文本/内边距, 供「宽度折叠」显隐方案还原
        up_btn._orig_text = up_btn.cget("text")   # "▲"
        up_btn._orig_padx = up_btn.cget("padx")   # (0, 0)
        up_btn.bind("<Button-1>", lambda e, c=code: _move_stock(c, "up"))
        # move_btns 兼容旧二元组结构: (up_btn, None) —— down 已移除, 消费方遇 None 跳过
        move_btns[code] = (up_btn, None)
        # 个股参数按钮(⚙, 功能④): 位于工具区最左(排序箭头左侧), 左键打开该股参数面板。
        # 同时是右键菜单不可靠(macOS)时配置参数的可达入口。
        param_btn = tk.Label(f, text="⚙", bg=style["bg"], fg=style["fg_dim"],
                             font=style["FONT_SM"], cursor="hand2")
        param_btn.bind("<Button-1>", lambda e, c=code: _open_param_panel(c))
        param_btn.pack(side="right", padx=(0, 1))
        # 记录原始文本/内边距, 供「宽度折叠」显隐方案还原(本按钮不受 hide_sort/hide_del 控制,
        # 但为一致性预留 _orig_* 属性)
        param_btn._orig_text = param_btn.cget("text")   # "⚙"
        param_btn._orig_padx = param_btn.cget("padx")   # (0, 1)
        param_btns[code] = param_btn
        rows[code] = (sig_l, name_l, price_l, chg_l, dl_l)
        quote_frames[code] = f
        row_vis[code] = True
        # 右键菜单: 参数设置 + 删除(功能①④)——保留, 非 macOS 用户仍可用; mac 上以 ⚙/🗑 兜底
        f.bind("<Button-3>", lambda e, c=code: _show_row_menu(e, c))
        # 左键点击行情行(非工具按钮区域)复制股票代码(B2)
        f.bind("<Button-1>", lambda e, c=code, t=(del_btn, up_btn, param_btn): _on_row_click(e, c, t))
        # 滚轮兜底: 行 frame 及其全部子控件(名称/价格/涨幅/工具按钮)都绑 MouseWheel +
        # Button-4/5(外接鼠), 确保鼠标悬停于行内任意 widget 上滚动都能驱动行情滚动
        # (bind_all 在 macOS 不可靠时的补充)。
        for _ch in (sig_l, name_l, price_l, chg_l, dl_l, del_btn, up_btn, param_btn):
            _ch.bind("<MouseWheel>", _on_quote_wheel)
            _ch.bind("<Button-4>", _on_quote_wheel)
            _ch.bind("<Button-5>", _on_quote_wheel)
        f.bind("<MouseWheel>", _on_quote_wheel)
        f.bind("<Button-4>", _on_quote_wheel)
        f.bind("<Button-5>", _on_quote_wheel)

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
            # 防御: 跳过已被 destroy 的残留引用, 避免对已销毁 widget 调 .config() 抛 TclError
            if not up_btn.winfo_exists():
                move_btns.pop(c, None)
                continue
            # 若「隐藏排序」开启, 箭头已被 pack_forget, 跳过灰显配置(避免对隐藏 widget 无谓操作)。
            # 注意: 不依赖 winfo_ismapped()(macOS Tk 上该值不可靠, 会导致永远跳过)。
            if bool(settings.get("hide_sort", False)):
                continue
            up_btn.config(fg=(style["bg"] if i == 0 else style["fg_dim"]))
            # 下移箭头已移除(2026-08-12), down_btn 恒为 None, 仅兼容旧结构
            if down_btn is not None:
                down_btn.config(fg=(style["bg"] if i == n - 1 else style["fg_dim"]))

    def _apply_row_tools_visibility():
        """按 settings 的 hide_sort/hide_del/hide_param 即时显隐行情行右侧工具按钮(排序/删除/参数)。

        采用「宽度折叠」而非 pack_forget/pack: 隐藏时清文本+宽度0+内边距0(水平空间塌缩为0),
        显示时还原原始文本/内边距。macOS Tk 上反复 pack_forget/pack 偶发「第二次隐藏失效」,
        此方案不触发几何抖动, 对任意次数 toggle 幂等稳健。
        """
        hide_sort = bool(settings.get("hide_sort", False))
        hide_del = bool(settings.get("hide_del", False))
        hide_param = bool(settings.get("hide_param", False))
        for code, (up_btn, down_btn) in list(move_btns.items()):
            # 防御: 跳过已被 destroy 的残留引用(如极端情况下删除股票后未清理干净的 code),
            # 避免对已销毁 widget 调 .config() 抛 TclError: invalid command name
            if not up_btn.winfo_exists():
                move_btns.pop(code, None)
                continue
            for w in (up_btn, down_btn):
                if w is None:
                    continue  # 下移箭头已移除(2026-08-12), 兼容 (up_btn, None) 结构
                if hide_sort:
                    w.config(text="", width=0, padx=0, cursor="")
                else:
                    w.config(text=w._orig_text, width=0, padx=w._orig_padx, cursor="hand2")
        for code, del_btn in list(del_btns.items()):
            if not del_btn.winfo_exists():
                del_btns.pop(code, None)
                continue
            if hide_del:
                del_btn.config(text="", width=0, padx=0, cursor="")
            else:
                del_btn.config(text=del_btn._orig_text, width=0, padx=del_btn._orig_padx, cursor="hand2")
        for code, param_btn in list(param_btns.items()):
            if not param_btn.winfo_exists():
                param_btns.pop(code, None)
                continue
            if hide_param:
                param_btn.config(text="", width=0, padx=0, cursor="")
            else:
                param_btn.config(text=param_btn._orig_text, width=0, padx=param_btn._orig_padx, cursor="hand2")
        # 运行时关闭「隐藏排序」后, 立即刷新首行 up / 末行 down 的边界灰显。
        # 不依赖 winfo_ismapped, 且 _refresh_move_buttons 不回调本函数, 故无递归风险。
        if not hide_sort:
            _refresh_move_buttons()
        root.update_idletasks()

    def _move_stock(code, direction):
        """上移/下移一只股票(自定义手动排序)。

        流程: 边界检查 -> 纯函数 move_stock_in_order 计算新顺序 -> 重建内存 stocks
        -> 按新顺序重排 UI 显示(pack 追加到父容器末尾) -> 协调过滤可见性
        -> 刷新箭头灰显 -> 回写 stocks.toml(reorder)。仅内存模式(stocks_path 为空)则跳过写回。
        """
        # 守卫: hide_sort 开启时排序箭头已视觉隐藏, 拒绝误点(宽度折叠只是视觉, 绑定还在)
        if bool(settings.get("hide_sort", False)):
            return
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
        操作后刷新几何, 杜绝 pack_forget/re-pack 来回切换的脆弱性。
        """
        # 行情行(上半)始终可见: 构建时一次性 pack, 仅 _remove_stock 删除时才销毁,
        # 本函数不再对上半行情行做任何 pack/pack_forget。
        sf_ = sig_frames.get(code)
        apply_sig_visibility(sf_, visible, SIG_PACK)
        # 刷新几何: 确保 pack_forget 立即生效, 不被后续 pack 覆盖而「粘住」。
        # 刷新循环已用 row_vis != visible 守卫, 仅在真实状态变化时才调用本函数,
        # 切换类操作已移除, 刷新循环仅在可见性真实变化时调用 _apply_visibility, 开销可接受。
        root.update_idletasks()

    def _show_row_menu(event, code):
        """右键弹出个股操作菜单(功能①④): 参数设置 + 删除。

        macOS 上 tk.Menu.tk_popup 实测不可靠(可能不弹出), 故 ⚙(参数)/🗑(删除) 左键按钮为兜底入口;
        本菜单保留给右键可用的平台/场景。
        """
        menu = tk.Menu(root, tearoff=0)
        menu.add_command(label="参数设置…", command=lambda: _open_param_panel(code))
        menu.add_command(label=f"删除 {code}", command=lambda: _confirm_remove(code))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _copy_code(c: str):
        """点击行情行: 将股票代码复制到系统剪贴板, 状态栏短暂提示(随下一轮刷新清空)。"""
        try:
            root.clipboard_clear()
            root.clipboard_append(c)
            status.config(text=f"已复制 {c}")
            root.update_idletasks()
        except Exception:
            pass

    def _on_row_click(e, c, tools):
        """行情行左键: 点在删除/排序按钮上则放行(由其自身 handler 处理), 其余区域复制代码。"""
        if e.widget in tools:
            return
        _copy_code(c)

    def _flash_row(code: str):
        """行情行异动闪动: 临时把该行(含所有子 Label)背景设为高亮色, 280ms 后还原。"""
        f = quote_frames.get(code)
        if f is None or not f.winfo_exists():
            return
        targets = [f] + list(rows.get(code, ()))
        for w in targets:
            try:
                w.config(bg=style["flash"])
            except Exception:
                pass

        def _revert():
            for w in targets:
                if w.winfo_exists():
                    try:
                        w.config(bg=style["bg"])
                    except Exception:
                        pass
        root.after(280, _revert)

    def _toggle_topmost():
        """切换窗口置顶(always-on-top)(功能④)。

        置顶开关已并入设置面板(由 top_panel_toggle 调用), header 不再保留 📌 按钮。
        本函数只负责状态翻转 + 真正置顶 + 状态栏文案, 不再触碰任何 header 按钮。
        """
        ui["topmost"] = not ui["topmost"]
        on = ui["topmost"]
        set_topmost(root, on)
        status.config(text=("📌 窗口置顶" if on else "📍 取消置顶"))

    # ---- 添加面板: 从 Toplevel 弹窗改为主窗口下方内联展示(功能①) ----
    add_panel_built = {"done": False}

    def _add_stock_dialog():
        """点击 ＋: 切换主窗口下方内联添加面板显隐(原 Toplevel 弹窗已移除)。

        面板控件在首次打开时一次性构建到 add_inline, 之后仅做 pack/pack_forget 切换。
        无 tkinter 时直接返回。
        """
        if tk is None or root is None:
            return
        if not add_panel_built["done"]:
            _build_add_panel()
            add_panel_built["done"] = True
        if panel_state["add"]:
            add_inline._close()          # 再次点击 ＋ -> 收起并重置
            panel_state["add"] = False
        else:
            # 互斥: 参数面板打开时先收起
            if panel_state["param"]:
                param_inline.pack_forget()
                panel_state["param"] = None
            # 钉宽度 = 当前主题窗口宽(收起态), 避免面板内部控件把 shrink-to-fit 窗口撑大
            add_inline.config(width=root.winfo_width())
            add_inline.pack(after=(settings_inline if panel_state["settings"] else status),
                            fill="x")
            panel_state["add"] = True
            root.update_idletasks()

    def _build_add_panel():
        """一次性在 add_inline 中构建设置控件: 显示名/搜索/手动代码/结果列表/添加取消。

        逻辑与原弹窗完全一致: 搜索走腾讯 smartbox(search_stocks) + 后台线程,
        经 root.after(0, _populate) 回填; 手动代码 _normalize_manual_code 归一化;
        显示名置于搜索框上方; _on_add/_on_select 回调 + _add_stock 调用。
        """
        add_inline.configure(bg=style["bg"])

        # 面板顶部分界线(与信号提示上方一致)
        add_sep = tk.Frame(add_inline, bg=style["sep"], height=1)
        add_sep.pack(fill="x", padx=4, pady=1)

        # 选中的结果 + 与 listbox index 平行的结果列表(闭包引用, 原地变更无需 nonlocal)
        selected = {"code": None, "name": None}
        results: List[dict] = []

        def _normalize_manual_code(raw: str):
            """手动代码归一化(功能②): 空→None; 以 sh/sz/hk/us 前缀开头→原样返回;
            纯 6 位数字→首位为 6 归 sh 否则 sz(如 600519→sh600519, 000589→sz000589);
            其余(长度/字符不符)→None。"""
            if not raw:
                return None
            s = raw.strip().lower()
            if not s:
                return None
            if s[:2] in ("sh", "sz", "hk", "us"):
                return s
            if len(s) == 6 and s.isdigit():
                return ("sh" if s[0] == "6" else "sz") + s
            return None

        def _populate(items: List[dict]):
            result_lb.delete(0, tk.END)
            results.clear()
            if not items:
                result_lb.insert(tk.END, "无匹配结果")
                return
            for it in items:
                results.append({"code": it["code"], "name": it["name"]})
                result_lb.insert(tk.END, f"{it['name']}  ({it['code']})")

        def _run_search(q: str):
            try:
                found = search_stocks(q, limit=10)
            except Exception:
                found = []
            # 回主线程刷新 UI, 避免跨线程操作 Tk widget
            root.after(0, lambda: _populate(found))

        def _do_search():
            q = q_entry.get().strip()
            if not q:
                return
            results.clear()
            result_lb.delete(0, tk.END)
            result_lb.insert(tk.END, "搜索中…")
            threading.Thread(target=lambda: _run_search(q), daemon=True).start()

        def _on_select(event=None):
            sel = result_lb.curselection()
            if not sel:
                return
            idx = sel[0]
            if idx >= len(results):
                return
            chosen = results[idx]
            selected["code"] = chosen["code"]
            selected["name"] = chosen["name"]
            name_entry.delete(0, tk.END)
            name_entry.insert(0, chosen["name"])

        def _on_add():
            code = selected["code"]
            if code is None:
                # 搜索未选择 → 退而求其次读手动代码输入框(功能②, 与搜索并存)
                raw = manual_code_entry.get()
                normalized = _normalize_manual_code(raw)
                if normalized is None:
                    status.config(text="代码格式无效" if raw.strip() else "请先搜索选择，或手动输入代码")
                    return
                code = normalized
            name = name_entry.get().strip() or code
            _add_stock({"code": code, "name": name})
            add_inline._close()

        def _on_cancel():
            add_inline._close()

        # 收起面板并重置子控件状态, 避免下次打开残留
        def _close():
            add_inline.pack_forget()
            try:
                name_entry.delete(0, tk.END)
                q_entry.delete(0, tk.END)
                manual_code_entry.delete(0, tk.END)
                result_lb.delete(0, tk.END)
                result_lb.insert(tk.END, "输入关键词后点击『搜索』")
            except Exception:
                pass
            selected["code"] = None
            selected["name"] = None
            results.clear()

        # ---- 控件 ----
        # 显示名(可修改): 置于顶部, 便于搜索选择后直接编辑再『添加』
        tk.Label(add_inline, text="显示名（可修改）",
                 bg=style["bg"], fg=style["fg_dim"],
                 font=style["FONT_SM"], anchor="w").pack(fill="x", padx=8, pady=(8, 2))
        name_entry = tk.Entry(add_inline, bg=style["bg"], fg=style["fg"], font=style["FONT_SM"])
        name_entry.pack(fill="x", padx=8, pady=(0, 4))

        tk.Label(add_inline, text="股票名称搜索（模糊匹配）",
                 bg=style["bg"], fg=style["fg"],
                 font=style["FONT_SM"], anchor="w").pack(fill="x", padx=8, pady=(8, 2))

        q_entry = tk.Entry(add_inline, bg=style["bg"], fg=style["fg"], font=style["FONT_SM"])
        q_entry.pack(fill="x", padx=8, pady=(0, 4))
        q_entry.bind("<Return>", lambda e: _do_search())
        q_entry.focus_set()

        tk.Button(add_inline, text="搜索", command=_do_search,
                  bg=style["bg"], fg=style["fg"], font=style["FONT_SM"]
                  ).pack(anchor="w", padx=8, pady=(0, 4))

        # 功能②: 手动代码输入入口(与搜索选择并存, 不替代搜索流程)
        tk.Label(add_inline, text="代码（手动添加，如 sh600519 / 600519）",
                 bg=style["bg"], fg=style["fg_dim"],
                 font=style["FONT_SM"], anchor="w").pack(fill="x", padx=8, pady=(0, 2))
        manual_code_entry = tk.Entry(add_inline, bg=style["bg"], fg=style["fg"], font=style["FONT_SM"])
        manual_code_entry.pack(fill="x", padx=8, pady=(0, 4))
        manual_code_entry.bind("<Return>", lambda e: _on_add())

        list_frame = tk.Frame(add_inline, bg=style["bg"])
        list_frame.pack(fill="both", expand=True, padx=8, pady=(0, 4))
        result_lb = tk.Listbox(list_frame, bg=style["bg"], fg=style["fg"],
                               font=style["FONT_SM"], selectmode=tk.SINGLE, height=8)
        scroll = tk.Scrollbar(list_frame, command=result_lb.yview)
        result_lb.config(yscrollcommand=scroll.set)
        result_lb.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        result_lb.bind("<<ListboxSelect>>", _on_select)
        result_lb.bind("<Double-Button-1>", _on_select)

        btn_row = tk.Frame(add_inline, bg=style["bg"])
        btn_row.pack(fill="x", padx=8, pady=(4, 8))
        tk.Button(btn_row, text="添加", command=_on_add,
                  bg=style["bg"], fg=style["fg"], font=style["FONT_SM"]
                  ).pack(side="left", padx=(0, 4))
        tk.Button(btn_row, text="取消", command=_on_cancel,
                  bg=style["bg"], fg=style["fg"], font=style["FONT_SM"]
                  ).pack(side="left")

        result_lb.insert(tk.END, "输入关键词后点击『搜索』")
        # 暴露「收起 + 重置」句柄给外层 toggle 与内部回调使用
        add_inline._close = _close

    # ---- 个股参数面板(功能④): support / resistance / chg_alert / swing_alert 配置(内联) ----
    # 入口: 行情行 ⚙ 按钮(左键, mac 可靠) + 右键菜单「参数设置…」。后端已支持个股级四参数
    # (normalize 透传 + worker 个股优先 + _serialize_stocks 已含), 本面板补齐 UI 落盘。
    param_inline = tk.Frame(center, bg=style["bg"])
    param_inline.pack_forget()
    param_built = {"done": False}
    param_vars: Dict[str, "tk.StringVar"] = {}
    param_title_holder: Dict[str, object] = {"label": None}

    def _open_param_panel(code: str):
        """打开(或切换至)个股参数面板; 与设置/添加面板互斥。"""
        # 守卫: hide_param 开启时 ⚙ 按钮已视觉隐藏, 拒绝误点
        if bool(settings.get("hide_param", False)):
            return
        if not any(st["code"] == code for st in stocks):
            return
        # 互斥收起其他面板
        if panel_state["settings"]:
            settings_inline.pack_forget()
            panel_state["settings"] = False
        if panel_state["add"]:
            add_inline._close()
            panel_state["add"] = False
        if not param_built["done"]:
            _build_param_panel()
            param_built["done"] = True
        cur = next(s for s in stocks if s["code"] == code)
        # 填充当前值: support/resistance 逗号连接; chg/swing 留空 = 未配置(回退全局)
        param_vars["support"].set(", ".join(str(x) for x in (cur.get("support") or [])))
        param_vars["resistance"].set(", ".join(str(x) for x in (cur.get("resistance") or [])))
        param_vars["chg_alert"].set("" if cur.get("chg_alert") is None else str(cur["chg_alert"]))
        param_vars["swing_alert"].set("" if cur.get("swing_alert") is None else str(cur["swing_alert"]))
        if param_title_holder["label"] is not None:
            param_title_holder["label"].config(text=f"参数设置 — {cur.get('name', code)} ({code})")
        param_inline.config(width=root.winfo_width())
        param_inline.pack(after=status, fill="x")
        panel_state["param"] = code
        root.update_idletasks()

    def _build_param_panel():
        """一次性在 param_inline 中构建: 标题 + 4 输入行 + 保存/取消 + 提示。"""
        param_inline.configure(bg=style["bg"])
        param_title_holder["label"] = tk.Label(
            param_inline, text="参数设置", bg=style["bg"], fg=style["fg"],
            font=style["FONT_SM"], anchor="w")
        param_title_holder["label"].pack(fill="x", padx=8, pady=(4, 0))
        tk.Label(param_inline,
                 text="支撑/压力可多个(逗号分隔); 变动/波动提示单位 %; 留空 = 不配置(回退全局)",
                 bg=style["bg"], fg=style["fg_dim"], font=style["FONT_SM"], anchor="w"
                 ).pack(fill="x", padx=8, pady=(0, 2))

        def make_param_row(label_text: str, key: str):
            frm = tk.Frame(param_inline, bg=style["bg"])
            frm.pack(fill="x", padx=8, pady=2)
            tk.Label(frm, text=label_text, bg=style["bg"], fg=style["fg"],
                     font=style["FONT_SM"], width=13, anchor="w").pack(side="left")
            var = tk.StringVar()
            ent = tk.Entry(frm, textvariable=var, bg=style["bg"], fg=style["fg"],
                           font=style["FONT_SM"])
            ent.pack(side="left", fill="x", expand=True)
            param_vars[key] = var

        make_param_row("支撑位 support", "support")
        make_param_row("压力位 resistance", "resistance")
        make_param_row("变动提示 % chg_alert", "chg_alert")
        make_param_row("波动提示 % swing_alert", "swing_alert")

        btn_row = tk.Frame(param_inline, bg=style["bg"])
        btn_row.pack(fill="x", padx=8, pady=(4, 8))
        tk.Button(btn_row, text="保存", command=_save_param,
                  bg=style["bg"], fg=style["fg"], font=style["FONT_SM"]
                  ).pack(side="left", padx=(0, 4))
        tk.Button(btn_row, text="取消", command=_close_param,
                  bg=style["bg"], fg=style["fg"], font=style["FONT_SM"]
                  ).pack(side="left")

    def _save_param():
        """保存当前个股参数: 更新内存(worker 即刻生效) + 落盘 stocks.toml + 收起面板。"""
        code = panel_state["param"]
        if not code:
            return
        cur = next((s for s in stocks if s["code"] == code), None)
        if cur is None:
            return
        support = parse_levels_txt(param_vars["support"].get())
        resistance = parse_levels_txt(param_vars["resistance"].get())
        ca = parse_pct_txt(param_vars["chg_alert"].get())
        sa = parse_pct_txt(param_vars["swing_alert"].get())
        # 更新内存: worker 即刻生效; 清空 chg/swing = 回退全局值(与 load_stocks setdefault 语义一致)
        if support is None:
            cur.pop("support", None)
        else:
            cur["support"] = support
        if resistance is None:
            cur.pop("resistance", None)
        else:
            cur["resistance"] = resistance
        if ca is None:
            cur["chg_alert"] = _to_float(settings.get("chg_alert")) or 0.0
        else:
            cur["chg_alert"] = ca
        if sa is None:
            cur["swing_alert"] = _to_float(settings.get("swing_alert")) or 0.0
        else:
            cur["swing_alert"] = sa
        # 落盘: update_data 中 None = 移除字段(重启后回退全局/无配置)
        update_data = {"support": support, "resistance": resistance,
                       "chg_alert": ca, "swing_alert": sa}
        if stocks_path:
            try:
                rewrite_stocks_toml(stocks_path, update_code=code, update_data=update_data)
            except Exception as e:
                status.config(text=f"写入 stocks.toml 失败: {e}")
                return
        status.config(text=f"已保存 {code} 参数")
        _close_param()

    def _close_param():
        """收起参数面板并刷新几何。"""
        param_inline.pack_forget()
        panel_state["param"] = None
        root.update_idletasks()


    def _add_stock(parsed):
        """把解析后的股票加入内存列表 + GUI + 回写 stocks.toml(功能①)。"""
        code = parsed["code"]
        if any(st["code"] == code for st in stocks):
            status.config(text=f"{code} 已在自选, 忽略")
            return
        stocks.append(parsed)
        warm_klines([parsed])
        _build_quote_row(parsed)
        # 新加的行立即按当前隐藏开关显隐排序/删除按钮
        _apply_row_tools_visibility()
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
        # 守卫: hide_del 开启时删除按钮已视觉隐藏, 拒绝误点
        if bool(settings.get("hide_del", False)):
            return
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
            # 行情行 frame 被 destroy 时, 内部 del/up/down 子 Label 一并销毁;
            # 必须同步清理这两个字典, 否则残留已销毁引用, 后续 _apply_row_tools_visibility
            # 遍历到它们调 .config() 会抛 TclError: invalid command name
            "del_btns": del_btns,
            "move_btns": move_btns,
            "param_btns": param_btns,
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
    # 启动即按 config 的隐藏设置生效(隐藏排序/删除按钮)
    _apply_row_tools_visibility()

    # ---- 分隔线 + 下半部分: 信号提示 ----
    sep = tk.Frame(center, bg=style["sep"], height=1)
    sep.pack(fill="x", padx=4, pady=1)
    sighead = tk.Label(center, text="信号提示", bg=style["bg"], fg=style["fg_dim"],
                       font=style["FONT_SM"], anchor="w")
    sighead.pack(fill="x", padx=4, pady=(0, 0))

    sigpane = tk.Frame(center, bg=style["bg"])
    sigpane.pack(fill="x")
    sig_rows: Dict[str, tuple] = {}
    for st in stocks:
        _build_sig_row(st)

    # ---- 底部状态 ----
    status = tk.Label(center, text="连接中…", bg=style["bg"], fg=style["fg_dim"], font=style["FONT_SM"], anchor="w")
    status.pack(fill="x", padx=2, pady=(0, 0))

    # 初始定位到右上角: 只设位置不设尺寸, 让 Tk 根据内容自适应(避免硬编码 205/280 过窄/过宽)。
    # 首轮 refresh 时用 winfo_reqwidth() 捕获内容实际所需宽度并锁定, 不多不少。
    root.update_idletasks()
    sw = root.winfo_screenwidth()
    root.geometry(f"+{max(0, sw - 205)}+20")

    # ---- 数据层 ----
    data: Dict[str, dict] = {}                 # code -> rec(最近一次成功)
    last_sigs: Dict[str, dict] = {}            # code -> {sig, 阈值状态, last_price, ...}
    lock = threading.Lock()
    # 共享状态(后台线程读/主线程写; 单键原子操作在 CPython 下安全)
    state = {"refresh_sec": refresh_sec}
    refresh_event = threading.Event()   # ↺ 立即刷新信号

    def _force_refresh():
        """↺ 立即触发一次后台取数(获取最新行情)。"""
        refresh_event.set()
        status.config(text="↺ 刷新中…")

    def _cycle_freq():
        cur = state["refresh_sec"]
        nxt = {1: 5, 3: 5, 5: 10, 10: 1}.get(cur, 1)
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
        if freq_btn["w"] is not None:
            freq_btn["w"].config(text=f"刷新频率：{nxt}s")

    # ---- 信号提示显隐开关(功能①: 运行时态, 不落盘) ----
    def _toggle_signal():
        """切换信号提示显隐(功能①)。关闭时整块隐藏(含标题/分隔线/容器); 开启时恢复。"""
        ui["show_signal"] = not ui["show_signal"]
        on = ui["show_signal"]
        # 整块显隐: 分隔线 / 信号标题 / 信号容器
        # 按 on 直接 pack/pack_forget(pack 对已映射控件幂等, pack_forget 对未映射是无操作),
        # 不依赖 winfo_ismapped()(macOS Tk 上该值不可信, 会导致该隐不隐/该显不显)。
        # before=status 维持原始上下顺序(否则布局错乱)。
        if on:
            sep.pack(fill="x", padx=4, pady=1, before=status)
            sighead.pack(fill="x", padx=4, pady=(0, 0), before=status)
            sigpane.pack(fill="x", before=status)
        else:
            sep.pack_forget()
            sighead.pack_forget()
            sigpane.pack_forget()
        # 各股票信号行按 show_signal + is_row_visible 联合判定
        for st in stocks:
            c = st["code"]
            vis = on and is_row_visible(last_sig_change.get(c), SIG_CHANGE_WINDOW_SEC)
            if row_vis.get(c, True) != vis:
                _apply_visibility(c, vis)
                row_vis[c] = vis
        status.config(text=("🔔 信号提示开" if on else "🔕 信号提示关"))
        root.update_idletasks()

    # ---- 设置面板: 透明度 + 灰度(实时生效 + 持久化) ----
    # ---- 设置面板: 从 Toplevel 弹窗改为主窗口下方内联展示(透明度 + 灰度, 实时生效 + 持久化) ----
    settings_panel_built = {"done": False}

    def _open_settings_panel():
        """点击 ⚙️: 切换主窗口下方内联设置面板显隐(原 Toplevel 弹窗已移除)。

        面板控件在首次打开时一次性构建到 settings_inline, 之后仅做 pack/pack_forget 切换。
        无 tkinter 时直接返回。
        """
        if tk is None:
            return
        if not settings_panel_built["done"]:
            _build_settings_panel()
            settings_panel_built["done"] = True
        if panel_state["settings"]:
            settings_inline.pack_forget()          # 再次点击 ⚙️ -> 收起
            panel_state["settings"] = False
        else:
            # 互斥: 参数面板打开时先收起
            if panel_state["param"]:
                param_inline.pack_forget()
                panel_state["param"] = None
            # 钉宽度 = 当前主题窗口宽(收起态), 避免面板内部控件比行情行宽时把 shrink-to-fit 窗口撑大
            settings_inline.config(width=root.winfo_width())
            settings_inline.pack(after=status, fill="x")
            panel_state["settings"] = True
            root.update_idletasks()

    def _build_settings_panel():
        """一次性在 settings_inline 中构建设置控件: 透明度/灰度滑块 + 变动消息开关。

        布局/回调逻辑与原弹窗一致; 设置实时生效且自动持久化(alpha/grayness 拖动即存),
        面板由 ⚙️ 切换显隐, 无独立保存/取消按钮(显示变动开关已还原回 header)。
        """
        # 面板顶部分界线(与信号提示上方一致)
        settings_sep = tk.Frame(settings_inline, bg=style["sep"], height=1)
        settings_sep.pack(fill="x", padx=4, pady=1)

        def make_slider(label_text, from_, to_, default_val, resolution, on_change):
            """创建一行: 标签 + 滑块 + 数值显示。"""
            frm = tk.Frame(settings_inline, bg=style["bg"])
            frm.pack(fill="x", padx=8, pady=4)
            lbl = tk.Label(frm, text=label_text, bg=style["bg"], fg=style["fg"],
                          font=style["FONT_SM"], anchor="w", width=6)
            lbl.pack(side="left")
            var = tk.DoubleVar(value=default_val)
            scl = tk.Scale(frm, from_=from_, to=to_, resolution=resolution,
                          orient="horizontal", variable=var,
                          bg=style["bg"], fg=style["fg"],
                          highlightthickness=0, troughcolor=style["header"],
                          command=lambda v, cb=on_change: cb(float(v)))
            scl.pack(side="left", fill="x", expand=True, padx=(4, 0))
            return var, scl

        # --- 透明度 ---
        cur_alpha = settings.get("float_alpha", ALPHA_DEFAULT)
        make_slider(
            "透明度", 0.30, 1.0, float(cur_alpha), 0.01,
            lambda val: _apply_alpha(val))
        # --- 灰度 ---
        cur_gray = max(0.0, min(1.0, float(settings.get("grayness") or 0.0)))
        make_slider(
            "灰度", 0.0, 1.0, cur_gray, 0.05,
            lambda val: _apply_grayness(val))

        # --- 涨/跌色配置: 色块预览 + 当前 hex + 「选择」按钮, 弹系统颜色面板 ---
        # macOS 上 tkinter.colorchooser.askcolor 调起的是 NSColorPanel(效果如图), 可视化选择 HSL。
        # 写入 settings / config.toml 的都是「原色」(未应用灰度), build_style 内部 desaturate 后再渲。
        if colorchooser is not None:
            colors_row = tk.Frame(settings_inline, bg=style["bg"])
            colors_row.pack(fill="x", padx=8, pady=(0, 4))
            colors_row.columnconfigure(0, weight=1)

            def make_color_picker(parent, label_text: str, key: str, default_hex: str, row: int):
                """一行颜色选择器: 色块 + 「标签 #hex」+ 「选择」按钮; 选完实时持久化 + 重渲。

                每行一个颜色(涨/跌各占一行), 占满整行宽度, 便于色块/hex/按钮横向舒展。
                """
                # 当前原色(未灰度): settings 里有就 settings, 没有就默认
                cur = _hex_color(settings.get(key), default_hex)
                cell = tk.Frame(parent, bg=style["bg"])
                cell.grid(row=row, column=0, padx=4, pady=4, sticky="ew")
                cell.columnconfigure(1, weight=1)

                swatch = tk.Label(cell, bg=cur, width=3, relief="solid", bd=1, cursor="hand2")
                swatch.grid(row=0, column=0, padx=(0, 6))

                txt = tk.Label(cell, text=f"{label_text} {cur}", bg=style["bg"],
                               fg=style["fg"], font=style["FONT_SM"], anchor="w")
                txt.grid(row=0, column=1, sticky="ew", padx=(0, 4))

                def _pick():
                    """弹系统颜色选择器, 选完落 settings + config.toml + 重渲浮窗色。"""
                    # initialcolor 需要 #rrggbb 形式
                    initial = _hex_color(settings.get(key), default_hex)
                    try:
                        rgb, hexv = colorchooser.askcolor(
                            color=initial, parent=root, title=f"选择{label_text}")
                    except Exception:
                        return  # 无 GUI 兜底
                    if not hexv:                # 用户取消
                        return
                    hex_norm = hexv.lower()
                    if not re.fullmatch(r"#([0-9a-fA-F]{6})", hex_norm):
                        return                  # 防御: askcolor 几乎总返回合法值
                    settings[key] = hex_norm    # 原色入 settings(灰度由 build_style 应用)
                    _save_config_key(key, hex_norm)
                    swatch.config(bg=hex_norm)
                    txt.config(text=f"{label_text} {hex_norm}")
                    _reapply_style()            # 全量重刷(色已即时生效)
                    root.update_idletasks()

                btn = tk.Button(
                    cell, text="选择", bg=style["bg"], fg=style["fg"], font=style["FONT_SM"],
                    cursor="hand2", relief="flat", padx=8, pady=1,
                    command=_pick)
                btn.grid(row=0, column=2)

            # 默认色: 浅色主题涨 #d33 / 跌 #38d (与 build_style 内置 pal 一致; 缺省回退保证色块不为空)
            make_color_picker(colors_row, "涨色", "float_up_color",   "#d33d3d", row=0)
            make_color_picker(colors_row, "跌色", "float_down_color", "#3dc23d", row=1)

        # --- 4 个开关: 2 行 2 列网格, 紧凑布局 ---
        toggles_grid = tk.Frame(settings_inline, bg=style["bg"])
        toggles_grid.pack(fill="x", padx=8, pady=4)
        toggles_grid.columnconfigure(0, weight=1)
        toggles_grid.columnconfigure(1, weight=1)
        # 按钮换行宽度: 按当前窗口半宽(锁定后=主题宽)计算, 防止锁定宽度后长文案(如"隐藏排序：开")
        # 被截断; 超出则自动换行而非撑大窗口。
        _wl = max(40, (root.winfo_width() or 200) // 2 - 20)

        # --- 变动消息提示开关(控制 OS 弹框+声音, 持久化) ---
        def _toggle_notify():
            on = not notifier.enabled
            notifier.enabled = on
            notifier.sound = on          # 单总开关: 弹框与声音同开同关
            _save_config_key("notify", on)
            _save_config_key("notify_sound", on)
            notify_toggle.config(
                text=("变动消息：开" if on else "变动消息：关"))
        notify_toggle = tk.Button(
            toggles_grid,
            text=("变动消息：开" if notifier.enabled else "变动消息：关"),
            bg=style["bg"], fg=style["fg"], font=style["FONT_SM"],
            cursor="hand2", relief="flat", padx=6, pady=2, wraplength=_wl)
        notify_toggle.config(command=_toggle_notify)
        notify_toggle.grid(row=0, column=1, padx=4, pady=4, sticky="ew")

        # --- 窗口置顶开关(与 header 📌 按钮同源, 复用 _toggle_topmost) ---
        def _toggle_topmost_panel():
            _toggle_topmost()
            on = ui["topmost"]
            top_panel_toggle.config(
                text=("窗口置顶：开" if on else "窗口置顶：关"))
        top_panel_toggle = tk.Button(
            toggles_grid,
            text=("窗口置顶：开" if ui["topmost"] else "窗口置顶：关"),
            bg=style["bg"], fg=style["fg"], font=style["FONT_SM"],
            cursor="hand2", relief="flat", padx=6, pady=2, wraplength=_wl)
        top_panel_toggle.config(command=_toggle_topmost_panel)
        top_panel_toggle.grid(row=0, column=0, padx=4, pady=4, sticky="ew")

        # --- 隐藏排序功能开关(持久化 hide_sort) ---
        def _toggle_hide_sort():
            new = not bool(settings.get("hide_sort", False))
            settings["hide_sort"] = new
            _save_config_key("hide_sort", new)
            hide_sort_toggle.config(
                text=("隐藏排序：开" if new else "显示排序：关"))
            _apply_row_tools_visibility()
        hide_sort_toggle = tk.Button(
            toggles_grid,
            text=("隐藏排序：开" if settings.get("hide_sort", False) else "显示排序：关"),
            bg=style["bg"], fg=style["fg"], font=style["FONT_SM"],
            cursor="hand2", relief="flat", padx=6, pady=2, wraplength=_wl)
        hide_sort_toggle.config(command=_toggle_hide_sort)
        hide_sort_toggle.grid(row=1, column=0, padx=4, pady=4, sticky="ew")

        # --- 隐藏删除功能开关(持久化 hide_del) ---
        def _toggle_hide_del():
            new = not bool(settings.get("hide_del", False))
            settings["hide_del"] = new
            _save_config_key("hide_del", new)
            hide_del_toggle.config(
                text=("隐藏删除：开" if new else "显示删除：关"))
            _apply_row_tools_visibility()
        hide_del_toggle = tk.Button(
            toggles_grid,
            text=("隐藏删除：开" if settings.get("hide_del", False) else "显示删除：关"),
            bg=style["bg"], fg=style["fg"], font=style["FONT_SM"],
            cursor="hand2", relief="flat", padx=6, pady=2, wraplength=_wl)
        hide_del_toggle.config(command=_toggle_hide_del)
        hide_del_toggle.grid(row=1, column=1, padx=4, pady=4, sticky="ew")

        # --- 隐藏参数按钮开关(持久化 hide_param, 控制行情行右侧 ⚙ 是否显示) ---
        def _toggle_hide_param():
            new = not bool(settings.get("hide_param", False))
            settings["hide_param"] = new
            _save_config_key("hide_param", new)
            hide_param_toggle.config(
                text=("隐藏参数按钮：开" if new else "显示参数按钮：关"))
            _apply_row_tools_visibility()
        hide_param_toggle = tk.Button(
            toggles_grid,
            text=("隐藏参数按钮：开" if settings.get("hide_param", False) else "显示参数按钮：关"),
            bg=style["bg"], fg=style["fg"], font=style["FONT_SM"],
            cursor="hand2", relief="flat", padx=6, pady=2, wraplength=_wl)
        hide_param_toggle.config(command=_toggle_hide_param)
        hide_param_toggle.grid(row=2, column=0, padx=4, pady=4, sticky="ew")

        # --- 刷新频率切换(已从 header 移至设置面板, row=2 col=1 紧邻 hide_param) ---
        def _toggle_freq():
            _cycle_freq()
        freq_toggle = tk.Button(
            toggles_grid,
            text=f"刷新频率：{refresh_sec}s",
            bg=style["bg"], fg=style["fg"], font=style["FONT_SM"],
            cursor="hand2", relief="flat", padx=6, pady=2, wraplength=_wl)
        freq_toggle.config(command=_toggle_freq)
        freq_toggle.grid(row=2, column=1, padx=4, pady=4, sticky="ew")
        freq_btn["w"] = freq_toggle

        def _close_settings():
            """点击收起: 隐藏设置面板并刷新几何。

            用 pack_forget + update_idletasks 收起(macOS 上显隐后须刷新几何,
            否则隐藏不生效/粘住)。若添加面板(add_inline)正打开且曾挂在设置面板
            之后, 一并重排回 status 之后, 避免折叠顺序错乱。
            """
            settings_inline.pack_forget()
            panel_state["settings"] = False
            if add_inline is not None and panel_state["add"]:
                add_inline.pack_forget()
                add_inline.config(width=root.winfo_width())
                add_inline.pack(after=status, fill="x")
            root.update_idletasks()

        collapse_btn = tk.Button(
            settings_inline,
            text="▾ 收起设置",
            bg=style["bg"], fg=style["fg_dim"], font=style["FONT_SM"],
            cursor="hand2", relief="flat", padx=8, pady=3,
            command=_close_settings)
        collapse_btn.pack(pady=(6, 2), padx=8, anchor="e")   # 右对齐, 收起入口

    # 重入保护标志(dict 避免 nonlocal 复杂度): 在 macOS Tk 上, _reapply_style 末尾的
    # root.update_idletasks() 会从当前活动 Scale 的 -command 回调内部重入事件循环,
    # 再次触发同一滑块 command, 形成无限嵌套 -> RecursionError。alpha/grayness 共用
    # 同一守卫, 任一滑块回调重入时直接跳过实质工作, 递归被彻底切断。
    _style_busy = {"v": False}

    def _apply_alpha(val: float):
        """实时应用透明度(alpha 边界 clamp 在 [0.30, 1.0])。

        含重入保护: 与 _apply_grayness 共用 _style_busy 守卫, 避免 macOS Tk 在
        update_idletasks 中重触发 Scale command 导致递归(稳健性一致处理)。
        """
        def _work():
            v = max(0.30, min(1.0, val))           # 安全边界
            root.attributes("-alpha", v)
            nonlocal alpha
            alpha = v
            ui["_alpha_base"] = v                  # 同步淡出基值, 淡出后以此还原
            settings["float_alpha"] = v
            _save_config_key("float_alpha", v)
        _run_with_guard(_style_busy, _work)

    def _apply_grayness(val: float):
        """实时应用灰度(重算 style 并全量重刷 widget 配色)。

        含重入保护: 避免 macOS Tk 在 update_idletasks 中重触发 Scale command 导致
        无限递归(RecursionError)。嵌套触发的 _apply_grayness 因 _style_busy 标志为
        True 被 _run_with_guard 跳过, 由外层调用完成样式刷新并复位标志。
        """
        def _work():
            v = max(0.0, min(1.0, val))
            settings["grayness"] = v
            nonlocal style
            style = build_style(settings)          # 重算含新灰度的完整样式字典
            _reapply_style()                       # 全量重刷配色
            _save_config_key("grayness", v)
        _run_with_guard(_style_busy, _work)

    def _reapply_style():
        """用当前 style 字典重刷所有可见 widget 配色(灰度变更后调用)。

        涨跌幅 chg_l 不动(保留红涨绿跌语义), 交由下一轮刷新循环按新 up/down 自然更新;
        中性文本/背景色与 header 按钮立即刷新。
        """
        nonlocal up_color, down_color, sig_colors
        # 刷新随灰度变化的源色, 使下一轮刷新循环自动用新 up/down/信号色
        up_color = style["up"]
        down_color = style["down"]
        sig_colors = style["sig_colors"]
        root.configure(bg=style["bg"])
        header.configure(bg=style["header"])
        htitle.configure(bg=style["header"], fg=style["fg_dim"])
        # 容器背景随灰度刷新(content/center 始终存在; 两内联面板仅在已构建后刷新)
        content.configure(bg=style["bg"])
        center.configure(bg=style["bg"])
        if settings_panel_built.get("done"):
            settings_inline.configure(bg=style["bg"])
        if add_panel_built.get("done"):
            add_inline.configure(bg=style["bg"])
        status.configure(bg=style["bg"], fg=style["fg_dim"])
        sighead.configure(bg=style["bg"], fg=style["fg_dim"])
        sep.configure(bg=style["sep"])
        # 所有行情行背景 + 中性文本色(涨跌幅 chg_l 不动, 交由刷新循环更新)
        for code, (sig_l, name_l, price_l, chg_l, dl_l) in rows.items():
            parent = name_l.master
            parent.configure(bg=style["bg"])
            name_l.configure(fg=style["fg"])
            price_l.configure(fg=style["fg"])
            dl_l.configure(fg=style["dl"])
            sig_l.configure(fg=style["flat"])                  # 左侧中性信号点随灰度
        # 信号行
        for code, (sdot, slv, sbb) in sig_rows.items():
            parent = sdot.master
            parent.configure(bg=style["bg"])
            slv.configure(fg=style["fg_dim"])
            sbb.configure(fg=style["fg_dim"])
            # sdot 语义色保持 sig_colors(由刷新循环按信号着色, 此处不动)
        # header 按钮(统一底色 + dim 高亮; 特殊高亮由各 toggle 自管)
        for btn in (refresh_btn, add_btn, sig_btn, set_btn):
            btn.configure(bg=style["header"], fg=style["fg_dim"])
        root.update_idletasks()

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

    # ---- 配置热重载(B5): 后台线程轮询 stocks.toml / config.toml 的 mtime, ----
    # 发现"外部编辑"(mtime 超过自身写时间戳 + 缓冲)时, 经 root.after(0, ...) 在主线程安全重载,
    # 避免 Tk 跨线程调用崩溃。重载本身不写文件(绕开 _apply_alpha/_apply_grayness 的持久化分支),
    # 故不会触发自身写 -> 无重载循环。
    def _reload_settings():
        """主线程执行: 重新读取并应用 [settings](外观/开关), 不回写文件。"""
        nonlocal alpha, style
        new = load_settings()
        for k in ("float_alpha", "grayness", "topmost", "show_signal",
                  "hide_sort", "hide_del", "hide_param", "notify", "float_theme",
                  "float_font", "float_font_size", "refresh_sec"):
            if k in new:
                settings[k] = new[k]
        # 透明度(直接改属性, 不持久化, 不覆盖基值逻辑之外的东西)
        v = max(0.30, min(1.0, _to_float(settings.get("float_alpha")) or 1.0))
        root.attributes("-alpha", v)
        alpha = v
        ui["_alpha_base"] = v
        # 灰度(重算 style + 重刷配色, 不持久化)
        style = build_style(settings)
        _reapply_style()
        # 置顶
        set_topmost(root, bool(settings.get("topmost", True)))
        ui["topmost"] = bool(settings.get("topmost", True))
        # 信号提示开关同步
        ns = bool(settings.get("show_signal", True))
        if ns != ui["show_signal"]:
            _toggle_signal()
        # 隐藏排序/删除开关同步
        _apply_row_tools_visibility()

    def _reload_stocks():
        """主线程执行: 重新读取 stocks.toml, 增量增删 + 按新顺序重排行情行, 不回写文件。"""
        new = load_stocks(settings)
        new_codes = [s["code"] for s in new]
        old_set = set(quote_frames.keys())
        new_set = set(new_codes)
        # 删除已不在列表的(会触发 rewrite_stocks_toml -> 置 LAST_STOCKS_WRITE_T, 被守卫屏蔽)
        for code in old_set - new_set:
            _remove_stock(code)
        # 新增
        for s in new:
            if s["code"] not in old_set:
                _build_quote_row(s)
        # 按新顺序重排行情行(重新 pack 到父容器末尾, 维持新序)
        for s in new:
            f = quote_frames.get(s["code"])
            if f is not None and f.winfo_exists():
                f.pack(**QUOTE_PACK)
        stocks[:] = new
        _apply_row_tools_visibility()
        _refresh_move_buttons()

    def _check_reload():
        """守护线程执行: 仅做 mtime 判定, 真正的 UI 重载经 root.after(0,...) 调度到主线程。"""
        do_stocks = False
        do_config = False
        if stocks_path and os.path.exists(stocks_path):
            mt = os.path.getmtime(stocks_path)
            if mt > cfg_poll["stocks_mt"] + 0.01 and mt > LAST_STOCKS_WRITE_T + 0.5:
                cfg_poll["stocks_mt"] = mt
                do_stocks = True
        if config_path and os.path.exists(config_path):
            mt = os.path.getmtime(config_path)
            if mt > cfg_poll["config_mt"] + 0.01 and mt > LAST_CONFIG_WRITE_T + 0.5:
                cfg_poll["config_mt"] = mt
                do_config = True
        if do_stocks:
            root.after(0, _reload_stocks)
        if do_config:
            root.after(0, _reload_settings)

    def _watcher():
        while True:
            time.sleep(2)
            try:
                _check_reload()
            except Exception:
                pass

    def worker():
        while True:
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
                # 初次观测(prev_sig 为 None)不计为"变动": 否则启动瞬间所有股都被打时间戳 → 全假变动可见 300s
                if signal_became_changed(prev_sig, sig):
                    last_sig_change[code] = time.time()  # 功能②: 记录信号变动时间戳(仅真实档位变动)
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
                # 行情行异动闪动(B6): 真实变动(非首屏)才闪, 避免启动瞬间全屏闪。
                # 仅置标记, 真正改背景色由主线程 refresh 经 _flash_row 执行(Tk 线程安全)。
                if trigger and prev is not None:
                    flash_pending[code] = True
            with lock:
                data.update(new)
            if refresh_event.wait(timeout=state.get("refresh_sec", 1)):
                refresh_event.clear()  # ↺ 触发, 立即进入下一轮取数

    def refresh():
        with lock:
            snap = dict(data)
            sig_changes = dict(last_sig_change)
        offline = not snap
        # 窗口宽度锁定(首轮 capture + 每轮兜底): Configure 实时守卫锁宽度,
        # 本处周期级兜底(1~10s)作为冗余。高度完全放行——由内容自然撑高/收缩。
        # 首轮用内容理想宽度(行情行 body 请求宽, 含被 Canvas 遮挡的标签宽度; root 的
        # reqwidth 此时只反映画布 width=1 + 滚动条, 偏小), 避免初始尺寸过窄/过宽。
        w = root.winfo_width()
        if not width_locked["v"]:
            req_w = max(root.winfo_reqwidth(), body.winfo_reqwidth())
            # 下限 180 兜底(req_w 可能为 1: 窗口刚初始化, 内容未完成布局)
            target = max(req_w - 150, 180)
            if target > 0:
                root.minsize(target, 1)
                root.maxsize(target, 100000)
                width_locked["v"] = True
                width_locked["target"] = target
        elif w and w > 0:
            if w != width_locked["target"]:
                # 兜底: 宽度被拉大(极少情况, Configure 守卫已拦下绝大多数) → 强制恢复
                h = root.winfo_height()
                root.geometry(f"{width_locked['target']}x{h}")
                root.update_idletasks()
        for st in stocks:
            code = st["code"]
            # 信号行(下半)默认只展示有信号变动的股票; 信号提示关闭时一律隐藏(运行时态短路)
            visible = ui["show_signal"] and is_row_visible(sig_changes.get(code), SIG_CHANGE_WINDOW_SEC)
            if row_vis.get(code, True) != visible:
                _apply_visibility(code, visible)
                row_vis[code] = visible
            r = snap.get(code)
            sig_l, name_l, price_l, chg_l, dl_l = rows[code]
            if not r:
                continue
            price = r.get("price")
            chg = r.get("chg_pct")
            sig = r.get("signal")
            delayed = bool(r.get("delayed"))
            name_l.config(text=format_stock_name(st["name"], delayed))
            price_l.config(text=f"{price:.2f}" if isinstance(price, (int, float)) else "--")
            if isinstance(chg, (int, float)):
                col = up_color if chg > 0 else (down_color if chg < 0 else style["flat"])
                sign = "+" if chg > 0 else ""
                chg_l.config(text=f"{sign}{chg:.2f}%", fg=col)
            else:
                chg_l.config(text="", fg=style["flat"])
            sig_l.config(text="●", fg=sig_colors.get(sig, style["flat"]))
            # 延时标记已并入股票名(format_stock_name 处理), 此处留空避免重复
            dl_l.config(text="", fg=style["dl"])
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
            # 行情行异动闪动(B6): 消费 worker 置的标记, 在主线程安全改背景色(280ms 后还原)
            if flash_pending.pop(code, False):
                _flash_row(code)
        # 不再常驻显示「● 实时 · Ns」; 正常态清空状态栏(临时提示约 250ms 后随下一轮刷新自然消失),
        # 仅离线时给出提示, 避免窗口常驻冗余信息。
        if offline:
            status.config(text="○ 离线(显示上次)  ·  重试中")
        else:
            status.config(text="")
        root.after(250, refresh)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    # 配置热重载守护线程(B5): 每 2s 轮询文件 mtime, 外部改动时主线程安全重载
    tw = threading.Thread(target=_watcher, daemon=True)
    tw.start()
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
    # 配置热重载(B5): 解析 config.toml 实际路径, 供运行时检测外部编辑并重载 [settings]
    res_cfg = _load_first(SETTINGS_CANDIDATES)
    config_path = res_cfg[0] if res_cfg is not None else None

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
            notifier=notifier,
            cooldown=cooldown,
            stocks_path=stocks_path,
            config_path=config_path)


if __name__ == "__main__":
    main()
