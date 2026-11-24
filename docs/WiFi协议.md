# WiFi 控制与状态查询协议

> 面向 **Python 桌面应用**（Windows / macOS）开发。录音卡 WiFi 热点的开启/关闭、连接状态查询、TCP 建链与断链。

| **文档项** | **内容** |
| --- | --- |
| 版本 | V1.0（2026-11-24） |
| 适用项目 | `pnote-web-win&Mac-demo`（Python 桌面 + Web 控制台） |
| 通信角色 | 手机 / PC 为 TCP **客户端**；录音卡为 TCP **服务端** |
| 连接目标 | `192.168.1.1:27689` |
| 覆盖范围 | WiFi 热点开关、连接状态查询、TCP 建链/断链、心跳保活 |
| 不含范围 | 文件传输（见 `reverse_engineering/WIFI_AND_FILE_TRANSFER.md`）、录音控制、鉴权 |
| 协议依据 | 厂商《录音笔与APP通讯协议（20260313）》+ pnote Android SDK 逆向 + iOS/Android SDK 文档，三方一致 |
| 关联文档 | `docs/协议.md`（通用帧格式）、`docs/协议差异对照.md`（与厂商 docx 的差异） |

**一句话结论**

WiFi 控制是「**一条 BLE 命令 + 一个本机 TCP 连接**」的组合：请求帧走 BLE，连接状态由本机维护。设备侧只在**状态发生变化时**主动推送 `0-20` / `0-22` / `0-23`。

---

# 1\. 状态码：`wifi_state`

所有 WiFi 状态统一通过 `onDeviceDataEvent` 上报，JSON 形状固定：

```json
{"cmd":"12","state":"0","data":{"wifi_state":"2"}}
{"cmd":"12","state":"0","data":{"wifiName":"PONE","wifi_state":"0"}}
```

| **wifi_state** | **含义** | **产生来源** |
| --- | --- | --- |
| 0 | WiFi 热点打开成功 | `0-20` 应答的状态字节 = 0 |
| 1 | WiFi 热点关闭 / 打开失败 | `0-22` 应答；或 `0-20` 状态字节 ≠ 0 |
| 2 | WiFi（TCP）连接成功 | 本机 TCP 已连通 |
| 3 | WiFi（TCP）断开连接 | 本机 TCP 已断开 |
| 4 | 有手机连接上设备 WiFi 热点 | `0-23` 应答值 = 1 |
| 5 | 当前没有手机连接设备 WiFi 热点 | `0-23` 应答值 = 0 |

**两条硬规则**：

1. **外层 `state` 恒为 `"0"`**，表示回调/传输层正常，**与 WiFi 业务状态无关**。判断结果只看 `data.wifi_state`。
2. **`wifiName` 仅在有值时出现**，且**带尾部 NUL**。SDK 取 `frame[9..]` 全量不裁剪，实测 4 字符热点名会变成 5 字节 `"PONE\0"`，JSON 里含真实的 `\u0000`：

   ```json
   {"cmd":"12","state":"0","data":{"wifiName":"PONE\u0000","wifi_state":"0"}}
   ```

   → **必须自己裁**：`name.split("\0", 1)[0]`（Python）/ `split("\0", 1)[0]`（TS）。

---

# 2\. 命令总表

WiFi 控制命令全部属于 **TYPE=0（控制命令）**。

| **CMD** | **方向** | **含义** | **SDK API** | **参数 / 应答** |
| --- | --- | --- | --- | --- |
| 19 | App→Dev | 请求打开 WiFi 传输 | `openWiFi()` | 请求无参；应答 `0-20` |
| 20 | Dev→App | 打开 WiFi 回复 | —（被动） | `1B 状态`（0 成功 / 1 失败）`+ WiFi 名称`（仅状态=0 时存在） |
| 21 | App→Dev | 请求关闭 WiFi 传输 | `closeWiFi()` | 请求无参；应答 `0-22` |
| 22 | Dev→App | 关闭 WiFi 回复 | —（被动） | 无 body |
| 23 | Dev→App | 有手机连接 WiFi | —（被动） | `1B`（1 已连接 / 0 未连接） |
| 24 | App→Dev | 请求是否有手机连接 WiFi | `getWiFiHotspotState()` | 请求无参；应答 `0-23` |
| **62** | **App→Dev** | **设置热点密码** | **`setWifiPassword()`** | **`len:2B` + `密码字节`；应答 `0-63`** |
| **63** | **Dev→App** | **设置密码结果** | —（被动） | `1B` 结果码 |

**实跑请求帧**（SEQ=0，CRC-16/XMODEM 已校验）：

```
0-19 打开 WiFi     : 5A 00 3A CF 02 00 00 13
0-21 关闭 WiFi     : 5A 01 FC AF 02 00 00 15
0-24 问是否连接     : 5A 02 51 7E 02 00 00 18
```

三条都是 **8 字节**（帧头 6B + `TYPE=0` + `CMD`），`LEN = 0x0002`。

**应答体布局**（帧头 6B + TYPE + CMD = 8B，故 body 从 `frame[8]` 开始）：

| **应答** | **帧内偏移** | **长度** | **含义** |
| --- | --- | --- | --- |
| `0-20` | 8 | 1B | 状态：0 = 打开成功，1 = 打开失败 |
| `0-20` | 9 | 余下 | WiFi 名称（**仅状态 = 0 时存在**） |
| `0-23` | 8 | 1B | 1 = 有手机连接，0 = 没有手机连接 |
| `0-63` | 8 | 1B | 设置结果码（0 = 成功） |

## 2.1 设置热点密码（`0-62` / `0-63`）

请求 payload 布局：

| **偏移** | **长度** | **含义** |
| --- | --- | --- |
| 0 | 1B | TYPE = `0x00` |
| 1 | 1B | CMD = `0x3E`（62） |
| 2 | 2B | 密码**字节**长度，**大端**（高字节在前） |
| 4 | N | 密码 UTF-8 字节 |

实跑帧（密码 `12345678`，SEQ=0）：

```
5A 00 19 03 0C 00 00 3E 00 08 31 32 33 34 35 36 37 38
│  │  └─┬─┘ └─┬─┘ │  │  └──── len=8（大端）────┘└──── 密码 UTF-8 ────┘
│  │    │    │    │  └──── CMD = 62 (0x3E)
│  │    │    │    └────── TYPE = 0
│  │    │    └─────────── LEN = 0x000C = 12（2+2+8）
│  │    └──────────────── CRC = 0x0319
│  └───────────────────── SEQ = 0
└──────────────────────── MAGIC
```

**四条必须注意的规则**：

| **规则** | **说明** |
| --- | --- |
| **长度字段是大端** | `payload[2]`=高字节、`payload[3]`=低字节。**与同族的设备名命令（0-60）相反** —— 设备名是小端。写反了设备就解不出密码长度 |
| **长度按码点校验，字段填字节数** | 合法性校验是「8~16 个**字符**」（Unicode 码点），但长度字段填的是 **UTF-8 字节数**。8 个汉字 = 8 码点（合法）= 24 字节 → 长度字段 `00 18` |
| **不立即生效** | 热点**已开启**时，新密码在**下次开启热点**时生效。想立刻用新密码，必须先 `closeWiFi()` 再 `openWiFi()` |
| **非法参数同步抛异常** | Android SDK 抛 `IllegalArgumentException("Wifi密码必须为8-16个字符")`，**不发指令**；iOS SDK 静默不发送 |

**应答**：`0-63` 带 1 字节结果码，映射为 JSON：

```json
{"cmd":"34","state":"0","data":{"wifi_password_state":"0"}}
```

> **注意**：`0-62` / `0-63` **不在厂商《录音笔与APP通讯协议（20260313）》的 TYPE=0 命令表里**（该表止于 CMD 28），与 AuthKey（0-50~0-55）同属 **pnote 系 SDK 的扩展命令**。设备固件不支持时不会回 `0-63`，表现为**超时**。


---

# 3\. SDK API ↔ 协议映射

| **SDK API** | **发 BLE 帧** | **请求帧** | **应答** | **上报 wifi_state** | **附带动作** |
| --- | --- | --- | --- | --- | --- |
| `openWiFi()` | 是 | `00 13`（0-19） | `0-20` | 状态字节原样：0 / 1 | 快速连接流程中会用返回的 SSID 去连热点 |
| `closeWiFi()` | 是 | `00 15`（0-21） | `0-22` | 固定 `1` | **一并关闭 TCP 连接** |
| `getWiFiHotspotState()` | 是 | `00 18`（0-24） | `0-23` | `4` / `5` | 无 |
| `getDeviceWiFiState()` | **否** | — | 无 | `2` 已连 / `3` 未连 | 纯读本机 socket |
| `disconnectDeviceWiFi()` | **否** | — | 无 | `3` | 关闭 TCP 并清空 client |
| `setWifiPassword(pwd)` | 是 | `00 3E`（0-62） | `0-63` | 结果码透传（cmd 34） | 新密码**下次开热点**才生效 |

> **命名陷阱**：`getWiFiHotspotState()` 才是**问设备**「有没有手机连上热点」；`getDeviceWiFiState()` **不与设备通信**，只读本机 TCP socket 状态。两个名字很容易混。
>
> **另一个陷阱**：`closeWiFi()` 会**连带关闭 TCP 连接**（`0-22` 应答里 SDK 会主动关 socket）。如果你只是想断 TCP 而保留热点，用 `disconnectDeviceWiFi()`。

---

# 4\. TCP 连接管理

## 4.1 连接参数

对齐 Android SDK 的 `LongSocketClient`（`com.wind.pnote.service.a`）：

| **参数** | **值** | **说明** |
| --- | --- | --- |
| 主机 / 端口 | `192.168.1.1` / `27689` | 录音卡 = 服务端 |
| 连接超时 | 10 s | SDK `.b(10000)` |
| 重试次数 | 5 | SDK `.c(5)` |
| 重连间隔 | 3 s | SDK `.g(3000)` |
| 会话超时 | 600 s | SDK `.f(600000)` |
| 心跳间隔 | 30 s | SDK `.a(30000L, {0x01})` |
| 心跳内容 | `b"\x01"` | 分隔符 `"\n"` |
| 接收缓冲上限 | 1 MiB | SDK `.d(0x100000)`；Python `StreamReader` 默认只有 64 KiB，**必须显式调大** |
| ACK 模式 | 默认关 | 开启后每收一帧回 `"ACK\r\n"`；**除非设备明确要求，不要开** |

## 4.2 TCP 字节流必须自己拆帧

TCP 是字节流，设备会粘包/拆包。**不能假设「一次 recv = 一帧」。**

拆帧规则（与 `recorder/protocol.py` 的 `FrameParser` 完全一致）：

```
1. 丢弃 MAGIC(0x5A) 之前的所有噪声字节
2. 缓冲不足 6 字节 → 等待
3. LEN = frame[4] | frame[5]<<8
4. LEN > 上限(8192) → 判定假帧头，丢弃 1 字节重新同步
5. 缓冲不足 6+LEN → 等待后续数据
6. 取出一整帧并校验 CRC；CRC 不符 → 丢弃 1 字节重新同步并计数
7. 循环处理剩余数据
```

**直接复用 `FrameParser`，不要另写。**

## 4.3 状态机

```
                    ┌──────────────┐
                    │  IDLE        │  没有热点、没有 TCP
                    └──────┬───────┘
                           │ openWiFi() → 0-19
                           ▼
                    ┌──────────────┐
                    │  HOTSPOT_ON  │  等 0-20
                    └──────┬───────┘
                           │ 0-20 (状态=0 + SSID)
                           │ → 系统连上该热点
                           ▼
                    ┌──────────────┐
                    │  CONNECTING  │  TCP connect 192.168.1.1:27689
                    └──────┬───────┘
                           │ 连通
                           ▼
                    ┌──────────────┐
              ┌────►│  CONNECTED   │  wifi_state=2，心跳 30s
              │     └──────┬───────┘
              │            │ socket 断开 / closeWiFi() / disconnectDeviceWiFi()
              │            ▼
              │     ┌──────────────┐
              └─────┤  RECONNECT   │  3s 后重连，最多重试 5 次
                    └──────────────┘
```

**状态迁移要点**：

- 热点开启后，**设备会主动推 `0-20`**（带 SSID），应用据此连热点。
- 手机连上热点后，**设备会主动推 `0-23`（值=1）** → 上报 `wifi_state=4`。
- TCP 建立后由**本机**上报 `wifi_state=2`；断开上报 `wifi_state=3`。
- `0-23` 只在「手机连上/离开热点」时推，**不是轮询**。TCP 已连上时设备不再推（厂商原文：*"如果TCP/IP已经连接上则不发送这个指令"*）。
- 设备关掉热点 → 主动推 `0-22` → 上报 `wifi_state=1`，同时 TCP 断开 → `wifi_state=3`。

---

# 5\. Python 实现

## 5.1 常量（加到 `recorder/protocol.py`）

```python
# --------------------------------------------------------- WiFi 控制命令 TYPE=0
CTRL_WIFI_OPEN = 19            # App→Dev 请求打开 WiFi 传输
CTRL_WIFI_OPEN_RESP = 20       # Dev→App 打开回复: 1B 状态 + WiFi 名称
CTRL_WIFI_CLOSE = 21           # App→Dev 请求关闭 WiFi 传输
CTRL_WIFI_CLOSE_RESP = 22      # Dev→App 关闭回复: 无
CTRL_WIFI_PEER = 23            # Dev→App 有手机连接: 1B(1 已连 / 0 未连)
CTRL_WIFI_PEER_QUERY = 24      # App→Dev 请求是否有手机连接
CTRL_WIFI_PASSWORD = 62        # App→Dev 设置热点密码: len:2B(大端) + 密码字节
CTRL_WIFI_PASSWORD_RESP = 63   # Dev→App 设置结果: 1B 结果码（0=成功）

# ------------------------------------------------------------- wifi_state 取值
WIFI_STATE_HOTSPOT_ON = 0        # 热点打开成功
WIFI_STATE_HOTSPOT_OFF = 1       # 热点关闭 / 打开失败
WIFI_STATE_TCP_CONNECTED = 2     # TCP 已连接
WIFI_STATE_TCP_DISCONNECTED = 3  # TCP 已断开
WIFI_STATE_PEER_CONNECTED = 4    # 有手机连上热点
WIFI_STATE_PEER_NONE = 5         # 没有手机连接热点

# ------------------------------------------------------------ 热点密码参数
WIFI_PASSWORD_MIN_CHARS = 8      # 按 Unicode 码点计
WIFI_PASSWORD_MAX_CHARS = 16
```

## 5.2 帧构造（加到 `recorder/protocol.py`）

```python
def build_wifi_command(seq: int, cmd: int) -> bytes:
    """构造 WiFi 控制请求帧（0-19 / 0-21 / 0-24，均无参）。

    完整帧 8 字节：5A | SEQ | CRC(LE) | 02 00 | 00 | CMD
    """
    return build_command(seq, TYPE_CONTROL, cmd)


def encode_wifi_password(password: str) -> bytes:
    """编码热点密码参数（0-62 的 params）。

    合法性按 Unicode **码点**计（8~16 个字符），但长度字段填 **UTF-8 字节数**，
    且长度字段是 **大端**（高字节在前）—— 与设备名命令（0-60，小端）相反。

    返回 bytes: [len_hi][len_lo][密码 UTF-8 字节]
    """
    if password is None:
        raise ValueError("Wifi密码必须为8-16个字符")
    chars = len(password)                     # Python str 按码点计数
    if not (WIFI_PASSWORD_MIN_CHARS <= chars <= WIFI_PASSWORD_MAX_CHARS):
        raise ValueError("Wifi密码必须为8-16个字符")
    raw = password.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def build_set_wifi_password(seq: int, password: str) -> bytes:
    """构造 0-62 设置热点密码帧。

    payload = 00 | 3E | len(2B 大端) | 密码 UTF-8
    """
    return build_command(seq, TYPE_CONTROL, CTRL_WIFI_PASSWORD,
                         encode_wifi_password(password))
```

## 5.3 应答解析（加到 `recorder/device.py`）

```python
def decode_wifi_open_resp(body: bytes) -> Tuple[int, str]:
    """解码 0-20 打开 WiFi 回复的 body。

    body[0] = 状态（0 成功 / 1 失败）
    body[1:] = WiFi 名称（仅状态=0 时存在；**带尾部 NUL，必须裁掉**）

    返回 (status, ssid)；ssid 已去 NUL，失败时为空串。
    """
    if not body:
        return -1, ""
    status = body[0]
    ssid = ""
    if status == 0 and len(body) > 1:
        ssid = body[1:].split(b"\x00", 1)[0].decode("utf-8", errors="replace")
    return status, ssid


def decode_wifi_peer_state(body: bytes) -> bool:
    """解码 0-23 的 body：1 = 有手机连接，0 = 没有。"""
    return bool(body) and body[0] == 1


def decode_wifi_password_result(body: bytes) -> int:
    """解码 0-63 设置密码结果的 body：1B 结果码，0 = 成功。

    对应上报 JSON：{"cmd":"34","state":"0","data":{"wifi_password_state":"N"}}
    """
    return body[0] if body else -1
```

**在 `Recorder._handle_frame` 里挂上（与下载/列表分支并列）**：

```python
# WiFi 控制应答
if frame.type == P.TYPE_CONTROL:
    if frame.cmd == P.CTRL_WIFI_OPEN_RESP:
        status, ssid = P.decode_wifi_open_resp(frame.body)
        self.on_wifi_state(
            P.WIFI_STATE_HOTSPOT_ON if status == 0 else P.WIFI_STATE_HOTSPOT_OFF,
            ssid)
        return
    if frame.cmd == P.CTRL_WIFI_CLOSE_RESP:
        self.on_wifi_state(P.WIFI_STATE_HOTSPOT_OFF, "")
        return
    if frame.cmd == P.CTRL_WIFI_PEER:
        self.on_wifi_state(
            P.WIFI_STATE_PEER_CONNECTED if P.decode_wifi_peer_state(frame.body)
            else P.WIFI_STATE_PEER_NONE, "")
        return
    if frame.cmd == P.CTRL_WIFI_PASSWORD_RESP:
        # 结果码 0 = 成功；对应 JSON cmd=34 的 wifi_password_state
        self.on_wifi_password_result(P.decode_wifi_password_result(frame.body))
        return
```

> `on_wifi_state(wifi_state: int, ssid: str)` 由上层实现：
> `wifi_state` 直接用第 1 节的 0~5 常量；`ssid` 供「连热点」用（**已裁 NUL**）。

## 5.4 新增 `recorder/tcp.py`

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
RECV_LIMIT = 1 << 20          # 对齐 SDK .d(0x100000)


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

## 5.5 接入 `Recorder`

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

    def _handle_tcp_disconnect(self) -> None:
        """TCP 断开：上报 wifi_state=3。"""
        self.on_wifi_state(P.WIFI_STATE_TCP_DISCONNECTED, "")
```

> **务必用独立的 `FrameParser("TCP")`**：与 `_parser_key`（AE23）同理 —— 两个通道的字节可能交错，共用一个解析器会把半帧拼错。

## 5.6 完整编排

```python
async def open_wifi(rec: Recorder) -> str:
    """打开录音卡热点，返回热点名。"""
    resp = await rec._request(P.TYPE_CONTROL, P.CTRL_WIFI_OPEN, b"",
                              P.CTRL_WIFI_OPEN_RESP, timeout=10.0)
    status, ssid = P.decode_wifi_open_resp(resp.body)
    if status != 0:
        raise RuntimeError("设备打开 WiFi 失败")
    return ssid


async def connect_wifi(rec: Recorder) -> None:
    """建立 TCP 连接，失败自动重试。"""
    tries = 0
    while True:
        try:
            await rec.tcp.connect()
            rec.on_wifi_state(P.WIFI_STATE_TCP_CONNECTED, "")
            return
        except Exception as exc:
            tries += 1
            logger.warning("TCP 连接失败(%d/%d)：%s", tries, MAX_RETRY, exc)
            if tries >= MAX_RETRY:
                raise
            await asyncio.sleep(RETRY_INTERVAL)


async def close_wifi(rec: Recorder) -> None:
    """关闭热点（会连带断开 TCP）。"""
    await rec._request(P.TYPE_CONTROL, P.CTRL_WIFI_CLOSE, b"",
                       P.CTRL_WIFI_CLOSE_RESP, timeout=10.0)
    await rec.tcp.disconnect()


async def set_wifi_password(rec: Recorder, password: str) -> int:
    """设置热点密码，返回结果码（0 = 成功）。

    ⚠️ 热点已开启时，新密码**下次开启热点**才生效；
    想立刻用新密码，请先 close_wifi() 再 open_wifi()。
    """
    frame = P.build_set_wifi_password(rec._seq.next(), password)
    # 密码命令是「带参请求」，需要自己发帧并等 0-63 应答
    resp = await rec._request_frame(frame, P.CTRL_WIFI_PASSWORD_RESP, timeout=10.0)
    return P.decode_wifi_password_result(resp.body)
```

> `rec._request_frame(frame, resp_cmd, timeout=...)` 需要在 `Recorder` 里补一个
> 「按现成帧发送并等应答」的小工具（现有 `_request()` 只接受 type/cmd/params，
> 内部自行组帧）。或者把 `build_set_wifi_password` 拆成
> `encode_wifi_password()` + `_request(TYPE_CONTROL, 62, params, 63)`，后者**无需新增方法**：
>
> ```python
> resp = await rec._request(P.TYPE_CONTROL, P.CTRL_WIFI_PASSWORD,
>                           P.encode_wifi_password(password),
>                           P.CTRL_WIFI_PASSWORD_RESP, timeout=10.0)
> return P.decode_wifi_password_result(resp.body)
> ```
>
> 两种写法等价，后者改动更小。

---

# 6\. 验收清单

| **检查项** | **通过标准** |
| --- | --- |
| 请求帧 | `openWiFi` → `5A xx xx xx 02 00 00 13`（8B）；`closeWiFi` → `… 00 15`；`getWiFiHotspotState` → `… 00 18`；CRC 与 `crc16_xmodem` 一致 |
| 热点名裁剪 | 收到 `0-20`（状态 0，名称 `PONE\0`）→ 解析出的 SSID 为 `"PONE"`，**长度 4 而非 5** |
| 打开失败 | 收到 `0-20`（状态 1）→ 上报 `wifi_state=1`，SSID 为空串 |
| 关闭热点 | `closeWiFi()` → 收到 `0-22` → 上报 `wifi_state=1`，且 **TCP 被一并关闭** |
| 状态查询 | `getWiFiHotspotState()` → 收到 `0-23`（值 1）→ 上报 `wifi_state=4`；值 0 → `wifi_state=5` |
| 本地状态 | `getDeviceWiFiState()`：TCP 已连 → `wifi_state=2`；未连 → `wifi_state=3` |
| 断开 | `disconnectDeviceWiFi()` → 上报 `wifi_state=3`，socket 已关闭 |
| TCP 拆帧 | 一帧拆成 3 次 recv、2 帧合并成 1 次 recv，均能正确还原 |
| CRC 错误 | 注入坏 CRC 的帧 → 记 `crc_errors` 并重新同步，不崩溃、不卡死 |
| 心跳 | 30 秒一次 `{0x01}`，连续 10 分钟不掉线 |
| 断线重连 | 拔网线/关热点 → 3 秒后自动重连，最多重试 5 次后上报失败 |
| 密码帧 | `0-62` 帧长 = `8 + 4 + 密码字节数`；`LEN = 4 + 密码字节数`；长度字段**大端** |
| 密码长度校验 | 7 / 17 字符 / `None` → 本地抛 `ValueError`，**不发帧**；8 汉字（24 字节）→ 长度字段 `00 18` |
| 密码应答 | 收到 `0-63` → 上报 `{"cmd":"34",…,"wifi_password_state":"0"}`，结果码 0 = 成功 |
| 密码生效时机 | 热点开启状态下设密码 → `close_wifi()` + `open_wifi()` 后新密码才可连 |

---

# 7\. 坑清单

| # | **坑** | **说明** |
| --- | --- | --- |
| 1 | **`wifiName` 带尾部 NUL** | 不裁就连不上热点。`split("\0", 1)[0]` |
| 2 | **外层 `state` 恒为 `"0"`** | 判断结果只看 `data.wifi_state` |
| 3 | **命令只走 BLE** | WiFi 连上但 BLE 断开 → 请求帧发不出去，且无任何错误回调 |
| 4 | **TCP 必须自己拆帧** | 不能假设「一次 recv = 一帧」。用 `FrameParser` |
| 5 | **`closeWiFi()` 会连带断 TCP** | 只想断 TCP 请用 `disconnectDeviceWiFi()` |
| 6 | **`getWiFiHotspotState` vs `getDeviceWiFiState`** | 前者问设备，后者读本机 socket，语义完全不同 |
| 7 | **`0-23` 是推送不是应答** | 只在手机进出热点时推；TCP 已连时设备不再推 |
| 8 | **`StreamReader` 默认 limit 64 KiB** | 必须显式设 1 MiB |
| 9 | **ACK 默认关闭** | 设备要求 ACK 时才自行回 `"ACK\r\n"` |
| 10 | **断开只应上报一次** | 主动断开与被动断开走同一个回调，避免重复上报 |
| 11 | **CRC 与 SEQ 无关** | CRC 只覆盖 `LEN(2B) + DATA`，不含 SEQ。对拍帧时只变 SEQ 字节、CRC 不变，别误判成帧错 |
| 12 | **密码长度字段是大端** | `0-62` 的长度字段高字节在前；**同族的设备名 `0-60` 是小端**。写反设备解不出密码长度 |
| 13 | **密码长度按码点校验、字段填字节数** | 8 个汉字 = 8 码点（合法）但 24 字节 → 长度字段 `00 18` |
| 14 | **新密码不立即生效** | 热点已开启时，密码**下次开热点**才生效；要立刻用需先关再开 |
| 15 | **`0-62`/`0-63` 不在厂商协议里** | 厂商 TYPE=0 表止于 CMD 28，这两条与 AuthKey 同属 SDK 扩展；固件不支持时**超时**而非报错 |

---

# 附录 A\. 协议依据（逆向证据）

Android SDK（`pnote_release_20261027064449.aar`）的对应实现，字节码 + 原始 class 实跑验证：

| **项** | **事实** |
| --- | --- |
| `openWiFi()` | `PNote.openWiFi()` → `d.K` → case 33 → `c/e.x()` → `00 13` |
| `closeWiFi()` | `PNote.closeWiFi()` → `d.L` → case 34 → `c/e.b()` → `00 15` |
| `getWiFiHotspotState()` | `PNote.getWiFiHotspotState()` → `d.Q` → case 39 → `c/e.w()` → `00 18` |
| `getDeviceWiFiState()` | `PNote.getDeviceWiFiState()` → `d.P` → case 38 → **不发 BLE 帧**，读 `C.i()` |
| `disconnectDeviceWiFi()` | `PNote.disconnectDeviceWiFi()` → `d.O` → case 37 → **不发 BLE 帧**，关 socket |
| `connectDeviceWiFi()` | `PNote.connectDeviceWiFi()` → `d.M` → case 35 → **不发 BLE 帧**，纯本地建 TCP |
| 建连位置 | `WindBluetoothService.java:287` → `C.a("192.168.1.1", 27689, this)` |
| `0-20` 解析 | 类 `f/m`：`frame[8]`=状态，`frame[9..]`=名称（仅状态=0 时存在）→ `g/a.a(status, ssid)` |
| `0-22` 处理 | 上报 `g/a.a(1, "")`（wifi_state=1），**并关闭 TCP**（`C.g()` → `C=null`） |
| `0-23` 处理 | `frame[8]`=1 → `g/a.a(4, "")`；=0 → `g/a.a(5, "")` |
| 本地状态 | TCP 已连 → `g/a.a(2, "")`；断开 → `g/a.a(3, "")` |
| TCP 参数 | `.a(true)` 调试日志 → `.a(30000L,{0x01})` 心跳 → `.d(0x100000)` 缓冲 → `.b(10000)` 超时 → `.e(e0)` ACK → `.c(5)` 重试 |

**验证脚本**：`reverse_engineering/runtime/src/WifiApiVerify.java`（5 个 API 的帧与全部 JSON 实跑）、`NulCheck.java`（NUL 确认）、`StateLogic.java`。

**完整 WiFi + 文件传输流程**（含文件传输）见 `reverse_engineering/WIFI_AND_FILE_TRANSFER.md`。
