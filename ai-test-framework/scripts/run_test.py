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


def run_dataset(req_type, dataset_path, executor_mode, runs, out_path, system=None,
                report_trace=False, use_llm_judge=False, llm_detail=False,
                auto_report=False):
    """执行数据集 + Rubric 评分"""
    # 加载维度表 + Rubric
    tables = load_dimension_tables()
    judger = RubricJudger(tables)
    # 可选 LLM-as-Judge：对"规则判不了"的主观维度打分
    llm_judge = None
    if use_llm_judge:
        from rubric.llm_judge import LLMJudge
        llm_judge = LLMJudge()

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

    # 记录每条用例的多次运行得分（按 用例 分组，支撑 pass@k 判定）
    dim_case_counts = defaultdict(int)     # 维度 → 用例数
    dim_case_all = defaultdict(dict)       # 维度 → {用例ID: [(score, judgeable, passed), ...各run]}
    dim_scores_all = defaultdict(list)     # 维度 → 所有运行的score（供 avg）
    failed_cases = []
    case_traces = {}                       # 用例ID → {用例ID, 能力, 维度, 层, trace_ids:[]}
    # 每条用例的评分明细（供报告做"错误类型分布 + 失分用例明细"）
    # case_fails[uid] = {维度: {"score","error_type","detail","input","capability"}}
    # 取多次 run 里最差的一次（失分记录最有诊断价值）
    case_fails = {}
    trace_offline = None                   # trace_platform 离线状态（仅提醒一次）

    for case in cases:
        dim = case.get("维度", "")
        dim_case_counts[dim] += 1
        uid = case.get("用例ID", "") or f"case-{len(dim_case_all[dim])}"
        if report_trace and uid:
            case_traces.setdefault(uid, {
                "用例ID": uid,
                "能力": case.get("能力", ""),
                "维度": dim,
                "层": case.get("层", ""),
                "trace_ids": [],
            })
        # 每条用例实际采样次数：
        #   runs==1（默认/纯 pass@1）→ 所有用例固定跑 1 次，忽略 sample_extra（保证对比纯粹）
        #   runs>1  → 关键用例用 sample_extra 提升（安全/核心操作多跑），普通用全局 runs
        if int(runs) <= 1:
            case_k = 1
        else:
            case_k = max(int(runs), int(case.get("sample_extra") or 0))
        for run in range(case_k):
            result = registry.dispatch(case)
            if result is None:
                failed_cases.append({"用例ID": uid, "维度": dim,
                                     "输入": case.get("输入", {}),
                                     "error": "无执行器能处理该能力"})
                continue
            # Rubric 评分（统一判定：规则 → LLM → 默认；judgeable 标记该维度该条是否可信）
            scores = judger.score_case(req_type, case, result, judge_text=None,
                                       judge=llm_judge, use_llm=use_llm_judge,
                                       llm_detail=llm_detail)
            for k, v in scores.items():
                dim_scores_all[k].append(v["score"])
                # 按用例分组收集各 run 的二元判定，供 pass@k 使用
                dim_case_all[k].setdefault(uid, []).append(
                    (v.get("judgeable", False), v["score"] >= 3, v["score"]))
                # 收集失分明细：记录该用例该维度最差的一次（失分对诊断最有价值）
                # 仅失分(score<3)才记录；先判失分再 setdefault，避免全通过用例产生空 dict
                if v["score"] < 3:
                    cur = case_fails.get(uid, {}).get(k)
                    if cur is None or v["score"] < cur["score"]:
                        case_fails.setdefault(uid, {})[k] = {
                            "score": v["score"],
                            "error_type": v.get("error_type", "other"),
                            "detail": (v.get("detail") or "")[:200],
                            "input": str(case.get("输入", ""))[:200],
                            "capability": case.get("能力", ""),
                            "via": v.get("via", ""),
                        }
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
    # pass@k：统计单位是「用例」而非「单次 run」——某用例跑 k 次，只要 ≥1 次
    #          (judgeable=True 且 score>=3) 达标，即视为该用例通过。缓解 AI 非确定性误伤。
    dimensions = {}
    for dim, scores in dim_scores_all.items():
        if not scores:
            continue
        avg = sum(scores) / len(scores)
        # 取该维度 rubric，尝试查表
        dim_rubric = None
        for _rt, _dims in judger.tables.items():
            if dim in _dims:
                dim_rubric = _dims[dim]
                break
        # 按用例聚合：每个用例取 "是否达标"（judgeable 的 run 里至少一次 score>=3）
        case_rows = dim_case_all.get(dim, {})   # {用例ID: [(judgeable, passed, score), ...]}
        judgeable_cases = 0
        passed_cases = 0
        max_k = 0
        for _uid, run_rows in case_rows.items():
            # 该用例实际采样次数（可能因 sample_extra 而异）
            max_k = max(max_k, len(run_rows))
            # 该用例是否有任何一次 run 可被程序化判定（judgeable）
            if not any(j for j, p, s in run_rows):
                continue
            judgeable_cases += 1
            # pass@k：任意一次 run (judgeable 且 passed) → 该用例通过
            if any(j and p for j, p, s in run_rows):
                passed_cases += 1
        final_score = None
        score_type = "avg"
        rate = None
        if judgeable_cases:
            rate = passed_cases / judgeable_cases
            rate_score = dim_rubric.score_from_rate(rate) if dim_rubric else None
            if rate_score is not None:
                # 评分修复：score = max(rate映射分, avg)。
                # 通过率查 rubric 表可能比实际平均分更严（如 31.6% 通过率→1 分，
                # 但 avg 2.95 接近可接受）。用 avg 兜底，避免严重低估。
                # score 反映"平均质量"，pass_rate 反映"完全正确比例"，两者结合看。
                final_score = max(rate_score, round(avg, 2))
                score_type = "rate" if rate_score >= round(avg, 2) else "avg"
        if final_score is None:
            final_score = round(avg, 2)
        # pass@k 通过率（以用例计）；k 为该维度实际采样次数（可能受 sample_extra 提升）
        dimensions[dim] = {
            "avg_score": round(avg, 2),
            "score": final_score,           # 最终得分 = max(rate映射分, avg)，反映平均质量
            "score_type": score_type,       # rate=程序化统计 / avg=逐用例均值
            "pass_rate": round(passed_cases / judgeable_cases, 3) if judgeable_cases else 0,
            "rate": round(rate, 3) if rate is not None else None,  # 原始通过率（完全正确比例）
            "n": judgeable_cases,           # 可程序化判定的用例数
            "runs": max_k,                  # 该维度用例实际采样次数（pass@k 的 k）
            "ci": (0, 0),
        }

    # 组装失分用例明细（供报告"错误类型分布 + 失分用例明细"）
    case_results = []
    for uid, dims_fail in case_fails.items():
        if not dims_fail:
            continue
        case_results.append({
            "用例ID": uid,
            "能力": next((c.get("能力", "") for c in cases if (c.get("用例ID") or "") == uid), ""),
            "输入": next((str(c.get("输入", ""))[:200] for c in cases if (c.get("用例ID") or "") == uid), ""),
            "期望": next((c.get("期望", {}) for c in cases if (c.get("用例ID") or "") == uid), {}),
            "失败维度": list(dims_fail.keys()),
            "错误类型": sorted({d.get("error_type") for d in dims_fail.values()}),
            "最差得分": min(d["score"] for d in dims_fail.values()),
            "失分明细": dims_fail,   # {维度: {score,error_type,detail,...}}
        })
    # 按最差得分升序（最严重在前）
    case_results.sort(key=lambda x: x["最差得分"])

    result_data = {
        "req_type": req_type,
        "system": system,
        "runs": runs,
        "dimensions": dimensions,
        "failed_cases": failed_cases,
        "dim_case_counts": dict(dim_case_counts),
        "case_traces": list(case_traces.values()),   # 按用例挂 trace_id（含 trace_ids 列表）
        "case_results": case_results,                # 失分用例明细（错误类型分布 + 失分归因）
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(result_data, f, allow_unicode=True, sort_keys=False, width=120)
    print(f"结果已写入: {out_path}")
    print(f"覆盖维度: {len(dimensions)} 个 | 失败用例: {len(failed_cases)} 条")

    # 可选：跑完自动生成评估报告（--report）
    if auto_report:
        try:
            from scripts.report import generate_report
            report_path = os.path.join(_ROOT, "report",
                                       f"评估报告_{req_type}.md")
            generate_report(out_path, report_path)
            print(f"报告已自动生成: {report_path}")
        except Exception as e:
            print(f"⚠ 自动生成报告失败（{e}），可稍后手动运行 report.py")


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
    parser.add_argument("--report", action="store_true",
                        help="跑完自动生成评估报告（report/评估报告_<req_type>.md）")
    parser.add_argument("--llm-judge", action="store_true",
                        help="启用 LLM-as-Judge：对规则判不了的主观维度由 LLM 打分（更慢、耗 token）")
    parser.add_argument("--llm-detail", action="store_true",
                        help="LLM 打分时输出详细评分理由（需配合 --llm-judge，更耗 token）")
    args = parser.parse_args()

    out = args.out or os.path.join(_ROOT, "results", f"result_{args.req_type}.yaml")
    run_dataset(args.req_type, args.dataset, args.executor, args.runs, out,
                args.system, report_trace=args.trace, use_llm_judge=args.llm_judge,
                llm_detail=args.llm_detail, auto_report=args.report)


if __name__ == "__main__":
    main()
