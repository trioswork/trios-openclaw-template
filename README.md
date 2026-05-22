# OpenClaw Agent Template

Template público e genérico para subir, em poucos minutos, um agente OpenClaw com memória semântica, automações básicas, arquivos de persona e rotina de backup segura.

Este repositório não contém dados privados, memórias reais, clientes, credenciais ou contexto de uma empresa específica.

## Instalação em 1 comando

Numa VPS nova com Ubuntu/Debian e acesso root:

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/trioswork/trios-openclaw-template/main/install.sh)
```

O instalador prepara:

- Node.js
- PostgreSQL + pgvector
- Python e dependências de memória
- OpenClaw
- Workspace inicial em `/root/.openclaw/workspace`
- Arquivos `SOUL.md`, `USER.md`, `AGENTS.md`, `MEMORY.md` genéricos
- `.env` local com banco configurado
- Crontab de sincronização de memória
- Estrutura pronta para personalizar e treinar para a empresa da pessoa

## Depois de instalar

1. Edite as credenciais locais:

```bash
nano /root/.openclaw/workspace/.env
```

2. Configure o OpenClaw:

```bash
openclaw configure
```

3. Personalize o agente:

```bash
nano /root/.openclaw/workspace/SOUL.md
nano /root/.openclaw/workspace/USER.md
nano /root/.openclaw/workspace/AGENTS.md
```

4. Reinicie o gateway:

```bash
openclaw gateway restart
```

## Incluído

- Templates genéricos de identidade, usuário, operação e memória.
- Scripts de sincronização de memória para PostgreSQL/pgvector.
- Schema de memória.
- Rotina exemplo de backup via GitHub sem salvar token no remote.
- Guia básico de disaster recovery.

## Não incluído

- `.env` real.
- Tokens, chaves, credenciais ou sessões.
- `memory/` com conteúdo privado.
- Backups de produção.
- Dados pessoais, clientes, nomes de empresas ou contexto privado.
