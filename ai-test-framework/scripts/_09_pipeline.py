# -*- coding: utf-8 -*-
"""
AI 测试一键流水线入口（端到端）
===============================
一条命令跑完：框架自测 → 数据集生成(可选) → 数据集校验(门禁) → 执行+Rubric评分 → 报告 → trace上报。

用法：
  # 全流程：生成数据集 → 校验 → 执行 → 报告 → trace（推荐，落地一键跑）
  python scripts/_09_pipeline.py --req-type C --system 某系统 --executor auto --runs 3 --trace

  # 只执行已有数据集（跳过生成，但仍会校验）
  python scripts/_09_pipeline.py --req-type B --dataset "datasets/B_某系统.yaml" --executor auto

  # 快速 mock 验证（不连真实系统）
  python scripts/_09_pipeline.py --req-type C --system 某系统 --executor mock

  # LLM-as-Judge 主观维度打分
  python scripts/_09_pipeline.py --req-type C --system 某系统 --executor auto --llm-judge

  # 快捷别名（免去手敲中文数据集路径 —— 中文 argv 在 PowerShell 下会乱码）
  python scripts/_09_pipeline.py --job unattend --executor real
  python scripts/_09_pipeline.py --job both --executor real

参数说明：
  --job        快捷别名：retailpos / unattended / both（查 _10_maintain.SYSTEMS，
               自动填 --req-type/--dataset/--system，规避中文路径乱码）
  --req-type   A/B/C/D/E（需求类型，决定执行器）
  --system     被测系统名（用于匹配 ability/ 能力目录；中文系统名优先以能力目录内字段为准）
  --dataset    已有数据集路径（给则跳过生成）
  --executor   mock / auto / real（执行器模式）
  --runs       每条用例采样次数（pass@k 的 k；关键用例 sample_extra 自动多跑）
  --trace      上报 trace_platform（需先启动该服务）
  --no-report  不生成报告（默认自动生成）
  --llm-judge  启用 LLM-as-Judge
  --llm-detail LLM 打分输出详细理由（配合 --llm-judge）
  --ability    指定能力目录 yaml（默认按系统名自动发现）
  --products   指定实体清单 yaml（默认自动发现）
  --out        结果 yaml 输出路径
  --skip-selftest 跳过框架回归自测（默认执行）
  --skip-validate 跳过数据集校验门禁（不推荐；默认执行且 FAIL 则中止）
  --skip-review   跳过数据集软 review（默认执行，仅提示不阻断）
  --force          校验 FAIL 时仍继续执行（仅调试用）

流水线阶段：
  ⓪ 框架自测 → ① 生成/复用数据集 → ①.5 硬校验(门禁) → ①.6 软review(提醒)
  → ② 执行+评分+报告
"""
import argparse
import contextlib
import glob
import io
import os
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
sys.path.insert(0, os.path.join(_ROOT, "executors"))

from scripts._02_generate_dataset import main as _gen_main
from scripts._06_run_test import _build_out_path, _load_dataset_type, run_dataset


def _run_framework_selftest():
    """跑框架回归自测（_10_maintain selftest）。返回 True=通过。

    作为流水线第 0 步：改动框架代码后先确认核心逻辑没被破坏，
    避免「带着已知回归去跑数据集」浪费时间。
    """
    import _10_maintain as M
    print("⓪ 框架回归自测 ...")
    rc = M.cmd_selftest([])
    if rc != 0:
        print("\n⚠ 框架自测未通过 —— 说明框架代码存在回归，"
              "请先修复（或 --skip-selftest 跳过，但不推荐）")
    return rc == 0


def _discover_ability(dataset_path, explicit=None):
    """按数据集「系统」字段反查能力目录；失败返回 None。"""
    if explicit and os.path.exists(explicit):
        return explicit
    try:
        import yaml
        doc = yaml.safe_load(open(dataset_path, encoding="utf-8")) or {}
        sys_name = doc.get("系统") or ""
    except Exception:
        return None
    ab_dir = os.path.join(_ROOT, "ability")
    for f in glob.glob(os.path.join(ab_dir, "*.yaml")):
        try:
            import yaml
            d = yaml.safe_load(open(f, encoding="utf-8")) or {}
        except Exception:
            continue
        if d.get("系统") == sys_name:
            return f
    return None


def _validate_dataset_gate(dataset_path, ability, products):
    """数据集校验门禁。返回 True=可继续执行（PASS 或仅有警告）。

    这是流水线最容易漏掉的一环：不校验就执行，会把「生成器产出的
    矛盾用例」（如期望正常执行却传非法参数）一路带到执行阶段，
    跑完才发现问题、白烧一轮 MCP 调用。
    """
    import _03_validate_dataset as V
    print("①.5 数据集校验（门禁）...")
    if not ability:
        print("   ⚠ 未能定位能力目录，跳过校验（建议显式传 --ability）")
        return True
    dim_dir = os.path.join(_ROOT, "dimensions")
    V.DIM_A = os.path.join(dim_dir, "A_MCP工具.yaml")
    V.DIM_B = os.path.join(dim_dir, "B_Agent系统.yaml")
    V.DIM_C = os.path.join(dim_dir, "C_AgentMCP集成.yaml")
    V.DIM_D = os.path.join(dim_dir, "D_Skill原子能力.yaml")
    V.DIM_E = os.path.join(dim_dir, "E_RAG知识库.yaml")
    ck = V.Checker(dataset_path, ability, products)
    rc = ck.run(products)          # 0=PASS, 1=有致命/错误
    if rc != 0:
        print("\n⚠ 数据集校验 FAIL（致命/错误见上）—— 已中止，未进入执行阶段。")
        print("   修复后重跑；仅调试时可加 --force 强行继续。")
    else:
        print("   校验通过（警告项请人工确认，不影响执行）")
    return rc == 0


def _review_dataset_hint(dataset_path, ability):
    """数据集软 review（提醒，不阻断）。

    与 _validate_dataset_gate 的分工：
      validate（硬，门禁）：能不能跑 —— 数据结构 vs 能力目录，FAIL 则中止
      review （软，提醒）：好不好   —— 真重复 / 能力覆盖均衡 / 期望健康度

    为什么要放流水线里：review 能抓到的问题（尤其「真重复」）validate 完全不报，
    但会让你白烧 MCP 调用；跑完才发现 = 成本已经付出。放这里把发现时机提前到
    执行之前。**不阻断**：覆盖不均衡可能是有意为之，由人判断。
    """
    import _10_maintain as M
    print("①.6 数据集软 review（提醒，不阻断）...")
    try:
        doc = M._review_load(dataset_path)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            M._review_one(doc, ability, name=os.path.basename(dataset_path))
        report = buf.getvalue()
    except Exception as e:
        print(f"   ⚠ review 执行失败（不影响执行）：{e}")
        return True

    # 只挑「会浪费执行成本」的关键项提示，避免刷屏。
    # 注意：block=true 的拒绝类用例本就不需要 semantic，review 的
    # 「缺 semantic」统计会把它们一起计入，这里只看「缺 semantic」但在
    # 完整报告里另做区分——故此处只提示真重复/覆盖不均衡这两类
    # 「确定白跑/结论有偏」的硬信息。
    lines = report.splitlines()
    key = [ln for ln in lines
           if ("真重复(同能力+维度+输入)" in ln and "无" not in ln)
           or "覆盖不均衡" in ln]
    if key:
        print("   ⚠ 发现以下问题（建议先确认再执行，避免白跑）：")
        for ln in key:
            print("   " + ln.strip())
    else:
        print("   未发现明显问题 ✅")
    # 完整报告落盘，供细看
    try:
        os.makedirs(os.path.join(_ROOT, "results"), exist_ok=True)
        with open(os.path.join(_ROOT, "results", "pipeline_review.txt"),
                  "w", encoding="utf-8") as f:
            f.write(report)
        print("   （完整 review 见 results/pipeline_review.txt）")
    except Exception:
        pass
    return True


def _gen_dataset(req_type, system, ability, products):
    """生成数据集，返回实际写入的路径。

    关键：不传 --system 给 _02_generate_dataset，让它从能力目录自动读系统名
    （避免 Windows 命令行传中文被终端编码破坏）；文件名为 {req_type}_{实际系统名}.yaml。
    生成后通过 glob 定位最新生成的文件作为 dataset_path。
    """
    argv = ["_02_generate_dataset", "--req-type", req_type]
    if ability:
        argv += ["--ability", ability]
    if products:
        argv += ["--products", products]
    _saved = sys.argv
    sys.argv = argv
    try:
        _gen_main()
    finally:
        sys.argv = _saved

    # 定位刚生成的数据集（datasets/{req_type}_*.yaml，取最新）
    pat = os.path.join(_ROOT, "datasets", f"{req_type}_*.yaml")
    matches = glob.glob(pat)
    if not matches:
        raise RuntimeError(f"生成后未找到数据集: {pat}")
    return max(matches, key=os.path.getmtime)


# job 别名 → (系统别名, 需求类型)。系统元数据来自 _10_maintain.SYSTEMS（单一事实源），
# 避免"别名表分散多处、改一处漏一处"。数据集路径由系统中文名拼出。
JOB_ALIASES = {
    "retailpos":  ("retailpos", "A"),   # A_RetailPOS数据查询
    "unattended": ("unattended", "D"),  # D_小韩面无人值守门禁
}


def _resolve_job(job):
    """把 --job 别名解成 (req_type, dataset_path, system)。

    用途：中文数据集路径在 PowerShell 下会乱码，用别名可完全绕开；
    同时也让"只跑某系统的某个类型"变成一行命令。
    """
    import _10_maintain as M
    if job not in JOB_ALIASES:
        raise SystemExit(f"未知 --job: {job}（可选: {', '.join(JOB_ALIASES)}）")
    sys_alias, req_type = JOB_ALIASES[job]
    cfg = M.SYSTEMS[sys_alias]
    dataset = M._out_name(cfg, req_type)
    if not os.path.exists(dataset):
        raise SystemExit(f"--job {job} 对应的数据集不存在: {dataset}")
    return req_type, dataset, cfg["cn"]


def main():
    parser = argparse.ArgumentParser(description="AI 测试一键流水线")
    parser.add_argument("--job", default=None,
                        help="快捷别名：retailpos / unattended（自动填 --req-type/--dataset/--system）")
    parser.add_argument("--req-type", default=None, choices=["A", "B", "C", "D", "E"])
    parser.add_argument("--system", default=None,
                        help="被测系统名（默认按需求类型自动发现）")
    parser.add_argument("--dataset", default=None, help="已有数据集路径（给则跳过生成）")
    parser.add_argument("--executor", default="auto", choices=["mock", "auto", "real"])
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--trace", action="store_true", help="上报 trace_platform")
    parser.add_argument("--no-report", action="store_true", help="不生成报告")
    parser.add_argument("--llm-judge", action="store_true")
    parser.add_argument("--llm-detail", action="store_true")
    parser.add_argument("--ability", default=None)
    parser.add_argument("--products", default=None)
    parser.add_argument("--out", default=None, help="结果 yaml 输出路径")
    parser.add_argument("--skip-selftest", action="store_true",
                        help="跳过框架回归自测（默认执行）")
    parser.add_argument("--skip-validate", action="store_true",
                        help="跳过数据集校验门禁（不推荐）")
    parser.add_argument("--skip-review", action="store_true",
                        help="跳过数据集软 review（仅提示，不阻断；一般无需跳过）")
    parser.add_argument("--force", action="store_true",
                        help="校验 FAIL 时仍继续执行（仅调试用）")
    args = parser.parse_args()

    # --job 别名优先：解出 req_type / dataset / system，其余参数照旧
    if args.job:
        args.req_type, args.dataset, args.system = _resolve_job(args.job)
        print(f"① 使用别名 --job {args.job} → {args.req_type} 类 / {args.system}\n")
    # 非别名模式：--req-type 缺省回退 C（保持旧行为）
    if args.req_type is None:
        args.req_type = "C"

    # 0) 框架回归自测：先确认框架本身没被改坏
    if not args.skip_selftest:
        if not _run_framework_selftest():
            print("\n[中止] 框架自测未通过。修复后重跑，或 --skip-selftest 强行继续。")
            return 1
        print("   框架自测通过 ✅\n")
    else:
        print("⓪ 已跳过框架自测（--skip-selftest）\n")

    # 1) 数据集：有 --dataset 则复用；否则自动生成
    dataset_path = args.dataset
    if dataset_path:
        # --dataset 允许传相对路径（相对 ai-test-framework/），不受当前工作目录影响；
        # 否则从不同目录敲命令会 FileNotFoundError。
        if not os.path.isabs(dataset_path):
            cand = os.path.join(_ROOT, dataset_path)
            if os.path.exists(cand):
                dataset_path = cand
            elif not os.path.exists(dataset_path):
                raise SystemExit(f"--dataset 不存在: {args.dataset}"
                                 f"（已尝试: {cand}）")
    if not dataset_path:
        print(f"① 自动生成 {args.req_type} 类数据集（系统: {args.system or '自动发现'}）...")
        dataset_path = _gen_dataset(args.req_type, args.system, args.ability, args.products)
        print(f"   → {dataset_path}")
    else:
        print(f"① 复用数据集 → {dataset_path}")

    # 1.5) 数据集校验门禁（默认强制）
    ability = _discover_ability(dataset_path, args.ability)
    if not args.skip_validate:
        if not _validate_dataset_gate(dataset_path, ability, args.products) and not args.force:
            return 1
    else:
        print("①.5 已跳过数据集校验（--skip-validate，不推荐）\n")

    # 1.6) 数据集软 review（提醒，不阻断）—— 把「真重复/覆盖不均」等
    #      会浪费执行成本的问题提前到执行之前暴露
    if not args.skip_review:
        _review_dataset_hint(dataset_path, ability)
        print()
    else:
        print("①.6 已跳过数据集软 review（--skip-review）\n")

    # 2) 执行 + 评分 + 报告 + trace
    #    不传 system：让 run_dataset 从数据集读系统名（数据集系统名来自能力目录，
    #    是正确值，规避命令行传中文被终端编码破坏）。
    #    命名与 _06_run_test.py 保持一致：result_<类型>_<YYYYMMDD>.yaml
    #    （同天重跑自动加 _HHMMSS；此前这里漏改，会覆盖旧结果）
    # 类型以「数据集里的需求类型」为准，与 _06/报告命名保持一致（防 result=C、report=D）。
    # 复用 _06 的公共函数，保证三处命名逻辑只有一份实现。
    _eff_type = _load_dataset_type(dataset_path, args.req_type)
    out = args.out or _build_out_path(os.path.join(_ROOT, "results"), _eff_type)
    print(f"② 执行 + 评分（{args.executor}）")
    run_dataset(args.req_type, dataset_path, args.executor, args.runs, out,
                report_trace=args.trace,
                use_llm_judge=args.llm_judge, llm_detail=args.llm_detail,
                auto_report=not args.no_report)

    print("\n[OK] 流水线完成")
    print(f"   结果: {out}")
    if not args.no_report:
        print("   报告: report/ 目录，命名=评估报告_<时间戳>_<类型>_<数据集名>.md（见上方『报告已自动生成』提示）")
    if args.trace:
        print("   trace: 已上报 trace_platform（打开 http://127.0.0.1:8000 查看）")
    return 0


if __name__ == "__main__":
    sys.exit(main() or 0)
