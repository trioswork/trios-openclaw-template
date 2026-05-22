# Disaster Recovery

For a private production agent, keep a separate private backup repository.

Recommended restore flow:

1. Install the generic template on a fresh VPS.
2. Clone your private backup repository.
3. Restore non-secret workspace files.
4. Recreate `.env` manually or from a secure password manager.
5. Restore database dumps from your private storage if used.
6. Run `openclaw gateway restart`.

Never publish credentials, private memories or production backups in this public template repository.
