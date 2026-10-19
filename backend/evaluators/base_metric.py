from abc import ABC, abstractmethod
from typing import Any, Optional, Tuple
from dataclasses import dataclass
import time


@dataclass
class EvaluationResult:
    # score=None 表示「已跳过/未评」（缺输入或缺参数），不参与统计；float 为实际评分(0-1)。
    score: Optional[float]
    reason: str  # CoT explanation
    duration_ms: int


class BaseMetric(ABC):
    """Abstract base class for all evaluation metrics."""
    
    name: str = "base"
    
    @abstractmethod
    async def evaluate(
        self,
        question: str,
        answer: str,
        context: Optional[str] = None,
        ground_truth: Optional[str] = None,
        **kwargs: Any,
    ) -> EvaluationResult:
        """Evaluate the answer and return score with reasoning.

        基础签名只覆盖 question/answer/context/ground_truth 四个通用输入；
        DeepEval 适配器等需要更多过程数据(tool_calls/thoughts/turns 等)的子类，
        通过 **kwargs 接收，缺输入时返回 score=None 表示「已跳过」。

        Args:
            question: The input question
            answer: The model's answer to evaluate
            context: Optional context for the question
            ground_truth: Optional expected/reference answer
            
        Returns:
            EvaluationResult with score (0-1, 或 None 表示已跳过), reason (CoT), and duration
        """
        pass
    
    async def measure(
        self,
        question: str,
        answer: str,
        context: Optional[str] = None,
        ground_truth: Optional[str] = None,
        threshold: float = 0.8,
        **kwargs: Any,
    ) -> Tuple[EvaluationResult, bool]:
        """Measure and check against threshold.

        Returns:
            Tuple of (EvaluationResult, passed: bool)
            score=None(已跳过)时 passed=False,不参与达标统计。
        """
        result = await self.evaluate(question, answer, context, ground_truth, **kwargs)
        passed = result.score is not None and result.score >= threshold
        return result, passed
