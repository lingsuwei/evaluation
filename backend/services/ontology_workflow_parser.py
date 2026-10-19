"""本体包工作流解析器

解析本体包 markdown 文件为结构化工作流对象，用于证据集评分。

本体包结构：
    ontology_dir/
    ├── operations-list.md       # 操作清单表格：名称 | 标签 | 业务描述
    ├── operations/
    │   ├── 临机云方案构建.md
    │   ├── 任务要素提取.md
    │   └── ...
    └── objects-list.md          # 实体清单表格：中文名称 | 别名 | 业务定义 | 领域分类
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from logger import log


@dataclass
class StepDetail:
    """操作内的子步骤（来自 #### 步骤N：... 小节）"""
    order: int
    name: str
    references: List[str]  # 该子步骤内的 [[wikilink]] 引用


@dataclass
class Operation:
    """单个操作（来自 operations/{name}.md）"""
    name: str
    tag: str              # "主流程" | "logic" | "配置表" 等
    description: str
    inputs: List[str]     # 来自 ## 与object的关联 → 输入数据的对象
    outputs: List[str]    # 来自 ## 与object的关联 → 输出数据的对象
    references: List[str] # 文件内所有 [[wikilink]] 引用
    steps: List[StepDetail]  # 来自 #### 步骤N：... 小节


@dataclass
class OntologyObject:
    """业务实体（来自 objects-list.md）"""
    name: str
    alias: str
    definition: str
    domain: str


@dataclass
class OntologyWorkflow:
    """完整工作流结构"""
    operations: Dict[str, Operation]      # name -> Operation
    objects: Dict[str, OntologyObject]    # name -> OntologyObject
    main_flows: List[str]                 # tag="主流程" 的操作名列表
    dependency_graph: Dict[str, List[str]]  # 操作名 -> [[引用]] 列表


# 正则表达式
_WIKILINK_RE = re.compile(r'\[\[([^\]]+)\]\]')
_HEADING_RE = re.compile(r'^(#{1,4})\s+(.+?)$', re.MULTILINE)


def _extract_wikilinks(text: str) -> List[str]:
    """提取文本中所有 [[wikilink]] 引用"""
    return _WIKILINK_RE.findall(text)


def _parse_markdown_table(content: str) -> List[List[str]]:
    """解析 markdown 表格，返回行列表（每行是单元格列表）

    格式：
    | 列1 | 列2 | 列3 |
    | --- | --- | --- |
    | 值1 | 值2 | 值3 |
    """
    rows = []
    lines = content.split('\n')
    in_table = False

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # 跳过分隔行
        if re.match(r'^\|[\s\-\|:]+\|$', line):
            in_table = True
            continue

        # 检测表格行
        if line.startswith('|') and line.endswith('|'):
            if not in_table:
                continue  # 跳过表头行
            cells = [c.strip() for c in line[1:-1].split('|')]
            rows.append(cells)

    return rows


def _parse_operations_list(file_path: Path) -> List[Tuple[str, str, str]]:
    """解析 operations-list.md 表格

    格式：名称 | 标签 | 业务描述

    Returns: [(name, tag, description), ...]
    """
    if not file_path.exists():
        log("WARNING", "ONTOLOGY_PARSER", f"File not found: {file_path}")
        return []

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    rows = _parse_markdown_table(content)
    results = []

    for row in rows:
        if len(row) >= 3:
            name = row[0].strip()
            tag = row[1].strip()
            description = row[2].strip()
            if name:
                results.append((name, tag, description))

    return results


def _parse_objects_list(file_path: Path) -> Dict[str, OntologyObject]:
    """解析 objects-list.md 表格

    格式：中文名称 | 别名 | 业务定义 | 领域分类

    Returns: {name: OntologyObject}
    """
    if not file_path.exists():
        log("WARNING", "ONTOLOGY_PARSER", f"File not found: {file_path}")
        return {}

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    rows = _parse_markdown_table(content)
    objects = {}

    for row in rows:
        if len(row) >= 4:
            name = row[0].strip()
            alias = row[1].strip()
            definition = row[2].strip()
            domain = row[3].strip()
            if name:
                objects[name] = OntologyObject(
                    name=name,
                    alias=alias,
                    definition=definition,
                    domain=domain,
                )

    return objects


def _parse_section(content: str, heading: str) -> Optional[str]:
    """提取指定标题下的内容（直到下一个同级或更高级标题）"""
    # 查找标题位置
    pattern = rf'^(#+)\s+{re.escape(heading)}\s*$'
    match = re.search(pattern, content, re.MULTILINE | re.IGNORECASE)
    if not match:
        return None

    level = len(match.group(1))
    start = match.end()

    # 查找下一个同级或更高级标题
    next_heading_pattern = rf'^#{{1,{level}}}\s+'
    next_match = re.search(next_heading_pattern, content[start:], re.MULTILINE)
    if next_match:
        end = start + next_match.start()
    else:
        end = len(content)

    return content[start:end].strip()


def _parse_operation_file(file_path: Path, tag: str, description: str) -> Operation:
    """解析单个操作 .md 文件

    提取：
    - H1 标题作为操作名
    - ## 与logic的关联 → [[references]]
    - ## 与object的关联 → 输入/输出对象
    - ## 输入 / ## 输出 表格
    - ### 执行逻辑 → #### 步骤N：... → 子步骤
    """
    if not file_path.exists():
        log("WARNING", "ONTOLOGY_PARSER", f"File not found: {file_path}")
        return Operation(
            name=file_path.stem,
            tag=tag,
            description=description,
            inputs=[],
            outputs=[],
            references=[],
            steps=[],
        )

    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()

    # 提取 H1 标题
    h1_match = re.search(r'^#\s+(.+?)$', content, re.MULTILINE)
    name = h1_match.group(1).strip() if h1_match else file_path.stem

    # 提取所有 [[wikilink]] 引用
    all_references = _extract_wikilinks(content)

    # 解析输入/输出对象
    inputs = []
    outputs = []

    object_section = _parse_section(content, '与object的关联')
    if object_section:
        # 提取输入数据的对象
        input_match = re.search(r'输入数据的对象[：:]?\s*(.+?)(?:\n|$)', object_section)
        if input_match:
            input_text = input_match.group(1)
            inputs = [obj.strip() for obj in _extract_wikilinks(input_text)]
            if not inputs:
                # 尝试解析逗号/顿号分隔的列表
                inputs = [obj.strip() for obj in re.split(r'[,，、]', input_text) if obj.strip()]

        # 提取输出数据的对象
        output_match = re.search(r'输出数据的对象[：:]?\s*(.+?)(?:\n|$)', object_section)
        if output_match:
            output_text = output_match.group(1)
            outputs = [obj.strip() for obj in _extract_wikilinks(output_text)]
            if not outputs:
                outputs = [obj.strip() for obj in re.split(r'[,，、]', output_text) if obj.strip()]

    # 解析子步骤
    steps = []
    logic_section = _parse_section(content, '执行逻辑')
    if logic_section:
        # 查找 #### 步骤N：... 或 #### 步骤N: ...
        step_pattern = r'^#{4}\s+步骤\s*(\d+)[：:]\s*(.+?)(?:\n|$)'
        step_matches = list(re.finditer(step_pattern, logic_section, re.MULTILINE))

        for i, match in enumerate(step_matches):
            order = int(match.group(1))
            step_name = match.group(2).strip()

            # 提取该步骤的内容（直到下一个步骤或章节结束）
            step_start = match.end()
            if i + 1 < len(step_matches):
                step_end = step_matches[i + 1].start()
            else:
                step_end = len(logic_section)

            step_content = logic_section[step_start:step_end]
            step_refs = _extract_wikilinks(step_content)

            steps.append(StepDetail(
                order=order,
                name=step_name,
                references=step_refs,
            ))

    return Operation(
        name=name,
        tag=tag,
        description=description,
        inputs=inputs,
        outputs=outputs,
        references=all_references,
        steps=steps,
    )


def parse_ontology_workflow(ontology_dir: Path) -> Optional[OntologyWorkflow]:
    """解析本体包目录为结构化工作流

    Args:
        ontology_dir: 本体包目录路径（如 md/command-driven-tasking-formation/）

    Returns:
        OntologyWorkflow 对象，解析失败返回 None（graceful fallback）
    """
    try:
        if not ontology_dir.exists():
            log("WARNING", "ONTOLOGY_PARSER", f"Directory not found: {ontology_dir}")
            return None

        operations_list_file = ontology_dir / "operations-list.md"
        operations_dir = ontology_dir / "operations"
        objects_list_file = ontology_dir / "objects-list.md"

        # 解析操作清单
        operations = {}
        main_flows = []

        if operations_list_file.exists():
            ops_list = _parse_operations_list(operations_list_file)
            log("INFO", "ONTOLOGY_PARSER", f"Parsed operations list",
                count=len(ops_list), file=str(operations_list_file))

            for op_name, tag, description in ops_list:
                op_file = operations_dir / f"{op_name}.md"
                if op_file.exists():
                    operation = _parse_operation_file(op_file, tag, description)
                    operations[op_name] = operation
                    if tag == "主流程":
                        main_flows.append(op_name)
                else:
                    # 操作在清单中但文件不存在，创建占位符
                    operations[op_name] = Operation(
                        name=op_name,
                        tag=tag,
                        description=description,
                        inputs=[],
                        outputs=[],
                        references=_extract_wikilinks(description),
                        steps=[],
                    )
                    log("WARNING", "ONTOLOGY_PARSER",
                        f"Operation file not found, using placeholder",
                        operation=op_name, expected_path=str(op_file))
        else:
            log("WARNING", "ONTOLOGY_PARSER",
                "operations-list.md not found, scanning operations/ directory")
            # 降级：直接扫描 operations/ 目录
            if operations_dir.exists():
                for op_file in operations_dir.glob("*.md"):
                    op_name = op_file.stem
                    operation = _parse_operation_file(op_file, "logic", "")
                    operations[op_name] = operation

        # 解析实体清单
        objects = {}
        if objects_list_file.exists():
            objects = _parse_objects_list(objects_list_file)
            log("INFO", "ONTOLOGY_PARSER", f"Parsed objects list",
                count=len(objects), file=str(objects_list_file))

        # 构建依赖图
        dependency_graph = {
            op_name: op.references
            for op_name, op in operations.items()
        }

        workflow = OntologyWorkflow(
            operations=operations,
            objects=objects,
            main_flows=main_flows,
            dependency_graph=dependency_graph,
        )

        log("INFO", "ONTOLOGY_PARSER", "Successfully parsed ontology workflow",
            ontology_dir=str(ontology_dir),
            operations_count=len(operations),
            objects_count=len(objects),
            main_flows=main_flows)

        return workflow

    except Exception as e:
        log("ERROR", "ONTOLOGY_PARSER", f"Failed to parse ontology workflow: {e}",
            ontology_dir=str(ontology_dir))
        return None
