"""无第三方依赖的纯函数核心：事件常量、剧本重建、分支求值、状态重放。

engine / recorder / tests 共用本模块，便于脱离数据库做单元测试。
"""
from __future__ import annotations

from typing import Any

from .scenario import Scenario, parse_scenario

# 事件类型
EV_SESSION_CREATED = "session_created"
EV_PARTICIPANT_JOINED = "participant_joined"
EV_STAGE_STARTED = "stage_started"
EV_CHOICE_SUBMITTED = "choice_submitted"
EV_STAGE_SETTLED = "stage_settled"
EV_SESSION_ENDED = "session_ended"

TIMEOUT_MARK = "__timeout__"


def scenario_from_yaml(yaml_text: str) -> Scenario:
    return parse_scenario(yaml_text)


def evaluate_branches(scenario: Scenario, stage_id: str,
                      choices: dict[str, str]) -> tuple[str | None, dict | None]:
    """按定义顺序匹配条件分支；条件为 AND 语义，命中第一个即返回。

    choices 中只应包含按时提交的有效选择。
    """
    stage = scenario.stages[stage_id]
    for br in stage.branches:
        ok = True
        matched_conditions: list[dict] = []
        for cond in br.when:
            role_id = cond["role"]
            allowed = cond.get("in")
            if allowed is None:
                allowed = [cond["is"]]
            actual = choices.get(role_id)
            if actual is None or actual not in allowed:
                ok = False
                break
            matched_conditions.append({"role": role_id, "choice": actual})
        if ok:
            return br.next_stage, {"next_stage": br.next_stage,
                                   "matched": matched_conditions}
    if stage.default_next is not None:
        return stage.default_next, {"next_stage": stage.default_next,
                                    "matched": []}
    return None, None


def build_state(scenario: Scenario, events: list[dict],
                participants_rows: list[Any] | None = None) -> dict:
    """事件重放 → 当前状态（主持人全量视图的原始数据）。"""
    participants: dict[str, dict] = {}
    if participants_rows:
        for p in participants_rows:
            d = dict(p) if not isinstance(p, dict) else p
            participants[d["id"]] = {
                "id": d["id"],
                "role_id": d["role_id"],
                "name": d["name"],
                "joined_at": d.get("joined_at"),
            }

    status = "pending"
    current_stage: str | None = None
    stage_started_at: float | None = None
    deadline: float | None = None
    timeline: list[dict] = []
    by_stage: dict[str, dict] = {}
    version = 0

    for ev in events:
        version = ev["version"]
        p = ev["payload"]
        t = ev["at"]
        if ev["type"] == EV_PARTICIPANT_JOINED:
            participants[p["participant_id"]] = {
                "id": p["participant_id"],
                "role_id": p["role_id"],
                "name": p["name"],
                "joined_at": t,
            }
        elif ev["type"] == EV_STAGE_STARTED:
            status = "running"
            current_stage = p["stage_id"]
            stage_started_at = t
            deadline = p["deadline"]
            rec = {
                "stage_id": p["stage_id"],
                "started_at": t,
                "deadline": p["deadline"],
                "settled_at": None,
                "choices": {},
                "late_attempts": {},
                "timed_out": [],
                "next_stage": None,
            }
            timeline.append(rec)
            by_stage[p["stage_id"]] = rec
        elif ev["type"] == EV_CHOICE_SUBMITTED:
            if current_stage == p["stage_id"]:
                if p.get("late"):
                    by_stage[p["stage_id"]]["late_attempts"][p["role_id"]] = (
                        p["choice_id"])
                else:
                    by_stage[p["stage_id"]]["choices"][p["role_id"]] = (
                        p["choice_id"])
        elif ev["type"] == EV_STAGE_SETTLED:
            status = "running"
            rec = by_stage[p["stage_id"]]
            rec["settled_at"] = t
            rec["choices"] = dict(p["choices"])
            rec["late_attempts"] = dict(p.get("late_choices", {}))
            rec["timed_out"] = list(p["timed_out"])
            rec["next_stage"] = p.get("next_stage")
            current_stage = None
            deadline = None
        elif ev["type"] == EV_SESSION_ENDED:
            status = "ended"
            current_stage = None
            deadline = None

    return {
        "status": status,
        "current_stage": current_stage,
        "stage_started_at": stage_started_at,
        "deadline": deadline,
        "participants": participants,
        "timeline": timeline,
        "version": version,
    }
