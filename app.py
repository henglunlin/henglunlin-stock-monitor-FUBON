# -*- coding: utf-8 -*-
"""
盤中即時成交監控 - 純富邦 WebSocket 版 (含 Telegram 推送)

目標：
捕捉股價瞬間拉抬時的進場訊號。

核心邏輯：
1. 富邦 WebSocket 負責即時成交價、單筆量、內外盤。
2. yfinance 只抓昨日收盤價，一小時更新一次，存入本地 JSON 快取。
3. 使用「預估 30 秒成交量」提早判斷量能放大，不等完整 30 秒結束。
4. 使用 5 秒 / 10 秒 / 30 秒漲幅捕捉瞬間拉抬。
5. 使用外盤占比確認主動買盤。
6. 使用最近 60 秒高低點追蹤，判斷突破高點或從低點急拉。
7. 分成「預警」與「進場訊號」。
8. 修正儀表板 HTML anchor UI 問題。
9. 整合 Telegram 推播開關與 st.secrets 預設值讀取功能。
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
from streamlit.runtime.scriptrunner import add_script_run_ctx

try:
    from fubon_neo.sdk import FubonSDK
except Exception:
    FubonSDK = None

try:
    import yfinance as yf
except Exception:
    yf = None


# =============================================================================
# App Config
# =============================================================================
st.set_page_config(page_title="盤中瞬間拉抬進場監控", layout="wide")

TW_TZ = ZoneInfo("Asia/Taipei")

GROUP_EDIT_PIN = "1219"
GROUPS_FILE = "stock_groups.json"
BACKUP_DIR = "backups"
STOCK_NAME_FILE = "TWstocklistname.txt"
APP_LOGO = "jerry.jpg"

SIGNAL_LOG_DIR = "signal_logs"
SIGNAL_LOG_LOCK = threading.RLock()

YF_CLOSE_CACHE_FILE = "yf_yesterday_close_cache.json"
YF_CLOSE_CACHE_TTL_SEC = 3600

# 進場訊號預設參數
DEFAULT_ENTRY_BUCKET_SEC = 30
DEFAULT_ENTRY_TRACK_SEC = 60
DEFAULT_ENTRY_VOLUME_RATIO = 1.1
DEFAULT_ENTRY_PRICE_MOVE_PCT = 2.0
DEFAULT_EARLY_5S_PCT = 0.8
DEFAULT_EARLY_10S_PCT = 1.2
DEFAULT_BUY_PRESSURE_RATIO = 0.55
DEFAULT_SIGNAL_COOLDOWN_SEC = 45
DEFAULT_MIN_CURRENT_VOLUME = 20

# --- 🔥 極早期訊號（flash）---
# 依 2026-07-09 實測 log（8883筆訊號）分析：極早期訊號筆數佔全部訊號43%，
# 是量最大的一層，但15分鐘後續最高漲幅變化的中位數是 0.00%、只有23.8%
# 之後續漲、且僅3.9%會在5分鐘內轉化成預警/進場，訊號品質明顯偏弱。
# 觀察觸發當下的單筆跳動幅度中位數已達0.86%（不是卡在門檻邊緣），代表
# 問題不在門檻高低，而在於「單一一筆」本身就容易是雜訊；因此除了把門檻
# 從0.5%略調高到0.8%之外，同時在程式邏輯加上「連續兩筆外盤買」的確認
# （見 _recent_ticks_all_buy），並新增獨立冷卻秒數避免同一波雜訊連環觸發。
DEFAULT_EARLY_2S_PCT = 0.8
DEFAULT_TICK_JUMP_PCT = 0.8
DEFAULT_MIN_TICKS_IN_BUCKET = 2
DEFAULT_FLASH_COOLDOWN_SEC = 45
BUCKET_ELAPSED_FLOOR_SEC = 3.0

# --- 📈 今日最低點反彈偵測 ---
# 依實測 log 分析：低點反彈訊號筆數佔全部訊號46%（單日8883筆中就有4077筆），
# 是最大量的來源；把訊號依實際反彈幅度分桶比對15分鐘後續表現發現，
# 反彈落在1~2%區間的（佔比過半）後續轉正比例只有53%、中位數僅+0.17%，
# 反彈達2~3%以上的區間則明顯轉好（轉正比例62%、中位數+0.40%）。
# 另外同一檔股票相鄰兩次觸發的間隔，有49%落在65秒內（等於冷卻一過就
# 馬上再觸發），確認60秒冷卻太短、造成同一檔股票反覆洗版。
# 因此把門檻從2.0%上調到3.0%、冷卻從60秒拉長到240秒，同時砍掉大量
# 低品質訊號、又保留真正有意義的反彈。
DEFAULT_DAY_LOW_REBOUND_PCT = 3.0
DEFAULT_DAY_LOW_REBOUND_COOLDOWN_SEC = 240

# --- 📊 中期動能偵測（預設：3分鐘內上漲1.5%）---
# 和 5秒/10秒/30秒的瞬間拉抬判斷不同，這是抓「一段時間內持續緩步走高」的走勢。
# 依實測 log 分析：這是四層中品質最好的獨立訊號——15分鐘後續轉正比例
# 達67.4%（僅次於進場/預警的72%），且21.5%會在5分鐘內轉化成預警/進場
# （遠高於極早期的3.9%、低點反彈的9.1%），維持原本的視窗與漲幅門檻，
# 只把冷卻秒數從60小幅拉長到90秒，降低少量重複洗版但不影響原有品質。
DEFAULT_MOMENTUM_WINDOW_SEC = 180
DEFAULT_MOMENTUM_WINDOW_PCT = 1.5
DEFAULT_MOMENTUM_COOLDOWN_SEC = 90

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

# =============================================================================
# Secrets & Telegram 設定
# =============================================================================
def get_secret_or_default(key: str, default: str = "") -> str:
    try:
        if key in st.secrets:
            return st.secrets[key]
        if "telegram" in st.secrets and key in st.secrets["telegram"]:
            return st.secrets["telegram"][key]
    except Exception:
        pass
    return os.environ.get(key, default)

TELEGRAM_BOT_TOKEN = get_secret_or_default("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = get_secret_or_default("TELEGRAM_CHAT_ID", "")

# =============================================================================
# CSS
# =============================================================================
st.markdown(
    """
    <style>
    html { scroll-behavior: smooth; }

    .dashboard-grid {
        display:grid;
        grid-template-columns:repeat(4,minmax(260px,1fr));
        gap:14px;
        margin:14px 0 22px 0;
    }

    .dashboard-link,
    .dashboard-link:link,
    .dashboard-link:visited,
    .dashboard-link:hover,
    .dashboard-link:active {
        text-decoration:none !important;
        color:inherit !important;
        display:block !important;
    }

    .dash-card {
        border:1px solid #91d5ff;
        border-radius:14px;
        padding:16px 18px;
        min-height:210px;
        background:#f0f9ff;
        box-shadow:0 1px 3px rgba(0,0,0,.08);
        color:#111827;
        cursor:pointer;
        transition:transform .12s ease, box-shadow .12s ease;
    }

    .dash-card:hover {
        transform:translateY(-2px);
        box-shadow:0 6px 16px rgba(0,0,0,.18);
    }

    .dash-card.normal {
        border-color:#91d5ff;
        background:#f0f9ff;
    }

    .dash-card.warn {
        border-color:#ffd666;
        background:#fffbe6;
    }

    .dash-card.entry {
        border-color:#ff4d4f;
        background:#fff1f0;
        box-shadow:0 1px 10px rgba(207,19,34,.20);
    }

    .dash-card.idle {
        border-color:#d9d9d9;
        background:#fafafa;
    }

    .dash-card.flash {
        border-color:#ffa940;
        background:#fff7e6;
        box-shadow:0 1px 8px rgba(250,140,22,.18);
    }

    .dash-card.daylow {
        border-color:#36cfc9;
        background:#e6fffb;
        box-shadow:0 1px 8px rgba(19,194,194,.18);
    }

    .dash-card.momentum {
        border-color:#9254de;
        background:#f9f0ff;
        box-shadow:0 1px 8px rgba(114,46,209,.18);
    }

    .dash-title {
        font-weight:900;
        font-size:18px;
        margin-bottom:8px;
        color:#111827;
    }

    .dash-big {
        font-size:30px;
        font-weight:950;
        margin:4px 0 10px 0;
        color:#111827 !important;
        letter-spacing:.3px;
    }

    .dash-line {
        font-size:14px;
        line-height:1.65;
        color:#111827;
    }

    .dash-small {
        font-size:12px;
        color:#374151;
        margin-top:9px;
        border-top:1px solid rgba(0,0,0,.12);
        padding-top:8px;
        line-height:1.55;
    }

    .up-text {
        color:#cf1322;
        font-weight:800;
    }

    .down-text {
        color:#389e0d;
        font-weight:800;
    }

    .flat-text {
        color:#6b7280;
        font-weight:800;
    }

    .return-link-wrap {
        text-align:right;
        padding-top:0.6rem;
    }

    .return-link-wrap a {
        color:#1677ff;
        font-weight:700;
        text-decoration:none;
    }

    .return-link-wrap a:hover {
        text-decoration:underline;
    }

    @media (max-width:1400px) {
        .dashboard-grid {
            grid-template-columns:repeat(2,minmax(260px,1fr));
        }
    }

    @media (max-width:760px) {
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
def symbol_to_code(symbol: str) -> str:
    return str(symbol).strip().upper().split(".")[0]


def yahoo_quote_url(symbol: str) -> str:
    code = symbol_to_code(symbol)
    return f"https://tw.stock.yahoo.com/quote/{code}"


def make_anchor_id(group_name: str) -> str:
    anchor = re.sub(r"[^0-9A-Za-z一-鿿]+", "-", str(group_name)).strip("-")
    return f"group-{anchor or 'default'}"


def escape_html(text_value):
    return (
        str(text_value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


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


def format_percent_ratio(value):
    if value is None:
        return "-"
    try:
        return f"{float(value) * 100:.0f}%"
    except Exception:
        return "-"


def format_seconds_as_label(total_seconds):
    """
    把秒數轉成好讀的標籤：60的整數倍顯示「N分鐘」，否則顯示「N秒」。
    用在「60秒高低點」這類欄位名稱上，讓標籤跟著使用者調整的秒數走，
    而不是永遠寫死顯示 60。
    """
    try:
        total_seconds = int(total_seconds)
    except Exception:
        return "-"

    if total_seconds > 0 and total_seconds % 60 == 0:
        return f"{total_seconds // 60}分鐘"

    return f"{total_seconds}秒"


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


# =============================================================================
# yfinance 昨收快取
# =============================================================================
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

    def _default_status(self):
        return {
            "last_ws_price": None,
            "last_cumulative_volume": None,
            "real_tick_volume": None,
            "last_trade_type": "-",
            "total_buy_vol": 0,
            "total_sell_vol": 0,
            "recent_ticks": [],
            "price_points": [],
            "day_low": None,
            "day_low_date": None,
            "day_low_at": None,
            "entry_state": {
                "armed": False,
                "armed_at": None,
                "armed_low": None,
                "armed_high": None,
                "last_signal_at": None,
            },
            "day_low_rebound_state": {
                "last_signal_at": None,
                "last_signal_day_low": None, # 新增：記錄上次觸發的日低點
            },
            "momentum_state": {
                "last_signal_at": None,
            },
            "flash_state": {
                "last_signal_at": None,
            },
        }

    def _on_message(self, message):
        msg = self._parse_message(message)
        now = datetime.now(TW_TZ)

        # 💡 新增：過濾掉 09:00 前的「盤前試撮」資料，避免污染低點與量能
        if now.hour < 9:
            # 為了讓 UI 儀表板仍能顯示連線存活狀態，可選擇更新最後訊息時間，但不處理實質數據
            with self.lock:
                self.last_message_at = now
            return

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

            status = self.tick_status.get(symbol, self._default_status())

            if "entry_state" not in status:
                status["entry_state"] = self._default_status()["entry_state"]

            if "day_low_rebound_state" not in status:
                status["day_low_rebound_state"] = self._default_status()["day_low_rebound_state"]

            if "momentum_state" not in status:
                status["momentum_state"] = self._default_status()["momentum_state"]

            if "flash_state" not in status:
                status["flash_state"] = self._default_status()["flash_state"]

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
                    and (now - p.get("time")).total_seconds() <= 400
                ][-1500:]

                today_str = now.strftime("%Y%m%d")
                existing_day_low = status.get("day_low")
                existing_day_low_date = status.get("day_low_date")

                if existing_day_low is None or existing_day_low_date != today_str:
                    status["day_low"] = float(price)
                    status["day_low_date"] = today_str
                    status["day_low_at"] = now
                elif float(price) < float(existing_day_low):
                    status["day_low"] = float(price)
                    status["day_low_at"] = now

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
                ][-1500:]

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
            return copy.deepcopy(self.tick_status.get(code, self._default_status()))

    def _sum_tick_volume(self, ticks, start_ts, end_ts, trade_type=None):
        total = 0

        for tick in ticks:
            tick_time = tick.get("time")

            if not hasattr(tick_time, "timestamp"):
                continue

            ts = tick_time.timestamp()

            if start_ts <= ts < end_ts:
                if trade_type is None or tick.get("type") == trade_type:
                    try:
                        total += int(tick.get("volume") or 0)
                    except Exception:
                        pass

        return total

    def _price_at_or_before(self, price_points, target_ts):
        before = None
        earliest = None

        for point in price_points:
            point_time = point.get("time")
            price = self._safe_float(point.get("price"))

            if not hasattr(point_time, "timestamp") or price is None or price <= 0:
                continue

            ts = point_time.timestamp()

            if earliest is None or ts < earliest.get("time").timestamp():
                earliest = point

            if ts <= target_ts:
                if before is None or ts > before.get("time").timestamp():
                    before = point

        chosen = before if before is not None else earliest

        if chosen is None:
            return None

        return self._safe_float(chosen.get("price"))

    def _price_change_from_seconds(self, price_points, current_price, seconds):
        if current_price is None:
            return None

        now = datetime.now(TW_TZ)
        target_ts = now.timestamp() - seconds
        base_price = self._price_at_or_before(price_points, target_ts)

        if base_price is None or base_price <= 0:
            return None

        return (float(current_price) / float(base_price) - 1) * 100

    def _last_tick_jump_pct(self, recent_ticks, current_price):
        if current_price is None:
            return None

        valid_ticks = [
            t for t in recent_ticks
            if hasattr(t.get("time"), "timestamp")
            and self._safe_float(t.get("price")) is not None
            and self._safe_float(t.get("price")) > 0
        ]

        if len(valid_ticks) < 2:
            return None

        valid_ticks.sort(key=lambda x: x.get("time"))
        prev_price = self._safe_float(valid_ticks[-2].get("price"))

        if prev_price is None or prev_price <= 0:
            return None

        return (float(current_price) / float(prev_price) - 1) * 100

    def _recent_ticks_all_buy(self, recent_ticks, min_consecutive=2):
        valid_ticks = [
            t for t in recent_ticks
            if hasattr(t.get("time"), "timestamp")
        ]

        if len(valid_ticks) < min_consecutive:
            return True

        valid_ticks.sort(key=lambda x: x.get("time"))
        last_n = valid_ticks[-min_consecutive:]

        return all(t.get("type") == "外盤(買)" for t in last_n)

    def get_entry_signal(
        self,
        symbol: str,
        bucket_sec: int = DEFAULT_ENTRY_BUCKET_SEC,
        track_sec: int = DEFAULT_ENTRY_TRACK_SEC,
        volume_ratio_threshold: float = DEFAULT_ENTRY_VOLUME_RATIO,
        price_move_pct: float = DEFAULT_ENTRY_PRICE_MOVE_PCT,
        early_5s_pct: float = DEFAULT_EARLY_5S_PCT,
        early_10s_pct: float = DEFAULT_EARLY_10S_PCT,
        buy_pressure_ratio_threshold: float = DEFAULT_BUY_PRESSURE_RATIO,
        cooldown_sec: int = DEFAULT_SIGNAL_COOLDOWN_SEC,
        min_current_volume: int = DEFAULT_MIN_CURRENT_VOLUME,
        early_2s_pct: float = DEFAULT_EARLY_2S_PCT,
        tick_jump_pct: float = DEFAULT_TICK_JUMP_PCT,
        min_ticks_in_bucket: int = DEFAULT_MIN_TICKS_IN_BUCKET,
        flash_cooldown_sec: int = DEFAULT_FLASH_COOLDOWN_SEC,
    ):
        code = symbol_to_code(symbol)
        now = datetime.now(TW_TZ)
        now_ts = now.timestamp()

        try:
            bucket_sec = max(5, int(bucket_sec))
        except Exception:
            bucket_sec = DEFAULT_ENTRY_BUCKET_SEC

        try:
            track_sec = max(bucket_sec, int(track_sec))
        except Exception:
            track_sec = DEFAULT_ENTRY_TRACK_SEC

        try:
            volume_ratio_threshold = max(0.1, float(volume_ratio_threshold))
        except Exception:
            volume_ratio_threshold = DEFAULT_ENTRY_VOLUME_RATIO

        try:
            price_move_pct = max(0.1, float(price_move_pct))
        except Exception:
            price_move_pct = DEFAULT_ENTRY_PRICE_MOVE_PCT

        try:
            early_5s_pct = max(0.1, float(early_5s_pct))
        except Exception:
            early_5s_pct = DEFAULT_EARLY_5S_PCT

        try:
            early_10s_pct = max(0.1, float(early_10s_pct))
        except Exception:
            early_10s_pct = DEFAULT_EARLY_10S_PCT

        try:
            buy_pressure_ratio_threshold = max(0.0, min(1.0, float(buy_pressure_ratio_threshold)))
        except Exception:
            buy_pressure_ratio_threshold = DEFAULT_BUY_PRESSURE_RATIO

        try:
            cooldown_sec = max(0, int(cooldown_sec))
        except Exception:
            cooldown_sec = DEFAULT_SIGNAL_COOLDOWN_SEC

        try:
            min_current_volume = max(0, int(min_current_volume))
        except Exception:
            min_current_volume = DEFAULT_MIN_CURRENT_VOLUME

        try:
            early_2s_pct = max(0.05, float(early_2s_pct))
        except Exception:
            early_2s_pct = DEFAULT_EARLY_2S_PCT

        try:
            tick_jump_pct = max(0.05, float(tick_jump_pct))
        except Exception:
            tick_jump_pct = DEFAULT_TICK_JUMP_PCT

        try:
            min_ticks_in_bucket = max(1, int(min_ticks_in_bucket))
        except Exception:
            min_ticks_in_bucket = DEFAULT_MIN_TICKS_IN_BUCKET

        try:
            flash_cooldown_sec = max(0, int(flash_cooldown_sec))
        except Exception:
            flash_cooldown_sec = DEFAULT_FLASH_COOLDOWN_SEC

        with self.lock:
            status = copy.deepcopy(self.tick_status.get(code, self._default_status()))

        current_price = status.get("last_ws_price")
        recent_ticks = status.get("recent_ticks", [])
        price_points = status.get("price_points", [])
        entry_state = status.get("entry_state") or self._default_status()["entry_state"]
        flash_state = status.get("flash_state") or self._default_status()["flash_state"]

        bucket_start_ts = int(now_ts // bucket_sec) * bucket_sec
        previous_bucket_start_ts = bucket_start_ts - bucket_sec

        elapsed_in_bucket = max(BUCKET_ELAPSED_FLOOR_SEC, now_ts - bucket_start_ts)

        current_volume = self._sum_tick_volume(
            recent_ticks,
            bucket_start_ts,
            now_ts + 0.001,
        )

        ticks_in_bucket = sum(
            1 for t in recent_ticks
            if hasattr(t.get("time"), "timestamp")
            and bucket_start_ts <= t.get("time").timestamp() < now_ts + 0.001
        )

        previous_volume = self._sum_tick_volume(
            recent_ticks,
            previous_bucket_start_ts,
            bucket_start_ts,
        )

        current_buy_volume = self._sum_tick_volume(
            recent_ticks,
            bucket_start_ts,
            now_ts + 0.001,
            trade_type="外盤(買)",
        )

        projected_bucket_volume = current_volume / elapsed_in_bucket * bucket_sec

        volume_ratio = None
        volume_ok = False
        enough_ticks = ticks_in_bucket >= min_ticks_in_bucket

        if previous_volume > 0:
            volume_ratio = projected_bucket_volume / previous_volume
            volume_ok = (
                volume_ratio >= volume_ratio_threshold
                and current_volume >= min_current_volume
                and enough_ticks
            )
        elif current_volume >= min_current_volume and enough_ticks:
            volume_ok = True

        buy_pressure_ratio = None
        buy_pressure_ok = False

        if current_volume > 0:
            buy_pressure_ratio = current_buy_volume / current_volume
            buy_pressure_ok = buy_pressure_ratio >= buy_pressure_ratio_threshold

        price_change_2s = self._price_change_from_seconds(price_points, current_price, 2)
        price_change_5s = self._price_change_from_seconds(price_points, current_price, 5)
        price_change_10s = self._price_change_from_seconds(price_points, current_price, 10)
        price_change_30s = self._price_change_from_seconds(price_points, current_price, bucket_sec)

        last_tick_jump_pct = self._last_tick_jump_pct(recent_ticks, current_price)

        short_momentum_ok = (
            (price_change_2s is not None and price_change_2s >= early_2s_pct)
            or (price_change_5s is not None and price_change_5s >= early_5s_pct)
            or (price_change_10s is not None and price_change_10s >= early_10s_pct)
            or (price_change_30s is not None and price_change_30s >= price_move_pct)
        )

        track_start_ts = now_ts - track_sec
        track_prices = []

        for point in price_points:
            point_time = point.get("time")
            price = self._safe_float(point.get("price"))

            if not hasattr(point_time, "timestamp") or price is None or price <= 0:
                continue

            if track_start_ts <= point_time.timestamp() <= now_ts:
                track_prices.append(price)

        low_track = min(track_prices) if track_prices else None
        high_track = max(track_prices) if track_prices else None

        rise_from_low_pct = None
        drop_from_high_pct = None
        near_high_breakout = False

        if current_price is not None and low_track is not None and low_track > 0:
            rise_from_low_pct = (float(current_price) / float(low_track) - 1) * 100

        if current_price is not None and high_track is not None and high_track > 0:
            drop_from_high_pct = (float(current_price) / float(high_track) - 1) * 100
            near_high_breakout = float(current_price) >= float(high_track) * 0.998

        low_rebound_ok = (
            rise_from_low_pct is not None
            and rise_from_low_pct >= price_move_pct
        )

        position_ok = near_high_breakout or low_rebound_ok

        armed = bool(entry_state.get("armed", False))
        armed_at = entry_state.get("armed_at")
        last_signal_at = entry_state.get("last_signal_at")

        cooldown_ok = True

        if hasattr(last_signal_at, "timestamp"):
            cooldown_ok = (now - last_signal_at).total_seconds() >= cooldown_sec

        if volume_ok and not armed:
            entry_state["armed"] = True
            entry_state["armed_at"] = now
            entry_state["armed_low"] = low_track
            entry_state["armed_high"] = high_track

        if armed and hasattr(armed_at, "timestamp"):
            armed_age = (now - armed_at).total_seconds()

            if armed_age > track_sec:
                entry_state["armed"] = False
                entry_state["armed_at"] = None
                entry_state["armed_low"] = None
                entry_state["armed_high"] = None

        last_trade_type = status.get("last_trade_type", "-")

        warning_active = (
            volume_ok
            and buy_pressure_ok
            and (
                (price_change_2s is not None and price_change_2s >= early_2s_pct)
                or (price_change_5s is not None and price_change_5s >= early_5s_pct)
                or (price_change_10s is not None and price_change_10s >= early_10s_pct)
            )
        )

        entry_active = (
            volume_ok
            and buy_pressure_ok
            and short_momentum_ok
            and position_ok
            and cooldown_ok
        )

        flash_last_signal_at = flash_state.get("last_signal_at")
        flash_cooldown_ok = True

        if hasattr(flash_last_signal_at, "timestamp"):
            flash_cooldown_ok = (now - flash_last_signal_at).total_seconds() >= flash_cooldown_sec

        flash_confirmed = self._recent_ticks_all_buy(recent_ticks, min_consecutive=2)

        flash_active = (
            not entry_active
            and not warning_active
            and last_trade_type == "外盤(買)"
            and flash_confirmed
            and flash_cooldown_ok
            and (
                (last_tick_jump_pct is not None and last_tick_jump_pct >= tick_jump_pct)
                or (price_change_2s is not None and price_change_2s >= early_2s_pct)
            )
        )

        if flash_active:
            flash_state["last_signal_at"] = now

            with self.lock:
                self.tick_status.setdefault(code, self._default_status())["flash_state"] = flash_state

        if entry_active:
            entry_state["last_signal_at"] = now
            entry_state["armed"] = False
            entry_state["armed_at"] = None
            entry_state["armed_low"] = None
            entry_state["armed_high"] = None

        with self.lock:
            self.tick_status.setdefault(code, self._default_status())["entry_state"] = entry_state

        if entry_active:
            text = (
                f"🚀 進場訊號｜"
                f"預估{bucket_sec}秒量 {int(projected_bucket_volume)} / 前{bucket_sec}秒量 {int(previous_volume)}｜"
                f"量比 {format_ratio_value(volume_ratio)}｜"
                f"外盤占比 {format_percent_ratio(buy_pressure_ratio)}｜"
                f"2秒 {format_signed_pct(price_change_2s)}｜"
                f"5秒 {format_signed_pct(price_change_5s)}｜"
                f"10秒 {format_signed_pct(price_change_10s)}｜"
                f"{bucket_sec}秒 {format_signed_pct(price_change_30s)}｜"
                f"{track_sec}秒低點拉抬 {format_signed_pct(rise_from_low_pct)}"
            )
            signal_level = "entry"

        elif warning_active:
            text = (
                f"⚠️ 預警｜"
                f"預估{bucket_sec}秒量 {int(projected_bucket_volume)} / 前{bucket_sec}秒量 {int(previous_volume)}｜"
                f"量比 {format_ratio_value(volume_ratio)}｜"
                f"外盤占比 {format_percent_ratio(buy_pressure_ratio)}｜"
                f"2秒 {format_signed_pct(price_change_2s)}｜"
                f"5秒 {format_signed_pct(price_change_5s)}｜"
                f"10秒 {format_signed_pct(price_change_10s)}"
            )
            signal_level = "warning"

        elif flash_active:
            text = (
                f"🔥 極早期訊號｜"
                f"單筆跳動 {format_signed_pct(last_tick_jump_pct)}｜"
                f"2秒 {format_signed_pct(price_change_2s)}｜"
                f"本段筆數 {ticks_in_bucket}｜"
                f"外盤買進中，量能與突破條件尚未全部達標，僅供提早注意"
            )
            signal_level = "flash"

        else:
            text = "監控中"
            signal_level = "none"

        signal_key = (
            f"{code}_{int(now_ts // 5)}_{signal_level}_"
            f"cv{current_volume}_pv{previous_volume}_p{current_price}"
        )

        return {
            "active": bool(entry_active),
            "warning": bool(warning_active),
            "flash": bool(flash_active),
            "signal_level": signal_level,
            "text": text,
            "time": now,
            "signal_key": signal_key,
            "current_price": current_price,
            "current_volume": int(current_volume),
            "previous_volume": int(previous_volume),
            "projected_bucket_volume": int(projected_bucket_volume),
            "ticks_in_bucket": int(ticks_in_bucket),
            "volume_ratio": volume_ratio,
            "buy_pressure_ratio": buy_pressure_ratio,
            "price_change_2s": price_change_2s,
            "price_change_5s": price_change_5s,
            "price_change_10s": price_change_10s,
            "price_change_30s": price_change_30s,
            "last_tick_jump_pct": last_tick_jump_pct,
            "low_track": low_track,
            "high_track": high_track,
            "rise_from_low_pct": rise_from_low_pct,
            "drop_from_high_pct": drop_from_high_pct,
            "near_high_breakout": near_high_breakout,
            "volume_ok": volume_ok,
            "buy_pressure_ok": buy_pressure_ok,
            "short_momentum_ok": short_momentum_ok,
            "position_ok": position_ok,
            "bucket_sec": bucket_sec,
            "track_sec": track_sec,
        }

    def get_day_low_rebound_signal(
        self,
        symbol: str,
        rebound_pct_threshold: float = DEFAULT_DAY_LOW_REBOUND_PCT,
        cooldown_sec: int = DEFAULT_DAY_LOW_REBOUND_COOLDOWN_SEC,
    ):
        code = symbol_to_code(symbol)
        now = datetime.now(TW_TZ)

        try:
            rebound_pct_threshold = max(0.1, float(rebound_pct_threshold))
        except Exception:
            rebound_pct_threshold = DEFAULT_DAY_LOW_REBOUND_PCT

        try:
            cooldown_sec = max(0, int(cooldown_sec))
        except Exception:
            cooldown_sec = DEFAULT_DAY_LOW_REBOUND_COOLDOWN_SEC

        with self.lock:
            status = copy.deepcopy(self.tick_status.get(code, self._default_status()))

        current_price = status.get("last_ws_price")
        day_low = status.get("day_low")
        day_low_at = status.get("day_low_at")
        
        # 取得紀錄的狀態，包含上次觸發時的日低點
        rebound_state = status.get("day_low_rebound_state") or {"last_signal_at": None, "last_signal_day_low": None}
        last_signal_at = rebound_state.get("last_signal_at")
        last_signal_day_low = rebound_state.get("last_signal_day_low")

        rebound_pct = None
        cooldown_ok = True

        if hasattr(last_signal_at, "timestamp"):
            cooldown_ok = (now - last_signal_at).total_seconds() >= cooldown_sec

        active = False

        if current_price is not None and day_low is not None and day_low > 0:
            rebound_pct = (float(current_price) / float(day_low) - 1) * 100
            
            # 【修正邏輯 1】如果價格大幅回落（反彈幅度跌回門檻的一半以下），重置紀錄。
            # 這允許股價在「沒破底」的情況下，若回測低點後再次往上拉抬，依然能精準捕捉雙底反彈。
            if rebound_pct < (rebound_pct_threshold * 0.5):
                rebound_state["last_signal_day_low"] = None
                last_signal_day_low = None
                with self.lock:
                    self.tick_status.setdefault(code, self._default_status())["day_low_rebound_state"] = rebound_state

            # 【修正邏輯 2】確認「目前的日低點」跟「上次發送訊號的日低點」是否不同（或已經被重置）。
            # 藉此防堵股價攻上漲停或維持高檔時，只是因為數值大於門檻，就每 240 秒狂跳洗版。
            is_new_trigger = (last_signal_day_low != day_low)
            
            active = rebound_pct >= rebound_pct_threshold and cooldown_ok and is_new_trigger

        if active:
            rebound_state["last_signal_at"] = now
            rebound_state["last_signal_day_low"] = day_low

            with self.lock:
                self.tick_status.setdefault(code, self._default_status())["day_low_rebound_state"] = rebound_state

        if active:
            text = (
                f"📈 今日低點反彈｜"
                f"今日最低 {format_price_value(day_low)} → 現價 {format_price_value(current_price)}｜"
                f"反彈 {format_signed_pct(rebound_pct)}（門檻 {rebound_pct_threshold:.1f}%）"
            )
            signal_level = "day_low_rebound"
        else:
            text = "尚未觸發"
            signal_level = "none"

        signal_key = f"{code}_daylow_{int(now.timestamp() // 10)}_p{current_price}"

        return {
            "active": bool(active),
            "signal_level": signal_level,
            "text": text,
            "time": now,
            "signal_key": signal_key,
            "current_price": current_price,
            "day_low": day_low,
            "day_low_at": day_low_at,
            "rebound_pct": rebound_pct,
            "rebound_pct_threshold": rebound_pct_threshold,
        }

    def get_window_momentum_signal(
        self,
        symbol: str,
        window_sec: int = DEFAULT_MOMENTUM_WINDOW_SEC,
        pct_threshold: float = DEFAULT_MOMENTUM_WINDOW_PCT,
        cooldown_sec: int = DEFAULT_MOMENTUM_COOLDOWN_SEC,
    ):
        code = symbol_to_code(symbol)
        now = datetime.now(TW_TZ)

        try:
            window_sec = max(10, int(window_sec))
        except Exception:
            window_sec = DEFAULT_MOMENTUM_WINDOW_SEC

        try:
            pct_threshold = max(0.1, float(pct_threshold))
        except Exception:
            pct_threshold = DEFAULT_MOMENTUM_WINDOW_PCT

        try:
            cooldown_sec = max(0, int(cooldown_sec))
        except Exception:
            cooldown_sec = DEFAULT_MOMENTUM_COOLDOWN_SEC

        with self.lock:
            status = copy.deepcopy(self.tick_status.get(code, self._default_status()))

        current_price = status.get("last_ws_price")
        price_points = status.get("price_points", [])
        momentum_state = status.get("momentum_state") or {"last_signal_at": None}
        last_signal_at = momentum_state.get("last_signal_at")

        window_change_pct = self._price_change_from_seconds(price_points, current_price, window_sec)

        cooldown_ok = True

        if hasattr(last_signal_at, "timestamp"):
            cooldown_ok = (now - last_signal_at).total_seconds() >= cooldown_sec

        active = (
            window_change_pct is not None
            and window_change_pct >= pct_threshold
            and cooldown_ok
        )

        if active:
            momentum_state["last_signal_at"] = now

            with self.lock:
                self.tick_status.setdefault(code, self._default_status())["momentum_state"] = momentum_state

        window_label = format_seconds_as_label(window_sec)

        if active:
            text = (
                f"📊 {window_label}內上漲｜"
                f"{window_label}漲幅 {format_signed_pct(window_change_pct)}（門檻 {pct_threshold:.1f}%）｜"
                f"現價 {format_price_value(current_price)}"
            )
            signal_level = "momentum_window"
        else:
            text = "尚未觸發"
            signal_level = "none"

        signal_key = f"{code}_momentum{window_sec}_{int(now.timestamp() // 10)}_p{current_price}"

        return {
            "active": bool(active),
            "signal_level": signal_level,
            "text": text,
            "time": now,
            "signal_key": signal_key,
            "current_price": current_price,
            "window_sec": window_sec,
            "window_label": window_label,
            "window_change_pct": window_change_pct,
            "pct_threshold": pct_threshold,
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
# Telegram 推送功能
# =============================================================================
def send_telegram_message(text: str):
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


def send_telegram_document(file_path: str, caption: str = ""):
    """發送檔案到 Telegram"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption}
    try:
        with open(file_path, "rb") as f:
            files = {"document": f}
            res = requests.post(url, data=payload, files=files, timeout=10)
            if res.status_code != 200:
                st.error(f"Telegram 檔案傳送失敗，API 回傳：{res.text}")
    except Exception as e:
        st.error(f"Telegram 檔案連線失敗: {e}")


def push_telegram_signal(group_name, code, stock_name, signal, change_pct):
    if not st.session_state.tg_push_enabled:
        return

    if not signal:
        return

    level = signal.get("signal_level", "none")
    
    if level not in ("warning", "entry", "day_low_rebound", "momentum_window"):
        return

    signal_key = signal.get("signal_key")
    if not signal_key:
        return

    pushed_keys = st.session_state.telegram_pushed_keys
    if signal_key in pushed_keys:
        return

    pushed_keys.add(signal_key)
    if len(pushed_keys) > 2000:
        st.session_state.telegram_pushed_keys = set(list(pushed_keys)[-1000:])

    signal_time = signal.get("time")
    time_text = signal_time.strftime("%H:%M:%S") if hasattr(signal_time, "strftime") else "--:--:--"
    price_text = format_price_value(signal.get("current_price"))
    pct_text = format_pct_value(change_pct)

    prefix_map = {
        "entry": "🚀 <b>[進場訊號]</b>",
        "warning": "⚠️ <b>[預警]</b>",
        "day_low_rebound": "📈 <b>[今日低點反彈]</b>",
        "momentum_window": "📊 <b>[區間動能]</b>",
    }
    prefix = prefix_map.get(level, "⚠️ <b>[預警]</b>")

    msg = (
        f"{prefix}\n"
        f"<b>{group_name}｜{code} {stock_name}</b>\n"
        f"現價：{price_text}｜漲幅：{pct_text}\n"
        f"時間：{time_text}\n"
        f"------------------\n"
        f"{signal.get('text', '')}"
    )

    t = threading.Thread(target=send_telegram_message, args=(msg,))
    add_script_run_ctx(t)
    t.start()


# =============================================================================
# 每日訊號 Log
# =============================================================================
def get_signal_log_path(for_date=None):
    day = for_date or datetime.now(TW_TZ)
    os.makedirs(SIGNAL_LOG_DIR, exist_ok=True)
    return os.path.join(SIGNAL_LOG_DIR, f"log_{day.strftime('%Y%m%d')}.txt")


def append_signal_log(group_name, code, stock_name, signal, change_pct):
    if not signal:
        return

    level = signal.get("signal_level", "none")

    if level not in ("flash", "warning", "entry", "day_low_rebound", "momentum_window"):
        return

    signal_key = signal.get("signal_key")

    if signal_key:
        logged_keys = st.session_state.setdefault("signal_log_written_keys", set())

        if signal_key in logged_keys:
            return

        logged_keys.add(signal_key)

        if len(logged_keys) > 2000:
            st.session_state.signal_log_written_keys = set(list(logged_keys)[-1000:])

    signal_time = signal.get("time") or datetime.now(TW_TZ)
    time_text = signal_time.strftime("%Y-%m-%d %H:%M:%S") if hasattr(signal_time, "strftime") else "-"

    level_label = {
        "flash": "極早期",
        "warning": "預警",
        "entry": "進場",
        "day_low_rebound": "低點反彈",
        "momentum_window": "區間動能",
    }.get(level, level)
    price_text = format_price_value(signal.get("current_price"))
    pct_text = format_pct_value(change_pct)

    line = (
        f"{time_text} | {level_label} | {group_name} | {code} {stock_name} | "
        f"現價 {price_text} | 漲幅 {pct_text} | {signal.get('text', '')}"
    )

    try:
        log_path = get_signal_log_path(signal_time if hasattr(signal_time, "strftime") else None)

        with SIGNAL_LOG_LOCK:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")

    except Exception as e:
        st.session_state["signal_log_write_error"] = str(e)


def read_signal_log(for_date=None):
    log_path = get_signal_log_path(for_date)

    if not os.path.exists(log_path):
        return "", log_path

    try:
        with open(log_path, "r", encoding="utf-8") as f:
            return f.read(), log_path
    except Exception as e:
        return f"讀取 log 失敗：{e}", log_path


# Session State 初始化
if "auto_refresh_enabled" not in st.session_state:
    st.session_state.auto_refresh_enabled = True

if "refresh_sec" not in st.session_state:
    st.session_state.refresh_sec = 3

if "tg_push_enabled" not in st.session_state:
    st.session_state.tg_push_enabled = False

if "entry_bucket_sec" not in st.session_state:
    st.session_state.entry_bucket_sec = DEFAULT_ENTRY_BUCKET_SEC

if "entry_track_sec" not in st.session_state:
    st.session_state.entry_track_sec = DEFAULT_ENTRY_TRACK_SEC

if "entry_volume_ratio" not in st.session_state:
    st.session_state.entry_volume_ratio = DEFAULT_ENTRY_VOLUME_RATIO

if "entry_price_move_pct" not in st.session_state:
    st.session_state.entry_price_move_pct = DEFAULT_ENTRY_PRICE_MOVE_PCT

if "entry_early_5s_pct" not in st.session_state:
    st.session_state.entry_early_5s_pct = DEFAULT_EARLY_5S_PCT

if "entry_early_10s_pct" not in st.session_state:
    st.session_state.entry_early_10s_pct = DEFAULT_EARLY_10S_PCT

if "entry_buy_pressure_ratio" not in st.session_state:
    st.session_state.entry_buy_pressure_ratio = DEFAULT_BUY_PRESSURE_RATIO

if "entry_cooldown_sec" not in st.session_state:
    st.session_state.entry_cooldown_sec = DEFAULT_SIGNAL_COOLDOWN_SEC

if "entry_min_current_volume" not in st.session_state:
    st.session_state.entry_min_current_volume = DEFAULT_MIN_CURRENT_VOLUME

if "entry_early_2s_pct" not in st.session_state:
    st.session_state.entry_early_2s_pct = DEFAULT_EARLY_2S_PCT

if "entry_tick_jump_pct" not in st.session_state:
    st.session_state.entry_tick_jump_pct = DEFAULT_TICK_JUMP_PCT

if "entry_min_ticks_in_bucket" not in st.session_state:
    st.session_state.entry_min_ticks_in_bucket = DEFAULT_MIN_TICKS_IN_BUCKET

if "entry_flash_cooldown_sec" not in st.session_state:
    st.session_state.entry_flash_cooldown_sec = DEFAULT_FLASH_COOLDOWN_SEC

if "day_low_rebound_pct" not in st.session_state:
    st.session_state.day_low_rebound_pct = DEFAULT_DAY_LOW_REBOUND_PCT

if "day_low_rebound_cooldown_sec" not in st.session_state:
    st.session_state.day_low_rebound_cooldown_sec = DEFAULT_DAY_LOW_REBOUND_COOLDOWN_SEC

if "momentum_window_sec" not in st.session_state:
    st.session_state.momentum_window_sec = DEFAULT_MOMENTUM_WINDOW_SEC

if "momentum_window_pct" not in st.session_state:
    st.session_state.momentum_window_pct = DEFAULT_MOMENTUM_WINDOW_PCT

if "momentum_cooldown_sec" not in st.session_state:
    st.session_state.momentum_cooldown_sec = DEFAULT_MOMENTUM_COOLDOWN_SEC

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

if "entry_signal_toast_keys" not in st.session_state:
    st.session_state.entry_signal_toast_keys = set()

if "_entry_signal_toast_messages" not in st.session_state:
    st.session_state._entry_signal_toast_messages = []

if "telegram_pushed_keys" not in st.session_state:
    st.session_state.telegram_pushed_keys = set()
    
# 新增：用於記錄已發送過的定時 Log 時段，避免重複發送
if "sent_log_slots" not in st.session_state:
    st.session_state.sent_log_slots = set()
if "log_date" not in st.session_state:
    st.session_state.log_date = datetime.now(TW_TZ).date()


def show_pending_toasts():
    if "_quick_add_success_message" in st.session_state:
        st.toast(st.session_state._quick_add_success_message, duration="long")
        del st.session_state._quick_add_success_message

    messages = st.session_state.get("_entry_signal_toast_messages", [])

    if messages:
        for msg in messages[-5:]:
            st.toast(msg, icon="🚀", duration="long")
        st.session_state._entry_signal_toast_messages = []


def queue_entry_signal_toast(group_name, code, stock_name, signal, change_pct):
    if not signal:
        return

    if not signal.get("active") and not signal.get("warning"):
        return

    signal_key = signal.get("signal_key")

    if not signal_key:
        return

    if signal_key in st.session_state.entry_signal_toast_keys:
        return

    signal_time = signal.get("time")
    time_text = signal_time.strftime("%H:%M:%S") if hasattr(signal_time, "strftime") else "--:--:--"
    current_price = signal.get("current_price")
    price_text = format_price_value(current_price)

    prefix_map = {
        "entry": "🚀 進場訊號",
        "warning": "⚠️ 預警",
        "day_low_rebound": "📈 今日低點反彈",
        "momentum_window": "📊 區間動能",
    }
    prefix = prefix_map.get(signal.get("signal_level"), "⚠️ 預警")

    msg = (
        f"{prefix}\n"
        f"{group_name}｜{code} {stock_name}\n"
        f"{signal.get('text')}\n"
        f"現價：{price_text}｜漲幅：{format_pct_value(change_pct)}｜時間：{time_text}"
    )

    st.session_state._entry_signal_toast_messages.append(msg)
    st.session_state.entry_signal_toast_keys.add(signal_key)

    if len(st.session_state.entry_signal_toast_keys) > 700:
        st.session_state.entry_signal_toast_keys = set(list(st.session_state.entry_signal_toast_keys)[-400:])


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
        st.session_state.entry_signal_toast_keys = set()
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
            if st.button("盤中資料歸零", width="stretch"):
                manager.reset_runtime_data()
                st.session_state.entry_signal_toast_keys = set()
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
        st.markdown("## 🚀 盤中瞬間拉抬進場監控")
else:
    st.markdown("## 🚀 盤中瞬間拉抬進場監控")


# =============================================================================
# 主畫面頂端控制列 (僅保留基本操作與漲幅門檻)
# =============================================================================
control_cols = st.columns(5) 

with control_cols[0]:
    if st.button("🔄 手動刷新", width="stretch"):
        st.rerun()

with control_cols[1]:
    st.toggle("⏱️ 自動刷新", key="auto_refresh_enabled")

with control_cols[2]:
    tg_push = st.toggle("📲 Telegram 推播", value=st.session_state.tg_push_enabled, help="必須開啟此選項，機器人才會發送推播")
    if tg_push != st.session_state.tg_push_enabled:
        st.session_state.tg_push_enabled = tg_push
        st.rerun()

with control_cols[3]:
    st.number_input(
        "刷新秒數",
        min_value=1,
        max_value=60,
        step=1,
        key="refresh_sec",
    )

with control_cols[4]:
    pct_threshold = st.number_input(
        "漲幅門檻%",
        min_value=0.0,
        max_value=10.0,
        value=2.0,
        step=0.5,
    )

# =============================================================================
# 左側欄位：參數設定區塊 (合併所有紅框處的設定)
# =============================================================================
with st.sidebar.expander("⚙️ 參數設定", expanded=True):
    
    st.markdown("**🚀 進場預警參數**")
    st.number_input("量能視窗秒", min_value=5, max_value=120, step=5, key="entry_bucket_sec")
    st.number_input("高低點追蹤秒", min_value=10, max_value=300, step=10, key="entry_track_sec")
    st.number_input("量比門檻", min_value=0.1, max_value=10.0, step=0.1, key="entry_volume_ratio")
    st.number_input("價格變動%", min_value=0.1, max_value=10.0, step=0.1, key="entry_price_move_pct")
    st.number_input("外盤占比", min_value=0.0, max_value=1.0, step=0.05, key="entry_buy_pressure_ratio", help="0.55 = 外盤占比 55%")
    st.number_input("最低本段量", min_value=0, max_value=10000, step=10, key="entry_min_current_volume")
    st.number_input("冷卻秒數", min_value=0, max_value=300, step=5, key="entry_cooldown_sec")
    st.number_input("2秒預警%", min_value=0.1, max_value=5.0, step=0.1, key="entry_early_2s_pct", help="極短窗口漲幅門檻，比5秒窗更快抓到瞬間拉抬")
    st.number_input("單筆跳動%", min_value=0.1, max_value=5.0, step=0.1, key="entry_tick_jump_pct", help="最新一筆成交價相較上一筆的漲幅，抓單筆巨量瞬間跳價（🔥極早期訊號用）")
    st.number_input("視窗最少筆數", min_value=1, max_value=20, step=1, key="entry_min_ticks_in_bucket", help="量能視窗內至少要有幾筆成交才算數，避免單一大單誤觸發")
    st.number_input("極早期冷卻秒數", min_value=0, max_value=300, step=5, key="entry_flash_cooldown_sec", help="🔥極早期訊號同一檔股票的最短觸發間隔。實測log顯示單筆跳動很容易連環觸發，加上獨立冷卻可大幅降低洗版")

    st.markdown("---")
    st.markdown("**📈 今日低點反彈提醒**")
    st.caption("純看價格、不看量能")
    st.number_input("低點反彈%", min_value=0.1, max_value=20.0, step=0.5, key="day_low_rebound_pct", help="現價相較今日最低點反彈達此幅度即觸發📈提醒")
    st.number_input("反彈冷卻秒數", min_value=0, max_value=600, step=10, key="day_low_rebound_cooldown_sec", help="同一檔股票的低點反彈提醒最短間隔，避免在低點附近來回震盪時洗版")

    st.markdown("---")
    st.markdown("**📊 區間動能提醒**")
    st.caption("抓一段時間持續走高的走勢")
    st.number_input("動能視窗秒數", min_value=30, max_value=400, step=30, key="momentum_window_sec", help="預設180秒＝3分鐘，可依需求調整觀察窗口長度")
    st.number_input("動能漲幅門檻%", min_value=0.1, max_value=20.0, step=0.1, key="momentum_window_pct", help="視窗內漲幅達此門檻即觸發📊提醒")
    st.number_input("動能冷卻秒數", min_value=0, max_value=600, step=10, key="momentum_cooldown_sec", help="同一檔股票的區間動能提醒最短間隔")


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
    "訊號邏輯（四種判斷標準，彼此獨立，可同時出現）：\n"
    "🔥 極早期訊號：最新一筆連續兩筆都是外盤買，且單筆跳動或2秒漲幅已達標，最快、雜訊也最多，僅供提早注意。\n"
    "⚠️ 預警：量能達標＋外盤占比達標＋2/5/10秒任一短線漲幅達標。\n"
    f"🚀 進場訊號：預警條件全部成立，再加上{int(st.session_state.entry_bucket_sec)}秒漲幅或"
    f"{int(st.session_state.entry_track_sec)}秒突破高點/低點急拉確認，並通過冷卻時間。\n"
    "📈 今日低點反彈：不斷記錄當天最低成交價，現價相較今日最低點反彈達門檻即觸發，純看價格、不看量能，用來抓破底後止穩反彈。\n"
    "📊 區間動能：固定觀察一段時間窗口（預設3分鐘）的漲幅，抓沒有單筆爆量、但持續緩步走高的走勢。\n"
    "🔔 Telegram 推送：推送「預警」「進場訊號」「今日低點反彈」「區間動能」四種提醒至綁定的群組（🔥極早期不推送，僅記錄於log）。\n\n"
    "📊 依 2026-07-09 實測 8883 筆訊號log分析調整：極早期訊號原佔比43%但15分鐘後續轉正比例僅23.8%（中位數0%），"
    "已加強為需連續兩筆買盤＋獨立冷卻；低點反彈原佔比46%但1~2%反彈區間後續轉正僅53%，已將門檻由2.0%提高到3.0%、"
    "冷卻由60秒拉長到240秒（原本49%的重複觸發間隔都在65秒內，等於冷卻一過就馬上再觸發）；"
    "區間動能經驗證是品質最好的獨立訊號（15分鐘後續轉正67.4%，5分鐘內轉化成預警/進場的比例21.5%，遠高於極早期3.9%與低點反彈9.1%），維持原本門檻。"
)

st.caption(
    f"✅ 目前條件：量能視窗 {int(st.session_state.entry_bucket_sec)} 秒｜"
    f"預估量比 ≥ {float(st.session_state.entry_volume_ratio):.2f}x｜"
    f"價格變動 ≥ {float(st.session_state.entry_price_move_pct):.1f}%｜"
    f"2秒預警 ≥ {float(st.session_state.entry_early_2s_pct):.1f}%｜"
    f"5秒預警 ≥ {float(st.session_state.entry_early_5s_pct):.1f}%｜"
    f"10秒預警 ≥ {float(st.session_state.entry_early_10s_pct):.1f}%｜"
    f"單筆跳動 ≥ {float(st.session_state.entry_tick_jump_pct):.1f}%｜"
    f"外盤占比 ≥ {float(st.session_state.entry_buy_pressure_ratio) * 100:.0f}%｜"
    f"高低點追蹤 {int(st.session_state.entry_track_sec)} 秒｜"
    f"最低本段量 {int(st.session_state.entry_min_current_volume)} 張｜"
    f"視窗最少筆數 {int(st.session_state.entry_min_ticks_in_bucket)} 筆｜"
    f"極早期冷卻 {int(st.session_state.entry_flash_cooldown_sec)} 秒｜"
    f"冷卻 {int(st.session_state.entry_cooldown_sec)} 秒｜"
    f"今日低點反彈 ≥ {float(st.session_state.day_low_rebound_pct):.1f}%（冷卻 {int(st.session_state.day_low_rebound_cooldown_sec)} 秒）｜"
    f"區間動能 {format_seconds_as_label(st.session_state.momentum_window_sec)}內 ≥ "
    f"{float(st.session_state.momentum_window_pct):.1f}%（冷卻 {int(st.session_state.momentum_cooldown_sec)} 秒）"
)


# =============================================================================
# 整理資料
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

track_label = format_seconds_as_label(st.session_state.entry_track_sec)
low_col_name = f"{track_label}低點"
high_col_name = f"{track_label}高點"

momentum_window_label = format_seconds_as_label(st.session_state.momentum_window_sec)
momentum_col_name = f"{momentum_window_label}漲幅%"

for group_name, stocks in st.session_state.stock_groups.items():
    rows = []
    pct_hit_count = 0
    known_pct_count = 0
    up_count = 0
    down_count = 0
    warning_count = 0
    entry_count = 0
    flash_count = 0
    day_low_rebound_count = 0
    momentum_count = 0
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

        signal = manager.get_entry_signal(
            symbol,
            bucket_sec=st.session_state.entry_bucket_sec,
            track_sec=st.session_state.entry_track_sec,
            volume_ratio_threshold=st.session_state.entry_volume_ratio,
            price_move_pct=st.session_state.entry_price_move_pct,
            early_5s_pct=st.session_state.entry_early_5s_pct,
            early_10s_pct=st.session_state.entry_early_10s_pct,
            buy_pressure_ratio_threshold=st.session_state.entry_buy_pressure_ratio,
            cooldown_sec=st.session_state.entry_cooldown_sec,
            min_current_volume=st.session_state.entry_min_current_volume,
            early_2s_pct=st.session_state.entry_early_2s_pct,
            tick_jump_pct=st.session_state.entry_tick_jump_pct,
            min_ticks_in_bucket=st.session_state.entry_min_ticks_in_bucket,
            flash_cooldown_sec=st.session_state.entry_flash_cooldown_sec,
        ) if manager is not None else {
            "active": False,
            "warning": False,
            "signal_level": "none",
            "text": "監控中",
        }

        day_low_signal = manager.get_day_low_rebound_signal(
            symbol,
            rebound_pct_threshold=st.session_state.day_low_rebound_pct,
            cooldown_sec=st.session_state.day_low_rebound_cooldown_sec,
        ) if manager is not None else {
            "active": False,
            "signal_level": "none",
            "text": "尚未觸發",
            "day_low": None,
            "rebound_pct": None,
        }

        momentum_signal = manager.get_window_momentum_signal(
            symbol,
            window_sec=st.session_state.momentum_window_sec,
            pct_threshold=st.session_state.momentum_window_pct,
            cooldown_sec=st.session_state.momentum_cooldown_sec,
        ) if manager is not None else {
            "active": False,
            "signal_level": "none",
            "text": "尚未觸發",
            "window_change_pct": None,
        }

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
            entry_count += 1
        elif signal.get("warning"):
            warning_count += 1
        elif signal.get("flash"):
            flash_count += 1

        if day_low_signal.get("active"):
            day_low_rebound_count += 1

        if momentum_signal.get("active"):
            momentum_count += 1

        if signal.get("active") or signal.get("warning") or signal.get("flash"):
            signal_msg = {
                "group": group_name,
                "code": code,
                "name": stock_name,
                "text": signal.get("text", ""),
                "time": signal.get("time"),
                "pct": change_pct,
                "level": signal.get("signal_level", "none"),
            }

            group_signal_messages.append(signal_msg)
            recent_signals.append(signal_msg)
            
            queue_entry_signal_toast(group_name, code, stock_name, signal, change_pct)
            append_signal_log(group_name, code, stock_name, signal, change_pct)
            
            # --- 觸發 Telegram 推送 ---
            push_telegram_signal(group_name, code, stock_name, signal, change_pct)

        if day_low_signal.get("active"):
            day_low_signal_msg = {
                "group": group_name,
                "code": code,
                "name": stock_name,
                "text": day_low_signal.get("text", ""),
                "time": day_low_signal.get("time"),
                "pct": change_pct,
                "level": day_low_signal.get("signal_level", "none"),
            }

            group_signal_messages.append(day_low_signal_msg)
            recent_signals.append(day_low_signal_msg)

            queue_entry_signal_toast(group_name, code, stock_name, day_low_signal, change_pct)
            append_signal_log(group_name, code, stock_name, day_low_signal, change_pct)
            push_telegram_signal(group_name, code, stock_name, day_low_signal, change_pct)

        if momentum_signal.get("active"):
            momentum_signal_msg = {
                "group": group_name,
                "code": code,
                "name": stock_name,
                "text": momentum_signal.get("text", ""),
                "time": momentum_signal.get("time"),
                "pct": change_pct,
                "level": momentum_signal.get("signal_level", "none"),
            }

            group_signal_messages.append(momentum_signal_msg)
            recent_signals.append(momentum_signal_msg)

            queue_entry_signal_toast(group_name, code, stock_name, momentum_signal, change_pct)
            append_signal_log(group_name, code, stock_name, momentum_signal, change_pct)
            push_telegram_signal(group_name, code, stock_name, momentum_signal, change_pct)

        rows.append({
            "代碼": yahoo_quote_url(symbol),
            "股票名稱": stock_name,
            "進場訊號": signal.get("text", "監控中"),
            "即時價": format_price_value(current_price),
            "漲幅%": format_pct_value(change_pct),
            "最新單筆": "-" if tick_vol is None else f"{tick_vol} 張",
            "內外盤": trade_type,
            "外盤累積": total_buy_vol,
            "內盤累積": total_sell_vol,
            "本段量": signal.get("current_volume", 0),
            "本段筆數": signal.get("ticks_in_bucket", 0),
            "前段量": signal.get("previous_volume", 0),
            "預估本段量": signal.get("projected_bucket_volume", 0),
            "量比": format_ratio_value(signal.get("volume_ratio")),
            "外盤占比": format_percent_ratio(signal.get("buy_pressure_ratio")),
            "單筆跳動": format_signed_pct(signal.get("last_tick_jump_pct")),
            "2秒漲幅": format_signed_pct(signal.get("price_change_2s")),
            "5秒漲幅": format_signed_pct(signal.get("price_change_5s")),
            "10秒漲幅": format_signed_pct(signal.get("price_change_10s")),
            "30秒漲幅": format_signed_pct(signal.get("price_change_30s")),
            low_col_name: format_price_value(signal.get("low_track")),
            high_col_name: format_price_value(signal.get("high_track")),
            "低點拉抬": format_signed_pct(signal.get("rise_from_low_pct")),
            "高點回落": format_signed_pct(signal.get("drop_from_high_pct")),
            "今日最低": format_price_value(day_low_signal.get("day_low")),
            "今日低點反彈%": format_signed_pct(day_low_signal.get("rebound_pct")),
            momentum_col_name: format_signed_pct(momentum_signal.get("window_change_pct")),
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
        "warning_count": warning_count,
        "entry_count": entry_count,
        "flash_count": flash_count,
        "day_low_rebound_count": day_low_rebound_count,
        "momentum_count": momentum_count,
        "top_pct_text": top_pct_text,
        "signal_text": signal_text,
    })

    group_tables[group_name] = pd.DataFrame(
        rows,
        columns=[
            "代碼",
            "股票名稱",
            "進場訊號",
            "即時價",
            "漲幅%",
            "最新單筆",
            "內外盤",
            "外盤累積",
            "內盤累積",
            "本段量",
            "本段筆數",
            "前段量",
            "預估本段量",
            "量比",
            "外盤占比",
            "單筆跳動",
            "2秒漲幅",
            "5秒漲幅",
            "10秒漲幅",
            "30秒漲幅",
            low_col_name,
            high_col_name,
            "低點拉抬",
            "高點回落",
            "今日最低",
            "今日低點反彈%",
            momentum_col_name,
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

st.markdown("### 📌 瞬間拉抬進場儀表板")

st.caption(
    f"漲幅達標門檻：≥ {pct_threshold:.1f}%｜"
    f"量能視窗：{int(st.session_state.entry_bucket_sec)} 秒｜"
    f"預估量比門檻：{float(st.session_state.entry_volume_ratio):.2f}x｜"
    f"外盤占比：{float(st.session_state.entry_buy_pressure_ratio) * 100:.0f}%｜"
    f"價格變動門檻：{float(st.session_state.entry_price_move_pct):.1f}%｜"
    f"高低點追蹤：{int(st.session_state.entry_track_sec)} 秒｜"
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
    anchor_id = make_anchor_id(item["group"])

    if item["entry_count"] > 0:
        card_class = "dash-card entry"
    elif item["warning_count"] > 0:
        card_class = "dash-card warn"
    elif item["day_low_rebound_count"] > 0:
        card_class = "dash-card daylow"
    elif item["momentum_count"] > 0:
        card_class = "dash-card momentum"
    elif item["flash_count"] > 0:
        card_class = "dash-card flash"
    elif item["pct_hit_count"] > 0:
        card_class = "dash-card normal"
    else:
        card_class = "dash-card idle"

    card_html_parts.append(
        f'<a href="#{anchor_id}" class="dashboard-link" title="前往 {escape_html(item["group"])}">'
        f'<div class="{card_class}">'
        f'<div class="dash-title">{escape_html(item["group"])}</div>'
        f'<div class="dash-big">🚀 {item["entry_count"]}｜⚠️ {item["warning_count"]}｜'
        f'🔥 {item["flash_count"]}｜📈 {item["day_low_rebound_count"]}｜📊 {item["momentum_count"]}</div>'
        f'<div class="dash-line">進場訊號：<b>{item["entry_count"]}</b> 檔｜預警：<b>{item["warning_count"]}</b> 檔｜'
        f'極早期：<b>{item["flash_count"]}</b> 檔</div>'
        f'<div class="dash-line">低點反彈：<b>{item["day_low_rebound_count"]}</b> 檔｜'
        f'區間動能：<b>{item["momentum_count"]}</b> 檔</div>'
        f'<div class="dash-line">漲幅達標（≥{pct_threshold:.1f}%）：'
        f'<b>{item["pct_hit_count"]} / {item["total"]}</b>，比例 <b>{item["pct_hit_ratio"]:.0f}%</b></div>'
        f'<div class="dash-line">🔴 上漲：<b>{item["up_count"]}</b> '
        f'🟢 下跌：<b>{item["down_count"]}</b></div>'
        f'<div class="dash-small"><b>最新訊號</b><br>{item["signal_text"]}</div>'
        f'<div class="dash-small"><b>漲幅排行</b><br>{item["top_pct_text"]}</div>'
        f'</div></a>'
    )

card_html_parts.append("</div>")

st.markdown("".join(card_html_parts), unsafe_allow_html=True)


recent_signals.sort(
    key=lambda x: x["time"] or datetime.min.replace(tzinfo=TW_TZ),
    reverse=True,
)

st.caption(f"本輪掃到訊號：{len(recent_signals)} 檔")

if recent_signals:
    st.markdown("#### 🚨 最近預警 / 進場訊號")
    recent_cols = st.columns(min(3, len(recent_signals)))

    for idx, item in enumerate(recent_signals[:6]):
        with recent_cols[idx % len(recent_cols)]:
            if item["level"] == "entry":
                st.error(
                    f"{item['group']}｜{item['code']} {item['name']}\n\n"
                    f"{item['text']}"
                )
            elif item["level"] == "warning":
                st.warning(
                    f"{item['group']}｜{item['code']} {item['name']}\n\n"
                    f"{item['text']}"
                )
            else:
                st.info(
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
            '<div class="return-link-wrap">'
            '<a href="#dashboard-top">⬆️ 返回儀表板</a>'
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
            "進場訊號": st.column_config.TextColumn(
                "進場訊號",
                help="預警：預估量放大 + 外盤占比 + 5秒/10秒短線漲幅。進場：再加上突破高點或低點急拉確認。",
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
            "本段量": st.column_config.NumberColumn("本段量"),
            "本段筆數": st.column_config.NumberColumn(
                "本段筆數",
                help="量能視窗內的成交筆數，用來過濾單一大單造成的假突破",
            ),
            "前段量": st.column_config.NumberColumn("前段量"),
            "預估本段量": st.column_config.NumberColumn("預估本段量"),
            "量比": st.column_config.TextColumn("量比"),
            "外盤占比": st.column_config.TextColumn("外盤占比"),
            "單筆跳動": st.column_config.TextColumn(
                "單筆跳動",
                help="最新一筆成交價相較上一筆成交價的漲幅，抓單筆巨量瞬間跳價",
            ),
            "2秒漲幅": st.column_config.TextColumn(
                "2秒漲幅",
                help="極短窗口漲幅，反應速度比5秒窗更快",
            ),
            "5秒漲幅": st.column_config.TextColumn("5秒漲幅"),
            "10秒漲幅": st.column_config.TextColumn("10秒漲幅"),
            "30秒漲幅": st.column_config.TextColumn("30秒漲幅"),
            low_col_name: st.column_config.TextColumn(
                low_col_name,
                help=f"最近 {int(st.session_state.entry_track_sec)} 秒內的最低成交價，秒數可在左側「高低點追蹤秒」調整",
            ),
            high_col_name: st.column_config.TextColumn(
                high_col_name,
                help=f"最近 {int(st.session_state.entry_track_sec)} 秒內的最高成交價，秒數可在左側「高低點追蹤秒」調整",
            ),
            "低點拉抬": st.column_config.TextColumn("低點拉抬"),
            "高點回落": st.column_config.TextColumn("高點回落"),
            "今日最低": st.column_config.TextColumn(
                "今日最低",
                help="持續追蹤的當日最低成交價（不受高低點追蹤秒數影響，從開盤持續累積）",
            ),
            "今日低點反彈%": st.column_config.TextColumn(
                "今日低點反彈%",
                help="現價相較今日最低點的反彈幅度，達門檻觸發📈提醒",
            ),
            momentum_col_name: st.column_config.TextColumn(
                momentum_col_name,
                help="固定時間窗口內的漲幅，達門檻觸發📊區間動能提醒，視窗長度可在左側「動能視窗秒數」調整",
            ),
            "昨收日期": st.column_config.TextColumn("昨收日期"),
            "昨收來源": st.column_config.TextColumn("昨收來源"),
        },
    )


# =============================================================================
# 每日訊號 Log 檢視 / 下載
# =============================================================================
with st.sidebar.expander("📝 今日訊號 Log", expanded=False):
    today_log_text, today_log_path = read_signal_log()
    log_line_count = len([ln for ln in today_log_text.splitlines() if ln.strip()])

    st.caption(f"檔案：{today_log_path}｜目前已記錄 {log_line_count} 筆")

    if st.session_state.get("signal_log_write_error"):
        st.error(f"寫入 log 曾發生錯誤：{st.session_state['signal_log_write_error']}")

    if today_log_text:
        st.text_area(
            "今日訊號紀錄（最新在最下面）",
            value=today_log_text,
            height=260,
        )
        st.download_button(
            "⬇️ 下載今日 log.txt",
            data=today_log_text.encode("utf-8"),
            file_name=os.path.basename(today_log_path),
            mime="text/plain",
        )
    else:
        st.caption("今天尚無訊號紀錄")


with st.sidebar.expander("🔍 WebSocket Debug", expanded=False):
    debug_code = st.text_input("輸入代碼看最後 WS 原始訊息", value="2330")
    msg = manager.get_message(debug_code)

    if msg:
        st.caption(f"時間：{msg['time'].strftime('%Y-%m-%d %H:%M:%S')}")
        st.json(msg["raw"])

        debug_signal = manager.get_entry_signal(
            debug_code,
            bucket_sec=st.session_state.entry_bucket_sec,
            track_sec=st.session_state.entry_track_sec,
            volume_ratio_threshold=st.session_state.entry_volume_ratio,
            price_move_pct=st.session_state.entry_price_move_pct,
            early_5s_pct=st.session_state.entry_early_5s_pct,
            early_10s_pct=st.session_state.entry_early_10s_pct,
            buy_pressure_ratio_threshold=st.session_state.entry_buy_pressure_ratio,
            cooldown_sec=st.session_state.entry_cooldown_sec,
            min_current_volume=st.session_state.entry_min_current_volume,
            early_2s_pct=st.session_state.entry_early_2s_pct,
            tick_jump_pct=st.session_state.entry_tick_jump_pct,
            min_ticks_in_bucket=st.session_state.entry_min_ticks_in_bucket,
            flash_cooldown_sec=st.session_state.entry_flash_cooldown_sec,
        )

        debug_day_low_signal = manager.get_day_low_rebound_signal(
            debug_code,
            rebound_pct_threshold=st.session_state.day_low_rebound_pct,
            cooldown_sec=st.session_state.day_low_rebound_cooldown_sec,
        )

        debug_momentum_signal = manager.get_window_momentum_signal(
            debug_code,
            window_sec=st.session_state.momentum_window_sec,
            pct_threshold=st.session_state.momentum_window_pct,
            cooldown_sec=st.session_state.momentum_cooldown_sec,
        )

        st.markdown("### 訊號 Debug")
        st.json({
            "訊號": debug_signal.get("text"),
            "訊號等級": debug_signal.get("signal_level"),
            "本段量": debug_signal.get("current_volume"),
            "本段筆數": debug_signal.get("ticks_in_bucket"),
            "前段量": debug_signal.get("previous_volume"),
            "預估本段量": debug_signal.get("projected_bucket_volume"),
            "量比": debug_signal.get("volume_ratio"),
            "外盤占比": debug_signal.get("buy_pressure_ratio"),
            "單筆跳動": debug_signal.get("last_tick_jump_pct"),
            "2秒漲幅": debug_signal.get("price_change_2s"),
            "5秒漲幅": debug_signal.get("price_change_5s"),
            "10秒漲幅": debug_signal.get("price_change_10s"),
            "30秒漲幅": debug_signal.get("price_change_30s"),
            f"{track_label}低點": debug_signal.get("low_track"),
            f"{track_label}高點": debug_signal.get("high_track"),
            "低點拉抬": debug_signal.get("rise_from_low_pct"),
            "突破高點": debug_signal.get("near_high_breakout"),
            "volume_ok": debug_signal.get("volume_ok"),
            "buy_pressure_ok": debug_signal.get("buy_pressure_ok"),
            "short_momentum_ok": debug_signal.get("short_momentum_ok"),
            "position_ok": debug_signal.get("position_ok"),
            "flash": debug_signal.get("flash"),
        })

        st.markdown("### 📈 今日低點反彈 Debug")
        st.json({
            "訊號": debug_day_low_signal.get("text"),
            "今日最低": debug_day_low_signal.get("day_low"),
            "現價": debug_day_low_signal.get("current_price"),
            "反彈%": debug_day_low_signal.get("rebound_pct"),
            "門檻%": debug_day_low_signal.get("rebound_pct_threshold"),
            "active": debug_day_low_signal.get("active"),
        })

        st.markdown("### 📊 區間動能 Debug")
        st.json({
            "訊號": debug_momentum_signal.get("text"),
            "視窗": debug_momentum_signal.get("window_label"),
            "視窗漲幅%": debug_momentum_signal.get("window_change_pct"),
            "門檻%": debug_momentum_signal.get("pct_threshold"),
            "active": debug_momentum_signal.get("active"),
        })

    else:
        st.caption("尚未收到此代碼的 WebSocket 訊息")


# =============================================================================
# 定時推送 Log 到 Telegram (指定盤中時間)
# =============================================================================
# 無視 tg_push_enabled 開關，直接執行定時推送邏輯
now = datetime.now(TW_TZ)

# 跨日重置已發送紀錄
if "log_date" not in st.session_state or st.session_state.log_date != now.date():
    st.session_state.sent_log_slots = set()
    st.session_state.log_date = now.date()

# 定義要發送的目標時間 (小時, 分鐘)
target_times = [
    (9, 30), (10, 0), (10, 30), (11, 0), (11, 30),
    (12, 0), (12, 30), (13, 0), (13, 30)
]

now_hm = now.hour * 60 + now.minute

for t_h, t_m in target_times:
    target_hm = t_h * 60 + t_m
    
    # 檢查現在時間是否在目標時間的正負 1 分鐘內 (相差 <= 1 分鐘)
    if abs(now_hm - target_hm) <= 1:
        slot_id = f"{t_h:02d}:{t_m:02d}"
        
        # 確保同一個時段只發送一次
        if slot_id not in st.session_state.get("sent_log_slots", set()):
            _, today_log_path = read_signal_log()
            
            # 確保今天有產生 log 檔案才發送
            if os.path.exists(today_log_path):
                def push_log_task(path, slot):
                    send_telegram_document(
                        file_path=path,
                        caption=f"🕒 定時回報：{slot} 訊號 Log"
                    )
                
                # 使用 Thread 避免阻塞 Streamlit 主線程
                t = threading.Thread(target=push_log_task, args=(today_log_path, slot_id))
                add_script_run_ctx(t)
                t.start()
            
            # 記錄已發送，避免這兩分鐘內畫面刷新導致重複發送
            st.session_state.setdefault("sent_log_slots", set()).add(slot_id)


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
