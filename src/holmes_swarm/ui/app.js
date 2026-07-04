// Holmes Swarm — chat UI client.
// Talks to the FastAPI backend at /chat, /investigations/{id}/stream, /agents, /alerts.

const $ = (sel) => document.querySelector(sel);
const messagesEl = $('#messages');
const composerEl = $('#composer');
const promptEl = $('#prompt');
const sendBtn = $('#send-btn');
const autoSubmitEl = $('#auto-submit');
const tokenEl = $('#token');
const agentsListEl = $('#agents-list');
const eventLogEl = $('#event-log');
const eventFilterEl = $('#event-filter');
const runCardEl = $('#run-card');
const runIdEl = $('#run-id');
const runTargetEl = $('#run-target');
const runScopeEl = $('#run-scope');
const runStateEl = $('#run-state');
const agentsProgressEl = $('#agents-progress');
const agentThoughtsEl = $('#agent-thoughts');
const signalsListEl = $('#signals-list');
const alertsListEl = $('#alerts-list');
const signalCountEl = $('#signal-count');
const alertCountEl = $('#alert-count');

let currentRun = null; // { request_id, agents: {id: {state, signals}} }
let eventSource = null;
let knownAlertIds = new Set();
let activeEventFilter = 'all';
// Cache of full event list so toggling filters doesn't have to re-stream.
let allEventRows = []; // [{evt, li}]

function token() { return tokenEl.value.trim(); }

function authHeaders() {
  return token() ? { 'Authorization': `Bearer ${token()}`, 'Content-Type': 'application/json' } : { 'Content-Type': 'application/json' };
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: authHeaders(),
    body: body ? JSON.stringify(body) : undefined,
  });
  const txt = await res.text();
  let data = null;
  try { data = txt ? JSON.parse(txt) : null; } catch { data = txt; }
  if (!res.ok) {
    const msg = (data && data.detail) || (typeof data === 'string' ? data : res.statusText);
    throw new Error(`${res.status} ${msg}`);
  }
  return data;
}

function appendMessage(role, text, meta) {
  const div = document.createElement('div');
  div.className = `msg ${role}`;
  div.textContent = text;
  if (meta) {
    const m = document.createElement('span');
    m.className = 'meta';
    m.textContent = meta;
    div.appendChild(m);
  }
  messagesEl.appendChild(div);
  messagesEl.scrollTop = messagesEl.scrollHeight;
  return div;
}

function appendEventLog(evt) {
  // Manage the per-agent filter pills: every distinct agent_id seen on the
  // wire gets a chip. Click to scope the log to a single agent.
  if (evt.agent_id && !eventFilterEl.querySelector(`[data-filter="${evt.agent_id}"]`)) {
    const pill = document.createElement('button');
    pill.type = 'button';
    pill.className = 'filter-pill';
    pill.dataset.filter = evt.agent_id;
    pill.textContent = evt.agent_id;
    pill.addEventListener('click', () => setEventFilter(evt.agent_id));
    eventFilterEl.appendChild(pill);
  }
  const li = document.createElement('li');
  li.className = evt.kind;
  li.dataset.agentId = evt.agent_id || '';
  const ts = new Date(evt.at || Date.now()).toLocaleTimeString();
  li.innerHTML = `<span class="ts">${ts}</span><span class="kind">${evt.kind}</span><span>${evt.agent_id || ''} ${summarisePayload(evt.payload)}</span>`;
  allEventRows.unshift({ evt, li });
  while (allEventRows.length > 300) {
    const removed = allEventRows.pop();
    if (removed && removed.li && removed.li.parentNode) {
      removed.li.parentNode.removeChild(removed.li);
    }
  }
  if (rowMatchesFilter(evt)) {
    eventLogEl.prepend(li);
  }
}

function rowMatchesFilter(evt) {
  if (activeEventFilter === 'all') return true;
  return (evt.agent_id || '') === activeEventFilter;
}

function setEventFilter(filter) {
  activeEventFilter = filter;
  for (const btn of eventFilterEl.querySelectorAll('.filter-pill')) {
    btn.classList.toggle('active', btn.dataset.filter === filter);
  }
  // Re-render the visible list from cache (no SSE roundtrip).
  eventLogEl.innerHTML = '';
  for (const row of allEventRows) {
    if (rowMatchesFilter(row.evt)) {
      eventLogEl.appendChild(row.li);
    }
  }
}

// Reset the live event log + filter chips at the start of each investigation.
function resetEventLog() {
  allEventRows = [];
  eventLogEl.innerHTML = '';
  activeEventFilter = 'all';
  for (const btn of eventFilterEl.querySelectorAll('.filter-pill')) {
    if (btn.dataset.filter !== 'all') btn.remove();
  }
  for (const btn of eventFilterEl.querySelectorAll('.filter-pill')) {
    btn.classList.toggle('active', btn.dataset.filter === 'all');
  }
}

function summarisePayload(p) {
  if (!p) return '';
  if (p.signal_type && typeof p.confidence === 'number') {
    return `(${p.signal_type} conf=${p.confidence.toFixed(2)}${p.below_threshold ? ' ⚠ below threshold' : ''})`;
  }
  if (p.error) return `error=${p.error}`;
  if (typeof p === 'object') {
    const keys = Object.keys(p).slice(0, 4);
    return keys.map(k => `${k}=${shortVal(p[k])}`).join(' ');
  }
  return '';
}
function shortVal(v) {
  if (typeof v === 'string') return v.length > 30 ? v.slice(0, 30) + '…' : v;
  return JSON.stringify(v).slice(0, 60);
}

async function loadAgents() {
  try {
    const agents = await api('GET', '/agents');
    agentsListEl.innerHTML = '';
    for (const a of agents) {
      const li = document.createElement('li');
      li.className = 'idle';
      li.dataset.agentId = a.id;
      li.innerHTML = `<span class="dot"></span><span class="name">${a.name}</span><span class="badge">${a.signal_type}</span>`;
      agentsListEl.appendChild(li);
    }
  } catch (e) {
    appendMessage('bot system', `No se pudieron cargar los agentes: ${e.message}`);
  }
}

function setAgentState(agentId, state, signalsCount) {
  const li = agentsListEl.querySelector(`[data-agent-id="${agentId}"]`);
  if (li) {
    li.classList.remove('idle', 'running', 'done', 'failed');
    li.classList.add(state);
  }
  if (currentRun) {
    if (!currentRun.agents[agentId]) currentRun.agents[agentId] = { state: 'idle', signals: [] };
    currentRun.agents[agentId].state = state;
    if (signalsCount != null) currentRun.agents[agentId].signals = signalsCount;
  }
  renderAgentProgress();
}

function setRunState(state) {
  if (!currentRun) return;
  currentRun.state = state;
  runStateEl.textContent = state;
  runStateEl.className = 'state-badge ' + state;
}

function agentPanel(agentId, displayName) {
  let el = agentThoughtsEl.querySelector(`[data-agent-id="${agentId}"]`);
  if (el) return el;
  el = document.createElement('article');
  el.className = 'agent-thought idle';
  el.dataset.agentId = agentId;
  el.innerHTML = `
    <header>
      <span class="dot"></span>
      <span class="name">${displayName || agentId}</span>
      <span class="role">${agentId}</span>
      <span class="state-pill">esperando</span>
    </header>
    <div class="transcript" aria-live="polite"></div>
  `;
  agentThoughtsEl.appendChild(el);
  return el;
}

function setAgentPanelState(agentId, state) {
  const el = agentThoughtsEl.querySelector(`[data-agent-id="${agentId}"]`);
  if (!el) return;
  el.classList.remove('idle', 'running', 'done', 'failed');
  el.classList.add(state);
  const pill = el.querySelector('.state-pill');
  if (pill) pill.textContent = state;
}

function appendAgentThought(agentId, displayName, payload) {
  const el = agentPanel(agentId, displayName);
  const transcript = el.querySelector('.transcript');
  const row = document.createElement('div');
  row.className = 'thought kind-' + (payload.kind || 'note');
  const ts = new Date().toLocaleTimeString();
  const msg = payload.message || '';
  row.innerHTML = `<span class="ts">${ts}</span><span class="bubble">${escapeHtml(msg)}</span>`;
  transcript.appendChild(row);
  transcript.scrollTop = transcript.scrollHeight;
  // Keep the transcript size bounded so the page stays snappy.
  while (transcript.children.length > 200) transcript.removeChild(transcript.firstChild);
}

function appendAgentInput(agentId, displayName, summary) {
  const el = agentPanel(agentId, displayName);
  const transcript = el.querySelector('.transcript');
  const row = document.createElement('div');
  row.className = 'thought kind-input';
  const ts = new Date().toLocaleTimeString();
  const bits = Object.entries(summary || {})
    .filter(([k, v]) => k !== 'entity_id' && v)
    .map(([k, v]) => `<code>${k}</code>: ${escapeHtml(String(v))}`)
    .join(' · ');
  row.innerHTML = `<span class="ts">${ts}</span><span class="bubble"><b>Entrada:</b> ${bits || '(vacía)'}</span>`;
  transcript.appendChild(row);
  transcript.scrollTop = transcript.scrollHeight;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  }[c]));
}

function renderAgentProgress() {
  if (!currentRun) return;
  agentsProgressEl.innerHTML = '';
  const ids = Object.keys(currentRun.agents);
  for (const id of ids) {
    const a = currentRun.agents[id];
    const row = document.createElement('div');
    row.className = 'agent-row ' + a.state;
    const pct = a.state === 'done' ? 100 : a.state === 'failed' ? 100 : a.state === 'running' ? 60 : 0;
    row.innerHTML = `
      <span class="agent-name">${id}</span>
      <div class="bar"><div class="fill" style="width:${pct}%"></div></div>
      <span class="signals-count">${a.signals ?? 0} señales</span>
    `;
    agentsProgressEl.appendChild(row);
  }
}

function addSignal(signal, agentId, replay = false) {
  if (!currentRun) return;
  currentRun.signals = currentRun.signals || [];
  if (currentRun.signals.some(s => s.signal_id === signal.signal_id)) return;
  currentRun.signals.push({ ...signal, agent_id: agentId });
  signalCountEl.textContent = currentRun.signals.length;
  const li = document.createElement('li');
  const belowTag = signal.below_threshold ? '<span class="below">por debajo del umbral</span>' : '';
  li.innerHTML = `<span class="conf">${(signal.confidence * 100).toFixed(0)}%</span> · <b>${agentId}</b> · ${signal.signal_type} ${belowTag}
    <div>${summariseEvidence(signal.evidence)}</div>`;
  signalsListEl.prepend(li);
}

function summariseEvidence(ev) {
  if (!ev) return '';
  const keys = Object.keys(ev).slice(0, 3);
  return keys.map(k => `<code>${k}</code>: ${shortVal(ev[k])}`).join(' · ');
}

async function refreshAlerts() {
  if (!currentRun) return;
  try {
    const alerts = await api('GET', `/alerts?entity_id=${encodeURIComponent(currentRun.target_entity_id)}`);
    let count = 0;
    alertsListEl.innerHTML = '';
    for (const a of alerts) {
      const key = String(a.id);
      if (knownAlertIds.has(key)) continue;
      knownAlertIds.add(key);
      count += 1;
      const li = document.createElement('li');
      li.innerHTML = `<b>${a.contributing_agent_ids.join(', ')}</b> · ${a.summary}`;
      alertsListEl.prepend(li);
    }
    if (count) alertCountEl.textContent = alerts.length;
  } catch (e) { /* ignore */ }
}

function closeStream() {
  if (eventSource) {
    eventSource.close();
    eventSource = null;
  }
}

function openStream(requestId) {
  closeStream();
  const url = `/investigations/${requestId}/stream`;
  // EventSource cannot set headers, so we pass the token via query string fallback.
  // Our auth dependency reads Authorization header — but for SSE from the browser we
  // need a workaround. The simplest is to send the token as `?token=` and have the
  // backend read it; OR rely on cookie auth. For demo simplicity we ship a query
  // string param token support in the auth dependency — see backend notes.
  const fullUrl = token() ? `${url}?token=${encodeURIComponent(token())}` : url;
  eventSource = new EventSource(fullUrl);
  eventSource.onmessage = (e) => {
    try {
      const evt = JSON.parse(e.data);
      handleStreamEvent(evt);
    } catch {}
  };
  eventSource.addEventListener('done', () => closeStream());
  eventSource.onerror = () => { /* let it retry */ };
}

function handleStreamEvent(evt) {
  appendEventLog(evt);
  switch (evt.kind) {
    case 'state_changed':
      setRunState(evt.payload.state);
      break;
    case 'agent_started': {
      const name = evt.payload.agent_name || evt.agent_id;
      setAgentState(evt.agent_id, 'running');
      agentPanel(evt.agent_id, name);
      setAgentPanelState(evt.agent_id, 'running');
      appendAgentThought(evt.agent_id, name, { kind: 'start', message: `▶ ${name} (${evt.payload.signal_type}) — umbral ${(evt.payload.confidence_threshold * 100).toFixed(0)}%` });
      break;
    }
    case 'agent_completed':
      setAgentState(evt.agent_id, 'done', evt.payload.signals_emitted);
      setAgentPanelState(evt.agent_id, 'done');
      appendAgentThought(evt.agent_id, evt.payload.agent_name || evt.agent_id, {
        kind: 'done',
        message: `✔ Hecho — ${evt.payload.signals_emitted} señal(es) emitida(s) (${evt.payload.raw_signals} analizada(s)).`,
      });
      break;
    case 'agent_failed':
      setAgentState(evt.agent_id, 'failed');
      setAgentPanelState(evt.agent_id, 'failed');
      appendAgentThought(evt.agent_id, evt.agent_id, {
        kind: 'fail',
        message: `✖ Falló: ${evt.payload.error || 'unknown'}`,
      });
      break;
    case 'agent_thought':
      if (evt.payload.kind === 'input') {
        appendAgentInput(
          evt.agent_id,
          evt.payload.agent_name || evt.agent_id,
          evt.payload.batch_summary,
        );
      } else {
        appendAgentThought(
          evt.agent_id,
          evt.payload.agent_name || evt.agent_id,
          evt.payload,
        );
      }
      break;
    case 'signal':
    case 'signal_replay':
      addSignal(evt.payload, evt.agent_id, evt.kind === 'signal_replay');
      break;
    case 'completed':
      setRunState('completed');
      runStateEl.textContent = 'completed';
      appendMessage('bot system', `Investigación finalizada: ${evt.payload.summary}`);
      refreshAlerts();
      break;
    case 'failed':
      setRunState('failed');
      appendMessage('bot system', `Investigación falló: ${evt.payload.reason || 'unknown'}`);
      break;
  }
}

function showRun(parsed, requestId, streamUrl) {
  currentRun = {
    request_id: requestId,
    target_entity_id: parsed.target_entity_id,
    parsed,
    state: 'queued',
    agents: {},
    signals: [],
  };
  knownAlertIds = new Set();
  runCardEl.hidden = false;
  runIdEl.textContent = requestId;
  runTargetEl.textContent = parsed.display_name || parsed.target_entity_id;
  const scopeBits = [];
  if (parsed.location) scopeBits.push(`lugar: ${parsed.location}`);
  if (parsed.procedure) scopeBits.push(`proc: ${parsed.procedure}`);
  if (parsed.date_from || parsed.date_to) scopeBits.push(`fechas: ${parsed.date_from || '?'} → ${parsed.date_to || '?'}`);
  runScopeEl.textContent = scopeBits.join(' · ') || '(sin alcance adicional)';
  setRunState('queued');
  agentsProgressEl.innerHTML = '';
  agentThoughtsEl.innerHTML = '';
  resetEventLog();
  signalsListEl.innerHTML = '';
  alertsListEl.innerHTML = '';
  signalCountEl.textContent = '0';
  alertCountEl.textContent = '0';
  // Pre-create empty panels so the user sees all selected agents at once.
  for (const aid of (parsed.agents || [])) {
    setAgentState(aid, 'idle');
    agentPanel(aid, aid);
  }
  if (streamUrl) openStream(requestId);
}

composerEl.addEventListener('submit', async (e) => {
  e.preventDefault();
  const text = promptEl.value.trim();
  if (!text) return;
  sendBtn.disabled = true;
  appendMessage('user', text);
  promptEl.value = '';
  try {
    const data = await api('POST', '/chat', { message: text, auto_submit: autoSubmitEl.checked });
    if (data.parsed) {
      const agentsTxt = (data.parsed.agents || []).join(', ');
      const confTxt = data.parsed.confidence ? ` · confianza parser: ${data.parsed.confidence}` : '';
      const where = data.parsed.location ? `\nLugar: ${data.parsed.location}` : '';
      const summary = `Interpreté:\n  • Sujeto: ${data.parsed.display_name || data.parsed.target_entity_id}${where}\n  • Agentes: ${agentsTxt}${confTxt}\n  • Narrativa: ${data.parsed.narrative || ''}`;
      appendMessage('bot', summary);
    }
    if (data.request_id) {
      showRun(data.parsed, data.request_id, data.stream_url);
      // Periodically poll for alerts while the investigation runs
      const poll = setInterval(() => {
        if (currentRun && currentRun.state === 'completed') { clearInterval(poll); return; }
        refreshAlerts();
      }, 3000);
    } else {
      appendMessage('bot system', data.message || 'Sin solicitud creada.');
    }
  } catch (err) {
    appendMessage('bot system', `Error: ${err.message}`);
  } finally {
    sendBtn.disabled = false;
  }
});

$('#refresh-agents').addEventListener('click', loadAgents);

// Wire the "Todos" filter pill (the others are added dynamically).
eventFilterEl.querySelector('[data-filter="all"]').addEventListener('click', () => setEventFilter('all'));

// Init
loadAgents();
appendMessage('bot system', 'Listo. Escribe una investigación en lenguaje natural. Ej: "Encuentra movimientos alarmantes del Dr. Ciro Alfonso Gómez Meisel de la Clínica Meisel SAS en la SUBRED INTEGRADA DE SERVICIOS DE SALUD Norte y Sur".');