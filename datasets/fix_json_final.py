import json

# 读取原始文件
with open('onto-eval-20-with-context.json', 'r', encoding='utf-8') as f:
    content = f.read()

# 查找所有中文引号并替换为转义形式
# 中文左引号 " 和右引号 "
content = content.replace('"', '\\"').replace('"', '\\"')

# 保存
with open('onto-eval-20-with-context.json', 'w', encoding='utf-8') as f:
    f.write(content)

print('中文引号转义完成！')

# 验证JSON格式
try:
    with open('onto-eval-20-with-context.json', 'r', encoding='utf-8') as f:
        data = json.load(f)
    print(f'✅ JSON格式验证通过！包含 {len(data)} 个问题条目')
except json.JSONDecodeError as e:
    print(f'❌ JSON格式错误: {e}')
