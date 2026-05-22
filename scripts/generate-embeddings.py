#!/usr/bin/env python3
"""Gera embeddings pra todas as entradas sem embedding. Batch com delay."""
import os, sys, json, time
import psycopg2
import urllib.request

WORKSPACE = os.path.expanduser("~/.openclaw/workspace")
def load_env():
    env = os.path.join(WORKSPACE, ".env")
    if os.path.exists(env):
        for line in open(env):
            line = line.strip()
            if '=' in line and not line.startswith('#'):
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
load_env()

DB = {"host":"localhost","port":5432,"dbname":"agent_memory","user":"agent","password":os.environ['PG_PASSWORD']}
API_KEY = os.environ.get("GEMINI_API_KEY")
MODEL = "gemini-embedding-2"
DIM = 1536

def get_embedding(text):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:embedContent?key={API_KEY}"
    payload = json.dumps({"model":f"models/{MODEL}","content":{"parts":[{"text":text[:2000]}]},"outputDimensionality":DIM}).encode()
    req = urllib.request.Request(url, data=payload, headers={"Content-Type":"application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read()).get("embedding",{}).get("values",[])

conn = psycopg2.connect(**DB)
cur = conn.cursor()

# Pegar entradas sem embedding
cur.execute("SELECT id, title, content FROM memory_entries WHERE embedding IS NULL AND is_current = TRUE ORDER BY created_at DESC")
rows = cur.fetchall()
print(f"Entradas sem embedding: {len(rows)}")

ok = 0; err = 0
for i, (eid, title, content) in enumerate(rows):
    text = f"{title}\n\n{content[:1500]}"
    try:
        emb = get_embedding(text)
        if emb and len(emb) == DIM:
            cur.execute("UPDATE memory_entries SET embedding = %s WHERE id = %s", (emb, eid))
            ok += 1
            if ok % 10 == 0:
                conn.commit()
                print(f"  {ok}/{len(rows)} embeddings gerados...")
        else:
            err += 1
            print(f"  Erro dimensão: {len(emb) if emb else 0}")
    except Exception as e:
        err += 1
        if "429" in str(e) or "quota" in str(e).lower():
            print(f"  Rate limit! Pausando 30s...")
            time.sleep(30)
        else:
            print(f"  Erro: {e}")
    time.sleep(1)  # 1 request/second pra evitar rate limit

conn.commit()
cur.close()
conn.close()
print(f"\nConcluído: {ok} embeddings, {err} erros")
