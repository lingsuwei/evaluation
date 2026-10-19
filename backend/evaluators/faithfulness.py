import asyncio
import time
import math
import re
from collections import Counter
from typing import Optional, List, Dict
from .base_metric import BaseMetric, EvaluationResult
from .llm_client import evaluate_metric_with_llm
from .document_loader import get_document_loader
from config import _db_llm_models_cache, get_llm_config, get_model_pool
from logger import log


def _is_model_available(model_key: str) -> bool:
    """检查模型是否可用（包括硬编码配置和数据库缓存）"""
    if not model_key:
        return False
    # auto 模式：只要模型池中有可用模型即可
    if model_key == "auto":
        return bool(get_model_pool())
    return model_key in _db_llm_models_cache


class BM25Calculator:
    """BM25算法计算器"""
    
    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1  # 词频饱和参数
        self.b = b  # 长度归一化参数
        self.doc_freqs: Dict[str, int] = {}  # 文档频率
        self.doc_lengths: List[int] = []  # 文档长度列表
        self.avg_doc_length: float = 0.0  # 平均文档长度
        self.num_docs: int = 0  # 文档总数
        self.idf: Dict[str, float] = {}  # IDF值
    
    def tokenize(self, text: str) -> List[str]:
        """中文分词(简单按空格和标点分割)"""
        # 移除标点符号
        text = re.sub(r'[^\w\s]', ' ', text)
        # 按空格分割
        tokens = text.split()
        # 过滤空字符串和长度为1的词
        return [t.lower() for t in tokens if len(t) > 1]
    
    def add_document(self, doc: str):
        """添加文档到索引"""
        tokens = self.tokenize(doc)
        self.doc_lengths.append(len(tokens))
        self.num_docs += 1
        
        # 统计文档频率
        unique_tokens = set(tokens)
        for token in unique_tokens:
            self.doc_freqs[token] = self.doc_freqs.get(token, 0) + 1
    
    def build_index(self):
        """构建索引，计算IDF和平均文档长度"""
        if self.num_docs == 0:
            return
        
        self.avg_doc_length = sum(self.doc_lengths) / self.num_docs
        
        # 计算IDF
        for token, freq in self.doc_freqs.items():
            self.idf[token] = math.log((self.num_docs - freq + 0.5) / (freq + 0.5) + 1.0)
    
    def score(self, query: str, doc: str) -> float:
        """计算BM25分数"""
        if self.num_docs == 0:
            return 0.0
        
        query_tokens = self.tokenize(query)
        doc_tokens = self.tokenize(doc)
        doc_length = len(doc_tokens)
        
        # 统计查询词在文档中的词频
        doc_token_counts = Counter(doc_tokens)
        
        score = 0.0
        for token in query_tokens:
            if token not in doc_token_counts:
                continue
            
            # 计算词频
            tf = doc_token_counts[token]
            
            # BM25公式
            idf = self.idf.get(token, 0.0)
            numerator = tf * (self.k1 + 1)
            denominator = tf + self.k1 * (1 - self.b + self.b * (doc_length / self.avg_doc_length))
            score += idf * (numerator / denominator)
        
        return score
    
    def normalize_score(self, score: float, max_score: float) -> float:
        """归一化分数到[0,1]区间"""
        if max_score == 0:
            return 0.0
        return min(1.0, score / max_score)


class FaithfulnessMetric(BaseMetric):
    """Evaluates if answer is faithful to provided context."""

    name = "faithfulness"

    def __init__(self, model_key: str = None, use_bm25: bool = False, use_doc_library: bool = False, ontology_package: str = None):
        self.model_key = model_key  # 不使用默认值，保持None表示离线评估
        self.use_bm25 = use_bm25  # 是否使用BM25算法
        self.use_doc_library = use_doc_library  # 是否使用文档库作为参考
        self.ontology_package = ontology_package  # 本体包名称（使用指定目录的md文档）
    
    async def evaluate_with_bm25(
        self,
        question: str,
        answer: str,
        context: Optional[str] = None,
        ground_truth: Optional[str] = None
    ) -> EvaluationResult:
        """使用BM25算法评估忠实度"""
        start_time = time.time()

        # 如果启用文档库模式，使用文档库作为参考
        if self.use_doc_library:
            return await self._evaluate_with_bm25_library(question, answer)

        # 使用context作为参考文档
        reference_doc = context if context else (ground_truth or "")
        if not reference_doc:
            reason = "未提供参考内容或标准答案，也未配置本体包/知识库，采用中性降级评分（0.5分）。忠诚度评估在无参考依据时无法准确判定，建议配置知识库或数据集 context 字段以获得可靠评估。"
            return EvaluationResult(score=0.5, reason=reason, duration_ms=0)

        # 构建BM25索引(将参考文档作为文档集合)
        bm25 = BM25Calculator(k1=1.5, b=0.75)

        # 将参考文档按句子分割，每个句子作为一个文档
        sentences = re.split(r'[。！？\n]', reference_doc)
        sentences = [s.strip() for s in sentences if s.strip()]

        if not sentences:
            reason = "参考内容为空或无法分割句子"
            return EvaluationResult(score=0.0, reason=reason, duration_ms=0)

        # 添加文档到索引
        for sent in sentences:
            bm25.add_document(sent)

        bm25.build_index()

        # 计算答案与参考内容的BM25分数
        answer_score = bm25.score(answer, reference_doc)

        # 计算最大可能分数(用参考内容自身查询)
        max_score = bm25.score(reference_doc, reference_doc)

        # 归一化到[0,1]
        normalized_score = bm25.normalize_score(answer_score, max_score)

        duration_ms = int((time.time() - start_time) * 1000)

        # 生成评估原因
        reason = self._generate_bm25_reason(normalized_score, answer, reference_doc)

        log("INFO", "EVALUATOR",
            f"完成评估忠实度指标 - 使用BM25算法, 得分: {normalized_score:.3f}",
            metric=self.name,
            method="BM25",
            score=round(normalized_score, 3),
            duration_ms=duration_ms,
            reason_preview=reason[:100] if reason else "")

        return EvaluationResult(score=round(normalized_score, 3), reason=reason, duration_ms=duration_ms)
    
    def _generate_bm25_reason(self, score: float, answer: str, reference: str) -> str:
        """生成BM25评估原因"""
        if score >= 0.8:
            return f"回答高度忠实于参考内容，BM25得分为{score:.3f}。回答中的关键信息与参考内容高度匹配，没有明显的虚构或未提及信息。"
        elif score >= 0.6:
            return f"回答部分忠实于参考内容，BM25得分为{score:.3f}。回答包含了一些与参考内容匹配的信息，但也存在部分未提及或与参考内容不完全一致的内容。"
        elif score >= 0.4:
            return f"回答与参考内容有一定关联，BM25得分为{score:.3f}。回答中部分内容与参考内容相关，但有较多信息未在参考内容中找到依据。"
        elif score >= 0.2:
            return f"回答与参考内容关系薄弱，BM25得分为{score:.3f}。回答中大部分核心信息未在参考内容中提及，存在较多虚构内容。"
        else:
            return f"回答与参考内容几乎无关，BM25得分为{score:.3f}。回答中的信息基本未在参考内容中找到依据，建议检查回答的准确性。"

    async def _evaluate_with_bm25_library(
        self,
        question: str,
        answer: str
    ) -> EvaluationResult:
        """使用文档库和BM25算法评估忠实度"""
        start_time = time.time()

        # 获取文档加载器
        if self.ontology_package:
            import os as _os
            base_dir = _os.path.join(
                _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))),
                "md", self.ontology_package
            )
            doc_loader = get_document_loader(docs_dir=base_dir)
        else:
            doc_loader = get_document_loader()

        # 获取所有文档
        documents = doc_loader.get_all_documents()

        if not documents:
            reason = "本体包/知识库文档为空，采用中性降级评分（0.5分）。忠诚度评估在无参考依据时无法准确判定，建议配置本体包或上传知识库文档以获得可靠评估。"
            return EvaluationResult(score=0.5, reason=reason, duration_ms=0)

        # 构建BM25索引
        bm25 = BM25Calculator(k1=1.5, b=0.75)

        # 将每个文档按句子分割后添加到索引
        for doc_content in documents:
            sentences = re.split(r'[。！？\n]', doc_content)
            sentences = [s.strip() for s in sentences if len(s.strip()) > 10]  # 过滤过短的句子
            for sent in sentences:
                bm25.add_document(sent)

        bm25.build_index()

        # 对答案中的每个句子，计算其在文档库中的BM25得分
        answer_sentences = re.split(r'[。！？\n]', answer)
        answer_sentences = [s.strip() for s in answer_sentences if len(s.strip()) > 5]

        if not answer_sentences:
            reason = "答案为空或无法分割句子"
            return EvaluationResult(score=0.0, reason=reason, duration_ms=0)

        # 计算每个答案句子的BM25分数
        sentence_scores = []
        for ans_sent in answer_sentences:
            # 使用句子作为查询，在文档库中检索
            # 这里我们计算句子与文档库的平均相似度
            sentence_score = 0.0
            match_count = 0

            for ans_token in bm25.tokenize(ans_sent):
                # 对每个token，检查它在文档库中的IDF值
                idf = bm25.idf.get(ans_token, 0.0)
                if idf > 0:
                    sentence_score += idf
                    match_count += 1

            # 归一化句子分数（除以句子长度）
            normalized_sent_score = sentence_score / len(bm25.tokenize(ans_sent)) if bm25.tokenize(ans_sent) else 0
            sentence_scores.append(normalized_sent_score)

        # 计算所有答案句子的平均分数作为最终分数
        avg_score = sum(sentence_scores) / len(sentence_scores) if sentence_scores else 0.0

        # 归一化到[0,1]区间（使用经验值进行缩放）
        # 典型的IDF值范围是0-3，除以3得到归一化分数
        normalized_score = min(1.0, avg_score / 2.0)

        duration_ms = int((time.time() - start_time) * 1000)

        # 生成评估原因
        reason = self._generate_bm25_library_reason(normalized_score, question, answer)

        log("INFO", "EVALUATOR",
            f"完成评估忠实度指标 - 使用BM25文档库, 得分: {normalized_score:.3f}, 文档数: {len(documents)}",
            metric=self.name,
            method="BM25-Library",
            score=round(normalized_score, 3),
            doc_count=len(documents),
            duration_ms=duration_ms,
            reason_preview=reason[:100] if reason else "")

        return EvaluationResult(score=round(normalized_score, 3), reason=reason, duration_ms=duration_ms)

    def _generate_bm25_library_reason(self, score: float, question: str, answer: str) -> str:
        """生成BM25文档库评估原因"""
        if score >= 0.8:
            return f"回答高度忠实于文档库知识，BM25文档库得分为{score:.3f}。回答中的关键信息在文档库中有充分依据，无明显的虚构内容。"
        elif score >= 0.6:
            return f"回答部分忠实于文档库知识，BM25文档库得分为{score:.3f}。回答包含了一些在文档库中有依据的信息，但也存在部分未提及或不一致的内容。"
        elif score >= 0.4:
            return f"回答与文档库知识有一定关联，BM25文档库得分为{score:.3f}。回答中部分内容可在文档库中找到依据，但有较多信息未在文档库中记录。"
        elif score >= 0.2:
            return f"回答与文档库知识关系薄弱，BM25文档库得分为{score:.3f}。回答中大部分核心信息未在文档库中找到依据。"
        else:
            return f"回答与文档库知识几乎无关，BM25文档库得分为{score:.3f}。回答中的信息基本未在文档库中找到依据，建议检查回答的准确性。"
    
    async def evaluate(
        self,
        question: str,
        answer: str,
        context: Optional[str] = None,
        ground_truth: Optional[str] = None
    ) -> EvaluationResult:
        """主评估方法"""

        # 如果指定使用BM25，则使用BM25算法
        if self.use_bm25:
            return await self.evaluate_with_bm25(question, answer, context, ground_truth)

        model_name = (get_llm_config(self.model_key).get("model") or self.model_key or "unknown").strip()

        log("INFO", "EVALUATOR",
            f"开始评估忠实度指标 - 使用模型: {model_name}",
            metric=self.name,
            model_name=model_name,
            question_preview=question[:100] if question else "",
            answer_preview=answer[:100] if answer else "")

        start_time = time.time()

        # 总是使用LLM评估，不回退到离线评估
        if not _is_model_available(self.model_key):
            raise ValueError(f"未配置有效的LLM模型，无法执行评估：{self.model_key}")

        score, reason = await evaluate_metric_with_llm(
            "faithfulness", question, answer, context, ground_truth, self.model_key
        )

        duration_ms = int((time.time() - start_time) * 1000)

        log("INFO", "EVALUATOR",
            f"完成评估忠实度指标 - 使用模型: {model_name}, 得分: {score:.3f}",
            metric=self.name,
            model_name=model_name,
            score=round(score, 3),
            duration_ms=duration_ms,
            reason_preview=reason[:100] if reason else "")

        return EvaluationResult(score=round(score, 3), reason=reason, duration_ms=duration_ms)


class AnswerRelevancyMetric(BaseMetric):
    """Evaluates if answer is relevant to question."""
    
    name = "answer_relevancy"
    
    def __init__(self, model_key: str = None):
        self.model_key = model_key  # 不使用默认值，保持None表示离线评估
    
    async def evaluate(
        self,
        question: str,
        answer: str,
        context: Optional[str] = None,
        ground_truth: Optional[str] = None
    ) -> EvaluationResult:
        model_name = (get_llm_config(self.model_key).get("model") or self.model_key or "unknown").strip()

        log("INFO", "EVALUATOR",
            f"开始评估回答相关性指标 - 使用模型: {model_name}",
            metric=self.name,
            model_name=model_name,
            question_preview=question[:100] if question else "",
            answer_preview=answer[:100] if answer else "")

        start_time = time.time()

        # 总是使用LLM评估，不回退到离线评估
        if not _is_model_available(self.model_key):
            raise ValueError(f"未配置有效的LLM模型，无法执行评估：{self.model_key}")

        score, reason = await evaluate_metric_with_llm(
            "answer_relevancy", question, answer, context, ground_truth, self.model_key
        )

        duration_ms = int((time.time() - start_time) * 1000)

        log("INFO", "EVALUATOR",
            f"完成评估回答相关性指标 - 使用模型: {model_name}, 得分: {score:.3f}",
            metric=self.name,
            model_name=model_name,
            score=round(score, 3),
            duration_ms=duration_ms,
            reason_preview=reason[:100] if reason else "")

        return EvaluationResult(score=round(score, 3), reason=reason, duration_ms=duration_ms)


class CorrectnessMetric(BaseMetric):
    """Evaluates correctness against expected answer.

    支持两种评测模式：
    - Ground_Truth评测（默认）：将回答与 ground_truth 对比进行评分
    - 基于Prompt评测：使用数据项中的 prompt 作为评测标准对回答评分，ground_truth 非空时作为参考
    """

    name = "correctness"

    def __init__(self, model_key: str = None, judging_mode: str = "", eval_prompt: str = ""):
        self.model_key = model_key  # 不使用默认值，保持None表示离线评估
        self.judging_mode = judging_mode or ""
        self.eval_prompt = eval_prompt or ""

    async def evaluate(
        self,
        question: str,
        answer: str,
        context: Optional[str] = None,
        ground_truth: Optional[str] = None
    ) -> EvaluationResult:
        model_name = (get_llm_config(self.model_key).get("model") or self.model_key or "unknown").strip()

        # 兼容新旧名称：旧名"提示词评测"，新名"基于Prompt评测"
        is_prompt_mode = "Prompt" in self.judging_mode or "提示词" in self.judging_mode

        log("INFO", "EVALUATOR",
            f"开始评估正确性指标 - 使用模型: {model_name}, 评测模式: {'基于Prompt评测' if is_prompt_mode else 'Ground_Truth评测'}",
            metric=self.name,
            model_name=model_name,
            judging_mode=self.judging_mode,
            question_preview=question[:100] if question else "",
            answer_preview=answer[:100] if answer else "",
            ground_truth_preview=ground_truth[:100] if ground_truth else "",
            eval_prompt_preview=self.eval_prompt[:100] if self.eval_prompt else "")

        start_time = time.time()

        # 总是使用LLM评估，不回退到离线评估
        if not _is_model_available(self.model_key):
            raise ValueError(f"未配置有效的LLM模型，无法执行评估：{self.model_key}")

        score, reason = await evaluate_metric_with_llm(
            "correctness", question, answer, context, ground_truth, self.model_key,
            judging_mode=self.judging_mode,
            eval_prompt=self.eval_prompt,
        )

        duration_ms = int((time.time() - start_time) * 1000)

        log("INFO", "EVALUATOR",
            f"完成评估正确性指标 - 使用模型: {model_name}, 评测模式: {'基于Prompt评测' if is_prompt_mode else 'Ground_Truth评测'}, 得分: {score:.3f}",
            metric=self.name,
            model_name=model_name,
            judging_mode=self.judging_mode,
            score=round(score, 3),
            duration_ms=duration_ms,
            reason_preview=reason[:100] if reason else "")

        return EvaluationResult(score=round(score, 3), reason=reason, duration_ms=duration_ms)
