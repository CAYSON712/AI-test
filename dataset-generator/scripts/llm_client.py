# -*- coding: utf-8 -*-
"""
LLM 客户端封装
==============
统一封装公司内部大模型调用，供 维度推荐 / 打分裁判 / 数据集生成 使用。

特性：
  - 读取 dataset-generator/.env 配置
  - 支持绕过本地代理（trust_env=False）
  - 异常处理 + 重试
  - 简洁接口：chat() 返回文本，chat_json() 返回结构化 JSON

用法：
  from llm_client import LLMClient
  client = LLMClient()
  text = client.chat("请分析...")          # 返回文本
  data = client.chat_json("输出JSON...")    # 返回 dict
"""
import json
import os
import time

import requests
from dotenv import load_dotenv

# 加载 dataset-generator/.env
_ENV_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(_ENV_PATH)


class LLMClient:
    def __init__(self, api_base=None, api_key=None, model=None, no_proxy=None):
        self.api_base = api_base or os.getenv("LLM_API_BASE", "http://zsgw.sjdistributor.com:40000")
        self.api_key = api_key or os.getenv("LLM_API_KEY", "")
        self.model = model or os.getenv("LLM_MODEL", "metis-coder")
        # 默认 true，绕过本地代理（本机 10809 会劫持）
        self.no_proxy = (no_proxy if no_proxy is not None
                         else os.getenv("LLM_NO_PROXY", "true").lower() == "true")
        self._session = requests.Session()
        if self.no_proxy:
            self._session.trust_env = False  # 关键：绕过系统代理

    def chat(self, messages, temperature=0.2, max_tokens=2048, retries=2):
        """发送对话，返回文本内容"""
        url = f"{self.api_base}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        for attempt in range(retries + 1):
            try:
                resp = self._session.post(url, headers=headers, json=payload, timeout=60)
                if resp.status_code == 200:
                    return resp.json()["choices"][0]["message"]["content"]
                else:
                    err = resp.text[:300]
                    raise RuntimeError(f"LLM API 返回 {resp.status_code}: {err}")
            except Exception as e:
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
                    continue
                raise RuntimeError(f"LLM 调用失败（重试{retries}次后）: {e}")

    def chat_text(self, prompt, system=None):
        """便捷接口：传 prompt 和可选 system，返回文本"""
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        return self.chat(messages)

    def chat_json(self, prompt, system=None):
        """让 LLM 输出 JSON，解析返回 dict/list。
        增强容错：处理中文引号、尾逗号、代码块、前后缀文本、截断。
        全部修复后仍失败则抛异常。
        """
        sys_msg = system or "你是一个严谨的AI测试分析助手。只输出JSON，不要输出其他文字。"
        text = self.chat_text(prompt, sys_msg)
        return self._parse_json(text)

    @staticmethod
    def _extract_json(text: str) -> str:
        """从文本中提取最外层 JSON 片段（去掉前后缀、代码块、markdown）"""
        text = text.strip()
        # 去掉 markdown 代码块
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            text = "\n".join(lines).strip()
        # 找到第一个 { 或 [，和最后一个 } 或 ]
        start = -1
        for i, ch in enumerate(text):
            if ch in "{[":  # 找到最外层起始
                start = i
                break
        if start == -1:
            return text
        end = -1
        for i in range(len(text) - 1, -1, -1):
            if text[i] in "}]":
                end = i + 1
                break
        if end == -1:
            end = len(text)
        return text[start:end]

    @classmethod
    def _repair_json(cls, text: str) -> str:
        """修复常见 JSON 格式问题"""
        # 1. 中文引号 → 英文引号
        text = text.replace("“", '"').replace("”", '"')
        text = text.replace("‘", "'").replace("’", "'")
        # 2. 中文冒号/逗号 → 英文（仅在非字符串内？为简单全局替换，中文场景通常安全）
        #    注意：中文文本里的全角标点也可能需要保留，但 JSON 键值分隔必须用半角
        text = text.replace("：", ":").replace("，", ",")
        # 3. 去除尾逗号（数组/对象最后一项后的逗号）
        import re
        text = re.sub(r",(\s*[}\]])", r"\1", text)
        # 4. 单引号键 → 双引号（启发式，谨慎）
        #    匹配单引号包裹的键
        text = re.sub(r"'([^']+)'(\s*:)", r'"\1"\2', text)
        # 5. 未加引号的键（如 维度: ）→ 加双引号（支持中英文键）
        text = re.sub(r'([{,])\s*([A-Za-z_\u4e00-\u9fa5][A-Za-z0-9_\u4e00-\u9fa5]*)(\s*:)',
                      r'\1"\2"\3', text)
        # 6. 未加引号的字符串值（中文裸文本）→ 加引号
        #    匹配 : 之后、, 或 } 之前的中文/字母裸文本（排除 true/false/null/数字）
        text = re.sub(r':\s*([A-Za-z\u4e00-\u9fa5][A-Za-z0-9_\u4e00-\u9fa5]*)'
                      r'(?=\s*[,}])',
                      lambda m: ':"{}"'.format(m.group(1))
                      if m.group(1).lower() not in ("true", "false", "null")
                      else ':' + m.group(1), text)
        return text

    def _parse_json(self, text: str):
        """尝试多种方式解析 JSON，失败则抛异常"""
        # 尝试直接解析
        try:
            return json.loads(text)
        except Exception:
            pass
        # 提取最外层 JSON
        extracted = self._extract_json(text)
        # 修复后解析
        repairs = [
            extracted,
            self._repair_json(extracted),
        ]
        last_err = None
        for candidate in repairs:
            try:
                return json.loads(candidate)
            except Exception as e:
                last_err = e
                continue
        # 尝试按行截断修复（截断场景：补全括号）
        try:
            return self._parse_truncated(extracted)
        except Exception as e:
            last_err = e
        raise ValueError(f"JSON 解析失败: {last_err}. 原始片段: {extracted[:200]}")

    @staticmethod
    def _parse_truncated(text: str):
        """处理 JSON 被截断的情况（补全闭合括号）"""
        import json
        # 从后往前尝试补全
        for ch in ("}", "]"):
            candidate = text.rstrip() + ch
            try:
                return json.loads(candidate)
            except Exception:
                continue
        raise ValueError("截断修复失败")


if __name__ == "__main__":
    # 自测
    client = LLMClient()
    print("== chat 测试 ==")
    print(client.chat_text("请回复：连接成功"))
    print("\n== chat_json 测试 ==")
    try:
        data = client.chat_json('{"推荐维度": ["准确性", "意图识别"]}')
        print(data)
    except Exception as e:
        print("json 解析失败:", e)
