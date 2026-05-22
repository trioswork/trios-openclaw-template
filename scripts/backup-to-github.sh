#!/usr/bin/env bash
# Generic safe GitHub push helper. Requires GITHUB_BACKUP_TOKEN/OWNER/REPO in .env.
set -Eeuo pipefail
umask 077
ROOT="${ROOT:-$PWD}"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
BRANCH="${BRANCH:-main}"
log(){ printf '[%s] %s\n' "$(date)" "$*"; }
fail(){ log "ERROR: $*"; exit 1; }
[[ -f "$ENV_FILE" ]] || fail ".env not found"
set +x; set -a; source "$ENV_FILE"; set +a
TOKEN="${GITHUB_BACKUP_TOKEN:-}"; OWNER="${GITHUB_BACKUP_OWNER:-}"; REPO="${GITHUB_BACKUP_REPO:-}"
[[ -n "$TOKEN" && -n "$OWNER" && -n "$REPO" ]] || fail "missing GitHub backup env vars"
export TOKEN GIT_TERMINAL_PROMPT=0
ASKPASS_FILE=$(mktemp /tmp/github-askpass.XXXXXX)
cat > "$ASKPASS_FILE" <<'ASKPASS'
#!/usr/bin/env bash
case "$1" in
  *Username*) printf '%s\n' "x-access-token" ;;
  *Password*) printf '%s\n' "$TOKEN" ;;
  *) printf '\n' ;;
esac
ASKPASS
chmod 700 "$ASKPASS_FILE"
export GIT_ASKPASS="$ASKPASS_FILE"
trap 'rm -f "${ASKPASS_FILE:-}"' EXIT
cd "$ROOT"
git remote set-url origin "https://github.com/${OWNER}/${REPO}.git"
git add -A
git commit -m "backup: $(date +%Y-%m-%d)" --allow-empty
git push origin "$BRANCH"
