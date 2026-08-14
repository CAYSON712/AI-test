# -*- coding: utf-8 -*-
"""
执行器注册表（通用化路由）
==========================
统一管理所有执行器。核心思路：**按需求类型(A-E)选接入方式，按系统名加载配置**。

接入方式（由需求类型决定）：
  - A / D：直连 MCP 工具（generic_mcp_executor）
  - B / C：对话式 Agent（C=Agent+MCP 直连；B=Agent 对话，走 generic_mcp_executor 或 mock）
  - E：RAG 知识库（generic_rag_executor：检索 + 生成）

路由策略：
  - mode="mock"：只注册 Mock 执行器（隔离副作用，测 B/D 决策层）
  - mode="real"：真实执行器优先（按需求类型 + 系统加载配置驱动执行器）
  - mode="auto"：配置了 token 则真实优先，否则纯 Mock

新增系统（如客流/摄像头/知识库）：准备 ability/能力目录_<系统>.yaml + configs/<系统>.yaml，
然后 get_registry(system=..., ...) 即可，无需改 Python 逻辑。
"""
import os

from mock_executor import MockExecutor
from generic_mcp_executor import GenericMcpExecutor
from generic_rag_executor import GenericRagExecutor


def _resolve_system(system, req_type):
    """未显式指定系统时，按需求类型给默认系统名"""
    if system:
        return system
    # 默认系统（示例）：POS 商品管理
    return "POS_商品管理"


class ExecutorRegistry:
    def __init__(self, mode="auto", system=None, req_type="C"):
        self.mode = mode
        self.system = _resolve_system(system, req_type)
        self.req_type = req_type
        self.executors = []
        self._init_executors()

    def _make_executor(self):
        """按需求类型创建对应的配置驱动执行器实例"""
        try:
            if self.req_type == "E":
                return GenericRagExecutor(self.system)
            return GenericMcpExecutor(self.system)
        except Exception as e:
            print(f"⚠ 加载真实执行器失败（{e}），仅用 Mock")
            return None

    def _init_executors(self):
        if self.mode == "mock":
            self.register(MockExecutor())
            return
        # real / auto：按需求类型创建配置驱动的真实执行器
        real_exec = self._make_executor()
        has_cap = real_exec and real_exec.capabilities
        # RAG 执行器内置 Mock 检索，无需 token 即可产出有效结果；
        # MCP 执行器需 token 才连真实接口（无 token 时降级 Mock）
        is_rag = self.req_type == "E"
        has_token = getattr(real_exec, "sys", None) and bool(getattr(real_exec.sys, "token", ""))
        is_real = (self.mode == "real") or (self.mode == "auto" and (has_token or is_rag))

        if is_real and real_exec and has_cap:
            self.register(real_exec)          # 真实执行器（仅当有配置+能力时）
            self.register(MockExecutor())     # Mock 兜底：处理真实不支持的能力
        else:
            # 无配置/无能力/auto无token：纯 Mock（不误用他系统配置）
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


def get_registry(mode="auto", system=None, req_type="C"):
    global _registry
    # 每次调用按 系统+模式 重新解析，避免缓存串系统
    _registry = ExecutorRegistry(mode=mode, system=system, req_type=req_type)
    return _registry
