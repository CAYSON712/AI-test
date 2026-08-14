# -*- coding: utf-8 -*-
"""
Rubric 评分体系（基于《AI 测试方法体系手册》）
================================================
- 5 分制评分（5=优秀 ... 1=严重缺陷）
- 每条用例配 Rubric JSON（判定标准）
- 支持阈值判断（如"意图识别 ≥98% 得 5 分"）
- 统计判定：跑 ≥5 次，计算通过率 + 置信区间
"""
import json
import math
import os
from collections import defaultdict

# 5 分制评分
SCORE_LABELS = {
    5: "优秀",
    4: "良好",
    3: "可接受",
    2: "一般缺陷",
    1: "严重缺陷",
}


def score_to_label(score):
    """5分制数字 → 文本标签"""
    return SCORE_LABELS.get(int(round(score)), "未知")


class Rubric:
    """一个维度的 Rubric 判定标准"""

    def __init__(self, dimension, rubric_map, threshold=None):
        self.dimension = dimension          # 维度名
        self.rubric_map = rubric_map         # {5:描述, 4:..., ...}
        self.threshold = threshold           # 阈值（如 ">=0.98"），用于自动断言

    @staticmethod
    def from_yaml(dim_node):
        """从维度表 YAML 节点构造 Rubric"""
        return Rubric(
            dimension=dim_node.get("维度", ""),
            rubric_map=dim_node.get("rubric", {}),
            threshold=dim_node.get("通过标准"),
        )

    def judge_by_label(self, matched_label):
        """根据命中哪个分数档的描述，返回分数（1~5）"""
        matched_label = str(matched_label)
        for score in (5, 4, 3, 2, 1):
            desc = self.rubric_map.get(str(score), "")
            if desc and matched_label in desc:
                return score
        return 3  # 未命中，默认可接受


class RubricJudger:
    """Rubric 评分器：对一条执行结果按 Rubric 打分"""

    def __init__(self, dimension_tables):
        """dimension_tables: {需求类型: {维度: Rubric}}"""
        self.tables = dimension_tables

    def score_case(self, req_type, case, result, judge_text=None):
        """对一条用例打分。

        Args:
            req_type: 需求类型 A/B/C/D/E
            case: 用例 dict
            result: 执行结果（含实际输出/校验）
            judge_text: LLM-as-Judge 返回的文本/判定（可选）

        Returns:
            {维度: {"score": 1~5, "label": "优秀", "detail": ...}}
        """
        scores = {}
        dims = self._get_dimensions(req_type)
        for dim, rubric in dims.items():
            s = self._judge_single(rubric, case, result, judge_text)
            scores[dim] = {
                "score": s,
                "label": score_to_label(s),
                "rubric": rubric.rubric_map.get(str(s), ""),
            }
        return scores

    def _get_dimensions(self, req_type):
        """取某个需求类型的维度 Rubric。

        C 类已在加载时展开为 A+B+集成（23维），此处直接返回。
        """
        return dict(self.tables.get(req_type, {}))

    def _judge_single(self, rubric, case, result, judge_text):
        """对单个维度打分：
        1. 若有 LLM 判定文本，尝试匹配 rubric 描述
        2. 否则用规则（verify/状态/关键词）
        """
        dim = rubric.dimension
        # 优先用 LLM-as-Judge 判定
        if judge_text:
            # LLM 返回形如 "5" 或包含分数
            for score in (5, 4, 3, 2, 1):
                if str(score) in judge_text:
                    return score
        # 规则打分
        return self._rule_judge(rubric, case, result)

    def _rule_judge(self, rubric, case, result):
        """规则打分（RAG 指标 / verify 校验 / block / 关键词）"""
        output = getattr(result, "output_data", None)
        # 操作后校验：verify.match 为 True → 5 分，False → 1 分
        if isinstance(output, dict) and output.get("verify"):
            match = output["verify"].get("match")
            if match is True:
                return 5
            if match is False:
                return 1
        # RAG 类专项评分（需求类型 E）：按维度名取对应指标
        if isinstance(output, dict) and output.get("rag_metrics"):
            score = self._rag_judge(rubric.dimension, output["rag_metrics"])
            if score is not None:
                return score
        # block 拦截类：正确处理 = 5，未拦截 = 1
        expected_block = case.get("期望", {}).get("block", False)
        if expected_block:
            if getattr(result, "status", "") == "error":
                return 5  # 正确拦截
            return 1
        # 默认给可接受分（具体靠 LLM-as-Judge 细化）
        return 3

    def _rag_judge(self, dimension, metrics):
        """RAG 维度专项打分（对齐 E 类维度表）"""
        # 检索召回率：期望文档被召回比例 → 5分制
        if dimension == "检索召回率":
            r = metrics.get("召回率", 0)
            if r >= 0.95: return 5
            if r >= 0.90: return 4
            if r >= 0.80: return 3
            if r >= 0.60: return 2
            return 1
        # 检索精准率
        if dimension == "检索精准率":
            p = metrics.get("精准率", 0)
            if p >= 0.95: return 5
            if p >= 0.90: return 4
            if p >= 0.80: return 3
            if p >= 0.60: return 2
            return 1
        # 幻觉率：越低越好
        if dimension == "幻觉率":
            h = metrics.get("幻觉率", 0)
            if h < 0.02: return 5
            if h < 0.05: return 4
            if h < 0.10: return 3
            if h < 0.20: return 2
            return 1
        # 知识时效性：骨架暂用召回近似（真实场景需更新后对比）
        if dimension == "知识时效性":
            r = metrics.get("召回率", 0)
            return 5 if r >= 0.95 else (3 if r >= 0.8 else 1)
        # 答案 Groundedness：答案忠于检索文档的比例
        if dimension == "答案 Groundedness":
            g = metrics.get("Groundedness", 0)
            if g >= 0.95: return 5
            if g >= 0.90: return 4
            if g >= 0.80: return 3
            if g >= 0.60: return 2
            return 1
        return None


def wilson_interval(p, n, z=1.96):
    """Wilson 置信区间（通过率统计）"""
    if n == 0:
        return 0.0, 0.0
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n) / denom
    return max(0.0, center - margin), min(1.0, center + margin)


def compute_pass_rate(scores, threshold=0.85):
    """统计通过率：得分≥3（可接受）视为通过。
    返回 (通过率, 采样数, 置信区间)
    """
    n = len(scores)
    if n == 0:
        return 0.0, 0, (0.0, 0.0)
    passed = sum(1 for s in scores if s >= 3)
    rate = passed / n
    ci = wilson_interval(rate, n)
    return rate, n, ci


def aggregate_case_runs(case_scores_list):
    """聚合一条用例跑多次的分数：
    case_scores_list: [{维度: score}, ...]  (多次运行)
    返回 {维度: {avg_score, pass_rate, n, ci}}
    """
    agg = defaultdict(list)
    for run in case_scores_list:
        for dim, score in run.items():
            agg[dim].append(score)
    result = {}
    for dim, scores in agg.items():
        avg = sum(scores) / len(scores)
        rate, n, ci = compute_pass_rate(scores)
        result[dim] = {"avg_score": avg, "pass_rate": rate, "n": n, "ci": ci}
    return result


def format_report(agg, dimension_labels=None):
    """格式化聚合报告"""
    lines = []
    lines.append(f"{'维度':<20} {'平均分':<8} {'通过率':<8} {'采样':<6} {'95%CI':<16}")
    lines.append("-" * 60)
    for dim, data in agg.items():
        ci = data["ci"]
        lines.append(f"{dim:<20} {data['avg_score']:<8.2f} "
                     f"{data['pass_rate']:<8.1%} {data['n']:<6} "
                     f"[{ci[0]:.2f}, {ci[1]:.2f}]")
    return "\n".join(lines)
