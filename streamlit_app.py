"""MoneyPrintToolbox desktop inventory manager.

This is a native desktop UI (Tkinter), not a web page.
It supports:
- 全库存（多账号汇总）
- 单账号库存
- 分组：在售中 / 可交易 / 不可交易
- 批量上架（C5 OpenAPI: /merchant/sale/v2/create）
"""

from __future__ import annotations

import json
import threading
import tkinter as tk
from dataclasses import dataclass
from tkinter import messagebox, ttk
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib import error, parse, request


API_BASE = "https://openapi.c5game.com"

# 基于文档示例与实测习惯推断：status=1 常见为在售。
# 如果平台后续变更，可在 UI 中看到 status 原值并快速调整。
ON_SALE_STATUS = {1}


@dataclass
class InventoryItem:
    steam_id: str
    app_id: int
    asset_id: str
    name: str
    market_hash_name: str
    status: int
    if_tradable: bool
    price: float
    token: str
    style_token: str

    @property
    def group(self) -> str:
        if self.status in ON_SALE_STATUS:
            return "在售中"
        if self.if_tradable:
            return "可交易"
        return "不可交易"


class C5Client:
    def __init__(self, app_key: str):
        self.app_key = app_key.strip()

    def _http_json(self, method: str, path: str, *, query: Optional[Dict[str, Any]] = None, body: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        if not self.app_key:
            raise ValueError("请先输入 app-key")

        q = dict(query or {})
        q["app-key"] = self.app_key
        url = f"{API_BASE}{path}?{parse.urlencode(q)}"

        data = None
        headers = {"Content-Type": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")

        req = request.Request(url=url, method=method.upper(), headers=headers, data=data)
        try:
            with request.urlopen(req, timeout=25) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except error.HTTPError as e:
            payload = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"HTTP {e.code}: {payload}") from e
        except error.URLError as e:
            raise RuntimeError(f"网络请求失败: {e.reason}") from e

    def fetch_inventory(self, steam_id: str, app_id: int, *, language: str = "zh", count: int = 200) -> List[InventoryItem]:
        last_asset_id = "0"
        rows: List[InventoryItem] = []

        while True:
            resp = self._http_json(
                "GET",
                f"/merchant/inventory/v2/{steam_id}/{app_id}",
                query={"language": language, "startAssetId": last_asset_id, "count": count},
            )
            if not resp.get("success"):
                raise RuntimeError(f"拉取库存失败: {resp}")

            data = resp.get("data") or {}
            items = data.get("list") or []
            for item in items:
                rows.append(
                    InventoryItem(
                        steam_id=str(item.get("steamId", "")),
                        app_id=int(item.get("appId", app_id) or app_id),
                        asset_id=str(item.get("assetId", "")),
                        name=str(item.get("name", "")),
                        market_hash_name=str(item.get("marketHashName", "")),
                        status=int(item.get("status", 0) or 0),
                        if_tradable=bool(item.get("ifTradable", False)),
                        price=float(item.get("price", 0) or 0),
                        token=str(item.get("token", "")),
                        style_token=str(item.get("styleToken", "")),
                    )
                )

            last = data.get("lastAssetId")
            if not last or not items:
                break
            last_asset_id = str(last)

        return rows

    def create_sale(self, items: Iterable[Tuple[InventoryItem, float]], accept_bargain: int = 0, description: str = "") -> Dict[str, Any]:
        data_list = []
        for item, price in items:
            data_list.append(
                {
                    "price": float(price),
                    "description": description,
                    "acceptBargain": int(accept_bargain),
                    "token": item.token,
                    "styleToken": item.style_token,
                }
            )

        return self._http_json("POST", "/merchant/sale/v2/create", body={"dataList": data_list})


class InventoryView(ttk.Frame):
    columns = ("steam_id", "asset_id", "name", "group", "status", "tradable", "price")

    def __init__(self, master: tk.Widget, title: str):
        super().__init__(master)
        self.title = title
        self.items: List[InventoryItem] = []
        self.filtered: List[InventoryItem] = []

        title_lbl = ttk.Label(self, text=title, font=("Microsoft YaHei UI", 14, "bold"))
        title_lbl.pack(anchor="w", padx=8, pady=(8, 4))

        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=4)

        self.group_var = tk.StringVar(value="全部")
        ttk.Label(top, text="分组:").pack(side="left")
        group_cb = ttk.Combobox(top, textvariable=self.group_var, values=["全部", "在售中", "可交易", "不可交易"], width=12, state="readonly")
        group_cb.pack(side="left", padx=(4, 12))
        group_cb.bind("<<ComboboxSelected>>", lambda _e: self.refresh_table())

        self.keyword_var = tk.StringVar()
        ttk.Label(top, text="关键词:").pack(side="left")
        keyword_entry = ttk.Entry(top, textvariable=self.keyword_var, width=30)
        keyword_entry.pack(side="left", padx=4)
        keyword_entry.bind("<KeyRelease>", lambda _e: self.refresh_table())

        self.stats_var = tk.StringVar(value="总计: 0")
        ttk.Label(top, textvariable=self.stats_var).pack(side="right")

        table_wrap = ttk.Frame(self)
        table_wrap.pack(fill="both", expand=True, padx=8, pady=4)

        self.tree = ttk.Treeview(table_wrap, columns=self.columns, show="headings", selectmode="extended")
        labels = {
            "steam_id": "SteamID",
            "asset_id": "AssetID",
            "name": "饰品名",
            "group": "分组",
            "status": "Status",
            "tradable": "可交易",
            "price": "参考价",
        }
        widths = {"steam_id": 140, "asset_id": 120, "name": 300, "group": 90, "status": 70, "tradable": 70, "price": 80}
        for col in self.columns:
            self.tree.heading(col, text=labels[col])
            self.tree.column(col, width=widths[col], anchor="w")

        ybar = ttk.Scrollbar(table_wrap, orient="vertical", command=self.tree.yview)
        xbar = ttk.Scrollbar(table_wrap, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=ybar.set, xscrollcommand=xbar.set)

        self.tree.grid(row=0, column=0, sticky="nsew")
        ybar.grid(row=0, column=1, sticky="ns")
        xbar.grid(row=1, column=0, sticky="ew")
        table_wrap.rowconfigure(0, weight=1)
        table_wrap.columnconfigure(0, weight=1)

    def set_items(self, items: List[InventoryItem]) -> None:
        self.items = items
        self.refresh_table()

    def refresh_table(self) -> None:
        group = self.group_var.get()
        keyword = self.keyword_var.get().strip().lower()

        for iid in self.tree.get_children():
            self.tree.delete(iid)

        self.filtered = []
        for item in self.items:
            if group != "全部" and item.group != group:
                continue
            searchable = f"{item.name} {item.market_hash_name} {item.asset_id}"
            if keyword and keyword not in searchable.lower():
                continue
            self.filtered.append(item)
            self.tree.insert(
                "",
                "end",
                values=(
                    item.steam_id,
                    item.asset_id,
                    item.name,
                    item.group,
                    item.status,
                    "是" if item.if_tradable else "否",
                    f"{item.price:.2f}",
                ),
            )

        grp_count = {"在售中": 0, "可交易": 0, "不可交易": 0}
        for x in self.filtered:
            grp_count[x.group] += 1
        self.stats_var.set(
            f"总计: {len(self.filtered)} | 在售中: {grp_count['在售中']} | 可交易: {grp_count['可交易']} | 不可交易: {grp_count['不可交易']}"
        )

    def selected_items(self) -> List[InventoryItem]:
        sel = self.tree.selection()
        items: List[InventoryItem] = []
        for iid in sel:
            vals = self.tree.item(iid, "values")
            if len(vals) < 2:
                continue
            steam_id, asset_id = vals[0], vals[1]
            for it in self.filtered:
                if it.steam_id == steam_id and it.asset_id == asset_id:
                    items.append(it)
                    break
        return items


class DesktopApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("MoneyPrintToolbox Desktop")
        self.geometry("1180x760")
        self.minsize(980, 620)

        self.client: Optional[C5Client] = None

        self._build_top_bar()
        self._build_tabs()

    def _build_top_bar(self) -> None:
        frame = ttk.Frame(self, padding=8)
        frame.pack(fill="x")

        ttk.Label(frame, text="app-key").grid(row=0, column=0, sticky="w")
        self.app_key_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.app_key_var, width=48, show="*").grid(row=0, column=1, padx=4)

        ttk.Label(frame, text="appId").grid(row=0, column=2, padx=(16, 0))
        self.app_id_var = tk.StringVar(value="730")
        ttk.Entry(frame, textvariable=self.app_id_var, width=10).grid(row=0, column=3, padx=4)

        ttk.Label(frame, text="SteamIDs(逗号分隔)").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.steam_ids_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.steam_ids_var, width=86).grid(row=1, column=1, columnspan=3, sticky="we", padx=4, pady=(6, 0))

        btn_frame = ttk.Frame(frame)
        btn_frame.grid(row=0, column=4, rowspan=2, padx=(16, 0))
        ttk.Button(btn_frame, text="刷新单账号", command=self.load_single_inventory).pack(fill="x")
        ttk.Button(btn_frame, text="刷新全库存", command=self.load_all_inventory).pack(fill="x", pady=6)

        frame.columnconfigure(1, weight=1)

    def _build_tabs(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True)

        self.all_view = InventoryView(notebook, "所有库存")
        self.single_view = InventoryView(notebook, "单账号库存")

        notebook.add(self.all_view, text="所有库存")
        notebook.add(self.single_view, text="单账号库存")

        self._add_sale_panel(self.all_view)
        self._add_sale_panel(self.single_view)

    def _add_sale_panel(self, view: InventoryView) -> None:
        bar = ttk.Frame(view)
        bar.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Label(bar, text="上架价格").pack(side="left")
        price_var = tk.StringVar(value="10.0")
        ttk.Entry(bar, textvariable=price_var, width=10).pack(side="left", padx=4)

        bargain_var = tk.IntVar(value=0)
        ttk.Checkbutton(bar, text="接受议价", variable=bargain_var).pack(side="left", padx=8)

        ttk.Button(
            bar,
            text="上架选中饰品",
            command=lambda: self.list_selected_items(view, price_var, bargain_var),
        ).pack(side="left", padx=8)

    def _build_client(self) -> C5Client:
        app_key = self.app_key_var.get().strip()
        if not app_key:
            raise ValueError("请先输入 app-key")
        self.client = C5Client(app_key)
        return self.client

    def _parse_ids(self) -> List[str]:
        ids = [x.strip() for x in self.steam_ids_var.get().split(",") if x.strip()]
        if not ids:
            raise ValueError("请至少输入一个 SteamID")
        return ids

    def _parse_app_id(self) -> int:
        try:
            return int(self.app_id_var.get().strip())
        except Exception as e:
            raise ValueError("appId 必须是整数") from e

    def load_single_inventory(self) -> None:
        def job() -> None:
            try:
                client = self._build_client()
                app_id = self._parse_app_id()
                steam_ids = self._parse_ids()
                items = client.fetch_inventory(steam_ids[0], app_id)
                self.after(0, lambda: self.single_view.set_items(items))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("刷新失败", str(e)))

        threading.Thread(target=job, daemon=True).start()

    def load_all_inventory(self) -> None:
        def job() -> None:
            try:
                client = self._build_client()
                app_id = self._parse_app_id()
                steam_ids = self._parse_ids()

                all_items: List[InventoryItem] = []
                for sid in steam_ids:
                    all_items.extend(client.fetch_inventory(sid, app_id))
                self.after(0, lambda: self.all_view.set_items(all_items))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("刷新失败", str(e)))

        threading.Thread(target=job, daemon=True).start()

    def list_selected_items(self, view: InventoryView, price_var: tk.StringVar, bargain_var: tk.IntVar) -> None:
        selected = view.selected_items()
        if not selected:
            messagebox.showwarning("未选择", "请先在列表里选择要上架的饰品")
            return

        try:
            price = float(price_var.get().strip())
            if price <= 0:
                raise ValueError
        except ValueError:
            messagebox.showerror("价格错误", "上架价格必须是正数")
            return

        def job() -> None:
            try:
                client = self._build_client()
                payload = [(item, price) for item in selected]
                resp = client.create_sale(payload, accept_bargain=bargain_var.get())
                ok = bool(resp.get("success"))
                if ok:
                    self.after(0, lambda: messagebox.showinfo("上架成功", f"请求成功，返回: {json.dumps(resp, ensure_ascii=False)[:500]}"))
                else:
                    self.after(0, lambda: messagebox.showerror("上架失败", json.dumps(resp, ensure_ascii=False)))
            except Exception as e:
                self.after(0, lambda: messagebox.showerror("上架失败", str(e)))

        threading.Thread(target=job, daemon=True).start()


def apply_theme(root: tk.Tk) -> None:
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    bg = "#111827"
    fg = "#e5e7eb"
    panel = "#1f2937"
    style.configure(".", background=bg, foreground=fg, fieldbackground=panel)
    style.configure("TFrame", background=bg)
    style.configure("TLabel", background=bg, foreground=fg)
    style.configure("TButton", padding=6)
    style.configure("TCheckbutton", background=bg, foreground=fg)
    style.configure("Treeview", background="#0b1220", foreground=fg, fieldbackground="#0b1220")
    style.configure("Treeview.Heading", background="#374151", foreground="#ffffff")


def main() -> None:
    app = DesktopApp()
    apply_theme(app)
    app.mainloop()


if __name__ == "__main__":
    main()
