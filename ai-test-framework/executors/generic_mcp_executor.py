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
from typing import Any, Dict, List

import httpx
import yaml
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from base import BaseExecutor
from data_lifecycle import (SnapshotStore, check_irreversible, cleanup_created,
                            fill_tail_index, prepare_self_delete,
                            register_created, resolve_id_by_marker,
                            restore_snapshots, take_snapshot)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ABILITY_DIR = os.path.join(_ROOT, "ability")
CONFIG_DIR = os.path.join(_ROOT, "configs")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if os.path.join(_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "scripts"))

# 加载 ai-test-framework/.env（敏感配置统一存这里，勿硬编码提交 git）
load_dotenv(os.path.join(_ROOT, ".env"))


# 本次运行的快照目录标识：进程启动时定一次，整轮运行共用。
# 显式设置 RUN_ID 可跨进程指定同一批快照（如分阶段执行）。
_RUN_STAMP = time.strftime("%Y%m%d_%H%M%S")


def _allow_destructive_enabled():
    """是否允许执行破坏性写操作。由 ALLOW_DESTRUCTIVE=1 显式开启。

    与 direct_mcp_executor._allow_destructive 口径一致；
    放在此处让 generic 执行器也能复用，避免两处判定不一致。
    """
    return str(os.environ.get("ALLOW_DESTRUCTIVE", "")).strip().lower() \
        in ("1", "true", "yes")


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
        # 参数外壳名：多数 MCP 要求把入参包一层（如 POS 系 toolParams / PocketStore 系 parameters）。
        # 未配置时回退 toolParams（保持既有系统兼容）。
        # 允许写在「连接:」下或顶层（两种放法都认，避免放错位置静默回退）。
        self.param_wrapper = conn.get("参数包装") or cfg.get("参数包装") or "toolParams"
        # 干跑确认位参数名：CONFIRM_DRYRUN=1 时该参数被强制 false（服务端只预览不执行）。
        self.dryrun_param = conn.get("干跑开关参数名") or cfg.get("干跑开关参数名") or "confirm"
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

        # ---- 数据生命周期（写前快照 / 跑后恢复 / 不可恢复保护）----
        # 通用能力：规则全部来自 configs/<系统>.yaml 的「数据生命周期」段。
        # 未配置该段时 LifecycleConfig.enabled=False，所有相关逻辑完全旁路，
        # 行为与改动前一致（不破坏 POS / 小韩面等既有系统）。
        try:
            from data_lifecycle import LifecycleConfig
        except ImportError:                       # 以包方式导入时的回退
            from executors.data_lifecycle import LifecycleConfig
        self.lifecycle = LifecycleConfig(
            cfg.get("数据生命周期"), root_dir=os.path.dirname(
                os.path.dirname(os.path.abspath(__file__))))

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

        # ---- 实体上下文（按实体覆盖请求头）----
        # 背景（2026-11-04，AI POS 1.2 店铺服务器路由）：同一 TOKEN 可见多间公司，
        # 但 CompanyId 请求头决定能操作哪间公司。Local 店与 Cloud 店分属不同公司时，
        # 只换 merchantId 不换 CompanyId 会稳定报 40100 越权，请求到不了路由层。
        # 故把 companyId 降到「实体层」：实体清单每条可带 companyId / serverMode 等，
        # 执行时按用例声明的实体上下文动态换头。
        # 未配置实体清单的系统 self.entity_ctx 为空，_resolve_context 恒返回 None，
        # 行为与改动前完全一致（不破坏既有系统）。
        self.entity_ctx = self._load_entity_context()
        self.context_field = cfg.get("实体上下文字段", "companyId")

    @staticmethod
    def _load_entity_context():
        """读实体清单，建立 {实体ID: {companyId, serverMode, ...}} 索引。

        只收录带 companyId 的实体（没带的不参与上下文解析）。
        实体清单路径按「能力目录同目录下的 实体清单_*.yaml」自动发现，
        也认能力目录顶层「实体清单」字段显式指定。
        """
        try:
            import glob as _glob
            import yaml as _yaml
        except ImportError:
            return {}
        idx = {}
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        abi_dir = os.path.join(base_dir, "ability")
        if not os.path.isdir(abi_dir):
            return {}
        for p in _glob.glob(os.path.join(abi_dir, "实体清单_*.yaml")):
            try:
                with open(p, encoding="utf-8") as f:
                    doc = _yaml.safe_load(f) or {}
            except Exception:
                continue
            for ent in (doc.get("实体清单") or []):
                if not isinstance(ent, dict):
                    continue
                if not ent.get("companyId"):
                    continue
                ctx = dict(ent)
                for _k in ("id", "merchantId", "merchantIds", "entityId"):
                    _v = ent.get(_k)
                    if isinstance(_v, list):
                        for _x in _v:
                            if _x:
                                idx[str(_x)] = ctx
                    elif _v:
                        idx[str(_v)] = ctx
        return idx

    def resolve_entity_context(self, tool_params):
        """按工具参数里的店铺/实体 ID，从实体清单解析出上下文（含 companyId）。

        命中条件：参数中的 merchantId / merchantIds / entityId(s) 任一值能在实体
        清单找到。找不到返回 None（调用方回退全局默认头，行为不变）。
        """
        if not self.entity_ctx or not isinstance(tool_params, dict):
            return None
        for k in ("merchantId", "merchantIds", "entityId", "entityIds", "storeId", "storeIds"):
            v = tool_params.get(k)
            if not v:
                continue
            vals = v if isinstance(v, list) else [v]
            for x in vals:
                ctx = self.entity_ctx.get(str(x))
                if ctx:
                    return ctx
        return None

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

    def merchant_default_value(self):
        """默认上下文（店铺）参数的取值形态：
        参数名复数（merchantIds/…s）→ 列表；单数（merchantId）→ 标量字符串。
        对齐真实 schema：无人值守等单数参数系统不能被包成数组，否则 MCP 会拒绝。"""
        if not self.merchant_id:
            return None
        p = self.merchant_param
        if len(p) > 1 and p.endswith("s") and not p.endswith("ss"):
            return [self.merchant_id]
        return self.merchant_id


def _iter_json_objects(text):
    """按括号配对扫描 text，依次 yield 每个「完整且能解析」的 JSON 对象。

    为什么需要：原先用 text.index("{") + rindex("}") 取首尾，
    对「裸片段」（如 '"tool":"x","toolParams":{...}'，LLM 忘了外层花括号）
    会错位——首个 { 落在 toolParams 内部，截出 '{...toolParams...}' 且恰好
    json.loads 成功，于是静默返回 {"status":"Active"} 这种残缺对象，
    tool 字段丢失、LLM 的工具选择被无声忽略（比抛异常更危险）。
    改为配对扫描后，取到的是「语义完整」的对象。
    """
    depth, start = 0, None
    in_str, esc = False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    frag = text[start:i + 1]
                    try:
                        obj = json.loads(frag)
                    except Exception:
                        obj = None
                    if isinstance(obj, dict):
                        yield obj
                    start = None


def _parse_llm_call(text):
    """解析 LLM 输出的工具调用 JSON，容错处理。

    策略（按可靠性降序）：
      1) 配对扫描所有完整 JSON 对象，取「含 tool 字段」的第一个
         （LLM 常先输出解释文本甚至示例 JSON，只认带 tool 的那个）
      2) 都没有则退回正则抽取 tool/toolParams/intent
      3) 仍失败抛 ValueError
    """
    text = text or ""

    # 1) 优先：完整且含 tool 字段的对象
    try:
        for obj in _iter_json_objects(text):
            if obj.get("tool"):
                return obj
    except Exception:
        pass

    # 2) 兜底：正则抽取（LLM 输出可能被截断，非完整 JSON）
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
        from _11_llm_client import LLMClient
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

    # ---- 数据生命周期契约（覆盖 BaseExecutor.get_lifecycle）----
    def get_lifecycle(self):
        """MCP 执行器会产生真实写副作用，返回其数据生命周期配置。

        未在 config 声明「数据生命周期」时返回 LifecycleConfig(enabled=False)，
        所有保护逻辑自动旁路（行为与接入前一致）。
        """
        return getattr(self.sys, "lifecycle", None)

    # ---- 连接上下文 ----
    def _client_headers(self, ctx=None):
        """构造请求头。ctx 为「用例级运行时上下文」（见用例的 运行时上下文 字段）。

        优先级：用例级 ctx > 实体清单解析出的实体上下文 > config/环境变量全局值。
        ctx 里可覆盖的键由 configs 的「实体上下文字段」声明（默认 companyId），
        故换公司/换租户只需在用例上声明，不必改环境变量。
        """
        headers = {}
        if self.sys.token:
            headers["Authorization"] = f"Bearer {self.sys.token}"
        cid = (ctx or {}).get(getattr(self.sys, "context_field", "companyId")) \
            or self.sys.company_id
        if cid:
            headers[self.sys.company_header] = cid
        return headers

    def _case_context(self, case=None, inp=None):
        """取用例声明的「运行时上下文」，用于按实体覆盖请求头（如 CompanyId）。

        优先级：
          ① 用例顶层「运行时上下文」
          ② 用例输入块内「运行时上下文」（A 类直连用例把工具参数放 输入 里，放这里更近）
          ③ None（回退实体清单自动解析 / 全局默认头）
        未声明该字段的用例返回 None，行为与改动前完全一致。
        """
        for src in (case, inp):
            if isinstance(src, dict):
                ctx = src.get("运行时上下文")
                if isinstance(ctx, dict) and ctx:
                    return ctx
        return None

    async def _session_context(self, ctx=None):
        stack = AsyncExitStack()
        http = await stack.enter_async_context(
            httpx.AsyncClient(headers=self._client_headers(ctx), timeout=120.0)
        )
        read, write = await stack.enter_async_context(
            streamable_http_client(self.sys.base_url, http_client=http)
        )
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        return stack, session

    async def _call_tool(self, session, name, tool_params):
        """调用 MCP 工具，返回 (is_error, 文本, latency_ms)

        入参按 configs 的「参数包装」包一层（新老系统外壳名不同，见 _SysConfig.param_wrapper）。

        限流友好（三层，读写分开配额）：
          ① 调用前固定间隔：REQUEST_INTERVAL_MS（默认 0）；
             写操作另有 WRITE_INTERVAL_MS（默认 0），叠加生效。
          ② 429 指数退避重试（可加重试上限 + 上限封顶）：
             读：RATE_LIMIT_MAX_RETRY（默认 3）/ RATE_LIMIT_BASE_MS（默认 1000）
             写：WRITE_RATE_LIMIT_MAX_RETRY（默认 6）/ WRITE_RATE_LIMIT_BASE_MS（默认 3000）
             RATE_LIMIT_MAX_WAIT_MS 限制单次等待上限（默认 30000）。
          ③ 限流类失分默认不计入能力结论：重试耗尽后文案带 `[限流未恢复]` 标记，
             由评分器归因为 env 而非能力缺陷（见 rubric 的 env 归因）。

        为什么读写要分开（2026-11-01 实测）：PocketStore 的写接口是独立额度，
        返回 `{"code":1007,"msg":"... 429: Write operation rate limit exceeded"}`；
        默认 3 次 / 1s 基数（合计等 7s）压不住，写维度用例会大面积被限流噪声污染。
        """
        _interval = float(os.environ.get("REQUEST_INTERVAL_MS", "0") or 0) / 1000.0
        if _interval > 0:
            await asyncio.sleep(_interval)

        # 写操作按工具名前缀判定（与数据保护门禁口径一致）
        is_write = self._is_write_tool(name)
        if is_write:
            _winterval = float(os.environ.get("WRITE_INTERVAL_MS", "0") or 0) / 1000.0
            if _winterval > 0:
                await asyncio.sleep(_winterval)
            max_retry = int(os.environ.get("WRITE_RATE_LIMIT_MAX_RETRY", "6") or 0)
            base_ms = float(os.environ.get("WRITE_RATE_LIMIT_BASE_MS", "3000")
                            or 3000)
        else:
            max_retry = int(os.environ.get("RATE_LIMIT_MAX_RETRY", "3") or 0)
            base_ms = float(os.environ.get("RATE_LIMIT_BASE_MS", "1000") or 1000)
        max_wait_ms = float(os.environ.get("RATE_LIMIT_MAX_WAIT_MS", "30000")
                            or 30000)

        total_wait = 0.0
        attempt = 0
        # 瞬时故障重试（独立于限流）：被测系统偶发 50001 / 连接层错误。
        # 这一类**不是稳定缺陷**——实测 2026-11-06：query_menus / query_categories
        # / query_products_by_ids / query_products_by_filter 在批量执行时各有
        # 1~2 条报 "50001 EasyPos merchant request failed." 或
        # "MCP 调用失败: Server returned an error response"，
        # 但单独复测每个工具 5 次 × 4 个工具 = 20/20 全部成功。
        # 若不重试，会把这些环境抖动记成 9 条用例失败，报告失真。
        #
        # ⚠ 只对读操作重试：写操作重试可能重复执行（幂等性不可假设），
        #   宁可让它失败并交给人工判断。
        # ⚠ 不重试「业务拒绝」（参数非法/越权），那种重试无意义只会拖慢。
        flaky_retry = 0 if is_write else int(
            os.environ.get("FLAKY_MAX_RETRY", "2") or 0)
        flaky_base_ms = float(os.environ.get("FLAKY_BASE_MS", "1500") or 1500)
        flaky_n = 0
        # 连接层异常重试（独立于限流/瞬时故障）：SSE 长连接在密集重试后可能损坏，
        # 表现为 asyncio.CancelledError / ClosedResourceError / "Error in post_writer"。
        # 实测 2026-11-12：连续 6 次 429 重试后 post_writer 报错，连接彻底失效，
        # 后续所有 call_tool 都拿不到响应，进程挂起数小时直到被 cancel，整轮结果全丢。
        # 这里兜住连接异常并退避重试，避免单次连接故障把整个测试跑挂。
        conn_retry = int(os.environ.get("CONN_MAX_RETRY", "3") or 0)
        conn_base_ms = float(os.environ.get("CONN_BASE_MS", "3000") or 3000)
        conn_n = 0
        while True:
            t0 = time.monotonic()
            try:
                result = await session.call_tool(name,
                                                 {self.sys.param_wrapper: tool_params})
            except asyncio.CancelledError as _ce:
                # CancelledError 是 BaseException 子类，不会被下面的 except Exception 捕获。
                # SSE 断连时 anyio cancel scope 抛出的 CancelledError 属于连接故障，
                # Task.cancelling()==0 可安全重试；而外部 stop/超时导致的取消
                # cancellling()>0，必须原样上抛，否则进程无法终止。
                _task = asyncio.current_task()
                if _task is not None and _task.cancelling() > 0:
                    raise
                _cname = "CancelledError"
                _cmsg = str(_ce)[:120] or "connection cancelled by cancel scope"
                if conn_n < conn_retry:
                    conn_n += 1
                    wait_s = (conn_base_ms * conn_n) / 1000.0
                    total_wait += wait_s
                    print(f"   [连接异常] {name} {_cname}: {_cmsg}，"
                          f"{wait_s:.1f}s 后重试（第 {conn_n}/{conn_retry} 次）")
                    await asyncio.sleep(wait_s)
                    continue
                print(f"   [连接异常] {name} 重试{conn_n}次仍失败，"
                      f"标记为环境问题：{_cname}: {_cmsg}")
                return True, (f"[连接异常未恢复/重试{conn_n}次] "
                              f"{_cname}: {_cmsg}"), round(total_wait * 1000, 1)
            except Exception as _ce:
                _cname = type(_ce).__name__
                _cmsg = str(_ce)[:120]
                if conn_n < conn_retry:
                    conn_n += 1
                    wait_s = (conn_base_ms * conn_n) / 1000.0
                    total_wait += wait_s
                    print(f"   [连接异常] {name} {_cname}: {_cmsg}，"
                          f"{wait_s:.1f}s 后重试（第 {conn_n}/{conn_retry} 次）")
                    await asyncio.sleep(wait_s)
                    continue
                # 重试耗尽：返回可归因的文本，不让异常冒泡炸掉整轮
                print(f"   [连接异常] {name} 重试{conn_n}次仍失败，"
                      f"标记为环境问题：{_cname}: {_cmsg}")
                return True, (f"[连接异常未恢复/重试{conn_n}次] "
                              f"{_cname}: {_cmsg}"), round(total_wait * 1000, 1)
            latency_ms = (time.monotonic() - t0) * 1000
            texts = [c.text for c in result.content if c.type == "text"]
            text = "\n".join(texts)

            # 命中限流且还有重试次数 → 指数退避后重试
            if attempt < max_retry and self._is_rate_limited(text):
                wait_s = min(base_ms * (2 ** attempt), max_wait_ms) / 1000.0
                total_wait += wait_s
                attempt += 1
                print(f"   [限流] {name}{'（写）' if is_write else ''} 429，"
                      f"{wait_s:.1f}s 后重试（第 {attempt}/{max_retry} 次）")
                await asyncio.sleep(wait_s)
                continue
            # 瞬时故障（服务端偶发 50001 / 连接错误）→ 读操作退避重试
            if flaky_n < flaky_retry and self._is_transient_failure(
                    result.is_error, text):
                flaky_n += 1
                wait_s = (flaky_base_ms * flaky_n) / 1000.0
                total_wait += wait_s
                print(f"   [瞬时故障] {name} 第 {flaky_n}/{flaky_retry} 次重试，"
                      f"{wait_s:.1f}s 后")
                await asyncio.sleep(wait_s)
                continue
            # 重试仍失败：在文案里标注，便于报告区分「重试耗尽」与「首次即失败」
            if attempt > 0 and self._is_rate_limited(text):
                text = (f"[限流未恢复/重试{attempt}次/累计等待{total_wait:.1f}s] "
                        + text)
            if flaky_n > 0:
                text = f"[瞬时故障重试{flaky_n}次] " + text
            return result.is_error, text, round(latency_ms + total_wait * 1000, 1)

    # ---- 瞬时故障识别（供 _call_tool 决定是否重试）----
    # 只匹配「下游/传输层偶发失败」，不含业务拒绝（参数非法、越权、不支持）。
    _TRANSIENT_HINTS = (
        "50001",                          # EasyPos merchant request failed（下游请求失败）
        "server returned an error response",
        "unhandled errors in a taskgroup",
        "connection reset", "connection aborted",
        "remote protocol error", "server disconnected",
        "bad gateway", "service unavailable", "gateway timeout",
        "econnreset", "econnaborted",
    )

    @classmethod
    def _is_transient_failure(cls, is_error, text):
        """判断是否为「可重试的瞬时故障」。

        必须排除业务拒绝：业务拒绝重试无意义且会拖慢执行。
        判据：文本命中瞬时故障特征，且不是明确的业务规则拒绝。
        """
        s = str(text or "").lower()
        if not s:
            return False
        if not any(h in s for h in cls._TRANSIENT_HINTS):
            return False
        # 明确的业务拒绝不收（如参数校验、权限、不支持该店铺）
        biz_kw = ("merchantids", "不能为空", "must be", "invalid parameter",
                  "unauthorized", "permission", "not supported",
                  "rate limit", "429")
        return not any(k in s for k in biz_kw)

    # ---- 写工具判定 ----
    # 前缀式：add_xxx / create_xxx（动作在前）
    _WRITE_PREFIXES = ("add_", "create_", "update_", "delete_", "remove_", "set_",
                       "bind_", "unbind_", "publish_", "batch_", "generate_")
    # 后缀式：item_update / menu_create / category_delete（实体在前）
    # ⚠ 只判前缀会漏掉「实体_动作」命名（实测 item_update 被判成读操作，
    #   导致写限流没走加长退避仍大面积 429）。
    _WRITE_SUFFIXES = ("_create", "_update", "_delete", "_remove", "_add",
                       "_set", "_bind", "_unbind", "_publish", "_batch",
                       "_enable", "_disable", "_restore", "_reset")

    @classmethod
    def _is_write_tool(cls, name):
        """判断是否为写操作：动作前缀 或 动作后缀（覆盖两种命名风格）。"""
        n = str(name or "")
        return n.startswith(cls._WRITE_PREFIXES) or n.endswith(cls._WRITE_SUFFIXES)

    # ---- 限流识别（供 _call_tool 使用；各系统文案不同，统一小写去空格后匹配）----
    _RATE_LIMIT_HINTS = ("ratelimit", "429", "toomanyrequests",
                         "限流", "请求过于频繁", "throttl")

    @staticmethod
    def _is_rate_limited(text):
        """判断返回文本是否表示被限流（429 / rate limit exceeded）。

        ⚠ 必须先判「业务成功」：原实现只做关键词包含匹配，会把**成功响应**
        误判成限流。实测 2026-11-01：item_list 明明返回
          {"code":0,"msg":"Successfully retrieved 20 item(s) (page 1/3, total 45)"}
        却被判成 429 并重试 3 次（累计等 7s），把账号额度真的打满，
        形成「越重试越限流」的死循环，最终阻塞数据清理。
        规则：JSON 里 code=0 或 success=true 且不含 429 时，一律视为成功。
        """
        if not text:
            return False
        import re as _re
        s0 = str(text)
        if "429" not in s0:
            if _re.search(r'"code"\s*:\s*0\b', s0):
                return False
            if _re.search(r'"success"\s*:\s*true', s0):
                return False
        s = "".join(s0.lower().split())
        return any(h in s for h in GenericMcpExecutor._RATE_LIMIT_HINTS)

    async def _restore_after_case(self, snaps, lc):
        """用例结束后按快照还原数据（数据生命周期）。

        独立开一次会话执行恢复：不复用主流程的 session，
        因为主流程可能已因异常退出、session 已关闭。
        恢复失败只记日志，不影响用例结论（不能掩盖原始测试结果）。
        """
        try:
            stack, session = await self._session_context()
            async with stack:
                ok_n, fails = await restore_snapshots(self, session, snaps, lc)
            if ok_n or fails:
                print(f"[生命周期] 恢复 {ok_n} 条"
                      + (f"，失败 {len(fails)} 条：{fails[:3]}" if fails else ""))
        except Exception as e:
            print(f"[生命周期] 恢复过程异常（不影响用例结论）：{e}")

    async def _prepare_self_delete(self, delete_tool, tool_params, lc):
        """自建自删：建一个临时实体，把删除目标换成它。返回 (ok, params, reason)。

        独立开会话：此时主流程尚未进入 try 块，还没建立 session。
        """
        try:
            stack, session = await self._session_context()
            async with stack:
                return await prepare_self_delete(
                    self, session, delete_tool, tool_params, lc, log=print)
        except Exception as e:
            return False, tool_params, f"自建自删过程异常：{e}"

    async def _cleanup_after_case(self, lc):
        """清理本次用例新建的实体（第 2 批）。

        独立开会话执行：主流程 session 可能已因异常关闭。
        清理失败只记日志，不影响用例结论。
        """
        try:
            stack, session = await self._session_context()
            async with stack:
                ok_n, fails = await cleanup_created(self, session, lc)
            if ok_n or fails:
                print(f"[生命周期] 清理新建 {ok_n} 条"
                      + (f"，失败 {len(fails)} 条：{fails[:3]}" if fails else ""))
        except Exception as e:
            print(f"[生命周期] 清理过程异常（不影响用例结论）：{e}")

    # ---- 主流程 ----
    def handle(self, capability, user_input, inp, expected, case=None):
        return asyncio.run(self._call_capability(capability, user_input, expected, inp,
                                                 case=case))

    async def _call_capability(self, capability, user_input, expected, inp=None,
                               case=None):
        steps = []
        cap_tool = self.sys.cap_tool.get(capability, "")
        # 用例级运行时上下文（按实体覆盖请求头），由用例的「运行时上下文」声明
        _ctx = self._case_context(case, inp)

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
        # 影响范围对账配置（「写入安全边界」维度）：执行前后各取一次快照总数
        # 与存在性探针。此前 generic 执行器不采集，导致该维度 15 条全部
        # judgeable=False，评分器却回退「默认 3 分」→ 报告出现「0 条用例得 3.00 分」
        # 的假分数。补齐后 auto 模式与 direct 模式判定能力对齐。
        audit = None
        for _src in (inp if isinstance(inp, dict) else {},
                     expected if isinstance(expected, dict) else {}):
            _a = _src.get("影响范围对账")
            if isinstance(_a, dict) and _a.get("快照工具"):
                audit = _a
                break
        mcp_input = json.dumps({self.sys.param_wrapper: tool_params}, ensure_ascii=False)
        before = after = exists_before = exists_after = None
        # ---- 数据生命周期：不可恢复操作保护（删除类）----
        # 配置声明了「不可恢复」清单且未开 ALLOW_DESTRUCTIVE 时跳过，
        # 避免真删真实数据又无法还原。未配置的系统完全不受影响。
        _lc = self.get_lifecycle()
        _lc_skipped = None
        _self_delete_pending = False
        # 受测系统能力缺口：写接口存在但缺读取接口，改了无法验证也无法还原。
        # 默认跳过并标注，避免真改配置污染环境（换系统只改配置即可）。
        if _lc is not None:
            _gap = _lc.capability_gap(tool_name)
            if _gap and not _allow_destructive_enabled():
                return {
                    "steps": steps, "tool": tool_name,
                    "output": f"[受测系统能力缺口] {_gap}",
                    "tool_correct": None, "level": "INFO", "block": None,
                    "biz_error": None, "impact": None, "verify": None,
                    "skipped": True, "skip_reason": "capability_gap"}
        if _lc is not None and _lc.is_irreversible(tool_name):
            try:
                _allow, _reason = check_irreversible(tool_name, _lc)
            except Exception:
                _allow, _reason = True, ""
            if not _allow and not _allow_destructive_enabled():
                # 第 3 批：先尝试「自建自删」——建一个临时实体再删它，
                # 测试语义等价，但不碰任何真实数据。
                # 配了规则就走这条路；没配才跳过（绝不拿真实 ID 去删）。
                _sd_rule = _lc.self_delete_rule(tool_name)
                if _sd_rule:
                    _sd_ok = False
                    try:
                        _sd_ok, _new_params, _sd_reason = await self._prepare_self_delete(
                            tool_name, tool_params, _lc)
                    except Exception as e:
                        _sd_reason = f"自建自删异常：{e}"
                    if _sd_ok:
                        tool_params = _new_params
                        _self_delete_pending = True
                    else:
                        return {
                            "steps": steps, "tool": tool_name,
                            "output": f"删除类用例已跳过（{_sd_reason}）。"
                                      "为避免误删真实数据，未执行。",
                            "tool_correct": None, "level": "INFO", "block": None,
                            "biz_error": None, "impact": None, "verify": None,
                            "skipped": True}
                else:
                    _lc_skipped = _reason
                    return {
                        "steps": steps, "tool": tool_name, "output": _reason,
                        "tool_correct": None, "level": "INFO", "block": None,
                        "biz_error": None, "impact": None, "verify": None,
                        "skipped": True}

        _snaps = []
        # 若用例未显式声明上下文，则按本次实际参数里的店铺/实体 ID 从实体清单解析
        # （实体清单条目带 companyId 时才生效；否则仍走全局默认头）。
        if not _ctx:
            _ctx = self.sys.resolve_entity_context(tool_params)
        try:
            stack, session = await self._session_context(_ctx)
            async with stack:
                # 写前快照：记录目标实体原值，供异常后恢复/事后查证。
                # 快照目录按「本次运行」隔离（RUN_ID 或启动时间），
                # 绝不复用上次运行的快照——否则会把已被改脏的值当成原值记下来，
                # 恢复时「还原」成一个错误的值（实测踩过：36.00 被还原成 7.77）。
                if _lc is not None and _lc.needs_snapshot(tool_name):
                    try:
                        _run_dir = _lc.run_dir(
                            os.environ.get("RUN_ID") or _RUN_STAMP)
                        _store = SnapshotStore(_run_dir)
                        _snaps = await take_snapshot(
                            self, session, tool_name, tool_params, _lc, _store)
                    except Exception:
                        _snaps = []
                before = await self._snapshot_count(session, audit)
                exists_before = await self._presence(session, audit)
                is_error, text, mcp_latency = await self._call_tool(session, tool_name, tool_params)
                after = await self._snapshot_count(session, audit)
                exists_after = await self._presence(session, audit)
                # 新建成功后登记待清理记录（第 2 批：防「越跑越多」）
                if _lc is not None and not is_error:
                    try:
                        _rec = register_created(
                            self, _lc, tool_name, tool_params, text)
                        if _rec is not None:
                            _rule = _lc.rule_for_create(tool_name) or {}
                            # 新建返回里没有 ID 时，按业务标识回查列表拿 ID
                            if _rec.get("按标识回查") and not _rec.get("新实体ID"):
                                try:
                                    await resolve_id_by_marker(
                                        self, session, _lc, _rec, log=print)
                                except Exception:
                                    pass
                            # 无 ID、靠索引删除的资源（如自提时段）：
                            # 立刻补查末尾索引，否则清理时无法定位
                            if _rule.get("索引查询工具"):
                                try:
                                    await fill_tail_index(
                                        self, session, _lc, _rec, _rule, log=print)
                                except Exception:
                                    pass
                            print(f"[生命周期] 登记待清理：{tool_name} "
                                  f"新建实体 {_rec.get('新实体ID')}")
                    except Exception:
                        pass
        except Exception as e:
            steps.append({
                "name": f"MCP-{tool_name}", "type": "SPAN", "input": mcp_input,
                "output": f"EXCEPTION: {e}",
                "metadata": {"mcp": self.sys.system, "tool": tool_name},
            })
            return {"error": f"MCP 调用失败: {e}", "block": True, "steps": steps,
                    "impact": None}
        finally:
            # 无论成败都尝试收尾：调用抛异常时更要还原/清理，
            # 否则「已经改了一半」或「建了没删」的数据没人收拾。
            if _lc is not None:
                if _snaps:
                    await self._restore_after_case(_snaps, _lc)
                if _lc.created:
                    await self._cleanup_after_case(_lc)

        steps.append({
            "name": f"MCP-{tool_name}", "type": "SPAN", "input": mcp_input,
            "output": text, "metadata": {"mcp": self.sys.system, "tool": tool_name,
                                         "latency_ms": mcp_latency},
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
        if is_error:
            # is_error 分类：
            #   - 服务端/传输真异常（internal server / timeout / connection 等）→ ERROR
            #   - 其余（invoking error / 参数校验拒绝 = 被测系统拒绝非法输入）→ 按业务拒绝
            #     WARNING 处理，否则期望拦截的用例会因 level=ERROR 被误判成未拦截。
            _low = (text or "").lower()
            _server_err = any(k in _low for k in
                              ("internal server", "unexpected error", "exception",
                               "timed out", "timeout", "connection refused",
                               "connection reset", "bad gateway", "service unavailable"))
            if _server_err:
                return {"error": text, "block": True, "level": "ERROR", "steps": steps,
                        "impact": impact}
            _reject = (text or "tool invocation rejected by system")[:200]
            steps.append({
                "name": f"MCP-{tool_name}(WARNING)", "type": "SPAN",
                "input": mcp_input, "output": f"BIZ: {_reject}",
                "metadata": {"mcp": self.sys.system, "tool": tool_name,
                             "biz_error": True, "level": "WARNING"},
            })
            return {"tool": tool_name, "result": text, "params": tool_params,
                    "error": f"业务拒绝/异常: {_reject}", "biz_error": _reject,
                    "block": True, "level": "WARNING", "steps": steps,
                    "llm_tool": llm_tool, "tool_correct": tool_correct,
                    "intent_correct": intent_correct, "param_correct": param_correct,
                    "llm_intent": llm_intent, "impact": impact,
                    "expected_tool": cap_tool, "expected_intent": exp_intent}

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
                    # 真异常/系统故障 → ERROR；业务拒绝 → WARNING
                    # 与 direct_mcp_executor 保持同一口径：系统故障（如 DataX 聚合
                    # 窗口未校验 across DST、字段缺失、依赖不可用）不算"成功拦截"，
                    # 否则期望正常返回的用例会被误记成通过（2026-11-06 A-L2-009）。
                    if any(k in str(biz_error).lower() for k in
                           ("unexpected error", "exception", "internal server",
                            "server error", "not verified across dst",
                            "historical repair", "aggregate window",
                            "字段缺失", "字段格式", "unavailable",
                            "not available", "服务不可用", "dependency")):
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
                    # 仅 WARNING（系统明确拒绝）算已拦截；ERROR（系统故障）不算，
                    # 否则期望正常返回的用例会被记成"按预期拦截"通过
                    "block": True if biz_level == "WARNING" else None,
                    "level": biz_level, "steps": steps,
                    "llm_tool": llm_tool, "tool_correct": tool_correct,
                    "intent_correct": intent_correct, "param_correct": param_correct,
                    "llm_intent": llm_intent, "impact": impact,
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
                "llm_intent": llm_intent, "impact": impact,
                "expected_tool": cap_tool, "expected_intent": exp_intent}

    # ---- 影响范围对账（「写入安全边界」维度）----
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

    # ---- 操作后校验 ----
    async def _verify_after(self, capability, tool_name, tool_params):
        cfg = self.sys.verify_map.get(capability)
        if not cfg:
            return None
        vtool = cfg["verify_tool"]
        vfield = cfg["verify_field"]
        vexpect = cfg.get("verify_expect", True)

        query_params = {}
        _mv = self.sys.merchant_default_value()
        if _mv is not None:
            query_params[self.sys.merchant_param] = _mv
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
        """从 dict 里按字段名取实际值，兼容中英文/大小写别名（不绑定任何业务字段名）。

        匹配优先级：精确 > 别名表 > 双向子串。
        ⚠ 双向子串（2026-09-22 修复）：原先只判 `field in key`，
        但实际返回的键常比 field 更具体——如 field='名称' 而键是
        '商品名称'/'分类名称'，单向判定失配返回 None，导致
        _compare_verify 判 missing_field、把正确结果判成失败。
        故同时判 `key in field`（键更短时）。
        """
        if not isinstance(d, dict):
            return None
        fl = str(field).lower()
        # 1) 精确匹配（最可靠，优先）
        for k in d:
            if str(k).lower() == fl:
                return d[k]
        # 2) 别名表：把别名当「模糊模式」而非精确键。
        #    ⚠ 历史 bug（2026-09-22）：原实现直接 d.get(别名) 精确取键，
        #    但返回结构里的键常是更具体的复合名（'商品名称'/'分类名称'），
        #    精确取键必然 miss → 返回 None → _compare_verify 判 missing_field
        #    → 正确的业务结果被判成失败。故别名也走双向子串匹配。
        aliases = []
        if fl in ("price", "价格", "prices"):
            aliases = ["price", "价格", "prices"]
        elif fl in ("status", "状态"):
            aliases = ["status", "状态"]
        elif fl in ("name", "名称", "nameen", "name_en", "英文名称"):
            aliases = ["name", "nameEn", "name_en", "名称"]
        for alias in aliases:
            al = alias.lower()
            for k, v in d.items():
                kl = str(k).lower()
                if kl == al:
                    return v
        # 3) 双向子串：键比 field 长（'商品名称' ⊃ '名称'），或 field 比键长
        #    取最短命中键，避免长键（如 'merchantNameRemark'）抢先命中
        cands = []
        for k, v in d.items():
            kl = str(k).lower()
            if not kl:
                continue
            if fl and (fl in kl or kl in fl):
                cands.append((len(kl), k, v))
        if cands:
            return min(cands, key=lambda x: x[0])[2]
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
        # 默认上下文示例文本：参数名复数→列表写法；单数→标量写法（对齐真实 schema）
        _mv = self.sys.merchant_default_value()
        if isinstance(_mv, list):
            _sample = f'{self.sys.merchant_param}: {json.dumps(_mv, ensure_ascii=False)}'
        elif _mv is None:
            _sample = ""
        else:
            _sample = f'{self.sys.merchant_param}: "{_mv}"'
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
- 操作需带默认上下文参数但用户未指明时，toolParams 用 {_sample}
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
            if not has:
                _val = self.sys.merchant_default_value()
                if _val is not None:
                    tool_params[self.sys.merchant_param] = _val
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

    # =================================================================
    # 写用例保护路径诊断（跑之前先看「哪些真跑、哪些跳过」）
    # =================================================================
    def diag_write_paths(self, cases, dim=None):
        """逐条判定写用例会走哪条数据保护路径。

        为什么需要：2026-11-01 跑「写入安全边界」15 条，日志报「失败 0」，
        实际 dimensions 里 skipped=15 / n=0 —— 一条都没执行，是个假通过。
        根因是「跳过」只体现在结果文件的 skipped 计数里，执行前完全不可见。
        本方法把判定提前到执行前，跑之前就能看见哪些会被跳过、哪些无保护。

        返回 {路径: 条数}，以及每条用例的明细列表。
        """
        lc = self.get_lifecycle()
        restorable, creatable, selfdel = set(), set(), set()
        irreversible = set()
        if lc is not None:
            restorable = set((lc.restorable or {}).keys())
            creatable = set((lc.creatable or {}).keys())
            irreversible = set(lc.irreversible or ())
            selfdel = set((lc.self_delete or {}).keys())

        # ⚠ 判定顺序必须与 handle() 的拦截顺序完全一致，否则「诊断说能跑、
        #   实际整批跳过」（2026-11-01 踩过：诊断报 13 条可跑，跑起来 skipped=15）。
        #   handle() 的顺序是：① 能力缺口 ② 不可恢复（含自建自删兜底）③ 快照还原。
        stat = {}
        rows = []
        for c in cases:
            d = c.get("维度") or ""
            if dim and dim not in d:
                continue
            tool = ((c.get("输入") or {}).get("tool_name")) or ""
            if not self._is_write_tool(tool):
                continue
            if lc is not None and lc.capability_gap(tool):
                path = "能力缺口(跳过)"
            elif tool in irreversible:
                # 不可恢复：配了自建自删才算有保护，否则跳过
                path = "自建自删" if tool in selfdel else "不可恢复(跳过)"
            elif tool in restorable:
                path = "可还原"
            elif tool in creatable:
                path = "新建后清理"
            elif tool in selfdel:
                path = "自建自删"
            else:
                path = "无保护(会真跑)"
            stat[path] = stat.get(path, 0) + 1
            rows.append({"用例ID": c.get("用例ID"), "工具": tool,
                         "维度": d, "路径": path})
        return stat, rows

    # =================================================================
    # 写接口限流探测（实测恢复窗口，用数据定退避参数而不是拍脑袋）
    # =================================================================
    async def probe_write_ratelimit(self, tool="item_update", gap=5.0,
                                    rounds=5, read_limit=5, probe_id=None):
        """实测写接口的限流窗口。

        背景（2026-11-01）：PocketStore 写接口返回
          {"code":1007,"msg":"... 429: Write operation rate limit exceeded"}
        默认退避（3 次 / 1s 基数 / 合计 7s）压不住。这里按 gap 间隔连写，
        看第几次开始 429、读额度是否独立，据此定 WRITE_RATE_LIMIT_BASE_MS。

        ⚠ 会用「可还原」的写工具真改数据（item_update 改商品名），
        仅限隔离测试环境；无还原规则的写工具直接拒绝探测。
        """
        lc = self.get_lifecycle()
        rule = lc.rule_for(tool) if lc else None
        if not rule:
            return {"ok": False,
                    "reason": f"{tool} 未在「可还原」声明，探测会留下无法回滚的改动"}
        snap_tool = rule.get("快照工具")
        rec_keys = rule.get("恢复参数键") or {}
        write_key = next(iter(rec_keys.values()), "newName")
        # 探测必须指定一个真实存在的实体，否则读不到原值、写也不生效
        # （2026-11-01 踩过：只传 merchantIds 不传 itemId，item_detail 拿不到数据，
        #  写操作实际是空跑，「还原」也无从谈起）
        pk_param = rule.get("主键参数") or "itemId"
        if not probe_id:
            return {"ok": False,
                    "reason": f"必须用 --probe-id 指定一个真实的 {pk_param}"
                              f"（否则读不到原值、写不生效）"}
        read_params = {pk_param: probe_id}
        _mv = self.sys.merchant_default_value()
        if _mv is not None:
            read_params[self.sys.merchant_param] = _mv

        _stat: Dict[str, Any] = {"read_429": 0, "write_ok": 0, "write_429": 0}
        _detail: List[Dict[str, Any]] = []
        out: Dict[str, Any] = {"ok": True, "tool": tool, "gap": gap,
                               "rounds": rounds}

        stack, session = await self._session_context()
        async with stack:
            # ① 读额度是否独立于写额度
            for _i in range(read_limit):
                _e, t, _l = await self._call_tool(session, snap_tool, read_params)
                if self._is_rate_limited(t):
                    _stat["read_429"] += 1
            # ② 按 gap 连写
            # ⚠ 先取当前值：探测会真改名，测完必须改回，否则残留下 ratelimit-probe-N。
            _orig = None
            try:
                _e0, _t0, _l0 = await self._call_tool(
                    session, snap_tool, read_params)
                _data = json.loads(_t0).get("data") or {}
                # 恢复参数键形如 {"name": "newName"}：key 是实体字段，value 是写参。
                # 从实体字段名回查原值（取不到再退回写参剥离 new 前缀）。
                _base = next((k for k in rec_keys if k in _data), None)
                if _base is None:
                    _base = str(write_key)[3:] if str(write_key).lower(
                    ).startswith("new") else str(write_key)
                _orig = _data.get(_base)
            except Exception:
                _orig = None
            out["orig_value"] = _orig

            for i in range(rounds):
                params = dict(read_params)
                params[write_key] = f"ratelimit-probe-{i}"
                # MCP 写工具默认 confirm=false 只预览、不真写；不显式置 true，
                # 探测会「一律成功」的假象（实测 5/5 全成功但数据没变过）。
                params["confirm"] = True
                _t0 = time.monotonic()
                _e, t, _l = await self._call_tool(session, tool, params)
                limited = self._is_rate_limited(t)
                if limited:
                    _stat["write_429"] += 1
                else:
                    _stat["write_ok"] += 1
                _detail.append({
                    "n": i + 1, "limited": limited,
                    "sec": round(time.monotonic() - _t0, 1),
                    "head": t[:100]})
                if i + 1 < rounds:
                    await asyncio.sleep(gap)

            # ③ 收尾还原：把名字改回探测前的原值
            if _orig:
                try:
                    _rp = dict(read_params)
                    _rp[write_key] = _orig
                    _rp["confirm"] = True
                    _er, _tr, _lr = await self._call_tool(session, tool, _rp)
                    out["restored"] = not _er and not self._is_rate_limited(_tr)
                except Exception as e:  # noqa: BLE001
                    out["restored"] = False
                    out["restore_error"] = str(e)[:120]
            else:
                out["restored"] = None
        out.update(_stat)
        out["detail"] = _detail
        return out

    async def _lookup_id(self, tool_name, name):
        try:
            stack, session = await self._session_context()
            async with stack:
                params = {}
                _mv = self.sys.merchant_default_value()
                if _mv is not None:
                    params[self.sys.merchant_param] = _mv
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
            is_error, text, _latency = await ex._call_tool(session, demo_tool, {})
            print(f"{demo_tool}: is_error={is_error}\n  {text[:800]}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        print(f"连接失败: {e}")


if __name__ == "__main__":
    import argparse as _ap
    import sys as _s

    _p = _ap.ArgumentParser(description="通用执行器：连通性自检 / 写用例保护诊断 / 写限流探测")
    _p.add_argument("system", help="系统名（如 PocketStore）")
    _p.add_argument("--write-paths", metavar="DATASET",
                    help="诊断该数据集的写用例会走哪条保护路径（跑之前先看跳过项）")
    _p.add_argument("--dim", default=None, help="配合 --write-paths 只看某维度")
    _p.add_argument("--probe-ratelimit", action="store_true",
                    help="实测写接口限流窗口（会真改一处可还原数据）")
    _p.add_argument("--tool", default="item_update", help="探测用的可还原写工具")
    _p.add_argument("--gap", type=float, default=5.0, help="两次写之间的间隔秒")
    _p.add_argument("--rounds", type=int, default=5, help="写探测次数")
    _p.add_argument("--probe-id", default=None,
                    help="探测用的真实实体ID（如 itemId），不传会拒绝探测")
    _a = _p.parse_args()

    if _a.write_paths:
        import yaml as _y
        _doc = _y.safe_load(open(_a.write_paths, encoding="utf-8")) or {}
        _ex = GenericMcpExecutor(_a.system)
        _stat, _rows = _ex.diag_write_paths(_doc.get("用例列表") or [], _a.dim)
        print(f"数据集: {os.path.basename(_a.write_paths)}")
        print("=" * 76)
        for _k, _v in sorted(_stat.items(), key=lambda x: -x[1]):
            print(f"  {_k:16} {_v:>4} 条")
        _risky = [r for r in _rows if r["路径"].startswith("无保护")]
        if _risky:
            print("\n⚠ 无保护、会真实改动数据（建议先补 configs 的「新建后清理」）:")
            for _r in _risky:
                print(f"  {_r['用例ID']}  {_r['工具']}")
            _s.exit(2)
        print("\n跳过项不会执行；其余均有自动收尾")
    elif _a.probe_ratelimit:
        _ex = GenericMcpExecutor(_a.system)
        _r = asyncio.run(_ex.probe_write_ratelimit(
            _a.tool, _a.gap, _a.rounds, probe_id=_a.probe_id))
        print(_r if not _r.get("ok") else "")
        if _r.get("ok"):
            print(f"写探测 {_r['tool']}：成功 {_r['write_ok']}/{_r['rounds']}，"
                  f"429 {_r['write_429']}，读 429 {_r['read_429']}")
            for _d in _r["detail"]:
                print(f"  #{_d['n']}: {'429' if _d['limited'] else '成功'} "
                      f"{_d['sec']}s")
            # 还原结果必须报出来：探测会真改数据，没报就等于没验证
            _rs = _r.get("restored")
            print(f"  还原: {'已还原' if _rs else ('未还原!' if _rs is False else '无原值可取，请人工核对')}")
            if _r.get("restore_error"):
                print(f"  还原失败原因: {_r['restore_error']}")
            if _rs is False:
                _s.exit(3)
    else:
        asyncio.run(_demo_connect(_a.system))
