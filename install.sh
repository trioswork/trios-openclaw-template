#!/usr/bin/env bash
# ============================================================
# OpenClaw Agent Template, instalador genérico
# Uso:
#   bash <(curl -fsSL https://raw.githubusercontent.com/trioswork/trios-openclaw-template/main/install.sh)
# ============================================================
set -Eeuo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'
REPO_URL="${OPENCLAW_TEMPLATE_REPO:-https://github.com/trioswork/trios-openclaw-template.git}"
WS="${OPENCLAW_WORKSPACE:-/root/.openclaw/workspace}"
DB_NAME="${OPENCLAW_MEMORY_DB:-agent_memory}"
DB_USER="${OPENCLAW_MEMORY_USER:-agent}"

echo -e "${BOLD}${CYAN}OpenClaw Agent Template, installer${NC}"

if [ "${EUID:-$(id -u)}" -ne 0 ]; then
  echo -e "${RED}Rode como root: sudo bash install.sh${NC}"
  exit 1
fi

log_step(){ echo -e "${YELLOW}$1${NC}"; }
ok(){ echo -e "${GREEN}  ✓ $1${NC}"; }

log_step '[1/7] Dependências do sistema'
apt-get update -qq
apt-get install -y -qq git curl wget ca-certificates python3 python3-pip python3-venv postgresql postgresql-contrib build-essential libpq-dev ffmpeg openssl >/dev/null
ok 'Sistema pronto'

log_step '[2/7] Node.js 22'
if ! command -v node >/dev/null 2>&1 || [ "$(node -v | cut -d. -f1)" != "v22" ]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | bash - >/dev/null
  apt-get install -y -qq nodejs >/dev/null
fi
ok "Node.js $(node -v)"

log_step '[3/7] PostgreSQL + pgvector'
systemctl enable --now postgresql >/dev/null 2>&1 || true
PG_VER=$(psql --version 2>/dev/null | grep -oE '[0-9]+' | head -1 || echo '16')
apt-get install -y -qq "postgresql-${PG_VER}-pgvector" >/dev/null 2>&1 || apt-get install -y -qq postgresql-16-pgvector >/dev/null 2>&1 || {
  cd /tmp
  rm -rf pgvector
  git clone --depth 1 --branch v0.7.0 https://github.com/pgvector/pgvector.git >/dev/null 2>&1
  cd pgvector && make -j"$(nproc)" >/dev/null && make install >/dev/null
}
PG_PASS=$(openssl rand -hex 16)
sudo -u postgres psql -c "CREATE DATABASE ${DB_NAME};" >/dev/null 2>&1 || true
sudo -u postgres psql -c "CREATE USER ${DB_USER} WITH PASSWORD '${PG_PASS}';" >/dev/null 2>&1 || true
sudo -u postgres psql -d "${DB_NAME}" -c 'CREATE EXTENSION IF NOT EXISTS vector;' >/dev/null 2>&1 || true
sudo -u postgres psql -d "${DB_NAME}" -c "GRANT ALL ON SCHEMA public TO ${DB_USER}; ALTER SCHEMA public OWNER TO ${DB_USER};" >/dev/null 2>&1 || true
ok 'PostgreSQL + pgvector pronto'

log_step '[4/7] Dependências Python'
pip3 install --break-system-packages -q psycopg2-binary requests python-dotenv >/dev/null 2>&1 || pip3 install -q psycopg2-binary requests python-dotenv >/dev/null 2>&1 || true
ok 'Python pronto'

log_step '[5/7] OpenClaw'
if ! command -v openclaw >/dev/null 2>&1; then
  npm install -g openclaw >/dev/null
fi
ok "OpenClaw $(openclaw --version 2>/dev/null || echo instalado)"

log_step '[6/7] Workspace genérico'
mkdir -p /root/.openclaw
if [ -d "$WS/.git" ]; then
  git -C "$WS" pull --ff-only >/dev/null 2>&1 || true
else
  rm -rf "$WS"
  git clone "$REPO_URL" "$WS" >/dev/null
fi
cd "$WS"

mkdir -p memory/context memory/projects memory/sessions memory/integrations memory/feedback skills backups
for f in AGENTS HEARTBEAT IDENTITY MEMORY SOUL USER; do
  if [ ! -f "$WS/${f}.md" ] && [ -f "$WS/templates/${f}.md" ]; then cp "$WS/templates/${f}.md" "$WS/${f}.md"; fi
done

if [ -f scripts/memory-schema.sql ]; then
  sudo -u postgres psql -d "$DB_NAME" -f scripts/memory-schema.sql >/dev/null 2>&1 || true
fi

if [ ! -f .env ]; then
  cat > .env <<ENVFILE
# LLM provider, preencha pelo menos uma chave conforme seu modelo.
# OPENAI_API_KEY=
# ANTHROPIC_API_KEY=
# ZAI_API_KEY=
# GROQ_API_KEY=
# OPENROUTER_API_KEY=

# Embeddings/memória, opcional conforme scripts usados.
# GEMINI_API_KEY=

PG_HOST=localhost
PG_PORT=5432
PG_DBNAME=${DB_NAME}
PG_USER=${DB_USER}
PG_PASSWORD=${PG_PASS}

# Backup privado opcional.
# GITHUB_BACKUP_TOKEN=
# GITHUB_BACKUP_OWNER=
# GITHUB_BACKUP_REPO=
ENVFILE
  chmod 600 .env
fi

CRON_LINE="*/15 * * * * cd ${WS} && /usr/bin/python3 scripts/memory-sync.py >> /tmp/openclaw-memory-sync.log 2>&1"
(crontab -l 2>/dev/null | grep -v 'openclaw-memory-sync\|scripts/memory-sync.py'; echo "$CRON_LINE") | crontab -
openclaw gateway install >/dev/null 2>&1 || true
ok 'Workspace e memória prontos'

log_step '[7/7] Próximos passos'
echo -e "${GREEN}Instalação concluída.${NC}"
echo ""
echo "1. Edite credenciais: nano ${WS}/.env"
echo "2. Configure canais/modelo: openclaw configure"
echo "3. Reinicie: openclaw gateway restart"
echo "4. Treine o agente editando SOUL.md, USER.md, AGENTS.md e MEMORY.md"
