"""事件溯源引擎。

所有状态变化都追加到 events 表（单调递增 version），当前状态由事件重放得到。
- 每个 session 一把异步锁，保证“截止时刻自动结算”与“最后一秒提交”串行化。
- 写事务使用 BEGIN IMMEDIATE + busy_timeout，配合单 worker 串行化写入。
- 重复提交：submissions 上 (session_id, participant_id, stage_id) 唯一约束。
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid
from typing import Awaitable, Callable

import aiosqlite

from .core import (
    EV_CHOICE_SUBMITTED,
    EV_PARTICIPANT_JOINED,
    EV_SESSION_CREATED,
    EV_SESSION_ENDED,
    EV_STAGE_SETTLED,
    EV_STAGE_STARTED,
    TIMEOUT_MARK,
    build_state,
    evaluate_branches as _evaluate_branches,
)
from .scenario import (
    Scenario,
    parse_scenario,
    participant_stage_view,
    scenario_public_meta,
)

# 事件常量从 core 再导出，保持 engine / recorder 的既有导入路径
__all__ = [
    "Conflict", "NotFound", "AuthError", "Engine",
    "EV_SESSION_CREATED", "EV_PARTICIPANT_JOINED", "EV_STAGE_STARTED",
    "EV_CHOICE_SUBMITTED", "EV_STAGE_SETTLED", "EV_SESSION_ENDED",
    "TIMEOUT_MARK",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           TEXT PRIMARY KEY,
    host_token   TEXT NOT NULL,
    scenario_yaml TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    current_stage TEXT,
    created_at   REAL NOT NULL,
    deadline     REAL
);
CREATE TABLE IF NOT EXISTS participants (
    id         TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    role_id    TEXT NOT NULL,
    name       TEXT NOT NULL,
    token      TEXT NOT NULL,
    joined_at  REAL NOT NULL,
    UNIQUE(session_id, role_id)
);
CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    version    INTEGER NOT NULL,
    type       TEXT NOT NULL,
    payload    TEXT NOT NULL,
    at         REAL NOT NULL,
    UNIQUE(session_id, version)
);
CREATE TABLE IF NOT EXISTS submissions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL,
    participant_id TEXT NOT NULL,
    stage_id       TEXT NOT NULL,
    role_id        TEXT NOT NULL,
    choice_id      TEXT NOT NULL,
    at             REAL NOT NULL,
    late           INTEGER NOT NULL DEFAULT 0,
    request_id     TEXT,
    UNIQUE(session_id, participant_id, stage_id)
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, version);
CREATE INDEX IF NOT EXISTS idx_sub_session ON submissions(session_id, stage_id);
CREATE INDEX IF NOT EXISTS idx_participants_session ON participants(session_id);
"""


class Conflict(Exception):
    """409：状态冲突 / 重复提交 / 已截止。"""


class NotFound(Exception):
    """404。"""


class AuthError(Exception):
    """401/403。"""


def _gen_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_urlsafe(8)}"


# ---------------- 状态重放 ----------------

def _scenario_from_row(row) -> Scenario:
    return parse_scenario(row["scenario_yaml"])


# ---------------- 快照裁剪 ----------------

def host_snapshot(session: dict, scenario: Scenario, state: dict,
                  events: list[dict], now: float) -> dict:
    stage_history = []
    for rec in state["timeline"]:
        if rec["settled_at"] is None:
            continue  # 进行中的阶段只出现在 current_stage
        stage = scenario.stages[rec["stage_id"]]
        stage_history.append({
            **_host_stage_full(scenario, rec),
            "name": stage.name,
            "started_at": rec["started_at"],
            "deadline": rec["deadline"],
            "settled_at": rec["settled_at"],
            "next_stage": rec["next_stage"],
        })

    current = None
    if state["current_stage"]:
        rec = by_stage_or_running(state, state["current_stage"])
        current = _host_stage_full(scenario, rec)
        current["live_choices"] = dict(rec.get("choices", {}))
        current["late_attempts"] = dict(rec.get("late_attempts", {}))
        current["name"] = scenario.stages[state["current_stage"]].name
        current["started_at"] = state["stage_started_at"]
        current["deadline"] = state["deadline"]

    return {
        "view": "host",
        "session_id": session["id"],
        "status": state["status"],
        "version": state["version"],
        "server_time": now,
        "scenario": _scenario_to_dict(scenario),
        "participants": {
            pid: {"role_id": p["role_id"], "name": p["name"]}
            for pid, p in state["participants"].items()
        },
        "current_stage": current,
        "stages": stage_history,
        "events": events,
    }


def _scenario_to_dict(scenario: Scenario) -> dict:
    """面向前端的剧本结构：roles 为数组，stages 为 id 索引字典。"""
    return {
        "title": scenario.title,
        "description": scenario.description,
        "first_stage": scenario.first_stage,
        "roles": [
            {"id": r.id, "name": r.name, "description": r.description,
             "invite_code": r.invite_code}
            for r in scenario.roles.values()
        ],
        "stages": {
            sid: {
                "id": st.id,
                "name": st.name,
                "duration": st.duration,
                "brief": st.brief,
                "content": st.content,
                "options": st.options,
                "default_next": st.default_next,
            }
            for sid, st in scenario.stages.items()
        },
    }


def by_stage_or_running(state: dict, stage_id: str) -> dict:
    for rec in state["timeline"]:
        if rec["stage_id"] == stage_id:
            return rec
    raise KeyError(stage_id)


def _host_stage_full(scenario: Scenario, rec: dict) -> dict:
    """主持人看到的阶段全量：所有角色的 brief/content/options + 最终选择。"""
    stage_id = rec["stage_id"]
    stage = scenario.stages[stage_id]
    roles_view = {}
    for rid in scenario.roles:
        roles_view[rid] = participant_stage_view(scenario, stage_id, rid)
    return {
        "stage_id": stage_id,
        "roles": roles_view,
        "choices": dict(rec.get("choices", {})),
        "late_attempts": dict(rec.get("late_attempts", {})),
        "timed_out": list(rec.get("timed_out", [])),
    }


def participant_snapshot(session_id: str, participant: dict, scenario: Scenario,
                         state: dict, now: float) -> dict:
    rid = participant["role_id"]
    pid = participant["id"]

    stages = []
    current = None
    for rec in state["timeline"]:
        view = participant_stage_view(scenario, rec["stage_id"], rid)
        raw_mine = rec["choices"].get(rid)
        entry: dict = {
            "stage_id": rec["stage_id"],
            "name": view["name"],
            "brief": view["brief"],
            "content": view["content"],
            "options": view["options"],
            "my_choice": None if raw_mine == TIMEOUT_MARK else raw_mine,
            "i_timed_out": (raw_mine == TIMEOUT_MARK
                            or rid in rec.get("timed_out", [])),
            "settled": rec["settled_at"] is not None,
        }
        if rec["settled_at"] is None:
            # 进行中的阶段：只暴露自己的选择，隐藏他人
            current = {
                **entry,
                "started_at": rec["started_at"],
                "deadline": rec["deadline"],
                "server_time": now,
            }
        else:
            # 已结算：仍不暴露其他角色的私有内容与选择，
            # 只告知本角色下一阶段（已在后续阶段中自然呈现）。
            stages.append(entry)

    return {
        "view": "participant",
        "session_id": session_id,
        "participant_id": pid,
        "role": {"id": rid, "name": scenario.roles[rid].name},
        "status": state["status"],
        "version": state["version"],
        "server_time": now,
        "scenario_meta": scenario_public_meta(scenario),
        "current_stage": current,
        "stages": stages,
    }


# ---------------- Engine ----------------

Notify = Callable[[str], Awaitable[None]]


class Engine:
    def __init__(self, db_path: str, notify: Notify):
        self.db_path = db_path
        self.notify = notify
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, session_id: str) -> asyncio.Lock:
        # session 只增不减；演练生命周期短，锁表无需清理
        if session_id not in self._locks:
            self._locks[session_id] = asyncio.Lock()
        return self._locks[session_id]

    async def connect(self) -> aiosqlite.Connection:
        conn = await aiosqlite.connect(self.db_path)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        return conn

    async def init_db(self) -> None:
        conn = await self.connect()
        try:
            await conn.executescript(SCHEMA)
            await conn.commit()
        finally:
            await conn.close()

    # ---------- 基础读取 ----------

    async def _get_session_row(self, conn: aiosqlite.Connection,
                               session_id: str) -> aiosqlite.Row:
        cur = await conn.execute(
            "SELECT * FROM sessions WHERE id=?", (session_id,))
        row = await cur.fetchone()
        if not row:
            raise NotFound("演练不存在")
        return row

    async def get_session(self, session_id: str) -> dict:
        conn = await self.connect()
        try:
            row = await self._get_session_row(conn, session_id)
            return dict(row)
        finally:
            await conn.close()

    async def _load(self, conn: aiosqlite.Connection, session_id: str):
        row = await self._get_session_row(conn, session_id)
        scenario = _scenario_from_row(row)
        cur = await conn.execute(
            "SELECT * FROM events WHERE session_id=? ORDER BY version",
            (session_id,))
        events = [dict(r) for r in await cur.fetchall()]
        for ev in events:
            ev["payload"] = json.loads(ev["payload"])
        cur = await conn.execute(
            "SELECT * FROM participants WHERE session_id=?", (session_id,))
        prows = await cur.fetchall()
        state = build_state(scenario, events, prows)
        return row, scenario, events, state

    async def snapshot_host(self, session_id: str, token: str) -> dict:
        async with self.lock(session_id):
            conn = await self.connect()
            try:
                row, scenario, events, state = await self._load(conn, session_id)
                if not secrets.compare_digest(token, row["host_token"]):
                    raise AuthError("主持人凭证无效")
                return host_snapshot(dict(row), scenario, state, events,
                                     time.time())
            finally:
                await conn.close()

    async def snapshot_participant(self, session_id: str, participant_id: str,
                                   token: str) -> dict:
        async with self.lock(session_id):
            conn = await self.connect()
            try:
                row, scenario, _, state = await self._load(conn, session_id)
                cur = await conn.execute(
                    "SELECT * FROM participants WHERE id=?", (participant_id,))
                prow = await cur.fetchone()
                if not prow or prow["session_id"] != session_id:
                    raise AuthError("参与者不存在")
                if not secrets.compare_digest(token, prow["token"]):
                    raise AuthError("参与者凭证无效")
                return participant_snapshot(
                    session_id, dict(prow), scenario, state, time.time())
            finally:
                await conn.close()

    # ---------- 写入原语 ----------

    async def _append_event(self, conn: aiosqlite.Connection, session_id: str,
                            etype: str, payload: dict, at: float | None = None
                            ) -> dict:
        at = time.time() if at is None else at
        cur = await conn.execute(
            "SELECT COALESCE(MAX(version),0)+1 AS v FROM events WHERE session_id=?",
            (session_id,))
        version = (await cur.fetchone())["v"]
        event = {
            "id": None,
            "session_id": session_id,
            "version": version,
            "type": etype,
            "payload": payload,
            "at": at,
        }
        await conn.execute(
            "INSERT INTO events(session_id, version, type, payload, at) "
            "VALUES(?,?,?,?,?)",
            (session_id, version, etype, json.dumps(payload, ensure_ascii=False), at),
        )
        event["version"] = version
        return event

    async def create_session(self, yaml_text: str) -> dict:
        scenario = parse_scenario(yaml_text)
        session_id = _gen_id("s")
        host_token = secrets.token_urlsafe(24)
        now = time.time()
        async with self.lock(session_id):
            conn = await self.connect()
            try:
                await conn.execute("BEGIN IMMEDIATE")
                await conn.execute(
                    "INSERT INTO sessions(id, host_token, scenario_yaml, status,"
                    " created_at) VALUES(?,?,?,?,?)",
                    (session_id, host_token, yaml_text, "pending", now),
                )
                await self._append_event(conn, session_id, EV_SESSION_CREATED, {
                    "title": scenario.title,
                    "role_ids": scenario.role_ids,
                    "first_stage": scenario.first_stage,
                }, at=now)
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
            finally:
                await conn.close()
        await self.notify(session_id)
        return {"session_id": session_id, "host_token": host_token,
                "scenario": _scenario_to_dict(scenario)}

    async def join(self, session_id: str, invite_code: str, name: str,
                   request_id: str | None = None) -> dict:
        name = (name or "").strip()
        if not name:
            raise Conflict("请填写姓名")
        async with self.lock(session_id):
            conn = await self.connect()
            try:
                await conn.execute("BEGIN IMMEDIATE")
                row, scenario, _, state = await self._load(conn, session_id)
                if state["status"] != "pending":
                    raise Conflict("演练已开始或已结束，无法加入")
                role = scenario.role_by_code(invite_code)
                if role is None:
                    raise AuthError("邀请码无效")
                existing = [
                    p for p in state["participants"].values()
                    if p["role_id"] == role.id
                ]
                if existing:
                    raise Conflict(f"角色 {role.name} 已有人加入")
                participant_id = _gen_id("p")
                token = secrets.token_urlsafe(24)
                await conn.execute(
                    "INSERT INTO participants(id, session_id, role_id, name,"
                    " token, joined_at) VALUES(?,?,?,?,?,?)",
                    (participant_id, session_id, role.id, name, token,
                     time.time()),
                )
                await self._append_event(conn, session_id,
                                         EV_PARTICIPANT_JOINED, {
                    "participant_id": participant_id,
                    "role_id": role.id,
                    "name": name,
                })
                await conn.commit()
                ret = {"participant_id": participant_id, "token": token,
                       "role_id": role.id, "role_name": role.name,
                       "name": name, "duplicate": False}
            except BaseException:
                await conn.rollback()
                raise
            finally:
                await conn.close()
        await self.notify(session_id)
        return ret

    async def start(self, session_id: str, host_token: str) -> dict:
        async with self.lock(session_id):
            conn = await self.connect()
            started = False
            try:
                await conn.execute("BEGIN IMMEDIATE")
                row, scenario, _, state = await self._load(conn, session_id)
                if not secrets.compare_digest(host_token, row["host_token"]):
                    raise AuthError("主持人凭证无效")
                if state["status"] == "ended":
                    raise Conflict("演练已结束")
                if state["current_stage"]:
                    raise Conflict("已有阶段进行中")
                stage_id = (state["timeline"][-1]["stage_id"]
                            if state["timeline"] else scenario.first_stage)
                now = time.time()
                deadline = now + scenario.stages[stage_id].duration
                await self._append_event(conn, session_id, EV_STAGE_STARTED, {
                    "stage_id": stage_id,
                    "duration": scenario.stages[stage_id].duration,
                    "deadline": deadline,
                }, at=now)
                await conn.execute(
                    "UPDATE sessions SET status='running', current_stage=?,"
                    " deadline=? WHERE id=?",
                    (stage_id, deadline, session_id))
                await conn.commit()
                started = True
            except BaseException:
                await conn.rollback()
                raise
            finally:
                await conn.close()
        if started:
            await self.notify(session_id)
        return {"ok": True}

    async def submit_choice(self, session_id: str, participant_id: str,
                            token: str, choice_id: str,
                            request_id: str | None = None) -> dict:
        """参与者提交选择。幂等：同阶段重复提交返回首次结果。"""
        async with self.lock(session_id):
            conn = await self.connect()
            try:
                await conn.execute("BEGIN IMMEDIATE")
                row, scenario, _, state = await self._load(conn, session_id)
                cur = await conn.execute(
                    "SELECT * FROM participants WHERE id=?", (participant_id,))
                prow = await cur.fetchone()
                if not prow or prow["session_id"] != session_id:
                    raise AuthError("参与者不存在")
                if not secrets.compare_digest(token, prow["token"]):
                    raise AuthError("参与者凭证无效")

                rid = prow["role_id"]
                stage_id = state["current_stage"]
                if state["status"] != "running" or not stage_id:
                    raise Conflict("当前没有进行中的阶段")

                # 截止竞争：以服务端时钟为准
                now = time.time()
                late = now > state["deadline"] + 1e-6

                # 合法选项校验
                view = participant_stage_view(scenario, stage_id, rid)
                valid_ids = {o["id"] for o in view["options"]}
                if choice_id not in valid_ids:
                    raise Conflict("选项无效或本角色不可见")

                # 幂等：查首次提交（含 request_id 去重）
                cur = await conn.execute(
                    "SELECT * FROM submissions WHERE session_id=? AND"
                    " participant_id=? AND stage_id=?",
                    (session_id, participant_id, stage_id))
                prev = await cur.fetchone()
                if prev:
                    await conn.commit()
                    return {"duplicate": True, "accepted": not bool(prev["late"]),
                            "choice_id": prev["choice_id"],
                            "stage_id": stage_id}

                await conn.execute(
                    "INSERT INTO submissions(session_id, participant_id,"
                    " stage_id, role_id, choice_id, at, late, request_id)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    (session_id, participant_id, stage_id, rid, choice_id,
                     now, 1 if late else 0, request_id or uuid.uuid4().hex),
                )
                payload = {
                    "participant_id": participant_id,
                    "role_id": rid,
                    "stage_id": stage_id,
                    "choice_id": choice_id,
                    "at": now,
                    "late": late,
                }
                # 逾期提交不进入有效选择，但仍记录为事件（审计可见）
                event = await self._append_event(
                    conn, session_id, EV_CHOICE_SUBMITTED, payload, at=now)
                await conn.commit()
                accepted = not late
                # 若恰好踩在截止点/全员齐，在【同一把锁、同一连接】内继续结算，
                # 杜绝“提交已提交但结算连接读不到”的可见性窗口。
                should_settle = late or await self._all_submitted(
                    conn, scenario, session_id, stage_id, state)
                settle_result = {"settled": False}
                if should_settle:
                    await conn.execute("BEGIN IMMEDIATE")
                    row2, scenario2, _, state2 = await self._load(
                        conn, session_id)
                    settle_result = await self._settle_tx(
                        conn, row2, scenario2, state2,
                        manual=False, allow_early=not late, now=time.time())
                    await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
            finally:
                await conn.close()

        await self.notify(session_id)
        return {"duplicate": False, "accepted": accepted,
                "choice_id": choice_id, "stage_id": stage_id,
                "version": event["version"],
                "settled": settle_result.get("settled", False),
                "next_stage": settle_result.get("next_stage")}

    async def _all_submitted(self, conn, scenario, session_id, stage_id,
                             state) -> bool:
        cur = await conn.execute(
            "SELECT COUNT(*) AS c FROM submissions WHERE session_id=? AND"
            " stage_id=? AND late=0", (session_id, stage_id))
        n = (await cur.fetchone())["c"]
        return n >= len(scenario.roles)

    async def _settle_tx(self, conn, row, scenario, state, *, manual: bool,
                         allow_early: bool, now: float,
                         host_token: str | None = None) -> dict:
        """在已持锁、已 BEGIN IMMEDIATE 的连接上执行结算。调用方负责提交。"""
        if host_token is not None and not secrets.compare_digest(
                host_token, row["host_token"]):
            raise AuthError("主持人凭证无效")
        session_id = row["id"]
        stage_id = state["current_stage"]
        if not stage_id:
            return {"settled": False}

        due = now >= state["deadline"] - 1e-6
        if not manual and not due:
            if not allow_early:
                return {"settled": False, "reason": "not_due"}
            cur = await conn.execute(
                "SELECT COUNT(*) AS c FROM submissions WHERE"
                " session_id=? AND stage_id=? AND late=0",
                (session_id, stage_id))
            if (await cur.fetchone())["c"] < len(scenario.roles):
                return {"settled": False, "reason": "not_due"}

        cur = await conn.execute(
            "SELECT role_id, choice_id FROM submissions WHERE"
            " session_id=? AND stage_id=? AND late=0",
            (session_id, stage_id))
        choices = {r["role_id"]: r["choice_id"]
                   for r in await cur.fetchall()}
        cur = await conn.execute(
            "SELECT role_id, choice_id FROM submissions WHERE"
            " session_id=? AND stage_id=? AND late=1",
            (session_id, stage_id))
        late_choices = {r["role_id"]: r["choice_id"]
                        for r in await cur.fetchall()}
        timed_out = [
            rid for rid in scenario.roles
            if rid not in choices and rid not in late_choices
        ]

        # 仅按时提交的有效选择参与条件分支求值
        next_stage, branch_info = _evaluate_branches(
            scenario, stage_id, choices)

        # 结算事件中的 choices：有效选择 + 逾期角色标记为超时，
        # 供快照/重放完整还原；分支仅使用有效 choices。
        settled_choices = dict(choices)
        for rid in late_choices:
            settled_choices[rid] = TIMEOUT_MARK

        await self._append_event(conn, session_id, EV_STAGE_SETTLED, {
            "stage_id": stage_id,
            "choices": settled_choices,
            "timed_out": timed_out,
            "late_choices": late_choices,
            "branch": branch_info,
            "next_stage": next_stage,
            "manual": manual,
            "at": now,
        }, at=now)

        if next_stage:
            deadline2 = now + scenario.stages[next_stage].duration
            await self._append_event(
                conn, session_id, EV_STAGE_STARTED, {
                    "stage_id": next_stage,
                    "duration": scenario.stages[next_stage].duration,
                    "deadline": deadline2,
                    "from": stage_id,
                }, at=now)
            await conn.execute(
                "UPDATE sessions SET current_stage=?, deadline=?,"
                " status='running' WHERE id=?",
                (next_stage, deadline2, session_id))
        else:
            await self._append_event(
                conn, session_id, EV_SESSION_ENDED,
                {"reason": "no_next_stage", "from": stage_id}, at=now)
            await conn.execute(
                "UPDATE sessions SET status='ended', current_stage=NULL,"
                " deadline=NULL WHERE id=?", (session_id,))
        return {"settled": True, "stage_id": stage_id,
                "next_stage": next_stage}

    async def settle(self, session_id: str, host_token: str | None,
                     manual: bool = False, now_override: float | None = None,
                     allow_early: bool = False) -> dict:
        """结算当前阶段：汇总有效选择、超时名单、求分支、进入下一阶段/结束。

        幂等：没有进行中的阶段时直接返回当前状态。
        - manual=True（主持人）：允许提前结算。
        - allow_early=True：全员已按时提交时，即使未到截止也立即结算。
        """
        async with self.lock(session_id):
            conn = await self.connect()
            try:
                await conn.execute("BEGIN IMMEDIATE")
                row, scenario, _, state = await self._load(conn, session_id)
                now = time.time() if now_override is None else now_override
                result = await self._settle_tx(
                    conn, row, scenario, state, manual=manual,
                    allow_early=allow_early, now=now, host_token=host_token)
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
            finally:
                await conn.close()
        if result.get("settled"):
            await self.notify(session_id)
        return result

    async def end_session(self, session_id: str, host_token: str) -> dict:
        """主持人提前终止演练。"""
        async with self.lock(session_id):
            conn = await self.connect()
            try:
                await conn.execute("BEGIN IMMEDIATE")
                row, scenario, _, state = await self._load(conn, session_id)
                if not secrets.compare_digest(host_token, row["host_token"]):
                    raise AuthError("主持人凭证无效")
                if state["status"] == "ended":
                    await conn.commit()
                    return {"ended": False, "reason": "already_ended"}
                if state["current_stage"]:
                    # 先把进行中的阶段按手动提前结算归档，再结束
                    stage_id = state["current_stage"]
                    cur = await conn.execute(
                        "SELECT role_id, choice_id FROM submissions WHERE"
                        " session_id=? AND stage_id=? AND late=0",
                        (session_id, stage_id))
                    choices = {r["role_id"]: r["choice_id"]
                               for r in await cur.fetchall()}
                    await self._append_event(
                        conn, session_id, EV_STAGE_SETTLED, {
                            "stage_id": stage_id,
                            "choices": choices,
                            "timed_out": [rid for rid in scenario.roles
                                          if rid not in choices],
                            "late_choices": {},
                            "branch": None,
                            "next_stage": None,
                            "manual": True,
                            "ended_early": True,
                        })
                await self._append_event(conn, session_id, EV_SESSION_ENDED,
                                         {"reason": "host_ended"})
                await conn.execute(
                    "UPDATE sessions SET status='ended', current_stage=NULL,"
                    " deadline=NULL WHERE id=?", (session_id,))
                await conn.commit()
            except BaseException:
                await conn.rollback()
                raise
            finally:
                await conn.close()
        await self.notify(session_id)
        return {"ended": True}

    # ---------- 事件流 / 回放 ----------

    async def _get_session_row_ro(self, session_id: str) -> dict:
        """供回放/导出：session 行 + 反序列化后的事件列表。"""
        conn = await self.connect()
        try:
            row = await self._get_session_row(conn, session_id)
            cur = await conn.execute(
                "SELECT version, type, payload, at FROM events WHERE"
                " session_id=? ORDER BY version", (session_id,))
            events = []
            for r in await cur.fetchall():
                d = dict(r)
                d["payload"] = json.loads(d["payload"])
                events.append(d)
            data = dict(row)
            data["events"] = events
            return data
        finally:
            await conn.close()

    async def events(self, session_id: str, after_version: int = 0) -> list[dict]:
        conn = await self.connect()
        try:
            cur = await conn.execute(
                "SELECT version, type, payload, at FROM events WHERE"
                " session_id=? AND version>? ORDER BY version",
                (session_id, after_version))
            out = []
            for r in await cur.fetchall():
                d = dict(r)
                d["payload"] = json.loads(d["payload"])
                out.append(d)
            return out
        finally:
            await conn.close()

    async def authenticate_participant(self, session_id: str, participant_id: str,
                                       token: str) -> dict:
        conn = await self.connect()
        try:
            cur = await conn.execute(
                "SELECT * FROM participants WHERE id=? AND session_id=?",
                (participant_id, session_id))
            row = await cur.fetchone()
            if not row or not secrets.compare_digest(token, row["token"]):
                raise AuthError("认证失败")
            return dict(row)
        finally:
            await conn.close()

    async def check_host(self, session_id: str, token: str | None) -> bool:
        if not token:
            return False
        row = await self.get_session(session_id)
        return secrets.compare_digest(token, row["host_token"])

    async def deadline_map(self) -> dict[str, float]:
        """启动时扫描所有仍在运行的 session。"""
        conn = await self.connect()
        try:
            cur = await conn.execute(
                "SELECT id, deadline FROM sessions WHERE status='running'"
                " AND deadline IS NOT NULL")
            return {r["id"]: r["deadline"] for r in await cur.fetchall()}
        finally:
            await conn.close()

    async def _set_deadline_for_test(self, session_id: str,
                                     deadline: float) -> None:
        """测试辅助：把当前阶段截止时间改为指定时刻。"""
        async with self.lock(session_id):
            conn = await self.connect()
            try:
                await conn.execute("BEGIN IMMEDIATE")
                await conn.execute(
                    "UPDATE sessions SET deadline=? WHERE id=?",
                    (deadline, session_id))
                # 同步最近一次 stage_started 事件的载荷，保证事件日志自洽
                cur = await conn.execute(
                    "SELECT id, payload FROM events WHERE session_id=?"
                    " AND type=? ORDER BY version DESC LIMIT 1",
                    (session_id, EV_STAGE_STARTED))
                row = await cur.fetchone()
                if row:
                    payload = json.loads(row["payload"])
                    payload["deadline"] = deadline
                    await conn.execute(
                        "UPDATE events SET payload=? WHERE id=?",
                        (json.dumps(payload, ensure_ascii=False), row["id"]))
                await conn.commit()
            finally:
                await conn.close()
