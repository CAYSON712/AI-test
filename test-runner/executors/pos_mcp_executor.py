# -*- coding: utf-8 -*-
"""
POS 真实 MCP 执行器（LLM 驱动）
===============================
把数据集里的自然语言用例，通过 LLM 解析成真实的 MCP 工具调用，并返回结果。

流程（端到端）：
  用户自然语言（用例 user_input）
    → LLM 解析意图/选工具/抽参数（基于真实 MCP 工具 schema）
    → 输出 JSON {"tool": "...", "toolParams": {...}}
    → 调用真实 MCP（https://pos-test-mcp.proton-system.com/mcp）
    → 返回结果给打分器

依赖：
  - 环境变量 POS_MCP_TOKEN（鉴权）
  - LLM 配置（dataset-generator/.env，复用 LLMClient）
  - 能力目录 dataset-generator/ability/能力目录_POS商品管理.yaml

用法：
  python pos_mcp_executor.py            # 手动验证 MCP 连接
  由 run_dataset.py 通过 registry 分发调用（正式执行流程）
"""
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack

import httpx
import yaml
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from base import BaseExecutor

# 项目根 / 能力目录（生成端维护，单一数据源）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_ROOT = os.path.dirname(_ROOT)
ABILITY_DIR = os.path.join(_PROJECT_ROOT, "dataset-generator", "ability")
# 生成端 scripts（复用 llm_client）
_GEN_SCRIPTS = os.path.join(_PROJECT_ROOT, "dataset-generator", "scripts")
if _GEN_SCRIPTS not in sys.path:
    sys.path.insert(0, _GEN_SCRIPTS)

# 加载 dataset-generator/.env（MCP token 等敏感配置统一存这里，勿硬编码提交 git）
_ENV_PATH = os.path.join(_PROJECT_ROOT, "dataset-generator", ".env")
load_dotenv(_ENV_PATH)

# ---- MCP 连接配置（从 .env / 环境变量读取）----
URL = os.getenv("POS_MCP_URL", "https://pos-test-mcp.proton-system.com/mcp")
TOKEN = os.getenv("POS_MCP_TOKEN", "")
COMPANY_ID = os.getenv("POS_MCP_COMPANY_ID", "9088125566714885")


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


# =====================================================================
# 真实 MCP 工具定义（给 LLM 做工具选择 + 参数抽取）
# =====================================================================
MCP_TOOLS = [
    {
        "name": "query_menus",
        "description": "查询菜单列表，支持按店铺、状态、销售渠道、菜单名称筛选。",
        "params": ["merchantIds", "status", "saleChannel", "name"],
        "required": [],
    },
    {
        "name": "query_categories",
        "description": "查询分类列表，支持按店铺、分类名称筛选。",
        "params": ["merchantIds", "name"],
        "required": [],
    },
    {
        "name": "search_products_by_name",
        "description": "按商品名称模糊搜索商品，支持按上下架状态筛选，返回商品ID、名称、价格。通常作为操作的前置查询。",
        "params": ["merchantIds", "productName", "status"],
        "required": ["merchantIds", "productName"],
    },
    {
        "name": "query_products_by_ids",
        "description": "根据商品ID列表批量查询商品信息，需先通过 search_products_by_name 获取商品ID。",
        "params": ["merchantIds", "productIds"],
        "required": ["merchantIds", "productIds"],
    },
    {
        "name": "query_products_by_filter",
        "description": "按分类ID、商品类型、商品状态等条件组合查询商品列表。",
        "params": ["merchantIds", "categoryIds", "status"],
        "required": ["merchantIds"],
    },
    {
        "name": "query_product_detail_by_id",
        "description": "根据商品ID查询商品详细信息，包括基本信息、分类、规格组、菜单等关联数据。",
        "params": ["merchantIds", "productIds"],
        "required": ["merchantIds", "productIds"],
    },
    {
        "name": "update_products_by_ids",
        "description": "按商品ID列表批量修改商品属性，支持修改库存状态、上下架状态、价格和名称。status: Selling=上架, Off=下架。",
        "params": ["merchantIds", "productIds", "status", "price", "name", "nameEn"],
        "required": ["merchantIds", "productIds"],
    },
    {
        "name": "batch_insert_products",
        "description": "在指定店铺批量新增商品，每个商品含价格、多语言名称，可选分类和规格组。单次最多200个。",
        "params": ["merchantId", "products"],
        "required": ["merchantId", "products"],
    },
    {
        "name": "delete_products_by_ids",
        "description": "按商品ID列表批量删除商品。删除不可恢复，请谨慎。通常配合查询工具确认后再删。",
        "params": ["merchantIds", "productIds"],
        "required": ["merchantIds", "productIds"],
    },
    {
        "name": "query_companies",
        "description": "查询当前账号授权公司列表，前置步骤。",
        "params": ["companyIds", "name"],
        "required": [],
    },
    {
        "name": "query_merchants",
        "description": "查询授权店铺列表，大多数业务操作的前置步骤，获取 MerchantId。",
        "params": [],
        "required": [],
    },
]

# 能力 → 建议工具（兜底映射，LLM 解析失败时用）
CAP_TOOL_HINT = {
    "新增商品": "batch_insert_products",
    "批量新增商品": "batch_insert_products",
    "商品上架": "update_products_by_ids",
    "商品下架": "update_products_by_ids",
    "批量上下架": "update_products_by_ids",
    "修改价格": "update_products_by_ids",
    "批量改价": "update_products_by_ids",
    "修改商品名称": "update_products_by_ids",
    "修改商品英文名称": "update_products_by_ids",
    "批量修改名称": "update_products_by_ids",
    "删除商品": "delete_products_by_ids",
    "批量删除商品": "delete_products_by_ids",
    "查询商品": "search_products_by_name",
    "按ID查询商品": "query_products_by_ids",
    "按条件筛选商品": "query_products_by_filter",
    "查询商品详情": "query_product_detail_by_id",
    "查询菜单": "query_menus",
    "查询分类": "query_categories",
    "查询公司": "query_companies",
    "查询店铺": "query_merchants",
    "删除不存在商品": "delete_products_by_ids",
    "越权操作": "query_merchants",
    "指令注入": "query_products_by_filter",
    "组合操作": "query_products_by_ids",
    "查询": "query_products_by_filter",
}

# 测试店铺（默认用 Test01，避免误操作生产数据）
DEFAULT_MERCHANT_ID = os.getenv("POS_MERCHANT_ID", "9088143804924933")


async def _call_tool(session, name, tool_params):
    """调用 MCP 工具，返回 (is_error, 文本)"""
    result = await session.call_tool(name, {"toolParams": tool_params})
    texts = []
    for c in result.content:
        if c.type == "text":
            texts.append(c.text)
    return result.is_error, "\n".join(texts)


def _build_tools_prompt():
    """把工具定义转成给 LLM 的文本"""
    lines = ["可用工具列表（JSON 格式）："]
    for t in MCP_TOOLS:
        required = "必填:" + ",".join(t["required"]) if t["required"] else ""
        lines.append(
            f"- {t['name']}: {t['description']} | 参数: {t['params']} | {required}"
        )
    return "\n".join(lines)


def _extract_product_name(user_input):
    """从用户自然语言里粗提取商品名（去掉常见动词/数量词/价格）。
    返回商品名片段；提取失败返回空串。真实 ID 查找依赖它，若提取不准则需人工校准。
    """
    if not user_input:
        return ""
    # 去掉常见引导词，取引号内的或剩余的实体词
    import re
    quoted = re.findall(r"['\"「『]([^'\"」』]+)['\"」』]", user_input)
    if quoted:
        return quoted[0].strip()
    # 去掉动词/修饰词，粗略取最后一个有意义的片段
    stop_words = ["把", "将", "给", "对", "帮", "我", "的", "改成", "改为", "变成",
                  "下架", "上架", "删除", "删掉", "移除", "价格", "改成", "调整到",
                  "新增", "添加", "元", "块", "修改", "改名", "英文名", "查询", "多少",
                  "所有", "全部", "重新", "店内", "菜单", "里", "的"]
    # 简单策略：去掉停止词后剩下的连续英文/中文串作为候选（取最后一个较长片段）
    for token in stop_words:
        user_input = user_input.replace(token, " ")
    parts = [p.strip() for p in re.split(r"[\s,，。]+", user_input) if p.strip()]
    # 取看起来像商品名的（长度>=2，含字母或中文字符）
    candidates = [p for p in parts if len(p) >= 2]
    # 过滤纯数字/价格
    candidates = [p for p in candidates if not re.fullmatch(r"\d+(\.\d+)?", p)]
    return candidates[-1] if candidates else ""


def _parse_llm_call(text):
    """解析 LLM 输出的工具调用 JSON，容错处理"""
    # 提取最外层 JSON 对象
    try:
        start = text.index("{")
        end = text.rindex("}") + 1
        candidate = text[start:end]
        return json.loads(candidate)
    except Exception:
        # 尝试常见字段
        import re
        tool = re.search(r'"tool"\s*:\s*"([^"]+)"', text)
        params = re.search(r'"toolParams"\s*:\s*(\{.*?\})', text, re.DOTALL)
        if tool:
            params_obj = {}
            if params:
                try:
                    params_obj = json.loads(params.group(1))
                except Exception:
                    params_obj = {}
            return {"tool": tool.group(1), "toolParams": params_obj}
        raise ValueError(f"无法解析 LLM 工具调用: {text[:300]}")


def _load_verify_map(ability_file):
    """读取能力目录里的操作后校验配置：能力 → {verify_tool, verify_field, verify_expect}"""
    path = os.path.join(ABILITY_DIR, ability_file)
    vmap = {}
    if not os.path.exists(path):
        return vmap
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    for group in data.get("能力分组", []):
        for c in group.get("能力列表", []):
            if c.get("verify_tool"):
                vmap[c["能力"]] = {
                    "verify_tool": c["verify_tool"],
                    "verify_field": c.get("verify_field", "exists"),
                    "verify_expect": c.get("verify_expect", True),
                }
    return vmap


class PosMcpExecutor(BaseExecutor):
    """真实 POS MCP 执行器：LLM 解析自然语言 → 调真实 MCP → 操作后实时校验"""

    capabilities = list(dict.fromkeys(_load_capabilities("能力目录_POS商品管理.yaml")))
    _verify_map = _load_verify_map("能力目录_POS商品管理.yaml")

    def __init__(self):
        from llm_client import LLMClient  # 复用生成端的 LLM 封装
        self.llm = LLMClient()

    def handle(self, capability, user_input, inp, expected):
        return asyncio.run(self._call_capability(capability, user_input, expected))

    async def _call_capability(self, capability, user_input, expected):
        """执行一条用例，返回结构化结果 + 每步子步骤明细（供构建多层 trace）"""
        steps = []  # 收集 trace 子步骤：[{name,type,input,output,metadata}]

        # 1. LLM 意图理解
        intent_raw = self._llm_parse(user_input, capability, raw=True)
        intent_call = intent_raw.get("parsed", {})
        tool_name = intent_call.get("tool") or CAP_TOOL_HINT.get(capability)
        tool_params = intent_call.get("toolParams") or {}

        # 工具纠正：能力有明确提示工具时，强制用提示工具（避免 LLM 选成查询类）
        hint_tool = CAP_TOOL_HINT.get(capability)
        if hint_tool and hint_tool != tool_name:
            tool_name = hint_tool
            # 保留 LLM 抽出的参数，补上能力提示参数
            tool_params.setdefault("productIds", tool_params.get("productIds", []))
        steps.append({
            "name": "LLM-意图理解", "type": "GENERATION",
            "input": user_input,
            "output": json.dumps(intent_call, ensure_ascii=False),
            "metadata": {"scenario": capability},
        })
        if not tool_name:
            return {
                "error": f"LLM 未能解析出工具，能力: {capability}",
                "block": True, "steps": steps,
            }

        # 2. 参数抽取（补默认店铺）
        tool_params = self._ensure_merchant(tool_name, tool_params, capability)

        # 2.1 ID 解析：操作类工具缺 productIds 时，先按商品名查出真实 ID
        id_resolved = await self._resolve_product_ids(tool_name, tool_params, user_input, steps)

        steps.append({
            "name": "Skill-参数翻译", "type": "SPAN",
            "input": json.dumps(intent_call, ensure_ascii=False),
            "output": json.dumps({"工具": tool_name, "参数": tool_params}, ensure_ascii=False),
            "metadata": {"skill": "商品管理", "capability": capability},
        })

        # 3. 调用真实 MCP
        mcp_input = json.dumps({"toolParams": tool_params}, ensure_ascii=False)
        try:
            async with AsyncExitStack() as stack:
                http = await stack.enter_async_context(
                    httpx.AsyncClient(headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "CompanyId": COMPANY_ID,
                    })
                )
                read, write = await stack.enter_async_context(
                    streamable_http_client(URL, http_client=http)
                )
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    is_error, text = await _call_tool(session, tool_name, tool_params)
        except Exception as e:
            steps.append({
                "name": f"MCP-{tool_name}", "type": "SPAN",
                "input": mcp_input,
                "output": f"EXCEPTION: {e}",
                "metadata": {"mcp": "POS-mcp", "tool": tool_name},
            })
            return {"error": f"MCP 调用失败: {e}", "block": True, "steps": steps}

        steps.append({
            "name": f"MCP-{tool_name}", "type": "SPAN",
            "input": mcp_input,
            "output": text,
            "metadata": {"mcp": "POS-mcp", "tool": tool_name},
        })
        if is_error:
            return {"error": text, "block": True, "steps": steps}

        # 4. 操作后实时校验（E2E 层）：用 MCP 实时拉取商品状态，和操作目标对比
        verify = None
        if tool_name in ("update_products_by_ids", "delete_products_by_ids", "batch_insert_products"):
            verify = await self._verify_after(capability, tool_name, tool_params)
            if verify:
                steps.append({
                    "name": f"校验-MCP实时", "type": "SPAN",
                    "input": json.dumps({
                        "verify_tool": verify.get("verify_tool"),
                        "field": verify.get("field"),
                        "expect": verify.get("expect"),
                    }, ensure_ascii=False),
                    "output": json.dumps({
                        "actual": verify.get("actual"),
                        "match": verify.get("match"),
                    }, ensure_ascii=False),
                    "metadata": {"verify": True, "capability": capability},
                })

        # 5. LLM 生成回答
        reply = self._gen_reply(capability, user_input, tool_name, text)
        steps.append({
            "name": "LLM-生成回答", "type": "GENERATION",
            "input": json.dumps({"tool": tool_name, "mcp_result": text[:500]}, ensure_ascii=False),
            "output": reply,
            "metadata": {"capability": capability},
        })

        return {
            "tool": tool_name,
            "result": text,
            "params": tool_params,
            "reply": reply,
            "verify": verify,
            "steps": steps,
        }

    async def _verify_after(self, capability, tool_name, tool_params):
        """操作后实时校验：用 MCP 查询工具拉取商品实时状态，与预期对比。

        校验字段（来自能力目录 verify_field）：
          - price:   校验商品价格 = 操作目标价
          - status:  校验上下架状态 = 目标状态
          - name/nameEn: 校验名称 = 目标名称
          - exists:  校验存在性（新增=应存在；删除=应不存在）
        """
        cfg = self._verify_map.get(capability)
        if not cfg:
            return None
        vtool = cfg["verify_tool"]
        vfield = cfg["verify_field"]
        vexpect = cfg.get("verify_expect", True)

        # 构造校验查询参数
        query_params = {"merchantIds": [DEFAULT_MERCHANT_ID]}
        if vfield != "exists":
            product_ids = self._extract_product_ids(tool_params)
            if not product_ids:
                return {"verify_tool": vtool, "field": vfield, "expect": vexpect,
                        "actual": None, "match": None, "note": "无 productIds 可校验"}
            query_params["productIds"] = product_ids

        # 实时拉取
        try:
            async with AsyncExitStack() as stack:
                http = await stack.enter_async_context(
                    httpx.AsyncClient(headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "CompanyId": COMPANY_ID,
                    })
                )
                read, write = await stack.enter_async_context(
                    streamable_http_client(URL, http_client=http)
                )
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    _, text = await _call_tool(session, vtool, query_params)
        except Exception as e:
            return {"verify_tool": vtool, "field": vfield, "expect": vexpect,
                    "actual": None, "match": None, "error": str(e)}

        actual, match = self._compare_verify(vfield, vexpect, tool_params, text)
        return {"verify_tool": vtool, "field": vfield, "expect": vexpect,
                "actual": actual, "match": match}

    @staticmethod
    def _extract_product_ids(tool_params):
        """从工具参数里抽取 productIds"""
        ids = tool_params.get("productIds") or tool_params.get("product_ids") or []
        if isinstance(ids, str):
            ids = [ids]
        return list(ids) if ids else []

    def _compare_verify(self, field, expect, op_params, verify_text):
        """把 MCP 实时返回和操作目标对比，返回 (actual, match)"""
        try:
            data = json.loads(verify_text)
            if data.get("code") != 0 or not data.get("data"):
                return str(data.get("msg", "查询失败")), False
        except Exception:
            return verify_text[:200], None

        items = data["data"]
        # 结果结构可能是 {名称: [商品列表]} 或 [商品列表]
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
            # exists 校验：删除后查不到 = 成功
            if field == "exists":
                return "not_found", (vexpect is False)
            return "not_found", False

        first = flat[0]
        if field == "exists":
            return "found", (vexpect is True)

        # price / status / name 对比
        if field == "price":
            expect_price = op_params.get("price")
            actual_price = first.get("price")
            match = (actual_price is not None and str(actual_price) == str(expect_price))
            return str(actual_price), match
        if field == "status":
            expect_status = op_params.get("status")
            actual_status = first.get("status")
            match = (actual_status is not None and str(actual_status).lower() == str(expect_status).lower())
            return str(actual_status), match
        if field in ("name", "nameEn"):
            expect_name = op_params.get("name") or op_params.get("nameEn")
            actual_name = first.get("name") or first.get("nameEn")
            match = (actual_name is not None and str(actual_name).lower() == str(expect_name).lower())
            return str(actual_name), match

        return str(first), None

    def _llm_parse(self, user_input, capability, raw=False):
        """让 LLM 解析自然语言 → 工具调用 JSON"""
        hint_tool = CAP_TOOL_HINT.get(capability, "")
        hint_line = f"必须使用工具：{hint_tool}" if hint_tool else ""
        prompt = f"""
你是餐厅菜单管理助手的意图解析引擎。请把用户的自然语言请求，解析为对真实 MCP 工具的一次调用。

{_build_tools_prompt()}

当前任务类型（能力）：{capability}
{hint_line}
测试店铺 MerchantId：{DEFAULT_MERCHANT_ID}

请解析以下用户请求，输出 JSON（不要其他文字）：
{{
  "tool": "工具名",
  "toolParams": {{ 工具参数 }}
}}

用户请求：{user_input}

注意：
- 严格优先使用上面"必须使用工具"指定的工具（若有），不要用查询类工具替代操作类工具
- 参数名必须用列表里定义的字段名
- 涉及某店铺操作但用户未指明店铺时，toolParams 用 merchantIds: ["{DEFAULT_MERCHANT_ID}"]
- 下架用 status: "Off"，上架用 status: "Selling"
- 若是删除操作，务必先想清楚是否合理，删除不可恢复
"""
        try:
            text = self.llm.chat_text(prompt)
            parsed = _parse_llm_call(text)
        except Exception as e:
            parsed = {"tool": CAP_TOOL_HINT.get(capability), "toolParams": {}}
        if raw:
            return {"parsed": parsed}
        return parsed

    def _gen_reply(self, capability, user_input, tool_name, mcp_result):
        """用 LLM 生成面向用户的自然语言回复（基于 MCP 真实返回）"""
        prompt = f"""
你是餐厅菜单管理助手的回复生成器。基于真实 MCP 工具返回结果，生成一句面向用户的中文回复。

工具：{tool_name}
能力：{capability}
用户请求：{user_input}
MCP 返回结果：
{mcp_result[:800]}

请生成简洁的中文回复（一句话），如实反映操作结果。不要编造不存在的成功/失败信息。
"""
        try:
            return self.llm.chat_text(prompt)
        except Exception:
            # 退回简单描述
            return f"已处理请求（工具:{tool_name}），结果见 MCP 返回。"

    @staticmethod
    def _ensure_merchant(tool_name, tool_params, capability):
        """确保涉及店铺的工具带上 merchantIds，避免漏传"""
        merchant_needed = tool_name in (
            "update_products_by_ids", "delete_products_by_ids",
            "query_products_by_ids", "query_products_by_filter",
            "query_product_detail_by_id", "query_menus",
            "query_categories", "search_products_by_name",
        )
        if merchant_needed:
            has_merchant = any(
                k in tool_params for k in ("merchantIds", "merchantId")
            )
            if not has_merchant:
                tool_params["merchantIds"] = [DEFAULT_MERCHANT_ID]
        return tool_params

    async def _resolve_product_ids(self, tool_name, tool_params, user_input, steps):
        """操作类工具缺 productIds 时，先按商品名查出真实 ID 填入 tool_params。

        修改/删除等操作需要 productIds，而 LLM 解析时只知道商品名、不知道真实 ID，
        所以需先调 search_products_by_name 查出商品 ID。
        """
        need_ids = tool_name in ("update_products_by_ids", "delete_products_by_ids",
                                 "query_products_by_ids", "query_product_detail_by_id")
        if not need_ids:
            return False
        has_ids = tool_params.get("productIds") or tool_params.get("product_ids")
        if has_ids:
            return False

        # 从用户输入提取商品名（去掉常见动词/数量词）
        name = _extract_product_name(user_input)
        if not name:
            return False

        resolved = await self._lookup_product_id(name)
        if resolved:
            tool_params["productIds"] = [resolved]
            steps.append({
                "name": "ID解析-查询商品ID", "type": "SPAN",
                "input": f"商品名: {name}",
                "output": f"productId: {resolved}",
                "metadata": {"step": "id_resolve"},
            })
            return True
        return False

    async def _lookup_product_id(self, name):
        """调 search_products_by_name 查出商品真实 ID"""
        try:
            async with AsyncExitStack() as stack:
                http = await stack.enter_async_context(
                    httpx.AsyncClient(headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "CompanyId": COMPANY_ID,
                    })
                )
                read, write = await stack.enter_async_context(
                    streamable_http_client(URL, http_client=http)
                )
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    is_error, text = await _call_tool(
                        session, "search_products_by_name",
                        {"merchantIds": [DEFAULT_MERCHANT_ID], "productName": name},
                    )
                    if is_error:
                        return None
                    data = json.loads(text)
                    if data.get("code") != 0 or not data.get("data"):
                        return None
                    # data 结构可能是 {名称: [{id,...}]} 或 [{id,...}]
                    for k, v in data["data"].items():
                        if isinstance(v, list) and v:
                            return v[0].get("id")
                        if isinstance(v, dict) and v.get("id"):
                            return v.get("id")
                    return None
        except Exception:
            return None


async def _demo_connect():
    """手动验证：连接真实 MCP 并查询公司/店铺"""
    if not TOKEN:
        print("⚠️ 未配置 POS_MCP_TOKEN，无法连接真实 MCP。")
        return
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(
            httpx.AsyncClient(headers={
                "Authorization": f"Bearer {TOKEN}",
                "CompanyId": COMPANY_ID,
            })
        )
        read, write = await stack.enter_async_context(
            streamable_http_client(URL, http_client=http)
        )
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("✅ 已连接\n")
            print("== query_merchants（查店铺）==")
            is_error, text = await _call_tool(session, "query_merchants", {})
            print(f"  is_error={is_error}\n  {text[:1200]}")


if __name__ == "__main__":
    asyncio.run(_demo_connect())
