// 主持人控制台
'use strict';
const sessionId = qs('session');
let token = qs('token');
const msg = document.getElementById('msg');
let snapshot = null, timerUI = null, conn = null;

// URL 没带 token 时尝试本地存储
if (!token) {
  const saved = loadCred('host', sessionId);
  if (saved && saved.token) token = saved.token;
}
if (sessionId && token) saveCred('host', sessionId, { token });

if (!sessionId || !token) {
  document.getElementById('title').textContent = '缺少主持人链接参数';
} else {
  init();
}

async function init() {
  document.getElementById('sid').textContent = sessionId;
  document.getElementById('joinHint').innerHTML =
    `参与者加入地址：<code>${location.origin}/join.html?session=${enc(sessionId)}</code>`;

  document.getElementById('startBtn').onclick = async () => {
    try { await api(`/api/sessions/${enc(sessionId)}/start`, {
      method: 'POST', body: { token } }); refresh();
    } catch (e) { showMsg(msg, e.message, true); }
  };
  document.getElementById('settleBtn').onclick = async () => {
    try { await api(`/api/sessions/${enc(sessionId)}/settle`, {
      method: 'POST', body: { token } }); refresh();
    } catch (e) { showMsg(msg, e.message, true); }
  };
  document.getElementById('endBtn').onclick = async () => {
    if (!confirm('确定提前终止本次演练？')) return;
    try { await api(`/api/sessions/${enc(sessionId)}/end`, {
      method: 'POST', body: { token } }); refresh();
    } catch (e) { showMsg(msg, e.message, true); }
  };
  document.getElementById('exportHost').href =
    `/api/sessions/${enc(sessionId)}/export/host.md?token=${enc(token)}`;
  document.getElementById('replayLink').href =
    `/api/sessions/${enc(sessionId)}/replay?token=${enc(token)}`;

  await refresh();
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  conn = connectWS(
    `${proto}://${location.host}/ws/sessions/${enc(sessionId)}?kind=host&token=${enc(token)}`,
    {
      onEvent(m) {
        if (m.type === 'changed') refresh();
      },
      onState(on) {
        document.getElementById('wsState').innerHTML =
          `<span class="ws-dot ${on ? 'on' : 'off'}"></span>${on ? '实时连接' : '重连中…'}`;
      }
    });
  // 每秒刷新倒计时；每 5 秒兜底拉取
  timerUI = setInterval(renderTimer, 1000);
  setInterval(refresh, 5000);
}

async function refresh() {
  if (!sessionId || !token) return;
  try {
    snapshot = await api(
      `/api/sessions/${enc(sessionId)}/host-snapshot?token=${enc(token)}`);
    await calibrate(snapshot.server_time);
    render();
    msg.textContent = '';
  } catch (e) {
    showMsg(msg, e.message, true);
  }
}

function statusBadge(s) {
  const cn = { pending: '待开始', running: '进行中', ended: '已结束' };
  return `<span class="badge ${esc(s)}">${cn[s] || s}</span>`;
}

function render() {
  const s = snapshot;
  document.getElementById('title').textContent = s.scenario.title;
  document.getElementById('status').innerHTML = statusBadge(s.status);
  document.getElementById('version').textContent = s.version;

  // 参与者
  const proles = {};
  Object.values(s.participants).forEach(p => { proles[p.role_id] = p.name; });
  document.getElementById('participants').innerHTML =
    `<table><tr><th>角色</th><th>姓名</th><th>状态</th></tr>` +
    s.scenario.roles.map(r => {
      const name = proles[r.id] ? esc(proles[r.id]) : '<span class="muted">未加入</span>';
      return `<tr><td>${esc(r.name)}</td><td>${name}</td>`
        + `<td>${proles[r.id] ? '✓' : '—'}</td></tr>`;
    }).join('') + '</table>';

  renderCurrent();
  renderHistory();

  document.getElementById('startBtn').disabled =
    (s.status === 'ended' || !!s.current_stage);
  document.getElementById('settleBtn').disabled = !s.current_stage;
  document.getElementById('endBtn').disabled = s.status === 'ended';
}

function optLabel(stageRoleView, cid) {
  const o = (stageRoleView.options || []).find(x => x.id === cid);
  return o ? o.label : cid;
}

function renderCurrent() {
  const s = snapshot;
  const el = document.getElementById('currentStage');
  if (!s.current_stage) {
    el.innerHTML = s.status === 'pending'
      ? '<p class="muted">演练待开始。所有参与者加入后，点击上方按钮开始第一阶段。</p>'
      : s.status === 'ended'
        ? '<p class="muted">演练已结束。可导出全量记录与回放。</p>'
        : '<p class="muted">阶段之间，点击“开始 / 进入下一阶段”。</p>';
    return;
  }
  const c = s.current_stage;
  const rows = s.scenario.roles.map(r => {
    const view = c.roles[r.id];
    const chosen = c.live_choices[r.id];
    const late = c.late_attempts[r.id];
    return `<tr>
      <td><b>${esc(r.name)}</b></td>
      <td>${chosen ? '✓ ' + esc(optLabel(view, chosen))
        : late ? `<span class="badge late">逾期: ${esc(optLabel(view, late))}</span>`
        : '<span class="muted">等待中…</span>'}</td>
    </tr>`;
  }).join('');
  el.innerHTML = `
    <h3>${esc(c.name)}</h3>
    <table><tr><th>角色</th><th>当前选择</th></tr>${rows}</table>
    <p class="kpi">截止：${new Date(c.deadline * 1000).toLocaleTimeString()}</p>`;
}

function renderTimer() {
  const el = document.getElementById('curTimer');
  const s = snapshot;
  if (!s || !s.current_stage) { el.textContent = ''; return; }
  const remain = s.current_stage.deadline * 1000 - clock.serverNow();
  if (remain <= 0) {
    el.textContent = '结算中…';
    el.className = 'timer urgent';
  } else {
    el.textContent = ' ' + Math.ceil(remain / 1000) + ' s';
    el.className = 'timer' + (remain < 10000 ? ' urgent' : '');
  }
}

function renderHistory() {
  const s = snapshot;
  if (!s.stages.length) {
    document.getElementById('history').innerHTML = '<p class="muted">暂无已结算阶段。</p>';
    return;
  }
  document.getElementById('history').innerHTML = s.stages.map(st => {
    const rows = s.scenario.roles.map(r => {
      const cid = st.choices[r.id];
      let text;
      if (cid === '__timeout__') text = '<span class="badge late">逾期作废</span>';
      else if (cid) text = esc(optLabel(st.roles[r.id], cid));
      else text = '<span class="badge late">超时未提交</span>';
      return `<tr><td>${esc(r.name)}</td><td>${text}</td></tr>`;
    }).join('');
    const nxt = st.next_stage
      ? esc(s.scenario.stages[st.next_stage]?.name || st.next_stage)
      : '结束演练';
    return `<details><summary><b>${esc(st.name)}</b>
        <span class="muted">→ ${nxt}</span></summary>
      <table><tr><th>角色</th><th>最终选择</th></tr>${rows}</table>
      <p class="kpi">开始 ${new Date(st.started_at*1000).toLocaleTimeString()} ·
        结算 ${new Date(st.settled_at*1000).toLocaleTimeString()}</p>
    </details>`;
  }).join('');
}
