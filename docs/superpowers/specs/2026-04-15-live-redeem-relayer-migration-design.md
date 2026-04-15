# Live Redeem Relayer Migration Design

## Goal

Replace the current live auto-redeem implementation that sends Polygon transactions directly with an official Polymarket Relayer / Gasless flow, while keeping live order placement unchanged.

## Background

The current live redeem worker discovers redeemable positions correctly through the positions API, but it executes redemption by calling `redeemPositions()` on-chain with `web3` and a locally managed private key. That approach works for direct EOA execution, but it is not the target architecture for this project. The project should switch to the official Relayer / Gasless integration path and stop treating direct on-chain redemption as a supported runtime mode.

The current code already has stable redeem discovery, retry scheduling, state persistence, and dashboard visibility. The migration should reuse those pieces and replace only the redeem submission backend plus the related configuration and validation surface.

## Scope

This migration covers:

- live auto-redeem configuration
- runtime validation for redeem credentials
- redeem execution backend
- redeem worker state updates
- dashboard configuration and status presentation
- automated tests and operator docs

This migration does not cover:

- changes to live order placement
- changes to paper trading
- automatic migration of existing private keys to Safe or proxy wallets
- changing how redeemable positions are discovered

## Architecture

The redeem worker will keep its current high-level structure:

1. discover redeemable positions from the positions API
2. reconcile local redeem state
3. attempt due redeems serially
4. persist runtime and per-condition status
5. expose worker state to the dashboard

The only supported redeem submission path will become an official Relayer / Gasless client adapter. Runtime selection will be deterministic:

- if Builder credentials are fully configured, use Builder mode
- else if Relayer API key credentials are fully configured, use Relayer-key mode
- else the worker stays enabled-but-blocked with a configuration error

Direct on-chain redemption with `web3` will be removed as a supported execution path.

## Official Credential Model

The project must treat live trading credentials and live redeem credentials as different concerns.

### Existing CLOB live trading credentials

These stay in place for live order placement:

- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_API_PASSPHRASE`

### New Relayer / Gasless credentials

These are added for live redeem execution:

- `POLYMARKET_BUILDER_API_KEY`
- `POLYMARKET_BUILDER_SECRET`
- `POLYMARKET_BUILDER_PASSPHRASE`
- `POLYMARKET_RELAYER_API_KEY`
- `POLYMARKET_RELAYER_API_KEY_ADDRESS`

### Runtime selection

The project will compute a read-only redeem auth mode:

- `builder`
- `relayer`
- `unconfigured`

No user-facing mode switch will be added for `onchain` or `web3`.

## Configuration Changes

### `config.py`

Add new app-config fields for:

- builder API key
- builder secret
- builder passphrase
- relayer API key
- relayer API key address

Add a small resolver helper or computed property that determines the effective redeem auth mode from the configured fields.

### Validation rules

`validate_live_runtime_config()` will continue to validate live trading requirements. It will also validate redeem requirements when `LIVE_AUTO_REDEEM_ENABLED=true`:

- live mode must still be valid
- either Builder credentials must be complete
- or Relayer API key credentials must be complete
- otherwise validation fails with a redeem-specific configuration error

Validation must clearly distinguish:

- trading credential problems
- redeem credential problems

## Runtime Design

### Worker lifecycle

`run_live_redeem_worker()` remains the live-only background worker. It continues to:

- load `logs/live_redeem_state.json`
- discover `redeemable=true` positions
- reconcile known conditions with current positions
- attempt due redeems
- write runtime status for the dashboard

### Submission backend

The current direct on-chain helpers will be removed from the supported path:

- `_import_live_redeem_web3`
- `_build_live_redeem_web3`
- `_execute_live_redeem_onchain`

They will be replaced by relayer-facing helpers such as:

- `_build_live_redeem_relayer_client(cfg)`
- `_execute_live_redeem_via_relayer(cfg, condition_id, event_slug, index_sets)`

`execute_live_redeem()` becomes a thin adapter that:

- supports existing dry-run behavior
- delegates real execution only to the official relayer path
- returns a submission identifier and any transaction hash made available by the relayer response

### State model

The current redeem state file structure will be preserved as much as possible:

- per-condition status
- retry counters
- next-attempt timestamps
- runtime last result
- runtime pending count

Because relayer submission does not guarantee that a chain transaction hash is available immediately, the state model should expand from a tx-only view to a submission-aware view.

Recommended additions:

- `last_submission_id`
- `last_submission_status`
- keep `last_tx_hash` only when known

Existing consumers should continue to work if a tx hash is absent.

## Dashboard Changes

### Config editor

Add editable fields for the new Builder and Relayer credentials.

Update wording so the distinction is obvious:

- `POLYMARKET_API_*` means CLOB live trading credentials
- `POLYMARKET_BUILDER_*` and `POLYMARKET_RELAYER_*` mean gasless redeem credentials

### Runtime status

Expose a compact redeem auth summary in the runtime panel:

- `Redeem Auth Mode`
- `Pending Redeems`
- `Last Result`
- `Last Attempt`
- `Last Submission Id`
- `Last Tx Hash`

When auto redeem is enabled but unconfigured, the runtime surface should make the block explicit without requiring log inspection.

## Error Handling

The redeem worker must separate configuration, authentication, transient platform failures, and terminal business outcomes.

### Configuration errors

Examples:

- auto redeem enabled but no Builder credentials
- auto redeem enabled but no Relayer key credentials

Behavior:

- no submission attempt
- runtime `last_result = "config_error"`
- dashboard validation shows a redeem-specific message

### Authentication errors

Examples:

- `401`
- `403`
- invalid Builder credentials
- invalid Relayer key

Behavior:

- classify as terminal for the current configuration
- record auth mode used
- do not retry the same condition immediately until config changes

### Transient execution errors

Examples:

- timeout
- rate limit
- relayer service unavailable
- `5xx`

Behavior:

- reuse existing exponential backoff scheduling
- record `retry_wait`
- keep condition eligible for later retry

### Terminal business outcomes

Examples:

- already redeemed
- nothing to redeem
- no redeemable balance
- position not redeemable

Behavior:

- mark condition `completed` or `terminal_error` depending on relayer response semantics
- do not schedule another attempt

## Testing Strategy

The migration will be done under TDD and should replace the current `web3`-focused redeem tests with relayer-focused tests.

### Configuration tests

Cover:

- Builder mode resolution
- Relayer-key mode resolution
- unconfigured mode resolution
- live validation failure when auto redeem is enabled but relayer credentials are missing

### Execution tests

Cover:

- Builder-mode redeem request construction
- Relayer-key-mode redeem request construction
- dry-run behavior remains side-effect free
- authentication errors become terminal
- transient errors become `retry_wait`

### Worker tests

Cover:

- worker still discovers redeemable positions the same way
- worker still serializes attempts
- worker persists runtime status and pending counts
- worker writes submission metadata when tx hash is not immediately available

### Dashboard tests

Cover:

- new credential fields are editable and masked
- help text distinguishes trading credentials from redeem credentials
- runtime payload exposes redeem auth mode and submission metadata

## File-Level Impact

### Modify

- `config.py`
  - add Builder and Relayer redeem credential fields
  - add redeem auth mode resolver

- `trader.py`
  - remove supported use of direct `web3` redeem path
  - add relayer client builder and redeem adapter
  - update worker status persistence

- `dashboard.py`
  - add new config fields
  - update help text and labels
  - add redeem auth mode runtime surface

- `README.md`
  - update live redeem credential documentation

- `docs/operations_runbook.md`
  - explain official relayer credential setup and runtime expectations

- `docs/dashboard_runbook.md`
  - explain new dashboard fields and blocking validation messages

- `tests/test_trader_runtime_and_live.py`
  - replace direct-web3 redeem tests with relayer execution tests

- `tests/test_dashboard.py`
  - cover new config fields and runtime payload

### Runtime artifacts

- `logs/live_redeem_state.json`
  - keep file path unchanged
  - extend schema with submission metadata

## Rollout Plan

1. land config support first
2. land relayer execution adapter behind tests
3. update worker state model
4. update dashboard and docs
5. verify with mocked relayer flow locally
6. verify in a real environment by detecting a redeemable position and submitting through relayer

## Risks

- official relayer client semantics may differ from current tx-hash-first assumptions
- Builder and Relayer credential models may need slightly different response parsing
- existing dashboard copy currently describes Polygon on-chain redeem behavior and must be corrected everywhere
- a partial migration that keeps `web3` dependencies around in the worker would make operations ambiguous

## Success Criteria

The migration is successful when all of the following are true:

- live auto redeem no longer depends on local `web3` transaction submission
- redeem execution always uses official Relayer / Gasless credentials
- worker behavior remains deterministic across restart and retry
- dashboard clearly shows which redeem auth mode is active
- configuration errors are visible before a redeem attempt is made
- local automated tests cover Builder mode, Relayer-key mode, and failure handling

