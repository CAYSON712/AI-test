# -*- coding: utf-8 -*-
"""
测试执行 + Rubric 评分入口（手册方法论）
==========================================
读数据集 → 执行器跑用例（Mock/真实MCP）→ Rubric 评分 → 统计 → 输出结果

用法：
  cd ai-test-framework/scripts
  python run_test.py --req-type C --dataset ../datasets/C_POS.yaml --executor mock
  python run_test.py --req-type C --dataset ../datasets/C_POS.yaml --executor real --runs 5
"""
import argparse
import os
import sys
import yaml
from collections import defaultdict

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "executors"))

from executors.registry import get_registry
from rubric.rubric import RubricJudger, score_to_label
from scripts.evaluate import load_dimension_tables
from scripts.trace_client import report_case_trace


def _judge_dim_pass(dim, case, result):
    """判断「该维度 + 该用例」能否程序化判定，以及是否达标。
    返回 (judgeable: bool, passed: bool)
      - judgeable=False → 该维度无法程序化判定（走 LLM），不参与准确率统计
      - judgeable=True  → 按 verify/block/error/工具匹配 判定对错

    业务失败按「维度语义」分流，不做一刀切：
      - 决策相关维度（意图/工具/规划）：只看 tool_correct（意图对就对，不受业务结果影响）
      - 结果相关维度（参数端到端/调用/返回/参数生成）：业务失败判失败
    """
    output = getattr(result, "output_data", None)
    if not isinstance(output, dict):
        output = {}
    # 维度类型
    tool_dims = ("意图识别", "工具选择准确率", "工具调用", "工具选择与调用",
                 "规划与推理", "意图到工具映射准确率")
    verify_dims = ("参数端到端准确率", "参数生成", "操作后校验", "参数校验")
    result_dims = tool_dims + verify_dims
    is_biz_fail = (getattr(result, "status", "") != "success") or output.get("biz_error")

    # A. block 拦截类（先判）：正确拦截 = 通过，未拦截 = 失败
    expected_block = (case.get("期望", {}) or {}).get("block", False)
    if expected_block:
        return True, (getattr(result, "status", "") == "error")

    # B. 决策相关维度（意图/工具/规划）：业务失败也看 tool_correct（意图对就对）
    if dim in tool_dims and "tool_correct" in output:
        return True, (output["tool_correct"] is True)

    # C. 结果相关维度（参数端到端/调用/返回）：业务失败判失败
    if is_biz_fail:
        if dim in result_dims:
            return True, False
        return False, False  # 其他维度（主观类）无法程序化判定

    # D. 参数/verify 类维度（成功时）→ 用 verify.match
    if dim in verify_dims and output.get("verify"):
        return True, (output["verify"].get("match") is True)

    # E. 无法程序化判定 → 留给 LLM
    return False, False


def run_dataset(req_type, dataset_path, executor_mode, runs, out_path, system=None,
                report_trace=False):
    """执行数据集 + Rubric 评分"""
    # 加载维度表 + Rubric
    tables = load_dimension_tables()
    judger = RubricJudger(tables)

    # 加载数据集（兼容两种结构：顶层 dict 含「用例列表」，或直接是用例列表）
    # 同时从数据集提取系统名/需求类型（避免命令行中文乱码 + 自动关联系统配置）
    with open(dataset_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if isinstance(data, dict) and "用例列表" in data:
        cases = data["用例列表"]
        system = system or data.get("系统", "")
        req_type = req_type or data.get("需求类型", "C")
    elif isinstance(data, list):
        cases = data
    else:
        raise ValueError(f"无法识别的数据集结构: {dataset_path}")

    # 执行器（按需求类型 + 系统名加载配置驱动的执行器）
    registry = get_registry(executor_mode, system=system, req_type=req_type)

    # 批次信息（一次 run_test = 一个批次）：用于 trace_platform 按批次分组浏览
    import time
    batch_id = f"B{int(time.time())}"
    batch_time = time.strftime("%Y-%m-%d %H:%M:%S")
    dataset_name = os.path.basename(dataset_path)

    print(f"需求类型: {req_type} | 系统: {system or '(自动)'} | 用例: {len(cases)} | 执行器: {executor_mode} | 每条跑 {runs} 次")

    # 记录每条用例的多次运行得分（维度 → score）
    dim_case_counts = defaultdict(int)   # 维度 → 用例数
    dim_scores_all = defaultdict(list)   # 维度 → 所有运行的score
    dim_binary = defaultdict(list)       # 维度 → 该用例能否程序化判定 + 是否达标 [(judgeable, passed)]
    failed_cases = []
    case_traces = {}                     # 用例ID → {用例ID, 能力, 维度, 层, trace_ids:[]}
    trace_offline = None                 # trace_platform 离线状态（仅提醒一次）

    for case in cases:
        dim = case.get("维度", "")
        dim_case_counts[dim] += 1
        uid = case.get("用例ID", "")
        if report_trace and uid:
            case_traces.setdefault(uid, {
                "用例ID": uid,
                "能力": case.get("能力", ""),
                "维度": dim,
                "层": case.get("层", ""),
                "trace_ids": [],
            })
        for run in range(runs):
            result = registry.dispatch(case)
            if result is None:
                failed_cases.append({"用例ID": uid, "维度": dim,
                                     "输入": case.get("输入", {}),
                                     "error": "无执行器能处理该能力"})
                continue
            # Rubric 评分
            scores = judger.score_case(req_type, case, result, judge_text=None)
            for k, v in scores.items():
                dim_scores_all[k].append(v["score"])
            # 该维度该用例的"程序化二元判定"（对/错，能否判）
            judgeable, passed = _judge_dim_pass(dim, case, result)
            dim_binary[dim].append((judgeable, passed))
            if result.status != "success":
                failed_cases.append({"用例ID": uid, "维度": dim,
                                     "输入": case.get("输入", {}),
                                     "error": result.error or result.status})
            # 上报 trace（可选，全部用例）：trace_id 挂到该用例的 trace_ids 列表
            if report_trace and uid:
                tid = report_case_trace(case, result, scores, system=system,
                                        batch_id=batch_id, dataset=dataset_name,
                                        batch_time=batch_time)
                if tid:
                    case_traces[uid]["trace_ids"].append(tid)
                elif trace_offline is None:
                    trace_offline = True
                    print("⚠️ 提示：trace_platform 未启动，已跳过 trace 上报（可启动后重跑）")

    # 聚合维度得分（混合评分）：
    #   - 可程序化判定（有 verify/block/error/工具匹配）→ 统计准确率 → 查 rubric 得全局分
    #   - 不可程序化判定 → 保留逐用例 avg（留给 LLM-as-Judge）
    dimensions = {}
    for dim, scores in dim_scores_all.items():
        if not scores:
            continue
        avg = sum(scores) / len(scores)
        n = len(scores)
        # 取该维度 rubric，尝试查表
        dim_rubric = None
        for _rt, _dims in judger.tables.items():
            if dim in _dims:
                dim_rubric = _dims[dim]
                break
        # 程序化判定统计
        binaries = dim_binary.get(dim, [])
        judgeable = [p for j, p in binaries if j]
        final_score = None
        score_type = "avg"
        if judgeable:
            rate = sum(judgeable) / len(judgeable)
            rate_score = dim_rubric.score_from_rate(rate) if dim_rubric else None
            if rate_score is not None:
                final_score = rate_score
                score_type = "rate"
        if final_score is None:
            final_score = round(avg, 2)
        passed = sum(1 for s in scores if s >= 3)
        dimensions[dim] = {
            "avg_score": round(avg, 2),
            "score": final_score,           # 最终得分（全局统计分或 avg）
            "score_type": score_type,       # rate=程序化统计 / avg=逐用例均值
            "pass_rate": round(passed / n, 3),
            "n": n,
            "ci": (0, 0),
        }

    result_data = {
        "req_type": req_type,
        "system": system,
        "runs": runs,
        "dimensions": dimensions,
        "failed_cases": failed_cases,
        "dim_case_counts": dict(dim_case_counts),
        "case_traces": list(case_traces.values()),   # 按用例挂 trace_id（含 trace_ids 列表）
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(result_data, f, allow_unicode=True, sort_keys=False, width=120)
    print(f"结果已写入: {out_path}")
    print(f"覆盖维度: {len(dimensions)} 个 | 失败用例: {len(failed_cases)} 条")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--req-type", default="C")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--executor", choices=["auto", "real", "mock"], default="mock")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--system", default=None,
                        help="被测系统名（对应 ability/能力目录_<系统>.yaml + configs/<系统>.yaml）")
    parser.add_argument("--out", default=None)
    parser.add_argument("--trace", action="store_true",
                        help="上报 trace 到 trace_platform（需先启动该服务）")
    args = parser.parse_args()

    out = args.out or os.path.join(_ROOT, "results", f"result_{args.req_type}.yaml")
    run_dataset(args.req_type, args.dataset, args.executor, args.runs, out,
                args.system, report_trace=args.trace)


if __name__ == "__main__":
    main()
