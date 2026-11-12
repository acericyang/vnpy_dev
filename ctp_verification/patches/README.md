# 网关最小补丁留底（程序化交易接入验证）

> 打补丁日期：2026-11-11
> 目的：vnpy_ctptest 网关缺少接入验证所需的两类柜台数据透出——
> ① 合约的最小/最大下单手数（任务 2.7-测试点20 本地预检需要柜台真值）；
> ② 交易所层面拒单/撤单原因文字（任务 2.8 测试点 21/22/23 截图需要柜台原文）。
> 性质：**纯字段映射与日志输出，零交易逻辑改动**。所有日志走网关既有
> `write_log/write_error` → EVENT_LOG → 日志监控/日志文件 链路。

## 补丁位置

补丁直接修改 conda 环境 site-packages 中的网关文件（**环境重装会丢失，以本文件为准恢复**）：

- `C:\Users\aceri\.conda\envs\vnpy_dev\Lib\site-packages\vnpy_ctptest\gateway\ctptest_gateway.py`
- `C:\Users\aceri\.conda\envs\vnpy_dev\Lib\site-packages\vnpy_ctp\gateway\ctp_gateway.py`
  （该文件此前已含用户自加的 [穿透验证] 插桩，与本补丁无关，保留不动）

## vnpy_ctptest 补丁（3 处）

### 补丁 1 —— onRspQryInstrument 映射最小/最大委托量

`CtptestTdApi.onRspQryInstrument` 中 `ContractData(...)` 构造（原 L606 附近），
在 `pricetick=data["PriceTick"],` 之后新增两行（与 vnpy_ctp 原生写法逐字一致）：

```python
                size=data["VolumeMultiple"],
                pricetick=data["PriceTick"],
                min_volume=data["MinLimitOrderVolume"],      # ← 新增
                max_volume=data["MaxLimitOrderVolume"],      # ← 新增
                gateway_name=self.gateway_name
```

### 补丁 2 —— onRtnOrder 记录交易所撤单/拒单原因

`CtptestTdApi.onRtnOrder` 末尾（`self.sysid_orderid_map[data["OrderSysID"]] = orderid`
之后）追加（照抄 vnpy_ctp 原生 L723-729）：

```python
        # [程序化验证] 交易所层面撤单/拒单原因（非交易时段、资金不足、持仓不足等）
        status_msg: str = data["StatusMsg"]
        if (
            data["OrderStatus"] == THOST_FTDC_OST_Canceled
            and status_msg != "已撤单"
        ):
            self.gateway.write_log(f"委托 {orderid} 状态更新，{status_msg}")
```

### 补丁 3 —— 覆写两个交易所错误回报（原被基类静默丢弃，带洪水节流）

`CtptestTdApi` 中 `onRspOrderAction` 之后新增（vnpy_ctptest 文件顶部需补 `import time`）：

```python
    def onErrRtnOrderInsert(self, data: dict, error: dict) -> None:
        """[程序化验证] 交易所回报：报单被交易所拒绝（按错误码节流）"""
        write_error_throttled(self.gateway, "交易所报单拒绝", error)

    def onErrRtnOrderAction(self, data: dict, error: dict) -> None:
        """[程序化验证] 交易所回报：撤单被交易所拒绝（按错误码节流）"""
        write_error_throttled(self.gateway, "交易所撤单拒绝", error)
```

同时在模块级（`symbol_contract_map` 之后）新增节流函数：

```python
# [程序化验证] 错误回报节流：共享测试账号时柜台会向所有连接持续重推他人错误回报，
# 日志洪水会拖死界面。每个错误码每 60 秒最多打印 10 条，超出只计数并在窗口结束汇总一行。
_ERR_THROTTLE_MAX: int = 10
_ERR_THROTTLE_WINDOW: float = 60.0
_err_throttle_stat: dict[int, tuple[float, int, int]] = {}


def write_error_throttled(gateway: BaseGateway, msg: str, error: dict) -> None:
    """[程序化验证] 按错误码节流输出错误回报"""
    error_id: int = error["ErrorID"]
    now: float = time.monotonic()
    start, printed, suppressed = _err_throttle_stat.get(error_id, (now, 0, 0))

    if now - start >= _ERR_THROTTLE_WINDOW:
        if suppressed:
            gateway.write_log(
                f"[程序化验证] 错误码{error_id}上一窗口另有 {suppressed} 条同类错误回报被节流"
            )
        start, printed, suppressed = now, 0, 0

    if printed < _ERR_THROTTLE_MAX:
        gateway.write_error(msg, error)
        printed += 1
    else:
        suppressed += 1

    _err_throttle_stat[error_id] = (start, printed, suppressed)
```

> 节流背景（2026-11-12 实测）：飞泰评测账号 50004711 为多测试方共用，他人客户端在日盘
> 疯狂报撤单时，柜台把数万条 `交易所报单拒绝` 错误回报推给所有在线连接（约 400 条/秒），
> 2 分钟写 5 万行日志直接拖死 GUI。节流后每个错误码每分钟最多 10 行 + 1 行汇总。

### 补丁 4 —— "委托 xxx 状态更新"日志同样节流（vnpy_ctp 原生块 + vnpy_ctptest 补丁 2）

他人委托的交易所拒绝走 onRtnOrder Canceled+StatusMsg 路径（如 DCE 40024"下单数量不在合理范围内"），
每个委托一条 `委托 xxx 状态更新，NN:原因` 日志，同样会洪水。两网关的该日志块改为：

```python
        code_text: str = status_msg.split(":", 1)[0]
        code: int = int(code_text) if code_text.isdigit() else 0
        write_log_throttled(
            self.gateway, f"委托 {orderid} 状态更新，{status_msg}", code
        )
```

`write_log_throttled` 为模块级函数（与 `write_error_throttled` 相邻，共用
`_err_throttle_stat` 节流字典，参数为日志文本 + 错误码）。

引擎侧配套防线（`ctp_verification/engine.py`）：委托回报不再逐条推送界面（他人/回放单
`is_local=False`，仅打开监控窗时统一回填，明细上限 2000 条）；统计快照只由每秒定时器推送；
趋势图数据未变化时跳过重建。

### 补丁 5 —— 会话过滤（根治共享账号事件洪水，2026-11-12 下午 UI 三次卡死后加）

py-spy 实锤：主线程卡在核心 `ActiveOrderMonitor.process_event`——他人客户端（共享仿真账号）
每秒数百笔委托的 onRtnOrder 推送，逐条插入主窗口活动委托表，把 Qt 主线程打满。
日志节流（补丁 3/4）挡不住**事件本身**，必须在网关层丢弃他人回报。

**判定原理**：登录应答（RspUserLogin）返回本会话 `(FrontID, SessionID)`（交易核心分配、
全局唯一，同账号第二次登录必得不同 SessionID）；所有委托回报携带下单会话的同名三元组
（委托号 `1_1607532924_3` 即 FrontID_SessionID_OrderRef）。仅放行本进程历次登录会话的回报。

**总开关**：两网关模块级 `SESSION_FILTER_ENABLED: bool = True`（带 `===== 共享账号防护补丁（总开关）=====` 横幅注释）。
**验证期结束恢复原生行为任选其一**：
1. 把 `SESSION_FILTER_ENABLED` 改为 `False`（一行开关，只影响会话过滤；
   日志节流不受控于该开关，但单客户端正常使用永远达不到其阈值，等同无感）；
2. 按本文档删除所有 `[程序化验证]` 标记块；
3. 重装：`uv pip install --force-reinstall vnpy_ctp vnpy_ctptest`。

两网关 `TdApi` 同步修改（5 处，代码内均有 `[程序化验证][补丁5-N]` 标记）：

1. `[补丁5-1]` `__init__` 增加 `self.session_keys: set = set()`
   （本进程历次登录的 (FrontID, SessionID) 集合）；
2. `[补丁5-2]` `onRspUserLogin` 成功分支增加 `self.session_keys.add((self.frontid, self.sessionid))`
   ——重连后旧会话的未成交单仍能继续接收（集合不清空）；
3. `[补丁5-3]` `onRtnOrder` 解析 frontid/sessionid 后增加：

```python
        # [程序化验证][补丁5-3] 会话过滤（受总开关 SESSION_FILTER_ENABLED 控制）
        if SESSION_FILTER_ENABLED and (frontid, sessionid) not in self.session_keys:
            return
```

4. `[补丁5-4]` `onErrRtnOrderInsert/onErrRtnOrderAction` 开头增加同款过滤（受总开关控制）；
5. `[补丁5-5]` `onRtnTrade` 的 `self.sysid_orderid_map[data["OrderSysID"]]` 改为 `.get(..., "")`
   且为空即 `return`——他人成交原代码会直接 KeyError。**此条无条件生效**（纯健壮性修复，
   对自有委托行为完全一致，建议长期保留）。

**行为边界（与原生 vnpy 的差异，专用账号下均不涉及）**：
- 本进程启动前的历史会话委托回报（"柜台回放"）不再进入本地界面；
- 同一账号多进程部署时，进程间互相看不到对方的委托（原生可以看到）。

## vnpy_ctp 补丁（1 处，可选）

`CtpTdApi` 中 `onRspOrderAction`（L532-534）之后新增与上文**补丁 3 相同**的方法与节流函数
（vnpy_ctp 文件顶部已有 `import time`）。
（vnpy_ctp 原生已含 min/max 手数映射与 StatusMsg 日志，无需补丁 1/2。）

## 验证补丁生效

1. 启动任一 run 脚本并连接柜台，日志出现完整认证链即不影响原流程；
2. 查询合约后，任一合约的"单笔最大委托量"本地校验生效（任务 20）；
3. 非交易时段/资金不足下单后，日志出现"委托 xxx 状态更新，NN:原因"或
   "，代码：N，信息：原文"，且监控窗「错误信息」页同步出现对应行、
   委托明细备注回写（任务 21/22/23 截图素材）。

## 回滚

删除上述新增代码段即可（均带 `[程序化验证]` 注释或独立方法体，易于辨认）；
或重装对应包：`uv pip install --force-reinstall vnpy_ctptest==6.7.2.1 --system`。
