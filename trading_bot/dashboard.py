"""
dashboard.py — لوحة متابعة حية في Terminal باستخدام rich
تشغيل: python dashboard.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

from dotenv import load_dotenv
from loguru import logger
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

import database as db

load_dotenv()

# ─── إعداد اللوجر (Terminal فقط — بدون bot.log للـ dashboard) ────────────────
logger.remove()
logger.add(sys.stderr, format="{time:YYYY-MM-DD HH:mm:ss} | {level:<8} | {message}",
           level="WARNING")

REFRESH_INTERVAL = int(os.getenv("DASHBOARD_REFRESH", 30))  # ثانية
INTERVAL_MINUTES = int(os.getenv("INTERVAL_MINUTES", 60))

console = Console()


# ─── دوال بناء المكونات ────────────────────────────────────────────────────────

def sentiment_color(sentiment: str) -> str:
    return {"bullish": "green", "bearish": "red", "neutral": "yellow"}.get(
        sentiment.lower(), "white"
    )


def action_color(action: str) -> str:
    return {"buy": "green", "sell": "red", "hold": "yellow"}.get(
        action.lower(), "white"
    )


def status_color(status: str) -> str:
    return {
        "completed": "green",
        "running":   "cyan",
        "failed":    "red",
        "filled":    "green",
        "pending":   "yellow",
        "cancelled": "dim",
    }.get(status.lower(), "white")


def fmt_dt(dt) -> str:
    """يُنسّق التاريخ والوقت بصيغة مقروءة."""
    if dt is None:
        return "—"
    if hasattr(dt, "strftime"):
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    return str(dt)


def time_until_next(last_cycle_time, interval_min: int) -> str:
    """يحسب الوقت المتبقي حتى الدورة القادمة."""
    if last_cycle_time is None:
        return "—"
    now = datetime.now(timezone.utc)
    if last_cycle_time.tzinfo is None:
        last_cycle_time = last_cycle_time.replace(tzinfo=timezone.utc)
    elapsed   = (now - last_cycle_time).total_seconds()
    remaining = max(0.0, interval_min * 60 - elapsed)
    mins, secs = divmod(int(remaining), 60)
    return f"{mins:02d}:{secs:02d}"


# ─── بناء اللوحات ─────────────────────────────────────────────────────────────

def build_header(stats: dict) -> Panel:
    """لوحة معلومات الدورة والوقت."""
    last_cycle  = stats.get("last_cycle")
    last_status = stats.get("last_status") or "—"
    next_in     = time_until_next(last_cycle, INTERVAL_MINUTES)
    color       = status_color(last_status)

    text = Text()
    text.append("آخر دورة:  ", style="bold")
    text.append(f"{fmt_dt(last_cycle)}\n", style="cyan")
    text.append("الحالة:    ", style="bold")
    text.append(f"{last_status}\n", style=color)
    text.append("الدورة القادمة خلال:  ", style="bold")
    text.append(f"{next_in}", style="bright_white bold")

    return Panel(text, title="[bold blue]⏱  حالة الدورة[/bold blue]", box=box.ROUNDED)


def build_tweets_panel(stats: dict) -> Panel:
    """لوحة إحصائيات التغريدات."""
    count = stats.get("recent_tweets", 0)
    text = Text()
    text.append("تغريدات آخر 5 دقائق:  ", style="bold")
    text.append(str(count), style="bright_cyan bold")
    return Panel(text, title="[bold blue]🐦  التغريدات[/bold blue]", box=box.ROUNDED)


def build_analysis_panel() -> Panel:
    """لوحة آخر تحليل من Claude."""
    try:
        analysis = db.get_latest_analysis()
    except Exception:
        analysis = None

    if not analysis:
        return Panel(
            Text("لا يوجد تحليل بعد", style="dim"),
            title="[bold blue]📊  التحليل[/bold blue]",
            box=box.ROUNDED,
        )

    sentiment  = analysis.get("sentiment", "—")
    confidence = analysis.get("confidence", 0)
    reasoning  = analysis.get("reasoning", "—") or "—"
    coins_raw  = analysis.get("coins", [])
    if isinstance(coins_raw, str):
        try:
            coins_raw = json.loads(coins_raw)
        except Exception:
            coins_raw = []

    sc = sentiment_color(sentiment)

    text = Text()
    text.append("التوجه العام:  ", style="bold")
    text.append(f"{sentiment}\n", style=f"{sc} bold")
    text.append("الثقة:         ", style="bold")
    text.append(f"{confidence}%\n", style="bright_white")
    text.append("السبب:         ", style="bold")
    text.append(f"{reasoning[:120]}...\n" if len(reasoning) > 120 else f"{reasoning}\n",
                style="dim")
    text.append("العملات:       ", style="bold")

    for c in coins_raw[:8]:
        sym  = c.get("symbol", "?")
        sent = c.get("sentiment", "neutral")
        ment = c.get("mentions", 0)
        text.append(f"{sym}({ment}) ", style=sentiment_color(sent))

    return Panel(text, title="[bold blue]📊  التحليل[/bold blue]", box=box.ROUNDED)


def build_decisions_panel() -> Panel:
    """جدول آخر القرارات."""
    try:
        decisions = db.get_latest_decisions()
    except Exception:
        decisions = []

    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold magenta")
    table.add_column("العملة",  style="cyan",        no_wrap=True)
    table.add_column("الإجراء", justify="center",    no_wrap=True)
    table.add_column("الثقة",   justify="right")
    table.add_column("المبلغ (USDT)", justify="right")
    table.add_column("السبب",   style="dim",         overflow="fold")

    if not decisions:
        table.add_row("—", "—", "—", "—", "لا قرارات")
    else:
        for d in decisions[:10]:
            action = d.get("action", "—")
            ac     = action_color(action)
            table.add_row(
                d.get("coin", "—"),
                Text(action.upper(), style=f"{ac} bold"),
                f"{d.get('confidence', 0)}%",
                f"{float(d.get('amount', 0)):.2f}",
                (d.get("reason") or "—")[:60],
            )

    return Panel(table, title="[bold blue]🎯  القرارات[/bold blue]", box=box.ROUNDED)


def build_trades_panel(stats: dict) -> Panel:
    """جدول آخر الصفقات والربح/الخسارة."""
    try:
        with db.get_cursor() as cur:
            cur.execute(
                """SELECT coin, action, amount, price, status, pnl, executed_at
                   FROM   trades
                   ORDER  BY executed_at DESC
                   LIMIT  10"""
            )
            trades = cur.fetchall()
    except Exception:
        trades = []

    total_pnl    = float(stats.get("total_pnl", 0))
    total_trades = int(stats.get("total_trades", 0))
    pnl_color    = "green" if total_pnl >= 0 else "red"
    pnl_sign     = "+" if total_pnl >= 0 else ""

    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold magenta")
    table.add_column("العملة",   style="cyan",     no_wrap=True)
    table.add_column("الإجراء",  justify="center", no_wrap=True)
    table.add_column("المبلغ",   justify="right")
    table.add_column("السعر",    justify="right")
    table.add_column("الحالة",   justify="center")
    table.add_column("PnL",      justify="right")
    table.add_column("الوقت",    style="dim")

    if not trades:
        table.add_row("—", "—", "—", "—", "—", "—", "—")
    else:
        for t in trades:
            action = t.get("action", "—")
            status = t.get("status", "—")
            pnl    = float(t.get("pnl") or 0)
            pc     = "green" if pnl >= 0 else "red"
            table.add_row(
                t.get("coin", "—"),
                Text(action.upper(), style=f"{action_color(action)} bold"),
                f"{float(t.get('amount', 0)):.2f}",
                f"{float(t.get('price') or 0):.4f}",
                Text(status, style=status_color(status)),
                Text(f"{'+' if pnl >= 0 else ''}{pnl:.2f}", style=pc),
                fmt_dt(t.get("executed_at")),
            )

    summary = Text()
    summary.append(f"\nإجمالي الصفقات: {total_trades}   |   ", style="bold")
    summary.append("إجمالي PnL: ", style="bold")
    summary.append(f"{pnl_sign}{total_pnl:.2f} USDT", style=f"{pnl_color} bold")

    panel_content = table.__rich_console__  # سيُعرض مباشرة

    return Panel(
        table,
        title=f"[bold blue]💰  الصفقات[/bold blue]  "
              f"[{pnl_color}]{pnl_sign}{total_pnl:.2f} USDT PnL[/{pnl_color}]",
        box=box.ROUNDED,
    )


def build_layout(stats: dict) -> Layout:
    """يبني تخطيط الشاشة الكامل."""
    layout = Layout()

    layout.split_column(
        Layout(name="header",    size=7),
        Layout(name="middle",    size=12),
        Layout(name="decisions", size=16),
        Layout(name="trades"),
    )

    layout["middle"].split_row(
        Layout(name="tweets",   ratio=1),
        Layout(name="analysis", ratio=3),
    )

    layout["header"].update(build_header(stats))
    layout["tweets"].update(build_tweets_panel(stats))
    layout["analysis"].update(build_analysis_panel())
    layout["decisions"].update(build_decisions_panel())
    layout["trades"].update(build_trades_panel(stats))

    return layout


def run_dashboard() -> None:
    """يُشغّل اللوحة الحية وتُحدَّث كل REFRESH_INTERVAL ثانية."""
    console.clear()
    console.print(
        Panel(
            "[bold green]نظام التداول الذكي — لوحة المتابعة[/bold green]",
            box=box.DOUBLE,
        )
    )

    with Live(console=console, refresh_per_second=1, screen=True) as live:
        while True:
            try:
                stats  = db.get_stats()
                layout = build_layout(dict(stats))
                live.update(layout)
            except Exception as e:
                live.update(
                    Panel(
                        f"[red]خطأ في تحميل البيانات: {e}[/red]",
                        title="[red]خطأ[/red]",
                    )
                )
            time.sleep(REFRESH_INTERVAL)


if __name__ == "__main__":
    try:
        db.init_db()
        run_dashboard()
    except KeyboardInterrupt:
        console.print("\n[dim]تم إيقاف اللوحة[/dim]")
    finally:
        db.close_pool()
