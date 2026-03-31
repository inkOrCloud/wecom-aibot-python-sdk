"""
WsConnectionManager 心跳重连修复测试

覆盖范围：
  - _send_heartbeat 在连接失活（missed_pong >= max）时触发 _schedule_reconnect（PR #2515 修复）
  - 心跳停止和 ws.close() 的调用顺序
  - ws.close() 抛异常时记录 warning 日志，重连仍被触发
  - ws 为 None 时重连仍被触发
  - 正常心跳（missed_pong < max）不触发重连，计数器递增，帧正确发出

运行方式：
  python -m pytest tests/test_ws_heartbeat.py -v
"""

import sys
import types as _types

# ── 注入存根（与 test_media_features.py 保持一致）────────────────────────────

# --- pyee stub ---
_pyee = _types.ModuleType("pyee")
_pyee_asyncio = _types.ModuleType("pyee.asyncio")


class _AsyncIOEventEmitter:
    def __init__(self):
        self._listeners: dict = {}

    def on(self, event, f=None):
        def _reg(func):
            self._listeners.setdefault(event, []).append(func)
            return func
        return _reg(f) if f is not None else _reg

    def emit(self, event, *args):
        for fn in self._listeners.get(event, []):
            fn(*args)


_pyee_asyncio.AsyncIOEventEmitter = _AsyncIOEventEmitter
sys.modules.setdefault("pyee", _pyee)
sys.modules.setdefault("pyee.asyncio", _pyee_asyncio)

# --- aiohttp stub ---
_aiohttp = _types.ModuleType("aiohttp")


class _FakeTimeout:
    def __init__(self, total=None):
        pass


class _FakeConnector:
    def __init__(self, ssl=None):
        pass


_aiohttp.ClientTimeout = _FakeTimeout
_aiohttp.TCPConnector = _FakeConnector
sys.modules.setdefault("aiohttp", _aiohttp)

# --- websockets stub ---
_ws_mod = _types.ModuleType("websockets")
_ws_client_mod = _types.ModuleType("websockets.client")
_ws_exc_mod = _types.ModuleType("websockets.exceptions")


class _FakeProtocol:
    open = True


_ws_client_mod.WebSocketClientProtocol = _FakeProtocol
_ws_exc_mod.ConnectionClosed = Exception
_ws_mod.connect = None
_ws_mod.exceptions = _ws_exc_mod
sys.modules.setdefault("websockets", _ws_mod)
sys.modules.setdefault("websockets.client", _ws_client_mod)
sys.modules.setdefault("websockets.exceptions", _ws_exc_mod)

import unittest
from unittest.mock import AsyncMock, MagicMock

from aibot.ws import WsConnectionManager
from aibot.types import WsCmd


# ════════════════════════════════════════════════════════════════════════════
# 辅助
# ════════════════════════════════════════════════════════════════════════════

def _make_manager() -> WsConnectionManager:
    """构造一个不建立真实连接的 WsConnectionManager。"""
    return WsConnectionManager(logger=MagicMock(), heartbeat_interval=30000)


# ════════════════════════════════════════════════════════════════════════════
# 测试：_send_heartbeat 连接失活（missed_pong >= max）
# ════════════════════════════════════════════════════════════════════════════

class TestSendHeartbeatDeadConnection(unittest.IsolatedAsyncioTestCase):
    """连接失活时 _send_heartbeat 的行为（PR #2515 修复验证）"""

    async def asyncSetUp(self):
        self.mgr = _make_manager()
        self.mgr._schedule_reconnect = AsyncMock()

    # ------------------------------------------------------------------
    # 1. 核心修复：必须调用 _schedule_reconnect
    # ------------------------------------------------------------------

    async def test_reconnect_triggered_at_pong_limit(self):
        """missed_pong_count == max_missed_pong 时应触发 _schedule_reconnect。"""
        self.mgr._missed_pong_count = self.mgr._max_missed_pong
        self.mgr._ws = AsyncMock()
        await self.mgr._send_heartbeat()
        self.mgr._schedule_reconnect.assert_awaited_once()

    async def test_reconnect_triggered_above_pong_limit(self):
        """missed_pong_count > max_missed_pong 时同样应触发 _schedule_reconnect。"""
        self.mgr._missed_pong_count = self.mgr._max_missed_pong + 5
        self.mgr._ws = AsyncMock()
        await self.mgr._send_heartbeat()
        self.mgr._schedule_reconnect.assert_awaited_once()

    # ------------------------------------------------------------------
    # 2. 调用顺序：先停心跳 → 再重连
    # ------------------------------------------------------------------

    async def test_heartbeat_stopped_before_reconnect(self):
        """应先停止心跳定时器，再调用 _schedule_reconnect。"""
        self.mgr._missed_pong_count = self.mgr._max_missed_pong
        self.mgr._ws = AsyncMock()

        order = []
        original_stop = self.mgr._stop_heartbeat
        self.mgr._stop_heartbeat = lambda: (order.append("stop"), original_stop())[1]
        self.mgr._schedule_reconnect = AsyncMock(
            side_effect=lambda: order.append("reconnect")
        )

        await self.mgr._send_heartbeat()
        self.assertEqual(order, ["stop", "reconnect"])

    async def test_ws_close_called_before_reconnect(self):
        """应先调用 ws.close()，再触发重连。"""
        self.mgr._missed_pong_count = self.mgr._max_missed_pong
        fake_ws = AsyncMock()
        self.mgr._ws = fake_ws

        await self.mgr._send_heartbeat()

        fake_ws.close.assert_awaited_once()
        self.mgr._schedule_reconnect.assert_awaited_once()

    # ------------------------------------------------------------------
    # 3. ws.close() 抛异常：记录 warning，重连仍触发
    # ------------------------------------------------------------------

    async def test_reconnect_triggered_even_when_ws_close_raises(self):
        """ws.close() 抛异常时，重连仍应被触发。"""
        self.mgr._missed_pong_count = self.mgr._max_missed_pong
        fake_ws = AsyncMock()
        fake_ws.close = AsyncMock(side_effect=OSError("connection reset"))
        self.mgr._ws = fake_ws

        await self.mgr._send_heartbeat()
        self.mgr._schedule_reconnect.assert_awaited_once()

    async def test_warning_logged_when_ws_close_raises(self):
        """ws.close() 抛异常时应记录 warning 日志（不能静默吞掉）。"""
        self.mgr._missed_pong_count = self.mgr._max_missed_pong
        fake_ws = AsyncMock()
        fake_ws.close = AsyncMock(side_effect=OSError("connection reset"))
        self.mgr._ws = fake_ws

        await self.mgr._send_heartbeat()

        logger = self.mgr._logger
        self.assertTrue(
            logger.warning.called or logger.warn.called,
            "ws.close() 抛异常时应记录 warning 日志",
        )

    # ------------------------------------------------------------------
    # 4. ws 为 None：跳过 close，重连仍触发
    # ------------------------------------------------------------------

    async def test_reconnect_triggered_when_ws_is_none(self):
        """ws 为 None 时应跳过 close，但重连仍应被触发。"""
        self.mgr._missed_pong_count = self.mgr._max_missed_pong
        self.mgr._ws = None

        await self.mgr._send_heartbeat()
        self.mgr._schedule_reconnect.assert_awaited_once()


# ════════════════════════════════════════════════════════════════════════════
# 测试：_send_heartbeat 正常心跳（missed_pong < max）
# ════════════════════════════════════════════════════════════════════════════

class TestSendHeartbeatNormal(unittest.IsolatedAsyncioTestCase):
    """正常心跳（未超限）时 _send_heartbeat 的行为。"""

    async def asyncSetUp(self):
        self.mgr = _make_manager()
        self.mgr._schedule_reconnect = AsyncMock()
        self.mgr.send = AsyncMock()

    async def test_no_reconnect_when_within_limit(self):
        """missed_pong_count < max_missed_pong 时不应触发 _schedule_reconnect。"""
        self.mgr._missed_pong_count = 0
        await self.mgr._send_heartbeat()
        self.mgr._schedule_reconnect.assert_not_awaited()

    async def test_pong_count_incremented(self):
        """正常心跳发出后，_missed_pong_count 应递增 1。"""
        self.mgr._missed_pong_count = 0
        await self.mgr._send_heartbeat()
        self.assertEqual(self.mgr._missed_pong_count, 1)

    async def test_heartbeat_frame_sent(self):
        """正常心跳应通过 send() 发出 cmd=='ping' 的帧。"""
        self.mgr._missed_pong_count = 0
        await self.mgr._send_heartbeat()
        self.mgr.send.assert_awaited_once()
        sent_frame = self.mgr.send.call_args.args[0]
        self.assertEqual(sent_frame["cmd"], WsCmd.HEARTBEAT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
