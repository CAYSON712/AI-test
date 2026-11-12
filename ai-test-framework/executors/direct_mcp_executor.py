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


def _allow_reconcile():
    """是否允许执行「写入对账」用例。由环境变量 ALLOW_RECONCILE=1 显式开启。

    为什么单独设开关（而不是复用 ALLOW_DESTRUCTIVE）：
      对账用例本身是真实写操作 + 需要额外调读接口，一旦被测系统状态异常
      （如某个 set_* 不生效、读接口返回脏数据），可能连环改坏配置且难还原。
      默认跳过，只有在确认「数据生命周期」覆盖完整后才显式开启。
    """
    return str(os.environ.get("ALLOW_RECONCILE", "")).strip().lower() in ("1", "true", "yes")


def _dryrun_mode():
    """是否开启「干跑」：把所有写操作的确认位强制为 false（服务端只预览不执行）。

    由环境变量 CONFIRM_DRYRUN=1 显式开启。适用于被测 MCP 自带 confirm 二段式
    （如 PocketStore）：false=仅预览、true=真正执行。开启后执行器会把参数里的
    确认位统一覆写为 false，从而零风险地验证「参数/协议/权限」层。
    """
    return str(os.environ.get("CONFIRM_DRYRUN", "")).strip().lower() in ("1", "true", "yes")


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

    def _has_recovery_rule(self, tool_name):
        """该工具是否配了「能把数据收回」的保护规则。

        三种保护都算数（任一即可）：
          可还原    —— 改前快照，跑完按原值写回
          新建后清理 —— 建完登记，跑完调用对应 delete 删掉
          自建自删  —— 建临时实体替身来删，不碰真实数据
        """
        lc = self.get_lifecycle()
        if lc is None or not tool_name:
            return False
        if (lc.restorable or {}).get(tool_name):
            return True
        if (lc.creatable or {}).get(tool_name):
            return True
        try:
            if lc.self_delete_rule(tool_name):
                return True
        except Exception:  # noqa: BLE001
            pass
        return False

    # ---- 主流程：直连工具调用 ----
    def handle(self, capability, user_input, inp, expected, case=None):
        return asyncio.run(self._call_direct(capability, inp, expected, case=case))

    async def _call_direct(self, capability, inp, expected, case=None):
        """按用例指定的工具+参数直连 MCP，返回结构化结果。"""
        steps = []
        # 取工具名 + 参数（A 类用例输入为工具调用格式）
        if isinstance(inp, dict):
            tool_name = inp.get("tool_name") or inp.get("tool") or ""
            tool_params = inp.get("tool_params") or inp.get("params") or {}
        else:
            tool_name, tool_params = "", {}
        # 干跑：CONFIRM_DRYRUN=1 时把写操作的确认位强制为 false（服务端只预览不执行）。
        # 确认位参数名由 configs 的「干跑开关参数名」声明（默认 confirm）。
        if _dryrun_mode() and isinstance(tool_params, dict):
            _ck = getattr(self.sys, "dryrun_param", "confirm")
            if _ck in tool_params:
                tool_params = dict(tool_params)
                tool_params[_ck] = False
        # 高危用例保护：「有数据保护兜底」才允许执行，否则跳过。
        #
        # 演进（2026-11-01）：原先只看 ALLOW_DESTRUCTIVE 一个开关，导致
        # PocketStore「写入安全边界」15 条**全部被跳过**（skipped=15, n=0），
        # 报告只剩一个假的 n=0。实际这 15 条都配了「新建后清理 / 可还原 /
        # 自建自删」，数据能完整回收，根本不需要开高危开关。
        #
        # 现规则（更精确）：
        #   ① 显式 ALLOW_DESTRUCTIVE=1          → 直接放行（隔离环境人工确认）
        #   ② 该工具配了任一种可回收保护        → 放行（跑完自动还原/清理）
        #   ③ 两种都没有                        → 跳过（真会留下无法回滚的数据）
        # 背景不变：某 MCP 传不存在的 ID 也会真实删掉全量数据，无保护时绝不执行。
        if isinstance(expected, dict) and expected.get("高危") \
                and not _allow_destructive():
            tool_guess = (inp or {}).get("tool_name") if isinstance(inp, dict) else ""
            if not self._has_recovery_rule(tool_guess):
                return {
                    "steps": [], "tool": tool_guess,
                    "output": f"高危用例已跳过（{tool_guess} 未配置任何可回收保护，"
                              "需设置 ALLOW_DESTRUCTIVE=1 且确认隔离环境后才会执行）",
                    "tool_correct": None, "level": "INFO", "block": None,
                    "biz_error": None, "impact": None, "verify": None,
                    "skipped": True, "skip_reason": "no_recovery_rule"}

        # 「写入对账」用例保护：默认不执行（见 _allow_reconcile 注释）。
        # 必须在真正调用工具之前拦截，否则跳过了还是要改数据。
        _rc_meta = (case or {}).get("对账") or (case or {}).get("reconcile")
        if isinstance(_rc_meta, dict) and _rc_meta.get("read_tool") \
                and not _allow_reconcile() and not _allow_destructive():
            return {
                "steps": [], "tool": tool_name,
                "output": ("写入对账用例已跳过（默认不执行写后回读，"
                           "需设置 ALLOW_RECONCILE=1 在隔离环境开启）"),
                "tool_correct": None, "level": "INFO", "block": None,
                "biz_error": None, "impact": None, "reconcile": None,
                "verify": None,
                "skipped": True, "skip_reason": "reconcile_disabled",
            }

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

        mcp_input = json.dumps({self.sys.param_wrapper: tool_params}, ensure_ascii=False)
        before = after = exists_before = exists_after = None
        # 对账结果：异常分支可能没赋值，先置 None 保证返回值结构稳定
        reconcile = None
        # 用例级运行时上下文（按实体覆盖 CompanyId 等请求头）：
        # 先看用例显式声明，再退回「按本次参数里的店铺 ID 查实体清单」。
        # 两者都没有时 → 全局默认头（既有行为不变）。
        _ctx = self._case_context(case, inp) \
            or self.sys.resolve_entity_context(tool_params)
        try:
            stack, session = await self._session_context(_ctx)
            async with stack:
                before = await self._snapshot_count(session, audit)
                exists_before = await self._presence(session, audit)
                # 写入对账的 before 快照必须在写操作【之前】取（2026-11-10 踩坑修正）。
                # 曾经把整个 _reconcile 放在写操作之后调用，它内部的 before 快照
                # 拿到的是「写后」状态 → before == after → 真生效也被判成假成功。
                # 正确顺序：取 before 快照 → 写 → 取 after 快照 → 比对。
                rc_before = await self._reconcile_snapshot(session, case, tool_params)
                is_error, text, latency = await self._call_tool(session, tool_name, tool_params)
                reconcile = await self._reconcile(
                    session, case, tool_params, text, rc_before
                )
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
                        _msg = str(_parsed.get("msg") or _parsed.get("message")
                                   or _code or "")
                        biz_error = _msg
                        # 区分「业务拒绝」与「系统故障」——两者都不算成功，但不能都当拦截：
                        #   业务拒绝（参数非法/越权/不支持）= 系统正确拒绝请求
                        #     → level=WARNING + block=True，供 block 类用例计分
                        #   系统故障（聚合窗口未校验/内部错误/依赖不可用）= 系统没能完成查询
                        #     → level=ERROR + block=None，不能算「成功拦截」
                        #
                        # 动机（2026-11-06，A-L2-009 DST 切换日）：
                        #   query_datax_total_reports 返回
                        #   {code:40000, success:false, msg:"DataX aggregate window
                        #    2026-10-31 is not verified across DST; historical repair
                        #    is required."}
                        #   原逻辑一律当 WARNING+block=True，评分器按「已拦截且符合预期」
                        #   给 5 分 —— 一条真实的系统故障被记成了通过。
                        #   该用例期望本就是「应正常返回」，故障必须暴露而非被当拦截。
                        _sys_fail_kw = (
                            "not verified across dst", "historical repair",
                            "字段缺失", "字段格式", "内部错误", "internally",
                            "unavailable", "not available", "服务不可用",
                            "依赖", "dependency", "aggregate window",
                        )
                        if any(k in _msg.lower() for k in _sys_fail_kw):
                            level = "ERROR"
                        else:
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
            # 服务端是否明确拒绝本次调用：基线为 0 时，这是「作用范围必然为 0」的
            # 唯一有效证据（Δ 恒为 0 本身证明不了任何事）。
            # 判定口径与执行器一致：biz_error 存在，或返回体 code != 0 / success=false。
            _rejected = bool(biz_error)
            if not _rejected:
                try:
                    _d = json.loads(text)
                    if isinstance(_d, dict):
                        _c = _d.get("code")
                        if _c not in (None, 0, 200) or _d.get("success") is False:
                            _rejected = True
                except Exception:
                    pass
            impact = {"快照工具": audit.get("快照工具"), "before": before, "after": after,
                      "delta": delta,
                      "最大影响条数": audit.get("最大影响条数", 0),
                      "存在性目标ID": (audit.get("存在性检查") or {}).get("目标ID"),
                      "exists_before": exists_before, "exists_after": exists_after,
                      "服务端拒绝": _rejected}
        return {
            "steps": steps,
            "tool": tool_name,
            "output": text,
            # 回传实际入参：评分器需据此判断「期望拦截是否成立」
            # （如"仅多了 schema 未声明的字段"→ 期望立错，属数据集问题，
            #  而非安全缺口）。此前不回传 params，评分器只能一律判 AI 系统问题。
            "params": tool_params,
            "tool_correct": True,   # 直连模式无 LLM 选择，视为工具匹配
            "level": level,
            # 只有 WARNING（系统明确拒绝请求）才算「已拦截」。
            # ERROR（系统故障：DST 窗口未校验、内部错误、依赖不可用）虽然也是
            # 非成功响应，但不能记作「按预期拦截」——它代表系统没完成这次查询，
            # 标成 block 会被评分器当成成功（2026-11-06 A-L2-009 踩到）。
            "block": True if level == "WARNING" else None,
            "biz_error": biz_error,
            "impact": impact,
            "reconcile": reconcile,
            "verify": None,
        }

    # ------------------------------------------------------------------
    # 写入对账
    # ------------------------------------------------------------------
    @staticmethod
    def _rc_meta_of(case):
        """取出用例的「对账」段（兼容中英文键）。"""
        if not isinstance(case, dict):
            return None
        meta = case.get("对账") or case.get("reconcile")
        if isinstance(meta, dict) and meta.get("read_tool"):
            return meta
        return None

    @staticmethod
    def _rc_read_params(meta, tool_params):
        """构造读工具入参：沿用写工具入参里的店铺上下文，避免跨店铺读。"""
        params = {}
        if isinstance(tool_params, dict):
            for k in ("merchId", "companyId"):
                if k in tool_params:
                    params[k] = tool_params[k]
        params.setdefault("confirm", True)
        return params

    async def _reconcile_snapshot(self, session, case, tool_params):
        """在写操作【之前】取对账快照。无需对账或未开启时返回 None。"""
        meta = self._rc_meta_of(case)
        if not meta or not _allow_reconcile():
            return None
        try:
            read_tool = meta["read_tool"]
            params = self._rc_read_params(meta, tool_params)
            _, text, _ = await self._call_tool(session, read_tool, params)
            return self._safe_json(text)
        except Exception:
            # before 取不到不阻断主流程：后续 judge 会因为没有 before 而判「证据不足」
            return None

    async def _reconcile(self, session, case, tool_params, write_text, before_snapshot=None):
        """写后回读对账（通用实现，零业务硬编码）。

        原理：用例的「对账」段（由 configs/<系统>.yaml 的「写入对账」生成）
        声明了 read_tool / compare / id_field 等，这里照它执行：
          ① before 快照（若 audit 已取到则复用）
          ② 写操作（已在调用方完成，结果在 write_text）
          ③ after 快照
          ④ 交给 reconcile.judge_reconcile 打分

        返回 None 表示本用例不需要对账；返回 dict 供评分器消费。
        任何异常都降级为 skipped，绝不让对账把主流程带崩。
        """
        try:
            meta = self._rc_meta_of(case)
            if not meta:
                return None

            # 默认跳过对账：需 ALLOW_RECONCILE=1 显式开启（见 _allow_reconcile 注释）
            if not _allow_reconcile():
                return {"skipped": True,
                        "reason": "对账用例默认跳过；设 ALLOW_RECONCILE=1 后执行"}

            from reconcile import judge_reconcile

            read_tool = meta["read_tool"]
            read_params = self._rc_read_params(meta, tool_params)

            # ① before 快照：优先用调用方在「写操作之前」取好的
            before_payload = before_snapshot
            if before_payload is None:
                # 兜底：此刻写操作已完成，这里取到的其实是「写后」状态。
                # 若走到这个分支，比对结果不可信 —— 明确标记出来，不静默产生假阳性。
                _, b_text, _ = await self._call_tool(session, read_tool, read_params)
                before_payload = self._safe_json(b_text)
                return {
                    "skipped": True,
                    "reason": ("before 快照缺失（应在写操作之前取），"
                               "当前对账结论不可信，已跳过"),
                }

            # ③ after 快照
            _, a_text, _ = await self._call_tool(session, read_tool, read_params)
            after_payload = self._safe_json(a_text)

            write_result = self._safe_json(write_text) or {}

            score, reason, detail = judge_reconcile(
                write_result, meta, before_payload, after_payload
            )
            diffs = detail.get("field_diffs") or []
            changed = bool(diffs) or (
                detail.get("exists_before") is False
                and detail.get("exists_after") is True
            )
            return {
                "score": score,
                "reason": reason,
                "changed": changed,
                "read_tool": read_tool,
                "detail": detail,
                "before": before_payload,
                "after": after_payload,
            }
        except Exception as e:
            return {"skipped": True, "reason": f"对账执行异常：{e}"}

    @staticmethod
    def _safe_json(text):
        """把返回文本解析成 dict/list；失败返回 None。"""
        try:
            return json.loads(text)
        except Exception:
            return None

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
