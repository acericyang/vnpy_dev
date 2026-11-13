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
from vnpy.trader.logger import WARNING
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
        self.subscribed: list = []

    def connect(self, setting: dict) -> None:
        """空实现"""

    def subscribe(self, req) -> None:
        """记录订阅请求"""
        self.subscribed.append(f"{req.symbol}.{req.exchange.value}")

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

            self.logs: list = []
            self.event_engine.register(EVENT_LOG, self.on_log)

            self.event_engine.put(Event(EVENT_CONTRACT, make_contract()))
            self.wait()
        except Exception:
            self.main_engine.close()
            raise

    def on_warning(self, event: Event) -> None:
        """收集预警"""
        self.warnings.append(event.data)

    def on_log(self, event: Event) -> None:
        """收集日志"""
        self.logs.append(event.data)

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


def test_status_msg_reject_capture_and_dedup():
    """交易所拒绝（委托状态更新路径）：入错误表、备注回写、计入柜台拒单、回放去重"""
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
        t.event_engine.put(Event(EVENT_ORDER, od))
        t.wait()

        orderid: str = vt_orderid.split(".", 1)[1]
        msg: str = f"委托 {orderid} 状态更新，50:已撤单报单被拒绝SHFE:价格跌破跌停板"
        for _ in range(2):
            t.event_engine.put(
                Event(EVENT_LOG, LogData(msg=msg, gateway_name="FAKE"))
            )
            t.wait()

        assert len(t.engine.counter_errors) == 1
        assert t.engine.counter_errors[0].related_orderid == vt_orderid
        assert t.engine.counter_reject_count == 1
        assert "代码50" in t.engine.records[vt_orderid].remark
    finally:
        t.close()


def test_status_msg_replay_old_order_not_counted():
    """回放旧委托的交易所拒绝：入错误表留证，不计入柜台拒单数"""
    t: Harness = Harness()
    try:
        t.event_engine.put(
            Event(
                EVENT_LOG,
                LogData(
                    msg="委托 9_999999_1 状态更新，26:已撤单报单被拒绝SHFE:当前状态禁止此项操作",
                    gateway_name="FAKE",
                ),
            )
        )
        t.wait()

        assert len(t.engine.counter_errors) == 1
        assert t.engine.counter_errors[0].related_orderid == "FAKE.9_999999_1"
        assert t.engine.counter_reject_count == 0
    finally:
        t.close()


def test_error_replay_dedup_and_warning_log():
    """write_error 路径：重连重复推送去重 + 【柜台错误】WARNING 日志留痕"""
    t: Harness = Harness()
    try:
        for _ in range(2):
            t.event_engine.put(
                Event(
                    EVENT_LOG,
                    LogData(
                        msg="交易所报单拒绝，代码：31，信息：CTP:资金不足",
                        gateway_name="FAKE",
                    ),
                )
            )
            t.wait(0.1)

        assert len(t.engine.counter_errors) == 1
        warning_logs: list = [
            log for log in t.logs
            if log.msg.startswith("【柜台错误】") and log.level == WARNING
        ]
        assert warning_logs
        assert "代码：31" in warning_logs[0].msg
    finally:
        t.close()


def test_order_cancel_chinese_logs():
    """成功报单/撤单产生【委托】/【撤单】中文日志（任务 2.11 交易日志检索依据）"""
    t: Harness = Harness()
    try:
        vt_orderid: str = t.main_engine.send_order(make_request(), "FAKE")
        t.wait()
        assert any(log.msg.startswith("【委托】") for log in t.logs)

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
            status=Status.NOTTRADED,
            gateway_name="FAKE",
        )
        req: CancelRequest = od.create_cancel_request()
        t.main_engine.cancel_order(req, "FAKE")
        t.wait()
        assert any(log.msg.startswith("【撤单】") for log in t.logs)
    finally:
        t.close()


def test_error_flood_throttled():
    """柜台错误洪水：速率闸只登记窗口上限条数，超出压制计数"""
    t: Harness = Harness()
    try:
        for i in range(50):
            t.event_engine.put(
                Event(
                    EVENT_LOG,
                    LogData(
                        msg=f"交易所报单拒绝，代码：31，信息：CTP:资金不足，约缺少资金[{10000 + i}]",
                        gateway_name="FAKE",
                    ),
                )
            )
        t.wait(0.5)

        assert len(t.engine.counter_errors) == 20
        assert t.engine.suppressed_errors == 30
    finally:
        t.close()


def test_status_msg_orderid_with_spaces():
    """他人客户端 OrderRef 带空格对齐时仍可解析（委托号首尾去空格）"""
    t: Harness = Harness()
    try:
        t.event_engine.put(
            Event(
                EVENT_LOG,
                LogData(
                    msg="委托 3_515924376_         487 状态更新，138:已撤单报单被拒绝CFFEX:订单触发自成交",
                    gateway_name="FAKE",
                ),
            )
        )
        t.wait()

        assert len(t.engine.counter_errors) == 1
        related: str = t.engine.counter_errors[0].related_orderid
        assert related.startswith("FAKE.3_515924376_")
        assert related.endswith("487")
        assert t.engine.counter_errors[0].error_id == 138
    finally:
        t.close()


def test_foreign_rejected_not_counted():
    """他人/回放委托的 REJECTED 回报不计入柜台拒单数（is_local 防线）"""
    t: Harness = Harness()
    try:
        foreign: OrderData = OrderData(
            symbol="rb2505",
            exchange=Exchange.SHFE,
            orderid="9_999999_1",
            type=OrderType.LIMIT,
            direction=Direction.LONG,
            offset=Offset.OPEN,
            price=3500,
            volume=1,
            status=Status.REJECTED,
            gateway_name="FAKE",
        )
        t.event_engine.put(Event(EVENT_ORDER, foreign))
        t.wait()
        assert "FAKE.9_999999_1" in t.engine.records
        assert not t.engine.records["FAKE.9_999999_1"].is_local
        assert t.engine.counter_reject_count == 0
        assert "FAKE.9_999999_1" not in t.engine.recent_rejected
    finally:
        t.close()


def test_pause_blocks_modify_order():
    """暂停交易期间改单整体拒绝（撤单与重报都不执行），恢复后正常"""
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
            status=Status.NOTTRADED,
            gateway_name="FAKE",
        )

        t.engine.set_trading_paused(True)
        result: str = t.engine.modify_order(od, 3600, 2)
        t.wait()
        assert result == ""
        assert t.engine.local_reject_count == 1
        assert t.engine.cancel_count == 0
        assert t.gateway.cancel_count == 0

        t.engine.set_trading_paused(False)
        result = t.engine.modify_order(od, 3600, 2)
        t.wait()
        assert result
        assert t.engine.cancel_count == 1
        assert t.engine.order_count == 2
        assert any("改单" in log.msg for log in t.logs)
    finally:
        t.close()


def test_watch_symbols_subscribe():
    """自选行情清单：存在的合约自动订阅，不存在的跳过并留痕"""
    t: Harness = Harness()
    try:
        t.engine.update_setting({"watch_symbols": ["rb2505.SHFE", "xx9999.SHFE"]})
        t.engine.subscribe_watch_symbols()
        t.wait()

        assert t.gateway.subscribed == ["rb2505.SHFE"]
        assert any("xx9999.SHFE" in log.msg for log in t.logs)
    finally:
        t.close()


def test_apply_scan_result():
    """活跃扫描结果：有活跃合约则更新清单；无活跃保留原清单"""
    t: Harness = Harness()
    try:
        t.engine.update_setting({"watch_symbols": ["rb2505.SHFE"]})

        t.engine.apply_scan_result({"active": False, "symbols": []})
        assert t.engine.setting["watch_symbols"] == ["rb2505.SHFE"]

        t.engine.apply_scan_result({"active": True, "symbols": ["rb2505.SHFE"]})
        assert t.engine.setting["watch_symbols"] == ["rb2505.SHFE"]
        assert t.gateway.subscribed == ["rb2505.SHFE"]
        assert any("活跃扫描" in log.msg for log in t.logs)
    finally:
        t.close()
