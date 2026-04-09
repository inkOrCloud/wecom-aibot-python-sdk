"""
WebSocket 心跳失败自动重连功能测试

测试覆盖：
  - 心跳失败时触发重连逻辑（bug 修复验证）
  - 重连任务在独立任务中执行，不受心跳任务取消影响
  - 正常心跳发送和响应
  - 连续丢失 pong 后的行为

运行方式：
  python -m pytest tests/test_ws_heartbeat.py -v
  # 或：
  python -m unittest tests.test_ws_heartbeat -v
"""

import asyncio
import sys
import types as _types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, call

# ── 在 import aibot 之前注入缺失的第三方包存根 ──────────────────────────────
# --- websockets stub ---
_ws_mod = _types.ModuleType("websockets")
_ws_client_mod = _types.ModuleType("websockets.client")
_ws_exc_mod = _types.ModuleType("websockets.exceptions")


class _FakeProtocol:
    """模拟 WebSocket 协议对象"""

    def __init__(self, open=True):
        self.open = open
        self.closed = False

    async def send(self, data):
        if not self.open:
            raise Exception("WebSocket is closed")

    async def close(self, code=None, reason=None):
        self.open = False
        self.closed = True


class _ConnectionClosed(Exception):
    """模拟 ConnectionClosed 异常"""

    def __init__(self, code=None, reason=None):
        self.code = code
        self.reason = reason
        super().__init__(f"Connection closed: code={code}, reason={reason}")


_ws_client_mod.WebSocketClientProtocol = _FakeProtocol
_ws_exc_mod.ConnectionClosed = _ConnectionClosed
_ws_mod.connect = None
_ws_mod.exceptions = _ws_exc_mod
sys.modules.setdefault("websockets", _ws_mod)
sys.modules.setdefault("websockets.client", _ws_client_mod)
sys.modules.setdefault("websockets.exceptions", _ws_exc_mod)

# --- pyee stub ---
_pyee = _types.ModuleType("pyee")
_pyee_asyncio = _types.ModuleType("pyee.asyncio")


class _AsyncIOEventEmitter:
    def __init__(self):
        self._listeners = {}

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

# ── 现在可以安全 import aibot ────────────────────────────────────────────────
from aibot.ws import WsConnectionManager


class TestHeartbeatReconnect(unittest.IsolatedAsyncioTestCase):
    """测试心跳失败时的自动重连逻辑"""

    def setUp(self):
        """设置测试环境"""
        self.logger = MagicMock()
        self.manager = WsConnectionManager(
            logger=self.logger,
            heartbeat_interval=100,  # 100ms 快速测试
            reconnect_base_delay=50,  # 50ms 快速重连
            max_reconnect_attempts=3,
        )
        self.manager.set_credentials("test_bot", "test_secret")

        # 记录回调触发情况
        self.on_disconnected_called = False
        self.on_reconnecting_called = False
        self.reconnect_attempts = []

        def on_disconnected(reason):
            self.on_disconnected_called = True

        def on_reconnecting(attempt):
            self.on_reconnecting_called = True
            self.reconnect_attempts.append(attempt)

        self.manager.on_disconnected = on_disconnected
        self.manager.on_reconnecting = on_reconnecting

    async def test_heartbeat_failure_triggers_reconnect(self):
        """
        核心测试：当连续丢失 pong 达到上限时，应该触发重连
        
        这是对 bug 修复的验证：
        - 修复前：_send_heartbeat 会 return 而不调用 _schedule_reconnect
        - 修复后：使用 asyncio.ensure_future 在独立任务中触发重连
        """
        # 模拟 WebSocket 连接
        fake_ws = _FakeProtocol(open=True)
        self.manager._ws = fake_ws

        # 模拟认证成功，启动心跳
        self.manager._start_heartbeat()
        self.assertIsNotNone(self.manager._heartbeat_task)

        # 模拟连续丢失 pong：设置 _missed_pong_count 达到上限
        self.manager._missed_pong_count = self.manager._max_missed_pong

        # Mock connect 方法以避免真实连接
        self.manager.connect = AsyncMock()

        # 手动调用 _send_heartbeat，模拟心跳检查
        await self.manager._send_heartbeat()

        # 等待异步任务执行
        await asyncio.sleep(0.15)  # 150ms 等待重连延迟

        # 验证：
        # 1. 心跳任务应该被停止
        self.assertIsNone(self.manager._heartbeat_task)

        # 2. WebSocket 应该被关闭
        self.assertTrue(fake_ws.closed)

        # 3. 重连方法应该被调用（关键验证点）
        self.manager.connect.assert_called()
        self.assertGreater(
            self.manager.connect.call_count,
            0,
            "Bug 修复验证失败：心跳失败后没有触发重连",
        )

    async def test_reconnect_scheduled_in_separate_task(self):
        """
        验证重连在独立任务中执行，不受心跳任务取消影响
        
        这是修复的关键：使用 asyncio.ensure_future 而不是 await
        """
        fake_ws = _FakeProtocol(open=True)
        self.manager._ws = fake_ws

        # 启动心跳
        self.manager._start_heartbeat()
        heartbeat_task = self.manager._heartbeat_task

        # 设置 pong 丢失计数
        self.manager._missed_pong_count = self.manager._max_missed_pong

        # Mock _schedule_reconnect 来跟踪调用
        original_schedule_reconnect = self.manager._schedule_reconnect
        reconnect_called = asyncio.Event()

        async def mock_schedule_reconnect():
            reconnect_called.set()
            # 不实际重连，只是标记
            pass

        self.manager._schedule_reconnect = mock_schedule_reconnect

        # 触发心跳检查
        await self.manager._send_heartbeat()

        # 等待重连任务被调度
        try:
            await asyncio.wait_for(reconnect_called.wait(), timeout=0.3)
            reconnect_was_scheduled = True
        except asyncio.TimeoutError:
            reconnect_was_scheduled = False

        # 验证重连被调度
        self.assertTrue(
            reconnect_was_scheduled,
            "重连任务应该在独立任务中被调度，即使心跳任务被取消",
        )

        # 清理
        self.manager._schedule_reconnect = original_schedule_reconnect

    async def test_missed_pong_counter_increments(self):
        """测试 missed_pong 计数器正常递增"""
        fake_ws = _FakeProtocol(open=True)
        self.manager._ws = fake_ws
        self.manager.send = AsyncMock()

        # 初始计数为 0
        self.assertEqual(self.manager._missed_pong_count, 0)

        # 发送心跳（未达到上限）
        await self.manager._send_heartbeat()
        self.assertEqual(self.manager._missed_pong_count, 1)

        await self.manager._send_heartbeat()
        self.assertEqual(self.manager._missed_pong_count, 2)

    async def test_pong_received_resets_counter(self):
        """测试收到 pong 后计数器重置"""
        self.manager._missed_pong_count = 1

        # 模拟收到心跳响应（req_id 以 "ping_" 开头）
        frame = {
            "headers": {"req_id": "ping_123456_abc"},
            "errcode": 0,
        }

        self.manager._handle_frame(frame)

        # 计数器应该重置为 0
        self.assertEqual(self.manager._missed_pong_count, 0)

    async def test_heartbeat_sends_correct_frame(self):
        """测试心跳帧格式正确"""
        fake_ws = _FakeProtocol(open=True)
        self.manager._ws = fake_ws

        with patch.object(self.manager, "send", new_callable=AsyncMock) as mock_send:
            await self.manager._send_heartbeat()

            # 验证发送的帧格式
            mock_send.assert_called_once()
            frame = mock_send.call_args[0][0]
            self.assertEqual(frame["cmd"], "ping")  # WsCmd.HEARTBEAT = "ping"
            self.assertIn("req_id", frame["headers"])
            self.assertTrue(frame["headers"]["req_id"].startswith("ping_"))

    async def test_ws_close_exception_is_logged(self):
        """测试 WebSocket 关闭异常被正确记录（bug 修复的一部分）"""
        # 创建一个会抛出异常的 fake_ws
        fake_ws = _FakeProtocol(open=True)

        async def close_with_error(code=None, reason=None):
            raise Exception("Close failed")

        fake_ws.close = close_with_error
        self.manager._ws = fake_ws

        # 设置 pong 丢失计数达到上限
        self.manager._missed_pong_count = self.manager._max_missed_pong

        # Mock connect 避免真实重连
        self.manager.connect = AsyncMock()

        # 调用 _send_heartbeat
        await self.manager._send_heartbeat()

        # 等待任务执行
        await asyncio.sleep(0.1)

        # 验证警告日志被调用（修复后应该记录异常）
        warn_calls = [
            call
            for call in self.logger.warn.call_args_list
            if "Failed to close WebSocket" in str(call)
        ]
        self.assertGreater(
            len(warn_calls), 0, "WebSocket 关闭异常应该被记录为警告日志"
        )

    async def test_heartbeat_loop_continues_after_send_error(self):
        """测试心跳发送失败后循环继续"""
        fake_ws = _FakeProtocol(open=True)
        self.manager._ws = fake_ws

        send_count = 0

        async def mock_send_with_error(frame):
            nonlocal send_count
            send_count += 1
            if send_count == 1:
                raise Exception("Network error")
            # 第二次成功

        with patch.object(self.manager, "send", side_effect=mock_send_with_error):
            self.manager._start_heartbeat()

            # 等待多个心跳周期
            await asyncio.sleep(0.3)

            # 验证发送被调用多次（说明循环继续）
            self.assertGreater(send_count, 1, "心跳循环应该在错误后继续")

            # 清理
            self.manager._stop_heartbeat()


class TestReconnectLogic(unittest.IsolatedAsyncioTestCase):
    """测试重连逻辑"""

    def setUp(self):
        self.logger = MagicMock()
        self.manager = WsConnectionManager(
            logger=self.logger,
            heartbeat_interval=100,
            reconnect_base_delay=20,  # 20ms 快速测试
            max_reconnect_attempts=3,
        )
        self.manager.set_credentials("test_bot", "test_secret")

    async def test_reconnect_delay_exponential_backoff(self):
        """测试重连延迟采用指数退避"""
        self.manager.connect = AsyncMock()

        # 记录每次重连的延迟
        delays = []
        original_sleep = asyncio.sleep

        async def mock_sleep(delay):
            delays.append(delay)
            await original_sleep(0.01)  # 实际只等待 10ms

        with patch("asyncio.sleep", side_effect=mock_sleep):
            # 触发多次重连
            for i in range(3):
                self.manager._reconnect_attempts = i
                await self.manager._schedule_reconnect()

        # 验证延迟递增（指数退避）
        # 第一次: 20ms, 第二次: 40ms, 第三次: 80ms
        self.assertEqual(len(delays), 3)
        self.assertLess(delays[0], delays[1])
        self.assertLess(delays[1], delays[2])

    async def test_reconnect_stops_at_max_attempts(self):
        """测试达到最大重连次数后停止"""
        self.manager._max_reconnect_attempts = 2
        self.manager.connect = AsyncMock()

        error_triggered = False

        def on_error(e):
            nonlocal error_triggered
            if "Max reconnect attempts exceeded" in str(e):
                error_triggered = True

        self.manager.on_error = on_error

        # 模拟多次重连失败
        self.manager._reconnect_attempts = 2  # 已经重连 2 次

        await self.manager._schedule_reconnect()

        # 验证不再尝试重连
        self.manager.connect.assert_not_called()
        self.assertTrue(error_triggered, "应该触发最大重连次数错误")


if __name__ == "__main__":
    unittest.main(verbosity=2)
