import asyncio
import sys
import os
import json

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from evaluators.faithfulness import FaithfulnessMetric

async def test_faithfulness_with_context():
    print("\n" + "="*60)
    print("验证忠实度计算修复")
    print("="*60)
    
    # 创建离线模式的评估器
    evaluator = FaithfulnessMetric(model_key=None)
    print(f"评估器模式: {'离线' if evaluator.model_key is None else '在线'}")
    
    # 读取带上下文的测试数据
    test_data_path = "d:\\llm-eval-agent-master\\onto-eval\\datasets\\onto-eval-20-with-context.json"
    if os.path.exists(test_data_path):
        with open(test_data_path, 'r', encoding='utf-8') as f:
            test_data = json.load(f)
        print(f"\n测试数据已加载: {len(test_data)} 个问题")
        
        # 测试前3个问题
        for i, item in enumerate(test_data[:3]):
            print(f"\n{'-'*50}")
            print(f"测试问题 {i+1}")
            print(f"问题: {item['question'][:50]}...")
            print(f"上下文长度: {len(item['context'])} 字符")
            print(f"模型回答: {item['model_answer'][:50]}...")
            
            try:
                # 运行忠实度评估
                result = await evaluator.evaluate(
                    question=item['question'],
                    answer=item['model_answer'],
                    context=item['context'],
                    ground_truth=item['ground_truth']
                )
                
                print(f"忠实度得分: {result.score:.3f}")
                print(f"评估理由: {result.reason}")
                print(f"评估耗时: {result.duration_ms} ms")
                
                # 验证是否使用了上下文
                if "上下文" in result.reason:
                    print("✅ 已正确使用上下文进行评估")
                else:
                    print("❌ 未能使用上下文进行评估")
                    
            except Exception as e:
                print(f"❌ 评估错误: {e}")
                import traceback
                traceback.print_exc()
    else:
        print(f"❌ 测试数据文件不存在: {test_data_path}")
    
    print("\n" + "="*60)
    print("验证完成")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(test_faithfulness_with_context())
