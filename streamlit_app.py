# -*- coding: utf-8 -*-
import http.client
import json
import sqlite3
from datetime import datetime
from collections import defaultdict
import threading
import os
from flask import Flask, render_template_string, request, redirect, url_for, jsonify

app = Flask(__name__)

# 获取当前脚本所在目录，确保路径正确
config_path = os.path.join(os.path.dirname(__file__), 'config.json')

def load_config():
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
            return config.get("APP_KEY")
    except FileNotFoundError:
        print("错误：找不到 config.json 文件")
        return None

APP_KEY = load_config()
DEFAULT_APP_ID = "730"
DB_PATH = "steam_inventory_app.db"
FAILED_SYNC_FILE = "failed_inventory_sync.json"


# =========================
# 同步任务状态
# =========================
SYNC_STATE = {
    "running": False,
    "cancel_requested": False,
    "current_steam_id": "",
    "finished": 0,
    "total": 0,
    "success_count": 0,
    "empty_count": 0,
    "failed_count": 0,
    "failed_ids": [],
    "failed_messages": [],
    "last_message": "",
    "last_error": "",
    "started_at": "",
    "ended_at": "",
    "app_id": DEFAULT_APP_ID,
}
SYNC_LOCK = threading.Lock()


def get_sync_state():
    with SYNC_LOCK:
        return dict(SYNC_STATE)


def reset_sync_state(app_id=DEFAULT_APP_ID):
    with SYNC_LOCK:
        SYNC_STATE["running"] = False
        SYNC_STATE["cancel_requested"] = False
        SYNC_STATE["current_steam_id"] = ""
        SYNC_STATE["finished"] = 0
        SYNC_STATE["total"] = 0
        SYNC_STATE["success_count"] = 0
        SYNC_STATE["empty_count"] = 0
        SYNC_STATE["failed_count"] = 0
        SYNC_STATE["failed_ids"] = []
        SYNC_STATE["failed_messages"] = []
        SYNC_STATE["last_message"] = ""
        SYNC_STATE["last_error"] = ""
        SYNC_STATE["started_at"] = ""
        SYNC_STATE["ended_at"] = ""
        SYNC_STATE["app_id"] = app_id


def update_sync_state(**kwargs):
    with SYNC_LOCK:
        for k, v in kwargs.items():
            if k in SYNC_STATE:
                SYNC_STATE[k] = v


def request_cancel_sync():
    with SYNC_LOCK:
        if not SYNC_STATE["running"]:
            return False
        SYNC_STATE["cancel_requested"] = True
        return True


def _inventory_sync_worker(app_id):
    error = sync_accounts_to_db()
    if error:
        update_sync_state(
            running=False,
            current_steam_id="",
            last_error=f"同步所有账号失败：{error}",
            last_message="库存同步任务启动失败",
            ended_at=now_str(),
        )
        return

    accounts = get_all_accounts_from_db()
    if not accounts:
        update_sync_state(
            running=False,
            current_steam_id="",
            last_error="没有可用账号，无法同步库存",
            last_message="没有可用账号，无法同步库存",
            ended_at=now_str(),
        )
        return

    update_sync_state(
        total=len(accounts),
        finished=0,
        success_count=0,
        empty_count=0,
        failed_count=0,
        failed_ids=[],
        failed_messages=[],
        current_steam_id="",
        started_at=now_str(),
        ended_at="",
        app_id=app_id,
        last_message="库存同步进行中",
        last_error="",
    )

    failed_ids = []
    failed_msgs = []
    success_count = 0
    empty_count = 0

    for idx, acc in enumerate(accounts, start=1):
        state = get_sync_state()
        if state["cancel_requested"]:
            break

        steam_id = str(acc["steam_id"]).strip()
        if not steam_id:
            update_sync_state(finished=idx)
            continue

        update_sync_state(
            current_steam_id=steam_id,
            finished=idx - 1,
            last_message=f"正在同步 SteamID: {steam_id}",
        )

        result = sync_inventory_to_db(steam_id, app_id=app_id)

        if result == "empty":
            success_count += 1
            empty_count += 1
        elif result:
            failed_ids.append(steam_id)
            failed_msgs.append(f"{steam_id}: {result}")
        else:
            success_count += 1

        update_sync_state(
            finished=idx,
            success_count=success_count,
            empty_count=empty_count,
            failed_count=len(failed_ids),
            failed_ids=list(failed_ids),
            failed_messages=list(failed_msgs[:20]),
            current_steam_id=steam_id,
        )

    save_failed_sync_ids(failed_ids)

    final_state = get_sync_state()
    was_cancelled = final_state["cancel_requested"]

    last_message = (
        f"库存同步已取消：成功 {success_count} 个，其中库存为空 {empty_count} 个，失败 {len(failed_ids)} 个"
        if was_cancelled else
        f"库存同步完成：成功 {success_count} 个，其中库存为空 {empty_count} 个，失败 {len(failed_ids)} 个"
    )
    last_error = " | ".join(failed_msgs[:8])
    if len(failed_msgs) > 8:
        last_error += f" ... 其余 {len(failed_msgs) - 8} 个失败"

    update_sync_state(
        running=False,
        cancel_requested=False,
        current_steam_id="",
        finished=final_state["finished"],
        success_count=success_count,
        empty_count=empty_count,
        failed_count=len(failed_ids),
        failed_ids=list(failed_ids),
        failed_messages=list(failed_msgs[:20]),
        last_message=last_message,
        last_error=last_error,
        ended_at=now_str(),
    )


def start_inventory_sync_background(app_id=DEFAULT_APP_ID):
    state = get_sync_state()
    if state["running"]:
        return False, "当前已经有库存同步任务在运行"

    reset_sync_state(app_id=app_id)
    update_sync_state(
        running=True,
        cancel_requested=False,
        started_at=now_str(),
        app_id=app_id,
        last_message="库存同步任务已启动",
        last_error="",
    )

    t = threading.Thread(target=_inventory_sync_worker, args=(app_id,), daemon=True)
    t.start()
    return True, "库存同步任务已启动"



# =========================
# 数据库
# =========================
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS steam_accounts (
        steam_id TEXT PRIMARY KEY,
        nickname TEXT,
        username TEXT,
        avatar TEXT,
        updated_at TEXT
    )
    """)

    # 关键点：
    # 这里的唯一键改成 UNIQUE(steam_id, app_id, asset_id)
    # 不再用 item_key 做库存唯一性
    cur.execute("""
    CREATE TABLE IF NOT EXISTS inventory_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        steam_id TEXT NOT NULL,
        app_id TEXT NOT NULL,
        item_key TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        token TEXT,
        style_token TEXT,
        name TEXT,
        short_name TEXT,
        image_url TEXT,
        price REAL DEFAULT 0,
        status INTEGER,
        if_tradable INTEGER DEFAULT 0,
        wear TEXT,
        style_id TEXT,
        weapon_name TEXT,
        exterior_name TEXT,
        updated_at TEXT,
        UNIQUE(steam_id, app_id, asset_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS item_purchase_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        steam_id TEXT NOT NULL,
        app_id TEXT NOT NULL,
        asset_id TEXT NOT NULL,
        purchase_price REAL DEFAULT 0,
        updated_at TEXT,
        UNIQUE(steam_id, app_id, asset_id)
    )
    """)


    cur.execute("""
    CREATE TABLE IF NOT EXISTS group_purchase_prices (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        steam_id TEXT NOT NULL,
        app_id TEXT NOT NULL,
        group_name TEXT NOT NULL,
        default_purchase_price REAL DEFAULT 0,
        updated_at TEXT,
        UNIQUE(steam_id, app_id, group_name)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS seller_orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id TEXT NOT NULL UNIQUE,
        steam_id TEXT,
        product_id TEXT,
        app_id TEXT,
        item_id TEXT,
        name TEXT,
        market_hash_name TEXT,
        image_url TEXT,
        order_price REAL DEFAULT 0,
        order_status INTEGER,
        status_name TEXT,
        order_create_time INTEGER,
        asset_id TEXT,
        style_id TEXT,
        wear TEXT,
        weapon_name TEXT,
        exterior_name TEXT,
        updated_at TEXT
    )
    """)

    conn.commit()
    conn.close()

# =========================
# 工具函数
# =========================
def now_str():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_float(v, default=0.0):
    try:
        if v is None or v == "":
            return default
        return float(v)
    except Exception:
        return default


def bool_to_int(v):
    return 1 if v else 0


def save_failed_sync_ids(failed_ids):
    with open(FAILED_SYNC_FILE, "w", encoding="utf-8") as f:
        json.dump({
            "failed_ids": failed_ids,
            "updated_at": now_str()
        }, f, ensure_ascii=False, indent=4)


def load_failed_sync_ids():
    try:
        with open(FAILED_SYNC_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("failed_ids", [])
    except Exception:
        return []


def translate_status(status):
    mapping = {
        0: "正常",
        1: "在售中",
        2: "平台禁售",
        3: "永久不可交易",
        4: "暂时不可交易",
        5: "待发货",
        6: "处理中",
        7: "可出租",
        10: "已完成",
        11: "已取消",
    }
    return mapping.get(status, f"未知({status})")


def build_item_key(item):
    asset_id = item.get("assetId")
    token = item.get("token")
    style_token = item.get("styleToken")

    if asset_id:
        return f"assetId:{asset_id}"
    if token:
        return f"token:{token}"
    if style_token:
        return f"styleToken:{style_token}"

    return f"name:{item.get('name', '')}|price:{item.get('price', '')}"


def item_search_blob(item):
    parts = [
        str(item.get("name", "")),
        str(item.get("short_name", "")),
        str(item.get("weapon_name", "")),
        str(item.get("exterior_name", "")),
        str(item.get("wear", "")),
        str(item.get("style_id", "")),
        str(item.get("asset_id", "")),
        str(item.get("purchase_price", "")),
        str(item.get("status_text", "")),
    ]
    return " ".join(parts).lower()


def summary_search_blob(group):
    parts = [
        str(group.get("name", "")),
        str(group.get("short_name", "")),
        str(group.get("weapon_name", "")),
        str(group.get("exterior_name", "")),
        str(group.get("style_summary", "")),
        str(group.get("default_purchase_price", "")),
    ]
    return " ".join(parts).lower()


def get_status_bucket(item):
    status = int(item.get("status", 0))
    if_tradable = bool(item.get("if_tradable", 0))

    if status == 1:
        return "on_sale"
    if if_tradable:
        return "tradable"
    return "not_tradable"


def apply_inventory_filter(items, inventory_filter):
    if inventory_filter == "on_sale":
        return [x for x in items if int(x.get("status", 0)) == 1]
    if inventory_filter == "tradable":
        return [x for x in items if bool(x.get("if_tradable", 0)) and int(x.get("status", 0)) != 1]
    if inventory_filter == "not_tradable":
        return [x for x in items if (not bool(x.get("if_tradable", 0))) and int(x.get("status", 0)) != 1]
    return items


def unix_ms_to_str(ts):
    try:
        ts = int(ts)
        if ts <= 0:
            return ""
        if ts > 10**12:
            return datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d %H:%M:%S")
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


# =========================
# C5 API
# =========================
def c5_get(path: str):
    conn = http.client.HTTPSConnection("openapi.c5game.com", timeout=30)
    try:
        conn.request("GET", path)
        res = conn.getresponse()
        raw = res.read()
        text = raw.decode("utf-8")
        return json.loads(text), None
    except Exception as e:
        return None, str(e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def c5_post_json(path: str, payload: dict):
    conn = http.client.HTTPSConnection("openapi.c5game.com", timeout=30)
    try:
        body = json.dumps(payload, ensure_ascii=False)
        headers = {
            "Content-Type": "application/json"
        }
        conn.request("POST", path, body=body.encode("utf-8"), headers=headers)
        res = conn.getresponse()
        raw = res.read()
        text = raw.decode("utf-8")
        return json.loads(text), None
    except Exception as e:
        return None, str(e)
    finally:
        try:
            conn.close()
        except Exception:
            pass


def fetch_steam_accounts_from_api():
    path = f"/merchant/account/v2/steamInfo?minRelationId=0&limit=1000&app-key={APP_KEY}"
    response_data, error = c5_get(path)
    if error:
        return None, error

    if not response_data.get("success"):
        return None, response_data.get("errorMsg", "获取账号失败")

    steam_list = response_data.get("data", {}).get("steamList", [])
    result = []

    for steam_info in steam_list:
        steam_id = steam_info.get("steamId")
        if not steam_id:
            continue

        result.append({
            "steam_id": str(steam_id),
            "nickname": steam_info.get("nickname", ""),
            "username": steam_info.get("username", ""),
            "avatar": steam_info.get("avatar", ""),
        })

    return result, None


def fetch_inventory_from_api(steam_id, app_id=DEFAULT_APP_ID, language="zh"):
    all_items = []
    start_asset_id = "0"
    max_pages = 20

    for _ in range(max_pages):
        path = (
            f"/merchant/inventory/v2/{steam_id}/{app_id}"
            f"?language={language}&startAssetId={start_asset_id}&count=1000&app-key={APP_KEY}"
        )

        response_data, error = c5_get(path)
        if error:
            return None, error

        if not response_data.get("success"):
            return None, response_data.get("errorMsg", "获取库存失败")

        data = response_data.get("data", {})
        current_list = data.get("list", []) or []
        all_items.extend(current_list)

        last_asset_id = data.get("lastAssetId")
        total = data.get("total", len(all_items))

        if not current_list:
            break
        if not last_asset_id:
            break
        if len(all_items) >= total:
            break

        start_asset_id = str(last_asset_id)

    return all_items, None


def fetch_seller_order_list_from_api(steam_id=None, app_id=DEFAULT_APP_ID, status="10", page=1, limit=100):
    params = [f"app-key={APP_KEY}", f"page={page}", f"limit={limit}"]

    if steam_id:
        params.append(f"steamId={steam_id}")
    if app_id:
        params.append(f"appId={app_id}")
    if status != "":
        params.append(f"status={status}")

    path = "/merchant/order/v1/list?" + "&".join(params)
    response_data, error = c5_get(path)
    if error:
        return None, error

    if not response_data.get("success"):
        return None, response_data.get("errorMsg", "获取卖家订单列表失败")

    return response_data.get("data", {}), None


def sale_inventory_item(token, style_token, price, description="", accept_bargain=0):
    path = f"/merchant/sale/v2/create?app-key={APP_KEY}"
    payload = {
        "dataList": [
            {
                "price": safe_float(price, 0),
                "description": description or "",
                "acceptBargain": int(accept_bargain),
                "token": token,
                "styleToken": style_token
            }
        ]
    }

    response_data, error = c5_post_json(path, payload)
    if error:
        return None, error

    if not response_data.get("success"):
        return None, response_data.get("errorMsg", "上架失败")

    return response_data.get("data", {}), None


# =========================
# 数据持久化
# =========================
def sync_accounts_to_db():
    accounts, error = fetch_steam_accounts_from_api()
    if error:
        return error

    conn = get_conn()
    cur = conn.cursor()
    current_time = now_str()

    for acc in accounts:
        cur.execute("""
        INSERT INTO steam_accounts (steam_id, nickname, username, avatar, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(steam_id) DO UPDATE SET
            nickname=excluded.nickname,
            username=excluded.username,
            avatar=excluded.avatar,
            updated_at=excluded.updated_at
        """, (
            acc["steam_id"],
            acc["nickname"],
            acc["username"],
            acc["avatar"],
            current_time
        ))

    conn.commit()
    conn.close()
    return None


def get_all_accounts_from_db():
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT steam_id, nickname, username, avatar, updated_at
    FROM steam_accounts
    ORDER BY updated_at DESC, steam_id ASC
    """)
    rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def sync_inventory_to_db(steam_id, app_id=DEFAULT_APP_ID):
    raw_items, error = fetch_inventory_from_api(steam_id, app_id=app_id, language="zh")
    if error:
        # 库存为空，不算失败
        if str(error).strip() == "库存为空":
            conn = get_conn()
            cur = conn.cursor()

            # 既然库存为空，就把这个账号当前 app_id 下的库存记录清空
            cur.execute("""
            DELETE FROM inventory_items
            WHERE steam_id = ? AND app_id = ?
            """, (steam_id, app_id))

            conn.commit()
            conn.close()

            return "empty"   # 特殊状态：库存为空，但同步成功
        return error

    conn = get_conn()
    cur = conn.cursor()
    current_time = now_str()

    current_asset_ids = set()

    for item in raw_items:
        item_info = item.get("itemInfo", {}) or {}
        asset_info = item.get("assetInfo", {}) or {}

        asset_id = str(item.get("assetId", "") or "").strip()
        if not asset_id:
            continue

        current_asset_ids.add(asset_id)

        item_key = build_item_key(item)
        token = item.get("token", "") or ""
        style_token = item.get("styleToken", "") or ""
        name = item.get("name", "")
        short_name = item.get("shortName", "")
        image_url = item.get("imageUrl", "")
        price = safe_float(item.get("price", 0))
        status = int(item.get("status", 0) or 0)
        if_tradable = bool_to_int(item.get("ifTradable", False))
        wear = asset_info.get("wear", "")
        style_id = str(asset_info.get("styleId", "") or "")
        weapon_name = item_info.get("weaponName", "")
        exterior_name = item_info.get("exteriorName", "")

        cur.execute("""
        INSERT INTO inventory_items (
            steam_id, app_id, item_key, asset_id, token, style_token,
            name, short_name, image_url, price,
            status, if_tradable, wear, style_id, weapon_name, exterior_name, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(steam_id, app_id, asset_id) DO UPDATE SET
            item_key=excluded.item_key,
            token=excluded.token,
            style_token=excluded.style_token,
            name=excluded.name,
            short_name=excluded.short_name,
            image_url=excluded.image_url,
            price=excluded.price,
            status=excluded.status,
            if_tradable=excluded.if_tradable,
            wear=excluded.wear,
            style_id=excluded.style_id,
            weapon_name=excluded.weapon_name,
            exterior_name=excluded.exterior_name,
            updated_at=excluded.updated_at
        """, (
            steam_id, app_id, item_key, asset_id, token, style_token,
            name, short_name, image_url, price,
            status, if_tradable, wear, style_id, weapon_name, exterior_name, current_time
        ))

    # 删除本次同步中已经不存在的旧库存记录
    cur.execute("""
    SELECT id, asset_id
    FROM inventory_items
    WHERE steam_id = ? AND app_id = ?
    """, (steam_id, app_id))
    db_rows = cur.fetchall()

    ids_to_delete = []
    for row in db_rows:
        db_id = row["id"]
        db_asset_id = str(row["asset_id"] or "").strip()

        if not db_asset_id or db_asset_id not in current_asset_ids:
            ids_to_delete.append(db_id)

    if ids_to_delete:
        placeholders = ",".join(["?"] * len(ids_to_delete))
        cur.execute(f"DELETE FROM inventory_items WHERE id IN ({placeholders})", ids_to_delete)

    conn.commit()
    conn.close()
    return None


def sync_seller_orders_to_db(app_id=DEFAULT_APP_ID, status="10"):
    accounts = get_all_accounts_from_db()
    if not accounts:
        return "没有账号，请先同步所有账号"

    conn = get_conn()
    cur = conn.cursor()
    current_time = now_str()

    success_accounts = 0
    failed = []

    for acc in accounts:
        steam_id = str(acc["steam_id"]).strip()
        if not steam_id:
            continue

        page = 1
        max_pages = 50

        try:
            while page <= max_pages:
                data, error = fetch_seller_order_list_from_api(
                    steam_id=steam_id,
                    app_id=app_id,
                    status=status,
                    page=page,
                    limit=100
                )
                if error:
                    failed.append(f"{steam_id}: {error}")
                    break

                order_list = data.get("list", []) or []
                pages = int(data.get("pages", 0) or 0)

                for order in order_list:
                    asset_info = order.get("assetInfo", {}) or {}
                    item_info = order.get("itemInfo", {}) or {}

                    order_id = str(order.get("orderId", "") or "")
                    product_id = str(order.get("productId", "") or "")
                    item_id = str(order.get("itemId", "") or "")
                    name = order.get("name", "")
                    market_hash_name = order.get("marketHashName", "")
                    image_url = order.get("imageUrl", "")
                    order_price = safe_float(order.get("price", 0))
                    order_status = int(order.get("status", 0) or 0)
                    status_name = order.get("statusName", "")
                    order_create_time = int(order.get("orderCreateTime", 0) or 0)
                    asset_id = str(asset_info.get("assetId", "") or "")
                    style_id = str(asset_info.get("styleId", "") or "")
                    wear = str(asset_info.get("wear", "") or "")
                    weapon_name = item_info.get("weaponName", "")
                    exterior_name = item_info.get("exteriorName", "")

                    if not order_id:
                        continue

                    cur.execute("""
                    INSERT INTO seller_orders (
                        order_id, steam_id, product_id, app_id, item_id, name, market_hash_name,
                        image_url, order_price, order_status, status_name, order_create_time,
                        asset_id, style_id, wear, weapon_name, exterior_name, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(order_id) DO UPDATE SET
                        steam_id=excluded.steam_id,
                        product_id=excluded.product_id,
                        app_id=excluded.app_id,
                        item_id=excluded.item_id,
                        name=excluded.name,
                        market_hash_name=excluded.market_hash_name,
                        image_url=excluded.image_url,
                        order_price=excluded.order_price,
                        order_status=excluded.order_status,
                        status_name=excluded.status_name,
                        order_create_time=excluded.order_create_time,
                        asset_id=excluded.asset_id,
                        style_id=excluded.style_id,
                        wear=excluded.wear,
                        weapon_name=excluded.weapon_name,
                        exterior_name=excluded.exterior_name,
                        updated_at=excluded.updated_at
                    """, (
                        order_id, steam_id, product_id, str(app_id), item_id, name, market_hash_name,
                        image_url, order_price, order_status, status_name, order_create_time,
                        asset_id, style_id, wear, weapon_name, exterior_name, current_time
                    ))

                conn.commit()

                if not order_list:
                    break
                if pages and page >= pages:
                    break

                page += 1

            success_accounts += 1

        except Exception as e:
            failed.append(f"{steam_id}: {str(e)}")

    conn.close()

    if failed:
        return f"订单同步完成，成功账号 {success_accounts} 个，失败 {len(failed)} 个。失败示例：{' | '.join(failed[:5])}"
    return None


def get_inventory_from_db(steam_id, app_id=DEFAULT_APP_ID):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        i.steam_id,
        i.app_id,
        i.item_key,
        i.asset_id,
        i.token,
        i.style_token,
        i.name,
        i.short_name,
        i.image_url,
        i.price,
        i.status,
        i.if_tradable,
        i.wear,
        i.style_id,
        i.weapon_name,
        i.exterior_name,
        i.updated_at,
        COALESCE(p.purchase_price, 0) AS purchase_price
    FROM inventory_items i
    LEFT JOIN item_purchase_prices p
      ON i.steam_id = p.steam_id
     AND i.app_id = p.app_id
     AND i.asset_id = p.asset_id
    WHERE i.steam_id = ? AND i.app_id = ?
    ORDER BY i.price DESC, i.name ASC
    """, (steam_id, app_id))

    rows = cur.fetchall()
    conn.close()

    items = []
    for r in rows:
        d = dict(r)
        d["status_text"] = translate_status(d["status"])
        d["purchase_price"] = safe_float(d.get("purchase_price", 0), 0)
        d["profit"] = safe_float(d.get("price", 0), 0) - d["purchase_price"]
        d["search_blob"] = item_search_blob(d)
        items.append(d)

    return items


def get_group_default_purchase_price(steam_id, app_id, group_name):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT default_purchase_price
    FROM group_purchase_prices
    WHERE steam_id = ? AND app_id = ? AND group_name = ?
    """, (steam_id, app_id, group_name))
    row = cur.fetchone()
    conn.close()
    if not row:
        return 0.0
    return safe_float(row["default_purchase_price"], 0)


def save_group_default_purchase_price(steam_id, app_id, group_name, price):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO group_purchase_prices (steam_id, app_id, group_name, default_purchase_price, updated_at)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(steam_id, app_id, group_name) DO UPDATE SET
        default_purchase_price=excluded.default_purchase_price,
        updated_at=excluded.updated_at
    """, (steam_id, app_id, group_name, safe_float(price, 0), now_str()))
    conn.commit()
    conn.close()


def save_item_purchase_price(steam_id, app_id, asset_id, purchase_price):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    INSERT INTO item_purchase_prices (steam_id, app_id, asset_id, purchase_price, updated_at)
    VALUES (?, ?, ?, ?, ?)
    ON CONFLICT(steam_id, app_id, asset_id) DO UPDATE SET
        purchase_price=excluded.purchase_price,
        updated_at=excluded.updated_at
    """, (steam_id, app_id, asset_id, safe_float(purchase_price, 0), now_str()))
    conn.commit()
    conn.close()


def apply_group_price_to_items(steam_id, app_id, group_name, price):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
    SELECT asset_id
    FROM inventory_items
    WHERE steam_id = ? AND app_id = ? AND name = ?
    """, (steam_id, app_id, group_name))
    rows = cur.fetchall()

    current_time = now_str()
    normalized_price = safe_float(price, 0)

    for row in rows:
        asset_id = str(row["asset_id"] or "").strip()
        if not asset_id:
            continue

        cur.execute("""
        INSERT INTO item_purchase_prices (steam_id, app_id, asset_id, purchase_price, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(steam_id, app_id, asset_id) DO UPDATE SET
            purchase_price=excluded.purchase_price,
            updated_at=excluded.updated_at
        """, (steam_id, app_id, asset_id, normalized_price, current_time))

    conn.commit()
    conn.close()


def group_inventory_by_name(items, steam_id, app_id):
    grouped = defaultdict(list)

    for item in items:
        key = (item.get("name") or "未命名饰品").strip()
        grouped[key].append(item)

    result = []
    for name, group_items in grouped.items():
        count = len(group_items)
        total_market_value = sum(safe_float(x.get("price", 0), 0) for x in group_items)
        avg_market_price = total_market_value / count if count else 0
        first = group_items[0]

        styles = sorted({str(x.get("style_id", "")).strip() for x in group_items if str(x.get("style_id", "")).strip()})
        wears = sorted({str(x.get("wear", "")).strip() for x in group_items if str(x.get("wear", "")).strip()})

        style_summary = ", ".join(styles[:6])
        if len(styles) > 6:
            style_summary += " ..."

        default_purchase_price = get_group_default_purchase_price(steam_id, app_id, name)
        total_cost = default_purchase_price * count
        total_profit = total_market_value - total_cost

        on_sale_count = sum(1 for x in group_items if int(x.get("status", 0)) == 1)
        tradable_count = sum(1 for x in group_items if get_status_bucket(x) == "tradable")
        not_tradable_count = sum(1 for x in group_items if get_status_bucket(x) == "not_tradable")

        row = {
            "name": name,
            "short_name": first.get("short_name", ""),
            "image_url": first.get("image_url", ""),
            "weapon_name": first.get("weapon_name", ""),
            "exterior_name": first.get("exterior_name", ""),
            "count": count,
            "total_market_value": total_market_value,
            "avg_market_price": avg_market_price,
            "on_sale_count": on_sale_count,
            "tradable_count": tradable_count,
            "not_tradable_count": not_tradable_count,
            "style_summary": style_summary,
            "wear_summary": ", ".join(wears[:6]),
            "default_purchase_price": default_purchase_price,
            "total_cost": total_cost,
            "total_profit": total_profit,
            "search_blob": "",
        }
        row["search_blob"] = summary_search_blob(row)
        result.append(row)

    result.sort(key=lambda x: (-x["total_market_value"], x["name"]))
    return result


def get_inventory_item_for_sale(item_key, steam_id=None, app_id=DEFAULT_APP_ID):
    conn = get_conn()
    cur = conn.cursor()

    if steam_id:
        cur.execute("""
        SELECT item_key, steam_id, app_id, asset_id, token, style_token, name, price
        FROM inventory_items
        WHERE item_key = ? AND steam_id = ? AND app_id = ?
        LIMIT 1
        """, (item_key, steam_id, app_id))
    else:
        cur.execute("""
        SELECT item_key, steam_id, app_id, asset_id, token, style_token, name, price
        FROM inventory_items
        WHERE item_key = ? AND app_id = ?
        LIMIT 1
        """, (item_key, app_id))

    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def get_profit_rows_from_db(app_id=DEFAULT_APP_ID, keyword="", steam_id=""):
    conn = get_conn()
    cur = conn.cursor()

    sql = """
    SELECT
        o.order_id,
        o.steam_id,
        o.product_id,
        o.app_id,
        o.item_id,
        o.name,
        o.market_hash_name,
        o.image_url,
        o.order_price,
        o.order_status,
        o.status_name,
        o.order_create_time,
        o.asset_id,
        o.style_id,
        o.wear,
        o.weapon_name,
        o.exterior_name,
        a.nickname,
        a.username,
        COALESCE(p.purchase_price, 0) AS item_purchase_price,
        COALESCE(g.default_purchase_price, 0) AS group_purchase_price
    FROM seller_orders o
    LEFT JOIN steam_accounts a
      ON o.steam_id = a.steam_id
    LEFT JOIN item_purchase_prices p
      ON o.steam_id = p.steam_id
     AND o.app_id = p.app_id
     AND o.asset_id = p.asset_id
    LEFT JOIN group_purchase_prices g
      ON o.steam_id = g.steam_id
     AND o.app_id = g.app_id
     AND o.name = g.group_name
    WHERE o.app_id = ?
    """


    params = [str(app_id)]

    if steam_id:
        sql += " AND o.steam_id = ?"
        params.append(steam_id)

    sql += " ORDER BY o.order_create_time DESC, o.order_id DESC"

    cur.execute(sql, params)
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    result = []
    keyword = (keyword or "").strip().lower()

    for row in rows:
        item_cost = safe_float(row.get("item_purchase_price", 0), 0)
        group_cost = safe_float(row.get("group_purchase_price", 0), 0)
        cost_price = item_cost if item_cost > 0 else group_cost

        order_price = safe_float(row.get("order_price", 0), 0)
        profit = order_price - cost_price

        record = {
            "order_id": str(row.get("order_id", "") or ""),
            "steam_id": str(row.get("steam_id", "") or ""),
            "product_id": str(row.get("product_id", "") or ""),
            "app_id": str(row.get("app_id", "") or ""),
            "name": row.get("name", ""),
            "market_hash_name": row.get("market_hash_name", ""),
            "image_url": row.get("image_url", ""),
            "order_price": order_price,
            "order_status": int(row.get("order_status", 0) or 0),
            "status_name": row.get("status_name", ""),
            "order_create_time": int(row.get("order_create_time", 0) or 0),
            "order_create_time_str": unix_ms_to_str(row.get("order_create_time", 0)),
            "asset_id": str(row.get("asset_id", "") or ""),
            "style_id": str(row.get("style_id", "") or ""),
            "wear": str(row.get("wear", "") or ""),
            "weapon_name": row.get("weapon_name", ""),
            "exterior_name": row.get("exterior_name", ""),
            "nickname": row.get("nickname", ""),
            "username": row.get("username", ""),
            "cost_price": cost_price,
            "profit": profit,
        }

        search_blob = " ".join(
            str(x or "") for x in [
                record.get("name"),
                record.get("market_hash_name"),
                record.get("asset_id"),
                record.get("style_id"),
                record.get("steam_id"),
                record.get("nickname"),
                record.get("username"),
                record.get("weapon_name"),
                record.get("exterior_name"),
                record.get("order_id"),
                record.get("product_id"),
            ]
        ).lower()

        if keyword and keyword not in search_blob:
            continue

        result.append(record)

    return result


def build_profit_summary(rows):
    total_orders = len(rows)
    total_revenue = sum(safe_float(x["order_price"], 0) for x in rows)
    total_cost = sum(safe_float(x["cost_price"], 0) for x in rows)
    total_profit = sum(safe_float(x["profit"], 0) for x in rows)

    by_name = defaultdict(lambda: {
        "name": "",
        "count": 0,
        "revenue": 0.0,
        "cost": 0.0,
        "profit": 0.0,
    })

    by_account = defaultdict(lambda: {
        "steam_id": "",
        "nickname": "",
        "username": "",
        "count": 0,
        "revenue": 0.0,
        "cost": 0.0,
        "profit": 0.0,
    })

    for row in rows:
        name_key = row["name"] or "未命名商品"
        by_name[name_key]["name"] = name_key
        by_name[name_key]["count"] += 1
        by_name[name_key]["revenue"] += safe_float(row["order_price"], 0)
        by_name[name_key]["cost"] += safe_float(row["cost_price"], 0)
        by_name[name_key]["profit"] += safe_float(row["profit"], 0)

        acc_key = row["steam_id"] or "unknown"
        by_account[acc_key]["steam_id"] = row["steam_id"]
        by_account[acc_key]["nickname"] = row["nickname"]
        by_account[acc_key]["username"] = row["username"]
        by_account[acc_key]["count"] += 1
        by_account[acc_key]["revenue"] += safe_float(row["order_price"], 0)
        by_account[acc_key]["cost"] += safe_float(row["cost_price"], 0)
        by_account[acc_key]["profit"] += safe_float(row["profit"], 0)

    by_name_rows = sorted(by_name.values(), key=lambda x: (-x["profit"], -x["revenue"], x["name"]))
    by_account_rows = sorted(by_account.values(), key=lambda x: (-x["profit"], -x["revenue"], x["steam_id"]))

    return {
        "total_orders": total_orders,
        "total_revenue": total_revenue,
        "total_cost": total_cost,
        "total_profit": total_profit,
        "by_name_rows": by_name_rows,
        "by_account_rows": by_account_rows,
    }


# =========================
# 模板
# =========================
ACCOUNTS_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>Steam 库存管理软件</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { box-sizing: border-box; }
        body { margin: 0; font-family: "Microsoft YaHei", Arial, sans-serif; background: #0b1220; color: #e5eefc; }
        .container { max-width: 1450px; margin: 0 auto; padding: 24px; }
        .topbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 24px; }
        .title { font-size: 30px; font-weight: 800; color: #f8fbff; }
        .sub { color: #8ca3c7; margin-top: 6px; font-size: 14px; }
        .toolbar { display: flex; gap: 10px; flex-wrap: wrap; }
        .input { width: 320px; padding: 12px 14px; border-radius: 12px; border: 1px solid #26354d; background: #111b2d; color: #eef4ff; outline: none; }
        .btn { padding: 12px 16px; border: 0; border-radius: 12px; background: #2563eb; color: #fff; cursor: pointer; font-size: 14px; text-decoration: none; }
        .btn:hover { background: #1e4fc0; }
        .msg { background: #12301d; border: 1px solid #2f8f50; color: #d7ffe4; padding: 14px 16px; border-radius: 14px; margin-bottom: 18px; }
        .error { background: #4a1318; border: 1px solid #d44; color: #ffd6d6; padding: 16px; border-radius: 16px; margin-bottom: 18px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px; margin-bottom: 24px; }
        .stat { background: #121c2f; border: 1px solid #25344c; border-radius: 18px; padding: 18px; box-shadow: 0 10px 24px rgba(0,0,0,0.25); }
        .stat-label { color: #8ca3c7; font-size: 13px; margin-bottom: 8px; }
        .stat-value { font-size: 30px; font-weight: 800; color: #f8fbff; }
        .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(310px, 1fr)); gap: 18px; }
        .card { background: linear-gradient(180deg, #152238, #101a2b); border: 1px solid #25344c; border-radius: 20px; padding: 18px; box-shadow: 0 12px 28px rgba(0,0,0,0.28); transition: transform .18s ease, box-shadow .18s ease; cursor: pointer; }
        .card:hover { transform: translateY(-3px); box-shadow: 0 16px 34px rgba(0,0,0,0.36); }
        .row-top { display: flex; align-items: center; gap: 14px; margin-bottom: 16px; }
        .avatar { width: 76px; height: 76px; border-radius: 18px; object-fit: cover; background: #0b1220; border: 2px solid #314763; flex-shrink: 0; }
        .name-wrap { min-width: 0; flex: 1; }
        .nickname { font-size: 20px; font-weight: 800; color: #f8fbff; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .username { color: #84b8ff; font-size: 14px; margin-top: 4px; word-break: break-all; }
        .info-row { margin-top: 10px; background: rgba(255,255,255,0.03); border: 1px solid rgba(140,163,199,0.12); border-radius: 12px; padding: 10px 12px; }
        .label { color: #8ca3c7; font-size: 12px; margin-bottom: 4px; }
        .value { color: #f1f6ff; font-size: 14px; word-break: break-all; }
        .enter { margin-top: 14px; display: inline-block; background: #0f9d58; color: white; padding: 10px 14px; border-radius: 12px; font-size: 14px; }
        .empty { text-align: center; padding: 40px; color: #8ca3c7; font-size: 16px; }
        .sync-panel { background: linear-gradient(180deg, #152238, #101a2b); border: 1px solid #25344c; border-radius: 20px; padding: 18px; margin-bottom: 22px; box-shadow: 0 12px 28px rgba(0,0,0,0.28); }
        .sync-head { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:12px; }
        .sync-title { font-size: 18px; font-weight: 800; color:#f8fbff; }
        .sync-badge { padding: 8px 12px; border-radius: 999px; font-size: 12px; font-weight: 700; }
        .sync-running { background:#5b3b08; color:#ffd89a; border:1px solid #a56a12; }
        .sync-idle { background:#12301d; color:#d7ffe4; border:1px solid #2f8f50; }
        .sync-cancel { background:#4a1318; color:#ffd6d6; border:1px solid #d44; }
        .sync-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap:12px; }
        .sync-card { background: rgba(255,255,255,0.03); border:1px solid rgba(140,163,199,0.12); border-radius: 14px; padding: 12px 14px; }
        .sync-k { color:#8ca3c7; font-size:12px; margin-bottom:6px; }
        .sync-v { color:#f1f6ff; font-size:15px; font-weight:700; word-break:break-all; }
        .progress-wrap { margin-top: 14px; }
        .progress-bar { width:100%; height:14px; background:#0f1727; border-radius:999px; overflow:hidden; border:1px solid #30465f; }
        .progress-inner { height:100%; background:#2563eb; width:0%; transition:width .2s ease; }
        .sync-note { margin-top: 10px; color:#8ca3c7; font-size:13px; line-height:1.5; }
    </style>
</head>
<body>
<div class="container">
    <div class="topbar">
        <div>
            <div class="title">Steam 库存管理软件</div>
            <div class="sub">按名称汇总 + 购入成本价 + 上架功能</div>
        </div>
        <div class="toolbar">
            <input id="searchInput" class="input" type="text" placeholder="搜索 昵称 / 用户名 / SteamID">
            <a class="btn" href="/">刷新页面</a>
            <a class="btn" href="/sync/accounts">同步所有账号</a>
            <a class="btn" href="/sync/all_inventory">开始同步所有库存</a>
            <a class="btn" href="/sync/cancel_all_inventory">取消同步库存</a>
            <a class="btn" href="/sync/failed_inventory">同步失败账号库存</a>
            <a class="btn" href="/all_inventory">显示所有账号库存</a>
            <a class="btn" href="/profit_analysis">利润分析</a>
        </div>
    </div>

    {% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
    {% if error %}<div class="error">{{ error }}</div>{% endif %}

    <div class="sync-panel">
        <div class="sync-head">
            <div class="sync-title">库存同步任务状态</div>
            {% if sync_status.running %}
                <div id="syncBadge" class="sync-badge {% if sync_status.cancel_requested %}sync-cancel{% else %}sync-running{% endif %}">
                    {{ '取消中' if sync_status.cancel_requested else '运行中' }}
                </div>
            {% else %}
                <div id="syncBadge" class="sync-badge sync-idle">空闲</div>
            {% endif %}
        </div>

        <div class="sync-grid">
            <div class="sync-card">
                <div class="sync-k">当前同步状态</div>
                <div id="syncStateText" class="sync-v">
                    {% if sync_status.running %}
                        {{ '已请求取消，等待当前账号结束' if sync_status.cancel_requested else '正在同步库存' }}
                    {% else %}
                        {{ sync_status.last_message or '当前没有运行中的同步任务' }}
                    {% endif %}
                </div>
            </div>

            <div class="sync-card">
                <div class="sync-k">当前正在同步的 SteamID</div>
                <div id="syncCurrentSteamId" class="sync-v">{{ sync_status.current_steam_id or '-' }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">已完成 / 总数</div>
                <div id="syncProgressText" class="sync-v">{{ sync_status.finished }}/{{ sync_status.total }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">成功 / 库存为空 / 失败</div>
                <div id="syncCounterText" class="sync-v">{{ sync_status.success_count }}/{{ sync_status.empty_count }}/{{ sync_status.failed_count }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">开始时间</div>
                <div id="syncStartedAt" class="sync-v">{{ sync_status.started_at or '-' }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">结束时间</div>
                <div id="syncEndedAt" class="sync-v">{{ sync_status.ended_at or '-' }}</div>
            </div>
        </div>

        <div class="progress-wrap">
            <div class="progress-bar">
                <div id="syncProgressBar" class="progress-inner" style="width: {{ (sync_status.finished * 100 / sync_status.total) if sync_status.total else 0 }}%;"></div>
            </div>
        </div>

        <div id="syncLastError" class="sync-note">
            {% if sync_status.last_error %}
                失败信息：{{ sync_status.last_error }}
            {% else %}
                暂无失败信息
            {% endif %}
        </div>
    </div>

    <div class="stats">
        <div class="stat">
            <div class="stat-label">账号总数</div>
            <div class="stat-value">{{ steam_accounts|length }}</div>
        </div>
    </div>

    {% if steam_accounts %}
        <div class="grid">
            {% for item in steam_accounts %}
            <div class="card account-card"
                 data-search="{{ (item.nickname ~ ' ' ~ item.username ~ ' ' ~ item.steam_id)|lower }}"
                 onclick="window.location='/inventory/{{ item.steam_id }}'">
                <div class="row-top">
                    <img class="avatar" src="{{ item.avatar }}" alt="avatar"
                         onerror="this.src='https://via.placeholder.com/76?text=No+Img'">
                    <div class="name-wrap">
                        <div class="nickname">{{ item.nickname if item.nickname else '未设置昵称' }}</div>
                        <div class="username">{{ item.username if item.username else '无用户名' }}</div>
                    </div>
                </div>

                <div class="info-row">
                    <div class="label">SteamID</div>
                    <div class="value">{{ item.steam_id }}</div>
                </div>

                <div class="enter">查看库存</div>
            </div>
            {% endfor %}
        </div>

        <div id="emptyState" class="empty" style="display:none;">没有匹配到账号</div>
    {% else %}
        <div class="empty">当前数据库里没有账号，请先点“同步所有账号”</div>
    {% endif %}
</div>

<script>
const input = document.getElementById("searchInput");
const cards = document.querySelectorAll(".account-card");
const emptyState = document.getElementById("emptyState");

if (input) {
    input.addEventListener("input", function () {
        const keyword = this.value.trim().toLowerCase();
        let visibleCount = 0;
        cards.forEach(card => {
            const text = card.getAttribute("data-search") || "";
            const matched = text.includes(keyword);
            card.style.display = matched ? "block" : "none";
            if (matched) visibleCount++;
        });
        if (emptyState) emptyState.style.display = visibleCount === 0 ? "block" : "none";
    });
}

async function refreshSyncStatus() {
    try {
        const resp = await fetch('/sync/status', { cache: 'no-store' });
        if (!resp.ok) return;
        const data = await resp.json();

        const badge = document.getElementById('syncBadge');
        const stateText = document.getElementById('syncStateText');
        const currentSteamId = document.getElementById('syncCurrentSteamId');
        const progressText = document.getElementById('syncProgressText');
        const counterText = document.getElementById('syncCounterText');
        const startedAt = document.getElementById('syncStartedAt');
        const endedAt = document.getElementById('syncEndedAt');
        const lastError = document.getElementById('syncLastError');
        const progressBar = document.getElementById('syncProgressBar');

        if (badge) {
            badge.className = 'sync-badge ' + (data.running ? (data.cancel_requested ? 'sync-cancel' : 'sync-running') : 'sync-idle');
            badge.textContent = data.running ? (data.cancel_requested ? '取消中' : '运行中') : '空闲';
        }

        if (stateText) {
            if (data.running) {
                stateText.textContent = data.cancel_requested ? '已请求取消，等待当前账号结束' : (data.last_message || '正在同步库存');
            } else {
                stateText.textContent = data.last_message || '当前没有运行中的同步任务';
            }
        }

        if (currentSteamId) currentSteamId.textContent = data.current_steam_id || '-';
        if (progressText) progressText.textContent = `${data.finished || 0}/${data.total || 0}`;
        if (counterText) counterText.textContent = `${data.success_count || 0}/${data.empty_count || 0}/${data.failed_count || 0}`;
        if (startedAt) startedAt.textContent = data.started_at || '-';
        if (endedAt) endedAt.textContent = data.ended_at || '-';

        if (lastError) {
            lastError.textContent = data.last_error ? `失败信息：${data.last_error}` : '暂无失败信息';
        }

        if (progressBar) {
            const total = Number(data.total || 0);
            const finished = Number(data.finished || 0);
            const percent = total > 0 ? Math.min(100, (finished * 100 / total)) : 0;
            progressBar.style.width = percent + '%';
        }
    } catch (e) {
        console.log('refresh sync status failed', e);
    }
}

refreshSyncStatus();
setInterval(refreshSyncStatus, 2000);
</script>
</body>
</html>
"""


INVENTORY_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>库存详情 - {{ steam_id }}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { box-sizing: border-box; }
        body { margin: 0; font-family: "Microsoft YaHei", Arial, sans-serif; background: #0b1220; color: #e5eefc; }
        .container { max-width: 1700px; margin: 0 auto; padding: 24px; }
        .topbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
        .title { font-size: 28px; font-weight: 800; color: #f8fbff; }
        .sub { color: #8ca3c7; margin-top: 6px; font-size: 14px; word-break: break-all; }
        .toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
        .input { width: 340px; padding: 12px 14px; border-radius: 12px; border: 1px solid #26354d; background: #111b2d; color: #eef4ff; outline: none; }
        .btn { padding: 12px 16px; border: 0; border-radius: 12px; background: #2563eb; color: #fff; cursor: pointer; font-size: 14px; text-decoration: none; }
        .btn:hover { background: #1e4fc0; }
        .msg { background: #12301d; border: 1px solid #2f8f50; color: #d7ffe4; padding: 14px 16px; border-radius: 14px; margin-bottom: 18px; }
        .error { background: #4a1318; border: 1px solid #d44; color: #ffd6d6; padding: 16px; border-radius: 16px; margin-bottom: 18px; }
        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin: 20px 0 24px 0; }
        .stat { background: #121c2f; border: 1px solid #25344c; border-radius: 18px; padding: 18px; box-shadow: 0 10px 24px rgba(0,0,0,0.25); }
        .stat-label { color: #8ca3c7; font-size: 13px; margin-bottom: 8px; }
        .stat-value { font-size: 28px; font-weight: 800; color: #f8fbff; }
        .toggle-bar { display: flex; gap: 10px; flex-wrap: wrap; margin-bottom: 18px; }
        .toggle-btn { padding: 10px 14px; border-radius: 12px; text-decoration: none; font-size: 14px; background: #13233a; border: 1px solid #2c4464; color: #dce9ff; }
        .toggle-btn.active { background: #0f9d58; border-color: #0f9d58; color: white; }

        .summary-list {
            display: grid;
            gap: 12px;
        }
        .summary-row {
            display: grid;
            grid-template-columns: 90px 2.4fr 0.8fr 0.8fr 0.8fr 0.9fr 1fr 1fr 1fr 160px;
            gap: 12px;
            align-items: center;
            background: linear-gradient(180deg, #152238, #101a2b);
            border: 1px solid #25344c;
            border-radius: 18px;
            padding: 14px;
        }
        .summary-head {
            display: grid;
            grid-template-columns: 90px 2.4fr 0.8fr 0.8fr 0.8fr 0.9fr 1fr 1fr 1fr 160px;
            gap: 12px;
            padding: 8px 14px;
            color: #8ca3c7;
            font-size: 12px;
            font-weight: 700;
        }
        .summary-img {
            width: 80px;
            height: 64px;
            object-fit: contain;
            background: #0f1727;
            border-radius: 12px;
            border: 1px solid #30465f;
            padding: 6px;
        }
        .summary-name {
            font-weight: 800;
            color: #f8fbff;
            line-height: 1.4;
        }
        .subtext {
            margin-top: 4px;
            color: #8ca3c7;
            font-size: 12px;
            line-height: 1.35;
        }
        .num-box {
            font-weight: 800;
            color: #f8fbff;
        }
        .small {
            font-size: 12px;
            color: #8ca3c7;
            margin-top: 3px;
        }
        .profit-plus { color: #8ef0a7; font-weight: 800; }
        .profit-minus { color: #ffb4b4; font-weight: 800; }

        .price-form {
            display: grid;
            gap: 8px;
        }
        .price-input, .text-input, .select-input {
            width: 100%;
            padding: 10px 12px;
            border-radius: 10px;
            border: 1px solid #30465f;
            background: #0f1727;
            color: #eef4ff;
            outline: none;
        }
        .save-btn, .detail-btn, .sell-btn {
            padding: 10px 12px;
            border: 0;
            border-radius: 10px;
            background: #0f9d58;
            color: white;
            cursor: pointer;
            font-size: 13px;
            text-decoration: none;
            display: inline-block;
            text-align: center;
        }
        .sell-btn {
            background: #d97706;
        }
        .save-btn:hover, .detail-btn:hover { background: #0c7e47; }
        .sell-btn:hover { background: #b45309; }

        .detail-list {
            display: grid;
            gap: 12px;
        }
        .detail-row {
            display: grid;
            grid-template-columns: 100px 2.1fr 1fr 0.8fr 0.8fr 0.8fr 1fr 260px;
            gap: 12px;
            align-items: start;
            background: linear-gradient(180deg, #152238, #101a2b);
            border: 1px solid #25344c;
            border-radius: 18px;
            padding: 14px;
        }
        .detail-head {
            display: grid;
            grid-template-columns: 100px 2.1fr 1fr 0.8fr 0.8fr 0.8fr 1fr 260px;
            gap: 12px;
            padding: 8px 14px;
            color: #8ca3c7;
            font-size: 12px;
            font-weight: 700;
        }
        .detail-img {
            width: 90px;
            height: 72px;
            object-fit: contain;
            background: #0f1727;
            border-radius: 12px;
            border: 1px solid #30465f;
            padding: 6px;
        }
        .item-title {
            font-weight: 800;
            color: #f8fbff;
            line-height: 1.35;
        }
        .asset-id {
            margin-top: 6px;
            font-size: 12px;
            color: #8ca3c7;
            word-break: break-all;
        }
        .pill-row { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 8px; }
        .pill {
            display: inline-block;
            padding: 4px 8px;
            border-radius: 999px;
            font-size: 11px;
            border: 1px solid #30465f;
            background: #122035;
            color: #d9e8ff;
        }
        .status-on-sale { color: #f59e0b; font-weight: 700; }
        .status-tradable { color: #8ef0a7; font-weight: 700; }
        .status-not-tradable { color: #ffb4b4; font-weight: 700; }

        .compact-price-form, .sell-form {
            display: grid;
            gap: 8px;
        }
        .compact-input {
            width: 100%;
            padding: 9px 10px;
            border-radius: 10px;
            border: 1px solid #30465f;
            background: #0f1727;
            color: #eef4ff;
            outline: none;
        }

        .action-stack {
            display: grid;
            gap: 10px;
        }

        .empty { text-align: center; padding: 40px; color: #8ca3c7; font-size: 16px; }
        .sync-panel { background: linear-gradient(180deg, #152238, #101a2b); border: 1px solid #25344c; border-radius: 20px; padding: 18px; margin-bottom: 22px; box-shadow: 0 12px 28px rgba(0,0,0,0.28); }
        .sync-head { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:12px; }
        .sync-title { font-size: 18px; font-weight: 800; color:#f8fbff; }
        .sync-badge { padding: 8px 12px; border-radius: 999px; font-size: 12px; font-weight: 700; }
        .sync-running { background:#5b3b08; color:#ffd89a; border:1px solid #a56a12; }
        .sync-idle { background:#12301d; color:#d7ffe4; border:1px solid #2f8f50; }
        .sync-cancel { background:#4a1318; color:#ffd6d6; border:1px solid #d44; }
        .sync-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap:12px; }
        .sync-card { background: rgba(255,255,255,0.03); border:1px solid rgba(140,163,199,0.12); border-radius: 14px; padding: 12px 14px; }
        .sync-k { color:#8ca3c7; font-size:12px; margin-bottom:6px; }
        .sync-v { color:#f1f6ff; font-size:15px; font-weight:700; word-break:break-all; }
        .progress-wrap { margin-top: 14px; }
        .progress-bar { width:100%; height:14px; background:#0f1727; border-radius:999px; overflow:hidden; border:1px solid #30465f; }
        .progress-inner { height:100%; background:#2563eb; width:0%; transition:width .2s ease; }
        .sync-note { margin-top: 10px; color:#8ca3c7; font-size:13px; line-height:1.5; }

        @media (max-width: 1280px) {
            .summary-head, .summary-row,
            .detail-head, .detail-row {
                grid-template-columns: 1fr;
            }
            .summary-head, .detail-head {
                display: none;
            }
        }
    </style>
</head>
<body>
<div class="container">
    <div class="topbar">
        <div>
            <div class="title">库存详情</div>
            <div class="sub">SteamID：{{ steam_id }}</div>
        </div>
        <div class="toolbar">
            <input id="searchInput" class="input" type="text"
                   placeholder="{{ '搜索 名称 / 武器 / 外观 / styleId / 成本价' if view_mode == 'summary' else '搜索 名称 / 武器 / 外观 / 磨损 / styleId / assetId / 成本价' }}">
            <a class="btn" href="/">返回账号列表</a>
            <a class="btn" href="/sync/inventory/{{ steam_id }}">同步该账号库存</a>
        </div>
    </div>

    {% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
    {% if error %}<div class="error">{{ error }}</div>{% endif %}

    <div class="sync-panel">
        <div class="sync-head">
            <div class="sync-title">库存同步任务状态</div>
            {% if sync_status.running %}
                <div id="syncBadge" class="sync-badge {% if sync_status.cancel_requested %}sync-cancel{% else %}sync-running{% endif %}">
                    {{ '取消中' if sync_status.cancel_requested else '运行中' }}
                </div>
            {% else %}
                <div id="syncBadge" class="sync-badge sync-idle">空闲</div>
            {% endif %}
        </div>

        <div class="sync-grid">
            <div class="sync-card">
                <div class="sync-k">当前同步状态</div>
                <div id="syncStateText" class="sync-v">
                    {% if sync_status.running %}
                        {{ '已请求取消，等待当前账号结束' if sync_status.cancel_requested else '正在同步库存' }}
                    {% else %}
                        {{ sync_status.last_message or '当前没有运行中的同步任务' }}
                    {% endif %}
                </div>
            </div>

            <div class="sync-card">
                <div class="sync-k">当前正在同步的 SteamID</div>
                <div id="syncCurrentSteamId" class="sync-v">{{ sync_status.current_steam_id or '-' }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">已完成 / 总数</div>
                <div id="syncProgressText" class="sync-v">{{ sync_status.finished }}/{{ sync_status.total }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">成功 / 库存为空 / 失败</div>
                <div id="syncCounterText" class="sync-v">{{ sync_status.success_count }}/{{ sync_status.empty_count }}/{{ sync_status.failed_count }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">开始时间</div>
                <div id="syncStartedAt" class="sync-v">{{ sync_status.started_at or '-' }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">结束时间</div>
                <div id="syncEndedAt" class="sync-v">{{ sync_status.ended_at or '-' }}</div>
            </div>
        </div>

        <div class="progress-wrap">
            <div class="progress-bar">
                <div id="syncProgressBar" class="progress-inner" style="width: {{ (sync_status.finished * 100 / sync_status.total) if sync_status.total else 0 }}%;"></div>
            </div>
        </div>

        <div id="syncLastError" class="sync-note">
            {% if sync_status.last_error %}
                失败信息：{{ sync_status.last_error }}
            {% else %}
                暂无失败信息
            {% endif %}
        </div>
    </div>

    <div class="toggle-bar">
        <a class="toggle-btn {{ 'active' if view_mode == 'summary' else '' }}"
            href="/inventory/{{ steam_id }}?view=summary&inventory_filter={{ inventory_filter }}">按名称汇总</a>

        <a class="toggle-btn {{ 'active' if view_mode == 'detail' and not selected_name else '' }}"
            href="/inventory/{{ steam_id }}?view=detail&inventory_filter={{ inventory_filter }}">全部明细</a>

        {% if selected_name %}
        <a class="toggle-btn active"
            href="/inventory/{{ steam_id }}?view=detail&name={{ selected_name|urlencode }}&inventory_filter={{ inventory_filter }}">当前名称明细</a>
        {% endif %}
    </div>

    <div class="toggle-bar">
        <a class="toggle-btn {{ 'active' if inventory_filter == 'all' else '' }}"
            href="/inventory/{{ steam_id }}?view={{ view_mode }}{% if selected_name %}&name={{ selected_name|urlencode }}{% endif %}&inventory_filter=all">全部</a>

        <a class="toggle-btn {{ 'active' if inventory_filter == 'on_sale' else '' }}"
            href="/inventory/{{ steam_id }}?view={{ view_mode }}{% if selected_name %}&name={{ selected_name|urlencode }}{% endif %}&inventory_filter=on_sale">在售中</a>

        <a class="toggle-btn {{ 'active' if inventory_filter == 'tradable' else '' }}"
            href="/inventory/{{ steam_id }}?view={{ view_mode }}{% if selected_name %}&name={{ selected_name|urlencode }}{% endif %}&inventory_filter=tradable">可交易</a>

        <a class="toggle-btn {{ 'active' if inventory_filter == 'not_tradable' else '' }}"
            href="/inventory/{{ steam_id }}?view={{ view_mode }}{% if selected_name %}&name={{ selected_name|urlencode }}{% endif %}&inventory_filter=not_tradable">不可交易</a>
    </div>

    <div class="stats">
        <div class="stat">
            <div class="stat-label">{{ '名称分组数' if view_mode == 'summary' else '库存总数' }}</div>
            <div id="statTotal" class="stat-value">{{ total_count }}</div>
        </div>
        <div class="stat">
            <div class="stat-label">在售中数量</div>
            <div id="statOnSale" class="stat-value">{{ on_sale_count }}</div>
        </div>
        <div class="stat">
            <div class="stat-label">可交易数量</div>
            <div id="statTradable" class="stat-value">{{ tradable_count }}</div>
        </div>
        <div class="stat">
            <div class="stat-label">库存总市值</div>
            <div id="statValue" class="stat-value">{{ "%.2f"|format(total_market_value) }}</div>
        </div>
        <div class="stat">
            <div class="stat-label">总成本</div>
            <div id="statCost" class="stat-value">{{ "%.2f"|format(total_cost) }}</div>
        </div>
    </div>

    {% if view_mode == 'summary' %}
        {% if summary_rows %}
            <div class="summary-head">
                <div>图片</div>
                <div>名称 / 信息</div>
                <div>在售</div>
                <div>可交易</div>
                <div>不可交易</div>
                <div>均价</div>
                <div>总市值</div>
                <div>总成本</div>
                <div>预估利润</div>
                <div>操作</div>
            </div>

            <div class="summary-list" id="summaryList">
                {% for row in summary_rows %}
                <div class="summary-row summary-item"
                     data-search="{{ row.search_blob }}"
                     data-count="{{ row.count }}"
                     data-market="{{ row.total_market_value }}"
                     data-cost="{{ row.total_cost }}">
                    <div>
                        <img class="summary-img"
                             src="{{ row.image_url or 'https://via.placeholder.com/80x64?text=No+Img' }}"
                             alt="summary image"
                             onerror="this.src='https://via.placeholder.com/80x64?text=No+Img'">
                    </div>

                    <div>
                        <div class="summary-name">{{ row.name }}</div>
                        <div class="subtext">
                            {% if row.weapon_name %}{{ row.weapon_name }}{% endif %}
                            {% if row.weapon_name and row.exterior_name %} / {% endif %}
                            {% if row.exterior_name %}{{ row.exterior_name }}{% endif %}
                        </div>
                        {% if row.style_summary %}
                        <div class="subtext">styleId: {{ row.style_summary }}</div>
                        {% endif %}
                        {% if row.wear_summary %}
                        <div class="subtext">磨损: {{ row.wear_summary }}</div>
                        {% endif %}
                    </div>

                    <div><div class="num-box">{{ row.on_sale_count }}</div></div>
                    <div><div class="num-box">{{ row.tradable_count }}</div></div>
                    <div><div class="num-box">{{ row.not_tradable_count }}</div></div>

                    <div>
                        <div class="num-box">¥ {{ "%.2f"|format(row.avg_market_price) }}</div>
                        <div class="small">市场均价</div>
                    </div>

                    <div>
                        <div class="num-box">¥ {{ "%.2f"|format(row.total_market_value) }}</div>
                        <div class="small">总市值</div>
                    </div>

                    <div>
                        <div class="num-box">¥ {{ "%.2f"|format(row.total_cost) }}</div>
                        <div class="small">默认成本 × 数量</div>
                    </div>

                    <div>
                        <div class="{{ 'profit-plus' if row.total_profit >= 0 else 'profit-minus' }}">
                            ¥ {{ "%.2f"|format(row.total_profit) }}
                        </div>
                    </div>

                    <div class="action-stack">
                        <form class="price-form" method="post" action="/save_group_price">
                            <input type="hidden" name="steam_id" value="{{ steam_id }}">
                            <input type="hidden" name="app_id" value="{{ app_id }}">
                            <input type="hidden" name="group_name" value="{{ row.name }}">
                            <input class="price-input" type="number" step="0.01" name="default_purchase_price"
                                   value="{{ "%.2f"|format(row.default_purchase_price) }}">
                            <button class="save-btn" type="submit">保存组成本价</button>
                        </form>

                        <a class="detail-btn" href="/inventory/{{ steam_id }}?view=detail&name={{ row.name|urlencode }}&inventory_filter={{ inventory_filter }}">查看该组明细</a>
                    </div>
                </div>
                {% endfor %}
            </div>

            <div id="emptyState" class="empty" style="display:none;">没有匹配到分组</div>
        {% else %}
            <div class="empty">当前数据库里没有该账号库存，请先同步库存</div>
        {% endif %}
    {% else %}
        {% if items %}
            <div class="detail-head">
                <div>图片</div>
                <div>名称 / 标识</div>
                <div>状态 / 属性</div>
                <div>市场价</div>
                <div>购入成本价</div>
                <div>单件利润</div>
                <div>分类</div>
                <div>操作</div>
            </div>

            <div class="detail-list" id="detailList">
                {% for item in items %}
                <div class="detail-row detail-item"
                     data-search="{{ item.search_blob }}"
                     data-bucket="{{ item.bucket }}"
                     data-market="{{ item.price }}"
                     data-cost="{{ item.purchase_price }}">
                    <div>
                        <img class="detail-img"
                             src="{{ item.image_url or 'https://via.placeholder.com/90x72?text=No+Img' }}"
                             alt="item image"
                             onerror="this.src='https://via.placeholder.com/90x72?text=No+Img'">
                    </div>

                    <div>
                        <div class="item-title">{{ item.name or '未命名饰品' }}</div>
                        <div class="subtext">{{ item.short_name or '' }}</div>
                        <div class="asset-id">assetId: {{ item.asset_id }}</div>
                    </div>

                    <div>
                        <div class="pill-row">
                            <span class="pill">{{ item.status_text }}</span>
                            {% if item.weapon_name %}<span class="pill">{{ item.weapon_name }}</span>{% endif %}
                            {% if item.exterior_name %}<span class="pill">{{ item.exterior_name }}</span>{% endif %}
                            {% if item.style_id %}<span class="pill">styleId: {{ item.style_id }}</span>{% endif %}
                        </div>
                        {% if item.wear %}
                        <div class="subtext" style="margin-top:8px;">磨损: {{ item.wear }}</div>
                        {% endif %}
                    </div>

                    <div>
                        <div class="num-box">¥ {{ "%.2f"|format(item.price) }}</div>
                        <div class="small">当前市场价</div>
                    </div>

                    <div>
                        <div class="num-box">¥ {{ "%.2f"|format(item.purchase_price) }}</div>
                        <div class="small">购入成本价</div>
                    </div>

                    <div>
                        <div class="{{ 'profit-plus' if item.profit >= 0 else 'profit-minus' }}">
                            ¥ {{ "%.2f"|format(item.profit) }}
                        </div>
                    </div>

                    <div>
                        {% if item.bucket == 'on_sale' %}
                            <div class="status-on-sale">在售中</div>
                        {% elif item.bucket == 'tradable' %}
                            <div class="status-tradable">可交易</div>
                        {% else %}
                            <div class="status-not-tradable">不可交易</div>
                        {% endif %}
                    </div>

                    <div class="action-stack">
                        <form class="compact-price-form" method="post" action="/save_item_price">
                            <input type="hidden" name="steam_id" value="{{ steam_id }}">
                            <input type="hidden" name="app_id" value="{{ app_id }}">
                            <input type="hidden" name="asset_id" value="{{ item.asset_id }}">
                            <input type="hidden" name="return_name" value="{{ selected_name or '' }}">
                            <input class="compact-input" type="number" step="0.01" name="purchase_price"
                                   value="{{ "%.2f"|format(item.purchase_price) }}">
                            <button class="save-btn" type="submit">保存成本价</button>
                        </form>

                        {% if item.bucket != 'on_sale' and item.token and item.style_token %}
                        <form class="sell-form" method="post" action="/sell_item">
                            <input type="hidden" name="steam_id" value="{{ steam_id }}">
                            <input type="hidden" name="app_id" value="{{ app_id }}">
                            <input type="hidden" name="item_key" value="{{ item.item_key }}">
                            <input type="hidden" name="return_name" value="{{ selected_name or '' }}">
                            <input type="hidden" name="inventory_filter" value="{{ inventory_filter }}">
                            <input class="compact-input" type="number" step="0.01" name="sale_price"
                                   value="{{ "%.2f"|format(item.price) }}" placeholder="上架价格">
                            <input class="compact-input" type="text" name="sale_description" placeholder="描述（可空）">
                            <select class="select-input" name="accept_bargain">
                                <option value="0">不接受还价</option>
                                <option value="1">接受还价</option>
                            </select>
                            <button class="sell-btn" type="submit">出售</button>
                        </form>
                        {% else %}
                        <div class="subtext">
                            {% if item.bucket == 'on_sale' %}
                            已在售中
                            {% else %}
                            无法上架
                            {% endif %}
                        </div>
                        {% endif %}
                    </div>
                </div>
                {% endfor %}
            </div>

            <div id="emptyState" class="empty" style="display:none;">没有匹配到饰品</div>
        {% else %}
            <div class="empty">当前数据库里没有该账号库存，请先同步库存</div>
        {% endif %}
    {% endif %}
</div>

<script>
const searchInput = document.getElementById("searchInput");
const emptyState = document.getElementById("emptyState");
const statTotal = document.getElementById("statTotal");
const statOnSale = document.getElementById("statOnSale");
const statTradable = document.getElementById("statTradable");
const statValue = document.getElementById("statValue");
const statCost = document.getElementById("statCost");
const viewMode = "{{ view_mode }}";

function applySummaryFilters() {
    const rows = document.querySelectorAll(".summary-item");
    const keyword = (searchInput?.value || "").trim().toLowerCase();

    let visibleGroups = 0;
    let totalItems = 0;
    let totalMarket = 0;
    let totalCost = 0;

    rows.forEach(card => {
        const blob = card.getAttribute("data-search") || "";
        const count = parseInt(card.getAttribute("data-count") || "0");
        const market = parseFloat(card.getAttribute("data-market") || "0");
        const cost = parseFloat(card.getAttribute("data-cost") || "0");

        const ok = !keyword || blob.includes(keyword);
        card.style.display = ok ? "grid" : "none";

        if (ok) {
            visibleGroups++;
            totalItems += count;
            totalMarket += market;
            totalCost += cost;
        }
    });

    if (statTotal) statTotal.textContent = visibleGroups;
    if (statOnSale) statOnSale.textContent = "-";
    if (statTradable) statTradable.textContent = totalItems;
    if (statValue) statValue.textContent = totalMarket.toFixed(2);
    if (statCost) statCost.textContent = totalCost.toFixed(2);
    if (emptyState) emptyState.style.display = visibleGroups === 0 ? "block" : "none";
}

function applyDetailFilters() {
    const rows = document.querySelectorAll(".detail-item");
    const keyword = (searchInput?.value || "").trim().toLowerCase();

    let visibleCount = 0;
    let visibleOnSale = 0;
    let visibleTradable = 0;
    let totalMarket = 0;
    let totalCost = 0;

    rows.forEach(card => {
        const blob = card.getAttribute("data-search") || "";
        const bucket = card.getAttribute("data-bucket") || "";
        const market = parseFloat(card.getAttribute("data-market") || "0");
        const cost = parseFloat(card.getAttribute("data-cost") || "0");

        let ok = true;
        if (keyword && !blob.includes(keyword)) ok = false;

        card.style.display = ok ? "grid" : "none";

        if (ok) {
            visibleCount++;
            totalMarket += market;
            totalCost += cost;
            if (bucket === "on_sale") visibleOnSale++;
            if (bucket === "tradable") visibleTradable++;
        }
    });

    if (statTotal) statTotal.textContent = visibleCount;
    if (statOnSale) statOnSale.textContent = visibleOnSale;
    if (statTradable) statTradable.textContent = visibleTradable;
    if (statValue) statValue.textContent = totalMarket.toFixed(2);
    if (statCost) statCost.textContent = totalCost.toFixed(2);
    if (emptyState) emptyState.style.display = visibleCount === 0 ? "block" : "none";
}

function applyFilters() {
    if (viewMode === "summary") {
        applySummaryFilters();
    } else {
        applyDetailFilters();
    }
}

if (searchInput) searchInput.addEventListener("input", applyFilters);
</script>
</body>
</html>
"""


ALL_INVENTORY_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>所有账号库存</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { box-sizing: border-box; }
        body { margin: 0; font-family: "Microsoft YaHei", Arial, sans-serif; background: #0b1220; color: #e5eefc; }
        .container { max-width: 1750px; margin: 0 auto; padding: 24px; }
        .topbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
        .title { font-size: 28px; font-weight: 800; color: #f8fbff; }
        .sub { color: #8ca3c7; margin-top: 6px; font-size: 14px; }
        .toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
        .input { width: 360px; padding: 12px 14px; border-radius: 12px; border: 1px solid #26354d; background: #111b2d; color: #eef4ff; outline: none; }
        .btn { padding: 12px 16px; border: 0; border-radius: 12px; background: #2563eb; color: #fff; cursor: pointer; font-size: 14px; text-decoration: none; }
        .btn:hover { background: #1e4fc0; }

        .msg { background: #12301d; border: 1px solid #2f8f50; color: #d7ffe4; padding: 14px 16px; border-radius: 14px; margin-bottom: 18px; }
        .error { background: #4a1318; border: 1px solid #d44; color: #ffd6d6; padding: 16px; border-radius: 16px; margin-bottom: 18px; }

        .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 14px; margin: 20px 0 24px 0; }
        .stat { background: #121c2f; border: 1px solid #25344c; border-radius: 18px; padding: 18px; }
        .stat-label { color: #8ca3c7; font-size: 13px; margin-bottom: 8px; }
        .stat-value { font-size: 28px; font-weight: 800; color: #f8fbff; }

        .toggle-bar {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }

        .list { display: grid; gap: 12px; }
        .head, .row {
            display: grid;
            grid-template-columns: 90px 2fr 1fr 1fr 0.8fr 0.8fr 0.8fr 0.8fr 260px;
            gap: 12px;
            align-items: center;
        }
        .head {
            padding: 8px 14px;
            color: #8ca3c7;
            font-size: 12px;
            font-weight: 700;
        }
        .row {
            background: linear-gradient(180deg, #152238, #101a2b);
            border: 1px solid #25344c;
            border-radius: 18px;
            padding: 14px;
        }
        .img {
            width: 80px;
            height: 64px;
            object-fit: contain;
            background: #0f1727;
            border-radius: 12px;
            border: 1px solid #30465f;
            padding: 6px;
        }
        .name { font-weight: 800; color: #f8fbff; line-height: 1.35; }
        .subtext { margin-top: 4px; color: #8ca3c7; font-size: 12px; line-height: 1.35; }
        .num { font-weight: 800; color: #f8fbff; }
        .profit-plus { color: #8ef0a7; font-weight: 800; }
        .profit-minus { color: #ffb4b4; font-weight: 800; }
        .status-on-sale { color: #f59e0b; font-weight: 700; }
        .status-tradable { color: #8ef0a7; font-weight: 700; }
        .status-not-tradable { color: #ffb4b4; font-weight: 700; }
        .empty { text-align: center; padding: 40px; color: #8ca3c7; font-size: 16px; }
        .sync-panel { background: linear-gradient(180deg, #152238, #101a2b); border: 1px solid #25344c; border-radius: 20px; padding: 18px; margin-bottom: 22px; box-shadow: 0 12px 28px rgba(0,0,0,0.28); }
        .sync-head { display:flex; justify-content:space-between; align-items:center; gap:12px; flex-wrap:wrap; margin-bottom:12px; }
        .sync-title { font-size: 18px; font-weight: 800; color:#f8fbff; }
        .sync-badge { padding: 8px 12px; border-radius: 999px; font-size: 12px; font-weight: 700; }
        .sync-running { background:#5b3b08; color:#ffd89a; border:1px solid #a56a12; }
        .sync-idle { background:#12301d; color:#d7ffe4; border:1px solid #2f8f50; }
        .sync-cancel { background:#4a1318; color:#ffd6d6; border:1px solid #d44; }
        .sync-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap:12px; }
        .sync-card { background: rgba(255,255,255,0.03); border:1px solid rgba(140,163,199,0.12); border-radius: 14px; padding: 12px 14px; }
        .sync-k { color:#8ca3c7; font-size:12px; margin-bottom:6px; }
        .sync-v { color:#f1f6ff; font-size:15px; font-weight:700; word-break:break-all; }
        .progress-wrap { margin-top: 14px; }
        .progress-bar { width:100%; height:14px; background:#0f1727; border-radius:999px; overflow:hidden; border:1px solid #30465f; }
        .progress-inner { height:100%; background:#2563eb; width:0%; transition:width .2s ease; }
        .sync-note { margin-top: 10px; color:#8ca3c7; font-size:13px; line-height:1.5; }

        .sell-form {
            display: grid;
            gap: 8px;
        }
        .compact-input, .select-input {
            width: 100%;
            padding: 9px 10px;
            border-radius: 10px;
            border: 1px solid #30465f;
            background: #0f1727;
            color: #eef4ff;
            outline: none;
        }
        .sell-btn {
            padding: 10px 12px;
            border: 0;
            border-radius: 10px;
            background: #d97706;
            color: white;
            cursor: pointer;
            font-size: 13px;
        }
        .sell-btn:hover {
            background: #b45309;
        }

        @media (max-width: 1350px) {
            .head, .row { grid-template-columns: 1fr; }
            .head { display: none; }
        }
    </style>
</head>
<body>
<div class="container">
    <div class="topbar">
        <div>
            <div class="title">所有账号库存</div>
            <div class="sub">汇总查看全部账号下的库存明细</div>
        </div>

        <div class="toolbar">
             <form method="get" action="/all_inventory" style="display:flex; gap:10px; flex-wrap:wrap;">
                <input class="input" type="text" name="q" value="{{ keyword }}" placeholder="搜索 名称 / assetId / styleId / SteamID / 昵称">
                <input type="hidden" name="inventory_filter" value="{{ inventory_filter }}">
                <button class="btn" type="submit">搜索</button>
             </form>
             <a class="btn" href="/sync/all_inventory">同步所有账号库存</a>
             <a class="btn" href="/">返回主页面</a>
        </div>
    </div>

    {% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
    {% if error %}<div class="error">{{ error }}</div>{% endif %}

    <div class="sync-panel">
        <div class="sync-head">
            <div class="sync-title">库存同步任务状态</div>
            {% if sync_status.running %}
                <div id="syncBadge" class="sync-badge {% if sync_status.cancel_requested %}sync-cancel{% else %}sync-running{% endif %}">
                    {{ '取消中' if sync_status.cancel_requested else '运行中' }}
                </div>
            {% else %}
                <div id="syncBadge" class="sync-badge sync-idle">空闲</div>
            {% endif %}
        </div>

        <div class="sync-grid">
            <div class="sync-card">
                <div class="sync-k">当前同步状态</div>
                <div id="syncStateText" class="sync-v">
                    {% if sync_status.running %}
                        {{ '已请求取消，等待当前账号结束' if sync_status.cancel_requested else '正在同步库存' }}
                    {% else %}
                        {{ sync_status.last_message or '当前没有运行中的同步任务' }}
                    {% endif %}
                </div>
            </div>

            <div class="sync-card">
                <div class="sync-k">当前正在同步的 SteamID</div>
                <div id="syncCurrentSteamId" class="sync-v">{{ sync_status.current_steam_id or '-' }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">已完成 / 总数</div>
                <div id="syncProgressText" class="sync-v">{{ sync_status.finished }}/{{ sync_status.total }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">成功 / 库存为空 / 失败</div>
                <div id="syncCounterText" class="sync-v">{{ sync_status.success_count }}/{{ sync_status.empty_count }}/{{ sync_status.failed_count }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">开始时间</div>
                <div id="syncStartedAt" class="sync-v">{{ sync_status.started_at or '-' }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">结束时间</div>
                <div id="syncEndedAt" class="sync-v">{{ sync_status.ended_at or '-' }}</div>
            </div>
        </div>

        <div class="progress-wrap">
            <div class="progress-bar">
                <div id="syncProgressBar" class="progress-inner" style="width: {{ (sync_status.finished * 100 / sync_status.total) if sync_status.total else 0 }}%;"></div>
            </div>
        </div>

        <div id="syncLastError" class="sync-note">
            {% if sync_status.last_error %}
                失败信息：{{ sync_status.last_error }}
            {% else %}
                暂无失败信息
            {% endif %}
        </div>
    </div>

    <div class="toggle-bar" style="margin: 16px 0 20px 0;">
        <a class="btn" href="/all_inventory?inventory_filter=all{% if keyword %}&q={{ keyword|urlencode }}{% endif %}"
           style="{{ 'background:#0f9d58;' if inventory_filter == 'all' else '' }}">全部</a>

        <a class="btn" href="/all_inventory?inventory_filter=on_sale{% if keyword %}&q={{ keyword|urlencode }}{% endif %}"
           style="{{ 'background:#0f9d58;' if inventory_filter == 'on_sale' else '' }}">在售中</a>

        <a class="btn" href="/all_inventory?inventory_filter=tradable{% if keyword %}&q={{ keyword|urlencode }}{% endif %}"
           style="{{ 'background:#0f9d58;' if inventory_filter == 'tradable' else '' }}">可交易</a>

        <a class="btn" href="/all_inventory?inventory_filter=not_tradable{% if keyword %}&q={{ keyword|urlencode }}{% endif %}"
           style="{{ 'background:#0f9d58;' if inventory_filter == 'not_tradable' else '' }}">不可交易</a>
    </div>

    <div class="stats">
        <div class="stat">
            <div class="stat-label">总件数</div>
            <div class="stat-value">{{ total_count }}</div>
        </div>
        <div class="stat">
            <div class="stat-label">在售中数量</div>
            <div class="stat-value">{{ on_sale_count }}</div>
        </div>
        <div class="stat">
            <div class="stat-label">可交易数量</div>
            <div class="stat-value">{{ tradable_count }}</div>
        </div>
        <div class="stat">
            <div class="stat-label">总市值</div>
            <div class="stat-value">{{ "%.2f"|format(total_market_value) }}</div>
        </div>
        <div class="stat">
            <div class="stat-label">总成本</div>
            <div class="stat-value">{{ "%.2f"|format(total_cost) }}</div>
        </div>
    </div>

    {% if items %}
        <div class="head">
            <div>图片</div>
            <div>商品</div>
            <div>账号</div>
            <div>标识</div>
            <div>市场价</div>
            <div>成本价</div>
            <div>利润</div>
            <div>分类</div>
            <div>出售</div>
        </div>

        <div class="list">
            {% for item in items %}
            <div class="row">
                <div>
                    <img class="img" src="{{ item.image_url or 'https://via.placeholder.com/80x64?text=No+Img' }}" alt="item"
                         onerror="this.src='https://via.placeholder.com/80x64?text=No+Img'">
                </div>

                <div>
                    <div class="name">{{ item.name }}</div>
                    <div class="subtext">{{ item.short_name or '' }}</div>
                    <div class="subtext">
                        {% if item.weapon_name %}{{ item.weapon_name }}{% endif %}
                        {% if item.weapon_name and item.exterior_name %} / {% endif %}
                        {% if item.exterior_name %}{{ item.exterior_name }}{% endif %}
                    </div>
                </div>

                <div>
                    <div class="num">{{ item.nickname or '未命名账号' }}</div>
                    <div class="subtext">{{ item.username or '' }}</div>
                    <div class="subtext">{{ item.steam_id }}</div>
                </div>

                <div>
                    <div class="subtext">assetId: {{ item.asset_id }}</div>
                    {% if item.style_id %}
                    <div class="subtext">styleId: {{ item.style_id }}</div>
                    {% endif %}
                    {% if item.wear %}
                    <div class="subtext">磨损: {{ item.wear }}</div>
                    {% endif %}
                </div>

                <div class="num">¥ {{ "%.2f"|format(item.price) }}</div>
                <div class="num">¥ {{ "%.2f"|format(item.purchase_price) }}</div>

                <div class="{{ 'profit-plus' if item.profit >= 0 else 'profit-minus' }}">
                    ¥ {{ "%.2f"|format(item.profit) }}
                </div>

                <div>
                    {% if item.bucket == 'on_sale' %}
                        <div class="status-on-sale">在售中</div>
                    {% elif item.bucket == 'tradable' %}
                        <div class="status-tradable">可交易</div>
                    {% else %}
                        <div class="status-not-tradable">不可交易</div>
                    {% endif %}
                </div>

                <div>
                    {% if item.bucket != 'on_sale' and item.token and item.style_token %}
                    <form class="sell-form" method="post" action="/sell_item">
                        <input type="hidden" name="steam_id" value="{{ item.steam_id }}">
                        <input type="hidden" name="app_id" value="{{ item.app_id }}">
                        <input type="hidden" name="item_key" value="{{ item.item_key }}">
                        <input type="hidden" name="return_all" value="1">
                        <input type="hidden" name="inventory_filter" value="{{ inventory_filter }}">
                        <input class="compact-input" type="number" step="0.01" name="sale_price"
                               value="{{ "%.2f"|format(item.price) }}" placeholder="上架价格">
                        <input class="compact-input" type="text" name="sale_description" placeholder="描述（可空）">
                        <select class="select-input" name="accept_bargain">
                            <option value="0">不接受还价</option>
                            <option value="1">接受还价</option>
                        </select>
                        <button class="sell-btn" type="submit">出售</button>
                    </form>
                    {% else %}
                    <div class="subtext">
                        {% if item.bucket == 'on_sale' %}
                        已在售中
                        {% else %}
                        无法上架
                        {% endif %}
                    </div>
                    {% endif %}
                </div>
            </div>
            {% endfor %}
        </div>
    {% else %}
        <div class="empty">当前没有库存数据，请先同步所有账号库存</div>
    {% endif %}
</div>
</body>
</html>
"""


PROFIT_ANALYSIS_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>利润分析</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { box-sizing: border-box; }
        body { margin: 0; font-family: "Microsoft YaHei", Arial, sans-serif; background: #0b1220; color: #e5eefc; }
        .container { max-width: 1800px; margin: 0 auto; padding: 24px; }
        .topbar { display: flex; justify-content: space-between; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
        .title { font-size: 28px; font-weight: 800; color: #f8fbff; }
        .sub { color: #8ca3c7; margin-top: 6px; font-size: 14px; }
        .toolbar { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
        .input { width: 360px; padding: 12px 14px; border-radius: 12px; border: 1px solid #26354d; background: #111b2d; color: #eef4ff; outline: none; }
        .select { padding: 12px 14px; border-radius: 12px; border: 1px solid #26354d; background: #111b2d; color: #eef4ff; outline: none; }
        .btn { padding: 12px 16px; border: 0; border-radius: 12px; background: #2563eb; color: #fff; cursor: pointer; font-size: 14px; text-decoration: none; }
        .btn:hover { background: #1e4fc0; }
        .msg { background: #12301d; border: 1px solid #2f8f50; color: #d7ffe4; padding: 14px 16px; border-radius: 14px; margin-bottom: 18px; }
        .error { background: #4a1318; border: 1px solid #d44; color: #ffd6d6; padding: 16px; border-radius: 16px; margin-bottom: 18px; }

        .stats {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
            gap: 14px;
            margin: 20px 0 24px 0;
        }
        .stat {
            background: #121c2f;
            border: 1px solid #25344c;
            border-radius: 18px;
            padding: 18px;
        }
        .stat-label { color: #8ca3c7; font-size: 13px; margin-bottom: 8px; }
        .stat-value { font-size: 28px; font-weight: 800; color: #f8fbff; }

        .section {
            margin-top: 28px;
        }
        .section-title {
            font-size: 20px;
            font-weight: 800;
            margin-bottom: 14px;
            color: #f8fbff;
        }

        .list { display: grid; gap: 12px; }
        .head, .row {
            display: grid;
            gap: 12px;
            align-items: center;
        }

        .order-head, .order-row {
            grid-template-columns: 90px 2.2fr 1fr 1fr 0.9fr 0.9fr 0.9fr 1fr;
        }

        .summary-head, .summary-row {
            grid-template-columns: 2.2fr 0.8fr 1fr 1fr 1fr;
        }

        .head {
            padding: 8px 14px;
            color: #8ca3c7;
            font-size: 12px;
            font-weight: 700;
        }

        .row {
            background: linear-gradient(180deg, #152238, #101a2b);
            border: 1px solid #25344c;
            border-radius: 18px;
            padding: 14px;
        }

        .img {
            width: 80px;
            height: 64px;
            object-fit: contain;
            background: #0f1727;
            border-radius: 12px;
            border: 1px solid #30465f;
            padding: 6px;
        }

        .name { font-weight: 800; color: #f8fbff; line-height: 1.35; }
        .subtext { margin-top: 4px; color: #8ca3c7; font-size: 12px; line-height: 1.35; }
        .num { font-weight: 800; color: #f8fbff; }
        .profit-plus { color: #8ef0a7; font-weight: 800; }
        .profit-minus { color: #ffb4b4; font-weight: 800; }

        .empty {
            text-align: center;
            padding: 40px;
            color: #8ca3c7;
            font-size: 16px;
            background: #121c2f;
            border: 1px solid #25344c;
            border-radius: 18px;
        }

        @media (max-width: 1280px) {
            .order-head, .order-row,
            .summary-head, .summary-row {
                grid-template-columns: 1fr;
            }
            .head { display: none; }
        }
    </style>
</head>
<body>
<div class="container">
    <div class="topbar">
        <div>
            <div class="title">利润分析</div>
            <div class="sub">基于卖家订单列表中的已完成订单计算利润</div>
        </div>

        <div class="toolbar">
            <form method="get" action="/profit_analysis" style="display:flex; gap:10px; flex-wrap:wrap; align-items:center;">
                <input class="input" type="text" name="q" value="{{ keyword }}" placeholder="搜索 名称 / assetId / styleId / SteamID / 订单号">
                <select class="select" name="steam_id">
                    <option value="">全部账号</option>
                    {% for acc in accounts %}
                    <option value="{{ acc.steam_id }}" {{ 'selected' if selected_steam_id == acc.steam_id else '' }}>
                        {{ acc.nickname if acc.nickname else acc.steam_id }}
                    </option>
                    {% endfor %}
                </select>
                <button class="btn" type="submit">筛选</button>
            </form>

            <a class="btn" href="/sync/profit_orders">同步利润订单</a>
            <a class="btn" href="/">返回主页面</a>
        </div>
    </div>

    {% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
    {% if error %}<div class="error">{{ error }}</div>{% endif %}

    <div class="sync-panel">
        <div class="sync-head">
            <div class="sync-title">库存同步任务状态</div>
            {% if sync_status.running %}
                <div id="syncBadge" class="sync-badge {% if sync_status.cancel_requested %}sync-cancel{% else %}sync-running{% endif %}">
                    {{ '取消中' if sync_status.cancel_requested else '运行中' }}
                </div>
            {% else %}
                <div id="syncBadge" class="sync-badge sync-idle">空闲</div>
            {% endif %}
        </div>

        <div class="sync-grid">
            <div class="sync-card">
                <div class="sync-k">当前同步状态</div>
                <div id="syncStateText" class="sync-v">
                    {% if sync_status.running %}
                        {{ '已请求取消，等待当前账号结束' if sync_status.cancel_requested else '正在同步库存' }}
                    {% else %}
                        {{ sync_status.last_message or '当前没有运行中的同步任务' }}
                    {% endif %}
                </div>
            </div>

            <div class="sync-card">
                <div class="sync-k">当前正在同步的 SteamID</div>
                <div id="syncCurrentSteamId" class="sync-v">{{ sync_status.current_steam_id or '-' }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">已完成 / 总数</div>
                <div id="syncProgressText" class="sync-v">{{ sync_status.finished }}/{{ sync_status.total }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">成功 / 库存为空 / 失败</div>
                <div id="syncCounterText" class="sync-v">{{ sync_status.success_count }}/{{ sync_status.empty_count }}/{{ sync_status.failed_count }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">开始时间</div>
                <div id="syncStartedAt" class="sync-v">{{ sync_status.started_at or '-' }}</div>
            </div>

            <div class="sync-card">
                <div class="sync-k">结束时间</div>
                <div id="syncEndedAt" class="sync-v">{{ sync_status.ended_at or '-' }}</div>
            </div>
        </div>

        <div class="progress-wrap">
            <div class="progress-bar">
                <div id="syncProgressBar" class="progress-inner" style="width: {{ (sync_status.finished * 100 / sync_status.total) if sync_status.total else 0 }}%;"></div>
            </div>
        </div>

        <div id="syncLastError" class="sync-note">
            {% if sync_status.last_error %}
                失败信息：{{ sync_status.last_error }}
            {% else %}
                暂无失败信息
            {% endif %}
        </div>
    </div>

    <div class="stats">
        <div class="stat">
            <div class="stat-label">已完成订单数</div>
            <div class="stat-value">{{ summary.total_orders }}</div>
        </div>
        <div class="stat">
            <div class="stat-label">总成交额</div>
            <div class="stat-value">{{ "%.2f"|format(summary.total_revenue) }}</div>
        </div>
        <div class="stat">
            <div class="stat-label">总成本</div>
            <div class="stat-value">{{ "%.2f"|format(summary.total_cost) }}</div>
        </div>
        <div class="stat">
            <div class="stat-label">总利润</div>
            <div class="stat-value">{{ "%.2f"|format(summary.total_profit) }}</div>
        </div>
    </div>

    <div class="section">
        <div class="section-title">单笔订单利润</div>
        {% if profit_rows %}
            <div class="head order-head">
                <div>图片</div>
                <div>商品 / 订单</div>
                <div>账号</div>
                <div>标识</div>
                <div>卖出价</div>
                <div>成本价</div>
                <div>利润</div>
                <div>时间</div>
            </div>

            <div class="list">
                {% for row in profit_rows %}
                <div class="row order-row">
                    <div>
                        <img class="img" src="{{ row.image_url or 'https://via.placeholder.com/80x64?text=No+Img' }}" alt="item"
                             onerror="this.src='https://via.placeholder.com/80x64?text=No+Img'">
                    </div>

                    <div>
                        <div class="name">{{ row.name or '未命名商品' }}</div>
                        <div class="subtext">
                            订单号: {{ row.order_id }}
                            {% if row.product_id %} / productId: {{ row.product_id }}{% endif %}
                        </div>
                        <div class="subtext">
                            {% if row.weapon_name %}{{ row.weapon_name }}{% endif %}
                            {% if row.weapon_name and row.exterior_name %} / {% endif %}
                            {% if row.exterior_name %}{{ row.exterior_name }}{% endif %}
                        </div>
                    </div>

                    <div>
                        <div class="num">{{ row.nickname or '未命名账号' }}</div>
                        <div class="subtext">{{ row.username or '' }}</div>
                        <div class="subtext">{{ row.steam_id }}</div>
                    </div>

                    <div>
                        <div class="subtext">assetId: {{ row.asset_id }}</div>
                        {% if row.style_id %}
                        <div class="subtext">styleId: {{ row.style_id }}</div>
                        {% endif %}
                        {% if row.wear %}
                        <div class="subtext">磨损: {{ row.wear }}</div>
                        {% endif %}
                    </div>

                    <div class="num">¥ {{ "%.2f"|format(row.order_price) }}</div>
                    <div class="num">¥ {{ "%.2f"|format(row.cost_price) }}</div>

                    <div class="{{ 'profit-plus' if row.profit >= 0 else 'profit-minus' }}">
                        ¥ {{ "%.2f"|format(row.profit) }}
                    </div>

                    <div>
                        <div class="num">{{ row.order_create_time_str }}</div>
                        <div class="subtext">{{ row.status_name or '已完成' }}</div>
                    </div>
                </div>
                {% endfor %}
            </div>
        {% else %}
            <div class="empty">暂无利润订单数据，先点“同步利润订单”</div>
        {% endif %}
    </div>

    <div class="section">
        <div class="section-title">按名称汇总利润</div>
        {% if summary.by_name_rows %}
            <div class="head summary-head">
                <div>名称</div>
                <div>数量</div>
                <div>成交额</div>
                <div>成本</div>
                <div>利润</div>
            </div>

            <div class="list">
                {% for row in summary.by_name_rows %}
                <div class="row summary-row">
                    <div class="name">{{ row.name }}</div>
                    <div class="num">{{ row.count }}</div>
                    <div class="num">¥ {{ "%.2f"|format(row.revenue) }}</div>
                    <div class="num">¥ {{ "%.2f"|format(row.cost) }}</div>
                    <div class="{{ 'profit-plus' if row.profit >= 0 else 'profit-minus' }}">
                        ¥ {{ "%.2f"|format(row.profit) }}
                    </div>
                </div>
                {% endfor %}
            </div>
        {% else %}
            <div class="empty">暂无汇总数据</div>
        {% endif %}
    </div>

    <div class="section">
        <div class="section-title">按账号汇总利润</div>
        {% if summary.by_account_rows %}
            <div class="head summary-head">
                <div>账号</div>
                <div>数量</div>
                <div>成交额</div>
                <div>成本</div>
                <div>利润</div>
            </div>

            <div class="list">
                {% for row in summary.by_account_rows %}
                <div class="row summary-row">
                    <div>
                        <div class="name">{{ row.nickname or '未命名账号' }}</div>
                        <div class="subtext">{{ row.username or '' }}</div>
                        <div class="subtext">{{ row.steam_id }}</div>
                    </div>
                    <div class="num">{{ row.count }}</div>
                    <div class="num">¥ {{ "%.2f"|format(row.revenue) }}</div>
                    <div class="num">¥ {{ "%.2f"|format(row.cost) }}</div>
                    <div class="{{ 'profit-plus' if row.profit >= 0 else 'profit-minus' }}">
                        ¥ {{ "%.2f"|format(row.profit) }}
                    </div>
                </div>
                {% endfor %}
            </div>
        {% else %}
            <div class="empty">暂无账号汇总数据</div>
        {% endif %}
    </div>
</div>
</body>
</html>
"""


# =========================
# 路由
# =========================
@app.route("/")
def accounts_page():
    msg = request.args.get("msg", "")
    error = request.args.get("error", "")
    steam_accounts = get_all_accounts_from_db()
    sync_status = get_sync_state()

    return render_template_string(
        ACCOUNTS_TEMPLATE,
        msg=msg,
        error=error,
        steam_accounts=steam_accounts,
        sync_status=sync_status
    )


@app.route("/sync/accounts")
def sync_accounts():
    error = sync_accounts_to_db()
    if error:
        return redirect(url_for("accounts_page", error=f"同步账号失败：{error}"))
    return redirect(url_for("accounts_page", msg="所有账号同步成功"))


@app.route("/sync/all_inventory")
def sync_all_inventory():
    app_id = request.args.get("appId", DEFAULT_APP_ID)
    ok, message = start_inventory_sync_background(app_id=app_id)
    if not ok:
        return redirect(url_for("accounts_page", error=message))
    return redirect(url_for("accounts_page", msg=message))


@app.route("/sync/cancel_all_inventory")
def cancel_all_inventory():
    cancelled = request_cancel_sync()
    if not cancelled:
        return redirect(url_for("accounts_page", msg="当前没有正在进行的库存同步任务"))
    return redirect(url_for("accounts_page", msg="已请求取消库存同步，当前账号处理完后会停止"))


@app.route("/sync/status")
def sync_status_api():
    return jsonify(get_sync_state())


@app.route("/sync/failed_inventory")
def sync_failed_inventory():
    app_id = request.args.get("appId", DEFAULT_APP_ID)

    if get_sync_state().get("running"):
        return redirect(url_for("accounts_page", error="当前有库存同步任务正在运行，请先取消或等待完成"))

    failed_ids = load_failed_sync_ids()
    if not failed_ids:
        return redirect(url_for("accounts_page", msg="当前没有失败账号需要重试"))

    success_count = 0
    empty_count = 0
    still_failed = []
    failed_msgs = []

    for steam_id in failed_ids:
        steam_id = str(steam_id).strip()
        if not steam_id:
            continue

        print(f"[重试失败账号] 正在处理 SteamID: {steam_id}")

        result = sync_inventory_to_db(steam_id, app_id=app_id)

        if result == "empty":
            print(f"[重试成功-库存为空] {steam_id}")
            success_count += 1
            empty_count += 1
        elif result:
            print(f"[重试仍失败] {steam_id}: {result}")
            still_failed.append(steam_id)
            failed_msgs.append(f"{steam_id}: {result}")
        else:
            print(f"[重试成功] {steam_id}")
            success_count += 1

    save_failed_sync_ids(still_failed)

    if still_failed:
        msg = f"失败账号重试完成，成功 {success_count} 个，其中库存为空 {empty_count} 个，仍失败 {len(still_failed)} 个"
        err = " | ".join(failed_msgs[:8])
        if len(failed_msgs) > 8:
            err += f" ... 其余 {len(failed_msgs) - 8} 个失败"
        return redirect(url_for("accounts_page", msg=msg, error=err))

    return redirect(url_for("accounts_page", msg=f"失败账号已全部重试成功，共 {success_count} 个，其中库存为空 {empty_count} 个"))


@app.route("/sync/profit_orders")
def sync_profit_orders():
    app_id = request.args.get("appId", DEFAULT_APP_ID)
    error = sync_accounts_to_db()
    if error:
        return redirect(url_for("accounts_page", error=f"同步账号失败：{error}"))

    # 只同步已完成订单
    sync_error = sync_seller_orders_to_db(app_id=app_id, status="10")
    if sync_error:
        return redirect(url_for("profit_analysis_page", error=sync_error))

    return redirect(url_for("profit_analysis_page", msg="利润订单同步成功"))


@app.route("/profit_analysis")
def profit_analysis_page():
    sync_status = get_sync_state()
    app_id = request.args.get("appId", DEFAULT_APP_ID)
    keyword = request.args.get("q", "").strip()
    selected_steam_id = request.args.get("steam_id", "").strip()
    msg = request.args.get("msg", "")
    error = request.args.get("error", "")

    accounts = get_all_accounts_from_db()
    profit_rows = get_profit_rows_from_db(app_id=app_id, keyword=keyword, steam_id=selected_steam_id)
    summary = build_profit_summary(profit_rows)

    return render_template_string(
        PROFIT_ANALYSIS_TEMPLATE,
        accounts=accounts,
        profit_rows=profit_rows,
        summary=summary,
        keyword=keyword,
        selected_steam_id=selected_steam_id,
        msg=msg,
        error=error,
        sync_status = sync_status
    )


@app.route("/inventory/<steam_id>")
def inventory_page(steam_id):
    msg = request.args.get("msg", "")
    error = request.args.get("error", "")
    app_id = request.args.get("appId", DEFAULT_APP_ID)
    view_mode = request.args.get("view", "summary")
    selected_name = request.args.get("name", "").strip()
    inventory_filter = request.args.get("inventory_filter", "all").strip()

    sync_status = get_sync_state()

    items = get_inventory_from_db(steam_id, app_id=app_id)

    if selected_name:
        items = [x for x in items if str(x.get("name", "")).strip() == selected_name]

    items = apply_inventory_filter(items, inventory_filter)

    for item in items:
        item["bucket"] = get_status_bucket(item)

    total_count = len(items)
    on_sale_count = sum(1 for x in items if x["bucket"] == "on_sale")
    tradable_count = sum(1 for x in items if x["bucket"] == "tradable")
    total_market_value = sum(safe_float(x["price"], 0) for x in items)
    total_cost = sum(safe_float(x["purchase_price"], 0) for x in items)

    summary_rows = []
    if view_mode == "summary":
        summary_rows = group_inventory_by_name(items, steam_id, app_id)
        total_count = len(summary_rows)
        on_sale_count = sum(x["on_sale_count"] for x in summary_rows)
        tradable_count = sum(x["tradable_count"] for x in summary_rows)
        total_market_value = sum(x["total_market_value"] for x in summary_rows)
        total_cost = sum(x["total_cost"] for x in summary_rows)

    return render_template_string(
        INVENTORY_TEMPLATE,
        msg=msg,
        error=error,
        steam_id=steam_id,
        app_id=app_id,
        view_mode=view_mode,
        selected_name=selected_name,
        inventory_filter=inventory_filter,
        items=items,
        summary_rows=summary_rows,
        total_count=total_count,
        on_sale_count=on_sale_count,
        tradable_count=tradable_count,
        total_market_value=total_market_value,
        total_cost=total_cost,
        sync_status=sync_status
    )

@app.route("/sync/inventory/<steam_id>")
def sync_inventory(steam_id):
    app_id = request.args.get("appId", DEFAULT_APP_ID)
    result = sync_inventory_to_db(steam_id, app_id=app_id)

    if result == "empty":
        return redirect(url_for("inventory_page", steam_id=steam_id, appId=app_id, msg="库存同步成功：该账号当前库存为空"))

    if result:
        return redirect(url_for("inventory_page", steam_id=steam_id, appId=app_id, error=f"同步库存失败：{result}"))

    return redirect(url_for("inventory_page", steam_id=steam_id, appId=app_id, msg="库存同步成功"))


@app.route("/save_group_price", methods=["POST"])
def save_group_price_route():
    steam_id = request.form.get("steam_id", "").strip()
    app_id = request.form.get("app_id", DEFAULT_APP_ID).strip()
    group_name = request.form.get("group_name", "").strip()
    default_purchase_price = request.form.get("default_purchase_price", "0").strip()

    if not steam_id or not group_name:
        return redirect(url_for("accounts_page", error="保存组成本价失败：缺少必要参数"))

    save_group_default_purchase_price(steam_id, app_id, group_name, default_purchase_price)
    apply_group_price_to_items(steam_id, app_id, group_name, default_purchase_price)

    return redirect(url_for(
        "inventory_page",
        steam_id=steam_id,
        appId=app_id,
        view="summary",
        msg=f"已保存组默认成本价并同步到组内单品：{group_name}"
    ))


@app.route("/save_item_price", methods=["POST"])
def save_item_price_route():
    steam_id = request.form.get("steam_id", "").strip()
    app_id = request.form.get("app_id", DEFAULT_APP_ID).strip()
    asset_id = request.form.get("asset_id", "").strip()
    purchase_price = request.form.get("purchase_price", "0").strip()
    return_name = request.form.get("return_name", "").strip()

    if not steam_id or not asset_id:
        return redirect(url_for("accounts_page", error="保存单品成本价失败：缺少必要参数"))

    save_item_purchase_price(steam_id, app_id, asset_id, purchase_price)

    if return_name:
        return redirect(url_for(
            "inventory_page",
            steam_id=steam_id,
            appId=app_id,
            view="detail",
            name=return_name,
            msg="单品购入成本价保存成功"
        ))

    return redirect(url_for(
        "inventory_page",
        steam_id=steam_id,
        appId=app_id,
        view="detail",
        msg="单品购入成本价保存成功"
    ))


@app.route("/sell_item", methods=["POST"])
def sell_item_route():
    steam_id = request.form.get("steam_id", "").strip()
    app_id = request.form.get("app_id", DEFAULT_APP_ID).strip()
    item_key = request.form.get("item_key", "").strip()
    sale_price = request.form.get("sale_price", "0").strip()
    sale_description = request.form.get("sale_description", "").strip()
    accept_bargain = request.form.get("accept_bargain", "0").strip()
    return_name = request.form.get("return_name", "").strip()
    return_all = request.form.get("return_all", "").strip()
    inventory_filter = request.form.get("inventory_filter", "all").strip()

    if not item_key:
        return redirect(url_for("accounts_page", error="上架失败：缺少 item_key"))

    item = get_inventory_item_for_sale(item_key=item_key, steam_id=steam_id if not return_all else None, app_id=app_id)
    if not item:
        if return_all:
            return redirect(url_for("all_inventory_page", error="上架失败：未找到库存记录"))
        return redirect(url_for("inventory_page", steam_id=steam_id, appId=app_id, error="上架失败：未找到库存记录"))

    token = item.get("token", "")
    style_token = item.get("style_token", "")

    if not token or not style_token:
        if return_all:
            return redirect(url_for("all_inventory_page", error="上架失败：缺少 token 或 styleToken"))
        return redirect(url_for("inventory_page", steam_id=steam_id, appId=app_id, error="上架失败：缺少 token 或 styleToken"))

    result, error = sale_inventory_item(
        token=token,
        style_token=style_token,
        price=sale_price,
        description=sale_description,
        accept_bargain=accept_bargain
    )

    if error:
        if return_all:
            return redirect(url_for("all_inventory_page", error=f"上架失败：{error}"))
        if return_name:
            return redirect(url_for(
                "inventory_page",
                steam_id=steam_id,
                appId=app_id,
                view="detail",
                name=return_name,
                inventory_filter=inventory_filter,
                error=f"上架失败：{error}"
            ))
        return redirect(url_for(
            "inventory_page",
            steam_id=steam_id,
            appId=app_id,
            view="detail",
            inventory_filter=inventory_filter,
            error=f"上架失败：{error}"
        ))
    # 上架成功后，按 asset_id 更新当前物品状态为在售中
    asset_id = str(item.get("asset_id", "") or "").strip()

    if asset_id:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("""
        UPDATE inventory_items
        SET status = 1, updated_at = ?
        WHERE steam_id = ? AND app_id = ? AND asset_id = ?
        """, (now_str(), steam_id, app_id, asset_id))
        conn.commit()
        conn.close()


    succeed = result.get("succeed", 0)
    failed = result.get("failed", 0)

    msg = f"上架完成：成功 {succeed} 个，失败 {failed} 个"

    if return_all:
        return redirect(url_for("all_inventory_page", inventory_filter=inventory_filter, msg=msg))

    if return_name:
        return redirect(url_for(
            "inventory_page",
            steam_id=steam_id,
            appId=app_id,
            view="detail",
            name=return_name,
            inventory_filter=inventory_filter,
            msg=msg
        ))

    return redirect(url_for(
        "inventory_page",
        steam_id=steam_id,
        appId=app_id,
        view="detail",
        inventory_filter=inventory_filter,
        msg=msg
    ))


@app.route("/all_inventory")
def all_inventory_page():
    app_id = request.args.get("appId", DEFAULT_APP_ID)
    keyword = request.args.get("q", "").strip().lower()
    inventory_filter = request.args.get("inventory_filter", "all").strip()
    msg = request.args.get("msg", "")
    error = request.args.get("error", "")

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        i.steam_id,
        i.app_id,
        i.item_key,
        i.asset_id,
        i.token,
        i.style_token,
        i.name,
        i.short_name,
        i.image_url,
        i.price,
        i.status,
        i.if_tradable,
        i.wear,
        i.style_id,
        i.weapon_name,
        i.exterior_name,
        COALESCE(p.purchase_price, 0) AS purchase_price,
        a.nickname,
        a.username
    FROM inventory_items i
    LEFT JOIN item_purchase_prices p
    ON i.steam_id = p.steam_id
    AND i.app_id = p.app_id
    AND i.asset_id = p.asset_id

    LEFT JOIN steam_accounts a
      ON i.steam_id = a.steam_id
    WHERE i.app_id = ?
    ORDER BY i.price DESC, i.name ASC
    """, (app_id,))

    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    items = []
    for row in rows:
        row["purchase_price"] = safe_float(row.get("purchase_price", 0), 0)
        row["profit"] = safe_float(row.get("price", 0), 0) - row["purchase_price"]
        row["status_text"] = translate_status(row["status"])
        row["bucket"] = get_status_bucket(row)
        row["search_blob"] = " ".join([
            str(row.get("name", "")),
            str(row.get("short_name", "")),
            str(row.get("asset_id", "")),
            str(row.get("style_id", "")),
            str(row.get("weapon_name", "")),
            str(row.get("exterior_name", "")),
            str(row.get("wear", "")),
            str(row.get("steam_id", "")),
            str(row.get("nickname", "")),
            str(row.get("username", "")),
        ]).lower()

        if keyword and keyword not in row["search_blob"]:
            continue

        items.append(row)

    items = apply_inventory_filter(items, inventory_filter)

    total_count = len(items)
    on_sale_count = sum(1 for x in items if x["bucket"] == "on_sale")
    tradable_count = sum(1 for x in items if x["bucket"] == "tradable")
    total_market_value = sum(safe_float(x["price"], 0) for x in items)
    total_cost = sum(safe_float(x["purchase_price"], 0) for x in items)

    return render_template_string(
        ALL_INVENTORY_TEMPLATE,
        items=items,
        total_count=total_count,
        on_sale_count=on_sale_count,
        tradable_count=tradable_count,
        total_market_value=total_market_value,
        total_cost=total_cost,
        keyword=keyword,
        inventory_filter=inventory_filter,
        msg=msg,
        error=error
    )


# =========================
# 启动
# =========================
if __name__ == "__main__":
    init_db()
    print("启动成功，请打开浏览器访问: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=True)
