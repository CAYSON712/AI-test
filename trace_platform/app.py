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
def list_traces(limit: int = 20):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM traces ORDER BY timestamp DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()
    return {"traces": [dict(r) for r in rows]}


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
    """把扁平 observation 列表还原成父子嵌套树"""
    nodes = {o["id"]: dict(o, children=[]) for o in obs}
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
  .trace-list { background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  .trace-item { padding: 10px; border-bottom: 1px solid #eee; cursor: pointer; }
  .trace-item:hover { background: #f0f4ff; }
  .trace-item:last-child { border-bottom: none; }
  .trace-name { font-weight: 600; color: #2c3e50; }
  .trace-time { color: #888; font-size: 12px; margin-left: 8px; }
  #detail { margin-top: 20px; background: #fff; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.06); }
  .node { padding: 8px; margin: 4px 0; border-left: 3px solid #3498db; background: #fafbfc; }
  .node.GENERATION { border-left-color: #e91e63; }
  .node.SPAN { border-left-color: #9b59b6; }
  .node-name { font-weight: 600; color: #2c3e50; }
  .node-type { font-size: 11px; color: #888; margin-left: 6px; }
  .node-detail { font-size: 12px; color: #555; margin-top: 4px; white-space: pre-wrap; word-break: break-all; }
  .node-children { margin-left: 20px; border-left: 1px dashed #ccc; padding-left: 10px; }
  .score { display: inline-block; background: #e8f5e9; color: #2e7d32; padding: 3px 8px; border-radius: 12px; font-size: 12px; margin: 2px; }
</style>
</head>
<body>
<h1>📊 AI Trace Platform</h1>
<div class="trace-list">
  <h3>Trace 列表</h3>
  <div id="traceList">加载中...</div>
</div>
<div id="detail"></div>

<script>
async function loadList() {
  const r = await fetch('/api/traces');
  const d = await r.json();
  const c = document.getElementById('traceList');
  if (!d.traces.length) { c.innerHTML = '(暂无 trace)'; return; }
  c.innerHTML = d.traces.map(t => `
    <div class="trace-item" onclick="loadDetail('${t.id}')">
      <span class="trace-name">${t.name}</span>
      <span class="trace-time">${t.timestamp}</span>
    </div>`).join('');
}

function renderNode(n) {
  return `<div class="node ${n.type}">
    <span class="node-name">${n.name}</span>
    <span class="node-type">[${n.type}]</span>
    <div class="node-detail"><b>input:</b> ${n.input || ''}</div>
    <div class="node-detail"><b>output:</b> ${n.output || ''}</div>
    ${n.children && n.children.length ? '<div class="node-children">' + n.children.map(renderNode).join('') + '</div>' : ''}
  </div>`;
}

async function loadDetail(id) {
  const r = await fetch('/api/traces/' + id);
  const d = await r.json();
  const c = document.getElementById('detail');
  c.innerHTML = `<h3>${d.trace.name}</h3>
    <div style="color:#888;font-size:12px">${d.trace.timestamp} · ${d.total_observations} nodes</div>
    <div>${d.tree.map(renderNode).join('')}</div>
    <h4>打分 (${d.scores.length})</h4>
    <div>${d.scores.map(s => `<span class="score">${s.name} = ${s.value}</span>`).join('')}</div>`;
}

loadList();
</script>
</body>
</html>"""


@app.get("/")
def index():
    return HTMLResponse(INDEX_HTML)
