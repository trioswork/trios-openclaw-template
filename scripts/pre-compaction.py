#!/usr/bin/env python3
"""
Trios Pre-Compaction Extractor — Extrai memórias de conversas antes da compactação.

Recebe texto de conversa, identifica memórias potenciais e salva no PostgreSQL.

Uso:
    echo "texto da conversa" | python3 pre-compaction.py [--agent-id main] [--session-id xxx]
    python3 pre-compaction.py --file conversa.txt [--agent-id main]
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

# Configuração
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('pre-compaction')

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
EMBEDDING_MODEL = 'gemini-embedding-2'
EMBEDDING_DIM = 1536

# ============================================================
# Padrões de detecção
# ============================================================

DECISION_PATTERNS = [
    r'(?i)(?:decidimos|definimos|vamos\s+usar|optamos\s+por|escolhemos)',
    r'(?i)(?:a\s+decisão\s+é|ficou\s+decidido|resolvemos)',
    r'(?i)(?:regra|política|protocolo)\s*[:=]',
    r'(?i)nunca\s+(?:faça|use|execute|mande)',
    r'(?i)sempre\s+(?:faça|use|execute|mande)',
]

LESSON_PATTERNS = [
    r'(?i)(?:lição|aprendizado|aprendi\s+que)',
    r'(?i)(?:não\s+repetir|erro\s+que|deu\s+errado)',
    r'(?i)(?:cuidado\s+com|atenção\s+na\s+hora)',
    r'(?i)(?:o\s+que\s+funcionou|o\s+que\s+não\s+funcionou)',
]

PEOPLE_PATTERNS = [
    r'(?i)(?:cliente|parceiro|contato|equipe)\s*[:=]',
    r'(?i)(?:nome|email|telefone|whatsapp)\s*[:=]',
    r'(?i)(?:[A-Z][a-z]+\s+[A-Z][a-z]+)\s*(?:é|trabalha|morava)',
]

PROJECT_PATTERNS = [
    r'(?i)(?:projeto|sprint|milestone|entregável)\s*[:=]',
    r'(?i)(?:prazo|deadline|entrega)\s*(?:dia|em|para)\s*\d',
    r'(?i)(?:status|progresso)\s*(?:do|da)\s*projeto',
]

PENDING_PATTERNS = [
    r'(?i)(?:pendente|aguardando|falta|fazer|próximo\s+passo)',
    r'(?i)(?:TODO|FIXME|ACTION|TASK)\s*[:=]',
    r'(?i)(?:precisa|necessário)\s+(?:fazer|verificar|enviar)',
]

INSIGHT_PATTERNS = [
    r'(?i)(?:percebi|notei|importante|interessante)',
    r'(?i)(?:padrão|tendência|oportunidade)',
    r'(?i)(?:insight|revelação|descoberta)',
]


def classify_segment(text: str) -> tuple[str, float]:
    """Classifica um segmento de texto e retorna (kind, confidence)."""
    scores = {
        'decision': sum(1 for p in DECISION_PATTERNS if re.search(p, text)),
        'lesson': sum(1 for p in LESSON_PATTERNS if re.search(p, text)),
        'people_update': sum(1 for p in PEOPLE_PATTERNS if re.search(p, text)),
        'project_update': sum(1 for p in PROJECT_PATTERNS if re.search(p, text)),
        'pending': sum(1 for p in PENDING_PATTERNS if re.search(p, text)),
        'insight': sum(1 for p in INSIGHT_PATTERNS if re.search(p, text)),
    }

    if not any(scores.values()):
        return 'insight', 0.1

    best = max(scores, key=scores.get)
    total = sum(scores.values())
    confidence = scores[best] / max(total, 1)

    return best, round(confidence, 2)


def extract_segments(conversation: str) -> list[dict]:
    """Extrai segmentos memoráveis da conversa."""
    segments = []

    # Divide por parágrafos ou blocos
    paragraphs = re.split(r'\n\s*\n', conversation)

    for para in paragraphs:
        para = para.strip()
        if len(para) < 30:  # Muito curto pra ser memorável
            continue

        # Pula saudações e filler
        if re.match(r'^(?:olá|oi|obrigado|valeu|ok|beleza|tchau|até)\s*[!.]?$', para, re.IGNORECASE):
            continue

        kind, confidence = classify_segment(para)

        if confidence >= 0.3:
            # Extrai título (primeira linha ou resumo)
            first_line = para.split('\n')[0][:100]
            title = re.sub(r'^[#*\->\s]+', '', first_line).strip()
            if not title:
                title = f"{kind.title()} — {datetime.now().strftime('%Y-%m-%d %H:%M')}"

            segments.append({
                'title': title,
                'content': para,
                'kind': kind,
                'confidence': confidence,
            })

    return segments


def get_domain(text: str) -> str:
    """Detecta domínio."""
    domain_keywords = {
        'financeiro': ['faturamento', 'receita', 'custo', 'pagamento', 'preço'],
        'clientes': ['cliente', 'contato', 'proposta', 'contrato', 'reunião'],
        'infraestrutura': ['server', 'vps', 'deploy', 'postgres', 'docker'],
        'ia': ['openclaw', 'claude', 'gpt', 'agente', 'embedding'],
        'negócio': ['example company', 'business', 'pipeline', 'vendas'],
        'pessoal': ['família', 'casa', 'viagem'],
        'automação': ['automação', 'workflow', 'integração', 'api'],
    }
    text_lower = text.lower()
    scores = {d: sum(1 for kw in kws if kw in text_lower) for d, kws in domain_keywords.items()}
    if any(scores.values()):
        return max(scores, key=scores.get)
    return 'geral'


def generate_embedding(text: str) -> list[float] | None:
    """Gera embedding via Gemini."""
    if not GEMINI_API_KEY:
        return None
    try:
        import urllib.request
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL}:embedContent?key={GEMINI_API_KEY}"
        payload = json.dumps({
            "model": f"models/{EMBEDDING_MODEL}",
            "content": {"parts": [{"text": text[:32000]}]},
            "outputDimensionality": EMBEDDING_DIM,
        }).encode('utf-8')

        req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            return data.get('embedding', {}).get('values')
    except Exception as e:
        log.warning(f"Embedding falhou: {e}")
        return None


def content_hash(text: str) -> str:
    return hashlib.sha256(text.strip().encode('utf-8')).hexdigest()


def extract_and_save(conversation: str, agent_id: str = 'main', session_id: str = None, dry_run: bool = False) -> dict:
    """Extrai memórias da conversa e salva no banco."""
    start_time = time.time()
    segments = extract_segments(conversation)

    if not segments:
        log.info("Nenhum segmento memorável encontrado.")
        return {'extracted': 0, 'saved': 0, 'segments': []}

    log.info(f"Encontrados {len(segments)} segmentos memoráveis")

    if dry_run:
        for seg in segments:
            log.info(f"[DRY] {seg['kind']} ({seg['confidence']}): {seg['title'][:60]}")
        return {'extracted': len(segments), 'saved': 0, 'segments': segments}

    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = False
    cur = conn.cursor()

    saved = 0
    results = []

    try:
        for seg in segments:
            hash_val = content_hash(seg['content'])

            # Duplicata?
            cur.execute("""
                SELECT id FROM memory_entries
                WHERE content_hash = %s AND is_current = TRUE
            """, (hash_val,))
            if cur.fetchone():
                continue

            domain = get_domain(seg['content'])
            kind = seg['kind']
            retention = 'permanent' if kind in ('decision', 'lesson') else 'tactical_30d'
            expires_at = datetime.now(timezone.utc) + timedelta(days=30) if retention == 'tactical_30d' else None
            tags = [domain, kind, 'pre-compaction']

            # Gera embedding
            embedding = generate_embedding(f"{seg['title']}\n\n{seg['content']}")
            embedding_str = None
            if embedding:
                if len(embedding) != EMBEDDING_DIM:
                    log.warning(f"Embedding ignorado: dimensão {len(embedding)} != {EMBEDDING_DIM}")
                    embedding = None
                else:
                    embedding_str = f"[{','.join(str(f) for f in embedding)}]"

            # Salva
            cur.execute("""
                SELECT upsert_memory(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                kind, 'context', seg['title'], seg['content'], domain, tags,
                'pre-compaction', agent_id, session_id, retention, expires_at, hash_val
            ))

            new_id = cur.fetchone()[0]

            if embedding_str:
                cur.execute("UPDATE memory_entries SET embedding = %s::vector WHERE id = %s", (embedding_str, new_id))

            saved += 1
            results.append({'id': str(new_id), 'kind': kind, 'title': seg['title'][:80]})

            if embedding:
                time.sleep(0.5)

        # Log
        duration_ms = int((time.time() - start_time) * 1000)
        cur.execute("""
            INSERT INTO memory_sync_log (sync_type, agent_id, entries_synced, embeddings_generated, errors, duration_ms, details)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, ('pre_compaction', agent_id, saved, saved, 0, duration_ms, json.dumps({'segments': len(segments)})))

        conn.commit()
        log.info(f"Pré-compactação: {len(segments)} extraídos, {saved} salvos")

    except Exception as e:
        conn.rollback()
        log.error(f"Erro: {e}")
        raise
    finally:
        cur.close()
        conn.close()

    return {'extracted': len(segments), 'saved': saved, 'segments': results}


def main():
    parser = argparse.ArgumentParser(description='Trios Pre-Compaction Extractor')
    parser.add_argument('--agent-id', default='main', help='ID do agente')
    parser.add_argument('--session-id', default=None, help='ID da sessão')
    parser.add_argument('--file', default=None, help='Arquivo com texto da conversa')
    parser.add_argument('--dry-run', action='store_true', help='Apenas mostra o que extrairia')
    args = parser.parse_args()

    if args.file:
        conversation = Path(args.file).read_text(encoding='utf-8')
    else:
        conversation = sys.stdin.read()

    if not conversation.strip():
        log.error("Nenhum texto fornecido.")
        sys.exit(1)

    result = extract_and_save(
        conversation=conversation,
        agent_id=args.agent_id,
        session_id=args.session_id,
        dry_run=args.dry_run,
    )

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
