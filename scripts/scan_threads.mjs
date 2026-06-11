#!/usr/bin/env node
// Scan Codex thread inventory through app-server thread/list.
//
// This is read-only, but useStateDbOnly=false asks Codex to scan rollout files
// and repair thread metadata before returning paginated results.

import fs from "node:fs";
import path from "node:path";
import process from "node:process";

function usage() {
  console.log(`Usage:
  node scan_threads.mjs [options]

Options:
  --ws-url <url>                 App-server WebSocket URL (default: ws://127.0.0.1:4888)
  --output <path>                JSON output path
  --model-providers <a,b,c>      Optional provider filter
  --source-kinds <a,b,c>         Optional source kind filter; omit for app defaults
  --limit <n>                    Page size (default: 100)
  --use-state-db-only            Do not scan rollout JSONL files
  --experimental                 Declare experimentalApi capability (default: true)
  --no-experimental              Do not declare experimentalApi capability
`);
}

function parseArgs(argv) {
  const args = {
    wsUrl: "ws://127.0.0.1:4888",
    limit: 100,
    useStateDbOnly: false,
    experimental: true,
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
      case "--ws-url":
        args.wsUrl = next();
        break;
      case "--output":
        args.output = next();
        break;
      case "--model-providers":
        args.modelProviders = next().split(",").map((value) => value.trim()).filter(Boolean);
        break;
      case "--source-kinds":
        args.sourceKinds = next().split(",").map((value) => value.trim()).filter(Boolean);
        break;
      case "--limit":
        args.limit = Number.parseInt(next(), 10);
        break;
      case "--use-state-db-only":
        args.useStateDbOnly = true;
        break;
      case "--experimental":
        args.experimental = true;
        break;
      case "--no-experimental":
        args.experimental = false;
        break;
      default:
        throw new Error(`Unknown argument: ${arg}`);
    }
  }
  return args;
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
      clientInfo: { name: "codex-history-recovery-scan", version: "1.0.0" },
      protocolVersion: 1,
      capabilities: experimental ? { experimentalApi: true } : {},
    });
  }

  call(method, params = {}, timeoutMs = 120000) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`timeout calling ${method}`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer, method });
      this.ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
    });
  }

  close() {
    if (this.ws) this.ws.close();
  }
}

function rowsFrom(result) {
  if (Array.isArray(result)) return result;
  if (Array.isArray(result?.data)) return result.data;
  if (Array.isArray(result?.threads)) return result.threads;
  return [];
}

function nextCursorFrom(result) {
  return result?.nextCursor || result?.next_cursor || null;
}

function countBy(rows, fn) {
  const counts = {};
  for (const row of rows) {
    const key = fn(row);
    counts[key] = (counts[key] || 0) + 1;
  }
  return Object.fromEntries(Object.entries(counts).sort(([a], [b]) => a.localeCompare(b)));
}

async function listAll(client, params, pageSize) {
  const rows = [];
  let cursor = null;
  let pages = 0;
  do {
    const result = await client.call("thread/list", { ...params, cursor, limit: pageSize }, 180000);
    rows.push(...rowsFrom(result));
    cursor = nextCursorFrom(result);
    pages += 1;
  } while (cursor);
  return { rows, pages };
}

const args = parseArgs(process.argv.slice(2));
if (args.help) {
  usage();
  process.exit(0);
}

const baseParams = {
  useStateDbOnly: args.useStateDbOnly,
};
if (args.modelProviders) baseParams.modelProviders = args.modelProviders;
if (args.sourceKinds) baseParams.sourceKinds = args.sourceKinds;

const client = new RpcClient(args.wsUrl);
await client.connect(args.experimental);
try {
  const active = await listAll(client, { ...baseParams, archived: false }, args.limit);
  const archived = await listAll(client, { ...baseParams, archived: true }, args.limit);
  const allRows = [...active.rows, ...archived.rows];
  const result = {
    wsUrl: args.wsUrl,
    scannedRollouts: !args.useStateDbOnly,
    modelProvidersFilter: args.modelProviders || null,
    sourceKindsFilter: args.sourceKinds || null,
    active: {
      count: active.rows.length,
      pages: active.pages,
      byModelProvider: countBy(active.rows, (row) => row.modelProvider || row.model_provider || ""),
    },
    archived: {
      count: archived.rows.length,
      pages: archived.pages,
      byModelProvider: countBy(archived.rows, (row) => row.modelProvider || row.model_provider || ""),
    },
    total: allRows.length,
    byArchivedAndProvider: countBy(
      allRows,
      (row) => `${row.archived ? "archived" : "active"}|${row.modelProvider || row.model_provider || ""}`,
    ),
    sampleIds: allRows.slice(0, 20).map((row) => row.id || row.threadId || row.thread_id || ""),
  };
  if (args.output) {
    const output = path.resolve(args.output);
    fs.mkdirSync(path.dirname(output), { recursive: true });
    fs.writeFileSync(output, `${JSON.stringify(result, null, 2)}\n`, "utf8");
  }
  console.log(JSON.stringify(result, null, 2));
} finally {
  client.close();
}
