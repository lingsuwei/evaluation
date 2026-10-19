import pandas as pd
import json

def extract_excel_to_json(excel_path, output_json_path):
    # 读取Excel文件
    df = pd.read_excel(excel_path)
    
    # 清理列名（去除空格和特殊字符）
    df.columns = df.columns.str.strip().str.replace('\n', '').str.replace(' ', '')
    
    # 筛选有效列（确保包含question、ground_truth、model_answer）
    required_cols = ['question', 'ground_truth', 'model_answer']
    df = df[required_cols].dropna(subset=required_cols)  # 移除关键字段为空的行
    
    # 构建JSON数据列表
    json_data = []
    for _, row in df.iterrows():
        # 清理字段内容（去除前后空格、多余换行）
        question = str(row['question']).strip().replace('\n\n', '\n').replace('\r', '')
        ground_truth = str(row['ground_truth']).strip().replace('\n\n', '\n').replace('\r', '')
        model_answer = str(row['model_answer']).strip().replace('\n\n', '\n').replace('\r', '')
        
        # 跳过ground_truth或model_answer为空的无效数据
        if ground_truth in ['nan', ''] or model_answer in ['nan', '']:
            continue
        
        # 按照参考格式构建条目
        entry = {
            "question": question,
            "context": "",  # 原Excel无context字段，留空
            "ground_truth": ground_truth,
            "model_answer": model_answer
        }
        json_data.append(entry)
    
    # 保存为JSON文件（确保中文编码正确）
    with open(output_json_path, 'w', encoding='utf-8') as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)
    
    print(f"成功生成JSON文件：{output_json_path}")
    print(f"共提取有效数据条目：{len(json_data)} 条")

# 执行提取（请替换为实际的Excel文件路径）
if __name__ == "__main__":
    excel_file = "e2e-test-1226.xlsx"  # 输入Excel文件路径
    output_file = "e2e-1226.json"    # 输出JSON文件路径
    extract_excel_to_json(excel_file, output_file)
