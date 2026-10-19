"""
扩展功能示例客户端
演示OpenTelemetry Trace接入和版本对比评测的使用方法
"""
import json
import requests
from datetime import datetime, timedelta
from typing import List, Optional, Dict


BASE_URL = "http://localhost:8000"


class OpenTelemetryClient:
    """OpenTelemetry Trace服务客户端"""
    
    @staticmethod
    def send_trace(trace_id: str, spans: List[Dict]) -> Dict:
        """发送Trace数据"""
        data = {
            "trace_id": trace_id,
            "spans": spans
        }
        response = requests.post(f"{BASE_URL}/extended/otel/traces", json=data)
        response.raise_for_status()
        return response.json()
    
    @staticmethod
    def send_llm_trace(
        trace_id: str,
        span_id: str,
        parent_span_id: Optional[str],
        model: str,
        prompt: str,
        completion: str,
        duration_ms: float,
        tokens: int
    ) -> Dict:
        """发送LLM调用Trace"""
        now = datetime.utcnow()
        start_time = now.isoformat()
        end_time = (now + timedelta(milliseconds=duration_ms)).isoformat()
        
        span = {
            "span_id": span_id,
            "parent_span_id": parent_span_id,
            "name": f"llm.{model}.generate",
            "kind": "client",
            "start_time": start_time,
            "end_time": end_time,
            "attributes": {
                "service.name": "llm-agent",
                "llm.model": model,
                "llm.prompt": prompt,
                "llm.completion": completion,
                "llm.tokens": tokens,
                "llm.latency_ms": duration_ms
            },
            "events": [],
            "status": "ok"
        }
        
        return OpenTelemetryClient.send_trace(trace_id, [span])
    
    @staticmethod
    def query_traces(
        trace_id: Optional[str] = None,
        service_name: Optional[str] = None,
        limit: int = 100
    ) -> Dict:
        """查询Trace列表"""
        params = {"limit": limit}
        if trace_id:
            params["trace_id"] = trace_id
        if service_name:
            params["service_name"] = service_name
        
        response = requests.get(f"{BASE_URL}/extended/otel/traces", params=params)
        response.raise_for_status()
        return response.json()
    
    @staticmethod
    def get_trace_detail(trace_id: str) -> Dict:
        """获取Trace详情"""
        response = requests.get(f"{BASE_URL}/extended/otel/traces/{trace_id}")
        response.raise_for_status()
        return response.json()
    
    @staticmethod
    def get_llm_stats(
        service_name: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None
    ) -> Dict:
        """获取LLM调用统计"""
        params = {}
        if service_name:
            params["service_name"] = service_name
        if start_time:
            params["start_time"] = start_time
        if end_time:
            params["end_time"] = end_time
        
        response = requests.get(f"{BASE_URL}/extended/otel/llm/stats", params=params)
        response.raise_for_status()
        return response.json()


class VersionComparisonClient:
    """版本对比服务客户端"""
    
    @staticmethod
    def compare(
        version_a_name: str,
        version_b_name: str,
        items: List[Dict],
        metrics: Optional[List[str]] = None
    ) -> Dict:
        """执行版本对比"""
        data = {
            "version_a_name": version_a_name,
            "version_b_name": version_b_name,
            "items": items,
            "metrics": metrics or ["faithfulness", "answer_relevancy", "correctness"]
        }
        
        response = requests.post(f"{BASE_URL}/extended/versions/compare", json=data)
        response.raise_for_status()
        return response.json()
    
    @staticmethod
    def compare_simple(
        version_a_name: str,
        version_b_name: str,
        items: List[Dict]
    ) -> Dict:
        """简化版版本对比"""
        params = {
            "version_a_name": version_a_name,
            "version_b_name": version_b_name
        }
        
        response = requests.post(
            f"{BASE_URL}/extended/versions/compare/simple",
            params=params,
            json=items
        )
        response.raise_for_status()
        return response.json()
    
    @staticmethod
    def get_result(comparison_id: str) -> Dict:
        """获取对比结果"""
        response = requests.get(f"{BASE_URL}/extended/versions/compare/{comparison_id}")
        response.raise_for_status()
        return response.json()
    
    @staticmethod
    def get_details(comparison_id: str) -> Dict:
        """获取对比详情"""
        response = requests.get(f"{BASE_URL}/extended/versions/compare/{comparison_id}/details")
        response.raise_for_status()
        return response.json()
    
    @staticmethod
    def list_comparisons(limit: int = 20) -> Dict:
        """列出对比记录"""
        response = requests.get(f"{BASE_URL}/extended/versions/compare", params={"limit": limit})
        response.raise_for_status()
        return response.json()
    
    @staticmethod
    def get_conclusion_descriptions() -> Dict:
        """获取结论说明"""
        response = requests.get(f"{BASE_URL}/extended/versions/conclusions")
        response.raise_for_status()
        return response.json()


def demo_otel():
    """演示OpenTelemetry Trace接入"""
    print("=== OpenTelemetry Trace 接入演示 ===\n")
    
    # 发送LLM调用Trace
    trace_id = f"trace_{datetime.now().strftime('%Y%m%d%H%M%S')}"
    
    result = OpenTelemetryClient.send_llm_trace(
        trace_id=trace_id,
        span_id="span_001",
        parent_span_id=None,
        model="gpt-4",
        prompt="What is the capital of France?",
        completion="The capital of France is Paris.",
        duration_ms=1500,
        tokens=20
    )
    
    print(f"Trace已发送: {result}")
    
    # traces = OpenTelemetry 查询Trace
   Client.query_traces()
    print(f"\n当前Trace数量: {traces['count']}")
    
    # 获取统计
    stats = OpenTelemetryClient.get_llm_stats()
    print(f"\nLLM调用统计: {json.dumps(stats, indent=2, ensure_ascii=False)}")


def demo_version_comparison():
    """演示版本对比"""
    print("\n=== 版本对比演示 ===\n")
    
    # 准备测试数据
    items = [
        {
            "question": "What is the capital of France?",
            "context": "France is a country in Western Europe.",
            "ground_truth": "The capital of France is Paris.",
            "version_a_answer": "Paris is the capital city of France.",
            "version_b_answer": "The capital of France is Paris, which is known for the Eiffel Tower."
        },
        {
            "question": "Explain photosynthesis.",
            "context": "Photosynthesis converts light into energy.",
            "ground_truth": "Photosynthesis is the process by which plants convert sunlight into energy.",
            "version_a_answer": "Photosynthesis turns sunlight into energy.",
            "version_b_answer": "Photosynthesis is a process used by plants to convert light energy into chemical energy that can later be released to fuel the organism's activities."
        },
        {
            "question": "What are the benefits of exercise?",
            "context": "Exercise improves health.",
            "ground_truth": "Exercise provides multiple health benefits.",
            "version_a_answer": "Exercise is good for health.",
            "version_b_answer": "Regular exercise provides numerous health benefits including improved cardiovascular health, stronger muscles, better mental health, and weight management."
        }
    ]
    
    # 执行版本对比
    result = VersionComparisonClient.compare(
        version_a_name="v1.0",
        version_b_name="v2.0",
        items=items
    )
    
    print(f"对比ID: {result['comparison_id']}")
    print(f"版本A: {result['version_a_name']}")
    print(f"版本B: {result['version_b_name']}")
    print(f"问题数量: {result['question_count']}")
    print(f"\n总体结论: {result['conclusion']}")
    print(f"结论说明: {result['conclusion_reason']}")
    print(f"提升比例: {result['improvement_percentage']}%")
    
    print(f"\n指标汇总:")
    for metric in result['metric_summaries']:
        print(f"  {metric['metric']}: {metric['version_a_score']} -> {metric['version_b_score']} "
              f"(变化: {metric['score_change_percent']:+.1f}%)")
    
    print(f"\n问题级别结果:")
    for q in result['question_results']:
        print(f"  问题{q['question_id']}: 胜者={q['winner']}")
        for m in q['metrics']:
            print(f"    {m['metric']}: {m['version_a_score']} -> {m['version_b_score']} "
                  f"({'+' if m['improved'] else '-'} {abs(m['score_change_percent']):.1f}%)")


def main():
    """主函数"""
    import sys
    
    if len(sys.argv) < 2:
        print("使用方法:")
        print("  python extended_client.py otel    - 演示OpenTelemetry Trace接入")
        print("  python extended_client.py compare - 演示版本对比")
        print("  python extended_client.py all     - 运行所有演示")
        return
    
    mode = sys.argv[1]
    
    if mode == "otel":
        demo_otel()
    elif mode == "compare":
        demo_version_comparison()
    elif mode == "all":
        demo_otel()
        demo_version_comparison()
    else:
        print(f"未知模式: {mode}")


if __name__ == "__main__":
    main()
