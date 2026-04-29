#!/bin/bash
# setup.sh — تثبيت كامل لنظام التداول على Ubuntu 24.04
# تشغيل: sudo bash setup.sh

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log()     { echo -e "${GREEN}[+]${NC} $1"; }
warn()    { echo -e "${YELLOW}[!]${NC} $1"; }
error()   { echo -e "${RED}[✗]${NC} $1"; exit 1; }
success() { echo -e "${GREEN}[✓]${NC} $1"; }

# ─── التحقق من صلاحيات root ────────────────────────────────────────────────────
[[ $EUID -ne 0 ]] && error "يجب تشغيل السكريبت بصلاحيات root: sudo bash setup.sh"

# ─── 1. تحديث النظام ──────────────────────────────────────────────────────────
log "تحديث قائمة الحزم..."
apt-get update -qq

# ─── 2. تثبيت Python 3.11+ ────────────────────────────────────────────────────
log "تثبيت Python والأدوات الأساسية..."
apt-get install -y -qq \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    libpq-dev \
    curl \
    git

PYTHON_VER=$(python3 --version | awk '{print $2}')
success "Python $PYTHON_VER مثبت"

# ─── 3. تثبيت PostgreSQL ──────────────────────────────────────────────────────
log "تثبيت PostgreSQL..."
apt-get install -y -qq postgresql postgresql-contrib

log "تشغيل وتمكين PostgreSQL..."
systemctl enable postgresql
systemctl start postgresql

success "PostgreSQL مثبت ويعمل"

# ─── 4. إنشاء قاعدة البيانات والمستخدم ─────────────────────────────────────────
log "إعداد قاعدة البيانات trading_bot..."

DB_NAME="trading_bot"
DB_USER="postgres"

# توليد كلمة مرور عشوائية
DB_PASS=$(openssl rand -base64 16 | tr -d '/+=')

# إنشاء قاعدة البيانات إذا لم تكن موجودة
sudo -u postgres psql -c "SELECT 1 FROM pg_database WHERE datname='${DB_NAME}';" \
    | grep -q 1 || sudo -u postgres createdb "${DB_NAME}"

# تحديث كلمة مرور postgres
sudo -u postgres psql -c "ALTER USER ${DB_USER} WITH PASSWORD '${DB_PASS}';"

success "قاعدة البيانات '${DB_NAME}' جاهزة"

# ─── 5. إنشاء بيئة Python الافتراضية ──────────────────────────────────────────
BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${BOT_DIR}/venv"

log "إنشاء بيئة Python افتراضية في ${VENV_DIR}..."
python3 -m venv "${VENV_DIR}"
source "${VENV_DIR}/bin/activate"

pip install --upgrade pip -q
success "البيئة الافتراضية جاهزة"

# ─── 6. تثبيت المتطلبات ───────────────────────────────────────────────────────
log "تثبيت متطلبات Python من requirements.txt..."
pip install -r "${BOT_DIR}/requirements.txt" -q
success "جميع المتطلبات مثبتة"

# ─── 7. إنشاء ملف .env إذا لم يكن موجوداً ───────────────────────────────────
ENV_FILE="${BOT_DIR}/.env"
if [[ ! -f "${ENV_FILE}" ]]; then
    log "إنشاء ملف .env..."
    cat > "${ENV_FILE}" << EOF
ANTHROPIC_API_KEY=
BINANCE_API_KEY=
BINANCE_SECRET_KEY=
BINANCE_FUTURES=false
TRADING_ENABLED=false
PAPER_CAPITAL_USDT=1000
DB_HOST=localhost
DB_PORT=5432
DB_NAME=${DB_NAME}
DB_USER=${DB_USER}
DB_PASS=${DB_PASS}
MIN_CONFIDENCE=60
INTERVAL_MINUTES=5
TWEETS_PER_ACCOUNT=5
MAX_CONCURRENT=10
STOP_LOSS_PCT=2.0
TAKE_PROFIT_PCT=4.0
DASHBOARD_REFRESH=30
CLAUDE_MODEL=claude-3-5-sonnet-20241022
MAX_TWEETS_IN_PROMPT=200
EOF
    chmod 600 "${ENV_FILE}"
    success "تم إنشاء .env (صلاحيات مقيدة 600)"
else
    warn ".env موجود بالفعل — لم يُعدَّل"
    # تحديث DB_PASS في الملف الموجود إذا كان فارغاً
    if grep -q "DB_PASS=$" "${ENV_FILE}"; then
        sed -i "s/DB_PASS=$/DB_PASS=${DB_PASS}/" "${ENV_FILE}"
        log "تم تحديث DB_PASS في .env"
    fi
fi

# ─── 8. إنشاء جداول قاعدة البيانات ──────────────────────────────────────────
log "إنشاء جداول قاعدة البيانات..."
cd "${BOT_DIR}"
source "${VENV_DIR}/bin/activate"
python3 database.py && success "الجداول جاهزة"

# ─── 9. تهيئة twscrape ────────────────────────────────────────────────────────
log "تهيئة twscrape..."
python3 -c "from twscrape import API; API()" 2>/dev/null || true

# إنشاء accounts.txt نموذجي إذا لم يكن موجوداً
ACCOUNTS_FILE="${BOT_DIR}/accounts.txt"
if [[ ! -f "${ACCOUNTS_FILE}" ]]; then
    cat > "${ACCOUNTS_FILE}" << 'EOF'
# أضف حسابات X هنا — حساب في كل سطر
# يمكن إضافة @ أو بدونها
# مثال:
# elonmusk
# cz_binance
# VitalikButerin
EOF
    success "تم إنشاء accounts.txt النموذجي"
fi

# ─── 10. إنشاء systemd service ───────────────────────────────────────────────
log "إنشاء systemd service للبوت..."

cat > /etc/systemd/system/trading-bot.service << EOF
[Unit]
Description=نظام التداول الذكي
After=network.target postgresql.service
Requires=postgresql.service

[Service]
Type=simple
User=root
WorkingDirectory=${BOT_DIR}
ExecStart=${VENV_DIR}/bin/python3 ${BOT_DIR}/scheduler.py
Restart=on-failure
RestartSec=30
StandardOutput=append:${BOT_DIR}/bot.log
StandardError=append:${BOT_DIR}/bot.log
EnvironmentFile=${BOT_DIR}/.env

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
success "تم إنشاء trading-bot.service"

# ─── 11. إنشاء systemd service للواجهة ──────────────────────────────────────
log "إنشاء systemd service للواجهة الويب..."

cat > /etc/systemd/system/trading-webui.service << EOF
[Unit]
Description=واجهة الويب — نظام التداول الذكي
After=network.target postgresql.service trading-bot.service

[Service]
Type=simple
User=root
WorkingDirectory=${BOT_DIR}
ExecStart=${VENV_DIR}/bin/uvicorn webui:app --host 0.0.0.0 --port 8080
Restart=on-failure
RestartSec=10
StandardOutput=append:${BOT_DIR}/bot.log
StandardError=append:${BOT_DIR}/bot.log
EnvironmentFile=${BOT_DIR}/.env

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
success "تم إنشاء trading-webui.service (port 8080)"

# ─── ملخص التثبيت ─────────────────────────────────────────────────────────────
echo ""
echo "════════════════════════════════════════════════════════"
echo -e "${GREEN}  تم التثبيت بنجاح ✓${NC}"
echo "════════════════════════════════════════════════════════"
echo ""
echo "  الخطوات التالية:"
echo ""
echo "  1) افتح ملف .env وأضف مفاتيحك:"
echo "     nano ${ENV_FILE}"
echo ""
echo "  2) أضف حسابات X للمراقبة:"
echo "     nano ${ACCOUNTS_FILE}"
echo ""
echo "  3) أضف حسابات twscrape لجلب التغريدات:"
echo "     source ${VENV_DIR}/bin/activate"
echo "     twscrape add_accounts accounts_auth.txt --cookies"
echo "     twscrape login_all"
echo ""
echo "  4) شغّل البوت والواجهة:"
echo "     systemctl start trading-bot trading-webui"
echo "     systemctl enable trading-bot trading-webui"
echo ""
echo "  5) تابع السجلات:"
echo "     tail -f ${BOT_DIR}/bot.log"
echo "     journalctl -u trading-bot -f"
echo ""
echo "  6) الواجهة الويب:"
echo "     http://5.78.66.14:8080"
echo ""
echo "  7) نشر التحديثات من GitHub:"
echo "     bash ${BOT_DIR}/deploy.sh"
echo ""
echo "  8) لوحة Terminal:"
echo "     source ${VENV_DIR}/bin/activate && python3 ${BOT_DIR}/dashboard.py"
echo ""
echo "  كلمة مرور PostgreSQL: ${DB_PASS}"
echo "  (محفوظة في .env)"
echo ""
