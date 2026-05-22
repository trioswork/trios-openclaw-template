#!/usr/bin/env python3
"""
Trios Memory Sync — Sincroniza arquivos de memória do workspace com PostgreSQL.

Lê todos os arquivos .md do workspace/memory/, extrai seções como memórias
individuais, classifica, gera embeddings via Gemini e insere/atualiza no banco.

Uso:
    python3 memory-sync.py [--agent-id main] [--full] [--dry-run]
"""

import os
import sys
import json
import hashlib
import re
import time
import argparse
import logging
from pathlib import Path
from datetime import datetime, timezone, timedelta

import psycopg2
import psycopg2.extras

# Configuração de logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('memory-sync')

# ============================================================
# Configuração
# ============================================================
# Carrega .env se existir
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
EMBEDDING_PROVIDER = os.getenv('MEMORY_EMBEDDING_PROVIDER', 'openai' if OPENAI_API_KEY else 'gemini').lower()
EMBEDDING_MODEL = os.getenv('MEMORY_EMBEDDING_MODEL', 'text-embedding-3-small' if EMBEDDING_PROVIDER == 'openai' else 'gemini-embedding-2')
EMBEDDING_DIM = 1536

WORKSPACE = Path(os.getenv('WORKSPACE', '/root/.openclaw/workspace'))
MEMORY_DIR = WORKSPACE / 'memory'

# Mapeamento de arquivo → category
CATEGORY_MAP = {
    'context': 'context',
    'projects': 'projects',
    'sessions': 'sessions',
    'integrations': 'integrations',
    'feedback': 'feedback',
    'pending': 'pending',
}

# Mapeamento de arquivo → kind (quando não dá pra inferir do conteúdo)
KIND_FROM_FILENAME = {
    'decisions.md': 'decision',
    'lessons.md': 'lesson',
    'people.md': 'people_update',
    'business-context.md': 'fact',
    'pending.md': 'pending',
    'ferramentas.md': 'fact',
}

# Padrões para detectar kind no conteúdo
KIND_PATTERNS = {
    'decision': [
        r'(?i)decisão', r'(?i)decidimos', r'(?i)definimos',
        r'(?i)regra.*permanente', r'(?i)nunca\s+',
    ],
    'lesson': [
        r'(?i)lição', r'(?i)aprendizado', r'(?i)erro\s+que',
        r'(?i)não\s+repetir', r'(?i)lição\s+aprendida',
    ],
    'insight': [
        r'(?i)insight', r'(?i)percepção', r'(?i)notei\s+que',
        r'(?i)padrão', r'(?i)importante',
    ],
    'pending': [
        r'(?i)pendente', r'(?i)aguardando', r'(?i)TODO',
        r'(?i)fazer', r'(?i)próximo\s+passo',
    ],
    'people_update': [
        r'(?i)contato', r'(?i)parceiro', r'(?i)equipe',
        r'(?i)cliente.*:', r'(?i)essoa',
    ],
    'project_update': [
        r'(?i)projeto', r'(?i)sprint', r'(?i)milestone',
        r'(?i)entregável', r'(?i)deadline',
    ],
}

# Domínios conhecidos
DOMAIN_KEYWORDS = {
    'financeiro': ['faturamento', 'receita', 'custo', 'pagamento', 'preço', 'mensalidade'],
    'clientes': ['cliente', 'contato', 'proposta', 'contrato', 'reunião'],
    'infraestrutura': ['server', 'vps', 'deploy', 'postgres', 'docker', 'n8n'],
    'ia': ['openclaw', 'claude', 'gpt', 'agente', 'embedding', 'memoria'],
    'negócio': ['example company', 'business', 'pipeline', 'vendas', 'marketing'],
    'pessoal': ['família', 'casa', 'viagem', 'descanso'],
    'automação': ['automação', 'workflow', 'integração', 'api', 'webhook'],
}


def get_domain(text: str) -> str:
    """Detecta domínio baseado em keywords no texto."""
    text_lower = text.lower()
    scores = {}
    for domain, keywords in DOMAIN_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text_lower)
        if score > 0:
            scores[domain] = score
    if scores:
        return max(scores, key=scores.get)
    return 'geral'


def get_kind(title: str, content: str, source_file: str) -> str:
    """Detecta kind baseado no arquivo de origem e conteúdo."""
    filename = Path(source_file).name
    if filename in KIND_FROM_FILENAME:
        return KIND_FROM_FILENAME[filename]

    # Session notes
    if re.match(r'\d{4}-\d{2}-\d{2}', filename):
        return 'session_note'

    # Tenta detectar pelo conteúdo
    text = f"{title} {content}"
    scores = {}
    for kind, patterns in KIND_PATTERNS.items():
        score = sum(1 for p in patterns if re.search(p, text))
        if score > 0:
            scores[kind] = score
    if scores:
        return max(scores, key=scores.get)

    return 'insight'


def get_retention(kind: str, content: str) -> str:
    """Determina retenção baseado no kind e marcadores no conteúdo."""
    if kind in ('decision', 'principle', 'fact'):
        return 'permanent'
    if '🔒' in content or 'permanente' in content.lower():
        return 'permanent'
    if '⏳' in content or 'tático' in content.lower():
        return 'tactical_30d'
    if kind == 'session_note':
        return 'session_only'
    if kind == 'pending':
        return 'tactical_30d'
    return 'permanent'


def get_expires_at(retention: str) -> datetime | None:
    """Calcula data de expiração."""
    if retention == 'tactical_30d':
        return datetime.now(timezone.utc) + timedelta(days=30)
    return None


def content_hash(text: str) -> str:
    """Gera hash SHA-256 do conteúdo."""
    return hashlib.sha256(text.strip().encode('utf-8')).hexdigest()


def extract_tags(title: str, content: str, domain: str) -> list[str]:
    """Extrai tags do título e conteúdo."""
    tags = set()
    text = f"{title} {content}".lower()

    # Tags do domínio
    if domain and domain != 'geral':
        tags.add(domain)

    # Tags comuns
    tag_patterns = {
        'automação': ['automação', 'automation', 'workflow', 'n8n'],
        'ia': ['ia', 'ai', 'openai', 'claude', 'gpt', 'gemini', 'llm'],
        'marketing': ['marketing', 'conteúdo', 'instagram', 'linkedin'],
        'vendas': ['vendas', 'pipeline', 'comercial', 'proposta'],
        'operações': ['operações', 'processo', 'processos', 'operations'],
        'ferramentas': ['ferramenta', 'tool', 'api', 'integração'],
        'família': ['família', 'familymember1', 'familymember2', 'partner'],
        'negócio': ['negócio', 'empresa', 'trios', 'business'],
    }

    for tag, keywords in tag_patterns.items():
        if any(kw in text for kw in keywords):
            tags.add(tag)

    return sorted(list(tags))[:10]  # Max 10 tags


def extract_sections(filepath: Path) -> list[dict]:
    """Extrai seções de um arquivo markdown como memórias individuais."""
    try:
        text = filepath.read_text(encoding='utf-8')
    except Exception as e:
        log.warning(f"Erro lendo {filepath}: {e}")
        return []

    if not text.strip():
        return []

    rel_path = str(filepath.relative_to(WORKSPACE))
    filename = filepath.name

    # Para arquivos pequenos (< 2KB), trata como uma única memória
    if len(text) < 2000:
        return [{
            'title': filepath.stem.replace('-', ' ').replace('_', ' ').title(),
            'content': text.strip(),
            'source_file': rel_path,
        }]

    # Para arquivos maiores, divide por headers
    sections = []
    current_title = filepath.stem.replace('-', ' ').replace('_', ' ').title()
    current_lines = []

    for line in text.split('\n'):
        if re.match(r'^#{1,3}\s+', line):
            # Salva seção anterior
            if current_lines:
                content = '\n'.join(current_lines).strip()
                if content and len(content) > 20:
                    sections.append({
                        'title': current_title,
                        'content': content,
                        'source_file': rel_path,
                    })
            current_title = re.sub(r'^#{1,3}\s+', '', line).strip()
            current_lines = []
        else:
            current_lines.append(line)

    # Última seção
    if current_lines:
        content = '\n'.join(current_lines).strip()
        if content and len(content) > 20:
            sections.append({
                'title': current_title,
                'content': content,
                'source_file': rel_path,
            })

    # Se não encontrou seções, retorna o arquivo inteiro
    if not sections:
        sections.append({
            'title': current_title,
            'content': text.strip(),
            'source_file': rel_path,
        })

    return sections


def generate_embedding(text: str) -> list[float] | None:
    """Gera embedding via OpenAI (preferencial) ou Gemini, sempre com 1536 dimensões."""
    try:
        import urllib.request
        import urllib.error

        truncated = text[:32000]

        if EMBEDDING_PROVIDER == 'openai':
            if not OPENAI_API_KEY:
                log.warning("OPENAI_API_KEY não configurada. Pulando embedding.")
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
            log.warning("GEMINI_API_KEY não configurada. Pulando embedding.")
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
        log.error(f"{EMBEDDING_PROVIDER} embedding HTTP {e.code}: {e.read().decode()[:200]}")
        return None
    except Exception as e:
        log.error(f"Erro gerando embedding ({EMBEDDING_PROVIDER}): {e}")
        return None


def sync_memories(agent_id: str = 'main', full: bool = False, dry_run: bool = False):
    """Sincroniza todos os arquivos de memória com o PostgreSQL."""
    start_time = time.time()

    if not MEMORY_DIR.exists():
        log.error(f"Diretório de memória não encontrado: {MEMORY_DIR}")
        return

    # Coleta todos os arquivos .md
    md_files = sorted(MEMORY_DIR.rglob('*.md'))
    log.info(f"Encontrados {len(md_files)} arquivos .md em {MEMORY_DIR}")

    # Conecta ao banco
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    entries_synced = 0
    embeddings_generated = 0
    errors = 0

    try:
        for filepath in md_files:
            try:
                sections = extract_sections(filepath)
                rel_path = str(filepath.relative_to(WORKSPACE))

                for section in sections:
                    title = section['title']
                    content = section['content']
                    source_file = section['source_file']
                    hash_val = content_hash(content)

                    # Classificação
                    kind = get_kind(title, content, source_file)
                    category = CATEGORY_MAP.get(filepath.parent.name, 'context')
                    domain = get_domain(f"{title} {content}")
                    tags = extract_tags(title, content, domain)
                    retention = get_retention(kind, content)
                    expires_at = get_expires_at(retention)

                    if dry_run:
                        log.info(f"[DRY] {kind}/{category}: {title[:50]}... ({source_file})")
                        entries_synced += 1
                        continue

                    # Verifica se já existe (por hash)
                    cur.execute("""
                        SELECT id FROM memory_entries
                        WHERE content_hash = %s AND agent_id = %s AND is_current = TRUE
                    """, (hash_val, agent_id))

                    if cur.fetchone():
                        continue  # Já existe, pula

                    # Gera embedding
                    embedding = generate_embedding(f"{title}\n\n{content}")
                    embedding_str = None
                    if embedding:
                        embedding_str = f"[{','.join(str(f) for f in embedding)}]"
                        embeddings_generated += 1

                    # Insere via upsert
                    cur.execute("""
                        SELECT upsert_memory(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """, (
                        kind, category, title, content, domain, tags,
                        source_file, agent_id, None, retention, expires_at, hash_val
                    ))

                    # Atualiza embedding se gerado
                    if embedding_str:
                        new_id = cur.fetchone()[0]
                        cur.execute("""
                            UPDATE memory_entries SET embedding = %s::vector WHERE id = %s
                        """, (embedding_str, new_id))

                    entries_synced += 1

                    # Rate limit: 2 req/s pro Gemini
                    if embedding:
                        time.sleep(0.5)

            except Exception as e:
                log.error(f"Erro processando {filepath}: {e}")
                errors += 1

        # Log da sincronização
        if not dry_run:
            duration_ms = int((time.time() - start_time) * 1000)
            cur.execute("""
                INSERT INTO memory_sync_log (sync_type, agent_id, entries_synced, embeddings_generated, errors, duration_ms)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, ('manual' if full else 'periodic_15min', agent_id, entries_synced, embeddings_generated, errors, duration_ms))

            conn.commit()

        log.info(f"Sync concluído: {entries_synced} entradas, {embeddings_generated} embeddings, {errors} erros")

    except Exception as e:
        conn.rollback()
        log.error(f"Erro fatal na sincronização: {e}")
        raise
    finally:
        cur.close()
        conn.close()


def main():
    parser = argparse.ArgumentParser(description='Trios Memory Sync')
    parser.add_argument('--agent-id', default='main', help='ID do agente (default: main)')
    parser.add_argument('--full', action='store_true', help='Sincronização completa (ignora cache)')
    parser.add_argument('--dry-run', action='store_true', help='Apenas mostra o que faria')
    args = parser.parse_args()

    log.info(f"Iniciando sync (agent={args.agent_id}, full={args.full}, dry_run={args.dry_run})")
    sync_memories(agent_id=args.agent_id, full=args.full, dry_run=args.dry_run)


if __name__ == '__main__':
    main()
