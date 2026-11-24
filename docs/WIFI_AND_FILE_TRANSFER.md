# WiFi 热点连接与文件传输协议

> 面向 **Python 桌面应用**（Windows / macOS）开发。基于 `pnote-web-win&Mac-demo` 现有 `recorder/` 模块扩展。

| **文档项** | **内容** |
| --- | --- |
| 版本 | V1.0（2026-11-24） |
| 适用项目 | `pnote-web-win&Mac-demo`（Python 桌面 + Web 控制台） |
| 通信角色 | 手机 / PC 为 TCP **客户端**；录音卡为 TCP **服务端** |
| 连接目标 | `192.168.1.1:27689` |
| 协议依据 | 厂商《录音笔与APP通讯协议（20260313）》+ pnote Android SDK 逆向 + CB08 真机抓包 |
| 交叉验证 | 帧格式与 `docs/协议.md` 第 3 节、`recorder/protocol.py` 完全一致 |
| 关联文档 | `docs/协议.md`（通用帧格式、文件列表、下载流程）、`docs/协议差异对照.md` |

**本节结论摘要**

WiFi 热点模式**不是一套新协议**，而是同一套 `0x5A` 帧协议换了一条物理链路：BLE 通知换成 TCP 字节流。因此 `recorder/protocol.py` 里的帧构造、CRC、命令常量、解析器**全部原样复用**，只需要新增一个 TCP 传输层。

⚠️ **两个反直觉的事实，实现前必须知道**：

1. **命令下行只走 BLE。** Android SDK 的发送队列唯一出口是 `k(byte[])`，里面没有任何 TCP 分支 —— WiFi 连上了但 BLE 断了，命令发不出去且无任何错误回调。本协议沿用同一约定：**请求帧仍经 BLE `0xAE21` 写入，数据块经 TCP 收**。
2. **TCP 收到的字节不能按「一次 data 事件 = 一帧」处理。** 设备会粘包/拆包，必须自己做流式拆帧。`recorder/protocol.py` 的 `FrameParser` 已经实现了完整拆帧（丢弃帧头前噪声、LEN 上限保护、CRC 校验、CRC 错误重同步），**直接复用即可**。

---

# 1\. 适用范围与前置条件

| **前置条件** | **说明** |
| --- | --- |
| 设备已配对 | 录音卡热点通过 BLE 指令 `0-19` 打开，热点名由 `0-20` 应答返回 |
| 手机 / PC 已连上热点 | 由操作系统完成，**不在本协议范围内** |
| BLE 保持连接 | 用于下发请求帧。BLE 断开则文件请求发不出去 |
| 设备固件支持 | `0-20` 应答里带热点名即支持；老固件可能不回复 |

**与 BLE 模式的关系**：文件传输的**下行请求**走 BLE，**上行数据**在 WiFi 下走 TCP。BLE 模式下数据全部走 BLE（`0xAE22` 通知），协议帧完全相同，只是分片大小不同（见 5.4）。

---

# 2\. 传输层总览

```
┌──────────────┐   请求帧                ┌──────────────┐
│  App / PC    │ ──────────────────────► │  录音卡      │
│              │   BLE 0xAE21（写）      │              │
│              │                         │              │
│              │ ◄────────────────────── │              │
│              │   BLE 0xAE22（通知）     │              │
│              │        或                │              │
│              │   TCP 192.168.1.1:27689 │              │
└──────────────┘        （数据块）        └──────────────┘
```

| **通道** | **特征 UUID** | **属性** | **方向 / 用途** |
| --- | --- | --- | --- |
| BLE 命令 | 0xAE21 | WRITE_WITHOUT_RESPONSE | App → Dev，请求帧 |
| BLE 通知 | 0xAE22 | NOTIFY | Dev → App，BLE 模式下的全部数据 |
| BLE 按键 | 0xAE23 | NOTIFY | Dev → App，机身按键（与本节无关） |
| BLE 大文件 | 0xAE24 | WRITE_WITHOUT_RESPONSE | App → Dev，OTA / 大文件上传 |
| **TCP** | 192.168.1.1:27689 | — | WiFi 模式下的数据块与控制应答 |

---

# 3\. 帧格式（直接复用现有实现）

**完全沿用 `docs/协议.md` 第 3 节，本节只列要点。**

| **偏移** | **长度** | **字段** | **字节序** | **说明** |
| --- | --- | --- | --- | --- |
| 0 | 1B | MAGIC | - | 固定 `0x5A` |
| 1 | 1B | SEQ | - | 包序号，0~255 循环递增 |
| 2 | 2B | CRC | LE | CRC-16/XMODEM，计算范围 = `LEN(2B) + DATA` |
| 4 | 2B | LEN | LE | DATA 的真实字节数 |
| 6 | LEN | DATA | - | `[TYPE:1B][CMD:1B][PARAMS...]` |

**CRC 参数**（`recorder/crc16.py` → `crc16_xmodem`）：poly `0x1021`、init `0x0000`、RefIn/RefOut false、XorOut `0x0000`；检验向量 `ASCII "123456789" → 0x31C3`。

**复用方式**：

```python
from recorder.protocol import build_frame, build_command, TYPE_CONTROL, TYPE_FILE
from recorder.crc16 import crc16_xmodem
```

---

# 4\. WiFi 热点连接

## 4.1 命令族（TYPE=0 控制命令）

| **CMD** | **方向** | **含义** | **参数 / 应答** |
| --- | --- | --- | --- |
| 19 | App→Dev | 请求打开 WiFi 传输 | 无；应答 `0-20` |
| 20 | Dev→App | 打开 WiFi 回复 | `1B 状态`（0 成功 / 1 失败）`+ WiFi 名称` |
| 21 | App→Dev | 请求关闭 WiFi 传输 | 无；应答 `0-22` |
| 22 | Dev→App | 关闭 WiFi 回复 | 无 |
| 23 | Dev→App | 有手机连接 WiFi | `1B`（1 已连接 / 0 未连接）；**TCP 已连上时设备不再发** |
| 24 | App→Dev | 请求是否有手机连接 WiFi | 无；应答 `0-23` |

实跑帧（SEQ=0，CRC 已校验）：

```
0-19 打开 WiFi : 5A 00 3A CF 02 00 00 13
0-21 关闭 WiFi : 5A 01 FC AF 02 00 00 15
0-24 问是否连接 : 5A 02 51 7E 02 00 00 18
```

> `0-19` / `0-21` / `0-24` 三条**必须经 BLE 下发**（0xAE21）。`0-20` / `0-22` / `0-23` 由设备主动上报，可在任意通道收到。

## 4.2 TCP 连接参数

对齐 Android SDK 的 `LongSocketClient`（`com.wind.pnote.service.a`）配置项：

| **参数** | **Android SDK** | **Python 建议** | **说明** |
| --- | --- | --- | --- |
| 主机 / 端口 | `192.168.1.1` / `27689` | 同 | 厂商 docx 明确 |
| 连接超时 | 10 000 ms | `10.0` s | SDK `.b(n2)`，`j()` 传 10000 |
| 重试次数 | 5 | 5 | SDK `.c(5)` |
| 重连间隔 | 3 000 ms | `3.0` s | SDK `.g(3000)` |
| 会话超时 | 600 000 ms | 600 s | SDK `.f(600000)` |
| 心跳间隔 | 30 000 ms | `30.0` s | SDK `.a(30000L, {0x01})` |
| 心跳内容 | `{0x01}` | `b"\x01"` | 心跳分隔符 `"\n"` |
| 接收缓冲上限 | 1 MiB | `1 << 20` | SDK `.d(0x100000)` |
| ACK 模式 | 默认关（`e0=0`） | 默认关 | 开启后每收一帧回 `"ACK\r\n"`；**除非设备明确要求，不要开** |

## 4.3 TCP 字节流拆帧 ★

TCP 是字节流，设备可能把一帧拆成多次 `recv`，也可能把多帧一次送来。拆帧规则（与 BLE 侧 `FrameParser` 一致）：

```
1. 丢弃 MAGIC(0x5A) 之前的所有噪声字节
2. 缓冲不足 6 字节 → 等待
3. LEN = frame[4] | frame[5]<<8
4. LEN > 上限(8192) → 判定为假帧头，丢弃 1 字节后重新同步
5. 缓冲不足 6+LEN → 等待后续数据
6. 取出一整帧，校验 CRC；CRC 不符 → 丢弃 1 字节重新同步并计数
7. 循环处理剩余数据
```

**`recorder/protocol.py` 的 `FrameParser` 已经完整实现了以上全部逻辑，直接复用，不要另写。**

```python
from recorder.protocol import FrameParser

parser = FrameParser("TCP")
for frame in parser.feed(chunk):     # chunk = 本次 recv 到的字节
    ...                              # frame.type / frame.cmd / frame.body
```

> **接收缓冲要够大**：SDK 用 1 MiB。WiFi 下设备每包 1024 B、连续推送，很容易一次读到几十 KB。Python `asyncio` 的 `StreamReader` 默认 limit 是 64 KiB，**建议 `open_connection(..., limit=1<<20)`**。

## 4.4 状态上报

Android SDK 把 WiFi 连接状态上报为 JSON `cmd=12`：

```json
{"cmd":"12","state":"0","data":{"wifi_state":"2"}}            // 已连接
{"cmd":"12","state":"0","data":{"wifi_state":"3"}}            // 已断开
{"cmd":"12","state":"0","data":{"wifiName":"PONE","wifi_state":"2"}}  // 带热点名（仅非空时出现）
```

| **wifi_state** | **含义** |
| --- | --- |
| 2 | TCP 已连接 |
| 3 | TCP 未连接 / 已断开 |

> Python 侧不需要拼 JSON，直接按帧解析：`TYPE=0` 且 `CMD` 为设备事件时自行映射。若要与 Android/iOS SDK 的事件流保持一致，可照 `recorder/device.py` 的 `onDeviceDataEvent` 语义自行封装。

## 4.5 心跳与保活

- 每 **30 秒**向 socket 写一次 `b"\x01"`（可带 `"\n"` 分隔）。
- Android SDK 的心跳是**无条件写**，不做超时检测；断开由 socket 错误事件感知。
- Python 侧建议：写心跳失败或 `reader` 返回 EOF → 判定断开 → 关闭 socket → 上报 `wifi_state=3` → 按 3 秒间隔重连。

## 4.6 完整时序

```
App / PC                                    录音卡
  │                                            │
  │ ── BLE 0xAE21 ── 0-19 打开 WiFi ──────────► │  打开热点
  │ ◄── BLE 0xAE22 ── 0-20 状态+热点名 ────────── │
  │                                            │
  │  操作系统连上热点（SSID = 热点名）           │
  │                                            │
  │ ── TCP connect 192.168.1.1:27689 ─────────► │  接受连接
  │ ◄── wifi_state=2 ──────────────────────────── │
  │                                            │
  │ ── TCP ── 每 30s 心跳 {0x01} ──────────────► │
  │ ◄── TCP ── 数据帧（1024B/包）──────────────── │
  │                                            │
  │ ── BLE 0xAE21 ── 0-2 请求文件 ─────────────► │  ← 请求仍走 BLE
  │ ◄── TCP ── 0-3 / 0-4 / 0-5 ───────────────── │
```

---

# 5\. 文件传输

## 5.1 命令与请求帧

| **CMD** | **方向** | **含义** | **参数** |
| --- | --- | --- | --- |
| 2 | App→Dev | 请求导入文件 | `offset:4B LE` + `filename:24B` |
| 12 | App→Dev | 分段导入（区间） | `start:4B LE` + `end:4B LE` + `filename:24B` |
| 7 | App→Dev | 终止导入 | 无 |
| 3 | Dev→App | 开始导入 | 实际导入的文件名 |
| 4 | Dev→App | 文件数据 | 音频字节分片 |
| 5 | Dev→App | 导入结束 | `1B` 结果码 |

**请求帧构造（`recorder/protocol.py` 已有现成函数）**：

```python
from recorder.protocol import build_import_request, build_segment_request

frame = build_import_request(seq, filename, offset)        # 0-2
frame = build_segment_request(seq, filename, start, end)   # 0-12
```

实测帧（`filename="note20260710-162938.wav"`, `offset=0`, `seq=3`）：

```
5a 03 9e 20 1e 00 02 02 00 00 00 00 6e 6f 74 65 32 30 32 36 30 37 31 30
2d 31 36 32 39 33 38 2e 77 61 76 00
│  │  └─┬─┘ └─┬─┘ │  │  └────── offset 4B LE ──────┘└──── filename 24B ────┘
│  │    │    │    │  └────── CMD=2
│  │    │    │    └───────── TYPE=2（文件操作）
│  │    │    └────────────── LEN=30（2+4+24）
│  │    └─────────────────── CRC=0x209E
│  └──────────────────────── SEQ=3
└─────────────────────────── MAGIC
```

**完整帧 36 字节，必须一次 GATT 写入。** `recorder/ble.py` 的 `write_frame(frame, atomic=True)` 会阻止拆分（拆成 20+16 会让设备把文件名解析错，返回 `2-5 / code=1`）。

## 5.2 文件名字段：固定 24 字节 ★

**这是最容易写错的地方。**

| **规则** | **说明** |
| --- | --- |
| 长度 | **固定 24 字节** |
| 填充 | NUL（`0x00`）补齐 |
| 超长 | 截断到 24 字节 |
| 编码 | UTF-8（实际是 ASCII 文件名） |
| 典型值 | `"note20260710-162938.wav"` = 23 字节 + 1 个 NUL |

> **注意**：Android SDK（pnote 系）发的是**变长** filename、不补 NUL；而 CB08 真机抓包是 24 字节定长。CB08 / QS668 必须按 24 字节定长发，否则设备会判为「文件不存在」（`2-5 / code=1`）。
>
> 本仓库 `recorder/protocol.py` 原先的 `FILENAME_FIELD_LEN = 29` 与真机不符（会生成 41 字节帧），**已于 2026-11-24 修正为 24**，现与真机抓包帧逐字节一致（`tests/test_protocol.py::test_real_import_request_frame` 覆盖）。
>
> 同批修正：`LIST_NAME_LEN` 25 → **20**、`LIST_ENTRY_LEN` 33 → **28**（厂商测试页 `docs/vendor_page/app.js` 的 `parseFileList` 按 28 B/条目解析）。

## 5.3 应答帧

| **CMD** | **body** | **处理** |
| --- | --- | --- |
| 3 | 文件名字节串 | 记录实际导入的文件名；按 UTF-8 解码（尾部 NUL 需自行裁掉） |
| 4 | 音频字节分片 | 追加写入本地文件；刷新空闲超时 |
| 5 | `1B` 结果码 | 0 完成 / 1 文件不存在 / 2 offset 过大 / 3 其他停止 |

**结果码处理**：

| **code** | **含义** | **建议动作** |
| --- | --- | --- |
| 0 | 完成 | 收尾、校验文件头 |
| 1 | 文件不存在 | 换候选文件名重试（列表名常被截断扩展名） |
| 2 | offset 过大 | 重置 offset 到 0 重试 |
| 3 | 其他停止 | 中止并提示用户 |

## 5.4 分片规格

| **通道** | **每包数据** | **说明** |
| --- | --- | --- |
| BLE 0xAE22 | **160 B** | 设备端行为 |
| TCP | **1024 B** | 设备端行为（约为 BLE 的 6~10 倍） |

- 分片大小由**设备决定**，App 侧不要假设，只管追加落盘。
- WiFi 下 1024 B/包的吞吐远高于 BLE，这也是 WiFi 模式的主要价值。

## 5.5 落盘

**App 侧负责落盘。** 收到 `2-4` 就追加写入本地文件：

```python
if frame.type == P.TYPE_FILE and frame.cmd == P.FILE_DATA:
    with open(local_path, "ab") as fh:      # 追加
        fh.write(frame.body)
    received += len(frame.body)
```

注意：

- `frame.body` 是 `DATA` 中 `[TYPE][CMD]` 之后的部分，即**纯音频数据**。
- 写盘用 `ab`（追加），别用 `wb`，否则每片都会截断文件。
- 落盘路径要能容纳同名文件，建议加 `time/size/序号`（见 `docs/协议.md` 第 9 节）。
- 下载结束（`2-5`）后再校验文件头：WAV 应满足 `bytes[0:4]==RIFF`、`bytes[8:12]==WAVE`。

## 5.6 断点续传

两种方式，按设备支持情况选：

| **方式** | **命令** | **用法** |
| --- | --- | --- |
| 偏移续传 | `0-2` | 把已下载字节数作为 `offset` 传入，设备从该位置继续 |
| 区间导入 | `0-12` | 传 `start` / `end` 字节区间，适合只要文件的一段 |

两者都用 4 字节小端。

## 5.7 完整时序

```
App / PC                                     录音卡
  │                                            │
  │ ── BLE 0xAE21 ── 0-2 (offset=0, name) ───► │  校验文件名、定位文件
  │                                            │
  │ ◄── TCP ── 0-3 实际文件名 ────────────────── │  开始导入
  │                                            │
  │ ◄── TCP ── 0-4 分片1 (1024B) ────────────── │  文件数据
  │ ◄── TCP ── 0-4 分片2 (1024B) ────────────── │  （持续推送）
  │      …每片追加落盘、刷新空闲超时…             │
  │                                            │
  │ ◄── TCP ── 0-5 result=0 ─────────────────── │  传输完成
  │                                            │
  │  校验 RIFF/WAVE 头、收尾                     │
```

**空闲超时**：BLE 模式建议 12 秒、WiFi 模式可放宽到 30 秒以上；每收到一片 `0-4` 就刷新计时器。若 `received == 0` 且超时，可换候选文件名重试。

---

# 6\. Python 实现指南

## 6.1 复用清单

| **已有资产** | **位置** | **本协议怎么用** |
| --- | --- | --- |
| `crc16_xmodem` | `recorder/crc16.py` | 直接用 |
| `build_frame` / `build_command` | `recorder/protocol.py` | 直接用 |
| `build_import_request` / `build_segment_request` | `recorder/protocol.py` | 直接用 |
| `FrameParser` | `recorder/protocol.py` | **TCP 通道直接复用**（已含完整拆帧 + CRC 校验） |
| `Frame` | `recorder/protocol.py` | 直接用 |
| `BleTransport` | `recorder/ble.py` | 保持不变，作为请求帧通道 |
| `Recorder._request` / `_send` | `recorder/device.py` | 保持不变 |
| `Recorder._feed_main` → `_handle_frame` | `recorder/device.py` | **TCP 复用同一条分发链路**（见 6.3） |
| `Recorder._dl_*` 下载会话 | `recorder/device.py` | **零改动**（数据来源换成 TCP 即可） |

**关键设计**：把 TCP 做成一个与 `BleTransport` **同构**的类（`on_main` 回调 + `write_frame` + `is_connected`），`Recorder` 只需增加一个 `FrameParser("TCP")` 并把 TCP 的回调接到 `_dispatch_stream` 上，**协议解析、应答配对、下载会话代码一行都不用改**。

## 6.2 新增 `recorder/tcp.py`

```python
"""recorder/tcp.py —— 录音卡 WiFi 热点 TCP 传输层。

对应 Android SDK 的 com.wind.pnote.service.a（LongSocketClient）。
设备侧是 TCP 服务端，手机/PC 是客户端：192.168.1.1:27689。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEVICE_HOST = "192.168.1.1"
DEVICE_PORT = 27689
CONNECT_TIMEOUT = 10.0        # 对齐 SDK .b(10000)
RETRY_INTERVAL = 3.0          # 对齐 SDK .g(3000)
MAX_RETRY = 5                 # 对齐 SDK .c(5)
HEARTBEAT_INTERVAL = 30.0     # 对齐 SDK .a(30000L, {0x01})
HEARTBEAT_PAYLOAD = b"\x01"
RECV_LIMIT = 1 << 20          # 对齐 SDK .d(0x100000)，覆盖 SDK 的 1 MiB


class TcpTransport:
    """与 BleTransport 同构：把收到的原始字节交给上层 FrameParser 即可。"""

    def __init__(self, host: str = DEVICE_HOST, port: int = DEVICE_PORT) -> None:
        self._host = host
        self._port = port
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._read_task: Optional[asyncio.Task] = None
        self._hb_task: Optional[asyncio.Task] = None
        self._closed = False
        self._on_disconnect: Optional[Callable[[], None]] = None
        self.on_main: Optional[Callable[[bytes], None]] = None

    # ------------------------------------------------------------ 回调

    @property
    def on_disconnect(self) -> Optional[Callable[[], None]]:
        return self._on_disconnect

    @on_disconnect.setter
    def on_disconnect(self, cb: Optional[Callable[[], None]]) -> None:
        """注册断开回调。若连接已断开则立即补发一次，避免注册过晚丢事件。"""
        self._on_disconnect = cb
        if cb is not None and self._closed:
            cb()

    # ------------------------------------------------------------ 状态

    @property
    def is_connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    # ------------------------------------------------------------ 连接

    async def connect(self) -> None:
        """建立 TCP 连接；失败抛异常，由上层决定是否重试。"""
        self._closed = False
        self._reader, self._writer = await asyncio.wait_for(
            asyncio.open_connection(self._host, self._port, limit=RECV_LIMIT),
            timeout=CONNECT_TIMEOUT,
        )
        self._read_task = asyncio.create_task(self._read_loop())
        self._hb_task = asyncio.create_task(self._heartbeat_loop())
        logger.info("TCP 已连接 %s:%s", self._host, self._port)

    async def disconnect(self) -> None:
        for task in (self._read_task, self._hb_task):
            if task is not None:
                task.cancel()
        self._read_task = self._hb_task = None
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass
        self._writer = None
        self._reader = None
        self._closed = True

    async def _read_loop(self) -> None:
        """持续读取字节流，交给上层解析（拆帧由上层 FrameParser 负责）。"""
        try:
            while True:
                chunk = await self._reader.read(RECV_LIMIT)
                if not chunk:
                    raise ConnectionError("对端关闭连接")
                if self.on_main is not None:
                    self.on_main(chunk)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("TCP 接收结束：%s", exc)
        finally:
            self._closed = True
            if self._on_disconnect is not None:
                self._on_disconnect()

    async def _heartbeat_loop(self) -> None:
        """每 30 秒写一次心跳 {0x01}。"""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                self.write_frame(HEARTBEAT_PAYLOAD)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("心跳失败：%s", exc)

    # ------------------------------------------------------------ 写入

    def write_frame(self, data: bytes) -> None:
        """向 socket 写入字节。请求帧仍走 BLE；这里只用于心跳等。"""
        if self._writer is None:
            raise ConnectionError("TCP 未连接")
        self._writer.write(data)
```

## 6.3 接入 `Recorder`（下载逻辑零改动）

```python
# recorder/device.py
from .tcp import TcpTransport

class Recorder:
    def __init__(self, output_dir=Path("downloads")) -> None:
        ...
        self.transport = BleTransport()          # 请求帧通道（BLE）
        self.tcp = TcpTransport()                # 数据通道（WiFi）
        self._parser_tcp = FrameParser("TCP")    # 独立解析器，与 AE22 隔离
        ...
        self.tcp.on_main = self._feed_tcp
        self.tcp.on_disconnect = self._handle_tcp_disconnect

    def _feed_tcp(self, chunk: bytes) -> None:
        """TCP 收到的原始字节 → 独立 FrameParser → 复用同一条分发链路。"""
        self._dispatch_stream(self._parser_tcp, chunk, source="TCP")
```

**为什么这样接**：`Recorder._feed_main` 收到的是**原始字节**（不是 `Frame`），内部走 `_dispatch_stream(parser, chunk)` → `_dispatch_chunk` → `_handle_frame`。TCP 复用同一入口后，`_handle_frame` 里对下载会话（`2-3` / `2-4` / `2-5`）、`_waiters` 应答配对、文件列表、实时音频的**全部处理逻辑自动生效**。

> **务必用独立的 `FrameParser("TCP")`**：与 `_parser_key`（AE23）同理 —— 两个通道的字节可能交错，共用一个解析器会把半帧拼错。

## 6.4 WiFi 连接编排

```python
async def enable_wifi_and_connect(rec: Recorder) -> None:
    """打开录音卡热点 → （用户让系统连上）→ 建 TCP。"""
    # 1. 请求打开 WiFi（走 BLE）
    resp = await rec._request(P.TYPE_CONTROL, 19, b"", 20, timeout=10.0)
    status = resp.body[0] if resp.body else -1
    if status != 0:
        raise RuntimeError("设备打开 WiFi 失败")
    ssid = resp.body[1:].split(b"\x00", 1)[0].decode("utf-8", "replace")
    logger.info("录音卡热点已开启：%s（请先让系统连上该热点）", ssid)

    # 2. 等系统连上热点后，建 TCP
    await rec.tcp.connect()
    # 此后数据块自动走 TCP（_feed_tcp 已接入分发链路）
```

断线重连建议：

```python
async def _reconnect_loop(rec: Recorder) -> None:
    tries = 0
    while True:
        await asyncio.sleep(RETRY_INTERVAL)      # 3 s，对齐 SDK
        if rec.tcp.is_connected:
            tries = 0
            continue
        try:
            await rec.tcp.connect()
            tries = 0
        except Exception:
            tries += 1
            if tries >= MAX_RETRY:                # 5，对齐 SDK
                logger.error("TCP 重连失败，放弃")
                return
```

## 6.5 文件下载

`Recorder` 里已有 `_download_once()`（`recorder/device.py:443`），已处理 2-2 请求、2-3/2-4/2-5 应答、空闲超时、候选文件名重试、落盘。**接入 WiFi 后无需改动**，只需保证：

- 请求帧仍通过 `rec._request(..., atomic=True)` 走 BLE；
- 2-4 的数据从统一分发路径进来（BLE 或 TCP 都一样）；
- 空闲超时按通道取值（BLE 12 s / WiFi 30 s）。

---

# 7\. 验收清单

| **检查项** | **通过标准** |
| --- | --- |
| 2-2 帧构造 | 帧长 **36 字节**，`LEN=0x001E`，CRC 与 `crc16_xmodem` 一致，与真机帧逐字节相同 |
| 文件名长度 | 23 字符名 → filename 字段 24 字节（末尾 1 个 NUL） |
| TCP 拆帧 | 人为把一帧拆成 3 次 `recv`、把 2 帧合并成 1 次 `recv`，均能正确还原 |
| CRC 错误 | 注入坏 CRC 的帧 → 记 `crc_errors` 并重新同步，不崩溃、不卡死 |
| 热打开 | 0-19 → 收到 0-20 且 `status=0`、能读到热点名 |
| TCP 连接 | 连接成功上报 `wifi_state=2`；断开热点后上报 `wifi_state=3` |
| 心跳 | 30 秒一次 `{0x01}`，连续 10 分钟不掉线 |
| 下载 | 0-2 → 0-3 → 连续 0-4 → 0-5 code=0，落地文件大小与 `size` 字段一致 |
| 结果码 | 故意请求不存在的文件名 → 收到 `2-5 / code=1`，能换候选名重试 |
| 落盘 | 用 `ab` 追加；WAV 头 `RIFF`/`WAVE` 正确、声明长度等于实际长度 |
| 断点续传 | 中断后带 `offset` 重发 `0-2`，能从断点续上，不重复不丢 |

---

# 8\. 已知差异与坑

| # | **坑** | **说明** |
| --- | --- | --- |
| 1 | **命令下行只走 BLE** | WiFi 连上但 BLE 断开 → 请求帧发不出去，且**无任何错误回调**（Android SDK 的 `k()` 直接 return） |
| 2 | **TCP 必须自己拆帧** | 不能假设「一次 recv = 一帧」。用 `FrameParser` |
| 3 | **文件名 24 字节定长** | 变长会被 CB08 判为「文件不存在」。本仓库已修正为 24 |
| 4 | **BLE 侧不校验 CRC、不拆帧** | Android SDK 的 BLE 路径假定「一次 Notify = 一帧」，且**不校验接收帧 CRC**。Python 侧 `FrameParser` 两个都做了，不要退化 |
| 5 | **ACK 默认关闭** | Android SDK `e0=0`，收到帧不回 `ACK\r\n`。除非设备明确要求，不要开 |
| 6 | **字符串不做 NUL 裁剪** | 设备返回的热点名、文件名、SN 都可能带尾部 NUL，需自行 `split(b"\x00", 1)[0]` |
| 7 | **不要用外层 `state` 判断成功** | SDK 的 JSON 外层 `state` 恒为 `"0"`；设备真实结果在应答帧的结果码里 |
| 8 | **WiFi 下的 `0-23` 会缺失** | 设备检测到 TCP 已连上就不再发「有手机连接 WiFi」，属正常 |
| 9 | **`StreamReader` 默认 limit 64 KiB** | 建议显式设 1 MiB，避免频繁扩容 |
| 10 | **列表 name 只有 20 字节** | 扩展名会被截断（`note20260710-162938.`），下载时需重建扩展名 |

---

# 附录 A\. 协议依据（逆向证据）

本节记录 Android SDK（`pnote_release_20261027064449.aar`）的对应实现，供追溯与交叉验证。**实现时只需读正文第 4~6 节。**

| **项** | **Android SDK 事实** |
| --- | --- |
| 传输类 | `com.wind.pnote.service.a`，自报名 `LongSocketClient` |
| 建连位置 | `WindBluetoothService.java:287` → `C.a("192.168.1.1", 27689, this)` |
| 打开 WiFi | `PNote.openWiFi()` → `d.K` → case 33 → `c/e.x()` → `00 13`（0-19） |
| 关闭 WiFi | `PNote.closeWiFi()` → `d.L` → case 34 → `c/e.b()` → `00 15`（0-21） |
| 问是否连接 | `PNote.getWiFiHotspotState()` → `d.Q` → case 39 → `c/e.w()` → `00 18`（0-24） |
| 建连动作 | `PNote.connectDeviceWiFi()` → `d.M` → case 35 → **不发任何 BLE 帧**，纯本地建 TCP |
| 读状态 | `PNote.getDeviceWiFiState()` → `d.P` → case 38 → **不发 BLE 帧**，读 `C.i()` |
| 状态上报 | `g/a.a(2|3, "")` → `{"cmd":"12",…,"wifi_state":"2|3"}` |
| 接收统一 | BLE notify 与 TCP 回调都写进同一个队列 `r`；`Observable.interval(1000ms, 2ms)` 轮询 → 同一个 `e(byte[])` 解析器 |
| TCP 拆帧 | LongSocketClient 扫 `0x5A` + 小端 LEN 动态组包，缓冲 1 MiB；**BLE 侧没有这层** |
| 上传分片 | `WindBluetoothService$e0` 线程：WiFi **1024 B/10 ms**，BLE **160 B/100 ms** |
| 文件数据 | `f/f` 取 `frame[8..]` → `DeviceDataListener.onDeviceFileData(byte[])`，**SDK 不写盘** |
| 2-2 文件名 | Android SDK 发**变长**、不补 NUL（与 CB08 的 24 B 定长不同，见 5.2） |

逆向产物：`reverse_engineering/GETSN_REVERSE_ENGINEERING.md`、`RECORD_AND_AUTH_REVERSE_ENGINEERING.md`、`AUTHKEY_REVERSE_ENGINEERING.md`、`reverse_engineering/runtime/src/WifiTest.java`（帧字节实跑）。
