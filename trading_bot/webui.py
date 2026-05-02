"""
webui.py — واجهة ويب لمراقبة نظام التداول
تشغيل: uvicorn webui:app --host 0.0.0.0 --port 8080
"""

import json
import os
import sys
from pathlib import Path
from decimal import Decimal
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from dotenv import load_dotenv
from loguru import logger
from binance.client import Client as BinanceClient
from binance.exceptions import BinanceAPIException

import database as db

load_dotenv()

logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")
logger.add("bot.log", rotation="10 MB", retention="7 days",
           format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}")

app = FastAPI(title="نظام التداول الذكي", docs_url=None, redoc_url=None)

WEBUI_PORT      = int(os.getenv("WEBUI_PORT", 8080))
INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", 60))
TRADING_ENABLED  = os.getenv("TRADING_ENABLED", "false").lower() == "true"


def jsonable(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, list):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    return value


HTML = r"""<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>⚡ بوت المضاربة السريعة</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
  body{font-family:'Segoe UI',Tahoma,sans-serif}
  @keyframes pulse-dot{0%,100%{opacity:1}50%{opacity:.2}}
  .pulse{animation:pulse-dot 1.4s infinite}
  @keyframes spin{to{transform:rotate(360deg)}}
  .spin{animation:spin 1s linear infinite;display:inline-block}
  .confidence-bar{height:8px;border-radius:4px;transition:width .6s ease}
</style>
</head>
<body class="bg-gray-950 text-gray-100 min-h-screen">

<!-- ══════════════════ HEADER ══════════════════ -->
<header class="bg-gray-900 border-b border-gray-800 sticky top-0 z-50">
  <div class="max-w-7xl mx-auto px-4 py-3 flex items-center justify-between gap-4">
    <div class="flex items-center gap-3">
      <span class="text-3xl">🤖</span>
      <div>
        <h1 class="text-lg font-bold text-white leading-none">⚡ بوت المضاربة السريعة</h1>
        <p class="text-xs text-gray-500">Scalping — Binance Spot — كل دقيقة</p>
      </div>
    </div>
    <div class="flex items-center gap-3 flex-wrap justify-end">
      <div id="phase-badge" class="px-3 py-1 rounded-full text-xs font-bold bg-gray-700 text-gray-300">جارٍ التحميل...</div>
      <div class="text-center">
        <p class="text-xs text-gray-500">تحديث تلقائي</p>
        <p id="refresh-timer" class="text-sm font-mono text-gray-400">10ث</p>
      </div>
    </div>
  </div>
</header>

<main class="max-w-7xl mx-auto px-4 py-6 space-y-6">

<!-- ══════════════════ STATS ══════════════════ -->
<div class="grid grid-cols-2 md:grid-cols-4 gap-3">
  <div class="bg-gray-900 rounded-xl p-4 border border-gray-800">
    <p class="text-xs text-gray-500 mb-1">💰 رصيد USDT</p>
    <p id="stat-balance" class="text-2xl font-bold text-yellow-400">—</p>
    <p id="stat-portfolio" class="text-xs text-cyan-400 mt-0.5">محفظة: —</p>
    <p id="stat-balance-mode" class="text-xs text-gray-600 mt-1">—</p>
  </div>
  <div class="bg-gray-900 rounded-xl p-4 border border-gray-800">
    <p class="text-xs text-gray-500 mb-1">📂 مراكز مفتوحة</p>
    <p id="stat-open" class="text-2xl font-bold text-blue-400">—</p>
    <p id="stat-trades" class="text-xs text-gray-500 mt-0.5">إجمالي صفقات: —</p>
  </div>
  <div class="bg-gray-900 rounded-xl p-4 border border-gray-800">
    <p class="text-xs text-gray-500 mb-1">💵 ربح اليوم</p>
    <p id="stat-pnl" class="text-2xl font-bold">—</p>
    <p id="stat-pnl-total" class="text-xs text-gray-500 mt-0.5">إجمالي: —</p>
  </div>
  <div class="bg-gray-900 rounded-xl p-4 border border-gray-800">
    <p class="text-xs text-gray-500 mb-1">📊 آخر تحديث</p>
    <p id="stat-last-update" class="text-lg font-bold text-gray-300">—</p>
    <p id="stat-scalp-status" class="text-xs mt-0.5">—</p>
  </div>
</div>


<!-- ══════════════════ COINS MANAGER ══════════════════ -->
<div class="bg-gray-900 rounded-xl border border-gray-800">
  <!-- رأس القسم قابل للطي -->
  <button onclick="toggleCoinsPanel()" class="w-full flex items-center justify-between p-5 text-right">
    <h2 class="text-lg font-bold text-white flex items-center gap-2">
      🎯 العملات المختارة للتداول
      <span id="coins-count-badge" class="px-2 py-0.5 rounded-full bg-blue-900 text-blue-300 text-xs font-normal">—</span>
    </h2>
    <span id="coins-toggle-icon" class="text-gray-400 text-xl">▼</span>
  </button>

  <div id="coins-panel" class="border-t border-gray-800 p-5">
    <!-- أدوات البحث والتصفية -->
    <div class="flex flex-wrap gap-3 mb-4">
      <input id="coins-search" type="text" placeholder="ابحث عن عملة..." oninput="filterCoins()"
        class="flex-1 min-w-[180px] bg-gray-800 border border-gray-700 rounded-lg px-3 py-2 text-sm text-white placeholder-gray-500 outline-none focus:border-blue-500"/>
      <div class="flex gap-2 flex-wrap">
        <button onclick="selectAllCoins()" class="px-3 py-1.5 text-xs bg-green-900 text-green-300 rounded-lg hover:bg-green-800">تحديد الكل</button>
        <button onclick="deselectAllCoins()" class="px-3 py-1.5 text-xs bg-red-900 text-red-300 rounded-lg hover:bg-red-800">إلغاء الكل</button>
        <button onclick="saveCoinsConfig()" class="px-4 py-1.5 text-sm bg-blue-600 text-white rounded-lg font-bold hover:bg-blue-500">💾 حفظ التغييرات</button>
      </div>
    </div>

    <!-- تبويبات الفئات -->
    <div id="cat-tabs" class="flex flex-wrap gap-2 mb-4">
      <button onclick="setCat('الكل')" data-cat="الكل"
        class="cat-tab px-3 py-1 text-xs rounded-full bg-white text-gray-900 font-bold">الكل</button>
      <button onclick="setCat('الكبار')" data-cat="الكبار"
        class="cat-tab px-3 py-1 text-xs rounded-full bg-gray-800 text-gray-400 hover:bg-gray-700">الكبار</button>
      <button onclick="setCat('DeFi')" data-cat="DeFi"
        class="cat-tab px-3 py-1 text-xs rounded-full bg-gray-800 text-gray-400 hover:bg-gray-700">DeFi</button>
      <button onclick="setCat('L1/L2')" data-cat="L1/L2"
        class="cat-tab px-3 py-1 text-xs rounded-full bg-gray-800 text-gray-400 hover:bg-gray-700">Layer 1/2</button>
      <button onclick="setCat('Meme')" data-cat="Meme"
        class="cat-tab px-3 py-1 text-xs rounded-full bg-gray-800 text-gray-400 hover:bg-gray-700">Meme</button>
      <button onclick="setCat('AI')" data-cat="AI"
        class="cat-tab px-3 py-1 text-xs rounded-full bg-gray-800 text-gray-400 hover:bg-gray-700">AI</button>
      <button onclick="setCat('Gaming')" data-cat="Gaming"
        class="cat-tab px-3 py-1 text-xs rounded-full bg-gray-800 text-gray-400 hover:bg-gray-700">Gaming</button>
      <button onclick="setCat('بنية')" data-cat="بنية"
        class="cat-tab px-3 py-1 text-xs rounded-full bg-gray-800 text-gray-400 hover:bg-gray-700">بنية تحتية</button>
    </div>

    <!-- شبكة العملات -->
    <div id="coins-grid-mgr" class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6 gap-2"></div>

    <!-- رسالة الحفظ -->
    <p id="save-msg" class="text-sm text-green-400 mt-3 hidden"></p>
  </div>
</div>

  <!-- توجه السوق + الثقة -->
  <div class="flex flex-wrap items-center gap-4 mb-4 p-4 bg-gray-950 rounded-xl border border-gray-800">
    <div class="text-center min-w-[100px]">
      <p class="text-xs text-gray-500 mb-1">توجه السوق</p>
      <p id="report-sentiment" class="text-2xl font-bold">—</p>
    </div>
    <div class="flex-1 min-w-[140px]">
      <div class="flex justify-between text-xs mb-1">
        <span class="text-gray-500">مستوى الثقة</span>
        <span id="report-confidence-val" class="text-white font-bold">—</span>
      </div>
      <div class="bg-gray-800 rounded-full h-2">
        <div id="report-confidence-bar" class="confidence-bar bg-yellow-500" style="width:0%"></div>
      </div>
    </div>
    <div class="text-center min-w-[80px]">
      <p class="text-xs text-gray-500 mb-1">التغريدات</p>
      <p id="report-tweets-count" class="text-xl font-bold text-cyan-400">—</p>
    </div>
  </div>

  <!-- الملخص -->
  <div class="bg-gray-950 rounded-lg p-3 border border-gray-800 mb-4">
    <p class="text-xs text-gray-500 mb-1">📝 ملخص التحليل</p>
    <p id="report-summary" class="text-sm text-gray-300 leading-relaxed">—</p>
  </div>

  <!-- التوصيات -->
  <div>
    <h3 class="font-bold text-white mb-3 flex items-center gap-2">🎯 توصيات التداول</h3>
    <div id="report-recs" class="grid grid-cols-1 md:grid-cols-2 gap-3">
      <p class="text-gray-500 text-sm">لا توجد توصيات بعد</p>
    </div>
  </div>
</div>


<!-- ══════════════════ SCALPER ══════════════════ -->
<div class="bg-gray-900 rounded-xl border border-gray-800 p-5">
  <div class="flex items-center justify-between mb-4 flex-wrap gap-3">
    <div>
      <h2 class="text-lg font-bold text-white flex items-center gap-2">
        ⚡ المضاربة السريعة
        <span id="scalp-interval-badge" class="text-xs text-green-500 font-normal">● يعمل — كل دقيقة</span>
      </h2>
    </div>
    <div class="flex items-center gap-3">
      <!-- إحصائيات -->
      <div class="flex gap-3 text-center">
        <div class="bg-gray-800 rounded-lg px-3 py-2">
          <p class="text-xs text-gray-500">مفتوحة</p>
          <p id="sc-open" class="font-bold text-yellow-400">—</p>
        </div>
        <div class="bg-gray-800 rounded-lg px-3 py-2">
          <p class="text-xs text-gray-500">فوز/خسارة</p>
          <p id="sc-winloss" class="font-bold text-white">—</p>
        </div>
        <div class="bg-gray-800 rounded-lg px-3 py-2">
          <p class="text-xs text-gray-500">PnL إجمالي</p>
          <p id="sc-pnl" class="font-bold">—</p>
        </div>
      </div>
      <!-- زر التفعيل -->
      <button id="scalp-toggle-btn" onclick="toggleScalper()"
        class="px-4 py-2 rounded-lg text-sm font-bold transition-all bg-gray-700 text-gray-300">
        جارٍ التحميل...
      </button>
    </div>
  </div>

  <!-- الاستراتيجية: 3 طبقات -->
  <div class="grid grid-cols-3 gap-2 mb-3 text-center text-xs">
    <div class="bg-blue-950 border border-blue-800 rounded-lg p-2">
      <p class="text-blue-400 font-bold mb-0.5">📉 Mean Reversion</p>
      <p class="text-gray-400">انخفاض 0.5–3.5% عن القمة</p>
    </div>
    <div class="bg-purple-950 border border-purple-800 rounded-lg p-2">
      <p class="text-purple-400 font-bold mb-0.5">⚡ Momentum</p>
      <p class="text-gray-400">RSI + MACD + Stoch + TEMA + 5m</p>
    </div>
    <div class="bg-green-950 border border-green-800 rounded-lg p-2">
      <p class="text-green-400 font-bold mb-0.5">🎯 Scalping</p>
      <p class="text-gray-400">ATR SL/TP · ROI ينخفض بالوقت</p>
    </div>
  </div>
  <!-- الإعدادات الحالية -->
  <div class="grid grid-cols-2 md:grid-cols-5 gap-2 mb-4 text-center">
    <div class="bg-gray-950 rounded-lg p-2 border border-gray-800">
      <p class="text-xs text-gray-500">TP (0–2 دقيقة)</p>
      <p class="font-bold text-green-400">+2%</p>
    </div>
    <div class="bg-gray-950 rounded-lg p-2 border border-gray-800">
      <p class="text-xs text-gray-500">TP (2–5 دقائق)</p>
      <p class="font-bold text-green-300">+1.5%</p>
    </div>
    <div class="bg-gray-950 rounded-lg p-2 border border-gray-800">
      <p class="text-xs text-gray-500">TP (5+ دقائق)</p>
      <p class="font-bold text-yellow-400">+1%</p>
    </div>
    <div class="bg-gray-950 rounded-lg p-2 border border-gray-800">
      <p class="text-xs text-gray-500">SL (ATR × 1.5)</p>
      <p class="font-bold text-red-400">ديناميكي</p>
    </div>
    <div class="bg-gray-950 rounded-lg p-2 border border-gray-800">
      <p class="text-xs text-gray-500">رصيد/صفقة</p>
      <p class="font-bold text-white">90% من الرصيد</p>
    </div>
  </div>

  <!-- جدول الصفقات المفتوحة -->
  <div class="overflow-x-auto">
    <table class="w-full text-sm">
      <thead>
        <tr class="text-gray-500 border-b border-gray-800 text-right">
          <th class="pb-2 font-medium">العملة</th>
          <th class="pb-2 font-medium">دخول</th>
          <th class="pb-2 font-medium">TP</th>
          <th class="pb-2 font-medium">SL</th>
          <th class="pb-2 font-medium">العمر</th>
          <th class="pb-2 font-medium">PnL الحالي</th>
          <th class="pb-2 font-medium">الحالة</th>
        </tr>
      </thead>
      <tbody id="scalp-body">
        <tr><td colspan="7" class="text-gray-500 py-6 text-center">لا توجد صفقات مضاربة — فعّل الخاصية أولاً</td></tr>
      </tbody>
    </table>
  </div>
</div>

<!-- ══════════════════ TRADES ══════════════════ -->
<div class="bg-gray-900 rounded-xl border border-gray-800 p-5">
  <h2 class="font-bold text-white mb-3 flex items-center gap-2">💱 الصفقات المنفذة</h2>
  <div class="overflow-x-auto">
    <table class="w-full text-sm">
      <thead>
        <tr class="text-gray-500 border-b border-gray-800 text-right">
          <th class="pb-2 font-medium">العملة</th>
          <th class="pb-2 font-medium">الإجراء</th>
          <th class="pb-2 font-medium">المبلغ (USDT)</th>
          <th class="pb-2 font-medium">سعر التنفيذ</th>
          <th class="pb-2 font-medium">الحالة</th>
          <th class="pb-2 font-medium">ربح/خسارة</th>
          <th class="pb-2 font-medium">الوقت</th>
        </tr>
      </thead>
      <tbody id="trades-body">
        <tr><td colspan="7" class="text-gray-500 py-6 text-center">لا توجد صفقات منفذة بعد</td></tr>
      </tbody>
    </table>
  </div>
</div>



<!-- ══════════════════ CHART + TA ══════════════════ -->
<div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
<div class="lg:col-span-2">
<div class="bg-gray-900 rounded-xl border border-gray-800 overflow-hidden">
  <div class="flex items-center justify-between px-4 py-3 border-b border-gray-800">
    <h2 class="font-semibold text-white">📊 TradingView — رسم بياني</h2>
    <div class="flex items-center gap-2">
      <select id="tv-symbol" class="bg-gray-800 text-white text-sm rounded px-2 py-1 border border-gray-700">
        <option value="BTCUSDT">BTC/USDT</option>
        <option value="ETHUSDT">ETH/USDT</option>
        <option value="SOLUSDT">SOL/USDT</option>
        <option value="BNBUSDT">BNB/USDT</option>
        <option value="XRPUSDT">XRP/USDT</option>
        <option value="ADAUSDT">ADA/USDT</option>
        <option value="DOGEUSDT">DOGE/USDT</option>
        <option value="AVAXUSDT">AVAX/USDT</option>
        <option value="DOTUSDT">DOT/USDT</option>
        <option value="MATICUSDT">MATIC/USDT</option>
      </select>
      <select id="tv-interval" class="bg-gray-800 text-white text-sm rounded px-2 py-1 border border-gray-700">
        <option value="5">5 دقائق</option>
        <option value="15" selected>15 دقيقة</option>
        <option value="60">ساعة</option>
        <option value="240">4 ساعات</option>
        <option value="D">يومي</option>
      </select>
    </div>
  </div>
  <div id="tv-chart-container" style="height:480px;"></div>
</div>

</div>
<!-- TA PANEL -->
<div class="bg-gray-900 rounded-xl border border-gray-800 p-4">
  <div class="flex items-center justify-between mb-3">
    <h2 class="font-semibold text-white">🔬 تحليل تقني</h2>
    <button onclick="loadTA()" class="text-xs bg-blue-700 hover:bg-blue-600 px-3 py-1 rounded">تحديث</button>
  </div>
  <!-- الصف الأول: الإشارات الرئيسية -->
  <div class="grid grid-cols-2 gap-2 mb-2">
    <div class="bg-gray-800 rounded-lg p-3 text-center col-span-2">
      <p class="text-xs text-gray-500 mb-1">الإشارة الكلية</p>
      <p id="ta-signal" class="text-2xl font-bold">—</p>
      <p id="ta-score-bar" class="text-xs text-gray-500 mt-1">—</p>
    </div>
  </div>
  <!-- الصف الثاني: المؤشرات -->
  <div class="grid grid-cols-3 gap-2 mb-2">
    <div class="bg-gray-800 rounded-lg p-2 text-center">
      <p class="text-xs text-gray-500">RSI 1m</p>
      <p id="ta-rsi" class="text-lg font-bold text-white">—</p>
    </div>
    <div class="bg-gray-800 rounded-lg p-2 text-center">
      <p class="text-xs text-gray-500">RSI 5m</p>
      <p id="ta-rsi5m" class="text-lg font-bold text-gray-300">—</p>
    </div>
    <div class="bg-gray-800 rounded-lg p-2 text-center">
      <p class="text-xs text-gray-500">Stochastic K</p>
      <p id="ta-stoch" class="text-lg font-bold text-white">—</p>
    </div>
  </div>
  <div class="grid grid-cols-3 gap-2 mb-2">
    <div class="bg-gray-800 rounded-lg p-2 text-center">
      <p class="text-xs text-gray-500">MACD Hist</p>
      <p id="ta-macd" class="text-lg font-bold text-white">—</p>
    </div>
    <div class="bg-gray-800 rounded-lg p-2 text-center">
      <p class="text-xs text-gray-500">TEMA 9</p>
      <p id="ta-tema" class="text-lg font-bold text-white">—</p>
    </div>
    <div class="bg-gray-800 rounded-lg p-2 text-center">
      <p class="text-xs text-gray-500">ATR 14</p>
      <p id="ta-atr" class="text-lg font-bold text-cyan-400">—</p>
    </div>
  </div>
  <div id="ta-reasons" class="mt-2 text-xs text-gray-400 flex flex-wrap gap-1"></div>
  <!-- شريط BB Position -->
  <div class="mt-2">
    <div class="flex justify-between text-xs text-gray-500 mb-1">
      <span>BB Lower</span><span id="ta-bb-pct-val">—</span><span>BB Upper</span>
    </div>
    <div class="bg-gray-700 rounded-full h-2 relative">
      <div id="ta-bb-bar" class="absolute top-0 h-2 w-2 rounded-full bg-yellow-400" style="left:50%;transform:translateX(-50%)"></div>
    </div>
  </div>
</div>
</div>

</main>

<script>
// ──────────────────────────────────────────────
// أدوات مساعدة
// ──────────────────────────────────────────────
const REFRESH_SEC = 15;
let refreshCountdown = REFRESH_SEC;
let nextCycleMs = null;

function fmtDt(dt) {
  if (!dt) return '—';
  return new Date(dt).toLocaleString('ar-SA', {
    month:'2-digit', day:'2-digit',
    hour:'2-digit', minute:'2-digit', second:'2-digit', hour12:false
  });
}

function fmtDuration(ms) {
  if (!ms || ms < 0) return '—';
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const h = Math.floor(m / 60);
  if (h > 0) return h+'س '+(m%60)+'د';
  if (m > 0) return m+'د '+(s%60)+'ث';
  return s+'ث';
}

function fmtCountdown(ms) {
  if (!ms || ms <= 0) return 'الآن';
  const s = Math.floor(ms / 1000);
  const m = Math.floor(s / 60);
  const hh = String(Math.floor(m / 60)).padStart(2,'0');
  const mm = String(m % 60).padStart(2,'0');
  const ss = String(s % 60).padStart(2,'0');
  return hh+':'+mm+':'+ss;
}

function sentimentAr(s) {
  if (s === 'bullish') return '<span class="text-green-400">صاعد 📈</span>';
  if (s === 'bearish') return '<span class="text-red-400">هابط 📉</span>';
  return '<span class="text-yellow-400">محايد ➡️</span>';
}

function sentimentCls(s) {
  if (s === 'bullish') return 'text-green-400';
  if (s === 'bearish') return 'text-red-400';
  return 'text-yellow-400';
}

function actionBadge(a) {
  const m = {
    buy:      'bg-green-900 text-green-300',
    sell:     'bg-red-900   text-red-300',
    watch:    'bg-blue-900  text-blue-300',
    avoid:    'bg-orange-900 text-orange-300',
    no_trade: 'bg-gray-800  text-gray-400',
    hold:     'bg-gray-800  text-gray-400',
  };
  const labels = {buy:'شراء ✅',sell:'بيع 🔴',watch:'مراقبة 👀',avoid:'تجنب ⛔',no_trade:'لا تداول',hold:'انتظار'};
  const cls = m[a] || 'bg-gray-800 text-gray-400';
  return `<span class="px-2 py-0.5 rounded text-xs font-bold ${cls}">${labels[a]||a}</span>`;
}

function statusBadge(s) {
  const map = {
    completed:         'bg-green-900 text-green-300',
    analyzed:          'bg-green-900 text-green-300',
    running:           'bg-cyan-900  text-cyan-300',
    collecting:        'bg-cyan-900  text-cyan-300',
    collected:         'bg-blue-900  text-blue-300',
    analyzing:         'bg-purple-900 text-purple-300',
    collector_failed:  'bg-red-900   text-red-300',
    analyzer_failed:   'bg-red-900   text-red-300',
    decision_failed:   'bg-red-900   text-red-300',
    trader_failed:     'bg-orange-900 text-orange-300',
    no_new_tweets:     'bg-gray-800  text-gray-400',
    failed:            'bg-red-900   text-red-300',
    filled:            'bg-green-900 text-green-300',
    pending:           'bg-yellow-900 text-yellow-300',
    cancelled:         'bg-gray-800  text-gray-400',
  };
  const labels = {
    completed:'مكتملة ✓', analyzed:'تم التحليل ✓',
    running:'تعمل...', collecting:'يجمع تغريدات...', collected:'تم الجمع',
    analyzing:'يحلل...', collector_failed:'فشل الجمع ✗',
    analyzer_failed:'فشل التحليل ✗', decision_failed:'فشل القرار ✗',
    trader_failed:'خطأ في التنفيذ', no_new_tweets:'لا تغريدات جديدة',
    failed:'فاشلة ✗', filled:'منفذة ✓', pending:'معلقة', cancelled:'ملغاة',
  };
  const cls = map[s] || 'bg-gray-800 text-gray-400';
  return `<span class="px-2 py-1 rounded text-xs font-bold ${cls}">${labels[s]||s}</span>`;
}

function parseJson(v) {
  if (!v) return null;
  if (typeof v === 'object') return v;
  try { return JSON.parse(v); } catch { return null; }
}

// ──────────────────────────────────────────────
// Phase tracker
// ──────────────────────────────────────────────
function updatePhaseStep(id, state, label) {
  const el = document.getElementById(id);
  if (!el) return;
  const dot = el.querySelector('div');
  const val = document.getElementById(id+'-val');
  if (state === 'active') {
    dot.className = 'w-8 h-8 rounded-full flex items-center justify-center mx-auto mb-1 text-sm bg-cyan-600 text-white pulse';
    el.querySelector('p.text-xs').className = 'text-xs text-cyan-400 font-bold';
  } else if (state === 'done') {
    dot.className = 'w-8 h-8 rounded-full flex items-center justify-center mx-auto mb-1 text-sm bg-green-700 text-white';
    dot.textContent = '✓';
    el.querySelector('p.text-xs').className = 'text-xs text-green-400';
  } else if (state === 'error') {
    dot.className = 'w-8 h-8 rounded-full flex items-center justify-center mx-auto mb-1 text-sm bg-red-700 text-white';
    dot.textContent = '✗';
    el.querySelector('p.text-xs').className = 'text-xs text-red-400';
  } else {
    dot.className = 'w-8 h-8 rounded-full flex items-center justify-center mx-auto mb-1 text-sm bg-gray-800 text-gray-400';
  }
  if (val) val.textContent = label || '—';
}

// ──────────────────────────────────────────────
// Load functions
// ──────────────────────────────────────────────
async function loadCurrentCycle() {
  try {
    const r = await fetch('/api/current-cycle');
    const d = await r.json();
    if (!d || !d.cycle_id) return;

    const s = d.status;

    // phase badge في الهيدر
    const phaseBadge = document.getElementById('phase-badge');
    const phaseMap = {
      collecting: {cls:'bg-cyan-900 text-cyan-300',   txt:'⚙️ يجمع التغريدات...'},
      collected:  {cls:'bg-blue-900  text-blue-300',   txt:'✓ تم الجمع'},
      analyzing:  {cls:'bg-purple-900 text-purple-300', txt:'🧠 يحلل بـ Claude...'},
      analyzed:   {cls:'bg-green-900 text-green-300',  txt:'✓ تم التحليل'},
      completed:  {cls:'bg-green-900 text-green-300',  txt:'✅ اكتملت'},
      collector_failed:{cls:'bg-red-900 text-red-300', txt:'✗ فشل الجمع'},
      analyzer_failed: {cls:'bg-red-900 text-red-300', txt:'✗ فشل التحليل'},
      no_new_tweets:   {cls:'bg-gray-800 text-gray-400',txt:'— لا تغريدات جديدة'},
    };
    const ph = phaseMap[s] || {cls:'bg-gray-700 text-gray-300', txt:s};
    phaseBadge.className = 'px-3 py-1 rounded-full text-xs font-bold ' + ph.cls;
    phaseBadge.textContent = ph.txt;

    // cycle status badge
    document.getElementById('cycle-status-badge').innerHTML =
      '#' + d.cycle_id + ' &nbsp;' + statusBadge(s);

    // next cycle timer
    if (['completed','analyzed','no_new_tweets','collector_failed','analyzer_failed'].includes(s)) {
      if (d.started_at) {
        const startMs = new Date(d.started_at).getTime();
        nextCycleMs = startMs + 60 * 60 * 1000;
      }
    } else {
      nextCycleMs = null;
    }

    // خطوات الـ pipeline
    // خطوة 1: جمع
    if (['collecting'].includes(s)) {
      updatePhaseStep('step-collect', 'active', d.tweets_count + ' تغريدة');
    } else if (['collected','analyzing','analyzed','completed'].includes(s)) {
      updatePhaseStep('step-collect', 'done', d.tweets_count + ' تغريدة');
    } else if (s === 'collector_failed') {
      updatePhaseStep('step-collect', 'error', 'فشل');
    } else {
      updatePhaseStep('step-collect', 'pending', d.tweets_count ? d.tweets_count+' ت' : '—');
    }

    // خطوة 2: تحليل
    if (s === 'analyzing') {
      updatePhaseStep('step-analyze', 'active', 'جارٍ...');
    } else if (['analyzed','completed'].includes(s)) {
      updatePhaseStep('step-analyze', 'done', d.analyzer_status || 'مكتمل');
    } else if (s === 'analyzer_failed') {
      updatePhaseStep('step-analyze', 'error', 'فشل');
    } else if (['collected'].includes(s)) {
      updatePhaseStep('step-analyze', 'pending', 'في الانتظار');
    } else {
      updatePhaseStep('step-analyze', 'pending', '—');
    }

    // خطوة 3: قرارات
    if (['analyzed','completed'].includes(s)) {
      updatePhaseStep('step-decide', 'done', 'مكتمل');
    } else if (s === 'decision_failed') {
      updatePhaseStep('step-decide', 'error', 'فشل');
    } else {
      updatePhaseStep('step-decide', 'pending', '—');
    }

    // خطوة 4: تنفيذ
    if (s === 'completed') {
      updatePhaseStep('step-trade', 'done', 'مكتمل');
    } else if (s === 'trader_failed') {
      updatePhaseStep('step-trade', 'error', 'خطأ');
    } else {
      updatePhaseStep('step-trade', 'pending', '—');
    }

    // خطأ
    const errEl = document.getElementById('cycle-error');
    if (d.error_message && d.error_message !== '—') {
      errEl.textContent = '⚠️ ' + d.error_message;
      errEl.classList.remove('hidden');
    } else {
      errEl.classList.add('hidden');
    }

  } catch(e) { console.error('cycle error', e); }
}

async function loadStats() {
  try {
    // استخدم scalp-stats بدل stats القديم
    const [sr, statusR] = await Promise.all([
      fetch('/api/scalp-stats'),
      fetch('/api/scalp-status'),
    ]);
    const d = await sr.json();
    const st = await statusR.json();

    const daily = parseFloat(d.daily_pnl || 0);
    const total = parseFloat(d.total_pnl || 0);
    const pnlEl = document.getElementById('stat-pnl');
    pnlEl.textContent = (daily >= 0 ? '+' : '') + daily.toFixed(3) + ' $';
    pnlEl.className = 'text-2xl font-bold ' + (daily >= 0 ? 'text-green-400' : 'text-red-400');
    const totalEl = document.getElementById('stat-pnl-total');
    if (totalEl) totalEl.textContent = 'إجمالي: ' + (total >= 0 ? '+' : '') + total.toFixed(3) + ' $';
    const tradesEl = document.getElementById('stat-trades');
    if (tradesEl) {
      const w = d.wins || 0, l = d.losses || 0;
      tradesEl.textContent = 'صفقات: ' + (d.total_closed || 0) + ' | ✅' + w + ' 🛑' + l;
    }
    const openEl = document.getElementById('stat-open');
    if (openEl) openEl.textContent = (d.open_count || 0) + ' / 1';
    const updEl = document.getElementById('stat-last-update');
    if (updEl) updEl.textContent = new Date().toLocaleTimeString('ar-SA');
    const scalEl = document.getElementById('stat-scalp-status');
    if (scalEl) {
      const isOn = st.enabled !== false;
      scalEl.textContent = isOn ? '✅ المضارب يعمل' : '⏸ المضارب موقوف';
      scalEl.className = 'text-xs mt-0.5 ' + (isOn ? 'text-green-400' : 'text-gray-500');
    }
    // phase badge في الهيدر — حالة المضارب
    const phaseBadge = document.getElementById('phase-badge');
    if (phaseBadge) {
      const isOn = st.enabled !== false;
      phaseBadge.textContent = isOn ? '⚡ المضارب يعمل' : '⏸ المضارب موقوف';
      phaseBadge.className = 'px-3 py-1 rounded-full text-xs font-bold ' + (isOn ? 'bg-green-900 text-green-300' : 'bg-gray-800 text-gray-400');
    }
  } catch(e) { console.error(e); }
}

async function loadBalance() {
  try {
    const r = await fetch('/api/balance');
    const d = await r.json();
    const balEl = document.getElementById('stat-balance');
    const modeEl = document.getElementById('stat-balance-mode');
    if (d.error) {
      balEl.textContent = 'خطأ';
      modeEl.textContent = d.error.substring(0,30);
    } else {
      const free = parseFloat(d.usdt_free || 0);
      const total = parseFloat(d.total_value_usdt || free);
      balEl.textContent = free.toFixed(2) + ' $';
      const portEl = document.getElementById('stat-portfolio');
      if (portEl) portEl.textContent = 'محفظة: ' + total.toFixed(2) + ' $';
      const portEl = document.getElementById('stat-portfolio');
      if (portEl) {
        const total = parseFloat(d.total_value_usdt || 0);
        portEl.textContent = total > 0.01 ? 'محفظة: ' + total.toFixed(2) + ' $' : '';
      }
      modeEl.textContent = d.paper_mode ? '🧪 وضع تجريبي' : '✅ Binance حقيقي';
      modeEl.className = 'text-xs mt-1 ' + (d.paper_mode ? 'text-orange-400' : 'text-green-400');
    }
  } catch(e) { console.error(e); }
}





async function loadTrades() {
  try {
    const r = await fetch('/api/trades');
    const d = await r.json();
    const tbody = document.getElementById('trades-body');
    if (!d.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-gray-500 py-8 text-center">لا توجد صفقات منفذة بعد — تحتاج USDT في Binance</td></tr>';
      return;
    }
    tbody.innerHTML = d.map(t => {
      const pnl = parseFloat(t.pnl || 0);
      return `<tr class="border-b border-gray-800 hover:bg-gray-850">
        <td class="py-3 font-bold text-cyan-300">${t.coin}</td>
        <td class="py-3">${actionBadge(t.action)}</td>
        <td class="py-3 font-mono">${parseFloat(t.amount||0).toFixed(2)}</td>
        <td class="py-3 font-mono text-gray-300">${t.price ? parseFloat(t.price).toFixed(4) : '—'}</td>
        <td class="py-3">${statusBadge(t.status)}</td>
        <td class="py-3 font-mono font-bold ${pnl>=0?'text-green-400':'text-red-400'}">${(pnl>=0?'+':'')+pnl.toFixed(2)}</td>
        <td class="py-3 text-xs text-gray-500">${fmtDt(t.executed_at)}</td>
      </tr>`;
    }).join('');
  } catch(e) { console.error(e); }
}



// ──────────────────────────────────────────────
// Tick ثانية بثانية
// ──────────────────────────────────────────────
function tick() {
  // عداد الدورة القادمة
  const nextEl = document.getElementById('next-timer');
  if (nextCycleMs) {
    const diff = nextCycleMs - Date.now();
    nextEl.textContent = fmtCountdown(diff);
    if (diff <= 0) { nextCycleMs = null; nextEl.textContent = 'الآن'; }
  } else {
    nextEl.textContent = 'جارٍ الآن';
  }

  // عداد التحديث
  refreshCountdown--;
  document.getElementById('refresh-timer').textContent = refreshCountdown + 'ث';
  if (refreshCountdown <= 0) {
    refreshCountdown = REFRESH_SEC;
    refresh();
  }
}

async function refresh() {
  await Promise.all([
    loadCurrentCycle(),
    loadStats(),
    loadBalance(),
    loadAnalysis(),
    loadDecisions(),
    loadTrades(),
    loadCycles(),
  ]);
}


// ──────────────────────────────────────────────
// إدارة العملات
// ──────────────────────────────────────────────
const ALL_COINS = [
  {symbol:'BTC',name:'Bitcoin',cat:'الكبار'},
  {symbol:'ETH',name:'Ethereum',cat:'الكبار'},
  {symbol:'BNB',name:'BNB',cat:'الكبار'},
  {symbol:'SOL',name:'Solana',cat:'الكبار'},
  {symbol:'XRP',name:'Ripple',cat:'الكبار'},
  {symbol:'ADA',name:'Cardano',cat:'الكبار'},
  {symbol:'LTC',name:'Litecoin',cat:'الكبار'},
  {symbol:'ETC',name:'Ethereum Classic',cat:'الكبار'},
  {symbol:'BCH',name:'Bitcoin Cash',cat:'الكبار'},
  {symbol:'DOGE',name:'Dogecoin',cat:'Meme'},
  {symbol:'SHIB',name:'Shiba Inu',cat:'Meme'},
  {symbol:'PEPE',name:'Pepe',cat:'Meme'},
  {symbol:'FLOKI',name:'Floki',cat:'Meme'},
  {symbol:'WIF',name:'dogwifhat',cat:'Meme'},
  {symbol:'BONK',name:'Bonk',cat:'Meme'},
  {symbol:'MEME',name:'Memecoin',cat:'Meme'},
  {symbol:'NEIRO',name:'Neiro',cat:'Meme'},
  {symbol:'NOT',name:'Notcoin',cat:'Meme'},
  {symbol:'DOGS',name:'Dogs',cat:'Meme'},
  {symbol:'HMSTR',name:'Hamster Kombat',cat:'Meme'},
  {symbol:'AVAX',name:'Avalanche',cat:'L1/L2'},
  {symbol:'MATIC',name:'Polygon',cat:'L1/L2'},
  {symbol:'NEAR',name:'NEAR Protocol',cat:'L1/L2'},
  {symbol:'APT',name:'Aptos',cat:'L1/L2'},
  {symbol:'ARB',name:'Arbitrum',cat:'L1/L2'},
  {symbol:'OP',name:'Optimism',cat:'L1/L2'},
  {symbol:'SUI',name:'Sui',cat:'L1/L2'},
  {symbol:'SEI',name:'Sei',cat:'L1/L2'},
  {symbol:'TIA',name:'Celestia',cat:'L1/L2'},
  {symbol:'ALGO',name:'Algorand',cat:'L1/L2'},
  {symbol:'EGLD',name:'MultiversX',cat:'L1/L2'},
  {symbol:'KAS',name:'Kaspa',cat:'L1/L2'},
  {symbol:'TON',name:'Toncoin',cat:'L1/L2'},
  {symbol:'TRX',name:'TRON',cat:'L1/L2'},
  {symbol:'EOS',name:'EOS',cat:'L1/L2'},
  {symbol:'STX',name:'Stacks',cat:'L1/L2'},
  {symbol:'MINA',name:'Mina Protocol',cat:'L1/L2'},
  {symbol:'ZK',name:'ZKsync',cat:'L1/L2'},
  {symbol:'STRK',name:'Starknet',cat:'L1/L2'},
  {symbol:'SCROLL',name:'Scroll',cat:'L1/L2'},
  {symbol:'MANTLE',name:'Mantle',cat:'L1/L2'},
  {symbol:'CFX',name:'Conflux',cat:'L1/L2'},
  {symbol:'ROSE',name:'Oasis Network',cat:'L1/L2'},
  {symbol:'ONE',name:'Harmony',cat:'L1/L2'},
  {symbol:'UNI',name:'Uniswap',cat:'DeFi'},
  {symbol:'AAVE',name:'Aave',cat:'DeFi'},
  {symbol:'MKR',name:'Maker',cat:'DeFi'},
  {symbol:'SNX',name:'Synthetix',cat:'DeFi'},
  {symbol:'CRV',name:'Curve',cat:'DeFi'},
  {symbol:'COMP',name:'Compound',cat:'DeFi'},
  {symbol:'INJ',name:'Injective',cat:'DeFi'},
  {symbol:'LDO',name:'Lido DAO',cat:'DeFi'},
  {symbol:'RUNE',name:'THORChain',cat:'DeFi'},
  {symbol:'JUP',name:'Jupiter',cat:'DeFi'},
  {symbol:'DYDX',name:'dYdX',cat:'DeFi'},
  {symbol:'GMX',name:'GMX',cat:'DeFi'},
  {symbol:'1INCH',name:'1inch',cat:'DeFi'},
  {symbol:'BAL',name:'Balancer',cat:'DeFi'},
  {symbol:'JTO',name:'Jito',cat:'DeFi'},
  {symbol:'ENA',name:'Ethena',cat:'DeFi'},
  {symbol:'ETHFI',name:'ether.fi',cat:'DeFi'},
  {symbol:'REZ',name:'Renzo',cat:'DeFi'},
  {symbol:'LISTA',name:'Lista DAO',cat:'DeFi'},
  {symbol:'FET',name:'Fetch.ai',cat:'AI'},
  {symbol:'AGIX',name:'SingularityNET',cat:'AI'},
  {symbol:'OCEAN',name:'Ocean Protocol',cat:'AI'},
  {symbol:'RNDR',name:'Render',cat:'AI'},
  {symbol:'TAO',name:'Bittensor',cat:'AI'},
  {symbol:'WLD',name:'Worldcoin',cat:'AI'},
  {symbol:'ARKM',name:'Arkham',cat:'AI'},
  {symbol:'AXS',name:'Axie Infinity',cat:'Gaming'},
  {symbol:'SAND',name:'The Sandbox',cat:'Gaming'},
  {symbol:'MANA',name:'Decentraland',cat:'Gaming'},
  {symbol:'GALA',name:'Gala',cat:'Gaming'},
  {symbol:'IMX',name:'Immutable',cat:'Gaming'},
  {symbol:'BEAM',name:'Beam',cat:'Gaming'},
  {symbol:'CHZ',name:'Chiliz',cat:'Gaming'},
  {symbol:'GAL',name:'Galxe',cat:'Gaming'},
  {symbol:'PIXEL',name:'Pixels',cat:'Gaming'},
  {symbol:'RON',name:'Ronin',cat:'Gaming'},
  {symbol:'YGG',name:'Yield Guild',cat:'Gaming'},
  {symbol:'MAGIC',name:'Magic',cat:'Gaming'},
  {symbol:'DOT',name:'Polkadot',cat:'بنية'},
  {symbol:'LINK',name:'Chainlink',cat:'بنية'},
  {symbol:'ATOM',name:'Cosmos',cat:'بنية'},
  {symbol:'FIL',name:'Filecoin',cat:'بنية'},
  {symbol:'ICP',name:'Internet Computer',cat:'بنية'},
  {symbol:'HBAR',name:'Hedera',cat:'بنية'},
  {symbol:'XLM',name:'Stellar',cat:'بنية'},
  {symbol:'VET',name:'VeChain',cat:'بنية'},
  {symbol:'QNT',name:'Quant',cat:'بنية'},
  {symbol:'THETA',name:'Theta Network',cat:'بنية'},
  {symbol:'GRT',name:'The Graph',cat:'بنية'},
  {symbol:'PYTH',name:'Pyth Network',cat:'بنية'},
  {symbol:'W',name:'Wormhole',cat:'بنية'},
  {symbol:'OMNI',name:'Omni Network',cat:'بنية'},
];

const CAT_COLORS = {
  'الكبار': 'border-blue-700   bg-blue-950   text-blue-300',
  'DeFi':   'border-purple-700 bg-purple-950 text-purple-300',
  'L1/L2':  'border-green-700  bg-green-950  text-green-300',
  'Meme':   'border-pink-700   bg-pink-950   text-pink-300',
  'AI':     'border-cyan-700   bg-cyan-950   text-cyan-300',
  'Gaming': 'border-orange-700 bg-orange-950 text-orange-300',
  'بنية':   'border-gray-600   bg-gray-900   text-gray-300',
};
const CAT_BADGE = {
  'الكبار': 'bg-blue-900 text-blue-300',
  'DeFi':   'bg-purple-900 text-purple-300',
  'L1/L2':  'bg-green-900 text-green-300',
  'Meme':   'bg-pink-900 text-pink-300',
  'AI':     'bg-cyan-900 text-cyan-300',
  'Gaming': 'bg-orange-900 text-orange-300',
  'بنية':   'bg-gray-700 text-gray-300',
};

let selectedCoins = new Set();
let currentCat   = 'الكل';
let coinsPanelOpen = false;

function toggleCoinsPanel() {
  coinsPanelOpen = !coinsPanelOpen;
  document.getElementById('coins-panel').style.display = coinsPanelOpen ? 'block' : 'none';
  document.getElementById('coins-toggle-icon').textContent = coinsPanelOpen ? '▲' : '▼';
}

function setCat(cat) {
  currentCat = cat;
  document.querySelectorAll('.cat-tab').forEach(btn => {
    const active = btn.dataset.cat === cat;
    btn.className = 'cat-tab px-3 py-1 text-xs rounded-full ' +
      (active ? 'bg-white text-gray-900 font-bold' : 'bg-gray-800 text-gray-400 hover:bg-gray-700');
  });
  renderCoinsGrid();
}

function filterCoins() { renderCoinsGrid(); }

function renderCoinsGrid() {
  const q   = (document.getElementById('coins-search').value || '').toLowerCase();
  const grid = document.getElementById('coins-grid-mgr');
  const visible = ALL_COINS.filter(c => {
    if (currentCat !== 'الكل' && c.cat !== currentCat) return false;
    if (q && !c.symbol.toLowerCase().includes(q) && !c.name.toLowerCase().includes(q)) return false;
    return true;
  });

  if (!visible.length) {
    grid.innerHTML = '<p class="col-span-full text-gray-500 text-sm py-4 text-center">لا توجد نتائج</p>';
    return;
  }

  grid.innerHTML = visible.map(c => {
    const on  = selectedCoins.has(c.symbol);
    const clr = CAT_COLORS[c.cat] || 'border-gray-700 bg-gray-900 text-gray-300';
    const badgeCls = CAT_BADGE[c.cat] || 'bg-gray-700 text-gray-300';
    return `
      <div onclick="toggleCoin('${c.symbol}')" data-sym="${c.symbol}"
        class="coin-card cursor-pointer rounded-xl border-2 p-3 transition-all select-none
               ${on ? clr + ' opacity-100' : 'border-gray-800 bg-gray-950 opacity-50 hover:opacity-80'}">
        <div class="flex items-center justify-between mb-1">
          <span class="font-bold text-sm">${c.symbol}</span>
          <span class="text-lg">${on ? '✅' : '⬜'}</span>
        </div>
        <p class="text-xs text-gray-400 leading-tight">${c.name}</p>
        <span class="mt-1 inline-block px-1.5 py-0.5 rounded text-xs ${badgeCls}">${c.cat}</span>
      </div>`;
  }).join('');
  updateCoinsCountBadge();
}

function toggleCoin(sym) {
  if (selectedCoins.has(sym)) selectedCoins.delete(sym);
  else selectedCoins.add(sym);
  renderCoinsGrid();
}

function selectAllCoins() {
  const q = (document.getElementById('coins-search').value || '').toLowerCase();
  ALL_COINS.filter(c => {
    if (currentCat !== 'الكل' && c.cat !== currentCat) return false;
    if (q && !c.symbol.toLowerCase().includes(q) && !c.name.toLowerCase().includes(q)) return false;
    return true;
  }).forEach(c => selectedCoins.add(c.symbol));
  renderCoinsGrid();
}

function deselectAllCoins() {
  const q = (document.getElementById('coins-search').value || '').toLowerCase();
  ALL_COINS.filter(c => {
    if (currentCat !== 'الكل' && c.cat !== currentCat) return false;
    if (q && !c.symbol.toLowerCase().includes(q) && !c.name.toLowerCase().includes(q)) return false;
    return true;
  }).forEach(c => selectedCoins.delete(c.symbol));
  renderCoinsGrid();
}

function updateCoinsCountBadge() {
  document.getElementById('coins-count-badge').textContent = selectedCoins.size + ' عملة محددة';
}

async function saveCoinsConfig() {
  try {
    const r = await fetch('/api/coins-config', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({selected: [...selectedCoins]}),
    });
    const d = await r.json();
    const msg = document.getElementById('save-msg');
    if (d.ok) {
      msg.textContent = '✅ تم الحفظ — ' + d.count + ' عملة محددة للتداول';
      msg.className = 'text-sm text-green-400 mt-3';
    } else {
      msg.textContent = '❌ خطأ: ' + (d.error||'');
      msg.className = 'text-sm text-red-400 mt-3';
    }
    msg.classList.remove('hidden');
    setTimeout(() => msg.classList.add('hidden'), 4000);
  } catch(e) {
    alert('فشل الحفظ: ' + e);
  }
}

async function loadCoinsConfig() {
  try {
    const r = await fetch('/api/coins-config');
    const d = await r.json();
    selectedCoins = new Set(d.selected || ALL_COINS.map(c=>c.symbol));
    renderCoinsGrid();
  } catch(e) {
    selectedCoins = new Set(ALL_COINS.map(c=>c.symbol));
    renderCoinsGrid();
  }
}

// إخفاء القسم في البداية
document.addEventListener('DOMContentLoaded', () => {
  document.getElementById('coins-panel').style.display = 'none';
  loadCoinsConfig();
});


// ──────────────────────────────────────────────
// المضاربة السريعة
// ──────────────────────────────────────────────
let scalpEnabled = false;

async function loadScalpStats() {
  try {
    const [sr, pr] = await Promise.all([
      fetch('/api/scalp-stats').then(r=>r.json()),
      fetch('/api/scalp-positions').then(r=>r.json()),
    ]);

    document.getElementById('sc-open').textContent = sr.open_count ?? 0;
    document.getElementById('sc-winloss').textContent =
      (sr.wins ?? 0) + 'W / ' + (sr.losses ?? 0) + 'L';
    const pnl = parseFloat(sr.total_pnl || 0);
    const pnlEl = document.getElementById('sc-pnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + pnl.toFixed(4) + ' $';
    pnlEl.className = 'font-bold ' + (pnl >= 0 ? 'text-green-400' : 'text-red-400');

    // جدول الصفقات
    const tbody = document.getElementById('scalp-body');
    if (!pr.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-gray-500 py-6 text-center">' +
        (scalpEnabled ? 'لا توجد صفقات مفتوحة حالياً' : 'فعّل المضاربة السريعة للبدء') +
        '</td></tr>';
      return;
    }
    tbody.innerHTML = pr.map(p => {
      const entry = parseFloat(p.entry_price||0);
      const tp    = parseFloat(p.tp_price||0);
      const sl    = parseFloat(p.sl_price||0);
      const pnlV  = parseFloat(p.pnl||0);
      const isOpen = p.status === 'open';
      const reasonBadge = p.close_reason === 'tp'
        ? '<span class="px-1.5 py-0.5 rounded bg-green-900 text-green-300 text-xs">TP ✅</span>'
        : p.close_reason === 'sl'
        ? '<span class="px-1.5 py-0.5 rounded bg-red-900   text-red-300   text-xs">SL 🛑</span>'
        : '<span class="px-1.5 py-0.5 rounded bg-yellow-900 text-yellow-300 text-xs">مفتوحة</span>';
      return `<tr class="border-b border-gray-800">
        <td class="py-2 font-bold text-cyan-300">${p.symbol||'—'}</td>
        <td class="py-2 font-mono text-sm">${entry.toFixed(5)}</td>
        <td class="py-2 font-mono text-green-400 text-sm">${tp.toFixed(5)}</td>
        <td class="py-2 font-mono text-red-400 text-sm">${sl.toFixed(5)}</td>
        <td class="py-2">${reasonBadge}</td>
        <td class="py-2 font-mono font-bold text-sm ${pnlV>=0?'text-green-400':'text-red-400'}">${isOpen?'—':(pnlV>=0?'+':'')+pnlV.toFixed(4)}</td>
        <td class="py-2 text-xs text-gray-500">${fmtDt(p.opened_at)}</td>
      </tr>`;
    }).join('');
  } catch(e) { console.error('scalp', e); }
}

async function checkScalpStatus() {
  try {
    const r = await fetch('/api/scalp-stats');
    // نفترض أن الحالة محفوظة في .env — نقرأها من الـ API
    const envR = await fetch('/api/coins-config'); // تحقق إذا الـ API متاح
    // للتبسيط: نقرأ من localStorage
    scalpEnabled = localStorage.getItem('scalpEnabled') === 'true';
    updateScalpBtn();
  } catch(e) {}
}

function updateScalpBtn() {
  const btn = document.getElementById('scalp-toggle-btn');
  if (scalpEnabled) {
    btn.textContent = '🟢 مفعّلة — إيقاف';
    btn.className = 'px-4 py-2 rounded-lg text-sm font-bold transition-all bg-green-900 text-green-300 hover:bg-red-900 hover:text-red-300';
  } else {
    btn.textContent = '⭕ تفعيل المضاربة';
    btn.className = 'px-4 py-2 rounded-lg text-sm font-bold transition-all bg-blue-900 text-blue-300 hover:bg-blue-700';
  }
}

async function toggleScalper() {
  scalpEnabled = !scalpEnabled;
  localStorage.setItem('scalpEnabled', scalpEnabled);
  updateScalpBtn();
  try {
    await fetch('/api/scalp-toggle', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({enabled: scalpEnabled}),
    });
  } catch(e) { console.error(e); }
}


async function refresh() {
  await Promise.all([
    loadCurrentCycle(), loadStats(), loadBalance(),
    loadAnalysis(), loadDecisions(), loadTrades(),
    loadCycles(), loadScalpStats(),
  ]);
}
checkScalpStatus();
setInterval(tick, 1000);
refresh();
</script>

<script type="text/javascript" src="https://s3.tradingview.com/tv.js"></script>
<script>
let tvWidget = null;

function loadTVChart(symbol, interval) {
  const sym = symbol || document.getElementById('tv-symbol').value;
  const intv = interval || document.getElementById('tv-interval').value;
  const container = document.getElementById('tv-chart-container');
  if (!container) return;
  container.innerHTML = '';
  tvWidget = new TradingView.widget({
    autosize: true,
    symbol: 'BINANCE:' + sym,
    interval: intv,
    timezone: 'Asia/Riyadh',
    theme: 'dark',
    style: '1',
    locale: 'ar',
    toolbar_bg: '#111827',
    enable_publishing: false,
    allow_symbol_change: true,
    container_id: 'tv-chart-container',
    hide_side_toolbar: false,
    studies: ['RSI@tv-basicstudies','MACD@tv-basicstudies','BB@tv-basicstudies'],
  });
}

async function loadTA() {
  const sym = document.getElementById('tv-symbol').value;
  const intv = document.getElementById('tv-interval').value;
  const apiInterval = intv === 'D' ? '1d' : intv === '60' ? '1h' : intv === '240' ? '4h' : intv + 'm';
  try {
    const r = await fetch('/api/ta?symbol=' + sym + '&interval=' + apiInterval);
    const d = await r.json();
    if (d.error) { document.getElementById('ta-rsi').textContent = 'خطأ'; return; }

    // الإشارة الكلية
    const sigEl = document.getElementById('ta-signal');
    sigEl.textContent = d.signal || '—';
    sigEl.className = 'text-2xl font-bold ' + (d.signal_color === 'green' ? 'text-green-400' : d.signal_color === 'red' ? 'text-red-400' : 'text-yellow-400');
    const scoreEl = document.getElementById('ta-score-bar');
    if (scoreEl) {
      const sc = d.ta_score || 0;
      scoreEl.textContent = 'قوة الإشارة: ' + (sc*100).toFixed(0) + '% | ' + (d.buy_signals||0) + ' إشارات شراء';
    }

    // RSI 1m
    const rsiEl = document.getElementById('ta-rsi');
    const rsi = d.rsi || 50;
    rsiEl.textContent = rsi.toFixed(1);
    rsiEl.className = 'text-lg font-bold ' + (rsi < 35 ? 'text-green-400' : rsi > 62 ? 'text-red-400' : 'text-white');

    // RSI 5m (نجلبه بشكل منفصل)
    const rsi5El = document.getElementById('ta-rsi5m');
    if (rsi5El) {
      fetch('/api/ta?symbol=' + sym + '&interval=5m').then(r=>r.json()).then(d5=>{
        const r5 = d5.rsi || 50;
        rsi5El.textContent = r5.toFixed(1);
        rsi5El.className = 'text-lg font-bold ' + (r5 < 42 ? 'text-red-400' : r5 > 60 ? 'text-yellow-400' : 'text-green-400');
      }).catch(()=>{});
    }

    // Stochastic
    const stochEl = document.getElementById('ta-stoch');
    if (stochEl) {
      const sk = d.stoch_k || 50;
      stochEl.textContent = sk.toFixed(1);
      stochEl.className = 'text-lg font-bold ' + (sk < 30 ? 'text-green-400' : sk > 75 ? 'text-red-400' : 'text-white');
    }

    // MACD Hist
    const macdEl = document.getElementById('ta-macd');
    const hist = d.macd_hist || 0;
    macdEl.textContent = hist.toFixed(5);
    macdEl.className = 'text-lg font-bold ' + (hist > 0 ? 'text-green-400' : 'text-red-400');

    // TEMA
    const temaEl = document.getElementById('ta-tema');
    if (temaEl) {
      const tema = parseFloat(d.tema || 0);
      temaEl.textContent = tema > 0 ? tema.toFixed(3) : '—';
      temaEl.className = 'text-lg font-bold ' + (d.price > tema ? 'text-green-400' : 'text-red-400');
    }

    // ATR
    const atrEl = document.getElementById('ta-atr');
    if (atrEl) atrEl.textContent = d.atr ? parseFloat(d.atr).toFixed(4) : '—';

    // Bollinger Band position bar
    const bbPct = d.bb_pct !== undefined ? d.bb_pct : 0.5;
    const bbBar = document.getElementById('ta-bb-bar');
    const bbVal = document.getElementById('ta-bb-pct-val');
    if (bbBar) bbBar.style.left = (bbPct*100).toFixed(0) + '%';
    if (bbVal) bbVal.textContent = (bbPct*100).toFixed(0) + '%';

    // Reasons
    const reasonsEl = document.getElementById('ta-reasons');
    if (d.reasons && d.reasons.length > 0) {
      reasonsEl.innerHTML = d.reasons.map(r => '<span class="bg-gray-700 rounded px-2 py-0.5">' + r + '</span>').join('');
    } else {
      reasonsEl.innerHTML = '';
    }
  } catch(e) { console.error('TA error:', e); }
}

// Wire up symbol/interval selectors
document.addEventListener('DOMContentLoaded', function() {
  loadTVChart('BTCUSDT', '15');
  loadTA();
  const symSel = document.getElementById('tv-symbol');
  const intSel = document.getElementById('tv-interval');
  if (symSel) symSel.addEventListener('change', function() { loadTVChart(); loadTA(); });
  if (intSel) intSel.addEventListener('change', function() { loadTVChart(); loadTA(); });
});
</script>
</body>
</html>"""


# ─── API Endpoints ─────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def dashboard():
    return HTML


@app.get("/api/stats")
def api_stats():
    try:
        stats = db.get_stats()
        return JSONResponse(jsonable(dict(stats)))
    except Exception as e:
        logger.error(f"[WebUI] /api/stats: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/current-cycle")
def api_current_cycle():
    try:
        cycle = db.get_current_cycle()
        if not cycle:
            return JSONResponse({})
        result = jsonable(dict(cycle))
        if isinstance(result.get("analysis_result"), str) and result["analysis_result"]:
            try:
                result["analysis_result"] = json.loads(result["analysis_result"])
            except json.JSONDecodeError:
                pass
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"[WebUI] /api/current-cycle: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/analysis")
def api_analysis():
    try:
        analysis = db.get_latest_analysis()
        if not analysis:
            return JSONResponse({})
        result = jsonable(dict(analysis))
        if isinstance(result.get("coins"), str):
            result["coins"] = json.loads(result["coins"])
        # دمج analysis_result من cycles
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT analysis_result FROM cycles WHERE id = %s AND analysis_result IS NOT NULL LIMIT 1",
                (result.get("cycle_id"),),
            )
            row = cur.fetchone()
        if row and row.get("analysis_result"):
            try:
                full = json.loads(row["analysis_result"])
                if isinstance(full, dict):
                    result.update(full)
            except json.JSONDecodeError:
                pass
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"[WebUI] /api/analysis: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/decisions")
def api_decisions():
    try:
        decisions = db.get_latest_decisions()
        return JSONResponse([jsonable(dict(d)) for d in decisions])
    except Exception as e:
        logger.error(f"[WebUI] /api/decisions: {e}")
        return JSONResponse([], status_code=500)


@app.get("/api/trades")
def api_trades():
    try:
        with db.get_cursor() as cur:
            cur.execute(
                "SELECT coin,action,amount,price,status,pnl,executed_at,order_id FROM trades ORDER BY executed_at DESC LIMIT 30"
            )
            rows = cur.fetchall()
        return JSONResponse([jsonable(dict(r)) for r in rows])
    except Exception as e:
        logger.error(f"[WebUI] /api/trades: {e}")
        return JSONResponse([], status_code=500)


@app.get("/api/balance")
def api_balance():
    try:
        trading_enabled = os.getenv("TRADING_ENABLED", "false").lower() == "true"
        if not trading_enabled:
            return JSONResponse({
                "paper_mode": True,
                "usdt_free":   float(os.getenv("PAPER_CAPITAL_USDT", "1000")),
                "usdt_locked": 0,
                "balances":    [],
                "message":     "وضع تجريبي — TRADING_ENABLED=false",
            })
        api_key    = os.getenv("BINANCE_API_KEY", "")
        api_secret = os.getenv("BINANCE_SECRET_KEY", "")
        if not api_key or not api_secret:
            return JSONResponse({"error": "مفاتيح Binance غير موجودة"})
        bc = BinanceClient(api_key, api_secret)
        account = bc.get_account()
        balances = {b["asset"]: b for b in account.get("balances", [])}
        usdt = balances.get("USDT", {})
        total_value = float(usdt.get("free", 0)) + float(usdt.get("locked", 0))
        enriched = []
        for b in account.get("balances", []):
            qty = float(b["free"]) + float(b["locked"])
            if qty <= 0:
                continue
            asset = b["asset"]
            val = 0.0
            if asset == "USDT":
                val = qty
            else:
                try:
                    ticker = bc.get_symbol_ticker(symbol=asset + "USDT")
                    val = qty * float(ticker["price"])
                    total_value += val
                except Exception:
                    pass
            enriched.append({"asset": asset, "free": float(b["free"]), "locked": float(b["locked"]), "value_usdt": round(val, 4)})
        return JSONResponse({
            "paper_mode":        False,
            "usdt_free":         float(usdt.get("free", 0)),
            "usdt_locked":       float(usdt.get("locked", 0)),
            "total_value_usdt":  round(total_value, 4),
            "balances":          enriched,
        })
    except BinanceAPIException as e:
        return JSONResponse({"error": f"Binance: {e.message}"})
    except Exception as e:
        logger.error(f"[WebUI] /api/balance: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)



@app.get("/api/ta")
def api_ta(symbol: str = "BTCUSDT", interval: str = "15m"):
    try:
        api_key    = os.getenv("BINANCE_API_KEY", "")
        api_secret = os.getenv("BINANCE_SECRET_KEY", "")
        if not api_key:
            return JSONResponse({"error": "مفاتيح Binance غير موجودة"})
        import ta_engine
        bc = BinanceClient(api_key, api_secret)
        result = ta_engine.get_ta_signal(bc, symbol.upper(), interval)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/cycles")
def api_cycles():
    try:
        rows = db.get_cycles(limit=20)
        result = []
        for r in rows:
            item = jsonable(dict(r))
            if isinstance(item.get("analysis_result"), str) and item["analysis_result"]:
                try:
                    item["analysis_result"] = json.loads(item["analysis_result"])
                except json.JSONDecodeError:
                    pass
            result.append(item)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"[WebUI] /api/cycles: {e}")
        return JSONResponse([], status_code=500)



@app.get("/api/coins-config")
def api_get_coins_config():
    import datetime
    config_path = Path(__file__).parent / "data" / "coins_config.json"
    if not config_path.exists():
        return JSONResponse({"selected": [], "updated_at": None})
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
        return JSONResponse(data)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/coins-config")
async def api_post_coins_config(request):
    import datetime
    try:
        body = await request.json()
        selected = [str(s).upper() for s in body.get("selected", [])]
        config_path = Path(__file__).parent / "data" / "coins_config.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"selected": selected, "updated_at": datetime.datetime.utcnow().isoformat()}
        config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.info(f"[WebUI] حُدِّثت قائمة العملات: {len(selected)} عملة")
        return JSONResponse({"ok": True, "count": len(selected)})
    except Exception as e:
        logger.error(f"[WebUI] /api/coins-config POST: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)



@app.get("/api/scalp-status")
def api_scalp_status():
    enabled = os.getenv("SCALP_ENABLED", "true").lower() == "true"
    return JSONResponse({"enabled": enabled})


@app.get("/api/scalp-positions")
def api_scalp_positions():
    try:
        rows = db.get_scalp_positions(limit=30)
        return JSONResponse([jsonable(dict(r)) for r in rows])
    except Exception as e:
        return JSONResponse([], status_code=500)


@app.get("/api/scalp-stats")
def api_scalp_stats():
    try:
        stats = db.get_scalp_stats()
        return JSONResponse(jsonable(dict(stats)))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/scalp-toggle")
async def api_scalp_toggle(request):
    try:
        body = await request.json()
        enabled = bool(body.get("enabled", False))
        env_path = Path(__file__).parent / ".env"
        env_text = env_path.read_text(encoding="utf-8")
        new_val  = "true" if enabled else "false"
        if "SCALP_ENABLED=" in env_text:
            import re
            env_text = re.sub(r"SCALP_ENABLED=\S+", f"SCALP_ENABLED={new_val}", env_text)
        else:
            env_text += f"\nSCALP_ENABLED={new_val}\n"
        env_path.write_text(env_text, encoding="utf-8")
        logger.info(f"[WebUI] SCALP_ENABLED → {new_val}")
        return JSONResponse({"ok": True, "enabled": enabled})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


if __name__ == "__main__":
    db.init_db()
    logger.info(f"[WebUI] تشغيل على http://0.0.0.0:{WEBUI_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=WEBUI_PORT, log_level="warning")
