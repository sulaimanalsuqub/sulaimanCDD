#!/bin/bash
# deploy.sh — يسحب آخر تحديثات GitHub ويعيد تشغيل الخدمات
# تشغيل: bash deploy.sh
# يمكن ربطه بـ GitHub Webhook أو cron

set -euo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

log()   { echo -e "${GREEN}[+]${NC} $1"; }
error() { echo -e "${RED}[✗]${NC} $1"; exit 1; }

# مجلد المشروع
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOT_DIR="${REPO_DIR}/trading_bot"

log "سحب آخر التحديثات من GitHub..."
cd "${REPO_DIR}"
git pull origin main || error "فشل git pull"

log "إعادة تشغيل الخدمات..."
systemctl restart trading-bot  2>/dev/null && log "trading-bot أُعيد تشغيله" || true
systemctl restart trading-webui 2>/dev/null && log "trading-webui أُعيد تشغيله" || true

log "حالة الخدمات:"
systemctl is-active trading-bot   --quiet && echo "  ✓ trading-bot   يعمل" || echo "  ✗ trading-bot   متوقف"
systemctl is-active trading-webui --quiet && echo "  ✓ trading-webui يعمل" || echo "  ✗ trading-webui متوقف"

echo ""
log "تم النشر بنجاح ✓"
