# Polymarket BTC 5m Trading Bot

用于 Polymarket 比特币 5 分钟 `Up / Down` 滚动市场的纯 Python 交易程序。

当前版本已经支持：

- 历史 BTC 5 分钟市场数据导出到 CSV
- 基于同一套策略和资金管理逻辑的回测
- 纸面交易运行
- 运行状态持久化
- 默认禁用的实盘下单占位接口

当前版本还不支持直接真实下单。`live-trade` 命令仍然会拒绝执行，直到后续接入 Polymarket 实盘签名和钱包凭据。

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
- `strategy_id = 1`
- `target_profit = 0.5`
- `max_consecutive_losses = 8`
- `max_stake = 25.0`
- `max_price_threshold = 0.65`
- `daily_loss_cap = 50.0`
- `poll_interval_seconds = 5`
- `entry_timing = "OPEN"`
- `open_delay_seconds = 5`
- `preclose_seconds = 30`

你最常会修改的是这些参数：

- `strategy_id`
  说明：`1/2/3/4` 分别对应：
  - 1: `UP, DOWN, UP, DOWN`
  - 2: `UP, UP, DOWN, DOWN`
  - 3: `UP, UP, UP, DOWN, DOWN, DOWN`
  - 4: `UP, UP, UP, UP, DOWN, DOWN, DOWN, DOWN`
- `target_profit`
  说明：每次赢后希望净赚多少
- `max_consecutive_losses`
  说明：最大连续亏损次数，达到后触发止损重置
- `max_stake`
  说明：单笔实际最大花费 USDC 上限
- `max_price_threshold`
  说明：如果目标方向价格高于这个阈值则跳过
- `daily_loss_cap`
  说明：每日累计亏损达到该值后停止交易
- `entry_timing`
  说明：`OPEN` 表示开盘后入场，`PRE_CLOSE` 表示临近收盘入场
- `open_delay_seconds`
  说明：`OPEN` 模式下开盘后延迟多少秒再尝试
- `preclose_seconds`
  说明：`PRE_CLOSE` 模式下收盘前多少秒尝试入场

## 3. 命令总览

查看全部命令：

```bash
python main.py --help
```

当前支持 4 个主命令：

- `fetch-history`
- `backtest`
- `paper-trade`
- `live-trade`

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

## 8. 实盘命令当前状态

目前命令行里虽然有：

```bash
python main.py live-trade --enable-live-trading
```

但当前版本不会真实下单，原因是：

- `config.py` 里的 `live_trading_enabled = False`
- `trader.py` 里的真实下单函数还是占位实现
- 还没有接入 Polymarket 真实签名、API 凭据和钱包类型配置

所以当前这个项目适合：

- 拉历史数据
- 跑回测
- 跑纸面交易

不适合直接拿去实盘下真单。

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
