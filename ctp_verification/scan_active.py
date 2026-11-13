"""
活跃品种扫描脚本（独立子进程运行，由监控引擎拉起）。

只订阅行情、绝不发送任何委托；扫描近月合约 60 秒，按成交活跃度
排序取前 10 写入结果文件，引擎读取后自动订阅。
"""

import argparse
import json
import tempfile
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# 命令行参数（凭据/输出路径由引擎传入，避免依赖运行目录）
parser: argparse.ArgumentParser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=["ctp", "ctptest"], default="ctp")
parser.add_argument("--cred", required=True)
parser.add_argument("--out", required=True)
parser.add_argument("--seconds", type=int, default=60)
args: argparse.Namespace = parser.parse_args()

# 凭据/输出路径先转绝对（chdir 到临时目录后相对路径会失效）
CRED_PATH: Path = Path(args.cred).resolve()
OUT_PATH: Path = Path(args.out).resolve()

# 独立临时运行目录（不污染 GUI 的 .vntrader）
TMP: Path = Path(tempfile.mkdtemp(prefix="ctr_scan_"))
(TMP / ".vntrader").mkdir()

import os  # noqa: E402

os.chdir(TMP)

import sys  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vnpy.event import Event, EventEngine  # noqa: E402
from vnpy.trader.constant import Product  # noqa: E402
from vnpy.trader.engine import MainEngine  # noqa: E402
from vnpy.trader.event import EVENT_LOG, EVENT_TICK  # noqa: E402
from vnpy.trader.object import SubscribeRequest  # noqa: E402
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

# 各交易所流动性较好的品种前缀
SCAN_PLAN: dict = {
    "SHFE": ["cu", "al", "zn", "pb", "ni", "sn", "ss", "au", "ag", "rb", "hc", "bu", "ru", "sp", "fu"],
    "INE": ["sc", "nr", "lu", "bc"],
    "DCE": ["i", "m", "y", "p", "v", "pp", "eg", "j", "jm", "c", "a"],
    "CZCE": ["MA", "TA", "SR", "CF", "OI", "RM", "FG", "SA"],
}

# 近月候选：当前月起顺延 6 个月（YYMM）
now: datetime = datetime.now()
month_index: int = now.year * 12 + (now.month - 1)
MONTHS: set = set()
for i in range(7):
    y, m = divmod(month_index + i, 12)
    MONTHS.add(f"{y % 100:02d}{m + 1:02d}")

tick_stats: dict = defaultdict(list)      # vt_symbol -> [(ts, last, volume)]
query_done: list = []


def on_tick(event: Event) -> None:
    """收集 tick"""
    t = event.data
    tick_stats[t.vt_symbol].append((time.time(), t.last_price, t.volume))


def on_log(event: Event) -> None:
    """等待合约查询完成"""
    if "合约信息查询成功" in event.data.msg:
        query_done.append(True)


ee: EventEngine = EventEngine()
me: MainEngine = MainEngine(ee)
ee.register(EVENT_TICK, on_tick)
ee.register(EVENT_LOG, on_log)

try:
    me.add_gateway(gateway_class)
    me.connect(dict(CRED), gateway_class.default_name)

    deadline: float = time.time() + 90
    while time.time() < deadline and not query_done:
        time.sleep(1)
    if not query_done:
        result: dict = {"time": datetime.now().isoformat(), "active": False, "symbols": []}
        OUT_PATH.write_text(json.dumps(result), encoding="utf-8")
        raise SystemExit(0)

    contracts: list = []
    for c in me.get_all_contracts():
        if c.product != Product.FUTURES:
            continue
        sym: str = c.symbol.upper()
        prefixes: list = SCAN_PLAN.get(c.exchange.value, [])
        # 取最长匹配前缀（避免 pp 被 p、jm 被 j 误吃）
        best_prefix: str = ""
        for prefix in prefixes:
            if sym.startswith(prefix.upper()) and len(prefix) > len(best_prefix):
                best_prefix = prefix
        if best_prefix and sym[len(best_prefix):] in MONTHS:
            contracts.append(c)

    for c in contracts:
        req: SubscribeRequest = SubscribeRequest(symbol=c.symbol, exchange=c.exchange)
        me.subscribe(req, gateway_class.default_name)

    time.sleep(args.seconds)

    rows: list = []
    for c in contracts:
        data = tick_stats.get(c.vt_symbol, [])
        if not data:
            continue
        prices = [d[1] for d in data if d[1] > 0]
        vols = [d[2] for d in data]
        if not prices:
            continue
        rows.append((
            max(vols) - min(vols),            # 窗口内成交量增量（活跃度首要指标）
            max(prices) - min(prices),         # 价格波动
            len(data),
            c.vt_symbol,
        ))

    rows.sort(reverse=True)
    top: list = [r[3] for r in rows[:10]]
    active: bool = bool(rows and (rows[0][0] > 0 or rows[0][1] > 0))

    result = {"time": datetime.now().isoformat(), "active": active, "symbols": top}
    OUT_PATH.write_text(json.dumps(result), encoding="utf-8")
finally:
    me.close()
