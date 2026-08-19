# -*- coding: utf-8 -*-
"""
执行器注册表（通用化路由）
==========================
统一管理所有执行器。核心思路：**按需求类型(A-E)选接入方式，按系统名加载配置**。

接入方式（由需求类型决定）：
  - A / D：直连 MCP 工具（generic_mcp_executor）
  - B：纯对话 Agent（generic_chat_executor：不连 MCP，只测 Agent 决策/话术）
  - C：Agent + MCP 对话式直连（generic_mcp_executor）
  - E：RAG 知识库（generic_rag_executor：检索 + 生成）

B 类纯对话判定：
  - 能力目录声明了「工具」（有真实 MCP）→ 回退 generic_mcp_executor（当它具备工具能力）
  - 否则 → 纯对话执行器（只测决策，Mock 工具）

路由策略：
  - mode="mock"：只注册 Mock 执行器（隔离副作用，测 B/D 决策层）
  - mode="real"：真实执行器优先（按需求类型 + 系统加载配置驱动执行器）
  - mode="auto"：配置了 token 则真实优先，否则纯 Mock

新增系统（如客流/摄像头/知识库）：准备 ability/能力目录_<系统>.yaml + configs/<系统>.yaml，
然后 get_registry(system=..., ...) 即可，无需改 Python 逻辑。
"""
import os

import yaml

from mock_executor import MockExecutor
from generic_mcp_executor import GenericMcpExecutor
from generic_rag_executor import GenericRagExecutor
from direct_mcp_executor import DirectMcpExecutor
from generic_chat_executor import GenericChatExecutor


def _resolve_system(system, req_type):
    """未显式指定系统时，按需求类型自动发现可用系统（通用化）。

    规则：
      1. 优先按「连接.需求类型」匹配 configs/<系统>.yaml（要求一致或未声明类型）
      2. 兜底：取 ability/ 能力目录第一个可用系统
      3. 都没有 → 通用占位名（后续加载会提示缺配置）
    """
    if system:
        return system
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # 1) 按需求类型匹配 configs/<系统>.yaml 的「连接.需求类型」
    cfg_dir = os.path.join(root, "configs")
    if os.path.isdir(cfg_dir):
        for f in sorted(os.listdir(cfg_dir)):
            if not f.endswith(".yaml"):
                continue
            try:
                with open(os.path.join(cfg_dir, f), encoding="utf-8") as fh:
                    _cfg = yaml.safe_load(fh) or {}
                _rtype = (_cfg.get("连接") or {}).get("需求类型")
            except Exception:
                _rtype = None
            if not _rtype or _rtype == req_type:
                return os.path.splitext(f)[0]
    # 2) 兜底：能力目录第一个可用系统
    abi_dir = os.path.join(root, "ability")
    if os.path.isdir(abi_dir):
        for f in sorted(os.listdir(abi_dir)):
            if f.startswith("能力目录_") and f.endswith(".yaml"):
                return f[len("能力目录_"):-len(".yaml")]
    return "被测系统"


class ExecutorRegistry:
    def __init__(self, mode="auto", system=None, req_type="C"):
        self.mode = mode
        self.system = _resolve_system(system, req_type)
        self.req_type = req_type
        self.executors = []
        self._init_executors()

    def _make_executor(self):
        """按需求类型创建对应的配置驱动执行器实例。
        A/D=直连 MCP（纯工具，不过 LLM）；B=纯对话（不连 MCP）；
        C=Agent+MCP；E=RAG 检索。
        """
        try:
            if self.req_type in ("A", "D"):
                return DirectMcpExecutor(self.system)
            if self.req_type == "E":
                return GenericRagExecutor(self.system)
            if self.req_type == "B":
                return GenericChatExecutor(self.system)
            return GenericMcpExecutor(self.system)
        except Exception as e:
            print(f"⚠ 加载真实执行器失败（{e}），仅用 Mock")
            return None

    def _init_executors(self):
        if self.mode == "mock":
            # E 类：RAG 执行器自带内置 Mock 检索，mock 模式也注册它，
            # 否则 E 数据集在 mock 模式会被 MockExecutor 判定"无执行器能处理"。
            if self.req_type == "E":
                rag = self._make_executor()
                if rag and rag.capabilities:
                    self.register(rag)
            self.register(MockExecutor())
            return
        # real / auto：按需求类型创建配置驱动的真实执行器
        real_exec = self._make_executor()
        has_cap = real_exec and real_exec.capabilities
        # RAG 执行器内置 Mock 检索，无需 token 即可产出有效结果；
        # MCP 执行器需 token 才连真实接口（无 token 时降级 Mock）；
        # B 类纯对话执行器不连 MCP，无需 token 即可跑（只测决策层）。
        is_rag = self.req_type == "E"
        is_chat = self.req_type == "B"
        has_token = getattr(real_exec, "sys", None) and bool(getattr(real_exec.sys, "token", ""))
        # B 类纯对话：无需 token；若真实执行器是 C 类（有工具能力）则仍需 token
        is_real = (self.mode == "real") or (self.mode == "auto" and (has_token or is_rag or is_chat))

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
