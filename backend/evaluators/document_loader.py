"""
文档加载器 - 用于加载47个markdown文档作为BM25评估的参考文档库
"""
import os
import re
from typing import List, Dict
from logger import log


class MarkdownDocumentLoader:
    """加载和管理markdown文档库"""

    def __init__(self, docs_dir: str = None):
        """
        初始化文档加载器

        Args:
            docs_dir: markdown文档目录路径，默认为项目根目录下的 md 文件夹
        """
        if docs_dir is None:
            # 默认使用项目根目录下的 md 文件夹
            project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            docs_dir = os.path.join(project_root, "md")

        self.docs_dir = docs_dir
        self.documents: List[Dict[str, str]] = []
        self._load_documents()

    def _load_documents(self):
        """加载目录下所有的markdown文档（包括子文件夹）"""
        try:
            if not os.path.exists(self.docs_dir):
                log("WARN", "DOC_LOADER", f"文档库目录不存在: {self.docs_dir}")
                return

            md_files = []
            # 递归查找所有 .md 文件
            for root, dirs, files in os.walk(self.docs_dir):
                for file in files:
                    if file.endswith('.md') and not file.startswith('~'):  # 排除临时文件
                        md_files.append(os.path.join(root, file))
            # 按完整路径排序:消除 os.walk 文件系统遍历顺序的不确定性,
            # 保证文档加载顺序确定(评估稳定性:检索/截断/拼接跨轮跨进程一致)
            md_files.sort()

            log("INFO", "DOC_LOADER", f"开始加载文档库，目录: {self.docs_dir}，发现 {len(md_files)} 个文档")

            for md_file in md_files:
                try:
                    with open(md_file, 'r', encoding='utf-8') as f:
                        content = f.read()

                    # 提取标题（第一个#开头的行）
                    title = self._extract_title(content)
                    if not title:
                        title = os.path.basename(md_file)

                    # 清理内容（移除markdown标记）
                    clean_content = self._clean_markdown(content)

                    if clean_content.strip():
                        self.documents.append({
                            'title': title,
                            'content': clean_content,
                            'file_path': md_file
                        })

                except Exception as e:
                    log("ERROR", "DOC_LOADER", f"加载文档失败: {md_file}, 错误: {str(e)}")

            log("INFO", "DOC_LOADER",
                f"文档库加载完成，共加载 {len(self.documents)} 个文档",
                doc_count=len(self.documents))

        except Exception as e:
            log("ERROR", "DOC_LOADER", f"初始化文档加载器失败: {str(e)}")

    def _extract_title(self, content: str) -> str:
        """从markdown内容中提取标题"""
        # 匹配第一个一级标题
        match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
        if match:
            return match.group(1).strip()

        # 匹配第一个二级标题
        match = re.search(r'^##\s+(.+)$', content, re.MULTILINE)
        if match:
            return match.group(1).strip()

        return ""

    def _clean_markdown(self, content: str) -> str:
        """清理markdown标记，保留纯文本"""
        # 移除markdown标题标记
        content = re.sub(r'^#+\s+', '', content, flags=re.MULTILINE)

        # 移除代码块标记
        content = re.sub(r'```[\s\S]*?```', '', content, flags=re.MULTILINE)
        content = re.sub(r'`[^`]+`', '', content, flags=re.MULTILINE)

        # 移除链接标记 [text](url) -> text
        content = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', content)

        # 移除图片标记
        content = re.sub(r'!\[([^\]]*)\]\([^)]+\)', '', content, flags=re.MULTILINE)

        # 移除加粗和斜体标记
        content = re.sub(r'\*\*([^*]+)\*\*', r'\1', content)
        content = re.sub(r'\*([^*]+)\*', r'\1', content)
        content = re.sub(r'__([^_]+)__', r'\1', content)
        content = re.sub(r'_([^_]+)_', r'\1', content)

        # 移除水平线
        content = re.sub(r'^-{3,}$', '', content, flags=re.MULTILINE)
        content = re.sub(r'^\*{3,}$', '', content, flags=re.MULTILINE)

        # 移除过多的空行
        content = re.sub(r'\n{3,}', '\n\n', content)

        return content.strip()

    def get_all_documents(self) -> List[str]:
        """获取所有文档的内容列表"""
        return [doc['content'] for doc in self.documents]

    def get_documents_with_titles(self) -> List[Dict[str, str]]:
        """获取带标题的文档列表"""
        return self.documents

    def get_document_by_keyword(self, keyword: str) -> str:
        """根据关键词查找相关文档"""
        results = []
        keyword_lower = keyword.lower()

        for doc in self.documents:
            # 在标题和内容中搜索关键词
            if keyword_lower in doc['title'].lower() or keyword_lower in doc['content'].lower():
                results.append(f"{doc['title']}\n{doc['content']}")

        if results:
            return '\n\n'.join(results)
        return ""

    def get_combined_content(self) -> str:
        """获取所有文档的合并内容"""
        return '\n\n'.join([doc['content'] for doc in self.documents])


# 全局单例
_doc_loader_instance = None
_doc_loader_docs_dir = None


def get_document_loader(docs_dir: str = None) -> MarkdownDocumentLoader:
    """获取文档加载器单例。
    
    Args:
        docs_dir: 指定文档目录。如果与上次不同则重新加载。
    """
    global _doc_loader_instance, _doc_loader_docs_dir
    if docs_dir is not None and docs_dir != _doc_loader_docs_dir:
        _doc_loader_instance = MarkdownDocumentLoader(docs_dir=docs_dir)
        _doc_loader_docs_dir = docs_dir
    if _doc_loader_instance is None:
        _doc_loader_instance = MarkdownDocumentLoader()
        _doc_loader_docs_dir = None
    return _doc_loader_instance
