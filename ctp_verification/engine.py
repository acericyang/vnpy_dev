"""
程序化交易监控引擎（CTP 接入验证）。

职责：
1. 包装 MainEngine.send_order/cancel_order —— 本地预检（暂停/合约/价位/手数）、
   重复报撤单检测、报撤单计数，通过的请求真实透传网关；
2. 解析 EVENT_LOG —— 网关连接状态机（交易/行情双通道）、柜台错误码采集
   （write_error 与"委托状态更新"两条交易所拒绝路径，含重连回放去重）；
3. 跟踪 EVENT_ORDER —— 委托回报状态、柜台拒单关联、撤单成功计数；
4. 阈值预警（latch 回滞）、分钟桶趋势、统计同日落盘恢复。

注意：本引擎的事件回调运行在 EventEngine 线程，禁止直接调用任何 Qt 界面对象；
所有界面更新通过自定义事件由 widget 的信号桥（QueuedConnection）完成。
"""

import re
import threading
import time
from datetime import datetime
from decimal import Decimal

from vnpy.event import Event, EventEngine
from vnpy.trader.constant import OrderType, Status
from vnpy.trader.engine import BaseEngine, MainEngine
from vnpy.trader.event import EVENT_LOG, EVENT_ORDER, EVENT_TIMER
from vnpy.trader.logger import INFO, WARNING
from vnpy.trader.object import (
    CancelRequest,
    ContractData,
    LogData,
    OrderData,
    OrderRequest,
)
from vnpy.trader.utility import load_json, save_json

from .constant import (
    CHANNEL_MD,
    CHANNEL_TD,
    CONN_CONNECTING,
    CONN_DISCONNECTED,
    CONN_READY,
    ConnectionEventData,
    CounterErrorData,
    DIRECTION_TEXT,
    EVENT_CTR_CONNECTION,
    EVENT_CTR_COUNTER_ERROR,
    EVENT_CTR_ORDER_RECORD,
    EVENT_CTR_REJECT,
    EVENT_CTR_STATS,
    EVENT_CTR_WARNING,
    OFFSET_TEXT,
    ORDER_TYPE_TEXT,
    OrderRecordData,
    RejectData,
    STATUS_TEXT,
    StatsData,
    WarningData,
)


SETTING_FILENAME: str = "ctr_setting.json"

# 默认设置
DEFAULT_SETTING: dict = {
    "order_threshold": 500,          # 报单笔数预警阈值（0=不启用）
    "cancel_threshold": 200,         # 撤单笔数预警阈值（0=不启用）
    "dup_order_threshold": 5,        # 重复报单笔数预警阈值（0=不启用）
    "dup_window_seconds": 5,         # 重复检测时间窗口（秒）
    "dup_mode": "warn",              # warn=提示（两笔均真实送达柜台）/ block=拦截第二笔
    "default_max_volume": 500,       # 单笔最大委托量兜底值（合约无柜台值时生效）
    "max_volume_overrides": {},      # 按合约覆盖，如 {"rb2505.SHFE": 500}
    "warn_popup": True,              # 预警弹窗
    "warn_sound": True,              # 预警声音
    "error_id_hints": {              # 常见柜台错误码提示
        "21": "该交易席位未连接到交易所（非交易时段常见）",
        "25": "撤单找不到相应报单（可能已成交或已撤销）",
        "26": "报单已全部成交或已撤销，无法再撤",
        "30": "平仓量超过持仓量（持仓不足）",
        "31": "资金不足",
        "32": "持仓不足",
        "50": "委托价格超过涨跌停板",
    },
}

# 网关错误日志格式："交易委托失败，代码：31，信息：资金不足"
ERROR_RE: re.Pattern = re.compile(r"，代码：(\d+)，信息：(.+)$")

# 交易所拒绝（委托状态更新路径）："委托 1_xxx_1 状态更新，50:已撤单报单被拒绝SHFE:价格跌破跌停板"
# （错误码在 StatusMsg 的 "NN:" 前缀；委托号直连，无需时间窗关联；
#  OrderRef 容忍空格对齐——共享测试账号时他人客户端的报单号带空格填充）
STATUS_MSG_RE: re.Pattern = re.compile(r"^委托 (.+?)\s+状态更新，(\d+):(.+)$")

# 柜台错误回放去重窗口（秒）——重连时柜台会重复推送当日错误回报
ERROR_DEDUP_SECONDS: int = 300

# 柜台错误洪水防护（共享测试账号时，柜台会向所有连接持续推送他人错误回报）
ERROR_RATE_MAX: int = 20            # 窗口内最多登记进错误表的条数
ERROR_RATE_WINDOW: int = 10         # 速率窗口（秒）
MAX_COUNTER_ERRORS: int = 2000      # 错误表保留上限（防内存/界面暴涨）
MAX_ORDER_RECORDS: int = 2000       # 委托明细保留上限（他人回放单防暴涨）

# 分钟桶保留时长（分钟）
MAX_BUCKETS: int = 240


class CtrMonitorEngine(BaseEngine):
    """程序化交易监控引擎"""

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """初始化状态并装载下单/撤单包装"""
        super().__init__(main_engine, event_engine, "CtrMonitor")

        self.lock: threading.Lock = threading.Lock()

        self.setting: dict = self.load_setting()
        self.trading_paused: bool = False

        # 统计计数
        self.order_count: int = 0
        self.cancel_count: int = 0
        self.cancel_ok_count: int = 0
        self.dup_order_count: int = 0
        self.dup_cancel_count: int = 0
        self.local_reject_count: int = 0
        self.counter_reject_count: int = 0
        self.minute_buckets: dict = {}

        # 撤单请求跟踪（区分本端主动撤单与柜台自动撤销）
        self.cancel_requested: set = set()
        self.cancel_done: set = set()

        # 重复检测指纹（monotonic 时间戳, 指纹, vt_orderid）
        self.recent_orders: list = []
        self.recent_cancels: list = []

        # 柜台错误回放去重：
        # 委托状态更新路径按 (vt_orderid, 错误码) 终身去重；
        # write_error 路径（日志行不含委托号）按 (网关, 码, 信息) 短窗口去重
        self.seen_status_errors: set = set()
        self.recent_error_keys: list = []

        # 洪水防护：速率闸时间戳与被压制计数（每分钟汇总一次）
        self.error_rate_ts: list = []
        self.suppressed_errors: int = 0

        # 委托明细与错误记录
        self.records: dict = {}            # vt_orderid -> OrderRecordData
        self.rejects: list = []            # RejectData 列表（供界面回填）
        self.counter_errors: list = []     # CounterErrorData 列表（供界面回填）
        self.conn_history: list = []       # ConnectionEventData 列表（供界面回填）
        self.recent_rejected: dict = {}    # vt_orderid -> 拒单时间（错误关联用）
        self.error_seq: int = 0            # 错误记录序号（生成唯一键）

        # 连接状态机：gateway_name -> {"td": state, "md": state}
        self.conn_state: dict = {}

        # 阈值触发锁存（防止同一阈值反复弹窗）
        self.threshold_latch: dict = {"order": False, "cancel": False, "dup": False}

        # 定时器辅助
        self.timer_count: int = 0
        self.last_minute: str = ""

        # 合约未加载提示（只记一次）
        self.contract_load_warned: bool = False

        # 装载下单/撤单包装
        self.orig_send_order = None
        self.orig_cancel_order = None
        self.install_hooks()

        # 注册事件
        self.event_engine.register(EVENT_ORDER, self.process_order_event)
        self.event_engine.register(EVENT_LOG, self.process_log_event)
        self.event_engine.register(EVENT_TIMER, self.process_timer_event)

        # 同日统计恢复
        self.load_today_stats()

        self.write_ctr_log("程序化交易监控引擎启动（下单/撤单链路已接管）")

    # ------------------------------------------------------------------
    # 下单/撤单包装
    # ------------------------------------------------------------------
    def install_hooks(self) -> None:
        """幂等包装 MainEngine 的下单与撤单方法"""
        if getattr(self.main_engine, "_ctr_wrapped", False):
            return

        self.orig_send_order = self.main_engine.send_order
        self.orig_cancel_order = self.main_engine.cancel_order
        self.main_engine.send_order = self.wrapped_send_order        # type: ignore[assignment]
        self.main_engine.cancel_order = self.wrapped_cancel_order    # type: ignore[assignment]
        self.main_engine._ctr_wrapped = True                          # type: ignore[attr-defined]

    def uninstall_hooks(self) -> None:
        """还原 MainEngine 原始方法"""
        if not getattr(self.main_engine, "_ctr_wrapped", False):
            return

        if self.orig_send_order:
            self.main_engine.send_order = self.orig_send_order       # type: ignore[assignment]
        if self.orig_cancel_order:
            self.main_engine.cancel_order = self.orig_cancel_order   # type: ignore[assignment]
        self.main_engine._ctr_wrapped = False                         # type: ignore[attr-defined]

    def wrapped_send_order(self, req: OrderRequest, gateway_name: str) -> str:
        """包装后的下单入口：预检 -> 重复检测 -> 真实透传网关 -> 计数"""
        # 1. 本地预检
        ok, reason = self.pre_check(req)
        if not ok:
            self.on_local_reject(reason, req)
            return ""

        # 2. 重复报单检测
        dup_hit: bool = self.register_order_fingerprint(req)
        if dup_hit and self.setting["dup_mode"] == "block":
            self.on_local_reject(
                f"重复报单拦截：{self.setting['dup_window_seconds']}秒内已提交过相同委托", req
            )
            return ""

        # 3. 真实发送至网关
        vt_orderid: str = self.orig_send_order(req, gateway_name)

        if not vt_orderid:
            self.write_ctr_log(
                f"【发送失败】网关未接受委托：{req.vt_symbol} "
                f"{DIRECTION_TEXT.get(req.direction, '')}{OFFSET_TEXT.get(req.offset, '')} "
                f"{req.volume}手 @ {req.price}",
                WARNING,
            )
            return vt_orderid

        # 4. 计数与记录（真实送达柜台后才计）
        now: datetime = datetime.now()
        record: OrderRecordData = OrderRecordData(
            time=now,
            vt_orderid=vt_orderid,
            symbol=req.vt_symbol,
            direction=DIRECTION_TEXT.get(req.direction, str(req.direction)),
            offset=OFFSET_TEXT.get(req.offset, str(req.offset)),
            type=ORDER_TYPE_TEXT.get(req.type, str(req.type)),
            price=req.price,
            volume=req.volume,
            status="已发送",
            source=req.reference or "-",
            gateway_name=gateway_name,
            remark="重复报单" if dup_hit else "",
        )

        with self.lock:
            self.order_count += 1
            self.add_minute_bucket("order")
            if dup_hit:
                self.dup_order_count += 1
                self.add_minute_bucket("dup")
            self.records[vt_orderid] = record

        self.write_ctr_log(
            f"【委托】{req.vt_symbol} "
            f"{DIRECTION_TEXT.get(req.direction, '')}{OFFSET_TEXT.get(req.offset, '')} "
            f"{req.volume}手 @ {req.price} {ORDER_TYPE_TEXT.get(req.type, '')}"
            f"（{gateway_name}，来源：{req.reference or '手动'}）"
        )
        self.put_event(EVENT_CTR_ORDER_RECORD, record)

        if dup_hit:
            self.fire_warning(
                "重复报单",
                f"检测到重复报单：{req.vt_symbol} "
                f"{DIRECTION_TEXT.get(req.direction, '')}{OFFSET_TEXT.get(req.offset, '')} "
                f"{req.volume}手 @ {req.price}（{self.setting['dup_window_seconds']}秒内重复提交，"
                f"第2笔已真实送达柜台）",
            )
            self.check_threshold("dup", self.dup_order_count)

        self.check_threshold("order", self.order_count)
        self.push_stats()
        return vt_orderid

    def wrapped_cancel_order(self, req: CancelRequest, gateway_name: str) -> None:
        """包装后的撤单入口：重复撤单检测 -> 真实透传网关 -> 计数"""
        vt_orderid: str = f"{gateway_name}.{req.orderid}"
        dup_hit: bool = self.register_cancel_fingerprint(vt_orderid)

        if dup_hit and self.setting["dup_mode"] == "block":
            self.on_local_reject(
                f"重复撤单拦截：{self.setting['dup_window_seconds']}秒内已对委托 "
                f"{vt_orderid} 发出撤单请求",
                None,
                symbol=req.vt_symbol,
            )
            return

        # 真实发送撤单请求至网关
        self.orig_cancel_order(req, gateway_name)

        with self.lock:
            self.cancel_count += 1
            self.add_minute_bucket("cancel")
            self.cancel_requested.add(vt_orderid)
            if dup_hit:
                self.dup_cancel_count += 1

        self.write_ctr_log(f"【撤单】撤销委托 {vt_orderid}（{gateway_name}）")

        if dup_hit:
            self.fire_warning(
                "重复撤单",
                f"检测到重复撤单指令：委托 {vt_orderid} "
                f"（{self.setting['dup_window_seconds']}秒内第2次撤单，已真实送达柜台）",
            )

        self.check_threshold("cancel", self.cancel_count)
        self.push_stats()

    # ------------------------------------------------------------------
    # 预检（任务 2.7 交易指令检查）
    # ------------------------------------------------------------------
    def pre_check(self, req: OrderRequest) -> tuple:
        """本地预检：返回 (是否通过, 拒绝原因)"""
        # 暂停交易拦截（只拦报单，不拦撤单）
        if self.trading_paused:
            return False, "程序化交易已暂停，委托被拒绝"

        contracts: dict = self.main_engine.get_all_contracts()

        # 合约信息尚未加载完成时不做参数校验（避免误拒）
        if not contracts:
            if not self.contract_load_warned:
                self.contract_load_warned = True
                self.write_ctr_log("合约信息尚未加载完成，暂跳过合约/价格/数量校验")
            return True, ""

        contract: ContractData = self.main_engine.get_contract(req.vt_symbol)

        # 合约代码检查
        if not contract:
            return False, f"合约代码错误：{req.vt_symbol} 不存在"

        # 价格检查
        if req.type in (OrderType.LIMIT, OrderType.FAK, OrderType.FOK):
            if req.price <= 0:
                return False, f"限价类委托价格无效：{req.price}"
            if contract.pricetick and contract.pricetick > 0:
                if Decimal(str(req.price)) % Decimal(str(contract.pricetick)) != 0:
                    return False, (
                        f"价格 {req.price} 不符合最小变动价位 {contract.pricetick}"
                    )

        # 数量检查
        min_volume: float = contract.min_volume or 0
        if min_volume > 0:
            if req.volume < min_volume:
                return False, f"委托数量 {req.volume} 低于最小下单量 {min_volume}"
            if Decimal(str(req.volume)) % Decimal(str(min_volume)) != 0:
                return False, f"委托数量 {req.volume} 不是最小单位 {min_volume} 的整数倍"

        max_volume = (
            contract.max_volume
            or self.setting["max_volume_overrides"].get(req.vt_symbol)
            or self.setting["default_max_volume"]
        )
        if max_volume and max_volume > 0 and req.volume > max_volume:
            return False, f"委托数量 {req.volume} 超过单笔最大委托量 {max_volume}"

        return True, ""

    def register_order_fingerprint(self, req: OrderRequest) -> bool:
        """登记报单指纹，返回是否命中窗口内重复"""
        fingerprint: tuple = (
            req.vt_symbol,
            req.direction.name,
            req.offset.name,
            str(req.price),
            str(req.volume),
        )
        now: float = time.monotonic()
        window: float = float(self.setting["dup_window_seconds"])

        dup_hit: bool = False
        with self.lock:
            self.recent_orders = [
                (ts, fp) for ts, fp in self.recent_orders if now - ts <= window
            ]
            for _, fp in self.recent_orders:
                if fp == fingerprint:
                    dup_hit = True
                    break
            self.recent_orders.append((now, fingerprint))

        return dup_hit

    def register_cancel_fingerprint(self, vt_orderid: str) -> bool:
        """登记撤单指纹，返回是否命中窗口内重复"""
        now: float = time.monotonic()
        window: float = float(self.setting["dup_window_seconds"])

        dup_hit: bool = False
        with self.lock:
            self.recent_cancels = [
                (ts, oid) for ts, oid in self.recent_cancels if now - ts <= window
            ]
            for _, oid in self.recent_cancels:
                if oid == vt_orderid:
                    dup_hit = True
                    break
            self.recent_cancels.append((now, vt_orderid))

        return dup_hit

    def on_local_reject(
        self,
        reason: str,
        req: OrderRequest | None,
        symbol: str = "",
    ) -> None:
        """记录本地拒绝：计数 + 事件 + 日志 + 弹窗预警"""
        with self.lock:
            self.local_reject_count += 1
            self.error_seq += 1
            key: str = f"reject-{self.error_seq}"

        if req:
            check_item: str = self.classify_reject(reason)
            request_text: str = (
                f"{req.vt_symbol} {DIRECTION_TEXT.get(req.direction, '')}"
                f"{OFFSET_TEXT.get(req.offset, '')} "
                f"{req.volume}手 @ {req.price} {ORDER_TYPE_TEXT.get(req.type, '')}"
            )
            symbol = req.vt_symbol
            source: str = req.reference or "-"
        else:
            check_item = self.classify_reject(reason)
            request_text = "-"
            source = "-"

        reject: RejectData = RejectData(
            key=key,
            time=datetime.now(),
            check_item=check_item,
            detail=reason,
            symbol=symbol,
            source=source,
            request=request_text,
        )

        with self.lock:
            self.rejects.append(reject)

        self.put_event(EVENT_CTR_REJECT, reject)
        self.write_ctr_log(f"【本地拒绝】{reason}（{request_text}）", WARNING)
        self.fire_warning("本地拒绝", f"{reason}（委托未发送至柜台）")
        self.push_stats()

    @staticmethod
    def classify_reject(reason: str) -> str:
        """根据拒绝原因归类检查项"""
        if "合约代码" in reason:
            return "合约代码"
        if "变动价位" in reason or "价格" in reason:
            return "委托价格"
        if "数量" in reason or "委托量" in reason:
            return "委托数量"
        if "暂停" in reason:
            return "暂停交易"
        if "重复撤单" in reason:
            return "重复撤单"
        if "重复报单" in reason:
            return "重复报单"
        return "其他"

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------
    def process_order_event(self, event: Event) -> None:
        """委托回报：更新明细、柜台拒单计数、撤单成功计数"""
        order: OrderData = event.data
        status_text: str = STATUS_TEXT.get(order.status, str(order.status))

        with self.lock:
            record: OrderRecordData = self.records.get(order.vt_orderid, None)

            if not record:
                # 非本端会话发出（共享账号他人委托/柜台回放）也纳入明细，
                # 但不计入报单笔数、不实时推送界面（打开监控窗时统一回填，
                # 否则他人洪水回报会以每单一次表格插入+界面刷新拖死 UI）
                record = OrderRecordData(
                    time=order.datetime or datetime.now(),
                    vt_orderid=order.vt_orderid,
                    symbol=order.vt_symbol,
                    direction=DIRECTION_TEXT.get(order.direction, "-"),
                    offset=OFFSET_TEXT.get(order.offset, ""),
                    type=ORDER_TYPE_TEXT.get(order.type, ""),
                    price=order.price,
                    volume=order.volume,
                    status=status_text,
                    source=order.reference or "-",
                    gateway_name=order.gateway_name,
                    remark="柜台回放",
                    is_local=False,
                )
                self.records[order.vt_orderid] = record
                # 洪水防护：超出上限丢弃最旧记录
                if len(self.records) > MAX_ORDER_RECORDS:
                    for old_orderid in list(self.records):
                        self.records.pop(old_orderid)
                        break
            else:
                record.status = status_text
                record.time = order.datetime or record.time

            if order.status == Status.REJECTED and record.is_local:
                self.counter_reject_count += 1
                self.recent_rejected[order.vt_orderid] = datetime.now()
                if not record.remark:
                    record.remark = "柜台拒单（详见错误信息页）"

            if (
                order.status == Status.CANCELLED
                and order.vt_orderid in self.cancel_requested
                and order.vt_orderid not in self.cancel_done
            ):
                self.cancel_done.add(order.vt_orderid)
                self.cancel_ok_count += 1

        # 统计快照由定时器每秒推送，此处不推（他人回报洪水会刷爆界面）；
        # 他人/回放委托不实时推送界面（打开监控窗时统一回填）
        if record.is_local:
            self.put_event(EVENT_CTR_ORDER_RECORD, record)

    def process_log_event(self, event: Event) -> None:
        """网关日志：连接状态机 + 柜台错误采集（只匹配网关固定中文文案）"""
        log: LogData = event.data

        # 只解析已注册网关来源的日志，避免本引擎/MainEngine 自身日志
        # 含相同关键字被再次解析（否则断开告警日志会自我触发、无限增殖）
        if log.gateway_name not in self.main_engine.gateways:
            return

        msg: str = log.msg
        gateway_name: str = log.gateway_name

        # ---- 连接状态 ----
        if "服务器连接断开" in msg:
            channel: str = CHANNEL_TD if "交易服务器" in msg else CHANNEL_MD
            self.update_conn_state(gateway_name, channel, CONN_DISCONNECTED, msg)
            self.fire_warning(
                "连接断开",
                f"{gateway_name} {channel}服务器连接断开：{msg}",
            )
            return

        if "服务器连接成功" in msg:
            channel = CHANNEL_TD if "交易服务器" in msg else CHANNEL_MD
            states: dict = self.conn_state.setdefault(
                gateway_name, {CHANNEL_TD: CONN_DISCONNECTED, CHANNEL_MD: CONN_DISCONNECTED}
            )
            if states.get(channel) != CONN_READY:
                self.update_conn_state(gateway_name, channel, CONN_CONNECTING, msg)
            return

        if "行情服务器登录成功" in msg:
            self.update_conn_state(gateway_name, CHANNEL_MD, CONN_READY, msg)
            return

        if "合约信息查询成功" in msg:
            # 交易链路完全就绪的标志
            self.update_conn_state(gateway_name, CHANNEL_TD, CONN_READY, msg)
            return

        # ---- 柜台错误路径一：write_error（"交易委托失败，代码：N，信息：..."）----
        m = ERROR_RE.search(msg)
        if m:
            error_id: int = int(m.group(1))
            error_msg: str = m.group(2)
            if self.register_error_fingerprint(gateway_name, error_id, error_msg):
                if self.allow_error_rate():
                    error = self.add_counter_error(gateway_name, error_id, error_msg)
                    self.correlate_error_to_record(error)
            return

        # ---- 柜台错误路径二：委托状态更新（交易所拒绝，错误码在 StatusMsg "NN:" 前缀）----
        m2 = STATUS_MSG_RE.match(msg)
        if m2:
            orderid: str = m2.group(1).strip()
            error_id = int(m2.group(2))
            error_msg = m2.group(3)
            vt_orderid: str = f"{gateway_name}.{orderid}"

            with self.lock:
                dedup_key: tuple = (vt_orderid, error_id)
                if dedup_key in self.seen_status_errors:
                    return
                self.seen_status_errors.add(dedup_key)

                record: OrderRecordData = self.records.get(vt_orderid, None)
                # 回放的旧委托只入表留证，不计入本会话柜台拒单数
                is_replay: bool = record is None or record.remark == "柜台回放"
                if record and not is_replay:
                    self.counter_reject_count += 1
                if record and not record.remark.startswith("柜台拒单"):
                    suffix: str = "（回放）" if is_replay and record.remark == "柜台回放" else ""
                    record.remark = f"柜台拒单：代码{error_id} {error_msg}{suffix}"

            if self.allow_error_rate():
                self.add_counter_error(
                    gateway_name, error_id, error_msg, related_orderid=vt_orderid
                )
            if record:
                self.put_event(EVENT_CTR_ORDER_RECORD, record)

    def allow_error_rate(self) -> bool:
        """柜台错误速率闸：窗口内超过上限的推送只压制计数，不进表不写日志"""
        now: float = time.monotonic()

        with self.lock:
            self.error_rate_ts = [
                ts for ts in self.error_rate_ts
                if now - ts <= ERROR_RATE_WINDOW
            ]
            if len(self.error_rate_ts) >= ERROR_RATE_MAX:
                self.suppressed_errors += 1
                return False
            self.error_rate_ts.append(now)

        return True

    def register_error_fingerprint(
        self, gateway_name: str, error_id: int, error_msg: str
    ) -> bool:
        """柜台错误去重指纹（重连时柜台会在数分钟内重复推送相同错误回报）"""
        key: tuple = (gateway_name, error_id, error_msg)
        now: float = time.monotonic()

        with self.lock:
            self.recent_error_keys = [
                (ts, k) for ts, k in self.recent_error_keys
                if now - ts <= ERROR_DEDUP_SECONDS
            ]
            for _, k in self.recent_error_keys:
                if k == key:
                    return False
            self.recent_error_keys.append((now, key))

        return True

    def add_counter_error(
        self,
        gateway_name: str,
        error_id: int,
        error_msg: str,
        related_orderid: str = "",
    ) -> CounterErrorData:
        """登记柜台错误：入错误表 + 【柜台错误】WARNING 日志留痕"""
        with self.lock:
            self.error_seq += 1
            key: str = f"error-{self.error_seq}"

        error: CounterErrorData = CounterErrorData(
            key=key,
            time=datetime.now(),
            gateway_name=gateway_name,
            error_id=error_id,
            error_msg=error_msg,
            hint=str(self.setting["error_id_hints"].get(str(error_id), "")),
            related_orderid=related_orderid,
        )
        with self.lock:
            self.counter_errors.append(error)
            # 洪水防护：超出上限丢弃最旧记录
            if len(self.counter_errors) > MAX_COUNTER_ERRORS:
                del self.counter_errors[:len(self.counter_errors) - MAX_COUNTER_ERRORS]
        self.put_event(EVENT_CTR_COUNTER_ERROR, error)

        suffix: str = f"（委托 {related_orderid}）" if related_orderid else ""
        self.write_ctr_log(
            f"【柜台错误】{gateway_name}，代码：{error_id}，信息：{error_msg}{suffix}",
            WARNING,
        )
        return error

    def update_conn_state(
        self, gateway_name: str, channel: str, state: str, detail: str
    ) -> None:
        """更新连接状态并广播事件"""
        with self.lock:
            states: dict = self.conn_state.setdefault(
                gateway_name,
                {CHANNEL_TD: CONN_DISCONNECTED, CHANNEL_MD: CONN_DISCONNECTED},
            )
            states[channel] = state

        event_data: ConnectionEventData = ConnectionEventData(
            time=datetime.now(),
            gateway_name=gateway_name,
            channel=channel,
            state=state,
            detail=detail,
        )
        with self.lock:
            self.conn_history.append(event_data)
        self.put_event(EVENT_CTR_CONNECTION, event_data)

        if state == CONN_READY:
            self.write_ctr_log(
                f"{gateway_name} {channel}通道就绪", INFO
            )
        elif state == CONN_DISCONNECTED:
            self.write_ctr_log(f"{gateway_name} {channel}通道断开：{detail}", WARNING)

    def correlate_error_to_record(self, error: CounterErrorData) -> None:
        """将柜台错误关联到 3 秒内同网关的最近一笔拒单委托"""
        now: datetime = datetime.now()

        updated: OrderRecordData | None = None
        with self.lock:
            candidates: list = []
            for vt_orderid, reject_time in self.recent_rejected.items():
                record: OrderRecordData = self.records.get(vt_orderid)
                if not record:
                    continue
                if record.gateway_name != error.gateway_name:
                    continue
                if abs((now - reject_time).total_seconds()) > 3:
                    continue
                if record.remark.startswith("柜台拒单：代码"):
                    continue
                candidates.append((reject_time, record))

            if candidates:
                candidates.sort(key=lambda item: item[0])
                _, updated = candidates[-1]
                updated.remark = f"柜台拒单：代码{error.error_id} {error.error_msg}"
                error.related_orderid = updated.vt_orderid

        if updated:
            self.put_event(EVENT_CTR_ORDER_RECORD, updated)

    def process_timer_event(self, event: Event) -> None:
        """定时维护：指纹清理、分钟统计日志、统计落盘、界面快照"""
        self.timer_count += 1
        now: float = time.monotonic()
        window: float = float(self.setting["dup_window_seconds"]) + 1

        # 清理过期指纹
        with self.lock:
            self.recent_orders = [
                (ts, fp) for ts, fp in self.recent_orders if now - ts <= window
            ]
            self.recent_cancels = [
                (ts, oid) for ts, oid in self.recent_cancels if now - ts <= window
            ]
            # 分钟桶裁剪
            if len(self.minute_buckets) > MAX_BUCKETS:
                for minute in sorted(self.minute_buckets)[:-MAX_BUCKETS]:
                    self.minute_buckets.pop(minute)

        # 分钟切换时输出上一分钟统计摘要
        current_minute: str = datetime.now().strftime("%H:%M")
        if self.last_minute and current_minute != self.last_minute:
            self.log_minute_summary(self.last_minute)
        self.last_minute = current_minute

        # 每 60 秒统计落盘 + 洪水压制汇总
        if self.timer_count % 60 == 0:
            self.save_today_stats()
            with self.lock:
                suppressed: int = self.suppressed_errors
                self.suppressed_errors = 0
            if suppressed:
                self.write_ctr_log(
                    f"【柜台错误】过去1分钟压制了 {suppressed} 条柜台错误洪水推送"
                    f"（共享测试账号他人回报），仅按速率上限采样记录",
                    WARNING,
                )

        # 每秒推送统计快照（驱动界面刷新）
        self.push_stats()

    def log_minute_summary(self, minute: str) -> None:
        """输出指定分钟的统计摘要日志"""
        with self.lock:
            bucket: dict = self.minute_buckets.get(minute, {})

        if not bucket:
            return

        self.write_ctr_log(
            f"【统计】{minute} 报单 {bucket.get('order', 0)} 笔，"
            f"撤单 {bucket.get('cancel', 0)} 笔，重复报单 {bucket.get('dup', 0)} 笔"
        )

    def add_minute_bucket(self, kind: str) -> None:
        """分钟桶计数（调用方需持有锁）；kind: order/cancel/dup"""
        minute: str = datetime.now().strftime("%H:%M")
        bucket: dict = self.minute_buckets.setdefault(
            minute, {"order": 0, "cancel": 0, "dup": 0}
        )
        bucket[kind] = bucket.get(kind, 0) + 1

    # ------------------------------------------------------------------
    # 阈值预警（任务 2.6）
    # ------------------------------------------------------------------
    def check_threshold(self, kind: str, count: int) -> None:
        """阈值检查：达标触发一次预警（latch），回落解除可再触发"""
        threshold: int = {
            "order": self.setting["order_threshold"],
            "cancel": self.setting["cancel_threshold"],
            "dup": self.setting["dup_order_threshold"],
        }.get(kind, 0)

        if threshold <= 0:
            return

        if count < threshold:
            self.threshold_latch[kind] = False
            return

        if self.threshold_latch[kind]:
            return

        self.threshold_latch[kind] = True
        label: str = {
            "order": "报单笔数",
            "cancel": "撤单笔数",
            "dup": "重复报单笔数",
        }[kind]
        self.fire_warning(
            f"{label}阈值",
            f"{label}已达到阈值 {threshold}（当前 {count} 笔），请关注交易频率",
        )

    def fire_warning(self, kind: str, message: str) -> None:
        """发出预警：事件（界面弹窗/高亮）+ WARNING 日志（留痕）"""
        warning: WarningData = WarningData(
            time=datetime.now(),
            kind=kind,
            message=message,
        )
        self.put_event(EVENT_CTR_WARNING, warning)
        self.write_ctr_log(f"【预警】[{kind}] {message}", WARNING)

    # ------------------------------------------------------------------
    # 界面动作入口（widget 在 Qt 主线程调用）
    # ------------------------------------------------------------------
    def set_trading_paused(self, paused: bool) -> None:
        """暂停/恢复程序化交易（只拦报单，不拦撤单）"""
        self.trading_paused = paused
        action: str = "开启" if paused else "关闭"
        self.write_ctr_log(f"【暂停交易】程序化交易暂停开关已{action}", WARNING if paused else INFO)
        self.push_stats()

    def reset_stats(self) -> None:
        """重置统计（清空计数与分钟桶，保留明细记录）"""
        with self.lock:
            self.order_count = 0
            self.cancel_count = 0
            self.cancel_ok_count = 0
            self.dup_order_count = 0
            self.dup_cancel_count = 0
            self.local_reject_count = 0
            self.counter_reject_count = 0
            self.minute_buckets = {}
            self.cancel_requested.clear()
            self.cancel_done.clear()
            self.threshold_latch = {"order": False, "cancel": False, "dup": False}

        self.write_ctr_log("【统计重置】报撤单笔数统计已清零")
        self.save_today_stats()
        self.push_stats()

    def batch_cancel(self, orders: list) -> None:
        """批量撤单（每笔真实送达柜台）"""
        for order in orders:
            req: CancelRequest = order.create_cancel_request()
            self.main_engine.cancel_order(req, order.gateway_name)

    def modify_order(self, order: OrderData, price: float, volume: float) -> str:
        """改单 = 真实撤原单 + 真实重新报单（暂停交易期间整体拒绝，防撤旧单后新单被拦的裸仓态）"""
        if self.trading_paused:
            self.on_local_reject(
                "程序化交易已暂停，改单被拒绝（撤单与重报整体不执行）",
                None,
                symbol=order.vt_symbol,
            )
            return ""

        req: CancelRequest = order.create_cancel_request()
        self.main_engine.cancel_order(req, order.gateway_name)

        new_req: OrderRequest = OrderRequest(
            symbol=order.symbol,
            exchange=order.exchange,
            direction=order.direction,
            type=order.type,
            volume=volume,
            price=price,
            offset=order.offset,
            reference="CtrModifyOrder",
        )
        self.write_ctr_log(
            f"【改单】撤销原单 {order.vt_orderid}，重新报单 {price}/{volume} 手"
        )
        return self.main_engine.send_order(new_req, order.gateway_name)

    def stop_all_strategies(self) -> None:
        """停止全部 CTA 策略（暂停程序化策略执行）"""
        engine = self.main_engine.get_engine("CtaStrategy")
        if engine:
            self.write_ctr_log("【暂停策略】停止全部CTA策略执行", WARNING)
            engine.stop_all_strategies()    # type: ignore[attr-defined]
        else:
            self.write_ctr_log("未找到CTA策略引擎（未加载 vnpy_ctastrategy）", WARNING)

    # ------------------------------------------------------------------
    # 设置与持久化
    # ------------------------------------------------------------------
    def load_setting(self) -> dict:
        """读取设置，缺失项用默认值补齐"""
        setting: dict = load_json(SETTING_FILENAME)
        result: dict = dict(DEFAULT_SETTING)
        result.update(setting)
        return result

    def update_setting(self, setting: dict) -> None:
        """更新并保存设置"""
        with self.lock:
            self.setting.update(setting)
            save_json(SETTING_FILENAME, self.setting)

        self.write_ctr_log(
            f"【阈值设置】报单阈值 {self.setting['order_threshold']}，"
            f"撤单阈值 {self.setting['cancel_threshold']}，"
            f"重复报单阈值 {self.setting['dup_order_threshold']}，"
            f"重复处理模式 {self.setting['dup_mode']}"
        )
        self.push_stats()

    def save_today_stats(self) -> None:
        """统计落盘（同日重启恢复）"""
        with self.lock:
            data: dict = {
                "date": datetime.now().strftime("%Y-%m-%d"),
                "order_count": self.order_count,
                "cancel_count": self.cancel_count,
                "cancel_ok_count": self.cancel_ok_count,
                "dup_order_count": self.dup_order_count,
                "dup_cancel_count": self.dup_cancel_count,
                "local_reject_count": self.local_reject_count,
                "counter_reject_count": self.counter_reject_count,
                "minute_buckets": self.minute_buckets,
            }
        save_json(f"ctr_stats_{datetime.now().strftime('%Y%m%d')}.json", data)

    def load_today_stats(self) -> None:
        """同日启动时恢复统计"""
        filename: str = f"ctr_stats_{datetime.now().strftime('%Y%m%d')}.json"
        data: dict = load_json(filename)

        if not data or data.get("date") != datetime.now().strftime("%Y-%m-%d"):
            return

        with self.lock:
            self.order_count = data.get("order_count", 0)
            self.cancel_count = data.get("cancel_count", 0)
            self.cancel_ok_count = data.get("cancel_ok_count", 0)
            self.dup_order_count = data.get("dup_order_count", 0)
            self.dup_cancel_count = data.get("dup_cancel_count", 0)
            self.local_reject_count = data.get("local_reject_count", 0)
            self.counter_reject_count = data.get("counter_reject_count", 0)
            self.minute_buckets = data.get("minute_buckets", {})

        self.write_ctr_log(
            f"已恢复今日统计：报单 {self.order_count} 笔，撤单 {self.cancel_count} 笔"
        )

    # ------------------------------------------------------------------
    # 通用
    # ------------------------------------------------------------------
    def get_all_order_records(self) -> list:
        """返回全部委托明细（供界面打开时回填）"""
        with self.lock:
            return list(self.records.values())

    def get_all_rejects(self) -> list:
        """返回全部本地拒绝记录（供界面打开时回填）"""
        with self.lock:
            return list(self.rejects)

    def get_all_counter_errors(self) -> list:
        """返回全部柜台错误记录（供界面打开时回填）"""
        with self.lock:
            return list(self.counter_errors)

    def get_all_conn_history(self) -> list:
        """返回全部连接状态历史（供界面打开时回填）"""
        with self.lock:
            return list(self.conn_history)

    def put_event(self, event_type: str, data) -> None:
        """发送自定义事件"""
        event: Event = Event(event_type, data)
        self.event_engine.put(event)

    def push_stats(self) -> None:
        """推送统计快照"""
        with self.lock:
            stats: StatsData = StatsData(
                time=datetime.now(),
                order_count=self.order_count,
                cancel_count=self.cancel_count,
                cancel_ok_count=self.cancel_ok_count,
                dup_order_count=self.dup_order_count,
                dup_cancel_count=self.dup_cancel_count,
                local_reject_count=self.local_reject_count,
                counter_reject_count=self.counter_reject_count,
                trading_paused=self.trading_paused,
                order_threshold=int(self.setting["order_threshold"]),
                cancel_threshold=int(self.setting["cancel_threshold"]),
                dup_order_threshold=int(self.setting["dup_order_threshold"]),
                buckets=dict(self.minute_buckets),
            )
        self.put_event(EVENT_CTR_STATS, stats)

    def write_ctr_log(self, msg: str, level: int = INFO) -> None:
        """输出本引擎日志（进入日志监控与日志文件）"""
        log: LogData = LogData(
            msg=msg, gateway_name="CtrMonitor", level=level
        )
        event: Event = Event(EVENT_LOG, log)
        self.event_engine.put(event)

    def close(self) -> None:
        """引擎关闭：还原包装并落盘"""
        self.save_today_stats()
        self.uninstall_hooks()
        super().close()
