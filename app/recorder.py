"""确定性回放与记录导出。

回放：仅依赖事件日志（events），从任意阶段重新求值分支并输出每步状态。
导出：
- 主持人：Markdown 全量记录（含所有角色隐藏信息、选择、分支、事件）
- 参与者：Markdown 个人记录（按角色权限裁剪，绝不包含他人隐藏内容）
"""
from __future__ import annotations

import json
from typing import Any

from .core import (
    EV_CHOICE_SUBMITTED,
    EV_PARTICIPANT_JOINED,
    EV_SESSION_ENDED,
    EV_STAGE_SETTLED,
    EV_STAGE_STARTED,
    TIMEOUT_MARK,
    evaluate_branches as _evaluate_branches,
)
from .engine import _scenario_from_row
from .scenario import participant_stage_view


def replay(session_row: dict, up_to_stage: str | None = None) -> dict:
    """基于事件日志确定性回放。

    返回每个阶段的进入/选择/结算/分支决策。up_to_stage 给出时，
    在该阶段结算事件处理后停止（用于“回放任一阶段”）。
    """
    scenario = _scenario_from_row(session_row)
    events = session_row["events"]

    participants: dict[str, dict] = {}
    stages: list[dict] = []
    current: dict | None = None
    checksums: list[str] = []

    def cksum(obj: Any) -> str:
        import hashlib
        blob = json.dumps(obj, sort_keys=True, ensure_ascii=False,
                          separators=(",", ":"))
        return hashlib.sha256(blob.encode()).hexdigest()[:12]

    for ev in events:
        p = ev["payload"]
        t = ev["type"]
        if t == EV_PARTICIPANT_JOINED:
            participants[p["participant_id"]] = {
                "role_id": p["role_id"], "name": p["name"]}
        elif t == EV_STAGE_STARTED:
            current = {
                "stage_id": p["stage_id"],
                "name": scenario.stages[p["stage_id"]].name,
                "version": ev["version"],
                "started_at": ev["at"],
                "submissions": [],
                "settlement": None,
            }
            stages.append(current)
        elif t == EV_CHOICE_SUBMITTED:
            if current is not None:
                current["submissions"].append({
                    "role_id": p["role_id"],
                    "choice_id": p["choice_id"],
                    "late": p.get("late", False),
                    "at": p["at"],
                })
        elif t == EV_STAGE_SETTLED:
            # 不从事件读取 next_stage，而是用相同输入重新求值，
            # 证明分支结果由事件日志确定性决定。
            re_eval_next, re_eval_branch = _evaluate_branches(
                scenario, p["stage_id"],
                {r: c for r, c in p["choices"].items() if c != TIMEOUT_MARK})
            consistent = re_eval_next == p.get("next_stage")
            settlement = {
                "version": ev["version"],
                "choices": p["choices"],
                "timed_out": p["timed_out"],
                "late_choices": p.get("late_choices", {}),
                "recorded_next_stage": p.get("next_stage"),
                "recomputed_next_stage": re_eval_next,
                "branch_consistent": consistent,
                "branch_detail": re_eval_branch,
                "manual": p.get("manual", False),
                "ended_early": p.get("ended_early", False),
            }
            if current is not None:
                current["settlement"] = settlement
            checksums.append(cksum(settlement))
            current = None
            if up_to_stage is not None and p["stage_id"] == up_to_stage:
                break
        elif t == EV_SESSION_ENDED:
            break

    return {
        "title": scenario.title,
        "session_id": session_row["id"],
        "replayed_stages": stages,
        "stopped_at_stage": up_to_stage,
        "settlement_checksums": checksums,
        "deterministic": all(
            s["settlement"]["branch_consistent"]
            for s in stages if s["settlement"]
        ),
    }


# ---------------- Markdown 导出 ----------------

def _opt_label(scenario, stage_id: str, role_id: str, choice_id: str | None
               ) -> str:
    if choice_id is None:
        return ""
    if choice_id == TIMEOUT_MARK:
        return "（逾期作废）"
    view = participant_stage_view(scenario, stage_id, role_id)
    for o in view["options"]:
        if o["id"] == choice_id:
            return f"{o['label']} (`{o['id']}`)"
    return f"`{choice_id}`"


def export_host_markdown(session_row: dict) -> str:
    scenario = _scenario_from_row(session_row)
    events = session_row["events"]
    events_md = "\n".join(
        f"| {e['version']} | {_ev_name(e['type'])} | "
        f"{e['at']:.3f} | "
        f"{json.dumps(e['payload'], ensure_ascii=False)} |"
        for e in events
    )

    lines = [
        f"# 应急演练全量记录（主持人）：{scenario.title}",
        "",
        f"- 演练 ID：`{session_row['id']}`",
        f"- 最终状态：{_status_cn(session_row['status'])}",
        "",
        "## 角色",
        "",
        "| 角色 | 名称 | 邀请码 | 说明 |",
        "|---|---|---|---|",
    ]
    for r in scenario.roles.values():
        lines.append(f"| `{r.id}` | {r.name} | `{r.invite_code}` | "
                     f"{r.description} |")

    settled = [e for e in events if e["type"] == EV_STAGE_SETTLED]
    joined = {e["payload"]["participant_id"]: e["payload"]
              for e in events if e["type"] == EV_PARTICIPANT_JOINED}

    lines += ["", "## 阶段回放", ""]
    idx = 0
    for e in events:
        if e["type"] != EV_STAGE_STARTED:
            continue
        idx += 1
        sid = e["payload"]["stage_id"]
        stage = scenario.stages[sid]
        lines += [f"### {idx}. {stage.name} (`{sid}`)", ""]
        lines += ["**各角色可见信息（全量）**", ""]
        for rid, role in scenario.roles.items():
            view = participant_stage_view(scenario, sid, rid)
            opts = "、".join(f"{o['label']}(`{o['id']}`)"
                             for o in view["options"]) or "（无选项）"
            lines += [
                f"- **{role.name}**（`{rid}`）",
                f"  - 简报：{view['brief'] or '—'}",
                f"  - 私有信息：{view['content'] or '—'}",
                f"  - 可选：{opts}",
            ]
        se = settled[idx - 1] if idx - 1 < len(settled) else None
        # 注意 settled 顺序与 stage_started 顺序一致
        se = next((x for x in settled
                   if x["payload"]["stage_id"] == sid), None)
        lines += ["", "**结算**", ""]
        if se:
            sp = se["payload"]
            lines.append("| 角色 | 参与者 | 最终选择 |")
            lines.append("|---|---|---|")
            pid_by_role = {v["role_id"]: k for k, v in joined.items()}
            for rid, role in scenario.roles.items():
                name = joined.get(pid_by_role.get(rid, ""), {}).get("name", "—")
                lines.append(
                    f"| {role.name} | {name} | "
                    f"{_opt_label(scenario, sid, rid, sp['choices'].get(rid)) or '（未提交/超时）'} |"
                )
            if sp.get("timed_out"):
                tn = "、".join(scenario.roles[r].name for r in sp["timed_out"])
                lines.append("")
                lines.append(f"超时未提交：{tn}")
            if sp.get("late_choices"):
                ln = "、".join(
                    f"{scenario.roles[r].name}→{_opt_label(scenario, sid, r, c)}"
                    for r, c in sp["late_choices"].items())
                lines.append(f"逾期提交（作废）：{ln}")
            lines.append("")
            if sp.get("branch"):
                matched = "；".join(
                    f"{scenario.roles[m['role']].name}="
                    f"{_opt_label(scenario, sid, m['role'], m['choice'])}"
                    for m in sp["branch"].get("matched", []))
                nxt = sp.get("next_stage")
                nxt_name = (scenario.stages[nxt].name
                            if nxt and nxt in scenario.stages else "结束演练")
                lines.append(f"分支命中：{matched or '（默认）'} → **{nxt_name}**")
            else:
                lines.append("无分支命中，演练结束。")
        else:
            lines.append("（该阶段尚未结算）")
        lines.append("")

    lines += [
        "## 事件日志",
        "",
        "| 版本 | 事件 | 时间戳 | 载荷 |",
        "|---|---|---|---|",
        events_md,
        "",
        "> 本文件包含主持人全量信息，请勿直接分发给参与者。",
        "",
    ]
    return "\n".join(lines)


def export_participant_markdown(session_row: dict, participant_row: dict) -> str:
    scenario = _scenario_from_row(session_row)
    rid = participant_row["role_id"]
    role = scenario.roles[rid]
    events = session_row["events"]

    lines = [
        f"# 应急演练个人记录：{scenario.title}",
        "",
        f"- 演练 ID：`{session_row['id']}`",
        f"- 我的角色：**{role.name}**（{participant_row.get('name', '')}）",
        f"- 最终状态：{_status_cn(session_row['status'])}",
        "",
        "> 本记录仅包含你所在角色可见的信息与你本人的选择。",
        "",
    ]
    idx = 0
    for e in events:
        if e["type"] != EV_STAGE_STARTED:
            continue
        idx += 1
        sid = e["payload"]["stage_id"]
        view = participant_stage_view(scenario, sid, rid)
        se = next((x for x in events
                   if x["type"] == EV_STAGE_SETTLED
                   and x["payload"]["stage_id"] == sid), None)
        lines += [f"## {idx}. {view['name']}", ""]
        if view["brief"]:
            lines += [f"**简报**：{view['brief']}", ""]
        if view["content"]:
            lines += [f"**角色信息**：{view['content']}", ""]
        opts = "、".join(f"{o['label']}(`{o['id']}`)" for o in view["options"])
        lines += [f"可选行动：{opts or '（无）'}", ""]
        mine = None
        late = False
        if se:
            mine = se["payload"]["choices"].get(rid)
            late = rid in se["payload"].get("late_choices", {})
        lines.append(
            f"**我的决定**：{_opt_label(scenario, sid, rid, mine) or '（未提交）'}"
            + ("（逾期，判定超时）" if late or mine == TIMEOUT_MARK else "")
        )
        lines.append("")
    return "\n".join(lines)


def _ev_name(t: str) -> str:
    return {
        "session_created": "创建演练",
        "participant_joined": "参与者加入",
        "stage_started": "阶段开始",
        "choice_submitted": "提交选择",
        "stage_settled": "阶段结算",
        "session_ended": "演练结束",
    }.get(t, t)


def _status_cn(s: str) -> str:
    return {"pending": "待开始", "running": "进行中", "ended": "已结束"}.get(s, s)
