# 多人应急演练 Web 应用

主持人用 **YAML** 定义演练的阶段、各角色可见信息、限时选项与条件分支；参与者凭
**角色邀请码** 加入，只能看到本角色的内容并在限时内提交选择。后端基于
**FastAPI + SQLite + WebSocket**，前端为无构建步骤的原生 JavaScript。

- 服务端以 **单调递增版本号** 原子结算每一轮（每次状态变更是一个数据库版本）
- 正确处理 **重复提交**（幂等）、**断线重连**（WebSocket 自动重连 + 快照重同步）和
  **截止时刻的并发竞争**（拒绝与结算在同一个 SQLite 立即事务内完成）
- 每次状态变化向客户端推送 **按权限裁剪的快照**（角色视角 / 主持人全量视角）
- 结束后可依据不可变事件日志 **确定性回放任一版本**，并导出
  **个人记录（不含任何他人隐藏信息）** 与 **主持人全量记录**

---

## 目录结构

```
.
├── backend/
│   ├── app/
│   │   ├── main.py          # FastAPI：HTTP API、WebSocket、定时结算、静态托管
│   │   ├── engine.py        # 纯事件溯源 reducer + 原子提交（版本号/结算/幂等）
│   │   ├── definitions.py   # YAML 剧本解析与校验
│   │   ├── db.py            # SQLite（WAL、立即事务、事件日志表）
│   │   ├── snapshots.py     # 按角色裁剪的快照构造
│   │   ├── records.py       # 确定性回放 + 个人/全量记录导出（Markdown/JSON）
│   │   ├── hub.py           # WebSocket 连接管理（在线状态、背压安全）
│   │   └── config.py        # 路径与密钥（环境变量）
│   └── requirements.txt
├── frontend/                # 原生 HTML/CSS/JS（由后端直接托管）
│   ├── index.html
│   ├── styles.css
│   └── app.js
├── drills/
│   └── chem-plant.yaml      # 示例剧本：化工厂反应釜泄漏与次生火灾
├── Dockerfile
└── docker-compose.yml
```

---

## 快速开始（Docker Compose）

前置要求：安装 Docker 与 Docker Compose 插件。

```bash
# 1. 启动（首次会构建镜像）
docker compose up -d --build

# 2. 查看日志
docker compose logs -f
```

打开浏览器访问：

| 入口 | 地址 |
| --- | --- |
| 应用首页（主持人/参与者入口） | <http://localhost:8000/> |
| 健康检查 | <http://localhost:8000/api/health> |
| OpenAPI 文档 | <http://localhost:8000/docs> |

停止与清理：

```bash
docker compose down          # 停止
docker compose down -v       # 同时删除会话数据库卷（drill-data）
```

### 不使用 Docker（本地开发）

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
# 前端与 drills/ 目录通过相对路径自动定位；数据库默认写入仓库根 /data
```

---

## 配置

所有配置通过环境变量提供（见 `docker-compose.yml`）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `HOST_KEY` | `host-1234` | 主持人密钥。前端首页输入；HTTP 用请求头 `X-Host-Key`，WebSocket 用查询参数 `host=`。**生产环境必须修改** |
| `DATA_DIR` | `/data` | SQLite 数据库目录（`drills.db`）。Compose 中为持久化卷 |
| `DRILLS_DIR` | `/drills` | YAML 剧本目录，启动时自动加载（只读挂载 `./drills`） |
| `STATIC_DIR` | `/app/frontend` | 前端静态文件目录 |

端口映射在 `docker-compose.yml` 的 `ports` 中修改（如 `"18000:8000"`）。
时间均以服务器 UTC 时间戳（POSIX 秒）传输，前端按浏览器本地时区显示。

> 部署模型为 **单副本**：写串行锁与 WebSocket 连接表在进程内维护。
> SQLite 本身已是单文件、零外部依赖；需要横向扩展时应改用粘性会话或外移状态。

---

## 使用指南

### 主持人

1. 打开 <http://localhost:8000/>，选择“主持人入口”，输入密钥（默认 `host-1234`）。
2. 在“演练剧本”处可直接粘贴 YAML 上传/更新（写入 `drills/`，重启生效保留），
   或预先把 YAML 放到宿主机 `./drills/` 目录（容器内 `/drills`）。
3. 对剧本点击“创建会话”，得到一个 **8 位会话编号**，进入主持台。
4. 把 **邀请链接/邀请码** 发给对应角色的参与者（主持台可一键复制）。
5. 人员就绪后点击“▶ 开始演练”。主持台可看到：
   - 每个阶段的公开信息、**所有角色的隐藏信息**、选项指向的分支；
   - 各角色是否已提交、在线/离线状态、倒计时；
   - “⏭ 立即结算当前阶段”（未提交角色按超时处理）、“■ 结束整场演练”。
6. 结束后在主持台导出 **全量记录（Markdown/JSON）** 或打开“确定性回放”。

### 参与者

1. 打开邀请链接（形如 `http://主机:8000/#/join/<会话编号>?invite=<邀请码>`），
   或在首页手动输入会话编号与邀请码、姓名。
2. 加入后浏览器本地保存访问令牌（用于断线重连、个人回放与导出）。
3. 页面只显示 **公开信息 + 本角色可见信息 + 本角色的选项**；
   在倒计时内点选并确认提交（**提交不可更改**）。
4. 网络中断会自动重连并重新获取快照；若重发了已提交的选择，服务器幂等返回原结果。
5. 演练结束后可导出自己的 **个人记录**，并查看自己视角的版本回放。

### 示例剧本的邀请码（chem-plant）

| 角色 | 邀请码 |
| --- | --- |
| 值班班长 | `CMD-2026` |
| 外操员 | `EVA-2026` |
| 安全员 | `SAF-2026` |

---

## YAML 剧本格式

最小示例：

```yaml
id: demo                 # 必须与文件名一致（demo.yaml）
name: 示例演练
description: 一句话说明

roles:
  - id: cmd
    name: 值班班长
    invite: CMD-2026     # 每个角色唯一的加入邀请码

stages:
  - id: s1
    title: 第一阶段
    public: 所有角色都能看到的公开信息
    announcement: 阶段公告（默认同 public）
    duration: 45         # 限时秒数；null 表示只能由主持人手动结算
    default_next: s2     # 无匹配分支时的去向；省略或 null 表示演练结束

    views:               # 仅对应角色可见的隐藏信息
      cmd: 只有班长知道的情况

    options:             # 需要做决定的角色与其限时选项
      cmd:
        - id: evacuate
          text: 组织撤离
        - id: hold
          text: 原地等待
          next: s3       # 单决策角色可直接用选项指定去向（优先级最高）

    transitions:         # 多角色条件分支：按顺序匹配，首个全部相等的生效
      - when: {cmd: evacuate}
        next: s2
      - when: {cmd: hold}
        next: s3
```

### 结算与分支规则

1. 每个阶段的“决策角色”是 `options` 下出现的角色；其余角色该阶段只需等待。
2. 满足任一条件即结算该阶段：
   - **全部决策角色都已提交** → 自动结算（`complete`）；
   - **到达 `duration` 截止时刻** → 定时器结算，未提交者选择记为 `null`（超时，`deadline`）；
   - **主持人手动结算** → 未提交者同样记为超时（`host`）。
3. 下一阶段按优先级确定：
   1. 已提交选项上的 `next`（适合单决策者直接跳转）；
   2. `transitions` 中首个 `when` 条件全部满足的规则
      （`{role: choice_id}` 全等；超时角色不等于任何选项 id，因此不会命中）；
   3. `default_next`；为 `null`/省略则演练结束。
4. `duration: null` 的阶段永不自动超时，由主持人手动结算（如复盘阶段）。
5. 校验规则：id 唯一、邀请码唯一、所有 `next`/`default_next` 必须指向已存在阶段、
   选项/视图/分支引用的角色必须存在、`duration` 为正整数。

---

## HTTP / WebSocket 接口摘要

### HTTP（`X-Host-Key` 为主持人密钥；参与者接口用 `?token=` 或加入返回的令牌）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 健康检查 |
| GET | `/api/drills` | 剧本列表（主持人） |
| POST | `/api/drills` | 上传/更新剧本，body `{"yaml": "..."}`（主持人） |
| POST | `/api/sessions?drill_id=...` | 创建会话（主持人） |
| GET | `/api/sessions` | 会话列表（主持人） |
| POST | `/api/sessions/{sid}/join` | body `{"invite": "...", "name": "..."}`，返回角色与令牌 |
| GET | `/api/sessions/{sid}/replay?token=&version=` | 逐版本确定性回放（主持人或参与者） |
| GET | `/api/sessions/{sid}/record?fmt=md\|json&token=` | 全量记录（主持人）/ 个人记录（参与者） |

### WebSocket

```
ws://<host>:8000/ws/{sessionId}?host=<HOST_KEY>      # 主持人
ws://<host>:8000/ws/{sessionId}?token=<participantToken>  # 参与者
```

- 服务端推送：`{"type":"snapshot","snapshot":{...}}`（裁剪后的完整快照）。
- 参与者发送：`{"type":"submit","choice_id":"...","client_stage":"..."}`，
  服务端回 `submit_result`（`accepted` / `duplicate` 幂等重放 / 截止拒绝并已结算）。
- 主持人发送：`{"type":"start"}`、`{"type":"settle"}`、`{"type":"end"}`。
- 心跳：客户端 `{"type":"ping","t":...}`，服务端回 `{"type":"pong",...}`（同时用于时钟校准）。
- 认证失败返回 `error` 后以 4401 关闭；队列积压时服务端丢弃旧消息并触发快照重同步。

---

## 一致性设计（重复提交 / 断线重连 / 截止竞争）

- **事件溯源 + 单调版本号**：所有状态由 `events(session_id, version, seq)` 折叠得到；
  每次原子提交占一个新版本（如“全员提交 → 结算 → 打开下一阶段”同属 vN）。
- **原子结算**：写操作在会话级 `asyncio.Lock` 内执行
  `BEGIN IMMEDIATE`，提交选择与可能触发的结算在同一事务内完成。
- **截止竞争**：到达 deadline 后到达的提交会被拒绝，且“拒绝 + 超时结算”在同一事务；
  定时器回调在事务内再次校验 deadline，主持人手动结算与定时器结算由同一把锁串行化。
- **重复提交**：`submissions(session, stage, role)` 主键去重；
  重复请求（含阶段已结束后的重发）幂等返回首次结果，不产生第二条事件。
- **断线重连**：WebSocket 指数退避自动重连，连上即收到当前版本快照；
  在线状态随连接建立/关闭广播；服务重启后为所有运行中会话重新武装截止定时器。
- **权限裁剪**：参与者快照只含已开放阶段、公开文本、本人视图/选项/本人选择；
  其他角色的私有视图、选项分支与决定不会出现在任何参与者响应、回放或导出中。
- **确定性回放**：回放只依赖事件日志与固定剧本，逐版本折叠，
  同一版本永远得到同一快照；主持人可见全量事件，参与者仅得到本人视角。
