# -*- coding: utf-8 -*-
"""DeepEval Metrics 子包——从 agent_eval 移植的声明式指标注册表与执行器。

本子包为 onto-eval 的可选评测能力扩展，依赖 deepeval 包（pip install deepeval）。
未安装 deepeval 时，本子包的 LLM 指标不可用，但 code_metric（沙箱代码指标）仍可独立工作。
"""
