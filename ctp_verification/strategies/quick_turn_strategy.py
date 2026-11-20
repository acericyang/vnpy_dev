"""
快进快出策略：循环执行 开仓 -> 平仓 -> 挂单 -> 撤单，直到手动停止。

每个动作之间随机等待 wait_min_sec ~ wait_max_sec 秒，
委托来源列（reference）为 CtaStrategy_<实例名>，与手工单可区分。

用法：
  1. CTA策略管理 -> 添加策略 -> QuickTurnStrategy
  2. vt_symbol 如 i2701.DCE，volume 默认 1 手
  3. 交易时段内：初始化 -> 启动；停止按钮或退出程序即结束
  4. 每轮结束自动开始下一轮，循环往复

注意：far_offset 需为合约最小变动价位的整数倍（i2701 tick=0.5，默认 5.0 合法）。
"""
from random import randint
from time import time

from vnpy.trader.constant import Offset
from vnpy_ctastrategy import (
    BarData,
    CtaTemplate,
    OrderData,
    StopOrder,
    TickData,
    TradeData,
)


class QuickTurnStrategy(CtaTemplate):
    """快进快出：循环执行 开仓 -> 平仓 -> 挂单 -> 撤单，直到手动停止"""

    author = "quant"

    volume: int = 1
    far_offset: float = 5.0
    wait_min_sec: int = 30
    wait_max_sec: int = 60

    stage: int = 0
    state: str = "未启动"
    tick_count: int = 0
    round_count: int = 0

    parameters = ["volume", "far_offset", "wait_min_sec", "wait_max_sec"]
    variables = ["state", "tick_count", "round_count"]

    def __init__(self, *args, **kwargs) -> None:
        """"""
        super().__init__(*args, **kwargs)
        self.last_tick: TickData | None = None
        self.stage_since: float = 0.0
        self.wait_sec: int = 0

    def draw_wait(self) -> None:
        """抽取本步骤的随机等待秒数"""
        self.wait_sec = randint(self.wait_min_sec, self.wait_max_sec)
        self.stage_since = time()

    def on_init(self) -> None:
        """
        策略初始化。
        """
        self.write_log("策略初始化")
        self.stage = 0
        self.state = "待启动"
        self.last_tick = None
        self.round_count = 0

    def on_start(self) -> None:
        """
        启动：等待行情后开仓。
        """
        self.write_log("策略启动，等待行情")
        self.stage = 1
        self.state = "监控启动，扫描行情"
        self.put_event()

    def on_stop(self) -> None:
        """
        停止兜底：撤掉全部挂单，平掉残留持仓。
        """
        self.cancel_all()
        if self.pos != 0 and self.last_tick:
            price = (self.last_tick.bid_price_1 if self.pos > 0
                     else self.last_tick.ask_price_1)
            if price > 0:
                if self.pos > 0:
                    self.sell(price, abs(self.pos))
                else:
                    self.cover(price, abs(self.pos))
                self.write_log(f"停止时平掉残留持仓 {abs(self.pos)} 手")
        self.state = "已停止"
        self.write_log("策略停止")
        self.put_event()

    def on_tick(self, tick: TickData) -> None:
        """
        状态机驱动：1等行情开仓 -> 2等开仓成交 -> 3等待后平仓
        -> 4等平仓成交 -> 5等待后挂单 -> 6等待后撤单
        -> 8轮间等待 -> 回到1，循环执行。
        """
        if self.stage == 0:
            return

        self.last_tick = tick
        self.tick_count += 1
        now: float = time()
        remain: int = max(0, int(self.wait_sec - (now - self.stage_since)))

        if self.stage == 1:
            if tick.ask_price_1 > 0:
                self.buy(tick.ask_price_1, self.volume)
                self.state = f"买入信号 @ {tick.ask_price_1}"
                self.write_log(
                    f"买入开仓 {self.volume} 手 @ {tick.ask_price_1}（对手价）"
                )
                self.stage = 2
        elif self.stage == 3:
            if remain <= 0 and tick.bid_price_1 > 0:
                self.sell(tick.bid_price_1, self.volume)
                self.state = f"卖出信号 @ {tick.bid_price_1}"
                self.write_log(f"卖出平仓 {self.volume} 手 @ {tick.bid_price_1}")
                self.stage = 4
            else:
                self.state = "持仓监控中"
        elif self.stage == 5:
            far_price: float = tick.bid_price_1 - self.far_offset
            if remain <= 0 and tick.bid_price_1 > 0 and far_price > 0:
                self.buy(far_price, self.volume)
                self.state = f"限价跟踪 @ {far_price}"
                self.write_log(f"限价买入挂单 {self.volume} 手 @ {far_price}")
                self.stage = 6
                self.draw_wait()
            else:
                self.state = "空仓扫描中"
        elif self.stage == 6:
            if remain <= 0:
                self.cancel_all()
                self.write_log("撤销挂单")
                self.stage = 8
                self.draw_wait()
            else:
                self.state = "限价跟踪中"
        elif self.stage == 8:
            if remain <= 0:
                self.stage = 1
                self.round_count += 1
                self.state = f"第 {self.round_count + 1} 轮扫描"
                self.write_log(f"开始第 {self.round_count + 1} 轮")
            else:
                self.state = "轮动扫描中"

        self.put_event()

    def on_trade(self, trade: TradeData) -> None:
        """
        成交回报推进状态机（成交后进入随机时长等待）。
        """
        close_offsets = (Offset.CLOSE, Offset.CLOSETODAY, Offset.CLOSEYESTERDAY)
        if self.stage == 2 and trade.offset == Offset.OPEN:
            self.stage = 3
            self.draw_wait()
            self.state = f"持仓 {int(trade.volume)} 手 @ {trade.price}"
            self.write_log(f"开仓成交 @ {trade.price}")
        elif self.stage == 4 and trade.offset in close_offsets:
            self.stage = 5
            self.draw_wait()
            self.state = f"平仓完成 @ {trade.price}"
            self.write_log(f"平仓成交 @ {trade.price}")
        self.put_event()

    def on_bar(self, bar: BarData) -> None:
        """
        本策略仅用 tick 驱动。
        """
        pass

    def on_order(self, order: OrderData) -> None:
        """
        委托回报。
        """
        self.put_event()

    def on_stop_order(self, stop_order: StopOrder) -> None:
        """
        本策略不使用停止单。
        """
        pass
