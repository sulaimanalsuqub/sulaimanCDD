"""
webui.py — واجهة ويب لمراقبة نظام التداول
تشغيل: uvicorn webui:app --host 0.0.0.0 --port 8080
"""

import json
import os
import sys
from decimal import Decimal
from datetime import datetime, timezone

import uvicorn
from fastapi import FastAPI
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

WEBUI_PORT = int(os.getenv("WEBUI_PORT", 8080))


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


# ─── HTML الرئيسي ─────────────────────────────────────────────────────────────
HTML = """<!DOCTYPE html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0"/>
  <title>نظام التداول الذكي</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    body { font-family: 'Segoe UI', Tahoma, sans-serif; }
    .card { @apply bg-slate-800 rounded-xl p-5 shadow-lg; }
    .bullish  { color: #22c55e; }
    .bearish  { color: #ef4444; }
    .neutral  { color: #eab308; }
    .pending  { color: #94a3b8; }
    @keyframes pulse-dot { 0%,100%{opacity:1} 50%{opacity:.3} }
    .pulse-dot { animation: pulse-dot 1.5s infinite; }
  </style>
</head>
<body class="bg-slate-900 text-slate-100 min-h-screen">

<!-- Header -->
<header class="bg-slate-800 border-b border-slate-700 px-6 py-4 flex items-center justify-between">
  <div class="flex items-center gap-3">
    <span class="text-2xl">🤖</span>
    <div>
      <h1 class="text-xl font-bold text-white">نظام التداول الذكي</h1>
      <p class="text-xs text-slate-400">مراقبة في الوقت الفعلي</p>
    </div>
  </div>
  <div class="flex items-center gap-3">
    <span id="status-badge" class="px-3 py-1 rounded-full text-xs font-semibold bg-slate-700 text-slate-300">جارٍ التحميل...</span>
    <div class="text-xs text-slate-500">
      تحديث كل <span id="countdown" class="text-slate-300 font-mono">30</span>ث
    </div>
  </div>
</header>

<main class="p-6 space-y-6 max-w-7xl mx-auto">

  <!-- بطاقات الإحصائيات -->
  <div class="grid grid-cols-2 md:grid-cols-5 gap-4">
    <div class="bg-slate-800 rounded-xl p-5 border border-slate-700">
      <p class="text-slate-400 text-sm mb-1">رصيد USDT</p>
      <p id="balance-usdt" class="text-2xl font-bold text-yellow-400">—</p>
      <p id="balance-locked" class="text-xs text-slate-500 mt-1">محجوز: —</p>
    </div>
    <div class="bg-slate-800 rounded-xl p-5 border border-slate-700">
      <p class="text-slate-400 text-sm mb-1">إجمالي الصفقات</p>
      <p id="stat-trades" class="text-3xl font-bold text-white">—</p>
    </div>
    <div class="bg-slate-800 rounded-xl p-5 border border-slate-700">
      <p class="text-slate-400 text-sm mb-1">الربح / الخسارة</p>
      <p id="stat-pnl" class="text-3xl font-bold">—</p>
    </div>
    <div class="bg-slate-800 rounded-xl p-5 border border-slate-700">
      <p class="text-slate-400 text-sm mb-1">تغريدات محللة</p>
      <p id="stat-tweets" class="text-3xl font-bold text-cyan-400">—</p>
    </div>
    <div class="bg-slate-800 rounded-xl p-5 border border-slate-700">
      <p class="text-slate-400 text-sm mb-1">آخر دورة</p>
      <p id="stat-cycle" class="text-sm font-mono text-slate-300">—</p>
      <p id="stat-cycle-status" class="text-xs mt-1">—</p>
    </div>
  </div>

  <!-- حالة الدورة الحالية -->
  <div class="bg-slate-800 rounded-xl p-5 border border-slate-700">
    <div class="flex flex-col md:flex-row md:items-center md:justify-between gap-3 mb-5">
      <h2 class="text-lg font-bold flex items-center gap-2">
        <span>🔎</span> حالة دورة جمع التغريدات والتحليل
      </h2>
      <div class="flex items-center gap-2">
        <span id="current-cycle-id" class="font-mono text-slate-300">—</span>
        <span id="current-cycle-status">—</span>
      </div>
    </div>
    <div class="grid grid-cols-1 md:grid-cols-4 gap-4 mb-5">
      <div class="bg-slate-900 rounded-lg p-3 border border-slate-700">
        <p class="text-xs text-slate-500 mb-1">الحالة الحالية</p>
        <p id="current-cycle-status-text" class="text-sm font-bold text-white">—</p>
      </div>
      <div class="bg-slate-900 rounded-lg p-3 border border-slate-700">
        <p class="text-xs text-slate-500 mb-1">جمع التغريدات</p>
        <p id="current-collector-status" class="text-sm font-mono text-slate-300">—</p>
      </div>
      <div class="bg-slate-900 rounded-lg p-3 border border-slate-700">
        <p class="text-xs text-slate-500 mb-1">تحليل Claude</p>
        <p id="current-analyzer-status" class="text-sm font-mono text-slate-300">—</p>
      </div>
      <div class="bg-slate-900 rounded-lg p-3 border border-slate-700">
        <p class="text-xs text-slate-500 mb-1">عدد التغريدات</p>
        <p id="current-tweets-count" class="text-xl font-bold text-cyan-300">—</p>
      </div>
    </div>
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-5">
      <div class="bg-slate-900 rounded-lg p-3 border border-slate-700">
        <p class="text-xs text-slate-500 mb-1">ملف التغريدات</p>
        <p id="current-tweets-file" class="text-xs font-mono text-slate-300 break-all">—</p>
      </div>
      <div class="bg-slate-900 rounded-lg p-3 border border-slate-700">
        <p class="text-xs text-slate-500 mb-1">الخطأ</p>
        <p id="current-error" class="text-xs text-red-300 break-words">—</p>
      </div>
    </div>
    <div class="bg-slate-900 rounded-lg p-4 border border-slate-700">
      <h3 class="font-bold text-white mb-3">نتيجة التحليل</h3>
      <div id="current-analysis-result">
        <p class="text-slate-500 text-sm">لا توجد نتيجة تحليل بعد</p>
      </div>
    </div>
  </div>

  <!-- التحليل والقرارات -->
  <div class="grid grid-cols-1 md:grid-cols-2 gap-6">

    <!-- آخر تحليل -->
    <div class="bg-slate-800 rounded-xl p-5 border border-slate-700">
      <h2 class="text-lg font-bold mb-4 flex items-center gap-2">
        <span>📊</span> آخر تحليل Claude
      </h2>
      <div id="analysis-content" class="space-y-3">
        <p class="text-slate-500 text-sm">لا يوجد تحليل بعد</p>
      </div>
    </div>

    <!-- آخر القرارات -->
    <div class="bg-slate-800 rounded-xl p-5 border border-slate-700">
      <h2 class="text-lg font-bold mb-4 flex items-center gap-2">
        <span>🎯</span> القرارات الأخيرة
      </h2>
      <div id="decisions-content">
        <p class="text-slate-500 text-sm">لا توجد قرارات</p>
      </div>
    </div>
  </div>

  <!-- جدول الصفقات -->
  <div class="bg-slate-800 rounded-xl p-5 border border-slate-700">
    <h2 class="text-lg font-bold mb-4 flex items-center gap-2">
      <span>💰</span> آخر الصفقات
    </h2>
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="text-slate-400 border-b border-slate-700 text-right">
            <th class="pb-3 font-medium">العملة</th>
            <th class="pb-3 font-medium">الإجراء</th>
            <th class="pb-3 font-medium">المبلغ</th>
            <th class="pb-3 font-medium">السعر</th>
            <th class="pb-3 font-medium">الحالة</th>
            <th class="pb-3 font-medium">PnL</th>
            <th class="pb-3 font-medium">الوقت</th>
          </tr>
        </thead>
        <tbody id="trades-body">
          <tr><td colspan="7" class="text-slate-500 py-4 text-center">لا توجد صفقات</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- سجل الدورات -->
  <div class="bg-slate-800 rounded-xl p-5 border border-slate-700">
    <h2 class="text-lg font-bold mb-4 flex items-center gap-2">
      <span>🔄</span> سجل الدورات
    </h2>
    <div class="overflow-x-auto">
      <table class="w-full text-sm">
        <thead>
          <tr class="text-slate-400 border-b border-slate-700 text-right">
            <th class="pb-3 font-medium">#</th>
            <th class="pb-3 font-medium">بدأت</th>
            <th class="pb-3 font-medium">الحالة</th>
            <th class="pb-3 font-medium">التغريدات</th>
            <th class="pb-3 font-medium">ملف التغريدات</th>
            <th class="pb-3 font-medium">الخطأ</th>
          </tr>
        </thead>
        <tbody id="cycles-body">
          <tr><td colspan="6" class="text-slate-500 py-4 text-center">لا توجد دورات</td></tr>
        </tbody>
      </table>
    </div>
  </div>

</main>

<script>
const REFRESH = 10;
let countdown = REFRESH;

function fmtDt(dt) {
  if (!dt) return '—';
  return new Date(dt).toLocaleString('ar-SA', {
    year:'numeric', month:'2-digit', day:'2-digit',
    hour:'2-digit', minute:'2-digit', second:'2-digit',
    hour12: false
  });
}

function sentimentClass(s) {
  if (!s) return '';
  if (s === 'bullish') return 'text-green-400';
  if (s === 'bearish') return 'text-red-400';
  return 'text-yellow-400';
}

function sentimentAr(s) {
  return {bullish:'صاعد 📈', bearish:'هابط 📉', neutral:'محايد ➡️'}[s] || s || '—';
}

function statusAr(s) {
  return {
    collecting: 'جاري جمع التغريدات',
    collected: 'تم جمع التغريدات',
    analyzing: 'جاري تحليل التغريدات بواسطة Claude',
    analyzed: 'تم التحليل',
    collector_failed: 'فشل جمع التغريدات',
    analyzer_failed: 'فشل التحليل',
    decision_failed: 'فشل اتخاذ القرار',
    trader_failed: 'فشل تنفيذ التداول',
    no_new_tweets: 'لا توجد تغريدات جديدة',
    running: 'تعمل',
    completed: 'مكتملة',
    failed: 'فاشلة'
  }[s] || s || '—';
}

function actionBadge(a) {
  const map = {
    buy:  '<span class="px-2 py-0.5 rounded bg-green-900 text-green-300 font-bold">شراء</span>',
    sell: '<span class="px-2 py-0.5 rounded bg-red-900  text-red-300  font-bold">بيع</span>',
    watch: '<span class="px-2 py-0.5 rounded bg-cyan-900 text-cyan-300 font-bold">مراقبة</span>',
    avoid: '<span class="px-2 py-0.5 rounded bg-orange-900 text-orange-300 font-bold">تجنب</span>',
    no_trade: '<span class="px-2 py-0.5 rounded bg-slate-700 text-slate-300 font-bold">لا تداول</span>',
    hold: '<span class="px-2 py-0.5 rounded bg-slate-700 text-slate-300 font-bold">انتظار</span>',
  };
  return map[a] || a;
}

function statusBadge(s) {
  const map = {
    completed: '<span class="px-2 py-0.5 rounded bg-green-900  text-green-300  text-xs">مكتملة</span>',
    running:   '<span class="px-2 py-0.5 rounded bg-cyan-900   text-cyan-300   text-xs">تعمل</span>',
    failed:    '<span class="px-2 py-0.5 rounded bg-red-900    text-red-300    text-xs">فاشلة</span>',
    collecting:'<span class="px-2 py-0.5 rounded bg-cyan-900   text-cyan-300   text-xs">جاري جمع التغريدات</span>',
    collected: '<span class="px-2 py-0.5 rounded bg-blue-900   text-blue-300   text-xs">تم جمع التغريدات</span>',
    analyzing: '<span class="px-2 py-0.5 rounded bg-purple-900 text-purple-300 text-xs">جاري تحليل التغريدات بواسطة Claude</span>',
    analyzed:  '<span class="px-2 py-0.5 rounded bg-green-900  text-green-300  text-xs">تم التحليل</span>',
    collector_failed: '<span class="px-2 py-0.5 rounded bg-red-900 text-red-300 text-xs">فشل جمع التغريدات</span>',
    analyzer_failed:  '<span class="px-2 py-0.5 rounded bg-red-900 text-red-300 text-xs">فشل التحليل</span>',
    decision_failed:  '<span class="px-2 py-0.5 rounded bg-red-900 text-red-300 text-xs">فشل اتخاذ القرار</span>',
    trader_failed:    '<span class="px-2 py-0.5 rounded bg-red-900 text-red-300 text-xs">فشل تنفيذ التداول</span>',
    no_new_tweets:    '<span class="px-2 py-0.5 rounded bg-slate-700 text-slate-300 text-xs">لا توجد تغريدات جديدة</span>',
    filled:    '<span class="px-2 py-0.5 rounded bg-green-900  text-green-300  text-xs">منفذة</span>',
    pending:   '<span class="px-2 py-0.5 rounded bg-yellow-900 text-yellow-300 text-xs">معلقة</span>',
    cancelled: '<span class="px-2 py-0.5 rounded bg-slate-700  text-slate-400  text-xs">ملغاة</span>',
  };
  return map[s] || `<span class="text-slate-400 text-xs">${s}</span>`;
}

function parseAnalysisResult(raw) {
  if (!raw) return null;
  if (typeof raw === 'object') return raw;
  try { return JSON.parse(raw); } catch { return {summary: raw}; }
}

function renderRecommendations(analysis) {
  const recs = (analysis.recommendations || []).slice(0, 8);
  if (!recs.length) {
    return '<p class="text-sm text-slate-500">لا توجد توصيات واضحة</p>';
  }
  return '<div class="space-y-2">' + recs.map(r => {
    const accounts = (r.supporting_accounts || r.accounts || []).slice(0, 4).join(', ');
    const evidence = (r.evidence || []).slice(0, 2).map(e => `<li>${e}</li>`).join('');
    return `
      <div class="rounded-lg border border-slate-700 bg-slate-950 p-3">
        <div class="flex flex-wrap items-center justify-between gap-2 mb-2">
          <div class="flex items-center gap-2">
            <span class="font-bold text-cyan-300">${r.symbol || '—'}</span>
            ${actionBadge(r.action || 'watch')}
          </div>
          <span class="text-xs font-mono text-slate-300">${r.confidence ?? 0}%</span>
        </div>
        <p class="text-sm text-slate-300 leading-relaxed">${r.reason || '—'}</p>
        ${accounts ? `<p class="text-xs text-slate-500 mt-2">الحسابات: ${accounts}</p>` : ''}
        ${evidence ? `<ul class="list-disc list-inside text-xs text-slate-400 mt-2 space-y-1">${evidence}</ul>` : ''}
        ${r.risk ? `<p class="text-xs text-orange-300 mt-2">المخاطر: ${r.risk}</p>` : ''}
      </div>
    `;
  }).join('') + '</div>';
}

async function loadCurrentCycle() {
  try {
    const r = await fetch('/api/current-cycle');
    const d = await r.json();
    if (!d || d.error) return;

    document.getElementById('current-cycle-id').textContent = d.cycle_id ? `#${d.cycle_id}` : '—';
    document.getElementById('current-cycle-status').innerHTML = statusBadge(d.status);
    document.getElementById('current-cycle-status-text').textContent = statusAr(d.status);
    document.getElementById('current-collector-status').textContent = d.collector_status || '—';
    document.getElementById('current-analyzer-status').textContent = d.analyzer_status || '—';
    document.getElementById('current-tweets-count').textContent = d.tweets_count ?? 0;
    document.getElementById('current-tweets-file').textContent = d.tweets_file_path || '—';
    document.getElementById('current-error').textContent = d.error_message || '—';

    const analysis = parseAnalysisResult(d.analysis_result);
    const resultEl = document.getElementById('current-analysis-result');
    if (!analysis) {
      resultEl.innerHTML = '<p class="text-slate-500 text-sm">لا توجد نتيجة تحليل بعد</p>';
    } else {
      const signals = (analysis.strong_signals || []).slice(0, 6).map(sig =>
        `<li class="text-sm text-slate-300"><span class="font-bold text-cyan-300">${sig.symbol || '—'}</span> ${sig.sentiment || ''}: ${sig.reason || ''}</li>`
      ).join('');
      const coins = (analysis.coins || []).slice(0, 10).map(c =>
        `<span class="px-2 py-0.5 rounded bg-slate-700 text-xs ${sentimentClass(c.sentiment)}">${c.symbol || '—'} (${c.mentions || 0})</span>`
      ).join(' ');
      resultEl.innerHTML = `
        <div class="space-y-3">
          <div class="flex items-center justify-between gap-3">
            <span class="text-lg font-bold ${sentimentClass(analysis.market_sentiment)}">${sentimentAr(analysis.market_sentiment)}</span>
            <span class="text-sm font-mono text-slate-300">${analysis.confidence ?? 0}%</span>
          </div>
          <p class="text-sm text-slate-300 leading-relaxed whitespace-pre-wrap">${analysis.summary || analysis.reasoning || '—'}</p>
          <div>
            <h4 class="font-bold text-white mb-2">توصيات Claude</h4>
            ${renderRecommendations(analysis)}
          </div>
          <div class="flex flex-wrap gap-1">${coins || '<span class="text-slate-500 text-xs">لا رموز</span>'}</div>
          <ul class="list-disc list-inside space-y-1">${signals || '<li class="text-sm text-slate-500">لا إشارات قوية</li>'}</ul>
          <p class="text-xs text-yellow-300">${analysis.trading_note || 'التداول الحقيقي معطل حاليًا.'}</p>
        </div>
      `;
    }

    if (['collecting', 'analyzing'].includes(d.status)) {
      countdown = Math.min(countdown, 5);
    }
  } catch(e) { console.error('current cycle error', e); }
}

async function loadStats() {
  try {
    const r = await fetch('/api/stats');
    const d = await r.json();

    document.getElementById('stat-trades').textContent = d.total_trades ?? '0';

    const pnl = parseFloat(d.total_pnl || 0);
    const pnlEl = document.getElementById('stat-pnl');
    pnlEl.textContent = (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + ' $';
    pnlEl.className = 'text-3xl font-bold ' + (pnl >= 0 ? 'text-green-400' : 'text-red-400');

    document.getElementById('stat-tweets').textContent = d.recent_tweets ?? '0';
    document.getElementById('stat-cycle').textContent  = fmtDt(d.last_cycle);

    const st = d.last_status;
    const statusEl = document.getElementById('stat-cycle-status');
    statusEl.innerHTML = statusBadge(st);

    const badge = document.getElementById('status-badge');
    if (st === 'completed') {
      badge.className = 'px-3 py-1 rounded-full text-xs font-semibold bg-green-900 text-green-300';
      badge.textContent = '● يعمل';
    } else if (st === 'running') {
      badge.className = 'px-3 py-1 rounded-full text-xs font-semibold bg-cyan-900 text-cyan-300';
      badge.innerHTML = '<span class="pulse-dot">●</span> جارٍ...';
    } else if (st === 'failed') {
      badge.className = 'px-3 py-1 rounded-full text-xs font-semibold bg-red-900 text-red-300';
      badge.textContent = '● خطأ';
    } else {
      badge.className = 'px-3 py-1 rounded-full text-xs font-semibold bg-slate-700 text-slate-300';
      badge.textContent = '○ غير نشط';
    }
  } catch(e) { console.error('stats error', e); }
}

async function loadAnalysis() {
  try {
    const r = await fetch('/api/analysis');
    const d = await r.json();
    const el = document.getElementById('analysis-content');
    if (!d || d.error) { el.innerHTML = '<p class="text-slate-500 text-sm">لا يوجد تحليل</p>'; return; }

    const coins = (typeof d.coins === 'string' ? JSON.parse(d.coins) : d.coins) || [];
    const topCoins = coins.slice(0, 8).map(c =>
      `<span class="px-2 py-0.5 rounded text-xs ${sentimentClass(c.sentiment)} bg-slate-700">${c.symbol}(${c.mentions})</span>`
    ).join(' ');

    el.innerHTML = `
      <div class="flex items-center justify-between mb-2">
        <span class="text-2xl font-bold ${sentimentClass(d.sentiment)}">${sentimentAr(d.sentiment)}</span>
        <span class="text-lg font-bold text-white">${d.confidence}%</span>
      </div>
      <div class="w-full bg-slate-700 rounded-full h-2 mb-3">
        <div class="h-2 rounded-full ${d.sentiment==='bullish'?'bg-green-500':d.sentiment==='bearish'?'bg-red-500':'bg-yellow-500'}"
             style="width:${d.confidence}%"></div>
      </div>
      <p class="text-slate-400 text-xs mb-3 leading-relaxed">${(d.reasoning||'').substring(0,200)}${(d.reasoning||'').length>200?'...':''}</p>
      <div class="mb-3">
        <h3 class="text-sm font-bold text-white mb-2">توصيات Claude</h3>
        ${renderRecommendations(d)}
      </div>
      <div class="flex flex-wrap gap-1">${topCoins || '<span class="text-slate-500 text-xs">لا عملات</span>'}</div>
      <p class="text-slate-600 text-xs mt-3">${fmtDt(d.analyzed_at)}</p>
    `;
  } catch(e) { console.error('analysis error', e); }
}

async function loadDecisions() {
  try {
    const r = await fetch('/api/decisions');
    const d = await r.json();
    const el = document.getElementById('decisions-content');
    if (!d.length) { el.innerHTML = '<p class="text-slate-500 text-sm">لا توجد قرارات</p>'; return; }

    el.innerHTML = '<div class="space-y-2">' +
      d.map(dec => `
        <div class="flex items-center justify-between bg-slate-700 rounded-lg px-4 py-2">
          <div class="flex items-center gap-3">
            <span class="font-bold text-cyan-300">${dec.coin}</span>
            ${actionBadge(dec.action)}
          </div>
          <div class="text-left">
            <span class="text-white font-mono">${parseFloat(dec.amount||0).toFixed(2)} $</span>
            <span class="text-slate-400 text-xs mr-2">${dec.confidence}%</span>
          </div>
        </div>
      `).join('') + '</div>';
  } catch(e) { console.error('decisions error', e); }
}

async function loadTrades() {
  try {
    const r = await fetch('/api/trades');
    const d = await r.json();
    const tbody = document.getElementById('trades-body');
    if (!d.length) {
      tbody.innerHTML = '<tr><td colspan="7" class="text-slate-500 py-4 text-center">لا توجد صفقات</td></tr>';
      return;
    }
    tbody.innerHTML = d.map(t => {
      const pnl = parseFloat(t.pnl || 0);
      const pnlStr = (pnl >= 0 ? '+' : '') + pnl.toFixed(2);
      const pnlCls = pnl >= 0 ? 'text-green-400' : 'text-red-400';
      return `<tr class="border-b border-slate-700 hover:bg-slate-750">
        <td class="py-3 font-bold text-cyan-300">${t.coin}</td>
        <td class="py-3">${actionBadge(t.action)}</td>
        <td class="py-3 font-mono">${parseFloat(t.amount||0).toFixed(2)}</td>
        <td class="py-3 font-mono text-slate-300">${t.price ? parseFloat(t.price).toFixed(4) : '—'}</td>
        <td class="py-3">${statusBadge(t.status)}</td>
        <td class="py-3 font-mono font-bold ${pnlCls}">${pnlStr}</td>
        <td class="py-3 text-slate-500 text-xs">${fmtDt(t.executed_at)}</td>
      </tr>`;
    }).join('');
  } catch(e) { console.error('trades error', e); }
}

async function loadCycles() {
  try {
    const r = await fetch('/api/cycles');
    const d = await r.json();
    const tbody = document.getElementById('cycles-body');
    if (!d.length) {
      tbody.innerHTML = '<tr><td colspan="6" class="text-slate-500 py-4 text-center">لا توجد دورات</td></tr>';
      return;
    }
    tbody.innerHTML = d.map(c => `
      <tr class="border-b border-slate-700">
        <td class="py-3 text-slate-400 font-mono">#${c.cycle_id || c.id}</td>
        <td class="py-3 text-xs text-slate-300">${fmtDt(c.started_at)}</td>
        <td class="py-3">${statusBadge(c.status)}</td>
        <td class="py-3 text-cyan-300 font-mono">${c.tweets_count ?? 0}</td>
        <td class="py-3 text-xs text-slate-400 font-mono break-all">${c.tweets_file_path || '—'}</td>
        <td class="py-3 text-xs text-red-400">${c.error_message ? c.error_message.substring(0,90) : '—'}</td>
      </tr>
    `).join('');
  } catch(e) { console.error('cycles error', e); }
}

async function loadBalance() {
  try {
    const r = await fetch('/api/balance');
    const d = await r.json();
    if (d.error) {
      document.getElementById('balance-usdt').textContent   = 'خطأ';
      document.getElementById('balance-locked').textContent = d.error.substring(0, 30);
      return;
    }
    const free   = parseFloat(d.usdt_free   || 0);
    const locked = parseFloat(d.usdt_locked || 0);
    document.getElementById('balance-usdt').textContent   = free.toFixed(2) + ' $';
    document.getElementById('balance-locked').textContent = 'محجوز: ' + locked.toFixed(2) + ' $';
  } catch(e) { console.error('balance error', e); }
}

async function refresh() {
  await Promise.all([loadStats(), loadBalance(), loadCurrentCycle(), loadAnalysis(), loadDecisions(), loadTrades(), loadCycles()]);
}

// تحديث تلقائي
setInterval(() => {
  countdown--;
  document.getElementById('countdown').textContent = countdown;
  if (countdown <= 0) { countdown = REFRESH; refresh(); }
}, 1000);

// تحميل أولي
refresh();
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
        result = jsonable(dict(stats))
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"[WebUI] خطأ في /api/stats: {e}")
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
        logger.error(f"[WebUI] خطأ في /api/current-cycle: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/analysis")
def api_analysis():
    try:
        analysis = db.get_latest_analysis()
        if not analysis:
            return JSONResponse({})
        result = jsonable(dict(analysis))
        # coins JSONB → list
        if isinstance(result.get("coins"), str):
            result["coins"] = json.loads(result["coins"])
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT analysis_result
                   FROM cycles
                   WHERE id = %s AND analysis_result IS NOT NULL
                   LIMIT 1""",
                (result.get("cycle_id"),),
            )
            cycle = cur.fetchone()
        if cycle and cycle.get("analysis_result"):
            try:
                full = json.loads(cycle["analysis_result"])
                if isinstance(full, dict):
                    result.update(full)
            except json.JSONDecodeError:
                pass
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"[WebUI] خطأ في /api/analysis: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/decisions")
def api_decisions():
    try:
        decisions = db.get_latest_decisions()
        result = []
        for d in decisions:
            result.append(jsonable(dict(d)))
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"[WebUI] خطأ في /api/decisions: {e}")
        return JSONResponse([], status_code=500)


@app.get("/api/trades")
def api_trades():
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT coin, action, amount, price, status, pnl, executed_at, order_id
                   FROM   trades
                   ORDER  BY executed_at DESC
                   LIMIT  20"""
            )
            rows = cur.fetchall()
        result = []
        for r in rows:
            result.append(jsonable(dict(r)))
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"[WebUI] خطأ في /api/trades: {e}")
        return JSONResponse([], status_code=500)


@app.get("/api/balance")
def api_balance():
    try:
        if os.getenv("TRADING_ENABLED", "false").lower() != "true":
            return JSONResponse({
                "paper_mode": True,
                "usdt_free": float(os.getenv("PAPER_CAPITAL_USDT", "1000")),
                "usdt_locked": 0,
                "balances": [],
                "message": "التداول الحقيقي معطل TRADING_ENABLED=false",
            })

        api_key    = os.getenv("BINANCE_API_KEY", "")
        api_secret = os.getenv("BINANCE_SECRET_KEY", "")
        if not api_key or not api_secret:
            return JSONResponse({"error": "مفاتيح Binance غير مضافة"})

        client = BinanceClient(api_key, api_secret)
        account = client.get_account()

        balances = {b["asset"]: b for b in account.get("balances", [])}

        usdt = balances.get("USDT", {})
        result = {
            "usdt_free":   float(usdt.get("free", 0)),
            "usdt_locked": float(usdt.get("locked", 0)),
            "balances": [
                {"asset": b["asset"],
                 "free":  float(b["free"]),
                 "locked": float(b["locked"])}
                for b in account.get("balances", [])
                if float(b["free"]) > 0 or float(b["locked"]) > 0
            ]
        }
        return JSONResponse(result)

    except BinanceAPIException as e:
        logger.error(f"[WebUI] خطأ Binance في /api/balance: {e}")
        return JSONResponse({"error": f"Binance: {e.message}"})
    except Exception as e:
        logger.error(f"[WebUI] خطأ في /api/balance: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/cycles")
def api_cycles():
    try:
        rows = db.get_cycles(limit=15)
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
        logger.error(f"[WebUI] خطأ في /api/cycles: {e}")
        return JSONResponse([], status_code=500)


# ─── تشغيل مباشر ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    db.init_db()
    logger.info(f"[WebUI] تشغيل الواجهة على http://0.0.0.0:{WEBUI_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=WEBUI_PORT, log_level="warning")
