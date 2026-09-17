"""核心流程测试（需安装 requirements.txt 中的依赖）。

运行：
    docker compose run --rm drill python -m app.tests.test_flow
或本地：
    pip install -r requirements.txt && python -m app.tests.test_flow

覆盖：
1. 创建/加入（邀请码错误、角色唯一）
2. 开始阶段、提交、重复提交幂等
3. 全员提交自动结算 + 条件分支
4. 截止时刻并发竞争：临界提交与结算交错，结果确定无异常
5. 断线重连：基于版本号增量同步
6. 确定性回放（可指定阶段）与主持人/参与者导出的权限隔离
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time

from ..engine import Engine, AuthError, Conflict
from ..recorder import (
    export_host_markdown,
    export_participant_markdown,
    replay,
)

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "scenarios",
                       "example.yaml")

FAILURES = []


def check(name: str, cond: bool, detail: str = ""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


async def main():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()

    async def notify(sid):
        pass

    eng = Engine(tmp.name, notify)
    await eng.init_db()
    yaml_text = open(EXAMPLE, encoding="utf-8").read()

    # 1) 创建 / 加入
    created = await eng.create_session(yaml_text)
    sid, htoken = created["session_id"], created["host_token"]
    check("创建演练", sid.startswith("s_"))

    try:
        await eng.join(sid, "WRONG", "甲")
        check("错误邀请码被拒", False)
    except AuthError:
        check("错误邀请码被拒", True)

    codes = {"commander": "CMD-7421", "comms": "COM-3180",
             "medic": "MED-5562"}
    people = {}
    for rid, code in codes.items():
        people[rid] = await eng.join(sid, code, f"玩家-{rid}")
    try:
        await eng.join(sid, codes["commander"], "另一个指挥")
        check("同角色重复加入被拒", False)
    except Conflict:
        check("同角色重复加入被拒", True)

    try:
        await eng.submit_choice(sid, people["commander"]["participant_id"],
                                people["commander"]["token"], "evacuate")
        check("未开始阶段提交被拒", False)
    except Conflict:
        check("未开始阶段提交被拒", True)

    # 2) 开始第一阶段 alarm
    await eng.start(sid, htoken)
    snap = await eng.snapshot_host(sid, htoken)
    check("第一阶段为 alarm",
          snap["current_stage"]["stage_id"] == "alarm")
    v_start = snap["version"]

    # 快照裁剪
    check("指挥官视角含医疗员私有信息",
          "急救包" in snap["current_stage"]["roles"]["medic"]["content"])
    p_cmd = await eng.snapshot_participant(
        sid, people["commander"]["participant_id"],
        people["commander"]["token"])
    p_med = await eng.snapshot_participant(
        sid, people["medic"]["participant_id"], people["medic"]["token"])
    check("角色看到自己的私有信息", "义务消防队" in p_cmd["current_stage"]["content"])
    check("指挥官参与者看不到医疗员私有信息",
          "急救包" not in p_cmd["current_stage"]["content"])
    check("医疗员看不到通信员私有信息",
          "119" not in p_med["current_stage"]["content"])

    # 3) 提交 + 幂等
    r1 = await eng.submit_choice(
        sid, people["commander"]["participant_id"],
        people["commander"]["token"], "evacuate")
    check("首次提交 accepted", r1["accepted"] and not r1["duplicate"])
    r2 = await eng.submit_choice(
        sid, people["commander"]["participant_id"],
        people["commander"]["token"], "investigate")
    check("重复提交幂等且返回原选择",
          r2["duplicate"] and r2["choice_id"] == "evacuate")
    try:
        await eng.submit_choice(
            sid, people["comms"]["participant_id"],
            people["comms"]["token"], "not_exist")
        check("非法选项被拒", False)
    except Conflict:
        check("非法选项被拒", True)

    await eng.submit_choice(
        sid, people["comms"]["participant_id"],
        people["comms"]["token"], "investigate")
    await eng.submit_choice(
        sid, people["medic"]["participant_id"],
        people["medic"]["token"], "shelter")

    snap = await eng.snapshot_host(sid, htoken)
    check("全员齐 -> 自动结算进入 evacuation",
          bool(snap["current_stage"])
          and snap["current_stage"]["stage_id"] == "evacuation",
          str(snap["current_stage"] and snap["current_stage"]["stage_id"]))
    check("alarm 分支记录为 evacuation",
          snap["stages"][0]["next_stage"] == "evacuation")
    check("版本号单调递增", snap["version"] > v_start)

    # evacuation：制造“好路径”分支（commander=deny_return & comms=north_gate）
    await eng.submit_choice(
        sid, people["commander"]["participant_id"],
        people["commander"]["token"], "deny_return")
    await eng.submit_choice(
        sid, people["comms"]["participant_id"],
        people["comms"]["token"], "north_gate")
    await eng.submit_choice(
        sid, people["medic"]["participant_id"],
        people["medic"]["token"], "treat_asthma")
    snap = await eng.snapshot_host(sid, htoken)
    check("evacuation 条件分支 -> muster",
          bool(snap["current_stage"])
          and snap["current_stage"]["stage_id"] == "muster",
          str(snap["current_stage"] and snap["current_stage"]["stage_id"]))
    check("命中分支信息落事件",
          snap["stages"][1]["next_stage"] == "muster")

    # 4) 截止时刻竞争 —— 两个确定性场景分别覆盖两种交错结果

    # 场景 A：结算先于临界提交 -> 第三人超时
    await eng.submit_choice(
        sid, people["commander"]["participant_id"],
        people["commander"]["token"], "verify_first")
    await eng.submit_choice(
        sid, people["comms"]["participant_id"],
        people["comms"]["token"], "call_missing")
    # 把截止点设为“刚刚到期但尚未自动结算”
    await eng._set_deadline_for_test(sid, time.time() + 0.1)

    async def a_late_submit():
        await asyncio.sleep(0.15)  # 一定在截止点之后到达
        return await eng.submit_choice(
            sid, people["medic"]["participant_id"],
            people["medic"]["token"], "triage")

    async def a_deadline_fire():
        await asyncio.sleep(0.1)
        return await eng.settle(sid, None, manual=False)

    race_a = await asyncio.gather(a_late_submit(), a_deadline_fire(),
                                  return_exceptions=True)
    check("场景A 竞争无未预期异常",
          all(not isinstance(x, Exception)
              or isinstance(x, Conflict) for x in race_a), repr(race_a))
    sub_a = race_a[0]
    # 两种确定结果之一：
    #  (1) 逾期窗口内先到 -> accepted=False 并记录逾期；
    #  (2) 结算先完成 -> 409（阶段已推进）。两者都安全、不重复计票。
    late_or_conflict = (
        (isinstance(sub_a, dict) and sub_a["accepted"] is False)
        or isinstance(sub_a, Conflict))
    check("场景A 临界提交要么逾期作废要么被拒", late_or_conflict, repr(sub_a))
    snap = await eng.snapshot_host(sid, htoken)
    muster = snap["stages"][2]
    check("场景A medic 未计有效票", "medic" not in muster["choices"])
    check("场景A -> debrief_good（分支只取决于前两人）",
          snap["current_stage"]
          and snap["current_stage"]["stage_id"] == "debrief_good")

    # 终局阶段：全员提交自动结算结束；与并发手动结算交错保持幂等
    await asyncio.gather(
        eng.submit_choice(sid, people["commander"]["participant_id"],
                          people["commander"]["token"], "done"),
        eng.submit_choice(sid, people["comms"]["participant_id"],
                          people["comms"]["token"], "done"),
        eng.settle(sid, htoken, manual=True),
        return_exceptions=True,
    )
    snap = await eng.snapshot_host(sid, htoken)
    if snap["status"] != "ended":
        await eng.submit_choice(
            sid, people["medic"]["participant_id"],
            people["medic"]["token"], "done")
    snap = await eng.snapshot_host(sid, htoken)
    check("终局结算后演练结束", snap["status"] == "ended", snap["status"])
    end_version = snap["version"]
    await eng.settle(sid, htoken, manual=True)
    snap2 = await eng.snapshot_host(sid, htoken)
    check("结束后重复结算幂等",
          snap2["status"] == "ended" and snap2["version"] == end_version)

    # 场景 B：临界提交先于到期结算 -> 全员有效
    sid_b = (await eng.create_session(yaml_text))["session_id"]
    hb = (await eng.get_session(sid_b))["host_token"]
    pb = {}
    for rid2, code2 in codes.items():
        pb[rid2] = await eng.join(sid_b, code2, f"B-{rid2}")
    await eng.start(sid_b, hb)
    await eng.submit_choice(sid_b, pb["commander"]["participant_id"],
                            pb["commander"]["token"], "evacuate")
    await eng.submit_choice(sid_b, pb["comms"]["participant_id"],
                            pb["comms"]["token"], "investigate")
    # 截止 0.2s；最后一人 0.05s 后提交（先到期），到期结算在 0.2s 后
    await eng._set_deadline_for_test(sid_b, time.time() + 0.2)

    async def b_last_submit():
        await asyncio.sleep(0.05)
        return await eng.submit_choice(
            sid_b, pb["medic"]["participant_id"],
            pb["medic"]["token"], "shelter")

    async def b_deadline():
        await asyncio.sleep(0.2)
        return await eng.settle(sid_b, None, manual=False)

    race_b = await asyncio.gather(b_last_submit(), b_deadline(),
                                  return_exceptions=True)
    check("场景B 竞争无异常",
          all(not isinstance(x, Exception) for x in race_b), repr(race_b))
    check("场景B 临界提交被接受", race_b[0]["accepted"] is True)
    snap_b = await eng.snapshot_host(sid_b, hb)
    check("场景B alarm 全员有效无超时",
          set(snap_b["stages"][0]["choices"]) == set(codes)
          and not snap_b["stages"][0]["timed_out"])
    check("场景B 进入 evacuation",
          snap_b["current_stage"]["stage_id"] == "evacuation")

    # 5) 断线重连：增量事件
    all_events = await eng.events(sid, 0)
    pivot = all_events[2]
    tail = await eng.events(sid, pivot["version"])
    check("增量事件同步（after=version 语义）",
          len(tail) == len(all_events) - 3 and tail[0]["version"] == pivot["version"] + 1)

    # 6) 确定性回放
    row = await eng._get_session_row_ro(sid)
    rep = replay(row)
    settled_stages = [s for s in rep["replayed_stages"] if s["settlement"]]
    check("回放覆盖全部已结算阶段", len(settled_stages) >= 3,
          f"{len(settled_stages)}")
    check("回放分支全部确定性一致", rep["deterministic"],
          detail=repr([(s["stage_id"], s["settlement"]["recorded_next_stage"],
                        s["settlement"]["recomputed_next_stage"])
                       for s in settled_stages
                       if not s["settlement"]["branch_consistent"]]))
    rep1 = replay(row, up_to_stage="alarm")
    check("可只回放到指定阶段",
          rep1["stopped_at_stage"] == "alarm"
          and len(rep1["replayed_stages"]) == 1)

    # 7) 导出权限隔离
    host_md = export_host_markdown(row)
    check("主持人记录含全部邀请码",
          all(c in host_md for c in codes.values()))
    check("主持人记录含所有角色私有信息",
          "急救包" in host_md and "119" in host_md
          and "义务消防队" in host_md)

    my_md = export_participant_markdown(row, people["commander"])
    check("个人记录不含他人邀请码",
          "MED-5562" not in my_md and "COM-3180" not in my_md)
    check("个人记录不含他人私有信息",
          "急救包" not in my_md and "119 终于" not in my_md)
    check("个人记录含本人私有信息", "义务消防队" in my_md)
    check("个人记录记录本人选择", "立即下令全楼疏散" in my_md)

    my_md_medic = export_participant_markdown(row, people["medic"])
    check("医疗员记录含自己私有信息", "急救包" in my_md_medic)
    check("医疗员记录不含指挥官私有信息",
          "总经理已电话授权" not in my_md_medic)

    # 凭证校验
    try:
        await eng.snapshot_host(sid, "bad-token")
        check("错误主持人凭证被拒", False)
    except AuthError:
        check("错误主持人凭证被拒", True)
    try:
        await eng.snapshot_participant(
            sid, people["medic"]["participant_id"], "bad-token")
        check("错误参与者凭证被拒", False)
    except AuthError:
        check("错误参与者凭证被拒", True)

    # 结束后不能再加入/提交
    try:
        await eng.join(sid, "CMD-7421", "晚到者")
        check("结束后不能加入", False)
    except Conflict:
        check("结束后不能加入", True)

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项失败: {FAILURES}")
        sys.exit(1)
    print("全部通过 ✓")


if __name__ == "__main__":
    asyncio.run(main())
