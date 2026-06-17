# -*- coding: utf-8 -*-
"""
股票監控面板 - 富邦 WebSocket 即時價 + yfinance 歷史資料

本版修正 / 改進：
1. 修正 HTML 被寫成 &lt; / &gt; 導致錨點與按鈕無法跳轉的問題。
2. 修正 APP_LOGO 字串少了結尾引號的語法錯誤。
3. 新增 dashboard-top 錨點，「回到儀表板」可正常跳轉。
4. 儀表板卡片與分類錨點改成真正 HTML。
5. 加入 MACD 翻正指標與欄位。
6. 圖片不存在時不會中斷，改顯示文字標題。
"""

import re
import os
import json
import copy
import time
import gc
import base64
import tempfile
import threading
import requests
from html import escape
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
import streamlit as st
import yfinance as yf

# ===== 富邦 API 引入 =====
try:
    from fubon_neo.sdk import FubonSDK
except Exception:
    FubonSDK = None

# ===== Streamlit UI 基本設定（一定要放最前面）=====
st.set_page_config(layout="wide")

# ===== 常數設定 =====
TW_TZ = ZoneInfo("Asia/Taipei")
REFRESH_SEC = 30
HISTORY_CACHE_TTL = 3600  # 盤中 yfinance 歷史資料每小時最多重抓一次
ENABLE_GAP_SIGNAL = True
GROUP_EDIT_PIN = "1219"
GROUPS_FILE = "stock_groups.json"
BACKUP_DIR = "backups"
STOCK_NAME_FILE = "TWstocklistname.txt"
APP_LOGO = "jerry.jpg"

# ===== Secrets 安全讀取 =====
def get_secret_or_default(key: str, default: str = ""):
    try:
        return st.secrets.get(key, default)
    except Exception:
        return default


# ===== Telegram 設定 =====
TELEGRAM_BOT_TOKEN = get_secret_or_default("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = get_secret_or_default("TELEGRAM_CHAT_ID", "")

DEFAULT_STOCK_GROUPS = {
    "權值股": [
        "2330.TW", "00981A.TW", "2449.TW", "2317.TW", "3711.TW",
        "6488.TWO", "2327.TW", "6176.TW", "2303.TW", "5347.TWO",
    ],
    "自選股1": [
        "3008.TW", "3035.TW", "4566.TW", "4956.TW", "6456.TW",
        "4749.TWO", "6271.TW", "6290.TWO", "4919.TW",
    ],
    "低軌衛星": ["6285.TW", "2313.TW"],
    "ABF": ["4958.TW", "3037.TW", "8046.TW", "3189.TW", "8996.TW", "5439.TWO", "8358.TWO"],
    "記憶體": ["6770.TW", "2408.TW", "2344.TW", "8271.TW", "4967.TW", "3260.TWO", "2451.TW"],
    "CCL": ["2383.TW", "6274.TWO", "6213.TW", "8039.TW"],
    "CPO": ["4979.TWO", "3163.TWO", "4977.TW", "3081.TWO", "3450.TW", "6442.TW"],
}

# ===== CSS =====
st.markdown(
    """
<style>
html { scroll-behavior: smooth; }
.dashboard-scroll { overflow-x: auto; overflow-y: hidden; width: 100%; padding-bottom: 8px; }
.dashboard-grid { display: grid; grid-template-columns: repeat(4, minmax(260px, 1fr)); gap: 12px; min-width: 1120px; }
.dashboard-card { border-radius: 12px; padding: 14px 16px; min-height: 180px; box-shadow: 0 1px 4px rgba(0,0,0,0.08); box-sizing: border-box; }
.dashboard-title { font-size: 18px; font-weight: 700; margin-bottom: 10px; color: #000000 !important; }
.dashboard-main { font-size: 28px; font-weight: 800; margin-bottom: 6px; }
.dashboard-sub { font-size: 14px; color: #000000 !important; margin-bottom: 10px; }
.dashboard-detail { font-size: 14px; line-height: 1.7; color: #000000 !important; }
.dashboard-extra { font-size: 13px; line-height: 1.6; color: #000000 !important; margin-top: 10px; padding-top: 8px; border-top: 1px solid rgba(0,0,0,0.12); word-break: break-word; }
.dashboard-link, .dashboard-link:link, .dashboard-link:visited, .dashboard-link:hover, .dashboard-link:active { text-decoration: none !important; color: inherit !important; }
.back-to-dashboard-btn { display: inline-block; padding: 6px 12px; border-radius: 8px; border: 1px solid #999; background: #f5f5f5; color: #000 !important; text-decoration: none !important; font-size: 14px; font-weight: 600; text-align: center; }
.back-to-dashboard-btn:hover { background: #eaeaea; }
.ws-ok { color: #16a34a; font-weight: 700; }
.ws-bad { color: #dc2626; font-weight: 700; }
</style>
""",
    unsafe_allow_html=True,
)

# =============================================================================
# 基礎工具函式
# =============================================================================
def symbol_to_code(symbol: str) -> str:
    return str(symbol).strip().upper().split(".")[0]


def yahoo_quote_url(symbol: str) -> str:
    code = symbol_to_code(symbol)
    return f"https://tw.stock.yahoo.com/quote/{code}"


def make_anchor_id(group_name: str) -> str:
    anchor = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "-", group_name).strip("-")
    return f"group-{anchor}"


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


def normalize_symbols_from_text(text: str):
    if not text:
        return []
    text = text.replace("，", ",")
    lines = []
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        parts = [p.strip().upper() for p in raw_line.split(",") if p.strip()]
        lines.extend(parts)
    seen = set()
    result = []
    for s in lines:
        normalized = normalize_symbol_quick(s)
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def compact_name_list(names, max_show=3):
    names = [str(x).strip() for x in names if str(x).strip()]
    if not names:
        return "無"
    if len(names) <= max_show:
        return "、".join(names)
    return "、".join(names[:max_show]) + f" 等{len(names)}檔"

# =============================================================================
# 富邦 WebSocket：只負責「當日即時股價」
# =============================================================================
class FubonRealtimeManager:
    def __init__(self):
        self.sdk = None
        self.ws = None
        self.lock = threading.RLock()
        self.logged_in = False
        self.connected = False
        self.error = None
        self.prices = {}
        self.messages = {}
        self.subscribed = set()
        self.last_message_at = None
        self.cert_path = None

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
            self.prices = {}
            self.messages = {}
            self.subscribed = set()
            self.last_message_at = None

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
                self.prices = {}
                self.messages = {}
                self.subscribed = set()
                self.last_message_at = None
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

    def _extract_symbol_price(self, msg):
        data = msg.get("data", {})
        if not isinstance(data, dict):
            data = {}
        symbol = data.get("symbol") or msg.get("symbol") or data.get("stockNo") or msg.get("stockNo")
        if symbol:
            symbol = symbol_to_code(symbol)
        price_candidates = [
            data.get("price"), data.get("tradePrice"), data.get("lastPrice"),
            data.get("close"), data.get("closePrice"),
            msg.get("price"), msg.get("tradePrice"), msg.get("lastPrice"),
            msg.get("close"), msg.get("closePrice"),
        ]
        price = None
        for p in price_candidates:
            if p is not None and pd.notna(p):
                try:
                    price = float(p)
                    break
                except Exception:
                    continue
        return symbol, price

    def _on_message(self, message):
        msg = self._parse_message(message)
        symbol, price = self._extract_symbol_price(msg)
        now = datetime.now(TW_TZ)
        with self.lock:
            self.last_message_at = now
            if symbol:
                self.messages[symbol] = {"time": now, "raw": msg}
            if symbol and price is not None:
                self.prices[symbol] = price

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
        for s in symbols:
            self.subscribe(s)

    def get_price(self, symbol: str):
        code = symbol_to_code(symbol)
        with self.lock:
            return self.prices.get(code)

    def get_message(self, symbol: str):
        code = symbol_to_code(symbol)
        with self.lock:
            return copy.deepcopy(self.messages.get(code))

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


def ensure_backup_dir():
    os.makedirs(BACKUP_DIR, exist_ok=True)


def create_backup_filename():
    tw_now = datetime.now(TW_TZ)
    return f"stock_groups_backup_{tw_now.strftime('%Y%m%d_%H%M%S')}.json"


def save_backup_snapshot(groups):
    ensure_backup_dir()
    filename = create_backup_filename()
    file_path = os.path.join(BACKUP_DIR, filename)
    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(groups, f, ensure_ascii=False, indent=2)
    return file_path


def list_backup_files():
    if not os.path.exists(BACKUP_DIR):
        return []
    files = []
    for name in os.listdir(BACKUP_DIR):
        if name.lower().endswith(".json"):
            full_path = os.path.join(BACKUP_DIR, name)
            if os.path.isfile(full_path):
                files.append((name, os.path.getmtime(full_path)))
    files.sort(key=lambda x: x[1], reverse=True)
    return [name for name, _ in files]


def validate_and_normalize_group_json(data):
    if not isinstance(data, dict) or not data:
        raise ValueError("JSON 格式錯誤：最外層必須是非空物件（dict）")
    validated = {}
    for group_name, symbols in data.items():
        group_name = str(group_name).strip()
        if not group_name:
            raise ValueError("JSON 格式錯誤：分類名稱不可為空")
        if isinstance(symbols, list):
            raw_text = "\n".join(str(x) for x in symbols)
        elif isinstance(symbols, str):
            raw_text = symbols
        else:
            raise ValueError(f"JSON 格式錯誤：分類「{group_name}」的股票清單必須是 list 或 string")
        validated[group_name] = normalize_symbols_from_text(raw_text)
    if not validated:
        raise ValueError("JSON 內容為空")
    return validated

# =============================================================================
# Telegram
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


def check_telegram_push_command():
    if not TELEGRAM_BOT_TOKEN:
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    params = {"timeout": 1}
    if "tg_last_update_id" in st.session_state and st.session_state.tg_last_update_id:
        params["offset"] = st.session_state.tg_last_update_id + 1
    try:
        res = requests.get(url, params=params, timeout=3)
        if res.status_code == 200:
            data = res.json()
            if data.get("ok") and data.get("result"):
                st.sidebar.info(f"👀 偷看到 {len(data['result'])} 則新訊息")
                triggered = False
                for item in data["result"]:
                    update_id = item["update_id"]
                    st.session_state.tg_last_update_id = update_id
                    message_text = item.get("message", {}).get("text", "").strip().lower()
                    st.sidebar.write(f"💬 內容: {message_text}")
                    if message_text == "push":
                        triggered = True
                return triggered
    except Exception:
        pass
    return False

# =============================================================================
# yfinance：今日以前歷史資料
# =============================================================================
@st.cache_data(ttl=HISTORY_CACHE_TTL)
def download_stock_data(symbol):
    """今日以前歷史資料全部使用 yfinance；今日即時價不使用 yfinance。"""
    candidates = build_yfinance_candidates(symbol)
    last_error = ""
    for yf_symbol in candidates:
        try:
            df = yf.download(
                yf_symbol,
                period="3mo",
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
            )
        except Exception as e:
            last_error = f"{yf_symbol}: {e}"
            continue
        if df is None or df.empty:
            last_error = f"{yf_symbol}: yfinance 無資料"
            continue
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
        df = df.reset_index()
        date_col = "Date" if "Date" in df.columns else "Datetime" if "Datetime" in df.columns else df.columns[0]
        df = df.rename(columns={date_col: "Date"})
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        df = df.dropna(subset=["Date"])
        today = datetime.now(TW_TZ).date()
        df = df[df["Date"].dt.date < today]
        required_cols = ["Open", "High", "Low", "Close", "Volume"]
        if not set(required_cols).issubset(df.columns):
            last_error = f"{yf_symbol}: 缺少 OHLCV 欄位"
            continue
        for col in required_cols:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df["Volume"] = df["Volume"].fillna(0)
        df = df.dropna(subset=["Open", "High", "Low", "Close"])
        if len(df) < 26:
            last_error = f"{yf_symbol}: 歷史資料不足 {len(df)} 筆"
            continue
        return df[["Date", "Open", "High", "Low", "Close", "Volume"]].copy()
    raise ValueError(f"無法取得 yfinance 歷史資料。已嘗試：{', '.join(candidates)}。最後錯誤：{last_error}")

def normalize_ohlc(df):
    if df is None or df.empty:
        return pd.DataFrame()
    required_cols = ["Open", "High", "Low", "Close", "Volume"]
    if set(required_cols).issubset(df.columns):
        keep_cols = ["Date"] + required_cols if "Date" in df.columns else required_cols
        return df[keep_cols].copy()
    return pd.DataFrame()


def is_fubon_realtime_time():
    now = datetime.now(TW_TZ).time()
    start = datetime.strptime("09:00", "%H:%M").time()
    end = datetime.strptime("13:30", "%H:%M").time()
    return start <= now < end


def is_after_1330():
    """13:30 後不自動抓 yfinance，只有手動更新才會重新抓。"""
    now = datetime.now(TW_TZ).time()
    end = datetime.strptime("13:30", "%H:%M").time()
    return now >= end


def is_before_market_open():
    now = datetime.now(TW_TZ).time()
    start = datetime.strptime("09:00", "%H:%M").time()
    return now < start


def parse_price_value(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, dict):
        for key in ["raw", "fmt", "value"]:
            parsed = parse_price_value(value.get(key))
            if parsed is not None:
                return parsed
        return None
    try:
        text_val = str(value).strip().replace(",", "")
        if not text_val or text_val in ["-", "--", "None", "nan"]:
            return None
        return float(text_val)
    except Exception:
        return None


@st.cache_data(ttl=30)
def get_yfinance_fast_info_price(symbol: str):
    candidates = [str(symbol).strip().upper()] + [
        s for s in build_yfinance_candidates(symbol) if s != str(symbol).strip().upper()
    ]
    seen = set()
    last_error = ""
    for yf_symbol in candidates:
        if not yf_symbol or yf_symbol in seen:
            continue
        seen.add(yf_symbol)
        try:
            ticker = yf.Ticker(yf_symbol)
            price = ticker.fast_info.get("last_price", None)
            if price is not None and pd.notna(price):
                return float(price), yf_symbol
        except Exception as e:
            last_error = f"{yf_symbol}: {e}"
            continue
    raise ValueError(f"yfinance fast_info 無法取得 {symbol} 價格。最後錯誤：{last_error}")

@st.cache_data(ttl=30)
def get_yahoo_tw_quote_price(symbol: str):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept": "application/json,text/plain,*/*",
        "Referer": "https://tw.stock.yahoo.com/",
    }
    last_error = ""
    for yahoo_symbol in build_yfinance_candidates(symbol):
        url = f"https://tw.stock.yahoo.com/_td-stock/api/resource/StockServices.stockList;symbols={yahoo_symbol}"
        try:
            res = requests.get(url, headers=headers, timeout=5)
            if res.status_code != 200:
                last_error = f"{yahoo_symbol}: HTTP {res.status_code}"
                continue
            raw_text = res.text.strip()
            if raw_text.startswith(")]}'"):
                raw_text = raw_text.split("\n", 1)[-1]
            payload = json.loads(raw_text)
        except Exception as e:
            last_error = f"{yahoo_symbol}: {e}"
            continue
        items = payload if isinstance(payload, list) else payload.get("data", []) if isinstance(payload, dict) else []
        if isinstance(items, dict):
            items = [items]
        for item in items:
            if not isinstance(item, dict):
                continue
            price_keys = ["regularMarketPrice", "price", "lastPrice", "tradePrice", "close", "closePrice", "latestPrice"]
            for key in price_keys:
                price = parse_price_value(item.get(key))
                if price is not None:
                    return float(price), yahoo_symbol
            for value in item.values():
                if isinstance(value, dict):
                    for key in price_keys:
                        price = parse_price_value(value.get(key))
                        if price is not None:
                            return float(price), yahoo_symbol
        last_error = f"{yahoo_symbol}: 找不到可用價格欄位"
    raise ValueError(f"Yahoo TW 無法取得 {symbol} 價格。最後錯誤：{last_error}")


@st.cache_data(ttl=300)
def get_yfinance_latest_daily_close(symbol: str):
    last_error = ""
    for yf_symbol in build_yfinance_candidates(symbol):
        try:
            daily_df = yf.download(
                yf_symbol,
                period="10d",
                interval="1d",
                auto_adjust=True,
                progress=False,
                threads=False,
            )
        except Exception as e:
            last_error = f"{yf_symbol}: {e}"
            continue
        if daily_df is None or daily_df.empty:
            last_error = f"{yf_symbol}: daily 無資料"
            continue
        if isinstance(daily_df.columns, pd.MultiIndex):
            daily_df.columns = [c[0] if isinstance(c, tuple) else c for c in daily_df.columns]
        if "Close" not in daily_df.columns:
            last_error = f"{yf_symbol}: daily 缺少 Close 欄位"
            continue
        daily_df = daily_df.reset_index()
        date_col = "Date" if "Date" in daily_df.columns else "Datetime" if "Datetime" in daily_df.columns else daily_df.columns[0]
        daily_df = daily_df.rename(columns={date_col: "Date"})
        daily_df["Date"] = pd.to_datetime(daily_df["Date"], errors="coerce")
        daily_df["Close"] = pd.to_numeric(daily_df["Close"], errors="coerce")
        daily_df = daily_df.dropna(subset=["Date", "Close"]).sort_values("Date")
        if daily_df.empty:
            last_error = f"{yf_symbol}: daily Close 皆為空"
            continue
        last_row = daily_df.iloc[-1]
        return float(last_row["Close"]), pd.to_datetime(last_row["Date"]).date(), yf_symbol
    raise ValueError(f"yfinance daily 無法取得 {symbol} 最新收盤價。最後錯誤：{last_error}")

def after_1330_price_logic(symbol, df, forced=False):
    last_hist_close = None
    last_hist_date = None
    if df is not None and not df.empty and "Close" in df.columns:
        try:
            last_hist_close = float(df["Close"].iloc[-1])
        except Exception:
            last_hist_close = None
        try:
            if "Date" in df.columns:
                last_hist_date = pd.to_datetime(df["Date"].iloc[-1]).date()
        except Exception:
            last_hist_date = None
    fast_price = None
    try:
        fast_price, _ = get_yfinance_fast_info_price(symbol)
    except Exception:
        fast_price = None
    if fast_price is not None and pd.notna(fast_price):
        if last_hist_close is None or abs(float(fast_price) - last_hist_close) > 1e-9:
            return float(fast_price), "Forced 13:30 yfinance fast_info" if forced else "yfinance after 13:30"
    try:
        yahoo_price, _ = get_yahoo_tw_quote_price(symbol)
        if yahoo_price is not None and pd.notna(yahoo_price):
            return float(yahoo_price), "Forced 13:30 Yahoo TW" if forced else "Yahoo TW after 13:30"
    except Exception:
        pass
    try:
        daily_close, daily_date, _ = get_yfinance_latest_daily_close(symbol)
        if daily_close is not None and pd.notna(daily_close):
            if last_hist_date is None or daily_date > last_hist_date:
                return float(daily_close), "Forced 13:30 yfinance daily" if forced else "yfinance daily after 13:30"
    except Exception:
        pass
    if fast_price is not None and pd.notna(fast_price):
        return float(fast_price), "Forced 13:30 yfinance stale fast_info" if forced else "yfinance stale fast_info after 13:30"
    if last_hist_close is not None:
        return last_hist_close, "Forced 13:30 history fallback" if forced else "history after 13:30"
    raise ValueError("無法取得 13:30 後價格")


def get_last_price(symbol, df, manager=None):
    mode = st.session_state.get("price_source_override", "auto")
    use_fubon_ws = is_fubon_realtime_time()

    if mode == "websocket":
        if manager is None:
            raise ValueError("強制 WebSocket 模式，但富邦 manager 尚未建立")
        ws_price = manager.get_price(symbol)
        if ws_price is not None and pd.notna(ws_price):
            return float(ws_price), "Forced WebSocket"
        raise ValueError("強制 WebSocket 模式，但尚未收到此股票的 WebSocket trades 成交價")

    if mode == "yfinance":
        # 強制 yfinance 仍只在使用者手動切換/手動刷新後才會進入資料快照重建。
        return after_1330_price_logic(symbol, df, forced=True)

    # 盤中 09:00~13:30：只用富邦 WebSocket 即時價加入歷史資料運算；
    # 若尚未收到 WebSocket 價格，則用「已快取的歷史收盤價」暫代，不再呼叫 yfinance fast_info。
    if use_fubon_ws:
        if manager is not None:
            ws_price = manager.get_price(symbol)
            if ws_price is not None and pd.notna(ws_price):
                return float(ws_price), "Fubon WebSocket trades"
        if df is not None and not df.empty and "Close" in df.columns:
            return float(df["Close"].iloc[-1]), "history fallback during market"
        raise ValueError("盤中尚未取得 WebSocket 價格，且歷史資料不可用")

    # 非盤中：若快照不被重建，就不會走到這裡；13:30 後只有手動更新才會重建。
    return after_1330_price_logic(symbol, df, forced=False)

# =============================================================================
# 股票名稱 / 查詢
# =============================================================================
@st.cache_data(ttl=86400)
def load_stock_name_map(file_path: str = STOCK_NAME_FILE) -> dict:
    name_map = {}
    if not os.path.exists(file_path):
        return name_map
    with open(file_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            line = line.replace("\ufeff", "").replace("\u3000", "")
            if "\t" in line:
                parts = [p.strip() for p in line.split("\t") if p.strip()]
                if len(parts) >= 2:
                    symbol = parts[0].upper()
                    name = parts[1].strip()
                    name_map[symbol] = name
                    name_map[symbol_to_code(symbol)] = name
                    continue
            m = re.match(r"^([^\s]+)\s+(.+)$", line)
            if m:
                symbol = m.group(1).strip().upper()
                name = m.group(2).strip()
                name_map[symbol] = name
                name_map[symbol_to_code(symbol)] = name
    return name_map


@st.cache_data(ttl=86400)
def get_stock_name(symbol: str) -> str:
    name_map = load_stock_name_map(STOCK_NAME_FILE)
    code = symbol_to_code(symbol)
    if symbol in name_map:
        return name_map[symbol]
    if code in name_map:
        return name_map[code]
    try:
        for yf_symbol in build_yfinance_candidates(symbol):
            ticker = yf.Ticker(yf_symbol)
            info = {}
            try:
                info = ticker.get_info()
            except Exception:
                try:
                    info = ticker.info
                except Exception:
                    info = {}
            for key in ["shortName", "longName", "displayName", "name"]:
                val = info.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
    except Exception:
        pass
    return code


def normalize_lookup_symbol(raw_symbol: str) -> str:
    s = str(raw_symbol).strip().upper()
    if not s:
        return ""
    if "." in s:
        return s
    normalized = normalize_symbol_quick(s)
    return normalized or s


@st.cache_data(ttl=86400)
def load_stock_lookup_maps(file_path: str = STOCK_NAME_FILE) -> dict:
    code_to_name = {}
    code_to_symbol = {}
    name_to_symbol = {}
    if not os.path.exists(file_path):
        return {"code_to_name": code_to_name, "code_to_symbol": code_to_symbol, "name_to_symbol": name_to_symbol}
    with open(file_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            line = line.replace("\ufeff", "").replace("\u3000", " ").strip()
            if "\t" in line:
                parts = [p.strip() for p in line.split("\t") if p.strip()]
            else:
                m = re.match(r"^([^\s]+)\s+(.+)$", line)
                parts = [m.group(1).strip(), m.group(2).strip()] if m else []
            if len(parts) < 2:
                continue
            raw_symbol = parts[0].upper()
            stock_name = parts[1].strip()
            symbol = normalize_lookup_symbol(raw_symbol)
            code = symbol_to_code(symbol)
            if not code or not stock_name:
                continue
            code_to_name[code] = stock_name
            code_to_symbol[code] = symbol
            name_to_symbol[stock_name] = symbol
            name_to_symbol[stock_name.replace(" ", "")] = symbol
    return {"code_to_name": code_to_name, "code_to_symbol": code_to_symbol, "name_to_symbol": name_to_symbol}


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
        symbol = q_upper
        code = symbol_to_code(symbol)
        return symbol, code_to_name.get(code) or get_stock_name(symbol), "ticker"
    if q_upper.isdigit():
        code = q_upper
        symbol = code_to_symbol.get(code) or normalize_symbol_quick(code)
        return symbol, code_to_name.get(code) or get_stock_name(symbol), "code"
    symbol = name_to_symbol.get(q_raw) or name_to_symbol.get(q_raw.replace(" ", ""))
    if symbol:
        code = symbol_to_code(symbol)
        return symbol, code_to_name.get(code) or q_raw, "name"
    compact_query = q_raw.replace(" ", "")
    if compact_query:
        for stock_name, candidate_symbol in name_to_symbol.items():
            if compact_query in stock_name.replace(" ", ""):
                code = symbol_to_code(candidate_symbol)
                return candidate_symbol, code_to_name.get(code) or stock_name, "name_partial"
    symbol = normalize_symbol_quick(q_raw)
    if symbol:
        code = symbol_to_code(symbol)
        return symbol, code_to_name.get(code), "fallback"
    return None, None, None

# =============================================================================
# 指標計算
# =============================================================================
def compute_indicators(df, price):
    if df is None or df.empty:
        raise ValueError("下載資料為空")
    if len(df) < 26:
        raise ValueError("歷史資料不足（至少需要 26 筆，MACD 需要較長資料）")

    calc_df = df.copy().reset_index(drop=True)
    close = pd.to_numeric(calc_df["Close"].squeeze(), errors="coerce")
    low = pd.to_numeric(calc_df["Low"].squeeze(), errors="coerce")
    high = pd.to_numeric(calc_df["High"].squeeze(), errors="coerce")
    if close.isna().all() or low.isna().all() or high.isna().all():
        raise ValueError("OHLC 資料格式異常")

    yesterday_close = float(close.iloc[-1])
    yesterday_high = float(high.iloc[-1])
    if pd.isna(yesterday_close) or yesterday_close == 0:
        raise ValueError("昨收資料異常")

    price_val = float(price)
    change_pct = float((price_val / yesterday_close - 1) * 100)

    today_row = pd.DataFrame([{
        "Date": pd.Timestamp(datetime.now(TW_TZ).date()),
        "Open": price_val,
        "High": price_val,
        "Low": price_val,
        "Close": price_val,
        "Volume": 0,
    }])
    calc_df = pd.concat([calc_df, today_row], ignore_index=True)
    close = pd.to_numeric(calc_df["Close"].squeeze(), errors="coerce")
    low = pd.to_numeric(calc_df["Low"].squeeze(), errors="coerce")
    high = pd.to_numeric(calc_df["High"].squeeze(), errors="coerce")

    ma5 = float(close.tail(5).mean())
    ma10 = float(close.tail(10).mean())
    ma20 = float(close.tail(20).mean())

    if price_val > ma5:
        ma_range = ">MA5"
    elif ma5 >= price_val > ma10:
        ma_range = "MA5~10"
    elif ma10 >= price_val > ma20:
        ma_range = "MA10~20"
    else:
        ma_range = "<MA20"

    if ma5 > ma10 > ma20:
        ma_trend = "多頭"
    elif ma5 < ma10 < ma20:
        ma_trend = "空頭"
    else:
        ma_trend = "糾結"

    low_9 = low.rolling(9).min()
    high_9 = high.rolling(9).max()
    denominator = (high_9 - low_9).replace(0, pd.NA)
    rsv = ((close - low_9) / denominator) * 100
    k = rsv.ewm(alpha=1/3, adjust=False).mean()
    d = k.ewm(alpha=1/3, adjust=False).mean()
    if len(k.dropna()) < 2 or len(d.dropna()) < 2:
        raise ValueError("KD 計算資料不足")

    k_t = float(k.iloc[-1])
    d_t = float(d.iloc[-1])
    k_y = float(k.iloc[-2])
    d_y = float(d.iloc[-2])

    if k_y <= d_y and k_t > d_t:
        kd_signal = "黃金交叉"
    elif k_y >= d_y and k_t < d_t:
        kd_signal = "死亡交叉"
    elif k_t < d_t and (d_t - k_t) < 3:
        kd_signal = "即將黃金交叉"
    elif k_t > d_t and (k_t - d_t) < 3:
        kd_signal = "即將死亡交叉"
    elif k_t < 25:
        kd_signal = "超賣"
    else:
        kd_signal = "-"

    # MACD：以補入今日即時價後的 close 序列計算
    ema12 = close.ewm(span=12, adjust=False).mean()
    ema26 = close.ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd_hist = dif - dea
    if len(macd_hist.dropna()) < 2:
        raise ValueError("MACD 計算資料不足")
    macd_hist_t = float(macd_hist.iloc[-1])
    macd_hist_y = float(macd_hist.iloc[-2])
    if macd_hist_y <= 0 and macd_hist_t > 0:
        macd_signal = "MACD翻正"
    elif macd_hist_y >= 0 and macd_hist_t < 0:
        macd_signal = "MACD翻負"
    else:
        macd_signal = "-"

    gap_signal = "-"
    today_low = price_val
    if ENABLE_GAP_SIGNAL and pd.notna(today_low) and pd.notna(yesterday_high) and today_low > yesterday_high:
        gap_signal = "跳空"

    return {
        "price": round(price_val, 2),
        "pct": round(change_pct, 2),
        "yesterday_close": round(yesterday_close, 2),
        "ma_range": ma_range,
        "ma_trend": ma_trend,
        "k": round(k_t, 1),
        "d": round(d_t, 1),
        "kd_signal": kd_signal,
        "macd_hist": round(macd_hist_t, 4),
        "macd_signal": macd_signal,
        "gap_signal": gap_signal,
    }

# =============================================================================
# UI 格式
# =============================================================================
def format_color(val):
    if isinstance(val, (int, float)):
        if val > 0:
            return f"🔴 +{val:.2f}%"
        if val < 0:
            return f"🟢 {val:.2f}%"
        return f"{val:.2f}%"
    return val


def format_k(val):
    if isinstance(val, (int, float)):
        if val >= 74:
            return f"🔴 {val:.1f}"
        if val >= 50:
            return f"🟡 {val:.1f}"
        return f"🟢 {val:.1f}"
    return val


def format_gap(val):
    if val == "跳空":
        return "🔴 跳空"
    return "-"


def build_top3_html(valid_stock_stats):
    if not valid_stock_stats:
        return '<span style="color:#666666;">無可用資料</span>'
    top3_sorted = sorted(valid_stock_stats, key=lambda x: x["pct"], reverse=True)[:3]
    parts = []
    for item in top3_sorted:
        pct = float(item["pct"])
        pct_color = "#cf1322" if pct > 0 else "#389e0d" if pct < 0 else "#333333"
        code_text = escape(str(item["code"]))
        name_text = escape(str(item["name"]))
        pct_text = f"{pct:+.1f}%"
        parts.append(
            f'<span style="color:#000000;">{code_text} {name_text} </span>'
            f'<span style="color:{pct_color}; font-weight:600;">{pct_text}</span>'
        )
    return " | ".join(parts)


def render_summary_dashboard(group_up_summary, rise_threshold):
    # 目標錨點：讓「回到儀表板」可以跳到這裡
    st.markdown('<div id="dashboard-top" style="scroll-margin-top: 90px;"></div>', unsafe_allow_html=True)
    st.markdown("### 📌 漲幅儀表板")
    st.caption(f"目前儀表板統計門檻：漲幅 ≥ {rise_threshold}%")
    html_parts = ['<div class="dashboard-scroll"><div class="dashboard-grid">']

    for item in group_up_summary:
        group_name = escape(str(item["分類"]))
        anchor_id = make_anchor_id(group_name)
        hit_count = item["達標數"]
        total_count = item["總數"]
        up_count = item["上漲數"]
        down_count = item["下跌數"]
        hit_names_text = escape(str(item["達標股票名稱"]))
        top3_html = item["前三名HTML"]
        hit_ratio = (hit_count / total_count * 100) if total_count > 0 else 0
        if hit_ratio >= 60:
            bg_color = "#fff1f0"; border_color = "#ff7875"; accent_color = "#cf1322"
        elif hit_ratio > 0:
            bg_color = "#fff7e6"; border_color = "#ffa940"; accent_color = "#d46b08"
        else:
            bg_color = "#f6ffed"; border_color = "#95de64"; accent_color = "#389e0d"
        html_parts.append(
            f'<a href="#{anchor_id}" class="dashboard-link">'
            f'<div class="dashboard-card" style="background-color:{bg_color}; border:1px solid {border_color}; cursor:pointer;">'
            f'<div class="dashboard-title">{group_name}</div>'
            f'<div class="dashboard-main" style="color:{accent_color};">{hit_count} / {total_count}</div>'
            f'<div class="dashboard-sub">漲幅達標比例（≥{rise_threshold}%）：{hit_ratio:.0f}%</div>'
            f'<div class="dashboard-detail">'
            f'🎯 達標：<b>{hit_count}</b> 檔（{hit_names_text}）<br>'
            f'🔴 一般上漲：<b>{up_count}</b><br>'
            f'🟢 下跌：<b>{down_count}</b>'
            f'</div>'
            f'<div class="dashboard-extra">▶ {top3_html}</div>'
            f'</div></a>'
        )
    html_parts.append("</div></div>")
    st.markdown("".join(html_parts), unsafe_allow_html=True)

# =============================================================================
# Session State 初始化
# =============================================================================
if "auto_refresh_enabled" not in st.session_state:
    st.session_state.auto_refresh_enabled = False
if "tg_push_enabled" not in st.session_state:
    st.session_state.tg_push_enabled = False
if "scheduled_push_enabled" not in st.session_state:
    st.session_state.scheduled_push_enabled = True
if "processed_time_slots" not in st.session_state:
    st.session_state.processed_time_slots = set()
if "stock_groups" not in st.session_state:
    st.session_state.stock_groups = load_stock_groups()
if "group_editor_unlocked" not in st.session_state:
    st.session_state.group_editor_unlocked = False
if "editing_mode" not in st.session_state:
    st.session_state.editing_mode = False
if "fubon_manager" not in st.session_state:
    st.session_state.fubon_manager = FubonRealtimeManager()
if "fubon_logged_in" not in st.session_state:
    st.session_state.fubon_logged_in = False
if "price_source_override" not in st.session_state:
    st.session_state.price_source_override = "auto"
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
if "notified_stocks" not in st.session_state:
    st.session_state.notified_stocks = set()
if "tg_last_update_id" not in st.session_state:
    st.session_state.tg_last_update_id = None
if "market_snapshot" not in st.session_state:
    st.session_state.market_snapshot = None
if "market_snapshot_signature" not in st.session_state:
    st.session_state.market_snapshot_signature = None
if "market_snapshot_at" not in st.session_state:
    st.session_state.market_snapshot_at = None
if "force_market_refresh" not in st.session_state:
    st.session_state.force_market_refresh = False
if "manual_refresh_requested" not in st.session_state:
    st.session_state.manual_refresh_requested = False
if "history_cache_last_clear_at" not in st.session_state:
    st.session_state.history_cache_last_clear_at = None
if "_next_selected_group" in st.session_state:
    pending_group = st.session_state._next_selected_group
    del st.session_state._next_selected_group
    if pending_group in st.session_state.stock_groups:
        st.session_state.selected_group_editor = pending_group
        st.session_state.rename_group_input = pending_group
        st.session_state.symbols_text_area = "\n".join(st.session_state.stock_groups.get(pending_group, []))
if "_clear_quick_add_symbol_input" in st.session_state:
    del st.session_state._clear_quick_add_symbol_input
    st.session_state.quick_add_symbol_input = ""
if "_quick_add_success_message" in st.session_state:
    st.toast(st.session_state._quick_add_success_message)
    del st.session_state._quick_add_success_message


def set_next_selected_group(group_name: str):
    st.session_state._next_selected_group = group_name


def enter_edit_mode():
    st.session_state.editing_mode = True


def leave_edit_mode():
    st.session_state.editing_mode = False


def sync_editor_fields_from_selected_group():
    groups = st.session_state.stock_groups
    selected_group = st.session_state.selected_group_editor
    if selected_group not in groups:
        group_names = list(groups.keys())
        if group_names:
            selected_group = group_names[0]
            st.session_state.selected_group_editor = selected_group
        else:
            selected_group = ""
    st.session_state.rename_group_input = selected_group
    st.session_state.symbols_text_area = "\n".join(groups.get(selected_group, []))
    st.session_state.editing_mode = False

# =============================================================================
# 富邦登入 UI
# =============================================================================
def get_fubon_pfx_base64():
    try:
        return st.secrets["fubon"]["pfx_base64"]
    except Exception:
        return ""


def render_fubon_login():
    st.sidebar.markdown("## 🔑 富邦 WebSocket 即時價")
    manager = st.session_state.fubon_manager
    status = manager.get_status()

    if st.sidebar.button("清除富邦連線狀態", width="stretch"):
        st.session_state.fubon_manager = FubonRealtimeManager()
        st.session_state.fubon_logged_in = False
        st.session_state.pop("fubon_login_time", None)
        st.rerun()

    if FubonSDK is None:
        st.sidebar.warning("富邦 SDK 未載入，當日價格會使用 yfinance fallback。")
        return

    if st.session_state.fubon_logged_in:
        st.sidebar.success("✅ 富邦 WebSocket 已連線")
        st.sidebar.caption(f"已訂閱：{status['subscribed_count']} 檔")
        if status["last_message_at"]:
            st.sidebar.caption(f"最後資料：{status['last_message_at'].strftime('%H:%M:%S')}")
        if status["error"]:
            st.sidebar.warning(status["error"])
        if st.sidebar.button("登出 / 重新連線富邦", width="stretch"):
            st.session_state.fubon_manager = FubonRealtimeManager()
            st.session_state.fubon_logged_in = False
            st.session_state.pop("fubon_login_time", None)
            st.rerun()
        return

    pfx_base64 = get_fubon_pfx_base64()
    if not pfx_base64:
        st.sidebar.warning("未設定 st.secrets['fubon']['pfx_base64']，當日價格會使用 yfinance fallback。")
        return

    with st.sidebar.expander("富邦登入", expanded=False):
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
                    st.session_state.pop("fubon_login_time", None)
                    st.error(f"富邦登入失敗：{e}")
                    st.exception(e)

# =============================================================================
# 分組 UI
# =============================================================================
def render_group_editor_lock():
    st.sidebar.markdown("## 🔐 分組編輯鎖")
    if st.session_state.group_editor_unlocked:
        st.sidebar.success("已解鎖，可編輯股票分組")
        st.sidebar.info("為避免編輯中被重刷，分組編輯解鎖時會暫停自動更新")
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
            st.sidebar.success("PIN 正確，已解鎖")
            st.rerun()
        else:
            st.sidebar.error("PIN 錯誤")


def render_stock_group_editor():
    st.sidebar.markdown("## 🛠️ 股票分組編輯")
    groups = st.session_state.stock_groups
    group_names = list(groups.keys())
    if not group_names:
        st.session_state.stock_groups = copy.deepcopy(DEFAULT_STOCK_GROUPS)
        groups = st.session_state.stock_groups
        group_names = list(groups.keys())
    if st.session_state.selected_group_editor not in group_names:
        first_group = group_names[0]
        st.session_state.selected_group_editor = first_group
        st.session_state.rename_group_input = first_group
        st.session_state.symbols_text_area = "\n".join(groups.get(first_group, []))

    with st.sidebar.expander("➕ 新增分類", expanded=False):
        new_group_name = st.text_input("分類名稱", key="new_group_name_input")
        if st.button("新增分類", key="add_group_btn", width="stretch"):
            enter_edit_mode()
            name = new_group_name.strip()
            if not name:
                st.sidebar.warning("請輸入分類名稱")
            elif name in groups:
                st.sidebar.warning("分類名稱已存在")
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
        symbols_text = st.text_area("股票清單（每行一檔，或逗號分隔）", height=220, key="symbols_text_area", on_change=enter_edit_mode)
        st.markdown("### ⚡ 快速新增股票搜尋")
        quick_col1, quick_col2 = st.columns([2, 1])
        with quick_col1:
            quick_input = st.text_input("輸入股票代碼、名稱或 ticker", key="quick_add_symbol_input", on_change=enter_edit_mode)
        resolved_symbol, resolved_name, resolved_type = resolve_stock_query(quick_input)
        if quick_input.strip():
            if resolved_symbol:
                if resolved_name:
                    if resolved_type in ["code", "ticker"]:
                        st.caption(f"查詢結果：{resolved_name} / 將加入：{resolved_symbol}")
                    elif resolved_type in ["name", "name_partial"]:
                        st.caption(f"查詢結果：{resolved_name} → {resolved_symbol}")
                    else:
                        st.caption(f"標準化代碼：{resolved_symbol}")
                else:
                    st.caption(f"標準化代碼：{resolved_symbol}")
            else:
                st.caption("查無對應股票，請確認 TWstocklistname.txt 或輸入完整 ticker")
        with quick_col2:
            if st.button("加入目前分類", key="quick_add_btn", width="stretch"):
                enter_edit_mode()
                symbol, stock_name_for_msg, _ = resolve_stock_query(quick_input)
                if not symbol:
                    st.warning("請輸入股票代碼或股票名稱")
                else:
                    current_list = groups.get(selected_group, [])
                    if symbol in current_list:
                        st.warning("此股票已存在於目前分類")
                    else:
                        current_list.append(symbol)
                        groups[selected_group] = current_list
                        st.session_state.stock_groups = groups
                        save_stock_groups(groups)
                        set_next_selected_group(selected_group)
                        st.session_state._clear_quick_add_symbol_input = True
                        if stock_name_for_msg:
                            st.session_state._quick_add_success_message = f"已加入 {symbol}（{stock_name_for_msg}）"
                        else:
                            st.session_state._quick_add_success_message = f"已加入 {symbol}"
                        st.rerun()
        col1, col2 = st.columns(2)
        with col1:
            if st.button("💾 儲存分類", key="save_group_btn", width="stretch"):
                new_name = new_group_name.strip()
                if not new_name:
                    st.sidebar.warning("分類名稱不可為空")
                elif new_name != selected_group and new_name in groups:
                    st.sidebar.warning("分類名稱已存在，請使用其他名稱")
                else:
                    updated = {}
                    for k, v in groups.items():
                        updated[new_name if k == selected_group else k] = normalize_symbols_from_text(symbols_text) if k == selected_group else v
                    st.session_state.stock_groups = updated
                    save_stock_groups(updated)
                    leave_edit_mode()
                    set_next_selected_group(new_name)
                    st.rerun()
        with col2:
            if st.button("🗑️ 刪除分類", key="delete_group_btn", width="stretch"):
                if len(groups) <= 1:
                    st.sidebar.warning("至少保留一個分類")
                else:
                    groups.pop(selected_group, None)
                    st.session_state.stock_groups = groups
                    save_stock_groups(groups)
                    leave_edit_mode()
                    set_next_selected_group(list(groups.keys())[0])
                    st.rerun()

    with st.sidebar.expander("📦 備份 / 匯出 / 匯入 JSON", expanded=False):
        export_json_str = json.dumps(st.session_state.stock_groups, ensure_ascii=False, indent=2)
        st.download_button(label="⬇️ 匯出目前分組 JSON", data=export_json_str, file_name="stock_groups.json", mime="application/json", key="download_groups_json_btn", width="stretch")
        if st.button("🗂️ 建立本地備份", key="create_local_backup_btn", width="stretch"):
            try:
                backup_file = save_backup_snapshot(st.session_state.stock_groups)
                st.sidebar.success(f"已建立備份：{os.path.basename(backup_file)}")
            except Exception as e:
                st.sidebar.error(f"建立備份失敗：{e}")
        uploaded_file = st.file_uploader("上傳股票分組 JSON", type=["json"], key="upload_groups_json_file")
        if uploaded_file is not None:
            st.caption("上傳後按下「匯入並覆蓋目前分組」才會生效")
            if st.button("📥 匯入並覆蓋目前分組", key="import_groups_json_btn", width="stretch"):
                try:
                    raw = uploaded_file.read()
                    data = json.loads(raw.decode("utf-8"))
                    validated = validate_and_normalize_group_json(data)
                    save_backup_snapshot(st.session_state.stock_groups)
                    st.session_state.stock_groups = validated
                    save_stock_groups(validated)
                    leave_edit_mode()
                    set_next_selected_group(list(validated.keys())[0])
                    st.sidebar.success("JSON 匯入成功，已覆蓋目前股票分組")
                    st.rerun()
                except Exception as e:
                    st.sidebar.error(f"JSON 匯入失敗：{e}")
        backups = list_backup_files()
        if backups:
            st.markdown("**最近備份檔**")
            for name in backups[:5]:
                st.caption(name)
        else:
            st.caption("目前沒有本地備份檔")

    with st.sidebar.expander("♻️ 重設", expanded=False):
        if st.button("還原預設分組", key="reset_groups_btn", width="stretch"):
            try:
                save_backup_snapshot(st.session_state.stock_groups)
            except Exception:
                pass
            st.session_state.stock_groups = copy.deepcopy(DEFAULT_STOCK_GROUPS)
            save_stock_groups(st.session_state.stock_groups)
            leave_edit_mode()
            set_next_selected_group(list(st.session_state.stock_groups.keys())[0])
            st.rerun()

    with st.sidebar.expander("👀 分組預覽", expanded=False):
        for g, symbols in st.session_state.stock_groups.items():
            st.markdown(f"**{g}**（{len(symbols)}檔）")
            st.caption(", ".join(symbols) if symbols else "（空）")


# =============================================================================
# 市場資料快照：避免每個 widget rerun 都重新抓資料
# =============================================================================
def get_stock_groups_signature(groups: dict) -> str:
    """用股票分組內容產生穩定簽章；只有分組改變才需要重抓市場資料。"""
    try:
        normalized = {str(k): list(v) for k, v in groups.items()}
        return json.dumps(normalized, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(groups)


def should_rebuild_market_snapshot(signature: str, can_push_now: bool = False) -> tuple[bool, str]:
    """
    重建行情快照規則：
    1. 盤中 09:00~13:30：畫面可每 REFRESH_SEC 重算一次，但 yfinance 歷史資料由 @st.cache_data(ttl=3600) 控制每小時最多抓一次。
    2. 13:30 後：不自動重抓，只有按「手動更新」才重抓 yfinance / Yahoo TW。
    3. 一般 widget 變更：不重抓，只使用既有快照。
    """
    manual_refresh = st.session_state.get("force_market_refresh", False)

    if manual_refresh:
        return True, "手動更新"

    # 13:30 後完全停止自動重抓；若沒有快照，也提示使用者手動更新。
    if is_after_1330():
        if st.session_state.market_snapshot is None:
            return False, "13:30 後請按手動更新抓取收盤資料"
        return False, "13:30 後停止自動抓取，等待手動更新"

    # 盤前也不要自動打 yfinance；若需要盤前資料，按手動更新。
    if is_before_market_open():
        if st.session_state.market_snapshot is None:
            return False, "盤前請按手動更新抓取資料"
        return False, "盤前使用既有快照"

    # 盤中第一次載入或分組/價格模式改變，需要建立快照。
    if st.session_state.market_snapshot is None:
        return True, "盤中首次載入"
    if st.session_state.market_snapshot_signature != signature:
        return True, "股票分組或價格模式改變"
    if can_push_now:
        return True, "推播時段掃描"

    # 盤中自動更新：只重算快照與 WebSocket 即時價；yfinance 歷史資料因 TTL=3600 不會一直打。
    if st.session_state.auto_refresh_enabled and not st.session_state.group_editor_unlocked and not st.session_state.editing_mode:
        last_at = st.session_state.market_snapshot_at
        if last_at is None:
            return True, "盤中自動更新"
        elapsed = (datetime.now(TW_TZ) - last_at).total_seconds()
        if elapsed >= REFRESH_SEC:
            return True, f"盤中自動更新 {REFRESH_SEC}s；歷史資料每小時快取"

    return False, "使用快照快取"

def build_market_snapshot(stock_groups, manager, can_push_now, current_schedule_key, manual_push_triggered, tw_now):
    """真正會呼叫 download/price API 的地方；只在 should_rebuild_market_snapshot=True 時執行。"""
    group_tables = {}
    group_stats = {}

    for group_name, stocks in stock_groups.items():
        rows = []
        up_count = down_count = flat_count = error_count = 0
        valid_stock_stats = []

        for symbol in stocks:
            try:
                raw_df = download_stock_data(symbol)
                df = normalize_ohlc(raw_df)
                if df.empty:
                    raise ValueError("無法解析歷史資料欄位格式")

                price, price_source = get_last_price(symbol, df, manager)
                stock_name = get_stock_name(symbol)
                data = compute_indicators(df, price)

                is_high_gain = data["pct"] >= 5
                has_kd_signal = data["kd_signal"] in ["黃金交叉", "即將黃金交叉"]
                has_gap_signal = data["gap_signal"] == "跳空"

                if is_high_gain or has_kd_signal or has_gap_signal or has_macd_signal:
                    base_symbol = symbol.split('.')[0]
                    yahoo_url = f"https://tw.stock.yahoo.com/quote/{base_symbol}"
                    symbol_link = f'[{symbol}]({yahoo_url})'
                    today_str = tw_now.strftime("%Y-%m-%d")
                    notify_key = f"{symbol}_{today_str}"

                    if can_push_now and (notify_key not in st.session_state.notified_stocks):
                        msg = (
                            f"🔔 **強勢股達標通知：{stock_name} ({symbol_link})**\n\n"
                            f"📈 價格：{data['price']}\n"
                            f"🔥 漲幅：{data['pct']:+.2f}%\n"
                            f"📊 KD訊號：{data['kd_signal']}\n"
                            f"🚀 跳空訊號：{data['gap_signal']}\n"
                            f"📡 價格來源：{price_source}"
                        )
                        send_telegram_message(msg)
                        st.session_state.notified_stocks.add(notify_key)

                if data["pct"] > 0:
                    up_count += 1
                elif data["pct"] < 0:
                    down_count += 1
                else:
                    flat_count += 1

                valid_stock_stats.append({
                    "symbol": symbol,
                    "code": symbol_to_code(symbol),
                    "name": stock_name,
                    "pct": float(data["pct"]),
                })

                rows.append({
                    "代碼": symbol,
                    "代碼網址": yahoo_quote_url(symbol),
                    "股票名稱": stock_name,
                    "價格": f"{data['price']:.2f}",
                    "昨收": f"{data['yesterday_close']:.2f}",
                    "漲跌%": data["pct"],
                    "MA位置": data["ma_range"],
                    "MA排列": data["ma_trend"],
                    "K值": data["k"],
                    "D值": f"{data['d']:.1f}",
                    "KD訊號": data["kd_signal"],
                    "MACD柱": f"{data['macd_hist']:.2f}",
                    "MACD訊號": data["macd_signal"],
                    "跳空訊號": data["gap_signal"],
                    "價格來源": price_source,
                })

            except Exception as e:
                error_count += 1
                rows.append({
                    "代碼": symbol,
                    "代碼網址": "",
                    "股票名稱": get_stock_name(symbol),
                    "價格": "錯誤",
                    "昨收": "-",
                    "漲跌%": "-",
                    "MA位置": "-",
                    "MA排列": "-",
                    "K值": "-",
                    "D值": "-",
                    "KD訊號": "-",
                    "MACD柱": "-",
                    "MACD訊號": "-",
                    "跳空訊號": str(e),
                    "價格來源": "-",
                })

        df_table = pd.DataFrame(rows)
        display_df = df_table.copy()
        if not display_df.empty:
            display_df["漲跌%"] = display_df["漲跌%"].apply(format_color)
            display_df["K值"] = display_df["K值"].apply(format_k)
            display_df["跳空訊號"] = display_df["跳空訊號"].apply(format_gap)

        group_tables[group_name] = {"count": len(stocks), "table": display_df}
        group_stats[group_name] = {
            "分類": group_name,
            "valid_stock_stats": valid_stock_stats,
            "上漲數": up_count,
            "下跌數": down_count,
            "平盤數": flat_count,
            "錯誤數": error_count,
            "總數": len(stocks),
        }

    if can_push_now and st.session_state.scheduled_push_enabled and current_schedule_key and not manual_push_triggered:
        st.session_state.processed_time_slots.add(current_schedule_key)

    return {
        "group_tables": group_tables,
        "group_stats": group_stats,
        "built_at": datetime.now(TW_TZ),
    }


def build_group_up_summary_from_snapshot(snapshot, rise_threshold):
    """根據目前 slider 門檻重算儀表板；不需要重新抓任何資料。"""
    group_up_summary = []
    for group_name, stats in snapshot.get("group_stats", {}).items():
        valid_stock_stats = stats.get("valid_stock_stats", [])
        hit_items = [x for x in valid_stock_stats if float(x.get("pct", 0)) >= rise_threshold]
        hit_names = [x.get("name", "") for x in hit_items]
        group_up_summary.append({
            "分類": group_name,
            "達標數": len(hit_items),
            "達標股票名稱": compact_name_list(hit_names, max_show=4),
            "前三名HTML": build_top3_html(valid_stock_stats),
            "上漲數": stats.get("上漲數", 0),
            "下跌數": stats.get("下跌數", 0),
            "平盤數": stats.get("平盤數", 0),
            "錯誤數": stats.get("錯誤數", 0),
            "總數": stats.get("總數", 0),
        })
    return group_up_summary

# =============================================================================
# 主畫面
# =============================================================================
if os.path.exists(APP_LOGO):
    title_icon_col, title_text_col = st.columns([0.45, 8])
    with title_icon_col:
        st.image(APP_LOGO, width=58)
    with title_text_col:
        st.markdown(
            """
            <h1 style="margin:0; padding-top:4px; font-size:42px; font-weight:800; line-height:1.2;">
                股票監控面板 - 告訴我你會買日月光
            </h1>
            """,
            unsafe_allow_html=True,
        )
else:
    st.markdown(
        """
        <h1 style="margin:0; padding-top:4px; font-size:42px; font-weight:800; line-height:1.2;">
            📊 股票監控面板 - 告訴我你會買日月光
        </h1>
        """,
        unsafe_allow_html=True,
    )

col1, col2, col3, col4 = st.columns([1, 1, 1, 1])
with col1:
    if st.button("🔄 手動更新 / 13:30後抓收盤資料", width="stretch"):
        st.cache_data.clear()
        st.session_state.force_market_refresh = True
        st.session_state.manual_refresh_requested = True
        st.session_state.history_cache_last_clear_at = datetime.now(TW_TZ)
        st.rerun()
with col2:
    auto_refresh = st.toggle("⏱️ 啟用自動更新 (30秒)", value=st.session_state.auto_refresh_enabled)
    if auto_refresh != st.session_state.auto_refresh_enabled:
        st.session_state.auto_refresh_enabled = auto_refresh
        st.rerun()
with col3:
    tg_push = st.toggle("📲 Telegram 推送開關", value=st.session_state.tg_push_enabled, help="必須開啟此選項，機器人才會發送推播")
    if tg_push != st.session_state.tg_push_enabled:
        st.session_state.tg_push_enabled = tg_push
        st.rerun()
with col4:
    sched_push = st.toggle("⏰ 定時推送模式", value=st.session_state.scheduled_push_enabled, help="開啟後，僅在 09:40, 10:00, 11:00, 12:00, 13:00 執行推播檢查")
    if sched_push != st.session_state.scheduled_push_enabled:
        st.session_state.scheduled_push_enabled = sched_push
        st.rerun()

gc.collect()

render_fubon_login()
render_group_editor_lock()
if st.session_state.group_editor_unlocked:
    render_stock_group_editor()
else:
    st.sidebar.info("目前為唯讀模式：輸入 PIN 後才能修改股票分組")

tw_now = datetime.now(TW_TZ)
st.caption(f"更新時間：{tw_now.strftime('%Y-%m-%d %H:%M:%S')}")
rise_threshold = st.slider("儀表板漲幅達標門檻 (%)", min_value=5, max_value=9, value=5, step=1)

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

with st.sidebar.expander("📡 富邦 WebSocket 狀態", expanded=True):
    status = manager.get_status()
    if status["connected"]:
        st.markdown('<span class="ws-ok">● Connected</span>', unsafe_allow_html=True)
    else:
        st.markdown('<span class="ws-bad">● Not connected</span>', unsafe_allow_html=True)
    st.caption(f"已訂閱：{status['subscribed_count']} 檔")
    if status["last_message_at"]:
        st.caption(f"最後資料：{status['last_message_at'].strftime('%H:%M:%S')}")
    if status["error"]:
        st.warning(status["error"])

with st.sidebar.expander("🕒 價格來源模式", expanded=True):
    current_mode = st.session_state.get("price_source_override", "auto")
    if current_mode == "websocket":
        st.info("目前價格模式：強制 WebSocket。再次按 WebSocket 可回到自動模式。")
    elif current_mode == "yfinance":
        st.info("目前價格模式：強制 Yfinance，抓值邏輯同 13:30 後。再次按 Yfinance 可回到自動模式。")
    else:
        if is_fubon_realtime_time():
            st.info("目前價格模式：自動；09:00~13:30 優先 WebSocket，歷史資料每小時快取")
        else:
            st.info("目前價格模式：自動；13:30 後停止自動抓資料，需按手動更新")
    mode_col1, mode_col2 = st.columns(2)
    with mode_col1:
        ws_button_type = "primary" if current_mode == "websocket" else "secondary"
        if st.button("WebSocket", key="force_websocket_price_btn", width="stretch", type=ws_button_type):
            st.session_state.price_source_override = "auto" if current_mode == "websocket" else "websocket"
            st.rerun()
    with mode_col2:
        yf_button_type = "primary" if current_mode == "yfinance" else "secondary"
        if st.button("Yfinance", key="force_yfinance_price_btn", width="stretch", type=yf_button_type):
            st.session_state.price_source_override = "auto" if current_mode == "yfinance" else "yfinance"
            st.rerun()

# ===== 推送時間與手動指令邏輯判斷 =====
can_push_now = False
current_schedule_key = None
manual_push_triggered = False
if st.session_state.tg_push_enabled:
    manual_push_triggered = check_telegram_push_command()
    if manual_push_triggered:
        can_push_now = True
        st.session_state.notified_stocks = set()
        st.toast("🚀 收到 'push' 指令，強制觸發推播！")
        send_telegram_message("🤖 <b>收到指令，開始為您掃描並強制推播強勢股...</b>")
    elif st.session_state.scheduled_push_enabled:
        TARGET_TIMES = [
            tw_now.replace(hour=9, minute=40, second=0, microsecond=0),
            tw_now.replace(hour=10, minute=0, second=0, microsecond=0),
            tw_now.replace(hour=11, minute=0, second=0, microsecond=0),
            tw_now.replace(hour=12, minute=0, second=0, microsecond=0),
            tw_now.replace(hour=13, minute=0, second=0, microsecond=0),
        ]
        for target_dt in TARGET_TIMES:
            diff_seconds = (tw_now - target_dt).total_seconds()
            if abs(diff_seconds) <= 45:
                time_str = target_dt.strftime("%H%M")
                today_str = tw_now.strftime("%Y%m%d")
                current_schedule_key = f"slot_{today_str}_{time_str}"
                if current_schedule_key not in st.session_state.processed_time_slots:
                    can_push_now = True
                    break

# ===== 資料快照計算：只有必要時才抓資料，slider / sidebar widget 不會觸發重抓 =====
price_mode_for_signature = st.session_state.get("price_source_override", "auto")
groups_signature = get_stock_groups_signature(st.session_state.stock_groups)
market_signature = f"{groups_signature}|price_mode={price_mode_for_signature}|date={tw_now.strftime('%Y%m%d')}"
need_rebuild, rebuild_reason = should_rebuild_market_snapshot(market_signature, can_push_now=can_push_now)

if need_rebuild:
    with st.spinner(f"更新行情資料中...（{rebuild_reason}）"):
        st.session_state.market_snapshot = build_market_snapshot(
            st.session_state.stock_groups,
            manager,
            can_push_now,
            current_schedule_key,
            manual_push_triggered,
            tw_now,
        )
        st.session_state.market_snapshot_signature = market_signature
        st.session_state.market_snapshot_at = datetime.now(TW_TZ)
        st.session_state.force_market_refresh = False

snapshot = st.session_state.market_snapshot or {"group_tables": {}, "group_stats": {}}
group_tables = snapshot.get("group_tables", {})
group_up_summary = build_group_up_summary_from_snapshot(snapshot, rise_threshold)

if st.session_state.market_snapshot_at:
    st.caption(
        f"行情快照：{st.session_state.market_snapshot_at.strftime('%Y-%m-%d %H:%M:%S')}；"
        f"狀態：{'已重抓 - ' + rebuild_reason if need_rebuild else rebuild_reason}；"
        f"yfinance歷史資料TTL：{HISTORY_CACHE_TTL // 60}分鐘"
    )
else:
    st.info(f"尚未建立行情快照：{rebuild_reason}。若需要抓資料，請按上方『手動更新 / 13:30後抓收盤資料』。")

render_summary_dashboard(group_up_summary, rise_threshold)
st.divider()
for group_name, info in group_tables.items():
    anchor_id = make_anchor_id(group_name)
    st.markdown(f'<div id="{anchor_id}" style="scroll-margin-top: 80px;"></div>', unsafe_allow_html=True)
    header_col1, header_col2 = st.columns([8, 2])
    with header_col1:
        st.subheader(f"【{group_name}】({info['count']}檔)")
    with header_col2:
        st.markdown(
            '<div style="text-align:right; padding-top:0.4rem;">'
            '<a href="#dashboard-top" class="back-to-dashboard-btn">⬆️ 回到儀表板</a>'
            '</div>',
            unsafe_allow_html=True,
        )
    table_df = info["table"].copy()
    if not table_df.empty and "代碼網址" in table_df.columns:
        table_df["代碼"] = table_df["代碼網址"]
    display_columns = [
        "代碼", "股票名稱", "價格", "昨收", "漲跌%", "MA位置", "MA排列",
        "K值", "D值", "KD訊號", "MACD柱", "MACD訊號", "跳空訊號", "價格來源",
    ]
    st.dataframe(
        table_df[display_columns],
        width="stretch",
        column_config={
            "代碼": st.column_config.LinkColumn("代碼", help="點擊前往台股 Yahoo", display_text=r"https://tw.stock.yahoo.com/quote/(.*)"),
            "股票名稱": st.column_config.TextColumn("股票名稱"),
        },
    )
    st.markdown('<div style="margin-bottom: 10px;"></div>', unsafe_allow_html=True)

with st.sidebar.expander("🔍 WebSocket Debug", expanded=False):
    debug_code = st.text_input("輸入代碼看最後 WS 原始訊息", value="4919")
    msg = manager.get_message(debug_code)
    if msg:
        st.caption(f"時間：{msg['time'].strftime('%Y-%m-%d %H:%M:%S')}")
        st.json(msg["raw"])
    else:
        st.caption("尚未收到此代碼的 WebSocket 訊息")

if st.session_state.auto_refresh_enabled and not st.session_state.group_editor_unlocked and not st.session_state.editing_mode:
    time.sleep(REFRESH_SEC)
    st.rerun()
