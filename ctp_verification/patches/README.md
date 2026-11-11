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

### 补丁 3 —— 覆写两个交易所错误回报（原被基类静默丢弃）

`CtptestTdApi` 中 `onRspOrderAction` 之后新增：

```python
    def onErrRtnOrderInsert(self, data: dict, error: dict) -> None:
        """[程序化验证] 交易所回报：报单被交易所拒绝"""
        self.gateway.write_error("交易所报单拒绝", error)

    def onErrRtnOrderAction(self, data: dict, error: dict) -> None:
        """[程序化验证] 交易所回报：撤单被交易所拒绝"""
        self.gateway.write_error("交易所撤单拒绝", error)
```

## vnpy_ctp 补丁（1 处，可选）

`CtpTdApi` 中 `onRspOrderAction`（L532-534）之后新增与上文**补丁 3 相同**的两个方法。
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
