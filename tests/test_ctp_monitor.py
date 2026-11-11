"""
程序化交易监控引擎（ctp_verification）离线测试。

全部使用假网关与合成事件，不连接任何真实柜台。
"""

import time

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import Direction, Exchange, Offset, OrderType, Status
from vnpy.trader.engine import MainEngine
from vnpy.trader.event import EVENT_CONTRACT, EVENT_LOG, EVENT_ORDER
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    CancelRequest,
    ContractData,
    LogData,
    OrderData,
    OrderRequest,
    Product,
)

from ctp_verification import CtrMonitorApp
from ctp_verification.constant import EVENT_CTR_WARNING


class FakeGateway(BaseGateway):
    """假网关：记录请求并立即返回委托号"""

    def __init__(self, event_engine: EventEngine) -> None:
        """构造"""
        super().__init__(event_engine, "FAKE")

        self.order_seq: int = 0
        self.cancel_count: int = 0

    def connect(self, setting: dict) -> None:
        """空实现"""

    def subscribe(self, req) -> None:
        """空实现"""

    def query_account(self) -> None:
        """空实现"""

    def query_position(self) -> None:
        """空实现"""

    def close(self) -> None:
        """空实现"""

    def send_order(self, req: OrderRequest) -> str:
        """记录报单并返回委托号（模拟已送达柜台）"""
        self.order_seq += 1
        return f"{self.gateway_name}.{self.order_seq}"

    def cancel_order(self, req) -> None:
        """记录撤单请求"""
        self.cancel_count += 1


def make_contract() -> ContractData:
    """构造测试合约：tick=1，最小1手，最大500手"""
    return ContractData(
        symbol="rb2505",
        exchange=Exchange.SHFE,
        name="螺纹钢2505",
        product=Product.FUTURES,
        size=10,
        pricetick=1,
        min_volume=1,
        max_volume=500,
        gateway_name="FAKE",
    )


def make_request(
    price: float = 3500, volume: float = 1, symbol: str = "rb2505"
) -> OrderRequest:
    """构造测试委托请求"""
    return OrderRequest(
        symbol=symbol,
        exchange=Exchange.SHFE,
        direction=Direction.LONG,
        type=OrderType.LIMIT,
        volume=volume,
        price=price,
        offset=Offset.OPEN,
        reference="ManualTrading",
    )


class Harness:
    """测试夹具：引擎 + 假网关 + 事件收集"""

    def __init__(self, keep_stats: bool = False) -> None:
        """初始化（构造失败时也保证关闭，防止事件引擎线程阻塞进程退出）"""
        self.event_engine: EventEngine = EventEngine()
        self.main_engine: MainEngine = MainEngine(self.event_engine)

        try:
            # 清除同日统计落盘，避免上一测试的计数被同日恢复（测试隔离；
            # 持久化测试用 keep_stats=True 保留）
            if not keep_stats:
                from vnpy.trader.utility import get_file_path

                stats_path = get_file_path(
                    f"ctr_stats_{time.strftime('%Y%m%d')}.json"
                )
                stats_path.unlink(missing_ok=True)

            self.engine = self.main_engine.add_app(CtrMonitorApp)

            self.gateway: FakeGateway = FakeGateway(self.event_engine)
            self.main_engine.gateways["FAKE"] = self.gateway

            self.warnings: list = []
            self.event_engine.register(EVENT_CTR_WARNING, self.on_warning)

            self.event_engine.put(Event(EVENT_CONTRACT, make_contract()))
            self.wait()
        except Exception:
            self.main_engine.close()
            raise

    def on_warning(self, event: Event) -> None:
        """收集预警"""
        self.warnings.append(event.data)

    def wait(self, seconds: float = 0.2) -> None:
        """等待事件处理完成"""
        time.sleep(seconds)

    def close(self) -> None:
        """清理"""
        self.main_engine.close()


def test_order_count_and_record():
    """正常报单：计数 + 明细记录"""
    t: Harness = Harness()
    try:
        vt_orderid: str = t.main_engine.send_order(make_request(), "FAKE")
        t.wait()

        assert vt_orderid
        assert t.engine.order_count == 1
        assert vt_orderid in t.engine.records
        assert t.engine.records[vt_orderid].status == "已发送"
    finally:
        t.close()


def test_pre_check_invalid_contract():
    """预检：合约代码错误（本地拒绝，不发送柜台）"""
    t: Harness = Harness()
    try:
        vt_orderid: str = t.main_engine.send_order(
            make_request(symbol="rb9999"), "FAKE"
        )
        t.wait()

        assert vt_orderid == ""
        assert t.engine.order_count == 0
        assert t.engine.local_reject_count == 1
        assert t.gateway.order_seq == 0
    finally:
        t.close()


def test_pre_check_price_tick():
    """预检：价格不符合最小变动价位"""
    t: Harness = Harness()
    try:
        vt_orderid: str = t.main_engine.send_order(make_request(price=3500.5), "FAKE")
        t.wait()

        assert vt_orderid == ""
        assert t.engine.local_reject_count == 1
        assert t.gateway.order_seq == 0
    finally:
        t.close()


def test_pre_check_max_volume():
    """预检：超过单笔最大委托量"""
    t: Harness = Harness()
    try:
        vt_orderid: str = t.main_engine.send_order(make_request(volume=501), "FAKE")
        t.wait()

        assert vt_orderid == ""
        assert t.engine.local_reject_count == 1
        assert t.gateway.order_seq == 0
    finally:
        t.close()


def test_pause_blocks_order():
    """暂停交易：报单被拦截，撤单不受限"""
    t: Harness = Harness()
    try:
        t.engine.set_trading_paused(True)
        vt_orderid: str = t.main_engine.send_order(make_request(), "FAKE")
        t.wait()

        assert vt_orderid == ""
        assert t.engine.local_reject_count == 1

        t.engine.set_trading_paused(False)
        vt_orderid = t.main_engine.send_order(make_request(), "FAKE")
        t.wait()
        assert vt_orderid
        assert t.engine.order_count == 1
    finally:
        t.close()


def test_duplicate_order_warn():
    """重复报单（warn 模式）：两笔均真实送达柜台，第二笔计重复"""
    t: Harness = Harness()
    try:
        t.main_engine.send_order(make_request(), "FAKE")
        t.main_engine.send_order(make_request(), "FAKE")
        t.wait()

        assert t.gateway.order_seq == 2
        assert t.engine.order_count == 2
        assert t.engine.dup_order_count == 1
        assert any(w.kind == "重复报单" for w in t.warnings)
    finally:
        t.close()


def test_duplicate_order_block():
    """重复报单（block 模式）：第二笔被本地拦截"""
    t: Harness = Harness()
    try:
        t.engine.update_setting({"dup_mode": "block"})
        t.main_engine.send_order(make_request(), "FAKE")
        vt_orderid: str = t.main_engine.send_order(make_request(), "FAKE")
        t.wait()

        assert vt_orderid == ""
        assert t.gateway.order_seq == 1
        assert t.engine.order_count == 1
        assert t.engine.dup_order_count == 0
    finally:
        t.close()


def test_threshold_warning_latch():
    """阈值预警：达标触发一次，latch 防重复"""
    t: Harness = Harness()
    try:
        t.engine.update_setting({"order_threshold": 2})
        for _ in range(3):
            t.main_engine.send_order(make_request(price=3500 + _), "FAKE")
        t.wait()

        assert t.engine.order_count == 3
        fired: list = [w for w in t.warnings if w.kind == "报单笔数阈值"]
        assert len(fired) == 1
        assert "阈值 2" in fired[0].message
    finally:
        t.close()


def test_cancel_count_and_ok():
    """撤单：请求计数 + 柜台已撤销回报计撤单成功"""
    t: Harness = Harness()
    try:
        vt_orderid: str = t.main_engine.send_order(make_request(), "FAKE")
        t.wait()

        od: OrderData = OrderData(
            symbol="rb2505",
            exchange=Exchange.SHFE,
            orderid=vt_orderid.split(".", 1)[1],
            type=OrderType.LIMIT,
            direction=Direction.LONG,
            offset=Offset.OPEN,
            price=3500,
            volume=1,
            traded=0,
            status=Status.CANCELLED,
            gateway_name="FAKE",
        )
        req: CancelRequest = od.create_cancel_request()
        t.main_engine.cancel_order(req, "FAKE")
        t.wait()
        assert t.engine.cancel_count == 1
        assert t.gateway.cancel_count == 1

        t.event_engine.put(Event(EVENT_ORDER, od))
        t.wait()
        assert t.engine.cancel_ok_count == 1
    finally:
        t.close()


def test_counter_error_and_reject_correlation():
    """柜台拒单 + 错误日志关联：错误码写入明细备注"""
    t: Harness = Harness()
    try:
        vt_orderid: str = t.main_engine.send_order(make_request(), "FAKE")
        t.wait()

        od: OrderData = OrderData(
            symbol="rb2505",
            exchange=Exchange.SHFE,
            orderid=vt_orderid.split(".", 1)[1],
            type=OrderType.LIMIT,
            direction=Direction.LONG,
            offset=Offset.OPEN,
            price=3500,
            volume=1,
            status=Status.REJECTED,
            gateway_name="FAKE",
        )
        t.event_engine.put(Event(EVENT_ORDER, od))
        t.wait()

        log: LogData = LogData(
            msg="交易委托失败，代码：31，信息：资金不足",
            gateway_name="FAKE",
        )
        t.event_engine.put(Event(EVENT_LOG, log))
        t.wait()

        assert t.engine.counter_reject_count == 1
        record = t.engine.records.get(vt_orderid)
        assert record is not None
        assert "代码31" in record.remark
        assert "资金不足" in record.remark
    finally:
        t.close()


def test_connection_state_machine():
    """连接状态机：断开告警 -> 连接中 -> 就绪"""
    t: Harness = Harness()
    try:
        def push_log(msg: str) -> None:
            t.event_engine.put(Event(EVENT_LOG, LogData(msg=msg, gateway_name="FAKE")))

        push_log("交易服务器连接成功")
        t.wait()
        assert t.engine.conn_state["FAKE"]["交易"] == "连接中"

        push_log("交易服务器连接断开，原因4097")
        t.wait()
        assert t.engine.conn_state["FAKE"]["交易"] == "断开"
        assert any(w.kind == "连接断开" for w in t.warnings)

        push_log("交易服务器连接成功")
        push_log("交易服务器授权验证成功")
        push_log("交易服务器登录成功")
        push_log("结算信息确认成功")
        push_log("合约信息查询成功")
        t.wait(0.5)
        assert t.engine.conn_state["FAKE"]["交易"] == "就绪"

        push_log("行情服务器连接成功")
        push_log("行情服务器登录成功")
        t.wait()
        assert t.engine.conn_state["FAKE"]["行情"] == "就绪"
    finally:
        t.close()


def test_stats_persistence_same_day():
    """统计落盘与同日恢复"""
    t: Harness = Harness()
    try:
        t.main_engine.send_order(make_request(), "FAKE")
        t.wait()
        t.engine.save_today_stats()

        # 新引擎实例同日恢复
        t2: Harness = Harness(keep_stats=True)
        try:
            assert t2.engine.order_count == 1
        finally:
            t2.close()
    finally:
        t.close()
