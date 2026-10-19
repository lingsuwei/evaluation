"""
LLM Evaluation API Client
调用 /evaluate 接口的示例代码，支持 SSE 实时流式获取评估进度
执行后自动打开浏览器，前端任务监控页面同步显示思维链
CoT 日志保存到本地 logs/ 目录
"""
import requests
import json
import time
import sys
import os
import webbrowser
from datetime import datetime
from typing import Optional

BASE_URL = "http://192.168.202.2:8000"
FRONTEND_URL = "http://192.168.202.2:3000"
LOG_DIR = "logs"

# 全局日志文件句柄
_log_file = None


def init_log_file(task_uuid: str):
    """初始化任务日志文件"""
    global _log_file
    os.makedirs(LOG_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, f"{task_uuid}_{timestamp}.log")
    _log_file = open(log_path, "w", encoding="utf-8")
    _log_file.write(f"=== 任务日志 ===\n")
    _log_file.write(f"任务ID: {task_uuid}\n")
    _log_file.write(f"开始时间: {datetime.now().isoformat()}\n")
    _log_file.write(f"{'=' * 50}\n\n")
    print(f"📝 日志文件: {log_path}")
    return log_path


def log_write(message: str):
    """写入日志"""
    if _log_file:
        _log_file.write(message + "\n")
        _log_file.flush()


def close_log_file():
    """关闭日志文件"""
    global _log_file
    if _log_file:
        _log_file.write(f"\n{'=' * 50}\n")
        _log_file.write(f"结束时间: {datetime.now().isoformat()}\n")
        _log_file.close()
        _log_file = None


def upload_dataset(file_path: str) -> str:
    """上传数据集，返回 dataset_id"""
    with open(file_path, "rb") as f:
        response = requests.post(
            f"{BASE_URL}/upload",
            files={"file": (file_path, f, "application/json")}
        )
    response.raise_for_status()
    result = response.json()
    print(f"数据集上传成功: {result}")
    return result["dataset_id"]


def start_evaluation(dataset_id: str, model_name: Optional[str] = "default") -> str:
    """发起评估任务，返回 task_uuid"""
    payload = {
        "dataset_id": dataset_id,
        "model_name": model_name
    }
    response = requests.post(f"{BASE_URL}/evaluate", json=payload)
    response.raise_for_status()
    result = response.json()
    print(f"评估任务已创建: {result}")
    return result["task_uuid"]


def stream_evaluation(task_uuid: str):
    """SSE 实时流式获取评估进度和 CoT 推理过程"""
    print(f"\n=== 开始监听任务 {task_uuid} ===\n")
    
    response = requests.get(
        f"{BASE_URL}/stream/{task_uuid}",
        stream=True,
        headers={"Accept": "text/event-stream"}
    )
    response.raise_for_status()
    
    event_type = None
    data_buffer = ""
    
    for line in response.iter_lines(decode_unicode=True):
        if line is None:
            continue
            
        line = line.strip()
        
        if line.startswith("event:"):
            event_type = line[6:].strip()
        elif line.startswith("data:"):
            data_buffer = line[5:].strip()
        elif line == "" and data_buffer:
            # 空行表示事件结束，处理数据
            try:
                data = json.loads(data_buffer)
                handle_sse_event(event_type, data)
                
                if event_type in ["task_complete", "task_error"]:
                    return data
            except json.JSONDecodeError:
                pass
            
            event_type = None
            data_buffer = ""
    
    return None


def handle_sse_event(event_type: str, data: dict):
    """
    处理 SSE 事件 - 与前端 TaskMonitor.tsx 保持一致
    事件类型：cot_update, metric_result, metric_error, task_complete, task_error
    同时输出到控制台和日志文件
    """
    if event_type == "cot_update":
        # 思维链更新 - 对应前端的 💭 思维链
        question_id = data.get("question_id", 0)
        model = data.get("model", "")
        metric = data.get("metric", "")
        cot_chunk = data.get("cot_chunk", "")
        
        msg1 = f"\n💭 [问题 {question_id + 1}] | {model} | {metric}"
        msg2 = f"   {cot_chunk}"
        print(msg1)
        print(msg2)
        log_write(f"[COT] 问题 {question_id + 1} | {model} | {metric}")
        log_write(cot_chunk)
        log_write("")
    
    elif event_type == "metric_result":
        # 评估结果 - 对应前端的 📊 结果
        question_id = data.get("question_id", 0)
        model = data.get("model", "")
        metric = data.get("metric", "")
        score = data.get("score", 0)
        passed = data.get("passed", False)
        
        status = "✓ 通过" if passed else "✗ 未通过"
        msg1 = f"📊 [问题 {question_id + 1}] | {model} | {metric}"
        msg2 = f"   得分: {score:.2f} {status}"
        print(msg1)
        print(msg2)
        log_write(f"[RESULT] 问题 {question_id + 1} | {model} | {metric} | 得分: {score:.2f} | {status}")
    
    elif event_type == "metric_error":
        # 评估错误 - 对应前端的 ⚠️ 错误
        question_id = data.get("question_id", 0)
        model = data.get("model", "")
        metric = data.get("metric", "")
        error = data.get("error", "")
        
        msg1 = f"⚠️ [问题 {question_id + 1}] | {model} | {metric}"
        msg2 = f"   错误: {error}"
        print(msg1)
        print(msg2)
        log_write(f"[ERROR] 问题 {question_id + 1} | {model} | {metric} | {error}")
    
    elif event_type == "task_complete":
        print(f"\n✅ 任务完成")
        log_write("\n[COMPLETE] 任务完成")
    
    elif event_type == "task_error":
        msg = f"\n❌ 任务失败: {data.get('error', '未知错误')}"
        print(msg)
        log_write(f"[FAILED] {data.get('error', '未知错误')}")
    
    elif event_type == "ping":
        pass  # 心跳包，忽略
    
    else:
        # 其他未知事件
        print(f"[{event_type}] {data}")
        log_write(f"[{event_type}] {json.dumps(data, ensure_ascii=False)}")


def poll_result(task_uuid: str, interval: float = 2.0, timeout: float = 300.0):
    """轮询获取评估结果（备用方式）"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        response = requests.get(f"{BASE_URL}/result/{task_uuid}")
        response.raise_for_status()
        result = response.json()
        
        status = result.get("status")
        if status == "completed":
            print("✅ 评估完成!")
            # 输出思维链到日志
            log_cot_from_result(result)
            return result
        elif status == "failed":
            print(f"❌ 评估失败: {result.get('error')}")
            log_write(f"[FAILED] {result.get('error', '未知错误')}")
            return result
        else:
            print(f"⏳ 进度: {result.get('progress', 'N/A')}")
            time.sleep(interval)
    
    raise TimeoutError("评估超时")


def log_cot_from_result(result: dict):
    """从评估结果中提取思维链并输出到日志"""
    details = result.get("details", [])
    if not details:
        return
    
    print("\n=== 思维链详情 ===")
    log_write("\n=== 思维链详情 ===\n")
    
    # 按问题ID分组
    questions = {}
    for item in details:
        q_id = item.get("question_id", 0)
        if q_id not in questions:
            questions[q_id] = []
        questions[q_id].append(item)
    
    # 按问题顺序输出
    for q_id in sorted(questions.keys()):
        items = questions[q_id]
        print(f"\n📝 问题 {q_id + 1}")
        log_write(f"{'=' * 40}")
        log_write(f"问题 {q_id + 1}")
        log_write(f"{'=' * 40}")
        
        for item in items:
            model = item.get("model", "")
            metrics = item.get("metrics", [])
            
            for metric_info in metrics:
                metric_name = metric_info.get("name", "")
                score = metric_info.get("score", 0)
                passed = metric_info.get("passed", False)
                cot = metric_info.get("cot", "")
                
                status = "✓ 通过" if passed else "✗ 未通过"
                
                # 控制台输出
                print(f"  💭 {model} | {metric_name} | 得分: {score:.2f} {status}")
                if cot:
                    # 截取前200字符显示在控制台
                    cot_preview = cot[:200] + "..." if len(cot) > 200 else cot
                    print(f"     {cot_preview}")
                
                # 完整思维链写入日志
                log_write(f"\n[{model}] {metric_name}")
                log_write(f"得分: {score:.2f} | {status}")
                log_write(f"思维链:")
                log_write(f"{cot if cot else '(无)'}")
                log_write("")


def main():
    # 使用示例数据集
    dataset_file = "qa-ds.json" if len(sys.argv) < 2 else sys.argv[1]
    model_name = "default" if len(sys.argv) < 3 else sys.argv[2]
    # --stream: client独占SSE流（前端看不到）
    # 默认: 轮询模式，前端可以同步显示思维链
    use_stream = "--stream" in sys.argv
    no_browser = "--no-browser" in sys.argv
    
    print(f"=== LLM 评估客户端 ===")
    print(f"数据集: {dataset_file}")
    print(f"模型: {model_name}")
    print(f"模式: {'SSE 流式(前端无法同步)' if use_stream else '轮询(前端可同步显示思维链)'}\n")
    
    # 1. 上传数据集
    dataset_id = upload_dataset(dataset_file)
    
    # 2. 发起评估
    task_uuid = start_evaluation(dataset_id, model_name)
    
    # 3. 初始化日志文件
    log_path = init_log_file(task_uuid)
    log_write(f"数据集: {dataset_file}")
    log_write(f"模型: {model_name}")
    log_write(f"Dataset ID: {dataset_id}")
    log_write("")
    
    # 4. 自动打开浏览器到前端任务监控页面
    if not no_browser and not use_stream:
        monitor_url = f"{FRONTEND_URL}?task={task_uuid}"
        print(f"\n🌐 正在打开浏览器...")
        print(f"   {monitor_url}")
        webbrowser.open(monitor_url)
    
    print(f"\n📺 前端任务监控页面将同步显示思维链")
    print(f"   任务ID: {task_uuid}\n")
    
    # 5. 获取结果
    if use_stream:
        # SSE 流式获取实时进度和 CoT（会独占流，前端看不到）
        stream_evaluation(task_uuid)
        result = requests.get(f"{BASE_URL}/result/{task_uuid}").json()
    else:
        # 轮询方式 - 前端可以同步显示思维链
        result = poll_result(task_uuid)
    
    # 6. 输出最终结果
    print("\n=== 评估结果摘要 ===")
    log_write("\n=== 评估结果摘要 ===")
    if result.get("status") == "completed":
        summary = result.get("summary", {})
        radar_data = summary.get("radar_data", {})
        for model, metrics in radar_data.items():
            print(f"\n模型: {model}")
            log_write(f"\n模型: {model}")
            for metric, score in metrics.items():
                print(f"  {metric}: {score:.3f}")
                log_write(f"  {metric}: {score:.3f}")
    
    # 7. 关闭日志文件
    close_log_file()
    print(f"\n📝 日志已保存: {log_path}")


if __name__ == "__main__":
    main()
