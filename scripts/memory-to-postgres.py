#!/usr/bin/env python3
"""
Memory Sync: Lê arquivos de memória do workspace e salva no PostgreSQL com pgvector.
Rodar a cada 15 minutos via cron.
"""

import os
import re
import hashlib
import json
import psycopg2
from datetime import datetime, timezone

# Config
WORKSPACE = os.path.expanduser("~/.openclaw/workspace")

def load_env_file(path):
    if not os.path.exists(path):
        return
    with open(path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

load_env_file(os.path.join(WORKSPACE, '.env'))

DB_CONFIG = {
    "host": "localhost",
    "port": 5432,
    "dbname": "agent_memory",
    "user": "trios",
    "password": os.environ['PG_PASSWORD']
}

# Arquivos de memória pra sincronizar
MEMORY_FILES = [
    "MEMORY.md",
    "memory/context/decisions.md",
    "memory/context/lessons.md",
    "memory/context/people.md",
    "memory/context/business-context.md",
    "memory/pending.md",
    "memory/integrations/ferramentas.md",
]

# Projetos
PROJECTS_DIR = os.path.join(WORKSPACE, "memory/projects")

def get_db():
    return psycopg2.connect(**DB_CONFIG)

def init_db():
    """Verifica conexão com o banco."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM memory_entries LIMIT 1")
    cur.close()
    conn.close()

def extract_sections(content, source_file):
    """Extrai seções de um arquivo markdown como memórias individuais."""
    memories = []
    current_section = None
    current_content = []
    
    for line in content.split('\n'):
        # Detecta headers (## ou ###)
        header_match = re.match(r'^(#{1,3})\s+(.+)', line)
        if header_match:
            # Salva seção anterior
            if current_section and current_content:
                text = '\n'.join(current_content).strip()
                if text and len(text) > 20:  # Ignora seções muito pequenas
                    memories.append({
                        'title': current_section,
                        'content': text,
                        'source': source_file
                    })
            current_section = header_match.group(2).strip()
            current_content = []
        else:
            current_content.append(line)
    
    # Última seção
    if current_section and current_content:
        text = '\n'.join(current_content).strip()
        if text and len(text) > 20:
            memories.append({
                'title': current_section,
                'content': text,
                'source': source_file
            })
    
    return memories

def classify_kind(title, content, source):
    """Classifica o tipo de memória baseado no contexto."""
    title_lower = title.lower()
    content_lower = content.lower()
    source_lower = source.lower()
    
    if 'decision' in source_lower or 'decisão' in title_lower:
        return 'decision'
    if 'lesson' in source_lower or 'lição' in title_lower or 'erro' in title_lower:
        return 'lesson'
    if 'people' in source_lower or 'cliente' in title_lower or 'contato' in title_lower:
        return 'people_update'
    if 'business' in source_lower or 'receita' in title_lower or 'custo' in title_lower:
        return 'fact'
    if 'pendenc' in title_lower or 'pending' in source_lower:
        return 'pending'
    if 'meta' in title_lower or 'objetivo' in title_lower:
        return 'principle'
    if 'como' in title_lower or 'processo' in title_lower:
        return 'pattern'
    if 'projeto' in title_lower or 'project' in source_lower:
        return 'project_update'
    return 'insight'

def extract_domain(title, content):
    """Extrai domínio da memória."""
    text = (title + ' ' + content).lower()
    
    domains = {
        'financeiro': ['receita', 'custo', 'faturamento', 'pagamento', 'dinheiro', 'valor', 'mei', 'das'],
        'clientes': ['cliente', 'contrato', 'proposta', 'clientname', 'clienta', 'clientb', 'clientname2'],
        'infraestrutura': ['vps', 'postgres', 'supabase', 'vercel', 'n8n', 'docker'],
        'automação': ['automação', 'n8n', 'workflow', 'integração', 'webhook'],
        'ia': ['llm', 'openclaw', 'gemini', 'gpt', 'claude', 'modelo', 'embedding'],
        'negócio': ['example company', 'business', 'operations', 'pipeline', 'prospecção'],
        'pessoal': ['família', 'owner', 'partner', 'familymember1', 'familymember2'],
        'equipe': ['colaborador', 'joseilton', 'equipe'],
    }
    
    found = []
    for domain, keywords in domains.items():
        if any(kw in text for kw in keywords):
            found.append(domain)
    
    return found[0] if found else 'geral'

def extract_tags(title, content):
    """Extrai tags relevantes."""
    text = (title + ' ' + content).lower()
    tags = []
    
    tag_patterns = [
        'mei', 'das', 'supabase', 'postgres', 'pgvector', 'n8n', 'vercel',
        'openclaw', 'telegram', 'whatsapp', 'clienta', 'clientb', 'clientname',
        'clientname2', 'sdoperations', 'automação', 'ia', 'llm', 'pipeline',
        'proposta', 'contrato', 'pagamento', 'aluguel', 'energia',
        'google', 'workspace', 'firecrawl', 'evolution',
    ]
    
    for tag in tag_patterns:
        if tag in text:
            tags.append(tag)
    
    return tags

def content_hash(text):
    """Hash do conteúdo pra detectar mudanças."""
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def generate_embedding(text):
    """Gera embedding usando Gemini API."""
    import urllib.request
    api_key = os.environ.get('GEMINI_API_KEY')
    if not api_key:
        return None
    url = f'https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:embedContent?key={api_key}'
    payload = json.dumps({
        'model': 'models/gemini-embedding-2',
        'content': {'parts': [{'text': text[:2000]}]},  # Limita a 2000 chars
        'outputDimensionality': 1536  # Compatível com pgvector vector(1536)
    }).encode('utf-8')
    
    try:
        req = urllib.request.Request(url, data=payload, headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            result = json.loads(resp.read())
            return result.get('embedding', {}).get('values', [])
    except Exception as e:
        return None

def classify_category(source):
    """Classifica a categoria baseado no arquivo de origem."""
    s = source.lower()
    if 'context' in s:
        return 'context'
    if 'projects' in s:
        return 'projects'
    if 'sessions' in s:
        return 'sessions'
    if 'integrations' in s:
        return 'integrations'
    if 'pending' in s:
        return 'pending'
    if 'feedback' in s:
        return 'feedback'
    return 'context'

def sync_memories():
    """Sincroniza memórias dos arquivos pro PostgreSQL usando upsert_memory."""
    conn = get_db()
    cur = conn.cursor()
    
    stats = {'new': 0, 'updated': 0, 'unchanged': 0, 'errors': 0, 'embeddings': 0}
    
    # Coleta todos os arquivos de memória
    all_files = MEMORY_FILES.copy()
    
    # Adiciona projetos
    if os.path.exists(PROJECTS_DIR):
        for f in os.listdir(PROJECTS_DIR):
            if f.endswith('.md'):
                all_files.append(f"memory/projects/{f}")
    
    # Adiciona sessões recentes (últimos 3 dias)
    sessions_dir = os.path.join(WORKSPACE, "memory/sessions")
    if os.path.exists(sessions_dir):
        for f in sorted(os.listdir(sessions_dir), reverse=True)[:3]:
            if f.endswith('.md'):
                all_files.append(f"memory/sessions/{f}")
    
    for rel_path in all_files:
        filepath = os.path.join(WORKSPACE, rel_path)
        if not os.path.exists(filepath):
            continue
        
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()
        except Exception as e:
            print(f"  Erro lendo {rel_path}: {e}")
            stats['errors'] += 1
            continue
        
        sections = extract_sections(content, rel_path)
        
        for section in sections:
            h = content_hash(section['content'])
            
            # Verifica se já existe com mesmo hash (sem mudança)
            cur.execute(
                """SELECT id FROM memory_entries 
                   WHERE title = %s AND source_file = %s AND content_hash = %s AND is_current = TRUE""",
                (section['title'], rel_path, h)
            )
            if cur.fetchone():
                stats['unchanged'] += 1
                continue
            
            kind = classify_kind(section['title'], section['content'], section['source'])
            category = classify_category(section['source'])
            domain = extract_domain(section['title'], section['content'])
            tags = extract_tags(section['title'], section['content'])
            
            # Gera embedding
            embedding = generate_embedding(section['content'])
            if embedding:
                stats['embeddings'] += 1
            
            try:
                # Usa upsert_memory pra inserir/atualizar com versionamento
                cur.execute("""
                    SELECT upsert_memory(
                        p_kind := %s,
                        p_category := %s,
                        p_title := %s,
                        p_content := %s,
                        p_domain := %s,
                        p_tags := %s,
                        p_source_file := %s,
                        p_agent_id := 'main',
                        p_retention := 'permanent',
                        p_content_hash := %s
                    )
                """, (kind, category, section['title'], section['content'],
                      domain, tags, rel_path, h))
                result_id = cur.fetchone()[0]
                
                # Atualiza embedding se gerado
                if embedding:
                    cur.execute(
                        "UPDATE memory_entries SET embedding = %s WHERE id = %s",
                        (embedding, result_id)
                    )
                
                # Verifica se era existente (updated) ou novo
                cur.execute(
                    "SELECT COUNT(*) FROM memory_entries WHERE title = %s AND source_file = %s AND is_current = FALSE",
                    (section['title'], rel_path)
                )
                had_previous = cur.fetchone()[0] > 0
                if had_previous:
                    stats['updated'] += 1
                else:
                    stats['new'] += 1
            except Exception as e:
                print(f"  Erro salvando '{section['title']}': {e}")
                stats['errors'] += 1
                conn.rollback()
                conn = get_db()
                cur = conn.cursor()
    
    # Log da sincronização
    try:
        cur.execute("""
            INSERT INTO memory_sync_log (sync_type, agent_id, entries_synced, embeddings_generated, errors, details)
            VALUES ('periodic_15min', 'main', %s, %s, %s, %s)
        """, (stats['new'] + stats['updated'], stats['embeddings'], stats['errors'],
              json.dumps(stats)))
    except Exception:
        pass
    
    conn.commit()
    cur.close()
    conn.close()
    
    return stats

def save_sync_log(stats):
    """Salva log da última sincronização."""
    log_path = os.path.join(WORKSPACE, "memory/sync_log.json")
    log = {
        "last_sync": datetime.now(timezone.utc).isoformat(),
        "stats": stats
    }
    with open(log_path, 'w') as f:
        json.dump(log, f, indent=2)

if __name__ == "__main__":
    print(f"[{datetime.now().isoformat()}] Iniciando sync de memórias...")
    
    init_db()
    stats = sync_memories()
    save_sync_log(stats)
    
    print(f"  Novas: {stats['new']}")
    print(f"  Atualizadas: {stats['updated']}")
    print(f"  Sem mudança: {stats['unchanged']}")
    print(f"  Embeddings gerados: {stats['embeddings']}")
    print(f"  Erros: {stats['errors']}")
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM memory_entries WHERE is_current = TRUE")
    total = cur.fetchone()[0]
    print(f"  Total atual no banco: {total}")
    cur.close()
    conn.close()
    
    print("  ✅ Sync concluído")
