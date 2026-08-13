# -*- coding: utf-8 -*-
"""
执行器注册表
===========
统一管理所有执行器。新增系统 = 在 EXECUTORS 里注册新 executor。

路由策略：
  - 配置了 POS_MCP_TOKEN 时，真实 MCP 执行器优先（所有能力走真实调用）
  - 未配置 token 时，只注册 Mock 执行器（默认）
  - 可用 get_registry(mode="real"|"mock") 强制指定模式
"""
import os

from mock_executor import MockExecutor
from pos_mcp_executor import PosMcpExecutor  # 真实 POS MCP 执行器（需配置 token）


class ExecutorRegistry:
    def __init__(self, mode="auto"):
        # mode: auto(自动) / real(强制真实) / mock(强制 Mock)
        self.mode = mode
        self.executors = []
        has_token = bool(os.getenv("POS_MCP_TOKEN"))

        if mode == "real":
            # 强制真实：真实优先，Mock 兜底（处理真实不支持的能力）
            self.register(PosMcpExecutor())
            self.register(MockExecutor())
        elif mode == "mock":
            # 强制 Mock
            self.register(MockExecutor())
        else:
            # auto：有 token 则真实优先，否则纯 Mock
            if has_token:
                self.register(PosMcpExecutor())
                self.register(MockExecutor())
            else:
                self.register(MockExecutor())

    def register(self, executor):
        self.executors.append(executor)

    def dispatch(self, case):
        """按用例的「能力」分发到对应执行器（按注册顺序，优先命中）"""
        cap = case.get("能力", "")
        for executor in self.executors:
            if executor.can_handle(cap):
                return executor.execute(case)
        return None  # 没有执行器能处理


_registry = None


def get_registry(mode="auto"):
    global _registry
    if _registry is None:
        _registry = ExecutorRegistry(mode)
    return _registry
