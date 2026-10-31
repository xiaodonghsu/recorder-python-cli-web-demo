"""BLE 传输层：基于 bleak 的扫描、连接、Notify 订阅与 AE21 写入。

协议第 2 节：
    Service        0xAE20  录音笔业务服务
    Characteristic 0xAE21  WRITE_WITHOUT_RESPONSE  App→Dev 协议帧
    Characteristic 0xAE22  NOTIFY  Dev→App 控制应答、音频、列表、文件数据
    Characteristic 0xAE23  NOTIFY  Dev→App 机身按键及录音状态消息

关键约束：2-2 文件导入请求帧（36B）必须一次 GATT 写入，
不允许分包器拆分（拆成 20+16 会稳定返回“文件不存在”）。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, List, Optional

from bleak import BleakClient, BleakScanner
from bleak.backends.device import BLEDevice
from bleak.uuids import normalize_uuid_str

logger = logging.getLogger(__name__)


def _uuid16(short: int) -> str:
    """16bit UUID 扩展为 128bit 标准形式。"""
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


SERVICE_UUID = _uuid16(0xAE20)
CHAR_WRITE = _uuid16(0xAE21)
CHAR_NOTIFY_MAIN = _uuid16(0xAE22)
CHAR_NOTIFY_KEY = _uuid16(0xAE23)

DEFAULT_CHUNK = 20  # 未协商 MTU 时的保守单包载荷

# 已知设备型号关键字（厂家测试页确认型号为 QS668，CB08 为早期叫法；
# 真机实测还存在 CAC10 系列，广播名形如 CAC10-E0A1DA1803E5）
DEVICE_NAME_KEYWORDS = ("cb08", "qs668", "cac10")

# 扫描默认窗口（秒）。实测该录音笔广播间隔在 0.5~9.6s 之间波动，
# 6s 窗口会漏掉设备，默认放宽到 15s。
DEFAULT_SCAN_TIMEOUT = 15.0

# 命中后再等多久让设备名补齐（首个广播包不带名字，名字在 scan response）
NAME_SETTLE_DELAY = 4.0


def _norm_uuid(uuid: str) -> str:
    """归一化广播里的服务 UUID。

    不同平台上报形式不同：Windows/macOS 通常给 128bit，
    部分后端给 16bit 短形式（"ae20"）。统一展开后再比较，
    否则 macOS/Linux 上会漏匹配。
    """
    try:
        return normalize_uuid_str(uuid)
    except Exception:
        return uuid.lower()


class BleTransport:
    """封装一台录音笔的 BLE 连接与收发。

    on_main / on_key 回调分别收到 AE22 / AE23 的原始通知字节，
    上层各自用独立 FrameParser 处理。
    """

    def __init__(self) -> None:
        self._client: Optional[BleakClient] = None
        self.on_main: Optional[Callable[[bytes], None]] = None
        self.on_key: Optional[Callable[[bytes], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None

    # ------------------------------------------------------------ 扫描

    @staticmethod
    async def scan(timeout: float = DEFAULT_SCAN_TIMEOUT,
                   compat: bool = False) -> List[BLEDevice]:
        """扫描广播含 AE20 服务或名称匹配 QS668/CB08/CAC10 的设备。

        实现要点：不能用 BleakScanner.discover() 一次性等待，
        因为该录音笔广播间隔不规则（实测 0.5~9.6s，偶发首包延迟数十秒），
        固定窗口内没收到广播就会漏掉设备。

        这里改用回调式扫描器持续监听，直到：
          - 匹配到目标设备（立刻返回，不空等满 timeout）；或
          - compat=True 时收满 timeout 以便列出全部有名设备；或
          - 超时未匹配到。

        compat=True 时返回全部有名设备（兼容广播不带服务 UUID
        的固件，由用户自行选择，对应厂家测试页的“兼容扫描”）。
        """
        matched_devs: dict[str, BLEDevice] = {}
        other_devs: dict[str, BLEDevice] = {}
        matched = asyncio.Event()
        named = asyncio.Event()
        deadline = time.monotonic() + timeout

        def on_detect(device: BLEDevice, adv) -> None:
            name = (device.name or adv.local_name or "")
            uuids = [_norm_uuid(u) for u in (adv.service_uuids or [])]
            logger.debug("Found device: %s, UUIDs: %s", name, adv.service_uuids)
            if SERVICE_UUID in uuids or \
                    any(k in name.lower() for k in DEVICE_NAME_KEYWORDS):
                matched_devs[device.address] = device
                matched.set()
                if name:
                    named.set()
            elif name:
                other_devs[device.address] = device

        scanner = BleakScanner(detection_callback=on_detect)
        await scanner.start()
        try:
            if compat:
                # 兼容模式需要收满整个窗口，才能列出全部有名设备
                await asyncio.sleep(timeout)
            else:
                try:
                    await asyncio.wait_for(
                        matched.wait(),
                        timeout=max(0.1, deadline - time.monotonic()))
                    # 首个广播包通常只带服务 UUID、不带名字，名字要到随后的
                    # scan response 才出现（该设备广播间隔最大近 10s）。
                    # 在此期间继续监听，等名字补齐再返回，避免显示"(无名称)"。
                    try:
                        await asyncio.wait_for(
                            named.wait(),
                            timeout=min(NAME_SETTLE_DELAY,
                                        max(0.1, deadline - time.monotonic())))
                    except asyncio.TimeoutError:
                        pass
                except asyncio.TimeoutError:
                    pass
        finally:
            await scanner.stop()

        if not matched_devs and not compat:
            logger.debug("扫描超时，未匹配到录音笔（可见设备: %s）",
                         [d.name for d in other_devs.values()])
        # 非兼容模式只返回匹配到的录音笔（与原行为一致，CLI 里 connect 0
        # 一定是录音笔）；兼容模式返回全部有名设备供手动选择。
        if not compat:
            return list(matched_devs.values())
        # 匹配设备排在前面，便于 CLI 直接 connect 0
        return list(matched_devs.values()) + list(other_devs.values())

    # ------------------------------------------------------------ 连接

    async def connect(self, device) -> None:
        """连接并订阅 AE22（必须）与 AE23（按键，尽力订阅）。"""
        client = BleakClient(
            device, disconnected_callback=self._handle_disconnect)
        await client.connect()
        self._client = client

        # 连接成功后必须先订阅 AE22
        await client.start_notify(CHAR_NOTIFY_MAIN, self._notify_main)
        try:
            await client.start_notify(CHAR_NOTIFY_KEY, self._notify_key)
        except Exception as exc:  # 部分固件可能无 AE23
            logger.warning("AE23 订阅失败（忽略）：%s", exc)

    async def disconnect(self) -> None:
        if self._client is not None:
            client, self._client = self._client, None
            try:
                await client.disconnect()
            except Exception:
                pass

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    @property
    def mtu(self) -> int:
        """真实 ATT_MTU；bleak 在 Windows/WinRT 上自动协商。"""
        if self._client is not None:
            try:
                return self._client.mtu_size
            except Exception:
                pass
        return 23

    @property
    def payload_size(self) -> int:
        """常规命令的单次写入载荷上限：MTU-3。"""
        return max(self.mtu - 3, DEFAULT_CHUNK)

    # ------------------------------------------------------------ 写入

    async def write_frame(self, frame: bytes, *, atomic: bool = False) -> None:
        """向 AE21 写入完整协议帧。

        atomic=True 用于 2-2 等必须整帧单写的命令，超过载荷上限时
        直接报错而不是拆分，避免设备解析出错误文件名。
        """
        if self._client is None:
            raise RuntimeError("BLE 未连接")
        limit = self.payload_size
        if len(frame) <= limit:
            await self._client.write_gatt_char(
                CHAR_WRITE, frame, response=False)
            return
        if atomic:
            raise RuntimeError(
                f"帧长 {len(frame)}B 超过单写上限 {limit}B，"
                "该命令要求整帧单写，请确认 MTU 协商结果")
        # 普通长帧按 MTU-3 分片顺序写入
        for i in range(0, len(frame), limit):
            await self._client.write_gatt_char(
                CHAR_WRITE, frame[i:i + limit], response=False)
            await asyncio.sleep(0.01)

    # ------------------------------------------------------------ 回调

    def _notify_main(self, _sender, data: bytearray) -> None:
        if self.on_main is not None:
            self.on_main(bytes(data))

    def _notify_key(self, _sender, data: bytearray) -> None:
        if self.on_key is not None:
            self.on_key(bytes(data))

    def _handle_disconnect(self, _client) -> None:
        logger.info("设备已断开")
        self._client = None
        if self.on_disconnect is not None:
            self.on_disconnect()
