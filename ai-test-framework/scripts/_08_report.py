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
  python _08_report.py --result ../results/result_C_20260927.yaml
  # 不传 --out 时自动命名：report/评估报告_<时间戳>_<类型>_<数据集名>.md
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
    # 错误归因升级：区分失分「该提给谁」
    #   dataset   数据集问题 → 提给测试修数据/期望（不是 AI 系统问题）
    #   ai_system AI 系统问题 → 提给开发
    #   env       环境/前置问题 → 提给运维
    #   test_pass 测试通过（误报）→ 无需处理
    ATTRIBUTIONS = {
        "dataset":    {"name": "数据集问题", "to": "提给测试人员修数据/期望，非 AI 系统问题", "icon": "📊"},
        "ai_system":  {"name": "AI 系统问题", "to": "提给开发（真做错了）", "icon": "🔧"},
        "env":        {"name": "环境/前置问题", "to": "提给运维（连接/ID解析/配置）", "icon": "🌐"},
        "test_pass":  {"name": "测试通过（误报）", "to": "无需处理", "icon": "✅"},
    }

    lines = []
    lines.append(f"# AI 测试评估报告\n")
    lines.append(f"- **需求类型**: {req_type} 类")
    lines.append(f"- **被测系统**: {system}")
    lines.append(f"- **采样次数**: 每条 {runs} 次\n")

    # 1. 维度得分表
    #    ⚠ 口径说明（2026-09-27 修正，勿按旧注释理解）：
    #    旧实现 score = max(rate映射分, avg) 已废弃，两处硬伤：
    #      ① max 是"择优选一个报"= 主动隐藏不利信息，无统计依据；
    #      ② rate映射分（通过率查表的离散分）与 avg（逐用例均值）量纲不同，比大小不成立。
    #    现 score = score_rate（达标率口径分，无 rate 时回退 avg），与 avg 并列展示、不再合并。
    #    通过率以「用例」为统计单位；每条用例跑 k 次（k 由全局 runs 与用例 sample_extra 取大），
    #    ≥1 次达标即判通过（pass@k）。
    max_k = max((d.get("runs", 1) or 1) for d in dims.values()) if dims else 1
    lines.append("## 维度得分（5 分制 Rubric）\n")
    lines.append(f"> 采样：每条用例跑 {max_k} 次（关键用例 sample_extra 自动多跑，见「采样k」列）\n")
    lines.append("> **指标口径**决定多次采样怎么看，分两类：")
    lines.append("> - **一致性**（功能类维度）：多次结果应当一致，看 **pass^k**"
                 "（k 次全对）；5 次里 1 次错即说明有缺陷。")
    lines.append("> - **分布**（性能类维度）：响应受网络/负载影响天然波动，"
                 "看 **平均分 + stdev**；pass^k 会把正常抖动误判成不稳定，故不展示。\n")
    lines.append("> 定义：`pass@k` = 至少一次达标（能力上界）；"
                 "`pass^k` = 每次都达标（可靠性）；`stdev` = 各用例分数的平均标准差\n")
    lines.append("| 维度 | 得分 | 平均分 | 通过率(pass@k) | pass^k | stdev | 口径 | 等级 | 发布建议 | 用例数 | 95%CI |")
    lines.append("|------|------|--------|----------------|--------|-------|------|------|----------|--------|-------|")
    for dim, d in sorted(dims.items(), key=lambda x: x[1].get("score", x[1].get("avg_score", 0))):
        score = d.get("score", d.get("avg_score", 0))
        avg = d.get("avg_score", 0)
        g = grade_info(score)
        rate = d.get("rate", d.get("pass_rate", 0)) or 0
        n = d.get("n", 0)
        ci = d.get("ci", (0, 0))
        mode = d.get("metric_mode") or "一致性"
        pak = d.get("pass_at_k")
        pk = d.get("pass_all_k")
        sd = d.get("stdev")
        # 分布口径（性能类）不展示 pass^k（性能天然波动，展示会误导）
        pk_txt = "—" if pk is None else f"{pk:.0%}"
        sd_txt = "—" if sd is None else f"{sd:.2f}"
        ci_txt = "—" if (not ci or (ci[0] == 0 and ci[1] == 0)) else f"[{ci[0]:.2f}, {ci[1]:.2f}]"
        lines.append(f"| {dim} | {score:.2f} | {avg:.2f} | {rate:.0%} | {pk_txt} | {sd_txt} | "
                     f"{mode} | {g['label']} | {g['verdict']} | {n} | {ci_txt} |")

    # 1.5 稳定性提示：一致性口径下 pass@k 高但 pass^k 明显低 → 时对时错
    unstable = []
    for dim, d in dims.items():
        if (d.get("metric_mode") or "一致性") != "一致性":
            continue
        pak2, pk2 = d.get("pass_at_k"), d.get("pass_all_k")
        if isinstance(pak2, (int, float)) and isinstance(pk2, (int, float)) \
                and pak2 - pk2 >= 0.1:
            unstable.append((dim, pak2, pk2, d.get("stdev")))
    if unstable:
        lines.append("\n### ⚠️ 稳定性提示（pass@k 高但 pass^k 低 = 时对时错）\n")
        lines.append("| 维度 | pass@k | pass^k | 落差 | stdev | 含义 |")
        lines.append("|------|--------|--------|------|-------|------|")
        for dim, pak2, pk2, sd2 in sorted(unstable, key=lambda x: x[2] - x[1]):
            lines.append(f"| {dim} | {pak2:.0%} | {pk2:.0%} | {pak2 - pk2:.0%} | "
                         f"{'—' if sd2 is None else f'{sd2:.2f}'} | "
                         f"能做到但不够稳定，需排查偶发失败原因 |")

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
    #     case_results 里每条失分维度都有 error_type + attribution，聚合成"哪类问题最多"。
    case_results = result.get("case_results", [])
    if case_results:
        from collections import Counter
        type_counter = Counter()
        att_counter = Counter()
        # 关联错误类型 → 归因（一个错误类型可能对应多个归因，用失分明细逐条统计）
        et_att_map = {}   # error_type -> Counter(attribution)
        for cr in case_results:
            for dim_fail, fdet in (cr.get("失分明细") or {}).items():
                if not isinstance(fdet, dict):
                    continue
                et = fdet.get("error_type", "other")
                att = fdet.get("attribution", "ai_system")
                type_counter[et] += 1
                att_counter[att] += 1
                et_att_map.setdefault(et, Counter())[att] += 1
        # 含 pass 不算问题，剔除
        type_counter.pop("pass", None)
        att_counter.pop("test_pass", None)
        et_att_map.pop("pass", None)

        # ★ 问题归属汇总（错误归因升级核心）：三类问题各占多少、该提给谁
        lines.append(f"\n## 问题归属汇总（{sum(att_counter.values())} 处失分 → 该提给谁？）\n")
        lines.append("> 错误归因升级：每条失分都标注「该提给谁」，报告不再把数据问题当系统问题误导开发。")
        total_att = sum(att_counter.values()) or 1
        for att in ("ai_system", "dataset", "env"):
            cnt = att_counter.get(att, 0)
            meta = ATTRIBUTIONS.get(att, ATTRIBUTIONS["ai_system"])
            lines.append(f"- {meta['icon']} **{meta['name']}**：{cnt} 处（{cnt/total_att:.0%}）→ **{meta['to']}**")
        # 优先级建议：AI 系统问题优先给开发，数据集问题优先给测试
        ai_cnt = att_counter.get("ai_system", 0)
        ds_cnt = att_counter.get("dataset", 0)
        lines.append("")
        if ai_cnt > 0:
            lines.append(f"> **给开发**：AI 系统问题 {ai_cnt} 处，见下方「AI 系统问题清单」，是核心待修复项。")
        if ds_cnt > 0:
            lines.append(f"> **给测试**：数据集问题 {ds_cnt} 处（实体/期望/数据缺陷），修数据集后重跑，**不是 AI 系统问题**。")

        lines.append(f"\n### 错误类型分布（{sum(type_counter.values())} 处失分）\n")
        lines.append("| 错误类型 | 失分处数 | 占比 | 主要归因 | 建议修复 |")
        lines.append("|----------|----------|------|----------|----------|")
        total_fail = sum(type_counter.values()) or 1
        for et, cnt in type_counter.most_common():
            meta = ERROR_TYPES.get(et, ERROR_TYPES["other"])
            # 该错误类型的主要归因
            att_c = et_att_map.get(et, Counter())
            main_att = att_c.most_common(1)[0][0] if att_c else "ai_system"
            att_name = ATTRIBUTIONS.get(main_att, {}).get("name", "AI 系统问题")
            lines.append(f"| {meta['name']} | {cnt} | {cnt/total_fail:.0%} | {att_name} | {meta['fix']} |")
        if not type_counter:
            lines.append("- 无失分用例")
        lines.append("")
        lines.append("> **给开发的关键信息**：排名靠前的错误类型即为最需优先修复的系统性问题。")

    # 4.7 失分用例明细（错误归因升级：按「归因」分组——AI 系统问题给开发、数据集问题给测试）
    if case_results:
        def _is(att): return [cr for cr in case_results if cr.get("归因", "ai_system") == att]
        ai_cases = _is("ai_system")
        ds_cases = _is("dataset")
        env_cases = _is("env")

        # 分组展示：AI 系统问题（核心待修）优先
        for att, title in (("ai_system", f"🔧 AI 系统问题（{len(ai_cases)} 条 → 给开发）"),
                           ("dataset", f"📊 数据集问题（{len(ds_cases)} 条 → 给测试，非 AI 问题）"),
                           ("env", f"🌐 环境问题（{len(env_cases)} 条 → 给运维）")):
            group = _is(att)
            if not group:
                continue
            lines.append(f"\n### {title}\n")
            for cr in group[:10]:
                uid = cr.get("用例ID", "?")
                cap = cr.get("能力", "")
                inp = str(cr.get("输入", ""))[:50]
                att_stats = cr.get("归因统计", {})
                # 主要错误类型
                ets = "、".join(ERROR_TYPES.get(e, ERROR_TYPES["other"])["name"]
                                for e in cr.get("错误类型", [])[:3])
                lines.append(f"- **{uid}** [{cap}] 最差分 {cr.get('最差得分', '?')} | {ets}")
                lines.append(f"  输入: {inp}")
                # 只列该归因下最典型的一条失分维度原因（避免 20 维刷屏）
                shown = False
                for dim_fail, fdet in (cr.get("失分明细") or {}).items():
                    if not isinstance(fdet, dict) or fdet.get("attribution") != att:
                        continue
                    lines.append(f"    - [{dim_fail}] {fdet.get('detail','')[:120]}")
                    shown = True
                    break
                if not shown:  # 兜底：没有该归因维度的细节，列最差分维度
                    for dim_fail, fdet in (cr.get("失分明细") or {}).items():
                        if isinstance(fdet, dict):
                            lines.append(f"    - [{dim_fail}] {fdet.get('detail','')[:120]}")
                            break
            if len(group) > 10:
                lines.append(f"- … 等共 {len(group)} 条，完整清单见结果 YAML `case_results`")
        lines.append("")
        lines.append("> **使用说明**：给开发看「AI 系统问题」，给测试看「数据集问题」，避免把数据缺陷当系统 bug。")

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
    # 统一出口打印（调用方无需再打印一次，避免 _06 --report 出现两行重复提示）
    print(f"报告已生成: {out_path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", required=True, help="评分结果 YAML")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()
    import time
    # 默认命名与 _06_run_test.py --report 保持一致：
    #   评估报告_<时间戳>_<类型>_<数据集名>.md
    # 之前只写 时间戳 会产出「评估报告_20260927_203402.md」这种半截名，
    # 多个数据集混在一起时分不清谁是谁。
    if args.out:
        out = args.out
    else:
        ts = time.strftime("%Y%m%d_%H%M%S")
        result_stem = os.path.splitext(os.path.basename(args.result))[0]  # e.g. result_D_20260927
        # 从 result_<类型>_<时间戳> 里解出类型；解不出则整个 stem 当类型占位
        parts = result_stem.split("_")
        req_type = parts[1] if len(parts) > 1 and len(parts[1]) == 1 else ""
        # 数据集名：优先从结果 yaml 的 system 字段推，取不到就用 result 文件名
        dataset_stem = ""
        try:
            import yaml as _yaml
            with open(args.result, encoding="utf-8") as _f:
                _d = _yaml.safe_load(_f) or {}
            dataset_stem = str(_d.get("system") or "").strip()
        except Exception:
            pass
        # 数据集名常已带类型前缀（system 字段一般不含，但保险起见剥一次）。
        # 复用 _06 的实现，保证三处命名逻辑只有一份。
        from scripts._06_run_test import _strip_type_prefix
        dataset_stem = _strip_type_prefix(dataset_stem, req_type)
        tag = "_".join(x for x in [req_type, dataset_stem] if x) or result_stem
        out = os.path.join(_ROOT, "report", f"评估报告_{ts}_{tag}.md")
    generate_report(args.result, out)


if __name__ == "__main__":
    main()
