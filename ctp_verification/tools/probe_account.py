"""
只读账户探测工具（独立子进程运行）。

连接柜台 → 查询资金与持仓（账户级，含其他测试方）→ 汇总打印 → 退出。
绝不发送任何委托；独立临时运行目录 + 独立会话，不干扰 GUI 的
委托看板与报撤单统计（网关补丁5会话过滤使本进程回报不进 GUI）。

用途：评测日各任务块开始前确认可用资金是否足够开仓、
目标合约的账户级持仓（任务22选无持仓冷门合约的依据）。
"""
import argparse
import json
import tempfile
import time
from datetime import datetime
from pathlib import Path

parser: argparse.ArgumentParser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=["ctp", "ctptest"], default="ctp")
parser.add_argument(
    "--cred",
    default=str(Path.cwd().joinpath(".vntrader", "connect_ctp.json")),
)
parser.add_argument("--wait", type=int, default=60, help="查询回报等待秒数")
args: argparse.Namespace = parser.parse_args()

CRED_PATH: Path = Path(args.cred).resolve()
if not CRED_PATH.exists():
    print(f"凭据文件不存在: {CRED_PATH}")
    raise SystemExit(1)

# 独立临时运行目录（不污染 GUI 的 .vntrader，避免流文件冲突）
TMP: Path = Path(tempfile.mkdtemp(prefix="ctr_probe_"))
TMP.joinpath(".vntrader").mkdir()

import os  # noqa: E402

os.chdir(TMP)

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vnpy.event import EventEngine  # noqa: E402
from vnpy.trader.constant import Direction  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy.trader.object import AccountData, PositionData  # noqa: E402
from vnpy.trader.setting import SETTINGS  # noqa: E402

SETTINGS["log.console"] = False
SETTINGS["log.file"] = False

if args.mode == "ctptest":
    from vnpy_ctptest import CtptestGateway

    gateway_class = CtptestGateway
else:
    from vnpy_ctp import CtpGateway

    gateway_class = CtpGateway

CRED: dict = json.loads(CRED_PATH.read_text(encoding="utf-8"))

DIRECTION_TEXT: dict = {
    Direction.LONG: "多",
    Direction.SHORT: "空",
    Direction.NET: "净",
}


def fmt_account(acc: AccountData) -> str:
    """账户资金一行汇总"""
    return (
        f"账户 {acc.accountid}  权益 {acc.balance:,.2f}  "
        f"可用 {acc.available:,.2f}  冻结 {acc.frozen:,.2f}"
    )


def fmt_position(pos: PositionData) -> str:
    """持仓一行（方向/持仓/可用/今仓/冻结）"""
    return (
        f"{pos.vt_symbol:<16} {DIRECTION_TEXT.get(pos.direction, '?'):<2} "
        f"持仓 {pos.volume:>4}  可用 {pos.volume - pos.frozen:>4}  "
        f"今仓 {pos.volume - pos.yd_volume:>4}  冻结 {pos.frozen:>4}"
    )


ee: EventEngine = EventEngine()
me: MainEngine = MainEngine(ee)

try:
    me.add_gateway(gateway_class)
    me.connect(dict(CRED), gateway_class.default_name)

    gateway = me.get_gateway(gateway_class.default_name)
    deadline: float = time.time() + args.wait
    while time.time() < deadline:
        # 登录就绪后主动补查加速回报（未连接时网关内部会忽略）
        if gateway:
            gateway.query_account()
            gateway.query_position()
        time.sleep(3)
        if me.get_all_accounts() and me.get_all_positions():
            break
    time.sleep(2)  # 回报尾包

    accounts: list = me.get_all_accounts()
    positions: list = me.get_all_positions()

    print("=" * 60)
    print(f"账户探测  {datetime.now():%Y-%m-%d %H:%M:%S}  ({args.mode})")
    print("=" * 60)

    print("--- 资金 ---")
    if accounts:
        for acc in accounts:
            print(fmt_account(acc))
    else:
        print("（超时未收到资金回报）")

    print("--- 持仓（账户级，含其他测试方；冻结=他人挂单占用） ---")
    if positions:
        for pos in sorted(positions, key=lambda p: p.vt_symbol):
            print(fmt_position(pos))
        print(f"共 {len(positions)} 条持仓记录")
    else:
        print("（无持仓回报——账户无持仓，或超时）")
except Exception as e:  # noqa: BLE001
    print(f"探测异常: {e}")
finally:
    me.close()
