---
name: codex-history-recovery
description: Recover, migrate, audit, back up, and fork Codex desktop conversation history across API keys, third-party providers, official accounts, CODEX_HOME directories, active and archived sidebars, and missing thread lists. Use when Codex conversations disappear after changing auth.json, config.toml, model_provider, base_url, API tokens, or login accounts, or when old local sessions must be made visible under the current provider without rewriting originals.
metadata:
  short-description: Recover and migrate Codex thread history
---

# Codex History Recovery

Use this skill when a user needs missing Codex desktop conversations restored to the left sidebar, migrated between providers/accounts, forked to the current API configuration, or preserved before risky auth/config changes.

## Core Rule

Treat local Codex history as user assets. Prefer read-only audit, consistent backup, then app-server `thread/fork`. Do not raw-edit thread rows unless every supported API path fails and the user explicitly accepts a fallback.

## Required User Confirmations

Before changing state, ask short questions and wait for answers. Do not guess silently when the answer affects data location or visibility.

Ask at least:

1. **Goal**: restore missing sidebar on this machine, migrate A machine to B machine, export a read-only backup, or fork archived history too?
2. **Codex home**: which `CODEX_HOME` should be used? Offer detected paths such as `D:\CodexHome` or `%USERPROFILE%\.codex`.
3. **Backup path**: where should timestamped backups/packages be written?
4. **Target provider/model**: current provider/model, or a specific provider such as `OpenAI` and model such as `gpt-5.5`?
5. **Active/archived behavior**: preserve source archive state, make all forks active, or put all forks into archived space?
6. **Scope**: all user-main threads, only specific providers, only archived, only active, or search keywords?
7. **Execution**: audit/dry-run first, then explicit permission before `--execute`.

For high-risk cases, add one more confirmation: whether to include full rollout folders in backup and whether to package data for another machine.

## Standard Workflow

1. **Identify state location**
   - Prefer an explicit `--codex-home`.
   - Otherwise inspect `$CODEX_HOME`, `codex doctor --json`, `%USERPROFILE%\.codex`, and known custom locations such as `D:\CodexHome`.
   - Confirm `state_5.sqlite` exists.

2. **Diagnose provider filtering**
   - Compare current `config.toml` provider/model with thread rows grouped by `model_provider`, `archived`, `source`, and `thread_source`.
   - If conversations exist under old providers such as `codex`, `openai`, `openai_http`, or another custom name, the sidebar may simply be filtering to the current provider.
   - API keys and third-party base URLs usually do not define history identity by themselves; the durable local separator is normally thread metadata plus active account/provider filtering.

3. **Back up before changing anything**
   - Make a timestamped backup of SQLite databases and metadata.
   - For high-value data, include rollout folders (`sessions/`, `archived_sessions/`) as well.
   - Keep originals unchanged; recovery should create new forks.

4. **Generate candidates**
   - Default to user/main threads only.
   - Skip `subagent`, memory consolidation, review, compact, and internal helper threads unless the user explicitly asks.
   - Split active and archived candidates when the desired destination state differs.

5. **Fork through app-server**
   - Use `thread/fork` with `modelProvider` set to the target provider.
   - If restoring archived history into archived space, temporarily unarchive the source, fork it, rearchive the source, then archive the new fork.
   - If restoring active history, fork and leave the new thread active.
   - Save source-to-fork mapping.
   - By default, set each fork title from the source title to reduce the "New conversation" display until first open. If titles still appear lazy, opening the thread lets Codex infer the title from history.

6. **Validate**
   - Trigger an app-server scan with `scan_threads.mjs`; it calls `thread/list` with `useStateDbOnly=false` and paginates active/archived results.
   - Run `codex doctor --json`; `state.rollout_db_parity` must be OK.
   - Query DB for source/fork IDs:
     - sources remain present and, if originally archived, still archived
     - forks exist under the target provider
     - forks have the expected provider, model, and archived/active state
   - If doctor reports duplicate rollout thread IDs, use `quarantine-duplicates` rather than deleting files.
   - Generate a user-facing Markdown summary with `report`.

## Bundled Scripts

Use the scripts from this skill folder rather than rewriting long one-off commands.

### Guided wizard

For non-expert users, start here:

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py wizard
```

Wizard modes:

- `local`: asks for Codex home, backup path, archive scope, providers, and search term, then runs audit.
- `export`: asks for an audit candidate file and creates a portable migration zip for another machine.
- `import`: asks for a migration zip, shows an import dry-run, then requires typing `yes` before writing rollout files.

### Audit and candidate generation

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py audit `
  --codex-home D:\CodexHome `
  --backup-root F:\codex-backups `
  --full-backup `
  --archived all
```

Outputs a timestamped directory containing backups, `thread-candidates.json`, and `thread-inventory-summary.json`.

Backup directory names include microseconds and retry on collision, so concurrent audits should not clobber each other.

Useful filters:

```powershell
--source-providers codex,openai,openai_http
--target-provider OpenAI
--archived active
--archived archived
--search "Microsoft store"
--include-subagents
```

### Fork candidates

Default is dry-run; pass `--execute` to change state.

```powershell
node D:\CodexHome\skills\codex-history-recovery\scripts\fork_threads.mjs `
  --candidates F:\codex-backups\restore-YYYYMMDD-HHMMSS\thread-candidates.json `
  --target-provider OpenAI `
  --target-model gpt-5.5 `
  --target-archive-mode preserve-source `
  --execute
```

Archive modes:

- `preserve-source`: archived sources create archived forks; active sources create active forks.
- `active`: every new fork stays active.
- `archived`: every new fork is archived.

If app-server rejects `excludeTurns`, the script retries without that experimental response-size optimization.

By default the fork script runs `thread/name/set` using the source title. Disable with `--no-set-title-from-source` if the user wants Codex to infer titles lazily.

### Validate mapping

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py validate `
  --codex-home D:\CodexHome `
  --mapping F:\codex-backups\restore-YYYYMMDD-HHMMSS\fork-results.json `
  --target-provider OpenAI `
  --target-model gpt-5.5
```

### Scan app-server inventory

Use this after importing rollout files, after forking, or whenever `state.rollout_db_parity` looks stale. It is read-only, but `useStateDbOnly=false` asks Codex to scan rollout JSONL files and repair thread metadata.

```powershell
node D:\CodexHome\skills\codex-history-recovery\scripts\scan_threads.mjs `
  --ws-url ws://127.0.0.1:4888 `
  --model-providers OpenAI `
  --output F:\codex-backups\thread-list-scan.json
```

Omit `--model-providers` to inspect all providers.

### Quarantine duplicate rollout IDs

Use this when doctor reports duplicate rollout thread IDs, commonly after importing a thread that already exists in active or archived space.

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py quarantine-duplicates `
  --codex-home D:\CodexHome `
  --backup-root F:\codex-backups
```

The default is dry-run. If the plan is correct, rerun with `--execute`. The command moves duplicate files into a timestamped quarantine directory and keeps the first archived/original-looking file in place.

### Final report

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py report `
  --codex-home D:\CodexHome `
  --mapping F:\codex-backups\restore-YYYYMMDD-HHMMSS\fork-results.json `
  --output F:\codex-backups\restore-report.md `
  --doctor
```

## Multi-Device Migration Branch

Use this when the user has machine A with old history and machine B with a different Codex install/account/provider.

Principle: do not overwrite B's database with A's database. Export A's rollout assets, import them into B, let Codex scan/repair the index, then fork to B's current provider if needed.

### A machine: audit and export

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py audit `
  --codex-home D:\CodexHome `
  --backup-root F:\codex-backups `
  --full-backup `
  --archived all
```

Then package the selected candidates:

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py export-package `
  --codex-home D:\CodexHome `
  --candidates F:\codex-backups\codex-history-recovery-YYYYMMDD-HHMMSS\thread-candidates.json `
  --output F:\codex-backups\codex-history-migration.zip `
  --name "A-to-B history migration"
```

The export package intentionally excludes `auth.json` and API keys.

### B machine: dry-run import, then execute

Copy the zip to B, install this skill, then run:

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py import-package `
  --codex-home D:\CodexHome `
  --package D:\incoming\codex-history-migration.zip `
  --backup-root F:\codex-backups
```

If the plan looks right:

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py import-package `
  --codex-home D:\CodexHome `
  --package D:\incoming\codex-history-migration.zip `
  --backup-root F:\codex-backups `
  --execute
```

After import:

1. Restart Codex, or run `scan_threads.mjs` so Codex scans imported rollout files.
2. Run audit again on B.
3. If imported threads are under old providers, fork them into B's current provider using `fork_threads.mjs`.
4. If duplicate rollout IDs appear, run `quarantine-duplicates` and then `codex doctor --json` again.

## Decision Guide

- **User changed only third-party `base_url` or API key and history stayed:** likely same provider/account filter; no migration needed.
- **Sidebar empty after switching provider/account:** audit providers, then fork old provider threads into the current provider.
- **Official login should show recovered threads:** if official login still uses the same `CODEX_HOME` and provider filter, existing forks should show; if it uses a different provider/account space, fork again to that provider.
- **Archived conversations must appear in archived space:** fork them with `--target-archive-mode archived` or `preserve-source`.
- **Move history from A computer to B computer:** export package on A, import package on B, scan/repair, then fork to B's current provider.
- **Recover from multiple local homes:** audit each source home separately, export/import non-target homes into the target home, scan, then fork only missing or old-provider threads. Avoid forking threads already under the target provider unless the user explicitly wants duplicates.
- **Only need a read-only asset dump:** stop after audit/backup and do not run `fork_threads.mjs --execute`.

## Safety Boundaries

- Never delete original `sessions/` or `archived_sessions/`.
- Never run destructive git or filesystem commands during recovery.
- Never expose or print full API keys from `auth.json`.
- Never include `auth.json` in a multi-device migration package.
- Prefer app-server methods over manual SQLite writes.
- If the app-server is unavailable, repair by `thread/list` scans first; raw DB surgery is last resort and needs explicit user approval.
- After any interrupted run, validate source archive state before retrying.
