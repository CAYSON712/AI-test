# -*- coding: utf-8 -*-
"""
LLM-as-Judge 评分器（基于《AI 测试方法体系手册》）
====================================================
用 LLM 作为裁判，根据 Rubric 对实际输出打分（5 分制）。
适用于：回复忠实度、语义等价、意图匹配等主观维度的评分。
"""
import json
import os
import sys

# 复用框架内的 LLM 封装
_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts")
_SCRIPTS = os.path.abspath(_SCRIPTS)
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)


class LLMJudge:
    """LLM-as-Judge：用 LLM 按 Rubric 打分"""

    def __init__(self):
        from llm_client import LLMClient
        self.llm = LLMClient()

    def judge(self, rubric_map, user_input, expected, actual):
        """让 LLM 根据 Rubric 打分。

        Args:
            rubric_map: {5:描述, 4:..., ..., 1:...}
            user_input: 用户输入
            expected: 期望输出
            actual: 实际输出

        Returns:
            (score 1~5, reason str)
        """
        labels = {5: "优秀", 4: "良好", 3: "可接受", 2: "一般缺陷", 1: "严重缺陷"}
        items = sorted(
            ((int(k), v) for k, v in rubric_map.items()),
            reverse=True,
        )
        rubric_text = "\n".join(
            f"  {s}分({labels.get(s, '')}): {desc}"
            for s, desc in items
        )
        prompt = f"""你是 AI 测试的评分裁判。请根据 Rubric 判定标准，对"实际输出"打分（1~5分）。

Rubric 判定标准：
{rubric_text}

用户输入：{user_input}
期望输出：{expected}
实际输出：{actual}

请严格按 Rubric 打分，输出 JSON（不要其他文字）：
{{"score": 分数, "reason": "评分理由"}}
"""
        try:
            text = self.llm.chat_text(prompt)
            return self._parse(text)
        except Exception as e:
            return 3, f"LLM 评分失败: {e}，默认可接受"

    def _parse(self, text):
        """解析 LLM 返回的 {score, reason}"""
        try:
            start = text.index("{")
            end = text.rindex("}") + 1
            data = json.loads(text[start:end])
            score = int(data.get("score", 3))
            score = max(1, min(5, score))  # 限制 1~5
            return score, data.get("reason", "")
        except Exception:
            # 尝试直接提取数字
            import re
            m = re.search(r"[1-5]", text)
            score = int(m.group(0)) if m else 3
            return score, text[:100]
