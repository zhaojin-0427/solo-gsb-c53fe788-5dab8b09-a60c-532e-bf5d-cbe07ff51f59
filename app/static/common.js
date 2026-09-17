// 公共工具：API、URL 参数、消息、request-id、本地会话存取、WebSocket 重连
'use strict';

function qs(name) {
  return new URLSearchParams(location.search).get(name);
}
function enc(s) { return encodeURIComponent(s); }
function rid() {
  if (window.crypto && crypto.randomUUID) return crypto.randomUUID();
  return 'r-' + Math.random().toString(36).slice(2) + Date.now().toString(36);
}
function showMsg(el, text, isError) {
  el.textContent = text;
  el.className = 'msg ' + (isError ? 'error' : 'ok');
}
async function api(path, opts = {}) {
  const init = {
    method: opts.method || 'GET',
    headers: { 'Content-Type': 'application/json' },
  };
  if (opts.body !== undefined) init.body = JSON.stringify(opts.body);
  const r = await fetch(path, init);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || ('请求失败 ' + r.status));
  return data;
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
function nl2br(s) { return esc(s).replace(/\n/g, '<br>'); }

// ---- 会话凭证本地存储（便于断线重连/刷新恢复） ----
function storeKey(kind, sessionId) { return `drill:${kind}:${sessionId}`; }
function saveCred(kind, sessionId, cred) {
  localStorage.setItem(storeKey(kind, sessionId), JSON.stringify(cred));
}
function loadCred(kind, sessionId) {
  try { return JSON.parse(localStorage.getItem(storeKey(kind, sessionId)) || 'null'); }
  catch { return null; }
}

// ---- 自动按服务端时间校准时钟偏移 ----
const clock = { offset: 0, serverNow() { return Date.now() + this.offset; } };
async function calibrate(serverTimeSec) {
  if (!serverTimeSec) return;
  clock.offset = serverTimeSec * 1000 - Date.now();
}

// ---- WebSocket 自动重连（指数退避） ----
function connectWS(url, { onEvent, onState }) {
  let ws = null, backoff = 500, closedByUser = false, lastPong = Date.now();
  function loop() {
    ws = new WebSocket(url);
    ws.onopen = () => {
      backoff = 500;
      onState && onState(true);
      ws.send(JSON.stringify({ type: 'sync', after: 0 }));
    };
    ws.onmessage = (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'pong') { lastPong = Date.now(); return; }
      if (msg.server_time) calibrate(msg.server_time);
      onEvent && onEvent(msg);
    };
    ws.onclose = () => {
      onState && onState(false);
      if (!closedByUser) setTimeout(loop, Math.min(backoff, 8000));
      backoff = Math.min(backoff * 2, 8000);
    };
    ws.onerror = () => { try { ws.close(); } catch {} };
  }
  loop();
  const pingTimer = setInterval(() => {
    if (ws && ws.readyState === 1) {
      ws.send(JSON.stringify({ type: 'ping' }));
    }
  }, 20000);
  return {
    close() { closedByUser = true; clearInterval(pingTimer); ws && ws.close(); },
    sync(after) {
      if (ws && ws.readyState === 1) {
        ws.send(JSON.stringify({ type: 'sync', after: after || 0 }));
      }
    }
  };
}
