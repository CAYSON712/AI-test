# -*- coding: utf-8 -*-
"""
评估入口：加载维度表 + Rubric + 对数据集跑评分
=================================================
用法：
  cd ai-test-framework/scripts
  python evaluate.py --req-type C --dataset ../datasets/xxx.yaml
"""
import argparse
import os
import sys
import yaml

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from rubric.rubric import Rubric, RubricJudger
from rubric.llm_judge import LLMJudge

DIMS_DIR = os.path.join(_ROOT, "dimensions")
TYPE_FILES = {
    "A": "A_MCP工具.yaml",
    "B": "B_Agent系统.yaml",
    "C": "C_AgentMCP集成.yaml",
    "D": "D_Skill原子能力.yaml",
    "E": "E_RAG知识库.yaml",
}


def load_dimension_tables(req_types=("A", "B", "C", "D", "E")):
    """加载维度表为 {需求类型: {维度: Rubric}}。

    C 类 = A 类(8) + B 类(11) + 集成特有维度(4)，按维度名去重合并后为 20 维。
    """
    raw = {}  # 原始加载
    for t in req_types:
        path = os.path.join(DIMS_DIR, TYPE_FILES[t])
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        dims = {}
        for node in data.get("维度列表", []):
            dims[node["维度"]] = Rubric.from_yaml(node)
        raw[t] = dims

    tables = {}
    # 单独加载 C 类的集成特有维度
    c_path = os.path.join(DIMS_DIR, TYPE_FILES["C"])
    c_integration = {}
    if os.path.exists(c_path):
        with open(c_path, encoding="utf-8") as f:
            cdata = yaml.safe_load(f)
        for node in cdata.get("集成特有维度", []):
            c_integration[node["维度"]] = Rubric.from_yaml(node)

    for t in req_types:
        if t == "C":
            # C 类 = A + B + 集成特有（20 独立维）
            # 重名维度合并：一个维度同时覆盖"工具层 + Agent 层"
            merged = dict(raw.get("A", {}))
            for dim, b_rubric in raw.get("B", {}).items():
                if dim in merged:
                    # 重名：合并 A 和 B 的 rubric，标注覆盖两层
                    merged[dim] = _merge_layer_rubric(dim, merged[dim], b_rubric)
                else:
                    merged[dim] = b_rubric
            merged.update(c_integration)
            tables["C"] = merged
        else:
            tables[t] = dict(raw.get(t, {}))
    return tables


def _merge_layer_rubric(dim, a_rubric, b_rubric):
    """合并工具层(A)与 Agent 层(B)同名的维度 rubric，覆盖两层。

    返回一个 Rubric，其 rubric_map 逐档合并两层含义，判定时任一层达标即给分。
    """
    from rubric.rubric import Rubric
    merged_map = {}
    for score in (5, 4, 3, 2, 1):
        s = str(score)
        a_desc = a_rubric.rubric_map.get(s, "")
        b_desc = b_rubric.rubric_map.get(s, "")
        merged_map[s] = f"[工具层] {a_desc}；[Agent层] {b_desc}".strip(" ；")
    return Rubric(dim, merged_map, threshold=None)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--req-type", default="C", choices=TYPE_FILES.keys())
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--runs", type=int, default=1, help="每条跑几次（统计判定）")
    parser.add_argument("--llm-judge", action="store_true", help="用 LLM-as-Judge 打分")
    args = parser.parse_args()

    # 加载维度表
    tables = load_dimension_tables()
    judger = RubricJudger(tables)
    judge = LLMJudge() if args.llm_judge else None

    # 加载数据集
    with open(args.dataset, encoding="utf-8") as f:
        cases = yaml.safe_load(f)

    print(f"需求类型: {args.req_type}")
    print(f"数据集用例数: {len(cases)}，每条跑 {args.runs} 次")
    dims = judger._get_dimensions(args.req_type)
    print(f"评测维度数: {len(dims)}")

    # 模拟执行（占位：真实执行后续接入执行器）
    # 这里先用 demo 结果演示评分流程
    all_runs = []  # 所有用例 × 多次运行 的 {维度: score}
    for c in cases:
        for _ in range(args.runs):
            result = _mock_result(c)
            scores = judger.score_case(args.req_type, c, result, judge_text=None,
                                       judge=judge, use_llm=args.llm_judge)
            all_runs.append({k: v["score"] for k, v in scores.items()})

    from rubric.rubric import aggregate_case_runs, format_report
    # 按维度聚合所有运行
    from collections import defaultdict
    agg = defaultdict(list)
    for run in all_runs:
        for dim, score in run.items():
            agg[dim].append(score)
    report_data = {}
    for dim, scores in agg.items():
        avg = sum(scores) / len(scores)
        passed = sum(1 for s in scores if s >= 3)
        report_data[dim] = {
            "avg_score": avg,
            "pass_rate": passed / len(scores),
            "n": len(scores),
            "ci": (0, 0),
        }
    print("\n===== 评分报告 =====")
    print(format_report(report_data))


def _mock_result(case):
    """占位：模拟执行结果（后续接真实执行器）"""
    class R:
        output_data = None
        status = "success"
    r = R()
    # 如果有 verify 期望，模拟通过
    exp = case.get("期望", {})
    if exp.get("verify"):
        r.output_data = {"verify": {"match": True, "actual": exp["verify"].get("expect")}}
    if exp.get("block"):
        r.status = "error"
    return r


if __name__ == "__main__":
    main()
