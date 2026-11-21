# Python SDK Guide

`xingshu-bci` 是 XingShu BCI 的官方 Python SDK，有两种用法：

- **直连设备**（`xingshu_bci.pro`）：脚本直接通过网线连接 XingShuN1 Pro，不需要桌面平台，可获取最高 16 kHz 的全速率原始数据，并修改采样率、增益、输入类型、BIAS 等全部硬件设置。
- **通过桌面平台**（`XingShuClient`）：连接桌面应用的本地 API，使用平台的滤波、分析、录制和实验功能。

两种用法在同一个包里，互不影响。

## 安装

使用 Python 3.10 或更高版本。下载 [SDK 1.0.0 源码 ZIP](https://github.com/XingShuAiCompany/xingshu-bci-python/archive/refs/tags/v1.0.0.zip) 并解压。在包含 pyproject.toml 的目录打开 PowerShell，运行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[device]"
.\.venv\Scripts\python.exe -c "import xingshu_bci as x; print(x.__version__)"
```

最后一行应显示 1.0.0。后续运行示例请使用同一个虚拟环境的 Python。

## 直连 XingShuN1 Pro（不需要桌面平台）

`xingshu_bci.pro` 直接通过网线和 N1 Pro 通信，不需要打开桌面应用，也不需要登录或 Token。它能拿到设备的全速率原始数据（最高 16 kHz × 32 通道），并能修改全部硬件设置。

使用前请确认：

- 设备已开机，网线直连电脑（可经过交换机，不能经过路由器），电脑有线网卡为 `169.254.x.x` 地址；
- Windows 防火墙允许 Python 接收 UDP 9000（首次运行时弹窗选择"允许"）；
- 桌面平台不要同时采集同一台设备，否则两边会互相改设置。

### 连接与读取

```python
from xingshu_bci.pro import XingShuN1Pro

with XingShuN1Pro(sample_rate=1000) as dev:   # 连接，并把设备切到 1000 Hz
    data = dev.read(seconds=2)                 # numpy 数组 (32, 2000)，单位 µV
    print(data.shape, dev.sample_rate, dev.battery)
```

- 采样率可选 `250 / 500 / 1000 / 2000 / 4000 / 8000 / 16000` Hz（`XingShuN1Pro.SAMPLE_RATES`）；不传 `sample_rate` 则沿用设备当前采样率。
- 数组的 32 行依次是 `A1..A8, B1..B8, C1..C8, D1..D8`（`CHANNEL_NAMES`），对应电极接口第 2～33 脚。
- 数据按设备回报的每通道增益换算成微伏，切换增益后无需自己换算。
- 找不到设备时抛出 `XingShuConnectionError`（`code="DEVICE_NOT_FOUND"`），并给出排查提示。

### 取数据的四种方式

| 方法 | 说明 |
|---|---|
| `read(n_samples)` / `read(seconds=2)` | 阻塞直到收齐，返回 `(32, n)`。连续读取会消耗缓冲中的样本，可用 stats 检查丢样。 |
| `read_available()` | 立即返回目前所有未读数据，可能为空。 |
| `get_latest(seconds)` | 返回最近 N 秒数据，不消耗缓冲，适合实时绘图。 |
| `stream()` | 逐块迭代 `ProChunk`（每块 1～12 个样本，含 `first_sample_index`），适合实时处理。 |

第一次取数时自动开始缓冲（`start()`），设备数据灯随之闪烁；`stop()` 停止缓冲。缓冲默认保留 10 秒未读数据（`buffer_seconds`），读得太慢时最旧的数据会被丢弃并计入 `stats["overflowSamples"]`。网络丢包计入 `stats["missingSamples"]`。

16 kHz 时连续读取的示例：

```python
import numpy as np
from xingshu_bci.pro import XingShuN1Pro

with XingShuN1Pro(sample_rate=16000) as dev:
    blocks = [dev.read(seconds=1) for _ in range(10)]     # 10 秒原始数据
    np.save("eeg_16k.npy", np.concatenate(blocks, axis=1))
    print(dev.stats["missingSamples"], dev.stats["overflowSamples"])
```

### 硬件设置

```python
dev.set_sample_rate(250)                                   # 切换采样率，通道设置不变

dev.configure_channels(enabled=False, bias=False)          # 先关掉全部通道
dev.configure_channels(range(1, 9), enabled=True,          # 只开 A1–A8
                       gain=24, input_type="normal", bias=True)
dev.configure_channels(["B1", "B2"], enabled=True, gain=12)

for name, ch in zip(dev.CHANNEL_NAMES, dev.channels):      # 读回设备确认的设置
    print(name, ch.enabled, ch.gain, ch.input_type, ch.bias)
print(dev.bias_amplifier_on)                               # 任一通道勾 BIAS 时为 True
```

- `configure_channels()` 只修改指定的项，其余保持不变；通道可写 `1..32` 或 `"A1".."D8"`。
- 增益：`1 / 2 / 4 / 6 / 8 / 12 / 24`；输入类型：`normal`、`shorted`（内部短接，测噪声）、`test`（测试信号）、`mvdd`、`temperature`、`bias-meas`、`bias-drp`、`bias-drn`。
- `bias=True` 表示该通道参与右腿驱动的共模提取；驱动放大器固定由 D 片输出到 RL 电极，四片勾选的通道都参与。
- 一次性写入全部设置用 `apply_settings(sample_rate=..., channels=[ProChannel(...)] * 32)`。
- 每次应用设置，设备会重启采集（约 0.3 秒），SDK 等设备确认后才返回，并清空未读缓冲，避免不同采样率或增益的数据混在一起。设备拒绝时抛出 `XingShuError`（`code="SETTINGS_REJECTED"`）。

### 方波测试

```python
dev.set_square_wave(True)        # 32 通道约 1.95 Hz、峰峰值约 3750 µV 的方波
data = dev.read(seconds=2)
dev.set_square_wave(False)
```

方波测试期间不能修改硬件设置；SDK 每秒为设备续租一次，`close()` 时自动关闭测试。

### 状态与断线

- `dev.battery`：`BatteryStatus`，与桌面平台一致；是否充满只看充电芯片 CHGOK# 引脚。
- `dev.stats`：收包数、丢包、溢出、设备地址、在线芯片（`onlineChips`，如 `"ABCD"`）、FPGA 诊断计数。
- 超过 `link_timeout`（默认 5 秒）没有数据时，正在进行的 `read()` / `stream()` 抛出 `XingShuConnectionError`（`code="LINK_LOST"`），提示设备关机或网线断开。

## 通过桌面平台使用（本地 API）

以下各节介绍第二种用法：脚本连接桌面应用的本地 API，由平台负责设备连接、滤波、分析和录制。这种方式需要桌面应用运行并创建 Token，实时样本事件为显示流（N1 Pro 最高 250 Hz）。需要全速率原始数据或修改硬件设置时，请用上面的直连方式。

## 准备 Token

1. 打开 XingShu BCI 桌面应用。
2. 点击标题栏 **开发者中心**。
3. 创建 Token。
4. 复制 Token，并通过参数或环境变量传给 SDK。

```bash
export XINGSHU_BCI_TOKEN=xs_live_xxxx
```

普通只读程序建议使用 `data` + `stream`。需要连接设备、录制或插入 marker 时，再增加 `control`。
运行外部实验分类模型时需要 `experiment` scope；该 scope 也允许订阅定向的实验窗口事件。

## 第一个脚本

```python
from xingshu_bci import XingShuClient

with XingShuClient.auto() as client:
    version = client.version()
    status = client.status()
    print(version.apiVersion, version.protocol)
    print(status.state, status.board, status.sampleRate)
```

`XingShuClient.auto()` 会读取桌面应用写入的 bootstrap 文件，所以你不需要硬编码端口。

还没有 Token 时，可以用模块级 `healthz()` 探测桌面应用的本地 API 是否在运行（`/healthz` 不需要鉴权）：

```python
from xingshu_bci import healthz

if not healthz():
    print("XingShu BCI 桌面应用未运行或未启用 API 服务。")
```

也可以显式传入地址：

```python
from xingshu_bci import XingShuClient

client = XingShuClient(
    base_url="http://127.0.0.1:54321",
    token="xs_live_...",
)
```

## 连接设备

Synthetic board 适合快速开发和调试：

```python
from xingshu_bci import XingShuClient

with XingShuClient.auto() as client:
    if client.status().state == "idle":
        client.connect(board="synthetic", transport="synthetic")

    print(client.status())
```

串口设备示例：

```python
with XingShuClient.auto() as client:
    ports = client.list_serial_ports()
    print(ports)

    client.connect(
        board="cyton",
        transport="serial",
        serialPort="COM3",
    )
```

断开：

```python
client.disconnect()
```

### XingShu N1 Pro

N1 Pro 使用四片 ADS1299、32 个通道，采样率可选 250、500、1000、2000、4000、
8000 或 16000 Hz，默认 16000 Hz。采样率在连接时通过 `sampleRate` 指定，平台会
通过 UDP 控制端口把设置写入设备；要更换采样率，请先 `disconnect()` 再重新连接。
Python SDK 仍然通过桌面应用的本地 REST/WebSocket API 工作：

```python
with XingShuClient.auto(token="xs_live_...") as client:
    client.connect(board="xingshu_n1_pro", transport="udp", sampleRate=1000)
    status = client.status()
    assert status.board == "xingshu_n1_pro"
    assert status.channelCount == 32
    assert status.sampleRate == 1000
    assert status.capabilities.topology.ready
```

不在上述列表中的采样率会被平台拒绝，SDK 抛出 `XingShuError`。设备固件过旧、
不支持切换采样率时，只能使用 16000 Hz。

SDK 的 `samples` 事件是平台为界面和脚本准备的显示流；原始数据由平台的 UDP
接收、录制和网络输出链路处理。SDK 不会再次滤波或重复计算这些数据。

### 读取设备电量

`status().battery` 是平台滤波后的电量（`BatteryStatus`），未连接或设备未上报时为 `None`：

```python
battery = client.status().battery
if battery and battery.valid:
    print(battery.percent, battery.state, battery.voltageMv)
```

`state` 取值为 `discharging`（电池供电）、`charging`（充电中）、`full`（已充满）、
`plugged`（刚接入电源）或 `unknown`。是否充满只看充电芯片的状态引脚（N1 Pro 为
MP2615 CHGOK#），不根据电压判断。`voltageMv` 为整组电池电压，`cellMv` 为单节电压。

## 订阅实时样本

```python
from xingshu_bci import XingShuClient

with XingShuClient.auto() as client:
    if client.status().state == "idle":
        client.connect(board="synthetic", transport="synthetic")

    for event in client.events(types=["samples", "analysis", "status"]):
        if event.type == "samples":
            data = event.payload.get("data") or []
            if data and data[0]:
                print("ch0 first sample:", data[0][0])
        elif event.type == "analysis":
            print("focus:", event.payload.get("focus"))
        elif event.type == "status":
            print("state:", event.payload.get("state"))
```

`events()` 返回同步生成器。按 `Ctrl+C` 结束程序即可。

鉴权与重连行为：

- Token 通过 `Authorization: Bearer` header 发送（不出现在 URL 里）。仅当服务端无法读取 upgrade header 时，才用 `token_in_query=True` 回退到 `?token=`（SDK 会自动 URL 编码）。
- 首次连接失败会立刻抛出 `XingShuConnectionError`；连接建立后如果断开，默认自动指数退避重连（0.5s 起，最长 30s）。传入 `on_disconnect=callback` 可以在每次断线时收到原因；连续重连失败 `max_reconnect_attempts` 次（默认 5）后抛出 `code="WS_RECONNECT_EXHAUSTED"`。`reconnect=False` 恢复旧行为（断开即结束迭代）。
- 重连后 `seq` 重新计数，断线期间的事件不会补发。

## 录制并插入 marker

```python
import time
from xingshu_bci import XingShuClient

with XingShuClient.auto() as client:
    client.connect(board="synthetic", transport="synthetic")
    try:
        client.start_recording()
        client.insert_marker(value=1, label="trial_start")
        time.sleep(2)
        client.insert_marker(value=2, label="stimulus_on")
        time.sleep(1)
        client.insert_marker(value=3, label="stimulus_off")
        print(client.stop_recording())
    finally:
        client.disconnect()
```

## 读取专注度 / 放松度

轮询最新快照：

```python
import time
from xingshu_bci import XingShuClient

with XingShuClient.auto() as client:
    if client.status().state == "idle":
        client.connect(board="synthetic", transport="synthetic")

    while True:
        snapshot = client.analysis_focus_latest()
        print(snapshot.get("focus"))
        time.sleep(0.5)
```

实时事件方式：

```python
for event in client.events(types=["analysis"]):
    focus = event.payload.get("focus") or {}
    if focus.get("state") == "ready":
        print(focus.get("prediction"))
```

## 用专注度控制外部设备

`SmoothedFocusStream` 封装了 EMA 平滑、滞回阈值、限速和亮度映射，适合把专注度/放松度接到灯泡、风扇、游戏或视觉刺激程序。

```python
from xingshu_bci import SmoothedFocusStream, XingShuClient

class Bulb:
    def on(self):
        print("[bulb] ON")

    def off(self):
        print("[bulb] OFF")

    def set_brightness(self, percent: int):
        print(f"[bulb] {percent}%")

with XingShuClient.auto() as client:
    if client.status().state == "idle":
        client.connect(board="synthetic", transport="synthetic")

    bulb = Bulb()
    for update in SmoothedFocusStream(client, metric="concentration"):
        if update.rising:
            bulb.on()
        elif update.falling:
            bulb.off()

        if update.is_on:
            bulb.set_brightness(update.brightness)
```

常用参数：

| 参数 | 默认 | 说明 |
|------|------|------|
| `metric` | `"concentration"` | 可选 `"concentration"` 或 `"relaxation"`。 |
| `smoothing` | `0.25` | EMA alpha，越小越平滑但延迟越高。 |
| `on_threshold` | `0.45` | 平滑值升至该阈值触发 `rising`。 |
| `off_threshold` | `0.30` | 平滑值降至该阈值触发 `falling`。 |
| `dead_zone` | `0.20` | 低于该值时亮度输出为 0。 |
| `min_interval_s` | `0.2` | 两次输出之间的最小间隔。 |
| `auto_configure` | `True` | 自动向服务端设置 focus metric。 |

## Token 管理

Token 管理接口需要 `admin` scope。

```python
from xingshu_bci import XingShuClient

with XingShuClient.auto() as client:
    created = client.create_token(
        name="experiment-script",
        scopes=["data", "stream", "control", "experiment"],
        expires_in_days=30,
    )
    print(created.token)  # 只显示一次

    for token in client.list_tokens():
        print(token.id, token.name, token.scopes)

    client.revoke_token(created.id)
```

## 运行自定义 SSVEP 模型

> 平台 1.0.0 暂未开放"实验工作台"。以下模型注册接口仍然可用，但只有在平台中运行
> 实验时模型才会收到试次窗口，因此在 1.0.0 中模型会保持在线、不会收到预测请求。

`serve_ssvep_model()` 会完成模型注册、后台心跳、定向 WebSocket 订阅、预测提交和退出注销。回调收到 `SsvepTrialWindow`，其中 `data` 是通道优先的二维数组，目标频率和相位位于 `targets`。

```python
from xingshu_bci import SsvepPrediction, SsvepTrialWindow, XingShuClient


def predict(window: SsvepTrialWindow) -> SsvepPrediction:
    # 在这里调用自己的 FBCCA、深度学习或远程推理逻辑。
    scores = {target["id"]: 0.0 for target in window.targets}
    selected = window.targets[0]["id"]
    scores[selected] = 1.0
    return SsvepPrediction(
        target_id=selected,
        scores=scores,
        confidence=1.0,
        metadata={"algorithm": "demo"},
    )


with XingShuClient.auto() as client:
    client.serve_ssvep_model("my-ssvep-model", predict, version="1.0.0")
```

回调也可以返回 REST 字段形式的字典：

```python
return {"targetId": selected, "scores": scores, "confidence": 0.87}
```

不确定时返回 `SsvepPrediction(abstain=True)`。字典回调可使用 `abstain` 或 HTTP 字段名 `abstained`；SDK 提交时统一发送 `abstained`。服务默认每 5 秒发送一次心跳；传入 `threading.Event` 作为 `stop_event` 可从其他线程停止服务。模型在平台中被选中并运行实验后，才会收到 `experiment.trial.window`。

每个窗口带有唯一 `attemptId`，提交预测时必须回传（服务端缺失返回 400、不匹配返回 409）。`serve_ssvep_model()` 自动把 `window.attempt_id` 回传给平台，无需手动处理。

容错行为：单个 trial 的失败（窗口格式异常、`predict_fn` 抛错、提交被拒绝如迟到 409）只会记录日志并跳过该 trial，模型保持在线并继续等待下一个窗口；WebSocket 断线会自动重连，心跳线程独立维持注册状态。只有心跳持续失败或重连次数耗尽才会退出并注销模型。

低层方法包括：

- `list_experiment_models()`
- `register_experiment_model(name, version=..., paradigm="ssvep")`
- `heartbeat_experiment_model(model_id)`
- `unregister_experiment_model(model_id)`
- `submit_experiment_prediction(run_id, trial_id, prediction, model_id=..., attempt_id=...)`（`attempt_id` 必填，可由 prediction 字典里的 `attemptId` 提供）

## 错误处理

```python
from xingshu_bci import (
    XingShuAuthError,
    XingShuForbiddenError,
    XingShuTimeoutError,
    XingShuRateLimitError,
    XingShuError,
)

try:
    client.start_recording()
except XingShuAuthError:
    print("Token 缺失、错误或已过期。")
except XingShuForbiddenError:
    print("Token 权限不足，请在开发者中心创建带 control scope 的 Token。")
except XingShuTimeoutError:
    print("请求超时（HTTP 408 或客户端超时）。")
except XingShuRateLimitError:
    print("请求过于频繁（HTTP 429），请稍后重试。")
except XingShuError as exc:
    print(exc.status, exc.code, exc.message, exc.request_id)
```

异常层级：`XingShuTimeoutError` 继承自 `XingShuConnectionError`，已有的连接错误处理会自然覆盖超时；`XingShuRateLimitError` 直接继承 `XingShuError`。

## 低层 HTTP 调用

SDK 没封装到的端点可以直接调用：

```python
response = client.request(
    "POST",
    "/v1/recording/markers",
    json={"value": 9, "label": "custom_event"},
)
print(response.json())
```

## 常用方法索引

| 方法 | 说明 |
|------|------|
| `healthz()`（模块级） | 无需 Token 探测本地 API 是否存活。 |
| `version()` | API 与应用版本。 |
| `status()` | 设备与采集状态。 |
| `list_serial_ports()` | 可用串口。 |
| `connect(...)` / `disconnect()` | 连接或断开设备。 |
| `pause()` / `resume()` | 暂停或恢复采集流。 |
| `events(types=[...])` | WebSocket 实时事件。 |
| `serve_ssvep_model(name, predict_fn, ...)` | 注册并运行同步式外部 SSVEP 模型。 |
| `list_experiment_models()` | 当前在线外部实验模型。 |
| `start_recording()` / `stop_recording()` | 录制控制。 |
| `insert_marker(value, label=...)` | 写入事件 marker。 |
| `analysis_focus_latest()` | 最新专注度/放松度快照。 |
| `signal_quality_latest()` | 最新信号质量结果。 |
| `logs(limit=200)` | 应用日志。 |

更多可运行示例见 [examples/](./examples)。
