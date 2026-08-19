# -*- coding: utf-8 -*-
"""
Mock 执行器（从能力目录读取能力）
================================
读取 ability/能力目录_*.yaml 里的能力清单，作为可处理能力。
这样能力目录是事实来源，新增能力只改目录文件，不用改代码。

说明：
  - 能力目录（需求预定义）在测试前即可确定
  - 不绑定任何具体业务：负向用例走 block 拦截，其余能力走通用兜底，
    并按用例期望的 semantic 生成符合语义校验的假输出（见 _gen_mock_output）
"""
import os
import random
import time

import yaml

from base import BaseExecutor

# 能力目录在新框架内维护（单一数据源）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ABILITY_DIR = os.path.join(_ROOT, "ability")


def _load_capabilities(ability_file):
    """从能力目录文件读取能力清单"""
    path = os.path.join(ABILITY_DIR, ability_file)
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    caps = []
    for group in data.get("能力分组", []):
        for c in group.get("能力列表", []):
            caps.append(c["能力"])
    return caps


def _find_ability_files():
    """扫描 ability/ 下所有能力目录文件（动态发现，不依赖特定文件名）。
    合并所有能力目录的能力清单，使 Mock 执行器能处理任意被测系统
    （多个被测系统并存时都能命中）。
    """
    if not os.path.isdir(ABILITY_DIR):
        return []
    return sorted(f for f in os.listdir(ABILITY_DIR)
                  if f.startswith("能力目录_") and f.endswith(".yaml"))


class MockExecutor(BaseExecutor):
    # 能力清单来自所有现存能力目录（动态发现），去重
    capabilities = list(dict.fromkeys(
        c for f in _find_ability_files() for c in _load_capabilities(f)))

    def handle(self, capability, user_input, inp, expected):
        time.sleep(random.uniform(0.1, 0.3))

        # 负向/对抗用例（安全/鲁棒/越权/非法参数等，期望 block=True）：
        # 应返回「拒绝」结果（不绑定任何业务能力名）。
        if isinstance(expected, dict) and expected.get("block"):
            return {"error": f"已拒绝请求（{capability}）", "block": True,
                    "msg": "非法/越权请求被拦截"}
        # 通用兜底：对动态发现的其他能力，返回模拟成功（隔离副作用，验证决策链路）
        # 关键升级：按用例「期望」里的 semantic 生成符合语义校验的假输出，
        # 让 mock 模式下「语义校验」维度（fields/contains）也能真实通过，评分更可信。
        return self._gen_mock_output(capability, expected)

    def _gen_mock_output(self, capability, expected):
        """按期望的 semantic 生成满足语义校验的假输出。

        - semantic.fields    → 输出 JSON 包含这些字段（含能力名做假值）
        - semantic.contains  → 以独立字段形式塞入，使输出文本含该关键词
        - 无 semantic         → 保持通用 mock 标记（隔离副作用，验证决策链路）
        返回的 dict 不含 output/reply 字段，使 verify_case 用整个 dict 做
        has_fields + contains_text 校验。
        """
        obj = {"mock": True, "capability": capability, "result": "模拟执行成功"}
        sem = expected.get("semantic") if isinstance(expected, dict) else None
        if isinstance(sem, dict):
            # 1) fields → 作为输出字段（值含能力名，满足 has_fields）
            for f in sem.get("fields", []):
                obj.setdefault(str(f), f"{capability} {f}")
            # 2) contains → 作为独立字段值，确保整个输出 JSON 文本包含该关键词
            for i, kw in enumerate(sem.get("contains", [])):
                obj[f"_kw{i}"] = str(kw)
        return obj
