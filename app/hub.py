"""WebSocket 连接注册与按权限推送。"""
from __future__ import annotations

import asyncio
import contextlib

from fastapi import WebSocket


class Hub:
    def __init__(self) -> None:
        # session_id -> {ws_id: (websocket, kind, principal_id)}
        self._conns: dict[str, dict[int, tuple[WebSocket, str, str]]] = {}
        self._lock = asyncio.Lock()
        self._next_id = 0

    async def register(self, session_id: str, kind: str, principal_id: str,
                       ws: WebSocket) -> int:
        async with self._lock:
            self._next_id += 1
            ws_id = self._next_id
            self._conns.setdefault(session_id, {})[ws_id] = (ws, kind,
                                                             principal_id)
            return ws_id

    async def unregister(self, session_id: str, ws_id: int) -> None:
        async with self._lock:
            self._conns.get(session_id, {}).pop(ws_id, None)

    async def connections(self, session_id: str):
        async with self._lock:
            return list(self._conns.get(session_id, {}).items())

    async def broadcast(self, session_id: str, message: dict) -> None:
        """向该 session 的所有连接推送同一个原始事件通知（notify）。

        具体快照由各连接按自己的凭证重新拉取，服务端永远不在这里做裁剪外的数据复用。
        """
        for ws_id, (ws, _kind, _pid) in await self.connections(session_id):
            with contextlib.suppress(Exception):
                await ws.send_json(message)
