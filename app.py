# -*- coding: utf-8 -*-
"""
盤中大單進出監控 - 純富邦 WebSocket 版

重點：
1. 富邦 WebSocket 負責盤中大單、單筆量、內外盤。
2. yfinance 只用來抓「昨日收盤價」，一小時更新一次，並存入本地 JSON 歷史快取。
3. 表格顯示：代碼 Yahoo 連結｜股票名稱｜大單追蹤｜即時價｜漲幅%｜最新單筆｜內外盤｜外盤累積｜內盤累積。
4. 修正「最新單筆」：富邦 trades 回傳的 volume 常是盤中累積量，本版會用「本次累積量 - 上次累積量」換算單筆增量。
"""

import os
import re
import json
import copy
import time
import base64
import tempfile
import threading
import requests
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st

try:
    from fubon_neo.sdk import FubonSDK
except Exception:
    FubonSDK = None

try:
    import yfinance as yf
except Exception:
    yf = None

st.set_page_config(page_title="盤中大單進出監控", layout="wide")

TW_TZ = ZoneInfo("Asia/Taipei")
GROUP_EDIT_PIN = "1219"
GROUPS_FILE = "stock_groups.json"
BACKUP_DIR = "backups"
STOCK_NAME_FILE = "TWstocklistname.txt"
APP_LOGO = "jerry.jpg"


def get_secret_or_default(key: str, default: str = ""):
    """安全讀取 Streamlit Secrets；未設定時回傳預設值。"""
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


# ===== Telegram 設定 =====
TELEGRAM_BOT_TOKEN = get_secret_or_default("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = get_secret_or_default("TELEGRAM_CHAT_ID", "")

YF_CLOSE_CACHE_FILE = "yf_yesterday_close_cache.json"
YF_CLOSE_CACHE_TTL_SEC = 3600

DEFAULT_STOCK_GROUPS = {
    "權值股": [
        "2330.TW", "00981A.TW", "2449.TW", "2317.TW", "3711.TW",
        "6488.TWO", "2327.TW", "6176.TW", "2303.TW", "5347.TWO",
    ],
    "自選股1": [
        "3008.TW", "3035.TW", "4566.TW", "4956.TW", "6456.TW",
        "4749.TWO", "6271.TW", "6290.TWO", "4919.TW",
    ],
}

st.markdown(
    """
    <style>
    html { scroll-behavior: smooth; }
    .dashboard-link, .dashboard-link:link, .dashboard-link:visited, .dashboard-link:hover, .dashboard-link:active { text-decoration:none !important; color:inherit !important; display:block; }
    .dash-card { cursor:pointer; transition:transform .12s ease, box-shadow .12s ease; }
    .dash-card:hover { transform:translateY(-2px); box-shadow:0 4px 10px rgba(0,0,0,.12); }
    .dashboard-grid {display:grid; grid-template-columns:repeat(4,minmax(240px,1fr)); gap:12px; margin:10px 0 18px 0;}
    .dash-card {border:1px solid #91d5ff; border-radius:12px; padding:14px 16px; min-height:190px; background:#f0f9ff; box-shadow:0 1px 2px rgba(0,0,0,.04);}
    /* 儀表板顏色依「漲幅達標比例」分級：0%、1~39%、40~69%、70%以上 */
    .dash-card.pct-zero {border-color:#d9d9d9; background:#fafafa;}
    .dash-card.pct-low {border-color:#91d5ff; background:#f0f9ff;}
    .dash-card.pct-mid {border-color:#ffd666; background:#fffbe6;}
    .dash-card.pct-high {border-color:#ff7875; background:#fff1f0; box-shadow:0 1px 8px rgba(207,19,34,.18);}
    .dash-card.hot {border-color:#ff9c6e; background:#fff7e6;}
    .dash-card.strong {border-color:#95de64; background:#f6ffed;}
    .dash-title {font-weight:800; font-size:18px; margin-bottom:8px; color:#111827;}
    .dash-big {font-size:28px; font-weight:900; margin:4px 0 10px 0;}
    .dash-line {font-size:14px; line-height:1.65; color:#111827;}
    .dash-small {font-size:12px; color:#4b5563; margin-top:8px; border-top:1px solid rgba(0,0,0,.08); padding-top:8px;}
    .up-text {color:#cf1322; font-weight:700;} .down-text {color:#389e0d; font-weight:700;} .flat-text {color:#6b7280; font-weight:700;}
    @media (max-width:1200px){.dashboard-grid{grid-template-columns:repeat(2,minmax(240px,1fr));}}
    @media (max-width:700px){.dashboard-grid{grid-template-columns:1fr;}}
    </style>
    """,
    unsafe_allow_html=True,
)

# =============================================================================
# 基礎工具
# =============================================================================
def symbol_to_code(symbol: str) -> str:
    return str(symbol).strip().upper().split(".")[0]


def yahoo_quote_url(symbol: str) -> str:
    """產生 Yahoo 台股個股頁連結。"""
    code = symbol_to_code(symbol)
    return f"https://tw.stock.yahoo.com/quote/{code}"


def make_anchor_id(group_name: str) -> str:
    """將分類名稱轉成穩定的 HTML 錨點 ID，讓儀表板卡片可跳到對應表格。"""
    anchor = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", str(group_name)).strip("-")
    return f"group-{anchor or 'default'}"


def format_price_value(value):
    """格式化富邦 WebSocket 即時價。"""
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}"
    except Exception:
        return "-"


def format_pct_value(value):
    if value is None:
        return "-"
    try:
        pct = float(value)
    except Exception:
        return "-"
    if pct > 0:
        return f"🔴 +{pct:.2f}%"
    if pct < 0:
        return f"🟢 {pct:.2f}%"
    return "⚪ 0.00%"


def pct_class(value):
    if value is None:
        return "flat-text"
    try:
        pct = float(value)
    except Exception:
        return "flat-text"
    if pct > 0:
        return "up-text"
    if pct < 0:
        return "down-text"
    return "flat-text"


def escape_html(text_value):
    return str(text_value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_yfinance_candidates(symbol: str):
    raw = str(symbol).strip().upper()
    code = symbol_to_code(raw)
    candidates = []
    if raw and "." in raw:
        candidates.append(raw)
    elif raw:
        normalized = normalize_symbol_quick(raw)
        if normalized:
            candidates.append(normalized)
    if code:
        candidates.extend([f"{code}.TW", f"{code}.TWO"])
    result, seen = [], set()
    for item in candidates:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def load_yf_close_cache():
    if not os.path.exists(YF_CLOSE_CACHE_FILE):
        return {}
    try:
        with open(YF_CLOSE_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_yf_close_cache(cache: dict):
    try:
        with open(YF_CLOSE_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_yfinance_yesterday_close(symbol: str):
    """取得昨日/前一交易日收盤價；一小時內讀本地 JSON 快取，超過才重新抓 yfinance。"""
    code = symbol_to_code(symbol)
    now_ts = time.time()
    cache = load_yf_close_cache()
    cached = cache.get(code)

    if isinstance(cached, dict):
        fetched_at = float(cached.get("fetched_at", 0) or 0)
        close = cached.get("close")
        if close is not None and now_ts - fetched_at < YF_CLOSE_CACHE_TTL_SEC:
            return float(close), cached.get("date", ""), "cache"

    if yf is None:
        if isinstance(cached, dict) and cached.get("close") is not None:
            return float(cached["close"]), cached.get("date", ""), "stale cache"
        return None, "", "yfinance unavailable"

    last_error = ""
    today = datetime.now(TW_TZ).date()
    for yf_symbol in build_yfinance_candidates(symbol):
        try:
            df = yf.download(yf_symbol, period="10d", interval="1d", auto_adjust=False, progress=False, threads=False)
            if df is None or df.empty:
                last_error = f"{yf_symbol}: no data"
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
            df = df.reset_index()
            date_col = "Date" if "Date" in df.columns else "Datetime" if "Datetime" in df.columns else df.columns[0]
            df[date_col] = pd.to_datetime(df[date_col], errors="coerce")
            df = df.dropna(subset=[date_col])
            df = df[df[date_col].dt.date < today].sort_values(date_col)
            if df.empty or "Close" not in df.columns:
                last_error = f"{yf_symbol}: no previous close"
                continue
            close = pd.to_numeric(df["Close"], errors="coerce").dropna()
            if close.empty:
                last_error = f"{yf_symbol}: close empty"
                continue
            close_value = float(close.iloc[-1])
            close_date = pd.to_datetime(df.loc[close.index[-1], date_col]).date().isoformat()
            cache[code] = {"symbol": yf_symbol, "close": close_value, "date": close_date, "fetched_at": now_ts, "fetched_at_text": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S")}
            save_yf_close_cache(cache)
            return close_value, close_date, "yfinance"
        except Exception as e:
            last_error = f"{yf_symbol}: {e}"
            continue

    if isinstance(cached, dict) and cached.get("close") is not None:
        return float(cached["close"]), cached.get("date", ""), "stale cache"
    return None, "", last_error or "no data"


def normalize_symbol_quick(input_text: str):
    s = str(input_text).strip().upper()
    if not s:
        return None
    if "." in s:
        return s
    if s.isdigit():
        if s.startswith(("3", "6", "8")):
            return f"{s}.TWO"
        return f"{s}.TW"
    return s


def normalize_symbols_from_text(text: str):
    if not text:
        return []
    text = text.replace("，", ",")
    items = []
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        items.extend([p.strip().upper() for p in raw_line.split(",") if p.strip()])

    result, seen = [], set()
    for item in items:
        symbol = normalize_symbol_quick(item)
        if symbol and symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return result


# =============================================================================
# 股票名稱 / 查詢：只讀本地 TWstocklistname.txt
# =============================================================================
@st.cache_data(ttl=86400)
def load_stock_lookup_maps(file_path: str = STOCK_NAME_FILE) -> dict:
    code_to_name = {}
    code_to_symbol = {}
    name_to_symbol = {}

    if not os.path.exists(file_path):
        return {"code_to_name": code_to_name, "code_to_symbol": code_to_symbol, "name_to_symbol": name_to_symbol}

    with open(file_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip().replace("\ufeff", "").replace("\u3000", " ")
            if not line:
                continue
            if "\t" in line:
                parts = [p.strip() for p in line.split("\t") if p.strip()]
            else:
                m = re.match(r"^([^\s]+)\s+(.+)$", line)
                parts = [m.group(1).strip(), m.group(2).strip()] if m else []
            if len(parts) < 2:
                continue

            raw_symbol = parts[0].upper()
            stock_name = parts[1].strip()
            symbol = normalize_symbol_quick(raw_symbol)
            code = symbol_to_code(symbol)
            if not code or not stock_name:
                continue
            code_to_name[code] = stock_name
            code_to_symbol[code] = symbol
            name_to_symbol[stock_name] = symbol
            name_to_symbol[stock_name.replace(" ", "")] = symbol

    return {"code_to_name": code_to_name, "code_to_symbol": code_to_symbol, "name_to_symbol": name_to_symbol}


@st.cache_data(ttl=86400)
def get_stock_name(symbol: str) -> str:
    lookup = load_stock_lookup_maps(STOCK_NAME_FILE)
    code = symbol_to_code(symbol)
    return lookup.get("code_to_name", {}).get(code, code)


def resolve_stock_query(input_text: str):
    q_raw = str(input_text).strip()
    if not q_raw:
        return None, None, None
    lookup = load_stock_lookup_maps(STOCK_NAME_FILE)
    code_to_name = lookup.get("code_to_name", {})
    code_to_symbol = lookup.get("code_to_symbol", {})
    name_to_symbol = lookup.get("name_to_symbol", {})

    q_upper = q_raw.upper()
    if "." in q_upper:
        code = symbol_to_code(q_upper)
        return q_upper, code_to_name.get(code, code), "ticker"
    if q_upper.isdigit():
        symbol = code_to_symbol.get(q_upper) or normalize_symbol_quick(q_upper)
        return symbol, code_to_name.get(q_upper, q_upper), "code"

    symbol = name_to_symbol.get(q_raw) or name_to_symbol.get(q_raw.replace(" ", ""))
    if symbol:
        code = symbol_to_code(symbol)
        return symbol, code_to_name.get(code, q_raw), "name"

    compact = q_raw.replace(" ", "")
    for stock_name, candidate_symbol in name_to_symbol.items():
        if compact and compact in stock_name.replace(" ", ""):
            code = symbol_to_code(candidate_symbol)
            return candidate_symbol, code_to_name.get(code, stock_name), "name_partial"

    symbol = normalize_symbol_quick(q_raw)
    if symbol:
        code = symbol_to_code(symbol)
        return symbol, code_to_name.get(code, code), "fallback"
    return None, None, None


# =============================================================================
# 富邦 WebSocket Manager
# =============================================================================
class FubonRealtimeManager:
    def __init__(self):
        self.sdk = None
        self.ws = None
        self.lock = threading.RLock()
        self.logged_in = False
        self.connected = False
        self.error = None
        self.messages = {}
        self.subscribed = set()
        self.last_message_at = None
        self.cert_path = None
        self.tick_status = {}

    def reset_runtime_data(self):
        with self.lock:
            self.messages = {}
            self.tick_status = {}
            self.last_message_at = None

    def login(self, fubon_id: str, fubon_password: str, cert_password: str, pfx_base64: str):
        if FubonSDK is None:
            raise RuntimeError("富邦 SDK 尚未安裝或載入失敗")
        try:
            if self.ws is not None:
                self.ws.disconnect()
        except Exception:
            pass

        with self.lock:
            self.sdk = None
            self.ws = None
            self.logged_in = False
            self.connected = False
            self.error = None
            self.messages = {}
            self.subscribed = set()
            self.last_message_at = None
            self.tick_status = {}

        pfx_base64 = str(pfx_base64).strip()
        if "," in pfx_base64 and "base64" in pfx_base64[:80].lower():
            pfx_base64 = pfx_base64.split(",", 1)[1].strip()
        try:
            cert_bytes = base64.b64decode(pfx_base64, validate=True)
        except Exception as e:
            raise RuntimeError(f"pfx_base64 不是有效的 Base64 憑證資料：{e}")
        if not cert_bytes:
            raise RuntimeError("pfx_base64 解碼後是空資料")

        tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".pfx")
        tmp.write(cert_bytes)
        tmp.close()
        self.cert_path = tmp.name

        sdk = None
        ws = None
        try:
            sdk = FubonSDK()
            login_result = sdk.login(fubon_id.strip().upper(), fubon_password, self.cert_path, cert_password)
            is_success = getattr(login_result, "is_success", None)
            message = getattr(login_result, "message", None)
            if is_success is False:
                raise RuntimeError(f"富邦登入失敗：{message or login_result}")
            sdk.init_realtime()
            ws = sdk.marketdata.websocket_client.stock
            ws.on("message", self._on_message)
            ws.connect()
            with self.lock:
                self.sdk = sdk
                self.ws = ws
                self.logged_in = True
                self.connected = True
                self.error = None
        except Exception as e:
            try:
                if ws is not None:
                    ws.disconnect()
            except Exception:
                pass
            with self.lock:
                self.sdk = None
                self.ws = None
                self.logged_in = False
                self.connected = False
                self.error = str(e)
                self.messages = {}
                self.subscribed = set()
                self.last_message_at = None
                self.tick_status = {}
            raise

    def _parse_message(self, message):
        if isinstance(message, str):
            try:
                return json.loads(message)
            except Exception:
                return {"raw_text": message}
        if isinstance(message, dict):
            return message
        return {"raw_unknown": str(message)}

    def _safe_float(self, value):
        try:
            if value is None or pd.isna(value):
                return None
        except Exception:
            pass
        try:
            return float(str(value).strip().replace(",", ""))
        except Exception:
            return None

    def _safe_int(self, value):
        val = self._safe_float(value)
        if val is None:
            return None
        return int(round(val))

    def _extract_symbol(self, msg):
        data = msg.get("data", {})
        if not isinstance(data, dict):
            data = {}
        symbol = data.get("symbol") or msg.get("symbol") or data.get("stockNo") or msg.get("stockNo")
        return symbol_to_code(symbol) if symbol else None

    def _extract_price(self, msg):
        data = msg.get("data", {})
        if not isinstance(data, dict):
            data = {}
        candidates = [
            data.get("price"), data.get("tradePrice"), data.get("lastPrice"),
            data.get("close"), data.get("closePrice"),
            msg.get("price"), msg.get("tradePrice"), msg.get("lastPrice"),
            msg.get("close"), msg.get("closePrice"),
        ]
        for value in candidates:
            price = self._safe_float(value)
            if price is not None:
                return price
        return None

    def _extract_cumulative_volume(self, msg):
        """讀取富邦 trades 的累積成交量欄位。

        目前畫面中「最新單筆」出現 14780、95466 等大數字，代表來源欄位其實是盤中累積成交量，
        所以不能直接顯示；必須在 _on_message 內用差值換算單筆量。
        """
        data = msg.get("data", {})
        if not isinstance(data, dict):
            data = {}
        candidates = [
            data.get("volume"), data.get("tradeVolume"), data.get("totalVolume"), data.get("total_volume"),
            data.get("accVolume"), data.get("accTradeVolume"), data.get("cumulativeVolume"),
            msg.get("volume"), msg.get("tradeVolume"), msg.get("totalVolume"), msg.get("total_volume"),
            msg.get("accVolume"), msg.get("accTradeVolume"), msg.get("cumulativeVolume"),
        ]
        for value in candidates:
            volume = self._safe_int(value)
            if volume is not None:
                return volume
        return None

    def _extract_trade_type(self, msg, price=None):
        data = msg.get("data", {})
        if not isinstance(data, dict):
            data = {}
        candidates = [
            data.get("tradeType"), data.get("tickType"), data.get("type"), data.get("side"), data.get("dealType"),
            msg.get("tradeType"), msg.get("tickType"), msg.get("type"), msg.get("side"), msg.get("dealType"),
        ]
        raw_type = next((str(x).strip() for x in candidates if x is not None and str(x).strip()), "")
        raw_upper = raw_type.upper()
        if raw_upper in ["BUY", "B", "BID", "外盤", "外盤(買)", "買", "1"]:
            return "外盤(買)"
        if raw_upper in ["SELL", "S", "ASK", "內盤", "內盤(賣)", "賣", "2"]:
            return "內盤(賣)"

        # 若未提供內外盤欄位，嘗試用成交價與買一/賣一判斷。
        if price is not None:
            bid = self._safe_float(data.get("bid") or data.get("bidPrice") or data.get("bestBidPrice") or msg.get("bid") or msg.get("bidPrice"))
            ask = self._safe_float(data.get("ask") or data.get("askPrice") or data.get("bestAskPrice") or msg.get("ask") or msg.get("askPrice"))
            if ask is not None and price >= ask:
                return "外盤(買)"
            if bid is not None and price <= bid:
                return "內盤(賣)"
        return "-"

    def _format_large_order_text(self, large_order):
        if not large_order:
            return "監控中"
        lo_time = large_order.get("time")
        time_text = lo_time.strftime("%H:%M:%S") if hasattr(lo_time, "strftime") else "--:--:--"
        price = large_order.get("price")
        price_text = f"@{price:.2f}" if isinstance(price, (int, float)) else ""
        volume = large_order.get("volume")
        trade_type = large_order.get("type") or "-"
        icon = "🚀" if trade_type == "外盤(買)" else "📉" if trade_type == "內盤(賣)" else "🔔"
        return f"{icon} {time_text} {volume}張 {price_text} {trade_type}"

    def _on_message(self, message):
        msg = self._parse_message(message)
        now = datetime.now(TW_TZ)
        symbol = self._extract_symbol(msg)
        price = self._extract_price(msg)
        cumulative_volume = self._extract_cumulative_volume(msg)
        trade_type = self._extract_trade_type(msg, price)

        with self.lock:
            self.last_message_at = now
            if not symbol:
                return
            self.messages[symbol] = {"time": now, "raw": msg}

            status = self.tick_status.get(symbol, {
                "last_ws_price": None,
                "last_cumulative_volume": None,
                "real_tick_volume": None,
                "last_trade_type": "-",
                "total_buy_vol": 0,
                "total_sell_vol": 0,
                "recent_tick_orders": [],
                "latest_large_order": None,
            })

            if price is not None:
                status["last_ws_price"] = price

            # ===== 重點修正：累積量轉單筆量 =====
            # 富邦 trades 的 volume 常是「盤中累積成交量」。
            # 第一筆收到時沒有前值可扣，因此不當作單筆大單；第二筆後用差值才是真正最新單筆。
            tick_volume = None
            if cumulative_volume is not None:
                prev_cum = status.get("last_cumulative_volume")
                if prev_cum is not None:
                    diff = int(cumulative_volume) - int(prev_cum)
                    if diff > 0:
                        tick_volume = diff
                    elif diff == 0:
                        tick_volume = 0
                    else:
                        # 可能跨日、重連或資料重置，先重設基準，不用負數。
                        tick_volume = None
                status["last_cumulative_volume"] = int(cumulative_volume)

            if tick_volume is not None:
                status["real_tick_volume"] = tick_volume

            if trade_type:
                status["last_trade_type"] = trade_type

            if tick_volume is not None and tick_volume > 0:
                if trade_type == "外盤(買)":
                    status["total_buy_vol"] = int(status.get("total_buy_vol", 0) or 0) + tick_volume
                elif trade_type == "內盤(賣)":
                    status["total_sell_vol"] = int(status.get("total_sell_vol", 0) or 0) + tick_volume

                # 不在 WebSocket callback 內使用固定門檻過濾，避免 UI 調整大單張數後仍沿用舊門檻。
                tick_order = {
                    "time": now,
                    "price": status.get("last_ws_price"),
                    "volume": int(tick_volume),
                    "type": status.get("last_trade_type", "-"),
                }
                recent_orders = status.get("recent_tick_orders", [])
                recent_orders.append(tick_order)
                status["recent_tick_orders"] = recent_orders[-200:]
                status["latest_large_order"] = tick_order

            self.tick_status[symbol] = status

    def subscribe(self, symbol: str):
        if not self.ws:
            return
        code = symbol_to_code(symbol)
        if not code or code in self.subscribed:
            return
        try:
            self.ws.subscribe({"channel": "trades", "symbol": code})
            with self.lock:
                self.subscribed.add(code)
                self.error = None
        except Exception as e:
            with self.lock:
                self.error = f"{code} WebSocket 訂閱失敗：{e}"

    def subscribe_many(self, symbols):
        for symbol in symbols:
            self.subscribe(symbol)

    def get_message(self, symbol: str):
        code = symbol_to_code(symbol)
        with self.lock:
            return copy.deepcopy(self.messages.get(code))

    def get_order_status(self, symbol: str, large_order_threshold: int = 50):
        code = symbol_to_code(symbol)
        threshold = int(large_order_threshold or 50)
        with self.lock:
            status = copy.deepcopy(self.tick_status.get(code, {
                "last_ws_price": None,
                "last_cumulative_volume": None,
                "real_tick_volume": None,
                "last_trade_type": "-",
                "total_buy_vol": 0,
                "total_sell_vol": 0,
                "recent_tick_orders": [],
                "latest_large_order": None,
            }))

        latest_large_order = None
        for order in reversed(status.get("recent_tick_orders", [])):
            if int(order.get("volume") or 0) >= threshold:
                latest_large_order = order
                break

        status["latest_large_order"] = latest_large_order
        if latest_large_order:
            status["large_order_text"] = self._format_large_order_text(latest_large_order)
        else:
            status["large_order_text"] = "監控中"
        return status

    def get_status(self):
        with self.lock:
            return {
                "logged_in": self.logged_in,
                "connected": self.connected,
                "error": self.error,
                "subscribed_count": len(self.subscribed),
                "last_message_at": self.last_message_at,
            }


# =============================================================================
# 分組讀寫
# =============================================================================
def load_stock_groups():
    if os.path.exists(GROUPS_FILE):
        try:
            with open(GROUPS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                return data
        except Exception:
            pass
    return copy.deepcopy(DEFAULT_STOCK_GROUPS)


def save_stock_groups(groups):
    with open(GROUPS_FILE, "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)


def save_backup_snapshot(groups):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    file_path = os.path.join(BACKUP_DIR, f"stock_groups_backup_{datetime.now(TW_TZ).strftime('%Y%m%d_%H%M%S')}.json")
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)
    return file_path


# =============================================================================
# Telegram
# =============================================================================
def send_telegram_message(text: str):
    """送出 Telegram 訊息；需要在 Streamlit secrets 設定 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID。"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=5)
        if res.status_code != 200:
            st.error(f"Telegram 傳送失敗，API 回傳：{res.text}")
    except Exception as e:
        st.error(f"Telegram 連線失敗: {e}")


# =============================================================================
# Session State
# =============================================================================
if "auto_refresh_enabled" not in st.session_state:
    st.session_state.auto_refresh_enabled = True
if "refresh_sec" not in st.session_state:
    st.session_state.refresh_sec = 3
if "large_order_threshold" not in st.session_state:
    st.session_state.large_order_threshold = 50
if "telegram_pct_threshold" not in st.session_state:
    st.session_state.telegram_pct_threshold = 3.0
if "stock_groups" not in st.session_state:
    st.session_state.stock_groups = load_stock_groups()
if "group_editor_unlocked" not in st.session_state:
    st.session_state.group_editor_unlocked = False
if "editing_mode" not in st.session_state:
    st.session_state.editing_mode = False
if "selected_group_editor" not in st.session_state:
    group_names_init = list(st.session_state.stock_groups.keys())
    st.session_state.selected_group_editor = group_names_init[0] if group_names_init else ""
if "rename_group_input" not in st.session_state:
    st.session_state.rename_group_input = st.session_state.selected_group_editor
if "symbols_text_area" not in st.session_state:
    selected = st.session_state.selected_group_editor
    st.session_state.symbols_text_area = "\n".join(st.session_state.stock_groups.get(selected, []))
if "quick_add_symbol_input" not in st.session_state:
    st.session_state.quick_add_symbol_input = ""
if "fubon_manager" not in st.session_state:
    st.session_state.fubon_manager = FubonRealtimeManager()
if "fubon_logged_in" not in st.session_state:
    st.session_state.fubon_logged_in = False
if "tg_push_enabled" not in st.session_state:
    st.session_state.tg_push_enabled = False
if "large_order_toast_keys" not in st.session_state:
    # 用來避免同一筆大單在自動刷新時重複跳出 toast / Telegram
    st.session_state.large_order_toast_keys = set()
if "_large_order_toast_messages" not in st.session_state:
    # 大單買入 toast 佇列；資料掃描階段加入，畫面輸出前統一顯示
    st.session_state._large_order_toast_messages = []


def show_pending_toasts():
    """顯示右上角 toast。duration='long' 約為 10 秒。"""
    if "_quick_add_success_message" in st.session_state:
        st.toast(st.session_state._quick_add_success_message, duration="long")
        del st.session_state._quick_add_success_message

    messages = st.session_state.get("_large_order_toast_messages", [])
    if messages:
        # 避免同一輪太多 toast 佔滿畫面，最多顯示最新 3 則
        for msg in messages[-3:]:
            st.toast(msg, icon="🚀", duration="long")
        st.session_state._large_order_toast_messages = []


def queue_large_buy_toast(group_name, code, stock_name, latest, change_pct):
    """偵測到外盤買入大單時，加入 toast 佇列；若 Telegram 開啟則同步推送。"""
    if not latest or latest.get("type") != "外盤(買)":
        return

    order_time = latest.get("time")
    time_key = order_time.strftime("%Y%m%d%H%M%S") if hasattr(order_time, "strftime") else str(order_time)
    volume = int(latest.get("volume") or 0)
    price = latest.get("price")
    toast_key = f"{code}_{time_key}_{volume}_{latest.get('type', '-')}_{price}"

    # 同一筆大單只通知一次，避免自動刷新時重複跳出 toast / Telegram
    if toast_key in st.session_state.large_order_toast_keys:
        return

    time_text = order_time.strftime("%H:%M:%S") if hasattr(order_time, "strftime") else "--:--:--"
    price_text = f"{float(price):.2f}" if isinstance(price, (int, float)) else "-"
    pct_text = format_pct_value(change_pct)

    toast_msg = (
        f"🚀 大單買入偵測\n"
        f"{group_name}｜{code} {stock_name}\n"
        f"單筆：{volume} 張｜價格：{price_text}\n"
        f"時間：{time_text}｜漲幅：{pct_text}"
    )
    st.session_state._large_order_toast_messages.append(toast_msg)

    should_send_telegram = False
    try:
        if change_pct is not None:
            pct_threshold = abs(float(st.session_state.get("telegram_pct_threshold", 3.0) or 3.0))
            should_send_telegram = abs(float(change_pct)) >= pct_threshold
    except Exception:
        should_send_telegram = False

    if st.session_state.get("tg_push_enabled", False) and should_send_telegram:
        yahoo_url = yahoo_quote_url(code)
        telegram_msg = (
            f"🚀 <b>大單買入偵測</b>\n"
            f"分類：{group_name}\n"
            f"股票：<a href='{yahoo_url}'>{code} {stock_name}</a>\n"
            f"單筆：<b>{volume}</b> 張\n"
            f"價格：<b>{price_text}</b>\n"
            f"時間：{time_text}\n"
            f"漲幅：{pct_text}\n"
            f"推送門檻：±{pct_threshold:.1f}%"
        )
        send_telegram_message(telegram_msg)

    st.session_state.large_order_toast_keys.add(toast_key)

    # 控制記憶體，保留最近約 500 筆去重紀錄
    if len(st.session_state.large_order_toast_keys) > 500:
        st.session_state.large_order_toast_keys = set(list(st.session_state.large_order_toast_keys)[-300:])


# 顯示上一輪 rerun 留下的提示，例如快速新增股票成功
show_pending_toasts()


def enter_edit_mode():
    st.session_state.editing_mode = True


def leave_edit_mode():
    st.session_state.editing_mode = False


def set_next_selected_group(group_name: str):
    st.session_state._next_selected_group = group_name


if "_next_selected_group" in st.session_state:
    pending_group = st.session_state._next_selected_group
    del st.session_state._next_selected_group
    if pending_group in st.session_state.stock_groups:
        st.session_state.selected_group_editor = pending_group
        st.session_state.rename_group_input = pending_group
        st.session_state.symbols_text_area = "\n".join(st.session_state.stock_groups.get(pending_group, []))


# =============================================================================
# UI：富邦登入
# =============================================================================
def get_fubon_pfx_base64():
    try:
        return st.secrets["fubon"]["pfx_base64"]
    except Exception:
        return ""


def render_fubon_login():
    st.sidebar.markdown("## 🔑 富邦 WebSocket")
    manager = st.session_state.fubon_manager
    status = manager.get_status()

    if FubonSDK is None:
        st.sidebar.error("富邦 SDK 未載入，無法監控盤中大單。")
        return

    if st.sidebar.button("清除 / 重建富邦連線狀態", width="stretch"):
        st.session_state.fubon_manager = FubonRealtimeManager()
        st.session_state.fubon_logged_in = False
        st.session_state.pop("fubon_login_time", None)
        st.rerun()

    if st.session_state.fubon_logged_in:
        st.sidebar.success("✅ 富邦 WebSocket 已連線")
        st.sidebar.caption(f"已訂閱：{status['subscribed_count']} 檔")
        if status["last_message_at"]:
            st.sidebar.caption(f"最後資料：{status['last_message_at'].strftime('%H:%M:%S')}")
        if status["error"]:
            st.sidebar.warning(status["error"])
        c1, c2 = st.sidebar.columns(2)
        with c1:
            if st.button("盤中累積歸零", width="stretch"):
                manager.reset_runtime_data()
                st.rerun()
        with c2:
            if st.button("登出", width="stretch"):
                st.session_state.fubon_manager = FubonRealtimeManager()
                st.session_state.fubon_logged_in = False
                st.session_state.pop("fubon_login_time", None)
                st.rerun()
        return

    pfx_base64 = get_fubon_pfx_base64()
    if not pfx_base64:
        st.sidebar.warning("未設定 st.secrets['fubon']['pfx_base64']，無法登入富邦 WebSocket。")
        return

    with st.sidebar.expander("富邦登入", expanded=True):
        f_id = st.text_input("身分證字號", key="fubon_id_input")
        f_pw = st.text_input("富邦登入密碼", key="fubon_pw_input", type="password")
        f_cert_pw = st.text_input("憑證密碼", key="fubon_cert_pw_input", type="password")
        if st.button("連線富邦 WebSocket", width="stretch"):
            if not f_id or not f_pw or not f_cert_pw:
                st.warning("請填寫完整登入資訊")
            else:
                try:
                    new_manager = FubonRealtimeManager()
                    with st.spinner("連線富邦 WebSocket 中..."):
                        new_manager.login(f_id, f_pw, f_cert_pw, pfx_base64)
                    st.session_state.fubon_manager = new_manager
                    st.session_state.fubon_logged_in = True
                    st.session_state.fubon_login_time = datetime.now(TW_TZ)
                    st.success("富邦 WebSocket 連線成功")
                    st.rerun()
                except Exception as e:
                    st.session_state.fubon_manager = FubonRealtimeManager()
                    st.session_state.fubon_logged_in = False
                    st.error(f"富邦登入失敗：{e}")
                    st.exception(e)


# =============================================================================
# UI：分組編輯
# =============================================================================
def sync_editor_fields_from_selected_group():
    groups = st.session_state.stock_groups
    selected_group = st.session_state.selected_group_editor
    if selected_group not in groups:
        group_names = list(groups.keys())
        selected_group = group_names[0] if group_names else ""
        st.session_state.selected_group_editor = selected_group
    st.session_state.rename_group_input = selected_group
    st.session_state.symbols_text_area = "\n".join(groups.get(selected_group, []))
    st.session_state.editing_mode = False


def render_group_editor_lock():
    st.sidebar.markdown("## 🔐 分組編輯鎖")
    if st.session_state.group_editor_unlocked:
        st.sidebar.success("已解鎖，可編輯股票分組")
        if st.sidebar.button("鎖定編輯", key="lock_group_editor_btn", width="stretch"):
            st.session_state.group_editor_unlocked = False
            leave_edit_mode()
            st.rerun()
        return

    pin_input = st.sidebar.text_input("請輸入 PIN 碼以編輯分組", type="password", key="group_edit_pin_input")
    if st.sidebar.button("解鎖編輯", key="unlock_group_editor_btn", width="stretch"):
        if pin_input == GROUP_EDIT_PIN:
            st.session_state.group_editor_unlocked = True
            enter_edit_mode()
            st.rerun()
        else:
            st.sidebar.error("PIN 錯誤")


def render_stock_group_editor():
    st.sidebar.markdown("## 🛠️ 股票分組編輯")
    groups = st.session_state.stock_groups
    if not groups:
        groups = copy.deepcopy(DEFAULT_STOCK_GROUPS)
        st.session_state.stock_groups = groups

    group_names = list(groups.keys())
    if st.session_state.selected_group_editor not in group_names:
        st.session_state.selected_group_editor = group_names[0]
        st.session_state.rename_group_input = group_names[0]
        st.session_state.symbols_text_area = "\n".join(groups.get(group_names[0], []))

    with st.sidebar.expander("➕ 新增分類", expanded=False):
        new_group_name = st.text_input("分類名稱", key="new_group_name_input")
        if st.button("新增分類", key="add_group_btn", width="stretch"):
            name = new_group_name.strip()
            if not name:
                st.warning("請輸入分類名稱")
            elif name in groups:
                st.warning("分類名稱已存在")
            else:
                groups[name] = []
                st.session_state.stock_groups = groups
                save_stock_groups(groups)
                set_next_selected_group(name)
                st.rerun()

    with st.sidebar.expander("📝 編輯分類", expanded=True):
        st.selectbox("選擇分類", options=group_names, key="selected_group_editor", on_change=sync_editor_fields_from_selected_group)
        selected_group = st.session_state.selected_group_editor
        new_group_name = st.text_input("分類名稱（可修改）", key="rename_group_input", on_change=enter_edit_mode)
        symbols_text = st.text_area("股票清單（每行一檔，或逗號分隔）", height=180, key="symbols_text_area", on_change=enter_edit_mode)

        st.markdown("### ⚡ 快速新增股票")
        quick_input = st.text_input("輸入股票代碼或名稱", key="quick_add_symbol_input", on_change=enter_edit_mode)
        if quick_input.strip():
            symbol, stock_name, _ = resolve_stock_query(quick_input)
            if symbol:
                st.caption(f"查詢結果：{stock_name} / 將加入：{symbol}")
            else:
                st.caption("查無對應股票，請確認 TWstocklistname.txt 或輸入完整 ticker")

        if st.button("加入目前分類", key="quick_add_btn", width="stretch"):
            symbol, stock_name, _ = resolve_stock_query(quick_input)
            if not symbol:
                st.warning("請輸入股票代碼或股票名稱")
            else:
                current = groups.get(selected_group, [])
                if symbol in current:
                    st.warning("此股票已存在於目前分類")
                else:
                    current.append(symbol)
                    groups[selected_group] = current
                    st.session_state.stock_groups = groups
                    save_stock_groups(groups)
                    set_next_selected_group(selected_group)
                    if stock_name:
                        st.session_state._quick_add_success_message = f"已加入 {symbol}（{stock_name}）"
                    else:
                        st.session_state._quick_add_success_message = f"已加入 {symbol}"
                    st.rerun()

        c1, c2 = st.columns(2)
        with c1:
            if st.button("💾 儲存分類", key="save_group_btn", width="stretch"):
                new_name = new_group_name.strip()
                if not new_name:
                    st.warning("分類名稱不可為空")
                elif new_name != selected_group and new_name in groups:
                    st.warning("分類名稱已存在")
                else:
                    updated = {}
                    for k, v in groups.items():
                        updated[new_name if k == selected_group else k] = normalize_symbols_from_text(symbols_text) if k == selected_group else v
                    st.session_state.stock_groups = updated
                    save_stock_groups(updated)
                    leave_edit_mode()
                    set_next_selected_group(new_name)
                    st.rerun()
        with c2:
            if st.button("🗑️ 刪除分類", key="delete_group_btn", width="stretch"):
                if len(groups) <= 1:
                    st.warning("至少保留一個分類")
                else:
                    groups.pop(selected_group, None)
                    st.session_state.stock_groups = groups
                    save_stock_groups(groups)
                    leave_edit_mode()
                    set_next_selected_group(list(groups.keys())[0])
                    st.rerun()

    with st.sidebar.expander("📦 匯出 / 匯入 / 重設", expanded=False):
        export_json = json.dumps(st.session_state.stock_groups, ensure_ascii=False, indent=2)
        st.download_button("⬇️ 匯出目前分組 JSON", data=export_json, file_name="stock_groups.json", mime="application/json", width="stretch")
        uploaded_file = st.file_uploader("上傳股票分組 JSON", type=["json"])
        if uploaded_file is not None and st.button("📥 匯入並覆蓋目前分組", width="stretch"):
            try:
                data = json.loads(uploaded_file.read().decode("utf-8"))
                if not isinstance(data, dict) or not data:
                    raise ValueError("JSON 最外層必須是非空物件")
                save_backup_snapshot(st.session_state.stock_groups)
                validated = {str(k).strip(): normalize_symbols_from_text("\n".join(v) if isinstance(v, list) else str(v)) for k, v in data.items()}
                st.session_state.stock_groups = validated
                save_stock_groups(validated)
                set_next_selected_group(list(validated.keys())[0])
                st.rerun()
            except Exception as e:
                st.error(f"JSON 匯入失敗：{e}")

        if st.button("♻️ 還原預設分組", width="stretch"):
            try:
                save_backup_snapshot(st.session_state.stock_groups)
            except Exception:
                pass
            st.session_state.stock_groups = copy.deepcopy(DEFAULT_STOCK_GROUPS)
            save_stock_groups(st.session_state.stock_groups)
            set_next_selected_group(list(st.session_state.stock_groups.keys())[0])
            st.rerun()


# =============================================================================
# 主畫面
# =============================================================================
if os.path.exists(APP_LOGO):
    title_icon_col, title_text_col = st.columns([0.45, 8])
    with title_icon_col:
        st.image(APP_LOGO, width=58)
    with title_text_col:
        st.markdown("## ⚡ 盤中大單進出監控")
else:
    st.markdown("## ⚡ 盤中大單進出監控")

control_col1, control_col2, control_col3, control_col4, control_col5, control_col6, control_col7 = st.columns([1, 1, 1, 1, 1, 1, 1])
with control_col1:
    if st.button("🔄 手動刷新畫面", width="stretch"):
        st.rerun()
with control_col2:
    st.toggle("⏱️ 自動刷新", key="auto_refresh_enabled")
with control_col3:
    tg_push = st.toggle(
        "📲 Telegram 推送開關",
        value=st.session_state.tg_push_enabled,
        help="必須開啟此選項，機器人才會依推送漲跌幅門檻發送大單買入推播",
    )
    if tg_push != st.session_state.tg_push_enabled:
        st.session_state.tg_push_enabled = tg_push
        st.rerun()
with control_col4:
    st.number_input("刷新秒數", min_value=1, max_value=60, step=1, key="refresh_sec")
with control_col5:
    st.number_input("大單門檻（張）", min_value=1, step=10, key="large_order_threshold")
with control_col6:
    st.number_input(
        "推送漲跌幅門檻 (%)",
        min_value=0.0,
        max_value=10.0,
        step=0.5,
        key="telegram_pct_threshold",
        help="Telegram 推送條件：漲跌幅絕對值 >= 此數字，且同時有大單買入。預設 3%。",
    )
with control_col7:
    pct_threshold = st.number_input("漲幅門檻 (%)", min_value=0.0, max_value=10.0, value=5.0, step=0.5)

render_fubon_login()
render_group_editor_lock()
if st.session_state.group_editor_unlocked:
    render_stock_group_editor()
else:
    st.sidebar.info("目前為唯讀模式：輸入 PIN 後才能修改股票分組")

manager = st.session_state.fubon_manager

if st.session_state.fubon_logged_in:
    login_time = st.session_state.get("fubon_login_time")
    can_subscribe = True
    if login_time:
        can_subscribe = (datetime.now(TW_TZ) - login_time).total_seconds() >= 1
    if can_subscribe:
        all_symbols = []
        for stocks in st.session_state.stock_groups.values():
            all_symbols.extend(stocks)
        manager.subscribe_many(all_symbols)
    else:
        st.sidebar.info("等待富邦 WebSocket 連線穩定後訂閱股票...")

status = manager.get_status()
st.caption(f"更新時間：{datetime.now(TW_TZ).strftime('%Y-%m-%d %H:%M:%S')}")
if status["connected"]:
    st.success(f"富邦 WebSocket 已連線｜已訂閱 {status['subscribed_count']} 檔")
else:
    st.warning("富邦 WebSocket 尚未連線。請先在左側登入富邦，否則表格只會顯示監控中。")
if status["last_message_at"]:
    st.caption(f"最後收到資料：{status['last_message_at'].strftime('%H:%M:%S')}")
if status["error"]:
    st.error(status["error"])

st.info("即時價由富邦 WebSocket trades 抓取；最新單筆 = 富邦盤中累積成交量差值；漲幅% = 富邦即時價 ÷ yfinance 昨日收盤價 - 1。yfinance 昨收每小時更新一次並存入 yf_yesterday_close_cache.json。")

# ===== 先整理資料：同一份資料同時產生儀表板與表格 =====
group_tables = {}
dashboard_items = []
recent_large_orders = []
yf_source_count = {"yfinance": 0, "cache": 0, "stale cache": 0, "missing": 0}

for group_name, stocks in st.session_state.stock_groups.items():
    rows = []
    large_order_count = 0
    large_buy_count = 0
    large_sell_count = 0
    pct_hit_count = 0
    known_pct_count = 0
    up_count = 0
    down_count = 0
    top_pct_items = []
    group_large_messages = []

    for symbol in stocks:
        code = symbol_to_code(symbol)
        stock_name = get_stock_name(symbol)
        order_status = manager.get_order_status(symbol, st.session_state.large_order_threshold) if manager is not None else {}
        tick_vol = order_status.get("real_tick_volume")
        current_price = order_status.get("last_ws_price")
        yesterday_close, close_date, yf_source = get_yfinance_yesterday_close(symbol)
        if yf_source in yf_source_count:
            yf_source_count[yf_source] += 1
        elif yesterday_close is None:
            yf_source_count["missing"] += 1

        change_pct = None
        if current_price is not None and yesterday_close is not None and yesterday_close > 0:
            change_pct = (float(current_price) / float(yesterday_close) - 1) * 100

        large_text = order_status.get("large_order_text", "監控中")
        if large_text != "監控中" and change_pct is not None:
            large_text = f"{large_text}｜{format_pct_value(change_pct)}"
        latest = order_status.get("latest_large_order")
        trade_type = order_status.get("last_trade_type", "-")

        if change_pct is not None:
            known_pct_count += 1
            if float(change_pct) > 0:
                up_count += 1
            elif float(change_pct) < 0:
                down_count += 1
            top_pct_items.append({"code": code, "name": stock_name, "pct": float(change_pct)})
            if float(change_pct) >= float(pct_threshold):
                pct_hit_count += 1

        if latest:
            large_order_count += 1
            if latest.get("type") == "外盤(買)":
                large_buy_count += 1
            elif latest.get("type") == "內盤(賣)":
                large_sell_count += 1
            msg = {"group": group_name, "code": code, "name": stock_name, "text": large_text, "time": latest.get("time"), "pct": change_pct, "type": latest.get("type", "-")}
            group_large_messages.append(msg)
            recent_large_orders.append(msg)

            # ✅ 右上角 toast + Telegram：監控到「外盤(買)」大單時通知
            queue_large_buy_toast(group_name, code, stock_name, latest, change_pct)

        rows.append({
            "代碼": yahoo_quote_url(symbol),
            "股票名稱": stock_name,
            "大單追蹤": large_text,
            "即時價": format_price_value(current_price),
            "漲幅%": format_pct_value(change_pct),
            "最新單筆": "-" if tick_vol is None else f"{tick_vol} 張",
            "內外盤": trade_type,
            "外盤累積": int(order_status.get("total_buy_vol", 0) or 0),
            "內盤累積": int(order_status.get("total_sell_vol", 0) or 0),
            "昨收日期": close_date or "-",
            "昨收來源": yf_source,
        })

    top_pct_items.sort(key=lambda x: x["pct"], reverse=True)
    top_pct_text = "｜".join([
        f'<span class="{pct_class(item["pct"])}">{escape_html(item["code"])} {escape_html(item["name"])} {item["pct"]:+.2f}%</span>'
        for item in top_pct_items[:3]
    ]) or "尚無漲幅資料"

    group_large_messages.sort(key=lambda x: x["time"] or datetime.min.replace(tzinfo=TW_TZ), reverse=True)
    large_msg_text = "<br>".join([
        f'▸ {escape_html(item["code"])} {escape_html(item["name"])}：{escape_html(item["text"])}'
        for item in group_large_messages[:3]
    ]) or "尚無大單"

    pct_hit_ratio = (pct_hit_count / len(stocks) * 100) if len(stocks) else 0
    dashboard_items.append({
        "group": group_name,
        "total": len(stocks),
        "large_order_count": large_order_count,
        "large_buy_count": large_buy_count,
        "large_sell_count": large_sell_count,
        "pct_hit_count": pct_hit_count,
        "known_pct_count": known_pct_count,
        "pct_hit_ratio": pct_hit_ratio,
        "up_count": up_count,
        "down_count": down_count,
        "top_pct_text": top_pct_text,
        "large_msg_text": large_msg_text,
    })
    group_tables[group_name] = pd.DataFrame(rows, columns=["代碼", "股票名稱", "大單追蹤", "即時價", "漲幅%", "最新單筆", "內外盤", "外盤累積", "內盤累積", "昨收日期", "昨收來源"])

# 顯示本輪掃描到的大單買入 toast
show_pending_toasts()

# ===== 儀表板 =====
st.markdown('<div id="dashboard-top" style="scroll-margin-top: 90px;"></div>', unsafe_allow_html=True)
st.markdown("### 📌 大單追蹤儀表板")
st.caption(f"大單門檻：單筆 ≥ {st.session_state.large_order_threshold} 張｜漲幅達標門檻：≥ {pct_threshold:.1f}%｜Telegram 推送門檻：漲跌幅 ≥ ±{st.session_state.telegram_pct_threshold:.1f}%｜yfinance 昨收快取：{YF_CLOSE_CACHE_TTL_SEC//60} 分鐘")
st.caption(f"昨收來源統計：yfinance 即時更新 {yf_source_count['yfinance']} 檔｜快取 {yf_source_count['cache']} 檔｜舊快取 {yf_source_count['stale cache']} 檔｜缺資料 {yf_source_count['missing']} 檔")

card_html_parts = ['<div class="dashboard-grid">']
for item in dashboard_items:
    pct_ratio = float(item.get("pct_hit_ratio", 0) or 0)
    if pct_ratio >= 70:
        card_class = "dash-card pct-high"
    elif pct_ratio >= 40:
        card_class = "dash-card pct-mid"
    elif pct_ratio > 0:
        card_class = "dash-card pct-low"
    else:
        card_class = "dash-card pct-zero"
    anchor_id = make_anchor_id(item["group"])
    card_html_parts.append(
        f'<a href="#{anchor_id}" class="dashboard-link" title="前往 {escape_html(item["group"])} 明細表；大數字 = 漲幅達標檔數 / 分組總檔數">'
        f'<div class="{card_class}">'
        f'<div class="dash-title">{escape_html(item["group"])}</div>'
        f'<div class="dash-big">{item["pct_hit_count"]} / {item["total"]}</div>'
        f'<div class="dash-line">漲幅達標比例（≥{pct_threshold:.1f}%）：<b>{item["pct_hit_ratio"]:.0f}%</b></div>'
        f'<div class="dash-line">🎯 達標：<b>{item["pct_hit_count"]}</b> 檔（有漲幅資料 {item["known_pct_count"]} 檔）</div>'
        f'<div class="dash-line">🔴 一般上漲：<b>{item["up_count"]}</b>　🟢 下跌：<b>{item["down_count"]}</b></div>'
        f'<div class="dash-line">🚀 外盤大單：<b>{item["large_buy_count"]}</b>　📉 內盤大單：<b>{item["large_sell_count"]}</b></div>'
        f'<div class="dash-small"><b>大單追蹤</b><br>{item["large_msg_text"]}</div>'
        f'<div class="dash-small"><b>漲幅排行</b><br>{item["top_pct_text"]}</div>'
        f'</div></a>'
    )
card_html_parts.append('</div>')
st.markdown("".join(card_html_parts), unsafe_allow_html=True)

recent_large_orders.sort(key=lambda x: x["time"] or datetime.min.replace(tzinfo=TW_TZ), reverse=True)
if recent_large_orders:
    st.markdown("#### 🔔 最近大單訊息")
    recent_cols = st.columns(min(3, len(recent_large_orders)))
    for idx, item in enumerate(recent_large_orders[:6]):
        with recent_cols[idx % len(recent_cols)]:
            st.info(f"{item['group']}｜{item['code']} {item['name']}\n\n{item['text']}")

st.divider()

# ===== 明細表 =====
for group_name, display_df in group_tables.items():
    anchor_id = make_anchor_id(group_name)
    st.markdown(f'<div id="{anchor_id}" style="scroll-margin-top: 90px;"></div>', unsafe_allow_html=True)
    table_header_col1, table_header_col2 = st.columns([8, 2])
    with table_header_col1:
        st.subheader(f"【{group_name}】({len(display_df)}檔)")
    with table_header_col2:
        st.markdown(
            '<div style="text-align:right; padding-top: 0.6rem;">'
            '<a href="#dashboard-top">⬆️ 返回儀表板</a>'
            '</div>',
            unsafe_allow_html=True,
        )

    st.dataframe(
        display_df,
        width="stretch",
        hide_index=True,
        column_config={
            "代碼": st.column_config.LinkColumn("代碼", help="點擊前往 Yahoo 台股個股頁", display_text=r"https://tw.stock.yahoo.com/quote/(.*)"),
            "股票名稱": st.column_config.TextColumn("股票名稱"),
            "大單追蹤": st.column_config.TextColumn("大單追蹤", help="達到大單門檻時顯示最近一筆大單時間、張數、價格、內外盤與漲幅"),
            "即時價": st.column_config.TextColumn("即時價", help="由富邦 WebSocket trades 即時成交價取得"),
            "漲幅%": st.column_config.TextColumn("漲幅%", help="富邦 WebSocket 即時價 / yfinance 昨日收盤價 - 1"),
            "最新單筆": st.column_config.TextColumn("最新單筆", help="由累積成交量差值換算，不再直接顯示累積成交量"),
            "內外盤": st.column_config.TextColumn("內外盤"),
            "外盤累積": st.column_config.NumberColumn("外盤累積"),
            "內盤累積": st.column_config.NumberColumn("內盤累積"),
            "昨收日期": st.column_config.TextColumn("昨收日期"),
            "昨收來源": st.column_config.TextColumn("昨收來源"),
        },
    )

with st.sidebar.expander("🔍 WebSocket Debug", expanded=False):
    debug_code = st.text_input("輸入代碼看最後 WS 原始訊息", value="2330")
    msg = manager.get_message(debug_code)
    if msg:
        st.caption(f"時間：{msg['time'].strftime('%Y-%m-%d %H:%M:%S')}")
        st.json(msg["raw"])
    else:
        st.caption("尚未收到此代碼的 WebSocket 訊息")

if st.session_state.auto_refresh_enabled and not st.session_state.group_editor_unlocked and not st.session_state.editing_mode:
    time.sleep(int(st.session_state.refresh_sec))
    st.rerun()
