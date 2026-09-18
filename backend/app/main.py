"""FastAPI application: HTTP API + WebSocket live channel + static frontend.

Run (container)::

    uvicorn app.main:app --host 0.0.0.0 --port 8000
"""
from __future__ import annotations

import asyncio
import hmac
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .db import Database
from .definitions import DrillError, load_directory
from .engine import Engine, EngineError, now_ts
from .hub import Client, Hub
from .records import (
    build_replay,
    host_record,
    load_events,
    personal_record,
    render_host_md,
    render_personal_md,
)
from .snapshots import build_snapshot


class TimerService:
    """One asyncio task per open stage deadline. Server clock is authoritative;
    the task only calls settle('deadline') which itself re-checks the deadline
    inside the atomic transaction, so races are impossible."""

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task] = {}

    def schedule(self, session_id: str, deadline: float | None) -> None:
        self.cancel(session_id)
        if deadline is None:
            return
        delay = max(0.05, deadline - time.time())
        self._tasks[session_id] = asyncio.create_task(
            self._fire(session_id, deadline, delay)
        )

    def cancel(self, session_id: str) -> None:
        t = self._tasks.pop(session_id, None)
        if t and not t.done():
            t.cancel()

    async def _fire(self, session_id: str, deadline: float, delay: float) -> None:
        await asyncio.sleep(delay)
        self._tasks.pop(session_id, None)
        await on_deadline(session_id, deadline)


# set during lifespan
db = Database(str(config.DB_PATH))
engine = Engine(db)
hub = Hub()
timers = TimerService()


def check_host(key: str | None) -> None:
    if not key or not hmac.compare_digest(key, config.HOST_KEY):
        raise HTTPException(status_code=401, detail="主持人密钥无效")


async def send_snapshot(client: Client, session_id: str, viewer: tuple[str, str] | None) -> None:
    sess, d, state = await engine.load(session_id)
    snap = build_snapshot(sess, d, state, viewer, online=hub.online_roles(session_id))
    snap["kind"] = viewer[0] if viewer else "none"
    snap["server_time"] = now_ts()
    hub.push_to(client, {"type": "snapshot", "snapshot": snap})


async def broadcast_snapshot(session_id: str) -> None:
    sess, d, state = await engine.load(session_id)
    online = hub.online_roles(session_id)
    ts = now_ts()
    for c in list(hub._sessions.get(session_id, ())):  # type: ignore[attr-defined]
        viewer = ("host", None) if c.role_id is None else ("role", c.role_id)
        snap = build_snapshot(sess, d, state, viewer, online=online)
        snap["server_time"] = ts
        hub.push_to(c, {"type": "snapshot", "snapshot": snap})


async def reschedule(session_id: str) -> None:
    try:
        _, _, state = await engine.load(session_id)
    except EngineError:
        timers.cancel(session_id)
        return
    if state.status == "running" and state.current is not None:
        timers.schedule(session_id, state.deadline)
    else:
        timers.cancel(session_id)


async def on_deadline(session_id: str, expected_deadline: float) -> None:
    try:
        info = await engine.settle(session_id, "deadline")
    except EngineError:
        return
    if not info.get("noop"):
        await broadcast_snapshot(session_id)
        await reschedule(session_id)


# --------------------------------------------------------------------------- #
# lifespan: open DB, seed drills from YAML, re-arm timers after a restart
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    config.DRILLS_DIR.mkdir(parents=True, exist_ok=True)
    await db.connect()
    drills = load_directory(config.DRILLS_DIR)
    for d in drills.values():
        await engine.upsert_drill(d)

    for s in await engine.list_sessions():
        if s["status"] == "running":
            try:
                _, _, state = await engine.load(s["id"])
            except Exception:
                continue
            if state.current is not None:
                timers.schedule(s["id"], state.deadline)
    try:
        yield
    finally:
        for sid in list(timers._tasks):
            timers.cancel(sid)
        await db.close()


app = FastAPI(title="多人应急演练", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
class JoinBody(BaseModel):
    invite: str
    name: str | None = None


class DrillBody(BaseModel):
    yaml: str


@app.get("/api/health")
async def health() -> dict[str, bool]:
    return {"ok": True}


@app.get("/api/drills")
async def api_drills(x_host_key: str | None = Header(default=None)) -> list[dict[str, str]]:
    check_host(x_host_key)
    return await engine.list_drills()


@app.post("/api/drills")
async def api_create_drill(
    body: DrillBody, x_host_key: str | None = Header(default=None)
) -> dict[str, Any]:
    check_host(x_host_key)
    import yaml as pyyaml

    try:
        raw = pyyaml.safe_load(body.yaml)
        from .definitions import parse_definition

        d = parse_definition(raw or {})
    except DrillError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    path = config.DRILLS_DIR / f"{d.id}.yaml"
    path.write_text(body.yaml, encoding="utf-8")
    await engine.upsert_drill(d)
    return {"id": d.id, "name": d.name}


@app.post("/api/sessions")
async def api_create_session(
    drill_id: str = Query(...), x_host_key: str | None = Header(default=None)
) -> JSONResponse:
    check_host(x_host_key)
    try:
        sid = await engine.create_session(drill_id)
    except EngineError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    return JSONResponse({"session_id": sid}, status_code=201)


@app.get("/api/sessions")
async def api_sessions(x_host_key: str | None = Header(default=None)) -> list[dict[str, Any]]:
    check_host(x_host_key)
    out = []
    for s in await engine.list_sessions():
        try:
            d = await engine.get_definition(s["drill_id"])
            s["drill_name"] = d.name
        except EngineError:
            s["drill_name"] = s["drill_id"]
        out.append(s)
    return out


@app.post("/api/sessions/{session_id}/join")
async def api_join(session_id: str, body: JoinBody) -> dict[str, Any]:
    try:
        await engine.get_session(session_id)
        return await engine.join(session_id, body.invite, body.name or "")
    except EngineError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


async def _viewer_for(
    session_id: str, host_key: str | None, token: str | None
) -> tuple[str, str]:
    """Returns ("host", "") or ("role", role_id)."""
    if host_key:
        if hmac.compare_digest(host_key, config.HOST_KEY):
            return ("host", "")
        raise HTTPException(status_code=401, detail="主持人密钥无效")
    if token:
        p = await engine.get_participant(session_id, token)
        if p is None:
            raise HTTPException(status_code=401, detail="访问令牌无效")
        return ("role", p["role_id"])
    raise HTTPException(status_code=401, detail="需要主持人密钥或参与者令牌")


@app.get("/api/sessions/{session_id}/replay")
async def api_replay(
    session_id: str,
    x_host_key: str | None = Header(default=None),
    token: str | None = Query(default=None),
    version: int | None = Query(default=None),
) -> dict[str, Any]:
    viewer = await _viewer_for(session_id, x_host_key, token)
    try:
        sess, d, state = await engine.load(session_id)
        rows = await db.all(
            "SELECT version, seq, type, actor, at, payload FROM events "
            "WHERE session_id=? ORDER BY version, seq",
            (session_id,),
        )
    except EngineError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    events = load_events(rows)
    if version is not None:
        events = [e for e in events if e["version"] <= version]
    return build_replay(sess, d, events, viewer if viewer[0] == "host" else viewer)


@app.get("/api/sessions/{session_id}/record")
async def api_record(
    session_id: str,
    fmt: str = Query("md"),
    x_host_key: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> Response:
    viewer = await _viewer_for(session_id, x_host_key, token)
    try:
        sess, d, _ = await engine.load(session_id)
        rows = await db.all(
            "SELECT version, seq, type, actor, at, payload FROM events "
            "WHERE session_id=? ORDER BY version, seq",
            (session_id,),
        )
    except EngineError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    events = load_events(rows)

    if viewer[0] == "host":
        rec = host_record(sess, d, events)
        if fmt == "json":
            return JSONResponse(rec)
        return Response(
            render_host_md(rec),
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="host-{session_id}.md"'},
        )

    rec = personal_record(sess, d, events, viewer[1])
    if fmt == "json":
        return JSONResponse(rec)
    return Response(
        render_personal_md(rec),
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="record-{session_id}-{viewer[1]}.md"'
        },
    )


# --------------------------------------------------------------------------- #
# WebSocket: /ws/{session_id}?token=...&host=...
# --------------------------------------------------------------------------- #
@app.websocket("/ws/{session_id}")
async def ws_endpoint(ws: WebSocket, session_id: str) -> None:
    token = ws.query_params.get("token")
    host_key = ws.query_params.get("host")

    # authenticate before accept is not possible with query params in all
    # clients, so accept then close with an error message if invalid.
    await ws.accept()
    try:
        viewer = await _viewer_for(session_id, host_key, token)
    except HTTPException as exc:
        await ws.send_json({"type": "error", "code": "auth", "detail": exc.detail})
        await ws.close(code=4401)
        return

    try:
        await engine.get_session(session_id)
    except EngineError:
        await ws.send_json({"type": "error", "code": "no_session", "detail": "会话不存在"})
        await ws.close(code=4404)
        return

    client = Client(ws=ws, role_id=None if viewer[0] == "host" else viewer[1])
    hub.add(session_id, client)

    sender = asyncio.create_task(_ws_sender(session_id, client, viewer))
    await send_snapshot(client, session_id, viewer)
    await broadcast_snapshot(session_id)  # refresh online flags for everyone

    try:
        while True:
            raw = await ws.receive_json()
            await _handle_ws_message(session_id, client, viewer, raw)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        sender.cancel()
        hub.remove(session_id, client)
        await broadcast_snapshot(session_id)


async def _ws_sender(session_id: str, client: Client, viewer: tuple[str, str]) -> None:
    while True:
        msg = await client.queue.get()
        if msg is None:
            return
        if msg.get("type") == "resync":
            await send_snapshot(client, session_id, viewer)
            continue
        try:
            await client.ws.send_json(msg)
        except Exception:
            return


async def _handle_ws_message(
    session_id: str, client: Client, viewer: tuple[str, str], raw: dict[str, Any]
) -> None:
    mtype = raw.get("type")
    try:
        if mtype == "ping":
            hub.push_to(client, {"type": "pong", "t": raw.get("t"), "server_time": now_ts()})
            return

        if viewer[0] == "host":
            await _handle_host_message(session_id, raw)
            return

        if mtype == "submit":
            cid = str(raw.get("choice_id") or "")
            stage_id = raw.get("stage_id")
            client_stage = raw.get("client_stage")
            try:
                result = await engine.submit(
                    session_id, viewer[1], stage_id, cid, client_stage=client_stage
                )
            except EngineError as exc:
                hub.push_to(
                    client,
                    {"type": "error", "code": "submit_rejected", "detail": str(exc),
                     "status": exc.status},
                )
                await send_snapshot(client, session_id, viewer)
                return
            hub.push_to(client, {"type": "submit_result", **result})
            await broadcast_snapshot(session_id)
            await reschedule(session_id)
            return

        hub.push_to(client, {"type": "error", "code": "unknown_type", "detail": mtype})
    except EngineError as exc:
        hub.push_to(client, {"type": "error", "code": "bad_action", "detail": str(exc)})


async def _handle_host_message(session_id: str, raw: dict[str, Any]) -> None:
    mtype = raw.get("type")
    if mtype == "start":
        await engine.start(session_id)
    elif mtype == "settle":
        await engine.settle(session_id, "host")
    elif mtype == "end":
        await engine.end_session(session_id)
    else:
        return
    await broadcast_snapshot(session_id)
    await reschedule(session_id)


# --------------------------------------------------------------------------- #
# static frontend (mounted last so /api and /ws take precedence)
# --------------------------------------------------------------------------- #
if config.STATIC_DIR.exists():
    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(config.STATIC_DIR / "index.html")

    app.mount("/", StaticFiles(directory=str(config.STATIC_DIR), html=True), name="static")
