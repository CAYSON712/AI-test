# -*- coding: utf-8 -*-
"""
执行器基类（Executor）
=====================
执行器的作用：把数据集里的一条用例，翻译成"对某个被测系统的真实调用"，并返回结果。

设计原则：
  - 测试用例针对「能力」（capability），不针对具体系统
  - 换系统 = 注册新的 executor，流程/数据集/打分都不动
  - 每种能力对应一个执行方法

接口：
  execute(case) -> ExecResult
    case: 数据集里的一条用例（dict）
    ExecResult: 执行结果，含 input/output/延迟/是否成功
"""
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List


@dataclass
class ExecResult:
    """一次用例执行的结果"""
    case_id: str                      # 用例 ID
    capability: str                   # 能力名
    dimension: str                    # 评测维度
    priority: str                     # P级
    status: str = "success"           # success / fail / error
    latency_ms: float = 0.0           # 真实延迟
    input_data: Any = None            # 发送给被测系统的输入
    output_data: Any = None           # 被测系统返回
    error: str = None                 # 错误信息
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)


class BaseExecutor:
    """执行器基类。子类实现 handle(capability, params)"""

    # 子类声明自己能处理的能力清单
    capabilities: List[str] = []

    def can_handle(self, capability: str) -> bool:
        return capability in self.capabilities

    def execute(self, case: dict) -> ExecResult:
        """执行一条用例（入口）"""
        cap = case.get("能力", "")
        if not self.can_handle(cap):
            return ExecResult(
                case_id=case["用例ID"], capability=cap,
                dimension=case.get("维度", ""), priority=case.get("优先级", ""),
                status="error", error=f"能力「{cap}」不被该执行器支持",
            )
        # 取输入
        inp = case.get("输入", {})
        user_input = inp.get("user_input", "") if isinstance(inp, dict) else inp

        t0 = time.time()
        try:
            result = self.handle(cap, user_input, inp, case.get("期望", {}))
            latency = (time.time() - t0) * 1000
            # 状态判定（对齐 Langfuse）：
            #   level=ERROR（真异常）→ status=error
            #   level=WARNING（业务拒绝）→ status=success，但带 biz_reject 标记
            #   其他 → success
            status = "success"
            error = None
            if isinstance(result, dict):
                _level = result.get("level")
                if _level == "ERROR":
                    status, error = "error", result.get("error") or result.get("biz_error")
                elif result.get("error"):
                    status, error = "error", result.get("error")
            return ExecResult(
                case_id=case["用例ID"], capability=cap,
                dimension=case.get("维度", ""), priority=case.get("优先级", ""),
                status=status, latency_ms=latency,
                input_data=user_input, output_data=result, error=error,
            )
        except Exception as e:
            latency = (time.time() - t0) * 1000
            return ExecResult(
                case_id=case["用例ID"], capability=cap,
                dimension=case.get("维度", ""), priority=case.get("优先级", ""),
                status="error", latency_ms=latency,
                input_data=user_input, error=str(e),
            )

    def handle(self, capability: str, user_input: str, inp: dict, expected: dict):
        """子类实现：根据能力执行真实调用，返回结果"""
        raise NotImplementedError
