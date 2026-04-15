import requests

url = "https://openapi.c5game.com/merchant/order/v1/list"

params = {
    "app-key": "",
    "appId": 730,
    "page": 1,
    "limit": 20
}

resp = requests.get(url, params=params, timeout=15)
resp.raise_for_status()

data = resp.json()
print(data)