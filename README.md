# 股票浮窗 stock_float.py

浮动隐蔽的股票行情 HUD：每秒刷新关注股票，叠加多空信号打分，信号/阈值/支撑压力异动时弹系统通知并落盘 CSV。纯 Python 标准库 + tkinter，**零第三方依赖**，单文件双击即可运行（mac 用 `启动浮窗.command`、Windows 用 `启动浮窗.bat`）。

## 快速开始

```bash
# mac / 通用
python3 stock_float.py

# 回看历史信号(最近30条)
python3 stock_float.py --review

# 当日信号聚合统计
python3 stock_float.py --stats

# 按代码 / 日期过滤
python3 stock_float.py --review --code hk01810 --date 2026-07-15
python3 stock_float.py --stats  --code hk01810

# 不写 CSV
python3 stock_float.py --no-log
```

配置两个文件（均放脚本同级）：

- `config.toml`：浮窗外观、通知、落盘存储，以及**运行参数（行情源 / 刷新频率）**等设置（参考 `config.toml.example`）。
- `stocks.toml`：股票列表 `[[stocks]]`，以及**监控策略（指标打分 / 盘中灵敏）+ 变动阈值 / 冷却**（`[settings]`），与个股 `support`/`resistance`（参考 `stocks.toml.example`）。

> 约定（配置归属理顺）：`stocks.toml` 的 `[settings]` 放**监控策略（指标打分 / 盘中灵敏）+ 变动阈值 / 冷却**；`config.toml` 的 `[settings]` 放外观 / 通知 / 落盘存储 + **运行参数（行情源 / 刷新频率）**。同一键两处都有时，`stocks.toml` 优先。在 `config.toml` 里写监控策略键（如 `indicators`）仍可用（向后兼容），只是官方文档归属已迁到 `stocks.toml`。

## 8 项增量功能（本次新增）

| # | 功能 | 默认 | 说明 |
|---|------|------|------|
| 1 | 架构抽象层 | 内置 | 内部抽取 `DataSource`/`Notifier`/`Indicator`/`SignalEngine`/`Hud` 段；`get_realtime` 委托 `DATA_SOURCE.fetch` 返回 8 元组 `(price, prev_close, open_px, ts, high, low, volume, delayed)`。**默认行为逐字一致**。 |
| 2 | 交互三件套（P0） | 默认开 | ① 跨平台通知：mac→AppleScript；linux→`notify-send`；Windows→`winsound.Beep` 蜂鸣 + 浮窗闪烁（`flash_fn` 经 `root.after` 回主线程）。② 浮窗右上角 `⏸` 暂停/继续 + 频率控件（1→3→5→1 秒）。③ 每行行情带 ~38px sparkline（用 `rec.kline`，**不在刷新内联网**）。 |
| 3 | 数据/信号引擎增强（P1） | 见下 | 备用免费源（新浪/东财）兜底；主源异常仅告警一次（非静默跳过）。新增 `kdj`/`bollinger`/`get_volume_hist` 纯函数。`monitor` 按 `settings.indicators` 动态累加打分（注册表 `SCORERS`）。默认 `["MA","RSI","MACD"]` 与改造前权重逐字一致；开启 `KDJ`/`BOLL`/`VOLUME` 时参与打分。支撑/压力穿越检测触发通知+CSV。 |
| 4 | 暗色模式与字号（P1） | 默认 light | `float_theme`(light/dark/auto) / `float_font` / `float_font_size`。`auto` 时 mac 读 `defaults`、Windows 读注册表跟随系统外观。 |
| 5 | 落盘与查询增强（P1） | 默认开 | `append_signal` 按 `csv_dedup_sec`(默认60s) 去重（同 code 且 signal/price 未变则跳过）。新增 `--stats` 当日聚合（各档位次数、最高/最低 net、信号切换次数）。`--review` 支持 `--code`/`--date` 过滤。 |

## 配置键一览（均含默认值，未配置=既有行为）

配置分两个文件（均放脚本同级），`stocks.toml` 的 `[settings]` 放监控策略 + 变动阈值 / 冷却：

- `config.toml` 的 `[settings]`：外观 + 通知 + 落盘存储 + 运行参数（行情源 / 刷新频率）。
- `stocks.toml` 的 `[settings]`：监控策略（指标打分 / 盘中灵敏）+ 变动阈值 / 冷却。下方 `[[stocks]]` 放股票列表。

> 约定：两份文件都会被读取并合并，同一键两处都有时，**`stocks.toml` 优先**（`load_settings()` 先 `update` config.toml 再 `update` stocks.toml）。在 `config.toml` 里写监控策略键（如 `indicators`）仍可用，只是官方文档归属已迁到 `stocks.toml`。

### `config.toml`（外观 / 通知 / 落盘 / 运行参数）

| 键 | 默认 | 说明 |
|----|------|------|
| `csv_mode` | `"single"` | `daily` 按天滚动到 `signals_YYYY-MM-DD.csv` |
| `csv_dedup_sec` | `60` | CSV 去重窗口(秒)，0=关闭 |
| `notify` | `false` | 跨平台系统通知开关 |
| `notify_sound` | `false` | 通知声音（Windows 用蜂鸣兜底） |
| `sources` | `["tencent","sina","eastmoney"]` | 行情源兜底顺序；设 `["tencent"]` 关备用源 |
| `refresh_sec` | `1` | 刷新周期(秒)，控件循环 1/3/5 |
| `float_theme` | `"light"` | light/dark/auto |
| `float_font` | `"Menlo"` | 字体族 |
| `float_font_size` | `7` | 字号 |
| `float_up_color` / `float_down_color` | 内置默认 | 涨/跌色(#RRGGBB) |
| `sig_buy` / `sig_long` / `sig_hold` / `sig_short` / `sig_sell` | 内置默认 | 五档信号色 |
| `float_alpha` | `0.94` | 浮窗透明度(0~1) |
| `grayness` | `0.0` | 界面强调色(涨/跌/信号点)去饱和(灰色)程度 0.0~1.0；0=原色，1=纯灰阶 |

### `stocks.toml`（监控策略 / 阈值·冷却；下方另含 `[[stocks]]`）

| 键 | 默认 | 说明 |
|----|------|------|
| `indicators` | `["MA","RSI","MACD"]` | 参与打分的指标；可加 `KDJ`/`BOLL`/`VOLUME` |
| `live_indicators` | `true` | 盘中把实时价并入指标序列 |
| `chg_alert` / `swing_alert` | `0` | 变动/波动提示阈值(%) |
| `alert_cooldown` | `15` | 阈值/支撑压力穿越冷却(分钟) |

### 个股支撑/压力

在 `stocks.toml` 的 `[[stocks]]` 里加（可多个价位）：

```toml
[[stocks]]
code = "hk01810"
name = "小米集团-W"
support = [18.0, 17.5]        # 价格下穿任一时弹通知+写CSV
resistance = [20.5, 21.0]     # 价格上穿任一时弹通知+写CSV
```

穿越判定用"前价 vs 现价"对比价位，并复用 `alert_cooldown` 冷却，避免反复横跳刷屏。需 `notify=true` 才弹通知；CSV 落盘独立生效。

## 多市场

- **A 股**（`sh`/`sz`）：实时行情。
- **港股**（`hk`）/ **美股**（`us`）：免费源约 15 分钟延时，浮窗标"延时"并显示接口数据时间。

## 已知限制 / 待实测

- **备用源字段映射需实测**：新浪（`hq.sinajs.cn`，需 `Referer`）与东财（`push2.eastmoney.com`，`secid` 编码 `hk→116.x`/`us→105.x`/`sh→1.x`/`sz→0.x`，价格÷100）的字段映射按常见口径实现，未在你网络环境逐源联调。任一备用源不可达时主源(腾讯)仍正常工作；如不可用可在 `sources` 中删去对应项。
- **腾讯实时 `volume` 字段**：先按 `p[6]`（手数）实测；若接口返回结构不符会回退 `volume=None` 且不报错（不影响其它功能）。
- **KDJ 简化口径**：用收盘价版 RSV（非严格 high/low 版），用户已拍板。
- **VOLUME 历史量**：默认关（`indicators` 不含 "VOLUME"）；启用时额外拉一次日K量（多一次请求/股），并显示量+量MA5 参与打分。
- **Windows 闪烁**：无边框窗口采用 alpha 取反闪烁约 1s 兜底（不做 ctypes Toast）。
- **GUI 交互需真机验证**：跨平台通知、暂停/频率、暗色、闪烁均需在对应系统实机验证（本环境无法启动 GUI）。
- **零第三方依赖**：仅标准库（`tkinter`/`subprocess`/`winsound`/`winreg`/`zoneinfo`/`tomllib`(3.11+) 等）。

## 建议测试点

1. **纯函数指标单测**：`sma`/`rsi`/`macd` 与改造前一致；`kdj`/`bollinger` 确定性 & 边界（数据不足返回 None）。
2. **默认行为回归**：不配置 `indicators` 时 `monitor` 的 `text`/打分/信号档位与改造前逐字一致；实时/日K/五档信号(滞回)/省流通知/CSV/`--review`/多市场原样工作。
3. **跨平台通知分支**：mac/linux/win 各自 `Notifier.notify` 分支；Windows 蜂鸣+闪烁经 `root.after` 线程安全。
4. **CSV 去重**：`csv_dedup_sec` 窗口内同 code+signal+price 跳过；窗口外或变动则写入。
5. **`--stats` / `--review` 过滤**：`--code`/`--date` 正确筛选；`csv_mode="daily"` 聚合当日 `signals_YYYY-MM-DD.csv`。
6. **多源兜底**：主源失败顺序尝试备用；主源异常仅告警一次（stderr）。
