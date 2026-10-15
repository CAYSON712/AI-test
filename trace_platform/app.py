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
    metadata: Optional[dict] = None

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
            "INSERT INTO scores (id, trace_id, observation_id, name, value, data_type, comment, metadata) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, trace_id, s.observation_id, s.name, s.value, s.data_type, s.comment,
             json.dumps(s.metadata, ensure_ascii=False) if s.metadata else None)
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
def list_batches(q: str = ""):
    """返回【批次摘要】列表（不含 trace 明细），供左侧批次列表展示。

    性能（2026-10-14）：原实现把每条 trace 明细都塞进返回体，一次拉几千条
    → 前端卡顿且超过 1000 条看不见。现改为：只做批次聚合（遍历全部行、只取
    轻量字段），明细由 /api/traces/batch/{batch_id} 按批次分页获取（点开才拉）。
    q: 可选搜索关键词，按 trace 的 name/input 模糊匹配（命中即该批次入选）。
    """
    conn = get_conn()
    c = conn.cursor()
    # 只取分组必需字段（不含 output 等大字段）；不再限制行数，保证批次统计准确
    c.execute("SELECT id, name, input, metadata, timestamp FROM traces")
    rows = c.fetchall()
    conn.close()

    import json
    if q:
        q = q.strip().lower()
        rows = [r for r in rows
                if q in ((r["name"] or "") + " " + (r["input"] or "")).lower()]

    batches = {}
    ungrouped = 0
    for r in rows:
        d = dict(r)
        try:
            meta = json.loads(d.get("metadata") or "{}")
        except Exception:
            meta = {}
        level = meta.get("level", "") or ""
        is_err = (level == "ERROR")
        is_warn = (level == "WARNING")
        bid = meta.get("batch_id") or ""
        if bid:
            b = batches.setdefault(bid, {
                "batch_id": bid,
                "dataset": meta.get("dataset", ""),
                "batch_time": meta.get("batch_time", ""),
                "system": meta.get("system", ""),
                "req_type": meta.get("req_type", ""),
                "count": 0, "error_count": 0, "warn_count": 0,
            })
            b["count"] += 1
            b["error_count"] += 1 if is_err else 0
            b["warn_count"] += 1 if is_warn else 0
            b["_ts"] = max(b.get("_ts", ""), d.get("timestamp") or "")
        else:
            ungrouped += 1

    blist = list(batches.values())
    blist.sort(key=lambda b: (b.get("batch_time") or "", b.get("_ts") or ""), reverse=True)
    for b in blist:
        b["pass_count"] = max(0, b["count"] - b["error_count"] - b["warn_count"])
        b["pass_rate"] = round(b["pass_count"] / b["count"], 3) if b["count"] else 0
        b.pop("_ts", None)
    # 未分组仅在真有「无批次」数据时才展示，避免出现空的 (未分组) 条目
    if ungrouped:
        blist.append({
            "batch_id": "", "dataset": "(未分组)", "batch_time": "",
            "system": "", "req_type": "", "count": ungrouped,
            "error_count": 0, "warn_count": 0, "pass_count": ungrouped,
            "pass_rate": 1.0,
        })
    return {"batches": blist}


@app.get("/api/traces/batch/{batch_id}")
def list_batch_traces(batch_id: str, offset: int = 0, limit: int = 200,
                      filter: str = "all"):
    """按批次【分页】返回 trace 明细（点开批次才拉，每页 ≤200 条）。

    filter: all / err / warn / ok —— 在服务端过滤，保证分页计数正确。
    """
    import json
    conn = get_conn()
    c = conn.cursor()
    if batch_id:
        # SQLite 里用 LIKE 粗筛该批次，再在 Python 里精确比对 batch_id
        c.execute("SELECT * FROM traces WHERE metadata LIKE ? "
                  "ORDER BY timestamp DESC", (f"%{batch_id}%",))
        rows_all = [dict(r) for r in c.fetchall()
                    if json.loads((r["metadata"] or "{}") or "{}").get("batch_id") == batch_id]
    else:
        c.execute("SELECT * FROM traces ORDER BY timestamp DESC")
        rows_all = [dict(r) for r in c.fetchall()
                    if not json.loads((r["metadata"] or "{}") or "{}").get("batch_id")]
    conn.close()

    # 解析级别 + 排序（ERROR 置顶 → WARNING → 正常）
    for d in rows_all:
        try:
            meta = json.loads(d.get("metadata") or "{}")
        except Exception:
            meta = {}
        d["_level"] = meta.get("level", "") or ""
        d["_error_summary"] = meta.get("error_summary", "") or ""
        d["_is_error"] = (d["_level"] == "ERROR")
        d["_is_warn"] = (d["_level"] == "WARNING")
    _prio = lambda t: (0 if t.get("_is_error") else 1 if t.get("_is_warn") else 2)
    rows_all.sort(key=lambda t: (_prio(t), t.get("timestamp") or ""))

    if filter == "err":
        rows_all = [t for t in rows_all if t["_is_error"]]
    elif filter == "warn":
        rows_all = [t for t in rows_all if t["_is_warn"]]
    elif filter == "ok":
        rows_all = [t for t in rows_all if not t["_is_error"] and not t["_is_warn"]]

    total = len(rows_all)
    limit = max(1, min(int(limit or 200), 200))     # 硬上限 200 条/页
    offset = max(0, int(offset or 0))
    page = rows_all[offset:offset + limit]
    return {"traces": page, "total": total, "offset": offset,
            "limit": limit, "has_more": offset + len(page) < total}


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

    # 把 value 按 data_type 还原为原类型；解析 scores 的 metadata（评分来源/理由）
    for sc in scores:
        v, dt = sc.get("value"), sc.get("data_type", "NUMERIC")
        try:
            if dt == "NUMERIC":
                sc["value"] = float(v) if v is not None else None
            elif dt == "BOOLEAN":
                sc["value"] = (str(v).lower() in ("true", "1", "yes"))
        except Exception:
            pass
        # 解析 scores 的 metadata（评分来源/理由），前端可直接使用
        try:
            sc["metadata"] = json.loads(sc.get("metadata") or "{}") or {}
        except Exception:
            sc["metadata"] = {}

    # 还原父子树形结构
    tree = _build_tree(obs)
    # 计算瀑布图时间窗口：优先用真实绝对时间；粗糙/缺失时用合成时间轴兜底。
    # 若所有节点耗时都为 0（秒级数据 / 无 latency_ms），真实时间无法表达条宽 → 用合成。
    def _collect(ns):
        for n in ns:
            yield n
            yield from _collect(n.get("children", []))
    _nodes = list(_collect(tree))
    _has_dur = any(n.get("_duration_ms", 0) > 0 for n in _nodes)
    _base, _total = _compute_waterfall_window(obs)
    _synthetic = (_total <= 0) or (not _has_dur)
    if _synthetic:
        _base, _total = _synthesize_timeline(tree)
    return {
        "trace": dict(trace),
        "tree": tree,
        "scores": scores,
        "total_observations": len(obs),
        "trace_start_ms": _base,          # trace 内最早观测点的相对时间偏移（通常 0）
        "trace_duration_ms": _total,      # trace 总跨度（最晚结束 - 最早开始）
        "synthetic": _synthetic,          # 是否用合成时间轴（真实时间缺失时兜底）
    }


def _parse_ts(ts):
    """把 start_time/end_time（ISO 字符串）转成可比较的毫秒时间戳；失败返回 None"""
    import datetime as _dt
    if not ts:
        return None
    try:
        s = str(ts).strip()
        # 兼容带 T 的 ISO 与空格分隔
        if "T" in s:
            s = s.replace("T", " ")
        dt = _dt.datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _node_duration_ms(node):
    """优先用 metadata.latency_ms；否则用 end_time - start_time 估算"""
    ms = node.get("_latency_ms")
    if ms:
        return float(ms)
    t0 = _parse_ts(node.get("start_time"))
    t1 = _parse_ts(node.get("end_time"))
    if t0 is not None and t1 is not None and t1 >= t0:
        return float(t1 - t0)
    return 0.0


def _compute_waterfall_window(obs):
    """返回 (start_offset_ms, total_duration_ms)：
    以所有节点最早开始时间为 0，总跨度为最晚结束-最早开始。
    无有效时间数据时返回 (0, 0)。"""
    starts, ends = [], []
    for o in obs:
        t0 = _parse_ts(o.get("start_time"))
        t1 = _parse_ts(o.get("end_time"))
        if t0 is not None:
            starts.append(t0)
        if t1 is not None:
            ends.append(t1)
    if not starts:
        return (0, 0)
    base = min(starts)
    end = max(ends) if ends else base
    return (0, max(0, end - base))


def _build_tree(obs: List[dict]):
    """把扁平 observation 列表还原成父子嵌套树，并解析节点 level/latency 等展示字段。"""
    import json as _json
    nodes = {}
    for o in obs:
        node = dict(o, children=[])
        # 解析节点 metadata：level（红/黄标）+ latency + model/tokens 等展示字段
        _m = {}
        try:
            _m = _json.loads(node.get("metadata") or "{}") or {}
        except Exception:
            _m = {}
        lvl = _m.get("level", "") or ""
        node["_level"] = lvl
        node["_latency_ms"] = _m.get("latency_ms", "") or ""
        # 提升常用展示字段到顶层，前端无需再解 metadata
        for k in ("model", "provider", "tool", "tokens_in", "tokens_out",
                  "skill", "skill_name", "scenario", "cost"):
            if _m.get(k) is not None:
                node["_" + k] = _m[k]
        # 先算耗时
        node["_duration_ms"] = _node_duration_ms(node)
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


def _synthesize_timeline(roots):
    """合成时间轴：当真实时间缺失（秒级/无 latency）时，按树的先序顺序
    依次排布每个节点，offset 顺延累积；节点宽度用 latency_ms，无则给默认 300ms。
    返回 (start_offset_ms, total_duration_ms)。"""
    _DEFAULT_DUR = 300.0
    _state = {"cursor": 0}
    def walk(node):
        # 有真实耗时则用，否则用默认宽度
        dur = node.get("_duration_ms") or 0.0
        if dur <= 0:
            dur = _DEFAULT_DUR
        # 有真实 offset（已解析到毫秒级绝对时间）则跳过合成——但此处进入即说明时间缺失
        node["_offset_ms"] = int(_state["cursor"])
        node["_duration_ms"] = dur
        _state["cursor"] += dur
        for ch in node.get("children", []):
            walk(ch)
    for r in roots:
        walk(r)
    return (0, _state["cursor"])


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
  .score { display: inline-block; background: #e8f5e9; color: #2e7d32; padding: 3px 8px; border-radius: 12px; font-size: 12px; margin: 2px; cursor: help; }
  .score .via { font-size: 9px; border-radius: 6px; padding: 0 4px; margin-left: 4px; vertical-align: 1px; }
  .score .via-rule { background: #e3f2fd; color: #1565c0; }
  .score .via-llm { background: #f3e5f5; color: #6a1b9a; }
  .score .via-default { background: #eceff1; color: #78909c; }
  .score-nojudge { background: #eceff1 !important; color: #78909c !important; font-style: italic; }
  /* ===== 瀑布图 ===== */
  .wf-head { font-weight: 600; color: #2c3e50; margin-bottom: 4px; }
  .wf-head .batch-meta { font-weight: normal; }
  .wf-scale { position: relative; height: 18px; margin: 6px 0 2px; border-bottom: 1px solid #eee; }
  .wf-scale-inner { position: absolute; left: 0; right: 0; top: 0; bottom: 0; }
  .wf-tick { position: absolute; top: 0; height: 100%; border-left: 1px solid #e0e0e0; }
  .wf-tick span { position: absolute; top: 2px; left: 3px; font-size: 10px; color: #999; white-space: nowrap; }
  .wf-timeline { position: relative; }
  .wf-row { display: flex; flex-wrap: wrap; align-items: center; min-height: 30px; border-bottom: 1px dashed #f0f0f0; }
  /* 直接显示 input/output 文本 */
  .wf-io { width: 100%; padding: 2px 8px 6px 0; margin-left: 280px; box-sizing: border-box; }
  .wf-io-in, .wf-io-out { font-size: 12px; line-height: 1.6; margin-top: 2px; }
  .wf-io-in b, .wf-io-out b { display: inline-block; min-width: 30px; color: #fff; border-radius: 3px; padding: 0 5px; margin-right: 6px; font-size: 10px; font-weight: 600; }
  .wf-io-in b { background: #42a5f5; }
  .wf-io-out b { background: #66bb6a; }
  .wf-io-txt { color: #333; white-space: pre-wrap; word-break: break-all; font-family: Consolas, Menlo, monospace; font-size: 11px; }
  .wf-meta { width: 280px; flex-shrink: 0; padding: 2px 8px 2px 0; overflow: hidden; }
  .wf-meta .node-type { font-size: 10px; color: #aaa; margin-left: 4px; }
  .wf-meta-bits { font-size: 11px; color: #888; margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .wf-dur { font-size: 11px; color: #777; background: #f0f1f4; border-radius: 8px; padding: 0 6px; margin-left: 6px; font-variant-numeric: tabular-nums; }
  .wf-track { flex: 1; position: relative; height: 24px; background: repeating-linear-gradient(90deg, #fafbfc, #fafbfc calc(25% - 1px), #f2f3f5 25%); border-radius: 4px; }
  .wf-bar { position: absolute; top: 4px; height: 16px; border-radius: 3px; background: #9b59b6; opacity: 0.9; cursor: pointer; }
  .wf-bar:hover { opacity: 1; outline: 1px solid #000; }
  .wf-row.gen .wf-bar { background: #e91e63; }
  .wf-row.st-err .wf-bar { background: #e53935; }
  .wf-row.st-warn .wf-bar { background: #fb8c00; }
  .wf-row.st-err .wf-meta { background: #fff5f5; border-left: 3px solid #e53935; }
  .wf-row.st-warn .wf-meta { background: #fffdf0; border-left: 3px solid #fb8c00; }
  .wf-empty { padding: 14px; margin: 4px 0 10px; background: #fff8e1; color: #8d6e63;
    border: 1px dashed #ffb300; border-radius: 6px; font-size: 12px; }
  .wf-legend { margin: 10px 0 4px; font-size: 11px; color: #666; }
  .lg { display: inline-block; padding: 1px 8px; border-radius: 8px; margin-right: 8px; background: #eee; }
  .lg-gen { background: #fce4ec; color: #ad1457; }
  .lg-span { background: #ede7f6; color: #6a1b9a; }
  .lg-err { background: #ffebee; color: #c62828; }
  .lg-warn { background: #fff8e1; color: #ef6c00; }
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
  .filter-btn:disabled { opacity: .45; cursor: not-allowed; }
  .pager { display: flex; align-items: center; gap: 8px; justify-content: center;
           flex-wrap: wrap; padding: 10px 0; border-top: 1px solid #eee; margin-top: 8px; }
  .page-btn { min-width: 30px; padding: 4px 8px; }
  .page-gap { color: #999; padding: 0 2px; }
  .pager-jump { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; color: #555; }
  .jump-input { width: 56px; padding: 4px 6px; border: 1px solid #ddd; border-radius: 6px;
                font-size: 12px; text-align: center; }
  .jump-input:focus { outline: none; border-color: #3498db; }
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
  window._batches = d.batches;      // 缓存批次摘要，showBatch 取用（不再全量拉明细）
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
let currentOffset = 0;
const PAGE_SIZE = 200;          // 每页最多 200 条（服务端也会限制 ≤200）

async function showBatch(batchId, offset) {
  if (offset === undefined) offset = 0;
  currentOffset = offset;
  // 从左侧已加载的批次摘要里取当前批次（不再全量拉明细）
  const b = (window._batches || []).find(x => x.batch_id === batchId);
  currentBatch = b || { batch_id: batchId, dataset: '(未分组)', count: 0 };
  const panel = document.getElementById('tracePanel');
  panel.innerHTML = '<div class="hint">加载中…</div>';
  const url = `/api/traces/batch/${encodeURIComponent(batchId)}`
            + `?offset=${offset}&limit=${PAGE_SIZE}&filter=${currentFilter}`;
  const r = await fetch(url);
  const d = await r.json();
  const shown = d.traces || [];
  const total = d.total || 0;
  const from = total ? offset + 1 : 0;
  const to = offset + shown.length;
  panel.innerHTML = `<div class="panel-head">
      <div><h3>${currentBatch.dataset || '(未分组)'}</h3>
      <span class="batch-meta">${currentBatch.batch_time || ''} · 共 ${total} 条 ·
        <span class="txt-err">${currentBatch.error_count||0}异常</span> ·
        <span class="txt-warn">${currentBatch.warn_count||0}拒绝</span></span></div>
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
    </div>
    <div class="pager" onclick="event.stopPropagation()">
      <button class="filter-btn" ${offset<=0?'disabled':''}
        onclick="showBatch('${batchId}', ${Math.max(0, offset-PAGE_SIZE)})">← 上一页</button>
      ${renderPageButtons(batchId, offset, total)}
      <button class="filter-btn" ${d.has_more?'':'disabled'}
        onclick="showBatch('${batchId}', ${offset+PAGE_SIZE})">下一页 →</button>
      <span class="pager-jump" onclick="event.stopPropagation()">
        第 <input id="jumpPageInput" class="jump-input" type="text"
                 autocomplete="off"
                 data-batch="${batchId}" data-total="${total}"
                 placeholder="${total? Math.floor(offset/PAGE_SIZE)+1 : 1}">
        页
        <button class="filter-btn" onclick="event.stopPropagation();jumpPage('${batchId}', ${total})">跳转</button>
      </span>
      <span class="batch-meta">第 ${from}-${to} 条 / 共 ${total} 条
        · 共 ${Math.max(1, Math.ceil(total/PAGE_SIZE))} 页</span>
    </div>`;
}

// 页码按钮：当前页高亮；首尾常显，中间用省略号收敛，避免页数过多撑爆。
function renderPageButtons(batchId, offset, total) {
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const cur = Math.floor(offset / PAGE_SIZE) + 1;
  const nums = [];
  const push = (n) => { if (n >= 1 && n <= pages && !nums.includes(n)) nums.push(n); };
  push(1); push(2);
  push(cur - 1); push(cur); push(cur + 1);
  push(pages - 1); push(pages);
  nums.sort((a, b) => a - b);
  let html = '';
  let prev = 0;
  for (const n of nums) {
    if (prev && n - prev > 1) html += '<span class="page-gap">…</span>';
    html += `<button class="filter-btn page-btn ${n===cur?'active':''}"
      onclick="showBatch('${batchId}', ${(n-1)*PAGE_SIZE})">${n}</button>`;
    prev = n;
  }
  return html;
}

// 自定义页码跳转：读输入框 → 转成 offset（越界自动夹到 [1, 总页数]）。
// 支持回车触发（见 input 的 onkeydown）。
function jumpPage(batchId, total) {
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const el = document.getElementById('jumpPageInput');
  let n = parseInt((el && el.value || '').replace(/[^0-9]/g, ''), 10);
  if (!n || isNaN(n)) { if (el) { el.value = ''; el.focus(); } return; }
  n = Math.min(Math.max(1, n), pages);
  showBatch(batchId, (n - 1) * PAGE_SIZE);
}

function setFilter(f) {
  currentFilter = f;
  if (currentBatch) showBatch(currentBatch.batch_id, 0);   // 切筛选回到第一页
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
    wfTotal = d.trace_duration_ms || 0;
    const totalTxt = wfTotal ? fmtMs(wfTotal) : '—';
    const synthTag = d.synthetic ? '<span class="lg lg-warn" title="真实毫秒时间缺失，按节点顺序估算">合成时间轴</span>' : '<span class="lg lg-span" title="基于节点真实 start/end_time">真实时间</span>';
    // 0 节点：该执行器未上报链路详情，仅展示评分
    const waterfallHtml = (d.tree && d.tree.length) ? `
        <div class="wf-head">⏱ 时间轴瀑布 ${synthTag} <span class="batch-meta">总跨度 ${totalTxt} · ${d.total_observations} 节点</span></div>
        <div class="wf-scale" id="wf-scale"></div>
        <div class="wf-timeline" id="wf-timeline">
          ${d.tree.map(renderNode).join('')}
        </div>
        <div class="wf-legend">
          <span class="lg lg-gen">LLM/生成</span>
          <span class="lg lg-span">工具/SPAN</span>
          <span class="lg lg-err">异常</span>
          <span class="lg lg-warn">拒绝</span>
        </div>
      ` : `
        <div class="wf-empty">⚠ 该执行器未上报链路节点（observations 为空）。仅展示输入与评分结果。</div>
      `;
    box.innerHTML = `<div class="detail-body">
        ${waterfallHtml}
        <div class="node-detail"><b>input:</b> ${d.trace.input || ''}</div>
        <h4>打分 (${d.scores.length})</h4>
        <div>${d.scores.map(renderScore).join('')}</div>
      </div>`;
    drawScale(wfTotal);
    box.style.display = 'block';
    row.querySelector('.toggle').textContent = '▾';
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  } else {
    box.style.display = 'none';
    row.querySelector('.toggle').textContent = '▸';
  }
}

let wfTotal = 0;

function fmtMs(ms) {
  if (!ms || isNaN(ms)) return '—';
  if (ms >= 1000) return (ms / 1000).toFixed(2) + 's';
  return Math.round(ms) + 'ms';
}

function drawScale(total) {
  const el = document.getElementById('wf-scale');
  if (!el) return;
  if (!total) { el.innerHTML = ''; return; }
  // 5 个等分刻度
  let ticks = [];
  for (let i = 0; i <= 4; i++) {
    const t = total * i / 4;
    ticks.push(`<div class="wf-tick" style="left:${i*25}%"><span>${fmtMs(t)}</span></div>`);
  }
  el.innerHTML = `<div class="wf-scale-inner">${ticks.join('')}</div>`;
}

// 瀑布条最小可见宽度（相对 total 的百分比）
function wfWidth(dur) {
  if (!wfTotal) return 2;
  const pct = (dur / wfTotal) * 100;
  return Math.max(0.6, Math.min(pct, 100));
}

function renderNode(n) {
  const lvl = n._level || '';
  const isErr = lvl === 'ERROR', isWarn = lvl === 'WARNING';
  const dur = n._duration_ms || 0;
  const off = n._offset_ms || 0;
  // 横条定位（相对整条 trace 时间窗）
  let left = 0, width = 2;
  if (wfTotal) {
    left = (off / wfTotal) * 100;
    width = (dur / wfTotal) * 100;
    width = Math.max(0.6, Math.min(width, 100 - left));
  }
  const typeCls = n.type === 'GENERATION' ? 'gen' : 'span';
  const stateCls = isErr ? ' st-err' : isWarn ? ' st-warn' : '';
  const metaBits = [];
  if (n._tool) metaBits.push('🔧 ' + n._tool);
  if (n._model) metaBits.push('🧠 ' + n._model);
  if (n._tokens_in !== undefined || n._tokens_out !== undefined) metaBits.push(`⇄ ${n._tokens_in||0}/${n._tokens_out||0} tok`);
  const tooltip = escAttr(n.input || '') + '\\n⇊\\n' + escAttr(n.output || '');
  // 直接渲染 input/output 文本（不折叠、不悬停），截断过长内容避免行超高
  // 若内容是 JSON（如 MCP 返回 {"code":0,"msg":"\u67E5..."}），先解析再重序列化：
  // 否则会把 Unicode 转义串（\u67E5\u8BE2...）原样显示，中文不可读。
  function truncStr(v, max) {
    if (v === undefined || v === null) return '';
    var s = String(v);
    // 尝试按 JSON 解析并重排（中文正常显示）；非 JSON 文本原样保留
    var t = s.trim();
    if (t && (t[0] === '{' || t[0] === '[')) {
      try {
        var o = JSON.parse(t);
        if (o && typeof o === 'object') {
          s = JSON.stringify(o);   // 紧凑单行，保持现有排版
        }
      } catch (e) { /* 非合法 JSON，按原文本显示 */ }
    }
    return s.length > max ? s.slice(0, max) + '…' : s;
  }
  const ioIn = escHtml(truncStr(n.input, 400));
  const ioOut = escHtml(truncStr(n.output, 400));
  const hasIo = !!(n.input !== undefined && n.input !== null && String(n.input).trim()) ||
                !!(n.output !== undefined && n.output !== null && String(n.output).trim());

  return `<div class="wf-row ${typeCls}${stateCls}">
    <div class="wf-meta">
      <span class="node-name">${n.name}</span>
      <span class="node-type">[${n.type}]</span>
      ${isErr ? '<span class="node-lvl-err">⚠ ERROR</span>' : isWarn ? '<span class="node-lvl-warn">◆ WARNING</span>' : ''}
      <span class="wf-dur">${fmtMs(dur)}</span>
      ${metaBits.length ? '<div class="wf-meta-bits">' + metaBits.join(' ') + '</div>' : ''}
    </div>
    <div class="wf-track">
      <div class="wf-bar ${isErr ? 'bar-err' : isWarn ? 'bar-warn' : ''}" style="left:${left}%;width:${width}%"
        title="${tooltip}"></div>
    </div>
    ${hasIo ? `<div class="wf-io">
      ${n.input !== undefined && n.input !== null && String(n.input).trim() !== ''
        ? `<div class="wf-io-in"><b>IN</b><span class="wf-io-txt">${ioIn}</span></div>` : ''}
      ${n.output !== undefined && n.output !== null && String(n.output).trim() !== ''
        ? `<div class="wf-io-out"><b>OUT</b><span class="wf-io-txt">${ioOut}</span></div>` : ''}
    </div>` : ''}
  </div>
  ${n.children && n.children.length ? n.children.map(renderNode).join('') : ''}`;
}

function escAttr(s) {
  return (s || '').replace(/&/g, '&amp;').replace(/"/g, '&quot;')
    .replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\\n/g, ' ');
}
function escHtml(s) {
  return (s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function renderScore(s) {
  const md = s.metadata || {};
  const via = md.via || '';
  const viaCls = via === 'rule' ? 'via-rule' : via === 'llm' ? 'via-llm' : 'via-default';
  const viaTxt = via === 'rule' ? '规则' : via === 'llm' ? 'LLM' : '默认';
  const detail = md.detail || s.comment || '';
  const noJudge = via === 'default' || via === '';
  const tooltip = `${s.name} = ${s.value}${detail ? '\\n理由: ' + detail : ''}${md.judgeable ? '' : '\\n(未实际判定，取默认)'}`;
  return `<span class="score${noJudge ? ' score-nojudge' : ''}" title="${escAttr(tooltip)}">
    ${s.name} = ${s.value}<span class="via ${viaCls}">${viaTxt}</span>
  </span>`;
}

// 页码跳转输入框：只用「事件委托」处理键盘，避免内联 onkeydown 受模板转义影响。
// 关键：只对 Enter 做 preventDefault；Backspace/Delete/方向键/数字键 → 一律放行，
// 保证退格能正常删字符（此前内联 onkeydown 在重建面板后行为异常）。
document.addEventListener('keydown', function (e) {
  const el = e.target;
  if (!el || el.id !== 'jumpPageInput') return;
  if (e.key === 'Enter') {
    e.preventDefault();
    jumpPage(el.getAttribute('data-batch') || '',
             parseInt(el.getAttribute('data-total') || '0', 10) || 0);
  }
  // 其余键（含 Backspace）不拦截，交由浏览器默认行为处理
}, true);

loadBatches();
</script>
</body>
</html>"""


@app.get("/")
def index():
    return HTMLResponse(INDEX_HTML)
