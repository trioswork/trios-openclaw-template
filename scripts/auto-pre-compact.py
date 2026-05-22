#!/usr/bin/env python3
"""
Trios Auto Pre-Compaction Watchdog — Monitora sessões e disca pre-compaction automaticamente.

Monitora o uso de tokens das sessões e roda o pre-compaction.py automaticamente
quando uma sessão ultrapassa 80% do limite, evitando perda de memórias na compactação.

Uso:
    python3 auto-pre-compact.py [--threshold 80] [--agent main]
"""

import os
import sys
import json
import subprocess
import argparse
import logging
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Any

# Configuração
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger('auto-pre-compact')


def load_dotenv():
    """Carrega variáveis de ambiente do .env."""
    env_file = Path(__file__).parent.parent / '.env'
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, _, value = line.partition('=')
                os.environ.setdefault(key.strip(), value.strip())


load_dotenv()


def get_session_status(agent: str = 'main') -> List[Dict[str, Any]]:
    """
    Obtém status das sessões via CLI do OpenClaw.

    Returns:
        Lista de sessões com token usage
    """
    try:
        cmd = ['openclaw', 'sessions', '--json', '--agent', agent]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30
        )

        if result.returncode != 0:
            log.error(f"Erro ao buscar sessões: {result.stderr}")
            return []

        data = json.loads(result.stdout)
        return data.get('sessions', [])

    except subprocess.TimeoutExpired:
        log.error("Timeout ao buscar sessões")
        return []
    except json.JSONDecodeError as e:
        log.error(f"Erro ao parsear JSON: {e}")
        return []
    except Exception as e:
        log.error(f"Erro inesperado: {e}")
        return []


def extract_transcript(session_file: Path) -> str:
    """
    Extrai o transcript de um arquivo de sessão JSONL.

    Args:
        session_file: Caminho para o arquivo .jsonl

    Returns:
        String com o transcript formatado (role: message)
    """
    if not session_file.exists():
        log.warning(f"Arquivo de sessão não encontrado: {session_file}")
        return ""

    lines = []
    try:
        for line in session_file.read_text(encoding='utf-8').splitlines():
            if not line.strip():
                continue

            try:
                entry = json.loads(line)

                # Extrai mensagens
                if entry.get('type') == 'message':
                    message = entry.get('message', {})
                    role = message.get('role', 'unknown')

                    # Extrai conteúdo
                    content = []
                    for part in message.get('content', []):
                        if isinstance(part, dict):
                            text = part.get('text', '')
                            if text:
                                content.append(text)
                        elif isinstance(part, str):
                            content.append(part)

                    if content:
                        message_text = '\n'.join(content)
                        lines.append(f"{role.upper()}: {message_text}")

            except json.JSONDecodeError:
                continue

        return '\n\n'.join(lines)

    except Exception as e:
        log.error(f"Erro ao ler {session_file}: {e}")
        return ""


def run_pre_compaction(transcript: str, session_id: str, agent_id: str = 'main') -> bool:
    """
    Roda o script pre-compaction.py com o transcript.

    Args:
        transcript: Texto da conversa
        session_id: ID da sessão
        agent_id: ID do agente

    Returns:
        True se rodou com sucesso, False caso contrário
    """
    if not transcript.strip():
        log.warning("Transcript vazio, pulando pre-compaction")
        return False

    try:
        pre_compact_script = Path(__file__).parent / 'pre-compaction.py'

        cmd = [
            'python3',
            str(pre_compact_script),
            '--agent-id', agent_id,
            '--session-id', session_id
        ]

        result = subprocess.run(
            cmd,
            input=transcript,
            capture_output=True,
            text=True,
            timeout=300,
            env=os.environ
        )

        if result.returncode == 0:
            log.info(f"Pre-compaction completado para sessão {session_id}")

            # Tenta parsear o resultado JSON
            try:
                output = json.loads(result.stdout)
                extracted = output.get('extracted', 0)
                saved = output.get('saved', 0)
                log.info(f"  → {extracted} segmentos extraídos, {saved} salvos")
            except:
                pass

            return True
        else:
            log.error(f"Erro no pre-compaction: {result.stderr}")
            return False

    except subprocess.TimeoutExpired:
        log.error("Timeout no pre-compaction")
        return False
    except Exception as e:
        log.error(f"Erro ao rodar pre-compaction: {e}")
        return False


def check_and_compact(threshold: int = 80, agent: str = 'main') -> Dict[str, Any]:
    """
    Verifica sessões e roda pre-compaction se necessário.

    Args:
        threshold: Porcentagem de uso para disparar (default 80%)
        agent: ID do agente a monitorar

    Returns:
        Dict com estatísticas da execução
    """
    sessions = get_session_status(agent)

    if not sessions:
        return {
            'sessions_checked': 0,
            'sessions_compacted': 0,
            'errors': 0,
            'reason': 'no_sessions'
        }

    sessions_compacted = 0
    errors = 0

    log.info(f"Verificando {len(sessions)} sessões (threshold: {threshold}%)")

    for session in sessions:
        session_id = session.get('sessionId', 'unknown')
        total_tokens = session.get('totalTokens') or 0
        context_tokens = session.get('contextTokens') or 0

        if context_tokens == 0:
            continue  # Não tem limite configurado

        if total_tokens == 0:
            continue  # Sem tokens ainda

        usage_percent = (total_tokens / context_tokens) * 100

        if usage_percent >= threshold:
            log.warning(f"Sessão {session_id}: {usage_percent:.1f}% usado ({total_tokens}/{context_tokens})")

            # Extrai transcript
            session_key = session.get('key', '')
            if not session_key:
                continue

            # Monta caminho do arquivo da sessão
            session_file = Path(f"/root/.openclaw/agents/{agent}/sessions/{session_id}.jsonl")

            if not session_file.exists():
                # Tenta usar o sessionId diretamente
                session_file = Path(f"/root/.openclaw/agents/{agent}/sessions/{session_id}.jsonl")

            if not session_file.exists():
                log.warning(f"Arquivo de sessão não encontrado: {session_file}")
                errors += 1
                continue

            transcript = extract_transcript(session_file)

            if not transcript:
                log.warning(f"Transcript vazio para {session_id}")
                errors += 1
                continue

            # Roda pre-compaction
            if run_pre_compaction(transcript, session_id, agent):
                sessions_compacted += 1
            else:
                errors += 1

    return {
        'sessions_checked': len(sessions),
        'sessions_compacted': sessions_compacted,
        'errors': errors,
        'threshold': threshold
    }


def main():
    parser = argparse.ArgumentParser(description='Trios Auto Pre-Compaction Watchdog')
    parser.add_argument('--threshold', type=int, default=80, help='Porcentagem de uso para disparar (default: 80)')
    parser.add_argument('--agent', default='main', help='ID do agente a monitorar')
    parser.add_argument('--once', action='store_true', help='Roda uma vez e sai (para testes)')
    parser.add_argument('--interval', type=int, default=300, help='Intervalo em segundos (default: 300 = 5min)')
    args = parser.parse_args()

    if args.once:
        # Modo único (para testes)
        log.info("Modo único (uma execução)")
        result = check_and_compact(args.threshold, args.agent)
        log.info(f"Resultado: {json.dumps(result, indent=2)}")
        sys.exit(0 if result['errors'] == 0 else 1)

    # Modo contínuo (para cron)
    log.info(f"Modo contínuo: verificando a cada {args.interval}s")
    log.info("Pressione Ctrl+C para parar")

    try:
        import time
        while True:
            result = check_and_compact(args.threshold, args.agent)
            if result['sessions_compacted'] > 0:
                log.info(f"Ciclo completo: {result['sessions_compacted']} sessões compactadas")

            time.sleep(args.interval)
    except KeyboardInterrupt:
        log.info("Parado pelo usuário")
        sys.exit(0)


if __name__ == '__main__':
    main()
