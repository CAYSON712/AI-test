"""检查当前数据集在数据生命周期覆盖上的缺口。

回答：哪些写用例会真改数据、哪些会被跳过、哪些有保护。
"""
import collections
import glob
import os
import sys

import yaml

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)

_W = ("add_", "create_", "update_", "delete_", "remove_", "set_",
      "bind_", "unbind_", "publish_", "batch_", "generate_")


def main():
    cfg = yaml.safe_load(open(os.path.join(_ROOT, "configs", "PocketStore.yaml"),
                              encoding="utf-8"))
    lc = cfg.get("数据生命周期") or {}
    restorable = {str(r["工具"]) for r in (lc.get("可还原") or [])
                  if isinstance(r, dict) and r.get("工具")}
    creatable = {str(r["工具"]) for r in (lc.get("新建后清理") or [])
                 if isinstance(r, dict) and r.get("工具")}
    irreversible = {str(x) for x in (lc.get("不可恢复") or [])}
    selfdel = {str(r["删除工具"]) for r in (lc.get("自建自删") or [])
               if isinstance(r, dict) and r.get("删除工具")}
    # 能力缺口：写接口存在但缺读取接口，默认跳过并作为缺陷上报
    gaps = set()
    for g in (lc.get("能力缺口") or []):
        if isinstance(g, dict) and g.get("工具"):
            gaps.add(str(g["工具"]))
        elif isinstance(g, str):
            gaps.add(g)

    ds = glob.glob(os.path.join(_ROOT, "datasets", "A_PocketStore*.yaml"))[0]
    cases = yaml.safe_load(open(ds, encoding="utf-8")).get("用例列表") or []

    # 真实工具名集合（来自 config 的 mcp_tools）：
    # 用于剔除 _typo 这类「故意传错工具名」的负向用例——
    # 它们本就不会真执行，不该算进「无保护」。
    real_tools = {str(t.get("name")) for t in (cfg.get("mcp_tools") or [])
                  if isinstance(t, dict) and t.get("name")}

    cnt = collections.Counter()
    cases_by_tool = collections.Counter()
    skipped_fake = 0
    for c in cases:
        t = str(((c.get("输入") or {}).get("tool_name")) or "")
        if not any(t.startswith(w) for w in _W):
            continue
        if t not in real_tools:
            skipped_fake += 1        # 假工具名，不会真执行，不计入
            continue
        cases_by_tool[t] += 1
        if t in gaps:
            cnt["跳过(能力缺口·上报)"] += 1
        elif t in restorable:
            cnt["可快照还原"] += 1
        elif t in creatable:
            cnt["新建可清理"] += 1
        elif t in irreversible and t in selfdel:
            cnt["自建自删"] += 1
        elif t in irreversible:
            cnt["会跳过(不可恢复未配)"] += 1
        else:
            cnt["无保护(会真改数据)"] += 1

    total = sum(cases_by_tool.values())
    print(f"真实写操作用例: {total}")
    print(f"涉及真实写工具: {len(cases_by_tool)}")
    if skipped_fake:
        print(f"（另有 {skipped_fake} 条 _typo 假工具用例，不会真执行，已排除）")
    print()
    for k, v in cnt.most_common():
        print(f"  {k:24s} {v:4d} 条")
    print()

    # 列出无保护的工具
    noprot = sorted({t for t in cases_by_tool
                     if t not in gaps
                     and t not in restorable and t not in creatable
                     and not (t in irreversible and t in selfdel)
                     and t not in irreversible})
    if noprot:
        print(f"⚠ 无保护工具 {len(noprot)} 个（会真改数据且不还原）：")
        for t in noprot:
            print(f"    {cases_by_tool[t]:3d} 条  {t}")
    else:
        print("✅ 所有写工具都有保护")

    skip = sorted({t for t in cases_by_tool
                   if t in irreversible and t not in selfdel})
    if skip:
        print(f"\nℹ 会跳过的工具 {len(skip)} 个（不可恢复且未配自建自删）：")
        for t in skip:
            print(f"    {cases_by_tool[t]:3d} 条  {t}")
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())
