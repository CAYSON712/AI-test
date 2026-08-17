# -*- coding: utf-8 -*-
"""
AI 测试一键流水线入口（端到端）
===============================
一条命令跑完：数据集生成(可选) → 执行+Rubric评分 → 报告 → trace上报。

用法：
  # 全流程：生成数据集 → 执行 → 报告 → trace（推荐，落地一键跑）
  python scripts/pipeline.py --req-type C --system POS商品管理 --executor auto --runs 3 --trace

  # 只执行已有数据集（跳过生成）
  python scripts/pipeline.py --req-type B --dataset datasets/B_POS_chat.yaml --executor auto

  # 快速 mock 验证（不连真实系统）
  python scripts/pipeline.py --req-type C --system POS商品管理 --executor mock

  # LLM-as-Judge 主观维度打分
  python scripts/pipeline.py --req-type C --system POS商品管理 --executor auto --llm-judge

参数说明：
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
"""
import argparse
import glob
import os
import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "scripts"))
sys.path.insert(0, os.path.join(_ROOT, "executors"))

from scripts.generate_dataset import main as _gen_main
from scripts.run_test import run_dataset


def _gen_dataset(req_type, system, ability, products):
    """生成数据集，返回实际写入的路径。

    关键：不传 --system 给 generate_dataset，让它从能力目录自动读系统名
    （避免 Windows 命令行传中文被终端编码破坏）；文件名为 {req_type}_{实际系统名}.yaml。
    生成后通过 glob 定位最新生成的文件作为 dataset_path。
    """
    argv = ["generate_dataset", "--req-type", req_type]
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


def main():
    parser = argparse.ArgumentParser(description="AI 测试一键流水线")
    parser.add_argument("--req-type", default="C", choices=["A", "B", "C", "D", "E"])
    parser.add_argument("--system", default="POS商品管理")
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
    args = parser.parse_args()

    # 1) 数据集：有 --dataset 则复用；否则自动生成
    dataset_path = args.dataset
    if not dataset_path:
        print(f"① 自动生成 {args.req_type} 类数据集（系统: {args.system}）...")
        dataset_path = _gen_dataset(args.req_type, args.system, args.ability, args.products)
        print(f"   → {dataset_path}")
    else:
        print(f"① 复用数据集 → {dataset_path}")

    # 2) 执行 + 评分 + 报告 + trace
    #    不传 system：让 run_dataset 从数据集读系统名（数据集系统名来自能力目录，
    #    是正确值，规避命令行传中文被终端编码破坏）。
    out = args.out or os.path.join(_ROOT, "results", f"result_{args.req_type}.yaml")
    print(f"② 执行 + 评分（{args.executor}）")
    run_dataset(args.req_type, dataset_path, args.executor, args.runs, out,
                report_trace=args.trace,
                use_llm_judge=args.llm_judge, llm_detail=args.llm_detail,
                auto_report=not args.no_report)

    print("\n✅ 流水线完成")
    print(f"   结果: {out}")
    if not args.no_report:
        print(f"   报告: {os.path.join(_ROOT, 'report', f'评估报告_{args.req_type}.md')}")
    if args.trace:
        print("   trace: 已上报 trace_platform（打开 http://127.0.0.1:8000 查看）")


if __name__ == "__main__":
    main()
