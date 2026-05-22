#!/usr/bin/env python3
"""
Trios Memory Sync v2 — Buffer-based memory pipeline.

Reads daily buffer → classifies with Gemini → embeds → upserts to PostgreSQL → regenerates context .md files.

Usage:
    python3 memory-sync-v2.py [--date YYYY-MM-DD] [--dry-run] [--manual]
"""

import os
import sys
import json
import hashlib
import time
import re
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

import psycopg2
import psycopg2.extras

# ============================================================
# Logging
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('/tmp/memory-sync-v2.log', mode='a'),
    ],
)
log = logging.getLogger('memory-sync-v2')

# ============================================================
# Config
# ============================================================
def load_dotenv():
    env_file = Path(__file__).parent.parent / '.env'
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, value = line.partition('=')
                os.environ.setdefault(key.strip(), value.strip())

load_dotenv()

DB_CONFIG = {
    'host': os.getenv('PG_HOST', 'localhost'),
    'port': int(os.getenv('PG_PORT', '5432')),
    'dbname': os.getenv('PG_DBNAME', 'agent_memory'),
    'user': os.getenv('PG_USER', 'agent'),
    'password': os.environ.get('PG_PASSWORD', ''),
}

GEMINI_API_KEY = os.getenv('GEMINI_API_KEY', '')
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY', '')
CLASSIFY_MODEL = 'gemini-2.0-flash'
EMBEDDING_PROVIDER = os.getenv('MEMORY_EMBEDDING_PROVIDER', 'openai' if OPENAI_API_KEY else 'gemini').lower()
EMBEDDING_MODEL = os.getenv('MEMORY_EMBEDDING_MODEL', 'text-embedding-3-small' if EMBEDDING_PROVIDER == 'openai' else 'gemini-embedding-2')
EMBEDDING_DIM = 1536

WORKSPACE = Path(os.getenv('WORKSPACE', '/root/.openclaw/workspace'))
BUFFER_DIR = WORKSPACE / 'memory' / 'buffer'
CONTEXT_DIR = WORKSPACE / 'memory' / 'context'

RATE_LIMIT_DELAY = 0.5  # 2 req/s


# ============================================================
# Gemini API helpers (urllib only, no requests)
# ============================================================
def _gemini_post(model: str, payload: dict, timeout: int = 60, retries: int = 3) -> dict:
    """POST to Gemini REST API. Returns parsed JSON. Retries on 429."""
    import urllib.request
    import urllib.error

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
    data = json.dumps(payload).encode('utf-8')
    req = urllib.request.Request(url, data=data, headers={'Content-Type': 'application/json'}, method='POST')

    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                wait = (attempt + 1) * 5
                log.warning(f"Rate limited (429), retrying in {wait}s... (attempt {attempt+1}/{retries})")
                time.sleep(wait)
                continue
            raise


def classify_buffer(text: str) -> list[dict]:
    """Send buffer text to Gemini for classification. Returns list of memory dicts."""
    prompt = """Você é um sistema de classificação de memórias pessoais e de negócio. Analise o texto abaixo (buffer de conversa) e extraia TODAS as memórias estruturadas.

Para CADA memória identificada, retorne um JSON com:
- "kind": um de [decision, lesson, insight, fact, pattern, principle, question, pending, project_update, people_update, session_note]
- "category": um de [context, projects, sessions, integrations, pending, feedback]
- "title": título curto e descritivo (até 80 chars)
- "content": conteúdo completo PRESERVANDO todos os dados, nomes, números, valores. Nunca resumir ou omitir informações.
- "domain": área principal (financeiro, clientes, infraestrutura, ia, negócio, pessoal, automação, geral)
- "tags": array de tags relevantes (max 10)

Regras:
- Extraia TODA informação útil. Se o texto menciona um nome, número, data, valor, projeto, decisão,.include tudo.
- Se há múltiplas informações distintas, separe em múltiplas memórias.
- Se não souber classificar, use kind=insight e category=context.
- Nomes de pessoas são people_update.
- Decisões e regras são decision.
- Erros e aprendizados são lesson.
- Tarefas e pendências são pending.
- Atualizações de projetos são project_update.
- Dados factuais são fact.

Retorne APENAS um JSON array válido, sem markdown, sem explicação. Exemplo:
[{"kind":"decision","category":"context","title":"Decisão sobre X","content":"Decidimos que X será Y porque Z","domain":"negócio","tags":["estratégia","vendas"]}]

Texto para análise:"""

    payload = {
        "contents": [{"parts": [{"text": prompt + "\n\n" + text}]}],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 8192,
            "responseMimeType": "application/json",
        },
    }

    try:
        data = _gemini_post(CLASSIFY_MODEL, payload)
        text_response = data['candidates'][0]['content']['parts'][0]['text']
        # Strip markdown code fences if present
        text_response = re.sub(r'^```(?:json)?\s*', '', text_response)
        text_response = re.sub(r'\s*```$', '', text_response)
        memories = json.loads(text_response)
        if isinstance(memories, list):
            return memories
        log.warning(f"Gemini returned non-list: {type(memories)}")
        return []
    except (KeyError, IndexError) as e:
        log.error(f"Gemini classify response parse error: {e}")
        return []
    except json.JSONDecodeError as e:
        log.error(f"Gemini classify JSON parse error: {e}")
        return []
    except Exception as e:
        log.error(f"Gemini classify error: {e}")
        return []


def generate_embedding(text: str) -> list[float] | None:
    """Generate embedding via OpenAI (preferred) or Gemini, always 1536 dimensions."""
    import urllib.request
    import urllib.error

    try:
        truncated = text[:32000]
        if EMBEDDING_PROVIDER == 'openai':
            if not OPENAI_API_KEY:
                log.warning("OPENAI_API_KEY not set, skipping embedding")
                return None
            payload = json.dumps({
                "model": EMBEDDING_MODEL,
                "input": truncated,
                "dimensions": EMBEDDING_DIM,
            }).encode('utf-8')
            req = urllib.request.Request(
                "https://api.openai.com/v1/embeddings",
                data=payload,
                headers={
                    'Content-Type': 'application/json',
                    'Authorization': f'Bearer {OPENAI_API_KEY}',
                },
                method='POST'
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                return data.get('data', [{}])[0].get('embedding')

        if not GEMINI_API_KEY:
            log.warning("GEMINI_API_KEY not set, skipping embedding")
            return None
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL}:embedContent?key={GEMINI_API_KEY}"
        payload = json.dumps({
            "model": f"models/{EMBEDDING_MODEL}",
            "content": {"parts": [{"text": truncated}]},
            "outputDimensionality": EMBEDDING_DIM,
        }).encode('utf-8')
        req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get('embedding', {}).get('values')
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        log.error(f"{EMBEDDING_PROVIDER} embedding HTTP {e.code}: {body}")
        return None
    except Exception as e:
        log.error(f"Embedding error ({EMBEDDING_PROVIDER}): {e}")
        return None


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode('utf-8')).hexdigest()


# ============================================================
# Context file regeneration
# ============================================================
def regenerate_context_files(conn):
    """Regenerate .md context files from PostgreSQL."""
    cur = conn.cursor()

    mappings = [
        {
            'file': CONTEXT_DIR / 'people.md',
            'kind': 'people_update',
            'header': '# People\n',
        },
        {
            'file': CONTEXT_DIR / 'decisions.md',
            'kind': 'decision',
            'header': '# Decisions\n',
        },
        {
            'file': CONTEXT_DIR / 'lessons.md',
            'kind': 'lesson',
            'header': '# Lessons\n',
        },
        {
            'file': CONTEXT_DIR / 'business-context.md',
            'kind': 'fact',
            'header': '# Business Context\n',
        },
    ]

    for m in mappings:
        try:
            cur.execute("""
                SELECT title, content, domain, tags, updated_at
                FROM memory_entries
                WHERE kind = %s AND is_current = TRUE
                ORDER BY updated_at DESC
            """, (m['kind'],))
            rows = cur.fetchall()

            lines = [m['header']]
            for title, content, domain, tags, updated_at in rows:
                lines.append(f"\n## {title}\n")
                if domain:
                    lines.append(f"**Domain:** {domain}\n")
                if tags:
                    lines.append(f"**Tags:** {', '.join(tags)}\n")
                lines.append(f"{content}\n")
                lines.append(f"*Updated: {updated_at.strftime('%Y-%m-%d')}*\n")

            m['file'].write_text('\n'.join(lines), encoding='utf-8')
            log.info(f"Regenerated {m['file'].name} ({len(rows)} entries)")
        except Exception as e:
            log.error(f"Error regenerating {m['file'].name}: {e}")

    # pending.md (separate location)
    pending_file = WORKSPACE / 'memory' / 'pending.md'
    try:
        cur.execute("""
            SELECT title, content, domain, tags, updated_at
            FROM memory_entries
            WHERE kind = 'pending' AND is_current = TRUE
            ORDER BY updated_at DESC
        """)
        rows = cur.fetchall()
        lines = ['# Pending\n']
        for title, content, domain, tags, updated_at in rows:
            lines.append(f"\n## {title}\n")
            lines.append(f"{content}\n")
            lines.append(f"*Updated: {updated_at.strftime('%Y-%m-%d')}*\n")
        pending_file.write_text('\n'.join(lines), encoding='utf-8')
        log.info(f"Regenerated pending.md ({len(rows)} entries)")
    except Exception as e:
        log.error(f"Error regenerating pending.md: {e}")

    cur.close()


# ============================================================
# Main sync pipeline
# ============================================================
def sync_buffer(date_str: str | None = None, dry_run: bool = False, manual: bool = False):
    start_time = time.time()

    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')

    buffer_file = BUFFER_DIR / f"{date_str}.md"

    if not buffer_file.exists():
        log.info(f"No buffer file for {date_str}: {buffer_file}; falling back to file-based memory sync")
        import subprocess
        result = subprocess.run([sys.executable, str(WORKSPACE / 'scripts' / 'memory-sync.py'), '--agent-id', 'main'], cwd=str(WORKSPACE))
        if result.returncode != 0:
            raise RuntimeError(f"file-based memory sync failed with exit code {result.returncode}")
        return

    raw_text = buffer_file.read_text(encoding='utf-8').strip()
    if not raw_text:
        log.info(f"Buffer file {buffer_file} is empty, nothing to do")
        return

    log.info(f"Processing buffer: {buffer_file} ({len(raw_text)} chars)")

    # Step 1: Classify with Gemini
    log.info("Classifying buffer content with Gemini...")
    memories = classify_buffer(raw_text)

    if not memories:
        log.warning("Gemini returned no memories. Saving entire buffer as a single insight.")
        memories = [{
            'kind': 'insight',
            'category': 'context',
            'title': f'Buffer {date_str} (unclassified)',
            'content': raw_text,
            'domain': 'geral',
            'tags': ['buffer', 'unclassified'],
        }]

    log.info(f"Classified {len(memories)} memories")

    if dry_run:
        for i, m in enumerate(memories):
            log.info(f"[DRY {i+1}] {m.get('kind','?')}/{m.get('category','?')}: {m.get('title','?')[:60]}")
        print(json.dumps(memories, indent=2, ensure_ascii=False))
        return

    # Step 2: Connect to PG and upsert
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    entries_synced = 0
    embeddings_generated = 0
    errors = 0

    try:
        for m in memories:
            try:
                kind = m.get('kind', 'insight')
                category = m.get('category', 'context')
                title = m.get('title', 'Untitled')[:200]
                content = m.get('content', '')
                domain = m.get('domain', 'geral')
                tags = m.get('tags', [])

                if not content:
                    log.warning(f"Empty content for '{title}', skipping")
                    continue

                hash_val = content_hash(content)

                # Determine retention
                if kind in ('decision', 'principle', 'fact'):
                    retention = 'permanent'
                elif kind in ('pending',):
                    retention = 'tactical_30d'
                elif kind == 'session_note':
                    retention = 'session_only'
                else:
                    retention = 'permanent'

                expires_at = None
                if retention == 'tactical_30d':
                    expires_at = datetime.now(timezone.utc) + timedelta(days=30)

                # Upsert via stored function
                cur.execute("""
                    SELECT upsert_memory(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    kind, category, title, content, domain, tags,
                    f"buffer/{date_str}.md", 'main', None, retention, expires_at, hash_val
                ))
                entry_id = cur.fetchone()[0]

                # Generate embedding
                embed_text = f"{title}\n\n{content}"
                embedding = generate_embedding(embed_text)
                if embedding:
                    embedding_str = f"[{','.join(str(f) for f in embedding)}]"
                    cur.execute("UPDATE memory_entries SET embedding = %s::vector WHERE id = %s", (embedding_str, entry_id))
                    embeddings_generated += 1
                    time.sleep(RATE_LIMIT_DELAY)

                entries_synced += 1
                log.info(f"Synced [{kind}] {title[:60]}")

            except Exception as e:
                log.error(f"Error processing memory '{m.get('title', '?')}': {e}")
                conn.rollback()
                conn.autocommit = False
                errors += 1

        # Step 3: Cleanup expired
        try:
            cur.execute("SELECT cleanup_expired()")
            expired = cur.fetchone()[0]
            if expired > 0:
                log.info(f"Cleaned up {expired} expired memories")
        except Exception as e:
            log.error(f"Cleanup error: {e}")

        # Step 4: Clear buffer
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
        buffer_file.write_text(f"# Buffer {date_str}\n# Processed at {timestamp}\n# Entries synced: {entries_synced}\n\n", encoding='utf-8')
        log.info(f"Buffer cleared")

        # Step 5: Regenerate context files
        regenerate_context_files(conn)

        # Step 6: Log sync
        duration_ms = int((time.time() - start_time) * 1000)
        sync_type = 'manual' if manual else 'periodic_15min'
        cur.execute("""
            INSERT INTO memory_sync_log (sync_type, agent_id, entries_synced, embeddings_generated, errors, duration_ms, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (sync_type, 'main', entries_synced, embeddings_generated, errors, duration_ms,
              json.dumps({'buffer_date': date_str, 'buffer_chars': len(raw_text)})))

        conn.commit()
        log.info(f"Sync complete: {entries_synced} entries, {embeddings_generated} embeddings, {errors} errors, {duration_ms}ms")

    except Exception as e:
        conn.rollback()
        log.error(f"Fatal sync error: {e}")
        raise
    finally:
        cur.close()
        conn.close()


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(description='Trios Memory Sync v2 — Buffer Pipeline')
    parser.add_argument('--date', default=None, help='Buffer date YYYY-MM-DD (default: today)')
    parser.add_argument('--dry-run', action='store_true', help='Classify only, no DB writes')
    parser.add_argument('--manual', action='store_true', help='Mark sync as manual in log')
    args = parser.parse_args()

    log.info(f"Starting v2 sync (date={args.date or 'today'}, dry_run={args.dry_run})")
    sync_buffer(date_str=args.date, dry_run=args.dry_run, manual=args.manual)


if __name__ == '__main__':
    main()
