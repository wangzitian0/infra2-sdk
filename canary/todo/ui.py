"""Minimal HTML and interactive UI for Canary Todo."""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Canary Todo — todo.zitian.party</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #0b0f19; color: #e2e8f0; margin: 2rem auto; max-width: 780px; padding: 0 1rem; }
    .card { background: #111827; border: 1px solid #1f2937; border-radius: 10px; padding: 1rem; margin-bottom: 1rem; }
    .row { display: flex; justify-content: space-between; align-items: center; gap: .75rem; padding: .4rem 0; }
    .badge { padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600; background: #334155; }
    .pass { background: #064e3b; color: #34d399; } .fail { background: #7f1d1d; color: #fca5a5; } .unconfigured { background: #78350f; color: #fcd34d; }
    input, button { padding: .45rem .7rem; border-radius: 6px; border: 1px solid #374151; background: #0f172a; color: #f8fafc; cursor: pointer; }
  </style>
</head>
<body>
  <div class="card">
    <div class="row">
      <strong>Canary Todo (todo.zitian.party)</strong>
      <div>${AUTH_BADGE}</div>
    </div>
    <button id="probeBtn" onclick="runInfraProbe()">Run Probes</button>
    <div id="radarGrid"></div>
  </div>
  <div class="card">
    <div class="row">
      <strong>Todos</strong>
      <button id="summaryBtn" onclick="summarizeTodos()">Summarize (LLM)</button>
    </div>
    <div id="todoSummaryBox" style="display:none;font-size:13px;margin:.5rem 0;"></div>
    <form onsubmit="addTodo(event)" class="row">
      <input id="todoInput" placeholder="New todo..." required style="flex:1">
      <button type="submit">Add</button>
    </form>
    <div id="todoList"></div>
  </div>
  <div style="font-size:12px;color:#64748b">infra2 canary • ${GIT_COMMIT_SHA} • https://todo.zitian.party</div>
  <script>
    const esc = (s) => s.replace(/[&<>"']/g, (m) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[m]));
    async function loadTodos() {
      const res = await fetch('/api/todos'); if (!res.ok) return;
      const items = await res.json();
      document.getElementById('todoList').innerHTML = items.map((t) =>
        `<div class="row"><label><input type="checkbox" ${t.completed?'checked':''} onchange="toggleTodo(${t.id},${!t.completed})"> ${esc(t.title)} [${esc(t.capability)}]</label><button onclick="deleteTodo(${t.id})">Delete</button></div>`
      ).join('');
    }
    async function addTodo(e) { e.preventDefault(); const i = document.getElementById('todoInput'); await fetch('/api/todos', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title:i.value,capability:'postgres'})}); i.value=''; loadTodos(); }
    async function toggleTodo(id, completed) { await fetch('/api/todos/'+id, {method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({completed})}); loadTodos(); }
    async function deleteTodo(id) { await fetch('/api/todos/'+id, {method:'DELETE'}); loadTodos(); }
    async function runInfraProbe() {
      const res = await fetch('/api/canary/status'); const data = await res.json();
      document.getElementById('radarGrid').innerHTML = Object.entries(data.checks || {}).map(([k, v]) =>
        `<div class="row"><span>${esc(k)}</span><span class="badge ${esc(v.status)}">${esc(v.status.toUpperCase())} (${v.latency_ms ?? 0}ms)</span></div>`
      ).join('');
    }
    async function summarizeTodos() {
      const box = document.getElementById('todoSummaryBox'); box.style.display='block';
      const res = await fetch('/api/todos/summary', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({limit:20,stream:false})});
      const d = await res.json();
      box.textContent = d.summary ? `${d.summary.headline} — ${d.summary.focus}` : (d.detail || d.error || d.status);
    }
    loadTodos(); runInfraProbe();
  </script>
</body>
</html>
"""
