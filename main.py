from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from backtest import run_backtest
from config import AppConfig
from paper_report import summarize_paper_trades
from polymarket_api import PolymarketClient
from streak_analysis import analyze_streak_risk
from strategy_research import export_strategy_research_csv, run_strategy_research
from test_table_builder import build_augmented_test_table
from dashboard import run_dashboard
from trader import place_live_order, run_paper_trading


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Polymarket BTC 5m trading bot")
    subparsers = parser.add_subparsers(dest="command")

    fetch_parser = subparsers.add_parser("fetch-history", help="Export BTC 5m history to CSV")
    fetch_parser.add_argument("--limit", type=int, default=100, help="Maximum rounds to export")
    fetch_parser.add_argument("--output", type=Path, default=None, help="Optional CSV output path")

    backtest_parser = subparsers.add_parser("backtest", help="Run backtest on CSV input")
    backtest_parser.add_argument("--csv", type=Path, required=True, help="Path to historical CSV")

    streak_parser = subparsers.add_parser("analyze-streak", help="Analyze consecutive-loss risk and suggest reset round")
    streak_parser.add_argument("--csv", type=Path, required=True, help="Path to historical CSV")
    streak_parser.add_argument("--strategy-id", type=int, default=None, help="Override strategy id (default: config value)")
    streak_parser.add_argument(
        "--target-occurrence",
        type=float,
        default=0.01,
        help="Target max occurrence per round for loss streak >= K (0~1)",
    )
    streak_parser.add_argument("--min-round", type=int, default=2, help="Minimum K to evaluate")
    streak_parser.add_argument("--max-round", type=int, default=10, help="Maximum K to evaluate")

    research_parser = subparsers.add_parser("research-strategy", help="Run repeated strategy validation and rank candidates")
    research_parser.add_argument("--csv", type=Path, required=True, help="Path to historical CSV")
    research_parser.add_argument("--strategy-ids", type=str, default="1,2,3,4", help="Comma-separated strategy ids")
    research_parser.add_argument("--reset-rounds", type=str, default="2,3,4,5,6", help="Comma-separated reset rounds")
    research_parser.add_argument("--target-profits", type=str, default="1.0", help="Comma-separated per-round target profits")
    research_parser.add_argument("--entry-timing", type=str, choices=("OPEN", "PRE_CLOSE"), default="OPEN", help="Entry price column")
    research_parser.add_argument("--segments", type=int, default=5, help="How many chronological validation segments")
    research_parser.add_argument("--top-n", type=int, default=5, help="How many best configs to print")
    research_parser.add_argument(
        "--bankroll-safety-multiplier",
        type=float,
        default=1.5,
        help="Multiplier to convert historical min bankroll into recommended bankroll",
    )
    research_parser.add_argument("--output", type=Path, default=None, help="Optional CSV output for all candidates")

    paper_parser = subparsers.add_parser("paper-trade", help="Run paper trading loop")
    paper_parser.add_argument("--dry-run-once", action="store_true", help="Evaluate one round and exit")

    report_parser = subparsers.add_parser("paper-report", help="Summarize paper-trading performance by day")
    report_parser.add_argument("--csv", type=Path, default=Path("logs/paper_trades.csv"), help="Path to paper trades CSV")
    report_parser.add_argument("--tz-offset", type=str, default="+08:00", help="UTC offset for day grouping, e.g. +08:00")
    report_parser.add_argument("--start-date", type=str, default=None, help="Optional start date (YYYY-MM-DD)")
    report_parser.add_argument("--end-date", type=str, default=None, help="Optional end date (YYYY-MM-DD)")

    table_parser = subparsers.add_parser(
        "build-test-table",
        help="Fill missing prices randomly and export augmented table with bet columns + bottom total PnL",
    )
    table_parser.add_argument("--csv", type=Path, required=True, help="Input CSV path")
    table_parser.add_argument("--output", type=Path, required=True, help="Output CSV path")
    table_parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible fill")
    table_parser.add_argument("--fill-min-price", type=float, default=0.45, help="Minimum sampled price")
    table_parser.add_argument("--fill-max-price", type=float, default=0.60, help="Maximum sampled price")
    table_parser.add_argument("--strategy-id", type=int, default=None, help="Override strategy id")
    table_parser.add_argument("--target-profit", type=float, default=None, help="Override target profit per win")
    table_parser.add_argument("--max-consecutive-losses", type=int, default=None, help="Override reset round")
    table_parser.add_argument("--max-stake", type=float, default=None, help="Override max order cost")
    table_parser.add_argument("--max-price-threshold", type=float, default=None, help="Override price threshold")
    table_parser.add_argument("--daily-loss-cap", type=float, default=None, help="Override daily loss cap")
    table_parser.add_argument("--entry-timing", type=str, choices=("OPEN", "PRE_CLOSE"), default=None, help="Override entry timing")
    table_parser.add_argument(
        "--bet-sizing-mode",
        type=str,
        choices=("TARGET_PROFIT", "FIXED_BASE_COST"),
        default=None,
        help="Override bet sizing mode",
    )
    table_parser.add_argument("--base-order-cost", type=float, default=None, help="Override fixed base order cost")

    live_parser = subparsers.add_parser("live-trade", help="Attempt live trading")
    live_parser.add_argument("--enable-live-trading", action="store_true", help="Explicitly enable live-trade path")
    live_parser.add_argument("--dry-run-once", action="store_true", help="Preview one live order plan without submitting")

    dashboard_parser = subparsers.add_parser("dashboard", help="Run local web dashboard")
    dashboard_parser.add_argument("--host", type=str, default="127.0.0.1", help="Dashboard host")
    dashboard_parser.add_argument("--port", type=int, default=8787, help="Dashboard port")
    dashboard_parser.add_argument("--env-file", type=Path, default=Path('.env.dashboard'), help="Dashboard env file path")

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


def _print_streak_analysis_summary(result) -> None:
    print("Streak risk analysis")
    print(f"  Analyzed rounds: {result.analyzed_round_count}")
    print(f"  Strategy id: {result.strategy_id}")
    print(f"  Hit rate: {result.hit_rate:.4%}")
    print(f"  Max loss streak: {result.max_loss_streak}")
    print(f"  Max affordable round (stake cap): {result.max_affordable_round}")
    print(f"  Target occurrence: {result.target_occurrence:.4%}")
    print(f"  Recommended reset round: {result.recommended_reset_round}")
    print("  Threshold table (K, groups, occurrence_per_round)")
    for row in result.thresholds:
        print(f"    K={row.threshold_round}, groups={row.streak_group_count}, occurrence={row.occurrence_per_round:.4%}")


def _parse_int_csv(raw: str) -> list[int]:
    values = [part.strip() for part in raw.split(",")]
    parsed = [int(value) for value in values if value]
    if not parsed:
        raise ValueError("Expected at least one integer value.")
    return parsed


def _parse_float_csv(raw: str) -> list[float]:
    values = [part.strip() for part in raw.split(",")]
    parsed = [float(value) for value in values if value]
    if not parsed:
        raise ValueError("Expected at least one float value.")
    return parsed


def _print_strategy_research_summary(report, *, top_n: int) -> None:
    print("Strategy research report")
    print(f"  CSV: {report.csv_path}")
    print(f"  Rounds analyzed: {report.analyzed_round_count}")
    print(f"  Candidates evaluated: {report.candidate_count}")
    print(f"  Top shown: {min(top_n, len(report.top_candidates))}")
    print("  Top candidates")
    for index, row in enumerate(report.top_candidates[:top_n], start=1):
        print(
            "    "
            f"{index}. strategy={row.strategy_id}, reset={row.reset_round}, target_profit={row.target_profit:.4f}, "
            f"pnl={row.total_pnl:.4f}, trades={row.trades}, hit_rate={row.hit_rate:.4%}, "
            f"required_bankroll={row.required_bankroll:.4f}, recommended_bankroll={row.recommended_bankroll:.4f}, "
            f"max_drawdown={row.max_drawdown:.4f}, profitable_segments={row.profitable_segments}/{row.segment_count}, "
            f"score={row.score:.4f}"
        )


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

    if args.command == "analyze-streak":
        result = analyze_streak_risk(
            args.csv,
            cfg,
            strategy_id=args.strategy_id,
            target_occurrence=args.target_occurrence,
            min_round=args.min_round,
            max_round=args.max_round,
        )
        _print_streak_analysis_summary(result)
        return 0

    if args.command == "research-strategy":
        try:
            strategy_ids = _parse_int_csv(args.strategy_ids)
            reset_rounds = _parse_int_csv(args.reset_rounds)
            target_profits = _parse_float_csv(args.target_profits)
        except ValueError as exc:
            parser.error(str(exc))
            return 2

        report = run_strategy_research(
            args.csv,
            cfg,
            strategy_ids=strategy_ids,
            reset_rounds=reset_rounds,
            target_profits=target_profits,
            entry_timing=args.entry_timing,
            segments=args.segments,
            bankroll_safety_multiplier=args.bankroll_safety_multiplier,
            top_n=args.top_n,
        )
        _print_strategy_research_summary(report, top_n=args.top_n)
        if args.output is not None:
            output = export_strategy_research_csv(args.output, report)
            print(f"  Full candidate report exported to: {output}")
        return 0

    if args.command == "paper-trade":
        result = run_paper_trading(cfg, dry_run_once=args.dry_run_once)
        print("Paper trading")
        for key, value in result.items():
            print(f"  {key}: {value}")
        return 0

    if args.command == "paper-report":
        try:
            summaries = summarize_paper_trades(
                args.csv,
                tz_offset=args.tz_offset,
                start_date=args.start_date,
                end_date=args.end_date,
            )
        except (FileNotFoundError, ValueError) as exc:
            print(f"Paper report failed: {exc}")
            return 1

        if not summaries:
            print("Paper report")
            print("  No rows matched the current filter.")
            return 0

        print("Paper report")
        print(f"  CSV: {args.csv}")
        print(f"  TZ offset: {args.tz_offset}")
        if args.start_date or args.end_date:
            print(f"  Date filter: {args.start_date or '-'} ~ {args.end_date or '-'}")
        for day in summaries:
            print(
                "  "
                f"{day.date} | rows={day.rows} trades={day.trade_rows} skips={day.skip_rows} "
                f"wins={day.wins} losses={day.losses} hit_rate={day.hit_rate:.2%} "
                f"pnl={day.total_pnl:.4f} avg_trade_pnl={day.avg_trade_pnl:.4f} max_drawdown={day.max_drawdown:.4f} "
                f"signal_rows={day.signal_rows} avg_abs_delta={day.avg_abs_signal_delta:.4f} "
                f"strong_signal_rate={day.strong_signal_rate:.2%} signal_locked_rate={day.signal_locked_rate:.2%}"
            )
            if day.skip_reason_counts:
                formatted = ", ".join(f"{key}={value}" for key, value in sorted(day.skip_reason_counts.items()))
                print(f"    skip_reasons: {formatted}")
        return 0

    if args.command == "build-test-table":
        table_cfg = AppConfig(
            strategy_id=args.strategy_id if args.strategy_id is not None else cfg.strategy_id,
            target_profit=args.target_profit if args.target_profit is not None else cfg.target_profit,
            max_consecutive_losses=(
                args.max_consecutive_losses if args.max_consecutive_losses is not None else cfg.max_consecutive_losses
            ),
            max_stake=args.max_stake if args.max_stake is not None else cfg.max_stake,
            max_price_threshold=args.max_price_threshold if args.max_price_threshold is not None else cfg.max_price_threshold,
            daily_loss_cap=args.daily_loss_cap if args.daily_loss_cap is not None else cfg.daily_loss_cap,
            entry_timing=args.entry_timing if args.entry_timing is not None else cfg.entry_timing,
            bet_sizing_mode=args.bet_sizing_mode if args.bet_sizing_mode is not None else cfg.bet_sizing_mode,
            base_order_cost=args.base_order_cost if args.base_order_cost is not None else cfg.base_order_cost,
        )
        result = build_augmented_test_table(
            input_csv=args.csv,
            output_csv=args.output,
            cfg=table_cfg,
            seed=args.seed,
            fill_min_price=args.fill_min_price,
            fill_max_price=args.fill_max_price,
        )
        print("Build test table")
        for key, value in result.items():
            print(f"  {key}: {value}")
        return 0

    if args.command == "live-trade":
        if args.dry_run_once:
            result = place_live_order(cfg=cfg, dry_run=True)
            print("Live trading dry run")
            for key, value in result.items():
                print(f"  {key}: {value}")
            return 0
        if not args.enable_live_trading or not cfg.live_trading_enabled:
            print("Live trading is disabled. Re-run with credentials and explicit enablement.")
            return 1
        try:
            result = place_live_order(cfg=cfg, dry_run=False)
        except RuntimeError as exc:
            print(f"Live trading failed: {exc}")
            return 1
        print("Live trading")
        for key, value in result.items():
            print(f"  {key}: {value}")
        return 0

    if args.command == "dashboard":
        run_dashboard(host=args.host, port=args.port, env_file=args.env_file)
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
