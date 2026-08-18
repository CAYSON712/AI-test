# -*- coding: utf-8 -*-
"""
评估报告生成（手册方法论）
============================
根据执行 + Rubric 评分结果，生成结构化评估报告：
- 需求类型 + 被测系统
- 各维度 5 分制得分 + 通过率 + 置信区间
- 问题定位（低分维度、失败用例）
- 反哺建议（哪些维度/类型用例不足）

用法：
  cd ai-test-framework/scripts
  python report.py --result results.yaml --out ../report/C_POS.md
"""
import argparse
import os
import sys
import yaml

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from rubric.rubric import format_report, score_to_label, grade_info, GRADE_DEFINITIONS


def generate_report(result_path, out_path):
    """根据评分结果生成报告"""
    with open(result_path, encoding="utf-8") as f:
        result = yaml.safe_load(f)

    req_type = result.get("req_type", "C")
    system = result.get("system", "")
    runs = result.get("runs", 1)
    dims = result.get("dimensions", {})  # {维度: {avg_score, pass_rate, n, ci}}

    # 错误类型 → 中文名 + 建议修复（业界约定：通过率不足以衡量，看错误类型分布）
    ERROR_TYPES = {
        "db_verify_fail":      {"name": "操作后校验失败", "fix": "检查 verify 工具实时数据返回是否与期望值格式/值一致"},
        "block_miss":          {"name": "危险/越权操作未拦截", "fix": "补充安全拦截规则（越权/注入/危险操作需明确拒绝）"},
        "tool_misuse":         {"name": "工具选择/调用错误", "fix": "检查意图→工具的映射与工具参数解析"},
        "semantic_miss":       {"name": "语义输出不符合期望", "fix": "检查输出是否包含期望字段/关键词，或调整期望语义"},
        "biz_fail":            {"name": "业务失败/执行报错", "fix": "检查执行链路报错（连接/参数/权限）"},
        "judge_inconclusive":  {"name": "无法确定性判定", "fix": "规则判不了，需 LLM-as-Judge 或补充判定标准"},
        "other":               {"name": "其他失分", "fix": "人工查看该用例失分原因"},
        "pass":                {"name": "达标", "fix": "-"},
    }

    lines = []
    lines.append(f"# AI 测试评估报告\n")
    lines.append(f"- **需求类型**: {req_type} 类")
    lines.append(f"- **被测系统**: {system}")
    lines.append(f"- **采样次数**: 每条 {runs} 次\n")

    # 1. 维度得分表（score = max(rate映射分, avg)，反映「平均质量」）
    #    通过率以「用例」为统计单位；每条用例跑 k 次（k 由全局 runs 与用例 sample_extra 取大），
    #    ≥1 次达标即判通过（pass@k）。
    #    说明：score 反映"平均质量"，pass_rate(rate) 反映"完全正确比例"，需结合看——
    #          通过率低但平均分高 = 多数用例基本对、但完全正确的不多。
    max_k = max((d.get("runs", 1) or 1) for d in dims.values()) if dims else 1
    lines.append("## 维度得分（5 分制 Rubric）\n")
    lines.append(f"> 通过率统计口径：**pass@{max_k}**（每条用例跑 {max_k} 次、≥1 次达标即判过；关键用例 sample_extra 自动多跑，见「采样k」列）\n")
    lines.append("> 得分=平均质量，通过率=完全正确比例，两者结合看\n")
    lines.append("| 维度 | 得分 | 平均分 | 通过率 | 评分方式 | 等级 | 发布建议 | 用例数 | 采样k | 95%CI |")
    lines.append("|------|------|--------|--------|----------|------|----------|--------|-------|-------|")
    for dim, d in sorted(dims.items(), key=lambda x: x[1].get("score", x[1].get("avg_score", 0))):
        score = d.get("score", d.get("avg_score", 0))
        avg = d.get("avg_score", 0)
        stype = "程序化" if d.get("score_type") == "rate" else "逐用例"
        g = grade_info(score)
        rate = d.get("rate", d.get("pass_rate", 0)) or 0
        n = d.get("n", 0)
        rk = d.get("runs", 1) or 1
        ci = d.get("ci", (0, 0))
        lines.append(f"| {dim} | {score:.2f} | {avg:.2f} | {rate:.0%} | {stype} | {g['label']} | {g['verdict']} | "
                     f"{n} | pass@{rk} | [{ci[0]:.2f}, {ci[1]:.2f}] |")

    # 2. 总体通过率
    total_rate = sum(d.get("pass_rate", 0) for d in dims.values()) / len(dims) if dims else 0
    lines.append(f"\n## 总体通过率\n")
    lines.append(f"- **平均通过率**: {total_rate:.1%}")

    # 3. 问题定位
    lines.append(f"\n## 问题定位\n")
    low_dims = [d for d, v in dims.items()
                if v.get("score", v.get("avg_score", 0)) < 3]
    if low_dims:
        lines.append("### 低分维度（得分 < 3，需重点关注）")
        for d in low_dims:
            v = dims[d]
            sc = v.get("score", v.get("avg_score", 0))
            lines.append(f"- **{d}**: {sc:.2f} 分 "
                         f"({score_to_label(sc)})，通过率 {v.get('pass_rate', 0):.0%}")
    else:
        lines.append("- ✅ 无低分维度（所有维度平均分 ≥ 3）")

    # 4. 失败用例
    fail_cases = result.get("failed_cases", [])
    lines.append(f"\n### 失败/异常用例（{len(fail_cases)} 条）")
    for c in fail_cases[:20]:
        uid = c.get("用例ID", "?") if isinstance(c, dict) else "?"
        dim = c.get("维度", "") if isinstance(c, dict) else ""
        err = c.get("error", "") if isinstance(c, dict) else str(c)
        # 输入可能是 dict（含 user_input）或字符串，健壮处理
        inp = c.get("输入", "") if isinstance(c, dict) else ""
        if isinstance(inp, dict):
            inp = inp.get("user_input", "") or str(inp)
        lines.append(f"- **{uid}** [{dim}] {inp}: {err}")
    if not fail_cases:
        lines.append("- 无失败用例")

    # 4.6 错误类型分布（业界标准：通过率不足以衡量，需看错误类型分布）
    #     case_results 里每条失分维度都有 error_type，聚合成"哪类问题最多"。
    case_results = result.get("case_results", [])
    if case_results:
        from collections import Counter
        type_counter = Counter()
        for cr in case_results:
            for et in cr.get("错误类型", []):
                type_counter[et] += 1
        # 含 pass 不算问题，剔除
        type_counter.pop("pass", None)
        lines.append(f"\n## 错误类型分布（{sum(type_counter.values())} 处失分）\n")
        lines.append("| 错误类型 | 失分处数 | 占比 | 建议修复 |")
        lines.append("|----------|----------|------|----------|")
        total_fail = sum(type_counter.values()) or 1
        for et, cnt in type_counter.most_common():
            meta = ERROR_TYPES.get(et, ERROR_TYPES["other"])
            lines.append(f"| {meta['name']} | {cnt} | {cnt/total_fail:.0%} | {meta['fix']} |")
        if not type_counter:
            lines.append("- 无失分用例")
        lines.append("")
        lines.append("> **给开发的关键信息**：排名靠前的错误类型即为最需优先修复的系统性问题。")

    # 4.7 失分用例明细（可提给开发的"问题清单"，含输入/期望/失分维度）
    if case_results:
        lines.append(f"\n## 失分用例明细（{len(case_results)} 条，按严重程度降序）\n")
        for cr in case_results[:20]:
            uid = cr.get("用例ID", "?")
            cap = cr.get("能力", "")
            inp = cr.get("输入", "")[:60]
            ets = "、".join(ERROR_TYPES.get(e, ERROR_TYPES["other"])["name"] for e in cr.get("错误类型", []))
            lines.append(f"- **{uid}** [{cap}] 最差分 {cr.get('最差得分', '?')} | 错误: {ets}")
            lines.append(f"  输入: {inp}")
            # 失分明细：列出各维度失分原因
            for dim_fail, fdet in (cr.get("失分明细") or {}).items():
                if isinstance(fdet, dict):
                    lines.append(f"    - {dim_fail}: {fdet.get('score', '?')} 分 "
                                 f"({ERROR_TYPES.get(fdet.get('error_type','other'), ERROR_TYPES['other'])['name']}) "
                                 f"— {fdet.get('detail','')[:120]}")
        if len(case_results) > 20:
            lines.append(f"- … 等共 {len(case_results)} 条失分用例，完整清单见结果 YAML `case_results`")
        lines.append("")

    # 4.5 trace 链路（上报过才有）：trace_id 挂在用例上
    case_traces = result.get("case_traces", result.get("traces", []))
    if case_traces:
        base_url = os.getenv("TRACE_PLATFORM_URL", "http://127.0.0.1:8000")
        lines.append(f"\n## Trace 链路（{len(case_traces)} 条用例）\n")
        lines.append(f"可在 trace_platform 查看用例内部链路：`{base_url}`\n")
        lines.append("| 用例ID | 能力 | 维度 | 层 | Trace 链接 |")
        lines.append("|--------|------|------|----|------------|")
        for t in case_traces:
            uid = t.get("用例ID", "?")
            cap = t.get("能力", "")
            dim = t.get("维度", "")
            layer = t.get("层", "")
            tids = t.get("trace_ids", [])
            if tids:
                links = " ".join(f"[{tid[:8]}]({base_url}/api/traces/{tid})"
                                 for tid in tids)
            else:
                links = "-"
            lines.append(f"| {uid} | {cap} | {dim} | {layer} | {links} |")

    # 5. 反哺建议
    lines.append(f"\n## 反哺建议\n")
    dim_counts = result.get("dim_case_counts", {})
    sparse = [d for d, cnt in dim_counts.items() if cnt and cnt < 2]
    if sparse:
        lines.append("以下维度用例偏少，建议补充：")
        for d in sparse:
            lines.append(f"- {d}（当前 {dim_counts[d]} 条）")
    else:
        lines.append("- 各维度用例覆盖均衡，无需补充")

    # 6. 评分等级标准总览（手册原文）
    lines.append(f"\n## 评分等级标准（《AI 测试方法体系手册》原文）\n")
    lines.append("| 得分 | 等级 | 标准 | 发布建议 |")
    lines.append("|------|------|------|----------|")
    for s in range(5, 0, -1):
        g = GRADE_DEFINITIONS[s]
        lines.append(f"| {s} | {g['label']} | {g['standard']} | {g['verdict']} |")

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"报告已生成: {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", required=True, help="评分结果 YAML")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    out = args.out or os.path.join(_ROOT, "report", "评估报告.md")
    generate_report(args.result, out)


if __name__ == "__main__":
    main()
