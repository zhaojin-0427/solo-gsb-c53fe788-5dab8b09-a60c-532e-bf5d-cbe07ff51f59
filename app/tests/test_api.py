"""HTTP + WebSocket 端到端冒烟测试。

    python -m app.tests.test_api

使用 Starlette TestClient（线程内跑 ASGI），覆盖：
创建 → 三人加入 → WS 实时收到推送 → 开始 → 提交 → 自动推进 →
断线重连增量同步 → 主持人/个人导出鉴权。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time

from starlette.testclient import TestClient

EXAMPLE = os.path.join(os.path.dirname(__file__), "..", "scenarios",
                       "example.yaml")

FAILURES = []


def check(name, cond, detail=""):
    print(f"[{'PASS' if cond else 'FAIL'}] {name}"
          + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def recv_with_timeout(ws, timeout: float):
    """单次带超时的 WS 接收；超时返回 None。"""
    import queue
    import threading
    q: queue.Queue = queue.Queue()

    def _recv():
        try:
            q.put(ws.receive_json())
        except Exception as exc:  # noqa: BLE001
            q.put(exc)

    t = threading.Thread(target=_recv, daemon=True)
    t.start()
    try:
        item = q.get(timeout=timeout)
    except queue.Empty:
        return None
    return item if isinstance(item, dict) else None


def expect_changed(ws, timeout: float = 1.0, label: str = ""):
    msg = recv_with_timeout(ws, timeout)
    return msg is not None and msg.get("type") in ("changed", "events")


def main():
    db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    db.close()
    os.environ["DRILL_DB_PATH"] = db.name

    # 延迟导入以吃到环境变量（lifespan 启动时才创建 engine）
    import app.main as main_mod
    main_mod.DB_PATH = db.name

    with TestClient(main_mod.app) as client:
        # 健康检查
        r = client.get("/api/health")
        check("health 200", r.status_code == 200)

        # 创建
        yaml_text = open(EXAMPLE, encoding="utf-8").read()
        r = client.post("/api/sessions", json={"yaml": yaml_text})
        check("创建演练 201", r.status_code == 201, r.text[:200])
        created = r.json()
        sid, htoken = created["session_id"], created["host_token"]

        # 非法 YAML
        r = client.post("/api/sessions",
                        json={"yaml": "roles: nope", "use_example": False})
        check("非法剧本 422", r.status_code == 422, r.text[:120])

        # 主持人 WS 先连上，等待 join/start 推送
        with client.websocket_connect(
                f"/ws/sessions/{sid}?kind=host&token={htoken}") as ws_host:
            ws_host.receive_json()  # hello

            codes = {"commander": "CMD-7421", "comms": "COM-3180",
                     "medic": "MED-5562"}
            people = {}
            for rid, code in codes.items():
                r = client.post(f"/api/sessions/{sid}/join",
                                json={"invite_code": code, "name": rid})
                check(f"加入 {rid}", r.status_code == 200, r.text[:200])
                people[rid] = r.json()
                note = ws_host.receive_json()
                check(f"主持人收到 {rid} 加入推送", note["type"] == "changed")

            # 错误 token 的 WS 被拒
            try:
                with client.websocket_connect(
                        f"/ws/sessions/{sid}?kind=host&token=bad") as ws_bad:
                    ws_bad.receive_json()
                check("错误 WS 凭证被关闭", False)
            except Exception:
                check("错误 WS 凭证被关闭", True)

            # 开始
            r = client.post(f"/api/sessions/{sid}/start",
                            json={"token": htoken})
            check("开始演练", r.status_code == 200, r.text[:200])
            ws_host.receive_json()  # changed

            # 参与者快照
            p = people["commander"]
            r = client.get(
                f"/api/sessions/{sid}/participant-snapshot"
                f"?participant_id={p['participant_id']}&token={p['token']}")
            snap = r.json()
            check("参与者看到 alarm 阶段",
                  snap["current_stage"]["stage_id"] == "alarm")
            check("参与者快照不含其他角色结构",
                  "roles" not in snap and "events" not in snap)

            # 参与者 WS
            with client.websocket_connect(
                    f"/ws/sessions/{sid}"
                    f"?kind=participant&participant_id={p['participant_id']}"
                    f"&token={p['token']}") as ws_p:
                ws_p.receive_json()  # hello
                ws_p.send_json({"type": "sync", "after": 0})
                ev_msg = ws_p.receive_json()
                check("WS 增量同步返回事件", ev_msg["type"] == "events"
                      and len(ev_msg["events"]) >= 1)

                # 提交阶段：先验证“同阶段重复提交幂等”（在最后一人提交前，
                # 由第一人重发；阶段尚未结算），再提交最后一票触发自动推进。
                choices = {"commander": "evacuate",
                           "comms": "investigate", "medic": "shelter"}
                order = ["commander", "comms", "medic"]
                for rid2 in order:
                    pp = people[rid2]
                    rr = client.post(f"/api/sessions/{sid}/choices", json={
                        "participant_id": pp["participant_id"],
                        "token": pp["token"], "choice_id": choices[rid2],
                        "request_id": f"req-{rid2}",
                    })
                    check(f"提交 {rid2}", rr.status_code == 200, rr.text[:200])
                    check(f"提交响应 {rid2}", rr.json()["accepted"] is True,
                          rr.text[:200])
                    if rid2 == "commander":
                        # 阶段仍在进行：新 request_id、改选另一个合法选项
                        rr2 = client.post(f"/api/sessions/{sid}/choices", json={
                            "participant_id": pp["participant_id"],
                            "token": pp["token"], "choice_id": "investigate",
                            "request_id": "req-retry",
                        })
                        check("重复提交幂等",
                              rr2.status_code == 200
                              and rr2.json()["duplicate"] is True
                              and rr2.json()["choice_id"] == "evacuate",
                              rr2.text[:200])

                r = client.get(
                    f"/api/sessions/{sid}/host-snapshot?token={htoken}")
                hs = r.json()
                check("自动推进到 evacuation",
                      hs["current_stage"]["stage_id"] == "evacuation",
                      str(hs["current_stage"] and hs["current_stage"]["stage_id"]))
                check("历史中 alarm 已结算",
                      hs["stages"][0]["stage_id"] == "alarm"
                      and hs["stages"][0]["next_stage"] == "evacuation")

                # 排空主持人/参与者 WS 缓冲，确认至少收到过一条变更推送
                check("主持人收到实时推送",
                      expect_changed(ws_host, label="host"))
                check("参与者收到实时推送",
                      expect_changed(ws_p, label="participant"))

                # 伪造他人 token 拉快照
                rr = client.get(
                    f"/api/sessions/{sid}/participant-snapshot"
                    f"?participant_id={p['participant_id']}&token=wrong")
                check("伪造参与者 token 403", rr.status_code == 403)
                rr = client.get(
                    f"/api/sessions/{sid}/host-snapshot?token=wrong")
                check("伪造主持人 token 403", rr.status_code == 403)

                # 主持人导出
                rr = client.get(
                    f"/api/sessions/{sid}/export/host.md?token={htoken}")
                check("主持人导出 200 且含私有信息",
                      rr.status_code == 200 and "总经理已电话授权" in rr.text)
                rr = client.get(
                    f"/api/sessions/{sid}/export/me.md"
                    f"?participant_id={p['participant_id']}"
                    f"&token={p['token']}")
                check("个人导出 200 且不含他人私有信息",
                      rr.status_code == 200
                      and "总经理已电话授权" in rr.text  # 这是指挥官本人
                      and "急救包" not in rr.text)
                rr = client.get(
                    f"/api/sessions/{sid}/replay?token={htoken}")
                check("回放 JSON 200 且确定性一致",
                      rr.status_code == 200 and rr.json()["deterministic"]
                      is True, rr.text[:200])

                # 无 token 不能回放
                rr = client.get(f"/api/sessions/{sid}/replay")
                check("无凭证回放 403", rr.status_code == 403)

        # 重新连接主持人 WS（模拟断线重连）
        with client.websocket_connect(
                f"/ws/sessions/{sid}?kind=host&token={htoken}") as ws2:
            ws2.receive_json()  # hello
            ws2.send_json({"type": "sync", "after": 0})
            msg = ws2.receive_json()
            check("重连后可拉取全量事件",
                  msg["type"] == "events" and len(msg["events"]) > 3)

        # 主持人提前终止
        r = client.post(f"/api/sessions/{sid}/end", json={"token": htoken})
        check("终止演练", r.status_code == 200, r.text[:200])
        r = client.get(f"/api/sessions/{sid}/host-snapshot?token={htoken}")
        check("状态为 ended", r.json()["status"] == "ended")

    print()
    if FAILURES:
        print(f"{len(FAILURES)} 项失败: {FAILURES}")
        sys.exit(1)
    print("全部通过 ✓")


if __name__ == "__main__":
    main()
