# -*- coding: utf-8 -*-
from __future__ import unicode_literals

"""Cache SQLite persistente para dados públicos de catálogo e Pluto.

O cache é opcional: falha de arquivo, lock ou banco danificado nunca interrompe
navegação/reprodução. Cada operação abre e fecha sua própria conexão para ser
segura entre o plugin, o service e portas Kodi com armazenamento mais restrito.
"""

import hashlib
import json
import os
import sqlite3
import time

from resources.lib.common import PROFILE_DIR, ensure_dir, log

CACHE_DIR = ensure_dir(os.path.join(PROFILE_DIR, 'navigation_cache'))
CACHE_DB_FILE = os.path.join(CACHE_DIR, 'oneplay_navigation_data.sqlite')
CACHE_SCHEMA_VERSION = 'oneplay_navigation_data_v2'
MAX_VALUE_BYTES = 8 * 1024 * 1024
CACHE_DB_TIMEOUT_SECONDS = 1.25
CACHE_DB_BUSY_TIMEOUT_MS = 1250
CACHE_SCHEMA_READY = False


def _to_text(value):
    if value is None:
        return ''
    try:
        if isinstance(value, bytes):
            return value.decode('utf-8', 'replace')
    except Exception:
        pass
    try:
        return str(value)
    except Exception:
        return ''


def make_key(value):
    try:
        raw = _to_text(value).encode('utf-8')
        return hashlib.sha256(raw).hexdigest()
    except Exception:
        return hashlib.md5(_to_text(value).encode('utf-8')).hexdigest()


def _close_quietly(conn):
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


def _is_database_damage(exc):
    text = _to_text(exc).lower()
    markers = (
        'file is not a database', 'database disk image is malformed',
        'database corrupt', 'malformed database schema',
        'incomplete oneplay catalog cache schema', 'malformed oneplay catalog cache schema',
        'unsupported file format',
    )
    return any(marker in text for marker in markers)


def _quarantine_database(path):
    """Isola somente banco realmente danificado, preservando diagnóstico local."""
    stamp = '{}.{}'.format(int(time.time()), os.getpid() if hasattr(os, 'getpid') else 0)
    moved = False
    for suffix in ('', '-wal', '-shm', '-journal'):
        source = path + suffix
        if not os.path.exists(source):
            continue
        target = '{}.corrupt.{}{}'.format(path, stamp, suffix)
        try:
            if hasattr(os, 'replace'):
                os.replace(source, target)
            else:
                os.rename(source, target)
            moved = True
        except Exception:
            # Não apaga arquivo que não conseguiu mover: usar cache vazio é
            # preferível a destruir evidência ou falhar o addon.
            pass
    return moved


def _table_columns(conn, table_name):
    try:
        return set(_to_text(row[1]) for row in conn.execute('PRAGMA table_info({})'.format(table_name)))
    except Exception:
        return set()


def _schema_state(conn):
    try:
        wanted = set(('cache_entries', 'cache_meta'))
        names = set(_to_text(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('cache_entries','cache_meta')"
        ))
        if not names:
            return 'empty'
        if not wanted.issubset(names):
            return 'incomplete'
        entries = _table_columns(conn, 'cache_entries')
        meta = _table_columns(conn, 'cache_meta')
        required_entries = set(('namespace', 'cache_key', 'value_json', 'content_hash', 'updated_at', 'expires_at', 'etag', 'last_modified'))
        required_meta = set(('meta_key', 'meta_value'))
        return 'ready' if required_entries.issubset(entries) and required_meta.issubset(meta) else 'incomplete'
    except Exception:
        return 'broken'


def _create_schema(conn):
    conn.execute(
        'CREATE TABLE IF NOT EXISTS cache_entries ('
        'namespace TEXT NOT NULL, cache_key TEXT NOT NULL, value_json TEXT NOT NULL, '
        'content_hash TEXT NOT NULL, updated_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, '
        "etag TEXT NOT NULL DEFAULT '', last_modified TEXT NOT NULL DEFAULT '', "
        'PRIMARY KEY(namespace, cache_key))'
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_cache_entries_expiry ON cache_entries(namespace, expires_at)')
    conn.execute(
        'CREATE TABLE IF NOT EXISTS cache_meta ('
        'meta_key TEXT PRIMARY KEY, meta_value TEXT NOT NULL)'
    )
    conn.execute('INSERT OR REPLACE INTO cache_meta(meta_key, meta_value) VALUES (?, ?)', ('schema_version', CACHE_SCHEMA_VERSION))
    conn.commit()


def _connect(allow_recovery=True):
    """Abre banco e recupera apenas corrupção comprovada.

    Lock/transient I/O não aciona reset: nesses casos a operação retorna ao
    chamador e o addon segue sem cache, em vez de remover um banco saudável.
    """
    global CACHE_SCHEMA_READY
    ensure_dir(CACHE_DIR)
    conn = None
    try:
        conn = sqlite3.connect(CACHE_DB_FILE, timeout=CACHE_DB_TIMEOUT_SECONDS)
        try:
            conn.execute('PRAGMA busy_timeout={}'.format(int(CACHE_DB_BUSY_TIMEOUT_MS)))
        except Exception:
            pass
        state = _schema_state(conn)
        if state == 'empty':
            _create_schema(conn)
        elif state == 'incomplete':
            raise sqlite3.DatabaseError('incomplete OnePlay catalog cache schema')
        elif state == 'broken':
            raise sqlite3.DatabaseError('malformed OnePlay catalog cache schema')
        elif not CACHE_SCHEMA_READY:
            # Atualiza a versão sem fazer DDL a cada clique.
            conn.execute('INSERT OR REPLACE INTO cache_meta(meta_key, meta_value) VALUES (?, ?)', ('schema_version', CACHE_SCHEMA_VERSION))
            conn.commit()
        CACHE_SCHEMA_READY = True
        return conn
    except Exception as exc:
        _close_quietly(conn)
        CACHE_SCHEMA_READY = False
        if allow_recovery and _is_database_damage(exc):
            if _quarantine_database(CACHE_DB_FILE):
                return _connect(allow_recovery=False)
        raise


def get_record(namespace, cache_key):
    conn = None
    try:
        conn = _connect()
        row = conn.execute(
            'SELECT value_json, content_hash, updated_at, expires_at, etag, last_modified '
            'FROM cache_entries WHERE namespace=? AND cache_key=?',
            (_to_text(namespace), _to_text(cache_key))
        ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
        except Exception:
            return None
        return {
            'value': value,
            'content_hash': _to_text(row[1]),
            'updated_at': int(row[2] or 0),
            'expires_at': int(row[3] or 0),
            'etag': _to_text(row[4]),
            'last_modified': _to_text(row[5]),
        }
    except Exception as exc:
        log('Cache persistente leitura falhou: {}'.format(exc), level=2)
        return None
    finally:
        _close_quietly(conn)


def is_fresh(record, now_ts=None):
    if not record:
        return False
    if now_ts is None:
        now_ts = int(time.time())
    try:
        return int(record.get('expires_at') or 0) > int(now_ts)
    except Exception:
        return False


def set_record(namespace, cache_key, value, ttl_seconds, etag='', last_modified=''):
    """Grava JSON; não persiste payload desproporcional ao propósito do cache."""
    conn = None
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(',', ':'))
        encoded = payload.encode('utf-8')
        if len(encoded) > MAX_VALUE_BYTES:
            log('Cache persistente ignorado: payload muito grande ({} bytes).'.format(len(encoded)), level=2)
            return False
        now_ts = int(time.time())
        ttl = max(1, int(ttl_seconds or 1))
        content_hash = hashlib.sha256(encoded).hexdigest()
        conn = _connect()
        existing = conn.execute(
            'SELECT content_hash FROM cache_entries WHERE namespace=? AND cache_key=?',
            (_to_text(namespace), _to_text(cache_key))
        ).fetchone()
        if existing and _to_text(existing[0]) == content_hash:
            conn.execute(
                'UPDATE cache_entries SET updated_at=?, expires_at=?, etag=?, last_modified=? '
                'WHERE namespace=? AND cache_key=?',
                (now_ts, now_ts + ttl, _to_text(etag), _to_text(last_modified),
                 _to_text(namespace), _to_text(cache_key))
            )
        else:
            conn.execute(
                'INSERT OR REPLACE INTO cache_entries('
                'namespace, cache_key, value_json, content_hash, updated_at, expires_at, etag, last_modified'
                ') VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (_to_text(namespace), _to_text(cache_key), payload, content_hash,
                 now_ts, now_ts + ttl, _to_text(etag), _to_text(last_modified))
            )
        conn.commit()
        return True
    except Exception as exc:
        log('Cache persistente gravação falhou: {}'.format(exc), level=2)
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        return False
    finally:
        _close_quietly(conn)


def touch_record(namespace, cache_key, ttl_seconds, etag=None, last_modified=None):
    conn = None
    try:
        now_ts = int(time.time())
        ttl = max(1, int(ttl_seconds or 1))
        conn = _connect()
        if etag is None and last_modified is None:
            conn.execute(
                'UPDATE cache_entries SET updated_at=?, expires_at=? WHERE namespace=? AND cache_key=?',
                (now_ts, now_ts + ttl, _to_text(namespace), _to_text(cache_key))
            )
        else:
            current = conn.execute(
                'SELECT etag, last_modified FROM cache_entries WHERE namespace=? AND cache_key=?',
                (_to_text(namespace), _to_text(cache_key))
            ).fetchone() or ('', '')
            conn.execute(
                'UPDATE cache_entries SET updated_at=?, expires_at=?, etag=?, last_modified=? '
                'WHERE namespace=? AND cache_key=?',
                (now_ts, now_ts + ttl,
                 _to_text(etag if etag is not None else current[0]),
                 _to_text(last_modified if last_modified is not None else current[1]),
                 _to_text(namespace), _to_text(cache_key))
            )
        conn.commit()
        return True
    except Exception:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        return False
    finally:
        _close_quietly(conn)


def clear_namespace(namespace):
    conn = None
    try:
        conn = _connect()
        cursor = conn.execute('DELETE FROM cache_entries WHERE namespace=?', (_to_text(namespace),))
        conn.commit()
        return int(cursor.rowcount or 0)
    except Exception:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        _close_quietly(conn)


def clear_all():
    conn = None
    try:
        conn = _connect()
        cursor = conn.execute('DELETE FROM cache_entries')
        conn.commit()
        return int(cursor.rowcount or 0)
    except Exception:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        _close_quietly(conn)


def prune_expired(grace_seconds=7 * 86400):
    conn = None
    try:
        cutoff = int(time.time()) - max(0, int(grace_seconds or 0))
        conn = _connect()
        cursor = conn.execute('DELETE FROM cache_entries WHERE expires_at < ?', (cutoff,))
        conn.commit()
        return int(cursor.rowcount or 0)
    except Exception:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        return 0
    finally:
        _close_quietly(conn)
