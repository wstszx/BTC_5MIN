# BTC_5MIN 每日固定巡检清单（1 页命令版）

适用目录：`D:\pythonProject\BTC_5MIN`

目标：每天按固定节奏跑策略 5 纸测，并快速发现异常。

## 1. 每日启动（开盘前/早上）

```powershell
cd D:\pythonProject\BTC_5MIN
git pull origin main
python -m pytest -q
```

启动统一运行时（策略、时间框架和模式从 `.env.dashboard` 读取），并记录 PID：

```powershell
cd D:\pythonProject\BTC_5MIN
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
$proc = Start-Process -FilePath python -ArgumentList @('main.py') `
  -WorkingDirectory 'D:\pythonProject\BTC_5MIN' `
  -RedirectStandardOutput "logs/runtime_$ts.out" `
  -RedirectStandardError "logs/runtime_$ts.err" `
  -PassThru
Set-Content -Path logs\runtime_latest.pid -Value $proc.Id
Set-Content -Path logs\runtime_latest_ts.txt -Value $ts
"Started PID=$($proc.Id) TS=$ts"
```

启动成功后打开 `http://127.0.0.1:8787/` 查看行情、信号、配置和纸面交易汇总。

## 2. 盘中巡检（建议每 2~4 小时执行一次）

确认进程在运行：

```powershell
Get-Process | Where-Object { $_.ProcessName -like 'python*' } | Select-Object Id,StartTime,Path
```

查看最新日志文件：

```powershell
$latestOut = Get-ChildItem logs\runtime_*.out | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$latestErr = Get-ChildItem logs\runtime_*.err | Sort-Object LastWriteTime -Descending | Select-Object -First 1
$latestOut.FullName
$latestErr.FullName
```

查看最近日志内容：

```powershell
Get-Content $latestOut.FullName -Tail 80
Get-Content $latestErr.FullName -Tail 80
```

手动做一次轻量健康检查：

```powershell
cd D:\pythonProject\BTC_5MIN
python -m pytest tests\test_strategy.py tests\test_risk_and_sizing.py -q
```

## 3. 日终复盘（每天固定时间）

优先在 Dashboard 的“纸面交易汇总”和“最近交易明细”查看当日摘要。需要命令行导出时，可直接调用 `paper_report` 模块：

```powershell
cd D:\pythonProject\BTC_5MIN
$d = Get-Date -Format 'yyyy-MM-dd'
@"
from pathlib import Path
from paper_report import summarize_paper_trades
for item in summarize_paper_trades(Path('logs/paper/5m/paper_trades.csv'), tz_offset='+08:00', start_date='$d', end_date='$d'):
    print(item)
"@ | python -
```

保存一份日报到文件：

```powershell
cd D:\pythonProject\BTC_5MIN
$ts = Get-Date -Format 'yyyyMMdd_HHmmss'
@"
from pathlib import Path
from paper_report import summarize_paper_trades
for item in summarize_paper_trades(Path('logs/paper/5m/paper_trades.csv'), tz_offset='+08:00'):
    print(item)
"@ | python - | Tee-Object -FilePath "logs/paper_report_$ts.txt"
```

## 4. 快速异常处理

`python` 进程没了：直接按“每日启动”重新启动。

出现 `order_cost_above_max_stake` 频繁跳过：

```powershell
# 临时降低目标收益（下次启动生效）
$env:TARGET_PROFIT='0.8'
```

出现 `signal_too_weak_skip` 过多：

```powershell
# 临时降低信号阈值（下次启动生效）
$env:SIGNAL_MOMENTUM_THRESHOLD='0.010'
```

## 5. 手动停止/重启

按 PID 停止：

```powershell
$pid = Get-Content logs\runtime_latest.pid
Stop-Process -Id $pid
```

停止所有 python（谨慎）：

```powershell
Get-Process | Where-Object { $_.ProcessName -like 'python*' } | Stop-Process
```

## 6. 每天必须记录的 6 个数字

从 `paper-report` 里记录：

- `trades`
- `hit_rate`
- `pnl`
- `max_drawdown`
- `strong_signal_rate`
- `signal_locked_rate`

如果连续 3 天 `pnl < 0` 且 `max_drawdown` 持续扩大，再考虑调参数，不要单日就频繁改策略。
