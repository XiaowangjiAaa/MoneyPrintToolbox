# -*- coding: utf-8 -*-
import http.client
import json
import time
import csv
import ssl
import socket
from collections import Counter

# ================== 配置 ==================
APP_ID = 730
APP_KEY = ""

DELAY_SECONDS = 5                  # 每个 SteamID 成功请求后的间隔
REQUEST_TIMEOUT = 15               # 单次请求超时秒数
MAX_RETRIES = 3                    # 单个 SteamID 单轮最多重试次数
RETRY_BACKOFF_BASE = 2             # 单个 SteamID 内部重试退避：2s,4s,6s...

ROUND_FAIL_DELAY = 180             # 一轮结束后，如果还有失败SteamID，下一轮开始前等待秒数
MAX_GLOBAL_ROUNDS = None           # 全局重试轮数；None 表示无限轮，直到全部成功

STEAM_IDS_FILE = "steam_ids.json"
OUTPUT_CSV = "inventory_summary.csv"
NO_INV_TXT = "no_inventory_steamids.txt"
FAILED_TXT = "request_failed_steamids.txt"
SUMMARY_TXT = "inventory_trade_summary.txt"

TRADE_OK_STATUS = 0                # 可交易
TRADE_NO_STATUS = 4                # 不可交易（按你当前定义）
EMPTY_INVENTORY_ERROR_CODE = 206003  # 库存为空

# ========= 价值统计相关配置 =========
ENABLE_VALUE_ESTIMATION = True     # 是否启用库存价值估算
MARKET_DELIVERY = 2                # 发货方式: 1-人工 2-自动
MARKET_ASSET_TYPE = 1              # 在售类型: 1-普通 2-冷却期
MARKET_PAGE_SIZE = 10             # 市场查询每页数量，最大50
MARKET_MAX_SCAN_PAGES = 2         # 每个饰品最多扫描多少页找最低价，避免太慢
MARKET_REQUEST_DELAY = 1         # 每次市场价格请求之间的轻微间隔
PRICE_NOT_FOUND_VALUE = 0.0        # 找不到价格时按多少算
# ==========================================


def money(v):
    try:
        return f"{float(v):.2f}"
    except Exception:
        return "0.00"


def load_steam_ids(path):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("steamIds", [])


def fetch_inventory(steam_id, retries=MAX_RETRIES, timeout=REQUEST_TIMEOUT):
    """
    请求 C5 库存接口，失败自动重试。

    返回:
      - 正常成功: data(dict)
      - 库存为空: {"total": 0, "list": []}
      - 真失败: None
    """
    path = f"/merchant/inventory/v2/{steam_id}/{APP_ID}?language=zh&app-key={APP_KEY}"

    for attempt in range(1, retries + 1):
        conn = None
        try:
            conn = http.client.HTTPSConnection(
                "openapi.c5game.com",
                timeout=timeout
            )

            headers = {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Connection": "close",
            }

            conn.request("GET", path, headers=headers)
            res = conn.getresponse()
            raw = res.read().decode("utf-8", errors="replace")

            if res.status != 200:
                print(f"[{steam_id}] HTTP状态异常: {res.status}")
                if attempt < retries:
                    sleep_s = RETRY_BACKOFF_BASE * attempt
                    print(f"[{steam_id}] {sleep_s} 秒后重试...")
                    time.sleep(sleep_s)
                    continue
                return None

            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[{steam_id}] JSON解析失败，返回内容前200字符: {raw[:200]!r}")
                return None

            # 1. 正常成功
            if obj.get("success"):
                return obj.get("data", {}) or {"total": 0, "list": []}

            # 2. 库存为空：按成功处理
            error_code = obj.get("errorCode")
            error_msg = obj.get("errorMsg", "")

            if error_code == EMPTY_INVENTORY_ERROR_CODE or error_msg == "库存为空":
                print(f"[{steam_id}] 查询成功：库存为空")
                return {"total": 0, "list": []}

            # 3. 其他业务错误：按失败处理
            print(f"[{steam_id}] 请求失败: errorCode={error_code}, errorMsg={error_msg}")
            return None

        except (
            ConnectionResetError,
            ConnectionAbortedError,
            TimeoutError,
            socket.timeout,
            ssl.SSLError,
            OSError,
            http.client.HTTPException,
        ) as e:
            print(f"[{steam_id}] 第 {attempt}/{retries} 次请求失败: {repr(e)}")
            if attempt < retries:
                sleep_s = RETRY_BACKOFF_BASE * attempt
                print(f"[{steam_id}] {sleep_s} 秒后重试...")
                time.sleep(sleep_s)
            else:
                print(f"[{steam_id}] 本轮重试后仍失败")
                return None

        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def fetch_market_products_page(
    item_id=None,
    market_hash_name=None,
    app_id=APP_ID,
    delivery=MARKET_DELIVERY,
    asset_type=MARKET_ASSET_TYPE,
    page_num=1,
    page_size=MARKET_PAGE_SIZE,
    retries=MAX_RETRIES,
    timeout=REQUEST_TIMEOUT
):
    """
    调用市场列表接口，获取某个饰品的一页在售数据。
    返回:
      - success: (list, has_more)
      - fail: (None, None)
    """
    payload_dict = {
        "delivery": delivery,
        "assetType": asset_type,
        "pageNum": page_num,
        "pageSize": page_size,
    }

    if item_id:
        payload_dict["itemId"] = item_id
    elif market_hash_name:
        payload_dict["marketHashName"] = market_hash_name
        payload_dict["appId"] = app_id
    else:
        return None, None

    payload = json.dumps(payload_dict, ensure_ascii=False)

    path = f"/merchant/market/v2/products/list?app-key={APP_KEY}"

    for attempt in range(1, retries + 1):
        conn = None
        try:
            conn = http.client.HTTPSConnection(
                "openapi.c5game.com",
                timeout=timeout
            )
            headers = {
                "User-Agent": "Mozilla/5.0",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Connection": "close",
            }

            conn.request("POST", path, body=payload.encode("utf-8"), headers=headers)
            res = conn.getresponse()
            raw = res.read().decode("utf-8", errors="replace")

            if res.status != 200:
                print(f"[market] HTTP状态异常: {res.status} | payload={payload_dict}")
                if attempt < retries:
                    sleep_s = RETRY_BACKOFF_BASE * attempt
                    time.sleep(sleep_s)
                    continue
                return None, None

            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                print(f"[market] JSON解析失败，返回内容前200字符: {raw[:200]!r}")
                return None, None

            if not obj.get("success"):
                print(
                    f"[market] 请求失败: errorCode={obj.get('errorCode')}, "
                    f"errorMsg={obj.get('errorMsg')} | payload={payload_dict}"
                )
                return None, None

            data = obj.get("data") or {}
            lst = data.get("list") or []
            has_more = bool(data.get("hasMore", False))
            return lst, has_more

        except (
            ConnectionResetError,
            ConnectionAbortedError,
            TimeoutError,
            socket.timeout,
            ssl.SSLError,
            OSError,
            http.client.HTTPException,
        ) as e:
            print(f"[market] 第 {attempt}/{retries} 次请求失败: {repr(e)} | payload={payload_dict}")
            if attempt < retries:
                sleep_s = RETRY_BACKOFF_BASE * attempt
                time.sleep(sleep_s)
            else:
                return None, None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass


def fetch_market_lowest_price(
    item_id=None,
    market_hash_name=None,
    app_id=APP_ID,
    max_scan_pages=MARKET_MAX_SCAN_PAGES
):
    """
    获取某个饰品在市场上的最低价格。
    做法：
      - 扫描最多 max_scan_pages 页
      - 从返回的所有 product 中取 min(price)

    返回:
      float 或 None
    """
    lowest = None

    for page in range(1, max_scan_pages + 1):
        lst, has_more = fetch_market_products_page(
            item_id=item_id,
            market_hash_name=market_hash_name,
            app_id=app_id,
            page_num=page,
            page_size=MARKET_PAGE_SIZE,
        )

        if lst is None:
            return None

        if not lst:
            break

        for prod in lst:
            p = prod.get("price")
            if p is None:
                continue
            try:
                p = float(p)
            except Exception:
                continue

            if lowest is None or p < lowest:
                lowest = p

        if not has_more:
            break

        time.sleep(MARKET_REQUEST_DELAY)

    return lowest


def _item_key(it):
    """
    统计名字段。
    优先 name，其次 marketHashName。
    """
    return (
        it.get("name")
        or it.get("marketHashName")
        or "UNKNOWN"
    ).strip()


def _item_market_hash_name(it):
    return (
        it.get("marketHashName")
        or it.get("name")
        or ""
    ).strip()


def _item_item_id(it):
    """
    尽量从库存对象里提取 itemId。
    不确定你这边库存接口实际字段名，所以多兼容几个常见写法。
    """
    for k in ("itemId", "item_id", "goodsId", "goods_id"):
        v = it.get(k)
        if v:
            return v
    return None


def build_price_query_key(item_id=None, market_hash_name=None):
    """
    构建价格缓存key
    """
    if item_id:
        return f"itemId:{item_id}"
    return f"hash:{market_hash_name}"


def analyze_inventory(data):
    """
    返回：
      total
      tradable_cnt
      untradable_cnt
      tradable_counter: Counter(name->count)
      untradable_counter: Counter(name->count)
      item_query_map: dict[name] = {"itemId":..., "marketHashName":...}
    """
    total = data.get("total", 0)
    items = data.get("list", []) or []

    tradable_counter = Counter()
    untradable_counter = Counter()

    tradable_cnt = 0
    untradable_cnt = 0

    item_query_map = {}

    for it in items:
        name = _item_key(it)
        st = it.get("status")

        item_id = _item_item_id(it)
        market_hash_name = _item_market_hash_name(it)

        if name not in item_query_map:
            item_query_map[name] = {
                "itemId": item_id,
                "marketHashName": market_hash_name,
            }
        else:
            # 如果之前没有 itemId，这次有，就补上
            if not item_query_map[name].get("itemId") and item_id:
                item_query_map[name]["itemId"] = item_id
            if not item_query_map[name].get("marketHashName") and market_hash_name:
                item_query_map[name]["marketHashName"] = market_hash_name

        if st == TRADE_OK_STATUS:
            tradable_cnt += 1
            tradable_counter[name] += 1
        elif st == TRADE_NO_STATUS:
            untradable_cnt += 1
            untradable_counter[name] += 1
        else:
            # 其他状态暂不统计
            pass

    return total, tradable_cnt, untradable_cnt, tradable_counter, untradable_counter, item_query_map


def estimate_inventory_value_for_result(result, price_cache):
    """
    对单个 SteamID 的分析结果做价值估算。
    会把估算结果写回 result 中：

    result["price_map"][name] = unit_price
    result["tradable_value"]
    result["untradable_value"]
    result["total_value"]
    """
    t_counter = result["tradable_counter"]
    u_counter = result["untradable_counter"]
    item_query_map = result.get("item_query_map", {}) or {}

    price_map = {}
    tradable_value = 0.0
    untradable_value = 0.0

    all_names = set(t_counter.keys()) | set(u_counter.keys())

    for name in sorted(all_names):
        meta = item_query_map.get(name, {}) or {}
        item_id = meta.get("itemId")
        market_hash_name = meta.get("marketHashName") or name

        cache_key = build_price_query_key(item_id=item_id, market_hash_name=market_hash_name)

        if cache_key in price_cache:
            unit_price = price_cache[cache_key]
        else:
            unit_price = fetch_market_lowest_price(
                item_id=item_id,
                market_hash_name=None if item_id else market_hash_name,
                app_id=APP_ID,
                max_scan_pages=MARKET_MAX_SCAN_PAGES
            )

            if unit_price is None:
                unit_price = PRICE_NOT_FOUND_VALUE

            price_cache[cache_key] = unit_price
            time.sleep(MARKET_REQUEST_DELAY)

        price_map[name] = unit_price

        tc = t_counter.get(name, 0)
        uc = u_counter.get(name, 0)

        tradable_value += tc * unit_price
        untradable_value += uc * unit_price

    result["price_map"] = price_map
    result["tradable_value"] = tradable_value
    result["untradable_value"] = untradable_value
    result["total_value"] = tradable_value + untradable_value


def write_summary_txt(path, global_tradable, global_untradable, per_sid_totals):
    """
    global_tradable/global_untradable: Counter(name->count) 跨所有 SteamID 汇总
    per_sid_totals: list of (steam_id, total, t_cnt, u_cnt, t_value, u_value, total_value)
    """
    t_total = sum(global_tradable.values())
    u_total = sum(global_untradable.values())

    grand_t_value = sum(x[4] for x in per_sid_totals)
    grand_u_value = sum(x[5] for x in per_sid_totals)
    grand_total_value = sum(x[6] for x in per_sid_totals)

    def _sorted_items(counter):
        return sorted(counter.items(), key=lambda x: (x[1], x[0]), reverse=True)

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"APP_ID={APP_ID}\n")
        f.write(f"统计SteamID数量: {len(per_sid_totals)}\n")
        f.write("-" * 100 + "\n")

        f.write("【每个SteamID总览】\n")
        for steam_id, total, t_cnt, u_cnt, t_value, u_value, total_value in per_sid_totals:
            f.write(
                f"{steam_id} | total={total} | "
                f"tradable(status0)={t_cnt} | "
                f"untradable(status4)={u_cnt} | "
                f"tradable_value={money(t_value)} | "
                f"untradable_value={money(u_value)} | "
                f"total_value={money(total_value)}\n"
            )
        f.write("-" * 100 + "\n")

        f.write(f"【可交易汇总 status==0】总数: {t_total}\n")
        for name, cnt in _sorted_items(global_tradable):
            f.write(f"{cnt:>6}  {name}\n")
        f.write("-" * 100 + "\n")

        f.write(f"【不可交易汇总 status==4】总数: {u_total}\n")
        for name, cnt in _sorted_items(global_untradable):
            f.write(f"{cnt:>6}  {name}\n")
        f.write("-" * 100 + "\n")

        f.write("【价值汇总】\n")
        f.write(f"可交易总价值: {money(grand_t_value)}\n")
        f.write(f"不可交易总价值: {money(grand_u_value)}\n")
        f.write(f"全部总价值: {money(grand_total_value)}\n")

    print("\n✅ 已保存汇总TXT到:", path)


def save_simple_list_txt(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for x in rows:
            f.write(f"{x}\n")


def process_single_steam_id(steam_id):
    """
    处理单个 SteamID

    返回:
      success(bool)
      result(dict|None)

    说明:
      - 接口正常返回库存数据 -> success=True
      - 接口返回“库存为空” -> success=True，且 total=0
      - 网络/HTTP/其他业务错误 -> success=False
    """
    data = fetch_inventory(steam_id)

    if data is None:
        return False, None

    total, t_cnt, u_cnt, t_counter, u_counter, item_query_map = analyze_inventory(data)

    result = {
        "steam_id": steam_id,
        "total": total,
        "tradable_cnt": t_cnt,
        "untradable_cnt": u_cnt,
        "tradable_counter": t_counter,
        "untradable_counter": u_counter,
        "item_query_map": item_query_map,
        "price_map": {},
        "tradable_value": 0.0,
        "untradable_value": 0.0,
        "total_value": 0.0,
    }
    return True, result


def main():
    steam_ids = load_steam_ids(STEAM_IDS_FILE)

    if not steam_ids:
        print("未读取到SteamID")
        return

    print(f"共读取 {len(steam_ids)} 个SteamID\n")

    # 待处理队列：第一次先全量跑
    pending_ids = list(steam_ids)

    # 最终成功的结果，避免重复处理
    success_results = {}

    round_no = 1

    while pending_ids:
        if MAX_GLOBAL_ROUNDS is not None and round_no > MAX_GLOBAL_ROUNDS:
            print(f"\n⚠ 已达到最大全局轮次限制: {MAX_GLOBAL_ROUNDS}")
            break

        print("\n" + "#" * 80)
        print(f"开始第 {round_no} 轮处理，本轮待处理 SteamID 数量: {len(pending_ids)}")
        print("#" * 80 + "\n")

        next_round_pending = []

        for idx, steam_id in enumerate(pending_ids, 1):
            print("=" * 70)
            print(f"[第{round_no}轮 {idx}/{len(pending_ids)}] 查询 SteamID: {steam_id}")

            ok, result = process_single_steam_id(steam_id)

            if not ok:
                print("❌ 本轮仍失败，加入下一轮重试列表")
                next_round_pending.append(steam_id)
                continue

            success_results[steam_id] = result

            print(
                f"✅ 查询成功 | total={result['total']} | "
                f"tradable={result['tradable_cnt']} | "
                f"untradable={result['untradable_cnt']}"
            )

            if idx < len(pending_ids):
                print(f"\n等待 {DELAY_SECONDS} 秒...")
                time.sleep(DELAY_SECONDS)

        pending_ids = next_round_pending

        if pending_ids:
            print(f"\n⚠ 第 {round_no} 轮结束，仍有 {len(pending_ids)} 个 SteamID 失败")
            print("失败列表：", pending_ids)
            print(f"{ROUND_FAIL_DELAY} 秒后开始下一轮...\n")
            time.sleep(ROUND_FAIL_DELAY)
        else:
            print(f"\n🎉 第 {round_no} 轮结束，全部 SteamID 已成功获取")

        round_no += 1

    # ===================== 价值估算 =====================
    price_cache = {}

    if ENABLE_VALUE_ESTIMATION:
        print("\n" + "#" * 80)
        print("开始估算库存价值（按当前市场最低在售价）")
        print("#" * 80 + "\n")

        success_ids_in_order = [sid for sid in steam_ids if sid in success_results]

        for i, steam_id in enumerate(success_ids_in_order, 1):
            result = success_results[steam_id]
            print(f"[价值估算 {i}/{len(success_ids_in_order)}] SteamID: {steam_id}")

            if result["total"] <= 0:
                print("  - 库存为空，跳过")
                continue

            estimate_inventory_value_for_result(result, price_cache)

            print(
                f"  - tradable_value={money(result['tradable_value'])} | "
                f"untradable_value={money(result['untradable_value'])} | "
                f"total_value={money(result['total_value'])}"
            )

    # ===================== 结果汇总输出 =====================
    no_inventory_ids = []

    global_tradable = Counter()
    global_untradable = Counter()
    per_sid_totals = []

    with open(OUTPUT_CSV, "w", newline="", encoding="utf-8-sig") as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow([
            "steamId",
            "total_inventory",
            "tradable_total(status0)",
            "untradable_total(status4)",
            "name",
            "tradable_count(status0)",
            "untradable_count(status4)",
            "sum_count(status0+status4)",
            "market_unit_price",
            "tradable_value",
            "untradable_value",
            "sum_value"
        ])

        for steam_id in steam_ids:
            result = success_results.get(steam_id)
            if not result:
                continue

            total = result["total"]
            t_cnt = result["tradable_cnt"]
            u_cnt = result["untradable_cnt"]
            t_counter = result["tradable_counter"]
            u_counter = result["untradable_counter"]
            price_map = result.get("price_map", {})
            t_value = result.get("tradable_value", 0.0)
            u_value = result.get("untradable_value", 0.0)
            total_value = result.get("total_value", 0.0)

            per_sid_totals.append((steam_id, total, t_cnt, u_cnt, t_value, u_value, total_value))

            if total == 0 or (not t_counter and not u_counter):
                no_inventory_ids.append(steam_id)
                continue

            global_tradable.update(t_counter)
            global_untradable.update(u_counter)

            all_names = set(t_counter.keys()) | set(u_counter.keys())
            sorted_rows = sorted(
                (
                    (name, t_counter.get(name, 0), u_counter.get(name, 0))
                    for name in all_names
                ),
                key=lambda x: (x[1] + x[2], x[1], x[2], x[0]),
                reverse=True
            )

            for name, tc, uc in sorted_rows:
                unit_price = float(price_map.get(name, PRICE_NOT_FOUND_VALUE) or 0.0)
                row_t_value = tc * unit_price
                row_u_value = uc * unit_price
                row_sum_value = row_t_value + row_u_value

                writer.writerow([
                    steam_id,
                    total,
                    t_cnt,
                    u_cnt,
                    name,
                    tc,
                    uc,
                    tc + uc,
                    money(unit_price),
                    money(row_t_value),
                    money(row_u_value),
                    money(row_sum_value)
                ])

    # 无库存列表
    if no_inventory_ids:
        save_simple_list_txt(NO_INV_TXT, no_inventory_ids)
        print(f"\n⚠ 无库存SteamID数量: {len(no_inventory_ids)}")
        print("已保存到:", NO_INV_TXT)
    else:
        print("\n🎉 没有发现无库存SteamID")

    # 最终仍失败的列表
    final_failed_ids = [sid for sid in steam_ids if sid not in success_results]
    if final_failed_ids:
        save_simple_list_txt(FAILED_TXT, final_failed_ids)
        print(f"\n❌ 最终仍失败SteamID数量: {len(final_failed_ids)}")
        print("已保存到:", FAILED_TXT)
    else:
        # 保证旧文件不误导
        save_simple_list_txt(FAILED_TXT, [])
        print("\n✅ 所有SteamID最终都请求成功")

    write_summary_txt(SUMMARY_TXT, global_tradable, global_untradable, per_sid_totals)
    print("\n✅ 已保存库存统计到", OUTPUT_CSV)


if __name__ == "__main__":
    main()