"""FastAPI 入口：REST API、WebSocket 推送、截止时刻自动结算。"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import (
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import recorder
from .engine import AuthError, Conflict, Engine, NotFound
from .hub import Hub
from .scenario import ScenarioError, parse_scenario, scenario_public_meta

DB_PATH = os.environ.get("DRILL_DB_PATH", "/data/drill.db")
EXAMPLE_PATH = Path(__file__).parent / "scenarios" / "example.yaml"

hub = Hub()
engine: Engine | None = None
_timer_tasks: dict[str, asyncio.Task] = {}


# ---------------- 通知与截止定时器 ----------------

async def notify(session_id: str) -> None:
    await hub.broadcast(session_id, {"type": "changed", "at": time.time()})


async def _schedule_deadline(session_id: str, delay: float) -> None:
    old = _timer_tasks.pop(session_id, None)
    if old:
        old.cancel()
    if delay <= 0:
        asyncio.create_task(_fire_deadline(session_id))
        return
    _timer_tasks[session_id] = asyncio.create_task(
        _deadline_waiter(session_id, delay))


async def _deadline_waiter(session_id: str, delay: float) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return
    await _fire_deadline(session_id)


async def _fire_deadline(session_id: str) -> None:
    """截止时刻自动结算（幂等），结算后若进入下一阶段则继续挂表。"""
    try:
        result = await engine.settle(session_id, None, manual=False)
        if result.get("settled") and result.get("next_stage"):
            await _arm_next_deadline(session_id)
    except NotFound:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"[deadline] {session_id} 自动结算失败: {exc!r}")


async def _arm_next_deadline(session_id: str) -> None:
    snap = await engine.get_session(session_id)
    if snap["status"] == "running" and snap["deadline"]:
        await _schedule_deadline(session_id,
                                 max(0.0, snap["deadline"] - time.time()))
    else:
        _timer_tasks.pop(session_id, None)


@asynccontextmanager
async def lifespan(app: FastAPI):
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    global engine
    engine = Engine(DB_PATH, notify)
    await engine.init_db()
    # 重启恢复：为所有仍在运行的演练重新挂截止定时器
    for sid, deadline in (await engine.deadline_map()).items():
        await _schedule_deadline(sid, max(0.0, deadline - time.time()))
    yield
    for task in _timer_tasks.values():
        task.cancel()


app = FastAPI(title="多人应急演练", lifespan=lifespan)


def _bearer(header: str | None, query: str | None) -> str | None:
    if header and header.lower().startswith("bearer "):
        return header[7:].strip()
    return query


@app.exception_handler(Conflict)
async def conflict_handler(request: Request, exc: Conflict):
    return JSONResponse(status_code=409, content={"detail": str(exc)})


@app.exception_handler(AuthError)
async def auth_handler(request: Request, exc: AuthError):
    return JSONResponse(status_code=403, content={"detail": str(exc)})


@app.exception_handler(NotFound)
async def notfound_handler(request: Request, exc: NotFound):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(ScenarioError)
async def scenario_handler(request: Request, exc: ScenarioError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# ---------------- 请求模型 ----------------

class CreateIn(BaseModel):
    yaml: str | None = None
    use_example: bool = True


class JoinIn(BaseModel):
    invite_code: str
    name: str
    request_id: str | None = None


class SubmitIn(BaseModel):
    participant_id: str
    token: str
    choice_id: str
    request_id: str | None = None


class AuthIn(BaseModel):
    token: str


# ---------------- 公共 API ----------------

@app.get("/api/health")
async def health():
    return {"ok": True, "time": time.time()}


@app.get("/api/example.yaml")
async def example_yaml():
    return PlainTextResponse(EXAMPLE_PATH.read_text(encoding="utf-8"),
                             media_type="text/yaml; charset=utf-8")


@app.post("/api/sessions", status_code=201)
async def create_session(body: CreateIn):
    if body.yaml is not None and body.yaml.strip():
        yaml_text = body.yaml
    elif body.use_example:
        yaml_text = EXAMPLE_PATH.read_text(encoding="utf-8")
    else:
        raise HTTPException(422, "需要提供 yaml 或 use_example=true")
    # 提前校验，错误以 422 返回
    scenario = parse_scenario(yaml_text)
    result = await engine.create_session(yaml_text)
    return {
        **result,
        "meta": scenario_public_meta(scenario),
        "host_url": f"/host.html?session={result['session_id']}"
                    f"&token={result['host_token']}",
        "join_path": f"/join.html?session={result['session_id']}",
    }


@app.get("/api/sessions/{session_id}")
async def session_meta(session_id: str):
    row = await engine.get_session(session_id)
    from .engine import _scenario_from_row
    scenario = _scenario_from_row(row)
    return {
        "session_id": session_id,
        "status": row["status"],
        "meta": scenario_public_meta(scenario),
    }


@app.post("/api/sessions/{session_id}/join")
async def join(session_id: str, body: JoinIn):
    return await engine.join(session_id, body.invite_code, body.name,
                             body.request_id)


@app.post("/api/sessions/{session_id}/start")
async def start(session_id: str, body: AuthIn,
                authorization: str | None = Header(default=None),
                token: str | None = Query(default=None)):
    tok = body.token or _bearer(authorization, token)
    await engine.start(session_id, tok)
    await _arm_next_deadline(session_id)
    return {"ok": True}


@app.post("/api/sessions/{session_id}/settle")
async def settle(session_id: str, body: AuthIn,
                 authorization: str | None = Header(default=None),
                 token: str | None = Query(default=None)):
    tok = body.token or _bearer(authorization, token)
    result = await engine.settle(session_id, tok, manual=True)
    await _arm_next_deadline(session_id)
    return result


@app.post("/api/sessions/{session_id}/end")
async def end(session_id: str, body: AuthIn,
              authorization: str | None = Header(default=None),
              token: str | None = Query(default=None)):
    tok = body.token or _bearer(authorization, token)
    result = await engine.end_session(session_id, tok)
    _timer_tasks.pop(session_id, None)
    return result


@app.get("/api/sessions/{session_id}/host-snapshot")
async def host_snapshot(session_id: str,
                        authorization: str | None = Header(default=None),
                        token: str | None = Query(default=None)):
    tok = _bearer(authorization, token)
    return await engine.snapshot_host(session_id, tok)


@app.get("/api/sessions/{session_id}/participant-snapshot")
async def participant_snapshot(
    session_id: str,
    participant_id: str = Query(...),
    token: str = Query(...),
):
    return await engine.snapshot_participant(session_id, participant_id, token)


@app.post("/api/sessions/{session_id}/choices")
async def submit_choice(session_id: str, body: SubmitIn):
    result = await engine.submit_choice(
        session_id, body.participant_id, body.token, body.choice_id,
        body.request_id)
    # 结算若自动推进了阶段，需要确保下一阶段定时器就绪
    await _arm_next_deadline(session_id)
    return result


@app.get("/api/sessions/{session_id}/replay")
async def replay(session_id: str,
                 up_to_stage: str | None = Query(default=None),
                 authorization: str | None = Header(default=None),
                 token: str | None = Query(default=None)):
    tok = _bearer(authorization, token)
    if not await engine.check_host(session_id, tok):
        raise AuthError("主持人凭证无效")
    row = await engine._get_session_row_ro(session_id)  # noqa: SLF001
    return recorder.replay(row, up_to_stage)


@app.get("/api/sessions/{session_id}/export/host.md")
async def export_host(session_id: str,
                      authorization: str | None = Header(default=None),
                      token: str | None = Query(default=None)):
    tok = _bearer(authorization, token)
    if not await engine.check_host(session_id, tok):
        raise AuthError("主持人凭证无效")
    row = await engine._get_session_row_ro(session_id)  # noqa: SLF001
    md = recorder.export_host_markdown(row)
    return Response(
        content=md, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="drill-{session_id}-host.md"'})


@app.get("/api/sessions/{session_id}/export/me.md")
async def export_me(session_id: str,
                    participant_id: str = Query(...),
                    token: str = Query(...)):
    prow = await engine.authenticate_participant(
        session_id, participant_id, token)
    row = await engine._get_session_row_ro(session_id)  # noqa: SLF001
    md = recorder.export_participant_markdown(row, prow)
    return Response(
        content=md, media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="drill-{session_id}-me.md"'})


# ---------------- WebSocket ----------------

@app.websocket("/ws/sessions/{session_id}")
async def ws_session(websocket: WebSocket, session_id: str):
    """断线重连友好的推送通道。

    查询参数：
    - kind=host&token=...
    - kind=participant&participant_id=...&token=...
    连接建立后客户端发送 {"after": N}，服务端立即补发缺失事件并推当前快照标记。
    """
    params = websocket.query_params
    kind = params.get("kind")
    token = params.get("token", "")
    participant_id = params.get("participant_id", "")
    try:
        if kind == "host":
            if not await engine.check_host(session_id, token):
                await websocket.close(code=4403)
                return
            principal = "host"
        elif kind == "participant":
            await engine.authenticate_participant(
                session_id, participant_id, token)
            principal = participant_id
        else:
            await websocket.close(code=4400)
            return
    except (AuthError, NotFound):
        await websocket.close(code=4403)
        return

    await websocket.accept()
    ws_id = await hub.register(session_id, kind, principal, websocket)
    try:
        await websocket.send_json({"type": "hello", "ws_id": ws_id,
                                   "server_time": time.time()})
        while True:
            msg = await websocket.receive_json()
            if msg.get("type") == "ping":
                await websocket.send_json(
                    {"type": "pong", "at": time.time()})
            elif msg.get("type") == "sync":
                after = int(msg.get("after", 0))
                events = await engine.events(session_id, after)
                await websocket.send_json({
                    "type": "events",
                    "events": events,
                    "server_time": time.time(),
                })
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        await hub.unregister(session_id, ws_id)


# ---------------- 静态前端 ----------------

_STATIC = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(_STATIC), html=True), name="static")
