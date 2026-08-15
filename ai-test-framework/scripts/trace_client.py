# -*- coding: utf-8 -*-
"""
Trace 上报客户端（通用）
========================
把执行器产出的多层链路（steps）自动上报到 trace_platform，实现可视化。
与具体系统无关：读 case + result 的通用字段。

功能：
  - 检测 trace_platform 服务是否在线（避免超时）
  - 把执行器 steps 转成 trace_platform 的 observations（含父子的树形）
  - 把 Rubric 评分转成 scores
  - 上报后返回 trace_id；离线时返回 None 并提醒

用法：
  from trace_client import report_case_trace
  trace_id = report_case_trace(base_url, case, result, scores, system)
"""
import json
import os

import requests

# trace_platform 默认地址（可被环境变量覆盖）
DEFAULT_BASE = os.getenv("TRACE_PLATFORM_URL", "http://127.0.0.1:8000")


def _check_online(base_url, timeout=1.5):
    """探测 trace_platform 服务是否在线"""
    try:
        r = requests.get(f"{base_url}/api/traces", timeout=timeout)
        return r.status_code == 200
    except Exception:
        return False


def _steps_to_observations(steps):
    """把执行器 steps（扁平列表）转成 observations 列表。
    steps 元素: {name, type, input, output, metadata}
    返回 [{name,type,input,output,metadata}]（平铺，作为 trace 根下节点）
    """
    obs = []
    for s in steps or []:
        obs.append({
            "name": s.get("name", "step"),
            "type": s.get("type", "SPAN"),
            "input": s.get("input", ""),
            "output": s.get("output", ""),
            "metadata": s.get("metadata", {}),
        })
    return obs


def _scores_to_list(scores):
    """把 Rubric 评分 {维度: {score,label,...}} 转成 scores 列表"""
    out = []
    for dim, v in (scores or {}).items():
        out.append({
            "name": dim,
            "value": float(v.get("score", 0)) if isinstance(v, dict) else float(v),
            "data_type": "NUMERIC",
            "comment": v.get("label", "") if isinstance(v, dict) else "",
        })
    return out


def report_case_trace(case, result, scores, system="", base_url=None,
                      batch_id=None, dataset="", batch_time=""):
    """上报一条用例的 trace。返回 trace_id；离线返回 None。
    case: 用例 dict
    result: 执行结果（ExecResult 或 dict，含 output_data.steps）
    scores: Rubric 评分结果 {维度: {score,label}}
    batch_id: 批次ID（一次 run_test 执行 = 一个批次）
    dataset: 数据集名
    batch_time: 批次时间戳
    """
    base_url = base_url or DEFAULT_BASE
    if not _check_online(base_url):
        return None

    # 提取 steps（链路）
    steps = []
    out = getattr(result, "output_data", result) if not isinstance(result, dict) else result.get("output_data", result)
    if isinstance(out, dict):
        steps = out.get("steps", [])

    # 提取输入
    case_input = case.get("输入", "")
    if isinstance(case_input, dict):
        case_input = case_input.get("user_input", "") or str(case_input)

    # 提取执行结果 level（对齐 Langfuse）：ERROR=真异常 / WARNING=业务拒绝 / 空=正常
    level = ""
    error_summary = ""
    if isinstance(out, dict):
        level = out.get("level", "") or ""
        if out.get("biz_error"):
            error_summary = str(out.get("error") or out.get("biz_error"))[:80]
        elif out.get("error"):
            error_summary = str(out.get("error"))[:80]

    payload = {
        "name": f"{case.get('用例ID', '?')} - {case.get('能力', '')}",
        "input": case_input,
        "output": json.dumps(out, ensure_ascii=False)[:2000] if out else "",
        "metadata": {
            "system": system,
            "req_type": case.get("需求类型", ""),
            "dimension": case.get("维度", ""),
            "capability": case.get("能力", ""),
            "layer": case.get("层", ""),
            "batch_id": batch_id or "",
            "dataset": dataset or "",
            "batch_time": batch_time or "",
            "level": level,
            "error_summary": error_summary,
        },
        "observations": _steps_to_observations(steps),
        "scores": _scores_to_list(scores),
    }

    try:
        r = requests.post(f"{base_url}/api/trace", json=payload, timeout=10)
        if r.status_code == 200:
            return r.json().get("trace_id")
        return None
    except Exception:
        return None
