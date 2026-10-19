"""
自定义Python脚本评估器

允许用户上传Python脚本作为"代码评分器"，替代LLM评估。
脚本必须定义 evaluate() 函数，签名为：

def evaluate(question: str, answer: str, context: str = None, ground_truth: str = None) -> dict:
    return {"score": float(0-1), "reason": str}
"""
import time
import importlib.util
import sys
import os
import tempfile
from typing import Optional
from .base_metric import BaseMetric, EvaluationResult
from logger import log


class CustomScriptMetric(BaseMetric):
    """使用用户上传的Python脚本进行评估的Metric"""

    name = "custom_script"

    def __init__(self, script_path: str = None, script_code: str = None, script_name: str = "unknown"):
        """
        Args:
            script_path: Python脚本文件路径
            script_code: Python脚本代码字符串（与script_path二选一）
            script_name: 脚本名称（用于日志）
        """
        self.script_path = script_path
        self.script_code = script_code
        self.script_name = script_name
        self._module = None

    def _load_script(self):
        """动态加载Python脚本模块"""
        if self._module is not None:
            return self._module

        try:
            if self.script_path and os.path.exists(self.script_path):
                # 从文件加载
                spec = importlib.util.spec_from_file_location(
                    f"custom_eval_{self.script_name}", self.script_path
                )
                self._module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(self._module)
            elif self.script_code:
                # 从代码字符串加载
                module_name = f"custom_eval_{self.script_name}_{id(self)}"
                self._module = type(sys)(module_name)
                exec(compile(self.script_code, f"<{self.script_name}>", "exec"), self._module.__dict__)
            else:
                raise ValueError("必须提供 script_path 或 script_code")

            # 验证脚本包含 evaluate 函数
            if not hasattr(self._module, 'evaluate'):
                raise ValueError(f"脚本 '{self.script_name}' 必须定义 evaluate() 函数")

            log("INFO", "CUSTOM_SCRIPT", f"成功加载评估脚本: {self.script_name}")
            return self._module

        except Exception as e:
            log("ERROR", "CUSTOM_SCRIPT", f"加载评估脚本失败: {self.script_name}", error=str(e))
            raise ValueError(f"加载脚本失败: {str(e)}")

    async def evaluate(
        self,
        question: str,
        answer: str,
        context: Optional[str] = None,
        ground_truth: Optional[str] = None
    ) -> EvaluationResult:
        """执行自定义脚本评估"""
        start_time = time.time()

        log("INFO", "CUSTOM_SCRIPT",
            f"开始执行自定义脚本评估: {self.script_name}",
            script_name=self.script_name,
            question_preview=question[:100] if question else "",
            answer_preview=answer[:100] if answer else "")

        try:
            module = self._load_script()

            # 调用脚本的 evaluate 函数
            result = module.evaluate(
                question=question,
                answer=answer,
                context=context,
                ground_truth=ground_truth
            )

            # 验证返回格式
            if not isinstance(result, dict):
                raise ValueError(f"脚本 evaluate() 必须返回字典，实际返回: {type(result)}")

            score = result.get("score")
            reason = result.get("reason", "无评估说明")

            if score is None:
                raise ValueError("脚本 evaluate() 返回的字典必须包含 'score' 字段")

            # 确保 score 在 [0, 1] 范围内
            try:
                score = float(score)
            except (TypeError, ValueError):
                raise ValueError(f"score 必须为数值，实际值: {score}")

            score = max(0.0, min(1.0, score))

            duration_ms = int((time.time() - start_time) * 1000)

            log("INFO", "CUSTOM_SCRIPT",
                f"自定义脚本评估完成: {self.script_name}, 得分: {score:.3f}",
                script_name=self.script_name,
                score=round(score, 3),
                duration_ms=duration_ms,
                reason_preview=reason[:100] if reason else "")

            return EvaluationResult(
                score=round(score, 3),
                reason=f"[Python脚本评估:{self.script_name}] (耗时{duration_ms}ms)\n{reason}",
                duration_ms=duration_ms
            )

        except Exception as e:
            duration_ms = int((time.time() - start_time) * 1000)
            error_msg = f"脚本执行失败: {str(e)}"
            log("ERROR", "CUSTOM_SCRIPT",
                f"自定义脚本评估失败: {self.script_name}",
                script_name=self.script_name,
                error=str(e),
                duration_ms=duration_ms)

            return EvaluationResult(
                score=0.0,
                reason=f"[脚本评估:{self.script_name}] {error_msg}",
                duration_ms=duration_ms
            )


def validate_script(script_code: str) -> dict:
    """
    验证脚本代码是否合法

    Returns:
        dict: {"valid": bool, "error": str or None}
    """
    try:
        # 尝试编译
        compile(script_code, "<validation>", "exec")

        # 尝试在隔离环境中执行
        test_module = type(sys)("test_module")
        exec(compile(script_code, "<validation>", "exec"), test_module.__dict__)

        # 检查 evaluate 函数
        if not hasattr(test_module, 'evaluate'):
            return {"valid": False, "error": "脚本必须定义 evaluate() 函数"}

        # 检查函数签名
        import inspect
        sig = inspect.signature(test_module.evaluate)
        params = list(sig.parameters.keys())
        required_params = ['question', 'answer']
        for p in required_params:
            if p not in params:
                return {"valid": False, "error": f"evaluate() 函数必须包含 '{p}' 参数"}

        # 尝试执行一次
        try:
            result = test_module.evaluate(
                question="测试问题",
                answer="测试答案",
                context="测试上下文",
                ground_truth="测试标准答案"
            )
            if not isinstance(result, dict) or 'score' not in result:
                return {"valid": False, "error": "evaluate() 必须返回包含 'score' 字段的字典"}
            float(result['score'])  # 确保 score 可以转为 float
        except Exception as e:
            return {"valid": False, "error": f"evaluate() 执行测试失败: {str(e)}"}

        return {"valid": True, "error": None}

    except SyntaxError as e:
        return {"valid": False, "error": f"语法错误: {str(e)}"}
    except Exception as e:
        return {"valid": False, "error": f"验证失败: {str(e)}"}
