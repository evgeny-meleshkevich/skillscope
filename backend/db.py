import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('SKILLSCOPE_DATA_DIR', ROOT / 'data'))

def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

@contextmanager
def connect():
    DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(DATA, 0o700)
    db = sqlite3.connect(DATA / 'skillscope.db', timeout=30)
    os.chmod(DATA / 'skillscope.db', 0o600)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

DEFAULTS = {'scheduler': True, 'interval_hours': 24, 'enabled_sources': ['arbeitnow', 'remotive'],
            'hh_area': '113', 'hh_limit': 100, 'arbeitnow_pages': 2}

def init():
    with connect() as db:
        db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, source TEXT NOT NULL, started TEXT NOT NULL,
            finished TEXT, status TEXT NOT NULL, message TEXT DEFAULT '', received INTEGER DEFAULT 0,
            accepted INTEGER DEFAULT 0, raw_hash TEXT, raw_path TEXT, scope TEXT NOT NULL DEFAULT '{}');
        CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, UNIQUE(source,external_id));
        CREATE TABLE IF NOT EXISTS versions(id INTEGER PRIMARY KEY, job TEXT REFERENCES jobs(id), hash TEXT NOT NULL,
            payload TEXT NOT NULL, created TEXT NOT NULL, UNIQUE(job,hash));
        CREATE TABLE IF NOT EXISTS run_items(run INTEGER REFERENCES runs(id), job TEXT REFERENCES jobs(id),
            version INTEGER REFERENCES versions(id), PRIMARY KEY(run,job));
        CREATE TABLE IF NOT EXISTS skill_evidence(version INTEGER REFERENCES versions(id), skill TEXT NOT NULL,
            kind TEXT NOT NULL, quote TEXT NOT NULL, method TEXT NOT NULL,
            PRIMARY KEY(version,skill,kind,quote));
        CREATE INDEX IF NOT EXISTS evidence_skill ON skill_evidence(skill);
        CREATE TABLE IF NOT EXISTS curricula(id INTEGER PRIMARY KEY,title TEXT NOT NULL,text TEXT NOT NULL,
            levels TEXT NOT NULL,created TEXT NOT NULL, parent INTEGER REFERENCES curricula(id));
        CREATE TABLE IF NOT EXISTS reports(id INTEGER PRIMARY KEY,title TEXT NOT NULL,created TEXT NOT NULL,
            filters TEXT NOT NULL,snapshot TEXT NOT NULL);
        ''')
        for key, value in DEFAULTS.items():
            db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', (key, json.dumps(value)))
        db.execute("UPDATE runs SET status='error', finished=?, message='Приложение остановлено до завершения сбора' WHERE status='running'", (now(),))

def settings():
    with connect() as db:
        return {r['key']: json.loads(r['value']) for r in db.execute('SELECT * FROM settings')}

def save_settings(values):
    with connect() as db:
        for key, value in values.items():
            db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                       (key, json.dumps(value)))

