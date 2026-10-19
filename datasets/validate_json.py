import json

try:
    with open('onto-eval-20-with-context.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    print(f'JSON格式验证通过，共{len(data)}个问题')
    
    # 检查问题15
    for i, item in enumerate(data):
        if "F000 架次已安装旧物料 A001" in item.get('question', ''):
            print(f'问题{i+1} (问题15):')
            print(f'    ground_truth: {item.get("ground_truth", "")}')
            answer = item.get("model_answer", "")
            # 提取天数信息
            if "135天" in answer:
                print(f'    model_answer: 包含"135天"计算过程 (正确)')
            else:
                print(f'    model_answer: 不包含"135天"')
            
except json.JSONDecodeError as e:
    print(f'JSON格式错误: {e}')
except Exception as e:
    print(f'错误: {e}')