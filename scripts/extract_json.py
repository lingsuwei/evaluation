"""
JSON 数据提取工具
从 events 列表中提取 stream_complete 事件的 aggregation.eco_id 和 summary
支持单文件和批量目录处理
"""
import json
import os
import glob
from datetime import datetime
from typing import List, Dict, Any, Optional


def extract_from_file(file_path: str) -> Optional[Dict[str, Any]]:
    """
    从单个 JSON 文件中提取数据
    
    Args:
        file_path: JSON 文件路径
        
    Returns:
        提取的数据字典，包含 question 和 model_answer
    """
    log_entries = []
    result = None
    
    try:
        log_entries.append(f"[{datetime.now().isoformat()}] 开始处理文件: {file_path}")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        
        log_entries.append(f"[{datetime.now().isoformat()}] 文件加载成功")
        
        # 获取顶层 id
        file_id = data.get('id', 'unknown')
        log_entries.append(f"[{datetime.now().isoformat()}] 文件 ID: {file_id}")
        
        # 查找 events 列表
        events = data.get('events', [])
        log_entries.append(f"[{datetime.now().isoformat()}] 找到 {len(events)} 个事件")
        
        # 查找 stream_complete 事件
        stream_complete_event = None
        for i, event in enumerate(events):
            if event.get('event') == 'stream_complete':
                stream_complete_event = event
                log_entries.append(f"[{datetime.now().isoformat()}] 在索引 {i} 找到 stream_complete 事件")
                break
        
        if not stream_complete_event:
            log_entries.append(f"[{datetime.now().isoformat()}] 未找到 stream_complete 事件")
            return None
        
        # 提取 data.result.aggregation
        event_data = stream_complete_event.get('data', {})
        result_data = event_data.get('result', {})
        aggregation = result_data.get('aggregation', {})
        
        log_entries.append(f"[{datetime.now().isoformat()}] 提取 aggregation 数据")
        
        # 提取 eco_id
        eco_id = aggregation.get('eco_id', '')
        log_entries.append(f"[{datetime.now().isoformat()}] eco_id: {eco_id}")
        
        # 提取 summary (与 aggregation 平级)
        summary = aggregation.get('summary', '')
        log_entries.append(f"[{datetime.now().isoformat()}] summary 长度: {len(summary)} 字符")
        
        # 构建输出结果
        result = {
            "question": eco_id,
            "model_answer": summary
        }
        
        log_entries.append(f"[{datetime.now().isoformat()}] 提取完成")
        
        # 写入日志文件
        log_file_path = f"{file_id}.log"
        with open(log_file_path, 'w', encoding='utf-8') as log_file:
            log_file.write('\n'.join(log_entries))
        
        print(f"✅ 处理完成: {file_path}")
        print(f"   日志文件: {log_file_path}")
        
        return result
        
    except Exception as e:
        log_entries.append(f"[{datetime.now().isoformat()}] 错误: {str(e)}")
        print(f"❌ 处理失败: {file_path} - {str(e)}")
        
        # 尝试写入错误日志
        try:
            error_log_path = f"error_{os.path.basename(file_path)}.log"
            with open(error_log_path, 'w', encoding='utf-8') as log_file:
                log_file.write('\n'.join(log_entries))
        except:
            pass
        
        return None


def extract_from_directory(dir_path: str, pattern: str = "*.json") -> List[Dict[str, Any]]:
    """
    批量处理目录下的所有 JSON 文件
    
    Args:
        dir_path: 目录路径
        pattern: 文件匹配模式，默认 *.json
        
    Returns:
        提取结果列表
    """
    results = []
    
    # 查找所有匹配的文件
    search_pattern = os.path.join(dir_path, pattern)
    json_files = glob.glob(search_pattern)
    
    print(f"📁 在目录 {dir_path} 中找到 {len(json_files)} 个 JSON 文件")
    
    for file_path in json_files:
        result = extract_from_file(file_path)
        if result:
            results.append(result)
    
    return results


def main():
    import sys
    
    if len(sys.argv) < 2:
        print("用法:")
        print("  单文件: python extract_json.py <file.json>")
        print("  批量:   python extract_json.py <directory> [pattern]")
        print("")
        print("示例:")
        print("  python extract_json.py 1.json")
        print("  python extract_json.py ./data")
        print("  python extract_json.py ./data '*.json'")
        sys.exit(1)
    
    target = sys.argv[1]
    
    if os.path.isfile(target):
        # 单文件处理
        result = extract_from_file(target)
        if result:
            results = [result]
            output_file = "output.json"
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            print(f"\n📄 结果已保存到: {output_file}")
            print(f"   提取了 {len(results)} 条记录")
    
    elif os.path.isdir(target):
        # 批量目录处理
        pattern = sys.argv[2] if len(sys.argv) > 2 else "*.json"
        results = extract_from_directory(target, pattern)
        
        if results:
            output_file = "output.json"
            with open(output_file, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            print(f"\n📄 结果已保存到: {output_file}")
            print(f"   提取了 {len(results)} 条记录")
        else:
            print("\n⚠️ 未提取到任何数据")
    
    else:
        print(f"❌ 路径不存在: {target}")
        sys.exit(1)


if __name__ == "__main__":
    main()
