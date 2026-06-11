# Codex History Recovery User Guide

[English](README.md) | [简体中文](README.zh-CN.md)

This project is inherited from the [CodexPanel](https://codexpanel.com) project, which is currently under development.

This skill audits, backs up, migrates, and restores local Codex Desktop conversation history.

It protects original history first: audit and back up, then create new conversations through Codex app-server `thread/fork`. By default, it does not manually edit thread rows in SQLite and does not delete original `sessions/` or `archived_sessions/` files.

## When To Use It

- Your left sidebar becomes empty or loses old conversations after changing `auth.json`, `config.toml`, API keys, third-party gateways, or login accounts.
- Old conversations still exist on disk but are not visible under the current provider.
- You want to fork old `codex`, `openai`, or `openai_http` threads into the current `OpenAI` provider.
- You want archived conversations restored into archived space.
- You want to migrate history from an old `%USERPROFILE%\.codex` directory or another computer into the current `D:\CodexHome`.
- You only want a read-only backup package for safekeeping.

## Confirm Before Writing

Before running commands that change state, decide:

1. Which `CODEX_HOME` to recover, for example `D:\CodexHome` or `C:\Users\you\.codex`.
2. Where backups should be written.
3. Target provider/model, for example `OpenAI` / `gpt-5.5`.
4. Whether archived conversations are included.
5. Whether subagent/internal threads are included.
6. Whether new forks preserve source archive state. Recommended: `preserve-source`.
7. Whether to restore everything or filter by provider, project path, or keyword.

## Safest Local Recovery Flow

### 1. Audit and back up

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py audit `
  --codex-home D:\CodexHome `
  --backup-root F:\codex-backups `
  --full-backup `
  --archived all `
  --source-providers codex,openai,openai_http `
  --target-provider OpenAI `
  --doctor
```

The output directory uses a unique timestamp and contains:

- `thread-candidates.json`
- `thread-inventory-summary.json`
- SQLite backups
- Optional `sessions/` and `archived_sessions/` backups
- Optional `doctor-before.json`

### 2. Dry-run fork

```powershell
node D:\CodexHome\skills\codex-history-recovery\scripts\fork_threads.mjs `
  --candidates F:\codex-backups\codex-history-recovery-YYYYMMDD-HHMMSS-ffffff\thread-candidates.json `
  --target-provider OpenAI `
  --target-model gpt-5.5 `
  --target-archive-mode preserve-source `
  --source-providers codex,openai,openai_http
```

Without `--execute`, no state is changed.

### 3. Execute fork

```powershell
node D:\CodexHome\skills\codex-history-recovery\scripts\fork_threads.mjs `
  --candidates F:\codex-backups\codex-history-recovery-YYYYMMDD-HHMMSS-ffffff\thread-candidates.json `
  --target-provider OpenAI `
  --target-model gpt-5.5 `
  --target-archive-mode preserve-source `
  --source-providers codex,openai,openai_http `
  --execute
```

Archive modes:

- `preserve-source`: active sources create active forks; archived sources create archived forks.
- `active`: every new fork appears in the active sidebar.
- `archived`: every new fork goes to archived space.

### 4. Trigger app-server scan

Sometimes rollout files exist before the state DB indexes them. This command paginates through `thread/list` and asks Codex to scan rollout files:

```powershell
node D:\CodexHome\skills\codex-history-recovery\scripts\scan_threads.mjs `
  --ws-url ws://127.0.0.1:4888 `
  --model-providers OpenAI `
  --output F:\codex-backups\thread-list-scan.json
```

Omit `--model-providers OpenAI` to inspect all providers.

### 5. Validate mapping

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py validate `
  --codex-home D:\CodexHome `
  --mapping F:\codex-backups\codex-history-recovery-YYYYMMDD-HHMMSS-ffffff\fork-results.json `
  --target-provider OpenAI `
  --target-model gpt-5.5
```

Validation checks:

- Source threads still exist.
- Fork threads exist.
- Fork provider is correct.
- Fork model is correct.
- Active/archived state matches the requested mode.

### 6. Run doctor

```powershell
codex doctor --json
```

The key check is:

```text
state.rollout_db_parity.status = ok
```

An unrelated warning under `updates.status` does not usually mean recovery failed.

### 7. Write a recovery report

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py report `
  --codex-home D:\CodexHome `
  --mapping F:\codex-backups\codex-history-recovery-YYYYMMDD-HHMMSS-ffffff\fork-results.json `
  --output F:\codex-backups\restore-report.md `
  --doctor
```

The report includes provider counts, fork totals, and doctor rollout parity status. A JSON version is written next to the Markdown file.

## Migrating From Old `.codex` Or Another Computer

### Source machine: audit

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py audit `
  --codex-home C:\Users\you\.codex `
  --backup-root F:\codex-backups `
  --full-backup `
  --archived all
```

### Source machine: export package

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py export-package `
  --codex-home C:\Users\you\.codex `
  --candidates F:\codex-backups\codex-history-recovery-YYYYMMDD-HHMMSS-ffffff\thread-candidates.json `
  --output F:\codex-backups\old-dotcodex-migration.zip `
  --name "old dotcodex migration"
```

The migration zip never includes `auth.json` or API keys.

### Destination machine: dry-run import

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py import-package `
  --codex-home D:\CodexHome `
  --package F:\codex-backups\old-dotcodex-migration.zip `
  --backup-root F:\codex-backups
```

The dry-run reports:

- Which rollouts would be written.
- Which files already exist.
- Which same-thread-id imports are skipped to avoid active/archived duplicates.

### Destination machine: execute import

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py import-package `
  --codex-home D:\CodexHome `
  --package F:\codex-backups\old-dotcodex-migration.zip `
  --backup-root F:\codex-backups `
  --execute
```

After import, run `scan_threads.mjs` and `codex doctor --json`. If imported threads are still under an old provider, use `fork_threads.mjs` to fork them into the current provider.

## Handling Duplicate Thread IDs

If doctor reports:

```text
duplicate rollout thread ids
missing active rows
```

the same thread ID probably exists in both `sessions/` and `archived_sessions/`, or an import copied a thread that was already present.

Dry-run:

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py quarantine-duplicates `
  --codex-home D:\CodexHome `
  --backup-root F:\codex-backups
```

Execute:

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py quarantine-duplicates `
  --codex-home D:\CodexHome `
  --backup-root F:\codex-backups `
  --execute
```

The command does not delete content. It moves duplicate rollout files into a `duplicate-rollout-quarantine-*` directory.

## FAQ

### Why did old conversations disappear?

They usually did not disappear from disk. The current sidebar may be filtering by provider, account space, or state DB index. Old conversations may still exist under providers such as `codex`, `openai`, or `openai_http`.

### Why not edit SQLite directly?

Manual `state_5.sqlite` edits can break rollout/state DB parity. The safer route is to create forks through app-server and let Codex maintain its own index.

### Why do old-provider rows remain after recovery?

That is expected. Recovery preserves original history and creates new `OpenAI` forks. It does not delete source threads.

### Why do some restored titles still look generic?

The fork script sets titles from source by default. If the UI still shows a generic title, restart Codex or open the thread so the UI refreshes.

### Can I copy `state_5.sqlite` from one computer to another?

Not recommended. It can overwrite destination history, account state, and indexes. Use `export-package` and `import-package` instead.

## File Reference

- `SKILL.md`: workflow used by Codex agents.
- `scripts/codex_history_inventory.py`: audit, backup, export, import, validate, quarantine, and report commands.
- `scripts/fork_threads.mjs`: forks threads through Codex app-server.
- `scripts/scan_threads.mjs`: paginates `thread/list`, triggers rollout scanning, and outputs visible inventory counts.
- `README.zh-CN.md`: Chinese user guide.
- `README.md`: English user guide.
