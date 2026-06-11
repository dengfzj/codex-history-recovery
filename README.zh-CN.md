# Codex History Recovery 使用指南

[English](README.md) | [简体中文](README.zh-CN.md)

本项目继承自正在开发中的 [CodexPanel](https://codexpanel.com) 项目。

这个 skill 用来审计、备份、迁移和恢复 Codex 桌面端的本地会话历史。

它优先保护原始历史：先审计和备份，再通过 Codex app-server 的 `thread/fork` 生成新会话。默认不直接改 SQLite 线程记录，不删除原始 `sessions/` 或 `archived_sessions/`。

## 适合什么时候用

- 切换 `auth.json`、`config.toml`、API Key、第三方中转、官方账号后，左侧会话列表变少或变空。
- 旧会话还在本机文件夹里，但当前 provider 下看不到。
- 想把 `codex`、`openai`、`openai_http` 等旧 provider 的会话恢复到当前 `OpenAI`。
- 想把归档会话也恢复到归档区。
- 想从旧的 `%USERPROFILE%\.codex` 或另一台电脑迁移历史到当前 `D:\CodexHome`。
- 想先导出一个只读备份包，暂时不恢复。

## 恢复前需要确认

执行写入动作前，建议明确这些选择：

1. 要恢复哪个 `CODEX_HOME`，例如 `D:\CodexHome` 或 `C:\Users\你\.codex`。
2. 备份放在哪里，例如 `F:\codex备份`。
3. 目标 provider/model，例如 `OpenAI` / `gpt-5.5`。
4. 是否包含归档会话。
5. 是否包含 subagent/internal 线程。
6. 新 fork 是否保留原归档状态，推荐 `preserve-source`。
7. 是否只恢复某些 provider、项目目录或关键词。

## 最安全的本机恢复流程

### 1. 审计并备份

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py audit `
  --codex-home D:\CodexHome `
  --backup-root F:\codex备份 `
  --full-backup `
  --archived all `
  --source-providers codex,openai,openai_http `
  --target-provider OpenAI `
  --doctor
```

输出目录会自动使用唯一时间戳，里面包含：

- `thread-candidates.json`
- `thread-inventory-summary.json`
- SQLite 备份
- 可选的 `sessions/`、`archived_sessions/` 完整备份
- 可选的 `doctor-before.json`

### 2. 先 dry-run

```powershell
node D:\CodexHome\skills\codex-history-recovery\scripts\fork_threads.mjs `
  --candidates F:\codex备份\codex-history-recovery-YYYYMMDD-HHMMSS-ffffff\thread-candidates.json `
  --target-provider OpenAI `
  --target-model gpt-5.5 `
  --target-archive-mode preserve-source `
  --source-providers codex,openai,openai_http
```

没有 `--execute` 时不会修改任何状态。

### 3. 执行恢复

```powershell
node D:\CodexHome\skills\codex-history-recovery\scripts\fork_threads.mjs `
  --candidates F:\codex备份\codex-history-recovery-YYYYMMDD-HHMMSS-ffffff\thread-candidates.json `
  --target-provider OpenAI `
  --target-model gpt-5.5 `
  --target-archive-mode preserve-source `
  --source-providers codex,openai,openai_http `
  --execute
```

归档模式：

- `preserve-source`：源会话 active，新 fork active；源会话 archived，新 fork archived。
- `active`：全部恢复到左侧活跃列表。
- `archived`：全部恢复到归档区。

### 4. 触发 app-server 扫描

有时 fork 或导入后的 rollout 文件已经存在，但 state DB 还没索引到。用这个命令分页扫描并修复索引：

```powershell
node D:\CodexHome\skills\codex-history-recovery\scripts\scan_threads.mjs `
  --ws-url ws://127.0.0.1:4888 `
  --model-providers OpenAI `
  --output F:\codex备份\thread-list-scan.json
```

如果想看所有 provider，去掉 `--model-providers OpenAI`。

### 5. 验证 fork 映射

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py validate `
  --codex-home D:\CodexHome `
  --mapping F:\codex备份\codex-history-recovery-YYYYMMDD-HHMMSS-ffffff\fork-results.json `
  --target-provider OpenAI `
  --target-model gpt-5.5
```

这个验证会检查：

- 源线程是否还在。
- fork 线程是否存在。
- fork provider 是否正确。
- fork model 是否正确。
- active/archived 状态是否符合预期。

### 6. 运行 doctor

```powershell
codex doctor --json
```

重点看：

```text
state.rollout_db_parity.status = ok
```

如果 `updates.status` 是 warning，但 `state.rollout_db_parity` 是 ok，通常说明恢复本身没问题，只是更新检查或网络探测有警告。

### 7. 生成恢复报告

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py report `
  --codex-home D:\CodexHome `
  --mapping F:\codex备份\codex-history-recovery-YYYYMMDD-HHMMSS-ffffff\fork-results.json `
  --output F:\codex备份\restore-report.md `
  --doctor
```

报告会包含 provider 计数、fork 结果、doctor rollout parity 状态，并同时写出 JSON 版。

## 从旧 `.codex` 或另一台电脑迁移

### A 端：审计

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py audit `
  --codex-home C:\Users\你\.codex `
  --backup-root F:\codex备份 `
  --full-backup `
  --archived all
```

### A 端：导出迁移包

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py export-package `
  --codex-home C:\Users\你\.codex `
  --candidates F:\codex备份\codex-history-recovery-YYYYMMDD-HHMMSS-ffffff\thread-candidates.json `
  --output F:\codex备份\old-dotcodex-migration.zip `
  --name "old dotcodex migration"
```

迁移包不会包含 `auth.json` 或 API Key。

### B 端：先 dry-run 导入

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py import-package `
  --codex-home D:\CodexHome `
  --package F:\codex备份\old-dotcodex-migration.zip `
  --backup-root F:\codex备份
```

dry-run 会显示：

- 计划写入哪些 rollout。
- 哪些文件已存在。
- 哪些同 thread id 的文件会被跳过，避免 active/archived 重复。

### B 端：确认后导入

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py import-package `
  --codex-home D:\CodexHome `
  --package F:\codex备份\old-dotcodex-migration.zip `
  --backup-root F:\codex备份 `
  --execute
```

导入后运行 `scan_threads.mjs` 和 `codex doctor --json`。如果导入的会话仍在旧 provider 下，再用 `fork_threads.mjs` fork 到当前 provider。

## 处理重复 thread id

如果 doctor 报：

```text
duplicate rollout thread ids
missing active rows
```

通常是同一个 thread id 同时存在于 `sessions/` 和 `archived_sessions/`，或者从旧目录导入了已有线程。

先 dry-run：

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py quarantine-duplicates `
  --codex-home D:\CodexHome `
  --backup-root F:\codex备份
```

确认计划正确后执行：

```powershell
python D:\CodexHome\skills\codex-history-recovery\scripts\codex_history_inventory.py quarantine-duplicates `
  --codex-home D:\CodexHome `
  --backup-root F:\codex备份 `
  --execute
```

它不会删除内容，只会把重复 rollout 移到 `duplicate-rollout-quarantine-*` 目录。

## 常见问题

### 为什么旧会话会消失？

常见原因不是文件丢了，而是当前 Codex 侧栏按 provider、账号空间或索引状态过滤。旧会话可能仍在 `codex`、`openai`、`openai_http` 等 provider 下。

### 为什么不直接改 SQLite？

直接改 `state_5.sqlite` 容易破坏 rollout/state DB 一致性。更稳妥的方式是通过 app-server 的 `thread/fork` 生成新线程，再让 Codex 自己维护索引。

### 为什么恢复后还有旧 provider 的记录？

这是正常的。恢复默认不删除原始历史，只是创建新的 `OpenAI` fork。旧 provider 记录保留作为原始资产。

### 为什么恢复后标题还是不对？

脚本默认会把源标题复制到 fork。如果 Codex UI 仍显示“新会话”，通常是 UI 缓存或懒加载，打开会话或重启桌面端后会刷新。

### 可以把 A 电脑的 `state_5.sqlite` 直接覆盖 B 电脑吗？

不推荐。这样可能覆盖 B 电脑已有会话、账号状态和索引。推荐用 `export-package` / `import-package`。

## 文件说明

- `SKILL.md`：Codex agent 使用这个 skill 时读取的工作流程。
- `scripts/codex_history_inventory.py`：审计、备份、导出、导入、验证、quarantine、报告。
- `scripts/fork_threads.mjs`：通过 app-server fork 线程到目标 provider。
- `scripts/scan_threads.mjs`：分页调用 `thread/list`，触发 rollout 扫描并输出可见列表计数。
- `README.zh-CN.md`：中文使用手册。
- `README.md`：英文使用手册。
