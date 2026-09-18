"""Drill engine: pure event-sourced state reducer + atomic SQLite commits.

Event log (all timestamps are POSIX seconds, UTC)
-------------------------------------------------
version 1, seq 0  session_started  {at, stage_id, deadline}
version 1, seq 1  stage_opened     {stage_id, deadline, at}
version N         participant_joined {role_id, display_name, at}
version N         stage_choice     {stage_id, role_id, choice_id, at}
version N, seq 0  stage_sealed     {stage_id, reason, choices, next, at}
version N, seq 1  stage_opened     {stage_id, deadline, at}
                 (or session_ended {reason, at})

A "version" is one atomic commit. Replay rebuilds state by folding events
in (version, seq) order — deterministic for any version prefix.
"""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from .db import Database, dumps
from .definitions import DrillDef, Option, Stage, Transition

E_SESSION_STARTED = "session_started"
E_PARTICIPANT_JOINED = "participant_joined"
E_STAGE_CHOICE = "stage_choice"
E_STAGE_SEALED = "stage_sealed"
E_STAGE_OPENED = "stage_opened"
E_SESSION_ENDED = "session_ended"


class EngineError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
def now_ts() -> float:
    return time.time()


def new_id(nbytes: int = 4) -> str:
    return secrets.token_hex(nbytes)  # 8-char session id


def new_token() -> str:
    return secrets.token_urlsafe(18)


def find_option(stage: Stage, role_id: str, choice_id: str) -> Option | None:
    for o in stage.options.get(role_id, []):
        if o.id == choice_id:
            return o
    return None


def evaluate(stage: Stage, choices: dict[str, str | None]) -> str | None:
    """Decide the next stage id from sealed choices.

    Priority: explicit option ``next`` of single decision holder, then ordered
    conditional transitions (first fully matching wins), then default_next.
    ``None`` means the drill ends.
    """
    for role_id, olist in stage.options.items():
        cid = choices.get(role_id)
        if cid is not None:
            opt = find_option(stage, role_id, cid)
            if opt is not None and opt.next is not None:
                return opt.next

    for t in stage.transitions:
        if all(choices.get(role) == want for role, want in t.when.items()):
            return t.next
    return stage.default_next


@dataclass
class SealedStage:
    stage_id: str
    version: int
    at: float
    reason: str
    choices: dict[str, str | None]
    next: str | None


@dataclass
class State:
    status: str = "created"  # created|running|ended
    end_reason: str | None = None
    current: str | None = None
    deadline: float | None = None
    started_at: float | None = None
    ended_at: float | None = None
    last_version: int = 0
    choices: dict[tuple[str, str], str] = field(default_factory=dict)
    sealed: dict[str, SealedStage] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    roster: dict[str, dict[str, Any]] = field(default_factory=dict)


def reduce_events(events: list[dict[str, Any]]) -> State:
    st = State()
    for ev in events:
        st.last_version = ev["version"]
        t = ev["type"]
        p = ev["payload"]
        if t == E_SESSION_STARTED:
            st.status = "running"
            st.started_at = p["at"]
        elif t == E_PARTICIPANT_JOINED:
            st.roster[p["role_id"]] = {
                "display_name": p["display_name"],
                "joined_at": p["at"],
            }
        elif t == E_STAGE_CHOICE:
            st.choices[(p["stage_id"], p["role_id"])] = p["choice_id"]
        elif t == E_STAGE_OPENED:
            st.current = p["stage_id"]
            st.deadline = p.get("deadline")
            if p["stage_id"] not in st.order:
                st.order.append(p["stage_id"])
        elif t == E_STAGE_SEALED:
            st.current = None
            st.deadline = None
            st.sealed[p["stage_id"]] = SealedStage(
                stage_id=p["stage_id"],
                version=ev["version"],
                at=p["at"],
                reason=p["reason"],
                choices=dict(p["choices"]),
                next=p["next"],
            )
        elif t == E_SESSION_ENDED:
            st.status = "ended"
            st.end_reason = p["reason"]
            st.ended_at = p["at"]
            st.current = None
            st.deadline = None
    return st


def _def_from_json(data: dict[str, Any]) -> DrillDef:
    from .definitions import Role

    roles = {rid: Role(**r) for rid, r in data["roles"].items()}
    stages: dict[str, Stage] = {}
    for sid, s in data["stages"].items():
        s = dict(s)
        s["options"] = {
            rid: [Option(**o) for o in olist] for rid, olist in s["options"].items()
        }
        s["transitions"] = [Transition(**t) for t in s["transitions"]]
        stages[sid] = Stage(**s)
    return DrillDef(
        id=data["id"],
        name=data["name"],
        description=data["description"],
        roles=roles,
        stages=stages,
        initial_stage=data["initial_stage"],
    )


# --------------------------------------------------------------------------- #
# engine
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self, db: Database):
        self.db = db
        self._locks: dict[str, asyncio.Lock] = {}

    def lock_for(self, session_id: str) -> asyncio.Lock:
        lk = self._locks.get(session_id)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[session_id] = lk
        return lk

    async def upsert_drill(self, d: DrillDef) -> None:
        import dataclasses

        await self.db.db.execute(
            "INSERT INTO drills(id, name, description, definition) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "description=excluded.description, definition=excluded.definition",
            (d.id, d.name, d.description, dumps(dataclasses.asdict(d))),
        )
        await self.db.commit()

    async def list_drills(self) -> list[dict[str, str]]:
        rows = await self.db.all("SELECT id, name, description FROM drills ORDER BY id")
        return [dict(r) for r in rows]

    async def get_definition(self, drill_id: str) -> DrillDef:
        row = await self.db.one("SELECT definition FROM drills WHERE id=?", (drill_id,))
        if row is None:
            raise EngineError(f"drill '{drill_id}' not found", 404)
        return _def_from_json(json.loads(row["definition"]))

    async def list_sessions(self) -> list[dict[str, Any]]:
        rows = await self.db.all(
            "SELECT id, drill_id, status, reason, created_at, started_at, ended_at "
            "FROM sessions ORDER BY rowid DESC"
        )
        return [dict(r) for r in rows]

    async def get_session(self, session_id: str) -> dict[str, Any]:
        row = await self.db.one(
            "SELECT id, drill_id, status, reason, created_at, started_at, ended_at "
            "FROM sessions WHERE id=?",
            (session_id,),
        )
        if row is None:
            raise EngineError("session not found", 404)
        return dict(row)

    async def create_session(self, drill_id: str) -> str:
        await self.get_definition(drill_id)  # 404 if missing
        for _ in range(5):
            sid = new_id()
            try:
                await self.db.db.execute(
                    "INSERT INTO sessions(id, drill_id, created_at) VALUES(?,?,?)",
                    (sid, drill_id, now_ts()),
                )
                await self.db.commit()
                return sid
            except Exception:
                await self.db.rollback()
        raise EngineError("could not allocate session id", 500)

    async def load(self, session_id: str) -> tuple[dict[str, Any], DrillDef, State]:
        """Fetch session row, its drill definition and folded state."""
        sess = await self.get_session(session_id)
        d = await self.get_definition(sess["drill_id"])
        rows = await self.db.all(
            "SELECT version, seq, type, actor, at, payload FROM events "
            "WHERE session_id=? ORDER BY version, seq",
            (session_id,),
        )
        events = [
            {
                "version": r["version"],
                "seq": r["seq"],
                "type": r["type"],
                "actor": r["actor"],
                "at": float(r["at"]),
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]
        return sess, d, reduce_events(events)

    async def get_participant(self, session_id: str, token: str) -> dict[str, Any] | None:
        row = await self.db.one(
            "SELECT session_id, role_id, display_name, token FROM participants "
            "WHERE session_id=? AND token=?",
            (session_id, token),
        )
        return dict(row) if row else None

    async def join(
        self, session_id: str, invite_code: str, display_name: str
    ) -> dict[str, Any]:
        async with self.lock_for(session_id):
            sess = await self.get_session(session_id)
            if sess["status"] == "ended":
                raise EngineError("演练已结束，无法加入", 409)
            d = await self.get_definition(sess["drill_id"])
            role = next((r for r in d.roles.values() if r.invite == invite_code.strip()), None)
            if role is None:
                raise EngineError("邀请码无效", 403)

            existing = await self.db.one(
                "SELECT token, display_name FROM participants WHERE session_id=? AND role_id=?",
                (session_id, role.id),
            )
            if existing is not None:
                raise EngineError(f"角色 {role.name} 已有人加入", 409)

            name = (display_name or "").strip() or role.name
            token = new_token()
            ts = now_ts()
            _, _, state = await self.load(session_id)
            version = state.last_version + 1
            await self.db.begin()
            try:
                await self.db.db.execute(
                    "INSERT INTO participants(session_id, role_id, display_name, token, joined_at) "
                    "VALUES(?,?,?,?,?)",
                    (session_id, role.id, name, token, ts),
                )
                await self._insert_event(
                    session_id, version, 0, E_PARTICIPANT_JOINED, role.id,
                    {"role_id": role.id, "display_name": name, "at": ts},
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
            return {
                "role_id": role.id,
                "role_name": role.name,
                "display_name": name,
                "token": token,
                "version": version,
            }

    async def start(self, session_id: str) -> dict[str, Any]:
        async with self.lock_for(session_id):
            sess = await self.get_session(session_id)
            if sess["status"] != "created":
                raise EngineError("会话已经开始或已结束", 409)
            d = await self.get_definition(sess["drill_id"])
            stage = d.stages[d.initial_stage]
            ts = now_ts()
            deadline = ts + stage.duration if stage.duration else None
            await self.db.begin()
            try:
                await self._insert_event(
                    session_id, 1, 0, E_SESSION_STARTED, "host",
                    {"at": ts, "stage_id": stage.id, "deadline": deadline},
                )
                await self._insert_event(
                    session_id, 1, 1, E_STAGE_OPENED, "system",
                    {"stage_id": stage.id, "deadline": deadline, "at": ts},
                )
                await self.db.db.execute(
                    "UPDATE sessions SET status='running', started_at=? WHERE id=?",
                    (ts, session_id),
                )
                await self.db.commit()
            except Exception:
                await self.db.rollback()
                raise
            return {
                "version": 1,
                "stage_id": stage.id,
                "deadline": deadline,
                "ended": False,
            }

    async def submit(
        self,
        session_id: str,
        role_id: str,
        stage_id: str | None,
        choice_id: str,
        client_stage: str | None = None,
    ) -> dict[str, Any]:
        """Submit a choice. Atomic against deadlines: if the deadline has been
        reached the choice is rejected and the stage is sealed in the same
        transaction. When the last required choice arrives, the stage settles
        immediately ("complete")."""
        async with self.lock_for(session_id):
            await self.db.begin()
            try:
                sess, d, state = await self.load(session_id)
                target_stage_id = stage_id or client_stage or state.current

                # Idempotency first: a retried submission (possibly arriving
                # after the stage has settled) replays the original outcome.
                if target_stage_id is not None:
                    prior = await self.db.one(
                        "SELECT choice_id FROM submissions "
                        "WHERE session_id=? AND stage_id=? AND role_id=?",
                        (session_id, target_stage_id, role_id),
                    )
                    if prior is not None:
                        await self.db.commit()
                        return {
                            "accepted": False,
                            "duplicate": True,
                            "stage_id": target_stage_id,
                            "choice_id": prior["choice_id"],
                            "same": prior["choice_id"] == choice_id,
                            "current_stage": state.current,
                            "deadline": state.deadline,
                            "settled": state.current != target_stage_id,
                            "ended": state.status == "ended",
                        }

                if sess["status"] != "running" or state.current is None:
                    raise EngineError("当前没有进行中的阶段", 409)
                stage = d.stages[state.current]
                if target_stage_id is not None and target_stage_id != stage.id:
                    raise EngineError("阶段已变化，请刷新后重试", 409)
                if role_id not in stage.options:
                    raise EngineError("本角色在当前阶段没有待决策项", 403)
                if find_option(stage, role_id, choice_id) is None:
                    raise EngineError("选项不存在", 400)

                ts = now_ts()
                if state.deadline is not None and ts >= state.deadline:
                    # Deadline race: reject and seal atomically.
                    seal = await self._seal(
                        session_id, d, state, stage, "deadline",
                        version=state.last_version + 1, at=ts,
                    )
                    await self._finalize_session_row(session_id, state)
                    await self.db.commit()
                    return {
                        "accepted": False,
                        "duplicate": False,
                        "reason": "deadline",
                        "deadline": state.deadline,
                        "settled": True,
                        **seal,
                    }

                version = state.last_version + 1
                await self.db.db.execute(
                    "INSERT INTO submissions(session_id, version, role_id, stage_id, choice_id, at) "
                    "VALUES(?,?,?,?,?,?)",
                    (session_id, version, role_id, stage.id, choice_id, ts),
                )
                await self._insert_event(
                    session_id, version, 0, E_STAGE_CHOICE, role_id,
                    {"stage_id": stage.id, "role_id": role_id, "choice_id": choice_id, "at": ts},
                )
                state.choices[(stage.id, role_id)] = choice_id
                state.last_version = version

                seal_info: dict[str, Any] | None = None
                deciding = list(stage.options.keys())
                all_in = all((stage.id, r) in state.choices for r in deciding)
                if all_in:
                    seal_info = await self._seal(
                        session_id, d, state, stage, "complete",
                        version=version, at=ts,
                    )
                await self._finalize_session_row(session_id, state)
                await self.db.commit()
                return {
                    "accepted": True,
                    "duplicate": False,
                    "choice_id": choice_id,
                    "version": version,
                    "deadline": state.deadline,
                    "settled": seal_info is not None,
                    **(seal_info or {}),
                }
            except EngineError:
                await self.db.rollback()
                raise
            except Exception:
                await self.db.rollback()
                raise

    async def _seal(
        self,
        session_id: str,
        d: DrillDef,
        state: State,
        stage: Stage,
        reason: str,
        version: int,
        at: float,
    ) -> dict[str, Any]:
        """Append stage_sealed and stage_opened/session_ended; mutate state.

        Must run inside an open transaction. ``reason`` is complete|deadline|host.
        """
        choices = {r: state.choices.get((stage.id, r)) for r in stage.options}
        nxt = evaluate(stage, choices)
        await self._insert_event(
            session_id, version, 0, E_STAGE_SEALED, "system",
            {
                "stage_id": stage.id,
                "reason": reason,
                "choices": choices,
                "next": nxt,
                "at": at,
            },
        )
        sealed = SealedStage(
            stage_id=stage.id, version=version, at=at, reason=reason,
            choices=choices, next=nxt,
        )
        state.sealed[stage.id] = sealed
        state.current = None
        state.deadline = None

        if nxt is not None and nxt in d.stages:
            nstage = d.stages[nxt]
            deadline = at + nstage.duration if nstage.duration else None
            await self._insert_event(
                session_id, version, 1, E_STAGE_OPENED, "system",
                {"stage_id": nstage.id, "deadline": deadline, "at": at},
            )
            state.current = nstage.id
            state.deadline = deadline
            if nstage.id not in state.order:
                state.order.append(nstage.id)
        else:
            await self._insert_event(
                session_id, version, 1, E_SESSION_ENDED,
                "host" if reason == "host" else "system",
                {"reason": reason, "at": at},
            )
            state.status = "ended"
            state.end_reason = reason
            state.ended_at = at
        state.last_version = version
        return {
            "sealed_stage": stage.id,
            "seal_reason": reason,
            "next_stage": state.current,
            "next_deadline": state.deadline,
            "ended": state.status == "ended",
            "seal_version": version,
        }

    async def settle(self, session_id: str, reason: str) -> dict[str, Any]:
        """Advance the open stage now. Used by the deadline timer and the
        presenter's manual control. Returns {noop:True} if nothing is open."""
        if reason not in ("deadline", "host"):
            reason = "host"
        async with self.lock_for(session_id):
            await self.db.begin()
            try:
                sess, d, state = await self.load(session_id)
                if sess["status"] != "running" or state.current is None:
                    await self.db.commit()
                    return {"noop": True}
                stage = d.stages[state.current]
                ts = now_ts()
                if reason == "deadline" and state.deadline is not None and ts < state.deadline:
                    # Timer fired early (shouldn't happen) — ignore.
                    await self.db.commit()
                    return {"noop": True, "early": True}
                info = await self._seal(
                    session_id, d, state, stage, reason,
                    version=state.last_version + 1, at=ts,
                )
                await self._finalize_session_row(session_id, state)
                await self.db.commit()
                return info
            except Exception:
                await self.db.rollback()
                raise

    async def end_session(self, session_id: str) -> dict[str, Any]:
        """Presenter terminates the whole drill: seal the open stage (host),
        then end the session. No-op if already ended."""
        async with self.lock_for(session_id):
            await self.db.begin()
            try:
                sess, d, state = await self.load(session_id)
                if state.status == "created":
                    ts = now_ts()
                    await self._insert_event(
                        session_id, 1, 0, E_SESSION_ENDED, "host",
                        {"reason": "host", "at": ts},
                    )
                    state.status = "ended"
                    state.end_reason = "host"
                    state.ended_at = ts
                elif state.status == "running":
                    ts = now_ts()
                    if state.current is not None:
                        stage = d.stages[state.current]
                        await self._seal(
                            session_id, d, state, stage, "host",
                            version=state.last_version + 1, at=ts,
                        )
                    if state.status != "ended":
                        await self._insert_event(
                            session_id, state.last_version + 1, 0,
                            E_SESSION_ENDED, "host",
                            {"reason": "host", "at": ts},
                        )
                        state.status = "ended"
                        state.end_reason = "host"
                        state.ended_at = ts
                await self._finalize_session_row(session_id, state)
                await self.db.commit()
                return {"ended": True, "ended_at": state.ended_at}
            except Exception:
                await self.db.rollback()
                raise

    # ---------- plumbing ----------
    async def _insert_event(
        self, session_id: str, version: int, seq: int, etype: str, actor: str, payload: dict
    ) -> None:
        await self.db.db.execute(
            "INSERT INTO events(session_id, version, seq, type, actor, at, payload) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                session_id, version, seq, etype, actor,
                payload.get("at", now_ts()), dumps(payload),
            ),
        )

    async def _finalize_session_row(self, session_id: str, state: State) -> None:
        if state.status == "ended":
            await self.db.db.execute(
                "UPDATE sessions SET status='ended', reason=?, ended_at=? WHERE id=?",
                (state.end_reason, state.ended_at, session_id),
            )
