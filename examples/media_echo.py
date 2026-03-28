"""
多媒体 Echo Demo

行为：
  - 用户发送图片 → 下载图片 → 上传 → 回复同一张图片
  - 用户发送文字 → 将文字写入 .txt → 上传 → 回复该文件

运行前在项目根目录创建 .env 文件并填写：
  WECHAT_BOT_ID=your_bot_id
  WECHAT_BOT_SECRET=your_bot_secret

启动方式：
  python examples/media_echo.py
"""

import asyncio
import os
import platform
import signal
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv()

from aibot import MediaType, WSClient, WSClientOptions

ws_client = WSClient(
    WSClientOptions(
        bot_id=os.getenv("WECHAT_BOT_ID"),
        secret=os.getenv("WECHAT_BOT_SECRET"),
    )
)


# ── 连接状态 ───────────────────────────────────────────────────────────────


@ws_client.on("connected")
def on_connected():
    print("[连接] WebSocket 已连接")


@ws_client.on("authenticated")
def on_authenticated():
    print("[认证] 认证成功，等待消息...")


@ws_client.on("disconnected")
def on_disconnected(reason):
    print(f"[断开] {reason}")


@ws_client.on("error")
def on_error(error):
    print(f"[错误] {error}", file=sys.stderr)


# ── 图片 Echo ──────────────────────────────────────────────────────────────


@ws_client.on("message.image")
async def on_image(frame):
    body = frame.get("body", {})
    image = body.get("image", {})
    url = image.get("url")
    aeskey = image.get("aeskey")

    print(f"[图片] 收到图片消息，开始下载...")

    try:
        # 1. 下载并解密原图
        data, filename = await ws_client.download_file(url, aeskey)
        filename = filename or f"echo_{int(time.time() * 1000)}.jpg"
        print(f"[图片] 下载成功：{filename}（{len(data)} bytes）")

        # 2. 上传为临时素材
        media_id = await ws_client.upload_media(data, filename, MediaType.Image)
        print(f"[图片] 上传成功：media_id={media_id}")

        # 3. 回复同一张图片
        await ws_client.reply_image(frame, media_id)
        print(f"[图片] 已回复图片")

    except Exception as e:
        print(f"[图片] 处理失败：{e}", file=sys.stderr)
        import traceback
        traceback.print_exc()


# ── 文字 → txt 文件 Echo ───────────────────────────────────────────────────


@ws_client.on("message.text")
async def on_text(frame):
    body = frame.get("body", {})
    content = body.get("text", {}).get("content", "").strip()

    if not content:
        return

    print(f"[文字] 收到文本：{content!r}")

    try:
        # 1. 将文字编码为 .txt 文件内容
        data = content.encode("utf-8")
        filename = f"echo_{int(time.time() * 1000)}.txt"

        # 2. 上传为临时素材
        media_id = await ws_client.upload_media(data, filename, MediaType.File)
        print(f"[文字] 上传成功：media_id={media_id}（文件名：{filename}）")

        # 3. 回复 .txt 文件
        await ws_client.reply_file(frame, media_id)
        print(f"[文字] 已回复文件")

    except Exception as e:
        print(f"[文字] 处理失败：{e}", file=sys.stderr)
        import traceback
        traceback.print_exc()


# ── 启动 ───────────────────────────────────────────────────────────────────


def _shutdown(loop: asyncio.AbstractEventLoop):
    print("\n[停止] 正在关闭...")
    ws_client.disconnect()
    loop.stop()


def main():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    if platform.system() != "Windows":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: _shutdown(loop))

    try:
        loop.run_until_complete(ws_client.connect())
        loop.run_forever()
    except KeyboardInterrupt:
        _shutdown(loop)
    finally:
        ws_client.disconnect()
        loop.close()


if __name__ == "__main__":
    main()
