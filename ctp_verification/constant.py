"""
常量定义：自定义事件类型、连接状态、监控数据结构、枚举中文映射。

注意：核心枚举的 .value 会被 gettext 翻译（本机环境为英文），
本模块统一使用下方映射表，保证界面与日志文案始终为中文。
"""

from dataclasses import dataclass, field
from datetime import datetime

from vnpy.trader.constant import Direction, Offset, OrderType, Status


DIRECTION_TEXT: dict = {
    Direction.LONG: "多",
    Direction.SHORT: "空",
    Direction.NET: "净",
}

OFFSET_TEXT: dict = {
    Offset.NONE: "",
    Offset.OPEN: "开",
    Offset.CLOSE: "平",
    Offset.CLOSETODAY: "平今",
    Offset.CLOSEYESTERDAY: "平昨",
}

ORDER_TYPE_TEXT: dict = {
    OrderType.LIMIT: "限价",
    OrderType.MARKET: "市价",
    OrderType.FAK: "FAK",
    OrderType.FOK: "FOK",
    OrderType.STOP: "停止",
    OrderType.RFQ: "询价",
}

STATUS_TEXT: dict = {
    Status.SUBMITTING: "提交中",
    Status.NOTTRADED: "未成交",
    Status.PARTTRADED: "部分成交",
    Status.ALLTRADED: "全部成交",
    Status.CANCELLED: "已撤销",
    Status.REJECTED: "拒单",
}


# 自定义事件类型
EVENT_CTR_ORDER_RECORD: str = "eCtrOrderRecord"       # 委托明细行（含本地标记）
EVENT_CTR_STATS: str = "eCtrStats"                     # 统计快照（计数+分钟桶）
EVENT_CTR_WARNING: str = "eCtrWarning"                 # 预警（阈值/重复/本地拒绝/连接断开）
EVENT_CTR_REJECT: str = "eCtrReject"                   # 本地拒绝记录
EVENT_CTR_COUNTER_ERROR: str = "eCtrCounterError"      # 柜台错误行
EVENT_CTR_CONNECTION: str = "eCtrConnection"           # 连接状态变化


# 连接状态
CONN_DISCONNECTED: str = "断开"
CONN_CONNECTING: str = "连接中"
CONN_READY: str = "就绪"

# 连接通道
CHANNEL_TD: str = "交易"
CHANNEL_MD: str = "行情"


@dataclass
class OrderRecordData:
    """委托明细行（统计与回报合一）"""

    time: datetime
    vt_orderid: str
    symbol: str                 # vt_symbol
    direction: str              # 多/空/净
    offset: str                 # 开/平/平今/平昨
    type: str                   # 限价/市价/FAK/FOK
    price: float
    volume: float
    status: str                 # 已发送/未成交/部分成交/全部成交/已撤销/拒单
    source: str                 # 下单来源（reference）
    gateway_name: str = ""
    remark: str = ""            # 重复报单标记/柜台错误信息/本地拒绝原因


@dataclass
class RejectData:
    """本地预检拒绝记录"""

    key: str                    # 唯一键（用于表格原地更新）
    time: datetime
    check_item: str             # 合约代码/价格/数量/暂停交易/重复报单
    detail: str                 # 拒绝原因
    symbol: str                 # vt_symbol
    source: str                 # 下单来源
    request: str                # 委托内容摘要


@dataclass
class CounterErrorData:
    """柜台错误记录（来自网关错误日志）"""

    key: str                    # 唯一键
    time: datetime
    gateway_name: str
    error_id: int
    error_msg: str
    hint: str = ""              # 常见错误码提示
    related_orderid: str = ""   # 关联的委托号（自动匹配）


@dataclass
class ConnectionEventData:
    """连接状态变化事件"""

    time: datetime
    gateway_name: str
    channel: str                # 交易/行情
    state: str                  # 就绪/连接中/断开
    detail: str                 # 原始日志信息


@dataclass
class StatsData:
    """统计快照"""

    time: datetime
    order_count: int            # 报单笔数（真实送达柜台）
    cancel_count: int           # 撤单笔数（真实发出撤单请求）
    cancel_ok_count: int        # 撤单成功笔数（柜台回报已撤销）
    dup_order_count: int        # 重复报单笔数
    dup_cancel_count: int       # 重复撤单笔数
    local_reject_count: int     # 本地拒绝笔数
    counter_reject_count: int   # 柜台拒单笔数
    trading_paused: bool
    order_threshold: int
    cancel_threshold: int
    dup_order_threshold: int
    buckets: dict = field(default_factory=dict)    # "HH:MM" -> {"order": n, "cancel": n, "dup": n}


@dataclass
class WarningData:
    """预警信息"""

    time: datetime
    kind: str                   # 报单阈值/撤单阈值/重复报单阈值/本地拒绝/连接断开
    message: str
