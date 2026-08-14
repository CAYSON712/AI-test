# -*- coding: utf-8 -*-
"""真实场景测试：下架咸檸檬七喜
链路: search_products_by_name 搜商品 -> update_products_by_ids 下架(status=Off)
"""
import asyncio
import os
import sys
import time
import json
from contextlib import AsyncExitStack

from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

# 从 ai-test-framework/.env 读取 MCP 配置（避免硬编码 token 提交 git）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
load_dotenv(os.path.join(_PROJECT_ROOT, "ai-test-framework", ".env"))

URL = os.getenv("POS_MCP_URL", "https://pos-test-mcp.proton-system.com/mcp")
TOKEN = os.getenv("POS_MCP_TOKEN", "")
COMPANY_ID = os.getenv("POS_MCP_COMPANY_ID", "9088125566714885")
MERCHANT_ID = os.getenv("POS_MERCHANT_ID", "9088143804924933")


async def call(session, name, tool_params, label=""):
    t0 = time.time()
    try:
        result = await session.call_tool(name, {"toolParams": tool_params})
        dt = (time.time() - t0) * 1000
        text = ""
        for c in result.content:
            if c.type == "text":
                text = c.text
        print(f"[{label}{name}] 耗时{dt:.0f}ms is_error={result.is_error}")
        return text
    except Exception as e:
        print(f"[{label}{name}] EXCEPTION: {e}")
        return f"ERROR: {e}"


async def main():
    import httpx
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(httpx.AsyncClient(headers={
            "Authorization": f"Bearer {TOKEN}",
            "CompanyId": COMPANY_ID,
        }))
        read, write = await stack.enter_async_context(streamable_http_client(URL, http_client=http))
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("✅ 已连接 EasyPOS AIM MCP Server\n")

            # ① 搜索"咸檸檬七喜"
            print("== ① search_products_by_name 搜索咸檸檬七喜 ==")
            search = await call(session, "search_products_by_name",
                                {"merchantIds": [MERCHANT_ID], "productName": "咸檸檬七喜"})
            print(f"返回:\n{search}\n")

            # 解析商品 ID（假设返回结构里有 id）
            product_id = None
            try:
                data = json.loads(search)
                # data 可能是 {code, msg, data: [...]} 或直接数组
                items = data.get("data", []) if isinstance(data, dict) else data
                if isinstance(items, dict):
                    # 可能按名称分组
                    for k, v in items.items():
                        if isinstance(v, list) and v:
                            product_id = v[0].get("id")
                            break
                elif items:
                    product_id = items[0].get("id")
                print(f"识别到商品 ID: {product_id}\n")

                # ② 下架（status=Off）
                if product_id:
                    print(f"== ② update_products_by_ids 下架商品 {product_id} ==")
                    off = await call(session, "update_products_by_ids",
                                     {"merchantIds": [MERCHANT_ID], "productIds": [product_id], "status": "Off"})
                    print(f"返回:\n{off}\n")
            except Exception as e:
                print(f"解析失败: {e}（返回不是标准 JSON，需人工看）\n")


if __name__ == "__main__":
    asyncio.run(main())
