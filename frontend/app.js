"use strict";
/* 多人应急演练平台 — 原生 JS 前端（无构建步骤）
 *
 * hash 路由：
 *   #/                         首页：主持人 / 参与者入口
 *   #/host                     主持人控制台（剧本、会话）
 *   #/host/session/<sid>       某会话的主持台（实时）
 *   #/join/<sid>[?invite=...]  参与者输入邀请码
 *   #/play/<sid>               参与者控制台（实时）
 *   #/replay/<sid>             确定性回放
 */

const app = document.getElementById("app");
const nav = document.getElementById("nav");
const navContext = document.getElementById("nav-context");
document.getElementById("nav-home").addEventListener("click", (e) => {
  e.preventDefault();
  location.hash = "#/";
});

// --------------------------------------------------------------------------- //
// utils
// --------------------------------------------------------------------------- //
const HOST_KEY = () => localStorage.getItem("host_key") || "";
const credKey = (sid) => `pt_${sid}`;
const getCred = (sid) => {
  try { return JSON.parse(localStorage.getItem(credKey(sid)) || "null"); }
  catch { return null; }
};
const setCred = (sid, c) => localStorage.setItem(credKey(sid), JSON.stringify(c));

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );

function toast(msg, kind = "") {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = `toast ${kind}`;
  clearTimeout(toast._tm);
  toast._tm = setTimeout(() => t.classList.add("hidden"), 3200);
}

function el(html) {
  const wrap = document.createElement("div");
  wrap.innerHTML = html.trim();
  return wrap.firstElementChild;
}

async function api(path, opts = {}) {
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (opts.host) headers["X-Host-Key"] = HOST_KEY();
  if (opts.token) headers["Authorization"] = `Bearer ${opts.token}`;
  const res = await fetch(path, {
    method: opts.method || "GET",
    headers,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const err = new Error((data && data.detail) || `请求失败 (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function copyText(text) {
  navigator.clipboard?.writeText(text).then(
    () => toast("已复制到剪贴板", "ok"),
    () => toast("复制失败")
  );
}

function fmtTime(ts) {
  if (ts == null) return "—";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}
function fmtClock(ts) {
  if (ts == null) return "--:--:--";
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}
function roleName(snap, rid) {
  const r = (snap.roles || []).find((x) => x.id === rid);
  if (r) return r.name;
  const p = (snap.participants || []).find((x) => x.role_id === rid);
  return p ? p.role_name : rid;
}
function statusChip(status) {
  return ({
    created: ["未开始", "warn"],
    running: ["进行中", "ok"],
    ended: ["已结束", "danger"],
  }[status] || [status, ""]);
}

// --------------------------------------------------------------------------- //
// live WebSocket with auto reconnect
// --------------------------------------------------------------------------- //
class Live {
  constructor(sessionId, viewer, handlers) {
    this.sessionId = sessionId;
    this.viewer = viewer;             // {host:'key'} or {token:'...'}
    this.onSnapshot = handlers.onSnapshot || (() => {});
    this.onResult = handlers.onResult || (() => {});
    this.onStatus = handlers.onStatus || (() => {});
    this.closedByUser = false;
    this.retry = 0;
    this.serverOffset = 0;
    this.connect();
  }
  url() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const q = this.viewer.host
      ? `host=${encodeURIComponent(this.viewer.host)}`
      : `token=${encodeURIComponent(this.viewer.token)}`;
    return `${proto}://${location.host}/ws/${this.sessionId}?${q}`;
  }
  connect() {
    const ws = new WebSocket(this.url());
    this.ws = ws;
    this.onStatus("connecting");
    ws.onopen = () => this.onStatus("open");
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === "snapshot") {
        this.retry = 0;
        this.serverOffset = (msg.snapshot.server_time || 0) - Date.now() / 1000;
        this.onSnapshot(msg.snapshot);
      } else if (msg.type === "pong") {
        this.serverOffset = msg.server_time - Date.now() / 1000;
      } else if (msg.type === "submit_result") {
        this.onResult(msg);
      } else if (msg.type === "error") {
        toast(msg.detail || "操作被拒绝", "error");
        if (msg.code === "auth") this.close();
      }
    };
    ws.onclose = () => {
      if (this.closedByUser) return;
      this.retry = Math.min(this.retry + 1, 6);
      const delay = Math.min(500 * 2 ** this.retry, 10000);
      this.onStatus("reconnecting", delay);
      setTimeout(() => { if (!this.closedByUser) this.connect(); }, delay);
    };
  }
  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
      return true;
    }
    toast("连接中断，正在重连…", "error");
    return false;
  }
  now() { return Date.now() / 1000 + this.serverOffset; }
  close() { this.closedByUser = true; try { this.ws.close(); } catch {} }
}

// --------------------------------------------------------------------------- //
// router
// --------------------------------------------------------------------------- //
let intervals = [];
let liveHandle = null;
function clearTimers() {
  intervals.forEach(clearInterval);
  intervals = [];
}
function teardownLive() {
  if (liveHandle) { liveHandle.close(); liveHandle = null; }
}

function route() {
  teardownLive();
  clearTimers();
  const raw = (location.hash || "#/").replace(/^#\/?/, "");
  const [path, query] = raw.split("?");
  const params = new URLSearchParams(query || "");
  const parts = path.split("/").filter(Boolean);
  nav.classList.toggle("hidden", parts.length === 0);
  navContext.textContent = "";

  if (parts[0] === "host" && parts[1] === "session" && parts[2]) {
    navContext.textContent = `主持人 · 会话 ${parts[2]}`;
    return hostSessionPage(parts[2]);
  }
  if (parts[0] === "host") { navContext.textContent = "主持人控制台"; return hostPage(); }
  if (parts[0] === "join" && parts[1]) return joinPage(parts[1], params);
  if (parts[0] === "play" && parts[1]) return playPage(parts[1]);
  if (parts[0] === "replay" && parts[1]) return replayPage(parts[1]);
  return homePage();
}
window.addEventListener("hashchange", route);

// --------------------------------------------------------------------------- //
// home
// --------------------------------------------------------------------------- //
function homePage() {
  app.innerHTML = "";
  app.appendChild(el(`
    <div class="card">
      <h2>主持人入口</h2>
      <p class="muted">管理 YAML 剧本、创建会话、实时结算、回放与导出全量记录。</p>
      <label>主持人密钥（HOST_KEY）</label>
      <input type="password" id="hk" value="${esc(HOST_KEY())}" placeholder="默认 host-1234" />
      <div class="row" style="margin-top:14px">
        <button id="go-host">进入主持人控制台</button>
      </div>
    </div>`));
  app.appendChild(el(`
    <div class="card">
      <h2>参与者入口</h2>
      <p class="muted">凭主持人发放的会话编号与角色邀请码加入；只能看到本角色的信息。</p>
      <div class="row">
        <input type="text" id="sid" placeholder="会话编号（8 位）" style="max-width:280px" />
        <button id="go-join" class="ghost">加入演练</button>
      </div>
    </div>`));
  document.getElementById("go-host").onclick = () => {
    localStorage.setItem("host_key", document.getElementById("hk").value.trim());
    location.hash = "#/host";
  };
  document.getElementById("go-join").onclick = () => {
    const sid = document.getElementById("sid").value.trim();
    if (!sid) return toast("请输入会话编号");
    location.hash = `#/join/${encodeURIComponent(sid)}`;
  };
}

// --------------------------------------------------------------------------- //
// host: drills & sessions
// --------------------------------------------------------------------------- //
async function hostPage() {
  app.innerHTML = `<div class="card"><h2>主持人控制台</h2><p class="muted">加载中…</p></div>`;
  let drills, sessions;
  try {
    [drills, sessions] = await Promise.all([
      api("/api/drills", { host: true }),
      api("/api/sessions", { host: true }),
    ]);
  } catch (e) {
    app.innerHTML = "";
    app.appendChild(el(`<div class="card"><h2>主持人控制台</h2>
      <p class="chip danger">${esc(e.message)}</p>
      <p class="muted small">请返回首页检查主持人密钥。</p></div>`));
    return;
  }

  const card = el(`<div class="card">
    <h2>演练剧本（YAML）</h2>
    <div id="drill-list"></div>
    <h3>上传 / 更新剧本</h3>
    <textarea id="yaml" placeholder="粘贴 YAML 剧本（顶层 id 需保证唯一）"></textarea>
    <div class="row" style="margin-top:10px"><button id="upload">保存剧本</button>
      <span class="muted small">保存后写入服务器 drills/ 目录，重启仍然生效。</span></div>
  </div>`);

  const list = card.querySelector("#drill-list");
  if (!drills.length) {
    list.innerHTML = `<p class="muted">暂无剧本。将 YAML 放入 <code>drills/</code> 目录，或在下方粘贴上传。</p>`;
  } else {
    for (const d of drills) {
      const row = el(`<div class="box">
        <div class="row"><strong>${esc(d.name)}</strong>
          <span class="chip mono">${esc(d.id)}</span><span class="spacer"></span>
          <button data-id="${esc(d.id)}">创建会话</button></div>
        <div class="muted small">${esc(d.description || "")}</div></div>`);
      row.querySelector("button").onclick = async (ev) => {
        try {
          const r = await api(`/api/sessions?drill_id=${encodeURIComponent(ev.target.dataset.id)}`,
            { method: "POST", host: true });
          toast(`会话 ${r.session_id} 已创建`, "ok");
          location.hash = `#/host/session/${r.session_id}`;
        } catch (e2) { toast(e2.message, "error"); }
      };
      list.appendChild(row);
    }
  }
  card.querySelector("#upload").onclick = async () => {
    try {
      const r = await api("/api/drills", {
        method: "POST", host: true, body: { yaml: card.querySelector("#yaml").value },
      });
      toast(`剧本 ${r.id} 已保存`, "ok");
      hostPage();
    } catch (e) { toast(e.message, "error"); }
  };

  const sc = el(`<div class="card"><h2>会话</h2><div id="session-list"></div></div>`);
  const sl = sc.querySelector("#session-list");
  if (!sessions.length) {
    sl.innerHTML = `<p class="muted">暂无会话。</p>`;
  } else {
    for (const s of sessions) {
      const [label, cls] = statusChip(s.status);
      sl.appendChild(el(`<div class="box">
        <div class="row">
          <strong class="mono">${esc(s.id)}</strong>
          <span class="chip ${cls}">${label}</span>
          <span class="muted small">${esc(s.drill_name || s.drill_id)}</span>
          <span class="spacer"></span>
          <a href="#/host/session/${esc(s.id)}"><button class="ghost">主持台</button></a>
          <a href="#/replay/${esc(s.id)}"><button class="ghost">回放</button></a>
        </div></div>`));
    }
  }

  app.innerHTML = "";
  app.append(card, sc);
}

// --------------------------------------------------------------------------- //
// host live console
// --------------------------------------------------------------------------- //
function hostSessionPage(sid) {
  app.innerHTML = `<div class="card"><h2>主持台 <span class="mono">${esc(sid)}</span></h2>
    <p id="connline" class="muted">正在建立实时连接…</p></div>`;
  let snapRef = null;

  liveHandle = new Live(sid, { host: HOST_KEY() }, {
    onStatus: (s, delay) => {
      const line = document.getElementById("connline");
      if (!line) return;
      if (s === "reconnecting") {
        line.innerHTML = `<span class="conn off">● 断开，${(delay / 1000).toFixed(0)}s 后自动重连…</span>`;
      } else if (s === "connecting") {
        line.innerHTML = `<span class="muted">● 连接中…</span>`;
      }
    },
    onSnapshot: (snap) => { snapRef = snap; renderHost(snap); },
  });

  function renderHost(snap) {
    clearTimers();
    const s = snap.session;
    const [lbl, cls] = statusChip(s.status);
    const banner = {
      created: ["wait", "会话尚未开始。参与者凭邀请码加入；就绪后开始演练。"],
      running: ["live", "演练进行中：服务端正向各客户端推送按角色裁剪的快照。"],
      ended: ["ended", `演练已结束（${esc(s.end_reason_label || s.end_reason || "")}）。可回放或导出记录。`],
    }[s.status];

    const root = el(`<div>
      <div class="banner ${banner[0]}">${banner[1]}</div>
      <div class="card">
        <div class="row">
          <h2 style="margin:0">控制台</h2>
          <span class="chip ${cls}">${lbl}</span>
          <span class="spacer"></span>
          <span class="conn on">● 已连接</span>
        </div>
        <div class="row" id="buttons" style="margin-top:12px"></div>
        <p class="muted small" style="margin-top:10px">
          当前单调版本号：<b class="mono">v${s.version}</b> ·
          服务端时间：<span id="srvclock"></span>
        </p>
      </div>
      <div class="card" id="people"></div>
      <div class="card" id="stages"><h2>阶段（含各角色隐藏信息）</h2></div>
      <div class="card" id="exports"></div>
    </div>`);

    const btns = root.querySelector("#buttons");
    if (s.status === "created") {
      const b = el(`<button>▶ 开始演练</button>`);
      b.onclick = () => liveHandle.send({ type: "start" });
      btns.appendChild(b);
    }
    if (s.status === "running") {
      const b1 = el(`<button class="warn">⏭ 立即结算当前阶段（未提交按超时）</button>`);
      b1.onclick = () => liveHandle.send({ type: "settle" });
      const b2 = el(`<button class="danger">■ 结束整场演练</button>`);
      b2.onclick = () => {
        if (confirm("确定结束整场演练？当前未提交的角色将按超时结算。"))
          liveHandle.send({ type: "end" });
      };
      btns.append(b1, b2);
    }
    btns.appendChild(el(`<a href="#/replay/${esc(sid)}"><button class="ghost">↺ 确定性回放</button></a>`));

    // people
    const people = root.querySelector("#people");
    people.innerHTML = `<h2>角色、邀请码与参与者</h2>`;
    const joined = Object.fromEntries(snap.participants.map((p) => [p.role_id, p]));
    const table = el(`<table><thead><tr>
      <th>角色</th><th>邀请码</th><th>参与者</th><th>连接</th><th>邀请链接</th></tr></thead><tbody></tbody></table>`);
    for (const r of snap.roles) {
      const p = joined[r.id];
      const link = `${location.origin}/#/join/${sid}?invite=${encodeURIComponent(r.invite)}`;
      const tr = el(`<tr>
        <td>${esc(r.name)}</td>
        <td class="mono">${esc(r.invite)}</td>
        <td>${p ? esc(p.display_name) : '<span class="muted">未加入</span>'}</td>
        <td>${p ? (p.online ? '<span class="conn on">●在线</span>' : '<span class="conn off">○离线</span>') : ""}</td>
        <td><div class="copybox"><code>${esc(link)}</code><button class="ghost">复制</button></div></td>
      </tr>`);
      tr.querySelector("button").onclick = () => copyText(link);
      table.querySelector("tbody").appendChild(tr);
    }
    people.appendChild(table);

    // stages
    const stagesCard = root.querySelector("#stages");
    const curFirst = [...snap.history].sort((a, b) =>
      (a.stage_id === s.current_stage ? -1 : b.stage_id === s.current_stage ? 1 : 0));
    for (const stg of curFirst) stagesCard.appendChild(hostStageBlock(stg, snap));

    // exports
    const ex = root.querySelector("#exports");
    ex.innerHTML = `<h2>记录导出</h2>
      <p class="muted small">主持人全量记录包含隐藏信息、全部决定与事件日志；
      个人记录由参与者用本人令牌导出，已剔除其他角色的任何隐藏信息。</p>
      <div class="row">
        <a href="/api/sessions/${sid}/record?fmt=md" target="_blank"><button>全量记录 Markdown</button></a>
        <a href="/api/sessions/${sid}/record?fmt=json" target="_blank"><button class="ghost">全量记录 JSON</button></a>
      </div>`;

    app.innerHTML = "";
    app.appendChild(root);

    const tick = () => {
      const clock = document.getElementById("srvclock");
      if (clock) clock.textContent = fmtClock(liveHandle.now());
    };
    intervals.push(setInterval(tick, 500));
    tick();
  }

  function hostStageBlock(stg, snap) {
    const s = snap.session;
    const isCurrent = stg.stage_id === s.current_stage;
    const cls = isCurrent ? "current" : stg.kind === "sealed" ? "sealed" : "";
    const node = el(`<div class="stage ${cls}">
      <div class="row">
        <span class="stage-title">${esc(stg.title)}</span>
        <span class="chip mono">${esc(stg.stage_id)}</span>
        ${isCurrent ? '<span class="chip ok">当前阶段</span>' : ""}
        ${stg.kind === "sealed" ? `<span class="chip">${esc(stg.reason_label)}</span>` : ""}
        ${!stg.reached ? '<span class="chip warn">未到达</span>' : ""}
        <span class="spacer"></span>
        ${isCurrent && stg.duration ? '<span class="timer" id="host-timer">--</span>' : ""}
      </div>
      <div class="box public"><div class="box-label">公开信息（全员可见）</div>${esc(stg.public)}</div>
      <div class="views"></div>
      <div class="opts"></div>
      ${stg.kind === "sealed" ? `<div class="muted small">下一阶段：<b>${esc(stg.next || "（结束）")}</b></div>` : ""}
    </div>`);

    for (const [rid, text] of Object.entries(stg.views || {})) {
      node.querySelector(".views").appendChild(el(
        `<div class="box private"><div class="box-label">仅 ${esc(roleName(snap, rid))} 可见</div>${esc(text)}</div>`));
    }

    for (const rid of stg.deciding_roles || []) {
      const choice = stg.choices?.[rid];
      let stateHtml;
      if (!stg.reached) stateHtml = '<span class="muted">—</span>';
      else if (stg.kind === "sealed" && !choice) stateHtml = '<span class="chip danger">超时未提交</span>';
      else if (choice) stateHtml = `<span class="chip ok">已提交：${esc(choice.label || choice.id)}</span>`;
      else stateHtml = '<span class="chip warn">待提交</span>';

      const optsHtml = (stg.options?.[rid] || []).map((o) => `
        <div class="option ${choice && choice.id === o.id ? "chosen" : "disabled"}">
          <span class="oid">${esc(o.id)}</span>${esc(o.text)}
          ${o.next ? `<span class="muted small"> → ${esc(o.next)}</span>` : ""}
        </div>`).join("");

      node.querySelector(".opts").appendChild(el(`<div class="box">
        <div class="row"><strong>${esc(roleName(snap, rid))}</strong>${stateHtml}</div>
        ${optsHtml}
      </div>`));
    }

    if (isCurrent) {
      const cd = snap.current_detail;
      node.appendChild(el(`<p class="muted small">
        决策角色 ${cd.deciding_roles.length} 人，已提交 ${cd.submitted_roles.length} 人。
        全员提交即自动结算；到达截止时刻未提交者按超时进入条件分支。</p>`));
      if (stg.duration) {
        const bar = el(`<div class="timerbar" id="host-bar"><div style="width:100%"></div></div>`);
        node.appendChild(bar);
      }
    }
    return node;
  }

  // host countdown refresh
  setInterval(() => {
    if (!snapRef || snapRef.session.status !== "running") return;
    const s = snapRef.session;
    const t = document.getElementById("host-timer");
    const bar = document.getElementById("host-bar");
    if (t && s.deadline) {
      const remain = s.deadline - liveHandle.now();
      t.textContent = remain > 0 ? `${Math.ceil(remain)} 秒` : "结算中…";
      t.classList.toggle("urgent", remain <= 10);
      if (bar) {
        const cur = snapRef.history.find((x) => x.stage_id === s.current_stage);
        const pct = cur?.duration ? Math.max(0, remain / cur.duration) * 100 : 0;
        bar.firstElementChild.style.width = `${pct}%`;
        bar.classList.toggle("urgent", remain <= 10);
      }
    }
  }, 300);
}

// --------------------------------------------------------------------------- //
// join
// --------------------------------------------------------------------------- //
function joinPage(sid, params) {
  const existing = getCred(sid);
  app.innerHTML = "";
  const card = el(`<div class="card">
    <h2>加入演练 <span class="mono">${esc(sid)}</span></h2>
    <p class="muted">请输入主持人向您提供的角色邀请码。加入后该角色即与您绑定。</p>
    <label>角色邀请码</label>
    <input type="text" id="invite" value="${esc(params.get("invite") || "")}" placeholder="如 CMD-2026" />
    <label>您的姓名（可选，默认使用角色名）</label>
    <input type="text" id="name" placeholder="姓名 / 工号" />
    <div class="row" style="margin-top:14px">
      <button id="do-join">加入</button>
      ${existing ? `<button class="ghost" id="resume">以「${esc(existing.role_name)}」重新进入</button>` : ""}
    </div>
    <p id="jerr" class="chip danger hidden" style="margin-top:12px"></p>
  </div>`);
  app.appendChild(card);

  const showErr = (m) => {
    const p = card.querySelector("#jerr");
    p.textContent = m;
    p.classList.remove("hidden");
  };
  card.querySelector("#do-join").onclick = async () => {
    const invite = card.querySelector("#invite").value.trim();
    const name = card.querySelector("#name").value.trim();
    if (!invite) return showErr("请输入邀请码");
    try {
      const r = await api(`/api/sessions/${encodeURIComponent(sid)}/join`, {
        method: "POST", body: { invite, name },
      });
      setCred(sid, {
        token: r.token, role_id: r.role_id, role_name: r.role_name,
        display_name: r.display_name,
      });
      toast(`已加入：${r.role_name}`, "ok");
      location.hash = `#/play/${sid}`;
    } catch (e) { showErr(e.message); }
  };
  const resume = card.querySelector("#resume");
  if (resume) resume.onclick = () => { location.hash = `#/play/${sid}`; };
}

// --------------------------------------------------------------------------- //
// participant live console
// --------------------------------------------------------------------------- //
function playPage(sid) {
  const cred = getCred(sid);
  if (!cred) { location.hash = `#/join/${sid}`; return; }

  app.innerHTML = `<div class="card"><h2>演练控制台</h2>
    <p id="connline" class="muted">正在建立实时连接…</p></div>`;
  let snapRef = null;

  liveHandle = new Live(sid, { token: cred.token }, {
    onStatus: (s, delay) => {
      const line = document.getElementById("connline");
      if (!line) return;
      line.innerHTML = s === "reconnecting"
        ? `<span class="conn off">● 断线，${(delay / 1000).toFixed(0)}s 后自动重连（提交不会丢失）…</span>`
        : `<span class="muted">● 连接中…</span>`;
    },
    onSnapshot: (snap) => { snapRef = snap; renderPlay(snap); },
    onResult: (r) => {
      if (r.duplicate) toast("已提交过，本次为重复请求（幂等返回）", "ok");
      else if (r.accepted) toast("选择已提交", "ok");
      else if (r.reason === "deadline") toast("到达截止时刻，本阶段已结算", "error");
    },
  });

  function renderPlay(snap) {
    clearTimers();
    const s = snap.session;
    const me = snap.me;
    const [lbl] = statusChip(s.status);

    const root = el(`<div>
      <div class="card">
        <div class="row">
          <h2 style="margin:0">${esc(snap.drill.name)}</h2>
          <span class="spacer"></span>
          <span class="chip host">${esc(me.role_name)}</span>
          <span class="conn on">● 已连接</span>
        </div>
        <p class="muted small" style="margin:6px 0 0">
          会话 <span class="mono">${esc(s.id)}</span> · 状态：${lbl} ·
          版本 <b class="mono">v${s.version}</b> · 服务端时间 <span id="srvclock"></span>
        </p>
      </div>
      <div id="banner-slot"></div>
      <div class="card" id="current"></div>
      <div class="card" id="history"><h2>已发生的阶段</h2></div>
      <div class="card" id="exports"></div>
    </div>`);

    const bannerSlot = root.querySelector("#banner-slot");
    if (s.status === "created") {
      bannerSlot.appendChild(el(`<div class="banner wait">演练尚未开始，请等待主持人开始。</div>`));
    } else if (s.status === "ended") {
      bannerSlot.appendChild(el(`<div class="banner ended">
        演练已结束（${esc(s.end_reason_label || "")}）。可在下方导出您的个人记录。</div>`));
    } else {
      bannerSlot.appendChild(el(`<div class="banner live">演练进行中。</div>`));
    }

    // current stage (the last block is current when kind == current)
    const curCard = root.querySelector("#current");
    const current = snap.history.find((x) => x.kind === "current");
    if (s.status === "running" && current) {
      curCard.innerHTML = "";
      curCard.appendChild(roleStageBlock(current, snap, true));
    } else {
      curCard.innerHTML = s.status === "created"
        ? "<h2>等待开始…</h2>"
        : "<h2>当前无开放阶段</h2>";
    }

    const hist = root.querySelector("#history");
    const sealed = snap.history.filter((x) => x.kind === "sealed");
    if (!sealed.length) {
      hist.innerHTML = `<h2>已发生的阶段</h2><p class="muted small">暂无。</p>`;
    } else {
      for (const stg of sealed) hist.appendChild(roleStageBlock(stg, snap, false));
    }

    // exports (visible always, most useful after end)
    const ex = root.querySelector("#exports");
    const tk = encodeURIComponent(cred.token);
    ex.innerHTML = `<h2>我的个人记录</h2>
      <p class="muted small">仅含公开信息与您本人的可见内容和选择，绝不含其他角色的隐藏信息。</p>
      <div class="row">
        <a href="/api/sessions/${sid}/record?fmt=md&token=${tk}" target="_blank"><button>导出 Markdown</button></a>
        <a href="/api/sessions/${sid}/record?fmt=json&token=${tk}" target="_blank"><button class="ghost">导出 JSON</button></a>
        <a href="#/replay/${sid}"><button class="ghost">我的回放</button></a>
        <span class="spacer"></span>
        <button class="ghost" id="leave">清除本地登录信息</button>
      </div>`;
    ex.querySelector("#leave").onclick = () => {
      localStorage.removeItem(credKey(sid));
      location.hash = "#/";
    };

    app.innerHTML = "";
    app.appendChild(root);

    const tick = () => {
      const clock = document.getElementById("srvclock");
      if (clock) clock.textContent = fmtClock(liveHandle.now());
      const t = document.getElementById("role-timer");
      const bar = document.getElementById("role-bar");
      if (t && snapRef.session.deadline) {
        const remain = snapRef.session.deadline - liveHandle.now();
        t.textContent = remain > 0 ? `剩余 ${Math.ceil(remain)} 秒` : "正在结算…";
        t.classList.toggle("urgent", remain <= 10);
        if (bar) {
          const dur = current?.duration || 0;
          bar.firstElementChild.style.width = `${dur ? Math.max(0, remain / dur) * 100 : 0}%`;
          bar.classList.toggle("urgent", remain <= 10);
        }
      }
    };
    intervals.push(setInterval(tick, 250));
    tick();
  }

  function roleStageBlock(stg, snap, isCurrent) {
    const node = el(`<div class="stage ${isCurrent ? "current" : "sealed"}">
      <div class="row">
        <span class="stage-title">${esc(stg.title)}</span>
        ${isCurrent ? '<span class="chip ok">当前阶段</span>' : ""}
        ${!isCurrent ? `<span class="chip">${esc(stg.reason_label)}</span>` : ""}
        <span class="spacer"></span>
        ${isCurrent && stg.duration ? '<span class="timer" id="role-timer">--</span>' : ""}
      </div>
      ${isCurrent && stg.duration ? '<div class="timerbar" id="role-bar"><div style="width:100%"></div></div>' : ""}
      <div class="box public"><div class="box-label">公开信息</div>${esc(stg.public)}</div>
      ${stg.private ? `<div class="box private"><div class="box-label">仅您可见</div>${esc(stg.private)}</div>` : ""}
      <div class="decision"></div>
      ${!isCurrent ? `<div class="muted small">阶段后续：<b>${esc(stg.next ? "进入下一阶段" : "演练结束")}</b></div>` : ""}
    </div>`);

    const dec = node.querySelector(".decision");
    if (stg.deciding) {
      const chosen = stg.my_choice;
      if (isCurrent) {
        if (chosen) {
          dec.appendChild(el(`<p><span class="chip ok">您已提交：${esc(stg.my_choice_label || chosen)}</span></p>`));
          dec.appendChild(el(`<p class="muted small">提交不可更改。如因断线重复发送，服务器幂等处理。</p>`));
        } else {
          dec.appendChild(el(`<p class="muted small">请在限时内选择（截止后未选按超时处理）：</p>`));
        }
      } else if (stg.timed_out) {
        dec.appendChild(el(`<p><span class="chip danger">您未在截止前提交（超时）</span></p>`));
      } else if (chosen) {
        dec.appendChild(el(`<p><span class="chip ok">您的决定：${esc(stg.my_choice_label || chosen)}</span></p>`));
      }

      for (const o of stg.options || []) {
        const btn = el(`<button class="option ${chosen === o.id ? "chosen" : ""} ${chosen ? "disabled" : ""}">
          <span class="oid">${esc(o.id)}</span>${esc(o.text)}</button>`);
        if (!chosen && isCurrent) {
          btn.onclick = () => {
            if (!confirm(`确认选择「${o.text}」？提交后不可更改。`)) return;
            liveHandle.send({
              type: "submit",
              choice_id: o.id,
              client_stage: stg.stage_id,
            });
            btn.disabled = true;
            toast("已发送，等待服务器确认…");
          };
        }
        dec.appendChild(btn);
      }
    } else if (isCurrent) {
      dec.appendChild(el(`<p class="muted small">本阶段您无需决策，请等待其他角色提交或主持人结算。</p>`));
    }
    return node;
  }
}

// --------------------------------------------------------------------------- //
// deterministic replay
// --------------------------------------------------------------------------- //
async function replayPage(sid) {
  app.innerHTML = `<div class="card"><h2>确定性回放 <span class="mono">${esc(sid)}</span></h2>
    <p class="muted">正在加载事件日志…</p></div>`;

  const cred = getCred(sid);
  let data = null;
  let asRole = false;
  try {
    data = await api(`/api/sessions/${sid}/replay`, { host: true });
  } catch (e) {
    if (cred) {
      try {
        data = await fetch(`/api/sessions/${sid}/replay?token=${encodeURIComponent(cred.token)}`).then((r) => {
          if (!r.ok) throw new Error("无权重放");
          return r.json();
        });
        asRole = true;
      } catch { /* fallthrough */ }
    }
    if (!data) {
      app.innerHTML = "";
      app.appendChild(el(`<div class="card"><h2>回放</h2>
        <p class="chip danger">${esc(e.message)}</p>
        <p class="muted small">主持人请在首页输入正确的 HOST_KEY；参与者请先从邀请链接加入。</p></div>`));
      return;
    }
  }

  const frames = data.frames;
  let idx = frames.length - 1;

  const card = el(`<div class="card">
    <div class="row">
      <h2 style="margin:0">${esc(data.drill_name)}</h2>
      <span class="chip ${asRole ? "warn" : "host"}">${asRole ? "个人视角回放（已按角色裁剪）" : "主持人视角回放（全量）"}</span>
      <span class="spacer"></span>
      <span class="muted small">最终版本 v${data.final_version}</span>
    </div>
    <p class="muted small" style="margin:6px 0 12px">
      每个版本号对应一次原子提交。事件日志不可变，任意版本的快照都可被确定性重建。
    </p>
    <div id="frame"></div>
  </div>
  <div id="replay-bar">
    <button class="ghost" id="prev">◀</button>
    <input type="range" id="slider" min="0" max="${frames.length - 1}" value="${idx}" />
    <button class="ghost" id="next">▶</button>
    <span class="mono small" id="vlabel">v${idx}</span>
  </div>`);

  app.innerHTML = "";
  app.appendChild(card);

  function renderFrame() {
    const f = frames[idx];
    const wrap = card.querySelector("#frame");
    const changes = f.changes.length
      ? f.changes.map((c) => `<div class="logline">· ${fmtTime(c.at)} ${esc(c.label || c.type)}</div>`).join("")
      : '<div class="logline muted">· （会话已创建，尚未开始）</div>';

    const snap = f.snapshot;
    const s = snap.session;
    const hostStage = (stg) => {
      const choices = (stg.deciding_roles || []).map((rid) => {
        const c = stg.choices?.[rid];
        const text = !stg.reached ? "—"
          : stg.kind === "sealed" && !c ? "⏱ 超时"
          : c ? esc(c.label || c.id) : "待提交";
        return `<li>${esc(roleName(snap, rid))}：${text}</li>`;
      }).join("");
      return `<div class="stage ${stg.kind}">
        <div class="stage-title">${esc(stg.title)} <span class="chip mono">${esc(stg.stage_id)}</span></div>
        <div class="box public">${esc(stg.public)}</div>
        ${choices ? `<ul class="small" style="margin:6px 0">${choices}</ul>` : ""}
      </div>`;
    };
    const roleStage = (stg) => {
      let mine = "";
      if (stg.deciding) {
        mine = stg.timed_out ? "⏱ 超时未提交"
          : stg.my_choice ? esc(stg.my_choice_label || stg.my_choice) : "未提交";
      }
      return `<div class="stage ${stg.kind}">
        <div class="stage-title">${esc(stg.title)}</div>
        <div class="box public">${esc(stg.public)}</div>
        ${stg.private ? `<div class="box private">${esc(stg.private)}</div>` : ""}
        ${mine ? `<div class="small">我的决定：${mine}</div>` : ""}
      </div>`;
    };
    const stagesHtml = (snap.history || [])
      .map((stg) => (snap.kind === "host" ? hostStage(stg) : roleStage(stg)))
      .join("");

    wrap.innerHTML = `
      <h3>v${f.version} 的事件</h3>${changes}
      <h3 style="margin-top:16px">该版本时刻的${snap.kind === "host" ? "全量" : "角色"}快照
        <span class="chip ${s.status === "ended" ? "danger" : s.status === "running" ? "ok" : "warn"}">
          ${esc(statusChip(s.status)[0])}</span></h3>
      ${stagesHtml || '<p class="muted small">无</p>'}`;
    card.querySelector("#vlabel").textContent = `v${f.version} / v${data.final_version}`;
    card.querySelector("#slider").value = idx;
  }

  card.querySelector("#slider").oninput = (e) => { idx = Number(e.target.value); renderFrame(); };
  card.querySelector("#prev").onclick = () => { idx = Math.max(0, idx - 1); renderFrame(); };
  card.querySelector("#next").onclick = () => { idx = Math.min(frames.length - 1, idx + 1); renderFrame(); };
  renderFrame();
}

// boot
route();
