#!/usr/bin/env node
// Fork Codex app-server threads into a target provider.
//
// Dry-run by default. Pass --execute to change state.

import fs from "node:fs";
import path from "node:path";
import process from "node:process";

function usage() {
  console.log(`Usage:
  node fork_threads.mjs --candidates <thread-candidates.json> --target-provider <provider> [options]

Options:
  --target-model <model>              Model to set on forks, for example gpt-5.5
  --target-archive-mode <mode>        preserve-source | active | archived (default: preserve-source)
  --ws-url <url>                      App-server WebSocket URL (default: ws://127.0.0.1:4888)
  --results <path>                    Results JSON path (default: fork-results.json next to candidates)
  --execute                           Actually fork. Without this, only prints dry-run summary.
  --limit <n>                         Process at most n candidates
  --start-after <thread-id>           Skip until after this source thread id
  --include-subagents                 Do not skip subagent/internal rows
  --source-providers <a,b,c>          Further filter candidates by sourceProvider
  --search <text>                     Further filter candidates by title/cwd/id
  --exclude-turns                     Request reduced fork response when supported (default: true)
  --no-exclude-turns                  Do not request excludeTurns
  --experimental                      Declare experimentalApi capability (default: true)
  --no-experimental                   Do not declare experimentalApi capability
  --set-title-from-source             Name each fork from its source title (default: true)
  --no-set-title-from-source          Leave fork title for Codex to infer lazily
  --max-title-length <n>              Max copied title length (default: 120)
  --title-settle-ms <n>               Delay before the final title re-apply (default: 250)
`);
}

function parseArgs(argv) {
  const args = {
    targetArchiveMode: "preserve-source",
    wsUrl: "ws://127.0.0.1:4888",
    execute: false,
    includeSubagents: false,
    excludeTurns: true,
    experimental: true,
    setTitleFromSource: true,
    maxTitleLength: 120,
    titleSettleMs: 250,
  };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const next = () => {
      if (i + 1 >= argv.length) throw new Error(`${arg} requires a value`);
      i += 1;
      return argv[i];
    };
    switch (arg) {
      case "--help":
      case "-h":
        args.help = true;
        break;
      case "--candidates":
        args.candidates = next();
        break;
      case "--target-provider":
        args.targetProvider = next();
        break;
      case "--target-model":
        args.targetModel = next();
        break;
      case "--target-archive-mode":
        args.targetArchiveMode = next();
        break;
      case "--ws-url":
        args.wsUrl = next();
        break;
      case "--results":
        args.results = next();
        break;
      case "--execute":
        args.execute = true;
        break;
      case "--limit":
        args.limit = Number.parseInt(next(), 10);
        break;
      case "--start-after":
        args.startAfter = next();
        break;
      case "--include-subagents":
        args.includeSubagents = true;
        break;
      case "--source-providers":
        args.sourceProviders = next();
        break;
      case "--search":
        args.search = next();
        break;
      case "--exclude-turns":
        args.excludeTurns = true;
        break;
      case "--no-exclude-turns":
        args.excludeTurns = false;
        break;
      case "--experimental":
        args.experimental = true;
        break;
      case "--no-experimental":
        args.experimental = false;
        break;
      case "--set-title-from-source":
        args.setTitleFromSource = true;
        break;
      case "--no-set-title-from-source":
        args.setTitleFromSource = false;
        break;
      case "--max-title-length":
        args.maxTitleLength = Number.parseInt(next(), 10);
        break;
      case "--title-settle-ms":
        args.titleSettleMs = Number.parseInt(next(), 10);
        break;
      default:
        throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return args;
}

function readJson(filePath) {
  return JSON.parse(fs.readFileSync(filePath, "utf8"));
}

function writeJson(filePath, data) {
  fs.writeFileSync(filePath, `${JSON.stringify(data, null, 2)}\n`, "utf8");
}

function normalizeCandidate(raw) {
  return {
    id: raw.id || raw.threadId || raw.sourceThreadId,
    sourceProvider: raw.sourceProvider ?? raw.model_provider ?? raw.modelProvider ?? "",
    sourceModel: raw.sourceModel ?? raw.model ?? "",
    title: raw.title ?? raw.name ?? raw.preview ?? "",
    cwd: raw.cwd ?? "",
    source: raw.source ?? "",
    threadSource: raw.threadSource ?? raw.thread_source ?? "",
    archived: Boolean(raw.archived),
    createdAt: raw.createdAt ?? raw.created_at ?? "",
    updatedAt: raw.updatedAt ?? raw.updated_at ?? "",
  };
}

function shouldSkipSubagent(candidate) {
  const text = `${candidate.source} ${candidate.threadSource}`.toLowerCase();
  return text.includes("subagent") || text.includes("memory_consolidation");
}

function filterCandidates(candidates, args) {
  let rows = candidates.map(normalizeCandidate).filter((row) => row.id);
  if (!args.includeSubagents) {
    rows = rows.filter((row) => !shouldSkipSubagent(row));
  }
  if (args.sourceProviders) {
    const allowed = new Set(args.sourceProviders.split(",").map((s) => s.trim()).filter(Boolean));
    rows = rows.filter((row) => allowed.has(row.sourceProvider));
  }
  if (args.search) {
    const needle = args.search.toLowerCase();
    rows = rows.filter((row) =>
      [row.id, row.title, row.cwd].some((value) => String(value || "").toLowerCase().includes(needle)),
    );
  }
  if (args.startAfter) {
    const index = rows.findIndex((row) => row.id === args.startAfter);
    if (index >= 0) rows = rows.slice(index + 1);
  }
  if (Number.isFinite(args.limit) && args.limit >= 0) {
    rows = rows.slice(0, args.limit);
  }
  return rows;
}

function targetArchivedFor(candidate, mode) {
  if (mode === "preserve-source") return Boolean(candidate.archived);
  if (mode === "active") return false;
  if (mode === "archived") return true;
  throw new Error(`Unknown target archive mode: ${mode}`);
}

class RpcClient {
  constructor(url) {
    this.url = url;
    this.nextId = 1;
    this.pending = new Map();
    this.ws = null;
  }

  async connect(experimental) {
    if (typeof WebSocket === "undefined") {
      throw new Error("This Node runtime has no global WebSocket. Use Node 20+ or the Codex bundled Node.");
    }
    this.ws = new WebSocket(this.url);
    this.ws.onmessage = (event) => {
      let msg;
      try {
        msg = JSON.parse(event.data);
      } catch {
        return;
      }
      if (msg.id && this.pending.has(msg.id)) {
        const pending = this.pending.get(msg.id);
        clearTimeout(pending.timer);
        this.pending.delete(msg.id);
        if (msg.error) pending.reject(new Error(`${pending.method}: ${JSON.stringify(msg.error)}`));
        else pending.resolve(msg.result);
      }
    };
    await new Promise((resolve, reject) => {
      this.ws.onopen = resolve;
      this.ws.onerror = (event) => reject(new Error(`WebSocket error: ${event.message || "unknown"}`));
    });
    await this.call("initialize", {
      clientInfo: { name: "codex-history-recovery", version: "1.0.0" },
      protocolVersion: 1,
      capabilities: experimental ? { experimentalApi: true } : {},
    });
  }

  call(method, params = {}, timeoutMs = 120000) {
    const id = this.nextId;
    this.nextId += 1;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`timeout calling ${method}`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer, method });
      this.ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
    });
  }

  async bestEffortArchive(threadId) {
    try {
      await this.call("thread/archive", { threadId }, 60000);
      return null;
    } catch (err) {
      return err.message || String(err);
    }
  }

  async bestEffortUnarchive(threadId) {
    try {
      await this.call("thread/unarchive", { threadId }, 60000);
      return null;
    } catch (err) {
      return err.message || String(err);
    }
  }

  async bestEffortSetTitle(threadId, name) {
    try {
      await this.call("thread/name/set", { threadId, name }, 60000);
      return null;
    } catch (err) {
      return err.message || String(err);
    }
  }

  close() {
    if (this.ws) this.ws.close();
  }
}

function forkParams(candidate, args, useExcludeTurns) {
  const params = {
    threadId: candidate.id,
    modelProvider: args.targetProvider,
    threadSource: "user",
    ephemeral: false,
  };
  if (args.targetModel) params.model = args.targetModel;
  if (useExcludeTurns) params.excludeTurns = true;
  return params;
}

function extractForkId(result) {
  return result?.threadId || result?.thread?.id || result?.id || "";
}

function titleFromSource(candidate, maxLength) {
  const title = String(candidate.title || "").replace(/\s+/g, " ").trim();
  if (!title) return "";
  if (!Number.isFinite(maxLength) || maxLength < 20) return title;
  return title.length > maxLength ? `${title.slice(0, maxLength - 1)}…` : title;
}

async function forkOne(client, candidate, args) {
  const targetArchived = targetArchivedFor(candidate, args.targetArchiveMode);
  const rec = {
    status: "started",
    sourceThreadId: candidate.id,
    sourceProvider: candidate.sourceProvider,
    sourceModel: candidate.sourceModel,
    sourceTitle: candidate.title,
    sourceCwd: candidate.cwd,
    sourceArchived: Boolean(candidate.archived),
    targetProvider: args.targetProvider,
    targetModel: args.targetModel || null,
    targetArchived,
    startedAt: new Date().toISOString(),
  };

  if (candidate.archived) {
    const error = await client.bestEffortUnarchive(candidate.id);
    if (error) throw new Error(`failed to unarchive source before fork: ${error}`);
  }

  let fork;
  try {
    fork = await client.call("thread/fork", forkParams(candidate, args, args.excludeTurns), 180000);
  } catch (err) {
    const message = err.message || String(err);
    if (args.excludeTurns && message.includes("excludeTurns")) {
      fork = await client.call("thread/fork", forkParams(candidate, args, false), 180000);
      rec.excludeTurnsRetry = true;
    } else {
      throw err;
    }
  }

  const forkThreadId = extractForkId(fork);
  if (!forkThreadId) throw new Error(`thread/fork returned no thread id: ${JSON.stringify(fork)}`);
  rec.forkThreadId = forkThreadId;

  const desiredTitle = args.setTitleFromSource ? titleFromSource(candidate, args.maxTitleLength) : "";
  if (args.setTitleFromSource) {
    if (desiredTitle) {
      const error = await client.bestEffortSetTitle(forkThreadId, desiredTitle);
      if (!error) {
        rec.titleSetFromSource = true;
        rec.title = desiredTitle;
      } else {
        rec.titleSetError = error;
      }
    }
  }

  const errors = {};
  if (candidate.archived) {
    const sourceArchiveError = await client.bestEffortArchive(candidate.id);
    if (sourceArchiveError) errors.sourceArchiveError = sourceArchiveError;
  }
  if (targetArchived) {
    const forkArchiveError = await client.bestEffortArchive(forkThreadId);
    if (forkArchiveError) errors.forkArchiveError = forkArchiveError;
  }
  if (Object.keys(errors).length > 0) {
    throw new Error(JSON.stringify(errors));
  }

  // Some Codex builds refresh sidebar metadata during archive/unarchive and can
  // overwrite an earlier thread/name/set with a lazy "New conversation" title.
  // Re-apply the copied source title after the final archive state is settled.
  if (desiredTitle) {
    const delay = Number.isFinite(args.titleSettleMs) && args.titleSettleMs > 0 ? args.titleSettleMs : 0;
    if (delay) await new Promise((resolve) => setTimeout(resolve, delay));
    const finalTitleError = await client.bestEffortSetTitle(forkThreadId, desiredTitle);
    if (!finalTitleError) {
      rec.titleSetAfterArchive = true;
      rec.title = desiredTitle;
    } else {
      rec.titleSetAfterArchiveError = finalTitleError;
    }
  }

  rec.status = "ok";
  rec.completedAt = new Date().toISOString();
  return rec;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  if (args.help) {
    usage();
    return 0;
  }
  if (!args.candidates) throw new Error("--candidates is required");
  if (!args.targetProvider) throw new Error("--target-provider is required");
  if (!["preserve-source", "active", "archived"].includes(args.targetArchiveMode)) {
    throw new Error("--target-archive-mode must be preserve-source, active, or archived");
  }

  const candidatesPath = path.resolve(args.candidates);
  const resultPath = args.results
    ? path.resolve(args.results)
    : path.join(path.dirname(candidatesPath), "fork-results.json");
  const raw = readJson(candidatesPath);
  const sourceRows = Array.isArray(raw) ? raw : raw.candidates;
  if (!Array.isArray(sourceRows)) throw new Error("Candidates JSON must be an array or { candidates: [] }");
  const candidates = filterCandidates(sourceRows, args);

  let results = [];
  if (fs.existsSync(resultPath)) {
    results = readJson(resultPath);
    if (!Array.isArray(results)) results = results.results || [];
  }
  const done = new Set(results.filter((row) => row.status === "ok").map((row) => row.sourceThreadId));
  const pending = candidates.filter((candidate) => !done.has(candidate.id));

  const summary = {
    candidatesPath,
    resultPath,
    selectedCandidates: candidates.length,
    alreadyDone: done.size,
    pending: pending.length,
    targetProvider: args.targetProvider,
    targetModel: args.targetModel || null,
    targetArchiveMode: args.targetArchiveMode,
    setTitleFromSource: args.setTitleFromSource,
    execute: args.execute,
  };
  console.log(JSON.stringify(summary, null, 2));
  if (!args.execute) {
    console.log("Dry-run only. Re-run with --execute to fork.");
    return 0;
  }

  const client = new RpcClient(args.wsUrl);
  await client.connect(args.experimental);
  try {
    for (const candidate of pending) {
      const started = {
        status: "started",
        sourceThreadId: candidate.id,
        sourceProvider: candidate.sourceProvider,
        sourceTitle: candidate.title,
        sourceArchived: Boolean(candidate.archived),
        targetProvider: args.targetProvider,
        targetModel: args.targetModel || null,
        targetArchived: targetArchivedFor(candidate, args.targetArchiveMode),
        startedAt: new Date().toISOString(),
      };
      results.push(started);
      writeJson(resultPath, results);
      try {
        const rec = await forkOne(client, candidate, args);
        Object.assign(started, rec);
        console.log(JSON.stringify({ ok: true, source: candidate.id, fork: rec.forkThreadId, title: candidate.title }));
      } catch (err) {
        started.status = "failed";
        started.error = err.message || String(err);
        started.failedAt = new Date().toISOString();
        if (candidate.archived) {
          const rearchiveError = await client.bestEffortArchive(candidate.id);
          if (rearchiveError) started.rearchiveSourceError = rearchiveError;
        }
        console.error(JSON.stringify({ ok: false, source: candidate.id, error: started.error }));
      } finally {
        writeJson(resultPath, results);
        await new Promise((resolve) => setTimeout(resolve, 250));
      }
    }
  } finally {
    client.close();
  }

  const ok = results.filter((row) => row.status === "ok").length;
  const failed = results.filter((row) => row.status === "failed").length;
  console.log(JSON.stringify({ resultPath, ok, failed }, null, 2));
  return failed ? 2 : 0;
}

main()
  .then((code) => process.exit(code))
  .catch((err) => {
    console.error(err.stack || String(err));
    process.exit(1);
  });
