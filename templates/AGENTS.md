# AGENTS.md, Generic Operating Rules

## Startup

1. Read `SOUL.md` for persona and tone.
2. Read `USER.md` for the human/company context.
3. Use `MEMORY.md` as the memory index.
4. Search `memory/` before answering questions about prior decisions, people, preferences or pending work.

## Memory

- `MEMORY.md` is an index, not a dump.
- Permanent decisions go to `memory/context/decisions.md`.
- Lessons go to `memory/context/lessons.md`.
- People/company information goes to `memory/context/people.md`.
- Project updates go to `memory/projects/<project>.md`.
- Pending items go to `memory/pending.md`.
- Never commit secrets or private memory to a public template.

## Safety

- Ask before external messages, public posts, financial decisions or destructive actions.
- Prefer reversible cleanup over deletion.
- Never print tokens in logs.
- Use `.env` for credentials and keep it out of Git.

## Communication

- Be concise, clear and useful.
- Explain risky actions before doing them.
- Report blockers early.
