#!/usr/bin/env python3
"""Audit, back up, and validate Codex desktop thread history.

This script is intentionally conservative: audit mode does not change Codex state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


STATE_DB = "state_5.sqlite"
SESSION_INDEX = "session_index.jsonl"
COPY_FILES = [
    SESSION_INDEX,
    "auth.json",
    "config.toml",
    ".codex-global-state.json",
    "version.json",
]
SQLITE_DBS = [
    "state_5.sqlite",
    "memories_1.sqlite",
    "goals_1.sqlite",
    "logs_2.sqlite",
]
ROLLOUT_DIRS = [
    "sessions",
    "archived_sessions",
]
UUID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    flags=re.IGNORECASE,
)
PLACEHOLDER_TITLES = {
    "",
    "new conversation",
    "new chat",
    "untitled",
    "untitled conversation",
    "新对话",
    "新会话",
    "未命名",
}


def eprint(*parts: object) -> None:
    print(*parts, file=sys.stderr)


def json_dump(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def now_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S-%f")


def make_timestamp_dir(root: Path, prefix: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    for index in range(100):
        suffix = now_stamp() if index == 0 else f"{now_stamp()}-{index}"
        candidate = root / f"{prefix}-{suffix}"
        try:
            candidate.mkdir(parents=True, exist_ok=False)
            return candidate
        except FileExistsError:
            continue
    raise SystemExit(f"Could not create a unique directory under {root}")


def expand_path(raw: str | None) -> Path | None:
    if not raw:
        return None
    return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()


def short_text(value: object, limit: int = 100) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def public_thread_sample(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row.get("id") or "",
        "model_provider": row.get("model_provider") or row.get("sourceProvider") or "",
        "model": row.get("model") or row.get("sourceModel") or "",
        "archived": row.get("archived"),
        "source": short_text(row.get("source") or "", 80),
        "thread_source": row.get("thread_source") or row.get("threadSource") or "",
        "title": short_text(row.get("title") or row.get("sourceTitle") or "", 100),
        "cwd": short_text(row.get("cwd") or row.get("sourceCwd") or "", 140),
    }


def doctor_codex_home_candidates() -> list[Path]:
    """Best-effort CODEX_HOME discovery from `codex doctor --json` output.

    The doctor JSON shape may change across Codex versions, so this function
    recursively inspects string values and accepts either a directory containing
    state_5.sqlite or a direct path to state_5.sqlite. It never assumes a
    user-specific custom path.
    """
    try:
        proc = subprocess.run(
            ["codex", "doctor", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return []
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return []

    found: list[Path] = []

    def consider(value: str) -> None:
        expanded = expand_path(value)
        if not expanded:
            return
        candidates = []
        if expanded.name == STATE_DB:
            candidates.append(expanded.parent)
        candidates.append(expanded)
        for candidate in candidates:
            if (candidate / STATE_DB).exists():
                found.append(candidate)

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, str):
            consider(value)

    walk(data)
    return found


def discover_codex_home(explicit: str | None) -> Path:
    candidates: list[Path] = []
    explicit_path = expand_path(explicit)
    if explicit_path:
        candidates.append(explicit_path)
    env_home = expand_path(os.environ.get("CODEX_HOME"))
    if env_home:
        candidates.append(env_home)
    candidates.extend(doctor_codex_home_candidates())

    standard_home = expand_path(str(Path.home() / ".codex"))
    if standard_home:
        candidates.append(standard_home)

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if (candidate / STATE_DB).exists():
            return candidate

    raise SystemExit(
        "Could not find Codex state_5.sqlite. Pass --codex-home explicitly."
    )


def connect_db(codex_home: Path, readonly: bool = True) -> sqlite3.Connection:
    db_path = codex_home / STATE_DB
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"pragma table_info({table})")}


def query_threads(
    conn: sqlite3.Connection,
    archived: str,
    source_providers: set[str] | None,
    search: str | None,
    include_subagents: bool,
) -> list[dict[str, Any]]:
    cols = table_columns(conn, "threads")
    wanted = [
        "id",
        "model_provider",
        "model",
        "title",
        "cwd",
        "source",
        "thread_source",
        "created_at",
        "updated_at",
        "archived",
    ]
    select_parts = []
    for name in wanted:
        if name in cols:
            select_parts.append(f"coalesce({name}, '') as {name}" if name != "archived" else name)
        else:
            select_parts.append(f"'' as {name}")

    where: list[str] = []
    params: list[Any] = []
    if archived == "active":
        where.append("archived = 0")
    elif archived == "archived":
        where.append("archived = 1")
    elif archived != "all":
        raise ValueError(f"unknown archived mode: {archived}")

    if source_providers:
        placeholders = ",".join("?" for _ in source_providers)
        where.append(f"coalesce(model_provider, '') in ({placeholders})")
        params.extend(sorted(source_providers))

    if search:
        where.append("(coalesce(title, '') like ? or coalesce(cwd, '') like ? or id like ?)")
        needle = f"%{search}%"
        params.extend([needle, needle, needle])

    if not include_subagents:
        where.append("coalesce(thread_source, 'user') <> 'subagent'")
        where.append("coalesce(source, '') not like '%\"subagent\"%'")

    sql = f"select {', '.join(select_parts)} from threads"
    if where:
        sql += " where " + " and ".join(where)
    sql += " order by archived asc, updated_at desc, created_at desc, id"
    return [dict(row) for row in conn.execute(sql, params)]


def summarize(rows: list[dict[str, Any]], codex_home: Path) -> dict[str, Any]:
    by_provider_archived: Counter[str] = Counter()
    by_source: Counter[str] = Counter()
    by_thread_source: Counter[str] = Counter()
    for row in rows:
        provider = row.get("model_provider") or ""
        archived = row.get("archived")
        by_provider_archived[f"{provider}|archived={archived}"] += 1
        by_source[str(row.get("source") or "")] += 1
        by_thread_source[str(row.get("thread_source") or "")] += 1
    return {
        "codexHome": str(codex_home),
        "totalCandidates": len(rows),
        "byProviderAndArchived": dict(sorted(by_provider_archived.items())),
        "bySource": dict(sorted(by_source.items())),
        "byThreadSource": dict(sorted(by_thread_source.items())),
        "sample": [public_thread_sample(row) for row in rows[:10]],
    }


def sqlite_backup(src: Path, dst: Path) -> None:
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as source:
        with sqlite3.connect(dst) as target:
            source.backup(target)


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def make_backup(codex_home: Path, backup_root: Path, full_backup: bool) -> Path:
    backup_dir = make_timestamp_dir(backup_root, "codex-history-recovery")

    for name in SQLITE_DBS:
        src = codex_home / name
        if src.exists():
            sqlite_backup(src, backup_dir / name)
            for suffix in ("-wal", "-shm"):
                sidecar = codex_home / f"{name}{suffix}"
                if sidecar.exists():
                    shutil.copy2(sidecar, backup_dir / sidecar.name)

    for name in COPY_FILES:
        src = codex_home / name
        if src.exists():
            shutil.copy2(src, backup_dir / name)

    if full_backup:
        for name in ROLLOUT_DIRS:
            src = codex_home / name
            if src.exists():
                copy_tree(src, backup_dir / name)

    return backup_dir


def run_doctor(codex_home: Path, output_path: Path) -> dict[str, Any] | None:
    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    try:
        proc = subprocess.run(
            ["codex", "doctor", "--json"],
            env=env,
            capture_output=True,
            text=True,
            timeout=180,
        )
    except Exception as exc:
        output_path.write_text(str(exc), encoding="utf-8")
        return None

    output_path.write_text(proc.stdout or proc.stderr, encoding="utf-8")
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def normalize_rollout_path(raw: str | None) -> Path | None:
    if not raw:
        return None
    text = str(raw)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    try:
        return Path(text)
    except Exception:
        return None


def candidate_rollout_paths(codex_home: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Find rollout JSONL files by DB path when present, then by active/archive folders."""
    by_id = {row["id"]: row for row in rows}
    found: dict[str, dict[str, Any]] = {}
    db_paths: dict[str, str] = {}

    with connect_db(codex_home, readonly=True) as conn:
        cols = table_columns(conn, "threads")
        path_cols = [name for name in ["path", "rollout_path", "jsonl_path"] if name in cols]
        if path_cols and by_id:
            placeholders = ",".join("?" for _ in by_id)
            sql = f"select id, {', '.join(path_cols)} from threads where id in ({placeholders})"
            for rec in conn.execute(sql, list(by_id)):
                for name in path_cols:
                    value = rec[name]
                    if value:
                        db_paths[rec["id"]] = value
                        break

    for thread_id, row in by_id.items():
        paths: list[Path] = []
        db_path = normalize_rollout_path(db_paths.get(thread_id))
        if db_path:
            paths.append(db_path if db_path.is_absolute() else codex_home / db_path)
        for folder in ROLLOUT_DIRS:
            paths.extend((codex_home / folder).rglob(f"*{thread_id}*.jsonl"))
        for p in paths:
            if p.exists() and p.is_file():
                try:
                    rel = p.relative_to(codex_home)
                except ValueError:
                    rel = Path("external_rollouts") / p.name
                found[thread_id] = {
                    "id": thread_id,
                    "relativePath": rel.as_posix(),
                    "absolutePath": str(p),
                    "archived": bool(row.get("archived")),
                    "sourceProvider": row.get("model_provider") or row.get("sourceProvider") or "",
                    "title": row.get("title") or "",
                    "size": p.stat().st_size,
                    "sha256": sha256_file(p),
                }
                break

    missing = sorted(set(by_id) - set(found))
    return [found[key] for key in sorted(found)] + [
        {"id": thread_id, "missing": True} for thread_id in missing
    ]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def rollout_thread_id(path: Path) -> str | None:
    match = re.search(
        r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
        path.name,
        flags=re.IGNORECASE,
    )
    return match.group(1).lower() if match else None


def find_rollouts_by_thread_id(codex_home: Path, thread_id: str) -> list[Path]:
    found: list[Path] = []
    for folder in ROLLOUT_DIRS:
        root = codex_home / folder
        if root.exists():
            found.extend(path for path in root.rglob(f"*{thread_id}*.jsonl") if path.is_file())
    return sorted(set(found), key=lambda p: str(p).lower())


def quarantine_path_for(quarantine_dir: Path, codex_home: Path, source: Path) -> Path:
    try:
        rel = source.relative_to(codex_home)
    except ValueError:
        rel = Path(source.name)
    return quarantine_dir / rel


def quarantine_file(source: Path, destination: Path) -> dict[str, Any]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        stem = destination.stem
        suffix = destination.suffix
        for index in range(1, 1000):
            candidate = destination.with_name(f"{stem}.{index}{suffix}")
            if not candidate.exists():
                destination = candidate
                break
    shutil.move(str(source), str(destination))
    return {
        "source": str(source),
        "destination": str(destination),
        "size": destination.stat().st_size,
        "sha256": sha256_file(destination),
    }


def quarantine_duplicate_rollouts(codex_home: Path, backup_root: Path, execute: bool) -> dict[str, Any]:
    by_id: dict[str, list[Path]] = {}
    for folder in ROLLOUT_DIRS:
        root = codex_home / folder
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            thread_id = rollout_thread_id(path)
            if thread_id:
                by_id.setdefault(thread_id, []).append(path)

    duplicates = {
        thread_id: sorted(paths, key=lambda p: (0 if "archived_sessions" in p.parts else 1, str(p).lower()))
        for thread_id, paths in by_id.items()
        if len(paths) > 1
    }
    plan = []
    for thread_id, paths in duplicates.items():
        keep = paths[0]
        for path in paths[1:]:
            plan.append({
                "threadId": thread_id,
                "keep": str(keep),
                "quarantine": str(path),
                "reason": "duplicate thread id rollout; keeping the first archived/original-looking path",
            })

    quarantine_dir = None
    moved = []
    if execute and plan:
        quarantine_dir = make_timestamp_dir(backup_root, "duplicate-rollout-quarantine")
        for item in plan:
            source = Path(item["quarantine"])
            if not source.exists():
                continue
            destination = quarantine_path_for(quarantine_dir, codex_home, source)
            moved.append({**item, **quarantine_file(source, destination)})

    return {
        "codexHome": str(codex_home),
        "execute": execute,
        "duplicateThreadIds": len(duplicates),
        "plannedQuarantineCount": len(plan),
        "quarantineDir": str(quarantine_dir) if quarantine_dir else None,
        "planSample": plan[:20],
        "moved": moved,
    }


def write_migration_package(
    codex_home: Path,
    candidates_path: Path,
    output_zip: Path,
    package_name: str | None,
    include_metadata: bool,
) -> dict[str, Any]:
    raw = json.loads(candidates_path.read_text(encoding="utf-8"))
    rows = raw if isinstance(raw, list) else raw.get("candidates", [])
    if not isinstance(rows, list):
        raise SystemExit("Candidates JSON must be a list or { candidates: [] }")
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        normalized.append(
            {
                "id": row.get("id") or row.get("threadId") or row.get("sourceThreadId"),
                "model_provider": row.get("sourceProvider") or row.get("model_provider") or "",
                "title": row.get("title") or row.get("sourceTitle") or "",
                "cwd": row.get("cwd") or row.get("sourceCwd") or "",
                "archived": bool(row.get("archived")),
            }
        )
    normalized = [row for row in normalized if row["id"]]
    rollouts = candidate_rollout_paths(codex_home, normalized)
    present = [item for item in rollouts if not item.get("missing")]
    missing = [item for item in rollouts if item.get("missing")]

    manifest = {
        "schemaVersion": 1,
        "packageKind": "codex-history-migration",
        "packageName": package_name or f"codex-history-migration-{now_stamp()}",
        "createdAt": datetime.now().isoformat(),
        "sourceCodexHome": str(codex_home),
        "threadCount": len(normalized),
        "rolloutCount": len(present),
        "missingRolloutCount": len(missing),
        "rollouts": present,
        "missingRollouts": missing,
        "notes": [
            "This package intentionally excludes auth.json and API keys.",
            "Import on another device, run thread/list or audit, then fork to the target provider if needed.",
        ],
    }

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        zf.writestr("thread-candidates.json", json.dumps(rows, ensure_ascii=False, indent=2))
        if include_metadata:
            for name in ["config.toml", ".codex-global-state.json", "version.json"]:
                p = codex_home / name
                if p.exists():
                    zf.write(p, f"metadata/{name}")
        for item in present:
            src = Path(item["absolutePath"])
            zf.write(src, f"rollouts/{item['relativePath']}")

    manifest["packagePath"] = str(output_zip)
    return manifest


def backup_before_import(codex_home: Path, backup_root: Path) -> Path:
    backup_dir = make_timestamp_dir(backup_root, "codex-history-import-before")
    for name in SQLITE_DBS:
        src = codex_home / name
        if src.exists():
            sqlite_backup(src, backup_dir / name)
    for name in ROLLOUT_DIRS:
        src = codex_home / name
        if src.exists():
            manifest = []
            for file in src.rglob("*.jsonl"):
                manifest.append({"path": str(file.relative_to(codex_home)), "size": file.stat().st_size})
            json_dump(backup_dir / f"{name}-manifest.json", manifest)
    return backup_dir


def import_migration_package(
    codex_home: Path,
    package_path: Path,
    backup_root: Path,
    execute: bool,
    overwrite: bool,
    allow_duplicate_thread_ids: bool,
) -> dict[str, Any]:
    with zipfile.ZipFile(package_path, "r") as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))
        rollout_entries = [
            name for name in zf.namelist()
            if name.startswith("rollouts/") and name.endswith(".jsonl")
        ]
        plan = []
        for entry in rollout_entries:
            rel = Path(entry[len("rollouts/"):])
            dest = codex_home / rel
            resolved_dest = dest.resolve()
            if not resolved_dest.is_relative_to(codex_home.resolve()):
                raise SystemExit(f"Refusing unsafe zip path outside Codex home: {entry}")
            thread_id = rollout_thread_id(resolved_dest)
            existing_same_thread = []
            if thread_id:
                existing_same_thread = [
                    str(path.resolve())
                    for path in find_rollouts_by_thread_id(codex_home, thread_id)
                    if path.resolve() != resolved_dest
                ]
            duplicate_blocked = bool(existing_same_thread) and not allow_duplicate_thread_ids
            plan.append({
                "zipEntry": entry,
                "destination": str(resolved_dest),
                "threadId": thread_id,
                "exists": resolved_dest.exists(),
                "existingSameThreadRollouts": existing_same_thread,
                "duplicateBlocked": duplicate_blocked,
                "willWrite": (overwrite or not resolved_dest.exists()) and not duplicate_blocked,
            })

        backup_dir = None
        if execute:
            backup_dir = backup_before_import(codex_home, backup_root)
            for item in plan:
                if not item["willWrite"]:
                    continue
                dest = Path(item["destination"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(item["zipEntry"], "r") as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)

    return {
        "package": str(package_path),
        "codexHome": str(codex_home),
        "execute": execute,
        "overwrite": overwrite,
        "backupDir": str(backup_dir) if backup_dir else None,
        "packageThreadCount": manifest.get("threadCount"),
        "packageRolloutCount": manifest.get("rolloutCount"),
        "plannedWrites": sum(1 for item in plan if item["willWrite"]),
        "skippedExisting": sum(1 for item in plan if item["exists"] and not item["willWrite"]),
        "skippedDuplicateThreadIds": sum(1 for item in plan if item["duplicateBlocked"]),
        "planSample": plan[:20],
        "nextSteps": [
            "Restart Codex or call thread/list with useStateDbOnly=false so the app scans imported rollouts.",
            "If duplicateThreadIds were skipped and you truly need both copies, rerun with --allow-duplicate-thread-ids or export one copy under a new fork.",
            "Run audit again, then fork imported source providers into the current provider if the sidebar still filters them out.",
        ],
    }


def cmd_audit(args: argparse.Namespace) -> int:
    codex_home = discover_codex_home(args.codex_home)
    backup_root = expand_path(args.backup_root) or (codex_home / "backups")
    backup_root.mkdir(parents=True, exist_ok=True)
    backup_dir = make_backup(codex_home, backup_root, args.full_backup)

    providers = None
    if args.source_providers:
        providers = {part.strip() for part in args.source_providers.split(",") if part.strip()}

    with connect_db(codex_home, readonly=True) as conn:
        rows = query_threads(
            conn,
            archived=args.archived,
            source_providers=providers,
            search=args.search,
            include_subagents=args.include_subagents,
        )

    candidates = []
    for row in rows:
        candidates.append(
            {
                "id": row["id"],
                "sourceProvider": row.get("model_provider") or "",
                "sourceModel": row.get("model") or "",
                "title": row.get("title") or "",
                "cwd": row.get("cwd") or "",
                "source": row.get("source") or "",
                "threadSource": row.get("thread_source") or "",
                "archived": bool(row.get("archived")),
                "createdAt": row.get("created_at") or "",
                "updatedAt": row.get("updated_at") or "",
            }
        )

    summary = summarize(rows, codex_home)
    summary["backupDir"] = str(backup_dir)
    summary["targetProviderHint"] = args.target_provider

    json_dump(backup_dir / "thread-candidates.json", candidates)
    json_dump(backup_dir / "thread-inventory-summary.json", summary)

    if args.doctor:
        doctor = run_doctor(codex_home, backup_dir / "doctor-before.json")
        summary["doctorOverallStatus"] = doctor.get("overallStatus") if doctor else None
        json_dump(backup_dir / "thread-inventory-summary.json", summary)

    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def load_mapping(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(data, dict) and "results" in data:
        data = data["results"]
    elif isinstance(data, dict) and any(key in data for key in ["succeeded", "failed", "skipped"]):
        rows = []
        for key in ["succeeded", "failed", "skipped"]:
            values = data.get(key)
            if isinstance(values, list):
                rows.extend(item for item in values if isinstance(item, dict))
        data = rows
    if not isinstance(data, list):
        raise SystemExit("Mapping must be a list or an object with results[].")
    return [item for item in data if isinstance(item, dict)]


def normalize_title(value: object, max_length: int = 160) -> str:
    title = re.sub(r"\s+", " ", str(value or "")).strip()
    if not title:
        return ""
    if max_length > 0 and len(title) > max_length:
        return title[: max(0, max_length - 1)] + "…"
    return title


def is_placeholder_title(value: object) -> bool:
    return normalize_title(value, 0).lower() in PLACEHOLDER_TITLES


def iso_now_z() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def epoch_to_iso_z(value: object) -> str:
    try:
        raw = int(value)
    except (TypeError, ValueError):
        return iso_now_z()
    if raw > 10_000_000_000:
        seconds = raw / 1000
    else:
        seconds = raw
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return iso_now_z()


def read_session_index(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            entries.append({"_raw": line})
            continue
        if isinstance(item, dict):
            entries.append(item)
    return entries


def write_session_index(path: Path, entries: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for item in entries:
        if "_raw" in item:
            lines.append(str(item["_raw"]))
        else:
            lines.append(json.dumps(item, ensure_ascii=False, separators=(",", ":")))
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def read_session_index_titles(path: Path) -> dict[str, list[dict[str, Any]]]:
    titles: dict[str, list[dict[str, Any]]] = {}
    for item in read_session_index(path):
        thread_id = item.get("id")
        title = normalize_title(item.get("thread_name"), 0)
        if thread_id and title:
            titles.setdefault(str(thread_id), []).append({
                "title": title,
                "kind": "session_index",
                "path": str(path),
            })
    return titles


def read_db_titles(path: Path, kind: str = "state_db") -> dict[str, list[dict[str, Any]]]:
    titles: dict[str, list[dict[str, Any]]] = {}
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        has_threads = conn.execute(
            "select name from sqlite_master where type='table' and name='threads'"
        ).fetchone()
        if not has_threads:
            return titles
        cols = {row["name"] for row in conn.execute("pragma table_info(threads)")}
        if "id" not in cols or "title" not in cols:
            return titles
        for row in conn.execute("select id, coalesce(title, '') as title from threads"):
            title = normalize_title(row["title"], 0)
            if row["id"] and title:
                titles.setdefault(str(row["id"]), []).append({
                    "title": title,
                    "kind": kind,
                    "path": str(path),
                })
    except sqlite3.Error:
        return titles
    return titles


def add_title_evidence(
    target: dict[str, list[dict[str, Any]]],
    thread_id: object,
    title: object,
    kind: str,
    path: object,
    extra: dict[str, Any] | None = None,
) -> None:
    tid = str(thread_id or "").strip()
    name = normalize_title(title, 0)
    if not tid or not name:
        return
    item = {"title": name, "kind": kind, "path": str(path)}
    if extra:
        item.update(extra)
    target.setdefault(tid, []).append(item)


def merge_title_evidence(
    target: dict[str, list[dict[str, Any]]],
    source: dict[str, list[dict[str, Any]]],
    kind_override: str | None = None,
) -> None:
    for thread_id, values in source.items():
        for item in values:
            copied = dict(item)
            if kind_override:
                copied["kind"] = kind_override
            target.setdefault(thread_id, []).append(copied)


def evidence_roots_from_args(raw_roots: list[str] | None) -> list[Path]:
    roots: list[Path] = []
    for raw in raw_roots or []:
        p = expand_path(raw)
        if p:
            roots.append(p)
    seen: set[Path] = set()
    result = []
    for root in roots:
        if root not in seen and root.exists():
            seen.add(root)
            result.append(root)
    return result


def iter_evidence_files(roots: list[Path], names: set[str]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        candidates = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
        for path in candidates:
            if path.name in names and path not in seen:
                seen.add(path)
                files.append(path)
    return sorted(files, key=lambda p: (p.name, str(p).lower()))


def load_title_evidence(
    codex_home: Path,
    evidence_roots: list[Path],
    mapping_paths: list[Path],
    include_current_db_fallback: bool,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, str]]], dict[str, Any]]:
    by_id: dict[str, list[dict[str, Any]]] = {}
    source_for_fork: dict[str, list[dict[str, str]]] = {}

    evidence_files = iter_evidence_files(
        evidence_roots,
        {
            SESSION_INDEX,
            STATE_DB,
            "thread-candidates.json",
            "fork-results.json",
        },
    )
    index_files = [codex_home / SESSION_INDEX] + [p for p in evidence_files if p.name == SESSION_INDEX]
    db_files = [p for p in evidence_files if p.name == STATE_DB]
    if include_current_db_fallback:
        db_files = [codex_home / STATE_DB] + db_files

    for path in index_files:
        if path.exists():
            merge_title_evidence(by_id, read_session_index_titles(path))

    for path in db_files:
        if path.exists():
            kind = "current_state_db" if path.resolve() == (codex_home / STATE_DB).resolve() else "state_db"
            merge_title_evidence(by_id, read_db_titles(path, kind=kind))

    candidate_files = [p for p in evidence_files if p.name == "thread-candidates.json"]
    for path in candidate_files:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        rows = data if isinstance(data, list) else data.get("candidates", [])
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            add_title_evidence(
                by_id,
                row.get("id") or row.get("threadId") or row.get("sourceThreadId"),
                row.get("title") or row.get("sourceTitle"),
                "thread_candidates",
                path,
            )

    mapping_files = [p for p in evidence_files if p.name == "fork-results.json"]
    for path in mapping_paths:
        if path.exists() and path not in mapping_files:
            mapping_files.append(path)

    for path in mapping_files:
        rows = load_mapping(path)
        for row in rows:
            source_id = row.get("sourceThreadId")
            fork_id = row.get("forkThreadId") or row.get("targetThreadId")
            if fork_id and source_id:
                source_for_fork.setdefault(str(fork_id), []).append({
                    "sourceThreadId": str(source_id),
                    "path": str(path),
                    "kind": "fork_results",
                })
            title = row.get("title") or row.get("sourceTitle")
            if fork_id and title:
                add_title_evidence(by_id, fork_id, title, "fork_results_direct", path)
            if source_id and title:
                add_title_evidence(by_id, source_id, title, "fork_results_source", path)

    stats = {
        "evidenceRoots": [str(p) for p in evidence_roots],
        "sessionIndexFiles": len([p for p in index_files if p.exists()]),
        "stateDbFiles": len([p for p in db_files if p.exists()]),
        "threadCandidateFiles": len(candidate_files),
        "forkResultFiles": len(mapping_files),
        "titleEvidenceThreadIds": len(by_id),
        "forkSourceLinks": len(source_for_fork),
    }
    return by_id, source_for_fork, stats


def load_current_threads(codex_home: Path) -> dict[str, dict[str, Any]]:
    with connect_db(codex_home, readonly=True) as conn:
        cols = table_columns(conn, "threads")
        wanted = [
            "id",
            "title",
            "first_user_message",
            "model_provider",
            "model",
            "rollout_path",
            "archived",
            "updated_at",
            "updated_at_ms",
        ]
        parts = []
        for name in wanted:
            if name in cols:
                parts.append(f"coalesce({name}, '') as {name}" if name != "archived" else name)
            else:
                parts.append(f"'' as {name}")
        return {row["id"]: dict(row) for row in conn.execute(f"select {', '.join(parts)} from threads")}


def infer_source_links_from_rollouts(
    codex_home: Path,
    current_threads: dict[str, dict[str, Any]],
    source_for_fork: dict[str, list[dict[str, str]]],
    target_providers: set[str] | None,
) -> int:
    inferred = 0
    for thread_id, row in current_threads.items():
        if thread_id in source_for_fork:
            continue
        if target_providers and str(row.get("model_provider") or "") not in target_providers:
            continue
        rollout_path = normalize_rollout_path(str(row.get("rollout_path") or ""))
        if not rollout_path:
            continue
        path = rollout_path if rollout_path.is_absolute() else codex_home / rollout_path
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        hits = {match.lower() for match in UUID_RE.findall(text)}
        hits.discard(thread_id.lower())
        old_ids = [
            candidate
            for candidate in hits
            if candidate in current_threads
            and str(current_threads[candidate].get("model_provider") or "") != str(row.get("model_provider") or "")
        ]
        if len(old_ids) == 1:
            source_for_fork.setdefault(thread_id, []).append({
                "sourceThreadId": old_ids[0],
                "path": str(path),
                "kind": "rollout_uuid_unique",
            })
            inferred += 1
    return inferred


TITLE_KIND_PRIORITY = {
    "fork_results_direct": 0,
    "source_session_index": 1,
    "source_thread_candidates": 2,
    "source_fork_results_source": 3,
    "source_state_db": 4,
    "session_index": 5,
    "thread_candidates": 6,
    "fork_results_source": 7,
    "state_db": 8,
    "current_state_db": 9,
}
TITLE_CONFIDENCE_KINDS = {
    "strong": {
        "fork_results_direct",
        "session_index",
        "thread_candidates",
        "source_session_index",
        "source_thread_candidates",
        "source_fork_results_source",
    },
    "medium": {
        "fork_results_direct",
        "session_index",
        "thread_candidates",
        "source_session_index",
        "source_thread_candidates",
        "source_fork_results_source",
        "source_state_db",
        "state_db",
    },
    "all": set(TITLE_KIND_PRIORITY),
}


def choose_title_evidence(values: list[dict[str, Any]], max_title_length: int) -> dict[str, Any] | None:
    candidates = []
    for item in values:
        title = normalize_title(item.get("title"), max_title_length)
        if not title:
            continue
        copied = dict(item)
        copied["title"] = title
        candidates.append(copied)
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            TITLE_KIND_PRIORITY.get(str(item.get("kind") or ""), 99),
            len(str(item.get("title") or "")) > 140,
            str(item.get("title") or ""),
        )
    )
    return candidates[0]


def build_title_repair_plan(
    codex_home: Path,
    evidence_roots: list[Path],
    mapping_paths: list[Path],
    target_providers: set[str] | None,
    max_title_length: int,
    include_current_db_fallback: bool,
    infer_from_rollout: bool,
    only_missing_index: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    current_threads = load_current_threads(codex_home)
    current_index = {
        str(item.get("id")): normalize_title(item.get("thread_name"), 0)
        for item in read_session_index(codex_home / SESSION_INDEX)
        if item.get("id")
    }
    evidence_by_id, source_for_fork, stats = load_title_evidence(
        codex_home=codex_home,
        evidence_roots=evidence_roots,
        mapping_paths=mapping_paths,
        include_current_db_fallback=include_current_db_fallback,
    )
    inferred = infer_source_links_from_rollouts(
        codex_home,
        current_threads,
        source_for_fork,
        target_providers,
    ) if infer_from_rollout else 0

    plan: list[dict[str, Any]] = []
    for thread_id, row in current_threads.items():
        provider = str(row.get("model_provider") or "")
        if target_providers and provider not in target_providers:
            continue
        current_title = normalize_title(row.get("title"), 0)
        current_index_title = current_index.get(thread_id, "")
        if only_missing_index and current_index_title:
            continue

        candidates = [dict(item) for item in evidence_by_id.get(thread_id, [])]
        for link in source_for_fork.get(thread_id, []):
            source_id = link["sourceThreadId"]
            for item in evidence_by_id.get(source_id, []):
                copied = dict(item)
                copied["kind"] = f"source_{copied.get('kind')}"
                copied["sourceThreadId"] = source_id
                copied["sourceEvidence"] = link.get("path")
                copied["sourceEvidenceKind"] = link.get("kind")
                candidates.append(copied)

        chosen = choose_title_evidence(candidates, max_title_length)
        if not chosen:
            continue
        desired = normalize_title(chosen["title"], max_title_length)
        if not desired:
            continue

        update_index = current_index_title != desired
        reliable_for_db = (
            str(chosen.get("kind")) in {
                "fork_results_direct",
                "session_index",
                "thread_candidates",
                "source_session_index",
                "source_thread_candidates",
                "source_fork_results_source",
                "source_state_db",
            }
            or bool(chosen.get("sourceThreadId"))
        )
        update_db = (
            current_title != desired
            and str(chosen.get("kind")) != "current_state_db"
            and (
                reliable_for_db
                or is_placeholder_title(current_title)
                or is_placeholder_title(current_index_title)
                or not current_index_title
            )
        )
        if not update_index and not update_db:
            continue

        plan.append({
            "threadId": thread_id,
            "modelProvider": provider,
            "archived": bool(row.get("archived")),
            "updatedAt": row.get("updated_at_ms") or row.get("updated_at"),
            "currentTitle": current_title,
            "currentIndexTitle": current_index_title,
            "desiredTitle": desired,
            "evidenceKind": chosen.get("kind"),
            "evidencePath": chosen.get("path"),
            "sourceThreadId": chosen.get("sourceThreadId"),
            "sourceEvidence": chosen.get("sourceEvidence"),
            "sourceEvidenceKind": chosen.get("sourceEvidenceKind"),
            "updateStateDbTitle": update_db,
            "updateSessionIndex": update_index,
        })

    stats.update({
        "codexHome": str(codex_home),
        "currentThreads": len(current_threads),
        "currentSessionIndexEntries": len(current_index),
        "currentThreadsMissingSessionIndex": sum(1 for tid in current_threads if tid not in current_index),
        "inferredSourceLinksFromRollouts": inferred,
    })
    return plan, stats


def backup_before_title_repair(codex_home: Path, backup_root: Path) -> Path:
    backup_dir = make_timestamp_dir(backup_root, "codex-title-repair-before")
    db_path = codex_home / STATE_DB
    if db_path.exists():
        sqlite_backup(db_path, backup_dir / STATE_DB)
        for suffix in ("-wal", "-shm"):
            sidecar = codex_home / f"{STATE_DB}{suffix}"
            if sidecar.exists():
                shutil.copy2(sidecar, backup_dir / sidecar.name)
    index_path = codex_home / SESSION_INDEX
    if index_path.exists():
        shutil.copy2(index_path, backup_dir / SESSION_INDEX)
    return backup_dir


def apply_title_repair(codex_home: Path, plan: list[dict[str, Any]]) -> dict[str, Any]:
    state_updates = [item for item in plan if item.get("updateStateDbTitle")]
    index_updates = [item for item in plan if item.get("updateSessionIndex")]

    if state_updates:
        with connect_db(codex_home, readonly=False) as conn:
            conn.execute("pragma busy_timeout=10000")
            for item in state_updates:
                conn.execute(
                    "update threads set title = ? where id = ?",
                    (item["desiredTitle"], item["threadId"]),
                )
            conn.commit()

    if index_updates:
        index_path = codex_home / SESSION_INDEX
        entries = read_session_index(index_path)
        desired_by_id = {item["threadId"]: item["desiredTitle"] for item in index_updates}
        seen: set[str] = set()
        updated_at_by_id = {
            item["threadId"]: epoch_to_iso_z(item.get("updatedAt"))
            for item in index_updates
        }
        rewritten: list[dict[str, Any]] = []
        for item in entries:
            thread_id = item.get("id")
            if not thread_id:
                rewritten.append(item)
                continue
            thread_id = str(thread_id)
            if thread_id in seen:
                continue
            seen.add(thread_id)
            if thread_id in desired_by_id:
                item = dict(item)
                item["thread_name"] = desired_by_id[thread_id]
                item["updated_at"] = item.get("updated_at") or updated_at_by_id.get(thread_id) or iso_now_z()
            rewritten.append(item)

        for thread_id, title in desired_by_id.items():
            if thread_id not in seen:
                rewritten.append({
                    "id": thread_id,
                    "thread_name": title,
                    "updated_at": updated_at_by_id.get(thread_id) or iso_now_z(),
                })
        write_session_index(index_path, rewritten)

    return {
        "stateDbTitleUpdates": len(state_updates),
        "sessionIndexUpdates": len(index_updates),
    }


def cmd_validate(args: argparse.Namespace) -> int:
    codex_home = discover_codex_home(args.codex_home)
    mapping_path = expand_path(args.mapping)
    if not mapping_path or not mapping_path.exists():
        raise SystemExit("--mapping does not exist")
    rows = load_mapping(mapping_path)
    ok_rows = [row for row in rows if row.get("status") == "ok"]
    source_ids = [row.get("sourceThreadId") for row in ok_rows if row.get("sourceThreadId")]
    fork_ids = [row.get("forkThreadId") for row in ok_rows if row.get("forkThreadId")]
    all_ids = source_ids + fork_ids

    found: dict[str, dict[str, Any]] = {}
    if all_ids:
        with connect_db(codex_home, readonly=True) as conn:
            placeholders = ",".join("?" for _ in all_ids)
            sql = (
                "select id, archived, coalesce(model_provider,'') as model_provider, "
                "coalesce(model,'') as model, coalesce(title,'') as title, "
                "coalesce(cwd,'') as cwd, coalesce(source,'') as source, "
                "coalesce(thread_source,'') as thread_source from threads "
                f"where id in ({placeholders})"
            )
            found = {row["id"]: dict(row) for row in conn.execute(sql, all_ids)}

    target_provider = args.target_provider
    missing_sources = [thread_id for thread_id in source_ids if thread_id not in found]
    missing_forks = [thread_id for thread_id in fork_ids if thread_id not in found]
    fork_wrong_provider = []
    fork_wrong_model = []
    fork_wrong_archive = []
    source_wrong_archive = []

    for row in ok_rows:
        source_id = row.get("sourceThreadId")
        fork_id = row.get("forkThreadId")
        source_archived = bool(row.get("sourceArchived"))
        expected_fork_archived = bool(row.get("targetArchived"))

        if source_id in found and source_archived and int(found[source_id]["archived"]) != 1:
            source_wrong_archive.append(found[source_id])
        if fork_id in found:
            fork = found[fork_id]
            if target_provider and fork["model_provider"] != target_provider:
                fork_wrong_provider.append(fork)
            if args.target_model and fork["model"] != args.target_model:
                fork_wrong_model.append(fork)
            if int(fork["archived"]) != int(expected_fork_archived):
                fork_wrong_archive.append(fork)

    validation = {
        "mapping": str(mapping_path),
        "codexHome": str(codex_home),
        "okMappings": len(ok_rows),
        "missingSources": missing_sources,
        "missingForks": missing_forks,
        "sourceWrongArchiveCount": len(source_wrong_archive),
        "forkWrongProviderCount": len(fork_wrong_provider),
        "forkWrongModelCount": len(fork_wrong_model),
        "forkWrongArchiveCount": len(fork_wrong_archive),
        "sampleForks": [found[thread_id] for thread_id in fork_ids[:10] if thread_id in found],
    }

    out = mapping_path.with_name("fork-validation.json")
    json_dump(out, validation)
    print(json.dumps(validation, ensure_ascii=False, indent=2))
    return 0 if not (
        missing_sources
        or missing_forks
        or source_wrong_archive
        or fork_wrong_provider
        or fork_wrong_model
        or fork_wrong_archive
    ) else 2


def cmd_repair_titles(args: argparse.Namespace) -> int:
    codex_home = discover_codex_home(args.codex_home)
    backup_root = expand_path(args.backup_root) or (codex_home / "backups")
    backup_root.mkdir(parents=True, exist_ok=True)

    evidence_roots = evidence_roots_from_args(args.evidence_root)
    mapping_paths = [p for p in (expand_path(raw) for raw in args.mapping or []) if p and p.exists()]
    source_providers = None
    if args.target_providers:
        source_providers = {part.strip() for part in args.target_providers.split(",") if part.strip()}

    plan, stats = build_title_repair_plan(
        codex_home=codex_home,
        evidence_roots=evidence_roots,
        mapping_paths=mapping_paths,
        target_providers=source_providers,
        max_title_length=args.max_title_length,
        include_current_db_fallback=not args.no_current_db_fallback,
        infer_from_rollout=not args.no_infer_source_from_rollout,
        only_missing_index=args.only_missing_index,
    )

    raw_plan_count = len(plan)
    allowed_kinds = TITLE_CONFIDENCE_KINDS[args.confidence]
    plan = [item for item in plan if str(item.get("evidenceKind")) in allowed_kinds]

    result: dict[str, Any] = {
        "codexHome": str(codex_home),
        "execute": args.execute,
        "confidence": args.confidence,
        **stats,
        "rawPlannedUpdatesBeforeConfidenceFilter": raw_plan_count,
        "plannedUpdates": len(plan),
        "plannedSessionIndexUpdates": sum(1 for item in plan if item["updateSessionIndex"]),
        "plannedStateDbTitleUpdates": sum(1 for item in plan if item["updateStateDbTitle"]),
        "plannedByProvider": dict(sorted(Counter(str(item["modelProvider"]) for item in plan).items())),
        "plannedByEvidenceKind": dict(sorted(Counter(str(item["evidenceKind"]) for item in plan).items())),
        "sourceLinkedUpdates": sum(1 for item in plan if item.get("sourceThreadId")),
    }

    if args.show_titles:
        result["planSample"] = plan[: args.sample_limit]
    else:
        result["planSample"] = [
            {
                "threadId": item["threadId"],
                "modelProvider": item["modelProvider"],
                "archived": item["archived"],
                "currentTitleLength": len(item["currentTitle"]),
                "currentIndexTitleLength": len(item["currentIndexTitle"]),
                "desiredTitleLength": len(item["desiredTitle"]),
                "evidenceKind": item["evidenceKind"],
                "evidencePath": item["evidencePath"],
                "sourceThreadId": item.get("sourceThreadId"),
                "updateStateDbTitle": item["updateStateDbTitle"],
                "updateSessionIndex": item["updateSessionIndex"],
            }
            for item in plan[: args.sample_limit]
        ]

    out = backup_root / f"title-repair-plan-{now_stamp()}.json"
    json_dump(out, {**result, "plan": plan})
    result["planPath"] = str(out)

    if args.execute:
        backup_dir = backup_before_title_repair(codex_home, backup_root)
        apply_result = apply_title_repair(codex_home, plan)
        result["backupDir"] = str(backup_dir)
        result.update(apply_result)
        json_dump(out, {**result, "plan": plan})
    else:
        eprint("Dry-run only. Re-run with --execute to update state_5.sqlite and session_index.jsonl.")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def provider_counts(codex_home: Path) -> list[dict[str, Any]]:
    with connect_db(codex_home, readonly=True) as conn:
        return [
            dict(row)
            for row in conn.execute(
                "select model_provider, archived, count(*) as count "
                "from threads group by model_provider, archived "
                "order by model_provider, archived"
            )
        ]


def cmd_quarantine_duplicates(args: argparse.Namespace) -> int:
    codex_home = discover_codex_home(args.codex_home)
    backup_root = expand_path(args.backup_root) or (codex_home / "backups")
    backup_root.mkdir(parents=True, exist_ok=True)
    result = quarantine_duplicate_rollouts(codex_home, backup_root, args.execute)
    out = backup_root / f"duplicate-rollout-quarantine-plan-{now_stamp()}.json"
    json_dump(out, result)
    result["resultPath"] = str(out)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.execute:
        eprint("Dry-run only. Re-run with --execute to move duplicate rollout files into quarantine.")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    codex_home = discover_codex_home(args.codex_home)
    output = expand_path(args.output)
    if not output:
        raise SystemExit("--output is required")

    report: dict[str, Any] = {
        "codexHome": str(codex_home),
        "generatedAt": datetime.now().isoformat(),
        "providerCounts": provider_counts(codex_home),
    }

    if args.mapping:
        mapping_path = expand_path(args.mapping)
        if not mapping_path or not mapping_path.exists():
            raise SystemExit("--mapping does not exist")
        rows = load_mapping(mapping_path)
        report["forkResults"] = {
            "mapping": str(mapping_path),
            "ok": sum(1 for row in rows if row.get("status") == "ok"),
            "failed": sum(1 for row in rows if row.get("status") == "failed"),
            "started": sum(1 for row in rows if row.get("status") == "started"),
            "targetProviders": dict(sorted(Counter(str(row.get("targetProvider") or "") for row in rows).items())),
            "targetArchived": dict(sorted(Counter(str(row.get("targetArchived")) for row in rows).items())),
        }

    doctor = None
    if args.doctor:
        doctor_path = output.with_suffix(".doctor.json")
        doctor = run_doctor(codex_home, doctor_path)
        parity = (doctor or {}).get("checks", {}).get("state.rollout_db_parity", {})
        report["doctor"] = {
            "path": str(doctor_path),
            "overallStatus": (doctor or {}).get("overallStatus"),
            "rolloutDbParityStatus": parity.get("status"),
            "rolloutDbParitySummary": parity.get("summary"),
            "rolloutDbParityDetails": parity.get("details", {}),
        }

    lines = [
        "# Codex History Recovery Report",
        "",
        f"- Codex home: `{codex_home}`",
        f"- Generated at: `{report['generatedAt']}`",
        "",
        "## Provider Counts",
        "",
        "| Provider | Archived | Count |",
        "| --- | ---: | ---: |",
    ]
    for row in report["providerCounts"]:
        lines.append(f"| {row['model_provider']} | {row['archived']} | {row['count']} |")

    if "forkResults" in report:
        fr = report["forkResults"]
        lines.extend([
            "",
            "## Fork Results",
            "",
            f"- Mapping: `{fr['mapping']}`",
            f"- OK: `{fr['ok']}`",
            f"- Failed: `{fr['failed']}`",
            f"- Started/incomplete: `{fr['started']}`",
        ])

    if "doctor" in report:
        d = report["doctor"]
        lines.extend([
            "",
            "## Doctor",
            "",
            f"- Overall status: `{d['overallStatus']}`",
            f"- Rollout DB parity: `{d['rolloutDbParityStatus']}`",
            f"- Summary: {d['rolloutDbParitySummary']}",
            f"- Raw doctor JSON: `{d['path']}`",
        ])

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(lines) + "\n", encoding="utf-8")
    json_dump(output.with_suffix(".json"), report)
    print(json.dumps({"report": str(output), "json": str(output.with_suffix(".json"))}, ensure_ascii=False, indent=2))
    return 0


def cmd_export_package(args: argparse.Namespace) -> int:
    codex_home = discover_codex_home(args.codex_home)
    candidates = expand_path(args.candidates)
    if not candidates or not candidates.exists():
        raise SystemExit("--candidates does not exist")
    output = expand_path(args.output)
    if not output:
        raise SystemExit("--output is required")
    manifest = write_migration_package(
        codex_home=codex_home,
        candidates_path=candidates,
        output_zip=output,
        package_name=args.name,
        include_metadata=args.include_metadata,
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest.get("missingRolloutCount", 0) == 0 else 2


def cmd_import_package(args: argparse.Namespace) -> int:
    codex_home = discover_codex_home(args.codex_home)
    package = expand_path(args.package)
    if not package or not package.exists():
        raise SystemExit("--package does not exist")
    backup_root = expand_path(args.backup_root) or (codex_home / "backups")
    backup_root.mkdir(parents=True, exist_ok=True)
    result = import_migration_package(
        codex_home=codex_home,
        package_path=package,
        backup_root=backup_root,
        execute=args.execute,
        overwrite=args.overwrite,
        allow_duplicate_thread_ids=args.allow_duplicate_thread_ids,
    )
    out = package.with_name("import-result.json")
    json_dump(out, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not args.execute:
        eprint("Dry-run only. Re-run with --execute to copy rollout files into this Codex home.")
    return 0


def prompt_text(label: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or (default or "")


def prompt_choice(label: str, choices: list[str], default: str) -> str:
    joined = "/".join(choice.upper() if choice == default else choice for choice in choices)
    while True:
        value = input(f"{label} ({joined}): ").strip().lower()
        if not value:
            return default
        if value in choices:
            return value
        print(f"Please choose one of: {', '.join(choices)}")


def prompt_yes(label: str, default: bool = False) -> bool:
    hint = "Y/n" if default else "y/N"
    value = input(f"{label} ({hint}): ").strip().lower()
    if not value:
        return default
    return value in {"y", "yes"}


def confirm_execute(label: str) -> bool:
    value = input(f"{label}\nType yes to continue: ").strip()
    return value == "yes"


def cmd_wizard(args: argparse.Namespace) -> int:
    print("Codex History Recovery Wizard")
    print("This wizard is conservative: audit/export/import dry-run first; writes need explicit yes.")
    mode = prompt_choice(
        "Choose mode",
        ["local", "export", "import"],
        args.mode or "local",
    )
    detected = str(discover_codex_home(args.codex_home))
    codex_home_raw = prompt_text("Codex home", args.codex_home or detected)
    codex_home = discover_codex_home(codex_home_raw)

    backup_default = str((codex_home / "backups").resolve())
    backup_root = expand_path(prompt_text("Backup/package root", args.backup_root or backup_default))
    if not backup_root:
        raise SystemExit("Backup root is required.")
    backup_root.mkdir(parents=True, exist_ok=True)

    if mode == "local":
        archived = prompt_choice("Scope by archive state", ["all", "active", "archived"], "all")
        providers = prompt_text("Source providers, comma-separated (blank = all)", "")
        search = prompt_text("Search keyword (blank = all)", "")
        full_backup = prompt_yes("Include sessions/ and archived_sessions/ in backup", True)
        doctor = prompt_yes("Run codex doctor after audit", False)
        audit_args = argparse.Namespace(
            codex_home=str(codex_home),
            backup_root=str(backup_root),
            full_backup=full_backup,
            archived=archived,
            source_providers=providers or None,
            target_provider=None,
            search=search or None,
            include_subagents=False,
            doctor=doctor,
        )
        return cmd_audit(audit_args)

    if mode == "export":
        candidates = prompt_text("Path to thread-candidates.json from audit")
        if not candidates:
            raise SystemExit("Candidates path is required.")
        output_default = str(backup_root / f"codex-history-migration-{now_stamp()}.zip")
        output = prompt_text("Output migration zip", output_default)
        name = prompt_text("Package name", "Codex history migration")
        include_metadata = prompt_yes("Include non-secret metadata such as config.toml (auth.json is never included)", False)
        export_args = argparse.Namespace(
            codex_home=str(codex_home),
            candidates=candidates,
            output=output,
            name=name,
            include_metadata=include_metadata,
        )
        return cmd_export_package(export_args)

    if mode == "import":
        package = prompt_text("Path to migration zip")
        if not package:
            raise SystemExit("Package path is required.")
        overwrite = prompt_yes("Overwrite rollout files if same relative path already exists", False)
        dry_args = argparse.Namespace(
            codex_home=str(codex_home),
            package=package,
            backup_root=str(backup_root),
            overwrite=overwrite,
            allow_duplicate_thread_ids=False,
            execute=False,
        )
        code = cmd_import_package(dry_args)
        if confirm_execute("Import dry-run finished. This will copy rollout files into the destination Codex home."):
            execute_args = argparse.Namespace(
                codex_home=str(codex_home),
                package=package,
                backup_root=str(backup_root),
                overwrite=overwrite,
                allow_duplicate_thread_ids=False,
                execute=True,
            )
            return cmd_import_package(execute_args)
        print("Import not executed.")
        return code

    raise SystemExit(f"Unknown wizard mode: {mode}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    audit = sub.add_parser("audit", help="Back up and produce candidate inventory")
    audit.add_argument("--codex-home", help="Codex home containing state_5.sqlite")
    audit.add_argument("--backup-root", default=None, help="Directory for timestamped backups")
    audit.add_argument("--full-backup", action="store_true", help="Copy sessions and archived_sessions too")
    audit.add_argument("--archived", choices=["active", "archived", "all"], default="all")
    audit.add_argument("--source-providers", help="Comma-separated source providers to include")
    audit.add_argument("--target-provider", default=None, help="Provider intended for later fork")
    audit.add_argument("--search", help="Filter title/cwd/id by substring")
    audit.add_argument("--include-subagents", action="store_true", help="Include subagent/internal threads")
    audit.add_argument("--doctor", action="store_true", help="Run codex doctor --json into backup dir")
    audit.set_defaults(func=cmd_audit)

    validate = sub.add_parser("validate", help="Validate fork mapping against state DB")
    validate.add_argument("--codex-home", help="Codex home containing state_5.sqlite")
    validate.add_argument("--mapping", required=True, help="fork-results.json path")
    validate.add_argument("--target-provider", help="Expected fork provider")
    validate.add_argument("--target-model", help="Expected fork model")
    validate.set_defaults(func=cmd_validate)

    repair_titles = sub.add_parser("repair-titles", help="Repair persistent thread titles from mappings and backups")
    repair_titles.add_argument("--codex-home", help="Codex home containing state_5.sqlite")
    repair_titles.add_argument("--backup-root", help="Backup root for plans and pre-write backups")
    repair_titles.add_argument(
        "--evidence-root",
        action="append",
        help="Directory or file to search for fork-results.json, thread-candidates.json, session_index.jsonl, and state_5.sqlite",
    )
    repair_titles.add_argument("--mapping", action="append", help="Explicit fork-results.json path; may be repeated")
    repair_titles.add_argument("--target-providers", help="Comma-separated providers to repair, for example OpenAI")
    repair_titles.add_argument("--max-title-length", type=int, default=160, help="Maximum repaired title length")
    repair_titles.add_argument("--confidence", choices=["strong", "medium", "all"], default="strong", help="Evidence threshold for repairing titles")
    repair_titles.add_argument("--only-missing-index", action="store_true", help="Only add missing session_index.jsonl entries")
    repair_titles.add_argument("--no-current-db-fallback", action="store_true", help="Do not use current DB title as a fallback for missing index entries")
    repair_titles.add_argument("--no-infer-source-from-rollout", action="store_true", help="Do not infer source thread ids by scanning rollout UUIDs")
    repair_titles.add_argument("--show-titles", action="store_true", help="Include title text in the plan sample")
    repair_titles.add_argument("--sample-limit", type=int, default=20, help="Number of planned repairs to include in output sample")
    repair_titles.add_argument("--execute", action="store_true", help="Actually update state_5.sqlite and session_index.jsonl; without this, dry-run only")
    repair_titles.set_defaults(func=cmd_repair_titles)

    export = sub.add_parser("export-package", help="Create a portable migration zip from candidates")
    export.add_argument("--codex-home", help="Source Codex home containing rollout files")
    export.add_argument("--candidates", required=True, help="thread-candidates.json from audit")
    export.add_argument("--output", required=True, help="Output .zip package path")
    export.add_argument("--name", help="Human-readable package name in manifest")
    export.add_argument("--include-metadata", action="store_true", help="Include config/version metadata; auth.json is never included")
    export.set_defaults(func=cmd_export_package)

    import_pkg = sub.add_parser("import-package", help="Import a migration zip into this device")
    import_pkg.add_argument("--codex-home", help="Destination Codex home")
    import_pkg.add_argument("--package", required=True, help="Migration .zip package")
    import_pkg.add_argument("--backup-root", help="Backup root before import")
    import_pkg.add_argument("--overwrite", action="store_true", help="Overwrite existing rollout files with same path")
    import_pkg.add_argument("--allow-duplicate-thread-ids", action="store_true", help="Allow importing a rollout when another file with the same thread id already exists")
    import_pkg.add_argument("--execute", action="store_true", help="Actually copy files; without this, dry-run only")
    import_pkg.set_defaults(func=cmd_import_package)

    quarantine = sub.add_parser("quarantine-duplicates", help="Move duplicate rollout thread-id files into a backup quarantine")
    quarantine.add_argument("--codex-home", help="Codex home containing rollout files")
    quarantine.add_argument("--backup-root", help="Directory for quarantine backups")
    quarantine.add_argument("--execute", action="store_true", help="Actually move duplicate files; without this, dry-run only")
    quarantine.set_defaults(func=cmd_quarantine_duplicates)

    report = sub.add_parser("report", help="Write a Markdown recovery report")
    report.add_argument("--codex-home", help="Codex home containing state_5.sqlite")
    report.add_argument("--mapping", help="Optional fork-results.json path")
    report.add_argument("--output", required=True, help="Output Markdown report path")
    report.add_argument("--doctor", action="store_true", help="Run codex doctor and include rollout parity status")
    report.set_defaults(func=cmd_report)

    wizard = sub.add_parser("wizard", help="Interactive guided workflow")
    wizard.add_argument("--mode", choices=["local", "export", "import"], help="Start wizard in a specific branch")
    wizard.add_argument("--codex-home", help="Initial Codex home")
    wizard.add_argument("--backup-root", help="Initial backup/package root")
    wizard.set_defaults(func=cmd_wizard)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        eprint("Interrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
