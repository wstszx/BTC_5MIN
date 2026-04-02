# Dashboard Runbook (PowerShell)

Workspace: `D:\python\BTC_5MIN`

## 1. Start

```powershell
cd D:\python\BTC_5MIN
py -u main.py dashboard --host 127.0.0.1 --port 8787 --env-file .env.dashboard
```

Expected terminal lines:

- `Dashboard running at http://127.0.0.1:8787`
- `Config file: .env.dashboard`

## 2. Open

Open in browser:

```text
http://127.0.0.1:8787
```

## 3. What You Can Do

- Edit strategy parameters and save them into `.env.dashboard`
- View current market quote snapshot and quote source
- View signal decision, risk plan, and skip reason
- View websocket runtime stats (`ws_runtime`) and stale-guard status
- View daily paper summary and recent paper trades

## 3.1 New UI Layout (Chinese)

The dashboard now uses a Chinese quant-terminal style and is split into four areas:

- Left: **Config Engine** (all editable env keys, reload + save)
- Center: **Market + Signal** (round slug/title, entry countdown, quote, signal, plan, session state)
- Right: **WS Runtime** + **Paper Summary** (latest day KPIs and recent 14-day table)
- Bottom: **Recent Paper Trades** (latest 80 rows by default)

Static assets:

- `/dashboard.css`
- `/dashboard.js`

JSON APIs remain unchanged:

- `/api/config`
- `/api/market`
- `/api/paper/summary`
- `/api/paper/recent?limit=20`

## 4. Quick Troubleshooting

- Page does not open
  - Ensure dashboard process is still running in terminal
  - Check if port is occupied; change `--port` if needed

- Data is stale or not updating
  - Check network access to Polymarket APIs
  - Check terminal for runtime errors

- Save seems ineffective
  - Confirm status text becomes `saved`
  - Confirm `.env.dashboard` contains updated values

## 5. Stop

Press:

```text
Ctrl + C
```
