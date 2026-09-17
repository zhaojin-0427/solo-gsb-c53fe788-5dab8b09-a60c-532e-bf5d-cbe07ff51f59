// 参与者控制台
'use strict';
const sessionId = qs('session');
let participantId = qs('p');
let token = qs('token');
let snapshot = null, conn = null, submitting = false;

// 刷新/断线恢复：URL 无凭证时从 localStorage 读取
if (sessionId) {
  const saved = loadCred('participant', sessionId);
  if (saved) {
    if (!participantId) participantId = saved.participant_id;
    if (!token) token = saved.token;
  }
}
if (sessionId && participantId && token) {
  saveCred('participant', sessionId, {
    participant_id: participantId, token
  });
}

if (!sessionId || !participantId || !token) {
  document.getElementById('title').textContent = '缺少加入凭证';
} else {
  init();
}

async function init() {
  document.getElementById('sid').textContent = sessionId;
  document.getElementById('exportMe').href =
    `/api/sessions/${enc(sessionId)}/export/me.md?participant_id=${enc(participantId)}&token=${enc(token)}`;

  await refresh();
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  conn = connectWS(
    `${proto}://${location.host}/ws/sessions/${enc(sessionId)}`
    + `?kind=participant&participant_id=${enc(participantId)}&token=${enc(token)}`,
    {
      onEvent(m) {
        if (m.type === 'changed' || m.type === 'events' || m.type === 'hello') {
          refresh();
        }
      },
      onState(on) {
        document.getElementById('wsState').innerHTML =
          `<span class="ws-dot ${on ? 'on' : 'off'}"></span>${on ? '实时连接' : '重连中…'}`;
      }
    });
  setInterval(renderTimer, 500);
  setInterval(refresh, 6000);
}

async function refresh() {
  try {
    snapshot = await api(
      `/api/sessions/${enc(sessionId)}/participant-snapshot`
      + `?participant_id=${enc(participantId)}&token=${enc(token)}`);
    await calibrate(snapshot.server_time);
    render();
  } catch (e) {
    document.getElementById('msg').className = 'msg error';
    document.getElementById('msg').textContent = e.message;
  }
}

function statusCn(s) {
  return { pending: '待开始', running: '进行中', ended: '已结束' }[s] || s;
}

function render() {
  const s = snapshot;
  document.getElementById('title').textContent = s.scenario_meta.title;
  document.getElementById('role').textContent = s.role.name;
  document.getElementById('status').innerHTML =
    `<span class="badge ${esc(s.status)}">${statusCn(s.status)}</span>`;
  document.getElementById('version').textContent = s.version;

  document.getElementById('lobby').hidden = s.status !== 'pending';
  document.getElementById('stageCard').hidden = !s.current_stage;
  document.getElementById('endCard').hidden = s.status !== 'ended';

  if (s.current_stage) renderStage(s.current_stage);
  renderHistory(s);
}

function renderStage(c) {
  document.getElementById('stageName').textContent = c.name;
  document.getElementById('brief').innerHTML = nl2br(c.brief || '（无）');
  document.getElementById('content').innerHTML = nl2br(c.content || '（无额外私有信息）');

  const box = document.getElementById('options');
  const mine = c.my_choice;
  box.innerHTML = (c.options || []).map(o => `
    <button class="choice ${mine === o.id ? 'selected' : ''}"
      data-choice="${esc(o.id)}" ${mine ? 'disabled' : ''}>
      <span class="optionlabel">${esc(o.label)}</span>
    </button>`).join('');

  box.querySelectorAll('.choice').forEach(btn => {
    btn.onclick = () => submit(btn.dataset.choice);
  });

  const state = document.getElementById('submitState');
  if (mine) {
    state.textContent = '已提交：' + mine + '（本阶段不可更改，重复提交将被忽略）';
  } else {
    state.textContent = '请尽快做出选择；超时未提交将记为超时。';
  }
  renderTimer();
}

function renderTimer() {
  if (!snapshot || !snapshot.current_stage) {
    document.getElementById('timer').textContent = '';
    return;
  }
  const c = snapshot.current_stage;
  const remain = c.deadline * 1000 - clock.serverNow();
  const el = document.getElementById('timer');
  if (remain <= 0) {
    el.textContent = '等待结算…';
    el.className = 'timer urgent';
  } else {
    el.textContent = Math.ceil(remain / 1000) + ' s';
    el.className = 'timer' + (remain < 10000 ? ' urgent' : '');
  }
}

async function submit(choiceId) {
  if (submitting) return;
  submitting = true;
  const state = document.getElementById('submitState');
  state.className = 'muted';
  state.textContent = '提交中…';
  try {
    const r = await api(
      `/api/sessions/${enc(sessionId)}/choices`, {
        method: 'POST',
        body: {
          participant_id: participantId, token, choice_id: choiceId,
          request_id: rid()
        }
      });
    if (r.duplicate) {
      state.textContent = '服务器记录你此前已提交：' + r.choice_id;
    } else if (!r.accepted) {
      state.className = 'msg error';
      state.textContent = '提交到达时已过截止时间，记为逾期/超时。';
    } else {
      state.className = 'msg ok';
      state.textContent = '已提交，等待其他角色与结算。';
    }
    await refresh();
  } catch (e) {
    state.className = 'msg error';
    state.textContent = e.message + '（可重试，服务器保证不重复计票）';
  } finally {
    submitting = false;
  }
}

function renderHistory(s) {
  const el = document.getElementById('history');
  if (!s.stages.length) { el.textContent = '暂无已结束阶段。'; return; }
  el.className = '';
  el.innerHTML = s.stages.map(st => {
    const label = (st.options || []).find(o => o.id === st.my_choice);
    let mine = st.i_timed_out
      ? '<span class="badge late">超时 / 逾期</span>'
      : st.my_choice
        ? esc(label ? label.label : st.my_choice)
        : '<span class="muted">未提交</span>';
    return `<div style="margin:8px 0">
      <b>${esc(st.name)}</b><br>
      <span class="muted">我的决定：</span>${mine}
    </div>`;
  }).join('');
}
