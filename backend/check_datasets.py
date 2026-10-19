import asyncio
import sys
import os
import json

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import init_db, get_session, Dataset

async def check_datasets():
    print("\n" + "="*60)
    print("检查数据库中的数据集")
    print("="*60)
    
    # 初始化数据库
    await init_db()
    
    # 获取所有数据集
    async with get_session() as session:
        result = await session.execute(
            select(Dataset).order_by(Dataset.created_at.desc())
        )
        datasets = result.scalars().all()
        
        print(f"数据库中共有 {len(datasets)} 个数据集")
        
        for i, dataset in enumerate(datasets):
            print(f"\n数据集 {i+1}:")
            print(f"  ID: {dataset.id}")
            print(f"  名称: {dataset.name}")
            print(f"  问题数量: {dataset.item_count}")
            print(f"  创建时间: {dataset.created_at}")
            
            # 检查第一个问题是否有上下文
            try:
                data = json.loads(dataset.data)
                if data and isinstance(data, list) and len(data) > 0:
                    first_item = data[0]
                    if 'context' in first_item and first_item['context']:
                        print(f"  第一个问题有上下文: ✅")
                        print(f"  上下文长度: {len(first_item['context'])} 字符")
                    else:
                        print(f"  第一个问题有上下文: ❌")
            except Exception as e:
                print(f"  检查上下文时出错: {e}")
    
    print("\n" + "="*60)
    print("检查完成")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(check_datasets())
