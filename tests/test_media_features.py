"""
多媒体上传与回复功能测试

覆盖范围：
  - MediaType 枚举 / WsCmd 常量 / MessageType.Video（types.py）
  - video 消息分发（message_handler.py）
  - upload_media() 三步上传逻辑（client.py）
  - reply_image / reply_file / reply_voice / reply_video（client.py）

运行方式：
  python -m pytest tests/test_media_features.py -v
  # 或纯 stdlib：
  python -m unittest tests.test_media_features -v
"""

# ── 在 import aibot 之前注入缺失的第三方包存根 ──────────────────────────────
import sys
import types as _types

# --- pyee stub ---
_pyee = _types.ModuleType("pyee")
_pyee_asyncio = _types.ModuleType("pyee.asyncio")

class _AsyncIOEventEmitter:
    """最小化的 EventEmitter 存根，仅用于测试。"""
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

# --- aiohttp stub ---
_aiohttp = _types.ModuleType("aiohttp")

class _FakeTimeout:
    def __init__(self, total=None): pass

class _FakeConnector:
    def __init__(self, ssl=None): pass

_aiohttp.ClientTimeout = _FakeTimeout
_aiohttp.TCPConnector = _FakeConnector
sys.modules.setdefault("aiohttp", _aiohttp)

# ── 现在可以安全 import aibot ────────────────────────────────────────────────
import base64
import hashlib
import math
import unittest
from unittest.mock import AsyncMock, MagicMock, call, patch

from aibot.types import MediaType, MessageType, WsCmd
from aibot.message_handler import MessageHandler
from aibot.client import WSClient
from aibot.types import WSClientOptions


# ════════════════════════════════════════════════════════════════════════════
# 辅助：构造一个轻量 WSClient（不建立真实 WS 连接）
# ════════════════════════════════════════════════════════════════════════════

def _make_client() -> WSClient:
    opts = WSClientOptions(bot_id="test_bot", secret="test_secret")
    client = WSClient(opts)
    # 替换 ws_manager 为 Mock，避免真实网络
    client._ws_manager = MagicMock()
    client._ws_manager.send_reply = AsyncMock()
    return client


def _fake_frame(req_id: str = "req_001") -> dict:
    return {"headers": {"req_id": req_id}, "body": {}}


# ════════════════════════════════════════════════════════════════════════════
# 1. Types 测试
# ════════════════════════════════════════════════════════════════════════════

class TestTypesNewAdditions(unittest.TestCase):
    """types.py 新增内容测试"""

    def test_media_type_values(self):
        self.assertEqual(MediaType.File, "file")
        self.assertEqual(MediaType.Image, "image")
        self.assertEqual(MediaType.Voice, "voice")
        self.assertEqual(MediaType.Video, "video")

    def test_media_type_is_str_subclass(self):
        self.assertIsInstance(MediaType.Image, str)

    def test_message_type_video_exists(self):
        self.assertEqual(MessageType.Video, "video")

    def test_wscmd_upload_constants(self):
        self.assertEqual(WsCmd.UPLOAD_MEDIA_INIT, "aibot_upload_media_init")
        self.assertEqual(WsCmd.UPLOAD_MEDIA_CHUNK, "aibot_upload_media_chunk")
        self.assertEqual(WsCmd.UPLOAD_MEDIA_FINISH, "aibot_upload_media_finish")


# ════════════════════════════════════════════════════════════════════════════
# 2. MessageHandler - video 分发测试
# ════════════════════════════════════════════════════════════════════════════

class TestMessageHandlerVideo(unittest.TestCase):
    """message_handler.py 中 video 消息分发测试"""

    def setUp(self):
        self.handler = MessageHandler(MagicMock())
        self.emitter = _AsyncIOEventEmitter()
        self.received: list = []
        self.emitter.on("message.video", lambda f: self.received.append(f))

    def _make_video_frame(self):
        return {
            "cmd": "aibot_msg_callback",
            "headers": {"req_id": "r1"},
            "body": {"msgtype": "video", "video": {"url": "https://example.com/v.mp4", "aeskey": "key"}},
        }

    def test_video_message_emits_message_video(self):
        frame = self._make_video_frame()
        self.handler.handle_frame(frame, self.emitter)
        self.assertEqual(len(self.received), 1)
        self.assertEqual(self.received[0], frame)

    def test_video_message_also_emits_generic_message(self):
        generic_received = []
        self.emitter.on("message", lambda f: generic_received.append(f))
        self.handler.handle_frame(self._make_video_frame(), self.emitter)
        self.assertEqual(len(generic_received), 1)


# ════════════════════════════════════════════════════════════════════════════
# 3. upload_media() 测试
# ════════════════════════════════════════════════════════════════════════════

class TestUploadMedia(unittest.IsolatedAsyncioTestCase):
    """WSClient.upload_media() 三步上传逻辑测试"""

    def setUp(self):
        self.client = _make_client()

    def _setup_send_reply(self, upload_id="uid_abc", media_id="mid_xyz"):
        """配置 send_reply 的返回序列：init → chunks(幂等 ok) → finish"""
        async def _side_effect(req_id, body, cmd):
            if cmd == WsCmd.UPLOAD_MEDIA_INIT:
                return {"body": {"upload_id": upload_id}, "errcode": 0}
            elif cmd == WsCmd.UPLOAD_MEDIA_CHUNK:
                return {"errcode": 0}
            elif cmd == WsCmd.UPLOAD_MEDIA_FINISH:
                return {"body": {"media_id": media_id, "type": "image"}, "errcode": 0}
            raise ValueError(f"Unexpected cmd: {cmd}")

        self.client._ws_manager.send_reply.side_effect = _side_effect

    async def test_single_chunk_upload_returns_media_id(self):
        """小文件（单片）上传后应返回 media_id"""
        self._setup_send_reply()
        data = b"fake image data"
        result = await self.client.upload_media(data, "photo.jpg", MediaType.Image)
        self.assertEqual(result, "mid_xyz")

    async def test_single_chunk_call_sequence(self):
        """单片上传：send_reply 应恰好被调用 3 次（init + 1 chunk + finish）"""
        self._setup_send_reply()
        data = b"x" * 100
        await self.client.upload_media(data, "test.jpg", MediaType.Image)
        calls = self.client._ws_manager.send_reply.call_args_list
        cmds = [c.args[2] for c in calls]
        self.assertEqual(cmds, [
            WsCmd.UPLOAD_MEDIA_INIT,
            WsCmd.UPLOAD_MEDIA_CHUNK,
            WsCmd.UPLOAD_MEDIA_FINISH,
        ])

    async def test_multi_chunk_upload(self):
        """跨片文件：分片数量应正确计算，每片 cmd 均为 UPLOAD_MEDIA_CHUNK"""
        self._setup_send_reply(media_id="multi_media_id")
        chunk_size = 512 * 1024
        # 构造 1.5 个 chunk 的数据
        data = b"a" * int(chunk_size * 1.5)
        expected_chunks = math.ceil(len(data) / chunk_size)  # = 2

        result = await self.client.upload_media(data, "large.bin", MediaType.File)
        self.assertEqual(result, "multi_media_id")

        calls = self.client._ws_manager.send_reply.call_args_list
        chunk_calls = [c for c in calls if c.args[2] == WsCmd.UPLOAD_MEDIA_CHUNK]
        self.assertEqual(len(chunk_calls), expected_chunks)

    async def test_chunk_index_and_base64_encoding(self):
        """分片的 chunk_index 从 0 开始，base64_data 与原始数据匹配"""
        self._setup_send_reply()
        data = b"hello world"
        await self.client.upload_media(data, "hi.txt", MediaType.File)

        calls = self.client._ws_manager.send_reply.call_args_list
        chunk_call = next(c for c in calls if c.args[2] == WsCmd.UPLOAD_MEDIA_CHUNK)
        body = chunk_call.args[1]

        self.assertEqual(body["chunk_index"], 0)
        self.assertEqual(body["upload_id"], "uid_abc")
        decoded = base64.b64decode(body["base64_data"])
        self.assertEqual(decoded, data)

    async def test_init_body_fields(self):
        """init 请求体应包含 type / filename / total_size / total_chunks / md5"""
        self._setup_send_reply()
        data = b"sample"
        expected_md5 = hashlib.md5(data).hexdigest()

        await self.client.upload_media(data, "sample.jpg", MediaType.Image)

        calls = self.client._ws_manager.send_reply.call_args_list
        init_call = next(c for c in calls if c.args[2] == WsCmd.UPLOAD_MEDIA_INIT)
        body = init_call.args[1]

        self.assertEqual(body["type"], "image")
        self.assertEqual(body["filename"], "sample.jpg")
        self.assertEqual(body["total_size"], len(data))
        self.assertEqual(body["total_chunks"], 1)
        self.assertEqual(body["md5"], expected_md5)

    async def test_custom_md5_is_used(self):
        """调用方显式传入 md5 时应直接使用，不重新计算"""
        self._setup_send_reply()
        await self.client.upload_media(b"data", "f.bin", MediaType.File, md5="custom_md5")

        calls = self.client._ws_manager.send_reply.call_args_list
        init_body = next(c for c in calls if c.args[2] == WsCmd.UPLOAD_MEDIA_INIT).args[1]
        self.assertEqual(init_body["md5"], "custom_md5")

    async def test_too_many_chunks_raises_value_error(self):
        """超过 100 片的文件应抛出 ValueError"""
        chunk_size = 512 * 1024
        # 101 片
        data = b"x" * (chunk_size * 101)
        with self.assertRaises(ValueError, msg="should raise ValueError for >100 chunks"):
            await self.client.upload_media(data, "huge.bin", MediaType.File)
        # send_reply 不应被调用
        self.client._ws_manager.send_reply.assert_not_called()

    async def test_media_type_enum_and_string_both_accepted(self):
        """media_type 参数接受 MediaType 枚举或等效字符串"""
        self._setup_send_reply()

        await self.client.upload_media(b"d", "a.jpg", MediaType.Image)
        calls1 = self.client._ws_manager.send_reply.call_args_list
        init_body1 = next(c for c in calls1 if c.args[2] == WsCmd.UPLOAD_MEDIA_INIT).args[1]

        self.client._ws_manager.send_reply.reset_mock()
        self._setup_send_reply()

        await self.client.upload_media(b"d", "a.jpg", "image")
        calls2 = self.client._ws_manager.send_reply.call_args_list
        init_body2 = next(c for c in calls2 if c.args[2] == WsCmd.UPLOAD_MEDIA_INIT).args[1]

        self.assertEqual(init_body1["type"], init_body2["type"])


# ════════════════════════════════════════════════════════════════════════════
# 4. reply_image / reply_file / reply_voice / reply_video 测试
# ════════════════════════════════════════════════════════════════════════════

class TestReplyMediaMethods(unittest.IsolatedAsyncioTestCase):
    """reply_image / reply_file / reply_voice / reply_video 测试"""

    def setUp(self):
        self.client = _make_client()
        self.client.reply = AsyncMock(return_value={"errcode": 0})
        self.frame = _fake_frame()

    async def test_reply_image_body(self):
        await self.client.reply_image(self.frame, "media_img")
        self.client.reply.assert_called_once_with(
            self.frame,
            {"msgtype": "image", "image": {"media_id": "media_img"}},
        )

    async def test_reply_file_body(self):
        await self.client.reply_file(self.frame, "media_file")
        self.client.reply.assert_called_once_with(
            self.frame,
            {"msgtype": "file", "file": {"media_id": "media_file"}},
        )

    async def test_reply_voice_body(self):
        await self.client.reply_voice(self.frame, "media_voice")
        self.client.reply.assert_called_once_with(
            self.frame,
            {"msgtype": "voice", "voice": {"media_id": "media_voice"}},
        )

    async def test_reply_video_body_minimal(self):
        """无 title / description 时，video body 只含 media_id"""
        await self.client.reply_video(self.frame, "media_vid")
        self.client.reply.assert_called_once_with(
            self.frame,
            {"msgtype": "video", "video": {"media_id": "media_vid"}},
        )

    async def test_reply_video_body_with_title_and_description(self):
        await self.client.reply_video(
            self.frame, "media_vid", title="Demo", description="A demo video"
        )
        self.client.reply.assert_called_once_with(
            self.frame,
            {"msgtype": "video", "video": {
                "media_id": "media_vid",
                "title": "Demo",
                "description": "A demo video",
            }},
        )

    async def test_reply_video_body_with_title_only(self):
        await self.client.reply_video(self.frame, "media_vid", title="Only Title")
        _, kwargs = self.client.reply.call_args
        body = self.client.reply.call_args.args[1]
        self.assertIn("title", body["video"])
        self.assertNotIn("description", body["video"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
