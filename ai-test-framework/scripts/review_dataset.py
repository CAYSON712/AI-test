# -*- coding: utf-8 -*-
"""快速数据集内容 review：统计概览 + 质量检查，辅助人工评审。

用法：
  python review_dataset.py                                # review 全部数据集
  python review_dataset.py <dataset.yaml> [更多...]       # review 指定数据集
  python review_dataset.py -a <能力目录.yaml> <dataset.yaml>  # 附带能力覆盖对照

检查项：
  1. 层分布（L1/L2）
  2. 维度分布与均衡性
  3. 能力覆盖（每能力用例数 min/max，能力目录对照）
  4. 期望健康度：缺 intent/output/semantic、block=true 却无 semantic
  5. 标签分布（正常/边界/契约等占比）
  6. 潜在重复（输入 + intent 完全一致的用例组）
  7. 头部「用例数」与实际是否一致
"""
import argparse
import glob
import os
import sys
from collections import Counter, defaultdict

import yaml

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


def load_yaml(path):
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def sem_empty(exp):
    """期望里的 semantic 是否为空（无可用的 fields/contains）。"""
    sem = (exp or {}).get("semantic")
    if not sem:
        return True
    if sem.get("fields") or sem.get("contains"):
        return False
    return True


def review(doc, ability_path=None, name=""):
    cases = doc.get("用例列表") or []
    n = len(cases)
    print("== %s ==" % name)
    print("系统: %s | 类型: %s | 用例: %d" % (doc.get("系统", "?"), doc.get("需求类型", "?"), n))

    hdr = doc.get("用例数")
    if hdr is not None and hdr != n:
        print("  [!] 头部「用例数」声明 %d，实际 %d，不一致" % (hdr, n))

    layer_cnt = Counter(c.get("层") for c in cases)
    print("  层分布: " + ", ".join("%s=%d" % (k, v) for k, v in sorted(layer_cnt.items())))

    dim_cnt = Counter(c.get("维度") for c in cases)
    print("  维度(共%d): " % len(dim_cnt)
          + ", ".join("%s(%d)" % (k, v) for k, v in dim_cnt.most_common()))

    cap_cnt = Counter(c.get("能力") for c in cases)
    if cap_cnt:
        vals = list(cap_cnt.values())
        flag = "  [!] 覆盖不均衡" if max(vals) > min(vals) * 3 else ""
        print("  能力(共%d): 每能力用例 min=%d max=%d avg=%.1f%s"
              % (len(cap_cnt), min(vals), max(vals), sum(vals) / len(vals), flag))

    no_intent = [c.get("用例ID") for c in cases if not (c.get("期望") or {}).get("intent")]
    no_output = [c.get("用例ID") for c in cases if not (c.get("期望") or {}).get("output")]
    no_sem = [c.get("用例ID") for c in cases if sem_empty(c.get("期望"))]
    block_no_sem = [c.get("用例ID") for c in cases
                    if (c.get("期望") or {}).get("block") and sem_empty(c.get("期望"))]
    if no_intent or no_output or no_sem or block_no_sem:
        print("  期望健康度:")
        if no_intent:
            print("    [!] 缺 intent: %d %s" % (len(no_intent), no_intent[:5]))
        if no_output:
            print("    [!] 缺 output: %d %s" % (len(no_output), no_output[:5]))
        if no_sem:
            print("    [!] 缺 semantic(空fields/contains): %d %s" % (len(no_sem), no_sem[:5]))
        if block_no_sem:
            print("    [!] block=true 却无 semantic: %d %s" % (len(block_no_sem), block_no_sem[:5]))
    else:
        print("  期望健康度: intent/output/semantic 全部就绪")

    tag_cnt = Counter(t for c in cases for t in (c.get("标签") or []))
    if tag_cnt:
        print("  标签分布: " + ", ".join("%s=%d" % (k, v) for k, v in tag_cnt.most_common()))

    # 同输入跨维度是设计使然（C 测不同维度、D 测同一工具调用的不同契约面），
    # 只有「同能力+同维度+同输入」才算真重复。
    groups = defaultdict(list)
    for c in cases:
        exp = c.get("期望") or {}
        key = (c.get("能力"), c.get("维度"), str(c.get("输入")),
               exp.get("intent"), bool(exp.get("block")))
        groups[key].append(c.get("用例ID"))
    dups = {k: v for k, v in groups.items() if len(v) > 1}
    if dups:
        print("  [!] 真重复(同能力+维度+输入): %d 组" % len(dups))
        for k, v in list(dups.items())[:5]:
            print("    %s 能力=%s 维度=%s" % (v, k[0], k[1]))
    else:
        print("  真重复: 无")

    if ability_path:
        ab = load_yaml(ability_path)
        ab_caps = []
        for grp in ab.get("能力分组") or []:
            for cap in grp.get("能力列表") or []:
                ab_caps.append(cap.get("能力"))
        ds_caps = set(cap_cnt)
        ab_set = set(ab_caps)
        missing = ab_set - ds_caps
        extra = ds_caps - ab_set
        print("  能力目录对照(目录%d个):" % len(ab_set))
        if missing:
            print("    [!] 目录有、数据集未覆盖: %s" % sorted(missing))
        else:
            print("    目录能力全部有覆盖")
        if extra:
            print("    [!] 数据集有、目录未声明: %s" % sorted(extra))
    print()


def main():
    parser = argparse.ArgumentParser(description="快速数据集内容 review")
    parser.add_argument("datasets", nargs="*", help="数据集文件；缺省则 review datasets/ 下全部")
    parser.add_argument("-a", "--ability", default=None, help="能力目录 yaml（能力覆盖对照）")
    args = parser.parse_args()

    if args.datasets:
        files = [d for d in args.datasets if os.path.isfile(d)]
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        files = sorted(glob.glob(os.path.join(base, "datasets", "*.yaml")))
    if not files:
        print("未找到数据集文件")
        sys.exit(1)

    for f in files:
        doc = load_yaml(f)
        review(doc, args.ability, name=os.path.basename(f))


if __name__ == "__main__":
    main()
