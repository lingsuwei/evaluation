import asyncio
import json
import sys
import os

# 添加当前目录到Python路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from database import init_db, get_session, Dataset

async def check_dataset_context():
    print("\n" + "="*60)
    print("检查数据集是否包含上下文")
    print("="*60)
    
    # 初始化数据库
    await init_db()
    
    async with get_session() as session:
        # 获取所有数据集
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
            
            # 检查第一个问题是否有上下文
            try:
                data = json.loads(dataset.data)
                if data and isinstance(data, list) and len(data) > 0:
                    first_item = data[0]
                    print(f"  第一个问题: {first_item.get('question', '')[:50]}...")
                    
                    if 'context' in first_item:
                        if first_item['context']:
                            print(f"  ✅ 有上下文，长度: {len(first_item['context'])} 字符")
                            print(f"     上下文前100字符: {first_item['context'][:100]}...")
                        else:
                            print(f"  ⚠️ 上下文为空字符串")
                    else:
                        print(f"  ❌ 没有context字段")
                else:
                    print(f"  ⚠️ 数据集为空列表")
            except Exception as e:
                print(f"  ❌ 解析错误: {e}")
    
    print("\n" + "="*60)
    print("检查完成")
    print("="*60)

if __name__ == "__main__":
    asyncio.run(check_dataset_context())
