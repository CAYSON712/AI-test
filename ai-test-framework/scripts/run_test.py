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


def run_dataset(req_type, dataset_path, executor_mode, runs, out_path, system=None):
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

    print(f"需求类型: {req_type} | 系统: {system or '(自动)'} | 用例: {len(cases)} | 执行器: {executor_mode} | 每条跑 {runs} 次")

    # 记录每条用例的多次运行得分（维度 → score）
    dim_case_counts = defaultdict(int)   # 维度 → 用例数
    dim_scores_all = defaultdict(list)   # 维度 → 所有运行的score
    failed_cases = []

    for case in cases:
        dim = case.get("维度", "")
        dim_case_counts[dim] += 1
        for run in range(runs):
            result = registry.dispatch(case)
            if result is None:
                failed_cases.append({"用例ID": case.get("用例ID"), "维度": dim,
                                     "输入": case.get("输入", {}),
                                     "error": "无执行器能处理该能力"})
                continue
            # Rubric 评分
            scores = judger.score_case(req_type, case, result, judge_text=None)
            for k, v in scores.items():
                dim_scores_all[k].append(v["score"])
            if result.status != "success":
                failed_cases.append({"用例ID": case.get("用例ID"), "维度": dim,
                                     "输入": case.get("输入", {}),
                                     "error": result.error or result.status})

    # 聚合维度得分
    dimensions = {}
    for dim, scores in dim_scores_all.items():
        if not scores:
            continue
        avg = sum(scores) / len(scores)
        passed = sum(1 for s in scores if s >= 3)
        dimensions[dim] = {
            "avg_score": round(avg, 2),
            "pass_rate": round(passed / len(scores), 3),
            "n": len(scores),
            "ci": (0, 0),
        }

    result_data = {
        "req_type": req_type,
        "runs": runs,
        "dimensions": dimensions,
        "failed_cases": failed_cases,
        "dim_case_counts": dict(dim_case_counts),
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
    args = parser.parse_args()

    out = args.out or os.path.join(_ROOT, "results", f"result_{args.req_type}.yaml")
    run_dataset(args.req_type, args.dataset, args.executor, args.runs, out, args.system)


if __name__ == "__main__":
    main()
