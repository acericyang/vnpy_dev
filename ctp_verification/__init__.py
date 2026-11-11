"""
程序化交易监控应用（CTP 接入验证）。

覆盖任务清单（ctr_verification/task_config.yaml）中需要客户端实现的功能点：
连接状态监测、报撤单笔数统计与趋势、重复报撤单检测、阈值设置与预警、
交易指令本地预检（合约代码/最小变动价位/单笔最大手数）、柜台错误信息展示、
暂停程序化交易、批量撤单与改单、日志中心检索导出。

所有下单/撤单均为真实交易逻辑：通过预检的请求真实透传 CTP 网关送达柜台，
统计计数只来源于真实发出的请求与柜台回报。
"""

from pathlib import Path

from vnpy.trader.app import BaseApp

from .engine import CtrMonitorEngine


APP_NAME: str = "CtrMonitor"


class CtrMonitorApp(BaseApp):
    """程序化交易监控应用"""

    app_name: str = APP_NAME
    app_module: str = __module__
    app_path: Path = Path(__file__).parent
    display_name: str = "程序化交易监控"
    engine_class = CtrMonitorEngine
    widget_name: str = "CtrMonitorWidget"
    icon_name: str = ""
