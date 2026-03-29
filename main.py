from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from backtest import run_backtest
from config import AppConfig
from polymarket_api import PolymarketClient
from trader import place_live_order, run_paper_trading


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket BTC 5m trading bot")
    subparsers = parser.add_subparsers(dest="command")

    fetch_parser = subparsers.add_parser("fetch-history", help="Export BTC 5m history to CSV")
    fetch_parser.add_argument("--limit", type=int, default=100, help="Maximum rounds to export")
    fetch_parser.add_argument("--output", type=Path, default=None, help="Optional CSV output path")

    backtest_parser = subparsers.add_parser("backtest", help="Run backtest on CSV input")
    backtest_parser.add_argument("--csv", type=Path, required=True, help="Path to historical CSV")

    paper_parser = subparsers.add_parser("paper-trade", help="Run paper trading loop")
    paper_parser.add_argument("--dry-run-once", action="store_true", help="Evaluate one round and exit")

    live_parser = subparsers.add_parser("live-trade", help="Attempt live trading")
    live_parser.add_argument("--enable-live-trading", action="store_true", help="Explicitly enable live-trade path")

    return parser


def _default_history_output(cfg: AppConfig) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return cfg.history_dir / f"btc_5m_history_{timestamp}.csv"


def _print_backtest_summary(result) -> None:
    print("Backtest summary")
    print(f"  Total PnL: {result.total_pnl:.4f}")
    print(f"  Trade count: {result.trade_count}")
    print(f"  Skipped rounds: {result.skipped_round_count}")
    print(f"  Stop-loss resets: {result.stop_loss_count}")
    print(f"  Max consecutive losses: {result.max_consecutive_losses}")
    print(f"  Avg PnL per round: {result.average_pnl_per_round:.4f}")
    print(f"  Max drawdown: {result.max_drawdown:.4f}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0

    cfg = AppConfig()

    if args.command == "fetch-history":
        client = PolymarketClient(cfg)
        output = args.output or _default_history_output(cfg)
        client.export_history(output_path=output, limit=args.limit)
        print(f"History exported to: {output}")
        return 0

    if args.command == "backtest":
        result = run_backtest(args.csv, cfg)
        _print_backtest_summary(result)
        return 0

    if args.command == "paper-trade":
        result = run_paper_trading(cfg, dry_run_once=args.dry_run_once)
        print("Paper trading")
        for key, value in result.items():
            print(f"  {key}: {value}")
        return 0

    if args.command == "live-trade":
        if not args.enable_live_trading or not cfg.live_trading_enabled:
            print("Live trading is disabled. Re-run with credentials and explicit enablement.")
            return 1
        place_live_order()
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
