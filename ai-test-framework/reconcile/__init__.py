# -*- coding: utf-8 -*-
"""写入对账模块（通用化）。

解决「只断言 code=0 抓不到假成功」的判定缺口。

设计原则：
  - 框架代码零业务硬编码：所有工具名、字段名、比对方式都来自 configs/*.yaml 的「写入对账」段
  - 换系统只需写一份新 config，不改本目录代码
"""

from .reconciler import (
    ReconcileSpec,
    load_reconcile_specs,
    build_reconcile_cases,
    judge_reconcile,
    extract_list,
    compare_states,
)

__all__ = [
    "ReconcileSpec",
    "load_reconcile_specs",
    "build_reconcile_cases",
    "judge_reconcile",
    "extract_list",
    "compare_states",
]
