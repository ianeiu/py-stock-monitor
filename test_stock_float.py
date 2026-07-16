#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""stock_float.py 独立测试套件 (QA 工程师严过关)。

约束:
- 仅用 Python 标准库 (unittest), 零第三方依赖。
- 本环境无 DISPLAY, 严禁实例化 Tk / 调用 run_hud/mainloop。
  GUI 相关项(sparkline 绘制 / 暂停按钮 / 频率控件 / Windows 闪烁 / 暗色渲染)
  仅做逻辑层 / 纯函数验证, 并明确标注「需真机验证」。
- 网络与 GUI 通过 unittest.mock 隔离。

运行:
  python3 -m unittest test_stock_float.py -v
"""

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
      - sparkline 绘制 (_draw_sparkline 在 Canvas 上画折线)
      - 暂停/继续按钮 (_toggle_pause 改 state["paused"])
      - 频率控件 (_cycle_freq 循环 1->3->5->10->1)
      - Windows 闪烁 (_flash_window 经 root.after 回主线程)
      - 暗色渲染 (build_style 已逻辑验证, 像素渲染需真机)
    """

    def test_gui_symbols_exist(self):
        # 模块级符号存在且可调用 (具体行为需真机)
        # 注: _toggle_pause / _cycle_freq / _flash_window 是 run_hud 内部嵌套函数,
        #     仅能在本机建 Tk 后提取, 此处不实例化 Tk, 故只校验模块级导出符号。
        for name in ("run_hud", "_draw_sparkline"):
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
        # filter_off -> 永远可见
        self.assertTrue(sf.is_row_visible(False, None))
        self.assertTrue(sf.is_row_visible(False, now - 1000))
        # filter_on + None -> 不可见
        self.assertFalse(sf.is_row_visible(True, None))
        # filter_on + 窗口内 -> 可见
        self.assertTrue(sf.is_row_visible(True, now))
        # filter_on + 超窗口 -> 不可见
        self.assertFalse(sf.is_row_visible(True, now - 1000))


# ----------------------------------------------------------------------------
# 16. P2 补充: 迷你 sparkline 显示/隐藏界面开关(功能③)
#     GUI 按钮/显隐只做代码审查, 标注「需真机验证」, 不实例化 Tk。
#     配置键 show_sparkline 走 settings.get(..., True), 默认开。
# ----------------------------------------------------------------------------
class TestSparklineToggle(unittest.TestCase):
    def test_show_sparkline_default_true(self):
        # 未配置该键时, settings.get("show_sparkline", True) 应默认 True(保持现有行为)
        fixture = {"settings": {"refresh_sec": 3}, "stocks": [{"code": "hk01810"}]}
        with mock.patch.object(sf, "_load_first", return_value=("fake", fixture)):
            s = sf.load_settings()
        self.assertTrue(s.get("show_sparkline", True))

    def test_show_sparkline_read_from_config(self):
        # config.toml 含 show_sparkline = false -> 经 load_settings 后为 False
        fixture = {"settings": {"show_sparkline": False}, "stocks": [{"code": "hk01810"}]}
        with mock.patch.object(sf, "_load_first", return_value=("fake", fixture)):
            s = sf.load_settings()
        self.assertFalse(s.get("show_sparkline"))


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
# 17. 修复回归: sparkline 显示/隐藏状态机(功能③) —— 杜绝「第二次隐藏失效」
#     抽成纯函数 apply_sparkline_state, 用桩对象无头验证「关→开→关→开」幂等。
# ----------------------------------------------------------------------------
class _StubSpark:
    """模拟 tk.Canvas 的最小桩: 记录 config/delete/create_line 调用, 维护 _width。"""
    def __init__(self, width=None):
        self._width = sf.SPARK_W if width is None else width
        self.calls = []

    def config(self, **kw):
        if "width" in kw:
            self._width = kw["width"]
        self.calls.append(("config", dict(kw)))

    def delete(self, *args):
        self.calls.append(("delete", args))

    def create_line(self, *args, **kw):
        self.calls.append(("create_line", args))

    def winfo_width(self):
        return self._width

    def winfo_height(self):
        return 14


class TestApplySparklineState(unittest.TestCase):
    """守「关→开→关→开」每个阶段 spark 折叠/展开状态, 且可重入/idempotent。"""

    def _style(self):
        return {"up": "#ff6b5e", "down": "#4cc38a"}

    def _snap(self):
        return {"kline": [1.0, 2.0, 3.0, 2.5], "price": 2.5, "prev_close": 1.0}

    def test_toggle_off_on_off_on(self):
        # 核心回归: 第二次(及任意次)隐藏都必须生效(_width==0)
        spark = _StubSpark()
        # 初始展开(构建时 width=SPARK_W)
        sf.apply_sparkline_state(spark, False, None, self._style())   # 隐藏
        self.assertEqual(spark._width, 0)
        sf.apply_sparkline_state(spark, True, self._snap(), self._style())  # 显示
        self.assertEqual(spark._width, sf.SPARK_W)
        sf.apply_sparkline_state(spark, False, None, self._style())   # 再次隐藏(修复点)
        self.assertEqual(spark._width, 0)
        sf.apply_sparkline_state(spark, True, self._snap(), self._style())   # 再次显示
        self.assertEqual(spark._width, sf.SPARK_W)

    def test_idempotent(self):
        # 连续两次同状态不漂移
        spark = _StubSpark()
        sf.apply_sparkline_state(spark, False, None, self._style())
        sf.apply_sparkline_state(spark, False, None, self._style())
        self.assertEqual(spark._width, 0)
        sf.apply_sparkline_state(spark, True, self._snap(), self._style())
        sf.apply_sparkline_state(spark, True, self._snap(), self._style())
        self.assertEqual(spark._width, sf.SPARK_W)

    def test_off_clears_canvas(self):
        # 隐藏时必须清空已有折线(delete), 避免残留上一张图
        spark = _StubSpark()
        sf.apply_sparkline_state(spark, False, None, self._style())
        self.assertTrue(any(c[0] == "delete" for c in spark.calls),
                        "隐藏时必须清空画布(delete)")

    def test_on_with_kline_redraws(self):
        # 显示且有 kline 时立即重绘(触发 create_line)
        spark = _StubSpark()
        sf.apply_sparkline_state(spark, True, self._snap(), self._style())
        self.assertEqual(spark._width, sf.SPARK_W)
        self.assertTrue(any(c[0] == "create_line" for c in spark.calls),
                        "显示且有 kline 时应重绘折线")

    def test_on_without_kline_no_draw(self):
        # 显示但无 kline 时不绘制, 仅恢复宽度
        spark = _StubSpark()
        sf.apply_sparkline_state(spark, True, {"kline": None}, self._style())
        self.assertEqual(spark._width, sf.SPARK_W)
        self.assertFalse(any(c[0] == "create_line" for c in spark.calls),
                        "无 kline 时不应绘制折线")


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

    def test_visible_idempotent_when_mapped(self):
        # visible=True 且已映射 -> 不再 pack(幂等)
        sf_row = _StubSigRow(mapped=True)
        sf.apply_sig_visibility(sf_row, True, dict(fill="x"))
        self.assertEqual(len(self._pack_calls(sf_row)), 0, "已映射时不应再 pack")
        self.assertEqual(len(self._forget_calls(sf_row)), 0)
        self.assertTrue(sf_row.winfo_ismapped())

    def test_hidden_forgets_when_mapped(self):
        # visible=False 且已映射 -> 调 pack_forget, 不调 pack
        sf_row = _StubSigRow(mapped=True)
        sf.apply_sig_visibility(sf_row, False, dict(fill="x"))
        self.assertEqual(len(self._forget_calls(sf_row)), 1, "已映射+隐藏必须 pack_forget")
        self.assertEqual(len(self._pack_calls(sf_row)), 0, "隐藏不应 pack")
        self.assertFalse(sf_row.winfo_ismapped(), "pack_forget 后应未映射")

    def test_hidden_idempotent_when_unmapped(self):
        # visible=False 且未映射 -> 不再 pack_forget(幂等)
        sf_row = _StubSigRow(mapped=False)
        sf.apply_sig_visibility(sf_row, False, dict(fill="x"))
        self.assertEqual(len(self._forget_calls(sf_row)), 0, "未映射时不应再 pack_forget")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
