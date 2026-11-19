# -*- coding: utf-8 -*-
"""MCP schema 快照的统一加载入口（生成器 / 校验器 / 执行器共用）。

为什么要有这个模块
==================
schema 快照（*.raw.json）在仓库里可能同时存在两份：

    schemas/<系统>.raw.json              —— 随仓库入库的共享快照
    results/mcp_schemas/<系统>.raw.json  —— _01_mcp_setup 刚拉取的最新版

历史上 5 个地方各自实现了「找文件 + 解析」的逻辑，口径各不相同：

    _02_generate_dataset.py : load_param_types / load_param_descriptions
    _03_validate_dataset.py : _tool_param_props / _load_schema_enums / _schema_enums

其中 4 处写的是「按目录顺序取第一个命中」——**入库的旧快照会永久压住刚
刷新的新版**。2026-11-18 实际踩坑：

    schemas/PocketStore.raw.json          186KB  2026-10-08  ← 旧
    results/mcp_schemas/PocketStore.raw.json  192KB  2026-11-18  ← 新

生成器读到旧版 → localizedNames 的 items.type 丢失 → 又生成出字符串数组；
新参数 repeatSelection 完全看不见 → 参数完整性用例永远不生成。
而校验器里只有一处改成「取最新」，其余仍是旧口径，于是出现
「数据集实际有 76 处类型错误，校验器却报 0 错误」的自相矛盾。

本模块把口径统一为一句话：**同名快照取修改时间最新的那一份**，
并允许 configs 的「原始schema」显式指定文件名（应对
「系统名 ≠ schema 文件名」的情况，如 POS Local路由 ↔ roosterpos）。
"""

import glob
import json
import os

# 相对仓库根的两处候选目录（顺序仅作同 mtime 时的兜底优先级）
_SCHEMA_DIRS = ("schemas", os.path.join("results", "mcp_schemas"))

# 参数外壳名：多数 MCP 把真参数包在 parameters 里
_WRAPPER_KEYS = ("parameters", "params", "arguments", "args")


def repo_root():
    """仓库根（scripts/ 的上一级）。"""
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def schema_dirs(root=None):
    """返回候选目录的绝对路径列表。"""
    root = root or repo_root()
    return [os.path.join(root, d) for d in _SCHEMA_DIRS]


def find_schema(system=None, declared=None, root=None, suffix=".raw.json"):
    """定位某个系统最合适的 schema 快照文件。

    参数
    ----
    system   : 系统名（用于按文件名模糊匹配，如 "PocketStore商家经营后台"）
    declared : configs 里「原始schema」显式声明的文件名（优先）
    root     : 仓库根，默认自动探测
    suffix   : 扩展名，默认 .raw.json（完整 schema，含 type/description）

    返回
    ----
    命中文件的绝对路径；找不到返回 None。

    选择规则（关键）
    ----------------
    1. 若给了 declared，只在同名文件里挑 —— 同名多份时取 **mtime 最新**。
    2. 否则按 system 模糊匹配文件名，同样多份取 mtime 最新。
    3. 仍然没有就返回 None（调用方自行降级，不要静默当作“无 schema”）。

    ⚠ 不要退化成「按目录顺序取第一个」：入库旧快照会压住刚刷新的新版，
      这是 2026-11-18 那次 JSON 类型漏校验的根因。
    """
    dirs = [d for d in schema_dirs(root) if os.path.isdir(d)]
    if not dirs:
        return None

    # 1) 显式声明
    if declared:
        best = None
        for d in dirs:
            p = os.path.join(d, str(declared))
            if os.path.exists(p):
                mt = _mtime(p)
                if best is None or mt > best[0]:
                    best = (mt, p)
        if best:
            return best[1]

    # 2) 按系统名模糊匹配
    if system:
        norm_sys = str(system).replace("_", "").replace(" ", "").lower()
        best = None
        for d in dirs:
            for p in glob.glob(os.path.join(d, "*" + suffix)):
                fn = os.path.basename(p)[: -len(suffix)]
                norm_fn = fn.replace("_", "").lower()
                if not norm_sys or not norm_fn:
                    continue
                if norm_fn in norm_sys or norm_sys in norm_fn:
                    mt = _mtime(p)
                    if best is None or mt > best[0]:
                        best = (mt, p)
        if best:
            return best[1]

    return None


def load_schema(system=None, declared=None, root=None, suffix=".raw.json"):
    """加载 schema 快照，返回 {"tools": [{name, description, inputSchema}, ...]}。

    找不到或解析失败返回 {}（绝不抛异常打断生成/校验流程）。
    返回值里的 tools 已做「字段名归一」：兼容新旧 MCP SDK 的
    inputSchema / input_schema 两种写法。
    """
    path = find_schema(system=system, declared=declared, root=root, suffix=suffix)
    if not path:
        return {}, None
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return {}, path

    # 兼容两种外层结构：{"tools": [...]} 或直接是 list
    tools = doc.get("tools") if isinstance(doc, dict) else doc
    if not isinstance(tools, list):
        return {}, path

    norm = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        sch = t.get("inputSchema") or t.get("input_schema") or {}
        if not isinstance(sch, dict):
            sch = {}
        norm.append({
            "name": t.get("name") or "",
            "description": t.get("description") or "",
            "inputSchema": sch,
        })
    return {"tools": norm}, path


def unwrap_props(schema):
    """从 inputSchema 里取出「真参数」的 properties（自动剥掉参数外壳）。"""
    if not isinstance(schema, dict):
        return {}
    props = schema.get("properties") or {}
    if isinstance(props, dict) and len(props) == 1:
        (only_key, only_val), = props.items()
        if only_key in _WRAPPER_KEYS and isinstance(only_val, dict):
            inner = only_val.get("properties")
            if isinstance(inner, dict) and inner:
                return inner
    return props if isinstance(props, dict) else {}


def param_props_by_tool(system=None, declared=None, root=None):
    """{工具名: {参数名: schema 声明}}，参数已剥壳。

    这是生成器与校验器**共用的唯一口径**：
      - 生成器据此按真实类型造值（array<object> / object / enum ...）
      - 校验器据此做类型校验与参数覆盖率统计
    两边只要都调这一个函数，就不会再出现「一边认为有该参数、另一边认为没有」。
    """
    doc, _ = load_schema(system=system, declared=declared, root=root)
    out = {}
    for t in (doc.get("tools") or []):
        nm = t.get("name")
        props = unwrap_props(t.get("inputSchema"))
        if nm and props:
            out[nm] = props
    return out


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0
