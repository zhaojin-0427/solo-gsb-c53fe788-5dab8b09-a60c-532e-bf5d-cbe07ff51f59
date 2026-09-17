# 多人应急演练 Web 应用

主持人用 YAML 定义**阶段、按角色可见的信息、限时选项与条件分支**；参与者凭**角色邀请码**加入，只能看到本角色内容并在限时内提交选择。服务端以**单调递增版本号 + 事件溯源**原子结算每一轮，处理重复提交、断线重连与截止时刻的并发竞争，并通过 WebSocket 推送**按权限裁剪**的快照。结束后可基于事件日志**确定性回放**任一阶段，导出**个人记录**（不含隐藏信息）与**主持人全量记录**。

- 后端：FastAPI + SQLite（WAL）+ WebSocket，单进程异步
- 前端：原生 JavaScript（无构建步骤）
- 部署：Docker Compose，一条命令启动

---

## 一、快速开始（Docker Compose）

```bash
docker compose up -d --build
```

启动后访问：

| 入口 | 地址 |
|---|---|
| 首页（创建 / 加入 / 恢复访问） | http://localhost:8000/ |
| 健康检查 | http://localhost:8000/api/health |
| OpenAPI 文档 | http://localhost:8000/docs |

数据保存在命名卷 `drill-data`（容器内 `/data/drill.db`），容器重建后演练记录仍在。

停止 / 清理：

```bash
docker compose down            # 停止（保留数据）
docker compose down -v         # 停止并删除演练数据
```

### 不使用 Docker（本地运行）

```bash
pip install -r requirements.txt
DRILL_DB_PATH=./drill.db uvicorn app.main:app --host 0.0.0.0 --port 8000
```

> 必须以 **单 worker** 运行（默认即是）。每会话互斥由进程内异步锁保证，多 worker 部署请改用共享锁/队列。

### 环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DRILL_DB_PATH` | `/data/drill.db` | SQLite 数据库路径 |
| `DRILL_SECRET` | `change-me-in-production` | 预留的部署密钥（当前版本主持人凭证为每场演练独立随机生成） |

---

## 二、使用流程

### 主持人

1. 打开首页，点击**「用示例剧本快速创建」**（或粘贴自定义 YAML 创建）。
2. 浏览器自动跳转到主持人控制台，链接中包含一次性的主持人 `token`，**请妥善保存，不要发给参与者**（刷新页面也会用 localStorage 恢复）。
3. 把加入链接 `http://<主机>:8000/join.html?session=s_xxxxx` 发给参与者。
4. 等人到齐后点击**「开始 / 进入下一阶段」**开始第一阶段。
5. 控制台实时显示倒计时、每人已提交的选择；可以：
   - **立即结算本阶段**：提前结算（未提交者记超时）；
   - **终止演练**：随时结束。
6. 全员按时提交后阶段**立即自动结算**；到截止时刻仍未齐则**定时器自动结算**。条件分支决定下一阶段，无下一阶段则演练结束。
7. 结束后在控制台底部：
   - **导出主持人全量记录 (Markdown)**：含所有角色的隐藏信息、邀请码、每人选择、分支命中、完整事件日志；
   - **查看确定性回放 (JSON)**：基于事件日志重算每个阶段的分支决策，并校验与落库结果一致。

### 参与者

1. 打开加入链接，输入主持人分发的**角色邀请码**和姓名加入。
2. 等待开始；阶段开始后页面显示：公共简报、**仅本角色可见**的信息、本角色可选项和倒计时。
3. 点击一个选项即提交；**提交后不可更改**，重复点击/断线重发都会被服务端幂等忽略（以第一次为准）。
4. 到点未提交记为**超时**；在截止点之后才到达的提交记为**逾期作废**，不参与分支。
5. WebSocket 断线会自动指数退避重连，页面凭证保存在 localStorage，刷新即可回到现场。
6. 演练结束后可**导出我的记录 (Markdown)**，内容只包含自己角色可见的信息和自己的选择。

### 内置示例剧本

`app/scenarios/example.yaml` 是一个「办公楼火灾应急演练」，包含三个角色（指挥官 / 通信联络员 / 医疗救护员，邀请码分别为 `CMD-7421`、`COM-3180`、`MED-5562`）、6 个阶段、角色私有信息、角色专属选项和多组条件分支（成功路径与问题路径）。也可通过 `GET /api/example.yaml` 获取。

---

## 三、剧本 YAML 格式

```yaml
title: 演练标题
description: 简介（加入页公开展示）
default_duration: 90            # 每阶段默认限时（秒）
first_stage: alarm              # 起始阶段 id

roles:
  - id: commander               # 角色 id（分支条件中引用）
    name: 现场指挥官
    description: 职责说明（公开）
    invite_code: CMD-7421       # 加入凭据，必须互不相同

stages:
  - id: alarm
    name: 警报初起
    duration: 60                # 覆盖 default_duration（秒）
    brief:                      # 阶段简报
      all: 所有人可见的文本       # all = 对所有角色可见
    content:                    # 角色私有信息（仅本角色可见）
      commander: 只有指挥官能看到……
      medic: 只有医疗员能看到……
    options:                    # 限时选项
      all:                      # all = 所有角色相同的选项
        - id: evacuate
          label: 立即下令全楼疏散
        - id: investigate
          label: 先派义务消防队核实火情
    branches:                   # 条件分支：按顺序匹配，命中第一条即跳转
      - when:
          - role: commander
            in: [evacuate]      # 也支持 is: evacuate
        next: evacuation
      - when:
          - role: commander
            in: [investigate, shelter]
        next: spread
    default_next: spread        # 无分支命中时的下一阶段；省略 = 演练结束
```

### 角色专属选项

`options` 下可以用角色 id 代替 `all`，不同角色看到不同选项：

```yaml
    options:
      commander:
        - { id: guide_west, label: 抽调整组人去西楼梯引导 }
      comms:
        - { id: north_gate, label: 到北门引导消防车 }
      medic:
        - { id: triage, label: 检伤分类 }
```

### 多角色组合分支（AND 语义）

`when` 中列出的条件必须**全部满足**才命中；各条件之间是「与」，分支之间按声明顺序取第一条：

```yaml
    branches:
      - when:
          - role: commander
            in: [deny_return]
          - role: comms
            in: [north_gate]
        next: muster
    default_next: casualty
```

校验规则（创建时返回 422 并给出中文原因）：角色/阶段 id 不重复、邀请码不重复、分支与 `default_next` 目标必须存在、角色引用必须已定义、每角色每阶段至少一个选项、时长为正整数等。

---

## 四、关键设计：并发一致性如何保证

- **事件溯源 + 单调版本号**：所有状态变化（创建、加入、阶段开始、提交、结算、结束）都只向 `events` 表追加一行，`(session_id, version)` 唯一且递增；当前状态随时可由事件重放得到。
- **原子结算**：每个演练一把异步互斥锁；写事务使用 `BEGIN IMMEDIATE`（SQLite WAL + `busy_timeout`）。「全员到齐立即结算」与「定时器到点结算」、「最后一刻的提交」全部串行化。提交与其触发的结算在**同一把锁、同一连接**内完成，不存在读不到自己写入的窗口。
- **重复提交幂等**：`submissions` 表对 `(session_id, participant_id, stage_id)` 有唯一约束；同一阶段再次提交直接返回首次结果（含首次是否按时），客户端可用 `request_id` 做请求级去重。
- **截止时刻竞争**：一律以**服务端时钟**判定。提交在截止点之后到达记 `late=1`（事件审计可见，但不计有效票、不参与分支）；若结算先完成，则该阶段已关闭，提交被拒绝。两种交错结果都确定、不重复计票。
- **断线重连**：WebSocket 只推送轻量 `changed` 通知，客户端随后凭自己的凭证重新拉取裁剪快照；也可发 `{"type":"sync","after":N}` 只拉取版本号大于 N 的事件。前端自动指数退避重连。
- **权限裁剪**：主持人快照包含全量剧本与所有选择；参与者快照只包含自己角色的 `brief/content/options`、自己的选择和剩余时间，已结算阶段也不会泄露他人的私有内容与投票。
- **定时器崩溃恢复**：服务重启时扫描所有仍在运行的演练，按数据库中的 `deadline` 重新挂自动结算（到点即结算）。
- **确定性回放**：`GET /api/sessions/{id}/replay?up_to_stage=<stage_id>` 只依赖事件日志，对每次结算**重新求值**分支，返回重算结果与落库结果的一致性校验（`branch_consistent`、`deterministic`），可停在任意阶段。

---

## 五、HTTP / WebSocket API 摘要

主持人凭证通过 `Authorization: Bearer <host_token>` 或 `?token=` 传递；参与者通过 `participant_id` + 参与者 `token`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/sessions` | 创建演练（`yaml` 或 `use_example`）→ 返回 session_id、host_token |
| GET | `/api/sessions/{id}` | 演练公开元信息（标题、角色、状态） |
| POST | `/api/sessions/{id}/join` | 邀请码 + 姓名加入 |
| POST | `/api/sessions/{id}/start` | 主持人开始/进入下一阶段 |
| POST | `/api/sessions/{id}/settle` | 主持人立即结算当前阶段 |
| POST | `/api/sessions/{id}/end` | 主持人终止演练 |
| POST | `/api/sessions/{id}/choices` | 参与者提交选择（幂等） |
| GET | `/api/sessions/{id}/host-snapshot` | 主持人全量快照 |
| GET | `/api/sessions/{id}/participant-snapshot` | 参与者裁剪快照 |
| GET | `/api/sessions/{id}/replay` | 主持人：确定性回放（可带 `up_to_stage`） |
| GET | `/api/sessions/{id}/export/host.md` | 主持人全量 Markdown |
| GET | `/api/sessions/{id}/export/me.md` | 参与者个人 Markdown |
| WS | `/ws/sessions/{id}?kind=host&token=…` | 主持人推送通道 |
| WS | `/ws/sessions/{id}?kind=participant&participant_id=…&token=…` | 参与者推送通道 |

WS 消息：服务端推 `{"type":"hello"}` 与 `{"type":"changed"}`；客户端可发 `{"type":"ping"}`（回 `pong`）和 `{"type":"sync","after":N}`（回增量 `events`）。

---

## 六、测试

依赖装好后（容器内已具备）运行：

```bash
# 事件溯源引擎：加入/幂等/分支/截止竞争/回放/导出隔离，40+ 项断言
python -m app.tests.test_flow

# HTTP + WebSocket 端到端（Starlette TestClient）
python -m app.tests.test_api
```

用 Docker 运行：

```bash
docker compose build
docker compose run --rm drill python -m app.tests.test_flow
docker compose run --rm drill python -m app.tests.test_api
```

---

## 七、目录结构

```
.
├── docker-compose.yml
├── Dockerfile
├── requirements.txt
├── README.md
└── app
    ├── main.py            # FastAPI：REST、WebSocket、截止定时器、异常映射
    ├── core.py            # 无第三方依赖：事件常量、分支求值、事件重放
    ├── scenario.py        # YAML 解析/校验、按角色裁剪视图
    ├── engine.py          # SQLite 事件溯源引擎（锁、原子结算、快照）
    ├── recorder.py        # 确定性回放、主持人/参与者 Markdown 导出
    ├── hub.py             # WebSocket 连接注册与广播
    ├── scenarios/example.yaml
    ├── static/            # 前端（index/join/host/participant + common.js）
    └── tests/             # test_flow.py / test_api.py
```
