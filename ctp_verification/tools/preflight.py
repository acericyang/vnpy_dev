"""
评测日环境自检（纯本地文件检查，不连柜台、不加载网关 DLL）。

全部通过才开测。任何 FAIL 按提示恢复后重跑本脚本。
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

REPO: Path = Path(__file__).resolve().parents[2]

EXPECT_USER: str = "50004711"
EXPECT_APPID: str = "client_ywbfuture_1.0.0"

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, hint: str = "") -> None:
    """登记一项检查结果"""
    results.append((name, ok, hint))


def read(path: Path) -> str:
    """读取文本（文件不存在返回空串）"""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def main() -> None:
    """执行全部检查并输出"""
    import importlib.util

    import vnpy

    vnpy_dir: Path = Path(vnpy.__file__).resolve().parent
    patches_doc: str = "按 ctp_verification/patches/README.md 恢复"

    def pkg_dir(pkg_name: str) -> Path:
        """用 find_spec 定位包目录（只解析路径，不执行包代码/不加载 DLL）"""
        spec = importlib.util.find_spec(pkg_name)
        if spec is None or spec.submodule_search_locations is None:
            return Path()
        return Path(list(spec.submodule_search_locations)[0])

    # ---- 1. 网关补丁（纯读文件，避免同进程加载两个互斥 DLL） ----
    gateway_files: dict = {
        "vnpy_ctp": pkg_dir("vnpy_ctp") / "gateway" / "ctp_gateway.py",
        "vnpy_ctptest": pkg_dir("vnpy_ctptest") / "gateway" / "ctptest_gateway.py",
    }
    ctp_src: str = ""
    for pkg, fp in gateway_files.items():
        src: str = read(fp)
        check(
            f"{pkg} 网关文件在位",
            bool(src),
            f"重装对应包或按 {patches_doc} 恢复",
        )
        if pkg == "vnpy_ctp":
            ctp_src = src
        if not src:
            continue
        check(
            f"{pkg} 会话过滤补丁5已启用",
            "SESSION_FILTER_ENABLED: bool = True" in src,
            patches_doc + "（补丁5）",
        )
        check(
            f"{pkg} 错误回报节流",
            "_ERR_THROTTLE_MAX" in src,
            patches_doc + "（补丁3）",
        )

    # ---- 2. DLL/CMD 日志脱路径（仅 vnpy_ctp 有 [穿透验证] 插桩） ----
    if ctp_src:
        check(
            "vnpy_ctp DLL日志已脱路径",
            "DLL文件: {dll_name}" in ctp_src and "DLL文件: {dll_path}" not in ctp_src,
            patches_doc + "（补丁6）",
        )
        check(
            "vnpy_ctp 子进程CMD日志已脱敏",
            "cmd = re.sub(" in ctp_src,
            patches_doc + "（补丁6）",
        )

    # ---- 3. 演示策略在位 ----
    strat_site: Path = pkg_dir("vnpy_ctastrategy") / "strategies" / "quick_turn_strategy.py"
    strat_repo: Path = REPO / "ctp_verification" / "strategies" / "quick_turn_strategy.py"
    check(
        "QuickTurnStrategy 策略文件在位",
        strat_site.exists(),
        f"从仓库备份恢复: cp {strat_repo} {strat_site}",
    )
    check(
        "仓库策略备份在位",
        strat_repo.exists(),
        "备份缺失不影响当日测试，事后补",
    )

    # ---- 4. 连接配置（评测账号 + 申报 AppID） ----
    cred_path: Path = REPO / ".vntrader" / "connect_ctp.json"
    cred_src: str = read(cred_path)
    ok_user: bool = False
    ok_appid: bool = False
    ok_password: bool = False
    if cred_src:
        import json

        try:
            cred: dict = json.loads(cred_src)
            ok_user = str(cred.get("用户名", "")) == EXPECT_USER
            ok_appid = str(cred.get("产品名称", "")) == EXPECT_APPID
            ok_password = bool(cred.get("密码", ""))
        except json.JSONDecodeError:
            pass
    check(
        f"connect_ctp.json 指向评测账号 {EXPECT_USER}",
        ok_user,
        "cp .vntrader/accounts/ctp_飞泰评测.json .vntrader/connect_ctp.json",
    )
    check(
        f"AppID = 申报值 {EXPECT_APPID}",
        ok_appid,
        "检查 产品名称 字段",
    )
    check("密码字段非空", ok_password, "编辑 connect_ctp.json")

    # ---- 5. 品牌替换生效（读 GUI 实际加载的 vnpy 包源码） ----
    ui_dir: Path = vnpy_dir / "trader" / "ui"
    check(
        "主窗口标题无 VeighNa 字样",
        "VeighNa Trader 社区版" not in read(ui_dir / "mainwindow.py"),
        "检查 vnpy/trader/ui/mainwindow.py 标题行",
    )
    check(
        "日志文件名前缀 client_ywbfuture_",
        "client_ywbfuture_" in read(vnpy_dir / "trader" / "logger.py"),
        "检查 vnpy/trader/logger.py",
    )
    check(
        "应用图标 ywb.ico 在位并被引用",
        (ui_dir / "ywb.ico").exists() and 'get_icon_path(__file__, "ywb.ico")' in read(ui_dir / "qt.py"),
        "检查 vnpy/trader/ui/{ywb.ico, qt.py}",
    )
    check(
        "图表窗口标题无 VeighNa",
        "VeighNa" not in read(vnpy_dir / "chart" / "widget.py"),
        "检查 vnpy/chart/widget.py",
    )

    # ---- 6. 引擎平仓预检开关 ----
    check(
        "平仓持仓预检开关已实现",
        '"close_position_check": True' in read(REPO / "ctp_verification" / "engine.py"),
        "检查 ctp_verification/engine.py DEFAULT_SETTING",
    )

    # ---- 输出 ----
    failed: int = 0
    for name, ok, hint in results:
        mark: str = "✓" if ok else "✗"
        if not ok:
            failed += 1
        line: str = f" {mark} {name}"
        if not ok and hint:
            line += f"   → {hint}"
        print(line)
    total: int = len(results)
    print("-" * 60)
    if failed:
        print(f"PREFLIGHT FAIL：{failed}/{total} 项未通过，恢复后重跑")
        sys.exit(1)
    print(f"PREFLIGHT PASS：{total}/{total} 项全部通过，可以开测")


if __name__ == "__main__":
    main()
