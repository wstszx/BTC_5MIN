# Polymarket BTC 5m Trading Bot

用于 Polymarket 比特币 5 分钟 `Up / Down` 滚动市场的纯 Python 交易程序。

当前版本已经支持：

- 历史 BTC 5 分钟市场数据导出到 CSV
- 基于同一套策略和资金管理逻辑的回测
- 纸面交易运行
- 运行状态持久化
- 基于 `py-clob-client` 的实盘下单（默认关闭，需显式开关与凭据）

完整 PowerShell 操作手册：

- [docs/operations_runbook.md](./docs/operations_runbook.md)
- [docs/dashboard_runbook.md](./docs/dashboard_runbook.md)
- [docs/daily_ops_checklist.md](./docs/daily_ops_checklist.md)

## 1. 安装

进入项目目录后安装依赖：

```bash
python -m pip install -r requirements.txt
```

## 2. 核心配置

主要配置都在 [config.py](./config.py)。

当前默认值：

- `series_id = 10684`
- `series_slug = "btc-up-or-down-5m"`
- `trade_mode = "paper"`
- `strategy_id = 2`
- `target_profit = 1.0`
- `bet_sizing_mode = "FIXED_BASE_COST"`
- `base_order_cost = 1.0`
- `max_consecutive_losses = 6`
- `max_stake = 15.0`
- `max_price_threshold = 0.65`
- `signal_momentum_threshold = 0.015`
- `signal_fallback_strategy_id = 2`
- `signal_weak_signal_mode = "SKIP"`
- `signal_history_fidelity_seconds = 5`
- `signal_anchor_max_offset_seconds = 20`
- `signal_dynamic_threshold_k = 1.5`
- `signal_dynamic_threshold_min_points = 8`
- `signal_lock_before_entry_seconds = 20`
- `max_stake_skip_alert_threshold = 5`
- `daily_loss_cap = 50.0`
- `poll_interval_seconds = 5`
- `entry_timing = "OPEN"`
- `open_delay_seconds = 5`
- `preclose_seconds = 30`
- `history_entry_fidelity_seconds = 5`
- `history_entry_max_offset_seconds = 120`

默认参数已切换为“稳健优先”配置：`strategy_id=2` + `FIXED_BASE_COST` + `base_order_cost=1.0` + `max_consecutive_losses=6` + `max_stake=15.0`。  
目标是优先控制回撤和资金压力，而不是追求单阶段最高收益。

实盘相关（默认都偏安全）：

- `live_trading_enabled` 默认从环境变量 `LIVE_TRADING_ENABLED` 读取（默认 `False`）
- `live_private_key` 从 `POLYMARKET_PRIVATE_KEY`（或 `PRIVATE_KEY`）读取
- `live_chain_id` 默认 `137`
- `live_signature_type` 默认 `0`（EOA）
- `live_funder` 可选，用于代理/智能钱包
- `live_order_type` 默认 `FOK`

你最常会修改的是这些参数：

- `strategy_id`
  说明：`1/2/3/4/5` 分别对应：
  - 1: `UP, DOWN, UP, DOWN`
  - 2: `UP, UP, DOWN, DOWN`
  - 3: `UP, UP, UP, DOWN, DOWN, DOWN`
  - 4: `UP, UP, UP, UP, DOWN, DOWN, DOWN, DOWN`
  - 5: `5分钟价格动量信号`（用本轮 `UP` 价格相对开盘的变化决定方向，变化不够大时回退到 `signal_fallback_strategy_id`）
  - 备注：策略 5 当前为 V2：会优先尝试历史 tick 对齐开盘锚点、接近入场时间锁定方向、并支持弱信号 `SKIP/FALLBACK` 两种模式；默认是 `SKIP`
  - 重要限制：历史 CSV 的 `open/preclose` 快照在很多样本里仍可能高度重合，离线回测会自动给出退化告警，避免把“伪动量”结果当成真实优势
- `target_profit`
  说明：每次赢后希望净赚多少
- `bet_sizing_mode`
  说明：下注金额模式，`FIXED_BASE_COST` 为固定起始金额（赢后回到固定金额），`TARGET_PROFIT` 为按目标盈利反推金额
- `base_order_cost`
  说明：`FIXED_BASE_COST` 模式下的固定起始下注金额（默认 `1.0`）
- `max_consecutive_losses`
  说明：最大连续亏损次数，达到后触发止损重置
- `max_stake`
  说明：单笔实际最大花费 USDC 上限
- `max_price_threshold`
  说明：如果目标方向价格高于这个阈值则跳过
- `signal_momentum_threshold`
  说明：`strategy_id=5` 时的动量阈值（`current_up_price - open_up_price` 的绝对判定门槛）
- `signal_fallback_strategy_id`
  说明：`strategy_id=5` 在信号不足时回退到的基础策略（建议 `1~4`）
- `signal_weak_signal_mode`
  说明：`strategy_id=5` 的弱信号处理模式，`SKIP`（默认，不下单）或 `FALLBACK`（回退到基础策略）
- `signal_history_fidelity_seconds`
  说明：策略 5 拉取 token 历史序列时使用的采样粒度（秒）
- `signal_anchor_max_offset_seconds`
  说明：对齐开盘锚点时允许的最大时间偏移（秒）
- `signal_dynamic_threshold_k`
  说明：动态阈值系数，实际阈值为 `max(signal_momentum_threshold, k * sigma)`
- `signal_dynamic_threshold_min_points`
  说明：动态阈值至少需要的历史点数，小于这个数量时退回基础阈值
- `signal_lock_before_entry_seconds`
  说明：距离入场时间多少秒内锁定策略 5 的方向，避免临近入场来回跳边
- `max_stake_skip_alert_threshold`
  说明：连续多少次触发 `order_cost_above_max_stake` 时发出告警（只告警，不自动重置）
- `daily_loss_cap`
  说明：每日累计亏损达到该值后停止交易
- `entry_timing`
  说明：`OPEN` 表示开盘后入场，`PRE_CLOSE` 表示临近收盘入场
- `open_delay_seconds`
  说明：`OPEN` 模式下开盘后延迟多少秒再尝试
- `preclose_seconds`
  说明：`PRE_CLOSE` 模式下收盘前多少秒尝试入场
- `history_entry_fidelity_seconds`
  说明：导出历史 CSV 时，开盘/收盘前快照价格所用的采样粒度（秒）
- `history_entry_max_offset_seconds`
  说明：导出历史 CSV 时，快照匹配允许的最大时间偏移（秒）；超出后会回退为“最近点”以减少空值

也支持通过环境变量临时覆盖关键参数（适合不改代码做多组纸测）：

- `STRATEGY_ID`
- `TARGET_PROFIT`
- `MAX_CONSECUTIVE_LOSSES`
- `MAX_STAKE`
- `MAX_PRICE_THRESHOLD`

## 3. 命令总览

查看全部命令：

```bash
python main.py --help
```

当前支持 8 个主命令：

- `fetch-history`
- `backtest`
- `analyze-streak`
- `research-strategy`
- `paper-trade`
- `paper-report`
- `live-trade`
- `dashboard`

## 4. 拉取历史数据

导出最近若干条 BTC 5 分钟市场历史：

```bash
python main.py fetch-history --limit 100
```

指定导出文件名：

```bash
python main.py fetch-history --limit 100 --output data/my_history.csv
```

运行后会在 `data/` 目录下生成 CSV。

CSV 中会包含：

- 市场 id / slug / 标题
- 开始时间 / 结束时间
- `price_to_beat`
- `final_price`
- 最终结果 `UP / DOWN`
- `Up / Down` token id
- `entry_price_open_*`
- `entry_price_preclose_*`

## 5. 回测

先用内置样例验证程序是否正常：

```bash
python main.py backtest --csv tests/fixtures/sample_history.csv
```

如果你已经导出了自己的历史 CSV，可以这样回测：

```bash
python main.py backtest --csv data/你的历史文件.csv
```

回测输出会显示：

- 总盈亏
- 交易次数
- 跳过次数
- 止损重置次数
- 最大连续亏损次数
- 平均每局收益
- 最大回撤

## 5.1 连亏风险评估（倍投轮数建议）

根据历史结果估算“连亏 >= K”的发生频率，并结合 `max_stake` 自动给出建议重置轮数：

```bash
python main.py analyze-streak --csv data/你的历史文件.csv
```

常用参数：

- `--strategy-id`：覆盖配置里的策略编号
- `--target-occurrence`：你能接受的“每轮发生概率”上限（例如 `0.01` 表示 1%）
- `--min-round` / `--max-round`：评估 K 的区间

## 5.2 自动策略研究（重复验证）

按多个策略 / 重置轮数 / 目标盈利做批量回放，并按稳健性打分给出 Top 结果。该命令会考虑每轮实际价格不同的情况，并给出：

- 总盈亏
- 交易次数 / 胜率
- 最大回撤
- 历史最小所需本金
- 建议本金（乘以安全系数）
- 分段验证表现（避免只看单一区间）

示例：

```bash
python main.py research-strategy --csv data/你的历史文件.csv --strategy-ids 1,2,3,4 --reset-rounds 2,3,4,5 --target-profits 1.0 --segments 5 --top-n 5
```

可选导出完整候选结果：

```bash
python main.py research-strategy --csv data/你的历史文件.csv --output data/research_report.csv
```

## 6. 纸面交易

只评估一轮并退出：

```bash
python main.py paper-trade --dry-run-once
```

这个模式适合快速检查当前程序会不会下单，会输出：

- 当前目标市场 `slug`
- 当前方向
- 当前价格
- 是否满足下单条件
- 如果不下单，跳过原因是什么
- 当前设定下的入场时间

持续运行纸面交易：

```bash
python main.py paper-trade
```

这个模式会持续轮询官方接口，执行：

1. 发现当前或下一期 BTC 5 分钟市场
2. 按策略计算下一次应买 `UP` 还是 `DOWN`
3. 按价格和累计亏损计算仓位
4. 执行风控检查
5. 记录纸面成交
6. 等待结算并更新状态

## 6.1 `Total PnL` 与手动启停

- `Total PnL` 含义：从当前会话起点开始累计的总盈亏（单位 USDC）。
- 正数表示累计盈利，负数表示累计亏损。

前台手动启动（窗口占用，适合临时观察）：

```bash
python main.py paper-trade
```

前台手动停止：

```text
Ctrl + C
```

后台手动启动（适合每天持续跑）：

```powershell
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
Start-Process -FilePath python -ArgumentList @('main.py','paper-trade') `
  -WorkingDirectory 'D:\python\BTC_5MIN' `
  -RedirectStandardOutput "logs/paper_live_$ts.out" `
  -RedirectStandardError "logs/paper_live_$ts.err"
```

后台查看进程：

```powershell
Get-Process | Where-Object { $_.ProcessName -like 'python*' }
```

后台手动停止（按 PID）：

```powershell
Stop-Process -Id <PID>
```

## 6.2 每日纸测报告（命中 / 盈亏 / 回撤 / 信号质量）

按天汇总 `logs/paper_trades.csv`，输出：

- 命中率、胜负次数
- 当日总盈亏、单笔均值、最大回撤
- 信号强度（`abs(delta)`）、强信号占比、信号锁定占比
- 跳过原因统计

```bash
python main.py paper-report --csv logs/paper_trades.csv --tz-offset +08:00
```

可选时间过滤：

```bash
python main.py paper-report --start-date 2026-03-31 --end-date 2026-04-02
```

## 6.3 本地可视化 Dashboard（参数调节 + 实时行情 + 纸测结果）

启动 Dashboard：

```bash
python main.py dashboard
```

默认访问地址：

```text
http://127.0.0.1:8787
```

可选参数：

- `--host`：监听地址（默认 `127.0.0.1`）
- `--port`：端口（默认 `8787`）
- `--env-file`：Dashboard 参数文件（默认 `.env.dashboard`）

示例：

```bash
python main.py dashboard --host 127.0.0.1 --port 8787 --env-file .env.dashboard
```

页面能力：

- 在线编辑策略参数（会写入 `.env.dashboard`）
- 查看当前轮次报价、信号方向与风控计划
- 查看 `ws_runtime` 与 `ws_stale_guard_triggered`
- 查看纸测日报汇总与最近成交记录

## 7. 状态文件与日志

程序运行过程中会使用这些目录：

- `data/`
  说明：历史导出的 CSV
- `logs/session_state.json`
  说明：纸面交易会话状态，包含轮次、累计盈亏、恢复亏损、连亏次数等
- `logs/*.csv`
  说明：纸面交易日志

如果你想重置纸面交易状态，从头开始统计，可以删除：

```bash
logs/session_state.json
```

如果还想连同历史纸面交易记录一起清掉，也可以删除：

```bash
logs/*.csv
```

然后重新执行：

```bash
python main.py paper-trade
```

## 8. 实盘命令使用方式

先做实盘 dry-run（不下单）：

```bash
python main.py live-trade --dry-run-once
```

如果 dry-run 输出正常，再启用真实下单：

```bash
# PowerShell 示例
$env:LIVE_TRADING_ENABLED='true'
$env:POLYMARKET_PRIVATE_KEY='你的私钥'
python main.py live-trade --enable-live-trading
```

实盘命令内置了两层闸门：

- 命令行必须显式传 `--enable-live-trading`
- 配置/环境里必须 `LIVE_TRADING_ENABLED=true`

任何一层不满足都不会发真实订单。

## 9. 典型使用流程

推荐你先这样使用：

1. 安装依赖

```bash
python -m pip install -r requirements.txt
```

2. 先拉一份历史

```bash
python main.py fetch-history --limit 200
```

3. 用内置样例确认回测流程

```bash
python main.py backtest --csv tests/fixtures/sample_history.csv
```

4. 用你自己的历史文件回测

```bash
python main.py backtest --csv data/你的历史文件.csv
```

5. 调整 `config.py` 参数

6. 先做一轮纸面 dry run

```bash
python main.py paper-trade --dry-run-once
```

7. 确认逻辑没问题后，再持续运行纸面交易

```bash
python main.py paper-trade
```

## 10. 说明

- 当前价格和历史快照都来自官方接口，但历史入场价仍然属于“基于可用快照的近似”，不是逐笔撮合级复原
- 当前实现优先保证结构清晰、可回测、可纸面运行
- 如果后续你要接实盘，下一步要补的是 Polymarket 的真实下单签名流程


- `WS_TRADE_GUARD_STALE_SECONDS`
  - 默认值：`1.5`
  - 含义：当 WebSocket 最新消息年龄超过该阈值时，交易会跳过并记录 `skip_reason=ws_stale`。
