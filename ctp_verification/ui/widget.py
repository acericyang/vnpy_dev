"""
程序化交易监控界面（CTP 接入验证）。

单窗口五标签：
Tab1 监控总览    —— 计数卡片 + 分钟趋势柱状图 + 委托明细表
Tab2 阈值与预警  —— 阈值/重复检测/兜底手数设置
Tab3 批量撤单    —— 活动委托勾选、批量撤单警示确认、改单（撤单+重报）
Tab4 错误信息    —— 柜台错误表 + 本地拒绝记录表
Tab5 日志中心    —— 按日期/级别/关键词检索与导出

跨线程说明：引擎回调在 EventEngine 线程，本模块一律通过 QtCore.Signal
（自动 QueuedConnection）桥接到 Qt 主线程更新界面。
"""

from datetime import datetime
from pathlib import Path

import pyqtgraph as pg

from vnpy.event import Event, EventEngine
from vnpy.trader.engine import MainEngine
from vnpy.trader.object import ContractData, OrderData
from vnpy.trader.ui.qt import QtCore, QtGui, QtWidgets
from vnpy.trader.ui.widget import BaseCell, BaseMonitor, MsgCell, TimeCell
from vnpy.trader.utility import TRADER_DIR, extract_vt_symbol, get_digits

from ..constant import (
    CHANNEL_MD,
    CHANNEL_TD,
    CONN_CONNECTING,
    CONN_DISCONNECTED,
    CONN_READY,
    ConnectionEventData,
    DIRECTION_TEXT,
    EVENT_CTR_CONNECTION,
    EVENT_CTR_COUNTER_ERROR,
    EVENT_CTR_ORDER_RECORD,
    EVENT_CTR_REJECT,
    EVENT_CTR_STATS,
    EVENT_CTR_WARNING,
    OFFSET_TEXT,
    STATUS_TEXT,
    StatsData,
    WarningData,
)
from ..engine import CtrMonitorEngine


COLOR_READY: str = "#2ecc71"
COLOR_CONNECTING: str = "#f1c40f"
COLOR_DISCONNECTED: str = "#e74c3c"

STATE_COLORS: dict = {
    CONN_READY: COLOR_READY,
    CONN_CONNECTING: COLOR_CONNECTING,
    CONN_DISCONNECTED: COLOR_DISCONNECTED,
}

# 趋势图保留分钟数
TREND_MINUTES: int = 60

# 批量撤单每批笔数（防柜台流控）
BATCH_CANCEL_SIZE: int = 10

# 日志检索显示上限
LOG_DISPLAY_LIMIT: int = 5000


class StatusCell(BaseCell):
    """委托状态单元格（按状态着色）"""

    STATUS_COLORS: dict = {
        "拒单": COLOR_DISCONNECTED,
        "已撤销": "#95a5a6",
        "全部成交": COLOR_READY,
        "部分成交": "#e67e22",
        "重复报单": COLOR_DISCONNECTED,
    }

    def set_content(self, content: object, data: object) -> None:
        """按状态文本设置颜色"""
        super().set_content(content, data)
        color: str = self.STATUS_COLORS.get(str(content), "")
        if color:
            self.setForeground(QtGui.QColor(color))


class ConnStateCell(BaseCell):
    """连接状态单元格（按状态着色）"""

    def set_content(self, content: object, data: object) -> None:
        """按状态文本设置颜色"""
        super().set_content(content, data)
        color: str = STATE_COLORS.get(str(content), "")
        if color:
            self.setForeground(QtGui.QColor(color))


class OrderRecordTable(BaseMonitor):
    """委托明细表（本地请求与柜台回报合一）"""

    event_type: str = EVENT_CTR_ORDER_RECORD
    data_key: str = "vt_orderid"
    sorting: bool = False

    headers: dict = {
        "time": {"display": "时间", "cell": TimeCell, "update": True},
        "vt_orderid": {"display": "委托号", "cell": BaseCell, "update": False},
        "symbol": {"display": "代码", "cell": BaseCell, "update": False},
        "direction": {"display": "方向", "cell": BaseCell, "update": False},
        "offset": {"display": "开平", "cell": BaseCell, "update": False},
        "type": {"display": "类型", "cell": BaseCell, "update": False},
        "price": {"display": "价格", "cell": BaseCell, "update": False},
        "volume": {"display": "数量", "cell": BaseCell, "update": False},
        "status": {"display": "状态", "cell": StatusCell, "update": True},
        "source": {"display": "来源", "cell": BaseCell, "update": False},
        "remark": {"display": "备注", "cell": MsgCell, "update": True},
    }


class CounterErrorTable(BaseMonitor):
    """柜台错误表（柜台返回错误码与原文）"""

    event_type: str = EVENT_CTR_COUNTER_ERROR
    data_key: str = "key"
    sorting: bool = False

    headers: dict = {
        "time": {"display": "时间", "cell": TimeCell, "update": True},
        "gateway_name": {"display": "接口", "cell": BaseCell, "update": False},
        "error_id": {"display": "错误码", "cell": BaseCell, "update": False},
        "error_msg": {"display": "柜台信息", "cell": MsgCell, "update": False},
        "hint": {"display": "常见提示", "cell": MsgCell, "update": True},
        "related_orderid": {"display": "关联委托号", "cell": BaseCell, "update": True},
    }


class LocalRejectTable(BaseMonitor):
    """本地拒绝记录表（终端预检拦截，未发送柜台）"""

    event_type: str = EVENT_CTR_REJECT
    data_key: str = "key"
    sorting: bool = False

    headers: dict = {
        "time": {"display": "时间", "cell": TimeCell, "update": False},
        "check_item": {"display": "检查项", "cell": BaseCell, "update": False},
        "detail": {"display": "拒绝原因", "cell": MsgCell, "update": False},
        "symbol": {"display": "代码", "cell": BaseCell, "update": False},
        "request": {"display": "委托内容", "cell": MsgCell, "update": False},
        "source": {"display": "来源", "cell": BaseCell, "update": False},
    }


class ConnectionTable(BaseMonitor):
    """连接状态历史表"""

    event_type: str = EVENT_CTR_CONNECTION
    data_key: str = ""
    sorting: bool = False

    headers: dict = {
        "time": {"display": "时间", "cell": TimeCell, "update": False},
        "gateway_name": {"display": "接口", "cell": BaseCell, "update": False},
        "channel": {"display": "通道", "cell": BaseCell, "update": False},
        "state": {"display": "状态", "cell": ConnStateCell, "update": False},
        "detail": {"display": "信息", "cell": MsgCell, "update": False},
    }


class ModifyOrderDialog(QtWidgets.QDialog):
    """改单对话框（撤原单 + 重新报单）"""

    def __init__(self, order: OrderData, contract: ContractData, parent=None) -> None:
        """构造价格/数量输入表单（预填原单值）"""
        super().__init__(parent)

        self.order: OrderData = order

        self.setWindowTitle(f"改单 - {order.vt_symbol}")

        digits: int = get_digits(contract.pricetick) if contract else 2

        self.price_spin: QtWidgets.QDoubleSpinBox = QtWidgets.QDoubleSpinBox(self)
        self.price_spin.setDecimals(digits)
        if contract and contract.pricetick > 0:
            self.price_spin.setSingleStep(contract.pricetick)
        self.price_spin.setRange(0, 100000000)
        self.price_spin.setValue(order.price)

        self.volume_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox(self)
        self.volume_spin.setRange(1, 100000)
        self.volume_spin.setValue(int(order.volume))

        info_label: QtWidgets.QLabel = QtWidgets.QLabel(
            f"原单：{DIRECTION_TEXT.get(order.direction, '-')}"
            f"{OFFSET_TEXT.get(order.offset, '')} {order.volume}手 "
            f"@ {order.price}（已成交 {order.traded}手）\n"
            "改单 = 撤销原委托，待柜台撤销确认后自动按新价量重报（真实指令）；\n"
            "若原单在撤销前成交，改单自动中止并预警（防双倍仓位）"
        )

        form: QtWidgets.QFormLayout = QtWidgets.QFormLayout()
        form.addRow("新价格", self.price_spin)
        form.addRow("新数量", self.volume_spin)

        confirm_button: QtWidgets.QPushButton = QtWidgets.QPushButton("确认改单")
        confirm_button.clicked.connect(self.accept)
        cancel_button: QtWidgets.QPushButton = QtWidgets.QPushButton("取消")
        cancel_button.clicked.connect(self.reject)

        button_layout: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        button_layout.addStretch()
        button_layout.addWidget(confirm_button)
        button_layout.addWidget(cancel_button)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addWidget(info_label)
        vbox.addLayout(form)
        vbox.addLayout(button_layout)
        self.setLayout(vbox)

    def get_new_price_volume(self) -> tuple:
        """返回 (新价格, 新数量)"""
        return self.price_spin.value(), float(self.volume_spin.value())


class OverviewPanel(QtWidgets.QWidget):
    """监控总览：计数卡片 + 分钟趋势柱状图"""

    def __init__(self, ctr_engine: CtrMonitorEngine) -> None:
        """构建卡片与趋势图"""
        super().__init__()

        self.ctr_engine: CtrMonitorEngine = ctr_engine
        self._trend_signature: str = ""

        self.init_ui()

    def init_ui(self) -> None:
        """初始化界面"""
        # 计数卡片
        card_layout: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()

        self.cards: dict = {}
        cards_def: list = [
            ("order", "报单笔数"),
            ("cancel", "撤单笔数"),
            ("cancel_ok", "撤单成功"),
            ("dup", "重复报单"),
            ("local_reject", "本地拒绝"),
            ("counter_reject", "柜台拒单"),
        ]
        for key, title in cards_def:
            frame, value_label = self.create_card(title)
            self.cards[key] = value_label
            card_layout.addWidget(frame)

        # 趋势图
        self.trend_plot: pg.PlotWidget = pg.PlotWidget(title="报撤单笔数趋势（每分钟）")
        self.trend_plot.addLegend(offset=(10, 10))
        self.trend_plot.showGrid(x=True, y=True, alpha=0.3)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(card_layout)
        vbox.addWidget(self.trend_plot, stretch=1)
        self.setLayout(vbox)

    @staticmethod
    def create_card(title: str) -> tuple:
        """创建一张计数卡片，返回 (frame, 数值标签)"""
        frame: QtWidgets.QFrame = QtWidgets.QFrame()
        frame.setFrameShape(QtWidgets.QFrame.Shape.Box)
        frame.setMinimumHeight(72)

        title_label: QtWidgets.QLabel = QtWidgets.QLabel(title)
        title_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        value_label: QtWidgets.QLabel = QtWidgets.QLabel("0")
        value_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        font: QtGui.QFont = QtGui.QFont()
        font.setPointSize(16)
        font.setBold(True)
        value_label.setFont(font)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addWidget(title_label)
        vbox.addWidget(value_label)
        frame.setLayout(vbox)

        return frame, value_label

    def update_stats(self, stats: StatsData) -> None:
        """更新卡片与趋势图"""
        values: dict = {
            "order": (stats.order_count, stats.order_threshold),
            "cancel": (stats.cancel_count, stats.cancel_threshold),
            "cancel_ok": (stats.cancel_ok_count, 0),
            "dup": (stats.dup_order_count, stats.dup_order_threshold),
            "local_reject": (stats.local_reject_count, 0),
            "counter_reject": (stats.counter_reject_count, 0),
        }

        for key, label in self.cards.items():
            count, threshold = values[key]
            label.setText(str(count))
            if threshold > 0 and count >= threshold:
                label.setStyleSheet(f"color: {COLOR_DISCONNECTED};")
            else:
                label.setStyleSheet("")

        self.update_trend(stats.buckets)

    def update_trend(self, buckets: dict) -> None:
        """重建分钟趋势柱状图（最近 N 分钟）；数据未变化时跳过（洪水下防刷爆）"""
        minutes: list = sorted(buckets.keys())[-TREND_MINUTES:]
        signature: str = str(
            [(minute, buckets[minute]) for minute in minutes]
        )
        if signature == self._trend_signature:
            return
        self._trend_signature = signature

        self.trend_plot.clear()

        if not minutes:
            return

        order_heights: list = []
        cancel_heights: list = []
        dup_heights: list = []
        for minute in minutes:
            bucket: dict = buckets.get(minute, {})
            order_heights.append(bucket.get("order", 0))
            cancel_heights.append(bucket.get("cancel", 0))
            dup_heights.append(bucket.get("dup", 0))

        x: list = list(range(len(minutes)))

        order_bar: pg.BarGraphItem = pg.BarGraphItem(
            x=[i - 0.27 for i in x], height=order_heights, width=0.24,
            brush=COLOR_DISCONNECTED, name="报单",
        )
        cancel_bar: pg.BarGraphItem = pg.BarGraphItem(
            x=x, height=cancel_heights, width=0.24,
            brush="#3498db", name="撤单",
        )
        dup_bar: pg.BarGraphItem = pg.BarGraphItem(
            x=[i + 0.27 for i in x], height=dup_heights, width=0.24,
            brush="#f39c12", name="重复报单",
        )

        self.trend_plot.addItem(order_bar)
        self.trend_plot.addItem(cancel_bar)
        self.trend_plot.addItem(dup_bar)
        self.trend_plot.getAxis("bottom").setTicks([list(zip(x, minutes, strict=True))])


class SettingPanel(QtWidgets.QWidget):
    """阈值与预警设置面板"""

    def __init__(self, ctr_engine: CtrMonitorEngine) -> None:
        """构建设置表单并载入当前值"""
        super().__init__()

        self.ctr_engine: CtrMonitorEngine = ctr_engine

        self.init_ui()
        self.load_setting()

    def init_ui(self) -> None:
        """初始化表单"""
        setting: dict = self.ctr_engine.setting

        self.order_threshold_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.order_threshold_spin.setRange(0, 1000000)
        self.order_threshold_spin.setSpecialValueText("不启用")
        self.order_threshold_spin.setValue(int(setting["order_threshold"]))

        self.cancel_threshold_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.cancel_threshold_spin.setRange(0, 1000000)
        self.cancel_threshold_spin.setSpecialValueText("不启用")
        self.cancel_threshold_spin.setValue(int(setting["cancel_threshold"]))

        self.dup_threshold_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.dup_threshold_spin.setRange(0, 1000000)
        self.dup_threshold_spin.setSpecialValueText("不启用")
        self.dup_threshold_spin.setValue(int(setting["dup_order_threshold"]))

        self.dup_window_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.dup_window_spin.setRange(1, 60)
        self.dup_window_spin.setSuffix(" 秒")
        self.dup_window_spin.setValue(int(setting["dup_window_seconds"]))

        self.dup_mode_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.dup_mode_combo.addItem("提示（重复报单均真实送达柜台）", "warn")
        self.dup_mode_combo.addItem("拦截（阻止第二笔重复报单）", "block")
        index: int = 0 if setting["dup_mode"] == "warn" else 1
        self.dup_mode_combo.setCurrentIndex(index)

        self.threshold_action_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.threshold_action_combo.addItem("仅提醒", "warn")
        self.threshold_action_combo.addItem("提醒并自动暂停交易", "warn_pause")
        if setting.get("threshold_action", "warn") == "warn_pause":
            self.threshold_action_combo.setCurrentIndex(1)

        self.default_max_volume_spin: QtWidgets.QSpinBox = QtWidgets.QSpinBox()
        self.default_max_volume_spin.setRange(0, 1000000)
        self.default_max_volume_spin.setSpecialValueText("不启用")
        self.default_max_volume_spin.setValue(int(setting["default_max_volume"]))

        self.overrides_edit: QtWidgets.QPlainTextEdit = QtWidgets.QPlainTextEdit()
        self.overrides_edit.setPlaceholderText("每行一条，格式：rb2505.SHFE:500")
        overrides: dict = setting.get("max_volume_overrides", {})
        self.overrides_edit.setPlainText(
            "\n".join(f"{k}:{v}" for k, v in overrides.items())
        )

        self.warn_popup_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox("预警弹窗")
        self.warn_popup_check.setChecked(bool(setting.get("warn_popup", True)))

        self.warn_sound_check: QtWidgets.QCheckBox = QtWidgets.QCheckBox("预警声音")
        self.warn_sound_check.setChecked(bool(setting.get("warn_sound", True)))

        self.watch_edit: QtWidgets.QPlainTextEdit = QtWidgets.QPlainTextEdit()
        self.watch_edit.setPlaceholderText("每行一个 vt_symbol，如 au2612.SHFE")
        watch_symbols: list = setting.get("watch_symbols", [])
        self.watch_edit.setPlainText("\n".join(watch_symbols))

        scan_button: QtWidgets.QPushButton = QtWidgets.QPushButton("扫描活跃品种并更新清单")
        scan_button.clicked.connect(self.run_active_scan)

        save_button: QtWidgets.QPushButton = QtWidgets.QPushButton("保存并生效")
        save_button.clicked.connect(self.save_setting)

        note_label: QtWidgets.QLabel = QtWidgets.QLabel(
            "提示：重复报单处理为「提示」模式时，两笔相同委托都会真实送达柜台（可能双成交），"
            "演示重复报单功能后请及时撤销余单；单笔最大委托量优先使用柜台返回值，"
            "柜台未返回时按合约覆盖值或默认兜底值校验。"
        )
        note_label.setWordWrap(True)

        form: QtWidgets.QFormLayout = QtWidgets.QFormLayout()
        form.addRow("报单笔数预警阈值", self.order_threshold_spin)
        form.addRow("撤单笔数预警阈值", self.cancel_threshold_spin)
        form.addRow("重复报单笔数预警阈值", self.dup_threshold_spin)
        form.addRow("阈值达标动作", self.threshold_action_combo)
        form.addRow("重复检测时间窗口", self.dup_window_spin)
        form.addRow("重复报单处理模式", self.dup_mode_combo)
        form.addRow("单笔最大委托量（默认兜底）", self.default_max_volume_spin)
        form.addRow("单笔最大委托量（按合约覆盖）", self.overrides_edit)
        form.addRow("预警方式", self.warn_popup_check)
        form.addRow("", self.warn_sound_check)
        form.addRow("自选行情清单（自动订阅）", self.watch_edit)

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(form)
        vbox.addWidget(save_button)
        vbox.addWidget(scan_button)
        vbox.addWidget(note_label)
        vbox.addStretch()
        self.setLayout(vbox)

    def load_setting(self) -> None:
        """从引擎重新载入设置（占位：初始已在 init_ui 载入）"""

    def save_setting(self) -> None:
        """收集表单并更新引擎设置"""
        overrides: dict = {}
        invalid_lines: int = 0
        for line in self.overrides_edit.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            if ":" in line:
                symbol, _, value = line.rpartition(":")
                try:
                    overrides[symbol] = int(value)
                except ValueError:
                    invalid_lines += 1
            else:
                invalid_lines += 1

        setting: dict = {
            "order_threshold": self.order_threshold_spin.value(),
            "cancel_threshold": self.cancel_threshold_spin.value(),
            "dup_order_threshold": self.dup_threshold_spin.value(),
            "dup_window_seconds": self.dup_window_spin.value(),
            "dup_mode": self.dup_mode_combo.currentData(),
            "default_max_volume": self.default_max_volume_spin.value(),
            "max_volume_overrides": overrides,
            "warn_popup": self.warn_popup_check.isChecked(),
            "warn_sound": self.warn_sound_check.isChecked(),
            "threshold_action": self.threshold_action_combo.currentData(),
            "watch_symbols": [
                s.strip() for s in self.watch_edit.toPlainText().splitlines() if s.strip()
            ],
        }
        self.ctr_engine.update_setting(setting)
        # 新增的自选合约立即订阅
        self.ctr_engine.subscribe_watch_symbols()

        message: str = "阈值与预警设置已保存并生效"
        if invalid_lines:
            message += f"（忽略 {invalid_lines} 行无效的合约覆盖配置）"
        QtWidgets.QMessageBox.information(self, "设置保存", message)

    def run_active_scan(self) -> None:
        """启动后台活跃品种扫描（完成后自动更新清单并订阅）"""
        self.ctr_engine.run_active_scan()
        QtWidgets.QMessageBox.information(
            self,
            "活跃扫描",
            "扫描已启动（约 2-3 分钟，只订阅行情、不下任何委托）。\n"
            "完成后自选清单自动更新并订阅，结果见日志。",
        )


class BatchCancelPanel(QtWidgets.QWidget):
    """批量撤单与改单面板"""

    headers: list = ["勾选", "委托号", "代码", "方向", "开平", "价格", "数量", "已成交", "状态", "时间"]

    def __init__(self, main_engine: MainEngine, ctr_engine: CtrMonitorEngine) -> None:
        """构建活动委托表与操作按钮"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.ctr_engine: CtrMonitorEngine = ctr_engine

        self.checked_orders: set = set()
        self.pending_cancel_orders: list = []

        self.init_ui()

        self.refresh_timer: QtCore.QTimer = QtCore.QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh)
        self.refresh_timer.start(2000)

        self.refresh()

    def init_ui(self) -> None:
        """初始化界面"""
        self.table: QtWidgets.QTableWidget = QtWidgets.QTableWidget()
        self.table.setColumnCount(len(self.headers))
        self.table.setHorizontalHeaderLabels(self.headers)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QtWidgets.QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setColumnWidth(0, 50)

        select_submitted_button: QtWidgets.QPushButton = QtWidgets.QPushButton("全选已报")
        select_submitted_button.clicked.connect(
            lambda: self.select_by_status(["提交中", "未成交"])
        )
        select_part_button: QtWidgets.QPushButton = QtWidgets.QPushButton("全选部分成交")
        select_part_button.clicked.connect(lambda: self.select_by_status(["部分成交"]))
        deselect_button: QtWidgets.QPushButton = QtWidgets.QPushButton("全不选")
        deselect_button.clicked.connect(lambda: self.select_by_status(None))
        refresh_button: QtWidgets.QPushButton = QtWidgets.QPushButton("刷新")
        refresh_button.clicked.connect(self.refresh)
        batch_cancel_button: QtWidgets.QPushButton = QtWidgets.QPushButton("批量撤单")
        batch_cancel_button.clicked.connect(self.batch_cancel)
        modify_button: QtWidgets.QPushButton = QtWidgets.QPushButton("改单")
        modify_button.clicked.connect(self.modify_order)

        button_layout: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        for button in (
            select_submitted_button,
            select_part_button,
            deselect_button,
            refresh_button,
            batch_cancel_button,
            modify_button,
        ):
            button_layout.addWidget(button)
        button_layout.addStretch()

        self.count_label: QtWidgets.QLabel = QtWidgets.QLabel("当前活动委托 0 笔")

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addWidget(self.count_label)
        vbox.addLayout(button_layout)
        vbox.addWidget(self.table)
        self.setLayout(vbox)

    def refresh(self) -> None:
        """刷新活动委托表（保留勾选状态）"""
        self.collect_checked()

        orders: list = self.main_engine.get_all_active_orders()

        self.table.setRowCount(0)
        for order in orders:
            row: int = self.table.rowCount()
            self.table.insertRow(row)

            checked: QtCore.Qt.CheckState = (
                QtCore.Qt.CheckState.Checked
                if order.vt_orderid in self.checked_orders
                else QtCore.Qt.CheckState.Unchecked
            )
            check_item: QtWidgets.QTableWidgetItem = QtWidgets.QTableWidgetItem()
            check_item.setCheckState(checked)
            check_item.setData(QtCore.Qt.ItemDataRole.UserRole, order.vt_orderid)
            self.table.setItem(row, 0, check_item)

            values: list = [
                order.vt_orderid,
                order.vt_symbol,
                DIRECTION_TEXT.get(order.direction, "-"),
                OFFSET_TEXT.get(order.offset, ""),
                str(order.price),
                str(order.volume),
                str(order.traded),
                STATUS_TEXT.get(order.status, str(order.status)),
                order.datetime.strftime("%H:%M:%S") if order.datetime else "-",
            ]
            for column, value in enumerate(values):
                item: QtWidgets.QTableWidgetItem = QtWidgets.QTableWidgetItem(value)
                item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, column + 1, item)

        self.count_label.setText(f"当前活动委托 {len(orders)} 笔")

    def collect_checked(self) -> None:
        """收集当前勾选的委托号"""
        self.checked_orders.clear()
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == QtCore.Qt.CheckState.Checked:
                vt_orderid: str = str(item.data(QtCore.Qt.ItemDataRole.UserRole))
                self.checked_orders.add(vt_orderid)

    def select_by_status(self, statuses: list | None) -> None:
        """按状态批量勾选（None 表示全不选）"""
        status_column: int = self.headers.index("状态")
        for row in range(self.table.rowCount()):
            check_item = self.table.item(row, 0)
            if not check_item:
                continue
            if statuses is None:
                check_item.setCheckState(QtCore.Qt.CheckState.Unchecked)
            else:
                status_item = self.table.item(row, status_column)
                if status_item and status_item.text() in statuses:
                    check_item.setCheckState(QtCore.Qt.CheckState.Checked)

    def get_checked_orders(self) -> list:
        """返回勾选的且仍处于活动状态的委托对象"""
        self.collect_checked()
        orders: list = [
            order
            for order in self.main_engine.get_all_active_orders()
            if order.vt_orderid in self.checked_orders
        ]
        return orders

    def batch_cancel(self) -> None:
        """批量撤单：警示确认后分批真实撤单"""
        orders: list = self.get_checked_orders()
        if not orders:
            QtWidgets.QMessageBox.warning(self, "批量撤单", "请先勾选需要撤销的委托")
            return

        submitted_count: int = sum(
            1 for o in orders if STATUS_TEXT.get(o.status) == "未成交"
        )
        part_count: int = sum(
            1 for o in orders if STATUS_TEXT.get(o.status) == "部分成交"
        )

        result: QtWidgets.QMessageBox.StandardButton = QtWidgets.QMessageBox.warning(
            self,
            "批量撤单警示",
            f"即将对 {len(orders)} 笔委托发出撤单指令"
            f"（已报 {submitted_count} 笔、部分成交 {part_count} 笔），\n"
            "确认执行批量撤单？",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if result != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self.pending_cancel_orders = list(orders)
        self.cancel_next_batch()

    def cancel_next_batch(self) -> None:
        """发送下一批撤单指令（每批 BATCH_CANCEL_SIZE 笔，间隔 1 秒防流控）"""
        if not self.pending_cancel_orders:
            return

        batch: list = self.pending_cancel_orders[:BATCH_CANCEL_SIZE]
        self.pending_cancel_orders = self.pending_cancel_orders[BATCH_CANCEL_SIZE:]

        self.ctr_engine.batch_cancel(batch)
        self.refresh()

        if self.pending_cancel_orders:
            QtCore.QTimer.singleShot(1000, self.cancel_next_batch)

    def modify_order(self) -> None:
        """改单：选中一行后弹出改单对话框（撤原单 + 真实重报）"""
        row: int = self.table.currentRow()
        if row < 0:
            QtWidgets.QMessageBox.warning(self, "改单", "请先选中需要改单的委托")
            return

        vt_orderid: str = str(
            self.table.item(row, self.headers.index("委托号")).text()
        )
        order = self.main_engine.get_order(vt_orderid)
        if not order or not order.is_active():
            QtWidgets.QMessageBox.warning(self, "改单", f"委托 {vt_orderid} 已不在活动状态")
            return

        contract: ContractData = self.main_engine.get_contract(order.vt_symbol)

        dialog: ModifyOrderDialog = ModifyOrderDialog(order, contract, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        price, volume = dialog.get_new_price_volume()

        result: QtWidgets.QMessageBox.StandardButton = QtWidgets.QMessageBox.warning(
            self,
            "改单警示",
            f"将撤销原单 {vt_orderid} 并按 {price}/{volume} 手重新报单，确认？",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if result != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        self.ctr_engine.modify_order(order, price, volume)


class LogCenterPanel(QtWidgets.QWidget):
    """日志中心：按日期/级别/关键词检索并导出"""

    LEVELS: list = ["全部", "DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

    def __init__(self) -> None:
        """构建检索界面"""
        super().__init__()

        self.records: list = []      # (时间, 级别, 来源, 信息, 原始行)

        self.init_ui()
        self.scan_dates()

    def init_ui(self) -> None:
        """初始化界面"""
        self.date_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()

        self.level_combo: QtWidgets.QComboBox = QtWidgets.QComboBox()
        self.level_combo.addItems(self.LEVELS)
        self.level_combo.setCurrentIndex(0)
        self.level_combo.setEditable(True)

        self.keyword_edit: QtWidgets.QLineEdit = QtWidgets.QLineEdit()
        self.keyword_edit.setPlaceholderText("关键词（如：委托 / 撤单 / 预警 / 代码： / 连接）")
        self.keyword_edit.returnPressed.connect(self.search)

        search_button: QtWidgets.QPushButton = QtWidgets.QPushButton("检索")
        search_button.clicked.connect(self.search)

        export_txt_button: QtWidgets.QPushButton = QtWidgets.QPushButton("导出txt")
        export_txt_button.clicked.connect(lambda: self.export("txt"))
        export_csv_button: QtWidgets.QPushButton = QtWidgets.QPushButton("导出csv")
        export_csv_button.clicked.connect(lambda: self.export("csv"))

        filter_layout: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        filter_layout.addWidget(QtWidgets.QLabel("日期"))
        filter_layout.addWidget(self.date_combo)
        filter_layout.addWidget(QtWidgets.QLabel("级别"))
        filter_layout.addWidget(self.level_combo)
        filter_layout.addWidget(QtWidgets.QLabel("关键词"))
        filter_layout.addWidget(self.keyword_edit, stretch=1)
        filter_layout.addWidget(search_button)
        filter_layout.addWidget(export_txt_button)
        filter_layout.addWidget(export_csv_button)

        self.result_table: QtWidgets.QTableWidget = QtWidgets.QTableWidget()
        self.result_table.setColumnCount(4)
        self.result_table.setHorizontalHeaderLabels(["时间", "级别", "来源", "信息"])
        self.result_table.verticalHeader().setVisible(False)
        self.result_table.setEditTriggers(QtWidgets.QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.horizontalHeader().setStretchLastSection(True)

        self.count_label: QtWidgets.QLabel = QtWidgets.QLabel("")

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(filter_layout)
        vbox.addWidget(self.count_label)
        vbox.addWidget(self.result_table)
        self.setLayout(vbox)

    def scan_dates(self) -> None:
        """扫描日志目录，列出可选日期（最新在前）"""
        self.date_combo.clear()

        log_dir: Path = TRADER_DIR.joinpath("log")
        if not log_dir.exists():
            return

        dates: list = sorted(
            [path.stem.replace("vt_", "") for path in log_dir.glob("vt_*.log")],
            reverse=True,
        )
        for date in dates:
            self.date_combo.addItem(f"{date[:4]}-{date[4:6]}-{date[6:]}", date)

        if dates:
            self.date_combo.setCurrentIndex(0)

    def search(self) -> None:
        """按条件检索日志"""
        self.records = []

        date: str = str(self.date_combo.currentData() or "")
        if not date:
            return

        log_path: Path = TRADER_DIR.joinpath("log", f"vt_{date}.log")
        if not log_path.exists():
            QtWidgets.QMessageBox.warning(self, "日志中心", "日志文件不存在")
            return

        level: str = self.level_combo.currentText()
        keyword: str = self.keyword_edit.text().strip()

        try:
            content: str = log_path.read_text(encoding="UTF-8", errors="replace")
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "日志中心", f"读取日志失败：{e}")
            return

        for line in content.splitlines():
            parts: list = line.split(" | ", 3)
            if len(parts) != 4:
                continue

            line_time, line_level, line_source, line_msg = parts
            if level != "全部" and line_level != level:
                continue
            if keyword and keyword not in line and keyword not in line_msg:
                continue
            self.records.append((line_time, line_level, line_source, line_msg, line))

        display: list = self.records[-LOG_DISPLAY_LIMIT:]
        self.result_table.setRowCount(0)
        for record in reversed(display):
            row: int = self.result_table.rowCount()
            self.result_table.insertRow(row)
            for column, value in enumerate(record[:4]):
                item: QtWidgets.QTableWidgetItem = QtWidgets.QTableWidgetItem(value)
                if column == 3:
                    item.setTextAlignment(
                        QtCore.Qt.AlignmentFlag.AlignLeft
                        | QtCore.Qt.AlignmentFlag.AlignVCenter
                    )
                else:
                    item.setTextAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                self.result_table.setItem(row, column, item)

        total: int = len(self.records)
        text: str = f"共命中 {total} 条"
        if total > LOG_DISPLAY_LIMIT:
            text += f"，显示最近 {LOG_DISPLAY_LIMIT} 条"
        self.count_label.setText(text)

    def export(self, fmt: str) -> None:
        """导出当前检索结果"""
        if not self.records:
            QtWidgets.QMessageBox.warning(self, "日志中心", "没有可导出的检索结果")
            return

        default_name: str = f"ctr_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{fmt}"
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "导出日志", default_name, f"(*.{fmt})"
        )
        if not path:
            return

        try:
            with open(path, "w", encoding="UTF-8") as f:
                if fmt == "csv":
                    f.write("时间,级别,来源,信息\n")
                    for record in self.records:
                        values: list = [
                            v.replace(",", "，") for v in record[:4]
                        ]
                        f.write(",".join(values) + "\n")
                else:
                    for record in self.records:
                        f.write(record[4] + "\n")
        except OSError as e:
            QtWidgets.QMessageBox.warning(self, "日志中心", f"导出失败：{e}")
            return

        QtWidgets.QMessageBox.information(
            self, "日志中心", f"已导出 {len(self.records)} 条至 {path}"
        )


class CtrMonitorWidget(QtWidgets.QWidget):
    """程序化交易监控主窗口"""

    signal_warning: QtCore.Signal = QtCore.Signal(Event)
    signal_stats: QtCore.Signal = QtCore.Signal(Event)
    signal_connection: QtCore.Signal = QtCore.Signal(Event)

    def __init__(self, main_engine: MainEngine, event_engine: EventEngine) -> None:
        """构建状态条与五个功能标签页"""
        super().__init__()

        self.main_engine: MainEngine = main_engine
        self.event_engine: EventEngine = event_engine
        self.ctr_engine: CtrMonitorEngine = main_engine.get_engine("CtrMonitor")

        self.conn_states: dict = {}

        self.init_ui()
        self.register_event()

        # 主窗口行情面板双击联动（核心部件零改动，动态挂接）
        self.install_tick_bridge()

        # 共享账号 UI 防护：禁用持仓面板实时排序
        self.install_shared_account_guard()

    def init_ui(self) -> None:
        """初始化界面"""
        self.setWindowTitle("程序化交易监控")
        self.resize(1280, 800)

        # ---- 顶部状态条 ----
        self.conn_label: QtWidgets.QLabel = QtWidgets.QLabel("未连接")
        self.conn_label.setTextFormat(QtCore.Qt.TextFormat.RichText)

        self.pause_button: QtWidgets.QPushButton = QtWidgets.QPushButton("暂停交易")
        self.pause_button.setCheckable(True)
        self.pause_button.clicked.connect(self.toggle_pause)

        reset_button: QtWidgets.QPushButton = QtWidgets.QPushButton("重置统计")
        reset_button.clicked.connect(self.reset_stats)

        stop_strategy_button: QtWidgets.QPushButton = QtWidgets.QPushButton(
            "停止全部CTA策略"
        )
        stop_strategy_button.clicked.connect(self.stop_all_strategies)

        status_layout: QtWidgets.QHBoxLayout = QtWidgets.QHBoxLayout()
        status_layout.addWidget(self.conn_label, stretch=1)
        status_layout.addWidget(self.pause_button)
        status_layout.addWidget(reset_button)
        status_layout.addWidget(stop_strategy_button)

        # ---- 功能标签页 ----
        self.overview_panel: OverviewPanel = OverviewPanel(self.ctr_engine)
        self.setting_panel: SettingPanel = SettingPanel(self.ctr_engine)
        self.batch_cancel_panel: BatchCancelPanel = BatchCancelPanel(
            self.main_engine, self.ctr_engine
        )
        self.log_center_panel: LogCenterPanel = LogCenterPanel()

        error_splitter: QtWidgets.QSplitter = QtWidgets.QSplitter(
            QtCore.Qt.Orientation.Vertical
        )
        counter_error_title: QtWidgets.QLabel = QtWidgets.QLabel("柜台错误（柜台返回错误码与信息）")
        error_splitter.addWidget(counter_error_title)
        self.counter_error_table: CounterErrorTable = CounterErrorTable(
            self.main_engine, self.event_engine
        )
        error_splitter.addWidget(self.counter_error_table)
        local_reject_title: QtWidgets.QLabel = QtWidgets.QLabel(
            "本地拒绝（终端预检拦截，未发送柜台）"
        )
        error_splitter.addWidget(local_reject_title)
        self.local_reject_table: LocalRejectTable = LocalRejectTable(
            self.main_engine, self.event_engine
        )
        error_splitter.addWidget(self.local_reject_table)

        error_tab: QtWidgets.QWidget = QtWidgets.QWidget()
        error_vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout(error_tab)
        error_vbox.addWidget(error_splitter)

        monitor_splitter: QtWidgets.QSplitter = QtWidgets.QSplitter(
            QtCore.Qt.Orientation.Vertical
        )
        monitor_splitter.addWidget(self.overview_panel)
        self.order_record_table: OrderRecordTable = OrderRecordTable(
            self.main_engine, self.event_engine
        )
        monitor_splitter.addWidget(self.order_record_table)
        self.connection_table: ConnectionTable = ConnectionTable(
            self.main_engine, self.event_engine
        )
        monitor_splitter.addWidget(self.connection_table)
        monitor_splitter.setStretchFactor(0, 3)
        monitor_splitter.setStretchFactor(1, 5)
        monitor_splitter.setStretchFactor(2, 2)

        self.tab_widget: QtWidgets.QTabWidget = QtWidgets.QTabWidget()
        self.tab_widget.addTab(monitor_splitter, "监控总览")
        self.tab_widget.addTab(self.setting_panel, "阈值与预警")
        self.tab_widget.addTab(self.batch_cancel_panel, "批量撤单")
        self.tab_widget.addTab(error_tab, "错误信息")
        self.tab_widget.addTab(self.log_center_panel, "日志中心")

        vbox: QtWidgets.QVBoxLayout = QtWidgets.QVBoxLayout()
        vbox.addLayout(status_layout)
        vbox.addWidget(self.tab_widget)
        self.setLayout(vbox)

        # 回填界面打开前已产生的记录（委托明细/错误/连接历史/连接状态）
        self.backfill_tables()

    def register_event(self) -> None:
        """注册事件信号桥（EventEngine 线程 -> Qt 主线程）"""
        self.signal_warning.connect(self.show_warning)
        self.event_engine.register(EVENT_CTR_WARNING, self.signal_warning.emit)

        self.signal_stats.connect(self.process_stats)
        self.event_engine.register(EVENT_CTR_STATS, self.signal_stats.emit)

        self.signal_connection.connect(self.process_connection)
        self.event_engine.register(EVENT_CTR_CONNECTION, self.signal_connection.emit)

    # ---- 共享账号 UI 防护 ----
    def install_shared_account_guard(self) -> None:
        """禁用主窗口持仓面板的实时排序（py-spy 实证：共享账号下他人刷出
        数百条期权持仓，每 2 秒全量推送一次，PositionMonitor 每处理一行都
        触发全表重排序（O(n log n)），主线程被打满导致 UI 无响应）。

        持仓查询是账户级数据、无会话归属，无法像委托回报那样在网关层过滤；
        关闭排序后每行插入/更新是 O(1)，数百行更新毫无压力。
        如需排序：用表格右键导出 CSV 后排序（核心文件零改动，仅运行时属性）。
        """
        from vnpy.trader.ui.widget import PositionMonitor

        if not PositionMonitor.sorting:
            return
        PositionMonitor.sorting = False

        app: QtWidgets.QApplication = QtWidgets.QApplication.instance()
        if not app:
            return
        widget: QtWidgets.QWidget
        for widget in app.topLevelWidgets():
            monitor = widget.findChild(PositionMonitor)
            if monitor:
                monitor.setSortingEnabled(False)

        self.ctr_engine.write_ctr_log(
            "【共享账号防护】已禁用持仓面板实时排序（他人持仓洪水会拖死主线程，"
            "需要排序时请导出CSV）"
        )

    # ---- 行情面板双击联动交易部件 ----
    def install_tick_bridge(self) -> None:
        """给主窗口「行情」面板挂双击事件：双击一行自动填充交易部件（核心零改动）"""
        from vnpy.trader.ui.widget import TickMonitor, TradingWidget

        app: QtWidgets.QApplication = QtWidgets.QApplication.instance()
        if not app:
            return

        tick_monitor = None
        trading_widget = None
        widget: QtWidgets.QWidget
        for widget in app.topLevelWidgets():
            if tick_monitor is None:
                tick_monitor = widget.findChild(TickMonitor)
            if trading_widget is None:
                trading_widget = widget.findChild(TradingWidget)
            if tick_monitor and trading_widget:
                break

        if not tick_monitor or not trading_widget:
            return
        if getattr(tick_monitor, "_ctr_bridge", False):
            return
        tick_monitor._ctr_bridge = True        # type: ignore[attr-defined]
        tick_monitor.cellDoubleClicked.connect(
            lambda row, col: self.fill_trading_from_tick(tick_monitor, trading_widget, row)
        )

    @staticmethod
    def fill_trading_from_tick(tick_monitor, trading_widget, row: int) -> None:
        """双击行情行：交易部件填充 合约/名称/接口，价格填该合约最新价"""
        vt_symbol: str = ""
        key: str
        cells: dict
        for key, cells in tick_monitor.cells.items():
            cell = next(iter(cells.values()), None)
            if cell is not None and cell.row() == row:
                vt_symbol = key
                break
        if not vt_symbol:
            return

        symbol: str
        exchange = None
        symbol, exchange = extract_vt_symbol(vt_symbol)
        trading_widget.exchange_combo.setCurrentText(exchange.value)
        trading_widget.symbol_line.setText(symbol)
        trading_widget.set_vt_symbol()

        # 价格自动填最新价（方向/开平/类型保持用户上次选择，可再修改）
        tick = trading_widget.main_engine.get_tick(vt_symbol)
        if tick and tick.last_price > 0:
            digits: int = trading_widget.price_digits
            price_text: str = (
                f"{tick.last_price:.{digits}f}" if digits else str(tick.last_price)
            )
            trading_widget.price_line.setText(price_text)

    def backfill_tables(self) -> None:
        """回填界面打开前已产生的记录（引擎保存记录，事件只驱动增量更新）"""
        for data in self.ctr_engine.get_all_order_records():
            self.order_record_table.insert_new_row(data)

        for data in self.ctr_engine.get_all_rejects():
            self.local_reject_table.insert_new_row(data)

        for data in self.ctr_engine.get_all_counter_errors():
            self.counter_error_table.insert_new_row(data)

        for data in self.ctr_engine.get_all_conn_history():
            self.process_connection(Event(EVENT_CTR_CONNECTION, data))

    # ---- 预警 ----
    def show_warning(self, event: Event) -> None:
        """弹出预警对话框（含声音）"""
        warning: WarningData = event.data

        if self.ctr_engine.setting.get("warn_sound", True):
            QtWidgets.QApplication.beep()

        if self.ctr_engine.setting.get("warn_popup", True):
            QtWidgets.QMessageBox.warning(
                self,
                "程序化交易监控预警",
                f"【{warning.kind}】{warning.message}",
            )

    # ---- 统计 ----
    def process_stats(self, event: Event) -> None:
        """更新计数卡片、趋势图与暂停按钮状态"""
        stats: StatsData = event.data
        self.overview_panel.update_stats(stats)

        self.pause_button.blockSignals(True)
        self.pause_button.setChecked(stats.trading_paused)
        self.pause_button.blockSignals(False)
        self.update_pause_style(stats.trading_paused)

    def toggle_pause(self, checked: bool) -> None:
        """切换暂停程序化交易开关"""
        self.ctr_engine.set_trading_paused(checked)
        self.update_pause_style(checked)

    def update_pause_style(self, paused: bool) -> None:
        """更新暂停按钮样式"""
        if paused:
            self.pause_button.setText("暂停交易（已暂停）")
            self.pause_button.setStyleSheet(
                f"background-color: {COLOR_DISCONNECTED}; color: white; font-weight: bold;"
            )
        else:
            self.pause_button.setText("暂停交易")
            self.pause_button.setStyleSheet("")

    def reset_stats(self) -> None:
        """重置统计（二次确认）"""
        result: QtWidgets.QMessageBox.StandardButton = QtWidgets.QMessageBox.warning(
            self,
            "重置统计",
            "确认清零报撤单笔数统计与趋势数据？（用于新一轮验证演示）",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if result == QtWidgets.QMessageBox.StandardButton.Yes:
            self.ctr_engine.reset_stats()

    def stop_all_strategies(self) -> None:
        """停止全部 CTA 策略（二次确认）"""
        result: QtWidgets.QMessageBox.StandardButton = QtWidgets.QMessageBox.warning(
            self,
            "暂停策略",
            "确认停止全部 CTA 策略执行？（程序化策略将暂停下单）",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if result == QtWidgets.QMessageBox.StandardButton.Yes:
            self.ctr_engine.stop_all_strategies()

    # ---- 连接状态 ----
    def process_connection(self, event: Event) -> None:
        """更新连接状态标签"""
        conn: ConnectionEventData = event.data

        gateway_states: dict = self.conn_states.setdefault(
            conn.gateway_name, {CHANNEL_TD: CONN_DISCONNECTED, CHANNEL_MD: CONN_DISCONNECTED}
        )
        gateway_states[conn.channel] = conn.state

        segments: list = []
        for gateway_name, states in self.conn_states.items():
            parts: list = [f"<b>{gateway_name}</b>"]
            for channel in (CHANNEL_TD, CHANNEL_MD):
                state: str = states.get(channel, CONN_DISCONNECTED)
                color: str = STATE_COLORS.get(state, COLOR_DISCONNECTED)
                parts.append(f"{channel}：<span style='color:{color}'>{state}</span>")
            segments.append("　".join(parts))

        self.conn_label.setText("　|　".join(segments))
