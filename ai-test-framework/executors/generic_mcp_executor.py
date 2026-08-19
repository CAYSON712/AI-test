# -*- coding: utf-8 -*-
"""
通用真实执行器（配置驱动）
==========================
由「系统配置 + 能力目录」驱动，一套执行器可测任意系统的
Agent+MCP（C 类）、Agent（B 类）、工具（A/D 类）。

设计：
  - 连接：从 configs/<系统>.yaml 读取（base_url/token 等 .env 变量名）
  - 工具 schema：从 configs/<系统>.yaml 的 mcp_tools 读取（给 LLM 选工具/抽参数）
  - 能力→工具映射：从 ability/能力目录_<系统>.yaml 读取（每能力有「工具」字段）
  - verify 配置：从能力目录读取（verify_tool / verify_field / verify_expect）
  - 校验查询工具、需默认上下文参数的工具、需校验的工具：从 configs 读取

接入新系统（C 类）只需：
  1. 新建 configs/<系统名>.yaml，填连接 + mcp_tools（可参考已有系统配置）
  2. 准备 ability/能力目录_<系统名>.yaml（含 能力/工具/verify_*）
  3. 在 registry 里按系统名实例化即可，不写任何 Python 逻辑

依赖：
  - llm_client（解析自然语言 → 工具调用）
  - ability/ 能力目录 + configs/ 系统配置
"""
import asyncio
import json
import os
import re
import sys
import time
from contextlib import AsyncExitStack

import httpx
import yaml
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from base import BaseExecutor

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ABILITY_DIR = os.path.join(_ROOT, "ability")
CONFIG_DIR = os.path.join(_ROOT, "configs")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if os.path.join(_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "scripts"))

# 加载 ai-test-framework/.env（敏感配置统一存这里，勿硬编码提交 git）
load_dotenv(os.path.join(_ROOT, ".env"))


# =====================================================================
# 配置 / 能力目录加载
# =====================================================================
def _load_yaml(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _find_config_file(system):
    """按系统名匹配 configs/ 下配置文件，返回完整路径；无精确匹配返回空串"""
    if not system or not os.path.isdir(CONFIG_DIR):
        return ""
    for f in os.listdir(CONFIG_DIR):
        if not f.endswith(".yaml"):
            continue
        base = os.path.splitext(f)[0]
        if _name_match(base, system):
            return os.path.join(CONFIG_DIR, f)
    return ""


def _find_ability_file(system):
    """按系统名匹配 ability/ 下能力目录，返回完整路径；无精确匹配返回空串。
    文件名格式「能力目录_<系统名>.yaml」，系统名可能带/不带分隔符，
    用完整 base 名与系统名双向包含匹配（忽略下划线/空格等分隔符差异）。
    """
    if not system or not os.path.isdir(ABILITY_DIR):
        return ""
    for f in os.listdir(ABILITY_DIR):
        if not (f.startswith("能力目录_") and f.endswith(".yaml")):
            continue
        base = os.path.splitext(f)[0]          # 完整 base：能力目录_<系统名>
        stripped = base.replace("能力目录_", "")  # <系统名>
        if _name_match(stripped, system) or _name_match(base, system):
            return os.path.join(ABILITY_DIR, f)
    return ""


def _name_match(stripped, system):
    """容错匹配：忽略下划线/空格分隔符差异，任一方向包含即可。
    兼容带/不带分隔符的系统名（能力目录与系统名分隔符可能不一致）。
    """
    if not stripped or not system:
        return False
    norm = lambda s: s.replace("_", "").replace(" ", "").replace("-", "")
    a, b = norm(stripped), norm(system)
    return b in a or a in b


class _SysConfig:
    """封装一份系统的 连接 + 工具 schema + verify 配置（配置驱动）"""

    def __init__(self, system):
        self.system = system
        cfg_path = _find_config_file(system)
        abi_path = _find_ability_file(system)
        cfg = _load_yaml(cfg_path) if cfg_path else {}
        abi = _load_yaml(abi_path) if abi_path else {}

        # ---- 连接 ----
        conn = cfg.get("连接", {})
        self.req_type = conn.get("需求类型", "C")
        self.base_url = os.getenv(conn.get("base_url_env", ""), "")
        self.token = os.getenv(conn.get("token_env", ""), "")
        self.company_id = os.getenv(conn.get("company_id_env", ""), "")
        self.merchant_id = os.getenv(conn.get("merchant_id_env", ""), "")

        # ---- 通用命名（配置驱动，默认值通用，新系统可在 configs/<系统>.yaml 覆盖）----
        # LLM 决策层环境变量名（优先配置字段，回退通用 LLM_* 前缀）
        self.llm_base_url_env = conn.get("llm_base_url_env", "LLM_API_BASE")
        self.llm_api_key_env = conn.get("llm_api_key_env", "LLM_API_KEY")
        self.llm_model_env = conn.get("llm_model_env", "LLM_MODEL")
        self.system_prompt_env = conn.get("system_prompt_env", "SYSTEM_PROMPT")
        # 业务字段名 / 工具名（不绑定任何系统命名，全部可由配置覆盖）
        self.company_header = cfg.get("公司ID请求头", "CompanyId")
        self.merchant_param = cfg.get("默认上下文参数名", "merchantIds")   # 复数（列表）
        self.entity_id_param = cfg.get("实体ID参数名", "entityIds")  # 复数（列表）
        self.entity_name_param = cfg.get("实体名参数名", "entityName")
        self.lookup_tools = list(cfg.get("按名搜索工具", [
            "search_by_name", "search", "lookup"]))
        self.id_resolve_tools = list(cfg.get("需ID解析的工具", [
            "update_by_ids", "delete_by_ids",
            "query_by_ids", "query_detail_by_id"]))
        self.demo_connect_tool = cfg.get("连接演示工具", "query_merchants")

        # ---- 工具 schema ----
        self.tools = cfg.get("mcp_tools", [])

        # ---- 需默认上下文参数 / 需校验的工具 ----
        self.merchant_needed_tools = set(cfg.get("需要默认上下文参数的工具", []))
        self.verify_tools = set(cfg.get("需要校验的工具", []))

        # ---- 能力→工具 / verify / 操作类型 映射（来自能力目录）----
        self.cap_tool = {}
        self.verify_map = {}
        self.cap_op = {}   # 操作类型（查询/创建/变更数值/变更状态/删除/组合/人工）
        for group in abi.get("能力分组", []):
            for c in group.get("能力列表", []):
                cap = c.get("能力")
                if not cap:
                    continue
                if c.get("工具"):
                    self.cap_tool[cap] = c["工具"]
                if c.get("操作类型"):
                    self.cap_op[cap] = c["操作类型"]
                # verify：优先读新版「成功标准」的 db 模式，回退旧版 verify_tool 字段
                vcfg = self._extract_db_verify(c)
                if vcfg:
                    self.verify_map[cap] = vcfg
                elif c.get("verify_tool"):
                    self.verify_map[cap] = {
                        "verify_tool": c["verify_tool"],
                        "verify_field": c.get("verify_field", "exists"),
                        "verify_expect": c.get("verify_expect", True),
                    }
        self.capabilities = list(self.cap_tool.keys())

    @staticmethod
    def _extract_db_verify(cap):
        """从新版能力目录「成功标准」里提取 db 模式校验配置。
        返回 {"verify_tool", "verify_field", "verify_expect"} 或 None。
        """
        sc = cap.get("成功标准") or []
        if isinstance(sc, dict):
            sc = [sc]
        for item in sc:
            if isinstance(item, dict) and item.get("模式") == "db":
                return {
                    "verify_tool": item.get("校验工具", ""),
                    "verify_field": item.get("字段", "exists"),
                    "verify_expect": item.get("期望", True),
                }
        return None


def _parse_llm_call(text):
    """解析 LLM 输出的工具调用 JSON，容错处理"""
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        return json.loads(text[start:end])
    except Exception:
        tool = re.search(r'"tool"\s*:\s*"([^"]+)"', text)
        params = re.search(r'"toolParams"\s*:\s*(\{.*?\})', text, re.DOTALL)
        intent = re.search(r'"intent"\s*:\s*"([^"]+)"', text)
        if tool:
            params_obj = {}
            if params:
                try:
                    params_obj = json.loads(params.group(1))
                except Exception:
                    params_obj = {}
            return {"tool": tool.group(1), "toolParams": params_obj,
                    "intent": intent.group(1) if intent else ""}
        raise ValueError(f"无法解析 LLM 工具调用: {text[:300]}")


def _extract_entity(user_input, capability):
    """从用户自然语言里粗提取实体名（去掉常见动词/数量词/收尾干扰词）。

    用于操作类工具缺 ID 时按名查 ID。通用实现：不同系统可覆盖提取逻辑。

    修复：此前取"最后一个片段"，但实体名常是中间词，收尾往往是
     "信息/详情/价格/多少钱"等干扰词，导致提取到干扰词而非实体名
     （如"查 XX 的信息"→ 取到"信息"）。改为识别并去掉
     收尾干扰词，取最可能是实体名的片段。
    """
    if not user_input:
        return ""
    quoted = re.findall(r"['\"「『]([^'\"」』]+)['\"」』]", user_input)
    if quoted:
        return quoted[0].strip()

    # 通用停止词（操作类动词 + 数量词），按长度降序替换，避免短词破坏长词/实体名
    stop_words = [
        # 多字词（先替换）
        "把那个", "调整到", "批量删除", "批量修改",
        "批量新增", "删除所有", "查询一下", "查一下",
        "请问一下", "告诉我", "查看一下", "改成", "改为", "变成",
        "删除", "删掉", "移除", "修改", "改名", "新增", "添加", "查询", "查看",
        "多少", "所有", "全部", "重新", "麻烦", "请问",
        "一下", "名称",
        # 单字词（最后替换）
        "把", "将", "给", "对", "帮", "我", "的", "里", "请", "查",
        "了", "啊", "呢", "下", "上", "为", "以",
    ]
    stop_words.sort(key=len, reverse=True)
    # 收尾干扰词：出现在实体名之后、不应作为实体的一部分（若实体是末尾则剔除）
    tail_words = [
        "信息", "详情", "资料", "价格", "多少钱", "售价", "是什么", "怎么样",
        "状态", "信息吗", "一下", "呢", "？", "?",
    ]
    for token in stop_words:
        user_input = user_input.replace(token, " ")
    # 先剔除收尾干扰词（只剔末尾出现的）
    lowered = user_input.lower()
    for tw in tail_words:
        if lowered.rstrip().endswith(tw):
            user_input = user_input[: -len(tw)]
    # 去停止词后剩余内容整体作为实体名（保留多词实体名）
    # 注意：实体名与中文操作词混排，去掉停止词后剩下的整段即实体名。
    remaining = re.sub(r"[\s,，。]+", " ", user_input).strip()
    if not remaining:
        return ""
    # 去掉孤立标点/纯数字残留
    remaining = re.sub(r"^[\s&]+|[\s&]+$", "", remaining)
    # 若是"批量删除 A、B、C"这类多实体，取第一段（框架按名反查仅支持单实体）
    if "、" in remaining or "," in remaining or "，" in remaining:
        remaining = re.split(r"[、,，]", remaining)[0].strip()
    return remaining


class GenericMcpExecutor(BaseExecutor):
    """通用真实执行器：LLM 解析自然语言 → 调真实 MCP → 操作后实时校验。

    通过 _SysConfig 加载系统配置，不绑定任何具体系统。
    """

    def __init__(self, system):
        self.sys = _SysConfig(system)
        self.capabilities = self.sys.capabilities
        import os as _os
        from llm_client import LLMClient
        # 支持用被测系统的 LLM 做决策：
        #   优先读 configs/<系统>.yaml「连接」声明的 llm_*_env 环境变量，
        #   回退通用 LLM_* 环境变量，再回退 LLMClient 内置默认。
        self.llm = LLMClient(
            api_base=_os.getenv(self.sys.llm_base_url_env) or None,
            api_key=_os.getenv(self.sys.llm_api_key_env) or None,
            model=_os.getenv(self.sys.llm_model_env) or None,
        )
        # 可配置系统提示词：环境变量指向 prompt 文件路径时用它（被测系统的真实 prompt），
        # 否则用框架默认。prompt 文件第一行可作为占位；支持 {tools}/{capability} 模板。
        self._system_prompt = ""
        sp_prompt_path = _os.getenv(self.sys.system_prompt_env, "")
        if sp_prompt_path and _os.path.exists(sp_prompt_path):
            with open(sp_prompt_path, encoding="utf-8") as _f:
                self._system_prompt = _f.read().strip()

    # ---- 连接上下文 ----
    def _client_headers(self):
        headers = {}
        if self.sys.token:
            headers["Authorization"] = f"Bearer {self.sys.token}"
        if self.sys.company_id:
            headers[self.sys.company_header] = self.sys.company_id
        return headers

    async def _session_context(self):
        stack = AsyncExitStack()
        http = await stack.enter_async_context(
            httpx.AsyncClient(headers=self._client_headers())
        )
        read, write = await stack.enter_async_context(
            streamable_http_client(self.sys.base_url, http_client=http)
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return stack, session

    async def _call_tool(self, session, name, tool_params):
        """调用 MCP 工具，返回 (is_error, 文本, latency_ms)"""
        t0 = time.monotonic()
        result = await session.call_tool(name, {"toolParams": tool_params})
        latency_ms = (time.monotonic() - t0) * 1000
        texts = [c.text for c in result.content if c.type == "text"]
        return result.is_error, "\n".join(texts), round(latency_ms, 1)

    # ---- 主流程 ----
    def handle(self, capability, user_input, inp, expected):
        return asyncio.run(self._call_capability(capability, user_input, expected))

    async def _call_capability(self, capability, user_input, expected):
        steps = []
        cap_tool = self.sys.cap_tool.get(capability, "")

        # 1. LLM 意图理解：记录 LLM 原始选择的工具 + 解析的意图（供评分区分维度）
        llm_tool = ""
        llm_intent = ""
        llm_intent_latency = 0.0
        try:
            intent_raw, llm_intent_latency = self._llm_parse(user_input, capability)
            llm_tool = intent_raw.get("tool") or cap_tool
            llm_intent = intent_raw.get("intent") or ""
            tool_params = intent_raw.get("toolParams") or {}
        except Exception:
            llm_tool = cap_tool
            tool_params = {}
        # 工具选择正确率：LLM 原始选择的工具是否与能力目录期望工具一致
        tool_correct = bool(cap_tool) and llm_tool == cap_tool
        # 意图识别正确率：LLM 解析出的意图 vs 用例期望意图（能力名/期望intent）
        exp = expected if isinstance(expected, dict) else {}
        exp_intent = exp.get("intent") or capability or ""
        intent_correct = None  # None=无法判定（LLM 未输出意图）
        if llm_intent:
            # 期望意图(能力名/中文)与 LLM 意图做宽松匹配：任一关键片段命中即算识别对
            _ei = str(exp_intent).lower().replace(" ", "")
            _li = llm_intent.lower().replace(" ", "")
            intent_correct = (_ei in _li) or (_li in _ei) or (_ei == _li)
        # 参数生成/端到端正确率：实际 tool_params vs 用例期望 params（含值/字段）
        param_correct = None
        exp_params = exp.get("params") if isinstance(exp, dict) else None
        if isinstance(exp_params, dict) and exp_params:
            _ok = True
            for k, v in exp_params.items():
                if k in tool_params:
                    # 值匹配：期望值可能因 ID 解析而变，数值宽松、字符串精确
                    if isinstance(v, list):
                        if not isinstance(tool_params[k], list) or not any(
                            str(x) in [str(y) for y in tool_params[k]] for x in v):
                            _ok = False
                    elif str(v) not in str(tool_params[k]):
                        _ok = False
                else:
                    _ok = False  # 期望参数字段缺失
            param_correct = _ok
        # 工具纠正：能力目录有明确工具时，强制用它（避免 LLM 选成查询类）
        # 注意：纠正只影响实际调用，原始选择 llm_tool 保留给评分
        tool_name = cap_tool if cap_tool else llm_tool
        steps.append({
            "name": "LLM-意图理解", "type": "GENERATION",
            "input": user_input,
            "output": json.dumps({"intent": llm_intent, "tool": llm_tool,
                                  "toolParams": tool_params}, ensure_ascii=False),
            "metadata": {"capability": capability, "system": self.sys.system,
                         "expected_tool": cap_tool, "tool_correct": tool_correct,
                         "intent_correct": intent_correct, "param_correct": param_correct,
                         "expected_intent": exp_intent, "llm_intent": llm_intent,
                         "latency_ms": llm_intent_latency},
        })
        if not tool_name:
            return {"error": f"LLM 未能解析出工具，能力: {capability}",
                    "block": True, "steps": steps}

        # 2. 补默认上下文参数 + ID 解析
        tool_params = self._ensure_merchant(tool_name, tool_params)
        await self._resolve_entity_id(tool_name, tool_params, user_input, steps, capability)

        # 3. 调用真实 MCP
        mcp_input = json.dumps({"toolParams": tool_params}, ensure_ascii=False)
        try:
            stack, session = await self._session_context()
            async with stack:
                is_error, text, mcp_latency = await self._call_tool(session, tool_name, tool_params)
        except Exception as e:
            steps.append({
                "name": f"MCP-{tool_name}", "type": "SPAN", "input": mcp_input,
                "output": f"EXCEPTION: {e}",
                "metadata": {"mcp": self.sys.system, "tool": tool_name},
            })
            return {"error": f"MCP 调用失败: {e}", "block": True, "steps": steps}

        steps.append({
            "name": f"MCP-{tool_name}", "type": "SPAN", "input": mcp_input,
            "output": text, "metadata": {"mcp": self.sys.system, "tool": tool_name,
                                         "latency_ms": mcp_latency},
        })
        if is_error:
            # 传输层/协议异常（网络、MCP 调用失败）→ 真异常 ERROR
            return {"error": text, "block": True, "level": "ERROR", "steps": steps}

        # 3.5 业务层结果检测：code != 0 或 success=false 视为业务拒绝/异常
        # 对齐 Langfuse：流程正常但业务拒绝 → WARNING；真异常（unexpected error）→ ERROR
        biz_error = None
        biz_level = None
        try:
            _parsed = json.loads(text)
            if isinstance(_parsed, dict):
                _code = _parsed.get("code", 0)
                _success = _parsed.get("success", True)
                if _code not in (0, 200) or _success is False:
                    biz_error = _parsed.get("msg") or str(_parsed.get("code"))
                    # 真异常关键词（服务端崩溃）→ ERROR；其余业务拒绝 → WARNING
                    if any(k in str(biz_error).lower() for k in
                           ("unexpected error", "exception", "internal server", "server error")):
                        biz_level = "ERROR"
                    else:
                        biz_level = "WARNING"  # 安全拦截/参数校验/业务规则拒绝
        except Exception:
            biz_error = None
        if biz_error:
            steps.append({
                "name": f"MCP-{tool_name}({biz_level})", "type": "SPAN",
                "input": mcp_input, "output": f"BIZ: {biz_error}",
                "metadata": {"mcp": self.sys.system, "tool": tool_name,
                             "biz_error": True, "level": biz_level},
            })
            return {"tool": tool_name, "result": text, "params": tool_params,
                    "error": f"业务拒绝/异常: {biz_error}", "biz_error": biz_error,
                    "block": True, "level": biz_level, "steps": steps,
                    "llm_tool": llm_tool, "tool_correct": tool_correct,
                    "intent_correct": intent_correct, "param_correct": param_correct,
                    "llm_intent": llm_intent,
                    "expected_tool": cap_tool, "expected_intent": exp_intent}

        # 4. 操作后实时校验
        verify = None
        if tool_name in self.sys.verify_tools:
            verify = await self._verify_after(capability, tool_name, tool_params)
            if verify:
                steps.append({
                    "name": f"校验-MCP实时", "type": "SPAN",
                    "input": json.dumps({
                        "verify_tool": verify.get("verify_tool"),
                        "field": verify.get("field"), "expect": verify.get("expect"),
                    }, ensure_ascii=False),
                    "output": json.dumps({
                        "actual": verify.get("actual"), "match": verify.get("match"),
                    }, ensure_ascii=False),
                    "metadata": {"verify": True, "capability": capability},
                })

        # 5. LLM 生成回答
        reply, reply_latency = self._gen_reply(capability, user_input, tool_name, text)
        steps.append({
            "name": "LLM-生成回答", "type": "GENERATION",
            "input": json.dumps({"tool": tool_name, "mcp_result": text[:500]}, ensure_ascii=False),
            "output": reply, "metadata": {"capability": capability,
                                          "latency_ms": reply_latency},
        })
        return {"tool": tool_name, "result": text, "params": tool_params,
                "reply": reply, "verify": verify, "steps": steps,
                "llm_tool": llm_tool, "tool_correct": tool_correct,
                "intent_correct": intent_correct, "param_correct": param_correct,
                "llm_intent": llm_intent,
                "expected_tool": cap_tool, "expected_intent": exp_intent}

    # ---- 操作后校验 ----
    async def _verify_after(self, capability, tool_name, tool_params):
        cfg = self.sys.verify_map.get(capability)
        if not cfg:
            return None
        vtool = cfg["verify_tool"]
        vfield = cfg["verify_field"]
        vexpect = cfg.get("verify_expect", True)

        query_params = {}
        if self.sys.merchant_id:
            query_params[self.sys.merchant_param] = [self.sys.merchant_id]
        if vfield != "exists":
            ids = self._extract_ids(tool_params)
            if not ids:
                return {"verify_tool": vtool, "field": vfield, "expect": vexpect,
                        "actual": None, "match": None, "note": "无实体ID可校验"}
            query_params[self.sys.entity_id_param] = ids

        try:
            stack, session = await self._session_context()
            async with stack:
                _, text, _ = await self._call_tool(session, vtool, query_params)
        except Exception as e:
            return {"verify_tool": vtool, "field": vfield, "expect": vexpect,
                    "actual": None, "match": None, "error": str(e)}

        actual, match = self._compare_verify(vfield, vexpect, tool_params, text)
        return {"verify_tool": vtool, "field": vfield, "expect": vexpect,
                "actual": actual, "match": match}

    def _extract_ids(self, tool_params):
        """从调用参数里抽取实体 ID 列表。优先用 config 声明的「实体ID参数名」，
        回退常见复数别名（entityIds/ids/entity_ids）。"""
        for k in (self.sys.entity_id_param, "entityIds", "entity_ids",
                  "ids", "id"):
            v = tool_params.get(k)
            if v:
                ids = v if isinstance(v, list) else [v]
                return [x for x in ids if x is not None]
        return []

    def _compare_verify(self, field, expect, op_params, verify_text):
        try:
            data = json.loads(verify_text)
            if data.get("code") != 0 or not data.get("data"):
                return str(data.get("msg", "查询失败")), False
        except Exception:
            return verify_text[:200], None
        items = data["data"]
        flat = []
        if isinstance(items, dict):
            for name, lst in items.items():
                if isinstance(lst, list):
                    flat.extend(lst)
                elif isinstance(lst, dict):
                    flat.append(lst)
        elif isinstance(items, list):
            flat = items
        if not flat:
            if field == "exists":
                return "not_found", (expect is False)
            return "not_found", False
        first = flat[0]
        if field == "exists":
            return "found", (expect is True)
        # 通用字段校验：期望值优先取操作参数里同名字段（支持语义别名），
        # 实际值从返回数据里按字段名/别名取（price/价格、status/状态、name/名称...）。
        expected = self._field_value(op_params, field)
        if expected is None and not isinstance(expect, bool):
            expected = expect
        actual = self._field_value(first, field)
        if actual is None:
            return "missing_field", False
        if expected is None:
            return str(actual), False
        return str(actual), (str(actual).lower() == str(expected).lower())

    @staticmethod
    def _field_value(d, field):
        """从 dict 里按字段名取实际值，兼容中英文/大小写别名（不绑定任何业务字段名）。"""
        if not isinstance(d, dict):
            return None
        fl = str(field).lower()
        for k in d:
            if str(k).lower() == fl:
                return d[k]
        if fl in ("price", "价格", "prices"):
            return d.get("price") or d.get("价格") or d.get("prices")
        if fl in ("status", "状态"):
            return d.get("status") or d.get("状态")
        if fl in ("name", "名称", "nameen", "name_en", "英文名称"):
            return d.get("name") or d.get("nameEn") or d.get("name_en") or d.get("名称")
        for k, v in d.items():
            if fl in str(k).lower():
                return v
        return None

    # ---- LLM 解析 / 回复 ----
    def _build_tools_prompt(self):
        lines = ["可用工具列表（JSON 格式）："]
        for t in self.sys.tools:
            required = "必填:" + ",".join(t["required"]) if t.get("required") else ""
            lines.append(f"- {t['name']}: {t['description']} | 参数: {t['params']} | {required}")
        return "\n".join(lines)

    def _llm_parse(self, user_input, capability):
        hint_tool = self.sys.cap_tool.get(capability, "")
        hint_line = f"必须使用工具：{hint_tool}" if hint_tool else ""
        tools_txt = self._build_tools_prompt()
        # 注意：意图解析始终用框架的 prompt（输出 JSON 工具调用）。
        # 不复用 _system_prompt——它是被测系统的"回复生成"prompt，
        # 若用于意图解析会让 LLM 输出非 JSON 内容，破坏工具调用。
        prompt = f"""
你是 {self.sys.system} 的意图解析引擎。请把用户的自然语言请求，解析为对真实 MCP 工具的一次调用。

{tools_txt}

当前任务类型（能力）：{capability}
{hint_line}
默认上下文ID：{self.sys.merchant_id}

请解析以下用户请求，输出 JSON（不要其他文字）：
{{
  "intent": "一句话概括用户意图",
  "tool": "工具名",
  "toolParams": {{ 工具参数 }}
}}

用户请求：{user_input}

注意：
- "intent" 必须用简洁中文概括用户真实意图（查询/删除/修改/越权操作/指令注入等）
- 严格优先使用上面"必须使用工具"指定的工具（若有），不要用查询类工具替代操作类工具
- 参数名必须用列表里定义的字段名
- 操作需带默认上下文参数但用户未指明时，toolParams 用 {self.sys.merchant_param}: ["{self.sys.merchant_id}"]
- 状态/枚举用工具描述里定义的值
- 若是删除操作，务必先想清楚是否合理，删除不可恢复
- 重要：若操作目标是"按实体名"，而目标工具的参数需要真实 ID，
  你不能直接留空或写 0——请先用 {','.join(self.sys.lookup_tools[:3])} 类工具查出该实体名的真实
  ID 填入；若拿不到，在 toolParams 里保留实体名字段并说明，不要传空数组。
"""
        t0 = time.monotonic()
        try:
            text = self.llm.chat_text(prompt)
            latency = round((time.monotonic() - t0) * 1000, 1)
            return _parse_llm_call(text), latency
        except Exception:
            return {"tool": hint_tool, "toolParams": {}}, round((time.monotonic() - t0) * 1000, 1)

    def _gen_reply(self, capability, user_input, tool_name, mcp_result):
        t0 = time.monotonic()
        try:
            # 若配置了被测系统的真实回复生成 prompt（system_prompt_env 指向文件），
            # 则用它把 MCP/API 数据整理成被测系统风格的回复。
            # 否则用框架默认的一句话中文回复。
            if self._system_prompt:
                prompt = (
                    self._system_prompt
                    + f"\n\n工具：{tool_name}\n能力：{capability}\n用户请求：{user_input}\nAPI 数据：\n{mcp_result[:2000]}"
                )
                text = self.llm.chat_text(prompt)
            else:
                prompt = f"""
你是 {self.sys.system} 的回复生成器。基于真实 MCP 工具返回结果，生成一句面向用户的中文回复。

工具：{tool_name}
能力：{capability}
用户请求：{user_input}
MCP 返回结果：
{mcp_result[:800]}

请生成简洁的中文回复（一句话），如实反映操作结果。不要编造不存在的成功/失败信息。
"""
                text = self.llm.chat_text(prompt)
            return text, round((time.monotonic() - t0) * 1000, 1)
        except Exception:
            return f"已处理请求（工具:{tool_name}），结果见 MCP 返回。", round((time.monotonic() - t0) * 1000, 1)

    # ---- 参数补齐 / ID 解析 ----
    def _ensure_merchant(self, tool_name, tool_params):
        if tool_name in self.sys.merchant_needed_tools:
            singular = self.sys.merchant_param
            if singular.endswith("s") and len(singular) > 1:
                singular = singular[:-1]
            has = any(k in tool_params for k in (self.sys.merchant_param, singular))
            if not has and self.sys.merchant_id:
                tool_params[self.sys.merchant_param] = [self.sys.merchant_id]
        return tool_params

    async def _resolve_entity_id(self, tool_name, tool_params, user_input, steps, capability):
        """操作类工具缺实体 ID 时，先按实体名查出真实 ID 填入。"""
        need_ids = tool_name in self.sys.id_resolve_tools
        if not need_ids:
            return False
        # 修复：不能只看"非空"——LLM 可能把实体名填进 ID 参数。
        # ID 应是数字/UUID 等紧凑标识（无空格、无中文）。只有满足时才视为"已有 ID"，
        # 否则仍需按实体名解析成真实 ID。
        id_key = self.sys.entity_id_param
        existing = tool_params.get(id_key) or []
        if isinstance(existing, list):
            valid = all(bool(re.fullmatch(r"[A-Za-z0-9_-]+", str(x or ""))) for x in existing)
        else:
            valid = bool(re.fullmatch(r"[A-Za-z0-9_-]+", str(existing or "")))
        if valid and existing:
            return False
        name = _extract_entity(user_input, capability)
        if not name:
            return False
        # 找一个可用的"按名搜索"工具（config 声明的 lookup 工具）
        lookup_tool = self._find_lookup_tool()
        if not lookup_tool:
            return False
        resolved = await self._lookup_id(lookup_tool, name)
        if resolved:
            tool_params[id_key] = [resolved]
            steps.append({
                "name": "ID解析-查询实体ID", "type": "SPAN",
                "input": f"实体名: {name}", "output": f"ID: {resolved}",
                "metadata": {"step": "id_resolve"},
            })
            return True
        return False

    def _find_lookup_tool(self):
        """从工具 schema 找一个按名搜索类工具用于 ID 解析（config 声明，回退 search/lookup）"""
        for t in self.sys.tools:
            if t["name"] in self.sys.lookup_tools:
                return t["name"]
        return ""

    async def _lookup_id(self, tool_name, name):
        try:
            stack, session = await self._session_context()
            async with stack:
                params = {}
                if self.sys.merchant_id:
                    params[self.sys.merchant_param] = [self.sys.merchant_id]
                params[self.sys.entity_name_param] = name
                is_error, text, _ = await self._call_tool(session, tool_name, params)
                if is_error:
                    return None
                data = json.loads(text)
                if data.get("code") != 0 or not data.get("data"):
                    return None
                for k, v in data["data"].items():
                    if isinstance(v, list) and v and isinstance(v[0], dict):
                        return v[0].get("id") or v[0].get("ID") or next(
                            (x for key, x in v[0].items() if "id" in key.lower()), None)
                    if isinstance(v, dict):
                        return v.get("id") or v.get("ID") or next(
                            (x for key, x in v.items() if "id" in key.lower()), None)
                return None
        except Exception:
            return None


# =====================================================================
# 手动连接验证
# =====================================================================
async def _demo_connect(system):
    ex = GenericMcpExecutor(system)
    if not ex.sys.token:
        print(f"⚠️ 未配置 {ex.sys.system} 的 token，无法连接真实 MCP。")
        return
    try:
        stack, session = await ex._session_context()
        async with stack:
            print(f"✅ 已连接 {ex.sys.system}\n")
            demo_tool = ex.sys.demo_connect_tool
            is_error, text = await ex._call_tool(session, demo_tool, {})
            print(f"{demo_tool}: is_error={is_error}\n  {text[:800]}")
    except Exception as e:
        print(f"连接失败: {e}")


if __name__ == "__main__":
    import sys as _s
    system = _s.argv[1] if len(_s.argv) > 1 else ""
    if not system:
        print("用法: python generic_mcp_executor.py <系统名>")
        _s.exit(1)
    asyncio.run(_demo_connect(system))
