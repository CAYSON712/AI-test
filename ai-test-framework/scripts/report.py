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

from rubric.rubric import format_report, score_to_label


def generate_report(result_path, out_path):
    """根据评分结果生成报告"""
    with open(result_path, encoding="utf-8") as f:
        result = yaml.safe_load(f)

    req_type = result.get("req_type", "C")
    system = result.get("system", "")
    runs = result.get("runs", 1)
    dims = result.get("dimensions", {})  # {维度: {avg_score, pass_rate, n, ci}}

    lines = []
    lines.append(f"# AI 测试评估报告\n")
    lines.append(f"- **需求类型**: {req_type} 类")
    lines.append(f"- **被测系统**: {system}")
    lines.append(f"- **采样次数**: 每条 {runs} 次\n")

    # 1. 维度得分表（score = 全局统计分或 avg，score_type 标注评分方式）
    lines.append("## 维度得分（5 分制 Rubric）\n")
    lines.append("| 维度 | 得分 | 评分方式 | 等级 | 通过率 | 采样 | 95%CI |")
    lines.append("|------|------|----------|------|--------|------|-------|")
    for dim, d in sorted(dims.items(), key=lambda x: x[1].get("score", x[1].get("avg_score", 0))):
        score = d.get("score", d.get("avg_score", 0))
        stype = "程序化" if d.get("score_type") == "rate" else "逐用例"
        rate = d.get("pass_rate", 0)
        n = d.get("n", 0)
        ci = d.get("ci", (0, 0))
        lines.append(f"| {dim} | {score:.2f} | {stype} | {score_to_label(score)} | {rate:.0%} | {n} | "
                     f"[{ci[0]:.2f}, {ci[1]:.2f}] |")

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
