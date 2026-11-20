"""HTML and UI templates for Canary Todo service."""

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Canary Todo — 平台基建校验工具</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body { background-color: #0b0f19; color: #e2e8f0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
    .badge { padding: 4px 10px; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; }
    .badge-pass { background-color: rgba(16, 185, 129, 0.2); color: #10b981; border: 1px solid rgba(16, 185, 129, 0.4); }
    .badge-fail { background-color: rgba(239, 68, 68, 0.2); color: #ef4444; border: 1px solid rgba(239, 68, 68, 0.4); }
    .badge-warn { background-color: rgba(245, 158, 11, 0.2); color: #f59e0b; border: 1px solid rgba(245, 158, 11, 0.4); }
    .badge-wait { background-color: rgba(100, 116, 139, 0.2); color: #94a3b8; border: 1px solid rgba(100, 116, 139, 0.4); }
  </style>
</head>
<body class="min-h-screen p-6 md:p-12">
  <div class="max-w-4xl mx-auto space-y-8">
    
    <!-- Header -->
    <div class="flex flex-col md:flex-row justify-between items-start md:items-center pb-6 border-b border-slate-800 gap-4">
      <div>
        <div class="flex items-center gap-3">
          <span class="text-2xl font-bold tracking-tight text-white">Canary Todo List</span>
          <span class="px-2 py-0.5 text-xs font-mono bg-indigo-500/20 text-indigo-400 rounded border border-indigo-500/30">todo.zitian.party</span>
        </div>
        <p class="text-sm text-slate-400 mt-1">全平台基础设施验收与运行时物理真源校验工具 (Verification Tool for All Infra)</p>
        <div class="mt-2.5 flex items-center gap-2">
          ${AUTH_BADGE}
        </div>
      </div>
      <button onclick="runInfraProbe()" id="probeBtn" class="flex items-center gap-2 px-4 py-2 bg-indigo-600 hover:bg-indigo-500 text-white rounded-lg font-medium shadow-lg transition active:scale-95 text-sm">
        <span>⚡ 触发全链路基建体检</span>
      </button>
    </div>

    <!-- Infrastructure Radar Card -->
    <div class="bg-slate-900/60 border border-slate-800 rounded-xl p-6 shadow-xl backdrop-blur">
      <div class="flex justify-between items-center mb-4">
        <h2 class="text-sm font-semibold uppercase tracking-wider text-slate-400">平台基建物理探针矩阵 (Physical Probes)</h2>
        <span id="lastProbeTime" class="text-xs text-slate-500 font-mono">尚未检测</span>
      </div>
      <div class="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4" id="radarGrid">
        <div class="p-4 bg-slate-950/60 rounded-lg border border-slate-800/80 flex items-center justify-between" id="card-postgres">
          <div>
            <div class="text-xs text-slate-400 font-medium">PostgreSQL (Login + Write/Read)</div>
            <div class="text-xs text-slate-500 mt-0.5" id="latency-postgres">-- ms</div>
          </div>
          <span class="badge badge-wait" id="badge-postgres">WAIT</span>
        </div>

        <div class="p-4 bg-slate-950/60 rounded-lg border border-slate-800/80 flex items-center justify-between" id="card-redis">
          <div>
            <div class="text-xs text-slate-400 font-medium">Redis (AUTH + Write/Read)</div>
            <div class="text-xs text-slate-500 mt-0.5" id="latency-redis">-- ms</div>
          </div>
          <span class="badge badge-wait" id="badge-redis">WAIT</span>
        </div>

        <div class="p-4 bg-slate-950/60 rounded-lg border border-slate-800/80 flex items-center justify-between" id="card-s3">
          <div>
            <div class="text-xs text-slate-400 font-medium">S3 (PUT/GET/DELETE)</div>
            <div class="text-xs text-slate-500 mt-0.5" id="latency-s3">-- ms</div>
          </div>
          <span class="badge badge-wait" id="badge-s3">WAIT</span>
        </div>

        <div class="p-4 bg-slate-950/60 rounded-lg border border-slate-800/80 flex items-center justify-between" id="card-signoz">
          <div>
            <div class="text-xs text-slate-400 font-medium">SigNoz (OTel Collector)</div>
            <div class="text-xs text-slate-500 mt-0.5" id="latency-signoz">-- ms</div>
          </div>
          <span class="badge badge-wait" id="badge-signoz">WAIT</span>
        </div>

        <div class="p-4 bg-slate-950/60 rounded-lg border border-slate-800/80 flex items-center justify-between" id="card-openpanel">
          <div>
            <div class="text-xs text-slate-400 font-medium">OpenPanel (Analytics API)</div>
            <div class="text-xs text-slate-500 mt-0.5" id="latency-openpanel">-- ms</div>
          </div>
          <span class="badge badge-wait" id="badge-openpanel">WAIT</span>
        </div>

        <div class="p-4 bg-slate-950/60 rounded-lg border border-slate-800/80 flex items-center justify-between" id="card-authentik">
          <div>
            <div class="text-xs text-slate-400 font-medium">Authentik (SSO/ForwardAuth)</div>
            <div class="text-xs text-slate-500 mt-0.5" id="latency-authentik">-- ms</div>
          </div>
          <span class="badge badge-wait" id="badge-authentik">WAIT</span>
        </div>
      </div>
    </div>

    <!-- Todo List Interactive Section -->
    <div class="bg-slate-900/60 border border-slate-800 rounded-xl p-6 shadow-xl backdrop-blur space-y-6">
      <div class="flex justify-between items-center">
        <h2 class="text-base font-semibold text-white">基建受验待办事项 (Verified Todos)</h2>
        <div class="flex items-center gap-3">
          <button type="button" onclick="summarizeTodos()" id="summaryBtn" class="px-3 py-1.5 bg-emerald-600/20 hover:bg-emerald-600/30 text-emerald-300 border border-emerald-500/30 rounded-lg text-xs font-medium transition active:scale-95">
            ✨ 生成 AI 摘要
          </button>
          <span id="todoStats" class="text-xs text-slate-400 font-mono">0 completed</span>
        </div>
      </div>

      <div id="todoSummaryBox" class="hidden p-4 bg-slate-950/70 border border-slate-800 rounded-lg text-xs text-slate-300 space-y-1">
        <div class="flex justify-between items-center">
          <span class="font-semibold text-emerald-400" id="todoSummaryHeadline"></span>
          <span class="font-mono text-slate-500" id="todoSummaryMeta"></span>
        </div>
        <div class="text-slate-400" id="todoSummaryFocus"></div>
      </div>

      <!-- Add Todo Form -->
      <form onsubmit="addTodo(event)" class="flex gap-3">
        <input type="text" id="todoInput" placeholder="输入待办项 (例如: 测试 S3 附件上传或 Redis 缓存失效)..." class="flex-1 bg-slate-950/80 border border-slate-800 rounded-lg px-4 py-2.5 text-sm text-white placeholder-slate-500 focus:outline-none focus:border-indigo-500 transition" required>
        <button type="submit" class="px-5 py-2.5 bg-slate-800 hover:bg-slate-700 text-white rounded-lg text-sm font-medium transition active:scale-95 border border-slate-700">添加 Todo</button>
      </form>

      <!-- Todo Items Container -->
      <div class="divide-y divide-slate-800/60 border-t border-slate-800" id="todoList">
        <!-- Rendered via JS -->
      </div>
    </div>

    <!-- Footer -->
    <div class="flex flex-col sm:flex-row justify-between items-center text-xs text-slate-600 gap-2 font-mono">
      <div>infra2 canary acceptance harness • commit ${GIT_COMMIT_SHA}</div>
      <div>https://todo.zitian.party</div>
    </div>
  </div>

  <script>
    let todos = [];

    async function loadTodos() {
      try {
        const res = await fetch('/api/todos');
        if (!res.ok) {
          throw new Error(`HTTP ${res.status}`);
        }
        todos = await res.json();
        renderTodos();
      } catch (err) {
        console.error("Failed to load todos:", err);
      }
    }

    function renderTodos() {
      const container = document.getElementById('todoList');
      container.innerHTML = '';
      let completedCount = 0;

      todos.forEach((item) => {
        if (item.completed) completedCount++;
        const div = document.createElement('div');
        div.className = 'py-3.5 flex items-center justify-between group';
        div.innerHTML = `
          <div class="flex items-center gap-3">
            <input type="checkbox" ${item.completed ? 'checked' : ''} onchange="toggleTodo(${item.id})" class="h-4 w-4 rounded border-slate-700 text-indigo-600 focus:ring-indigo-500 bg-slate-900 cursor-pointer">
            <span class="${item.completed ? 'line-through text-slate-500' : 'text-slate-200'} text-sm">${escapeHtml(item.title)}</span>
            ${item.capability ? `<span class="px-1.5 py-0.5 text-[10px] font-mono rounded bg-slate-800 text-slate-400 border border-slate-700">${item.capability}</span>` : ''}
          </div>
          <button onclick="deleteTodo(${item.id})" class="opacity-0 group-hover:opacity-100 text-slate-500 hover:text-red-400 text-xs transition px-2 py-1">删除</button>
        `;
        container.appendChild(div);
      });

      document.getElementById('todoStats').innerText = `${completedCount} / ${todos.length} 完成`;
    }

    async function addTodo(e) {
      e.preventDefault();
      const input = document.getElementById('todoInput');
      const title = input.value.trim();
      if (!title) return;

      try {
        const res = await fetch('/api/todos', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title, capability: 'postgres+redis' })
        });
        if (res.ok) {
          input.value = '';
          await loadTodos();
        }
      } catch (err) {
        alert("创建失败: " + err);
      }
    }

    async function toggleTodo(id) {
      try {
        const current = todos.find((t) => t.id === id);
        const body = current ? { completed: !current.completed } : {};
        await fetch(`/api/todos/${id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        await loadTodos();
      } catch (err) {
        console.error("Toggle failed:", err);
      }
    }

    async function deleteTodo(id) {
      try {
        await fetch(`/api/todos/${id}`, { method: 'DELETE' });
        await loadTodos();
      } catch (err) {
        console.error("Delete failed:", err);
      }
    }

    async function runInfraProbe() {
      const btn = document.getElementById('probeBtn');
      btn.disabled = true;
      btn.classList.add('opacity-50');

      try {
        const res = await fetch('/api/canary/status');
        const data = await res.json();
        document.getElementById('lastProbeTime').innerText = '最后检测: ' + new Date().toLocaleTimeString();

        for (const [key, result] of Object.entries(data.checks || {})) {
          const badge = document.getElementById(`badge-${key}`);
          const latency = document.getElementById(`latency-${key}`);
          if (badge && latency) {
            latency.innerText = `${result.latency_ms || 0} ms`;
            if (result.status === 'pass') {
              badge.className = 'badge badge-pass';
              badge.innerText = 'PASS';
            } else if (result.status === 'unconfigured') {
              badge.className = 'badge badge-warn';
              badge.innerText = 'UNCONFIGURED';
            } else {
              badge.className = 'badge badge-fail';
              badge.innerText = 'FAIL';
            }
          }
        }
      } catch (err) {
        alert("探针巡检失败: " + err);
      } finally {
        btn.disabled = false;
        btn.classList.remove('opacity-50');
      }
    }

    function escapeHtml(text) {
      return text.replace(/[&<>"']/g, function(m) {
        return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#039;' }[m];
      });
    }

    async function summarizeTodos() {
      const btn = document.getElementById('summaryBtn');
      const box = document.getElementById('todoSummaryBox');
      const headline = document.getElementById('todoSummaryHeadline');
      const meta = document.getElementById('todoSummaryMeta');
      const focus = document.getElementById('todoSummaryFocus');
      btn.disabled = true;
      btn.classList.add('opacity-50');
      try {
        const res = await fetch('/api/todos/summary', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ limit: 20, stream: false }),
        });
        const data = await res.json();
        box.classList.remove('hidden');
        if (data.status === 'pass' && data.summary) {
          headline.innerText = data.summary.headline;
          meta.innerText = `${data.model || 'llm'} · ${data.input_tokens || 0}+${data.output_tokens || 0} tok`;
          focus.innerText = `未完成 ${data.summary.open_count} / 已完成 ${data.summary.completed_count} · 下一步: ${data.summary.focus}`;
        } else if (data.status === 'unconfigured') {
          headline.innerText = 'AI 摘要未配置 (UNCONFIGURED)';
          meta.innerText = data.decoding_version || '';
          focus.innerText = data.detail || 'Missing LLM_API_KEY';
        } else {
          headline.innerText = 'AI 摘要失败';
          meta.innerText = `HTTP ${res.status}`;
          focus.innerText = data.detail || data.error || 'Unknown error';
        }
      } catch (err) {
        box.classList.remove('hidden');
        headline.innerText = 'AI 摘要请求异常';
        meta.innerText = '';
        focus.innerText = String(err);
      } finally {
        btn.disabled = false;
        btn.classList.remove('opacity-50');
      }
    }

    // Auto-run on boot
    loadTodos();
    runInfraProbe();
  </script>
</body>
</html>
"""
