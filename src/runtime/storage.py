# ── pluggable storage backend for memory (chat sessions) + checkpoints ──────
# Per-graph choice via CONFIG["storage"]["backend"]:
#   "disk"     — the original JSON-file layout (sessions/<id>.json, active.txt,
#                checkpoints/<tid>.json under BASE_DIR). The DEFAULT, byte-identical.
#   "sqlite"   — a single SQLite file (stdlib sqlite3; no extra dependency).
#   "postgres" — PostgreSQL via psycopg (an OPTIONAL dependency, lazily imported);
#                DSN from config storage.dsn, else $DATABASE_URL / $AGENT_DB_DSN.
# Both runtime/history.py (sessions = "memory") and runtime/checkpoint.py route
# their persistence through the _STORE object built at the bottom of this block,
# so swapping the backend is a single config key — the disk path is unchanged.
# This fragment is inlined into the agent module just before the history block,
# so it only uses names the host module already defines (BASE_DIR, CONFIG, and
# os/json/re/time/threading). It never raises out of a backend call — a DB hiccup
# degrades like a missing file (read -> empty, write/delete -> no-op).


def _fs_safe(name):
    """A Windows-legal, collision-free filename stem for a session/checkpoint id.
    Session ids coined by the web server / gateways contain ':' (e.g.
    "dingtalk:conv:staff", "feishu:chat:openid") which is ILLEGAL in a Windows
    filename — writing it raw raises OSError (or silently creates an NTFS alternate
    data stream). Already-safe ids (the timestamp ids from before per-session) match
    [\\w.-]+ and are returned unchanged, so existing files keep their names (no
    migration); anything else is percent-escaped. The escape is reversible and thus
    injective, so two distinct ids can never map to the same file (a naive
    re.sub(...,"_") would collide and merge two users' history)."""
    name = name or ""
    if re.fullmatch(r"[\w.\-]+", name):
        return name
    return re.sub(r"[^\w.\-]", lambda m: "%%%02x" % ord(m.group()), name)


def _atomic_write(path, text):
    """Crash-safe write: temp file + os.replace, with a Windows retry/fallback.
    Shared disk primitive (sessions, active.txt, checkpoints)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    for i in range(5):
        try:
            os.replace(tmp, path)
            return
        except OSError:
            if i == 4:
                break
            time.sleep(0.05 * (i + 1))
    try:                                  # last resort: direct (non-atomic) write
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


class _DiskStore:
    """Original behavior: sessions/<id>.json + sessions/active.txt +
    checkpoints/<tid>.json under BASE_DIR. Byte-identical to the pre-backend code
    (session JSON keeps indent=2/ensure_ascii=False; checkpoints stay compact)."""

    def __init__(self):
        self._sessions_dir = os.path.join(BASE_DIR, "sessions")
        self._checkpoints_dir = os.path.join(BASE_DIR, "checkpoints")

    def _spath(self, sid):
        return os.path.join(self._sessions_dir, _fs_safe(sid) + ".json")

    def _apath(self):
        return os.path.join(self._sessions_dir, "active.txt")

    def _cpath(self, tid):
        safe = _fs_safe(tid) or "default"
        return os.path.join(self._checkpoints_dir, safe + ".json")

    def session_read(self, sid):
        try:
            with open(self._spath(sid), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def session_write(self, sid, rec):
        _atomic_write(self._spath(sid),
                      json.dumps(rec, indent=2, ensure_ascii=False))

    def session_list(self):
        # Read each file DIRECTLY and take the id from the record — the filename
        # stem is now an escaped form of the id (see _fs_safe), so it can't be fed
        # back through session_read()/_spath() as if it were the raw id.
        out = []
        try:
            names = [n for n in os.listdir(self._sessions_dir) if n.endswith(".json")]
        except OSError:
            names = []
        for n in names:
            try:
                with open(os.path.join(self._sessions_dir, n), encoding="utf-8") as f:
                    rec = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(rec, dict) and rec.get("id"):
                out.append(rec)
        out.sort(key=lambda r: r.get("updated", ""), reverse=True)
        return out

    def session_delete(self, sid):
        try:
            os.remove(self._spath(sid))
        except OSError:
            pass

    def active_get(self):
        try:
            with open(self._apath(), encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            return ""

    def active_set(self, sid):
        _atomic_write(self._apath(), sid)

    def ckpt_read(self, tid):
        try:
            with open(self._cpath(tid), encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else None
        except (OSError, json.JSONDecodeError):
            return None

    def ckpt_write(self, tid, snap):
        try:
            _atomic_write(self._cpath(tid),
                          json.dumps(snap, ensure_ascii=False, default=str))
        except OSError:
            pass

    def ckpt_delete(self, tid):
        try:
            os.remove(self._cpath(tid))
        except OSError:
            pass


class _SqlStore:
    """Shared logic for the SQL backends. Rows mirror the disk records: a session
    row is the full session dict as JSON; a checkpoint row is the snapshot JSON;
    the active session id lives in a one-row `meta` table. Subclasses supply a
    connection/exec strategy (_exec) and the upsert dialect (_upsert). Every public
    method is fail-soft: a DB error reads as empty/None and writes/deletes no-op,
    so a flaky database can never crash the run worker (same contract as disk)."""

    _PH = "?"           # parameter placeholder; postgres overrides to "%s"

    def _exec(self, sql, params=(), fetch=None):
        raise NotImplementedError

    def _upsert(self, table, cols, key, params):
        raise NotImplementedError

    def _init_schema(self):
        self._exec("CREATE TABLE IF NOT EXISTS sessions "
                   "(id TEXT PRIMARY KEY, updated TEXT, data TEXT)")
        self._exec("CREATE TABLE IF NOT EXISTS checkpoints "
                   "(tid TEXT PRIMARY KEY, data TEXT)")
        self._exec("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")

    def session_read(self, sid):
        try:
            r = self._exec("SELECT data FROM sessions WHERE id=" + self._PH,
                           (sid,), fetch="one")
        except Exception:
            return {}
        if not r:
            return {}
        try:
            d = json.loads(r[0])
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def session_write(self, sid, rec):
        try:
            self._upsert("sessions", ("id", "updated", "data"), "id",
                         (sid, rec.get("updated", ""),
                          json.dumps(rec, ensure_ascii=False)))
        except Exception:
            pass

    def session_list(self):
        try:
            rows = self._exec("SELECT data FROM sessions", (), fetch="all") or []
        except Exception:
            return []
        out = []
        for row in rows:
            try:
                d = json.loads(row[0])
            except Exception:
                continue
            if isinstance(d, dict) and d.get("id"):
                out.append(d)
        out.sort(key=lambda r: r.get("updated", ""), reverse=True)
        return out

    def session_delete(self, sid):
        try:
            self._exec("DELETE FROM sessions WHERE id=" + self._PH, (sid,))
        except Exception:
            pass

    def active_get(self):
        try:
            r = self._exec("SELECT v FROM meta WHERE k=" + self._PH,
                           ("active",), fetch="one")
        except Exception:
            return ""
        return (r[0] if r else "") or ""

    def active_set(self, sid):
        try:
            self._upsert("meta", ("k", "v"), "k", ("active", sid))
        except Exception:
            pass

    def ckpt_read(self, tid):
        try:
            r = self._exec("SELECT data FROM checkpoints WHERE tid=" + self._PH,
                           (tid or "default",), fetch="one")
        except Exception:
            return None
        if not r:
            return None
        try:
            d = json.loads(r[0])
            return d if isinstance(d, dict) else None
        except Exception:
            return None

    def ckpt_write(self, tid, snap):
        try:
            self._upsert("checkpoints", ("tid", "data"), "tid",
                         (tid or "default",
                          json.dumps(snap, ensure_ascii=False, default=str)))
        except Exception:
            pass

    def ckpt_delete(self, tid):
        try:
            self._exec("DELETE FROM checkpoints WHERE tid=" + self._PH,
                       (tid or "default",))
        except Exception:
            pass


class _SqliteStore(_SqlStore):
    """SQLite backend (stdlib). One DB file under BASE_DIR (or an absolute path).
    A fresh connection per op (cheap, local) keeps it thread-safe across the run
    worker and GUI threads; a lock serializes writers to avoid 'database locked'."""

    _PH = "?"

    def __init__(self, path):
        import sqlite3
        self._sqlite3 = sqlite3
        self._lock = threading.Lock()
        self.path = path if os.path.isabs(path) else os.path.join(BASE_DIR, path)
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        self._init_schema()

    def _exec(self, sql, params=(), fetch=None):
        with self._lock:
            conn = self._sqlite3.connect(self.path)
            try:
                cur = conn.execute(sql, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                conn.commit()
                return None
            finally:
                conn.close()

    def _upsert(self, table, cols, key, params):
        ph = ", ".join(["?"] * len(cols))
        self._exec("INSERT OR REPLACE INTO " + table
                   + " (" + ", ".join(cols) + ") VALUES (" + ph + ")", params)


class _PostgresStore(_SqlStore):
    """PostgreSQL backend via psycopg (optional dependency, lazily imported). DSN
    from storage.dsn, else $DATABASE_URL / $AGENT_DB_DSN. A cached autocommit
    connection guarded by a lock; reconnects on error."""

    _PH = "%s"

    def __init__(self, dsn):
        import psycopg
        self._psycopg = psycopg
        self.dsn = (dsn or os.environ.get("DATABASE_URL")
                    or os.environ.get("AGENT_DB_DSN") or "")
        if not self.dsn:
            raise RuntimeError("postgres storage selected but no DSN "
                               "(set config storage.dsn or $DATABASE_URL)")
        self._lock = threading.Lock()
        self._conn = None
        self._init_schema()

    def _connect(self):
        if self._conn is None or getattr(self._conn, "closed", False):
            self._conn = self._psycopg.connect(self.dsn, autocommit=True)
        return self._conn

    def _exec(self, sql, params=(), fetch=None):
        with self._lock:
            try:
                cur = self._connect().execute(sql, params)
                if fetch == "one":
                    return cur.fetchone()
                if fetch == "all":
                    return cur.fetchall()
                return None
            except Exception:
                self._conn = None        # force reconnect on the next call
                raise

    def _upsert(self, table, cols, key, params):
        ph = ", ".join(["%s"] * len(cols))
        sets = ", ".join(c + "=EXCLUDED." + c for c in cols if c != key)
        self._exec("INSERT INTO " + table + " (" + ", ".join(cols) + ") VALUES ("
                   + ph + ") ON CONFLICT (" + key + ") DO UPDATE SET " + sets,
                   params)


def _make_store(cfg):
    """Build the storage backend from CONFIG["storage"]; fall back to disk if a
    SQL backend can't initialize (missing driver, bad DSN) so the agent still runs."""
    cfg = cfg if isinstance(cfg, dict) else {}
    backend = (cfg.get("backend") or "disk").lower()
    try:
        if backend == "sqlite":
            return _SqliteStore(cfg.get("sqlite_path") or "memory.db")
        if backend in ("postgres", "postgresql", "pg"):
            return _PostgresStore(cfg.get("dsn") or "")
    except Exception as _e:
        try:
            print("[storage] " + backend + " init failed (" + str(_e)
                  + "); falling back to disk")
        except Exception:
            pass
    return _DiskStore()


_STORE = _make_store(CONFIG.get("storage") if isinstance(CONFIG, dict) else None)
