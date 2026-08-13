# -*- coding: utf-8 -*-
"""
通用打分模块：支持 0.0~1.0 连续分（颗粒度 0.1）
================================================
核心思想：打分不再是"0/0.5/1 三档"，而是按比例/加权/布尔输出连续分。

提供 4 种打分器：
  - RatioScorer    按"正确项/总项"比例打分（参数抽取、检索召回等）
  - WeightedScorer 按多个子项加权求和打分（综合质量）
  - BooleanScorer  是/否打分（危险拦截、越权防护）
  - LLMScorer      LLM 裁判给 0~1 分（忠实度、相关性等主观质量）

所有打分器统一返回 round(x, 1)，保证颗粒度 0.1。
"""


def _norm(value: float) -> float:
    """归一化到 0.0~1.0，并四舍五入到 0.1 颗粒度"""
    value = max(0.0, min(1.0, float(value)))
    return round(value, 1)


class RatioScorer:
    """比例打分：correct/total -> 0.0~1.0
    适用：参数抽取（抽对几个）、检索召回（返回几个相关的）
    """
    def score(self, correct: int, total: int) -> float:
        if total <= 0:
            return 0.0
        return _norm(correct / total)


class WeightedScorer:
    """加权打分：多个子维度按权重加权求和
    适用：综合质量（准确性0.5 + 完整性0.3 + 一致性0.2）
    """
    def __init__(self, weights: dict):
        # weights = {"子维度名": 权重}, 权重和应=1
        self.weights = weights

    def score(self, sub_scores: dict) -> float:
        total = 0.0
        weight_sum = 0.0
        for name, weight in self.weights.items():
            if name in sub_scores:
                total += weight * float(sub_scores[name])
                weight_sum += weight
        if weight_sum <= 0:
            return 0.0
        return _norm(total / weight_sum)


class BooleanScorer:
    """布尔打分：true -> 1.0, false -> 0.0
    适用：危险拦截、越权防护、注入防护
    """
    def score(self, passed: bool) -> float:
        return 1.0 if passed else 0.0


class LLMScorer:
    """LLM 裁判打分：让 LLM 输出 0~1 的连续分
    适用：忠实度、相关性、回复质量等需要主观判断的维度
    """
    def __init__(self, llm_func=None):
        # llm_func: 接受 (question, answer, dimension) 返回 0~1 float
        self.llm_func = llm_func

    def score(self, question: str, answer: str, dimension: str) -> float:
        if self.llm_func is None:
            raise ValueError("LLMScorer 需要传入 llm_func（返回 0~1 的评估函数）")
        raw = self.llm_func(question, answer, dimension)
        return _norm(float(raw))


# ---------- 便捷工厂 ----------

def make_scorer(method: str, **kwargs):
    """根据方法名创建打分器
    method: ratio / weighted / boolean / llm
    """
    if method == "ratio":
        return RatioScorer()
    if method == "weighted":
        return WeightedScorer(kwargs.get("weights", {}))
    if method == "boolean":
        return BooleanScorer()
    if method == "llm":
        return LLMScorer(kwargs.get("llm_func"))
    raise ValueError(f"未知打分方式: {method}")


if __name__ == "__main__":
    # 自测示例
    print("Ratio 参数抽取(3/4):", make_scorer("ratio").score(3, 4), "-> 0.8")
    print("Ratio 检索(7/10):", make_scorer("ratio").score(7, 10), "-> 0.7")
    print("Weighted 综合:", make_scorer("weighted", weights={"准确":0.5,"完整":0.3,"一致":0.2})
          .score({"准确":0.9,"完整":0.8,"一致":0.7}))
    print("Boolean 拦截:", make_scorer("boolean").score(True), "-> 1.0")
    print("Boolean 未拦截:", make_scorer("boolean").score(False), "-> 0.0")
