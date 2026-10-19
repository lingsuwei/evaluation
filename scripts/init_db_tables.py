"""
初始化数据库表
"""
import asyncio
import sys
sys.path.insert(0, 'backend')

from database import engine, Base
from services.otel_service import TraceStore


async def init_tables():
    print("=" * 70)
    print("🔧 初始化数据库表")
    print("=" * 70)
    
    async with engine.begin() as conn:
        print("\n📋 创建 llm_traces 表...")
        await conn.run_sync(Base.metadata.create_all)
        print("✅ llm_traces 表创建成功!")
    
    print("\n✅ 数据库初始化完成!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(init_tables())
