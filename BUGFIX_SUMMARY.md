# WebSocket 心跳失败重连问题修复总结

## 问题来源

参考 agentscope-ai/CoPaw 项目的以下提交：
- PR #2515: 首次修复尝试
- PR #2641: 回退 #2515
- PR #2651: 最终正确的修复方案

## Bug 描述

### 问题现象
当网络连接断开时，企业微信机器人会永久离线，即使网络恢复也不会自动重连，必须手动重启服务。

### 根本原因

在 `aibot/ws.py` 的 `_send_heartbeat()` 方法中存在关键缺陷：

1. 当连续丢失 `_max_missed_pong` 次心跳响应时，代码会调用 `_stop_heartbeat()` 停止心跳任务
2. 由于 `_send_heartbeat()` 本身运行在心跳任务中，调用 `_stop_heartbeat()` 会取消当前任务
3. 任务被取消后，之后的 `await self._ws.close()` 会立即抛出 `CancelledError`
4. **关键问题**：没有调用 `_schedule_reconnect()` 来触发重连
5. 在网络故障情况下，`_receive_loop()` 可能永远收不到 `ConnectionClosed` 事件，导致连接永久断开

### 代码对比

**修复前：**
```python
async def _send_heartbeat(self) -> None:
    if self._missed_pong_count >= self._max_missed_pong:
        self._logger.warn("connection considered dead")
        self._stop_heartbeat()  # ← 取消当前任务
        if self._ws:
            try:
                await self._ws.close()  # ← CancelledError！
            except Exception:
                pass
        return  # ← 没有触发重连！
```

**修复后：**
```python
async def _send_heartbeat(self) -> None:
    if self._missed_pong_count >= self._max_missed_pong:
        self._logger.warn("connection considered dead")
        # ← 在独立任务中触发重连，避免被取消
        asyncio.ensure_future(self._schedule_reconnect())
        self._stop_heartbeat()
        if self._ws:
            try:
                await self._ws.close()
            except Exception as e:
                self._logger.warn(f"Failed to close WebSocket: {e}")
        return
```

## 修复方案

### 核心改动

1. **在独立任务中调度重连**：使用 `asyncio.ensure_future(self._schedule_reconnect())` 在单独的异步任务中触发重连，确保即使当前心跳任务被取消，重连逻辑也能正常执行

2. **改进异常日志**：将 `ws.close()` 的异常从静默吞掉改为记录警告日志，便于排查问题

3. **执行顺序优化**：在调用 `_stop_heartbeat()` **之前** 就创建重连任务，确保重连逻辑不会被任务取消所影响

### 关键技术点

- `asyncio.ensure_future()` 会立即创建并调度一个新任务，该任务独立于当前任务运行
- 即使当前心跳任务因为 `_stop_heartbeat()` 被取消，重连任务仍会继续执行
- 这避免了 "任务取消导致后续代码无法执行" 的问题

## 测试建议

### 单元测试

运行单元测试验证修复：

```bash
# 运行所有测试
python -m pytest tests/ -v

# 只运行 WebSocket 心跳测试
python -m pytest tests/test_ws_heartbeat.py -v

# 查看测试覆盖率
python -m pytest tests/test_ws_heartbeat.py --cov=aibot.ws --cov-report=term-missing
```

**测试覆盖范围**：
- ✅ 心跳失败触发重连（bug 修复验证）
- ✅ 重连在独立任务中执行（关键修复验证）
- ✅ missed_pong 计数器递增和重置
- ✅ 心跳帧格式验证
- ✅ WebSocket 关闭异常记录
- ✅ 重连指数退避
- ✅ 最大重连次数限制

### 集成测试

1. **模拟网络断开**：
   - 断开网络连接
   - 等待 2-3 个心跳周期（默认 30 秒 × 2 = 60 秒）
   - 观察日志是否显示 "connection considered dead" 并触发重连

2. **验证自动重连**：
   - 恢复网络连接
   - 观察是否自动重连成功
   - 检查机器人是否能正常接收和回复消息

3. **长期稳定性测试**：
   - 运行机器人 24 小时以上
   - 在此期间多次模拟网络抖动
   - 验证每次都能自动恢复连接

## 相关链接

- agentscope-ai/CoPaw PR #2515: https://github.com/agentscope-ai/CoPaw/pull/2515
- agentscope-ai/CoPaw PR #2641: https://github.com/agentscope-ai/CoPaw/pull/2641  
- agentscope-ai/CoPaw PR #2651: https://github.com/agentscope-ai/CoPaw/pull/2651

## 提交信息

### Bug 修复
- Commit: c04d673
- 消息: `fix(ws): schedule reconnect in separate task on heartbeat failure`
- 文件: `aibot/ws.py`
- 改动: +4 -2 行

### 单元测试
- Commit: 1d00043
- 消息: `test: add comprehensive unit tests for WebSocket heartbeat reconnect`
- 文件: `tests/test_ws_heartbeat.py`
- 改动: +395 行
- 测试数量: 9 个单元测试
- 测试结果: ✅ 全部通过
