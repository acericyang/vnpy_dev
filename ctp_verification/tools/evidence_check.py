"""
素材覆盖自检：扫描当日日志文件，按 33 项采集任务的证据签名
输出 ✓/✗ 清单。评测日每完成一个任务块跑一次，收盘前确保全绿。

日志签名与 ctp_verification/engine.py 及网关补丁的实际日志文案
严格对齐；标注〔人工〕的项无自动签名，需人工核对截图。
"""
import argparse
import re
import sys
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

# 每项：(任务号, 标题, any 正则列表 或 order 正则对)
TASKS: list[dict] = [
    {"no": 0, "title": "连通性", "any": [r"交易服务器连接成功", r"授权验证成功"]},
    {"no": 4, "title": "系统连接成功", "any": [r"交易服务器连接成功"]},
    {"no": 1, "title": "策略开仓委托", "any": [r"【委托】.*策略:CtaStrategy_"]},
    {"no": 2, "title": "策略平仓委托〔开/平各一单人工核对〕", "any": [r"【委托】.*策略:CtaStrategy_"]},
    {"no": 3, "title": "撤单操作", "any": [r"【撤单】"]},
    {"no": 5, "title": "连接断开告警", "any": [r"交易服务器连接断开"]},
    {"no": 6, "title": "重新连接成功", "order": [r"交易服务器连接断开", r"交易服务器连接成功"]},
    {"no": 7, "title": "报单笔数统计", "any": [r"【统计】.*报单"]},
    {"no": 8, "title": "撤单笔数统计", "any": [r"【统计】"]},
    {"no": 9, "title": "重复开仓检测〔开/平人工核对〕", "any": [
        r"【预警】\[重复报单\] 检测到重复报单", r"【本地拒绝】.*重复报单拦截"]},
    {"no": 10, "title": "重复平仓检测〔开/平人工核对〕", "any": [
        r"【预警】\[重复报单\] 检测到重复报单", r"【本地拒绝】.*重复报单拦截"]},
    {"no": 11, "title": "重复撤单检测", "any": [r"【预警】\[重复撤单\] 检测到重复撤单"]},
    {"no": 12, "title": "报单阈值设置", "any": [r"【阈值设置】"]},
    {"no": 14, "title": "撤单阈值设置", "any": [r"【阈值设置】"]},
    {"no": 16, "title": "重复报单阈值设置", "any": [r"【阈值设置】"]},
    {"no": 13, "title": "报单阈值警示", "any": [r"【预警】\[报单笔数阈值\].*已达到阈值"]},
    {"no": 15, "title": "撤单阈值警示", "any": [r"【预警】\[撤单笔数阈值\].*已达到阈值"]},
    {"no": 17, "title": "重复报单阈值警示", "any": [r"【预警】\[重复报单笔数阈值\].*已达到阈值"]},
    {"no": 18, "title": "合约代码错误检测", "any": [r"【本地拒绝】.*合约代码错误"]},
    {"no": 19, "title": "最小变动价位检测", "any": [r"【本地拒绝】.*最小变动价位"]},
    {"no": 20, "title": "单笔最大手数检测", "any": [r"【本地拒绝】.*单笔最大委托量"]},
    {
        "no": 21,
        "title": "资金不足(柜台31)",
        "any": [r"【柜台错误】.*代码[：:]31", r"状态更新，31[：:]", r"资金不足"],
    },
    {
        "no": 22,
        "title": "持仓不足(柜台30)",
        "any": [r"【柜台错误】.*代码[：:]30", r"状态更新，30[：:]", r"平仓量超过持仓量"],
    },
    {
        "no": 23,
        "title": "市场状态(柜台26)",
        "any": [r"【柜台错误】.*代码[：:]26", r"状态更新，26[：:]", r"当前状态禁止"],
    },
    {"no": 24, "title": "限制交易权限/暂停〔24/25/26 三选一〕", "any": [r"程序化交易暂停"]},
    {"no": 25, "title": "暂停程序化策略〔24/25/26 三选一〕", "any": [r"【暂停策略】|【暂停交易】"]},
    {"no": 26, "title": "强制退出交易〔24/25/26 三选一,人工〕", "any": []},
    {"no": 27, "title": "批量撤单(部分成交)", "any": [], "min_cancel": 3},
    {"no": 28, "title": "批量撤单(已报)", "any": [], "min_cancel": 3},
    {
        "no": 29,
        "title": "日志-交易记录",
        "any": [r"【委托】", r"【撤单】"],
    },
    {
        "no": 30,
        "title": "日志-系统运行",
        "any": [r"交易服务器连接(成功|断开)", r"程序化交易暂停"],
    },
    {"no": 31, "title": "日志-监测信息", "any": [r"【统计】", r"【预警】"]},
    {
        "no": 32,
        "title": "日志-错误提示",
        "any": [
            r"【柜台错误】.*代码[：:]3[012]",
            r"【本地拒绝】.*(合约代码错误|最小变动价位|单笔最大委托量)",
            r"【柜台错误】",
            r"【本地拒绝】",
        ]},
]


def load_lines(log_path: Path) -> list[str]:
    """读取日志行（不存在返回空表并提示）"""
    if not log_path.exists():
        print(f"日志文件不存在: {log_path}")
        return []
    return log_path.read_text(encoding="utf-8", errors="replace").splitlines()


def first_time(line: str) -> str:
    """从日志行提取时间戳（格式 YYYY-MM-DD HH:MM:SS）"""
    if len(line) > 19 and line[4] == "-" and line[10] == " ":
        return line[:19]
    return ""


def main() -> None:
    """扫描日志并输出覆盖清单"""
    parser: argparse.ArgumentParser = argparse.ArgumentParser()
    parser.add_argument("--date", default=datetime.now().strftime("%Y%m%d"))
    parser.add_argument("--log", default="", help="显式日志路径（覆盖 --date）")
    args: argparse.Namespace = parser.parse_args()

    if args.log:
        log_path: Path = Path(args.log)
    else:
        from vnpy.trader.utility import get_folder_path

        log_path = get_folder_path("log").joinpath(
            f"client_ywbfuture_{args.date}.log"
        )

    lines: list[str] = load_lines(log_path)
    print("=" * 64)
    print(f"素材覆盖自检  日志: {log_path}")
    print(f"共 {len(lines)} 行")
    print("=" * 64)

    cancel_count: int = sum(1 for ln in lines if re.search(r"【撤单】", ln))
    missing: list[int] = []

    for task in TASKS:
        no: int = task["no"]
        title: str = task["title"]
        hit_line: str = ""
        ok: bool = False

        for pat in task.get("any", []):
            rx: re.Pattern = re.compile(pat)
            for ln in lines:
                if rx.search(ln):
                    ok = True
                    hit_line = ln
                    break
            if ok:
                break

        if not ok and "order" in task:
            first_rx: re.Pattern = re.compile(task["order"][0])
            second_rx: re.Pattern = re.compile(task["order"][1])
            first_at: int = -1
            for i, ln in enumerate(lines):
                if first_rx.search(ln):
                    first_at = i
                    break
            if first_at >= 0:
                for ln in lines[first_at + 1:]:
                    if second_rx.search(ln):
                        ok = True
                        hit_line = ln
                        break

        if not ok and "min_cancel" in task:
            ok = cancel_count >= task["min_cancel"]
            if ok:
                hit_line = f"当日【撤单】共 {cancel_count} 条"

        if "人工" in title and not task.get("any") and "min_cancel" not in task:
            mark: str = "?"
            note: str = "无自动签名，人工核对截图"
        elif ok:
            mark = "✓"
            ts: str = first_time(hit_line)
            note = f"{ts}  {hit_line[40:100].strip()}" if ts else hit_line[:80]
        else:
            mark = "✗"
            note = "当日日志未见证据签名"
            missing.append(no)
        print(f" {mark} [{no:>2}] {title}")
        if note:
            print(f"        {note}")

    print("-" * 64)
    manual: list[int] = [t["no"] for t in TASKS if "人工" in t["title"] and not t.get("any") and "min_cancel" not in t]
    print(
        f"结论：{len(TASKS) - len(missing) - len(manual)}/{len(TASKS)} 项有日志证据；"
        f"缺 {missing if missing else '无'}；"
        f"人工项 {manual}"
    )


if __name__ == "__main__":
    main()
