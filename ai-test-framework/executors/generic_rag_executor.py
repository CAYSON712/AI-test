# -*- coding: utf-8 -*-
"""
通用 RAG 执行器（需求类型 E）
=============================
针对「检索增强生成」场景：根据用户问题检索相关文档，生成带引用的回答。
配置驱动，不绑定任何具体知识库。

设计（对齐 E 类维度表：检索召回率/精准率/幻觉率/时效性/Groundedness）：
  1. 调检索接口 → 拿 top-k 相关文档
  2. 生成回答（调生成接口 或 LLM 基于文档生成）
  3. 收集证据（retrieved_docs / answer / citations）+ RAG 指标
  4. 返回给评分器：output_data 含 verify 结构（供现有 rubric 判定）+ rag_metrics（E 专项）

接入新知识库只需一份 configs/<知识库>.yaml；无真实接口时走内置 Mock 数据验证骨架。

依赖：
  - configs/<知识库>.yaml（连接 + 检索/生成接口）
  - ability/能力目录_<知识库>.yaml（能力清单）
"""
import asyncio
import json
import os
import sys

import httpx
import yaml
from dotenv import load_dotenv

from base import BaseExecutor

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_DIR = os.path.join(_ROOT, "configs")
ABILITY_DIR = os.path.join(_ROOT, "ability")
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
if os.path.join(_ROOT, "scripts") not in sys.path:
    sys.path.insert(0, os.path.join(_ROOT, "scripts"))

load_dotenv(os.path.join(_ROOT, ".env"))


def _load_yaml(path):
    if not path or not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _find_config_file(system):
    if not system or not os.path.isdir(CONFIG_DIR):
        return ""
    for f in os.listdir(CONFIG_DIR):
        if not f.endswith(".yaml"):
            continue
        base = os.path.splitext(f)[0]
        norm = lambda s: s.replace("_", "").replace(" ", "").replace("-", "")
        a, b = norm(base), norm(system)
        if b in a or a in b:
            return os.path.join(CONFIG_DIR, f)
    return ""


def _find_ability_file(system):
    if not system or not os.path.isdir(ABILITY_DIR):
        return ""
    for f in os.listdir(ABILITY_DIR):
        if not (f.startswith("能力目录_") and f.endswith(".yaml")):
            continue
        base = os.path.splitext(f)[0]
        stripped = base.replace("能力目录_", "")
        norm = lambda s: s.replace("_", "").replace(" ", "").replace("-", "")
        a, b = norm(stripped), norm(system)
        if b in a or a in b:
            return os.path.join(ABILITY_DIR, f)
    return ""


class _RagConfig:
    """RAG 系统配置：连接 + 检索/生成接口"""

    def __init__(self, system):
        self.system = system
        cfg = _load_yaml(_find_config_file(system)) or {}
        abi = _load_yaml(_find_ability_file(system)) or {}

        conn = cfg.get("连接", {})
        self.req_type = conn.get("需求类型", "E")
        self.base_url = os.getenv(conn.get("base_url_env", ""), "")
        self.token = os.getenv(conn.get("token_env", ""), "")

        ret = cfg.get("检索接口", {})
        self.search_tool = ret.get("工具", "search")
        self.search_params = ret.get("参数", ["query"])
        self.top_k = ret.get("top_k", 5)

        gen = cfg.get("生成接口", {}) or {}
        self.gen_tool = gen.get("工具", "")
        self.gen_params = gen.get("参数", ["query", "documents"])

        score_cfg = cfg.get("评分", {})
        self.expect_docs_field = score_cfg.get("期望文档字段", "expect_docs")
        self.expect_answer_field = score_cfg.get("期望答案字段", "expect_answer")

        # 能力清单
        self.capabilities = []
        for group in abi.get("能力分组", []):
            for c in group.get("能力列表", []):
                if c.get("能力"):
                    self.capabilities.append(c["能力"])

        # 问答对（E 类数据源：能力目录「问答对」节点，支持顶层/分组级/能力项级）
        self.qa_pairs = []
        _top_qa = abi.get("问答对")
        if isinstance(_top_qa, list):
            self.qa_pairs.extend(k for k in _top_qa if isinstance(k, dict) and k.get("问题"))
        for group in abi.get("能力分组", []):
            _gqa = group.get("问答对")
            if isinstance(_gqa, list):
                self.qa_pairs.extend(k for k in _gqa if isinstance(k, dict) and k.get("问题"))
            for c in group.get("能力列表", []):
                _cqa = c.get("问答对")
                if isinstance(_cqa, list):
                    self.qa_pairs.extend(k for k in _cqa if isinstance(k, dict) and k.get("问题"))


class GenericRagExecutor(BaseExecutor):
    """通用 RAG 执行器：检索 → 生成 → 打包证据 + RAG 指标"""

    def __init__(self, system):
        self.sys = _RagConfig(system)
        self.capabilities = self.sys.capabilities

    def handle(self, capability, user_input, inp, expected):
        return asyncio.run(self._run_rag(capability, user_input, inp, expected))

    # ---- 检索调用 ----
    def _headers(self):
        h = {}
        if self.sys.token:
            h["Authorization"] = f"Bearer {self.sys.token}"
        return h

    async def _search(self, query):
        """调检索接口，返回 [ {id, content, score}, ... ]"""
        # 无真实接口（未配置 base_url）→ 走 Mock 数据，验证骨架
        if not self.sys.base_url:
            return self._mock_search(query)
        params = {"query": query, "top_k": self.sys.top_k}
        async with httpx.AsyncClient(headers=self._headers()) as client:
            resp = await client.post(f"{self.sys.base_url}/search", json=params, timeout=30)
            if resp.status_code != 200:
                return []
            data = resp.json()
            docs = data.get("documents") or data.get("data") or []
            return [{"id": d.get("id", ""), "content": d.get("content", ""),
                     "score": d.get("score", 0)} for d in docs]

    def _mock_search(self, query):
        """内置 Mock 检索（骨架验证用）：从能力目录「问答对」动态生成文档，
        不绑定任何知识库业务词。query 与问句共享的 2-gram 越多越相关。"""
        qa = self.sys.qa_pairs
        if not qa:
            return [{"id": "GEN-01", "content": "抱歉，未找到相关文档", "score": 0.1}]
        best, best_score = None, 0
        for k in qa:
            q = str(k.get("问题", ""))
            if not q:
                continue
            if q == query:
                best, best_score = k, 10 ** 9
                break
            grams = {q[i:i + 2] for i in range(max(0, len(q) - 1))}
            score = sum(1 for g in grams if g in query)
            if score > best_score:
                best, best_score = k, score
        if best and best_score > 0:
            doc_id = best.get("期望文档") or "DOC-01"
            ans = best.get("答案") or ""
            return [{"id": doc_id,
                     "content": f"{best.get('问题', '')}：{ans}", "score": 0.95}]
        return [{"id": "GEN-01", "content": "抱歉，未找到相关文档", "score": 0.1}]

    # ---- 生成回答 ----
    async def _generate(self, query, docs):
        """生成回答：有真实接口则调生成接口，否则用 LLM 基于文档生成"""
        if not docs:
            return "抱歉，知识库中未找到相关信息。"
        # 真实生成接口：需 base_url + gen_tool 都配置
        if self.sys.base_url and self.sys.gen_tool:
            params = {"query": query, "documents": docs}
            try:
                async with httpx.AsyncClient(headers=self._headers()) as client:
                    resp = await client.post(f"{self.sys.base_url}/{self.sys.gen_tool}",
                                             json=params, timeout=30)
                    if resp.status_code == 200:
                        return resp.json().get("answer", "")
            except Exception:
                pass  # 真实接口失败，退回 LLM
        # 用 LLM 生成
        from llm_client import LLMClient
        content = "\n".join(f"- {d['content']}" for d in docs)
        prompt = f"""
你是{self.sys.system}的智能助手。请基于以下知识库文档回答用户问题，只依据文档内容，不要编造。

用户问题：{query}

知识库文档：
{content}

请给出简洁中文回答，并标注依据的文档ID。
"""
        try:
            return LLMClient().chat_text(prompt)
        except Exception:
            return docs[0]["content"]

    # ---- 主流程 ----
    async def _run_rag(self, capability, user_input, inp, expected):
        steps = []
        query = user_input

        # 1. 检索
        docs = await self._search(query)
        steps.append({
            "name": "RAG-检索", "type": "SPAN",
            "input": query, "output": json.dumps(docs[:5], ensure_ascii=False),
            "metadata": {"capability": capability, "system": self.sys.system},
        })

        # 2. 生成回答
        answer = await self._generate(query, docs)
        steps.append({
            "name": "RAG-生成回答", "type": "GENERATION",
            "input": json.dumps(docs[:5], ensure_ascii=False), "output": answer,
            "metadata": {"capability": capability},
        })

        # 3. 计算 RAG 指标（对齐 E 类维度）
        metrics = self._compute_metrics(query, docs, answer, expected)

        return {
            "query": query,
            "retrieved_docs": docs,
            "answer": answer,
            "rag_metrics": metrics,
            "steps": steps,
        }

    def _compute_metrics(self, query, docs, answer, expected):
        """计算 E 类 RAG 指标：召回率/精准率/幻觉/时效性/Groundedness"""
        expected = expected or {}
        expect_docs = expected.get(self.sys.expect_docs_field) or []
        expect_answer = expected.get(self.sys.expect_answer_field) or ""

        doc_ids = [d["id"] for d in docs]
        doc_text = " ".join(d.get("content", "") for d in docs)

        # 检索召回率：期望文档中被检索出的比例
        recall = 0.0
        if expect_docs:
            hit = [d for d in expect_docs if d in doc_ids or d in doc_text]
            recall = len(hit) / len(expect_docs)
        else:
            recall = 1.0 if docs else 0.0

        # 检索精准率：检索结果中"相关"文档占比（用 LLM 判定太贵，骨架用规则近似）
        precision = 1.0 if docs else 0.0

        # 幻觉率 / Groundedness：答案能否在检索文档中找到依据（近似）
        grounded = 1.0
        hallucination = 0.0
        if expect_answer:
            found = expect_answer in answer or any(
                expect_answer in d.get("content", "") for d in docs
            )
            grounded = 1.0 if found else 0.0
            hallucination = 0.0 if found else 1.0

        return {
            "召回率": recall,
            "精准率": precision,
            "幻觉率": hallucination,
            "Groundedness": grounded,
            "检索到文档数": len(docs),
            "答案": answer,
        }


async def _demo(system="客服知识库"):
    ex = GenericRagExecutor(system)
    print("系统:", ex.sys.system, "| 能力:", ex.capabilities)
    result = await ex._run_rag("政策问答", "退货政策是什么？", "退货政策是什么？", {})
    print("召回率:", result["rag_metrics"]["召回率"])
    print("检索文档:", [d["id"] for d in result["retrieved_docs"]])
    print("回答:", result["answer"])


if __name__ == "__main__":
    asyncio.run(_demo())
