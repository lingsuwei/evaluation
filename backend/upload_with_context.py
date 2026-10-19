import asyncio
import json
import sys
import os
import uuid

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import init_db, get_session, Dataset
from schemas import DatasetItem

async def upload_with_context():
    print("\n" + "="*60)
    print("手动上传带上下文的数据集")
    print("="*60)
    
    # 读取带上下文的数据集
    dataset_path = "../datasets/onto-eval-20-with-context.json"
    if not os.path.exists(dataset_path):
        print(f"❌ 数据集文件不存在: {dataset_path}")
        return
    
    with open(dataset_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    print(f"已读取数据集: {len(data)} 个问题")
    
    # 验证数据格式
    errors = []
    for idx, item in enumerate(data):
        try:
            DatasetItem(**item)
            if 'context' in item and item['context']:
                print(f"  ✅ 问题{idx+1}: 有上下文")
            else:
                print(f"  ⚠️ 问题{idx+1}: 上下文为空")
        except Exception as e:
            errors.append(f"Item {idx}: {str(e)}")
    
    if errors:
        print(f"❌ 数据验证失败: {errors}")
        return
    
    # 初始化数据库
    await init_db()
    
    # 上传数据集
    async with get_session() as session:
        # 生成新的数据集ID和名称
        dataset_id = f"ds_{uuid.uuid4().hex[:8]}"
        dataset_name = "onto-eval-20-with-context"
        
        # 检查是否已存在同名数据集
        existing = await session.execute(
            select(Dataset).where(Dataset.name == dataset_name)
        )
        existing_dataset = existing.scalar_one_or_none()
        
        if existing_dataset:
            print(f"⚠️ 已存在同名数据集，将更新: {existing_dataset.id}")
            dataset = existing_dataset
            dataset.data = json.dumps(data)
            dataset.item_count = len(data)
        else:
            print(f"创建新数据集: {dataset_name}")
            dataset = Dataset(
                id=dataset_id,
                name=dataset_name,
                data=json.dumps(data),
                item_count=len(data)
            )
            session.add(dataset)
        
        await session.commit()
        print(f"✅ 数据集上传成功")
    
    print("\n" + "="*60)
    print("上传完成")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(upload_with_context())
