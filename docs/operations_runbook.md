# BTC_5MIN 操作手册（PowerShell 完整版）

本手册覆盖从安装到长期纸测、日报分析、实盘 dry-run、以及 Git 提交推送的全流程操作。

适用目录：`D:\python\BTC_5MIN`

快捷入口：

- 每日 1 页巡检清单：[docs/daily_ops_checklist.md](./daily_ops_checklist.md)

## 1. 基础准备

### 1.1 进入目录

```powershell
cd D:\python\BTC_5MIN
```

### 1.2 安装依赖

```powershell
python -m pip install -r requirements.txt
```

### 1.3 查看全部命令

```powershell
python main.py --help
```

---

## 2. 历史数据与回测

### 2.1 拉取历史数据

```powershell
python main.py fetch-history --limit 300 --output data/history_latest_300.csv
```

### 2.2 基础回测

```powershell
python main.py backtest --csv data/history_latest_300.csv
```

### 2.3 连亏风险评估（重置轮数建议）

```powershell
python main.py analyze-streak --csv data/history_latest_300.csv --strategy-id 2 --target-occurrence 0.01 --min-round 2 --max-round 10
```

### 2.4 批量策略研究（多参数）

```powershell
python main.py research-strategy --csv data/history_latest_300.csv --strategy-ids 1,2,3,4,5 --reset-rounds 3,4,5,6 --target-profits 1.0 --segments 5 --top-n 10 --output data/research_report.csv
```

---

## 3. 纸面交易（前台实时日志）

## 3.1 默认策略前台运行（实时看日志）

```powershell
cd D:\python\BTC_5MIN
python main.py paper-trade
```

停止：`Ctrl + C`

### 3.2 策略 5（弱信号跳过）前台运行（推荐）

```powershell
cd D:\python\BTC_5MIN
$env:STRATEGY_ID='5'
$env:SIGNAL_WEAK_SIGNAL_MODE='SKIP'
$env:SIGNAL_MOMENTUM_THRESHOLD='0.015'
python main.py paper-trade
```

停止：`Ctrl + C`

运行结束后建议清理环境变量：

```powershell
Remove-Item Env:STRATEGY_ID -ErrorAction SilentlyContinue
Remove-Item Env:SIGNAL_WEAK_SIGNAL_MODE -ErrorAction SilentlyContinue
Remove-Item Env:SIGNAL_MOMENTUM_THRESHOLD -ErrorAction SilentlyContinue
```

### 3.3 只做一轮检查（dry-run）

```powershell
python main.py paper-trade --dry-run-once
```

策略 5 dry-run：

```powershell
$env:STRATEGY_ID='5'
$env:SIGNAL_WEAK_SIGNAL_MODE='SKIP'
python main.py paper-trade --dry-run-once
Remove-Item Env:STRATEGY_ID -ErrorAction SilentlyContinue
Remove-Item Env:SIGNAL_WEAK_SIGNAL_MODE -ErrorAction SilentlyContinue
```

---

## 4. 纸面交易（后台长期运行）

### 4.1 后台启动（默认策略）

```powershell
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
Start-Process -FilePath python -ArgumentList @('main.py','paper-trade') `
  -WorkingDirectory 'D:\python\BTC_5MIN' `
  -RedirectStandardOutput "logs/paper_live_$ts.out" `
  -RedirectStandardError "logs/paper_live_$ts.err"
```

### 4.2 后台启动（策略5 + 弱信号跳过）

```powershell
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
$env:STRATEGY_ID='5'
$env:SIGNAL_WEAK_SIGNAL_MODE='SKIP'
$env:SIGNAL_MOMENTUM_THRESHOLD='0.015'
Start-Process -FilePath python -ArgumentList @('main.py','paper-trade') `
  -WorkingDirectory 'D:\python\BTC_5MIN' `
  -RedirectStandardOutput "logs/paper_live_signal_$ts.out" `
  -RedirectStandardError "logs/paper_live_signal_$ts.err"
Remove-Item Env:STRATEGY_ID -ErrorAction SilentlyContinue
Remove-Item Env:SIGNAL_WEAK_SIGNAL_MODE -ErrorAction SilentlyContinue
Remove-Item Env:SIGNAL_MOMENTUM_THRESHOLD -ErrorAction SilentlyContinue
```

### 4.3 查看后台进程

```powershell
Get-Process | Where-Object { $_.ProcessName -like 'python*' }
```

### 4.4 实时查看后台日志

标准输出日志（最近 100 行并持续刷新）：

```powershell
Get-Content logs\paper_live_signal_YYYYMMDD_HHMMSS.out -Tail 100 -Wait
```

错误日志：

```powershell
Get-Content logs\paper_live_signal_YYYYMMDD_HHMMSS.err -Tail 100 -Wait
```

### 4.5 停止后台进程

```powershell
Stop-Process -Id <PID>
```

---

## 5. 每日汇总报告（命中 / 盈亏 / 回撤 / 信号质量）

### 5.1 查看全量日报

```powershell
python main.py paper-report --csv logs/paper_trades.csv --tz-offset +08:00
```

### 5.2 指定日期区间

```powershell
python main.py paper-report --csv logs/paper_trades.csv --tz-offset +08:00 --start-date 2026-03-31 --end-date 2026-04-05
```

输出解释：

- `rows`：当日总记录数
- `trades`：真实下单并结算的记录数
- `skips`：跳过记录数
- `hit_rate`：当日命中率（wins / trades）
- `pnl`：当日总盈亏
- `max_drawdown`：当日最大回撤
- `signal_rows`：包含信号信息的记录数
- `avg_abs_delta`：平均信号强度（`abs(signal_delta)`）
- `strong_signal_rate`：强信号占比（`abs(delta) >= threshold`）
- `signal_locked_rate`：入场前锁方向占比

---

## 6. 实盘命令（仅 dry-run 推荐）

### 6.1 实盘 dry-run（不下单）

```powershell
python main.py live-trade --dry-run-once
```

### 6.2 真实下单（高风险，务必谨慎）

```powershell
$env:LIVE_TRADING_ENABLED='true'
$env:POLYMARKET_PRIVATE_KEY='你的私钥'
python main.py live-trade --enable-live-trading
```

---

## 7. 状态重置与日志管理

### 7.1 重置纸测状态（从头统计）

```powershell
Remove-Item logs\session_state.json -ErrorAction SilentlyContinue
```

### 7.2 清理纸测交易记录

```powershell
Remove-Item logs\paper_trades.csv -ErrorAction SilentlyContinue
```

说明：如果日志列结构变化，程序会自动把旧格式文件归档为 `paper_trades_legacy_*.csv`，再创建新格式文件。

---

## 8. Git 提交与推送

### 8.1 查看状态

```powershell
git status --short
git branch --show-current
git remote -v
```

### 8.2 运行测试

```powershell
python -m pytest -q
```

### 8.3 提交并推送

```powershell
git add .
git commit -m "your commit message"
git push origin main
```

### 8.4 查看最近提交

```powershell
git log --oneline -n 10
```

---

## 9. 常见问题排查

### 9.1 `order_cost_above_max_stake`

含义：本轮按资金管理计算的下单金额超过 `max_stake`，因此跳过。

建议：

- 降低 `TARGET_PROFIT` 或 `BASE_ORDER_COST`
- 放宽 `MAX_STAKE`
- 或减少连续追损轮数（`MAX_CONSECUTIVE_LOSSES`）

### 9.2 `signal_too_weak_skip`

含义：策略 5 中信号强度不足阈值，按 `SKIP` 模式跳过。

建议：

- 降低 `SIGNAL_MOMENTUM_THRESHOLD`
- 或改为 `SIGNAL_WEAK_SIGNAL_MODE=FALLBACK`

### 9.3 实盘被拒绝

检查：

- 是否传了 `--enable-live-trading`
- `LIVE_TRADING_ENABLED` 是否为 `true`
- 私钥环境变量是否正确

---

## 10. 推荐日常流程

1. 前台 dry-run 先看一轮是否正常。
2. 后台启动策略 5 跑全天纸测。
3. 每天固定时间执行 `paper-report`。
4. 连续观察 3~7 天后再调整阈值与风控参数。
5. 只在纸测稳定后考虑实盘接口。
