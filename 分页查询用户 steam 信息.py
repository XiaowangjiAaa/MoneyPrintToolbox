import http.client
import json

# 获取API数据
conn = http.client.HTTPSConnection("openapi.c5game.com")
payload = ''
headers = {}

conn.request(
    "GET",
    "/merchant/account/v2/steamInfo?minRelationId=0&limit=1000&app-key=",
    payload,
    headers
)

res = conn.getresponse()
data = res.read()

# 解析JSON响应
response_data = json.loads(data.decode("utf-8"))

# 提取数据
steam_list_output = []
steam_ids = []

# 检查响应是否成功
if response_data.get("success"):
    steam_list = response_data.get("data", {}).get("steamList", [])

    for steam_info in steam_list:
        steam_id = steam_info.get("steamId")
        avatar = steam_info.get("avatar", "")
        nickname = steam_info.get("nickname", "")
        username = steam_info.get("username", "")
        relation_id = steam_info.get("relationId")
        auto_type = steam_info.get("autoType")

        if steam_id:
            steam_id_str = str(steam_id)
            steam_ids.append(steam_id_str)

            steam_list_output.append({
                "steamId": steam_id_str,
                "avatar": avatar,
                "nickname": nickname,
                "username": username,
                "relationId": relation_id,
                "autoType": auto_type
            })
else:
    print(f"API请求失败: {response_data.get('errorMsg', '未知错误')}")

# 构建输出JSON
output_data = {
    "steamIds": steam_ids,
    "steamAccounts": steam_list_output
}

# 保存到文件
output_filename = "steam_accounts.json"
with open(output_filename, "w", encoding="utf-8") as f:
    json.dump(output_data, f, ensure_ascii=False, indent=4)

print(f"成功提取 {len(steam_ids)} 个 steam 账号")
print(f"数据已保存到 {output_filename}")

# 预览前5个
if steam_list_output:
    print("\n预览前5个账号信息：")
    for i, item in enumerate(steam_list_output[:5], 1):
        print(f"{i}. steamId   : {item['steamId']}")
        print(f"   nickname  : {item['nickname']}")
        print(f"   username  : {item['username']}")
        print(f"   avatar    : {item['avatar']}")
        print(f"   relationId: {item['relationId']}")
        print(f"   autoType  : {item['autoType']}")
        print()