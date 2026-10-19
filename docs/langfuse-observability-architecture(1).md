# Onto-Agent Langfuse 可观测性架构与流程分析

> 基于 `main_trace` 分支 (最新 commit `e5e7026`)，涵盖 Agent Loop 和 Direct Execution 两条执行路径的完整 trace 架构。

---

## 1. 概述

Onto-Agent 的 Langfuse 可观测性系统采用 **厂商无关的事件驱动架构**，核心设计原则：

- **解耦**：运行时代码只发出供应商无关的 `RuntimeEvent`，Langfuse 映射和 I/O 完全在导出层完成
- **非阻塞**：事件通过 `ObservabilityBus` 异步队列派发，`emit()` 永不阻塞 agent 执行路径
- **安全**：所有输出经过 PII 脱敏（邮箱、手机、身份证等），密钥字段自动 `[REDACTED]`
- **可扩展**：通过 `EXPORTER_REGISTRY` 可注册其他观测后端
- **双路径统一打点**：Agent Loop（多轮 LLM + 工具）和 Direct Execution（直接脚本执行）从统一的 `RunRuntimeTrace` 入口开始，终点按路径分流

---

## 2. 整体架构图

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              Onto-Agent 运行时                                       │
│                                                                                     │
│  ┌─────────────────────────────────────────────────────────────────────────────┐    │
│  │                           Orchestrator.run()                                 │    │
│  │                           RunRuntimeTrace                                     │    │
│  │                    (run.started / finished / failed / cancelled)               │    │
│  └────────┬────────────────────────────┬────────────────────────────────────────┘    │
│           │                            │                                             │
│           ▼                            ▼                                             │
│  ┌──────────────────┐      ┌─────────────────────────────────────┐                  │
│  │ Direct Execution  │      │        Agent Loop 路径              │                  │
│  │    路径            │      │                                     │                  │
│  │  (无需 LLM 迭代)   │      │  ┌─────────────────────────────┐   │                  │
│  │                   │      │  │  AgentLoopEngine.run()       │   │                  │
│  │ ┌─────────────┐   │      │  │  AgentRuntimeTrace            │   │                  │
│  │ │Router/Matcher│   │      │  │  agent_loop_scope()          │   │                  │
│  │ │ (无 trace)   │   │      │  │  ├── llm_call_scope()       │   │                  │
│  │ └──────┬──────┘   │      │  │  ├── tool_batch_scope()      │   │                  │
│  │        ▼          │      │  │  └── tool_batch_wait_scope() │   │                  │
│  │ ┌─────────────┐   │      │  └─────────────────────────────┘   │                  │
│  │ │run_direct_  │   │      │                                     │                  │
│  │ │script()     │   │      │  ┌─────────────────────────────┐   │                  │
│  │ │(SSE events) │   │      │  │  Tool Execution             │   │                  │
│  │ └─────────────┘   │      │  │  tools/events.py            │   │                  │
│  └──────────────────┘      │  │  emit_tool_started/finished   │   │                  │
│                             │  │  (TOOL_STARTED/FINISHED)     │   │                  │
│                             │  └─────────────────────────────┘   │                  │
│                             └─────────────────────────────────────┘                  │
│                                    │                                               │
│                                    ▼                                               │
│  ┌──────────────────────────────────────────────────────────────────────────────┐  │
│  │                      Trace Facades (业务门面)                                │  │
│  │                                                                              │  │
│  │   RunRuntimeTrace          AgentRuntimeTrace            Tool Events         │  │
│  │   (所有路径统一入口 /        (仅 Agent Loop 路径：       (仅 Agent Loop 路径：  │  │
│  │    终点按路径分流)           llm/tool_batch scope)      工具级别追踪)          │  │
│  └──────────────────────────────────┬───────────────────────────────────────────┘  │
│                                     │                                               │
│                                     ▼                                               │
│  ┌──────────────────────────────────────────────────────────────────────────────┐  │
│  │                     RuntimeEvent (供应商无关契约)                             │  │
│  │                                                                              │  │
│  │  type | run_id | session_id | turn_id | span_key | parent_key | payload     │  │
│  └──────────────────────────────────┬───────────────────────────────────────────┘  │
│                                     │                                               │
│                                     ▼                                               │
│  ┌──────────────────────────────────────────────────────────────────────────────┐  │
│  │                     RuntimeObserver.emit()                                    │  │
│  └──────────────────────────────────┬───────────────────────────────────────────┘  │
│                                     │                                               │
└─────────────────────────────────────┼───────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         ObservabilityBus (异步队列)                          │
│                                                                             │
│   ┌──────────┐    Queue(maxsize=1000)    ┌──────────────────────┐           │
│   │  emit()  │ ──── non-blocking ────▶  │  Background Worker   │           │
│   │ put_nowait                              │  (daemon thread)     │           │
│   └──────────┘                            └──────────┬───────────┘           │
│                                                       │                      │
│                           flush_interval=5s ◀─────────┘                      │
│                           drain on close                                    │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      LangfuseExporter (事件→Langfuse 映射)                   │
│                                                                             │
│   事件处理器映射:                                                             │
│   ┌────────────────────┬────────────────────────────────────────────────┐   │
│   │ RUN_STARTED        │ → Langfuse Trace (create_trace)               │   │
│   │ RUN_FINISHED       │ → Trace output (update_trace_output)          │   │
│   │ RUN_FAILED         │ → Trace output (error status)                 │   │
│   │ RUN_CANCELLED      │ → Trace output (cancelled status)             │   │
│   │ AGENT_LOOP_STARTED │ → Langfuse Span (create_span)                │   │
│   │ AGENT_LOOP_FINISHED│ → Span.end(success)                          │   │
│   │ LLM_STARTED        │ → Langfuse Generation (create_llm_span)      │   │
│   │ LLM_FINISHED       │ → Generation.end(usage, output)              │   │
│   │ LLM_FAILED         │ → Generation.end(error)                      │   │
│   │ TOOL_BATCH_STARTED │ → Langfuse Span (tools group)                │   │
│   │ TOOL_BATCH_FINISHED│ → Span.end(success)                          │   │
│   │ TOOL_BATCH_FAILED  │ → Span.end(error)                            │   │
│   │ TOOL_STARTED       │ → Langfuse Span (individual tool)            │   │
│   │ TOOL_PROGRESS      │ → 缓存 progress 事件                         │   │
│   │ TOOL_FINISHED      │ → Span.end(output + progress_events)         │   │
│   └────────────────────┴────────────────────────────────────────────────┘   │
│                                                                             │
│   内部状态:                                                                  │
│   _trace_ids_by_run   : {run_id → langfuse_trace_id}                       │
│   _spans_by_run       : {run_id → {span_key → langfuse_span_id}}           │
│   _tool_progress_by_run : {run_id → {span_key → [progress_events]}}        │
│   _run_stats_by_run   : {run_id → {llm_calls, tool_calls, usage, ...}}    │
│   _terminal_runs      : {run_id} (已完成的 run)                              │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    LangfuseTraceAdapter (SDK 适配层)                         │
│                                                                             │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │  PII 脱敏 (_sanitize_dict / _mask_text)                            │   │
│   │                                                                     │   │
│   │  手机号:   1[3-9]\d{9}         → 1*** **** ****                    │   │
│   │  身份证:   \d{17}[\dXx]       → *****************                   │   │
│   │  邮箱:     .*@.*\..*           → ***@***.***                       │   │
│   │  URL:      https?://.*         → [URL]                              │   │
│   │  IP:       \d{1,3}(\.\d{1,3}){3} → ***.***.***.***                │   │
│   │  MAC:      ([0-9A-Fa-f]{2}[:-]){5}.. → **:**:**:**:**:**          │   │
│   │                                                                     │   │
│   │  敏感字段: password/secret/token/key/api_key/credential → [REDACTED] │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│   ┌─────────────────────────────────────────────────────────────────────┐   │
│   │  SDK 调用封装                                                       │   │
│   │  create_trace()        → client.trace(...)                         │   │
│   │  create_span()         → trace.span(...)                           │   │
│   │  create_llm_span()     → trace.generation(...)                     │   │
│   │  update_llm_span_tokens() → span.end(usage=, output=)             │   │
│   │  end_observation()     → span.end(output=, level=)                │   │
│   │  flush()               → client.flush()                           │   │
│   │  close()               → client.shutdown() / flush()              │   │
│   └─────────────────────────────────────────────────────────────────────┘   │
│                                                                             │
│   生命周期管理:                                                              │
│   _active_traces : {trace_id → {trace, spans, terminal?}}                  │
│   terminal + no spans → release trace (内存回收)                             │
│   _active_traces_lock (threading.Lock) 保护并发访问                          │
│                                                                             │
│   Token 估算:                                                               │
│   _estimate_tokens(): 中文字符/2 + 其他字符/4 + 1                          │
└─────────────────────────────────────────────────────────────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                      Langfuse Cloud (SaaS 可观测平台)                        │
│                                                                             │
│  ┌─ Agent Loop 路径 ─────────────────────────────────────────────────────┐  │
│  │                                                                       │  │
│  │  Trace (agent_request:{run_id})                                      │  │
│  │  ├── Span: agent_loop:main                                           │  │
│  │  │   ├── Generation: llm:main_iteration (LLM 调用)                   │  │
│  │  │   │   input: {messages, tools, api_params}                        │  │
│  │  │   │   output: {content, reasoning_content, tool_calls, finish_reason}│  │
│  │  │   │   usage: {prompt_tokens, completion_tokens, total_tokens}     │  │
│  │  │   ├── Span: tools:main_iteration_0 (工具批次)                      │  │
│  │  │   │   ├── Span: tool:read_file:call-1 (单个工具)                  │  │
│  │  │   │   │   input: {path: "a.md"}                                   │  │
│  │  │   │   │   output: {status, output_preview, elapsed_ms}            │  │
│  │  │   │   └── Span: tool:business_data_search:call-2                  │  │
│  │  │   └── Generation: llm:main_iteration (第 2 轮迭代)                 │  │
│  │  └── Span: agent_loop:subagent:sub-1 (子 agent)                     │  │
│  │      ├── Generation: llm:subagent_iteration                          │  │
│  │      └── Span: tools:subagent:sub-1:3                                │  │
│  │          └── Span: tool:read_file:call-1                             │  │
│  │                                                                       │  │
│  └───────────────────────────────────────────────────────────────────────┘  │
│                                                                             │
│  ┌─ Direct Execution 路径 ───────────────────────────────────────────────┐  │
│  │                                                                       │  │
│  │  Trace (agent_request:{run_id})                                      │  │
│  │  ├── Span: agent_loop:direct                                         │  │
│  │  │   ├── Generation: llm:direct_iteration (虚拟路由)                  │  │
│  │  │   │   model: "direct_execution/virtual"                           │  │
│  │  │   │   usage: {prompt_tokens, completion_tokens, total_tokens}     │  │
│  │  │   └── Span: tools:direct:0                                        │  │
│  │  │       └── Span: tool:execute_script:供应商信息查询.py              │  │
│  │  │           input: {ontology_id, operation_key}                     │  │
│  │  │           output: {status, output_preview, elapsed_ms}            │  │
│  │  │                                                                   │  │
│  │  │  Trace Output:                                                    │  │
│  │  │    status: "success"                                              │  │
│  │  │    final_answer: "[Direct Execution] ..."                          │  │
│  │  │    direct_execution: true                                         │  │
│  │  │    llm_calls: 1, tool_calls_started: 1, main_iterations: 1       │  │
│  │  └───────────────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 模块职责划分

### 3.1 事件契约层 (`events.py`)

| 组件 | 职责 |
|------|------|
| `RuntimeEventType` | 定义 14 种供应商无关事件类型 |
| `RuntimeStatus` | 定义 success/error/cancelled 三种终态 |
| `RuntimeRequestLabel` | 标记请求类型 (如 `reasoning_summary`) |
| `RuntimeEvent` | 不可变事件数据类，包含 type/run_id/session_id/turn_id/span_key/parent_key/payload/timestamp_ms |

**事件类型一览：**

```
Agent Loop 路径 → 全量事件:
  Run 级:     RUN_STARTED → RUN_FINISHED / RUN_FAILED / RUN_CANCELLED
  Loop 级:    AGENT_LOOP_STARTED → AGENT_LOOP_FINISHED
  LLM 级:     LLM_STARTED → LLM_FINISHED / LLM_FAILED
  Tool 级:    TOOL_BATCH_STARTED → TOOL_BATCH_FINISHED / TOOL_BATCH_FAILED
              TOOL_STARTED → TOOL_PROGRESS → TOOL_FINISHED

Direct Execution 路径（direct_script 成功分支）→ 全量事件:
  Run 级:     RUN_STARTED → RUN_FINISHED
  Loop 级:    AGENT_LOOP_STARTED → AGENT_LOOP_FINISHED
  LLM 级:     LLM_STARTED → LLM_FINISHED (model="direct_execution/virtual")
  Tool 级:    TOOL_BATCH_STARTED → TOOL_BATCH_FINISHED
              TOOL_STARTED → TOOL_FINISHED

Direct Execution 路径（missing_args 分支）→ 仅两级事件:
  Run 级:     RUN_STARTED → RUN_FINISHED
  无 Loop/LLM/Tool 级事件
```

### 3.2 观察者协议层 (`observer.py`)

| 组件 | 职责 |
|------|------|
| `RuntimeObserver` | Protocol 接口，定义 `enabled` 属性和 `emit()` 方法 |
| `NoopObserver` | 空实现，`enabled=False`，所有 `emit()` 返回 False |
| `NOOP_OBSERVER` | 全局单例，未启用可观测性时使用 |

### 3.3 Trace Facade 层 (`traces/`)

运行时代码通过这些门面类描述业务语义，**不感知任何后端**：

| 门面类 | 作用 | 核心方法 |
|--------|------|----------|
| `RunRuntimeTrace` | 一次完整 workflow run 的生命周期 | `started()`, `finished()`, `cancelled()`, `failed()` |
| `AgentRuntimeTrace` | 一次 agent loop 执行 | `agent_loop_scope()`, `llm_call_scope()`, `tool_batch_scope()`, `tool_batch_wait_scope()` |
| `DirectExecutionRuntimeTrace` | Direct Execution 的虚拟链路追踪 | `start_loop()`, `finish_loop()`, `start_llm()`, `finish_llm()`, `start_tool_exec()`, `finish_tool_exec()` |
| `_LLMCallTrace` | 单次 LLM 调用（由 `llm_call_scope` 产出） | `finish(usage, prompt_text, completion_text, reasoning_text, tool_calls, finish_reason, ...)` |
| `_ToolBatchTrace` | 单次工具批次（由 `tool_batch_scope` 产出） | `finish(tool_calls_count, tool_names, duration_ms)` |

> **注意**：`DirectExecutionRuntimeTrace` 是 `main_trace` 分支新增的门面类，位于 `traces/direct_execution.py`。它模拟了 Agent Loop 的 span 层级但无需实际 LLM 迭代，在 Direct Execution 路径中发射完整的 `AGENT_LOOP → LLM → TOOL_BATCH → TOOL` 事件链，使两条路径在 Langfuse 中呈现统一的层次结构。

### 3.4 工具事件层 (`tools/events.py`)

| 函数 | 作用 |
|------|------|
| `emit_tool_started()` | 发射 `TOOL_STARTED` 事件 + SSE 协议事件 |
| `emit_tool_finished()` | 发射 `TOOL_FINISHED` 事件 + SSE 协议事件 |
| `emit_protocol_event()` | 统一入口：SSE 事件 + 可观测性事件双发 |
| `_emit_tool_observability_event()` | 将 `tool.started/finished/progress` 协议事件转换为 `RuntimeEvent` |
| `_inject_subagent_lineage()` | 自动注入 subagent 血缘信息 |

### 3.5 异步总线层 (`bus.py`)

`ObservabilityBus` 是事件派发的核心管道：

```
emit(event) → queue.put_nowait(event)  [非阻塞，队列满则丢弃]
                    │
                    ▼
          Background Worker (daemon thread)
                    │
          ┌─────────┼──────────┐
          ▼         ▼          ▼
     exporter.export()  flush()  close()
```

**关键参数：**
- `queue_size=1000`：事件队列容量
- `flush_interval_seconds=5.0`：定时 flush 间隔
- 队列满时 drop 事件并 warning 日志
- close 时 drain + flush 保证数据不丢

### 3.6 导出层 (`exporters/langfuse/`)

#### LangfuseExporter (`exporter.py`)

**核心职责**：将供应商无关的 `RuntimeEvent` 映射为 Langfuse Trace/Span/Generation

**关键映射逻辑：**

| 事件类型 | Langfuse 操作 | span_key 规则 |
|----------|---------------|---------------|
| RUN_STARTED | `trace()` 创建 Trace | N/A |
| AGENT_LOOP_STARTED | `trace.span()` | `agent_loop:{caller}` |
| LLM_STARTED | `trace.generation()` | `llm:{caller_suffix}:{iteration}` |
| TOOL_BATCH_STARTED | `trace.span()` | `tools:{caller_suffix}:{iteration}` |
| TOOL_STARTED | `trace.span()` | `tool:{caller_suffix}:{iteration}:{tool_name}:{tool_call_id}` |
| *_FINISHED | `span.end()` / `generation.end()` | 同上 |
| *_FAILED | `span.end(level=ERROR)` | 同上 |

**Span 父子关系：**

```
agent_loop:main (parent)
├── llm:main:0          (parent=agent_loop:main)
├── tools:main:0        (parent=agent_loop:main)
│   ├── tool:main:0:read_file:call-1    (parent=tools:main:0)
│   └── tool:main:0:search:call-2       (parent=tools:main:0)
└── llm:main:1          (parent=agent_loop:main)
```

**Run 统计聚合：**

```python
{
    "started_at_ms": ...,
    "model": "...",
    "model_provider": "...",
    "llm_calls": 5,            # 自动计数
    "tool_calls_started": 8,   # 自动计数
    "tool_calls_finished": 7,  # 自动计数
    "main_iterations": 3,     # 取最大 iteration
    "usage": {                 # 自动汇总
        "prompt_tokens": 1234,
        "completion_tokens": 567,
        "total_tokens": 1801
    },
    "subagents": {             # 自动收集 subagent 血缘
        "sub-1": {"subagent_id": "sub-1", "task_id": ..., ...}
    }
}
```

**Trace ID 稳定性：**

使用 `uuid5(NAMESPACE_DNS, "{session_id}:{run_id}")` 生成确定性 trace ID，支持同一次逻辑 run 的幂等回放。

#### LangfuseTraceAdapter (`client.py`)

**核心职责**：封装 Langfuse SDK 调用、PII 脱敏、生命周期管理

**PII 脱敏规则：**

| 模式 | 正则 | 替换 |
|------|------|------|
| 手机号 | `1[3-9]\d{9}` | `1*** **** ****` |
| 身份证 | `\d{17}[\dXx]` | `*****************` |
| 邮箱 | `[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}` | `***@***.***` |
| URL | `https?://[^\s]+` | `[URL]` |
| IP | `(\d{1,3}\.){3}\d{1,3}` | `***.***.***.***` |
| MAC | `([0-9A-Fa-f]{2}[:-]){5}..` | `**:**:**:**:**:**` |

**敏感字段关键词** (key 匹配则 value 置为 `[REDACTED]`)：
`password, secret, token, key, api_key, access_token, secret_key, private_key, credential, certificate, phone, mobile, tel, email, id_card, identity, ssn, address, location, ip, mac`

**生命周期管理：**

```
create_trace() → _active_traces[trace_id] = {trace, spans: {}}
create_span/generation() → entry["spans"][span_id] = span
end()/update_tokens() → span.end(...) → _remove_span()
update_trace_output(terminal_status) → _mark_trace_terminal()
_release_trace_if_terminal_and_idle() → del _active_traces[trace_id]
```

### 3.7 提供者工厂 (`provider.py`)

```
get_observer()
  │
  ├─ _observer 已缓存 → 直接返回
  │
  └─ _observer = None → _build_observer()
       │
       ├─ load_system_config()["observability"]
       │   ├─ enabled=False → NOOP_OBSERVER
       │   └─ enabled=True
       │       ├─ provider="langfuse" → EXPORTER_REGISTRY["langfuse"]
       │   └─ provider=unknown → NOOP_OBSERVER
       │
       └─ exporter = create_langfuse_exporter(config)
           ├─ create_langfuse_client(config) → Langfuse SDK 实例
           │   ├─ 缺 public_key/secret_key/base_url → None → NOOP_OBSERVER
           │   └─ SDK 未安装 → None → NOOP_OBSERVER
           └─ adapter = LangfuseTraceAdapter(client)
               └─ LangfuseExporter(adapter)
                   └─ ObservabilityBus(exporter) ← 返回给运行时
```

---

## 4. 端到端流程

### 4.1 Agent Loop 路径追踪流程

```
用户请求 → Orchestrator.run()
            │
            ▼
     ┌── RunRuntimeTrace.started()
     │   emit: RUN_STARTED
     │   → Langfuse: create_trace(name="agent_request:{run_id}")
     │
     │   ... (setup phase) ...
     │
     ├── _stream_agent_loop()
     │   │
     │   ▼
     │   AgentLoopEngine.run()
     │   │
     │   ├── AgentRuntimeTrace.agent_loop_scope()
     │   │   emit: AGENT_LOOP_STARTED
     │   │   → Langfuse: create_span(name="agent_loop:main")
     │   │
     │   ├── [Iteration 0]
     │   │   │
     │   │   ├── runtime_trace.llm_call_scope(iteration=0)
     │   │   │   │   emit: LLM_STARTED
     │   │   │   │   → Langfuse: create_llm_span(name="llm:main_iteration")
     │   │   │   │   payload: {model, messages, tools, api_params, input}
     │   │   │   │
     │   │   │   │   ... LLM API call ...
     │   │   │   │
     │   │   │   └── llm_trace.finish(usage, completion_text, reasoning_text,
     │   │   │                      tool_calls, finish_reason, ...)
     │   │   │       emit: LLM_FINISHED
     │   │   │       → Langfuse: span.end(usage=..., output={
     │   │   │           content, reasoning_content, tool_calls,
     │   │   │           finish_reason, metrics
     │   │   │         })
     │   │   │
     │   │   ├── runtime_trace.tool_batch_scope(iteration=0)
     │   │   │   emit: TOOL_BATCH_STARTED
     │   │   │   → Langfuse: create_span(name="tools:main_iteration_0")
     │   │   │
     │   │   ├── [Tool: read_file, call-1]
     │   │   │   │   emit_tool_started(context, "read_file", "call-1", {path: ...})
     │   │   │   │   emit: TOOL_STARTED
     │   │   │   │   → Langfuse: create_span(name="tool:read_file",
     │   │   │   │       parent=tools:main:0)
     │   │   │   │
     │   │   │   │   ... tool execution ...
     │   │   │   │
     │   │   │   └── emit_tool_finished(context, "read_file", "call-1",
     │   │   │          status="completed", raw_output="...", elapsed_ms=12)
     │   │   │       emit: TOOL_FINISHED
     │   │   │       → Langfuse: span.end(output={status, output_preview,
     │   │   │           elapsed_ms, progress_events})
     │   │   │
     │   │   └── emit: TOOL_BATCH_FINISHED
     │   │       → Langfuse: span.end(status=success)
     │   │
     │   ├── [Iteration 1] (LLM call + tool batch ...) 同上
     │   │
     │   └── agent_loop_scope.__exit__()
     │       emit: AGENT_LOOP_FINISHED
     │       → Langfuse: span.end(status=success)
     │
     ├── RunRuntimeTrace.finished(final_answer, duration_ms)
     │   emit: RUN_FINISHED
     │   → Langfuse: trace.update(output={
     │       final_answer, status, duration_ms,
     │       llm_calls, tool_calls_started, tool_calls_finished,
     │       main_iterations, usage, subagents
     │   })
     │   → _mark_trace_terminal() → _release_trace_if_terminal_and_idle()
     │
     └── ... (cleanup: flush, close) ...
```

### 4.2 Direct Execution 路径追踪流程

Direct Execution 是 bypass AgentLoopEngine 的快捷执行路径，适用于 ontology 脚本操作。其 trace 生命周期由 **orchestrator 直接管理**，通过 `DirectExecutionRuntimeTrace` 发射完整的 span 层级：

```
用户请求 → Orchestrator.run()
            │
            ▼
     ┌── RunRuntimeTrace.started()                          ← 共享入口
     │   emit: RUN_STARTED
     │   → Langfuse: create_trace(name="agent_request:{run_id}")
     │
     ├── OperationDirectRouter.route()                      ← 路由决策，无 trace
     │   │   ┌─ route="direct_script"  → 直接执行脚本
     │   │   ├─ route="missing_args"    → 返回缺少参数提示
     │   │   └─ route="fallback_agent_loop" → 降级到 Agent Loop
     │
     ├── [direct_script 分支]
     │   │
     │   ├── DirectExecutionRuntimeTrace.start_loop()       ← 模拟 agent_loop span
     │   │   emit: AGENT_LOOP_STARTED
     │   │   → Langfuse: create_span(name="agent_loop:direct")
     │   │
     │   ├── DirectExecutionRuntimeTrace.start_llm()        ← 模拟 LLM span
     │   │   emit: LLM_STARTED
     │   │   → Langfuse: create_llm_span(name="llm:direct_iteration",
     │   │       parent="agent_loop:direct")
     │   │
     │   ├── DirectExecutionRuntimeTrace.start_tool_batch() ← 模拟 tool_batch span
     │   │   emit: TOOL_BATCH_STARTED
     │   │   → Langfuse: create_span(name="tools:direct:0",
     │   │       parent="agent_loop:direct")
     │   │
     │   ├── DirectExecutionRuntimeTrace.start_tool_exec()  ← 模拟 tool span
     │   │   emit: TOOL_STARTED
     │   │   → Langfuse: create_span(name="tool:execute_script:{script_name}",
     │   │       parent="tools:direct:0")
     │   │
     │   ├── run_direct_script()
     │   │   └── handle_execute_script_path()
     │   │       └── subprocess.run() / _spawn_with_args_file()
     │   │
     │   ├── DirectExecutionRuntimeTrace.finish_tool_exec() ← 结束 tool span
     │   │   emit: TOOL_FINISHED
     │   │   → Langfuse: span.end(output={status, output_preview, elapsed_ms})
     │   │
     │   ├── DirectExecutionRuntimeTrace.finish_tool_batch()
     │   │   emit: TOOL_BATCH_FINISHED
     │   │   → Langfuse: span.end(status=success)
     │   │
     │   ├── DirectExecutionRuntimeTrace.finish_llm()       ← 结束 LLM span
     │   │   emit: LLM_FINISHED
     │   │   → Langfuse: generation.end(usage, output={...})
     │   │
     │   ├── DirectExecutionRuntimeTrace.finish_loop()      ← 结束 agent_loop span
     │   │   emit: AGENT_LOOP_FINISHED
     │   │   → Langfuse: span.end(status=success)
     │   │
     │   └── RunRuntimeTrace.finished(final_answer, duration_ms)
     │       emit: RUN_FINISHED
     │       → Langfuse: trace.update(output={
     │           status, final_answer, direct_execution: true,
     │           elapsed_ms, llm_calls: 1, tool_calls_started: 1,
     │           tool_calls_finished: 1, main_iterations: 1
     │       })
     │       → _mark_trace_terminal() → _release_trace_if_terminal_and_idle()
     │
     └── [missing_args 分支]
         │
         ├── 构造缺少参数提示文本
         │
         └── RunRuntimeTrace.finished(final_answer, duration_ms=0)
             emit: RUN_FINISHED
             → Langfuse: trace.update(output={
                 status: "success",
                 final_answer: "当前 {operation} 流程需要以下参数：...",
                 direct_execution: true
             })
             → _mark_trace_terminal() → _release_trace_if_terminal_and_idle()

关键差异:
  - 使用 `DirectExecutionRuntimeTrace` 发射完整的 agent_loop → LLM → tool_batch → tool 事件链
  - 模拟的 LLM span 携带 "direct_execution/virtual" model 标记，iteration=0
  - tool span 名包含脚本名称（如 `tool:execute_script:供应商信息查询.py`），便于识别
  - missing_args 分支仅发射 RUN_STARTED → RUN_FINISHED，无内部 span
  - Langfuse 中 Direct Execution 的 trace 呈现与 Agent Loop 统一的层次结构
```

### 4.3 Subagent 追踪流程

```
Main Agent Loop (caller="main")
│
├── LLM 返回 tool_call → subagent 调度
│   │
│   ▼
│   SubAgent Manager
│   │
│   ▼
│   AgentLoopEngine.run(caller="subagent")
│   │
│   ├── AgentRuntimeTrace(caller="subagent",
│   │     lineage={subagent_id: "sub-1", task_id: ..., depth: 1, ...})
│   │
│   ├── agent_loop_scope()
│   │   emit: AGENT_LOOP_STARTED
│   │   span_key: "agent_loop:subagent:sub-1"
│   │
│   ├── llm_call_scope(iteration=0)
│   │   span_key: "llm:subagent:sub-1:0"
│   │   parent_key: "agent_loop:subagent:sub-1"
│   │
│   ├── tool_batch_scope(iteration=0)
│   │   span_key: "tools:subagent:sub-1:0"
│   │
│   └── [Tool: read_file, call-1]
│       emit_tool_started(context, ...)
│       _inject_subagent_lineage() → 自动填充 subagent_id, task_id, ...
│       span_key: "tool:subagent:sub-1:0:read_file:call-1"
│       parent_key: "tools:subagent:sub-1:0"
```

### 4.4 错误/取消/missing_args 流程

```
Agent Loop 路径:
  正常完成:     RUN_STARTED → ... → RUN_FINISHED  (status=success)
  LLM 失败:     RUN_STARTED → LLM_STARTED → LLM_FAILED → RUN_FAILED  (status=error)
  工具失败:     RUN_STARTED → TOOL_STARTED → TOOL_FINISHED(status=error) → ... → RUN_FINISHED
  用户取消:     RUN_STARTED → ... → RUN_CANCELLED  (status=cancelled)
  Agent 异常:   RUN_STARTED → AGENT_LOOP_STARTED → (异常) → AGENT_LOOP_FINISHED(error)
                → RUN_FAILED  (status=error, error_type, error_message)

Direct Execution 路径:
  脚本成功:     RUN_STARTED → (AGENT_LOOP → LLM → TOOL_BATCH → TOOL → ...) → RUN_FINISHED
               带完整的 DirectExecutionRuntimeTrace 层级
  缺少参数:     RUN_STARTED → RUN_FINISHED  (status=success, direct_execution=true,
                missing_arguments=[...])
  脚本错误:     RUN_STARTED → (AGENT_LOOP → LLM → TOOL_BATCH → TOOL → error) → RUN_FINISHED
               工具执行 error 状态通过 span.end(level=ERROR) 反映
  通用异常:     RUN_STARTED → RUN_FAILED  (status=error)
```

---

## 5. 配置体系

### 5.1 系统配置 (`system_config.json`)

```json
{
  "observability": {
    "enabled": true,
    "provider": "langfuse",
    "queue_size": 1000,
    "flush_interval_seconds": 5,
    "langfuse": {
      "public_key": "pk-...",
      "secret_key": "sk-...",
      "base_url": "https://cloud.langfuse.com",
      "debug": false
    }
  }
}
```

### 5.2 配置加载优先级

```
saved system_config.json > 环境变量 > 内置默认值
```

### 5.3 安全字段管理

- `clear_langfuse_secret_key` 字段用于 UI/API 清除已保存的密钥
- `langfuse.json` 已从仓库中移除公钥/密钥，迁移至 `backend/config/` 之外
- 支持 env 或 json 两种配置方式（`set langfuse in env or json`）

---

## 6. 关键设计模式

### 6.1 Context Manager 模式

所有生命周期通过 Python `with` 语句管理，确保 span 正确关闭：

```python
with runtime_trace.agent_loop_scope(agent_mode=..., deep_thinking_enabled=...):
    with runtime_trace.llm_call_scope(iteration=0, ...) as llm_trace:
        response = llm.call(...)
        llm_trace.finish(usage=..., completion_text=...)
    
    with runtime_trace.tool_batch_scope(iteration=0):
        ...
```

### 6.2 Lazy Evaluation

LLM 响应的 `prompt_text` / `completion_text` / `reasoning_text` 使用 lambda 延迟求值，避免在可观测性未启用时浪费计算：

```python
llm_trace.finish(
    prompt_text=lambda: "\n".join(str(m.get("content", "")) for m in messages),
    completion_text=lambda: response.content or "",
    reasoning_text=lambda: getattr(response, "reasoning_content", None) or "",
)
```

### 6.3 ObservabilityContext (contextvars)

通过 `bind_observability_context()` 将 observer/run_id/session_id 绑定到上下文变量，使后台回调（如 tool callback）能获取当前可观测上下文：

```python
with bind_observability_context(observer=..., run_id=..., session_id=..., turn_id=...):
    response = llm.call(...)
```

### 6.4 线程安全

- `ObservabilityBus`：queue 天然线程安全
- `LangfuseTraceAdapter._active_traces`：使用 `threading.Lock` 保护
- Trace flush：使用 `threading.Thread` 而非 `asyncio.run` 避免事件循环冲突

---

## 7. 提交演进时间线

| 阶段 | 日期 | Commit | 变更 | 设计意图 |
|------|------|--------|------|----------|
| 初始实现 | 05-12 | `4af8522` | 引入 Langfuse 追踪基础设施 | 建立端到端可观测能力 |
| 安全修复 | 05-13 | `864103b` | 移除 langfuse.json 中的公钥/密钥 | 防止凭据泄露 |
| 配置迁移 | 05-13 | `e7f2fe6` | 迁移 langfuse.json 到 backend/config/ | 规范配置路径 |
| 配置增强 | 05-13 | `2638d8c` | 支持 env 或 json 配置方式 | 灵活部署 |
| 质量修复 | 05-13 | `023612f` | 修复代码审查全部 P0-P2 问题 | 生产级质量 |
| 关联修复 | 05-14 | `f281b2b` | 修复 Run/session 关联问题 | 追踪数据准确性 |
| 异步优化 | 05-14 | `db2b3c1` | flush 改为异步 | 避免阻塞主线程 |
| 线程修复 | 05-14 | `4bb8e00` | 用 thread 替代 asyncio.run 做 flush | 避免事件循环冲突 |
| 架构解耦 | 05-15 | `fc4bd5c` | 将 Langfuse 追踪从 agent 运行时解耦 | 关注点分离 |
| 配置统一 | 05-15 | `8955763` | 统一使用系统配置管理 Langfuse | 单一配置来源 |
| 生命周期 | 05-16 | `6a3e2c4` | 收紧 Langfuse 生命周期与连接校验 | 防止资源泄露 |
| 日志降级 | 05-19 | `9b85663` | 缺失 SDK 时日志降级为 warning | 改善未安装 SDK 体验 |
| 功能增强 | 05-19 | `f0caaf8` | 丰富追踪细节 (LLM params/tool events/PII) | 生产级可观测质量 |
| 链路修复 | 05-19 | `51addb4` | 增强 LLM trace 处理，支持取消场景 | 完整生命周期闭环 |
| 观察者工厂 | 05-19 | `172bafa` | 进程级 Observer 提供者工厂 | 支持跨模块共享 Observer |
| 配置模式 | 05-22 | `ca3c546` | 重构可观测性配置为 mode 模式 | 支持本地日志 + Langfuse 双模式 |
| 本地日志 | 05-22 | `51b7385` | 新增本地可观测性日志保留 | 离线调试能力 |
| DF-0511 合并 | 05-26 | `8374102` | 合并 DF-0511 特性 (含 Direct Execution) | 引入直接脚本执行路径 |
| Direct Exec trace | 05-26 | `553a385` | Direct Execution 分支注入 run_trace.finished | 补齐 Direct Execution 的 trace 生命周期 |
| 虚拟 LLM span | 05-27 | `1a41bc1` | Direct Execution 支持虚拟 LLM Span + Token 统计 | 模拟 agent_loop 层级结构 |
| 脚本名输出 | 05-27 | `c535203` | trace output 增加脚本名称和 round 信息 | 提升 trace 可读性 |
| Tracer 抽取 | 05-27 | `e8cd82c` | 抽取 DirectExecutionTracer 降低 runner.py 复杂度 | 关注点分离 |
| Tracer 集成 | 05-27 | `b3fafcb` | 将 DirectExecutionTracer 集成到 observability.traces 模块 | 官方门面 |
| 单元测试 | 05-27 | `e5e7026` | 新增 DirectExecutionRuntimeTrace 全面单元测试 | 测试覆盖保障 |

---

## 8. 目录结构

```
backend/agentic/
├── observability/
│   ├── __init__.py                  # 公共 API 导出
│   ├── events.py                    # RuntimeEvent 契约定义
│   ├── observer.py                  # RuntimeObserver 协议 + NoopObserver
│   ├── bus.py                       # ObservabilityBus 异步队列
│   ├── provider.py                  # 进程级 Observer 工厂 (get_observer)
│   ├── context.py                   # ObservabilityContext (contextvars)
│   ├── traces/
│   │   ├── __init__.py              # 导出所有 Trace 门面
│   │   ├── agent.py                 # AgentRuntimeTrace 门面
│   │   ├── direct_execution.py      # DirectExecutionRuntimeTrace 门面 (main_trace 新增)
│   │   ├── llm.py                   # LLMRuntimeTrace 门面（独立 LLM 调用）
│   │   └── run.py                   # RunRuntimeTrace 门面
│   └── exporters/
│       ├── __init__.py              # EXPORTER_REGISTRY 注册表
│       ├── base.py                  # RuntimeExporter 协议
│       └── langfuse/
│           ├── __init__.py
│           ├── exporter.py          # LangfuseExporter 事件映射
│           └── client.py           # LangfuseTraceAdapter SDK 适配
├── tools/
│   └── events.py                    # emit_tool_started/finished
├── loop/
│   └── engine.py                    # AgentLoopEngine (LLM/工具追踪入口)
├── orchestrator.py                  # AgenticOrchestrator (Run 追踪入口)
└── config/
    └── system_config.py             # 系统配置 (含 observability section)
```

---

## 9. Langfuse 平台上的数据视图

### 9.1 Agent Loop 路径（多轮 LLM + 工具调用）

在 Langfuse UI 中，一次典型的 agent run 呈现如下层级：

```
Trace: agent_request:{run_id}
│
├── Input:  {user_query, session_id, model, ...}
├── Output: {final_answer, status, duration_ms, llm_calls, usage, subagents, ...}
├── Metadata: {session_id, turn_id, workspace_id, model_provider, protocol, ...}
├── Tags: [agentic_chat, default, run:{run_id}, session:{session_id}, stream]
│
├── Span: agent_loop:main
│   ├── Metadata: {caller: main, agent_mode, deep_thinking_enabled, ...}
│   │
│   ├── Generation: llm:main_iteration
│   │   ├── Model: deepseek-chat
│   │   ├── Input:  {messages, tools, api_params}
│   │   ├── Output: {content, reasoning_content, tool_calls, finish_reason}
│   │   └── Usage:  {prompt_tokens: 1234, completion_tokens: 567, total_tokens: 1801}
│   │
│   ├── Span: tools:main_iteration_0
│   │   ├── Span: tool:read_file:call-1
│   │   │   ├── Input:  {path: "***@***.***"}    ← PII 已脱敏
│   │   │   └── Output: {status: completed, output_preview: "...", elapsed_ms: 12}
│   │   │
│   │   └── Span: tool:business_data_search:call-2
│   │       ├── Input:  {query: "..."}
│   │       └── Output: {status: completed, results_count: 5, elapsed_ms: 340}
│   │
│   ├── Generation: llm:main_iteration (第 2 轮)
│   │   ...
│   │
│   └── Span: agent_loop:subagent:sub-1
│       ├── Generation: llm:subagent_iteration
│       └── Span: tools:subagent:sub-1:3
│           └── Span: tool:read_file:call-1
│
└── [Session: session:{session_id}]  ← 跨 run 的会话关联
```

### 9.2 Direct Execution 路径（直接脚本执行）

在 Langfuse UI 中，direct execution 的 trace 与 Agent Loop 呈现**统一的层级结构**，差异在于 LLM 使用虚拟 model 标记：

```
Trace: agent_request:{run_id}
│
├── Input:  {user_query, session_id, direct_execution: true, ...}
├── Output: {
│     status: "success",
│     final_answer: "[Direct Execution] 查询完成，共 5 条记录...",
│     direct_execution: true,
│     elapsed_ms: 1234,
│     llm_calls: 1,
│     tool_calls_started: 1,
│     tool_calls_finished: 1,
│     main_iterations: 1,
│     usage: {prompt_tokens: ~, completion_tokens: ~, total_tokens: ~}
│   }
├── Metadata: {session_id, turn_id, workspace_id, request_id, ...}
├── Tags: [agentic_chat, default, run:{run_id}, session:{session_id}, stream]
│
├── Span: agent_loop:direct
│   ├── Metadata: {caller: direct, agent_mode: direct, execution_path: direct_execution,
│   │             ontology_id, operation_key, ...}
│   │
│   ├── Generation: llm:direct_iteration (虚拟路由阶段)
│   │   ├── Model: "direct_execution/virtual"
│   │   ├── Input:  {messages: [{role: user, content: "本体路由决策..."}], tools: []}
│   │   ├── Output: {content, status: success}
│   │   └── Usage:  {prompt_tokens, completion_tokens, total_tokens} (估计值)
│   │
│   └── Span: tools:direct:0
│       └── Span: tool:execute_script:供应商信息查询.py
│           ├── Input:  {ontology_id, operation_key, arguments: {...}}
│           └── Output: {status: completed, output_preview: "...", elapsed_ms: 1234}
│
└── [Session: session:{session_id}]

注：missing_args 分支的 trace 无上述 Span 层级，仅有 Trace 层面的 output。
```

---

## 10. 总结

Onto-Agent 的 Langfuse 可观测性系统经历了从初始实现到生产级的完整演进，核心架构特点：

1. **四层解耦**：运行时代码 → Trace Facade → 供应商无关事件 → Langfuse Exporter，每层职责清晰
2. **非阻塞设计**：ObservabilityBus 异步队列 + daemon thread，`emit()` 不影响 agent 性能
3. **安全优先**：PII 脱敏 + 敏感字段 REDACTED + 凭据从代码中移除
4. **全链路覆盖**：Run → Agent Loop → LLM Call → Tool Batch → Individual Tool，含 subagent 血缘
5. **双路径统一打点**：Agent Loop 和 Direct Execution 从统一的 `RunRuntimeTrace.started()` 入口开始，通过 `DirectExecutionRuntimeTrace` 在 Direct Execution 路径中模拟完整的 span 层级，确保 Langfuse 中所有 run 都有统一的生命周期结构
6. **Direct Execution 虚拟链路**：`DirectExecutionRuntimeTrace` 虚拟发射 `AGENT_LOOP → LLM → TOOL_BATCH → TOOL` 事件链，使两条路径在 Langfuse 中呈现一致的层次结构，LLM 使用 `direct_execution/virtual` model 标记
7. **优雅降级**：SDK 未安装/配置缺失 → NoopObserver，运行时零影响
8. **生命周期严谨**：terminal + idle → 释放 trace，flush 使用独立线程，atexit 注册 shutdown
