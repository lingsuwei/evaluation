# 示例：自定义评估脚本
# 脚本必须定义 evaluate() 函数，返回 {"score": 0-1, "reason": "说明"}

def evaluate(question, answer, context=None, ground_truth=None):
    """
    评估函数 - 检查Agent回答中的数值是否正确
    
    参数:
        question: 问题内容
        answer: Agent回答
        context: 上下文信息
        ground_truth: 标准答案
    
    返回:
        dict: {"score": float(0-1), "reason": str}
    """
    import re
    
    if not ground_truth or not answer:
        return {"score": 0.0, "reason": "缺少标准答案或Agent回答"}
    
    # 从标准答案中提取所有数值
    gt_numbers = re.findall(r'-?\d+\.?\d*', ground_truth)
    if not gt_numbers:
        return {"score": 0.5, "reason": "标准答案中未找到数值，无法进行数值评估"}
    
    # 从Agent回答中提取所有数值
    ans_numbers = re.findall(r'-?\d+\.?\d*', answer)
    
    # 计算匹配率
    matched = 0
    details = []
    for gt_num in gt_numbers:
        gt_val = float(gt_num)
        found = False
        for ans_num in ans_numbers:
            ans_val = float(ans_num)
            # 允许5%的误差
            if abs(gt_val) > 0 and abs(ans_val - gt_val) / abs(gt_val) < 0.05:
                found = True
                break
            elif abs(gt_val) == 0 and abs(ans_val) < 0.01:
                found = True
                break
        if found:
            matched += 1
            details.append(f"✅ {gt_num}")
        else:
            details.append(f"❌ {gt_num}(未匹配)")
    
    score = matched / len(gt_numbers) if gt_numbers else 0.0
    reason = f"数值匹配率: {matched}/{len(gt_numbers)} = {score:.1%}; " + "; ".join(details)
    
    return {"score": round(score, 3), "reason": reason}
