import http.client
import json

# 获取API数据
conn = http.client.HTTPSConnection("openapi.c5game.com")
payload = ''
headers = {}
conn.request("GET",
             "//merchant/account/v2/steamInfo?minRelationId=0&limit=1000&app-key=",
             payload, headers)
res = conn.getresponse()
data = res.read()

# 解析JSON响应
response_data = json.loads(data.decode("utf-8"))

# 提取steamIds
steam_ids = []

# 检查响应是否成功
if response_data.get('success'):
    # 获取data中的steamList
    steam_list = response_data.get('data', {}).get('steamList', [])

    # 遍历steamList，提取每个steamId
    for steam_info in steam_list:
        steam_id = steam_info.get('steamId')
        if steam_id:
            # 转换为字符串格式
            steam_ids.append(str(steam_id))
else:
    print(f"API请求失败: {response_data.get('errorMsg', '未知错误')}")

# 构建输出JSON
output_data = {
    "steamIds": steam_ids
}

# 保存到文件
output_filename = "steam_ids.json"
with open(output_filename, 'w', encoding='utf-8') as f:
    json.dump(output_data, f, ensure_ascii=False, indent=4)

print(f"成功提取 {len(steam_ids)} 个steamId")
print(f"数据已保存到 {output_filename}")

# 可选：显示前几个steamId作为预览
if steam_ids:
    print("\n预览前5个steamId:")
    for i, steam_id in enumerate(steam_ids[:5]):
        print(f"  {i + 1}. {steam_id}")