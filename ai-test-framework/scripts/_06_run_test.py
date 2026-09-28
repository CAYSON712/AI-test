# -*- coding: utf-8 -*-
"""
测试执行 + Rubric 评分入口（手册方法论）
==========================================
读数据集 → 执行器跑用例（Mock/真实MCP）→ Rubric 评分 → 统计 → 输出结果

用法：
  cd ai-test-framework/scripts
  python _06_run_test.py --req-type C --dataset ../datasets/C_某系统.yaml --executor mock
  python _06_run_test.py --req-type C --dataset ../datasets/C_某系统.yaml --executor real --runs 5
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
from scripts._07_evaluate import load_dimension_tables
from scripts._12_trace_client import report_case_trace


def _wilson_ci(passed, total, z=1.96):
    """Wilson score 95% 置信区间（小样本/极端比例下比正态近似稳健）。

    返回 (low, high)，保留 3 位小数；total<=0 时返回 (0, 0)。
    用途：报告里判断"通过率是否稳定、样本量是否足够"——n 小则区间宽，
    提示需要补采样，而不是把点估计当结论。
    """
    if not total or total <= 0:
        return (0, 0)
    p = passed / total
    d = 1 + z * z / total
    center = (p + z * z / (2 * total)) / d
    margin = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / d
    return (round(max(0.0, center - margin), 3), round(min(1.0, center + margin), 3))


def _load_dataset_type(dataset_path, req_type):
    """读取数据集里的「需求类型」，作为权威类型。

    为什么要它：显式传的 --req-type 可能与数据集不符（如用 C 去跑 D 数据集），
    静默接受会导致「用错维度表评分 + 执行器加载错类型 + 结果/报告命名撞车」。
    这里以数据集为准；读不到或结构异常时回退到传入值（再兜底 "C"），不抛异常。
    """
    try:
        with open(dataset_path, encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
    except Exception:
        return req_type or "C"
    if not isinstance(d, dict):
        return req_type or "C"
    ds_type = str(d.get("需求类型") or "").strip()
    if ds_type:
        return ds_type
    return req_type or "C"


def _strip_type_prefix(name, req_type):
    """剥掉数据集名开头的「类型_」前缀，避免拼出 ..._D_D_小韩面... 的重复。

    只剥与 req_type 完全一致的前缀；否则原样返回（系统名可能本身以该字母开头）。
    """
    if not name or not req_type:
        return name or ""
    p = f"{req_type}_"
    return name[len(p):] if name.startswith(p) else name


def _build_out_path(out_dir, req_type):
    """默认结果名带时间戳：result_<类型>_<YYYYMMDD>.yaml。

    同一天再次生成时追加 _HHMMSS，避免互相覆盖（不同 runs / 维度子集 / 重跑
    都会产出多份结果，覆盖会丢历史）。
    """
    import time as _t
    base = os.path.join(out_dir, f"result_{req_type}_{_t.strftime('%Y%m%d')}.yaml")
    if not os.path.exists(base):
        return base
    return os.path.join(
        out_dir, f"result_{req_type}_{_t.strftime('%Y%m%d_%H%M%S')}.yaml")


def run_dataset(req_type, dataset_path, executor_mode, runs, out_path, system=None,
                report_trace=False, use_llm_judge=False, llm_detail=False,
                auto_report=False, only_dims=None):
    """执行数据集 + Rubric 评分

    only_dims: 可选，只跑指定维度（list[str]，支持子串匹配）。
      用途：多次采样（runs>1）成本随数据量线性增长，全量跑易触发限流且耗时长；
      先用小样本维度验证口径/观察波动，再决定是否全量，可显著降低试错成本。
      例：only_dims=["性能"] → 只跑「性能」/「性能与资源」等含该子串的维度。
    """
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
        # 数据集的「需求类型」是权威值：显式传的不符时以数据集为准（否则会用错
        # 维度表评分、执行器加载错类型、结果/报告命名撞车）
        ds_type = _load_dataset_type(dataset_path, req_type)
        if req_type and ds_type and req_type != ds_type:
            print(f"⚠ 警告：--req-type {req_type} 与数据集「需求类型」{ds_type} 不一致，"
                  f"以数据集为准（{ds_type}）")
        req_type = ds_type
    elif isinstance(data, list):
        cases = data
    else:
        raise ValueError(f"无法识别的数据集结构: {dataset_path}")

    # 维度筛选（可选）：只跑指定维度，降低多次采样的试错成本
    if only_dims:
        keys = [str(d) for d in only_dims]
        before = len(cases)
        cases = [c for c in cases
                 if any(k in str(c.get("维度") or "") for k in keys)]
        print("维度筛选 {}: {} → {} 条".format(keys, before, len(cases)))
        if not cases:
            raise ValueError("维度筛选后无用例，请检查 only_dims: {}".format(keys))

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
    blocked_cases = []                     # 期望拦截且确实被拦截的用例（符合预期，非失败）
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
                            "attribution": v.get("attribution", "ai_system"),
                            "detail": (v.get("detail") or "")[:200],
                            "input": str(case.get("输入", ""))[:200],
                            "capability": case.get("能力", ""),
                            "via": v.get("via", ""),
                        }
            if result.status != "success":
                exp = case.get("期望") or {}
                out = getattr(result, "output_data", None)
                # 期望拦截且确实被拦截（业务拒绝/显式 block，排除传输层真异常）→ 符合预期
                blocked = isinstance(out, dict) and (
                    out.get("biz_error") or (out.get("block") is True and out.get("level") != "ERROR")
                )
                if exp.get("block") and blocked:
                    blocked_cases.append({"用例ID": uid, "维度": dim,
                                          "输入": case.get("输入", {}),
                                          "error": result.error or result.status})
                else:
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
        # 按用例聚合：区分「至少一次对」(pass@k) 与「每次都对」(pass^k)
        case_rows = dim_case_all.get(dim, {})   # {用例ID: [(judgeable, passed, score), ...]}
        judgeable_cases = 0
        passed_cases = 0        # pass@k：至少一次通过
        all_pass_cases = 0      # pass^k：每次采样都通过（稳定性指标）
        max_k = 0
        case_stdevs = []        # 该维度各用例的分数标准差（波动性）
        for _uid, run_rows in case_rows.items():
            # 该用例实际采样次数（可能因 sample_extra 而异）
            max_k = max(max_k, len(run_rows))
            # 该用例是否有任何一次 run 可被程序化判定（judgeable）
            if not any(j for j, p, s in run_rows):
                continue
            judgeable_cases += 1
            # 只统计 judgeable 的 run，避免未判定项污染稳定性统计
            jrows = [(p, s) for j, p, s in run_rows if j]
            passed_flags = [1 if p else 0 for p, s in jrows]
            # pass@k：任意一次 run passed → 该用例通过（能力上界）
            if any(passed_flags):
                passed_cases += 1
            # pass^k：全部 run 都 passed → 该用例稳定通过（可靠性）
            # ⚠ 与 pass@k 的区别：pass@k=100% 但 pass^k=20% 说明「能做到但很不稳」，
            #   旧口径只看 pass@k 时这种情况与「稳定正确」完全同分，掩盖了波动。
            if passed_flags and all(passed_flags):
                all_pass_cases += 1
            # 每用例标准差（样本>=2 才有意义）
            svals = [s for p, s in jrows if isinstance(s, (int, float))]
            if len(svals) >= 2:
                m = sum(svals) / len(svals)
                var = sum((x - m) ** 2 for x in svals) / (len(svals) - 1)
                case_stdevs.append(var ** 0.5)

        # ⚠ 评分口径修正（2026-09-27）：
        # 旧实现 final_score = max(rate映射分, avg) 有两处硬伤：
        #   ① max 是「择优选一个报」= 主动隐藏不利信息（选择性报告），无统计依据；
        #   ② rate_score 是通过率查表的离散分、avg 是逐用例均值，量纲不同，
        #      直接比大小数学上不成立。
        # 三者（pass@k 宽松 + 阈值偏松 + max 取高）叠加后系统性偏乐观：
        #   如 5 次错 4 次仍算通过(rate=100%) → rate_score=5，把 avg 2.x 盖掉 → 报告满分。
        # 现改为【并列展示，不合并为单一分】：score 置 None，由 score_rate /
        # score_avg 分别表达「达标率口径」与「平均质量口径」，报告侧同时呈现。
        score_rate = None
        score_avg = round(avg, 2)
        rate = None
        if judgeable_cases:
            rate = passed_cases / judgeable_cases
            if dim_rubric:
                score_rate = dim_rubric.score_from_rate(rate)
        # 指标口径决定聚合方式（来自维度表「指标口径」字段）：
        #   "分布"（性能类）：看 avg + stdev，不用 pass^k 判稳定性
        #     —— 性能受网络/负载影响天然波动，单次慢不构成缺陷。
        #   "一致性"（默认，功能类）：pass^k 是关键指标。
        metric_mode = getattr(dim_rubric, "metric_mode", None) or "一致性"
        # 分布类维度：pass^k 不参与判定，报告侧也不展示（避免误判正常抖动）
        pass_all_k_effective = None if metric_mode == "分布" else (
            round(all_pass_cases / judgeable_cases, 3) if judgeable_cases else 0)
        # 兼容字段：旧报告/脚本可能读 score。保留但语义变为「达标率口径分」，
        # 不再用 max 合并（无 rate 可算时回退到 avg）。
        final_score = score_rate if score_rate is not None else score_avg
        score_type = "rate" if score_rate is not None else "avg"
        # Wilson 95% 置信区间：小样本 + 极端通过率(0/1)下比正态近似稳健。
        # 注意：该 CI 基于 pass@k，本身偏乐观，需与 pass^k 一并解读。
        ci = _wilson_ci(passed_cases, judgeable_cases)
        all_ci = _wilson_ci(all_pass_cases, judgeable_cases)
        stdev = (sum(case_stdevs) / len(case_stdevs)) if case_stdevs else None
        dimensions[dim] = {
            "avg_score": score_avg,          # 平均质量口径（逐用例均值）
            "score": final_score,            # 兼容字段=达标率口径分（不再与 avg 取 max）
            "score_rate": score_rate,        # 达标率口径分（通过率查表）
            "score_avg": score_avg,          # 平均质量口径分
            "score_type": score_type,
            "pass_rate": round(passed_cases / judgeable_cases, 3) if judgeable_cases else 0,
            "rate": round(rate, 3) if rate is not None else None,          # pass@k（宽松）
            "pass_at_k": round(passed_cases / judgeable_cases, 3) if judgeable_cases else 0,
            # pass_all_k：仅「一致性」口径维度有意义；「分布」口径（性能类）置 None，
            # 因为性能天然波动，用它判稳定性会把正常抖动误判成缺陷。
            "pass_all_k": pass_all_k_effective,
            "metric_mode": metric_mode,      # 一致性 / 分布
            "stdev": round(stdev, 3) if stdev is not None else None,       # 用例分数平均标准差
            "n": judgeable_cases,
            "runs": max_k,
            "ci": ci,                        # pass@k 的 Wilson 95% CI
            "ci_all": all_ci if metric_mode != "分布" else None,
        }

    # 组装失分用例明细（供报告"错误类型分布 + 失分用例明细"）
    case_results = []
    for uid, dims_fail in case_fails.items():
        if not dims_fail:
            continue
        # 归因汇总：统计该用例各维度的归因，取"主归因"（数量最多的非 test_pass 归因）
        from collections import Counter as _C
        att_counter = _C(d.get("attribution", "ai_system") for d in dims_fail.values()
                         if d.get("attribution") != "test_pass")
        primary_attribution = att_counter.most_common(1)[0][0] if att_counter else "ai_system"
        case_results.append({
            "用例ID": uid,
            "能力": next((c.get("能力", "") for c in cases if (c.get("用例ID") or "") == uid), ""),
            "输入": next((str(c.get("输入", ""))[:200] for c in cases if (c.get("用例ID") or "") == uid), ""),
            "期望": next((c.get("期望", {}) for c in cases if (c.get("用例ID") or "") == uid), {}),
            "失败维度": list(dims_fail.keys()),
            "错误类型": sorted({d.get("error_type") for d in dims_fail.values()}),
            "归因": primary_attribution,          # 主归因：dataset/ai_system/env/test_pass
            "归因统计": dict(att_counter),         # 各归因数量分布
            "最差得分": min(d["score"] for d in dims_fail.values()),
            "失分明细": dims_fail,   # {维度: {score,error_type,attribution,detail,...}}
        })
    # 按最差得分升序（最严重在前）
    case_results.sort(key=lambda x: x["最差得分"])

    result_data = {
        "req_type": req_type,
        "system": system,
        "runs": runs,
        "dimensions": dimensions,
        "failed_cases": failed_cases,
        "blocked_cases": blocked_cases,      # 期望拦截且已拦截的用例（符合预期，非失败）
        "dim_case_counts": dict(dim_case_counts),
        "case_traces": list(case_traces.values()),   # 按用例挂 trace_id（含 trace_ids 列表）
        "case_results": case_results,                # 失分用例明细（错误类型分布 + 失分归因）
    }

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(result_data, f, allow_unicode=True, sort_keys=False, width=120)
    print(f"结果已写入: {out_path}")
    print(f"覆盖维度: {len(dimensions)} 个 | 失败用例: {len(failed_cases)} 条"
          f" | 已拦截(符合预期): {len(blocked_cases)} 条")

    # 可选：跑完自动生成评估报告（--report）
    # 报告命名 = 评估报告_<时间戳>_<类型>_<数据集名>.md
    # 与 _08_report.py 手动运行的默认命名保持一致（两处对齐，避免半截名）
    # ⚠ 数据集文件名本身常已带类型前缀（如 D_小韩面无人值守门禁），
    #   故先剥掉再拼，否则会出现 ..._D_D_小韩面... 的重复类型。
    if auto_report:
        try:
            from scripts._08_report import generate_report
            ts = time.strftime("%Y%m%d_%H%M%S")
            dataset_stem = os.path.splitext(dataset_name)[0]  # e.g. D_小韩面无人值守门禁
            dataset_stem = _strip_type_prefix(dataset_stem, req_type)
            report_path = os.path.join(_ROOT, "report",
                                       f"评估报告_{ts}_{req_type}_{dataset_stem}.md")
            generate_report(out_path, report_path)
        except Exception as e:
            print(f"⚠ 自动生成报告失败（{e}），可稍后手动运行 _08_report.py")


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
                        help="跑完自动生成评估报告（report/评估报告_<时间戳>_<类型>_<数据集名>.md）")
    parser.add_argument("--llm-judge", action="store_true",
                        help="启用 LLM-as-Judge：对规则判不了的主观维度由 LLM 打分（更慢、耗 token）")
    parser.add_argument("--llm-detail", action="store_true",
                        help="LLM 打分时输出详细评分理由（需配合 --llm-judge，更耗 token）")
    parser.add_argument("--dims", default=None,
                        help="只跑指定维度，逗号分隔的子串（如 --dims 性能,调用正确性）。"
                             "多次采样(runs>1)成本高，可先小样本验证再用此参数。")
    args = parser.parse_args()

    # 默认输出名带时间戳：result_<类型>_<YYYYMMDD>.yaml
    # ⚠ 类型以「数据集里的需求类型」为准 —— 与 run_dataset 内部校正保持一致，
    #   否则会出现 result 叫 C、报告却叫 D 的自相矛盾命名。
    _eff_type = _load_dataset_type(args.dataset, args.req_type)
    out = args.out or _build_out_path(os.path.join(_ROOT, "results"), _eff_type)
    only_dims = [s.strip() for s in args.dims.split(",") if s.strip()] if args.dims else None
    run_dataset(args.req_type, args.dataset, args.executor, args.runs, out,
                args.system, report_trace=args.trace, use_llm_judge=args.llm_judge,
                llm_detail=args.llm_detail, auto_report=args.report,
                only_dims=only_dims)


if __name__ == "__main__":
    main()
