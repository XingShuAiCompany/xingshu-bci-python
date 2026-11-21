# XingShu BCI Python SDK

使用 Python 直接从 XingShuN1 Pro 接收 32 通道脑电数据。设备通过网线连接电脑，支持 250 Hz 至 16 kHz。SDK 也提供 XingShu BCI 桌面软件的本地 API 客户端。

## 在 Windows 安装

安装 Python 3.10 或更高版本，下载 [1.0.0 源码 ZIP](https://github.com/XingShuAiCompany/xingshu-bci-python/archive/refs/tags/v1.0.0.zip) 并解压。在包含 pyproject.toml 的目录打开 PowerShell，运行以下命令：

~~~powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install ".[device]"
.\.venv\Scripts\python.exe -c "import xingshu_bci as x; print(x.__version__)"
~~~

版本检查应输出 1.0.0。Linux 或 macOS 可使用虚拟环境中的 .venv/bin/python 执行相同的安装和验证步骤。

## 读取两秒数据

将设备接到电脑有线网口并开机。电脑与设备（169.254.87.100）应在同一 169.254.0.0/16 网段。若桌面软件正在使用设备，请先断开该会话。允许 Python 接收 UDP 9000，并向设备发送 UDP 9001。

将以下内容保存为 receive_eeg.py，在 PowerShell 中运行：

~~~powershell
.\.venv\Scripts\python.exe receive_eeg.py
~~~

~~~python
from xingshu_bci.pro import XingShuN1Pro

with XingShuN1Pro(sample_rate=1000) as dev:
    data = dev.read(seconds=2)
    print(data.shape)
    print(data[0, :5])
~~~

第一行输出 (32, 2000)。每行是一个通道，每列是同一时刻的采样值，单位为微伏（µV）。第二行打印 A1 通道前五个采样值。连续接收可使用 dev.stream()。每个数据块的 chunk.data 形状为 (32, n)。

[完整 SDK 指南](docs/SDK.md) · [直连采集并保存为 NumPy 文件](docs/examples/python/n1pro_direct.py) · [更多示例](docs/examples/python)

## License

Proprietary © XingShu BCI.
