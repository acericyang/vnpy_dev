# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

VeighNa (vnpy) is a Python-based open-source quant trading system framework, currently at v4.4.0. This repository holds the **core framework only** (`vnpy/`). Broker gateways, trading apps, database drivers, and datafeeds are **separate packages** (`vnpy_ctp`, `vnpy_ctastrategy`, `vnpy_sqlite`, `vnpy_rqdata`, etc.) living in other repos and loaded dynamically at runtime. The project is bilingual: Chinese is primary (README.md, log messages, UI), with `README_ENG.md` and an English gettext locale available.

Python 3.10–3.13 (3.13 recommended), target-version `py310`. Type-annotated (`py.typed` shipped).

## Common Commands

```bash
# Lint and type check (run from repo root) — required before submitting PRs
ruff check .
mypy vnpy

# Build
uv build

# Install for development (includes alpha ML extras + dev type stubs)
uv pip install -e .[alpha,dev] --system
# ta-lib is a C dependency; install prebuilt wheel from the vnpy index:
uv pip install ta-lib==0.6.4 --index=https://pypi.vnpy.com --system

# Run tests (offline — all tests use synthetic data, no network or gateway needed)
pytest                                   # all
pytest tests/test_alpha101.py            # one file
pytest tests/test_alpha101.py -k "name"  # single test by keyword

# Launch the trading app (UI). See README "脚本运行" for the run.py template.
python run.py
```

CI (`.github/workflows/pythonapp.yml`, Windows/Python 3.13) runs `ruff check .`, `mypy vnpy`, `python -m pytest tests`, then `uv build`.

PRs target the **`dev`** branch, not `master`; branch features off `dev`.

## Code Style

- **ruff** selects `B, E, F, UP, W` and ignores `E501` (line length not enforced).
- **mypy** is strict: `disallow_untyped_defs`, `disallow_incomplete_defs`, `no_implicit_optional`, `warn_return_any`, etc. All function signatures need annotations.
- Missing-import overrides are granted only for `polars`, `lightgbm`, `hatchling.*`, `qrcode` (see `[[tool.mypy.overrides]]`).

## Architecture

### Event-driven core (`vnpy/event`)

`EventEngine` is the heartbeat of the platform. A background thread drains a `Queue` and dispatches each `Event(type, data)` to handlers registered for that type, plus any *general* handlers. A separate timer thread emits `EVENT_TIMER` every second by default. Everything — market data, orders, logs — flows through this queue as events.

### Trader core (`vnpy/trader`)

- **`MainEngine`** (`engine.py`): central hub. Owns the `EventEngine`, gateways, function engines, and apps. On init it starts the event engine and registers `LogEngine`, `OmsEngine`, `EmailEngine`, `WechatEngine`. For convenience it re-exposes OMS query methods (`get_tick`, `get_contract`, `send_order`, etc.) directly on itself.
- **`OmsEngine`**: subscribes to tick/order/trade/position/account/contract/quote events and caches the latest snapshot of each in dicts keyed by `vt_*` id. Also owns per-gateway `OffsetConverter`s.
- **`BaseGateway`** (`gateway.py`, abstract): connects to a broker API. Contract: thread-safe, non-blocking, auto-reconnect. Pushes data through `on_tick`/`on_order`/`on_trade`/`on_position`/`on_account`/`on_contract`/`on_quote`. **Each callback emits two events** — the general type (e.g. `EVENT_TICK`) and a symbol/id-specific type (e.g. `EVENT_TICK + vt_symbol`) so strategies can subscribe to a single instrument. Implementations live in external `vnpy_*` packages.
- **`BaseApp`** (`app.py`, abstract): declares `app_name`, `app_module`, `engine_class`, `widget_name`, `icon_name`. Apps (cta_strategy, spread_trading, data_manager, …) are external packages registered via `main_engine.add_app(AppClass)`, which instantiates the app and its engine.
- **Data objects** (`object.py`): `@dataclass` models — `TickData`, `BarData`, `OrderData`, `TradeData`, `PositionData`, `AccountData`, `ContractData`, `QuoteData`, `LogData` — plus request objects. All inherit `BaseData` (carries `gateway_name`). Each computes its `vt_symbol`/`vt_orderid`/… in `__post_init__`.
- **Enums** (`constant.py`): `Direction`, `Offset`, `Status`, `Product`, `OrderType`, `OptionType`, `Exchange`, `Interval`. Enum values are wrapped in `_(...)` for i18n (a few are plain strings). `Exchange` is heavy on Chinese exchanges (CFFEX, SHFE, CZCE, DCE, SSE, …).
- **`vt_symbol` convention**: `SYMBOL.EXCHANGE`, e.g. `IF2406.CFFEX`. Use `extract_vt_symbol` / `generate_vt_symbol` (`utility.py`) to convert.
- **Dynamic plugin loading**: `database.py` and `datafeed.py` import the implementation by name — `import_module(f"vnpy_{SETTINGS['database.name']}")` and `f"vnpy_{SETTINGS['datafeed.name']}"`. Falls back to `vnpy_sqlite` / a no-op `BaseDatafeed`. `get_database()` / `get_datafeed()` are module-level singletons.
- **`OffsetConverter` / `PositionHolding`** (`converter.py`): translate between net, long/short, and lock modes, and handle China-specific today/yesterday close offsets (`CLOSETODAY`/`CLOSEYESTERDAY`). The OMS builds one converter per gateway when its first contract arrives.
- **`BarGenerator` / `ArrayManager`** (`utility.py`): `BarGenerator` synthesizes 1-minute bars from ticks and x-minute/x-hour/daily bars from 1-minute bars (x must divide 60 for minute windows). `ArrayManager` keeps a rolling numpy array of OHLCV and computes ta-lib indicators. These are the standard building blocks for strategy development.
- **Settings & runtime dir**: `SETTINGS` (`setting.py`) is a dict seeded with defaults and updated from `vt_setting.json`. `TRADER_DIR`/`TEMP_DIR` resolve to a `.vntrader/` folder — found in the cwd if present, otherwise `~/.vntrader` (auto-created). All JSON config and logs live there; `MainEngine.__init__` does `os.chdir(TRADER_DIR)`.
- **Logging**: `loguru` wrapped by `LogEngine`; logs flow as `EVENT_LOG` events. Files written to `.vntrader/log/vt_YYYYMMDD.log`.
- **i18n**: `vnpy/trader/locale` via gettext; wrap user-visible strings with `_()`. `.mo` files are build artifacts (gitignored) compiled by a babel build hook (`locale/build_hook.py`); source strings live in `vnpy.pot`.
- **UI** (`vnpy/trader/ui`): PySide6 + pyqtgraph + qdarkstyle. Entry points `create_qapp()` and `MainWindow`. `widget.py` is large and contains most widgets.

### Other core packages

- **`vnpy/rpc`**: ZMQ-based request/reply cross-process RPC (`server.py`/`client.py`) for distributed deployments.
- **`vnpy/chart`**: pyqtgraph candlestick charting (`widget.py`, `item.py`, `manager.py`).
- **`vnpy/trader/optimize.py`**: parameter optimization using `deap` genetic algorithms + `ProcessPoolExecutor`; brute-force via `itertools.product`.

### Alpha module (`vnpy/alpha`) — AI quant research (v4.0+)

Inspired by Microsoft Qlib. Optional dependencies under the `[alpha]` extra (polars, scipy, scikit-learn, lightgbm, torch, pyarrow, alphalens-reloaded). Uses **polars** (not pandas) as its primary dataframe engine.

- **`AlphaLab`** (`lab.py`): on-disk research workspace. Manages bar data (parquet), index components (shelve), datasets/models (pickle), and signals (parquet) under a lab directory. Normalizes prices by first close and converts suspended-day zeros to NaN when loading.
- **`AlphaDataset`** (`dataset/template.py`): builds ML training data. You add feature *expressions* (string or polars `Expr`) and a label expression, plus infer/learn *processors*. `prepare_data()` evaluates all expressions in parallel via a `multiprocessing` spawn pool, then splits into TRAIN/VALID/TEST `Segment`s. `show_feature_performance` / `show_signal_performance` run alphalens tear sheets.
- **Expression engine** (`dataset/utility.py`): `calculate_by_expression` parses string expressions against a `DataProxy`; `register_functions([...])` adds custom functions. Built-in helpers in `cs_function` (cross-sectional), `ts_function` (time-series), `math_function`, `ta_function`. Datasets: `alpha_158` (Qlib feature set), `alpha_101`.
- **`AlphaModel`** (`model/template.py`): abstract `fit(dataset)` / `predict(dataset, segment)`. Implementations: `lasso_model`, `lgb_model`, `mlp_model`.
- **`AlphaStrategy` + `BacktestingEngine`** (`strategy/`): backtest ML-signal-driven strategies; supports cross-sectional (multi-symbol) and time-series (single-symbol) types.

### Tests

All tests are offline (synthetic data, no network/gateway/broker). Run `pytest` from the repo root — there is no pytest config file. Coverage spans the core (`test_event.py`, `test_engine.py`, `test_object.py`, `test_converter.py`, `test_utility.py`, `test_data_services.py`) and the alpha module (`test_alpha101.py`, `test_alpha_backtesting.py`, `alpha/test_dataproxy.py`, `alpha/test_expression.py`).

`tests/conftest.py` does two things before any `vnpy` import: it `os.chdir`s into a temp dir containing an empty `.vntrader/` (so tests never touch the user's real config in `~/.vntrader`), and disables file/console logging via `SETTINGS`. Any new test that changes global settings should follow this pattern.

## Gitignored runtime/build artifacts

`.vntrader/` (runtime config & logs), `lab/` (alpha research workspace), `*.mo` (compiled translations), `_build/` (docs) are all gitignored.
