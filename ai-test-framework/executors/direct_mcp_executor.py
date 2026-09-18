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
import os

from generic_mcp_executor import GenericMcpExecutor


def _allow_destructive():
    """是否允许执行高危（破坏性写入）用例。由环境变量 ALLOW_DESTRUCTIVE=1 显式开启。

    背景：某 MCP 的 delete_products_by_ids 传入不存在的 ID 时忽略过滤、真实删除了
    983 个商品。故破坏性用例默认跳过，仅在隔离测试环境显式开启后才执行。
    """
    return str(os.environ.get("ALLOW_DESTRUCTIVE", "")).strip().lower() in ("1", "true", "yes")


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
        # 高危用例保护：破坏性写入（delete/batch_update 等）默认跳过，
        # 仅在显式开启 ALLOW_DESTRUCTIVE=1 且确认是隔离测试环境时才执行。
        # 背景：某 MCP 传不存在的 ID 也会真实删掉全量数据，故默认不执行。
        if isinstance(expected, dict) and expected.get("高危") and not _allow_destructive():
            tool_guess = (inp or {}).get("tool_name") if isinstance(inp, dict) else ""
            return {
                "steps": [], "tool": tool_guess,
                "output": "高危用例已跳过（破坏性写入，需设置 ALLOW_DESTRUCTIVE=1 且确认"
                          "为隔离测试环境后才会执行）",
                "tool_correct": None, "level": "INFO", "block": None,
                "biz_error": None, "impact": None, "verify": None, "skipped": True}

        # 影响范围对账配置（「写入安全边界」维度）：执行前后各取一次快照总数
        audit = None
        for _src in (inp if isinstance(inp, dict) else {},
                     expected if isinstance(expected, dict) else {}):
            _a = _src.get("影响范围对账")
            if isinstance(_a, dict) and _a.get("快照工具"):
                audit = _a
                break
        # 兜底：从能力目录取默认工具
        if not tool_name:
            tool_name = self.sys.cap_tool.get(capability, "")

        mcp_input = json.dumps({"toolParams": tool_params}, ensure_ascii=False)
        before = after = exists_before = exists_after = None
        try:
            stack, session = await self._session_context()
            async with stack:
                before = await self._snapshot_count(session, audit)
                exists_before = await self._presence(session, audit)
                is_error, text, latency = await self._call_tool(session, tool_name, tool_params)
                after = await self._snapshot_count(session, audit)
                exists_after = await self._presence(session, audit)
        except Exception as e:
            steps.append({
                "name": f"MCP-{tool_name}", "type": "SPAN", "input": mcp_input,
                "output": f"EXCEPTION: {e}",
                "metadata": {"mcp": self.sys.system, "tool": tool_name, "latency_ms": 0},
            })
            # 区分两类调用异常：
            #   - 「工具/方法不存在」＝被测系统拒绝虚构工具/能力 → WARNING（block 用例算正确拦截）
            #   - 服务端/网络/未知异常 → ERROR（真异常，不算拦截成功）
            # TaskGroup 会把真实异常包在 ExceptionGroup 内，需沿异常树提取叶子消息判断。
            _full = self._err_tree_text(e).lower()
            _is_missing = any(k in _full for k in
                              ("not found", "unknown tool", "no such tool",
                               "not a valid tool", "unknown method",
                               "method not found", "no tool"))
            _server_err = any(k in _full for k in
                              ("internal server", "unexpected error", "exception",
                               "timed out", "timeout", "connection refused",
                               "connection reset", "bad gateway", "service unavailable"))
            if _is_missing and not _server_err:
                _msg = f"tool not found: {tool_name}"
                steps[-1]["output"] = f"REJECT: {_msg}"
                return {"tool": tool_name, "output": _msg, "tool_correct": True,
                        "level": "WARNING", "block": True, "biz_error": _msg,
                        "steps": steps, "verify": None}
            return {"error": f"MCP 调用失败: {e}", "level": "ERROR", "block": True,
                    "steps": steps, "tool": tool_name}

        # 工具调用失败（MCP result.is_error=True）分类：
        #   - 服务端/传输真异常（internal server / timeout / connection 等）→ ERROR
        #   - 其余（invoking error / 参数校验拒绝 / 越权提示等 = 被测系统拒绝非法输入）→ WARNING，
        #     视为「系统已明确拒绝请求」，供评分器按 block/契约拒绝正确计分。
        # 此前把 is_error 一律当 ERROR → status=error → 期望拦截的用例被误计入失败。
        level = ""
        if is_error:
            _low = (text or "").lower()
            if any(k in _low for k in ("internal server", "unexpected error",
                                       "exception", "timed out", "timeout",
                                       "connection refused", "connection reset",
                                       "bad gateway", "service unavailable")):
                level = "ERROR"
            else:
                level = "WARNING"

        # 业务层检测（对齐 GenericMcpExecutor 的业务码识别）：MCP 返回 isError=False
        # 但 JSON 包壳 code!=0 / success=false（如空参 {code:40000,msg:"店铺 ID 不能为空"}）
        # 属业务拒绝/异常 → 标 WARNING + biz_error，供评分器识别"系统已明确拒绝请求"。
        # 否则此类响应会被当成成功，block 用例误判成"未按预期拦截"。
        biz_error = None
        if not is_error:
            try:
                _parsed = json.loads(text)
                if isinstance(_parsed, dict):
                    _code = _parsed.get("code")
                    _succ = _parsed.get("success")
                    _bad = (
                        (isinstance(_code, (int, float)) and _code not in (0, 200))
                        or (isinstance(_succ, bool) and not _succ)
                    )
                    if _bad:
                        biz_error = str(_parsed.get("msg") or _parsed.get("message") or _code)
                        level = "WARNING"
            except Exception:
                biz_error = None
        elif level == "WARNING":
            # MCP 层拒绝（is_error 但非服务端异常）= 系统明确拒绝非法输入
            biz_error = (text or "tool invocation rejected by system")[:200]

        steps.append({
            "name": f"MCP-{tool_name}", "type": "SPAN", "input": mcp_input,
            "output": text, "metadata": {"mcp": self.sys.system, "tool": tool_name,
                                         "latency_ms": latency, "level": level},
        })
        # 影响范围对账（写入安全边界）：① 存在性对账（真实目标实体操作后是否仍在）
        # ② 快照总数对账（|Δ| ≤ 传入目标数）。两项证据都写入 impact，交评分器判定。
        impact = None
        if audit:
            delta = (abs(after - before)
                     if (before is not None and after is not None) else None)
            impact = {"快照工具": audit.get("快照工具"), "before": before, "after": after,
                      "delta": delta,
                      "最大影响条数": audit.get("最大影响条数", 0),
                      "存在性目标ID": (audit.get("存在性检查") or {}).get("目标ID"),
                      "exists_before": exists_before, "exists_after": exists_after}
        return {
            "steps": steps,
            "tool": tool_name,
            "output": text,
            "tool_correct": True,   # 直连模式无 LLM 选择，视为工具匹配
            "level": level,
            "block": True if level == "WARNING" else None,
            "biz_error": biz_error,
            "impact": impact,
            "verify": None,
        }

    @staticmethod
    def _count_items(text):
        """统计 MCP 返回中的条目总数（所有 list 长度之和），用于影响范围对账。"""
        try:
            data = json.loads(text)
        except Exception:
            return None

        def walk(o):
            if isinstance(o, list):
                return len(o) + sum(walk(x) for x in o if isinstance(x, (list, dict)))
            if isinstance(o, dict):
                return sum(walk(v) for v in o.values())
            return 0
        return walk(data)

    async def _snapshot_count(self, session, audit):
        """调只读快照工具取当前条目总数；无对账配置或失败返回 None。"""
        if not audit:
            return None
        try:
            _, text, _ = await self._call_tool(
                session, audit.get("快照工具"), audit.get("快照参数") or {})
        except Exception:
            return None
        return self._count_items(text)

    async def _presence(self, session, audit):
        """存在性探针：查「本次操作的真实目标实体」当前是否仍在。

        返回 True（仍在）/ False（已不存在，即被越范围改删）/ None（无配置或探针
        自身失败，不作为证据）。探针报业务错误码时返回 None —— 否则会把
        「探针查不到」误读成「数据被删」，制造假失败。
        """
        if not audit:
            return None
        cfg = audit.get("存在性检查") or {}
        tool, params = cfg.get("工具"), cfg.get("参数") or {}
        target = str(cfg.get("目标ID") or "")
        if not tool or not target:
            return None
        try:
            _, text, _ = await self._call_tool(session, tool, params)
        except Exception:
            return None
        if not text:
            return None
        try:
            data = json.loads(text)
        except Exception:
            data = None
        if isinstance(data, dict):
            _code = data.get("code")
            _succ = data.get("success")
            if (isinstance(_code, (int, float)) and _code not in (0, 200)) \
                    or (isinstance(_succ, bool) and not _succ):
                return None
        return target in str(text)

    @staticmethod
    def _err_tree_text(exc):
        """沿 ExceptionGroup / __cause__ / __context__ 链收集所有叶子异常文本。"""
        parts, seen = [], set()

        def _walk(ex):
            if ex is None or id(ex) in seen:
                return
            seen.add(id(ex))
            if isinstance(ex, BaseExceptionGroup):
                for sub in ex.exceptions:
                    _walk(sub)
            else:
                parts.append(str(ex))
            if ex.__cause__ is not None:
                _walk(ex.__cause__)
            if ex.__context__ is not None:
                _walk(ex.__context__)

        _walk(exc)
        return "\n".join(parts)
