# Polymarket BTC 5m Trading Bot Design

## Summary

Build a pure Python trading bot for Polymarket's BTC 5 minute "Up or Down" markets that supports:

- Historical market discovery and CSV export
- Backtesting with configurable entry timing
- Real-time market monitoring
- Paper trading end-to-end
- Live-trading integration points kept disabled by default
- Configurable rhythm-based directional strategies
- Dynamic position sizing based on current buy price and unrecovered loss
- Risk controls including stop loss and daily loss caps

The bot will target Polymarket's rolling BTC 5 minute series instead of any single fixed market:

- `series_id = 10684`
- `series_slug = btc-up-or-down-5m`

## Goals

- Provide a directly runnable Python project with a simple module layout
- Keep configuration centralized and easy to edit
- Share the same strategy and sizing logic between backtest and runtime
- Default to safe behavior by supporting paper trading first
- Leave a clean path to enable real order placement later when credentials are available

## Non-Goals

- No complex framework or database
- No fully reconstructed exchange-grade execution simulator
- No enabled live order placement without user-supplied credentials
- No attempt to infer trades from unofficial data sources

## Official Data Findings

The design is based on verified Polymarket official docs and public endpoints.

- BTC 5 minute markets are rolling event instances, not one permanent market id
- Each 5 minute round has a slug like `btc-updown-5m-1774798200`
- Resolved events expose `eventMetadata.priceToBeat` and `eventMetadata.finalPrice`
- Event outcomes for this series are `Up` and `Down`
- Live quotes can be read from market data such as `bestBid`, `bestAsk`, and `outcomePrices`
- The series can be discovered reliably via `events?series_id=10684`

## Architecture

The project stays intentionally small and file-oriented.

```text
BTC_5MIN/
  config.py
  models.py
  polymarket_api.py
  strategy.py
  risk_and_sizing.py
  backtest.py
  trader.py
  main.py
  requirements.txt
  README.md
  data/
  logs/
  docs/superpowers/specs/
```

### Module Responsibilities

#### `config.py`

Central place for runtime settings:

- API base URLs
- `series_id = 10684`
- mode selection
- strategy selection
- target profit
- max consecutive losses
- max stake
- max buy price threshold
- daily loss cap
- polling interval
- entry timing mode
- open-delay and pre-close timing offsets
- file paths for data and logs

#### `models.py`

Typed data structures used across the program:

- `MarketWindow`
- `MarketQuote`
- `ResolvedRound`
- `TradePlan`
- `TradeRecord`
- `SessionState`
- `BacktestResult`

These structures prevent ad hoc dict passing and make logic easier to test.

#### `polymarket_api.py`

Encapsulates all interaction with official Polymarket endpoints:

- Discover BTC 5 minute events by `series_id`
- Fetch event details by slug
- Fetch market quotes by slug
- Parse token ids for `Up` and `Down`
- Fetch token time series from official `prices-history`
- Provide polling helpers for live monitoring
- Expose live-trading placeholders for authenticated calls

#### `strategy.py`

Computes the intended direction for the next round based on the configured rhythm strategy.

#### `risk_and_sizing.py`

Computes:

- whether a round is tradable
- the intended buy price
- the contract quantity needed to recover current unrecovered loss plus fixed target profit
- resulting cash cost
- stop-loss and daily-cap behavior

#### `backtest.py`

Reads historical CSV and simulates trades using the same strategy and sizing code as runtime trading.

#### `trader.py`

Implements the live runtime state machine:

- discover current market
- wait for entry window
- evaluate trade
- place paper trade or disabled live-trade placeholder
- wait for settlement
- settle PnL and advance state

#### `main.py`

Command-line entry point with these commands:

- `python main.py fetch-history`
- `python main.py backtest`
- `python main.py paper-trade`
- `python main.py live-trade`

## Data Flow

### Market Discovery

The bot will not hard-code a fixed market id. Instead it will:

1. query official events with `series_id=10684`
2. identify:
   - the current round
   - the next round
   - the most recently resolved round
3. read event and market details for timing, quotes, and token ids

This makes the bot automatically follow Polymarket's rolling BTC 5 minute schedule.

### Historical Data Export

Historical export writes one row per round to CSV.

Required columns:

- `event_id`
- `market_id`
- `slug`
- `title`
- `series_id`
- `start_time`
- `end_time`
- `price_to_beat`
- `final_price`
- `result`
- `up_token_id`
- `down_token_id`
- `up_last_price`
- `down_last_price`
- `up_best_bid`
- `up_best_ask`
- `down_best_bid`
- `down_best_ask`

When available, the exporter also writes entry-timing-specific price snapshots:

- `entry_price_open_up`
- `entry_price_open_down`
- `entry_price_preclose_up`
- `entry_price_preclose_down`

### Resolution Logic

Resolved result is derived as:

- `UP` if `final_price >= price_to_beat`
- `DOWN` otherwise

This matches the verified official event description for the BTC 5 minute series.

### Runtime Quote Selection

Buy price selection rules:

1. choose the intended side: `UP` or `DOWN`
2. prefer that side's `bestAsk`
3. if missing, fall back to parsed `outcomePrices`
4. if price is missing, `<= 0`, or `>= 1`, skip the round

## Strategy Design

The bot supports four user-configurable rhythm strategies:

- Strategy 1: `UP, DOWN, UP, DOWN, ...`
- Strategy 2: `UP, UP, DOWN, DOWN, ...`
- Strategy 3: `UP, UP, UP, DOWN, DOWN, DOWN, ...`
- Strategy 4: `UP, UP, UP, UP, DOWN, DOWN, DOWN, DOWN, ...`

Implementation rule:

- `group_size` maps to the selected strategy number
- `block_index = round_index // group_size`
- even `block_index` means `UP`
- odd `block_index` means `DOWN`

This allows one compact implementation for all four strategies.

## Dynamic Sizing Design

The user requires sizing based on current price rather than a fixed multiplier ladder.

Definitions:

- `price`: current buy price for the selected side, between `0` and `1`
- `target_profit`: fixed desired profit for a winning round
- `recovery_loss`: current unrecovered cumulative loss
- `order_size`: number of outcome shares or contracts to buy
- `order_cost`: actual USDC spent, equal to `order_size * price`

### Sizing Formula

If there is no unrecovered loss:

```python
order_size = target_profit / (1 - price)
```

If there is unrecovered loss:

```python
order_size = (recovery_loss + target_profit) / (1 - price)
```

Interpretation:

- the formula solves for contract quantity, not cash spend
- actual cash spent is `order_cost = order_size * price`
- winning net profit is approximately `order_size * (1 - price)`
- losing cash loss is approximately `order_cost`
- this matches the user's formula that uses `1 - price` as net profit per winning share

## PnL and State Accounting

The bot tracks two main running values:

- `cash_pnl`: realized cumulative profit and loss
- `recovery_loss`: current outstanding loss not yet recovered

### Winning Round

- round profit = `order_size * (1 - price)`
- `cash_pnl += round_profit`
- `recovery_loss = 0`
- `consecutive_losses = 0`

### Losing Round

- round loss = `order_cost`
- `cash_pnl -= round_loss`
- `recovery_loss += round_loss`
- `consecutive_losses += 1`

## Risk Controls

Risk rules are checked in a fixed order before any trade is placed.

### 1. Daily Loss Cap

If cumulative loss for the current day exceeds configured cap:

- stop trading for the rest of the day
- resume on the next natural day boundary

### 2. Max Consecutive Losses

If `consecutive_losses >= max_consecutive_losses`:

- record a stop-loss event
- reset `recovery_loss = 0`
- reset `consecutive_losses = 0`
- skip the current round

### 3. Max Price Threshold

If the intended side buy price exceeds configured threshold, for example `price > 0.65`:

- skip the round
- do not count it as a loss
- do not alter `recovery_loss`
- still advance strategy rhythm with the next round

### 4. Max Stake

If computed `order_cost > max_stake`:

- skip the round rather than truncating size

This preserves the user's stated rule that one win should fully recover prior loss and still achieve the fixed target profit.

## Entry Timing

The program supports two configurable entry modes.

### `OPEN`

- runtime: enter as soon as possible after round start plus a configurable `open_delay_seconds`
- backtest: use `entry_price_open_up/down`

### `PRE_CLOSE`

- runtime: enter at `end_time - preclose_seconds`
- backtest: use `entry_price_preclose_up/down`

The user requested support for both modes with configuration-based switching.

## Runtime State Machine

The trading loop will use a small explicit state machine.

### `DISCOVER`

Find current, next, and recently resolved rounds.

### `WAIT_ENTRY`

Wait until configured entry window is reached.

### `EVALUATE`

Compute:

- next strategy side
- buy price
- risk checks
- contract quantity
- cash cost

### `PLACE_ORDER`

- paper mode: log a simulated fill
- live mode: call disabled-by-default authenticated order placeholder

### `WAIT_RESOLUTION`

Monitor until the round is resolved.

### `SETTLE`

Read official result and update:

- `cash_pnl`
- `recovery_loss`
- `consecutive_losses`
- daily counters
- trade log

### `ADVANCE`

- increment `round_index`
- move on to the next round

## Backtest Design

Backtest will:

1. read historical CSV
2. select entry price based on configured entry timing
3. generate intended side from the same strategy code used in runtime
4. apply the same sizing and risk checks
5. simulate win/loss and state changes
6. output performance metrics

### Backtest Outputs

- total PnL
- max consecutive losses
- number of stop-loss resets
- average PnL per round
- maximum drawdown
- trade count
- skipped-round count

## Logging and Persistence

### Trade Log

Each trade record should include:

- `timestamp`
- `mode`
- `round_index`
- `strategy`
- `entry_timing`
- `event_slug`
- `start_time`
- `end_time`
- `side`
- `price`
- `order_size`
- `order_cost`
- `expected_profit`
- `result`
- `trade_pnl`
- `cash_pnl`
- `recovery_loss`
- `consecutive_losses`
- `stop_loss_triggered`
- `skip_reason`

### Files

- `data/*.csv` for exported market history
- `logs/*.csv` for runtime trading logs
- `logs/session_state.json` for resumable session state

Persisted session state will allow restart without losing:

- `round_index`
- `cash_pnl`
- `recovery_loss`
- `consecutive_losses`
- day-level counters

## Error Handling

The bot should favor safe skipping and retrying over hard failure.

### API Errors

- retry transient network failures
- log warnings for incomplete payloads
- skip a round if essential data remains unavailable

### Price Errors

- skip if target-side price is missing or invalid
- skip if token mapping cannot be derived
- in backtest mode, fall back to nearest available history point when exact snapshot is missing

### Timing Errors

- skip a round if entry window is already missed
- continue polling if settlement is delayed
- mark rounds as unresolved only after timeout thresholds

## Live Trading Scope

The user selected support for both paper and live trading, but explicitly chose not to connect real credentials yet.

Therefore:

- paper trading will be fully implemented
- live trading code paths will exist structurally
- authenticated order placement remains disabled by default
- configuration and interface hooks will be prepared for later activation

## Testing Strategy

The project should include automated tests for:

- strategy sequence generation
- stake sizing formula
- risk control branches
- compact backtest scenarios with known outcomes

Representative checks:

- strategy rhythm outputs for the first several rounds of all four strategies
- stake sizing under no-loss and recovery-loss conditions
- stop-loss reset after max consecutive losses
- skip behavior when price threshold or max stake is exceeded
- backtest summary metrics on a controlled fixture CSV

## Dependencies

Keep dependencies minimal:

- `requests`
- `pandas`
- `python-dateutil`
- `websocket-client` or `websockets` as optional real-time enhancement
- `tenacity` as optional retry helper
- `pytest` for tests

## CLI Commands

```bash
python main.py fetch-history
python main.py backtest
python main.py paper-trade
python main.py live-trade
```

Behavior:

- `fetch-history`: export official BTC 5m rounds to CSV
- `backtest`: simulate strategy on CSV input
- `paper-trade`: monitor and simulate runtime trading
- `live-trade`: require explicit enabling and credentials before sending orders

## Open Risks and Assumptions

- Historical entry prices are approximations based on official available snapshots, not full reconstructed execution
- `series_id=10684` is assumed stable for the targeted BTC 5 minute series
- Live order placement is intentionally incomplete until credentials and wallet type are provided
- Some Polymarket list endpoints expose more data than documented filter behavior; implementation should rely on verified fields and local filtering where necessary

## Recommended Implementation Order

1. core models and config
2. official API wrapper and discovery
3. strategy and risk/sizing modules
4. historical export
5. backtest engine and tests
6. paper trading runtime loop
7. disabled live-trading integration hooks
8. README and final verification
