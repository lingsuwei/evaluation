import { useState, useEffect, useRef, Fragment } from 'react';
import axios from 'axios';
import { Tooltip } from './Tooltip';
import { formatDate } from '../utils/dateUtils';

const API_BASE = '/api';

interface Dataset {
  id: string;
  name: string;
  item_count: number;
  created_at: string;
}

interface DatasetDetail {
  id: string;
  name: string;
  item_count: number;
  created_at: string;
  data: any[];
}

interface Props {
  onUploadSuccess?: (datasetId: string) => void;
  agentId?: string | null;
}

export function DatasetManager({ onUploadSuccess, agentId }: Props) {
  
  // 数据集列表和管理状态
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [loadingDatasets, setLoadingDatasets] = useState(false);
  const [selectedDatasets, setSelectedDatasets] = useState<string[]>([]);
  const [deleting, setDeleting] = useState(false);
  const [copying, setCopying] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState('');
  const [editingDataset, setEditingDataset] = useState<DatasetDetail | null>(null);
  const [editData, setEditData] = useState<Array<Record<string, any>>>([]);
  const [showModal, setShowModal] = useState(false);
  const [modalMessage, setModalMessage] = useState('');
  const [modalType, setModalType] = useState<'confirm' | 'success' | 'error'>('confirm');
  const confirmActionRef = useRef<(() => Promise<void>) | null>(null);
  
  // 评估相关状态
  const [currentDatasetId, setCurrentDatasetId] = useState<string | null>(null);
  const [datasetDetail, setDatasetDetail] = useState<DatasetDetail | null>(null);
  const [loadingDataset, setLoadingDataset] = useState(false);
  const [uploading, setUploading] = useState(false);
  
  // 编辑项目相关状态
  const [editingItemIndex, setEditingItemIndex] = useState<number | null>(null);
  const [editingItem, setEditingItem] = useState<any>(null);
  const [showItemEditModal, setShowItemEditModal] = useState(false);
  
  // 新增项目相关状态
  const [showAddItemModal, setShowAddItemModal] = useState(false);
  const [newItem, setNewItem] = useState<any>({
    question: '',
    prompt: '',
    ground_truth: '',
    model_answer: '',
    judging_mode: 'Ground_Truth评测',
  });

  // 新增评测集相关状态
  const [showNewDatasetModal, setShowNewDatasetModal] = useState(false);
  const [newDatasetName, setNewDatasetName] = useState<string>('');
  const [newDatasetFile, setNewDatasetFile] = useState<File | null>(null);
  const [newDatasetError, setNewDatasetError] = useState<string | null>(null);
  const newDatasetFileRef = useRef<HTMLInputElement>(null);
  const [exportMenuOpenId, setExportMenuOpenId] = useState<string | null>(null);
  const [exportPreview, setExportPreview] = useState<{
    datasetId: string;
    datasetName: string;
    itemCount: number;
    createdAt: string;
    format: 'json' | 'csv' | 'xlsx';
    itemsWithSteps: number;
    enabledStepTotal: number;
    disabledStepTotal: number;
    keywordStepTotal: number;
    llmStepTotal: number;
    perItem: { question: string; enabled: number; total: number }[];
    loading: boolean;
    error: string;
  } | null>(null);

  const [showLangfuseImportModal, setShowLangfuseImportModal] = useState(false);
  const [langfuseProjectId, setLangfuseProjectId] = useState('');
  const [langfuseProjects, setLangfuseProjects] = useState<{ id: string; name: string; organization: string | null; langfuse_project_id: string | null }[]>([]);
  const [langfuseDatasets, setLangfuseDatasets] = useState<{name: string, id?: string, no_name?: boolean, can_import?: boolean, item_count?: number}[]>([]);
  const [langfuseLoadingList, setLangfuseLoadingList] = useState(false);
  const [langfuseImportLoading, setLangfuseImportLoading] = useState(false);
  const [langfuseStep, setLangfuseStep] = useState<1|2|3>(1);
  const [langfuseSelectedDatasets, setLangfuseSelectedDatasets] = useState<string[]>([]);
  const [langfusePreviewItems, setLangfusePreviewItems] = useState<any[]>([]);
  const [langfusePreviewWarnings, setLangfusePreviewWarnings] = useState<string[]>([]);
  const [langfusePreviewLoading, setLangfusePreviewLoading] = useState(false);
  const [langfuseMergeName, setLangfuseMergeName] = useState('');
  const [langfuseNameConflict, setLangfuseNameConflict] = useState(false);
  const [langfuseDragIdx, setLangfuseDragIdx] = useState<number | null>(null);
  
  // 折叠状态
  const [collapsedRows, setCollapsedRows] = useState<Set<number>>(new Set());

  // 就地展开详情行的 ref（用于 scrollIntoView）
  const detailRowRef = useRef<HTMLTableRowElement>(null);
  
  // 评估相关状态

  // 加载数据集列表
  const fetchDatasets = async () => {
    setLoadingDatasets(true);
    try {
      const res = await axios.get(`${API_BASE}/datasets`);
      setDatasets(res.data.datasets);
    } catch (err) {
      console.error('获取数据集列表失败:', err);
    } finally {
      setLoadingDatasets(false);
    }
  };

  useEffect(() => {
    fetchDatasets();
  }, []);

  useEffect(() => {
    if (!exportMenuOpenId) return;
    const onDocClick = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (target.closest('.dataset-export-menu')) return;
      setExportMenuOpenId(null);
    };
    document.addEventListener('mousedown', onDocClick);
    return () => document.removeEventListener('mousedown', onDocClick);
  }, [exportMenuOpenId]);

  // 加载数据集详情
  useEffect(() => {
    const fetchDatasetDetail = async () => {
      if (!currentDatasetId) {
        setDatasetDetail(null);
        setCollapsedRows(new Set());
        return;
      }
      setLoadingDataset(true);
      try {
        const res = await axios.get(`${API_BASE}/datasets/${currentDatasetId}`);
        setDatasetDetail(res.data);
        // 默认折叠所有行
        if (res.data && res.data.data) {
          setCollapsedRows(new Set(res.data.data.map((_: any, index: number) => index)));
        }
      } catch (err: any) {
        console.error('获取数据集详情失败:', err);
      } finally {
        setLoadingDataset(false);
      }
    };
    fetchDatasetDetail();
  }, [currentDatasetId]);

  // ========== 管理功能 ==========
  const filteredDatasets = datasets.filter(dataset =>
    dataset.name.toLowerCase().includes(searchQuery.toLowerCase()) ||
    dataset.id.toLowerCase().includes(searchQuery.toLowerCase())
  );

  const toggleDatasetSelection = (datasetId: string) => {
    setSelectedDatasets(prev => {
      if (prev.includes(datasetId)) {
        return prev.filter(id => id !== datasetId);
      } else {
        return [...prev, datasetId];
      }
    });
  };

  const toggleSelectAll = () => {
    if (selectedDatasets.length === filteredDatasets.length) {
      setSelectedDatasets([]);
    } else {
      setSelectedDatasets(filteredDatasets.map(d => d.id));
    }
  };

  const handleModalClose = () => {
    setShowModal(false);
    confirmActionRef.current = null;
  };

  const handleModalConfirm = async () => {
    if (confirmActionRef.current) {
      await confirmActionRef.current();
    }
  };

  const handleDeleteDataset = (datasetId: string) => {
    setModalMessage(`确定要删除数据集 ${datasetId} 吗？此操作不可恢复。`);
    setModalType('confirm');
    setShowModal(true);
    confirmActionRef.current = async () => {
      setDeleting(true);
      try {
        await axios.delete(`${API_BASE}/datasets/${datasetId}`);
        setModalMessage('删除成功');
        setModalType('success');
        if (currentDatasetId === datasetId) setCurrentDatasetId(null);
        await fetchDatasets();
      } catch (err: any) {
        setModalMessage(`删除失败: ${err.response?.data?.detail || '未知错误'}`);
        setModalType('error');
      } finally {
        setDeleting(false);
        confirmActionRef.current = null;
      }
    };
  };

  const handleCopyDataset = async (datasetId: string) => {
    setCopying(datasetId);
    try {
      const res = await axios.post(`${API_BASE}/datasets/${datasetId}/copy`);
      setModalMessage(`复制成功！新数据集ID: ${res.data.id}`);
      setModalType('success');
      setShowModal(true);
      await fetchDatasets();
    } catch (err: any) {
      setModalMessage(`复制失败: ${err.response?.data?.detail || '未知错误'}`);
      setModalType('error');
      setShowModal(true);
    } finally {
      setCopying(null);
    }
  };

  const handleBatchDelete = async () => {
    if (selectedDatasets.length === 0) {
      alert('请先选择要删除的数据集');
      return;
    }

    setModalMessage(`确定要删除选中的 ${selectedDatasets.length} 个数据集吗？此操作不可恢复。`);
    setModalType('confirm');
    confirmActionRef.current = async () => {
      setDeleting(true);
      try {
        await axios.delete(`${API_BASE}/datasets/batch`, {
          data: { dataset_ids: selectedDatasets }
        });
        setModalMessage(`成功删除 ${selectedDatasets.length} 个数据集`);
        setModalType('success');
        setSelectedDatasets([]);
        await fetchDatasets();
      } catch (err: any) {
        setModalMessage(`删除失败: ${err.response?.data?.detail || '未知错误'}`);
        setModalType('error');
      } finally {
        setDeleting(false);
        confirmActionRef.current = null;
      }
    };
    setShowModal(true);
  };

  const handleEditDataset = async (datasetId: string) => {
    try {
      const res = await axios.get(`${API_BASE}/datasets/${datasetId}`);
      setEditingDataset(res.data);
      setEditData(res.data.data || []);
    } catch (err: any) {
      alert(`获取数据集详情失败: ${err.response?.data?.detail || '未知错误'}`);
    }
  };

  const handleSaveDataset = async () => {
    if (!editingDataset) return;
    try {
      await axios.put(`${API_BASE}/datasets/${editingDataset.id}`, {
        data: editData,
        name: editingDataset.name
      });
      setModalMessage('保存成功');
      setModalType('success');
      setShowModal(true);
      setEditingDataset(null);
      setEditData([]);
      await fetchDatasets();
    } catch (err: any) {
      setModalMessage(`保存失败: ${err.response?.data?.detail || '未知错误'}`);
      setModalType('error');
      setShowModal(true);
    }
  };

  const handleSelectDataset = (datasetId: string) => {
    // 再次点击同一行收起
    setCurrentDatasetId(prev => (prev === datasetId ? null : datasetId));
  };

  // 展开后自动滚动到可视区中央，避免列表超一屏时详情不在可视范围
  useEffect(() => {
    if (currentDatasetId && detailRowRef.current) {
      detailRowRef.current.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  }, [currentDatasetId]);

  // 编辑项目相关函数
  const handleEditItem = (displayIndex: number) => {
    if (datasetDetail) {
      const actualIndex = datasetDetail.data.length - 1 - displayIndex;
      const item = datasetDetail.data[actualIndex];
      setEditingItemIndex(actualIndex);
      setEditingItem({ ...item });
      setShowItemEditModal(true);
    }
  };

  const handleSaveItem = async () => {
    if (!datasetDetail || editingItemIndex === null) return;
    
    try {
      const updatedData = [...datasetDetail.data];
      updatedData[editingItemIndex] = editingItem;
      
      await axios.put(`${API_BASE}/datasets/${datasetDetail.id}`, {
        data: updatedData,
        name: datasetDetail.name
      });
      
      setModalMessage('保存成功');
      setModalType('success');
      setShowModal(true);
      
      // 重新加载数据集详情
      const res = await axios.get(`${API_BASE}/datasets/${datasetDetail.id}`);
      setDatasetDetail(res.data);
      
      setShowItemEditModal(false);
      setEditingItemIndex(null);
      setEditingItem(null);
    } catch (err: any) {
      setModalMessage(`保存失败: ${err.response?.data?.detail || '未知错误'}`);
      setModalType('error');
      setShowModal(true);
    }
  };

  const handleCancelItemEdit = () => {
    setShowItemEditModal(false);
    setEditingItemIndex(null);
    setEditingItem(null);
  };

  // 删除项目相关函数
  const handleDeleteItem = (displayIndex: number) => {
    const actualIndex = datasetDetail ? datasetDetail.data.length - 1 - displayIndex : displayIndex;
    setModalMessage(`确定要删除第 ${displayIndex + 1} 条评测记录吗？此操作不可恢复。`);
    setModalType('confirm');
    setShowModal(true);
    confirmActionRef.current = async () => {
      if (!datasetDetail) return;
      
      try {
        const updatedData = datasetDetail.data.filter((_, i) => i !== actualIndex);
        
        await axios.put(`${API_BASE}/datasets/${datasetDetail.id}`, {
          data: updatedData,
          name: datasetDetail.name
        });
        
        setModalMessage('删除成功');
        setModalType('success');
        
        // 重新加载数据集详情
        const res = await axios.get(`${API_BASE}/datasets/${datasetDetail.id}`);
        setDatasetDetail(res.data);
        
        confirmActionRef.current = null;
      } catch (err: any) {
        setModalMessage(`删除失败: ${err.response?.data?.detail || '未知错误'}`);
        setModalType('error');
        confirmActionRef.current = null;
      }
    };
  };

  // 切换折叠状态
  const toggleCollapse = (displayIndex: number) => {
    setCollapsedRows(prev => {
      const newSet = new Set(prev);
      if (newSet.has(displayIndex)) {
        newSet.delete(displayIndex);
      } else {
        newSet.add(displayIndex);
      }
      return newSet;
    });
  };

  // 新增项目相关函数
  const handleOpenAddItemModal = () => {
    setNewItem({
      question: '',
      answer: '',
      context: '',
      ground_truth: ''
    });
    setShowAddItemModal(true);
  };

  const handleCloseAddItemModal = () => {
    setShowAddItemModal(false);
    setNewItem({
      question: '',
      answer: '',
      context: '',
      ground_truth: ''
    });
  };

  const handleAddItem = async () => {
    if (!datasetDetail) return;
    
    if (!newItem.question && !newItem.q) {
      setModalMessage('请填写问题');
      setModalType('error');
      setShowModal(true);
      return;
    }
    
    try {
      const updatedData = [...datasetDetail.data, newItem];
      
      await axios.put(`${API_BASE}/datasets/${datasetDetail.id}`, {
        data: updatedData,
        name: datasetDetail.name
      });
      
      setModalMessage('新增成功');
      setModalType('success');
      setShowModal(true);
      
      // 重新加载数据集详情
      const res = await axios.get(`${API_BASE}/datasets/${datasetDetail.id}`);
      setDatasetDetail(res.data);
      
      setShowAddItemModal(false);
      setNewItem({
        question: '',
        prompt: '',
        ground_truth: '',
        model_answer: '',
        judging_mode: 'Ground_Truth评测',
      });
    } catch (err: any) {
      setModalMessage(`新增失败: ${err.response?.data?.detail || '未知错误'}`);
      setModalType('error');
      setShowModal(true);
    }
  };

  // ========== 新增评测集功能 ==========
  const stripDatasetFilename = (filename: string) =>
    filename.replace(/\.(json|csv)$/i, '');

  const handleNewDatasetFileChange = async (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0];
    if (!f) return;

    const lower = f.name.toLowerCase();
    if (!lower.endsWith('.json') && !lower.endsWith('.csv') && !lower.endsWith('.xlsx')) {
      setNewDatasetError('仅支持 .json、.csv 或 .xlsx 文件');
      return;
    }

    setNewDatasetFile(f);
    setNewDatasetError(null);

    if (!newDatasetName && f.name) {
      setNewDatasetName(stripDatasetFilename(f.name));
    }

    if (lower.endsWith('.json')) {
      try {
        const text = await f.text();
        JSON.parse(text);
      } catch {
        setNewDatasetError('无效的 JSON 文件');
      }
    }
    // CSV/XLSX 不再前端检测 Step 列，统一由后端 /upload 自动分流
  };

  const handleDownloadTemplate = () => {
    window.open(`${API_BASE}/datasets/template-scenario.xlsx`, '_blank');
  };

  const handleExportDataset = async (datasetId: string, format: 'json' | 'csv' | 'xlsx') => {
    const dataset = datasets.find((d) => d.id === datasetId);
    setExportPreview({
      datasetId,
      datasetName: dataset?.name || datasetId,
      itemCount: dataset?.item_count ?? 0,
      createdAt: dataset?.created_at || '',
      format,
      itemsWithSteps: 0,
      enabledStepTotal: 0,
      disabledStepTotal: 0,
      keywordStepTotal: 0,
      llmStepTotal: 0,
      perItem: [],
      loading: true,
      error: '',
    });
    setExportMenuOpenId(null);

    try {
      const res = await axios.get(
        `${API_BASE}/scenario-eval/datasets/${datasetId}/item-steps-config`,
      );
      const data = res.data;
      const items: any[] = data.items || [];
      let enabledStepTotal = 0;
      let disabledStepTotal = 0;
      let keywordStepTotal = 0;
      let llmStepTotal = 0;
      let itemsWithSteps = 0;
      const perItem: { question: string; enabled: number; total: number }[] = [];
      for (const it of items) {
        const steps: any[] = it?.template?.steps || [];
        if (!steps.length) continue;
        itemsWithSteps += 1;
        let en = 0;
        let dis = 0;
        for (const s of steps) {
          if (s.enabled) {
            en += 1;
            enabledStepTotal += 1;
            const mode = s?.match_rules?.eval_config?.mode;
            if (mode === 'llm_aggregate') llmStepTotal += 1;
            else keywordStepTotal += 1;
          } else {
            dis += 1;
            disabledStepTotal += 1;
          }
        }
        perItem.push({
          question: (it.question || '').slice(0, 40),
          enabled: en,
          total: en + dis,
        });
      }
      setExportPreview((p) =>
        p && p.datasetId === datasetId && p.format === format
          ? {
              ...p,
              itemsWithSteps,
              enabledStepTotal,
              disabledStepTotal,
              keywordStepTotal,
              llmStepTotal,
              perItem,
              loading: false,
            }
          : p,
      );
    } catch (err: any) {
      setExportPreview((p) =>
        p && p.datasetId === datasetId && p.format === format
          ? {
              ...p,
              loading: false,
              error: err?.response?.data?.detail || err?.message || '加载子步骤摘要失败',
            }
          : p,
      );
    }
  };

  const handleExportConfirm = async () => {
    if (!exportPreview) return;
    const { datasetId, format } = exportPreview;
    setExportPreview((p) => (p ? { ...p, loading: true } : p));
    try {
      const res = await axios.get(`${API_BASE}/datasets/${datasetId}/export`, {
        params: { format },
        responseType: 'blob',
      });
      const disposition = res.headers['content-disposition'] as string | undefined;
      const utf8Match = disposition?.match(/filename\*=UTF-8''([^;]+)/i);
      const asciiMatch = disposition?.match(/filename="?([^";]+)"?/i);
      const fallback = `${datasetId}.${format}`;
      const filename = utf8Match?.[1]
        ? decodeURIComponent(utf8Match[1])
        : (asciiMatch?.[1] || fallback);
      const url = URL.createObjectURL(res.data);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      link.click();
      URL.revokeObjectURL(url);
      setExportPreview(null);
    } catch (err: any) {
      setExportPreview((p) =>
        p
          ? {
              ...p,
              loading: false,
              error: err?.response?.data?.detail || err?.message || '导出失败',
            }
          : p,
      );
    }
  };

  const handleExportPreviewClose = () => setExportPreview(null);

  const handleNewDatasetSubmit = async () => {
    if (!newDatasetFile && !newDatasetName) {
      setNewDatasetError('请填写评测集名称或选择一个 JSON / CSV / XLSX 文件');
      return;
    }
    
    setUploading(true);
    setNewDatasetError(null);
    
    try {
      let res;
      if (newDatasetFile) {
        const formData = new FormData();
        formData.append('file', newDatasetFile);
        formData.append('name', newDatasetName);

        // 统一走 /upload，后端自动检测 Step 列分流
        res = await axios.post(`${API_BASE}/upload`, formData, {
          headers: { 'Content-Type': 'multipart/form-data' }
        });
      } else {
        // 没有文件时，创建空评测集
        const formData = new FormData();
        const emptyData = [];
        const blob = new Blob([JSON.stringify(emptyData)], { type: 'application/json' });
        const file = new File([blob], 'empty.json');
        formData.append('file', file);
        formData.append('name', newDatasetName);
        res = await axios.post(`${API_BASE}/upload`, formData, {
          headers: { 'Content-Type': 'multipart/form-data' }
        });
      }
      
      const newDatasetId = res.data.dataset_id;
      setCurrentDatasetId(newDatasetId);
      onUploadSuccess?.(newDatasetId);
      fetchDatasets();
      
      // 质量预校验警告展示（场景导入走分流时 warnings 为空，不弹）
      if (res.data.quality_warnings && res.data.quality_warnings.length > 0) {
        const warningList = res.data.quality_warnings.map((w: string) => `• ${w}`).join('\n');
        setModalMessage(
          `导入成功（${res.data.item_count} 题），但以下质量问题可能影响场景子步骤提炼：\n\n${warningList}\n\n建议修正后重新导入，或在场景子步骤面板手动配置。`
        );
        setModalType('success');
        setShowModal(true);
      }
      setShowNewDatasetModal(false);
      setNewDatasetName('');
      setNewDatasetFile(null);
      if (newDatasetFileRef.current) newDatasetFileRef.current.value = '';
    } catch (err: any) {
      let errorMessage = '创建失败';
      if (err.response?.data) {
        const errorData = err.response.data;
        // 检查errorData是否为对象
        if (typeof errorData === 'object' && errorData !== null) {
          // 处理嵌套的detail字段
          if (errorData.detail && typeof errorData.detail === 'object') {
            const nestedDetail = errorData.detail;
            if (nestedDetail.details) {
              errorMessage = `${errorMessage}：${Array.isArray(nestedDetail.details) ? nestedDetail.details.join('; ') : nestedDetail.details}`;
            } else if (nestedDetail.error) {
              errorMessage = `${errorMessage}：${nestedDetail.error}`;
            } else {
              try {
                errorMessage = `${errorMessage}：${JSON.stringify(nestedDetail)}`;
              } catch {
                errorMessage = `${errorMessage}：未知错误`;
              }
            }
          } else if (errorData.details) {
            errorMessage = `${errorMessage}：${Array.isArray(errorData.details) ? errorData.details.join('; ') : errorData.details}`;
          } else if (errorData.error) {
            errorMessage = `${errorMessage}：${errorData.error}`;
          } else if (errorData.detail) {
            errorMessage = `${errorMessage}：${errorData.detail}`;
          } else {
            // 尝试将对象转换为字符串
            try {
              errorMessage = `${errorMessage}：${JSON.stringify(errorData)}`;
            } catch {
              errorMessage = `${errorMessage}：未知错误`;
            }
          }
        } else {
          // 如果errorData是字符串
          errorMessage = `${errorMessage}：${errorData}`;
        }
      }
      setNewDatasetError(errorMessage);
    } finally {
      setUploading(false);
    }
  };

  const handleOpenNewDatasetModal = () => {
    setShowNewDatasetModal(true);
    setNewDatasetName('');
    setNewDatasetFile(null);
    setNewDatasetError(null);
    if (newDatasetFileRef.current) newDatasetFileRef.current.value = '';
  };

  const handleCloseNewDatasetModal = () => {
    setShowNewDatasetModal(false);
    setNewDatasetName('');
    setNewDatasetFile(null);
    setNewDatasetError(null);
    if (newDatasetFileRef.current) newDatasetFileRef.current.value = '';
  };

  const handleLangfusePreview = async () => {
    if (langfuseSelectedDatasets.length === 0) {
      setModalMessage('请至少选择一个数据集');
      setModalType('error');
      setShowModal(true);
      return;
    }
    setLangfusePreviewLoading(true);
    try {
      const res = await axios.post(`${API_BASE}/datasets/langfuse/preview`, {
        project_id: langfuseProjectId || undefined,
        dataset_names: langfuseSelectedDatasets,
      });
      setLangfusePreviewItems(res.data.items || []);
      setLangfusePreviewWarnings(res.data.warnings || []);
      setLangfuseMergeName('');
      setLangfuseStep(2);
    } catch (err: any) {
      setModalMessage(`预览失败: ${err.response?.data?.detail || err.message || '未知错误'}`);
      setModalType('error');
      setShowModal(true);
    } finally {
      setLangfusePreviewLoading(false);
    }
  };

  const handleLangfuseMerge = async () => {
    setLangfuseImportLoading(true);
    try {
      const cleanItems = langfusePreviewItems.map(it => ({
        question: it.question,
        ground_truth: it.ground_truth,
        model_answer: it.model_answer || '',
        context: it.context || '',
        scenario_key: it.scenario_key || '',
        _langfuse_source: it._langfuse_source || '',
        _steps: it._steps || [],
      }));
      const res = await axios.post(`${API_BASE}/datasets/langfuse/merge`, {
        name: langfuseMergeName || undefined,
        project_id: langfuseProjectId || undefined,
        items: cleanItems,
      });
      setModalMessage(`合并导入成功！评测集: ${res.data.name}，共 ${res.data.item_count} 条`);
      setModalType('success');
      setShowModal(true);
      handleLangfuseClose();
      await fetchDatasets();
    } catch (err: any) {
      setModalMessage(`合并失败: ${err.response?.data?.detail || err.message || '未知错误'}`);
      setModalType('error');
      setShowModal(true);
    } finally {
      setLangfuseImportLoading(false);
    }
  };

  const handleLangfuseClose = () => {
    setShowLangfuseImportModal(false);
    setLangfuseProjectId('');
    setLangfuseSelectedDatasets([]);
    setLangfuseDatasets([]);
    setLangfusePreviewItems([]);
    setLangfusePreviewWarnings([]);
    setLangfuseMergeName('');
    setLangfuseNameConflict(false);
    setLangfuseDragIdx(null);
    setLangfuseStep(1);
  };

  const handleOpenLangfuseImport = async () => {
    setShowLangfuseImportModal(true);
    setLangfuseStep(1);
    setLangfuseProjectId('');
    setLangfuseSelectedDatasets([]);
    setLangfuseDatasets([]);
    setLangfusePreviewItems([]);
    setLangfusePreviewWarnings([]);
    setLangfuseMergeName('');
    setLangfuseProjects([]);
    setLangfuseLoadingList(true);
    try {
      const res = await axios.get(`${API_BASE}/langfuse/projects`);
      setLangfuseProjects(res.data.projects || []);
    } catch (err: any) {
      setLangfuseProjects([]);
    } finally {
      setLangfuseLoadingList(false);
    }
  };

  const handleLangfuseProjectChange = async (projectId: string) => {
    setLangfuseProjectId(projectId);
    setLangfuseSelectedDatasets([]);
    if (!projectId) {
      setLangfuseDatasets([]);
      return;
    }
    setLangfuseLoadingList(true);
    try {
      const res = await axios.get(`${API_BASE}/datasets/langfuse/list`, {
        params: { project_id: projectId },
      });
      setLangfuseDatasets(res.data.datasets || []);
    } catch (err: any) {
      setLangfuseDatasets([]);
    } finally {
      setLangfuseLoadingList(false);
    }
  };

  // 使用 dateUtils 中的 formatDate 函数

  // 评测集信息就地展开内容（标题栏 + 数据表格）
  const renderDetailContent = () => {
    if (loadingDataset) {
      return <div style={{ color: 'var(--edp-text2)', padding: 'var(--edp-space-3)' }}>⏳ 加载评测集信息中...</div>;
    }
    if (!datasetDetail) {
      return <div style={{ color: 'var(--edp-text2)', padding: 'var(--edp-space-3)' }}>加载中...</div>;
    }
    return (
      <>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 15 }}>
          <div>
            <h3 style={{ display: 'flex', alignItems: 'center', gap: '10px', margin: 0 }}>
            <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
              <polyline points="14 2 14 8 20 8"/>
              <line x1="16" y1="13" x2="8" y2="13"/>
              <line x1="16" y1="17" x2="8" y2="17"/>
            </svg>
            评测集信息
          </h3>
            <p style={{ color: 'var(--edp-text2)', marginTop: 5 }}>
              名称：{datasetDetail.name} | 共 {datasetDetail.item_count} 条数据
            </p>
          </div>
          <button
            className="btn btn-primary"
            style={{ padding: '8px 16px', fontSize: 'var(--edp-font-lg)' }}
            onClick={() => handleOpenAddItemModal()}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
              <circle cx="12" cy="12" r="10"/>
              <line x1="12" y1="8" x2="12" y2="16"/>
              <line x1="8" y1="12" x2="16" y2="12"/>
            </svg>
            新增
          </button>
        </div>
        <div style={{ overflowX: 'auto' }}>
          <table className="results-table" style={{ width: '100%', borderCollapse: 'collapse' }}>
            <thead>
              <tr>
                <th style={{ width: '10px', border: '1px solid var(--edp-border)', textAlign: 'center' }}>No.</th>
                <th style={{ width: '18%', border: '1px solid var(--edp-border)' }}>Question</th>
                <th style={{ width: '22%', border: '1px solid var(--edp-border)' }}>Prompt</th>
                <th style={{ width: '18%', border: '1px solid var(--edp-border)' }}>Ground_Truth</th>
                <th style={{ width: '14%', border: '1px solid var(--edp-border)' }}>Mode_Answer</th>
                <th style={{ width: '10%', border: '1px solid var(--edp-border)', textAlign: 'center' }}>Judging_Mode</th>
                <th style={{ width: '8%', border: '1px solid var(--edp-border)', textAlign: 'center' }}>Action</th>
              </tr>
            </thead>
            <tbody>
              {[...datasetDetail.data].reverse().map((item, index) => {
                const displayIndex = datasetDetail.data.length - 1 - index;
                const isCollapsed = collapsedRows.has(displayIndex);

                // 优先取 CSV 字段（scenario 数据），缺失时回退到旧别名
                const questionVal: string = item.question || item.q || '-';
                const promptVal: string = item.prompt || '-';
                const gtVal: string =
                  item.ground_truth || item.gt || item.true_answer || item.reference || '-';
                const modeAnswerVal: string =
                  item.model_answer || item.answer || item.a || item.response || '-';
                const judgingModeVal: string = item.judging_mode || '-';

                const truncate = (v: string) => {
                  if (!v || v === '-') return '-';
                  const lines = v.split('\n');
                  if (lines.length <= 2) return v;
                  return lines.slice(0, 2).join('\n') + '...';
                };

                return (
                <tr key={displayIndex} style={{ verticalAlign: 'middle' }}>
                  <td style={{ verticalAlign: 'middle', border: '1px solid var(--edp-border)', textAlign: 'center' }}>
                    <div style={{ display: 'inline-flex', alignItems: 'center', gap: 'var(--edp-space-1)' }}>
                      <Tooltip content={isCollapsed ? '展开' : '折叠'}>
                        <button
                          className="btn"
                          style={{
                            padding: '2px 6px',
                            fontSize: 'var(--edp-font-lg)',
                            background: isCollapsed ? 'var(--edp-primary)' : 'var(--edp-text2)',
                            minWidth: '20px',
                            height: '20px',
                            lineHeight: '1',
                            display: 'flex',
                            alignItems: 'center',
                            justifyContent: 'center'
                          }}
                          onClick={() => toggleCollapse(displayIndex)}
                        >
                          {isCollapsed ? '+' : '-'}
                        </button>
                      </Tooltip>
                      <span>{displayIndex + 1}</span>
                    </div>
                  </td>
                  <td style={{ verticalAlign: 'middle', border: '1px solid var(--edp-border)' }}>
                    <Tooltip content={questionVal}>
                      <div style={{ maxWidth: '250px', wordWrap: 'break-word', fontSize: 'var(--edp-font-md)' }}>
                        {isCollapsed ? truncate(questionVal) : questionVal}
                      </div>
                    </Tooltip>
                  </td>
                  <td style={{ verticalAlign: 'middle', border: '1px solid var(--edp-border)' }}>
                    <Tooltip content={promptVal}>
                      <div style={{ maxWidth: '320px', wordWrap: 'break-word', fontSize: 'var(--edp-font-md)', whiteSpace: 'pre-wrap' }}>
                        {isCollapsed ? truncate(promptVal) : promptVal}
                      </div>
                    </Tooltip>
                  </td>
                  <td style={{ verticalAlign: 'middle', border: '1px solid var(--edp-border)' }}>
                    <Tooltip content={gtVal}>
                      <div style={{ maxWidth: '250px', wordWrap: 'break-word', fontSize: 'var(--edp-font-md)' }}>
                        {isCollapsed ? truncate(gtVal) : gtVal}
                      </div>
                    </Tooltip>
                  </td>
                  <td style={{ verticalAlign: 'middle', border: '1px solid var(--edp-border)' }}>
                    <Tooltip content={modeAnswerVal}>
                      <div style={{ maxWidth: '200px', wordWrap: 'break-word', fontSize: 'var(--edp-font-md)', whiteSpace: 'pre-wrap' }}>
                        {isCollapsed ? truncate(modeAnswerVal) : modeAnswerVal}
                      </div>
                    </Tooltip>
                  </td>
                  <td style={{ verticalAlign: 'middle', border: '1px solid var(--edp-border)', textAlign: 'center' }}>
                    <span
                      style={{
                        display: 'inline-block',
                        padding: '2px 8px',
                        borderRadius: 'var(--edp-radius-sm)',
                        fontSize: 'var(--edp-font-base)',
                        background: judgingModeVal.includes('提示词') ? 'var(--edp-primary-soft)' : 'var(--edp-blue-soft)',
                        color: judgingModeVal.includes('提示词') ? 'var(--edp-primary)' : 'var(--edp-blue)',
                      }}
                    >
                      {judgingModeVal}
                    </span>
                  </td>
                  <td style={{ textAlign: 'center', verticalAlign: 'middle', paddingTop: 15, paddingBottom: 15, border: '1px solid var(--edp-border)' }}>
                    <div style={{ display: 'inline-flex', gap: 'var(--edp-space-2)', alignItems: 'center', justifyContent: 'center' }}>
                      <button
                        className="btn btn-primary"
                        style={{ padding: '6px' }}
                        onClick={() => handleEditItem(index)}
                        title="编辑"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ verticalAlign: 'middle' }}>
                          <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                          <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
                        </svg>
                      </button>
                      <button
                        className="btn btn-danger"
                        style={{ padding: '6px' }}
                        onClick={() => handleDeleteItem(index)}
                        title="删除"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ verticalAlign: 'middle' }}>
                          <polyline points="3 6 5 6 21 6"/>
                          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                        </svg>
                      </button>
                    </div>
                  </td>
                </tr>
              );
            })}
            </tbody>
          </table>
        </div>
      </>
    );
  };

  return (
    <div>
      {/* 模态框 */}
      {showModal && (
        <div className="modal-overlay" style={{
          position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
          background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000
        }}>
          <div className="modal-content" style={{ background: 'white', padding: 'var(--edp-space-5)', borderRadius: 'var(--edp-radius-sm)', maxWidth: 520, maxHeight: '80vh', overflowY: 'auto' }}>
            <h3 style={{ color: modalType === 'success' ? 'var(--edp-green)' : modalType === 'error' ? 'var(--edp-red)' : 'var(--edp-primary)' }}>
              {modalType === 'success' ? '成功' : modalType === 'error' ? '失败' : '确认'}
            </h3>
            <p style={{ whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>{modalMessage}</p>
            {modalType === 'confirm' ? (
              <div style={{ display: 'flex', gap: 10, justifyContent: 'center' }}>
                <button type="button" onClick={handleModalClose} style={{ background: 'var(--edp-text2)' }} disabled={deleting}>取消</button>
                <button type="button" onClick={handleModalConfirm} disabled={deleting}>确定</button>
              </div>
            ) : (
              <button type="button" onClick={handleModalClose} disabled={deleting}>确定</button>
            )}
          </div>
        </div>
      )}

      {/* 导出预览模态框 */}
      {exportPreview && (
        <div className="modal-overlay" style={{
          position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
          background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000
        }}>
          <div className="modal-content" style={{ background: 'white', padding: 'var(--edp-space-5)', borderRadius: 'var(--edp-radius-sm)', maxWidth: 560, width: '90%', maxHeight: '80vh', overflowY: 'auto' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--edp-space-4)' }}>
              <h3 style={{ color: 'var(--edp-primary)', display: 'flex', alignItems: 'center', gap: '8px', margin: 0 }}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                  <polyline points="7 10 12 15 17 10"/>
                  <line x1="12" y1="15" x2="12" y2="3"/>
                </svg>
                导出预览
              </h3>
              <button
                type="button"
                className="btn"
                style={{ padding: '4px 12px' }}
                onClick={handleExportPreviewClose}
                disabled={exportPreview.loading}
              >
                关闭
              </button>
            </div>

            {exportPreview.loading ? (
              <div style={{ textAlign: 'center', padding: '24px 0', color: 'var(--edp-text2)' }}>
                <svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite' }}>
                  <circle cx="12" cy="12" r="10"/>
                  <path d="M12 6v6l4 2"/>
                </svg>
                <p style={{ marginTop: 'var(--edp-space-2)' }}>加载子步骤摘要…</p>
              </div>
            ) : exportPreview.error ? (
              <div>
                <p style={{ color: 'var(--edp-red)', whiteSpace: 'pre-wrap', lineHeight: 1.6 }}>{exportPreview.error}</p>
                <div style={{ display: 'flex', gap: 10, justifyContent: 'center', marginTop: 'var(--edp-space-4)' }}>
                  <button type="button" onClick={handleExportPreviewClose}>关闭</button>
                  <button type="button" className="btn btn-primary" onClick={handleExportConfirm}>仍要导出</button>
                </div>
              </div>
            ) : (
              <>
                {/* 评测集基本信息 */}
                <div style={{ background: 'var(--edp-bg)', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', padding: 'var(--edp-space-3)', marginBottom: 14 }}>
                  <div style={{ fontWeight: 600, marginBottom: 6, color: 'var(--edp-text2)' }}>
                    {exportPreview.datasetName}
                  </div>
                  <div style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)', display: 'flex', gap: 'var(--edp-space-4)', flexWrap: 'wrap' }}>
                    <span>题目数：{exportPreview.itemCount}</span>
                    {exportPreview.createdAt && <span>创建于：{formatDate(exportPreview.createdAt)}</span>}
                    <span>格式：{exportPreview.format.toUpperCase()}</span>
                  </div>
                </div>

                {/* 子步骤摘要（仅当有子步骤时显示） */}
                {exportPreview.itemsWithSteps > 0 ? (
                  <div style={{ marginBottom: 14 }}>
                    <div style={{ fontWeight: 600, fontSize: 'var(--edp-font-md)', color: 'var(--edp-text2)', marginBottom: 'var(--edp-space-2)' }}>
                      子步骤摘要
                    </div>
                    <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
                      <span style={{ background: 'var(--edp-primary-soft)', color: 'var(--edp-primary)', padding: '3px 10px', borderRadius: 'var(--edp-radius)', fontSize: 'var(--edp-font-base)' }}>
                        {exportPreview.itemsWithSteps} 题配置子步骤
                      </span>
                      <span style={{ background: 'var(--edp-green-soft)', color: 'var(--edp-green)', padding: '3px 10px', borderRadius: 'var(--edp-radius)', fontSize: 'var(--edp-font-base)' }}>
                        启用 {exportPreview.enabledStepTotal} 步
                      </span>
                      {exportPreview.disabledStepTotal > 0 && (
                        <span style={{ background: 'var(--edp-red-soft)', color: 'var(--edp-red)', padding: '3px 10px', borderRadius: 'var(--edp-radius)', fontSize: 'var(--edp-font-base)' }}>
                          禁用 {exportPreview.disabledStepTotal} 步（不导出）
                        </span>
                      )}
                      {exportPreview.keywordStepTotal > 0 && (
                        <span style={{ background: 'var(--edp-border-soft)', color: 'var(--edp-text2)', padding: '3px 10px', borderRadius: 'var(--edp-radius)', fontSize: 'var(--edp-font-base)' }}>
                          关键词 {exportPreview.keywordStepTotal}
                        </span>
                      )}
                      {exportPreview.llmStepTotal > 0 && (
                        <span style={{ background: 'var(--edp-amber-soft)', color: 'var(--edp-amber)', padding: '3px 10px', borderRadius: 'var(--edp-radius)', fontSize: 'var(--edp-font-base)' }}>
                          提示词 {exportPreview.llmStepTotal}
                        </span>
                      )}
                    </div>
                    <details style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)' }}>
                      <summary style={{ cursor: 'pointer', padding: '4px 0' }}>按题查看</summary>
                      <ul style={{ margin: '6px 0 0', paddingLeft: 18, maxHeight: 160, overflowY: 'auto' }}>
                        {exportPreview.perItem.map((it, i) => (
                          <li key={i} style={{ padding: '2px 0' }}>
                            <span style={{ color: 'var(--edp-text2)' }}>{it.question || `第 ${i + 1} 题`}</span>
                            <span style={{ color: it.enabled === 0 ? 'var(--edp-red)' : 'var(--edp-text2)' }}> — 启用 {it.enabled}/{it.total}</span>
                          </li>
                        ))}
                      </ul>
                    </details>
                  </div>
                ) : (
                  <div style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text3)', marginBottom: 14, padding: '8px 12px', background: 'var(--edp-bg)', borderRadius: 'var(--edp-radius-sm)' }}>
                    该评测集未配置场景子步骤，仅导出题目级字段。
                  </div>
                )}

                {/* 格式说明 */}
                <div style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text2)', background: 'var(--edp-amber-soft)', border: '1px solid var(--edp-amber-soft)', borderRadius: 'var(--edp-radius-sm)', padding: 10, marginBottom: 'var(--edp-space-4)' }}>
                  {exportPreview.format === 'json' ? (
                    exportPreview.itemsWithSteps > 0
                      ? 'JSON：每个 item 内嵌 steps 数组（仅启用的子步骤）。'
                      : 'JSON：导出题目级字段数组。'
                  ) : (
                    exportPreview.itemsWithSteps > 0
                      ? `${exportPreview.format.toUpperCase()}：含 Step_Question/Step_Prompt/Step_Ground_Truth/Step_Mode_Answer/Step_Weight/Step_Judging_Mode 列，每个子步骤输出一行。`
                      : `${exportPreview.format.toUpperCase()}：导出题目级字段，不含 Step_* 列。`
                  )}
                </div>

                <div style={{ display: 'flex', gap: 10, justifyContent: 'center' }}>
                  <button type="button" onClick={handleExportPreviewClose}>取消</button>
                  <button type="button" className="btn btn-primary" onClick={handleExportConfirm}>确认导出</button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* 新增评测集模态框 */}
      {showNewDatasetModal && (
        <div className="modal-overlay" style={{
          position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
          background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000
        }}>
          <div className="modal-content" style={{ background: 'white', padding: 'var(--edp-space-5)', borderRadius: 'var(--edp-radius-sm)', maxWidth: 500, width: '90%' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--edp-space-5)' }}>
              <h3 style={{ color: 'var(--edp-primary)', display: 'flex', alignItems: 'center', gap: '8px' }}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10"/>
                <line x1="12" y1="8" x2="12" y2="16"/>
                <line x1="8" y1="12" x2="16" y2="12"/>
              </svg>
              新增评测集
            </h3>
              <button
                className="btn"
                style={{ padding: '4px 12px', display: 'flex', alignItems: 'center', gap: '4px' }}
                onClick={handleCloseNewDatasetModal}
                disabled={uploading}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <line x1="18" y1="6" x2="6" y2="18"/>
                  <line x1="6" y1="6" x2="18" y2="18"/>
                </svg>
                关闭
              </button>
            </div>
            
            <div style={{ marginBottom: 15, display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)' }}>
              <label style={{ fontWeight: 'bold', whiteSpace: 'nowrap' }}>
                评测集名称 <span style={{ color: 'var(--edp-red)' }}>*</span>
              </label>
              <input
                type="text"
                placeholder="请输入评测集名称"
                value={newDatasetName}
                onChange={(e) => setNewDatasetName(e.target.value)}
                style={{ 
                  flex: 1,
                  padding: '8px 12px',
                  border: '1px solid var(--edp-border)',
                  borderRadius: 'var(--edp-radius-sm)',
                  minWidth: '200px'
                }}
              />
            </div>
            
            <div style={{ marginBottom: 15, display: 'flex', alignItems: 'center', gap: 'var(--edp-space-2)', flexWrap: 'wrap' }}>
              <label style={{ fontWeight: 'bold', whiteSpace: 'nowrap' }}>
                导入评测集（JSON / CSV / XLSX）
              </label>
              <input
                type="text"
                value={newDatasetFile ? newDatasetFile.name : '未选择任何文件'}
                readOnly
                style={{ 
                  flex: 1,
                  padding: '8px 12px',
                  border: '1px solid var(--edp-border)',
                  borderRadius: 'var(--edp-radius-sm)',
                  backgroundColor: 'var(--edp-bg)',
                  minWidth: '200px'
                }}
              />
              <input
                ref={newDatasetFileRef}
                type="file"
                accept=".json,.csv,.xlsx"
                onChange={handleNewDatasetFileChange}
                style={{ display: 'none' }}
              />
              <button
                className="btn"
                onClick={() => newDatasetFileRef.current?.click()}
                style={{ padding: '6px 12px', fontSize: 'var(--edp-font-lg)', whiteSpace: 'nowrap' }}
              >
                选择文件
              </button>
              <button
                type="button"
                className="btn"
                onClick={handleDownloadTemplate}
                style={{ padding: '6px 12px', fontSize: 'var(--edp-font-lg)', whiteSpace: 'nowrap' }}
                title="下载含过程点（Step_*）的 XLSX 模板，含子步骤列时自动配置，不填 Step 列则系统自动提炼"
              >
                下载模板
              </button>
            </div>
            
            {newDatasetError && (
              <div style={{ 
                color: 'var(--edp-red)', 
                margin: '10px 0',
                maxHeight: '100px',
                overflow: 'auto',
                padding: '8px',
                border: '1px solid var(--edp-red-soft)',
                borderRadius: '4px',
                backgroundColor: 'var(--edp-red-soft)',
                textAlign: 'left'
              }}>
                <div>{newDatasetError}</div>
              </div>
            )}
            
            <div style={{ display: 'flex', gap: 10, justifyContent: 'center', marginTop: 'var(--edp-space-5)' }}>
              <button
                className="btn"
                style={{ background: 'var(--edp-text2)' }}
                onClick={handleCloseNewDatasetModal}
                disabled={uploading}
              >
                取消
              </button>
              <button
                className="btn btn-primary"
                onClick={handleNewDatasetSubmit}
                disabled={(!newDatasetFile && !newDatasetName) || uploading}
              >
                {uploading ? '⏳ 上传中...' : '确认创建'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Langfuse 导入模态框（多步向导） */}
      {showLangfuseImportModal && (
        <div className="modal-overlay" style={{
          position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
          background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000
        }}>
          <div className="modal-content" style={{ background: 'white', padding: 'var(--edp-space-5)', borderRadius: 'var(--edp-radius-sm)', maxWidth: langfuseStep === 2 ? 800 : 500, width: '90%', maxHeight: '85vh', overflowY: 'auto' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--edp-space-4)' }}>
              <h3 style={{ color: 'var(--edp-primary)', display: 'flex', alignItems: 'center', gap: '8px' }}>
                <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                  <polyline points="7 10 12 15 17 10"/>
                  <line x1="12" y1="15" x2="12" y2="3"/>
                </svg>
                从 Langfuse 导入评测集
              </h3>
              <div style={{ display: 'flex', gap: 'var(--edp-space-2)', alignItems: 'center' }}>
                <span style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text3)' }}>
                  步骤 {langfuseStep}/3
                </span>
                <button
                  className="btn"
                  style={{ padding: '4px 12px' }}
                  onClick={handleLangfuseClose}
                  disabled={langfuseImportLoading || langfusePreviewLoading}
                >
                  ✕ 关闭
                </button>
              </div>
            </div>

            {/* 步骤指示条 */}
            <div style={{ display: 'flex', marginBottom: 'var(--edp-space-5)', gap: 'var(--edp-space-1)' }}>
              {[1, 2, 3].map(s => (
                <div key={s} style={{
                  flex: 1, height: 4, borderRadius: 2,
                  background: s <= langfuseStep ? 'var(--edp-primary)' : 'var(--edp-border)',
                  transition: 'background 0.3s'
                }} />
              ))}
            </div>

            {/* Step 1: 选择项目 & 数据集 */}
            {langfuseStep === 1 && (
              <>
                <div style={{ marginBottom: 15 }}>
                  <label style={{ fontWeight: 'bold', display: 'block', marginBottom: 6 }}>
                    Langfuse 项目 <span style={{ color: 'var(--edp-red)' }}>*</span>
                  </label>
                  {langfuseProjects.length === 0 ? (
                    <p style={{ color: 'var(--edp-red)', fontSize: 'var(--edp-font-lg)' }}>未配置 Langfuse 项目，请在"系统设置 → Langfuse 项目配置"中添加。</p>
                  ) : (
                    <select
                      value={langfuseProjectId}
                      onChange={(e) => handleLangfuseProjectChange(e.target.value)}
                      style={{ width: '100%', padding: '8px 12px', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', boxSizing: 'border-box', fontSize: 'var(--edp-font-lg)' }}
                    >
                      <option value="">-- 请选择项目 --</option>
                      {(() => {
                        const grouped: Record<string, typeof langfuseProjects> = {};
                        langfuseProjects.forEach(p => {
                          const org = p.organization || '未分组';
                          if (!grouped[org]) grouped[org] = [];
                          grouped[org].push(p);
                        });
                        return Object.entries(grouped).map(([org, projects]) => (
                          <optgroup key={org} label={`组织: ${org}`}>
                            {projects.map(p => (
                              <option key={p.id} value={p.id}>
                                {p.name} {p.langfuse_project_id ? `(${p.langfuse_project_id})` : ''}
                              </option>
                            ))}
                          </optgroup>
                        ));
                      })()}
                    </select>
                  )}
                </div>

                {langfuseProjectId && (
                  <div style={{ marginBottom: 15 }}>
                    <label style={{ fontWeight: 'bold', display: 'block', marginBottom: 6 }}>
                      选择数据集 <span style={{ color: 'var(--edp-red)' }}>*</span>
                      <span style={{ fontWeight: 'normal', color: 'var(--edp-text3)', fontSize: 'var(--edp-font-base)', marginLeft: 'var(--edp-space-2)' }}>
                        （可多选，每个数据集的 items 将在预览中展开显示，合并时折叠为1条评测项）
                      </span>
                    </label>
                    {langfuseLoadingList ? (
                      <p style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-lg)' }}>⏳ 正在加载数据集列表...</p>
                    ) : langfuseDatasets.length === 0 ? (
                      <p style={{ color: 'var(--edp-red)', fontSize: 'var(--edp-font-lg)' }}>该项目下未找到数据集。</p>
                    ) : (
                      <div style={{ border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', maxHeight: 250, overflowY: 'auto' }}>
                        {langfuseDatasets.map((ds) => (
                          <label key={ds.name} style={{
                            display: 'flex', alignItems: 'center', padding: '8px 12px',
                            borderBottom: '1px solid var(--edp-border)', cursor: ds.can_import ? 'pointer' : 'not-allowed',
                            opacity: ds.can_import ? 1 : 0.5, background: langfuseSelectedDatasets.includes(ds.name) ? 'var(--edp-primary-soft)' : 'transparent'
                          }}>
                            <input
                              type="checkbox"
                              checked={langfuseSelectedDatasets.includes(ds.name)}
                              disabled={!ds.can_import}
                              onChange={(e) => {
                                if (e.target.checked) {
                                  setLangfuseSelectedDatasets(prev => [...prev, ds.name]);
                                } else {
                                  setLangfuseSelectedDatasets(prev => prev.filter(n => n !== ds.name));
                                }
                              }}
                              style={{ marginRight: 'var(--edp-space-2)' }}
                            />
                            <span style={{ flex: 1, fontSize: 'var(--edp-font-lg)', color: ds.can_import ? 'inherit' : 'var(--edp-text3)' }}>
                              {ds.can_import ? ds.name : `${ds.id || ds.name} (无名称，请在Langfuse中设置name)`}
                            </span>
                            {ds.can_import && ds.item_count !== undefined && (
                              <span style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text3)', marginLeft: 'var(--edp-space-2)' }}>
                                {ds.item_count} 条
                              </span>
                            )}
                          </label>
                        ))}
                      </div>
                    )}
                    {langfuseSelectedDatasets.length > 0 && (
                      <p style={{ fontSize: 'var(--edp-font-md)', color: 'var(--edp-primary)', marginTop: 'var(--edp-space-2)' }}>
                        已选 {langfuseSelectedDatasets.length} 个数据集
                      </p>
                    )}
                  </div>
                )}

                <p style={{ color: 'var(--edp-text2)', fontSize: 'var(--edp-font-base)', marginTop: 6 }}>
                  选择 Langfuse 项目和数据集，预览映射结果后再合并为 onto-eval 评测集。
                </p>

                <div style={{ display: 'flex', gap: 10, justifyContent: 'center', marginTop: 'var(--edp-space-5)' }}>
                  <button className="btn" style={{ background: 'var(--edp-text2)' }} onClick={handleLangfuseClose} disabled={langfusePreviewLoading}>
                    取消
                  </button>
                  <button
                    className="btn btn-primary"
                    onClick={handleLangfusePreview}
                    disabled={langfuseSelectedDatasets.length === 0 || langfusePreviewLoading}
                  >
                    {langfusePreviewLoading ? '⏳ 加载预览...' : '下一步：预览映射'}
                  </button>
                </div>
              </>
            )}

            {/* Step 2: 预览映射结果 */}
            {langfuseStep === 2 && (
              <>
                {langfusePreviewWarnings.length > 0 && (
                  <div style={{ background: 'var(--edp-amber-soft)', border: '1px solid var(--edp-amber)', borderRadius: 'var(--edp-radius-sm)', padding: '8px 12px', marginBottom: 'var(--edp-space-3)' }}>
                    {langfusePreviewWarnings.map((w, i) => (
                      <div key={i} style={{ fontSize: 'var(--edp-font-md)', color: 'var(--edp-amber)' }}>⚠️ {w}</div>
                    ))}
                  </div>
                )}

                <div style={{ marginBottom: 'var(--edp-space-3)' }}>
                  <span style={{ fontSize: 'var(--edp-font-lg)', color: 'var(--edp-text)' }}>
                    预览结果：<strong>{langfusePreviewItems.length}</strong> 条 item（按数据集来源分组，合并时每个数据集 → 1 条评测项）
                  </span>
                </div>

                <div style={{ maxHeight: 400, overflowY: 'auto', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)' }}>
                  {langfusePreviewItems.map((item, idx) => (
                    <div key={idx} style={{ padding: '12px 16px', borderBottom: '1px solid var(--edp-border)', background: idx % 2 === 0 ? 'var(--edp-bg)' : 'var(--edp-card)' }}>
                      <div style={{ fontSize: 'var(--edp-font-sm)', color: 'var(--edp-text3)', marginBottom: 6 }}>
                        来源: {item._langfuse_source || '未知'} | 步骤数: <strong>{item._steps?.length || 0}</strong>
                        {item._mapping_warnings && item._mapping_warnings.length > 0 && (
                          <span style={{ color: 'var(--edp-red)', marginLeft: 'var(--edp-space-2)' }}>
                            {item._mapping_warnings.map((w: string, wi: number) => <span key={wi}>⚠ {w} </span>)}
                          </span>
                        )}
                      </div>
                      <div style={{ display: 'grid', gridTemplateColumns: '120px 1fr', gap: '4px 8px', fontSize: 'var(--edp-font-md)' }}>
                        <span style={{ color: 'var(--edp-text2)', fontWeight: 'bold' }}>question:</span>
                        <input
                          value={item.question}
                          onChange={(e) => {
                            const newItems = [...langfusePreviewItems];
                            newItems[idx] = { ...newItems[idx], question: e.target.value };
                            setLangfusePreviewItems(newItems);
                          }}
                          style={{ width: '100%', padding: '4px 8px', border: '1px solid var(--edp-border)', borderRadius: 3, fontSize: 'var(--edp-font-md)', boxSizing: 'border-box' }}
                        />
                        <span style={{ color: 'var(--edp-text2)', fontWeight: 'bold' }}>ground_truth:</span>
                        <textarea
                          value={item.ground_truth}
                          onChange={(e) => {
                            const newItems = [...langfusePreviewItems];
                            newItems[idx] = { ...newItems[idx], ground_truth: e.target.value };
                            setLangfusePreviewItems(newItems);
                          }}
                          rows={2}
                          style={{ width: '100%', padding: '4px 8px', border: '1px solid var(--edp-border)', borderRadius: 3, fontSize: 'var(--edp-font-md)', boxSizing: 'border-box', resize: 'vertical' }}
                        />
                      </div>
                      <div style={{ marginTop: 'var(--edp-space-1)', display: 'flex', justifyContent: 'flex-end' }}>
                        <button
                          className="btn"
                          style={{ padding: '2px 8px', fontSize: 'var(--edp-font-base)', color: 'var(--edp-red)' }}
                          onClick={() => {
                            setLangfusePreviewItems(prev => prev.filter((_, i) => i !== idx));
                          }}
                        >
                          删除
                        </button>
                      </div>
                    </div>
                  ))}
                </div>

                <div style={{ display: 'flex', gap: 10, justifyContent: 'center', marginTop: 'var(--edp-space-5)' }}>
                  <button className="btn" style={{ background: 'var(--edp-text2)' }} onClick={() => setLangfuseStep(1)}>
                    上一步
                  </button>
                  <button
                    className="btn btn-primary"
                    onClick={() => setLangfuseStep(3)}
                    disabled={langfusePreviewItems.length === 0}
                  >
                    下一步：调整步骤顺序
                  </button>
                </div>
              </>
            )}

            {/* Step 3: 调整步骤顺序 & 确认合并 */}
            {langfuseStep === 3 && (
              <>
                <div style={{ marginBottom: 'var(--edp-space-4)' }}>
                  <label style={{ fontWeight: 'bold', display: 'block', marginBottom: 6 }}>
                    评测集名称
                  </label>
                  <input
                    value={langfuseMergeName}
                    onChange={(e) => {
                      const val = e.target.value.trim();
                      setLangfuseMergeName(e.target.value);
                      setLangfuseNameConflict(val && datasets.some(d => d.name === val));
                    }}
                    placeholder="留空则自动生成"
                    style={{
                      width: '100%', padding: '8px 12px',
                      border: langfuseNameConflict ? '1px solid var(--edp-red)' : '1px solid var(--edp-border)',
                      borderRadius: 'var(--edp-radius-sm)', boxSizing: 'border-box', fontSize: 'var(--edp-font-lg)'
                    }}
                  />
                  {langfuseNameConflict && (
                    <div style={{ color: 'var(--edp-red)', fontSize: 'var(--edp-font-base)', marginTop: 'var(--edp-space-1)' }}>
                      ⚠️ 该名称已存在，请修改后重试
                    </div>
                  )}
                </div>

                {/* Step 3: 按数据集来源分组展示，每组 = 1条最终评测项 */}
                {(() => {
                  // 按 _langfuse_source 分组
                  const groups: Record<string, typeof langfusePreviewItems> = {};
                  langfusePreviewItems.forEach((item) => {
                    const src = item._langfuse_source || '未知来源';
                    if (!groups[src]) groups[src] = [];
                    groups[src].push(item);
                  });
                  const sourceNames = Object.keys(groups);

                  return sourceNames.map((srcName, groupIdx) => {
                    // 收集该组下所有 steps（来自所有 item）
                    const groupItems = groups[srcName];
                    const allSteps: { order: number; desc: string; answer: string }[] = [];
                    groupItems.forEach(item => {
                      const steps = item._steps || [];
                      allSteps.push(...steps);
                    });
                    // 按 order 排序
                    allSteps.sort((a, b) => a.order - b.order);

                    if (allSteps.length === 0) return null;

                    // 取第一个有 question 的 item 作为该评测项的标题
                    const mainQuestion = groupItems.find(it => it.question)?.question?.slice(0, 60) || '(无question)';

                    return (
                      <div key={srcName} style={{ marginBottom: 'var(--edp-space-5)', border: '1px solid var(--edp-border)', borderRadius: 'var(--edp-radius-sm)', overflow: 'hidden' }}>
                        <div style={{ background: 'var(--edp-border)', padding: '8px 12px', fontSize: 'var(--edp-font-md)', fontWeight: 'bold', display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
                          <span>
                            评测项 {groupIdx + 1}（来自 <strong>{srcName}</strong>）：{mainQuestion}
                          </span>
                          <span style={{ fontSize: 'var(--edp-font-base)', color: 'var(--edp-text3)', fontWeight: 'normal' }}>
                            {groupItems.length} 个预览 item · {allSteps.length} 个子步骤 · 拖拽 ⠿ 调整顺序
                          </span>
                        </div>
                        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 'var(--edp-font-base)' }}>
                          <thead>
                            <tr style={{ background: 'var(--edp-bg)' }}>
                              <th style={{ padding: '6px 4px', textAlign: 'center', width: 30 }}></th>
                              <th style={{ padding: '6px 8px', textAlign: 'center', width: 40 }}>序号</th>
                              <th style={{ padding: '6px 8px', textAlign: 'left', width: 200 }}>步骤</th>
                              <th style={{ padding: '6px 8px', textAlign: 'left' }}>输出 (expectedOutput)</th>
                            </tr>
                          </thead>
                          <tbody>
                            {allSteps.map((step, stepIdx) => (
                              <tr
                                key={stepIdx}
                                onDragOver={(e) => { e.preventDefault(); }}
                                onDrop={() => {
                                  if (langfuseDragIdx !== null && langfuseDragIdx !== stepIdx) {
                                    const newItems = [...langfusePreviewItems];
                                    let flatSteps: Array<{itemIdx: number, stepIdxInItem: number, step: typeof step}> = [];
                                    newItems.forEach((ni, niIdx) => {
                                      (ni._steps || []).forEach((ns, nsIdx) => {
                                        flatSteps.push({itemIdx: niIdx, stepIdxInItem: nsIdx, step: ns});
                                      });
                                    });
                                    flatSteps.sort((a, b) => a.step.order - b.step.order);
                                    const [moved] = flatSteps.splice(langfuseDragIdx, 1);
                                    flatSteps.splice(stepIdx, 0, moved);
                                    flatSteps.forEach((fs, newOrder) => {
                                      fs.step.order = newOrder + 1;
                                      newItems[fs.itemIdx]._steps[fs.stepIdxInItem] = fs.step;
                                    });
                                    setLangfusePreviewItems(newItems);
                                    setLangfuseDragIdx(null);
                                  }
                                }}
                                style={{
                                  borderBottom: '1px solid var(--edp-border)',
                                  background: langfuseDragIdx === stepIdx ? 'var(--edp-primary-soft)' : (stepIdx % 2 === 0 ? 'var(--edp-card)' : 'var(--edp-bg)'),
                                  opacity: langfuseDragIdx !== null && langfuseDragIdx !== stepIdx ? 0.7 : 1,
                                }}
                              >
                                <td style={{ padding: '4px 4px', textAlign: 'center' }}>
                                  <span
                                    draggable
                                    onDragStart={() => setLangfuseDragIdx(stepIdx)}
                                    onDragEnd={() => setLangfuseDragIdx(null)}
                                    style={{ cursor: 'grab', color: 'var(--edp-text3)', fontSize: 'var(--edp-font-lg)' }}
                                    title="拖拽调整顺序"
                                  >
                                    ⠿
                                  </span>
                                </td>
                                <td style={{ padding: '4px 8px', textAlign: 'center', color: 'var(--edp-primary)', fontWeight: 'bold' }}>
                                  {step.order}
                                </td>
                                <td style={{ padding: '4px 8px', color: 'var(--edp-text2)', maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                  步骤 {step.order}
                                </td>
                                <td style={{ padding: '4px 8px', maxWidth: 400, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                  {step.answer?.slice(0, 120) || <span style={{ color: 'var(--edp-red)' }}>(空)</span>}
                                  {step.answer?.length > 120 && '...'}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    );
                  });
                })()}

                <div style={{ display: 'flex', gap: 10, justifyContent: 'center', marginTop: 'var(--edp-space-5)' }}>
                  <button className="btn" style={{ background: 'var(--edp-text2)' }} onClick={() => setLangfuseStep(2)} disabled={langfuseImportLoading}>
                    上一步
                  </button>
                  <button
                    className="btn btn-primary"
                    onClick={handleLangfuseMerge}
                    disabled={langfusePreviewItems.length === 0 || langfuseImportLoading || langfuseNameConflict}
                  >
                    {langfuseImportLoading ? '⏳ 合并中...' : '确认合并并创建评测集'}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}

      {/* 编辑项目模态框 */}
      {showItemEditModal && editingItem && (
        <div className="modal-overlay" style={{
          position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
          background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000
        }}>
          <div className="modal-content" style={{ background: 'white', padding: 'var(--edp-space-5)', borderRadius: 'var(--edp-radius-sm)', maxWidth: 600, width: '90%' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--edp-space-5)' }}>
              <h3 style={{ color: 'var(--edp-primary)', display: 'flex', alignItems: 'center', gap: '8px' }}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
              </svg>
              编辑评测项目
            </h3>
              <button
                className="btn"
                style={{ padding: '4px 12px', display: 'flex', alignItems: 'center', gap: '4px' }}
                onClick={handleCancelItemEdit}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <line x1="18" y1="6" x2="6" y2="18"/>
                  <line x1="6" y1="6" x2="18" y2="18"/>
                </svg>
                关闭
              </button>
            </div>
            
            <div style={{ marginBottom: 15 }}>
              <label style={{ display: 'block', marginBottom: 5, fontWeight: 'bold' }}>
                Question
              </label>
              <textarea
                value={editingItem.question || editingItem.q || ''}
                onChange={(e) => setEditingItem((prev: any) => ({
                  ...prev,
                  question: e.target.value,
                  q: e.target.value,
                }))}
                style={{
                  width: '100%',
                  padding: '8px 12px',
                  border: '1px solid var(--edp-border)',
                  borderRadius: 'var(--edp-radius-sm)',
                  minHeight: '80px',
                  resize: 'vertical'
                }}
                placeholder="Please enter question"
              />
            </div>

            <div style={{ marginBottom: 15 }}>
              <label style={{ display: 'block', marginBottom: 5, fontWeight: 'bold' }}>
                Prompt
              </label>
              <textarea
                value={editingItem.prompt || ''}
                onChange={(e) => setEditingItem((prev: any) => ({
                  ...prev,
                  prompt: e.target.value,
                }))}
                style={{
                  width: '100%',
                  padding: '8px 12px',
                  border: '1px solid var(--edp-border)',
                  borderRadius: 'var(--edp-radius-sm)',
                  minHeight: '100px',
                  resize: 'vertical'
                }}
                placeholder="Please enter prompt"
              />
            </div>

            <div style={{ marginBottom: 15 }}>
              <label style={{ display: 'block', marginBottom: 5, fontWeight: 'bold' }}>
                Ground_Truth
              </label>
              <textarea
                value={
                  editingItem.ground_truth ||
                  editingItem.gt ||
                  editingItem.true_answer ||
                  editingItem.reference ||
                  ''
                }
                onChange={(e) =>
                  setEditingItem((prev: any) => ({
                    ...prev,
                    ground_truth: e.target.value,
                    gt: e.target.value,
                    true_answer: e.target.value,
                    reference: e.target.value,
                  }))
                }
                style={{
                  width: '100%',
                  padding: '8px 12px',
                  border: '1px solid var(--edp-border)',
                  borderRadius: 'var(--edp-radius-sm)',
                  minHeight: '80px',
                  resize: 'vertical'
                }}
                placeholder="Please enter ground truth"
              />
            </div>

            <div style={{ marginBottom: 15 }}>
              <label style={{ display: 'block', marginBottom: 5, fontWeight: 'bold' }}>
                Mode_Answer
              </label>
              <textarea
                value={
                  editingItem.model_answer ||
                  editingItem.answer ||
                  editingItem.a ||
                  editingItem.response ||
                  ''
                }
                onChange={(e) =>
                  setEditingItem((prev: any) => ({
                    ...prev,
                    model_answer: e.target.value,
                    answer: e.target.value,
                    a: e.target.value,
                    response: e.target.value,
                  }))
                }
                style={{
                  width: '100%',
                  padding: '8px 12px',
                  border: '1px solid var(--edp-border)',
                  borderRadius: 'var(--edp-radius-sm)',
                  minHeight: '100px',
                  resize: 'vertical'
                }}
                placeholder="Please enter model answer"
              />
            </div>

            <div style={{ marginBottom: 15 }}>
              <label style={{ display: 'block', marginBottom: 5, fontWeight: 'bold' }}>
                Judging_Mode
              </label>
              <select
                value={editingItem.judging_mode || 'Ground_Truth评测'}
                onChange={(e) =>
                  setEditingItem((prev: any) => ({
                    ...prev,
                    judging_mode: e.target.value,
                  }))
                }
                style={{
                  width: '100%',
                  padding: '8px 12px',
                  border: '1px solid var(--edp-border)',
                  borderRadius: 'var(--edp-radius-sm)',
                  background: 'white',
                  fontSize: 'var(--edp-font-lg)',
                }}
              >
                <option value="Ground_Truth评测">Ground_Truth评测</option>
                <option value="基于Prompt评测">基于Prompt评测</option>
              </select>
            </div>
            
            <div style={{ display: 'flex', gap: 10, justifyContent: 'center', marginTop: 'var(--edp-space-5)' }}>
              <button
                className="btn"
                style={{ background: 'var(--edp-text2)' }}
                onClick={handleCancelItemEdit}
              >
                取消
              </button>
              <button
                className="btn btn-primary"
                onClick={handleSaveItem}
              >
                确认保存
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 新增项目模态框 */}
      {showAddItemModal && (
        <div className="modal-overlay" style={{
          position: 'fixed', top: 0, left: 0, right: 0, bottom: 0,
          background: 'rgba(0,0,0,0.5)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000
        }}>
          <div className="modal-content" style={{ background: 'white', padding: 'var(--edp-space-5)', borderRadius: 'var(--edp-radius-sm)', maxWidth: 600, width: '90%' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 'var(--edp-space-5)' }}>
              <h3 style={{ color: 'var(--edp-primary)', display: 'flex', alignItems: 'center', gap: '8px' }}>
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10"/>
                <line x1="12" y1="8" x2="12" y2="16"/>
                <line x1="8" y1="12" x2="16" y2="12"/>
              </svg>
              新增评测项目
            </h3>
              <button
                className="btn"
                style={{ padding: '4px 12px', display: 'flex', alignItems: 'center', gap: '4px' }}
                onClick={handleCloseAddItemModal}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <line x1="18" y1="6" x2="6" y2="18"/>
                  <line x1="6" y1="6" x2="18" y2="18"/>
                </svg>
                关闭
              </button>
            </div>
            
            <div style={{ marginBottom: 15 }}>
              <label style={{ display: 'block', marginBottom: 5, fontWeight: 'bold' }}>
                Question <span style={{ color: 'var(--edp-red)' }}>*</span>
              </label>
              <textarea
                value={newItem.question || newItem.q || ''}
                onChange={(e) => setNewItem(prev => ({
                  ...prev,
                  question: e.target.value,
                  q: e.target.value
                }))}
                style={{
                  width: '100%',
                  padding: '8px 12px',
                  border: '1px solid var(--edp-border)',
                  borderRadius: 'var(--edp-radius-sm)',
                  minHeight: '80px',
                  resize: 'vertical'
                }}
                placeholder="请输入问题"
              />
            </div>

            <div style={{ marginBottom: 15 }}>
              <label style={{ display: 'block', marginBottom: 5, fontWeight: 'bold' }}>
                Prompt
              </label>
              <textarea
                value={newItem.prompt || ''}
                onChange={(e) => setNewItem(prev => ({
                  ...prev,
                  prompt: e.target.value
                }))}
                style={{
                  width: '100%',
                  padding: '8px 12px',
                  border: '1px solid var(--edp-border)',
                  borderRadius: 'var(--edp-radius-sm)',
                  minHeight: '100px',
                  resize: 'vertical'
                }}
                placeholder="请输入 Prompt"
              />
            </div>

            <div style={{ marginBottom: 15 }}>
              <label style={{ display: 'block', marginBottom: 5, fontWeight: 'bold' }}>
                Ground_Truth
              </label>
              <textarea
                value={newItem.ground_truth || newItem.gt || ''}
                onChange={(e) => setNewItem(prev => ({
                  ...prev,
                  ground_truth: e.target.value,
                  gt: e.target.value
                }))}
                style={{
                  width: '100%',
                  padding: '8px 12px',
                  border: '1px solid var(--edp-border)',
                  borderRadius: 'var(--edp-radius-sm)',
                  minHeight: '80px',
                  resize: 'vertical'
                }}
                placeholder="请输入 Ground_Truth"
              />
            </div>

            <div style={{ marginBottom: 15 }}>
              <label style={{ display: 'block', marginBottom: 5, fontWeight: 'bold' }}>
                Mode_Answer
              </label>
              <textarea
                value={newItem.model_answer || newItem.answer || ''}
                onChange={(e) => setNewItem(prev => ({
                  ...prev,
                  model_answer: e.target.value,
                  answer: e.target.value
                }))}
                style={{
                  width: '100%',
                  padding: '8px 12px',
                  border: '1px solid var(--edp-border)',
                  borderRadius: 'var(--edp-radius-sm)',
                  minHeight: '100px',
                  resize: 'vertical'
                }}
                placeholder="请输入 Mode_Answer"
              />
            </div>

            <div style={{ marginBottom: 15 }}>
              <label style={{ display: 'block', marginBottom: 5, fontWeight: 'bold' }}>
                Judging_Mode
              </label>
              <select
                value={newItem.judging_mode || 'Ground_Truth评测'}
                onChange={(e) => setNewItem(prev => ({
                  ...prev,
                  judging_mode: e.target.value
                }))}
                style={{
                  width: '100%',
                  padding: '8px 12px',
                  border: '1px solid var(--edp-border)',
                  borderRadius: 'var(--edp-radius-sm)',
                  background: 'white',
                  fontSize: 'var(--edp-font-lg)',
                }}
              >
                <option value="Ground_Truth评测">Ground_Truth评测</option>
                <option value="基于Prompt评测">基于Prompt评测</option>
              </select>
            </div>
            
            <div style={{ display: 'flex', gap: 10, justifyContent: 'center', marginTop: 'var(--edp-space-5)' }}>
              <button
                className="btn"
                style={{ background: 'var(--edp-text2)' }}
                onClick={handleCloseAddItemModal}
              >
                取消
              </button>
              <button
                className="btn btn-primary"
                onClick={handleAddItem}
                disabled={!newItem.question && !newItem.q}
                style={{
                  opacity: (!newItem.question && !newItem.q) ? 0.6 : 1,
                  cursor: (!newItem.question && !newItem.q) ? 'not-allowed' : 'pointer'
                }}
              >
                确认新增
              </button>
            </div>
          </div>
        </div>
      )}

      {/* 评测集管理 */}
      <div className="card" style={{ marginBottom: 'var(--edp-space-5)' }}>
        <h2 style={{ marginBottom: 'var(--edp-space-5)', display: 'inline-block' }}><svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ marginRight: 'var(--edp-space-2)', verticalAlign: 'middle' }}><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/></svg>评测集管理</h2>
        
        <div style={{ marginBottom: 15, display: 'flex', gap: 10, alignItems: 'center' }}>
          <input
            type="text"
            placeholder="基于评测集名称查找..."
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            style={{
              padding: '8px 12px',
              border: '1px solid var(--edp-border)',
              borderRadius: 'var(--edp-radius-sm)',
              flex: 1,
              maxWidth: 300
            }}
          />
          <button
            className="btn btn-primary"
            onClick={handleOpenNewDatasetModal}
            disabled={uploading}
            style={{ padding: '6px 12px', fontSize: 'var(--edp-font-lg)' }}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10"/>
                <line x1="12" y1="8" x2="12" y2="16"/>
                <line x1="8" y1="12" x2="16" y2="12"/>
              </svg>
              新增评测集
          </button>
          <button
            className="btn"
            onClick={handleOpenLangfuseImport}
            disabled={uploading}
            style={{ padding: '6px 12px', fontSize: 'var(--edp-font-lg)', background: 'var(--edp-primary)', color: 'var(--edp-card)' }}
          >
            <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ verticalAlign: 'middle', marginRight: 'var(--edp-space-1)' }}>
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
              <polyline points="7 10 12 15 17 10"/>
              <line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
            从 Langfuse 导入
          </button>
          <button
            className="btn btn-danger"
            onClick={handleBatchDelete}
            disabled={deleting || selectedDatasets.length === 0}
            style={{ padding: '6px 12px', fontSize: 'var(--edp-font-lg)' }}
          >
            {deleting ? (
                <>
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite' }}>
                    <circle cx="12" cy="12" r="10"/>
                    <path d="M12 6v6l4 2"/>
                  </svg>
                  删除中...
                </>
              ) : (
                <>
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <polyline points="3 6 5 6 21 6"/>
                    <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                  </svg>
                  批量删除 ({selectedDatasets.length})
                </>
              )}
          </button>
        </div>

        {loadingDatasets ? (
          <div style={{ color: 'var(--edp-text2)' }}>⏳ 加载数据集中...</div>
        ) : filteredDatasets.length === 0 ? (
          <p style={{ color: 'var(--edp-text2)' }}>暂无数据集</p>
        ) : (
          <table className="results-table dataset-table" style={{ width: '100%' }}>
            <colgroup>
              <col style={{ width: '48px' }} />
              <col style={{ width: '20%' }} />
              <col style={{ width: '25%' }} />
              <col style={{ width: '15%' }} />
              <col style={{ width: '20%' }} />
              <col style={{ width: '20%' }} />
            </colgroup>
            <thead>
              <tr>
                  <th style={{ textAlign: 'center' }}>
                    <input
                      type="checkbox"
                      checked={selectedDatasets.length === filteredDatasets.length && filteredDatasets.length > 0}
                      onChange={toggleSelectAll}
                      disabled={deleting}
                    />
                  </th>
                  <th style={{ textAlign: 'center' }}>评测集ID</th>
                  <th style={{ textAlign: 'center' }}>评测集名称</th>
                  <th style={{ textAlign: 'center' }}>数据条数</th>
                  <th style={{ textAlign: 'center' }}>创建时间</th>
                  <th style={{ textAlign: 'center' }}>操作</th>
                </tr>
            </thead>
            <tbody>
              {filteredDatasets.map(dataset => (
                <Fragment key={dataset.id}>
                <tr 
                  className={currentDatasetId === dataset.id ? 'row-selected' : ''}
                  style={{ 
                    background: currentDatasetId === dataset.id ? 'var(--edp-primary-soft)' : 'transparent',
                    cursor: 'pointer'
                  }}
                  onClick={() => handleSelectDataset(dataset.id)}
                >
                  <td style={{ textAlign: 'center' }}>
                    <input
                      type="checkbox"
                      checked={selectedDatasets.includes(dataset.id)}
                      onChange={(e) => {
                        e.stopPropagation();
                        toggleDatasetSelection(dataset.id);
                      }}
                      disabled={deleting}
                    />
                  </td>
                  <td className="cell-ellipsis" title={dataset.id} style={{ textAlign: 'center' }}><code>{dataset.id}</code></td>
                  <td style={{ textAlign: 'center' }}>{dataset.name}</td>
                  <td style={{ textAlign: 'center' }}>{dataset.item_count}</td>
                  <td style={{ textAlign: 'center' }}>{formatDate(dataset.created_at)}</td>
                  <td className="cell-actions">
                    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 'var(--edp-space-1)', justifyContent: 'center' }}>
                      <div className="dataset-export-menu">
                        <button
                          type="button"
                          className="btn"
                          onClick={(e: React.MouseEvent) => {
                            e.stopPropagation();
                            setExportMenuOpenId(prev => (prev === dataset.id ? null : dataset.id));
                          }}
                          disabled={deleting}
                          style={{ padding: '6px' }}
                          title="导出"
                          aria-label="导出"
                          aria-expanded={exportMenuOpenId === dataset.id}
                          aria-haspopup="menu"
                        >
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ verticalAlign: 'middle' }}>
                            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
                            <polyline points="7 10 12 15 17 10"/>
                            <line x1="12" y1="15" x2="12" y2="3"/>
                          </svg>
                        </button>
                        {exportMenuOpenId === dataset.id && (
                          <div className="dataset-export-menu__panel" role="menu">
                            <button
                              type="button"
                              role="menuitem"
                              className="dataset-export-menu__item"
                              onClick={(e: React.MouseEvent) => {
                                e.stopPropagation();
                                setExportMenuOpenId(null);
                                handleExportDataset(dataset.id, 'xlsx');
                              }}
                            >
                              导出 XLSX
                            </button>
                            <button
                              type="button"
                              role="menuitem"
                              className="dataset-export-menu__item"
                              onClick={(e: React.MouseEvent) => {
                                e.stopPropagation();
                                setExportMenuOpenId(null);
                                handleExportDataset(dataset.id, 'csv');
                              }}
                            >
                              导出 CSV
                            </button>
                            <button
                              type="button"
                              role="menuitem"
                              className="dataset-export-menu__item"
                              onClick={(e: React.MouseEvent) => {
                                e.stopPropagation();
                                setExportMenuOpenId(null);
                                handleExportDataset(dataset.id, 'json');
                              }}
                            >
                              导出 JSON
                            </button>
                          </div>
                        )}
                      </div>
                      <button
                        className="btn btn-primary"
                        onClick={(e: React.MouseEvent) => { e.stopPropagation(); handleCopyDataset(dataset.id); }}
                        disabled={deleting || copying === dataset.id}
                        style={{ padding: '6px' }}
                        title="复制"
                      >
                        {copying === dataset.id ? (
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ animation: 'spin 1s linear infinite', verticalAlign: 'middle' }}>
                            <circle cx="12" cy="12" r="10"/>
                            <path d="M12 6v6l4 2"/>
                          </svg>
                        ) : (
                          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ verticalAlign: 'middle' }}>
                            <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/>
                            <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                          </svg>
                        )}
                      </button>
                      <button
                        className="btn btn-danger"
                        onClick={(e: React.MouseEvent) => { e.stopPropagation(); handleDeleteDataset(dataset.id); }}
                        disabled={deleting}
                        style={{ padding: '6px' }}
                        title="删除"
                      >
                        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" style={{ verticalAlign: 'middle' }}>
                          <polyline points="3 6 5 6 21 6"/>
                          <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                        </svg>
                      </button>
                    </div>
                  </td>
                </tr>
                {currentDatasetId === dataset.id && (
                  <tr ref={detailRowRef} className="detail-row">
                    <td colSpan={6} style={{ padding: 0, border: 'none' }}>
                      <div className="dataset-detail-panel">
                        {renderDetailContent()}
                      </div>
                    </td>
                  </tr>
                )}
                </Fragment>
              ))}
            </tbody>
          </table>
        )}
      </div>

    </div>
  );
}