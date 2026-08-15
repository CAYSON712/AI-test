# -*- coding: utf-8 -*-
"""
自建 trace 平台后端（FastAPI + SQLite）
对应 Langfuse 的核心：上报 trace + 查询 trace 树 + 打分

启动：
  python -m uvicorn app:app --reload --port 8000
  （或 python -m uvicorn trace_platform.app:app --port 8000 从上层目录）

接口：
  POST /api/trace          上报一条 trace（含 observations + scores）
  GET  /api/traces         查询 trace 列表
  GET  /api/traces/{id}    查询单条 trace（带父子树形结构）
  POST /api/scores         给已有 trace 打分
"""
import json
import time
import uuid
from typing import Optional, List

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from db import init_db, get_conn

app = FastAPI(title="AI Trace Platform")

# ---------- 请求模型 ----------

class ObservationIn(BaseModel):
    name: str
    type: str = "SPAN"                # GENERATION / SPAN
    input: Optional[str] = None
    output: Optional[str] = None
    parent_id: Optional[str] = None
    metadata: Optional[dict] = None
    children: List["ObservationIn"] = []   # 嵌套子节点，自动建立父子关系

class ScoreIn(BaseModel):
    name: str
    value: float
    data_type: str = "NUMERIC"
    comment: Optional[str] = None
    observation_id: Optional[str] = None

class TraceIn(BaseModel):
    name: str
    input: Optional[str] = None
    output: Optional[str] = None
    metadata: Optional[dict] = None
    observations: List[ObservationIn] = []
    scores: List[ScoreIn] = []

class ScorePost(BaseModel):
    trace_id: str
    name: str
    value: float
    data_type: str = "NUMERIC"
    comment: Optional[str] = None


# ---------- 上报 ----------

@app.post("/api/trace")
def create_trace(t: TraceIn):
    """上报一条 trace，含 observations 和 scores"""
    conn = get_conn()
    c = conn.cursor()
    trace_id = uuid.uuid4().hex

    c.execute(
        "INSERT INTO traces (id, name, input, output, metadata, timestamp) VALUES (?,?,?,?,?,?)",
        (trace_id, t.name, t.input, t.output,
         json.dumps(t.metadata, ensure_ascii=False) if t.metadata else None,
         time.strftime("%Y-%m-%dT%H:%M:%S"))
    )

    # 递归存 observations，通过 children 自动建立父子关系
    def insert_obs(o: ObservationIn, parent_oid: Optional[str]):
        oid = uuid.uuid4().hex
        c.execute(
            "INSERT INTO observations (id, trace_id, name, type, input, output, parent_id, start_time, end_time, metadata) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (oid, trace_id, o.name, o.type, o.input, o.output, parent_oid,
             time.strftime("%Y-%m-%dT%H:%M:%S"),
             time.strftime("%Y-%m-%dT%H:%M:%S"),
             json.dumps(o.metadata, ensure_ascii=False) if o.metadata else None)
        )
        for child in o.children:
            insert_obs(child, oid)   # 子节点挂在当前节点下

    for o in t.observations:
        insert_obs(o, None)          # 顶层节点挂在 trace 根下

    # 存 scores
    for s in t.scores:
        c.execute(
            "INSERT INTO scores (id, trace_id, observation_id, name, value, data_type, comment) "
            "VALUES (?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, trace_id, s.observation_id, s.name, s.value, s.data_type, s.comment)
        )

    conn.commit()
    conn.close()
    return {"trace_id": trace_id, "status": "ok"}


# ---------- 查询 ----------

@app.get("/api/traces")
def list_traces(limit: int = 1000):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM traces ORDER BY timestamp DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return {"traces": [dict(r) for r in rows]}


@app.get("/api/traces/batches")
def list_batches(limit: int = 1000, q: str = ""):
    """按「批次」分组返回 trace 列表（一次 run_test = 一个批次）。
    批次信息从 traces.metadata 提取（batch_id/dataset/batch_time）。
    q: 可选搜索关键词，按 trace 的 name/input 模糊匹配。
    """
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM traces ORDER BY timestamp DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()

    import json
    # 搜索过滤（若提供 q）
    if q:
        q = q.strip().lower()
        filtered = []
        for r in rows:
            d = dict(r)
            _hay = (d.get("name") or "") + " " + (d.get("input") or "")
            if q in _hay.lower():
                filtered.append(r)
        rows = filtered

    batches = {}  # batch_id -> {batch_id, dataset, batch_time, traces:[]}
    ungrouped = []
    for r in rows:
        d = dict(r)
        meta = {}
        try:
            meta = json.loads(d.get("metadata") or "{}")
        except Exception:
            meta = {}
        # 解析 trace 级别（对齐 Langfuse）：ERROR=真异常红 / WARNING=业务拒绝黄 / 其他正常
        d["_level"] = meta.get("level", "") or ""
        d["_error_summary"] = meta.get("error_summary", "") or ""
        d["_is_error"] = (d["_level"] == "ERROR")
        d["_is_warn"] = (d["_level"] == "WARNING")
        bid = meta.get("batch_id") or ""
        if bid:
            b = batches.setdefault(bid, {
                "batch_id": bid,
                "dataset": meta.get("dataset", ""),
                "batch_time": meta.get("batch_time", ""),
                "system": meta.get("system", ""),
                "req_type": meta.get("req_type", ""),
                "count": 0,
                "traces": [],
            })
            b["traces"].append(d)
            b["count"] += 1
        else:
            ungrouped.append(d)

    # 按 batch_time 降序（新的在前）
    blist = list(batches.values())
    blist.sort(key=lambda b: b["batch_time"] or "", reverse=True)
    # 每个批次内：ERROR 置顶 → WARNING 次之 → 正常；并统计红/黄/正常数 + 通过率
    for b in blist:
        _prio = lambda t: (0 if t.get("_is_error") else 1 if t.get("_is_warn") else 2)
        b["traces"].sort(key=lambda t: (_prio(t), t.get("timestamp") or ""))
        b["error_count"] = sum(1 for t in b["traces"] if t.get("_is_error"))
        b["warn_count"] = sum(1 for t in b["traces"] if t.get("_is_warn"))
        b["pass_count"] = max(0, b["count"] - b["error_count"] - b["warn_count"])
        b["pass_rate"] = round(b["pass_count"] / b["count"], 3) if b["count"] else 0
    # 未分组（旧数据）放到最前
    if ungrouped:
        _prio = lambda t: (0 if t.get("_is_error") else 1 if t.get("_is_warn") else 2)
        ungrouped.sort(key=lambda t: (_prio(t), t.get("timestamp") or ""))
        blist.insert(0, {
            "batch_id": "",
            "dataset": "(未分组)",
            "batch_time": "",
            "system": "",
            "req_type": "",
            "count": len(ungrouped),
            "error_count": sum(1 for t in ungrouped if t.get("_is_error")),
            "warn_count": sum(1 for t in ungrouped if t.get("_is_warn")),
            "pass_count": max(0, len(ungrouped)
                              - sum(1 for t in ungrouped if t.get("_is_error"))
                              - sum(1 for t in ungrouped if t.get("_is_warn"))),
            "pass_rate": round(max(0, len(ungrouped)
                                   - sum(1 for t in ungrouped if t.get("_is_error"))
                                   - sum(1 for t in ungrouped if t.get("_is_warn"))) / len(ungrouped), 3) if ungrouped else 0,
            "traces": ungrouped,
        })
    return {"batches": blist}


@app.get("/api/traces/{trace_id}")
def get_trace(trace_id: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM traces WHERE id = ?", (trace_id,))
    trace = c.fetchone()
    if not trace:
        conn.close()
        raise HTTPException(status_code=404, detail="trace not found")

    # 该 trace 的所有 observations
    c.execute("SELECT * FROM observations WHERE trace_id = ? ORDER BY start_time", (trace_id,))
    obs = [dict(r) for r in c.fetchall()]

    # 该 trace 的 scores
    c.execute("SELECT * FROM scores WHERE trace_id = ?", (trace_id,))
    scores = [dict(r) for r in c.fetchall()]
    conn.close()

    # 还原父子树形结构
    return {
        "trace": dict(trace),
        "tree": _build_tree(obs),
        "scores": scores,
        "total_observations": len(obs),
    }


def _build_tree(obs: List[dict]):
    """把扁平 observation 列表还原成父子嵌套树，并解析节点 level（ERROR/WARNING）"""
    import json as _json
    nodes = {}
    for o in obs:
        node = dict(o, children=[])
        # 解析节点 metadata 的 level（用于前端标红/黄）
        lvl = ""
        try:
            _m = _json.loads(node.get("metadata") or "{}")
            lvl = _m.get("level", "") or ""
        except Exception:
            lvl = ""
        node["_level"] = lvl
        nodes[o["id"]] = node
    roots = []
    for o in obs:
        node = nodes[o["id"]]
        pid = o["parent_id"]
        if pid and pid in nodes:
            nodes[pid]["children"].append(node)
        else:
            roots.append(node)
    return roots


# ---------- 打分 ----------

@app.post("/api/scores")
def add_score(s: ScorePost):
    conn = get_conn()
    c = conn.cursor()
    c.execute(
        "INSERT INTO scores (id, trace_id, name, value, data_type, comment) VALUES (?,?,?,?,?,?)",
        (uuid.uuid4().hex, s.trace_id, s.name, s.value, s.data_type, s.comment)
    )
    conn.commit()
    conn.close()
    return {"status": "ok", "trace_id": s.trace_id}


@app.on_event("startup")
def startup():
    init_db()


# ---------- 前端（单页 HTML） ----------

INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>AI Trace Platform</title>
<style>
  body { font-family: -apple-system, sans-serif; margin: 0; padding: 20px; background: #f5f6fa; }
  h1 { color: #2c3e50; }
  .layout { display: flex; gap: 16px; align-items: flex-start; margin-top: 16px; }
  .sidebar { width: 320px; flex-shrink: 0; background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); max-height: calc(100vh - 120px); overflow-y: auto; }
  .main { flex: 1; min-width: 0; }
  .batch-item { padding: 12px; margin-bottom: 8px; border: 1px solid #eef; border-radius: 8px; cursor: pointer; }
  .batch-item:hover { background: #f0f4ff; }
  .batch-title { font-weight: 600; color: #2c3e50; font-size: 14px; }
  .batch-meta { color: #888; font-size: 12px; margin-top: 4px; }
  #tracePanel { background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  .panel-head { display: flex; justify-content: space-between; align-items: center; border-bottom: 1px solid #eee; padding-bottom: 10px; margin-bottom: 10px; }
  .panel-head h3 { margin: 0; color: #2c3e50; }
  .hint { color: #999; padding: 40px; text-align: center; }
  .trace-row { margin-bottom: 4px; }
  .trace-item { padding: 10px; border: 1px solid #eee; border-radius: 6px; cursor: pointer; display: flex; align-items: center; }
  .trace-item:hover { background: #f0f4ff; }
  .trace-name { font-weight: 600; color: #2c3e50; flex: 1; }
  .trace-time { color: #888; font-size: 12px; margin-left: 8px; }
  .toggle { color: #aaa; margin-left: 8px; }
  .trace-detail { border: 1px solid #eee; border-radius: 6px; padding: 12px; margin-top: 4px; background: #fafbfc; }
  .detail-body { max-height: 400px; overflow-y: auto; }
  .node { padding: 8px; margin: 4px 0; border-left: 3px solid #3498db; background: #fff; }
  .node.GENERATION { border-left-color: #e91e63; }
  .node.SPAN { border-left-color: #9b59b6; }
  .node-name { font-weight: 600; color: #2c3e50; }
  .node-type { font-size: 11px; color: #888; margin-left: 6px; }
  .node-error { border-left-color: #e53935 !important; background: #fff5f5; }
  .node-warn { border-left-color: #fb8c00 !important; background: #fffdf0; }
  .node-lvl-err { display: inline-block; background: #ffebee; color: #c62828; font-size: 11px; padding: 1px 6px; border-radius: 8px; margin-left: 6px; }
  .node-lvl-warn { display: inline-block; background: #fff8e1; color: #ef6c00; font-size: 11px; padding: 1px 6px; border-radius: 8px; margin-left: 6px; }
  .node-detail { font-size: 12px; color: #555; margin-top: 4px; white-space: pre-wrap; word-break: break-all; }
  .node-children { margin-left: 20px; border-left: 1px dashed #ccc; padding-left: 10px; }
  .score { display: inline-block; background: #e8f5e9; color: #2e7d32; padding: 3px 8px; border-radius: 12px; font-size: 12px; margin: 2px; }
  .badge-err { display: inline-block; background: #ffebee; color: #c62828; padding: 1px 6px; border-radius: 10px; font-size: 11px; margin-left: 6px; }
  .badge-warn { display: inline-block; background: #fff8e1; color: #ef6c00; padding: 1px 6px; border-radius: 10px; font-size: 11px; margin-left: 6px; }
  .badge-pass { display: inline-block; background: #e8f5e9; color: #2e7d32; padding: 1px 6px; border-radius: 10px; font-size: 11px; margin-left: 6px; }
  .trace-error { border-color: #ef9a9a !important; background: #fff5f5 !important; }
  .trace-error:hover { background: #ffe0e0 !important; }
  .trace-warn { border-color: #ffe082 !important; background: #fffdf0 !important; }
  .trace-warn:hover { background: #fff8e1 !important; }
  .err-tag { color: #c62828; font-size: 11px; margin-left: 8px; max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .warn-tag { color: #ef6c00; font-size: 11px; margin-left: 8px; max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .txt-err { color: #c62828; }
  .txt-warn { color: #ef6c00; }
  .filter-bar { display: flex; gap: 6px; }
  .search-box { width: 100%; box-sizing: border-box; padding: 8px 12px; border: 1px solid #ddd; border-radius: 6px; font-size: 13px; margin-bottom: 12px; }
  .search-box:focus { outline: none; border-color: #3498db; }
  .filter-btn { border: 1px solid #ddd; background: #fff; color: #555; padding: 4px 12px; border-radius: 16px; cursor: pointer; font-size: 12px; }
  .filter-btn.active { background: #3498db; color: #fff; border-color: #3498db; }
</style>
</head>
<body>
<h1>📊 AI Trace Platform</h1>
<div class="layout">
  <div class="sidebar">
    <h3>测试批次</h3>
    <input id="searchBox" class="search-box" type="text" placeholder="🔍 搜用例ID/能力/输入..." oninput="onSearch(this.value)">
    <div id="batchList">加载中...</div>
  </div>
  <div class="main">
    <div id="tracePanel">
      <div class="hint">← 从左侧选择批次，再点 trace 查看链路</div>
    </div>
  </div>
</div>

<script>
async function loadBatches(q) {
  const url = q ? '/api/traces/batches?q=' + encodeURIComponent(q) : '/api/traces/batches';
  const r = await fetch(url);
  const d = await r.json();
  const c = document.getElementById('batchList');
  if (!d.batches.length) { c.innerHTML = '(暂无 trace)'; return; }
  c.innerHTML = d.batches.map(b => {
    const pr = Math.round((b.pass_rate || 0) * 100);
    return `<div class="batch-item" onclick="showBatch('${b.batch_id}')">
      <div class="batch-title">${b.dataset || '(未命名数据集)'}
        ${(b.error_count||0) ? `<span class="badge-err">${b.error_count}异常</span>` : ''}
        ${(b.warn_count||0) ? `<span class="badge-warn">${b.warn_count}拒绝</span>` : ''}
        <span class="badge-pass">${pr}%通过</span></div>
      <div class="batch-meta">${b.batch_time || ''} · ${b.count} 条${b.system ? ' · ' + b.system : ''}</div>
    </div>`;
  }).join('');
  // 默认展开第一个批次
  if (d.batches.length) showBatch(d.batches[0].batch_id);
}

let currentBatch = null;
let currentFilter = 'all';

async function showBatch(batchId) {
  const r = await fetch('/api/traces/batches');
  const d = await r.json();
  const b = d.batches.find(x => x.batch_id === batchId);
  currentBatch = b;
  const panel = document.getElementById('tracePanel');
  if (!b) { panel.innerHTML = '(批次不存在)'; return; }
  const shown = b.traces.filter(t => {
    if (currentFilter === 'err') return t._is_error;
    if (currentFilter === 'warn') return t._is_warn;
    if (currentFilter === 'ok') return !t._is_error && !t._is_warn;
    return true;
  });
  panel.innerHTML = `<div class="panel-head">
      <div><h3>${b.dataset || '(未分组)'}</h3>
      <span class="batch-meta">${b.batch_time || ''} · ${b.count} 条 ·
        <span class="txt-err">${b.error_count||0}异常</span> ·
        <span class="txt-warn">${b.warn_count||0}拒绝</span></span></div>
      <div class="filter-bar">
        <button class="filter-btn ${currentFilter==='all'?'active':''}" onclick="setFilter('all')">全部</button>
        <button class="filter-btn ${currentFilter==='err'?'active':''}" onclick="setFilter('err')">异常</button>
        <button class="filter-btn ${currentFilter==='warn'?'active':''}" onclick="setFilter('warn')">拒绝</button>
        <button class="filter-btn ${currentFilter==='ok'?'active':''}" onclick="setFilter('ok')">正常</button>
      </div>
    </div>
    <div class="trace-list-inner">
    ${shown.length ? shown.map(t => `
      <div class="trace-row" id="row-${t.id}">
        <div class="trace-item ${t._is_error ? 'trace-error' : t._is_warn ? 'trace-warn' : ''}" onclick="toggleDetail('${t.id}')">
          <span class="trace-name">${t.name}</span>
          ${t._is_error ? `<span class="err-tag">⚠ ${t._error_summary}</span>` : ''}
          ${t._is_warn ? `<span class="warn-tag">◆ ${t._error_summary}</span>` : ''}
          <span class="trace-time">${t.timestamp}</span>
          <span class="toggle">▸</span>
        </div>
        <div class="trace-detail" id="detail-${t.id}" style="display:none"></div>
      </div>`).join('') : '<div class="hint">无符合条件的 trace</div>'}
    </div>`;
}

function setFilter(f) {
  currentFilter = f;
  if (currentBatch) showBatch(currentBatch.batch_id);
}

let searchTimer = null;
function onSearch(v) {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadBatches(v.trim()), 300);
}

async function toggleDetail(id) {
  const box = document.getElementById('detail-' + id);
  const row = document.getElementById('row-' + id);
  if (box.style.display === 'none') {
    // 关闭其他已展开的
    document.querySelectorAll('.trace-detail').forEach(el => { el.style.display = 'none'; });
    document.querySelectorAll('.toggle').forEach(el => { el.textContent = '▸'; });
    const r = await fetch('/api/traces/' + id);
    const d = await r.json();
    box.innerHTML = `<div class="detail-body">
        <div class="node-detail"><b>input:</b> ${d.trace.input || ''}</div>
        ${d.tree.map(renderNode).join('')}
        <h4>打分 (${d.scores.length})</h4>
        <div>${d.scores.map(s => `<span class="score">${s.name} = ${s.value}</span>`).join('')}</div>
      </div>`;
    box.style.display = 'block';
    row.querySelector('.toggle').textContent = '▾';
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } else {
    box.style.display = 'none';
    row.querySelector('.toggle').textContent = '▸';
  }
}

function renderNode(n) {
  const lvl = n._level || '';
  const cls = lvl === 'ERROR' ? ' node-error' : lvl === 'WARNING' ? ' node-warn' : '';
  return `<div class="node ${n.type}${cls}">
    <span class="node-name">${n.name}</span>
    <span class="node-type">[${n.type}]</span>
    ${lvl === 'ERROR' ? '<span class="node-lvl-err">⚠ ERROR</span>' : lvl === 'WARNING' ? '<span class="node-lvl-warn">◆ WARNING</span>' : ''}
    <div class="node-detail"><b>input:</b> ${n.input || ''}</div>
    <div class="node-detail"><b>output:</b> ${n.output || ''}</div>
    ${n.children && n.children.length ? '<div class="node-children">' + n.children.map(renderNode).join('') + '</div>' : ''}
  </div>`;
}

loadBatches();
</script>
</body>
</html>"""


@app.get("/")
def index():
    return HTMLResponse(INDEX_HTML)
