"""剧本（scenario）解析与校验。

主持人用 YAML 定义：
- title / description / default_duration
- roles：角色 id、名称、邀请码
- stages：阶段 id、名称、时长、按角色裁剪的可见内容、限时选项、条件分支
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import yaml


class ScenarioError(ValueError):
    """剧本定义非法。"""


@dataclass
class Option:
    id: str
    label: str


@dataclass
class Branch:
    # 形如 [{"role": "commander", "in": ["evacuate"]}]
    when: list[dict[str, Any]]
    next_stage: str


@dataclass
class Stage:
    id: str
    name: str
    duration: int
    brief: dict[str, str] = field(default_factory=dict)
    # role_id -> [Option]
    options: dict[str, list[Option]] = field(default_factory=dict)
    # 各角色私有内容（brief 之外的长文本，按角色裁剪）
    content: dict[str, str] = field(default_factory=dict)
    branches: list[Branch] = field(default_factory=list)
    default_next: str | None = None


@dataclass
class Role:
    id: str
    name: str
    invite_code: str
    description: str = ""


@dataclass
class Scenario:
    title: str
    description: str
    roles: dict[str, Role]
    stages: dict[str, Stage]
    first_stage: str

    @property
    def role_ids(self) -> list[str]:
        return list(self.roles.keys())

    def role_by_code(self, code: str) -> Role | None:
        code = (code or "").strip()
        for role in self.roles.values():
            if role.invite_code == code:
                return role
        return None


def _require(mapping: dict, key: str, ctx: str) -> Any:
    if key not in mapping:
        raise ScenarioError(f"{ctx} 缺少必填字段: {key}")
    return mapping[key]


def parse_scenario(text: str) -> Scenario:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ScenarioError(f"YAML 解析失败: {exc}") from exc
    if not isinstance(raw, dict):
        raise ScenarioError("剧本顶层必须是映射 (mapping)")

    title = str(_require(raw, "title", "剧本"))
    description = str(raw.get("description", ""))
    default_duration = int(raw.get("default_duration", 60))
    if default_duration <= 0:
        raise ScenarioError("default_duration 必须为正整数（秒）")

    roles_raw = _require(raw, "roles", "剧本")
    if not isinstance(roles_raw, list) or not roles_raw:
        raise ScenarioError("roles 必须是非空列表")

    roles: dict[str, Role] = {}
    codes: set[str] = set()
    for item in roles_raw:
        if not isinstance(item, dict):
            raise ScenarioError("roles 中的每一项必须是映射")
        rid = str(_require(item, "id", "role"))
        name = str(_require(item, "name", f"role {rid}"))
        code = str(_require(item, "invite_code", f"role {rid}")).strip()
        if not code:
            raise ScenarioError(f"role {rid} 的 invite_code 不能为空")
        if rid in roles:
            raise ScenarioError(f"role id 重复: {rid}")
        if code in codes:
            raise ScenarioError(f"invite_code 重复: {code}")
        codes.add(code)
        roles[rid] = Role(
            id=rid,
            name=name,
            invite_code=code,
            description=str(item.get("description", "")),
        )

    stages_raw = _require(raw, "stages", "剧本")
    if not isinstance(stages_raw, list) or not stages_raw:
        raise ScenarioError("stages 必须是非空列表")

    stages: dict[str, Stage] = {}
    for item in stages_raw:
        if not isinstance(item, dict):
            raise ScenarioError("stages 中的每一项必须是映射")
        sid = str(_require(item, "id", "stage"))
        if sid in stages:
            raise ScenarioError(f"stage id 重复: {sid}")
        duration = int(item.get("duration", default_duration))
        if duration <= 0:
            raise ScenarioError(f"stage {sid} 的 duration 必须为正整数（秒）")

        def _role_text_map(field_name: str) -> dict[str, str]:
            value = item.get(field_name, {}) or {}
            if not isinstance(value, dict):
                raise ScenarioError(f"stage {sid} 的 {field_name} 必须是映射")
            out: dict[str, str] = {}
            for role_id, text in value.items():
                if role_id != "all" and role_id not in roles:
                    raise ScenarioError(
                        f"stage {sid}.{field_name} 引用了未定义的角色: {role_id}"
                    )
                out[role_id] = "" if text is None else str(text)
            return out

        brief = _role_text_map("brief")
        content = _role_text_map("content")

        # options: role_id -> [{id,label}] 或 all -> [...]
        options_raw = item.get("options", {}) or {}
        if not isinstance(options_raw, dict):
            raise ScenarioError(f"stage {sid} 的 options 必须是映射")
        options: dict[str, list[Option]] = {}
        for role_id, opt_list in options_raw.items():
            if role_id != "all" and role_id not in roles:
                raise ScenarioError(
                    f"stage {sid}.options 引用了未定义的角色: {role_id}"
                )
            if not isinstance(opt_list, list) or not opt_list:
                raise ScenarioError(
                    f"stage {sid}.options.{role_id} 必须是非空列表"
                )
            parsed: list[Option] = []
            seen: set[str] = set()
            for opt in opt_list:
                if not isinstance(opt, dict):
                    raise ScenarioError(
                        f"stage {sid}.options.{role_id} 的选项必须是映射"
                    )
                oid = str(_require(opt, "id", f"stage {sid} option"))
                if oid in seen:
                    raise ScenarioError(
                        f"stage {sid}.options.{role_id} 选项 id 重复: {oid}"
                    )
                seen.add(oid)
                parsed.append(
                    Option(
                        id=oid,
                        label=str(_require(opt, "label", f"stage {sid} option {oid}")),
                    )
                )
            options[role_id] = parsed

        branches: list[Branch] = []
        for br in item.get("branches", []) or []:
            when = _require(br, "when", f"stage {sid} branch")
            nxt = str(_require(br, "next", f"stage {sid} branch"))
            if not isinstance(when, list) or not when:
                raise ScenarioError(f"stage {sid} branch.when 必须是非空列表")
            for cond in when:
                if not isinstance(cond, dict) or "role" not in cond:
                    raise ScenarioError(
                        f"stage {sid} 的分支条件必须是包含 role 的映射"
                    )
                rid = str(cond["role"])
                if rid not in roles:
                    raise ScenarioError(
                        f"stage {sid} 分支条件引用了未定义角色: {rid}"
                    )
                allowed = cond.get("in")
                if allowed is None and "is" in cond:
                    allowed = [cond["is"]]
                if not isinstance(allowed, list) or not allowed:
                    raise ScenarioError(
                        f"stage {sid} 分支条件必须包含非空的 in/is"
                    )
            branches.append(Branch(when=when, next_stage=nxt))

        default_next = item.get("default_next")
        if default_next is not None:
            default_next = str(default_next)

        stages[sid] = Stage(
            id=sid,
            name=str(_require(item, "name", f"stage {sid}")),
            duration=duration,
            brief=brief,
            options=options,
            content=content,
            branches=branches,
            default_next=default_next,
        )

    first_stage = str(_require(raw, "first_stage", "剧本"))
    if first_stage not in stages:
        raise ScenarioError(f"first_stage 未定义: {first_stage}")

    # 分支目标可达性 + 环检测（分支允许形成循环，循环本身在演练中可重复走；
    # 这里只做目标存在性校验，并报告无法抵达终局的剧本供主持人注意）
    for stage in stages.values():
        if stage.default_next is not None and stage.default_next not in stages:
            raise ScenarioError(
                f"stage {stage.id} 的 default_next 未定义: {stage.default_next}"
            )
        for br in stage.branches:
            if br.next_stage not in stages:
                raise ScenarioError(
                    f"stage {stage.id} 的分支目标未定义: {br.next_stage}"
                )

    return Scenario(
        title=title,
        description=description,
        roles=roles,
        stages=stages,
        first_stage=first_stage,
    )


# ---------- 权限裁剪 ----------

def _role_text(role_map: dict[str, str], role_id: str) -> str:
    if role_id in role_map:
        return role_map[role_id]
    return role_map.get("all", "")


def _role_options(stage: Stage, role_id: str) -> list[dict]:
    opts = stage.options.get(role_id) or stage.options.get("all") or []
    # 选项级限时不外泄给参与者（选项 duration 属于主持人配置，
    # 参与者只需知道剩余时间），这里仅下发 id/label。
    return [{"id": o.id, "label": o.label} for o in opts]


def participant_stage_view(scenario: Scenario, stage_id: str, role_id: str) -> dict:
    """参与者在某阶段可见的全部内容（按角色裁剪）。"""
    stage = scenario.stages[stage_id]
    return {
        "id": stage.id,
        "name": stage.name,
        "brief": _role_text(stage.brief, role_id),
        "content": _role_text(stage.content, role_id),
        "options": _role_options(stage, role_id),
    }


def scenario_public_meta(scenario: Scenario) -> dict:
    """可公开的剧本元信息（加入页展示，不含隐藏内容）。"""
    return {
        "title": scenario.title,
        "description": scenario.description,
        "roles": [
            {"id": r.id, "name": r.name, "description": r.description}
            for r in scenario.roles.values()
        ],
    }
