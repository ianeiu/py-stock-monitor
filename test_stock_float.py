#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stock_float.py 独立测试套件 (QA 工程师严过关)。

约束:
- 仅用 Python 标准库 (unittest), 零第三方依赖。
- 本环境无 DISPLAY, 严禁实例化 Tk / 调用 run_hud/mainloop。
  GUI 相关项(暂停按钮 / 频率控件 / Windows 闪烁 / 暗色渲染)
  仅做逻辑层 / 纯函数验证, 并明确标注「需真机验证」。
- 网络与 GUI 通过 unittest.mock 隔离。

运行:
  python3 -m unittest test_stock_float.py -v
"""

import ast
import contextlib
import csv
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import stock_float as sf


# ----------------------------------------------------------------------------
# 测试辅助
# ----------------------------------------------------------------------------
def rt(price, prev_close=None, open_px=None, ts="", high=None, low=None,
       volume=None, delayed=False):
    """构造实时行情 8 元组 (price, prev_close, open_px, ts, high, low, volume, delayed)。"""
    pc = price if prev_close is None else prev_close
    op = price if open_px is None else open_px
    return (price, pc, op, ts, high, low, volume, delayed)


def full_rec(code, signal, net, price=10.0, **kw):
    """构造一个填满 CSV_FIELDS 的 rec (用于 --stats/--review 测试)。"""
    rec = {k: "" for k in sf.CSV_FIELDS}
    rec.update(dict(
        datetime="2026-07-15 09:30:00", code=code, name=code.upper(),
        price=price, chg_pct=0.0, open=price, prev_close=price,
        ma5="", ma10="", ma20="", rsi="", macd_dif="", macd_dea="", macd_hist="",
        bull=0, bear=0, net=net, signal=signal, reasons="r",
    ))
    rec.update(kw)
    return rec


# ----------------------------------------------------------------------------
# 1. 语法 / 导入 (无 GUI 副作用)
# ----------------------------------------------------------------------------
class TestImportAndCompile(unittest.TestCase):
    def test_syntax_compiles(self):
        import py_compile
        # doraise=True: 编译失败抛 PyCompileError
        py_compile.compile(sf.__file__, doraise=True)

    def test_import_no_gui_side_effects(self):
        # 导入必须成功且不启动 GUI / mainloop
        self.assertTrue(hasattr(sf, "run_hud"))
        self.assertTrue(hasattr(sf, "monitor"))
        # 模块级只构造 DataSource, 不应创建 Tk 根
        self.assertIsInstance(sf.DATA_SOURCE, sf.DataSource)
        # 若 tk 可用, 确认没有已实例化的默认根 (即 import 未调用 Tk())
        if sf.tk is not None:
            root = getattr(sf.tk, "_default_root", None)
            self.assertIsNone(root, "import 不应创建 Tk 根")


# ----------------------------------------------------------------------------
# 2. 指标纯函数一致性 (手算已知输入输出)
# ----------------------------------------------------------------------------
class TestIndicators(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(sf.sma([1.0, 2.0, 3.0, 4.0], 4), 2.5)
        self.assertEqual(sf.sma([10.0, 20.0, 30.0], 2), 25.0)
        self.assertIsNone(sf.sma([1.0, 2.0], 5))          # 数据不足 -> None
        self.assertEqual(sf.sma([5.0] * 10, 1), 5.0)     # n=1

    def test_ema_series(self):
        # n=3: 前2个 None, 第3个=前3均值, 之后递推 k=2/(n+1)=2/3
        out = sf.ema_series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0], 3)
        self.assertEqual(out[:3], [None, None, 2.0])
        self.assertEqual(out[3], 3.0)
        self.assertEqual(out[4], 4.0)
        self.assertEqual(out[5], 5.0)
        self.assertEqual(out[6], 6.0)
        self.assertEqual(out[7], 7.0)
        # n=2: prev=(2+4)/2=3; i=2: prev=6*(2/3)+3*(1/3)=5
        self.assertEqual(sf.ema_series([2.0, 4.0, 6.0], 2), [None, 3.0, 5.0])

    def test_rsi_flat_returns_100(self):
        # 全相等 -> 无损失 -> al==0 -> 返回 100.0 (与原公式一致)
        closes = [10.0] * 20
        self.assertAlmostEqual(sf.rsi(closes, 14), 100.0, places=6)

    def test_rsi_insufficient(self):
        self.assertIsNone(sf.rsi([1.0, 2.0, 3.0], 14))   # 长度需 >= n+1

    def test_rsi_mixed_handcalc(self):
        # 构造已知序列, 手算 RSI(14) (简单的平均口径, 非 Wilder 平滑)
        closes = [10, 10, 12, 12, 10, 10, 12, 12, 10, 10, 12, 12, 10, 10, 12]
        # 末14个差分: 增益 [0,2,0,0,0,2,0,0,0,2,0,0,0,2] -> 和=8 -> ag=8/14
        # 损失 [0,0,0,2,0,0,0,2,0,0,0,2,0,0] -> 和=6 -> al=6/14
        # ag/al = 8/6 = 1.3333; RSI = 100 - 100/(1+1.3333) = 57.1428...
        expected = 100 - 100 / (1 + 8.0 / 6.0)
        self.assertAlmostEqual(sf.rsi(closes, 14), expected, places=4)

    def test_macd_insufficient(self):
        closes = [float(i) for i in range(1, 35)]  # 长度34 < 35
        self.assertEqual(sf.macd(closes), (None, None, None, None))

    def test_macd_internal_consistency(self):
        # 用已单测的 ema_series 独立重建, 校验 macd 数学关系 (不依赖被测算子)
        closes = [float(i) for i in range(1, 45)]
        dif, dea, hist, prev_hist = sf.macd(closes)
        self.assertIsNotNone(dif)
        es_fast = sf.ema_series(closes, 12)
        es_slow = sf.ema_series(closes, 26)
        dif_series = [a - b if a is not None and b is not None else None
                     for a, b in zip(es_fast, es_slow)]
        dea_series = sf.ema_series(dif_series, 9)
        hist_series = [(d - e) * 2 if d is not None and e is not None else None
                       for d, e in zip(dif_series, dea_series)]
        self.assertAlmostEqual(dif, dif_series[-1], places=6)
        self.assertAlmostEqual(dea, dea_series[-1], places=6)
        self.assertAlmostEqual(hist, hist_series[-1], places=6)
        self.assertAlmostEqual(prev_hist, hist_series[-2], places=6)

    def test_kdj_insufficient(self):
        self.assertEqual(sf.kdj([1.0, 2.0, 3.0]), (None, None, None))  # <9

    def test_kdj_flat_is_50(self):
        # 全相等 -> RSV=50 -> K=D=J=50
        closes = [10.0] * 12
        k, d, j = sf.kdj(closes)
        self.assertAlmostEqual(k, 50.0, places=6)
        self.assertAlmostEqual(d, 50.0, places=6)
        self.assertAlmostEqual(j, 50.0, places=6)

    def test_kdj_rising_returns_floats(self):
        closes = [float(i) for i in range(1, 30)]
        k, d, j = sf.kdj(closes)
        self.assertIsInstance(k, float)
        self.assertIsInstance(d, float)
        self.assertIsInstance(j, float)
        self.assertTrue(all(isinstance(x, float) for x in (k, d, j)))

    def test_bollinger_insufficient(self):
        self.assertEqual(sf.bollinger([1.0] * 10), (None, None, None))  # <20

    def test_bollinger_handcalc(self):
        # 1..20: mean=10.5; 方差 = sum((x-10.5)^2)/20
        # sum(x^2)=2870; n*mean^2=20*110.25=2205; 差=665; var=33.25; sd=5.7663
        closes = [float(i) for i in range(1, 21)]
        mid, up, low = sf.bollinger(closes, 20, 2)
        self.assertAlmostEqual(mid, 10.5, places=4)
        self.assertAlmostEqual(up, 10.5 + 2 * (33.25 ** 0.5), places=3)
        self.assertAlmostEqual(low, 10.5 - 2 * (33.25 ** 0.5), places=3)


# ----------------------------------------------------------------------------
# 3. map_signal 滞回 (hysteresis)
# ----------------------------------------------------------------------------
class TestMapSignal(unittest.TestCase):
    # 用档位整数(避免 emoji 字面量跨文件编码差异): 2=买入 1=轻仓 0=持有 -1=减仓 -2=卖出
    CASES = [
        (None, 3, 2),
        (None, 1, 1),
        (None, -3, -2),
        (None, -1, -1),
        (None, 0, 0),
        # 持有档位边界抖动被吸收: net 在 0/1 间抖仍保持持有
        (0, 1, 0),
        (0, 2, 1),
        (0, -2, -1),
        # 轻仓档位: net=2 仍保持 (需>=3 才升), 吸收边界
        (1, 0, 0),
        (1, 2, 1),
        (1, 3, 2),
        # 买入档位: 降到 2 即降一级
        (2, 2, 1),
        (2, 5, 2),
        # 减仓档位
        (-1, 0, 0),
        (-1, -3, -2),
        # 卖出档位恢复: net=-2 升一级 (吸收恢复抖动)
        (-2, -2, -1),
        (-2, -1, -1),
    ]

    def test_hysteresis(self):
        LVL = sf._LEVEL_SIG  # int -> 信号字符串
        for prev_lvl, net, exp_lvl in self.CASES:
            prev_str = LVL.get(prev_lvl) if prev_lvl is not None else None
            with self.subTest(prev=prev_str, net=net):
                self.assertEqual(sf.map_signal(net, prev_str), LVL[exp_lvl])


# ----------------------------------------------------------------------------
# 4. 打分函数 (SCORERS) 单元验证
# ----------------------------------------------------------------------------
class TestScorers(unittest.TestCase):
    def test_score_ma(self):
        b, e, rs = sf._score_ma(12.0, {"ma5": 10.0, "ma10": 9.0, "ma20": 8.0})
        self.assertEqual((b, e), (3, 0))
        b, e, rs = sf._score_ma(5.0, {"ma5": 10.0, "ma10": 9.0, "ma20": 8.0})
        # 价在MA5下(-1) + MA5>MA10(+1) + MA10>MA20(+1) = (2,1)
        self.assertEqual((b, e), (2, 1))
        b, e, rs = sf._score_ma(10.0, {"ma5": 10.0, "ma10": 10.0, "ma20": 10.0})
        self.assertEqual((b, e), (0, 3))  # 相等走 else

    def test_score_rsi(self):
        self.assertEqual(sf._score_rsi(0, {"rsi": None}), (0, 0, []))
        b, e, rs = sf._score_rsi(0, {"rsi": 30.0})
        self.assertEqual((b, e), (1, 0))
        self.assertIn("RSI30超卖", rs)
        b, e, rs = sf._score_rsi(0, {"rsi": 70.0})
        self.assertEqual((b, e), (0, 1))
        self.assertIn("RSI70超买", rs)
        self.assertEqual(sf._score_rsi(0, {"rsi": 50.0}), (0, 0, []))

    def test_score_macd(self):
        # 红柱
        b, e, rs = sf._score_macd(0, {"hist": 1.0, "prev_hist": -0.5})
        self.assertEqual((b, e), (3, 0))  # 红柱+1 + 金叉+2
        self.assertIn("MACD金叉", rs)
        # 绿柱 + 死叉
        b, e, rs = sf._score_macd(0, {"hist": -1.0, "prev_hist": 0.5})
        self.assertEqual((b, e), (0, 3))  # 绿柱+1 + 死叉+2
        self.assertIn("MACD死叉", rs)
        # 红柱无交叉
        b, e, rs = sf._score_macd(0, {"hist": 1.0, "prev_hist": 2.0})
        self.assertEqual((b, e), (1, 0))
        # hist None
        self.assertEqual(sf._score_macd(0, {"hist": None}), (0, 0, []))

    def test_score_kdj(self):
        self.assertEqual(sf._score_kdj(0, {"k": None, "d": None}), (0, 0, []))
        b, e, rs = sf._score_kdj(0, {"k": 60.0, "d": 40.0, "j": 50.0})
        self.assertEqual((b, e), (1, 0))
        self.assertIn("KDJ金叉区(K>D)", rs)
        b, e, rs = sf._score_kdj(0, {"k": 40.0, "d": 60.0, "j": 50.0})
        self.assertEqual((b, e), (0, 1))
        self.assertIn("KDJ死叉区(K<D)", rs)
        b, e, rs = sf._score_kdj(0, {"k": 60.0, "d": 40.0, "j": 110.0})
        # 金叉区(K>D)+1, J>100超买 -> bear+1 => (1,1)
        self.assertEqual((b, e), (1, 1))
        self.assertIn("J>100超买", rs)
        b, e, rs = sf._score_kdj(0, {"k": 40.0, "d": 60.0, "j": -5.0})
        # K<D 死叉区(-1) + J<0超卖(+1) => (1,1)
        self.assertEqual((b, e), (1, 1))
        self.assertIn("J<0超卖", rs)

    def test_score_boll(self):
        self.assertEqual(sf._score_boll(10.0, {"boll_up": None}), (0, 0, []))
        b, e, rs = sf._score_boll(3.0, {"boll_up": 10.0, "boll_low": 4.0})
        self.assertEqual((b, e), (1, 0))  # 价破下轨(3<4)超卖
        b, e, rs = sf._score_boll(15.0, {"boll_up": 10.0, "boll_low": 4.0})
        self.assertEqual((b, e), (0, 1))  # 破上轨超买
        self.assertEqual(sf._score_boll(7.0, {"boll_up": 10.0, "boll_low": 4.0}), (0, 0, []))

    def test_score_volume(self):
        self.assertEqual(sf._score_volume(0, {"vol": None}), (0, 0, []))
        b, e, rs = sf._score_volume(0, {"vol": 200.0, "vol_ma5": 100.0})
        self.assertEqual((b, e), (1, 0))  # 放量 2.0x
        self.assertIn("放量", rs[0])
        b, e, rs = sf._score_volume(0, {"vol": 40.0, "vol_ma5": 100.0})
        self.assertEqual((b, e), (0, 1))  # 缩量 0.4x
        self.assertEqual(sf._score_volume(0, {"vol": 100.0, "vol_ma5": 100.0}), (0, 0, []))


# ----------------------------------------------------------------------------
# 5. monitor 默认行为回归 (默认 MA/RSI/MACD 与改造前逐字一致)
#   预期分数用「独立重写的验收规则 + 已单测的指标函数」计算, 与 monitor 输出逐字段比对。
# ----------------------------------------------------------------------------
def expected_default_score(closes, price, prev_sig=None):
    """按验收基准独立重写 MA/RSI/MACD 打分规则 (不调用 SCORERS), 用于交叉验证 monitor 接线。"""
    ind = list(closes[:-1]) + [price]
    ma5, ma10, ma20 = sf.sma(ind, 5), sf.sma(ind, 10), sf.sma(ind, 20)
    bull = bear = 0
    reasons = []
    # MA
    if ma5 and price > ma5:
        bull += 1; reasons.append("价在MA5上")
    else:
        bear += 1; reasons.append("价在MA5下")
    if ma5 and ma10 and ma5 > ma10:
        bull += 1; reasons.append("MA5>MA10(短多)")
    else:
        bear += 1; reasons.append("MA5<MA10(短空)")
    if ma10 and ma20 and ma10 > ma20:
        bull += 1; reasons.append("MA10>MA20(中多)")
    else:
        bear += 1; reasons.append("MA10<MA20(中空)")
    # RSI
    r = sf.rsi(ind)
    if r is not None:
        if r < 35:
            bull += 1; reasons.append(f"RSI{r:.0f}超卖")
        elif r > 65:
            bear += 1; reasons.append(f"RSI{r:.0f}超买")
    # MACD
    _dif, _dea, hist, prev_hist = sf.macd(ind)
    if hist is not None:
        if hist > 0:
            bull += 1; reasons.append("MACD红柱")
        else:
            bear += 1; reasons.append("MACD绿柱")
        if prev_hist is not None:
            if prev_hist <= 0 < hist:
                bull += 2; reasons.append("MACD金叉")
            elif prev_hist >= 0 > hist:
                bear += 2; reasons.append("MACD死叉")
    net = bull - bear
    sig = sf.map_signal(net, prev_sig)
    return bull, bear, net, sig, reasons


class TestMonitorDefault(unittest.TestCase):
    def _run(self, closes, price, settings=None, prev_sig=None, volume=None):
        r = rt(price, prev_close=price, volume=volume)
        with mock.patch.object(sf, "get_kline", return_value=list(closes)):
            _t, _s, rec = sf.monitor(
                {"code": "hk01810", "name": "X"}, rt=r, settings=settings, prev_sig=prev_sig)
        return rec

    def _check(self, closes, price, prev_sig=None):
        rec = self._run(closes, price, settings=None, prev_sig=prev_sig)
        eb, ee, en, esig, ereasons = expected_default_score(closes, price, prev_sig)
        self.assertEqual(rec["bull"], eb, "bull 不符")
        self.assertEqual(rec["bear"], ee, "bear 不符")
        self.assertEqual(rec["net"], en, "net 不符")
        self.assertEqual(rec["signal"], esig, "signal 不符")
        self.assertEqual(rec["reasons"], ereasons, "reasons 不符")

    def test_rising_series(self):
        closes = [float(i) for i in range(1, 41)]
        self._check(closes, 40.0)

    def test_falling_series(self):
        closes = [float(41 - i) for i in range(1, 41)]
        self._check(closes, 1.0)

    def test_flat_series(self):
        closes = [10.0] * 40
        self._check(closes, 10.0)

    def test_oscillating_series(self):
        closes = [10.0 + (i % 2) * 2 for i in range(40)]
        self._check(closes, 11.0)

    def test_hysteresis_prev_signal(self):
        # 验证 monitor 把 prev_sig 透传给 map_signal (滞回生效)
        closes = [float(i) for i in range(1, 41)]
        # 默认无 prev -> 强多; 带 prev 持有 且 net 边界抖动 -> 仍持有
        rec = self._run(closes, 40.0, prev_sig="⚪ 持有/观望")
        eb, ee, en, esig, ereasons = expected_default_score(closes, 40.0, "⚪ 持有/观望")
        self.assertEqual(rec["signal"], esig)
        self.assertEqual(rec["net"], en)

    def test_default_equals_explicit(self):
        # settings={} 必须与显式 indicators=["MA","RSI","MACD"] 完全一致
        closes = [float(i) for i in range(1, 41)]
        r = rt(40.0, prev_close=40.0)
        with mock.patch.object(sf, "get_kline", return_value=list(closes)):
            _t1, _s1, rec1 = sf.monitor({"code": "hk01810", "name": "X"}, rt=r, settings={})
            _t2, _s2, rec2 = sf.monitor(
                {"code": "hk01810", "name": "X"}, rt=r,
                settings={"indicators": ["MA", "RSI", "MACD"]})
        self.assertEqual(rec1["bull"], rec2["bull"])
        self.assertEqual(rec1["net"], rec2["net"])
        self.assertEqual(rec1["signal"], rec2["signal"])
        self.assertEqual(rec1["reasons"], rec2["reasons"])

    def test_unknown_indicator_ignored(self):
        # 未知指标被过滤, 不影响默认打分
        closes = [float(i) for i in range(1, 41)]
        r = rt(40.0, prev_close=40.0)
        with mock.patch.object(sf, "get_kline", return_value=list(closes)):
            _t1, _s1, rec1 = sf.monitor(
                {"code": "hk01810", "name": "X"}, rt=r, settings={})
            _t2, _s2, rec2 = sf.monitor(
                {"code": "hk01810", "name": "X"}, rt=r,
                settings={"indicators": ["MA", "RSI", "MACD", "BOGUS"]})
        self.assertEqual(rec1["bull"], rec2["bull"])
        self.assertEqual(rec1["net"], rec2["net"])


# ----------------------------------------------------------------------------
# 6. monitor 动态打分 (开启 KDJ / BOLL / VOLUME 改变 bull/bear 与 reasons)
# ----------------------------------------------------------------------------
class TestMonitorDynamic(unittest.TestCase):
    def test_kdj_boll_volume_contribute(self):
        # 价格尖刺场景: 全 10, 现价 100 -> 触发 BOLL 破上轨 + KDJ 死叉区 + VOLUME 放量
        closes = [10.0] * 40
        r = rt(100.0, prev_close=10.0, volume=200.0)
        with mock.patch.object(sf, "get_kline", return_value=list(closes)):
            with mock.patch.object(sf, "get_volume_hist", return_value=[100.0] * 5):
                _t, _s, rec = sf.monitor(
                    {"code": "hk01810", "name": "X"}, rt=r,
                    settings={"indicators": ["MA", "RSI", "MACD", "KDJ", "BOLL", "VOLUME"]})
        reasons = rec["reasons"]
        self.assertTrue(any("KDJ" in x for x in reasons), "KDJ 未参与")
        self.assertTrue(any("价破布林上轨" in x for x in reasons), "BOLL 未参与")
        self.assertTrue(any("放量" in x for x in reasons), "VOLUME 未参与")

    def test_kdj_rising_golden(self):
        closes = [float(i) for i in range(1, 41)]
        r = rt(40.0, prev_close=40.0)
        with mock.patch.object(sf, "get_kline", return_value=list(closes)):
            _t, _s, rec = sf.monitor(
                {"code": "hk01810", "name": "X"}, rt=r,
                settings={"indicators": ["KDJ"]})
        self.assertTrue(any("KDJ金叉区" in x for x in rec["reasons"]))

    def test_volume_only(self):
        closes = [10.0] * 40
        r = rt(10.0, prev_close=10.0, volume=300.0)
        with mock.patch.object(sf, "get_kline", return_value=list(closes)):
            with mock.patch.object(sf, "get_volume_hist", return_value=[100.0] * 5):
                _t, _s, rec = sf.monitor(
                    {"code": "hk01810", "name": "X"}, rt=r,
                    settings={"indicators": ["VOLUME"]})
        self.assertTrue(any("放量" in x for x in rec["reasons"]))
        self.assertEqual(rec["bull"], 1)


# ----------------------------------------------------------------------------
# 7. DataSource 多源兜底 + 主源告警仅一次
# ----------------------------------------------------------------------------
class TestDataSource(unittest.TestCase):
    RT_VAL = rt(12.34, prev_close=12.0, volume=100.0)

    def _make(self):
        return [sf.TencentSource(), sf.SinaSource(), sf.EastmoneySource()]

    def test_fallback_to_sina(self):
        alerts = []
        srcs = self._make()
        with mock.patch.object(sf.TencentSource, "fetch", side_effect=RuntimeError("tencent down")):
            with mock.patch.object(sf.SinaSource, "fetch", return_value=self.RT_VAL):
                ds = sf.DataSource(sources=srcs, alert_fn=alerts.append)
                res = ds.fetch("hk01810")
        self.assertEqual(res, self.RT_VAL)
        self.assertEqual(len(alerts), 1, "主源告警应仅一次")

    def test_primary_ok_no_alert(self):
        alerts = []
        srcs = self._make()
        with mock.patch.object(sf.TencentSource, "fetch", return_value=self.RT_VAL):
            ds = sf.DataSource(sources=srcs, alert_fn=alerts.append)
            res = ds.fetch("hk01810")
        self.assertEqual(res, self.RT_VAL)
        self.assertEqual(len(alerts), 0)

    def test_all_fail_raises_and_alert_once(self):
        alerts = []
        srcs = self._make()
        with mock.patch.object(sf.TencentSource, "fetch", side_effect=ValueError("a")):
            with mock.patch.object(sf.SinaSource, "fetch", side_effect=ValueError("b")):
                with mock.patch.object(sf.EastmoneySource, "fetch", side_effect=ValueError("c")):
                    ds = sf.DataSource(sources=srcs, alert_fn=alerts.append)
                    with self.assertRaises(ValueError):
                        ds.fetch("hk01810")
        self.assertEqual(len(alerts), 1, "即使全失败主源告警也只一次")

    def test_alert_only_on_primary(self):
        # 仅主源(索引0)异常时告警; 备用源异常静默跳过不告警
        alerts = []
        srcs = self._make()
        with mock.patch.object(sf.TencentSource, "fetch", side_effect=ValueError("primary")):
            with mock.patch.object(sf.SinaSource, "fetch", side_effect=ValueError("sina also down")):
                with mock.patch.object(sf.EastmoneySource, "fetch", return_value=self.RT_VAL):
                    ds = sf.DataSource(sources=srcs, alert_fn=alerts.append)
                    res = ds.fetch("hk01810")
        self.assertEqual(res, self.RT_VAL)
        self.assertEqual(len(alerts), 1)

    def test_build_sources(self):
        self.assertEqual(len(sf.build_sources(None)), 3)
        self.assertIsInstance(sf.build_sources(None)[0], sf.TencentSource)
        # 未知名跳过
        s2 = sf.build_sources(["tencent", "bad", "eastmoney"])
        self.assertEqual(len(s2), 2)
        self.assertIsInstance(s2[0], sf.TencentSource)
        self.assertIsInstance(s2[1], sf.EastmoneySource)
        # 全未知 -> 回退仅腾讯
        s3 = sf.build_sources(["nope"])
        self.assertEqual(len(s3), 1)
        self.assertIsInstance(s3[0], sf.TencentSource)


# ----------------------------------------------------------------------------
# 8. Notifier 跨平台分支 (mac / linux / win / 禁用 / 闪烁)
#   不实例化 Tk; Windows 闪烁经 flash_fn 注入断言被调度。
# ----------------------------------------------------------------------------
class TestNotifier(unittest.TestCase):
    def test_mac(self):
        with mock.patch.object(sf.sys, "platform", "darwin"):
            with mock.patch("stock_float.subprocess.run") as mrun:
                n = sf.Notifier(enabled=True)
                n.notify("hello")
                mrun.assert_called_once()
                self.assertEqual(mrun.call_args[0][0][0], "osascript")

    def test_linux(self):
        with mock.patch.object(sf.sys, "platform", "linux"):
            with mock.patch("stock_float.subprocess.run") as mrun:
                n = sf.Notifier(enabled=True)
                n.notify("hi")
                mrun.assert_called_once()
                self.assertEqual(mrun.call_args[0][0][0], "notify-send")

    def test_windows_beep_and_flash(self):
        fake_ws = mock.Mock()
        with mock.patch.object(sf.sys, "platform", "win32"):
            with mock.patch.dict(sys.modules, {"winsound": fake_ws}):
                with mock.patch("stock_float.subprocess.run") as mrun:
                    flash = mock.Mock()
                    n = sf.Notifier(enabled=True, flash_fn=flash)
                    n.notify("hi")
                    fake_ws.Beep.assert_called_once_with(880, 200)
                    flash.assert_called_once()
                    mrun.assert_not_called()  # windows 不走 osascript/notify-send

    def test_disabled_no_call(self):
        with mock.patch("stock_float.subprocess.run") as mrun:
            n = sf.Notifier(enabled=False)
            n.notify("hi")
            mrun.assert_not_called()


# ----------------------------------------------------------------------------
# 8b. Notifier 开关门控 + _save_config_key 布尔回归 (本次新增, 无头)
#     平台无关: 同时 patch sf.notify_mac(darwin) 与 sf.Notifier._notify_linux(linux),
#     断言禁用时不调用、启用时按 sys.platform 命中对应分支。
#     并通过 _save_config_key 的 bool 落盘往返, 锚定「裸小写布尔」修复
#     (旧实现会写 notify = 'False' 字符串, 重启后 bool('False')==True 误判为开)。
# ----------------------------------------------------------------------------
class TestNotifierGatingAndConfig(unittest.TestCase):
    """本次 QA 新增: 覆盖 (1) Notifier 门控 (2) 通知开关纯逻辑契约
    (3) _save_config_key 对 bool 的落盘往返固化。

    不实例化 Tk, 不依赖 sys.platform —— 用 patch.object 同时拦截
    darwin/linux 两个平台分支, 按当前平台断言「唯一命中分支」。
    """

    def test_disabled_no_dispatch(self):
        """enabled=False 时 notify 必须提前 return, 任何平台分支都不应被调用。"""
        with mock.patch.object(sf, "notify_mac") as m_mac, \
             mock.patch.object(sf.Notifier, "_notify_linux") as m_linux:
            n = sf.Notifier(enabled=False, sound=True)
            n.notify("测试消息")
            m_mac.assert_not_called()
            m_linux.assert_not_called()

    def test_enabled_dispatches_platform_branch(self):
        """enabled=True 时按 sys.platform 命中唯一对应分支; darwin 须带上 sound=True。"""
        with mock.patch.object(sf, "notify_mac") as m_mac, \
             mock.patch.object(sf.Notifier, "_notify_linux") as m_linux:
            n = sf.Notifier(enabled=True, sound=True)
            n.notify("x")
            if sys.platform == "darwin":
                m_mac.assert_called_once_with("x", True)
                m_linux.assert_not_called()
            elif sys.platform.startswith("linux"):
                m_linux.assert_called_once_with("x")
                m_mac.assert_not_called()
            else:
                # windows/其它: 走 _notify_windows(winsound+flash), 不命中 mac/linux 分支
                m_mac.assert_not_called()
                m_linux.assert_not_called()

    def test_notifier_object_toggle_contract(self):
        """纯逻辑契约: 构造 disabled 对象状态正确; 仿 toggle 闭包翻转
        enabled/sound 后, 再 notify 会进入平台分发(由 test_enabled_dispatches_platform_branch 佐证)。"""
        n = sf.Notifier(enabled=False)
        self.assertIs(False, n.enabled)
        self.assertIs(False, n.sound)
        # 模拟「变动消息提示」单一主开关翻转 notifier.enabled / notifier.sound
        n.enabled = True
        n.sound = True
        self.assertIs(True, n.enabled)
        self.assertIs(True, n.sound)
        with mock.patch.object(sf, "notify_mac") as m_mac, \
             mock.patch.object(sf.Notifier, "_notify_linux") as m_linux:
            n.notify("toggle-on")
            if sys.platform == "darwin":
                m_mac.assert_called_once_with("toggle-on", True)
            elif sys.platform.startswith("linux"):
                m_linux.assert_called_once()
            # windows/其它: 经 _notify_windows 分发(本类不实例化 Tk, 此处不拦截)

    def test_save_config_key_bool_false_roundtrip(self):
        """回归锚点: bool False 必须落为裸小写布尔 'false' 并经 tomllib 回读为真实 bool。

        旧实现会把 bool 写成字符串 'False' -> 重启 bool('False')==True 误判为开。
        本测试固化修复: 落盘 notify = false, 回读 data['settings']['notify'] is False(真 bool, 非字符串)。
        """
        try:
            import tomllib
        except ImportError:
            tomllib = None
        if tomllib is None:
            self.skipTest("tomllib 不可用(需 py3.11+)")
        path = "/tmp/qa_notify_cfg.toml"
        try:
            Path(path).write_text("[settings]\nnotify = 'stale'\n", encoding="utf-8")
            sf._save_config_key("notify", False, path=path)
            sf._save_config_key("notify_sound", False, path=path)
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            self.assertIs(False, data["settings"]["notify"])
            self.assertIs(False, data["settings"]["notify_sound"])
            raw = Path(path).read_bytes()
            self.assertIn(b"notify = false", raw)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_save_config_key_bool_true_roundtrip(self):
        """对照: bool True 落为裸小写布尔 'true', 回读为真实 bool(True), 且非字符串。"""
        try:
            import tomllib
        except ImportError:
            tomllib = None
        if tomllib is None:
            self.skipTest("tomllib 不可用(需 py3.11+)")
        path = "/tmp/qa_notify_cfg.toml"
        try:
            Path(path).write_text("[settings]\nnotify = 'stale'\n", encoding="utf-8")
            sf._save_config_key("notify", True, path=path)
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            self.assertIs(True, data["settings"]["notify"])
            raw = Path(path).read_bytes()
            self.assertIn(b"notify = true", raw)
            # 反向锚定: 不得是字符串形态(旧 Bug 表征)
            self.assertNotIn(b"notify = 'false'", raw)
            self.assertNotIn(b"notify = 'False'", raw)
            self.assertNotIn(b"notify = 'True'", raw)
        finally:
            if os.path.exists(path):
                os.remove(path)


# ----------------------------------------------------------------------------
# 8c. 「隐藏排序 / 隐藏删除 / 置顶」面板开关 —— 无头回归 (本次增强)
#     (1) 配置持久化: _save_config_key 把 bool 落为裸 true/false,
#         并经 load_settings 回读为真实布尔; 缺省回读为 False。
#     (2) 显隐守卫逻辑: 用 FakeWidget 桩移植 _apply_row_tools_visibility 的判定,
#         采用「宽度折叠」(config text=""+width=0+padx=0) 而非 pack/pack_forget 显隐,
#         连击同状态/反复 toggle 稳定幂等, 全程不碰 pack 几何。不实例化 Tk(无 DISPLAY 约束)。
# ----------------------------------------------------------------------------
class TestHideSortDelToggles(unittest.TestCase):
    """覆盖本次新增的 3 个设置面板开关的纯逻辑/持久化回归。

    不实例化 Tk、不依赖 DISPLAY; GUI 组装仅标注「需真机验证」。
    """

    def _write_tmp_cfg(self, tmp_path):
        Path(tmp_path).write_text("[settings]\n", encoding="utf-8")

    def test_save_config_key_hide_sort_bool_true(self):
        """hide_sort=True 落为裸小写布尔 'true', 经 tomllib 回读为真实 bool(True)。"""
        try:
            import tomllib
        except ImportError:
            tomllib = None
        if tomllib is None:
            self.skipTest("tomllib 不可用(需 py3.11+)")
        path = "/tmp/qa_hide_cfg.toml"
        try:
            self._write_tmp_cfg(path)
            sf._save_config_key("hide_sort", True, path=path)
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            self.assertIs(True, data["settings"]["hide_sort"])
            self.assertIn(b"hide_sort = true", Path(path).read_bytes())
            # 反向锚定: 不得是字符串形态(旧 Bug 表征)
            self.assertNotIn(b"hide_sort = 'true'", Path(path).read_bytes())
            self.assertNotIn(b"hide_sort = 'True'", Path(path).read_bytes())
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_save_config_key_hide_del_bool_false(self):
        """hide_del=False 落为裸小写布尔 'false', 回读为真实 bool(False)。"""
        try:
            import tomllib
        except ImportError:
            tomllib = None
        if tomllib is None:
            self.skipTest("tomllib 不可用(需 py3.11+)")
        path = "/tmp/qa_hide_cfg.toml"
        try:
            self._write_tmp_cfg(path)
            sf._save_config_key("hide_del", False, path=path)
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            self.assertIs(False, data["settings"]["hide_del"])
            self.assertIn(b"hide_del = false", Path(path).read_bytes())
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_save_config_key_persists_both_keys(self):
        """连续写 hide_sort/hide_del, 用 re 锚定两键均为裸布尔(不依赖 tomllib)。"""
        import re
        path = "/tmp/qa_hide_cfg.toml"
        try:
            self._write_tmp_cfg(path)
            sf._save_config_key("hide_sort", True, path=path)
            sf._save_config_key("hide_del", True, path=path)
            raw = Path(path).read_text("utf-8")
            # 裸布尔(无引号)而非字符串; 用内联 (?m) 开启多行锚点
            self.assertRegex(raw, r"(?m)^hide_sort = true$")
            self.assertRegex(raw, r"(?m)^hide_del = true$")
            self.assertNotIn("'true'", raw)
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_load_settings_roundtrip_hide_keys(self):
        """经 load_settings 回读 _save_config_key 写入的 hide_sort/hide_del 为真实布尔。"""
        path = "/tmp/qa_hide_cfg.toml"
        try:
            self._write_tmp_cfg(path)
            sf._save_config_key("hide_sort", True, path=path)
            sf._save_config_key("hide_del", False, path=path)
            with mock.patch.object(sf, "SETTINGS_CANDIDATES", [path]), \
                 mock.patch.object(sf, "STOCKS_CANDIDATES", [path]):
                s = sf.load_settings()
            self.assertIs(True, s.get("hide_sort"))
            self.assertIs(False, s.get("hide_del"))
        finally:
            if os.path.exists(path):
                os.remove(path)

    def test_load_settings_default_hide_keys_false(self):
        """缺省(无配置)时 hide_sort/hide_del 回读为缺省 False(或等价)。"""
        with mock.patch.object(sf, "_load_first", return_value=None):
            s = sf.load_settings()
        self.assertNotIn("hide_sort", s)
        self.assertNotIn("hide_del", s)
        self.assertIs(False, s.get("hide_sort", False))
        self.assertIs(False, s.get("hide_del", False))


class FakeWidget:
    """无 Tk 桩: 模拟 tk.Label 的文本/内边距显隐(宽度折叠)与 pack/pack_forget 副作用, 带调用计数。

    新方案采用「宽度折叠」(config(text="", width=0, padx=0)) 而非 pack_forget/pack 来显隐;
    桩同时记录 pack/pack_forget 调用, 供回归锁断言「全程不走 pack 几何」。

    force_unmapped=True 时 winfo_ismapped() 始终返回 False(模拟 macOS Tk 不可靠行为),
    用于回归测试「显隐逻辑不再依赖 winfo_ismapped」。
    """

    def __init__(self, mapped=True, force_unmapped=False, text="▲", padx=(0, 0)):
        self._mapped = mapped
        self._force_unmapped = force_unmapped
        self._text = text
        self._width = 0
        self._padx = padx
        # 原始文本/内边距(由 _make_rows 按控件语义注入, 对应源码 build 处的 _orig_* 记录)
        self._orig_text = text
        self._orig_padx = padx
        self.pack_calls = 0
        self.forget_calls = 0
        self.config_calls = 0
        self._destroyed = False

    def winfo_ismapped(self):
        return False if self._force_unmapped else self._mapped

    def winfo_exists(self):
        """模拟 Tk 的 winfo exists: 对已 destroy 的 widget 安全返回 0(不抛错)。"""
        return 0 if self._destroyed else 1

    def pack(self, *args, **kwargs):
        self._mapped = True
        self.pack_calls += 1

    def pack_forget(self):
        self._mapped = False
        self.forget_calls += 1

    def cget(self, key):
        return {"text": self._text, "width": self._width, "padx": self._padx}.get(key)

    def config(self, **kwargs):
        self.config_calls += 1
        if "text" in kwargs:
            self._text = kwargs["text"]
        if "width" in kwargs:
            self._width = kwargs["width"]
        if "padx" in kwargs:
            self._padx = kwargs["padx"]


def _apply_row_tools_visibility_logic(move_btns, del_btns, hide_sort, hide_del,
                                      param_btns=None, hide_param=False):
    """移植自 run_hud._apply_row_tools_visibility 的纯判定逻辑(去 Tk/root)。

    采用「宽度折叠」而非 pack_forget/pack: 隐藏时清文本+宽度0+内边距0(水平空间塌缩为0),
    显示时还原原始文本/内边距。macOS Tk 上反复 pack_forget/pack 偶发「第二次隐藏失效」,
    此方案不触发几何抖动, 对任意次数 toggle 幂等稳健。

    防御: 遍历前用 winfo_exists() 跳过已 destroy 的残留引用(否则对已销毁 widget 调
    .config() 抛 TclError: invalid command name)——与真实源码行为一致。

    param_btns/hide_param: 个股参数按钮(⚙)显隐, 与 hide_sort/hide_del 同款宽度折叠;
    默认 None/False 以兼容既有调用点。
    """
    for code, (up_btn, down_btn) in list(move_btns.items()):
        if not up_btn.winfo_exists():
            move_btns.pop(code, None)
            continue
        for w in (up_btn, down_btn):
            if hide_sort:
                w.config(text="", width=0, padx=0)
            else:
                w.config(text=w._orig_text, width=0, padx=w._orig_padx)
    for code, del_btn in list(del_btns.items()):
        if not del_btn.winfo_exists():
            del_btns.pop(code, None)
            continue
        if hide_del:
            del_btn.config(text="", width=0, padx=0)
        else:
            del_btn.config(text=del_btn._orig_text, width=0, padx=del_btn._orig_padx)
    for code, param_btn in (param_btns or {}).items():
        if not param_btn.winfo_exists():
            param_btns.pop(code, None)
            continue
        if hide_param:
            param_btn.config(text="", width=0, padx=0)
        else:
            param_btn.config(text=param_btn._orig_text, width=0, padx=param_btn._orig_padx)


class TestRowToolsVisibilityGuard(unittest.TestCase):
    """显隐逻辑单测: 直接移植 _apply_row_tools_visibility 判定(无 Tk)。

    覆盖回归(宽度折叠方案):
    - hide_sort=True -> 排序箭头文本塌缩为 "" 且 padx=0(水平空间塌缩); 切回 False -> 还原原始文本/内边距。
    - hide_del=True -> 删除按钮文本塌缩; 反之还原。
    - 不依赖 winfo_ismapped: 即便桩的 winfo_ismapped 始终 False(模拟 macOS), 隐藏仍生效。
    - 全程不调用 pack/pack_forget(锁定「不再走几何显隐」)。
    """

    def _make_rows(self, force_unmapped=False):
        def mk(text, padx):
            w = FakeWidget(mapped=True, force_unmapped=force_unmapped, text=text, padx=padx)
            w._orig_text = text
            w._orig_padx = padx
            return w
        return (
            {"A": (mk("▲", (0, 0)), mk("▼", (0, 0))),
             "B": (mk("▲", (0, 0)), mk("▼", (0, 0)))},
            {"A": mk("🗑", (0, 1)), "B": mk("🗑", (0, 1))},
        )

    def test_hide_sort_toggles_arrows(self):
        move_btns, del_btns = self._make_rows()
        _apply_row_tools_visibility_logic(move_btns, del_btns, hide_sort=True, hide_del=False)
        for up, down in move_btns.values():
            self.assertEqual(up.cget("text"), "")
            self.assertEqual(up.cget("padx"), 0)
            self.assertEqual(down.cget("text"), "")
            self.assertEqual(down.cget("padx"), 0)
            # 宽度折叠方案不碰 pack 几何
            self.assertEqual(up.pack_calls + up.forget_calls, 0)
            self.assertEqual(down.pack_calls + down.forget_calls, 0)
        # 切回显示: 还原原始文本与内边距
        _apply_row_tools_visibility_logic(move_btns, del_btns, hide_sort=False, hide_del=False)
        for up, down in move_btns.values():
            self.assertEqual(up.cget("text"), up._orig_text)
            self.assertEqual(up.cget("padx"), up._orig_padx)
            self.assertEqual(down.cget("text"), down._orig_text)
            self.assertEqual(down.cget("padx"), down._orig_padx)

    def test_hide_del_toggles_delete_button(self):
        move_btns, del_btns = self._make_rows()
        _apply_row_tools_visibility_logic(move_btns, del_btns, hide_sort=False, hide_del=True)
        for del_btn in del_btns.values():
            self.assertEqual(del_btn.cget("text"), "")
            self.assertEqual(del_btn.cget("padx"), 0)
            self.assertEqual(del_btn.pack_calls + del_btn.forget_calls, 0)
        _apply_row_tools_visibility_logic(move_btns, del_btns, hide_sort=False, hide_del=False)
        for del_btn in del_btns.values():
            self.assertEqual(del_btn.cget("text"), del_btn._orig_text)
            self.assertEqual(del_btn.cget("padx"), del_btn._orig_padx)

    def test_hide_param_toggles_gear_button(self):
        """hide_param=True -> ⚙ 参数按钮文本塌缩(padx=0, 不碰 pack 几何); 切回 False -> 还原。"""
        move_btns, del_btns = self._make_rows()
        param_btns = {"A": self._make_rows()[1]["A"], "B": self._make_rows()[1]["B"]}
        for w in param_btns.values():
            w._orig_text = "⚙"
            w._orig_padx = (0, 1)
        _apply_row_tools_visibility_logic(move_btns, del_btns, hide_sort=False, hide_del=False,
                                          param_btns=param_btns, hide_param=True)
        for pb in param_btns.values():
            self.assertEqual(pb.cget("text"), "")
            self.assertEqual(pb.cget("padx"), 0)
            self.assertEqual(pb.pack_calls + pb.forget_calls, 0)
        _apply_row_tools_visibility_logic(move_btns, del_btns, hide_sort=False, hide_del=False,
                                          param_btns=param_btns, hide_param=False)
        for pb in param_btns.values():
            self.assertEqual(pb.cget("text"), pb._orig_text)
            self.assertEqual(pb.cget("padx"), pb._orig_padx)

    def test_apply_collapses_text_even_when_macos_ismapped_broken(self):
        """无论 winfo_ismapped 返回什么(模拟 macOS 永远 False), hide 仍通过宽度折叠真正生效,
        且全程不碰 pack/pack_forget。"""
        move_btns, del_btns = self._make_rows(force_unmapped=True)
        # 隐藏排序 + 删除
        _apply_row_tools_visibility_logic(move_btns, del_btns, hide_sort=True, hide_del=True)
        for up, down in move_btns.values():
            self.assertEqual(up.cget("text"), "")
            self.assertEqual(up.cget("padx"), 0)
            self.assertEqual(down.cget("text"), "")
            self.assertEqual(down.cget("padx"), 0)
        for del_btn in del_btns.values():
            self.assertEqual(del_btn.cget("text"), "")
            self.assertEqual(del_btn.cget("padx"), 0)
        # 全程零 pack/pack_forget 调用
        for up, down in move_btns.values():
            self.assertEqual(up.pack_calls + up.forget_calls, 0)
            self.assertEqual(down.pack_calls + down.forget_calls, 0)
        for del_btn in del_btns.values():
            self.assertEqual(del_btn.pack_calls + del_btn.forget_calls, 0)
        # 切回显示: 还原原始文本/内边距
        _apply_row_tools_visibility_logic(move_btns, del_btns, hide_sort=False, hide_del=False)
        for up, down in move_btns.values():
            self.assertEqual(up.cget("text"), up._orig_text)
            self.assertEqual(up.cget("padx"), up._orig_padx)
            self.assertEqual(down.cget("text"), down._orig_text)
            self.assertEqual(down.cget("padx"), down._orig_padx)
        for del_btn in del_btns.values():
            self.assertEqual(del_btn.cget("text"), del_btn._orig_text)
            self.assertEqual(del_btn.cget("padx"), del_btn._orig_padx)

    def test_macos_unreliable_ismapped_regression(self):
        """回归: macOS Tk 下 winfo_ismapped 恒 False 时, 隐藏排序/删除仍通过宽度折叠真正生效。"""
        move_btns, del_btns = self._make_rows(force_unmapped=True)
        # 隐藏排序: 断言文本塌缩为 ""
        _apply_row_tools_visibility_logic(move_btns, del_btns, hide_sort=True, hide_del=False)
        for up, down in move_btns.values():
            self.assertEqual(up.cget("text"), "")
            self.assertEqual(down.cget("text"), "")
        # 隐藏删除: 断言文本塌缩为 ""
        _apply_row_tools_visibility_logic(move_btns, del_btns, hide_sort=False, hide_del=True)
        for del_btn in del_btns.values():
            self.assertEqual(del_btn.cget("text"), "")

    def test_repeat_same_state_reapplies(self):
        """新行为: 重复相同状态会再次 apply(宽度折叠, 幂等), 不依赖 pack_forget, 且无几何抖动。"""
        move_btns, del_btns = self._make_rows()
        _apply_row_tools_visibility_logic(move_btns, del_btns, hide_sort=True, hide_del=True)
        _apply_row_tools_visibility_logic(move_btns, del_btns, hide_sort=True, hide_del=True)
        # 每次 hide 均将文本塌缩为 ""(幂等, 无 pack_forget)
        for up, down in move_btns.values():
            self.assertEqual(up.cget("text"), "")
            self.assertEqual(down.cget("text"), "")
        for del_btn in del_btns.values():
            self.assertEqual(del_btn.cget("text"), "")
        # 状态仍稳定为隐藏, 且全程零 pack 调用
        for up, down in move_btns.values():
            self.assertEqual(up.pack_calls + up.forget_calls, 0)
        for del_btn in del_btns.values():
            self.assertEqual(del_btn.pack_calls + del_btn.forget_calls, 0)

    def test_repeated_toggle_n_times_is_stable_and_never_packs(self):
        """回归锁: 连续 toggle 多次(如 8 次)后, 控件最终态正确, 且全程不调用任何 pack/pack_forget。

        直接锁定「反复开关不再失效」——macOS Tk 反复 pack_forget/pack 偶发第二次隐藏失效的老毛病。

        本测试内嵌「旧 buggy 逻辑」对照(force_unmapped 模拟 macOS winfo_ismapped 恒 False),
        证明本锁确实能区分旧 bug 与新修复, 而非恒过:
          - 旧逻辑在 N 次 toggle 后仍调用 pack 几何, 且在隐藏步不真正塌缩文本(隐藏失效);
          - 新逻辑(宽度折叠)在 N 次 toggle 后状态稳定、文本正确塌缩/还原, 且全程零 pack。
        """
        n = 8

        # ---- 新逻辑(移植自修复版源码): 宽度折叠 ----
        move_btns, del_btns = self._make_rows()
        for i in range(1, n + 1):
            hide = (i % 2 == 1)  # 奇数次 = 隐藏, 偶数次 = 显示
            _apply_row_tools_visibility_logic(
                move_btns, del_btns, hide_sort=hide, hide_del=hide)
            for up, down in move_btns.values():
                if hide:
                    self.assertEqual(up.cget("text"), "")
                    self.assertEqual(down.cget("text"), "")
                else:
                    self.assertEqual(up.cget("text"), up._orig_text)
                    self.assertEqual(down.cget("text"), down._orig_text)
            for del_btn in del_btns.values():
                if hide:
                    self.assertEqual(del_btn.cget("text"), "")
                else:
                    self.assertEqual(del_btn.cget("text"), del_btn._orig_text)
        # 第 8 次(偶数) => 显示态
        self.assertEqual(n % 2, 0)
        for up, down in move_btns.values():
            self.assertEqual(up.cget("text"), up._orig_text)
            self.assertEqual(down.cget("text"), down._orig_text)
            self.assertEqual(up.pack_calls + up.forget_calls, 0)
            self.assertEqual(down.pack_calls + down.forget_calls, 0)
        for del_btn in del_btns.values():
            self.assertEqual(del_btn.cget("text"), del_btn._orig_text)
            self.assertEqual(del_btn.pack_calls + del_btn.forget_calls, 0)

        # ---- 旧 buggy 逻辑对照(force_unmapped 模拟 macOS winfo_ismapped 恒 False) ----
        def buggy_apply(move_btns, hide_sort):
            for up_btn, down_btn in move_btns.values():
                if hide_sort:
                    if up_btn.winfo_ismapped():
                        up_btn.pack_forget()
                    if down_btn.winfo_ismapped():
                        down_btn.pack_forget()
                else:
                    if not up_btn.winfo_ismapped():
                        up_btn.pack(side="right")
                    if not down_btn.winfo_ismapped():
                        down_btn.pack(side="right")

        def make_buggy_rows():
            out = {}
            for c in ("A", "B"):
                up = FakeWidget(mapped=True, force_unmapped=True, text="▲", padx=(0, 0))
                down = FakeWidget(mapped=True, force_unmapped=True, text="▼", padx=(0, 0))
                up._orig_text, up._orig_padx = "▲", (0, 0)
                down._orig_text, down._orig_padx = "▼", (0, 0)
                out[c] = (up, down)
            return out

        buggy = make_buggy_rows()
        buggy_collapsed_on_hidden = False  # 记录旧逻辑是否在某个隐藏步真正塌缩文本
        for i in range(1, n + 1):
            hide = (i % 2 == 1)
            buggy_apply(buggy, hide_sort=hide)
            if hide:
                for up, down in buggy.values():
                    # 旧逻辑依赖 winfo_ismapped(恒 False) -> 跳过 pack_forget -> 文本从不塌缩
                    if up.cget("text") == "" or down.cget("text") == "":
                        buggy_collapsed_on_hidden = True
        # 对比断言 1: 旧 buggy 逻辑在隐藏步从不真正塌缩文本(隐藏失效) -> 新修复才有意义
        self.assertFalse(
            buggy_collapsed_on_hidden,
            "对照断言: 旧 buggy 逻辑不应在隐藏步塌缩文本(否则本回归锁无法证明新修复的必要性)",
        )
        # 对比断言 2: 旧 buggy 逻辑走 pack 几何(本次修复彻底移除) -> pack/forget 调用数 > 0
        total_pack = sum(
            up.pack_calls + up.forget_calls + down.pack_calls + down.forget_calls
            for up, down in buggy.values()
        )
        self.assertGreater(
            total_pack, 0,
            "对照断言: 旧 buggy 逻辑应调用 pack/pack_forget(否则本回归锁无法证明新修复确实移除了几何抖动)",
        )

    def test_stale_destroyed_widget_does_not_crash_and_is_popped(self):
        """回归(2026-07-30 真机崩溃): 字典里若残留已 destroy 的 widget 引用
        (如删除股票后未清理干净), 遍历时不抛 TclError, 且自动将该 code 从字典弹出,
        存活 widget 仍正常处理。等价于真实源码 _apply_row_tools_visibility 的 winfo_exists 防御。"""
        move_btns, del_btns = self._make_rows()
        # 标记 A 的控件为「已销毁」(winfo_exists() 返回 0), 模拟残留失效引用
        up_a, down_a = move_btns["A"]
        up_a._destroyed = True
        down_a._destroyed = True
        del_btns["A"]._destroyed = True
        # 调用不应抛错(旧行为在此抛 TclError: invalid command name)
        _apply_row_tools_visibility_logic(move_btns, del_btns, hide_sort=True, hide_del=True)
        # 已销毁的 A 应被弹出, 杜绝后续再次遍历崩溃
        self.assertNotIn("A", move_btns)
        self.assertNotIn("A", del_btns)
        # 存活的 B 仍被正常处理(文本塌缩)
        self.assertIn("B", move_btns)
        self.assertIn("B", del_btns)
        up_b, down_b = move_btns["B"]
        self.assertEqual(up_b.cget("text"), "")
        self.assertEqual(down_b.cget("text"), "")
        self.assertEqual(del_btns["B"].cget("text"), "")


# ----------------------------------------------------------------------------
# 8b. 源码契约回归锁: 真实 _apply_row_tools_visibility / _refresh_move_buttons 不得再调用
#     winfo_ismapped; 且全模块不得残留 top_btn。直读 stock_float.py 的 AST(无 Tk、无 DISPLAY),
#     弥补"移植副本"式测试无法捕获「源码被回退到旧 bug」的盲区。
# ----------------------------------------------------------------------------
class TestRowToolsVisibilitySourceContract(unittest.TestCase):
    """锚定真实源码: 若工程师把 fix 回退成旧逻辑(如 if not up_btn.winfo_ismapped(): continue
    或 if up_btn.winfo_ismapped(): up_btn.pack_forget(), 或重新走 pack/pack_forget 显隐),
    本测试必须失败。

    采用 AST 解析而非正则/副本: 只检查「可执行代码里是否存在 .winfo_ismapped() / .pack() / .pack_forget()
    调用, 以及是否真用了宽度折叠(_orig_text/_orig_padx + .config())」,
    完全忽略 docstring/注释, 避免误报, 也真正绑定到 stock_float.py 的真实闭包函数。
    """

    @classmethod
    def setUpClass(cls):
        cls._src = Path(sf.__file__).read_text(encoding="utf-8")
        cls._tree = ast.parse(cls._src)
        cls._hud = next(
            n for n in cls._tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "run_hud"
        )
        cls._nested = {
            f.name: f for f in cls._hud.body
            if isinstance(f, ast.FunctionDef)
            and f.name in ("_apply_row_tools_visibility", "_refresh_move_buttons")
        }

    @staticmethod
    def _calls_ismapped(node):
        return any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "winfo_ismapped"
            for n in ast.walk(node)
        )

    @staticmethod
    def _calls_attr(node, attrs):
        """node 内部是否调用了任意形如 ``.attr(...)``(attr ∈ attrs) 的方法。"""
        return any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr in attrs
            for n in ast.walk(node)
        )

    @staticmethod
    def _uses_width_fold(node):
        """node 内部是否采用了宽度折叠: 调用了 .config() 且引用了 _orig_text/_orig_padx。"""
        has_config = any(
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "config"
            for n in ast.walk(node)
        )
        has_orig = any(
            isinstance(n, ast.Attribute)
            and n.attr in ("_orig_text", "_orig_padx")
            for n in ast.walk(node)
        )
        return has_config and has_orig

    def test_apply_row_tools_visibility_no_ismapped(self):
        self.assertIn("_apply_row_tools_visibility", self._nested,
                      "源码中找不到 run_hud._apply_row_tools_visibility 闭包")
        self.assertFalse(
            self._calls_ismapped(self._nested["_apply_row_tools_visibility"]),
            "回归: _apply_row_tools_visibility 仍调用 winfo_ismapped()(旧 bug: macOS 下隐藏失效)",
        )

    def test_apply_row_tools_visibility_no_pack_geometry(self):
        """锁定「宽度折叠、不走 pack」: _apply_row_tools_visibility 不得调用 pack()/pack_forget()。"""
        self.assertFalse(
            self._calls_attr(self._nested["_apply_row_tools_visibility"], ("pack", "pack_forget")),
            "回归: _apply_row_tools_visibility 仍走 pack/pack_forget(旧 bug: macOS 反复 toggle 失效)",
        )

    def test_apply_row_tools_visibility_uses_width_fold(self):
        """正向锁: _apply_row_tools_visibility 确实采用宽度折叠(config text/width/padx + _orig_* 还原)。"""
        node = self._nested["_apply_row_tools_visibility"]
        self.assertTrue(
            self._uses_width_fold(node),
            "宽度折叠方案必须调用 .config() 并引用 _orig_text/_orig_padx 还原原始态",
        )

    def test_refresh_move_buttons_no_ismapped(self):
        self.assertIn("_refresh_move_buttons", self._nested,
                      "源码中找不到 run_hud._refresh_move_buttons 闭包")
        self.assertFalse(
            self._calls_ismapped(self._nested["_refresh_move_buttons"]),
            "回归: _refresh_move_buttons 仍用 winfo_ismapped 守卫(旧 bug: 隐藏时跳过灰显)",
        )

    def test_refresh_move_buttons_no_pack_geometry(self):
        """_refresh_move_buttons 只做 fg 灰显, 不得触碰 pack/pack_forget 几何。"""
        self.assertFalse(
            self._calls_attr(self._nested["_refresh_move_buttons"], ("pack", "pack_forget")),
            "回归: _refresh_move_buttons 不应调用 pack/pack_forget(只配置 fg)",
        )

    def test_apply_row_tools_visibility_guards_destroyed_widgets(self):
        """锁定防御②: _apply_row_tools_visibility 遍历前必须用 winfo_exists() 跳过已销毁 widget,
        否则对已销毁引用调 .config() 会抛 TclError(2026-07-30 真机崩溃)。"""
        self.assertTrue(
            self._calls_attr(self._nested["_apply_row_tools_visibility"], ("winfo_exists",)),
            "回归: _apply_row_tools_visibility 未做 winfo_exists 防御(删除股票后残留引用会崩溃)",
        )

    def test_apply_row_tools_visibility_handles_hide_param(self):
        """锁定 hide_param(⚙ 参数按钮显隐)分支: 源码必须遍历 param_btns 并读取 hide_param 配置。"""
        node = self._nested["_apply_row_tools_visibility"]
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        self.assertIn("param_btns", names, "回归: _apply_row_tools_visibility 未处理 param_btns(⚙ 按钮)")
        self.assertIn("hide_param", names, "回归: _apply_row_tools_visibility 未读取 hide_param 配置")
        self.assertIn("hide_sort", names, "回归: 原 hide_sort 分支被移除")
        self.assertIn("hide_del", names, "回归: 原 hide_del 分支被移除")

    def test_refresh_move_buttons_guards_destroyed_widgets(self):
        """锁定防御③: _refresh_move_buttons 遍历前也必须用 winfo_exists() 跳过已销毁 widget。"""
        self.assertTrue(
            self._calls_attr(self._nested["_refresh_move_buttons"], ("winfo_exists",)),
            "回归: _refresh_move_buttons 未做 winfo_exists 防御",
        )

    def test_no_top_btn_residue(self):
        """全模块(含注释/元组)不得残留 top_btn 引用 —— 锁定「删除 header 置顶按钮」改动。"""
        self.assertNotIn("top_btn", self._src,
                         "回归: stock_float.py 仍残留 top_btn 引用(创建/config/注释)")

    def test_no_winformismapped_call_anywhere(self):
        """全模块 AST 不得再有 winfo_ismapped(...) 函数调用(仅允许注释/文档字符串里的文字)。
        锁定 A 整改: macOS Tk 上 winfo_ismapped 对可见控件常返回 False, 所有显隐已改为
        按目标态无条件 apply 或显式 panel_state 布尔, 不再依赖该不可信值。"""
        bad = []
        for node in ast.walk(self._tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Attribute) and f.attr == "winfo_ismapped":
                    bad.append(ast.get_source_segment(self._src, node) or "<winfo_ismapped call>")
        self.assertEqual(bad, [], f"回归: 模块内仍残留 winfo_ismapped() 调用: {bad}")

    def test_discriminates_buggy_vs_fixed(self):
        """证明本套测试工具具备区分力: 旧 buggy 逻辑依赖 winfo_ismapped 导致隐藏失效(文本未塌缩),
        新逻辑(宽度折叠)在 winfo_ismapped 恒 False 时也真正塌缩文本/内边距, 且全程不碰 pack。"""
        def buggy_apply(move_btns, hide_sort):
            # 旧逻辑: 依赖 winfo_ismapped() 判定控件是否可见, 不可见就跳过 pack_forget
            for up_btn, down_btn in move_btns.values():
                if hide_sort:
                    if up_btn.winfo_ismapped():
                        up_btn.pack_forget()
                    if down_btn.winfo_ismapped():
                        down_btn.pack_forget()
                else:
                    if not up_btn.winfo_ismapped():
                        up_btn.pack(side="right")
                    if not down_btn.winfo_ismapped():
                        down_btn.pack(side="right")

        def make_rows(force_unmapped):
            out = {}
            for c in ("A", "B"):
                up = FakeWidget(mapped=True, force_unmapped=force_unmapped, text="▲", padx=(0, 0))
                down = FakeWidget(mapped=True, force_unmapped=force_unmapped, text="▼", padx=(0, 0))
                up._orig_text, up._orig_padx = "▲", (0, 0)
                down._orig_text, down._orig_padx = "▼", (0, 0)
                out[c] = (up, down)
            return out

        # 旧 bug: winfo_ismapped 恒 False -> 跳过 pack_forget -> 文本未塌缩(隐藏失效)
        buggy = make_rows(force_unmapped=True)
        buggy_apply(buggy, hide_sort=True)
        for up, down in buggy.values():
            self.assertEqual(up.cget("text"), "▲", "对照: 旧逻辑本应隐藏失效(文本未塌缩)")
            self.assertEqual(down.cget("text"), "▼", "对照: 旧逻辑本应隐藏失效(文本未塌缩)")

        # 新逻辑(移植自源码修复版): 宽度折叠 -> 文本真正塌缩, 不依赖 winfo_ismapped
        fixed = make_rows(force_unmapped=True)
        _apply_row_tools_visibility_logic(fixed, {}, hide_sort=True, hide_del=False)
        for up, down in fixed.values():
            self.assertEqual(up.cget("text"), "", "修复版应在 winfo_ismapped 恒 False 时也塌缩文本")
            self.assertEqual(down.cget("text"), "", "修复版应在 winfo_ismapped 恒 False 时也塌缩文本")
            self.assertEqual(up.cget("padx"), 0)
            self.assertEqual(down.cget("padx"), 0)
            # 全程不碰 pack 几何
            self.assertEqual(up.pack_calls + up.forget_calls, 0)
            self.assertEqual(down.pack_calls + down.forget_calls, 0)


# ----------------------------------------------------------------------------
# 8c. 根因①回归锁: remove_stock_from_memory 必须把 del_btns/move_btns 一并清理,
#     否则删除股票后这两个字典残留已销毁 widget 引用(真机崩溃源)。
# ----------------------------------------------------------------------------
class TestRemoveStockCleansToolButtons(unittest.TestCase):
    """根因①(2026-07-30 真机崩溃): 删除股票时, remove_stock_from_memory 必须清理 del_btns
    与 move_btns 中对应 code 的条目。否则行情行 frame 被 destroy 后, 这两个字典残留已销毁
    Label 引用, 后续 _apply_row_tools_visibility 遍历到它们调 .config() 抛 TclError。"""

    def test_remove_stock_clears_del_and_move_btns(self):
        stocks = [{"code": "A", "name": "测试A"}, {"code": "B", "name": "测试B"}]
        del_btns = {
            "A": FakeWidget(text="🗑", padx=(0, 1)),
            "B": FakeWidget(text="🗑", padx=(0, 1)),
        }
        move_btns = {
            "A": (FakeWidget(text="▲", padx=(0, 0)), FakeWidget(text="▼", padx=(0, 0))),
            "B": (FakeWidget(text="▲", padx=(0, 0)), FakeWidget(text="▼", padx=(0, 0))),
        }
        removed = sf.remove_stock_from_memory(
            stocks, "A", {"del_btns": del_btns, "move_btns": move_btns})
        self.assertEqual(removed["code"], "A")
        self.assertNotIn("A", del_btns, "根因①: 删除后 del_btns 应清除该 code")
        self.assertNotIn("A", move_btns, "根因①: 删除后 move_btns 应清除该 code")
        self.assertIn("B", del_btns)
        self.assertIn("B", move_btns)
        self.assertEqual([s["code"] for s in stocks], ["B"])


# ----------------------------------------------------------------------------
# 9. CSV 去重 + 新增字段 (向后兼容)
# ----------------------------------------------------------------------------
class TestCsv(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        # 重置模块级去重缓存, 避免跨测试串扰
        sf._LAST_CSV.clear()

    def tearDown(self):
        sf._LAST_CSV.clear()

    def test_dedup_within_window(self):
        path = self.tmp / "signals_test.csv"
        rec1 = full_rec("hk01810", "⚪ 持有/观望", 0, price=10.0)
        sf.append_signal(rec1, str(path), dedup_sec=60)
        # 同 code+signal+price 且窗口内 -> 跳过
        rec2 = full_rec("hk01810", "⚪ 持有/观望", 0, price=10.0)
        sf.append_signal(rec2, str(path), dedup_sec=60)
        # 变动 price -> 写入
        rec3 = full_rec("hk01810", "⚪ 持有/观望", 0, price=11.0)
        sf.append_signal(rec3, str(path), dedup_sec=60)
        lines = path.read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 3, "去重失败: 应为 表头+2 行")

    def test_header_fields_and_backward_compat(self):
        # 验收: CSV_FIELDS 顺序 = 原18字段 + 新8字段(k/d/j/boll_*/volume/vol_ma5)
        expected = ["datetime", "code", "name", "price", "chg_pct", "open", "prev_close",
                    "ma5", "ma10", "ma20", "rsi", "macd_dif", "macd_dea", "macd_hist",
                    "bull", "bear", "net", "signal", "reasons",
                    "k", "d", "j", "boll_mid", "boll_up", "boll_low", "volume", "vol_ma5"]
        self.assertEqual(sf.CSV_FIELDS, expected)
        # 旧 rec 缺少新字段也能写 (get 默认 "")
        path = self.tmp / "old.csv"
        old = {k: "" for k in
               ["datetime", "code", "name", "price", "chg_pct", "open", "prev_close",
                "ma5", "ma10", "ma20", "rsi", "macd_dif", "macd_dea", "macd_hist",
                "bull", "bear", "net", "signal", "reasons"]}
        old.update(datetime="2026-07-15 09:00:00", code="A", name="A",
                   price=1.0, chg_pct=0.0, open=1.0, prev_close=1.0,
                   ma5=1.0, ma10=1.0, ma20=1.0, rsi=50.0,
                   macd_dif=0.0, macd_dea=0.0, macd_hist=0.0,
                   bull=0, bear=0, net=0, signal="⚪ 持有/观望", reasons=["r"])
        sf.append_signal(old, str(path), dedup_sec=0)
        with open(path, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["k"], "")  # 旧字段缺失 -> 空串

    def test_reasons_joined_by_semicolon(self):
        path = self.tmp / "reasons.csv"
        rec = full_rec("A", "🟠 轻仓/偏多", 1, reasons=["r1", "r2"])
        sf.append_signal(rec, str(path), dedup_sec=0)
        with open(path, "r", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(rows[0]["reasons"], "r1;r2")


# ----------------------------------------------------------------------------
# 10. --stats / --review 过滤 + signals_path
# ----------------------------------------------------------------------------
class TestStatsReview(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.path = self.tmp / "signals_2026-07-15.csv"
        rows = [
            full_rec("A", "🔴 买入(偏强)", 5, price=20.0),
            full_rec("A", "🔴 买入(偏强)", 4, price=20.0),
            full_rec("A", "⚪ 持有/观望", 0, price=20.0),
            full_rec("A", "🟢 卖出(偏弱)", -4, price=20.0),
            full_rec("B", "⚪ 持有/观望", 0, price=20.0),
            full_rec("B", "⚪ 持有/观望", 0, price=20.0),
        ]
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=sf.CSV_FIELDS)
            w.writeheader()
            for r in rows:
                w.writerow(r)

    def _capture(self, fn):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            fn()
        return buf.getvalue()

    def test_signals_path(self):
        self.assertTrue(sf.signals_path("single").endswith("signals.csv"))
        self.assertTrue(
            sf.signals_path("daily", "2026-07-15").endswith("signals_2026-07-15.csv"))

    def test_stats_filter_code(self):
        out = self._capture(lambda: sf.stats_csv(str(self.path), code="A"))
        self.assertIn("总记录数: 4", out)
        self.assertIn("信号切换次数: 2", out)        # A: 买入->买入(no), 买入->持有, 持有->卖出
        self.assertIn("最高 net: 5", out)
        self.assertIn("最低 net: -4", out)
        # 买入档位的出现次数: 用模块常量避免 emoji 跨文件编码差
        buy_lvl = [k for k in sf._SIG_LEVELS if "买入" in k][0]
        self.assertIn(f"{buy_lvl}: 2", out)

    def test_stats_filter_date_none(self):
        out = self._capture(lambda: sf.stats_csv(str(self.path), date="2099-01-01"))
        self.assertIn("CSV 为空", out)

    def test_stats_all(self):
        out = self._capture(lambda: sf.stats_csv(str(self.path)))
        self.assertIn("总记录数: 6", out)

    def test_review_filter_code(self):
        out = self._capture(lambda: sf.review_csv(30, str(self.path), code="B"))
        self.assertIn("B", out)
        self.assertNotIn("A(", out)

    def test_review_filter_date_none(self):
        out = self._capture(lambda: sf.review_csv(30, str(self.path), date="2099-01-01"))
        self.assertIn("CSV 为空", out)


# ----------------------------------------------------------------------------
# 11. 配置加载 (load_settings / load_stocks / _normalize)
# ----------------------------------------------------------------------------
class TestConfig(unittest.TestCase):
    def test_load_settings_merges(self):
        fixture = {"settings": {"refresh_sec": 3, "notify": True},
                   "stocks": [{"code": "hk01810"}]}
        with mock.patch.object(sf, "_load_first", return_value=("fake", fixture)):
            s = sf.load_settings()
        self.assertEqual(s.get("refresh_sec"), 3)
        self.assertTrue(s.get("notify"))

    def test_load_settings_empty(self):
        with mock.patch.object(sf, "_load_first", return_value=None):
            self.assertEqual(sf.load_settings(), {})

    def test_load_stocks_support_resistance(self):
        fixture = {"settings": {},
                   "stocks": [{"code": "hk01810", "name": "X",
                                "support": [18.0, "bad", 17.5],
                                "resistance": [20.5]}]}
        with mock.patch.object(sf, "_load_first", return_value=("fake", fixture)):
            stocks = sf.load_stocks()
        st = stocks[0]
        self.assertEqual(st["support"], [18.0, 17.5])     # 非法项过滤
        self.assertEqual(st["resistance"], [20.5])

    def test_load_stocks_default(self):
        with mock.patch.object(sf, "_load_first", return_value=None):
            self.assertEqual(sf.load_stocks(), sf.DEFAULT_STOCKS)

    def test_effective_defaults_at_callsites(self):
        # 验收基准: 即使 load_settings 不注入默认值, 各调用点均回退默认
        # monitor 默认 indicators
        closes = [float(i) for i in range(1, 41)]
        r = rt(40.0, prev_close=40.0)
        with mock.patch.object(sf, "get_kline", return_value=list(closes)):
            _t, _s, rec = sf.monitor({"code": "hk01810", "name": "X"}, rt=r, settings={})
        # expected_default_score 返回 (bull,bear,net,sig,reasons), [3]=sig
        self.assertEqual(rec["signal"], expected_default_score(closes, 40.0)[3])
        # build_style 默认 light / Menlo / 7
        st = sf.build_style({})
        self.assertEqual(st["bg"], sf.LIGHT_PALETTE["bg"])
        self.assertEqual(st["FONT"], ("Menlo", 7))
        # build_sources 默认三源
        self.assertEqual(len(sf.build_sources(None)), 3)


# ----------------------------------------------------------------------------
# 12. 样式 / 主题逻辑 (不含 GUI 实例化)
# ----------------------------------------------------------------------------
class TestStyle(unittest.TestCase):
    def test_build_style_light_default(self):
        st = sf.build_style({})
        self.assertEqual(st["bg"], sf.LIGHT_PALETTE["bg"])
        self.assertEqual(st["FONT"], ("Menlo", 7))

    def test_build_style_dark(self):
        st = sf.build_style({"float_theme": "dark"})
        self.assertEqual(st["bg"], sf.DARK_PALETTE["bg"])

    def test_build_style_custom_font_size_clamp(self):
        st = sf.build_style({"float_font": "Courier", "float_font_size": 20})
        self.assertEqual(st["FONT"], ("Courier", 20))
        st2 = sf.build_style({"float_font_size": 1})
        self.assertEqual(st2["FONT"][1], 5)  # 最小 5

    def test_build_style_invalid_color_fallback(self):
        st = sf.build_style({"float_up_color": "notacolor"})
        self.assertEqual(st["up"], sf.LIGHT_PALETTE["up"])

    def test_build_style_custom_up_down_color(self):
        """设置面板选色后: build_style 应接受 float_up_color / float_down_color 并经灰度处理。"""
        # 自定义涨红/跌绿; grayness=0 → 与原色一致
        st = sf.build_style({"float_up_color": "#ff0000", "float_down_color": "#00ff00",
                             "grayness": 0.0})
        self.assertEqual(st["up"], "#ff0000")
        self.assertEqual(st["down"], "#00ff00")
        # grayness>0 时应 desaturate(原色, amount) → 颜色改变但仍是合法 hex
        st2 = sf.build_style({"float_up_color": "#ff0000", "grayness": 0.5})
        self.assertNotEqual(st2["up"], "#ff0000")
        self.assertRegex(st2["up"], r"^#[0-9a-f]{6}$")
        # 缺省(未配)回退到 pal.up / pal.down
        st3 = sf.build_style({})
        self.assertEqual(st3["up"], sf.LIGHT_PALETTE["up"])
        self.assertEqual(st3["down"], sf.LIGHT_PALETTE["down"])

    def test_save_config_key_string_persists_hex(self):
        """设置面板选色写 config.toml: hex 字符串应作为带引号 TOML 字符串落盘, 重启可回读。"""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("[settings]\nfloat_alpha = 0.94\n")
            cfg = f.name
        try:
            sf._save_config_key("float_up_color", "#e5c9c7", path=cfg)
            with open(cfg, "rb") as f:
                raw = sf.tomllib.load(f)
            self.assertEqual(raw["settings"]["float_up_color"], "#e5c9c7")
        finally:
            os.unlink(cfg)

    def test_detect_system_theme(self):
        # darwin + "Dark" -> dark
        with mock.patch.object(sf.sys, "platform", "darwin"):
            with mock.patch("stock_float.subprocess.run",
                            return_value=mock.Mock(returncode=0, stdout="Dark\n")):
                self.assertEqual(sf.detect_system_theme(), "dark")
            with mock.patch("stock_float.subprocess.run",
                            return_value=mock.Mock(returncode=0, stdout="")):
                self.assertEqual(sf.detect_system_theme(), "light")
        # 非 mac/win -> light 回退
        with mock.patch.object(sf.sys, "platform", "linux"):
            self.assertEqual(sf.detect_system_theme(), "light")

    def test_cross_helper(self):
        # 支撑/压力穿越判定 (被 worker 复用)
        self.assertEqual(sf._cross(10.0, 9.0, 9.5), -1)   # 下穿
        self.assertEqual(sf._cross(9.0, 10.0, 9.5), 1)     # 上穿
        self.assertEqual(sf._cross(9.0, 10.0, 5.0), 0)    # 未穿越
        self.assertEqual(sf._cross(None, 10.0, 5.0), 0)    # 缺值


# ----------------------------------------------------------------------------
# 13. GUI 项 (代码审查 + 逻辑正确性, 明确标注「需真机验证」, 不实例化 Tk)
# ----------------------------------------------------------------------------
class TestGuiReviewOnly(unittest.TestCase):
    """以下项因无 DISPLAY 无法无头实例化 Tk, 仅做代码层存在性与接口契约校验。

    需真机验证项:
      - 频率控件 (_cycle_freq 循环 1->3->5->10->1)
      - Windows 闪烁 (_flash_window 经 root.after 回主线程)
      - 暗色渲染 (build_style 已逻辑验证, 像素渲染需真机)
    """

    def test_gui_symbols_exist(self):
        # 模块级符号存在且可调用 (具体行为需真机)
        # 注: _cycle_freq / _flash_window 是 run_hud 内部嵌套函数,
        #     仅能在本机建 Tk 后提取, 此处不实例化 Tk, 故只校验模块级导出符号。
        for name in ("run_hud",):
            self.assertTrue(hasattr(sf, name), f"{name} 缺失")
            self.assertTrue(callable(getattr(sf, name)), f"{name} 不可调用")


# ----------------------------------------------------------------------------
# 14. 监控引擎配置归属: 监控键应从 stocks.toml 的 [settings] 读取 (官方家)
#     (配置归属理顺: 运行逻辑零改动, load_settings 双文件合并 + stocks.toml 优先)
# ----------------------------------------------------------------------------
class TestMonitoringConfigFromStocksToml(unittest.TestCase):
    """锁定「监控策略放 stocks.toml、运行参数放 config.toml」这一归属约定。

    验收点:
      1. load_settings 在 SETTINGS_CANDIDATES 为空、仅 stocks.toml 存在时,
         能正确从 stocks.toml 的 [settings] 读出监控策略键(indicators / live_indicators /
         chg_alert / swing_alert / alert_cooldown); 运行参数(sources / refresh_sec)不在 stocks.toml。
      2. 同一键 config.toml 与 stocks.toml 都有时, stocks.toml 优先覆盖(以 indicators 验证)。
      3. 运行参数(sources / refresh_sec)随 config.toml 读出(单独隔离验证)。
      4. 用该 settings 调 monitor:
         - live_indicators=false 时 MA 只用日K收盘(实时价 100 不影响 MA5=10);
           live_indicators=true 时实时价并入(MA5≈28), 行为可区分。
         - indicators 含 KDJ 时 reasons 出现 KDJ 相关文案。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name, content):
        p = Path(self.tmp) / name
        p.write_text(content, encoding="utf-8")
        return p

    STOCKS_MONITORING = '''[settings]
indicators = ["MA", "RSI", "MACD", "KDJ"]
live_indicators = false
chg_alert = 3.0
swing_alert = 5.0
alert_cooldown = 15

[[stocks]]
code = "hk01810"
name = "小米集团-W"
'''

    def _load_from_stocks_only(self):
        """隔离: 清空 config 候选, 只从 stocks.toml 取配置。"""
        self._write("stocks.toml", self.STOCKS_MONITORING)
        with mock.patch.object(sf, "SCRIPT_DIR", self.tmp):
            with mock.patch.object(sf, "SETTINGS_CANDIDATES", []):
                with mock.patch.object(sf, "STOCKS_CANDIDATES", ["stocks.toml"]):
                    return sf.load_settings()

    def test_monitoring_keys_read_from_stocks_toml(self):
        s = self._load_from_stocks_only()
        self.assertEqual(s.get("indicators"), ["MA", "RSI", "MACD", "KDJ"])
        self.assertFalse(s.get("live_indicators"))
        self.assertEqual(s.get("chg_alert"), 3.0)
        self.assertEqual(s.get("swing_alert"), 5.0)
        self.assertEqual(s.get("alert_cooldown"), 15)
        # 运行参数(sources / refresh_sec)不属于 stocks.toml, 隔离场景读不到
        self.assertIsNone(s.get("sources"))
        self.assertIsNone(s.get("refresh_sec"))

    def test_stocks_toml_priority_over_config(self):
        # config.toml 写监控策略键(indicators, 向后兼容可用) + 运行参数(sources/refresh_sec, 原生键)
        # stocks.toml 覆盖监控策略键 -> stocks.toml 优先; 运行参数 stocks.toml 没有 -> 取 config.toml
        self._write("config.toml",
                    '[settings]\nindicators = ["MA"]\nrefresh_sec = 1\nsources = ["tencent"]\n')
        self._write("stocks.toml", self.STOCKS_MONITORING)
        with mock.patch.object(sf, "SCRIPT_DIR", self.tmp):
            with mock.patch.object(sf, "SETTINGS_CANDIDATES", ["config.toml"]):
                with mock.patch.object(sf, "STOCKS_CANDIDATES", ["stocks.toml"]):
                    s = sf.load_settings()
        # 监控策略键: stocks.toml 优先覆盖 config.toml 的同名键
        self.assertEqual(s.get("indicators"), ["MA", "RSI", "MACD", "KDJ"])
        # 运行参数: stocks.toml 未定义, 取 config.toml 原生值
        self.assertEqual(s.get("refresh_sec"), 1)
        self.assertEqual(s.get("sources"), ["tencent"])

    def test_runtime_keys_read_from_config_toml(self):
        # 隔离: 仅 config.toml, STOCKS_CANDIDATES 清空, 验证运行参数来自 config.toml
        self._write("config.toml",
                    '[settings]\nsources = ["tencent", "sina"]\nrefresh_sec = 3\n')
        with mock.patch.object(sf, "SCRIPT_DIR", self.tmp):
            with mock.patch.object(sf, "SETTINGS_CANDIDATES", ["config.toml"]):
                with mock.patch.object(sf, "STOCKS_CANDIDATES", []):
                    s = sf.load_settings()
        self.assertEqual(s.get("sources"), ["tencent", "sina"])
        self.assertEqual(s.get("refresh_sec"), 3)

    def test_monitor_live_indicators_false_uses_daily_close(self):
        settings = self._load_from_stocks_only()  # live_indicators=false
        # 日K平稳=10, 实时价剧烈偏离=100; live=false 时 MA 应只用日K收盘(=10)
        closes = [10.0] * 40
        r = rt(100.0, prev_close=10.0)
        with mock.patch.object(sf, "LIVE_INDICATORS", False):
            with mock.patch.object(sf, "get_kline", return_value=list(closes)):
                _t, _s, rec = sf.monitor(
                    {"code": "hk01810", "name": "X"}, rt=r, settings=settings)
        self.assertEqual(rec["ma5"], 10.0)   # 不受实时价 100 影响

        # 对照组: 同场景 live=true 会把实时价并入, MA5 显著不同(可区分)
        with mock.patch.object(sf, "LIVE_INDICATORS", True):
            with mock.patch.object(sf, "get_kline", return_value=list(closes)):
                _t2, _s2, rec2 = sf.monitor(
                    {"code": "hk01810", "name": "X"}, rt=r, settings=settings)
        self.assertNotEqual(rec2["ma5"], 10.0)  # 实时价并入 -> MA5≈28

    def test_monitor_kdj_reason_when_indicators_has_kdj(self):
        settings = self._load_from_stocks_only()  # indicators 含 KDJ
        closes2 = [float(i) for i in range(1, 41)]
        r2 = rt(40.0, prev_close=40.0)
        with mock.patch.object(sf, "LIVE_INDICATORS", False):
            with mock.patch.object(sf, "get_kline", return_value=list(closes2)):
                _t3, _s3, rec3 = sf.monitor(
                    {"code": "hk01810", "name": "X"}, rt=r2, settings=settings)
        self.assertTrue(any("KDJ" in x for x in rec3["reasons"]),
                        "indicators 含 KDJ 时 reasons 应出现 KDJ 文案")


# ----------------------------------------------------------------------------
# 15. P2 功能: 运行时增删自选(功能①) + 信号变动过滤(功能②) 纯函数契约
#     对应 QA 第2轮验证基准: parse_add_input / rewrite_stocks_toml / is_row_visible。
#     GUI 交互(＋按钮/simpledialog/右键菜单/🔎过滤/worker 时间戳)需真机验证。
# ----------------------------------------------------------------------------
class TestP2WatchlistFeatures(unittest.TestCase):
    def test_parse_add_input_with_name(self):
        self.assertEqual(sf.parse_add_input("sh600519,贵州茅台"),
                         {"code": "sh600519", "name": "贵州茅台"})

    def test_parse_add_input_name_fallback(self):
        # 仅有 code -> name 回退为 code
        self.assertEqual(sf.parse_add_input("hk01810"),
                         {"code": "hk01810", "name": "hk01810"})

    def test_parse_add_input_invalid_prefix_raises(self):
        # 非法前缀(如 xx123)/空串/None -> 抛 ValueError
        with self.assertRaises(ValueError):
            sf.parse_add_input("xx123")
        with self.assertRaises(ValueError):
            sf.parse_add_input("")
        with self.assertRaises(ValueError):
            sf.parse_add_input(None)

    def test_rewrite_stocks_toml_preserves_settings(self):
        tmp = tempfile.mkdtemp()
        try:
            path = Path(tmp) / "stocks.toml"
            path.write_text('''# 头注释
[settings]
indicators = ["MA", "RSI", "MACD", "KDJ"]
live_indicators = false
chg_alert = 3.0
swing_alert = 5.0
alert_cooldown = 15

[[stocks]]
code = "hk01810"
name = "小米集团-W"

[[stocks]]
code = "sh600062"
name = "华润双鹤"
''', encoding="utf-8")
            with mock.patch.object(sf, "SCRIPT_DIR", tmp):
                with mock.patch.object(sf, "STOCKS_CANDIDATES", ["stocks.toml"]):
                    # 删 hk01810, 加 sh600519
                    sf.rewrite_stocks_toml(
                        str(path),
                        add={"code": "sh600519", "name": "贵州茅台"},
                        remove="hk01810")
                    s = sf.load_settings()
                    stocks = sf.load_stocks()
            # [settings] 段与监控键完整不丢
            self.assertEqual(s.get("indicators"), ["MA", "RSI", "MACD", "KDJ"])
            self.assertFalse(s.get("live_indicators"))
            self.assertEqual(s.get("chg_alert"), 3.0)
            self.assertEqual(s.get("swing_alert"), 5.0)
            self.assertEqual(s.get("alert_cooldown"), 15)
            # 剩余股票在、新股票出现、被删者不在
            codes = [x["code"] for x in stocks]
            self.assertIn("sh600062", codes)
            self.assertIn("sh600519", codes)
            self.assertNotIn("hk01810", codes)
            # 无重复 [settings]、无残缺
            self.assertEqual(str(path.read_text(encoding="utf-8")).count("[settings]"), 1)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_rewrite_stocks_toml_keeps_support_and_dedup(self):
        tmp = tempfile.mkdtemp()
        try:
            path = Path(tmp) / "stocks.toml"
            path.write_text('''[settings]
indicators = ["MA"]

[[stocks]]
code = "hk01810"
name = "X"
support = [18.0, 17.5]
resistance = [20.5]
''', encoding="utf-8")
            with mock.patch.object(sf, "SCRIPT_DIR", tmp):
                with mock.patch.object(sf, "STOCKS_CANDIDATES", ["stocks.toml"]):
                    # 重复添加 hk01810 -> 忽略(去重)
                    sf.rewrite_stocks_toml(str(path), add={"code": "hk01810", "name": "X"})
                    stocks = sf.load_stocks()
            self.assertEqual(len(stocks), 1)
            self.assertEqual(stocks[0]["support"], [18.0, 17.5])
            self.assertEqual(stocks[0]["resistance"], [20.5])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_is_row_visible(self):
        now = time.time()
        # 信号行(下半)默认只展示有信号变动的股票: 无变动时间戳 -> 不可见
        self.assertFalse(sf.is_row_visible(None))
        # 窗口内变动 -> 可见
        self.assertTrue(sf.is_row_visible(now))
        # 超窗口 -> 不可见
        self.assertFalse(sf.is_row_visible(now - 1000))


# ----------------------------------------------------------------------------
# 17. P3 补充: 窗口置顶(always-on-top) 界面开关(功能④)
#     GUI 按钮点击的图标/状态栏反馈只做代码审查, 标注「需真机验证」, 不实例化 Tk。
#     配置键 topmost 走 settings.get(..., True), 默认置顶(启动置顶)。
#     核心副作用由 set_topmost() 薄封装 root.attributes, 用桩对象覆盖。
# ----------------------------------------------------------------------------
class _FakeRoot:
    """记录 attributes 调用, 用于无头验证 set_topmost 的核心副作用。"""
    def __init__(self):
        self.calls = []

    def attributes(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class TestTopmostToggle(unittest.TestCase):
    def test_topmost_default_true(self):
        # 未配置 topmost 时, settings.get("topmost", True) 应默认 True(启动置顶)
        fixture = {"settings": {"refresh_sec": 3}, "stocks": [{"code": "hk01810"}]}
        with mock.patch.object(sf, "_load_first", return_value=("fake", fixture)):
            s = sf.load_settings()
        self.assertTrue(s.get("topmost", True))

    def test_topmost_read_from_config(self):
        # config.toml 含 topmost = false -> 经 load_settings 后为 False(启动不置顶)
        fixture = {"settings": {"topmost": False}, "stocks": [{"code": "hk01810"}]}
        with mock.patch.object(sf, "_load_first", return_value=("fake", fixture)):
            s = sf.load_settings()
        self.assertFalse(s.get("topmost"))

    def test_set_topmost_stub(self):
        # set_topmost 薄封装 root.attributes("-topmost", on); 用桩对象覆盖核心副作用
        fake = _FakeRoot()
        sf.set_topmost(fake, True)
        self.assertEqual(fake.calls[-1], (("-topmost", True), {}))
        sf.set_topmost(fake, False)
        self.assertEqual(fake.calls[-1], (("-topmost", False), {}))
        # 验证启动初始化与 _toggle_topmost 共用同一入口(单一入口约定)
        self.assertEqual(len(fake.calls), 2)

    def test_set_topmost_non_bool_normalizes(self):
        # 健壮性(边缘用例补强): 实现使用 bool(on) 归一, 非布尔入参应被正确转换。
        # 重点确认不是直接传 on, 而是经 bool() 归一, 否则 0/"" 会被 tkinter 默认当成真值。
        fake = _FakeRoot()
        # 整数 1 -> True, 0 -> False
        sf.set_topmost(fake, 1)
        self.assertEqual(fake.calls[-1], (("-topmost", True), {}))
        sf.set_topmost(fake, 0)
        self.assertEqual(fake.calls[-1], (("-topmost", False), {}))
        # 非空字符串 "yes" -> True, 空字符串 "" -> False
        sf.set_topmost(fake, "yes")
        self.assertEqual(fake.calls[-1], (("-topmost", True), {}))
        sf.set_topmost(fake, "")
        self.assertEqual(fake.calls[-1], (("-topmost", False), {}))
        # None 被归一为 False(避免误置顶)
        sf.set_topmost(fake, None)
        self.assertEqual(fake.calls[-1], (("-topmost", False), {}))
        # 全部经过 bool() 归一, 共 5 次调用
        self.assertEqual(len(fake.calls), 5)

    def test_set_topmost_string_false_pitfall(self):
        # 已知设计取舍(非 Bug)的「表征测试(characterization)」: 锁定当前行为并显式标注。
        # 实现使用 bool(on) 而非 on: 若有人直接传字符串 "false"/"0", 因 bool("false")==True /
        # bool("0")==True, 会误判为「置顶 True」。
        # 但正常路径中 config 经 TOML 解析后是真正的 bool(true/false), 不会以字符串形态进入
        # set_topmost, 故正常路径无此问题 —— 据此判定为「可接受的设计取舍」, 无需改业务源码。
        # 若未来接口可能接收外部字符串(如命令行/HTTP 入参), 再考虑加防御:
        #   root.attributes("-topmost", bool(on) and str(on).lower() not in ("false", "0", "no", "off"))
        fake = _FakeRoot()
        sf.set_topmost(fake, "false")
        self.assertEqual(fake.calls[-1], (("-topmost", True), {}))
        sf.set_topmost(fake, "0")
        self.assertEqual(fake.calls[-1], (("-topmost", True), {}))

    # 注: _toggle_topmost 为 run_hud 闭包, 无法在无头环境直接 import;
    #     按钮点击的图标翻转(📌/📍)与状态栏文本需真机目测验证, 此处不实例化 Tk。



# ----------------------------------------------------------------------------
# 16. 界面颜色灰色程度 (grayness / desaturate)
# ----------------------------------------------------------------------------
class TestGrayness(unittest.TestCase):
    def test_desaturate_zero_returns_original(self):
        # amount=0.0 逐字返回原色(保证默认行为不变)
        for c in ("#d93025", "#188038", "#1a66c0"):
            self.assertEqual(sf.desaturate(c, 0.0), c)

    def test_desaturate_one_is_gray(self):
        # amount=1.0 三通道相等(纯灰阶)
        g1 = sf.desaturate("#d93025", 1.0)
        g2 = sf.desaturate("#188038", 1.0)
        self.assertEqual(g1[1:3], g1[3:5])
        self.assertEqual(g1[3:5], g1[5:7])
        self.assertEqual(g2[1:3], g2[3:5])
        self.assertEqual(g2[3:5], g2[5:7])
        # 独立复算精确灰阶值: #d93025 luma=round(0.299*217+0.587*48+0.114*37)=97 -> #616161
        self.assertEqual(g1, "#616161")
        # 精确值兜底: 三通道均须等于 luma(97)
        self.assertEqual((int(g1[1:3], 16), int(g1[3:5], 16), int(g1[5:7], 16)), (97, 97, 97))

    def test_desaturate_half_mixes(self):
        # 中间值: 非原色、非纯灰, 且逐通道介于原色与灰阶之间
        out = sf.desaturate("#d93025", 0.5)
        self.assertNotEqual(out, "#d93025")
        # 纯灰阶值(#616161, luma=97)
        self.assertNotEqual(out, "#616161")
        r = int(out[1:3], 16)
        g = int(out[3:5], 16)
        b = int(out[5:7], 16)
        orig_r, orig_g, orig_b = 0xd9, 0x30, 0x25
        gray_v = 97
        for ch, orig in ((r, orig_r), (g, orig_g), (b, orig_b)):
            self.assertLessEqual(min(orig, gray_v), ch)
            self.assertGreaterEqual(max(orig, gray_v), ch)

    def test_build_style_grayness_one_all_gray(self):
        # grayness=1.0 时, 所有强调色三通道相等
        st = sf.build_style({"grayness": 1.0})
        for v in st["sig_colors"].values():
            rv = int(v[1:3], 16)
            gv = int(v[3:5], 16)
            bv = int(v[5:7], 16)
            self.assertEqual(rv, gv)
            self.assertEqual(gv, bv)
        up = st["up"]
        self.assertEqual(int(up[1:3], 16), int(up[3:5], 16))
        self.assertEqual(int(up[3:5], 16), int(up[5:7], 16))
        down = st["down"]
        self.assertEqual(int(down[1:3], 16), int(down[3:5], 16))
        self.assertEqual(int(down[3:5], 16), int(down[5:7], 16))

    def test_build_style_grayness_zero_matches_default(self):
        # grayness=0.0 与未配置完全一致(默认行为不变)
        a = sf.build_style({})
        b = sf.build_style({"grayness": 0.0})
        self.assertEqual(a["up"], b["up"])
        self.assertEqual(a["down"], b["down"])
        self.assertEqual(a["sig_colors"], b["sig_colors"])

    def test_build_style_non_accent_colors_not_desaturated(self):
        # 验证目标#4: grayness=1.0 时非强调色(bg/fg/fg_dim/flat/dl/header/sep)
        # 必须仍等于原调色板值(未被去饱和)。
        st = sf.build_style({"grayness": 1.0})
        # 亮色调色板(默认 light)逐项对拍
        for key in ("bg", "fg", "fg_dim", "flat", "dl", "header", "sep"):
            self.assertEqual(st[key], sf.LIGHT_PALETTE[key],
                             f"{key} 在 grayness=1.0 被错误去饱和")
        # 暗色同理: 切 dark 后仍保持原调色板(不被 desaturate 影响)
        st_dark = sf.build_style({"grayness": 1.0, "float_theme": "dark"})
        for key in ("bg", "fg", "fg_dim", "flat", "dl", "header", "sep"):
            self.assertEqual(st_dark[key], sf.DARK_PALETTE[key],
                             f"dark {key} 在 grayness=1.0 被错误去饱和")




# ----------------------------------------------------------------------------
# 17b. 增强: 信号行(下半)显隐状态机(功能②) —— 抽成纯函数 apply_sig_visibility,
#      用桩对象无头验证「显→隐→显→隐」「幂等」「None 安全」。行情行(上半)始终可见, 本函数不碰。
# ----------------------------------------------------------------------------
class _StubSigRow:
    """模拟信号行 tk.Frame 的最小桩: 记录 pack/pack_forget 调用, 维护 _mapped 映射状态。"""
    def __init__(self, mapped: bool = False):
        self._mapped = mapped
        self.calls = []

    def pack(self, **kw):
        self._mapped = True
        self.calls.append(("pack", dict(kw)))

    def pack_forget(self):
        self._mapped = False
        self.calls.append(("pack_forget", ()))

    def winfo_ismapped(self):
        return self._mapped


class TestApplySigVisibility(unittest.TestCase):
    """守 apply_sig_visibility 仅控制信号行显隐、幂等、对 None 安全。"""

    @staticmethod
    def _pack_calls(stub):
        return [c for c in stub.calls if c[0] == "pack"]

    @staticmethod
    def _forget_calls(stub):
        return [c for c in stub.calls if c[0] == "pack_forget"]

    def test_visible_packs_when_unmapped(self):
        # visible=True 且未映射 -> 调 pack, 不调 pack_forget
        sf_row = _StubSigRow(mapped=False)
        sf.apply_sig_visibility(sf_row, True, dict(fill="x"))
        self.assertEqual(len(self._pack_calls(sf_row)), 1, "未映射+可见必须 pack")
        self.assertEqual(len(self._forget_calls(sf_row)), 0, "可见不应 pack_forget")
        self.assertTrue(sf_row.winfo_ismapped(), "pack 后应为已映射")

    def test_visible_always_packs_regardless_of_mapped(self):
        # 新可靠行为: visible=True 时无条件 pack(幂等), 不依赖 winfo_ismapped——
        # macOS Tk 上 winfo_ismapped 对可见控件常返回 False, 旧守卫会导致"该显不显"。
        # 即便已映射, 仍应 pack 一次(幂等空操作), 映射状态保持。
        sf_row = _StubSigRow(mapped=True)
        sf.apply_sig_visibility(sf_row, True, dict(fill="x"))
        self.assertEqual(len(self._pack_calls(sf_row)), 1, "可见应无条件 pack(已映射也 pack, 幂等)")
        self.assertEqual(len(self._forget_calls(sf_row)), 0)
        self.assertTrue(sf_row.winfo_ismapped())

    def test_no_winformismapped_dependency(self):
        # 强锁: 函数不得查询 winfo_ismapped 来决定显隐(macOS 上该值不可信)。
        # 用会抛异常的 winfo_ismapped 替代, 若被调用则测试失败。
        sf_row = _StubSigRow(mapped=True)
        sf_row.winfo_ismapped = lambda: (_ for _ in ()).throw(
            RuntimeError("winfo_ismapped must not be called"))
        try:
            sf.apply_sig_visibility(sf_row, True, dict(fill="x"))
            sf.apply_sig_visibility(sf_row, False, dict(fill="x"))
        except RuntimeError as exc:  # pragma: no cover
            self.fail(f"apply_sig_visibility 仍依赖 winfo_ismapped: {exc}")
        self.assertEqual(len(self._pack_calls(sf_row)), 1, "可见应 pack 一次")
        self.assertEqual(len(self._forget_calls(sf_row)), 1, "隐藏应 pack_forget 一次")

    def test_hidden_forgets_when_mapped(self):
        # visible=False 且已映射 -> 调 pack_forget, 不调 pack
        sf_row = _StubSigRow(mapped=True)
        sf.apply_sig_visibility(sf_row, False, dict(fill="x"))
        self.assertEqual(len(self._forget_calls(sf_row)), 1, "已映射+隐藏必须 pack_forget")
        self.assertEqual(len(self._pack_calls(sf_row)), 0, "隐藏不应 pack")
        self.assertFalse(sf_row.winfo_ismapped(), "pack_forget 后应未映射")

    def test_hidden_always_forgets_regardless_of_mapped(self):
        # 新可靠行为: visible=False 时无条件 pack_forget(幂等空操作), 不依赖 winfo_ismapped。
        # 即便未映射, 仍应 pack_forget 一次(无副作用), 且保持未映射。
        sf_row = _StubSigRow(mapped=False)
        sf.apply_sig_visibility(sf_row, False, dict(fill="x"))
        self.assertEqual(len(self._forget_calls(sf_row)), 1, "隐藏应无条件 pack_forget(未映射也调, 幂等)")
        self.assertEqual(len(self._pack_calls(sf_row)), 0)
        self.assertFalse(sf_row.winfo_ismapped())

    def test_none_is_noop(self):
        # sf_=None 无操作、不抛异常
        try:
            sf.apply_sig_visibility(None, True, dict(fill="x"))
            sf.apply_sig_visibility(None, False, dict(fill="x"))
        except Exception as exc:  # pragma: no cover
            self.fail(f"apply_sig_visibility(None, ...) 抛异常: {exc}")


# ----------------------------------------------------------------------------
# 18. 右键删除自选(功能①) —— 纯逻辑: 内存列表 + bookkeeping 字典清理
# ----------------------------------------------------------------------------
class TestRemoveStockFromMemory(unittest.TestCase):
    def test_removes_and_cleans_all_dicts(self):
        stocks = [{"code": "hk01810", "name": "小米"},
                  {"code": "sh600519", "name": "茅台"}]
        bookkeeping = {
            "rows": {"hk01810": object(), "sh600519": object()},
            "sig_rows": {"hk01810": object(), "sh600519": object()},
            "row_vis": {"hk01810": True, "sh600519": True},
            "last_sig_change": {"hk01810": 1.0, "sh600519": 2.0},
            "last_sigs": {"hk01810": {"sig": "x"}, "sh600519": {"sig": "y"}},
        }
        removed = sf.remove_stock_from_memory(stocks, "hk01810", bookkeeping)
        self.assertEqual(removed["code"], "hk01810")
        self.assertEqual([s["code"] for s in stocks], ["sh600519"])
        # 所有 bookkeeping 字典均已清理该 code
        for name, d in bookkeeping.items():
            self.assertNotIn("hk01810", d, f"{name} 未清理 hk01810")
        # 其余 code 不受影响
        self.assertIn("sh600519", bookkeeping["rows"])

    def test_absent_code_noop(self):
        stocks = [{"code": "hk01810"}]
        removed = sf.remove_stock_from_memory(stocks, "nope")
        self.assertIsNone(removed)
        self.assertEqual([s["code"] for s in stocks], ["hk01810"])

    def test_no_bookkeeping_ok(self):
        stocks = [{"code": "hk01810"}, {"code": "sh600519"}]
        removed = sf.remove_stock_from_memory(stocks, "sh600519")
        self.assertEqual(removed["code"], "sh600519")
        self.assertEqual([s["code"] for s in stocks], ["hk01810"])


# ----------------------------------------------------------------------------
# 19. 右键删除自选(功能①) —— rewrite_stocks_toml 删除分支端到端
#     用临时 stocks.toml(含 [settings] 段与若干 [[stocks]]), 验证删后文件正确。
# ----------------------------------------------------------------------------
class TestRewriteStocksTomlRemove(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "stocks.toml"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, content):
        self.path.write_text(content, encoding="utf-8")

    def test_remove_preserves_settings_and_others(self):
        self._write('''# 顶部注释保留
[settings]
indicators = ["MA", "RSI", "MACD"]
chg_alert = 3.0

[[stocks]]
code = "hk01810"
name = "小米集团-W"

[[stocks]]
code = "sh600519"
name = "贵州茅台"

[[stocks]]
code = "usAAPL"
name = "苹果"
''')
        p = str(self.path)
        # 删除 sh600519
        result = sf.rewrite_stocks_toml(p, remove="sh600519")
        # (a) 返回列表不含被删 code, 其余完整
        codes = [s["code"] for s in result]
        self.assertNotIn("sh600519", codes)
        self.assertEqual(codes, ["hk01810", "usAAPL"])
        # (b) 重写后文件解析正确
        parsed = sf.tomllib.loads(self.path.read_text(encoding="utf-8"))
        # [settings] 段原样保留
        self.assertIn("settings", parsed)
        self.assertEqual(parsed["settings"].get("indicators"), ["MA", "RSI", "MACD"])
        self.assertEqual(parsed["settings"].get("chg_alert"), 3.0)
        # 头注释保留(文本层校验)
        self.assertIn("# 顶部注释保留", self.path.read_text(encoding="utf-8"))
        # [[stocks]] 其余项完整
        codes2 = [s["code"] for s in parsed.get("stocks", [])]
        self.assertEqual(codes2, ["hk01810", "usAAPL"])
        self.assertNotIn("sh600519", codes2)
        # 仅一个 [settings] 段(无残留/重复)
        self.assertEqual(self.path.read_text(encoding="utf-8").count("[settings]"), 1)

    def test_remove_only_one_of_duplicates(self):
        self._write('''[settings]
indicators = ["MA"]

[[stocks]]
code = "hk01810"
name = "X"

[[stocks]]
code = "hk01810"
name = "X"

[[stocks]]
code = "sh600519"
name = "Y"
''')
        result = sf.rewrite_stocks_toml(str(self.path), remove="hk01810")
        # 两处 hk01810 全部移除, sh600519 保留
        self.assertEqual([s["code"] for s in result], ["sh600519"])
        parsed = sf.tomllib.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual([s["code"] for s in parsed.get("stocks", [])], ["sh600519"])


# ----------------------------------------------------------------------------
# 19b. 个股参数面板(功能④): 文本解析纯函数 + rewrite_stocks_toml 个股更新分支
# ----------------------------------------------------------------------------
class TestParseParamText(unittest.TestCase):
    def test_parse_levels_txt(self):
        # 空 / 纯空白 -> None(不配置)
        self.assertIsNone(sf.parse_levels_txt(""))
        self.assertIsNone(sf.parse_levels_txt("   "))
        # 逗号分隔(含中英文逗号容错不需要, 仅英文逗号)多价位
        self.assertEqual(sf.parse_levels_txt("18.0, 17.5"), [18.0, 17.5])
        self.assertEqual(sf.parse_levels_txt("18,17.5"), [18.0, 17.5])
        # 全非法 -> None
        self.assertIsNone(sf.parse_levels_txt("abc"))
        # 部分非法 -> 只取合法项
        self.assertEqual(sf.parse_levels_txt("18.0, abc"), [18.0])

    def test_parse_pct_txt(self):
        self.assertIsNone(sf.parse_pct_txt(""))
        self.assertEqual(sf.parse_pct_txt("3.5"), 3.5)
        self.assertEqual(sf.parse_pct_txt("0"), 0.0)
        self.assertIsNone(sf.parse_pct_txt("-1"))     # 负数视为不配置
        self.assertIsNone(sf.parse_pct_txt("abc"))    # 非法视为不配置


class TestRewriteStocksTomlUpdate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "stocks.toml"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, content):
        self.path.write_text(content, encoding="utf-8")

    def _base(self):
        return '''# 顶部注释保留
[settings]
chg_alert = 3.0

[[stocks]]
code = "hk01810"
name = "小米集团-W"

[[stocks]]
code = "sh600519"
name = "贵州茅台"
'''

    def test_update_params_writes_and_preserves_others(self):
        self._write(self._base())
        p = str(self.path)
        result = sf.rewrite_stocks_toml(
            p, update_code="hk01810",
            update_data={"support": [18.0, 17.5], "resistance": [20.5],
                         "chg_alert": 2.5, "swing_alert": 4.0})
        # 返回列表: 目标股四参数已更新
        st = next(s for s in result if s["code"] == "hk01810")
        self.assertEqual(st["support"], [18.0, 17.5])
        self.assertEqual(st["resistance"], [20.5])
        self.assertEqual(st["chg_alert"], 2.5)
        self.assertEqual(st["swing_alert"], 4.0)
        # 其它股票不受影响(不附带任何参数键)
        others = [s for s in result if s["code"] != "hk01810"]
        for o in others:
            for k in ("support", "resistance", "chg_alert", "swing_alert"):
                self.assertNotIn(k, o)
        # 文件回读一致 + 头注释/[settings] 保留
        raw = self.path.read_text(encoding="utf-8")
        self.assertIn("# 顶部注释保留", raw)
        self.assertEqual(raw.count("[settings]"), 1)
        parsed = sf.tomllib.loads(raw)
        st2 = next(s for s in parsed["stocks"] if s["code"] == "hk01810")
        self.assertEqual(st2["support"], [18.0, 17.5])
        self.assertEqual(st2["resistance"], [20.5])
        self.assertEqual(st2["chg_alert"], 2.5)
        self.assertEqual(st2["swing_alert"], 4.0)
        self.assertEqual(parsed["settings"]["chg_alert"], 3.0)   # 全局阈值不动

    def test_update_clear_fields_removes_keys(self):
        self._write(self._base() + '''[[stocks]]
code = "usAAPL"
name = "苹果"
support = [100.0]
chg_alert = 1.0
''')
        p = str(self.path)
        result = sf.rewrite_stocks_toml(
            p, update_code="usAAPL",
            update_data={"support": None, "resistance": None,
                         "chg_alert": None, "swing_alert": None})
        st = next(s for s in result if s["code"] == "usAAPL")
        for k in ("support", "resistance", "chg_alert", "swing_alert"):
            self.assertNotIn(k, st)
        # 文件层面: usAAPL 块不再含这些键
        parsed = sf.tomllib.loads(self.path.read_text(encoding="utf-8"))
        st2 = next(s for s in parsed["stocks"] if s["code"] == "usAAPL")
        for k in ("support", "resistance", "chg_alert", "swing_alert"):
            self.assertNotIn(k, st2)

    def test_update_unknown_code_noop(self):
        self._write(self._base())
        result = sf.rewrite_stocks_toml(
            str(self.path), update_code="nonexist", update_data={"chg_alert": 1.0})
        self.assertEqual([s["code"] for s in result], ["hk01810", "sh600519"])


# ----------------------------------------------------------------------------
# N. 刷新频率切换是否需要「可能被 ban」确认框 (纯函数, 无头直测)
# ----------------------------------------------------------------------------
class TestRefreshBanWarning(unittest.TestCase):
    """覆盖 stock_float.refresh_requires_ban_warning 的判定语义。

    该函数为模块级纯函数, 不依赖 Tk / GUI, 可无头直测, 无需 mock。
    语义: 仅当即将切换到的刷新周期为 1 秒时返回 True(需弹确认框警告
    数据源可能被限流/封禁), 其余周期一律返回 False。
    """

    def test_nxt_1_requires_warning(self):
        """切到 1 秒刷新 -> 必须弹确认框 (True)。"""
        # Arrange / Act
        result = sf.refresh_requires_ban_warning(1)
        # Assert
        self.assertIs(True, result)
        self.assertTrue(result)

    def test_nxt_3_no_warning(self):
        """切到 3 秒刷新 -> 不弹确认框 (False)。"""
        result = sf.refresh_requires_ban_warning(3)
        self.assertIs(False, result)
        self.assertFalse(result)

    def test_nxt_5_no_warning(self):
        """切到 5 秒刷新 -> 不弹确认框 (False)。"""
        result = sf.refresh_requires_ban_warning(5)
        self.assertIs(False, result)
        self.assertFalse(result)

    def test_nxt_2_no_warning_strict_equality(self):
        """切到 2 秒刷新 -> 不弹确认框 (False)。

        证明判定是严格等于 1, 而非「小于等于某个阈值」。
        """
        result = sf.refresh_requires_ban_warning(2)
        self.assertIs(False, result)
        self.assertFalse(result)

    def test_nxt_0_no_warning(self):
        """切到 0 秒(暂停/停更) -> 不弹确认框 (False)。"""
        result = sf.refresh_requires_ban_warning(0)
        self.assertIs(False, result)
        self.assertFalse(result)

    def test_nxt_ten_no_warning(self):
        """切到较大周期(10 秒) -> 不弹确认框 (False)。"""
        result = sf.refresh_requires_ban_warning(10)
        self.assertIs(False, result)
        self.assertFalse(result)


class TestMoveStockInOrder(unittest.TestCase):
    """手动排序纯函数 move_stock_in_order / _reorder_stocks 的无头单测(不依赖 Tk)。"""

    def test_middle_up(self):
        """中间元素上移一步正确。"""
        order = ["a", "b", "c", "d"]
        self.assertEqual(sf.move_stock_in_order(order, "b", "up"), ["b", "a", "c", "d"])

    def test_middle_down(self):
        """中间元素下移一步正确。"""
        order = ["a", "b", "c", "d"]
        self.assertEqual(sf.move_stock_in_order(order, "b", "down"), ["a", "c", "b", "d"])

    def test_first_up_noop(self):
        """首元素上移 -> 不变(返回副本, 不原地修改)。"""
        order = ["a", "b", "c"]
        result = sf.move_stock_in_order(order, "a", "up")
        self.assertEqual(result, ["a", "b", "c"])
        self.assertIsNot(result, order)

    def test_last_down_noop(self):
        """末元素下移 -> 不变。"""
        order = ["a", "b", "c"]
        result = sf.move_stock_in_order(order, "c", "down")
        self.assertEqual(result, ["a", "b", "c"])

    def test_unknown_code_noop(self):
        """不存在的 code -> 不变。"""
        order = ["a", "b", "c"]
        result = sf.move_stock_in_order(order, "z", "up")
        self.assertEqual(result, ["a", "b", "c"])
        self.assertIsNot(result, order)

    def test_invalid_direction_noop(self):
        """非法 direction -> 不变。"""
        order = ["a", "b", "c"]
        self.assertEqual(sf.move_stock_in_order(order, "b", "left"), ["a", "b", "c"])

    def test_full_order_integrity(self):
        """多元素顺序整体正确: 末元素上移后落到倒数第二, 元素集合不变。"""
        order = ["a", "b", "c", "d", "e"]
        result = sf.move_stock_in_order(order, "e", "up")
        self.assertEqual(result, ["a", "b", "c", "e", "d"])
        self.assertEqual(sorted(result), sorted(order))

    def test_reorder_stocks_keeps_extra(self):
        """reorder 中遗漏的 code 保持原相对序追加于末尾。"""
        stocks = [{"code": "a"}, {"code": "b"}, {"code": "c"}, {"code": "d"}]
        result = sf._reorder_stocks(stocks, ["c", "a"])
        self.assertEqual([s["code"] for s in result], ["c", "a", "b", "d"])

    def test_reorder_stocks_preserves_objects(self):
        """reorder 后仍是原 dict 对象(引用不变)。"""
        a = {"code": "a"}
        stocks = [a, {"code": "b"}]
        result = sf._reorder_stocks(stocks, ["b", "a"])
        self.assertEqual([s["code"] for s in result], ["b", "a"])
        self.assertIs(result[1], a)


# ----------------------------------------------------------------------------
# 10. 主题强调色 + 信号提示开关 (功能①②)
# ----------------------------------------------------------------------------


# ----------------------------------------------------------------------------
# 12. 设置面板: 透明度 / 灰度(实时生效 + 持久化) 与 信号整块显隐
#     闭包(_apply_alpha / _apply_grayness / _reapply_style / _toggle_signal)无头无法
#     import, 涉及 Toplevel / 真实 widget 重刷的项标注「需真机目测」, 不伪造通过。
# ----------------------------------------------------------------------------
class TestSettingsPanel(unittest.TestCase):
    def test_rebuild_style_with_grayness(self):
        # grayness 改变强调色(涨/跌/信号色)的饱和度, 但 bg/fg 等非强调色有意不动
        st1 = sf.build_style({"grayness": 0.0})
        st2 = sf.build_style({"grayness": 1.0})
        self.assertNotEqual(st1["up"], st2["up"])            # 涨色被去饱和
        self.assertNotEqual(st1["sig_colors"], st2["sig_colors"])  # 信号色被去饱和
        # bg 等非强调色(设计上有意)不受 grayness 影响,
        # 与 test_build_style_non_accent_colors_not_desaturated 一致
        self.assertEqual(st1["bg"], st2["bg"])

    def test_save_config_key_roundtrip(self):
        # _save_config_key 写入 [settings] 段并持久化; float 落为裸数值(合法 TOML)
        try:
            import tomllib
        except ImportError:
            tomllib = None
        if tomllib is None:
            self.skipTest("tomllib 不可用(需 py3.11+)")
        with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as f:
            f.write("[settings]\nfloat_alpha = 0.90\n")
            tmppath = f.name
        try:
            sf._save_config_key("float_alpha", 0.95, path=tmppath)
            with open(tmppath, "rb") as fh:
                data = tomllib.load(fh)
            self.assertEqual(data["settings"]["float_alpha"], 0.95)
            self.assertNotEqual(data["settings"]["float_alpha"], 0.90)
            # 再写 grayness, 两键并存
            sf._save_config_key("grayness", 0.3, path=tmppath)
            with open(tmppath, "rb") as fh:
                data = tomllib.load(fh)
            self.assertEqual(data["settings"]["grayness"], 0.3)
            self.assertEqual(data["settings"]["float_alpha"], 0.95)
        finally:
            os.unlink(tmppath)

    def test_hex_color_invalid_and_none_returns_default(self):
        # _hex_color 对 None / 非法串返回 default; 合法 3/6 位(含大小写)原值返回
        # (该函数与主题色无关, 保留其覆盖以防回归)
        self.assertIsNone(sf._hex_color(None, None))
        self.assertEqual(sf._hex_color("not-a-color", "#000000"), "#000000")
        self.assertEqual(sf._hex_color("#zzz", None), None)
        self.assertEqual(sf._hex_color("#abc", "#000000"), "#abc")
        self.assertEqual(sf._hex_color("#a1b2c3", "#000000"), "#a1b2c3")
        self.assertEqual(sf._hex_color("#ABCDEF", "#000000"), "#ABCDEF")

    def test_toggle_signal_hides_header_widgets(self):
        # _toggle_signal 是 run_hud 内闭包, 无头无法 import/实例化 Tk 根;
        # 信号区整块显隐(分隔线 sep / 标题 sighead / 容器 sigpane 的 pack/pack_forget)
        # 由代码层 winfo_ismapped 守卫 + before=status 保序保证, 需真机目测。
        self.skipTest("信号区整块显隐需真机目测: _toggle_signal 为 run_hud 闭包, 无头无法 import")

    def test_apply_alpha_bounds(self):
        # _apply_alpha 是 run_hud 内闭包, 依赖真实 Tk 根(root.attributes)与 nonlocal alpha,
        # 无头无法 import; 其边界 clamp(max(0.30, min(1.0, val))) 逻辑需真机目测。
        self.skipTest("透明度边界 clamp 需真机目测: _apply_alpha 为 run_hud 闭包, 无头无法 import")


class TestFormatStockName(unittest.TestCase):
    """format_stock_name 纯函数: 延时源在股票名后追加（延时）提示。"""
    def test_format_stock_name(self):
        self.assertEqual(sf.format_stock_name("小米", False), "小米")
        self.assertEqual(sf.format_stock_name("小米", True), "小米（延时）")
        self.assertEqual(sf.format_stock_name("腾讯控股", True), "腾讯控股（延时）")


class TestForceRefreshEvent(unittest.TestCase):
    """↺ 立即刷新: 依赖 Tk 主循环与后台 worker 线程, 无头无法 import 闭包/局部事件。"""
    def test_force_refresh_sets_event(self):
        # _force_refresh 与 refresh_event 是 run_hud 闭包/局部变量, 无法无头 import。
        self.skipTest("↺ 立即刷新需真机目测: 依赖 Tk 主循环与 worker 线程")


class TestSignalBecameChanged(unittest.TestCase):
    """回归锚点: 信号档位"真实变动"判定。

    针对修复: 启动瞬时 prev_sig 为 None, 若 (prev_sig != sig) 直接视为变动,
    会导致所有股票在信号面板假变动可见 300s(信号提示没有仅展示有变动 bug)。
    修正后: 首次观测(prev_sig is None)不计为变动。
    """
    def test_first_observation_not_a_change(self):
        # 回归锚点: 无 prev_sig 的首次观测必须不是"变动"
        self.assertFalse(sf.signal_became_changed(None, "L"))
        self.assertFalse(sf.signal_became_changed(None, "H"))

    def test_real_change_is_a_change(self):
        self.assertTrue(sf.signal_became_changed("L", "H"))
        self.assertTrue(sf.signal_became_changed("H", "L"))
        self.assertTrue(sf.signal_became_changed("⚪ 持有/观望", "🔴 买入(偏强)"))

    def test_same_level_not_a_change(self):
        self.assertFalse(sf.signal_became_changed("L", "L"))
        self.assertFalse(sf.signal_became_changed("H", "H"))

    def test_none_to_none_not_a_change(self):
        self.assertFalse(sf.signal_became_changed(None, None))


# ----------------------------------------------------------------------------
# 20. 股票模糊搜索: _parse_search_response + search_stocks (纯函数 / 桩 fetch)
#     无 headless 限制: 不实例化 Tk, 不真实联网; 用 mock.patch 隔离 sf.fetch。
# ----------------------------------------------------------------------------
class TestStockSearch(unittest.TestCase):
    """验证腾讯 smartbox 搜索响应的解析与搜索入口。

    - _parse_search_response: 市场前缀(sh/sz/hk/us)、未知市场跳过、
      按 code 去重、非法/空/无 v_hint 优雅降级为 []。
    - search_stocks: 空/空白查询短路返回 []; 委托 sf.fetch 后解析; 网络异常吞掉返回 []。
    """

    # 腾讯 smartbox 真实格式: v_hint="market~code~name~pinyin~type^..."
    HAPPY_HINT = (
        'v_hint="sh~600519~贵州茅台~gzmt~GP-A'
        '^sz~000001~平安银行~payh~GP-A'
        '^hk~00700~腾讯控股~txk~GP'
        '^us~AAPL~苹果~pg~US"'
    )

    EXPECTED_HAPPY = [
        {"code": "sh600519", "name": "贵州茅台"},
        {"code": "sz000001", "name": "平安银行"},
        {"code": "hk00700", "name": "腾讯控股"},
        {"code": "usAAPL", "name": "苹果"},
    ]

    def test_parse_search_response_happy_path(self):
        # 市场前缀直接拼接: sh/sz/hk/us + 代码
        result = sf._parse_search_response(self.HAPPY_HINT)
        self.assertEqual(result, self.EXPECTED_HAPPY)

    def test_parse_unknown_market_skipped(self):
        # 未知市场(xx)项必须被丢弃, 仅保留有效项
        hint = ('v_hint="xx~999~X~x~GP'
                '^sh~600519~贵州茅台~gzmt~GP-A"')
        result = sf._parse_search_response(hint)
        self.assertEqual(result, [{"code": "sh600519", "name": "贵州茅台"}])

    def test_parse_dedup_by_code(self):
        # 相同 code 出现两次 → 结果仅一条 (按 code 去重)
        hint = ('v_hint="sh~600519~贵州茅台~gzmt~GP-A'
                '^sh~600519~贵州茅台~gzmt~GP-A"')
        result = sf._parse_search_response(hint)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["code"], "sh600519")

    def test_parse_graceful_on_bad_input(self):
        # 空串 / 无 v_hint / 字段不足 → 均返回 []
        self.assertEqual(sf._parse_search_response(""), [])
        self.assertEqual(sf._parse_search_response("no hint here"), [])
        self.assertEqual(sf._parse_search_response('v_hint=""'), [])
        # 防御性: ~ 不足 3 段 / 字段缺失 → 该条跳过, 整体 []
        self.assertEqual(sf._parse_search_response('v_hint="bad"'), [])
        self.assertEqual(sf._parse_search_response('v_hint="sh~~茅台~gzmt~GP-A"'), [])

    def test_parse_search_response_decodes_unicode_escapes(self):
        # Bug 1 回归: smartbox 返回的 name 含字面 \uXXXX 转义必须解码为明文中文
        # (如 'TCL\\u79d1\\u6280' → 'TCL科技'); 明文中文名不受影响。
        hint = 'v_hint="sz~000100~TCL\\u79d1\\u6280~tkl~GP-A"'
        result = sf._parse_search_response(hint)
        self.assertEqual(result, [{"code": "sz000100", "name": "TCL科技"}])

    def test_search_stocks_delegates_to_fetch(self):
        # 完整路径: fetch(canned hint) → _parse_search_response; q= 接线正确
        from urllib.parse import quote as _quote
        with mock.patch.object(sf, "fetch", return_value=self.HAPPY_HINT) as mfetch:
            result = sf.search_stocks("茅台")
        self.assertEqual(result, self.EXPECTED_HAPPY)
        self.assertTrue(mfetch.called, "search_stocks 必须调用 fetch")
        args, _ = mfetch.call_args
        self.assertIn("q=" + _quote("茅台"), args[0], "搜索词必须经 q= 传入 URL")
        self.assertIn("smartbox.gtimg.cn", args[0])
        self.assertIn("t=all", args[0])

    def test_search_stocks_whitespace_shortcircuits(self):
        # 空/纯空白查询 → 短路返回 [], 不调用 fetch
        with mock.patch.object(sf, "fetch") as mfetch:
            self.assertEqual(sf.search_stocks("   "), [])
            self.assertEqual(sf.search_stocks(""), [])
        mfetch.assert_not_called()

    def test_search_stocks_swallows_network_error(self):
        # fetch 抛任意异常 → search_stocks 吞掉返回 [], 不向外逃逸
        with mock.patch.object(sf, "fetch", side_effect=Exception("boom")):
            result = sf.search_stocks("anything")
        self.assertEqual(result, [])


class TestGraynessReentrancyGuard(unittest.TestCase):
    """灰度滑块 RecursionError 回归: 验证 _run_with_guard 重入守卫对 macOS Tk 重触发免疫。

    无头环境: 全程用桩对象, 不实例化 Tk 根、不调用 run_hud / mainloop。
    覆盖机理: 原生 bug 在于 _reapply_style 末尾的 root.update_idletasks() 在 macOS Tk
    上会从当前活动 Scale 的 -command 回调内部重入事件循环、再次触发同一滑块 command,
    无限嵌套 -> RecursionError。本组测试直接验证生产代码使用的 _run_with_guard 守卫,
    并用桩复现「update_idletasks 重 fire 当前 Scale」场景, 断言:
      (1) 实质工作仅执行一次 (applied 计数 == 1);
      (2) 重入命令确实发生了 (scale_cmds >= 2), 证明复现了触发机制;
      (3) 无 RecursionError, 且守卫标志在拖动后复位为 False。
    附对照测试: 去掉守卫时同场景必抛 RecursionError, 证明确实复现了原始 bug。
    """

    def test_guard_runs_work_once_and_resets(self):
        # 基础: 守卫不拦截首次调用, 返回工作结果, 完成后复位标志。
        guard = {"v": False}
        calls = []

        def work():
            calls.append(1)
            return "ok"

        self.assertEqual(sf._run_with_guard(guard, work), "ok")
        self.assertEqual(calls, [1])
        self.assertFalse(guard["v"], "守卫标志应在完成后复位")

    def test_guard_reentrancy_skips_inner_and_no_recursion(self):
        # 复现: 在 work 内部经 update_idletasks 重入, 再次进入同一守卫。
        # 守卫失效将无限递归; 此处应只执行一次外层 work, 内层被跳过返回哨兵。
        guard = {"v": False}
        calls = []

        def work():
            calls.append(1)
            inner = sf._run_with_guard(guard, work)   # 模拟 macOS 重触发同一回调
            calls.append(("inner", inner))

        sf._run_with_guard(guard, work)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], 1)
        # 内层被守卫跳过, 返回哨兵 _GUARD_SKIPPED
        self.assertEqual(calls[1], ("inner", sf._GUARD_SKIPPED))
        self.assertFalse(guard["v"], "守卫标志应在嵌套结束后复位")

    def test_grayness_slider_fire_no_recursion_with_guard(self):
        # 端到端桩: 镜像生产 _apply_grayness(使用 _run_with_guard + _style_busy) +
        # _reapply_style 桩(末尾调用 root.update_idletasks) + FakeRoot 重 fire。
        guard = {"v": False}
        applied = []

        def reapply_style_stub():
            root.update_idletasks()                   # 复现 _reapply_style 末尾的调用

        def apply_grayness(val):
            def work():
                v = max(0.0, min(1.0, val))
                applied.append(v)
                reapply_style_stub()
            return sf._run_with_guard(guard, work)    # 与生产一致: _style_busy 守卫

        scale_cmds = []

        class FakeScale:
            def __init__(self, command):
                self.command = command

            def fire(self, value):
                scale_cmds.append(value)
                # 模拟 make_slider 的 command=lambda v, cb=on_change: cb(float(v))
                self.command(float(value))

        class FakeRoot:
            def update_idletasks(self):
                # 复现 macOS 重入: 在 update_idletasks 内部同步再次 fire 当前活动 Scale
                # 的 command(当前活动 Scale 即灰度滑块), 形成重入。
                scale.fire(0.5)

        scale = FakeScale(lambda v: apply_grayness(v))
        root = FakeRoot()
        # 用户拖一下灰度滑块 -> 触发一次 command
        scale.fire(0.5)
        self.assertEqual(applied, [0.5], "实质灰度工作应只执行一次(重入被守卫截断)")
        self.assertTrue(len(scale_cmds) >= 2, "应存在重入 fire(<=1 说明未复现重入机制)")
        self.assertFalse(guard["v"], "守卫标志应在拖动结束后复位")

    def test_alpha_slider_also_guarded(self):
        # 透明度滑块同样经 _run_with_guard + 同一 _style_busy 守卫, 重入须被截断。
        guard = {"v": False}
        applied = []

        def reapply_style_stub():
            root.update_idletasks()                   # 复现 _reapply_style 末尾的调用

        def apply_alpha(val):
            def work():
                v = max(0.30, min(1.0, val))
                applied.append(v)
                reapply_style_stub()                  # 触发重入(与生产一致)
            return sf._run_with_guard(guard, work)

        reentered = {"n": 0}

        class FakeScale:
            def fire(self, value):
                reentered["n"] += 1
                apply_alpha(float(value))

        class FakeRoot:
            def update_idletasks(self):
                scale.fire(0.8)                       # 重入触发当前活动 Scale

        scale = FakeScale()
        root = FakeRoot()
        scale.fire(0.8)
        self.assertEqual(applied, [0.8], "透明度实质工作应只执行一次")
        self.assertTrue(reentered["n"] >= 2, "应存在重入 fire")
        self.assertFalse(guard["v"])

    def test_no_guard_would_recurse(self):
        # 对照(证明测试确实复现了原始 bug 的触发机制): 去掉守卫后同场景必抛 RecursionError。
        applied = []

        def reapply_style_stub():
            root.update_idletasks()

        def apply_grayness_noguard(val):
            v = max(0.0, min(1.0, val))
            applied.append(v)
            reapply_style_stub()                      # 此处无守卫 -> 无限重入

        class FakeScale:
            def __init__(self, command):
                self.command = command

            def fire(self, value):
                self.command(float(value))

        class FakeRoot:
            def update_idletasks(self):
                scale.fire(0.5)                       # 重入触发

        scale = FakeScale(lambda v: apply_grayness_noguard(v))
        root = FakeRoot()
        with self.assertRaises(RecursionError):
            scale.fire(0.5)


# ----------------------------------------------------------------------------
# 20. macOS 窗口放大禁用锁: run_hud 必须包含 "-zoom" 平台属性处理(绿色放大按钮)
#     防止未来删掉导致放大破坏宽度锁定(无头 AST 校验, 不实例化 Tk)。
# ----------------------------------------------------------------------------
class TestMacWindowZoomDisabled(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._src = Path(sf.__file__).read_text(encoding="utf-8")
        cls._tree = ast.parse(cls._src)

    def test_run_hud_has_zoom_disabled(self):
        """锁定: run_hud 内必须存在字符串字面量 "-zoom"(macOS 禁用绿色放大按钮)。"""
        hud = next(
            n for n in self._tree.body
            if isinstance(n, ast.FunctionDef) and n.name == "run_hud"
        )
        literals = {
            n.value for n in ast.walk(hud)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
        }
        self.assertIn("-zoom", literals,
                      "回归: run_hud 缺失 macOS -zoom 禁用(绿色放大按钮会破坏宽度锁定)")

    def test_zoom_disabled_is_platform_guarded(self):
        """锁定: -zoom 设置必须包在 darwin 平台分支内(Windows/Linux 无需此属性)。"""
        src = self._src
        # 粗粒度契约: 属性调用行 root.attributes("-zoom" 出现, 且其前 25 行内有
        # sys.platform == "darwin" 守卫(容忍行序)。不匹配注释行(注释里的 "-zoom" 在守卫之前)。
        zoom_line = next(
            (i + 1 for i, ln in enumerate(src.splitlines())
             if 'attributes("-zoom"' in ln or "attributes('-zoom'" in ln),
            None,
        )
        self.assertIsNotNone(zoom_line, "源码中找不到 -zoom 属性设置行")
        window = src.splitlines()[max(0, zoom_line - 25):zoom_line + 2]
        self.assertTrue(
            any("sys.platform == \"darwin\"" in ln or "sys.platform == 'darwin'" in ln for ln in window),
            "回归: -zoom 设置未包在 darwin 平台守卫内",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
