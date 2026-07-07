# -*- coding: utf-8 -*-
"""
盤中即時成交監控 - 純富邦 WebSocket 版

重點：
1. 富邦 WebSocket 負責盤中即時價、單筆量、內外盤。
2. yfinance 只用來抓「昨日收盤價」，一小時更新一次，並存入本地 JSON 歷史快取。
3. 表格顯示：代碼 Yahoo 連結｜股票名稱｜量價訊號｜即時價｜漲幅%｜最新單筆｜內外盤｜外盤累積｜內盤累積。
4. 已重新定義訊號條件：
   條件1：
     用 30 秒為單位紀錄成交量。
     當目前 30 秒成交量 > 上一個 30 秒成交量 × 1.3，
     且目前 30 秒內股價拉抬 >= 2% 或下跌 <= -2% 時，發出訊號。
   條件2：
     用 30 秒為單位紀錄成交量。
     當目前 30 秒成交量 > 上一個 30 秒成交量 × 1.3，
     並追蹤最近 1 分鐘低點 / 高點，
     當股價自 1 分鐘低點拉抬 >= 2%，或自 1 分鐘高點下跌 >= 2% 時，發出訊號。
"""

import os
import re
import json
import copy
import time
import base64
import tempfile
import threading
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


st.set_page_config(page_title="盤中即時成交監控", layout="wide")

TW_TZ = ZoneInfo("Asia/Taipei")
GROUP_EDIT_PIN = "1219"
GROUPS_FILE = "stock_groups.json"
BACKUP_DIR = "backups"
STOCK_NAME_FILE = "TWstocklistname.txt"
APP_LOGO = "jerry.jpg"

YF_CLOSE_CACHE_FILE = "yf_yesterday_close_cache.json"
YF_CLOSE_CACHE_TTL_SEC = 3600

DEFAULT_SIGNAL_WINDOW_SEC = 30
DEFAULT_SIGNAL_TRACK_SEC = 60
DEFAULT_SIGNAL_VOLUME_RATIO = 1.3
DEFAULT_SIGNAL_PRICE_MOVE_PCT = 2.0

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

    .dashboard-link,
    .dashboard-link:link,
    .dashboard-link:visited,
    .dashboard-link:hover,
    .dashboard-link:active {
        text-decoration:none !important;
        color:inherit !important;
        display:block;
    }

    .dashboard-grid {
        display:grid;
        grid-template-columns:repeat(4,minmax(240px,1fr));
        gap:12px;
        margin:10px 0 18px 0;
    }

    .dash-card {
        border:1px solid #91d5ff;
        border-radius:12px;
        padding:14px 16px;
        min-height:180px;
        background:#f0f9ff;
        box-shadow:0 1px 2px rgba(0,0,0,.04);
        color:#111827;
        cursor:pointer;
        transition:transform .12s ease, box-shadow .12s ease;
    }

    .dash-card:hover {
        transform:translateY(-2px);
        box-shadow:0 4px 10px rgba(0,0,0,.18);
    }

    .dash-card.pct-zero {
        border-color:#d9d9d9;
        background:#fafafa;
        color:#374151;
    }

    .dash-card.pct-low {
        border-color:#91d5ff;
        background:#f0f9ff;
        color:#111827;
    }

    .dash-card.pct-mid {
        border-color:#ffd666;
        background:#fffbe6;
        color:#111827;
    }

    .dash-card.pct-high {
        border-color:#ff7875;
        background:#fff1f0;
        color:#111827;
        box-shadow:0 1px 8px rgba(207,19,34,.18);
    }

    .dash-title {
        font-weight:800;
        font-size:18px;
        margin-bottom:8px;
        color:#111827;
    }

    .dash-big {
        font-size:30px;
        font-weight:900;
        margin:4px 0 10px 0;
        color:#111827 !important;
        text-shadow:none;
        letter-spacing:.3px;
    }

    .dash-card.pct-zero .dash-big { color:#374151 !important; }
    .dash-card.pct-low .dash-big { color:#0f172a !important; }
    .dash-card.pct-mid .dash-big { color:#7a4e00 !important; }
    .dash-card.pct-high .dash-big {
        color:#b91c1c !important;
        text-shadow:0 1px 1px rgba(255,255,255,.65);
    }

    .dash-line {
        font-size:14px;
        line-height:1.65;
        color:#111827;
    }

    .dash-small {
        font-size:12px;
        color:#374151;
        margin-top:8px;
        border-top:1px solid rgba(0,0,0,.10);
        padding-top:8px;
    }

    .up-text {
        color:#cf1322;
        font-weight:700;
    }

    .down-text {
        color:#389e0d;
        font-weight:700;
    }

    .flat-text {
        color:#6b7280;
        font-weight:700;
    }

    @media (max-width:1200px) {
        .dashboard-grid {
            grid-template-columns:repeat(2,minmax(240px,1fr));
        }
    }

    @media (max-width:700px) {
        .dashboard-grid {
            grid-template-columns:1fr;
        }
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# =============================================================================
# 基礎工具
# =============================================================================
def get_secret_or_default(key: str, default: str = ""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


def symbol_to_code(symbol: str) -> str:
    return str(symbol).strip().upper().split(".")[0]


def yahoo_quote_url(symbol: str) -> str:
    code = symbol_to_code(symbol)
    return f"https://tw.stock.yahoo.com/quote/{code}"


def make_anchor_id(group_name: str) -> str:
    anchor = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", str(group_name)).strip("-")
    return f"group-{anchor or 'default'}"


def format_price_value(value):
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


def format_signed_pct(value):
    if value is None:
        return "-"
    try:
        value = float(value)
    except Exception:
        return "-"
    if value > 0:
        return f"+{value:.2f}%"
    return f"{value:.2f}%"


def format_ratio_value(value):
    if value is None:
        return "-"
    try:
        return f"{float(value):.2f}x"
    except Exception:
        return "-"


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
    return (
        str(text_value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


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
            df = yf.download(
                yf_symbol,
                period="10d",
                interval="1d",
                auto_adjust=False,
                progress=False,
                threads=False,
            )

            if df is None or df.empty:
                last_error = f"{yf_symbol}: no data"
                continue

            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]

            df = df.reset_index()

            if "Date" in df.columns:
                date_col = "Date"
            elif "Datetime" in df.columns:
                date_col = "Datetime"
            else:
                date_col = df.columns[0]

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

            cache[code] = {
                "symbol": yf_symbol,
                "close": close_value,
                "date": close_date,
                "fetched_at": now_ts,
                "fetched_at_text": datetime.now(TW_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            }

            save_yf_close_cache(cache)

            return close_value, close_date, "yfinance"

        except Exception as e:
            last_error = f"{yf_symbol}: {e}"
            continue

    if isinstance(cached, dict) and cached.get("close") is not None:
        return float(cached["close"]), cached.get("date", ""), "stale cache"

    return None, "", last_error or "no data"


# =============================================================================
# 股票名稱 / 查詢
# =============================================================================
@st.cache_data(ttl=86400)
def load_stock_lookup_maps(file_path: str = STOCK_NAME_FILE) -> dict:
    code_to_name = {}
    code_to_symbol = {}
    name_to_symbol = {}

    if not os.path.exists(file_path):
        return {
            "code_to_name": code_to_name,
            "code_to_symbol": code_to_symbol,
            "name_to_symbol": name_to_symbol,
        }

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

    return {
        "code_to_name": code_to_name,
        "code_to_symbol": code_to_symbol,
        "name_to_symbol": name_to_symbol,
    }


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
            login_result = sdk.login(
                fubon_id.strip().upper(),
                fubon_password,
                self.cert_path,
                cert_password,
            )

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

        symbol = (
            data.get("symbol")
            or msg.get("symbol")
            or data.get("stockNo")
            or msg.get("stockNo")
        )

        return symbol_to_code(symbol) if symbol else None

    def _extract_price(self, msg):
        data = msg.get("data", {})

        if not isinstance(data, dict):
            data = {}

        candidates = [
            data.get("price"),
            data.get("tradePrice"),
            data.get("lastPrice"),
            data.get("close"),
            data.get("closePrice"),
            msg.get("price"),
            msg.get("tradePrice"),
            msg.get("lastPrice"),
            msg.get("close"),
            msg.get("closePrice"),
        ]

        for value in candidates:
            price = self._safe_float(value)
            if price is not None:
                return price

        return None

    def _extract_cumulative_volume(self, msg):
        data = msg.get("data", {})

        if not isinstance(data, dict):
            data = {}

        candidates = [
            data.get("volume"),
            data.get("tradeVolume"),
            data.get("totalVolume"),
            data.get("total_volume"),
            data.get("accVolume"),
            data.get("accTradeVolume"),
            data.get("cumulativeVolume"),
            msg.get("volume"),
            msg.get("tradeVolume"),
            msg.get("totalVolume"),
            msg.get("total_volume"),
            msg.get("accVolume"),
            msg.get("accTradeVolume"),
            msg.get("cumulativeVolume"),
        ]

        for value in candidates:
            volume = self._safe_int(value)
            if volume is not None:
                return volume

        return None

    def _extract_tick_size(self, msg):
        data = msg.get("data", {})

        if not isinstance(data, dict):
            data = {}

        candidates = [
            data.get("size"),
            data.get("tradeSize"),
            data.get("trade_size"),
            data.get("quantity"),
            data.get("qty"),
            msg.get("size"),
            msg.get("tradeSize"),
            msg.get("trade_size"),
            msg.get("quantity"),
            msg.get("qty"),
        ]

        for value in candidates:
            size = self._safe_int(value)
            if size is not None:
                return size

        return None

    def _extract_trade_type(self, msg, price=None):
        data = msg.get("data", {})

        if not isinstance(data, dict):
            data = {}

        candidates = [
            data.get("tradeType"),
            data.get("tickType"),
            data.get("type"),
            data.get("side"),
            data.get("dealType"),
            msg.get("tradeType"),
            msg.get("tickType"),
            msg.get("type"),
            msg.get("side"),
            msg.get("dealType"),
        ]

        raw_type = next(
            (str(x).strip() for x in candidates if x is not None and str(x).strip()),
            "",
        )

        raw_upper = raw_type.upper()

        if raw_upper in ["BUY", "B", "BID", "外盤", "外盤(買)", "買", "1"]:
            return "外盤(買)"

        if raw_upper in ["SELL", "S", "ASK", "內盤", "內盤(賣)", "賣", "2"]:
            return "內盤(賣)"

        if price is not None:
            bid = self._safe_float(
                data.get("bid")
                or data.get("bidPrice")
                or data.get("bestBidPrice")
                or msg.get("bid")
                or msg.get("bidPrice")
            )

            ask = self._safe_float(
                data.get("ask")
                or data.get("askPrice")
                or data.get("bestAskPrice")
                or msg.get("ask")
                or msg.get("askPrice")
            )

            if ask is not None and price >= ask:
                return "外盤(買)"

            if bid is not None and price <= bid:
                return "內盤(賣)"

        return "-"

    def _on_message(self, message):
        msg = self._parse_message(message)
        now = datetime.now(TW_TZ)

        symbol = self._extract_symbol(msg)
        price = self._extract_price(msg)
        direct_tick_size = self._extract_tick_size(msg)
        cumulative_volume = self._extract_cumulative_volume(msg)
        trade_type = self._extract_trade_type(msg, price)

        with self.lock:
            self.last_message_at = now

            if not symbol:
                return

            self.messages[symbol] = {
                "time": now,
                "raw": msg,
            }

            status = self.tick_status.get(symbol, {
                "last_ws_price": None,
                "last_cumulative_volume": None,
                "real_tick_volume": None,
                "last_trade_type": "-",
                "total_buy_vol": 0,
                "total_sell_vol": 0,
                "recent_ticks": [],
                "price_points": [],
            })

            if price is not None:
                status["last_ws_price"] = price

                price_points = status.get("price_points", [])
                price_points.append({
                    "time": now,
                    "price": float(price),
                })
                status["price_points"] = [
                    p for p in price_points
                    if hasattr(p.get("time"), "timestamp")
                    and (now - p.get("time")).total_seconds() <= 300
                ][-1000:]

            tick_volume = None

            if direct_tick_size is not None:
                tick_volume = int(direct_tick_size)

                if cumulative_volume is not None:
                    status["last_cumulative_volume"] = int(cumulative_volume)

            elif cumulative_volume is not None:
                prev_cum = status.get("last_cumulative_volume")

                if prev_cum is not None:
                    diff = int(cumulative_volume) - int(prev_cum)

                    if diff > 0:
                        tick_volume = diff
                    elif diff == 0:
                        tick_volume = 0
                    else:
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

                tick = {
                    "time": now,
                    "price": status.get("last_ws_price"),
                    "volume": int(tick_volume),
                    "type": status.get("last_trade_type", "-"),
                }

                recent_ticks = status.get("recent_ticks", [])
                recent_ticks.append(tick)
                status["recent_ticks"] = [
                    t for t in recent_ticks
                    if hasattr(t.get("time"), "timestamp")
                    and (now - t.get("time")).total_seconds() <= 300
                ][-1000:]

            self.tick_status[symbol] = status

    def subscribe(self, symbol: str):
        if not self.ws:
            return

        code = symbol_to_code(symbol)

        if not code or code in self.subscribed:
            return

        try:
            self.ws.subscribe({
                "channel": "trades",
                "symbol": code,
            })

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

    def get_tick_status(self, symbol: str):
        code = symbol_to_code(symbol)

        with self.lock:
            return copy.deepcopy(self.tick_status.get(code, {
                "last_ws_price": None,
                "last_cumulative_volume": None,
                "real_tick_volume": None,
                "last_trade_type": "-",
                "total_buy_vol": 0,
                "total_sell_vol": 0,
                "recent_ticks": [],
                "price_points": [],
            }))

    def get_volume_price_signal(
        self,
        symbol: str,
        window_sec: int = DEFAULT_SIGNAL_WINDOW_SEC,
        track_sec: int = DEFAULT_SIGNAL_TRACK_SEC,
        volume_ratio_threshold: float = DEFAULT_SIGNAL_VOLUME_RATIO,
        price_move_pct: float = DEFAULT_SIGNAL_PRICE_MOVE_PCT,
    ):
        code = symbol_to_code(symbol)

        try:
            window_sec = max(5, int(window_sec))
        except Exception:
            window_sec = DEFAULT_SIGNAL_WINDOW_SEC

        try:
            track_sec = max(window_sec, int(track_sec))
        except Exception:
            track_sec = DEFAULT_SIGNAL_TRACK_SEC

        try:
            volume_ratio_threshold = max(0.1, float(volume_ratio_threshold))
        except Exception:
            volume_ratio_threshold = DEFAULT_SIGNAL_VOLUME_RATIO

        try:
            price_move_pct = max(0.1, float(price_move_pct))
        except Exception:
            price_move_pct = DEFAULT_SIGNAL_PRICE_MOVE_PCT

        now = datetime.now(TW_TZ)
        now_ts = now.timestamp()
        bucket_id = int(now_ts // window_sec)

        with self.lock:
            status = copy.deepcopy(self.tick_status.get(code, {
                "last_ws_price": None,
                "recent_ticks": [],
                "price_points": [],
            }))

        current_price = status.get("last_ws_price")
        recent_ticks = status.get("recent_ticks", [])
        price_points = status.get("price_points", [])

        current_window_ticks = []
        previous_window_ticks = []

        for tick in recent_ticks:
            tick_time = tick.get("time")
            if not hasattr(tick_time, "timestamp"):
                continue

            age_sec = (now - tick_time).total_seconds()

            volume = 0
            try:
                volume = int(tick.get("volume") or 0)
            except Exception:
                volume = 0

            if 0 <= age_sec <= window_sec:
                current_window_ticks.append(tick)
            elif window_sec < age_sec <= window_sec * 2:
                previous_window_ticks.append(tick)

        current_volume = sum(int(t.get("volume") or 0) for t in current_window_ticks)
        previous_volume = sum(int(t.get("volume") or 0) for t in previous_window_ticks)

        volume_ratio = None
        volume_ok = False

        if previous_volume > 0:
            volume_ratio = current_volume / previous_volume
            volume_ok = volume_ratio >= volume_ratio_threshold

        current_window_prices = []

        for point in price_points:
            point_time = point.get("time")
            point_price = self._safe_float(point.get("price"))

            if not hasattr(point_time, "timestamp") or point_price is None or point_price <= 0:
                continue

            age_sec = (now - point_time).total_seconds()

            if 0 <= age_sec <= window_sec:
                current_window_prices.append(point)

        current_window_prices.sort(key=lambda x: x.get("time"))

        start_30_price = None
        price_change_30_pct = None

        if current_window_prices:
            start_30_price = self._safe_float(current_window_prices[0].get("price"))

        if (
            current_price is not None
            and start_30_price is not None
            and start_30_price > 0
        ):
            price_change_30_pct = (float(current_price) / float(start_30_price) - 1) * 100

        track_prices = []

        for point in price_points:
            point_time = point.get("time")
            point_price = self._safe_float(point.get("price"))

            if not hasattr(point_time, "timestamp") or point_price is None or point_price <= 0:
                continue

            age_sec = (now - point_time).total_seconds()

            if 0 <= age_sec <= track_sec:
                track_prices.append(point_price)

        low_1m = min(track_prices) if track_prices else None
        high_1m = max(track_prices) if track_prices else None

        rise_from_low_pct = None
        drop_from_high_pct = None

        if current_price is not None and low_1m is not None and low_1m > 0:
            rise_from_low_pct = (float(current_price) / float(low_1m) - 1) * 100

        if current_price is not None and high_1m is not None and high_1m > 0:
            drop_from_high_pct = (float(current_price) / float(high_1m) - 1) * 100

        condition1_up = (
            volume_ok
            and price_change_30_pct is not None
            and price_change_30_pct >= price_move_pct
        )

        condition1_down = (
            volume_ok
            and price_change_30_pct is not None
            and price_change_30_pct <= -price_move_pct
        )

        condition2_up = (
            volume_ok
            and rise_from_low_pct is not None
            and rise_from_low_pct >= price_move_pct
        )

        condition2_down = (
            volume_ok
            and drop_from_high_pct is not None
            and drop_from_high_pct <= -price_move_pct
        )

        active_conditions = []

        if condition1_up:
            active_conditions.append("條件1急拉")
        if condition1_down:
            active_conditions.append("條件1急殺")
        if condition2_up:
            active_conditions.append("條件2低點拉抬")
        if condition2_down:
            active_conditions.append("條件2高點下跌")

        active = len(active_conditions) > 0

        direction = "-"
        if condition1_up or condition2_up:
            direction = "拉抬"
        elif condition1_down or condition2_down:
            direction = "下跌"

        if active:
            icon = "🚀" if direction == "拉抬" else "📉"
            text = (
                f"{icon} {direction}訊號｜"
                f"{'、'.join(active_conditions)}｜"
                f"本30秒量 {current_volume} / 前30秒量 {previous_volume}｜"
                f"量比 {format_ratio_value(volume_ratio)}｜"
                f"30秒變化 {format_signed_pct(price_change_30_pct)}｜"
                f"1分低點拉抬 {format_signed_pct(rise_from_low_pct)}｜"
                f"1分高點變化 {format_signed_pct(drop_from_high_pct)}"
            )
        else:
            text = "監控中"

        signal_key = (
            f"{code}_{bucket_id}_{direction}_"
            f"{'_'.join(active_conditions)}_"
            f"cv{current_volume}_pv{previous_volume}_"
            f"p{current_price}"
        )

        return {
            "active": bool(active),
            "code": code,
            "time": now,
            "bucket_id": bucket_id,
            "direction": direction,
            "conditions": active_conditions,
            "text": text,
            "current_price": current_price,
            "current_volume": int(current_volume),
            "previous_volume": int(previous_volume),
            "volume_ratio": volume_ratio,
            "volume_ok": bool(volume_ok),
            "start_30_price": start_30_price,
            "price_change_30_pct": price_change_30_pct,
            "low_1m": low_1m,
            "high_1m": high_1m,
            "rise_from_low_pct": rise_from_low_pct,
            "drop_from_high_pct": drop_from_high_pct,
            "signal_key": signal_key,
            "window_sec": window_sec,
            "track_sec": track_sec,
            "volume_ratio_threshold": volume_ratio_threshold,
            "price_move_pct": price_move_pct,
        }

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

    file_path = os.path.join(
        BACKUP_DIR,
        f"stock_groups_backup_{datetime.now(TW_TZ).strftime('%Y%m%d_%H%M%S')}.json",
    )

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)

    return file_path


# =============================================================================
# Session State
# =============================================================================
if "auto_refresh_enabled" not in st.session_state:
    st.session_state.auto_refresh_enabled = True

if "refresh_sec" not in st.session_state:
    st.session_state.refresh_sec = 3

if "signal_window_sec" not in st.session_state:
    st.session_state.signal_window_sec = DEFAULT_SIGNAL_WINDOW_SEC

if "signal_track_sec" not in st.session_state:
    st.session_state.signal_track_sec = DEFAULT_SIGNAL_TRACK_SEC

if "signal_volume_ratio" not in st.session_state:
    st.session_state.signal_volume_ratio = DEFAULT_SIGNAL_VOLUME_RATIO

if "signal_price_move_pct" not in st.session_state:
    st.session_state.signal_price_move_pct = DEFAULT_SIGNAL_PRICE_MOVE_PCT

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

if "signal_toast_keys" not in st.session_state:
    st.session_state.signal_toast_keys = set()

if "_signal_toast_messages" not in st.session_state:
    st.session_state._signal_toast_messages = []


def show_pending_toasts():
    if "_quick_add_success_message" in st.session_state:
        st.toast(st.session_state._quick_add_success_message, duration="long")
        del st.session_state._quick_add_success_message

    messages = st.session_state.get("_signal_toast_messages", [])
    if messages:
        for msg in messages[-5:]:
            st.toast(msg, icon="🚨", duration="long")
        st.session_state._signal_toast_messages = []


def queue_signal_toast(group_name, code, stock_name, signal, change_pct):
    if not signal or not signal.get("active"):
        return

    signal_key = signal.get("signal_key")

    if not signal_key:
        return

    if signal_key in st.session_state.signal_toast_keys:
        return

    signal_time = signal.get("time")
    time_text = signal_time.strftime("%H:%M:%S") if hasattr(signal_time, "strftime") else "--:--:--"
    current_price = signal.get("current_price")
    price_text = format_price_value(current_price)

    msg = (
        f"🚨 量價異常訊號\n"
        f"{group_name}｜{code} {stock_name}\n"
        f"{signal.get('text')}\n"
        f"現價：{price_text}｜漲幅：{format_pct_value(change_pct)}｜時間：{time_text}"
    )

    st.session_state._signal_toast_messages.append(msg)
    st.session_state.signal_toast_keys.add(signal_key)

    if len(st.session_state.signal_toast_keys) > 500:
        st.session_state.signal_toast_keys = set(list(st.session_state.signal_toast_keys)[-300:])


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
        st.session_state.symbols_text_area = "\n".join(
            st.session_state.stock_groups.get(pending_group, [])
        )


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
        st.sidebar.error("富邦 SDK 未載入，無法監控盤中成交。")
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
                st.session_state.signal_toast_keys = set()
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

    pin_input = st.sidebar.text_input(
        "請輸入 PIN 碼以編輯分組",
        type="password",
        key="group_edit_pin_input",
    )

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
        st.selectbox(
            "選擇分類",
            options=group_names,
            key="selected_group_editor",
            on_change=sync_editor_fields_from_selected_group,
        )

        selected_group = st.session_state.selected_group_editor

        new_group_name = st.text_input(
            "分類名稱（可修改）",
            key="rename_group_input",
            on_change=enter_edit_mode,
        )

        symbols_text = st.text_area(
            "股票清單（每行一檔，或逗號分隔）",
            height=180,
            key="symbols_text_area",
            on_change=enter_edit_mode,
        )

        st.markdown("### ⚡ 快速新增股票")

        quick_input = st.text_input(
            "輸入股票代碼或名稱",
            key="quick_add_symbol_input",
            on_change=enter_edit_mode,
        )

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
                        if k == selected_group:
                            updated[new_name] = normalize_symbols_from_text(symbols_text)
                        else:
                            updated[k] = v

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
        export_json = json.dumps(
            st.session_state.stock_groups,
            ensure_ascii=False,
            indent=2,
        )

        st.download_button(
            "⬇️ 匯出目前分組 JSON",
            data=export_json,
            file_name="stock_groups.json",
            mime="application/json",
            width="stretch",
        )

        uploaded_file = st.file_uploader("上傳股票分組 JSON", type=["json"])

        if uploaded_file is not None and st.button("📥 匯入並覆蓋目前分組", width="stretch"):
            try:
                data = json.loads(uploaded_file.read().decode("utf-8"))

                if not isinstance(data, dict) or not data:
                    raise ValueError("JSON 最外層必須是非空物件")

                save_backup_snapshot(st.session_state.stock_groups)

                validated = {
                    str(k).strip(): normalize_symbols_from_text(
                        "\n".join(v) if isinstance(v, list) else str(v)
                    )
                    for k, v in data.items()
                }

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
        st.markdown("## ⚡ 盤中即時成交監控")
else:
    st.markdown("## ⚡ 盤中即時成交監控")


control_col1, control_col2, control_col3, control_col4, control_col5, control_col6, control_col7, control_col8 = st.columns(
    [1, 1, 1, 1, 1, 1, 1, 1]
)

with control_col1:
    if st.button("🔄 手動刷新畫面", width="stretch"):
        st.rerun()

with control_col2:
    st.toggle("⏱️ 自動刷新", key="auto_refresh_enabled")

with control_col3:
    st.number_input(
        "刷新秒數",
        min_value=1,
        max_value=60,
        step=1,
        key="refresh_sec",
    )

with control_col4:
    pct_threshold = st.number_input(
        "漲幅門檻 (%)",
        min_value=0.0,
        max_value=10.0,
        value=5.0,
        step=0.5,
    )

with control_col5:
    st.number_input(
        "量能視窗秒數",
        min_value=5,
        max_value=120,
        step=5,
        key="signal_window_sec",
        help="預設 30 秒；用來比較目前視窗成交量與上一個視窗成交量。",
    )

with control_col6:
    st.number_input(
        "追蹤高低點秒數",
        min_value=10,
        max_value=300,
        step=10,
        key="signal_track_sec",
        help="預設 60 秒；用來追蹤最近一分鐘低點 / 高點。",
    )

with control_col7:
    st.number_input(
        "量比門檻",
        min_value=0.1,
        max_value=10.0,
        step=0.1,
        key="signal_volume_ratio",
        help="預設 1.3；目前 30 秒成交量需大於上一個 30 秒成交量的 1.3 倍。",
    )

with control_col8:
    st.number_input(
        "價格變動門檻 (%)",
        min_value=0.1,
        max_value=10.0,
        step=0.1,
        key="signal_price_move_pct",
        help="預設 2%；符合拉抬或下跌幅度時發出訊號。",
    )


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

st.info(
    "即時價由富邦 WebSocket trades 抓取；"
    "最新單筆優先使用富邦 trades 的 size 單筆成交量；若無 size 才用累積量差值；"
    "漲幅% = 富邦即時價 ÷ yfinance 昨日收盤價 - 1。"
    "yfinance 昨收每小時更新一次並存入 yf_yesterday_close_cache.json。"
)

st.caption(
    f"✅ 量價訊號條件："
    f"目前 {int(st.session_state.signal_window_sec)} 秒成交量 > 上一個 {int(st.session_state.signal_window_sec)} 秒成交量 × {float(st.session_state.signal_volume_ratio):.1f}，"
    f"且 30 秒內價格變化 ≥ ±{float(st.session_state.signal_price_move_pct):.1f}% "
    f"或最近 {int(st.session_state.signal_track_sec)} 秒高低點反彈 / 回落 ≥ ±{float(st.session_state.signal_price_move_pct):.1f}%。"
)


# =============================================================================
# 整理資料：儀表板與明細表
# =============================================================================
group_tables = {}
dashboard_items = []
recent_signals = []

yf_source_count = {
    "yfinance": 0,
    "cache": 0,
    "stale cache": 0,
    "missing": 0,
}

for group_name, stocks in st.session_state.stock_groups.items():
    rows = []
    pct_hit_count = 0
    known_pct_count = 0
    up_count = 0
    down_count = 0
    signal_count = 0
    top_pct_items = []
    group_signal_messages = []

    for symbol in stocks:
        code = symbol_to_code(symbol)
        stock_name = get_stock_name(symbol)

        tick_status = manager.get_tick_status(symbol) if manager is not None else {}

        tick_vol = tick_status.get("real_tick_volume")
        current_price = tick_status.get("last_ws_price")
        trade_type = tick_status.get("last_trade_type", "-")
        total_buy_vol = int(tick_status.get("total_buy_vol", 0) or 0)
        total_sell_vol = int(tick_status.get("total_sell_vol", 0) or 0)

        signal = manager.get_volume_price_signal(
            symbol,
            window_sec=st.session_state.signal_window_sec,
            track_sec=st.session_state.signal_track_sec,
            volume_ratio_threshold=st.session_state.signal_volume_ratio,
            price_move_pct=st.session_state.signal_price_move_pct,
        ) if manager is not None else {"active": False, "text": "監控中"}

        yesterday_close, close_date, yf_source = get_yfinance_yesterday_close(symbol)

        if yf_source in yf_source_count:
            yf_source_count[yf_source] += 1
        elif yesterday_close is None:
            yf_source_count["missing"] += 1

        change_pct = None

        if current_price is not None and yesterday_close is not None and yesterday_close > 0:
            change_pct = (float(current_price) / float(yesterday_close) - 1) * 100

        if change_pct is not None:
            known_pct_count += 1

            if float(change_pct) > 0:
                up_count += 1
            elif float(change_pct) < 0:
                down_count += 1

            top_pct_items.append({
                "code": code,
                "name": stock_name,
                "pct": float(change_pct),
            })

            if float(change_pct) >= float(pct_threshold):
                pct_hit_count += 1

        if signal.get("active"):
            signal_count += 1

            signal_msg = {
                "group": group_name,
                "code": code,
                "name": stock_name,
                "text": signal.get("text", ""),
                "time": signal.get("time"),
                "pct": change_pct,
                "direction": signal.get("direction", "-"),
            }

            group_signal_messages.append(signal_msg)
            recent_signals.append(signal_msg)

            queue_signal_toast(group_name, code, stock_name, signal, change_pct)

        rows.append({
            "代碼": yahoo_quote_url(symbol),
            "股票名稱": stock_name,
            "量價訊號": signal.get("text", "監控中"),
            "即時價": format_price_value(current_price),
            "漲幅%": format_pct_value(change_pct),
            "最新單筆": "-" if tick_vol is None else f"{tick_vol} 張",
            "內外盤": trade_type,
            "外盤累積": total_buy_vol,
            "內盤累積": total_sell_vol,
            "本30秒量": signal.get("current_volume", 0),
            "前30秒量": signal.get("previous_volume", 0),
            "30秒量比": format_ratio_value(signal.get("volume_ratio")),
            "30秒價格變化": format_signed_pct(signal.get("price_change_30_pct")),
            "1分低點": format_price_value(signal.get("low_1m")),
            "1分高點": format_price_value(signal.get("high_1m")),
            "1分低點拉抬": format_signed_pct(signal.get("rise_from_low_pct")),
            "1分高點下跌": format_signed_pct(signal.get("drop_from_high_pct")),
            "昨收日期": close_date or "-",
            "昨收來源": yf_source,
        })

    top_pct_items.sort(key=lambda x: x["pct"], reverse=True)

    top_pct_text = "｜".join([
        (
            f'<span class="{pct_class(item["pct"])}">'
            f'{escape_html(item["code"])} {escape_html(item["name"])} {item["pct"]:+.2f}%'
            f'</span>'
        )
        for item in top_pct_items[:3]
    ]) or "尚無漲幅資料"

    group_signal_messages.sort(
        key=lambda x: x["time"] or datetime.min.replace(tzinfo=TW_TZ),
        reverse=True,
    )

    signal_text = "<br>".join([
        f'▸ {escape_html(item["code"])} {escape_html(item["name"])}：{escape_html(item["text"])}'
        for item in group_signal_messages[:3]
    ]) or "尚無訊號"

    pct_hit_ratio = (pct_hit_count / len(stocks) * 100) if len(stocks) else 0

    dashboard_items.append({
        "group": group_name,
        "total": len(stocks),
        "pct_hit_count": pct_hit_count,
        "known_pct_count": known_pct_count,
        "pct_hit_ratio": pct_hit_ratio,
        "up_count": up_count,
        "down_count": down_count,
        "signal_count": signal_count,
        "top_pct_text": top_pct_text,
        "signal_text": signal_text,
    })

    group_tables[group_name] = pd.DataFrame(
        rows,
        columns=[
            "代碼",
            "股票名稱",
            "量價訊號",
            "即時價",
            "漲幅%",
            "最新單筆",
            "內外盤",
            "外盤累積",
            "內盤累積",
            "本30秒量",
            "前30秒量",
            "30秒量比",
            "30秒價格變化",
            "1分低點",
            "1分高點",
            "1分低點拉抬",
            "1分高點下跌",
            "昨收日期",
            "昨收來源",
        ],
    )


show_pending_toasts()


# =============================================================================
# 儀表板
# =============================================================================
st.markdown(
    '<div id="dashboard-top" style="scroll-margin-top: 90px;"></div>',
    unsafe_allow_html=True,
)

st.markdown("### 📌 量價訊號儀表板")

st.caption(
    f"漲幅達標門檻：≥ {pct_threshold:.1f}%｜"
    f"量能視窗：{int(st.session_state.signal_window_sec)} 秒｜"
    f"量比門檻：{float(st.session_state.signal_volume_ratio):.1f}x｜"
    f"價格變動門檻：±{float(st.session_state.signal_price_move_pct):.1f}%｜"
    f"高低點追蹤：{int(st.session_state.signal_track_sec)} 秒｜"
    f"yfinance 昨收快取：{YF_CLOSE_CACHE_TTL_SEC // 60} 分鐘"
)

st.caption(
    f"昨收來源統計："
    f"yfinance 即時更新 {yf_source_count['yfinance']} 檔｜"
    f"快取 {yf_source_count['cache']} 檔｜"
    f"舊快取 {yf_source_count['stale cache']} 檔｜"
    f"缺資料 {yf_source_count['missing']} 檔"
)

card_html_parts = ['<div class="dashboard-grid">']

for item in dashboard_items:
    pct_ratio = float(item.get("pct_hit_ratio", 0) or 0)

    if item.get("signal_count", 0) > 0:
        card_class = "dash-card pct-high"
    elif pct_ratio >= 70:
        card_class = "dash-card pct-high"
    elif pct_ratio >= 40:
        card_class = "dash-card pct-mid"
    elif pct_ratio > 0:
        card_class = "dash-card pct-low"
    else:
        card_class = "dash-card pct-zero"

    anchor_id = make_anchor_id(item["group"])

    card_html_parts.append(
        f'#{anchor_id} 明細表">'
        f'<div class="{card_class}">'
        f'<div class="dash-title">{escape_html(item["group"])}</div>'
        f'<div class="dash-big">訊號 {item["signal_count"]} 檔</div>'
        f'<div class="dash-line">漲幅達標（≥{pct_threshold:.1f}%）：'
        f'<b>{item["pct_hit_count"]} / {item["total"]}</b>，比例 <b>{item["pct_hit_ratio"]:.0f}%</b></div>'
        f'<div class="dash-line">🔴 上漲：<b>{item["up_count"]}</b>　'
        f'🟢 下跌：<b>{item["down_count"]}</b></div>'
        f'<div class="dash-small"><b>最新量價訊號</b><br>{item["signal_text"]}</div>'
        f'<div class="dash-small"><b>漲幅排行</b><br>{item["top_pct_text"]}</div>'
        f'</div></a>'
    )

card_html_parts.append("</div>")

st.markdown("".join(card_html_parts), unsafe_allow_html=True)


recent_signals.sort(
    key=lambda x: x["time"] or datetime.min.replace(tzinfo=TW_TZ),
    reverse=True,
)

st.caption(f"本輪掃到量價訊號：{len(recent_signals)} 檔")

if recent_signals:
    st.markdown("#### 🚨 最近量價訊號")
    recent_cols = st.columns(min(3, len(recent_signals)))

    for idx, item in enumerate(recent_signals[:6]):
        with recent_cols[idx % len(recent_cols)]:
            st.warning(
                f"{item['group']}｜{item['code']} {item['name']}\n\n"
                f"{item['text']}"
            )

st.divider()


# =============================================================================
# 明細表
# =============================================================================
for group_name, display_df in group_tables.items():
    anchor_id = make_anchor_id(group_name)

    st.markdown(
        f'<div id="{anchor_id}" style="scroll-margin-top: 90px;"></div>',
        unsafe_allow_html=True,
    )

    table_header_col1, table_header_col2 = st.columns([8, 2])

    with table_header_col1:
        st.subheader(f"【{group_name}】({len(display_df)}檔)")

    with table_header_col2:
        st.markdown(
            '<div style="text-align:right; padding-top: 0.6rem;">'
            '#dashboard-top⬆️ 返回儀表板</a>'
            '</div>',
            unsafe_allow_html=True,
        )

    st.dataframe(
        display_df,
        width="stretch",
        hide_index=True,
        column_config={
            "代碼": st.column_config.LinkColumn(
                "代碼",
                help="點擊前往 Yahoo 台股個股頁",
                display_text=r"https://tw.stock.yahoo.com/quote/(.*)",
            ),
            "股票名稱": st.column_config.TextColumn("股票名稱"),
            "量價訊號": st.column_config.TextColumn(
                "量價訊號",
                help="條件1：30秒量比放大且30秒內漲跌達門檻；條件2：30秒量比放大且1分鐘高低點突破達門檻。",
            ),
            "即時價": st.column_config.TextColumn(
                "即時價",
                help="由富邦 WebSocket trades 即時成交價取得",
            ),
            "漲幅%": st.column_config.TextColumn(
                "漲幅%",
                help="富邦 WebSocket 即時價 / yfinance 昨日收盤價 - 1",
            ),
            "最新單筆": st.column_config.TextColumn(
                "最新單筆",
                help="優先使用 size；若無 size，則由累積成交量差值換算",
            ),
            "內外盤": st.column_config.TextColumn("內外盤"),
            "外盤累積": st.column_config.NumberColumn("外盤累積"),
            "內盤累積": st.column_config.NumberColumn("內盤累積"),
            "本30秒量": st.column_config.NumberColumn("本30秒量"),
            "前30秒量": st.column_config.NumberColumn("前30秒量"),
            "30秒量比": st.column_config.TextColumn("30秒量比"),
            "30秒價格變化": st.column_config.TextColumn("30秒價格變化"),
            "1分低點": st.column_config.TextColumn("1分低點"),
            "1分高點": st.column_config.TextColumn("1分高點"),
            "1分低點拉抬": st.column_config.TextColumn("1分低點拉抬"),
            "1分高點下跌": st.column_config.TextColumn("1分高點下跌"),
            "昨收日期": st.column_config.TextColumn("昨收日期"),
            "昨收來源": st.column_config.TextColumn("昨收來源"),
        },
    )


# =============================================================================
# Debug
# =============================================================================
with st.sidebar.expander("🔍 WebSocket Debug", expanded=False):
    debug_code = st.text_input("輸入代碼看最後 WS 原始訊息", value="2330")
    msg = manager.get_message(debug_code)

    if msg:
        st.caption(f"時間：{msg['time'].strftime('%Y-%m-%d %H:%M:%S')}")
        st.json(msg["raw"])
    else:
        st.caption("尚未收到此代碼的 WebSocket 訊息")


# =============================================================================
# 自動刷新
# =============================================================================
if (
    st.session_state.auto_refresh_enabled
    and not st.session_state.group_editor_unlocked
    and not st.session_state.editing_mode
):
    time.sleep(int(st.session_state.refresh_sec))
    st.rerun()
