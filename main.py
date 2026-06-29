"""
DB-to-DB Sync API — pulls data from the SERVER Postgres down to your LOCAL
Postgres, triggered by a button click in another app calling POST /api/sync.

WHY THE OLD SYNC HIT FOREIGN KEY ERRORS:
custom_form_entries.created_by / reviewed_by reference users.id. If
custom_form_entries gets synced into local before the matching users rows
exist locally, every insert violates the FK constraint. Fix: inspect live
PostgreSQL FK metadata, sync every selected table's parent closure first,
then sync dependent tables. Upsert makes re-running it safe.

LONG-TERM (weeks/months of repeated clicking) BEHAVIOUR:
1. INCREMENTAL — each table tracks a "last_synced_at" cursor in a small
   embedded SQLite file (sync_meta.db), NOT in either Postgres. Every run
   only fetches rows where updated_at > cursor, so sync time doesn't keep
   growing as your data grows over months.

2. SAFE RETRY OF FAILURES — the cursor for a table only advances when that
   table's run had ZERO row errors. If even one row fails, the cursor
   stays put, so next run re-checks the same window and retries the
   failed row automatically (the upsert makes re-syncing already-ok rows
   harmless).

3. OVERLAP PROTECTION — a plain OS file lock (flock) means two clicks in a
   row (e.g. previous run still finishing because the table grew) can't
   run the sync at the same time. Returns 409 if already running. This
   doesn't depend on either Postgres being reachable to check.

4. AUDIT TRAIL — every run (start/end time, success, full summary) is
   logged into the same SQLite file, in sync_runs, so months from now you
   can see exactly when syncs ran and what happened.

WHY SQLITE INSTEAD OF A TABLE IN LOCAL_DB:
Keeps API bookkeeping (cursors, lock, run history) completely separate
from the destination Postgres, which stays a clean mirror of source data
with nothing extra bolted on. It's a single file, no separate DB server,
nothing to install -- sqlite3 is in the Python standard library.

NOT HANDLED YET (a business decision, not a coding one):
   If review_status flips from 'passed' back to pending/rejected AFTER
   it was already synced, the stale copy stays in the destination forever
   — nothing currently deletes it. Tell me if that should be cleaned up.

SYNC ORDER:
The API reads production foreign keys and derives parent-before-child order
at runtime. A request selects target tables; only their transitive parent
tables are included. Table-specific business filters live in
SYNC_TABLE_OPTIONS below, because PostgreSQL metadata cannot infer them.
"""

import fcntl
import hashlib
import json
import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
import psycopg2
from psycopg2 import sql, OperationalError
from psycopg2.extras import RealDictCursor, Json

load_dotenv()  # reads .env in the same folder as this file -- see .env.example

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("db_sync_api")

app = FastAPI()

# ADAPT: point this at your server's Postgres
SOURCE_DB = dict(
    host=os.getenv("SOURCE_DB_HOST"),
    port=os.getenv("SOURCE_DB_PORT", "5432"),
    dbname=os.getenv("SOURCE_DB_NAME"),
    user=os.getenv("SOURCE_DB_USER"),
    password=os.getenv("SOURCE_DB_PASSWORD"),
    sslmode=os.getenv("SOURCE_DB_SSLMODE", "require"),  # traffic crosses the public internet
)

# ADAPT: point this at your destination VPS's Postgres
LOCAL_DB = dict(
    host=os.getenv("LOCAL_DB_HOST"),
    port=os.getenv("LOCAL_DB_PORT", "5432"),
    dbname=os.getenv("LOCAL_DB_NAME"),
    user=os.getenv("LOCAL_DB_USER"),
    password=os.getenv("LOCAL_DB_PASSWORD"),
    sslmode=os.getenv("LOCAL_DB_SSLMODE", "require"),  # traffic crosses the public internet
)

# When callers omit `tables`, preserve this API's existing purpose: sync the
# approved entries plus every FK parent they require.
DEFAULT_SYNC_TABLES = ("custom_form_entries",)

# Keep business filters explicit and server-controlled. Never accept SQL from
# a request body. Tables not listed here are synced without a row filter.
SYNC_TABLE_OPTIONS = {
    "custom_form_entries": {"where": "review_status = 'passed'"},
    # Large FK parent table. It does not expose a usable updated_at in the
    # current schema, so treat created_at as an append-only incremental cursor.
    # Do not use created_at for tables where existing rows are edited later.
    "source_entries": {"updated_at_col": "created_at"},
}

# --- security: required now that callers can specify which DB to connect to ---
API_KEY = os.getenv("SYNC_API_KEY")  # required -- set this in .env
ALLOWED_DB_HOSTS = {h.strip() for h in os.getenv("ALLOWED_DB_HOSTS", "").split(",") if h.strip()}


def require_api_key(x_api_key: Optional[str] = Header(default=None)):
    if not API_KEY:
        # fail closed: if no key is configured server-side, refuse rather
        # than silently running unauthenticated
        logger.error("SYNC_API_KEY is not set -- refusing all requests until it is")
        raise HTTPException(status_code=503, detail="API key not configured on server")
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key header")


def check_host_allowed(host: Optional[str]):
    if host and ALLOWED_DB_HOSTS and host not in ALLOWED_DB_HOSTS:
        logger.warning(f"Rejected sync request targeting disallowed host: {host}")
        raise HTTPException(status_code=403, detail=f"Host '{host}' is not in ALLOWED_DB_HOSTS")


class DBConfigOverride(BaseModel):
    host: str
    port: str = "5432"
    dbname: str
    user: str
    password: str
    sslmode: str = "require"


class SyncRequest(BaseModel):
    # Both optional -- omit either (or send an empty body) to use the
    # .env defaults. Only override what you actually need to change.
    source_db: Optional[DBConfigOverride] = None
    local_db: Optional[DBConfigOverride] = None
    # Select the business tables to copy. FK parents are discovered and added
    # automatically; unrelated production tables are not copied.
    tables: Optional[list[str]] = None
    # Return the resolved plan without copying rows. Use this before a new
    # production sync to review tables, FK links, and filters.
    dry_run: bool = False
    # Ignore saved incremental cursors for this source/destination pair and
    # copy all selected tables again. Use after rebuilding the destination.
    full_resync: bool = False


class SyncErrorItem(BaseModel):
    code: str
    table: Optional[str] = None
    pk_value: Any = None
    message: str
    details: dict[str, Any] = {}


class SyncResponseData(BaseModel):
    tables: Optional[list[dict[str, Any]]] = None
    plan: Optional[list[dict[str, Any]]] = None


class SyncResponse(BaseModel):
    success: bool
    mode: str
    full_resync: bool
    auto_full_resync: bool
    message: str
    data: SyncResponseData
    errors: list[SyncErrorItem]
    summary: Optional[list[dict[str, Any]]] = None
    dry_run: Optional[bool] = None
    plan: Optional[list[dict[str, Any]]] = None


# --- lightweight embedded state, separate from both Postgres databases ---
META_DB_PATH = os.getenv("SYNC_META_DB_PATH", "sync_meta.db")
LOCK_FILE_PATH = os.getenv("SYNC_LOCK_FILE", "/tmp/db_sync_api.lock")

# Re-check this much overlap on every incremental fetch, to catch rows whose
# transaction committed slightly AFTER a row with a later updated_at was
# already seen and used to advance the cursor. Cheap (upsert dedupes it),
# and prevents permanently skipping a "late-committing" row. ADAPT if your
# slowest write transactions can take longer than this.
SAFETY_BUFFER = timedelta(minutes=5)


def get_meta_conn():
    conn = sqlite3.connect(META_DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_state (
            table_name TEXT PRIMARY KEY,
            last_synced_at TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT,
            finished_at TEXT,
            success INTEGER,
            summary TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sync_state_v2 (
            scope_key TEXT NOT NULL,
            table_name TEXT NOT NULL,
            last_synced_at TEXT,
            PRIMARY KEY (scope_key, table_name)
        )
    """)
    conn.commit()
    return conn


def get_last_synced(meta_conn, scope_key: str, table: str):
    try:
        row = meta_conn.execute(
            """
            SELECT last_synced_at
            FROM sync_state_v2
            WHERE scope_key = ? AND table_name = ?
            """,
            (scope_key, table),
        ).fetchone()
        if not row or row[0] is None:
            return None
        return datetime.fromisoformat(row[0])
    except Exception as e:
        # Can't read the cursor -- fall back to a full fetch for this table
        # rather than crashing the whole sync over a bookkeeping issue.
        logger.error(f"Could not read sync cursor for '{table}', doing a full fetch: {e}")
        return None


def set_last_synced(meta_conn, scope_key: str, table: str, ts: datetime):
    try:
        meta_conn.execute(
            """
            INSERT INTO sync_state_v2 (scope_key, table_name, last_synced_at)
            VALUES (?, ?, ?)
            ON CONFLICT(scope_key, table_name)
            DO UPDATE SET last_synced_at = excluded.last_synced_at
            """,
            (scope_key, table, ts.isoformat()),
        )
        meta_conn.commit()
    except Exception as e:
        # Sync itself already succeeded -- don't fail the request over a
        # cursor write error. Worst case, next run re-fetches this window.
        logger.error(f"Could not write sync cursor for '{table}': {e}")


def clear_sync_state(meta_conn, scope_key: str):
    """Reset only this source/destination plan's incremental cursors."""
    meta_conn.execute("DELETE FROM sync_state_v2 WHERE scope_key = ?", (scope_key,))
    meta_conn.commit()


def has_sync_state(meta_conn, scope_key: str) -> bool:
    row = meta_conn.execute(
        "SELECT 1 FROM sync_state_v2 WHERE scope_key = ? LIMIT 1",
        (scope_key,),
    ).fetchone()
    return row is not None


def target_looks_rebuilt(conn, sync_plan: list[dict], meta_conn, scope_key: str) -> bool:
    """Detect a reset target so a normal one-click sync can safely reload it."""
    if not has_sync_state(meta_conn, scope_key):
        return False

    with conn.cursor() as cur:
        for step in sync_plan:
            if not step.get("updated_at_col"):
                continue
            cur.execute(
                sql.SQL("SELECT EXISTS (SELECT 1 FROM {} LIMIT 1)").format(
                    sql.Identifier(step["table"])
                )
            )
            if not cur.fetchone()[0]:
                logger.warning(
                    "Target table '%s' is empty while sync cursors exist; treating this run as a full resync",
                    step["table"],
                )
                return True

    return False


def sync_scope_key(source_cfg: dict, local_cfg: dict, sync_plan: list[dict]):
    """Stable, non-secret cursor namespace for one source/destination plan."""
    def connection_identity(config: dict):
        return {
            key: config.get(key)
            for key in ("host", "port", "dbname", "user", "sslmode")
        }

    payload = {
        "source": connection_identity(source_cfg),
        "destination": connection_identity(local_cfg),
        "tables": [
            {
                "table": step["table"],
                "where": step.get("where"),
                "updated_at_col": step.get("updated_at_col"),
            }
            for step in sync_plan
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def log_run(meta_conn, started_at: datetime, finished_at: datetime, success: bool, summary: list):
    meta_conn.execute(
        "INSERT INTO sync_runs (started_at, finished_at, success, summary) VALUES (?, ?, ?, ?)",
        (started_at.isoformat(), finished_at.isoformat(), int(success), json.dumps(summary)),
    )
    meta_conn.commit()


def collect_sync_errors(summary: list[dict]) -> list[dict]:
    errors = []
    for table_summary in summary:
        table = table_summary.get("table")
        for error in table_summary.get("errors", []):
            errors.append({
                "code": "ROW_SYNC_FAILED",
                "table": table,
                "pk_value": error.get("pk_value"),
                "message": error.get("error", "Row sync failed"),
                "details": {
                    key: value
                    for key, value in error.items()
                    if key not in {"pk_value", "error"}
                },
            })
    return errors


def sync_response(
    *,
    success: bool,
    mode: str,
    message: str,
    summary: Optional[list[dict]] = None,
    plan: Optional[list[dict]] = None,
    full_resync: bool = False,
    auto_full_resync: bool = False,
):
    data = {}
    if summary is not None:
        data["tables"] = summary
    if plan is not None:
        data["plan"] = plan

    errors = collect_sync_errors(summary or [])

    response = {
        "success": success,
        "mode": mode,
        "full_resync": full_resync,
        "auto_full_resync": auto_full_resync,
        "message": message,
        "data": data,
        "errors": errors,
    }

    # Backward-compatible fields for existing callers.
    if summary is not None:
        response["summary"] = summary
    if plan is not None:
        response["dry_run"] = True
        response["plan"] = plan

    return response


def acquire_lock():
    """Plain OS file lock -- works across processes/workers, no DB needed to check it."""
    fd = open(LOCK_FILE_PATH, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fd
    except OSError:
        fd.close()
        return None


def release_lock(fd):
    if fd:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


def discover_sync_plan(conn, requested_tables: Optional[list[str]] = None):
    """Resolve selected tables into a parent-first plan from live FK metadata."""
    requested_tables = requested_tables or list(DEFAULT_SYNC_TABLES)
    if not requested_tables:
        raise HTTPException(status_code=422, detail="At least one table must be selected")

    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("""
            SELECT tablename
            FROM pg_tables
            WHERE schemaname = 'public'
        """)
        available_tables = {row["tablename"] for row in cur.fetchall()}

        unknown = sorted(set(requested_tables) - available_tables)
        if unknown:
            raise HTTPException(
                status_code=422,
                detail={"error": "Unknown selected tables", "tables": unknown},
            )

        cur.execute("""
            SELECT
                child.relname AS child_table,
                child_column.attname AS child_column,
                parent.relname AS parent_table,
                parent_column.attname AS parent_column,
                fk.conname AS constraint_name,
                child_key.ordinality AS column_position
            FROM pg_constraint fk
            JOIN pg_class child ON child.oid = fk.conrelid
            JOIN pg_namespace child_schema ON child_schema.oid = child.relnamespace
            JOIN pg_class parent ON parent.oid = fk.confrelid
            JOIN pg_namespace parent_schema ON parent_schema.oid = parent.relnamespace
            JOIN unnest(fk.conkey) WITH ORDINALITY AS child_key(attnum, ordinality) ON TRUE
            JOIN unnest(fk.confkey) WITH ORDINALITY AS parent_key(attnum, ordinality)
                ON parent_key.ordinality = child_key.ordinality
            JOIN pg_attribute child_column
                ON child_column.attrelid = child.oid AND child_column.attnum = child_key.attnum
            JOIN pg_attribute parent_column
                ON parent_column.attrelid = parent.oid AND parent_column.attnum = parent_key.attnum
            WHERE fk.contype = 'f'
              AND child_schema.nspname = 'public'
              AND parent_schema.nspname = 'public'
            ORDER BY child.relname, fk.conname, child_key.ordinality
        """)
        fk_rows = cur.fetchall()

        cur.execute("""
            SELECT table_name, column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'public'
        """)
        columns = cur.fetchall()

        cur.execute("""
            SELECT keys.table_name, keys.column_name
            FROM information_schema.table_constraints constraints
            JOIN information_schema.key_column_usage keys
              ON constraints.constraint_name = keys.constraint_name
             AND constraints.table_schema = keys.table_schema
            WHERE constraints.table_schema = 'public'
              AND constraints.constraint_type = 'PRIMARY KEY'
            ORDER BY table_name, keys.ordinal_position
        """)
        pk_rows = cur.fetchall()

    dependencies: dict[str, list[dict]] = {}
    for fk in fk_rows:
        dependencies.setdefault(fk["child_table"], []).append(dict(fk))

    state: dict[str, str] = {}
    ordered: list[str] = []
    included: set[str] = set()

    def visit(table: str, path: list[str]):
        status = state.get(table)
        if status == "visiting":
            start = path.index(table)
            cycle = path[start:] + [table]
            raise HTTPException(
                status_code=422,
                detail={"error": "Circular foreign-key dependency", "cycle": cycle},
            )
        if status == "visited":
            return

        state[table] = "visiting"
        included.add(table)
        for fk in dependencies.get(table, []):
            visit(fk["parent_table"], path + [table])
        state[table] = "visited"
        ordered.append(table)

    for table in dict.fromkeys(requested_tables):
        visit(table, [])

    primary_keys: dict[str, list[str]] = {}
    for row in pk_rows:
        primary_keys.setdefault(row["table_name"], []).append(row["column_name"])

    columns_by_table: dict[str, dict[str, str]] = {}
    for row in columns:
        columns_by_table.setdefault(row["table_name"], {})[row["column_name"]] = row["data_type"]

    plan = []
    for table in ordered:
        pks = primary_keys.get(table, [])
        if len(pks) != 1:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "Table needs exactly one primary key for upsert",
                    "table": table,
                    "primary_key_columns": pks,
                },
            )

        configured = SYNC_TABLE_OPTIONS.get(table, {})
        updated_at_col = configured.get("updated_at_col", "updated_at")
        data_type = columns_by_table.get(table, {}).get(updated_at_col)
        if data_type not in {"date", "timestamp without time zone", "timestamp with time zone"}:
            updated_at_col = None

        plan.append({
            "table": table,
            "pk": pks[0],
            "where": configured.get("where"),
            "updated_at_col": updated_at_col,
            "depends_on": sorted({fk["parent_table"] for fk in dependencies.get(table, []) if fk["parent_table"] in included}),
        })

    return plan


def fetch_rows(conn, table: str, where: str | None, updated_at_col: str | None, since_ts):
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        query = sql.SQL("SELECT * FROM {}").format(sql.Identifier(table))
        conditions = []
        params: list = []

        if where:
            conditions.append(sql.SQL(where))
        if updated_at_col and since_ts is not None:
            # After a table has a cursor, only rows that moved past the
            # cursor should be fetched. Rows with NULL updated_at are copied
            # during the first baseline fetch, but including them forever
            # makes every incremental run re-upsert the same old rows.
            conditions.append(
                sql.SQL("{col} > %s").format(col=sql.Identifier(updated_at_col))
            )
            params.append(since_ts)

        if conditions:
            query = sql.SQL("{} WHERE {}").format(query, sql.SQL(" AND ").join(conditions))

        cur.execute(query, params)
        return cur.fetchall()


def upsert_rows(conn, table: str, pk: str, rows: list[dict]):
    if not rows:
        return 0, 0, []

    columns = list(rows[0].keys())
    update_cols = [c for c in columns if c != pk]

    if update_cols:
        insert_stmt = sql.SQL(
            "INSERT INTO {table} ({cols}) VALUES ({vals}) "
            "ON CONFLICT ({pk}) DO UPDATE SET {updates} "
            "WHERE ({target_cols}) IS DISTINCT FROM ({excluded_cols})"
        ).format(
            table=sql.Identifier(table),
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
            vals=sql.SQL(", ").join(sql.Placeholder() for _ in columns),
            pk=sql.Identifier(pk),
            updates=sql.SQL(", ").join(
                sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in update_cols
            ),
            target_cols=sql.SQL(", ").join(
                sql.SQL("{table}.{column}").format(
                    table=sql.Identifier(table),
                    column=sql.Identifier(c),
                )
                for c in update_cols
            ),
            excluded_cols=sql.SQL(", ").join(
                sql.SQL("EXCLUDED.{column}").format(column=sql.Identifier(c)) for c in update_cols
            ),
        )
    else:
        insert_stmt = sql.SQL(
            "INSERT INTO {table} ({cols}) VALUES ({vals}) "
            "ON CONFLICT ({pk}) DO NOTHING"
        ).format(
            table=sql.Identifier(table),
            cols=sql.SQL(", ").join(sql.Identifier(c) for c in columns),
            vals=sql.SQL(", ").join(sql.Placeholder() for _ in columns),
            pk=sql.Identifier(pk),
        )

    synced, unchanged, errors = 0, 0, []
    for row in rows:
        # dict/list values (e.g. the JSONB `data` column) need wrapping for psycopg2
        values = [Json(v) if isinstance(v, (dict, list)) else v for v in (row[c] for c in columns)]
        cur = conn.cursor()
        try:
            cur.execute(insert_stmt, values)
            conn.commit()
            if cur.rowcount:
                synced += 1
            else:
                unchanged += 1
        except Exception as e:
            conn.rollback()
            errors.append({"pk_value": row.get(pk), "error": str(e)})
        finally:
            cur.close()

    return synced, unchanged, errors


@app.post(
    "/api/sync",
    response_model=SyncResponse,
    tags=["Sync"],
    summary="Run database sync",
)
def sync_server_to_local(
    payload: Optional[SyncRequest] = None,
    x_api_key: Optional[str] = Header(default=None),
):
    require_api_key(x_api_key)

    source_cfg = SOURCE_DB
    local_cfg = LOCAL_DB

    if payload and payload.source_db:
        check_host_allowed(payload.source_db.host)
        source_cfg = payload.source_db.model_dump()
    if payload and payload.local_db:
        check_host_allowed(payload.local_db.host)
        local_cfg = payload.local_db.model_dump()

    requested_tables = payload.tables if payload else None
    dry_run = payload.dry_run if payload else False
    full_resync = payload.full_resync if payload else False

    started_at = datetime.now(timezone.utc)

    lock_fd = acquire_lock()
    if not lock_fd:
        logger.warning("Sync already running, rejecting this trigger")
        raise HTTPException(status_code=409, detail="A sync is already in progress")

    src_conn = None
    local_conn = None
    meta_conn = get_meta_conn()
    success = False
    summary: list = []

    try:
        try:
            src_conn = psycopg2.connect(**source_cfg, connect_timeout=10)
        except OperationalError as e:
            logger.error(f"Cannot connect to SOURCE_DB: {e}")
            raise HTTPException(status_code=502, detail="Cannot reach source database")

        try:
            sync_plan = discover_sync_plan(src_conn, requested_tables)
        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Could not discover foreign-key sync plan: {e}")
            raise HTTPException(status_code=502, detail="Cannot read source database schema")

        scope_key = sync_scope_key(source_cfg, local_cfg, sync_plan)

        if dry_run:
            success = True
            summary = [{
                "table": step["table"],
                "depends_on": step["depends_on"],
                "where": step["where"],
                "incremental_column": step["updated_at_col"],
            } for step in sync_plan]
            return sync_response(
                success=True,
                mode="dry_run",
                full_resync=full_resync,
                message="Sync plan generated.",
                plan=summary,
            )

        try:
            local_conn = psycopg2.connect(**local_cfg, connect_timeout=10)
            local_conn.autocommit = False
        except OperationalError as e:
            logger.error(f"Cannot connect to LOCAL_DB: {e}")
            raise HTTPException(status_code=502, detail="Cannot reach destination database")

        summary = []
        success = True
        auto_full_resync = False
        if not full_resync and target_looks_rebuilt(local_conn, sync_plan, meta_conn, scope_key):
            full_resync = True
            auto_full_resync = True

        if full_resync:
            clear_sync_state(meta_conn, scope_key)

        failed_tables: set[str] = set()
        for step in sync_plan:
            failed_dependencies = sorted(set(step["depends_on"]) & failed_tables)
            if failed_dependencies:
                success = False
                failed_tables.add(step["table"])
                summary.append({
                    "table": step["table"],
                    "fetched": 0,
                    "synced": 0,
                    "skipped": True,
                    "errors": [{
                        "error": "Skipped because required parent table(s) failed",
                        "dependencies": failed_dependencies,
                    }],
                })
                continue

            updated_at_col = step.get("updated_at_col")
            cursor = get_last_synced(meta_conn, scope_key, step["table"]) if updated_at_col else None
            # subtract a small overlap so a row whose transaction committed
            # just after we last advanced the cursor still gets picked up
            since_ts = (cursor - SAFETY_BUFFER) if cursor else None

            try:
                rows = fetch_rows(src_conn, step["table"], step["where"], updated_at_col, since_ts)
            except Exception as e:
                # A fetch failure on a parent table means every dependent table
                # would fail on FK anyway -- stop here instead of generating a
                # wall of guaranteed errors, and say exactly which step broke.
                logger.error(f"Fetch failed for table '{step['table']}': {e}")
                summary.append({
                    "table": step["table"], "fetched": 0, "synced": 0,
                    "errors": [{"error": f"fetch failed: {e}"}],
                })
                success = False
                failed_tables.add(step["table"])
                continue

            synced, unchanged, errors = upsert_rows(local_conn, step["table"], step["pk"], rows)

            if errors:
                success = False
                failed_tables.add(step["table"])
                logger.warning(f"{len(errors)} row(s) failed syncing '{step['table']}': {errors}")
                # cursor stays put -- next run retries this same window, including the failures
            elif updated_at_col and rows:
                timestamps = [row[updated_at_col] for row in rows if row.get(updated_at_col) is not None]
                if timestamps:
                    set_last_synced(meta_conn, scope_key, step["table"], max(timestamps))

            summary.append({
                "table": step["table"], "fetched": len(rows),
                "synced": synced,
                "unchanged": unchanged,
                "incremental_column": updated_at_col,
                "errors": errors,
            })

        logger.info(f"Sync finished, success={success}, summary={summary}")
        if success:
            if auto_full_resync:
                message = "Target looked reset, so a full sync completed automatically."
            else:
                message = "Sync completed successfully."
        else:
            message = "Sync completed with errors."

        return sync_response(
            success=success,
            mode="full_resync" if full_resync else "incremental",
            full_resync=full_resync,
            auto_full_resync=auto_full_resync,
            message=message,
            summary=summary,
        )

    finally:
        finished_at = datetime.now(timezone.utc)
        try:
            log_run(meta_conn, started_at, finished_at, success, summary)
        except Exception:
            pass
        meta_conn.close()
        if src_conn:
            src_conn.close()
        if local_conn:
            local_conn.close()
        release_lock(lock_fd)


@app.get("/api/sync/history")
def sync_history(limit: int = 20, x_api_key: Optional[str] = Header(default=None)):
    require_api_key(x_api_key)
    """Quick audit trail -- see recent runs without needing DB access."""
    meta_conn = get_meta_conn()
    try:
        rows = meta_conn.execute(
            "SELECT started_at, finished_at, success, summary FROM sync_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return {
            "runs": [
                {
                    "started_at": r[0], "finished_at": r[1],
                    "success": bool(r[2]), "summary": json.loads(r[3]),
                }
                for r in rows
            ]
        }
    finally:
        meta_conn.close()
