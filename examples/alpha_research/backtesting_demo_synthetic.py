"""
Alpha 回测引擎演示（合成数据，零外部数据依赖）。

用随机游走 + 趋势段生成两个品种的 1 分钟 K 线（日盘交易时段），
配一个内联的双均线 AlphaStrategy，跑完整回测并输出统计与图。

运行：
    python examples/alpha_research/backtesting_demo_synthetic.py
"""

from collections import defaultdict
from datetime import datetime, timedelta
from typing import cast

import numpy as np
import polars as pl

from vnpy.alpha.lab import AlphaLab
from vnpy.alpha.strategy.backtesting import BacktestingEngine
from vnpy.alpha.strategy.template import AlphaStrategy
from vnpy.trader.constant import Exchange, Interval
from vnpy.trader.object import BarData, TradeData


# 合成行情参数
SYMBOLS: list[str] = ["rb2506.SHFE", "au2506.SHFE"]
BASE_PRICES: dict = {"rb2506.SHFE": 3500.0, "au2506.SHFE": 620.0}
TRADING_DAYS: int = 10
PRICETICKS: dict = {"rb2506.SHFE": 1.0, "au2506.SHFE": 0.5}

# 日盘时段（09:00-11:30, 13:30-15:00），每档 1 分钟
SESSIONS: list = [
    (9, 0, 11, 30),
    (13, 30, 15, 0),
]


class DoubleMaAlphaStrategy(AlphaStrategy):
    """双均线截面演示策略：快线上穿慢线持多、下穿持空（每个品种独立判断）"""

    fast_window: int = 10
    slow_window: int = 30
    fixed_volume: float = 1.0
    price_add: float = 0.001

    def on_init(self) -> None:
        """初始化收盘价缓存"""
        self.closes: defaultdict = defaultdict(list)

    def on_trade(self, trade: TradeData) -> None:
        """成交回报（演示策略无需处理）"""

    def on_bars(self, bars: dict[str, BarData]) -> None:
        """每根 K 线切片：更新均线目标仓位并执行交易"""
        bar: BarData
        for vt_symbol, bar in bars.items():
            self.closes[vt_symbol].append(bar.close_price)

        vt_symbol: str
        closes: list[float]
        for vt_symbol, closes in self.closes.items():
            if len(closes) < self.slow_window:
                continue
            fast_ma: float = sum(closes[-self.fast_window:]) / self.fast_window
            slow_ma: float = sum(closes[-self.slow_window:]) / self.slow_window
            if fast_ma > slow_ma:
                self.set_target(vt_symbol, self.fixed_volume)
            else:
                self.set_target(vt_symbol, -self.fixed_volume)

        self.execute_trading(bars, self.price_add)


def make_dts(days: int) -> list[datetime]:
    """生成日盘交易时段内的 1 分钟时间戳（跳过周末）"""
    today: datetime = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)

    dts: list[datetime] = []
    added_days: int = 0
    offset: int = 0
    while added_days < days:
        day: datetime = today + timedelta(days=offset)
        offset += 1
        if day.weekday() >= 5:
            continue
        added_days += 1

        start_h: int
        start_m: int
        end_h: int
        end_m: int
        for start_h, start_m, end_h, end_m in SESSIONS:
            cursor: datetime = day.replace(hour=start_h, minute=start_m)
            end: datetime = day.replace(hour=end_h, minute=end_m)
            while cursor <= end:
                dts.append(cursor)
                cursor += timedelta(minutes=1)
    return dts


def make_bars(vt_symbol: str, dts: list[datetime], seed: int) -> list[BarData]:
    """随机游走 + 正弦趋势段合成 OHLCV（保证有涨有跌，双均线有信号）"""
    rng: np.random.RandomState = np.random.RandomState(seed)
    price: float = BASE_PRICES[vt_symbol]
    pricetick: float = PRICETICKS[vt_symbol]

    bars: list[BarData] = []
    dt: datetime
    for i, dt in enumerate(dts):
        drift: float = 6.0 * np.sin(i / 120.0) - price * 0.00005
        noise: float = rng.randn() * price * 0.002
        open_price: float = price
        close_price: float = max(round((price + drift * 0.1 + noise) / pricetick) * pricetick, pricetick)
        high_price: float = round(max(open_price, close_price) * (1 + abs(rng.randn()) * 0.0005) / pricetick) * pricetick
        low_price: float = round(min(open_price, close_price) * (1 - abs(rng.randn()) * 0.0005) / pricetick) * pricetick
        volume: float = float(rng.randint(50, 500))

        bar: BarData = BarData(
            gateway_name="BACKTESTING",
            symbol=vt_symbol.split(".")[0],
            exchange=Exchange.SHFE,
            datetime=dt,
            interval=Interval.MINUTE,
            open_price=open_price,
            high_price=high_price,
            low_price=low_price,
            close_price=close_price,
            volume=volume,
        )
        bars.append(bar)
        price = close_price

    return bars


def main(show_chart: bool = True) -> None:
    """组装引擎、灌入合成数据、跑回测并输出统计"""
    dts: list[datetime] = make_dts(TRADING_DAYS)

    # 不经 AlphaLab：直接构造引擎并手动填充合约配置与历史数据
    engine: BacktestingEngine = BacktestingEngine(cast(AlphaLab, object()))
    engine.vt_symbols = list(SYMBOLS)
    engine.interval = Interval.MINUTE
    engine.capital = 1_000_000
    engine.cash = engine.capital
    engine.risk_free = 0
    engine.annual_days = 240

    for vt_symbol in SYMBOLS:
        engine.long_rates[vt_symbol] = 0.0001
        engine.short_rates[vt_symbol] = 0.0001
        engine.sizes[vt_symbol] = 10
        engine.priceticks[vt_symbol] = PRICETICKS[vt_symbol]

    for i, vt_symbol in enumerate(SYMBOLS):
        bar: BarData
        for bar in make_bars(vt_symbol, dts, seed=42 + i):
            engine.dts.add(bar.datetime)
            engine.history_data[(bar.datetime, vt_symbol)] = bar

    engine.add_strategy(DoubleMaAlphaStrategy, {}, pl.DataFrame())
    print(f"合成数据：{len(SYMBOLS)} 个品种 × {len(dts)} 根 1 分钟 K 线")

    engine.run_backtesting()
    print(f"回测完成：成交 {len(engine.trades)} 笔")

    engine.calculate_result()
    statistics: dict = engine.calculate_statistics()

    key: str
    value: object
    for key, value in statistics.items():
        if isinstance(value, float):
            print(f"    {key:<24}{value:,.4f}")
        else:
            print(f"    {key:<24}{value}")

    if show_chart:
        engine.show_chart()


if __name__ == "__main__":
    main()
