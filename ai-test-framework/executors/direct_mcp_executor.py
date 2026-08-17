# -*- coding: utf-8 -*-
"""A/D 类直连 MCP 执行器（纯工具测试，不经过 LLM）。

与 GenericMcpExecutor 的区别：
  - GenericMcpExecutor（B/C 类）：LLM 解析自然语言 → 意图 → 选工具 → 调 MCP → 生成回复
  - DirectMcpExecutor（A/D 类）：用例直接指定「工具名 + 参数」→ 直接调 MCP → 比对返回

A 类用例的「输入」格式：
  {"tool_name": "query_daily_sales", "tool_params": {...}}
  {"tool": "query_daily_sales", "params": {...}}      # 兼容别名

返回结构：
  {
    "steps": [{"name": "MCP-{tool}", "type": "SPAN", "input": ..., "output": ..., "metadata": {...}}],
    "tool": tool_name,
    "output": 工具返回文本,
    "tool_correct": True,
    "verify": None,
    "level": "ERROR"/"WARNING"/"",
  }
"""
import asyncio
import json

from generic_mcp_executor import GenericMcpExecutor


class DirectMcpExecutor(GenericMcpExecutor):
    """直连 MCP：按用例给定的工具+参数直接调用，不经过 LLM 意图/回复。

    复用 GenericMcpExecutor 的 MCP 连接（_session_context/_call_tool/_client_headers）。
    """

    def __init__(self, system):
        # 复用父类加载能力目录/配置（但不初始化 LLM，A 类不需要）
        self.sys = self._load_sys(system)
        self.capabilities = self.sys.capabilities

    def _load_sys(self, system):
        # 父类 __init__ 会初始化 LLM，这里手动加载 _SysConfig 避免浪费
        from generic_mcp_executor import _SysConfig
        return _SysConfig(system)

    # ---- 主流程：直连工具调用 ----
    def handle(self, capability, user_input, inp, expected):
        return asyncio.run(self._call_direct(capability, inp, expected))

    async def _call_direct(self, capability, inp, expected):
        """按用例指定的工具+参数直连 MCP，返回结构化结果。"""
        steps = []
        # 取工具名 + 参数（A 类用例输入为工具调用格式）
        if isinstance(inp, dict):
            tool_name = inp.get("tool_name") or inp.get("tool") or ""
            tool_params = inp.get("tool_params") or inp.get("params") or {}
        else:
            tool_name, tool_params = "", {}
        # 兜底：从能力目录取默认工具
        if not tool_name:
            tool_name = self.sys.cap_tool.get(capability, "")

        mcp_input = json.dumps({"toolParams": tool_params}, ensure_ascii=False)
        try:
            stack, session = await self._session_context()
            async with stack:
                is_error, text, latency = await self._call_tool(session, tool_name, tool_params)
        except Exception as e:
            steps.append({
                "name": f"MCP-{tool_name}", "type": "SPAN", "input": mcp_input,
                "output": f"EXCEPTION: {e}",
                "metadata": {"mcp": self.sys.system, "tool": tool_name, "latency_ms": 0},
            })
            return {"error": f"MCP 调用失败: {e}", "level": "ERROR", "block": True,
                    "steps": steps, "tool": tool_name}

        # 工具调用失败 → ERROR
        level = "ERROR" if is_error else ""
        steps.append({
            "name": f"MCP-{tool_name}", "type": "SPAN", "input": mcp_input,
            "output": text, "metadata": {"mcp": self.sys.system, "tool": tool_name,
                                         "latency_ms": latency, "level": level},
        })
        return {
            "steps": steps,
            "tool": tool_name,
            "output": text,
            "tool_correct": True,   # 直连模式无 LLM 选择，视为工具匹配
            "level": level,
            "verify": None,
        }
