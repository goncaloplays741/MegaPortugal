# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import base64
import calendar
import gzip
import gc
import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import time
import unicodedata

try:
    from xml.etree import cElementTree as ET
except Exception:
    from xml.etree import ElementTree as ET

try:
    from kodi_six import xbmcaddon
except ImportError:
    import xbmcaddon

from resources.lib.common import make_session, log, DEFAULT_TIMEOUT, PROFILE_DIR, ensure_dir


MASTER_URL = base64.b64decode('aHR0cHM6Ly9vbmVwbGF5aGQuY29tL2xpc3Rhc19vbmVwbGF5L21hc3Rlci50eHQ=').decode('utf-8')
SESSION = make_session()
M3U_META_CACHE = {}
M3U_SESSION_CACHE_SECONDS = 120
LISTS_CACHE = {'items': [], 'fetched_at': 0}
LISTS_CACHE_SECONDS = 120

# Cache persistente da navegação. Diferente do cache EPG, este banco guarda
# apenas a estrutura master -> listas -> grupos -> canais. Toda fonte é
# revalidada antes de ser reutilizada fora da janela curta; se o servidor
# confirmar 304 ou entregar o mesmo hash, o parser e as tabelas não são
# reconstruídos.
NAV_CACHE_DIR = ensure_dir(os.path.join(PROFILE_DIR, 'navigation_cache'))
NAV_DB_FILE = os.path.join(NAV_CACHE_DIR, 'm3u_navigation.sqlite')
NAV_SCHEMA_VERSION = 'm3u_navigation_sqlite_v2_unique_channel_ids'
NAV_VALIDATION_SECONDS = 120
NAV_STALE_MAX_AGE_SECONDS = 30 * 86400
NAV_DB_TIMEOUT_SECONDS = 1.0
NAV_DB_BUSY_TIMEOUT_MS = 1000
NAV_SCHEMA_READY = False
EPG_CACHE_DIR = ensure_dir(os.path.join(PROFILE_DIR, 'epg_cache'))
EPG_XMLTV_DIR = ensure_dir(EPG_CACHE_DIR)
EPG_INDEX_DIR = ensure_dir(EPG_CACHE_DIR)  # compatibilidade: diretório único do cache EPG
EPG_SQLITE_DIR = EPG_INDEX_DIR
EPG_CACHE_VERSION = 'xmltv_hoje_amanha_v11_sqlite_local_categoria_brt'
EPG_SQLITE_SCHEMA_VERSION = 'epg_sqlite_v2'
EPG_XMLTV_META_VERSION = 'xmltv_meta_v1_coverage_brt'
EPG_SERVICE_MANIFEST_VERSION = 'service_manifest_v1'
EPG_SERVICE_MANIFEST_FILE = os.path.join(EPG_CACHE_DIR, 'service_manifest.json')
BRAZIL_UTC_OFFSET_SECONDS = -3 * 3600
EPG_INDEX_DAYS = 2
EPG_MIN_FUTURE_COVERAGE_SECONDS = 6 * 3600
EPG_MIN_TOMORROW_COVERAGE_SECONDS = 12 * 3600
EPG_SHORT_FUTURE_REFRESH_GRACE_SECONDS = 2 * 3600

EPG_ALIAS_REPLACEMENTS = (
    ('+', ' plus '),
    ('&', ' and '),
)

EPG_NOISE_WORDS = set([
    'hd', 'sd', 'fhd', 'uhd', '4k', 'fullhd', 'full', 'hevc', 'h264', 'h265',
    'mpeg', 'aac', 'ac3', 'latino', 'dublado', 'dual', 'audio', 'backup',
    'opcao', 'option', 'canal', 'channel', 'ao', 'vivo', 'live', 'br', 'brazil',
    'brasil', 'la', 'latam', 'latin', 'america', 'east', 'west', 'feed'
])

EPG_PREFIX_WORDS = set(['tv'])


def _get_epg_ttl():
    try:
        addon = xbmcaddon.Addon()
        raw = _to_text(addon.getSetting('tv_epg_cache_days')).strip()
        days = int(raw) + 1 if raw != '' else 1
    except Exception:
        days = 1
    if days < 1:
        days = 1
    return days * 86400


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


def _normalize_key(value):
    text = _to_text(value).strip().lower()
    if not text:
        return ''
    try:
        text = unicodedata.normalize('NFKD', text)
        text = ''.join(ch for ch in text if not unicodedata.combining(ch))
    except Exception:
        pass
    text = re.sub(r'[^a-z0-9]+', ' ', text)
    return ' '.join(text.split())


def _tokenize_key(value):
    normalized = _normalize_key(value)
    return [token for token in normalized.split() if token]


def _compress_tokens(tokens):
    if not tokens:
        return ''
    return ' '.join([token for token in tokens if token]).strip()


def _channel_alias_forms(value):
    normalized = _normalize_key(value)
    if not normalized:
        return []

    aliases = []

    def add_alias(candidate):
        candidate = _normalize_key(candidate)
        if candidate and candidate not in aliases:
            aliases.append(candidate)

    add_alias(normalized)

    raw = normalized
    for before, after in EPG_ALIAS_REPLACEMENTS:
        raw = raw.replace(before, after)
    add_alias(raw)

    raw = re.sub(r'\b(tv)\b\s+', '', raw).strip()
    add_alias(raw)

    tokens = _tokenize_key(normalized)
    if tokens:
        cleaned = [token for token in tokens if token not in EPG_NOISE_WORDS]
        add_alias(_compress_tokens(cleaned))

        trimmed = list(cleaned)
        while trimmed and trimmed[0] in EPG_PREFIX_WORDS:
            trimmed = trimmed[1:]
        add_alias(_compress_tokens(trimmed))

        if len(cleaned) >= 2:
            add_alias(_compress_tokens([cleaned[0], cleaned[-1]]))

        if cleaned:
            digitless = [re.sub(r'\d+$', '', token) or token for token in cleaned]
            add_alias(_compress_tokens(digitless))

    return aliases


def _extract_attr(line, attr_name):
    pattern = r'%s="([^"]*)"' % re.escape(attr_name)
    match = re.search(pattern, line, re.IGNORECASE)
    return match.group(1).strip() if match else ''


def _hash_text(text):
    try:
        return hashlib.sha256(_to_text(text).encode('utf-8')).hexdigest()
    except Exception:
        return ''


def _nav_close(conn):
    try:
        if conn is not None:
            conn.close()
    except Exception:
        pass


def _nav_database_damage(exc):
    text = _to_text(exc).lower()
    return any(marker in text for marker in (
        'file is not a database', 'database disk image is malformed',
        'database corrupt', 'malformed database schema',
        'incomplete mega portugal navigation schema', 'malformed mega portugal navigation schema',
        'unsupported file format'
    ))


def _nav_quarantine_database():
    """Isola banco de navegação danificado sem tocar XMLTV/EPG."""
    stamp = '{}.{}'.format(int(time.time()), os.getpid() if hasattr(os, 'getpid') else 0)
    moved = False
    for suffix in ('', '-wal', '-shm', '-journal'):
        source = NAV_DB_FILE + suffix
        if not os.path.exists(source):
            continue
        target = '{}.corrupt.{}{}'.format(NAV_DB_FILE, stamp, suffix)
        try:
            if hasattr(os, 'replace'):
                os.replace(source, target)
            else:
                os.rename(source, target)
            moved = True
        except Exception:
            pass
    return moved


def _nav_table_columns(conn, table_name):
    try:
        return set(_to_text(row[1]) for row in conn.execute('PRAGMA table_info({})'.format(table_name)))
    except Exception:
        return set()


def _nav_schema_state(conn):
    try:
        required_tables = set(('nav_sources', 'nav_master_items', 'nav_groups', 'nav_channels'))
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('nav_sources','nav_master_items','nav_groups','nav_channels')"
        ).fetchall()
        found = set(_to_text(row[0]) for row in rows if row)
        if not found:
            return 'empty'
        if not required_tables.issubset(found):
            return 'incomplete'
        required_columns = {
            'nav_sources': set(('source_url', 'source_type', 'content_hash', 'etag', 'last_modified', 'epg_url', 'fetched_at', 'checked_at', 'payload_size', 'schema_version')),
            'nav_master_items': set(('master_url', 'position', 'item_url')),
            'nav_groups': set(('source_url', 'group_name', 'position', 'channel_count')),
            'nav_channels': set(('source_url', 'group_name', 'position', 'name', 'logo', 'tvg_id', 'tvg_name', 'channel_id', 'stream_url')),
        }
        for table_name, wanted in required_columns.items():
            if not wanted.issubset(_nav_table_columns(conn, table_name)):
                return 'incomplete'
        return 'ready'
    except Exception:
        return 'broken'


def _nav_create_schema(conn):
    conn.execute(
        'CREATE TABLE IF NOT EXISTS nav_sources ('
        'source_url TEXT PRIMARY KEY, source_type TEXT NOT NULL, content_hash TEXT NOT NULL, '
        "etag TEXT NOT NULL DEFAULT '', last_modified TEXT NOT NULL DEFAULT '', "
        "epg_url TEXT NOT NULL DEFAULT '', fetched_at INTEGER NOT NULL, checked_at INTEGER NOT NULL, "
        'payload_size INTEGER NOT NULL DEFAULT 0, schema_version TEXT NOT NULL)'
    )
    conn.execute(
        'CREATE TABLE IF NOT EXISTS nav_master_items ('
        'master_url TEXT NOT NULL, position INTEGER NOT NULL, item_url TEXT NOT NULL, '
        'PRIMARY KEY(master_url, position))'
    )
    conn.execute(
        'CREATE TABLE IF NOT EXISTS nav_groups ('
        'source_url TEXT NOT NULL, group_name TEXT NOT NULL, position INTEGER NOT NULL, '
        'channel_count INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(source_url, group_name))'
    )
    conn.execute(
        'CREATE TABLE IF NOT EXISTS nav_channels ('
        'source_url TEXT NOT NULL, group_name TEXT NOT NULL, position INTEGER NOT NULL, '
        "name TEXT NOT NULL, logo TEXT NOT NULL DEFAULT '', tvg_id TEXT NOT NULL DEFAULT '', "
        "tvg_name TEXT NOT NULL DEFAULT '', channel_id TEXT NOT NULL, stream_url TEXT NOT NULL, "
        'PRIMARY KEY(source_url, group_name, position))'
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_nav_groups_source_position ON nav_groups(source_url, position)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_nav_channels_source_group_position ON nav_channels(source_url, group_name, position)')
    conn.commit()


def _nav_connect(allow_recovery=True):
    """Abre navegação SQLite de modo seguro entre UI, service e plataformas.

    Um arquivo corrompido é isolado e recriado. Lock temporário não aciona
    limpeza: a UI usa a própria rota de fallback sem destruir um cache válido.
    """
    global NAV_SCHEMA_READY
    ensure_dir(NAV_CACHE_DIR)
    conn = None
    try:
        conn = sqlite3.connect(NAV_DB_FILE, timeout=NAV_DB_TIMEOUT_SECONDS)
        try:
            conn.execute('PRAGMA busy_timeout={}'.format(int(NAV_DB_BUSY_TIMEOUT_MS)))
        except Exception:
            pass
        state = _nav_schema_state(conn)
        if state == 'empty':
            _nav_create_schema(conn)
        elif state == 'incomplete':
            raise sqlite3.DatabaseError('incomplete Mega Portugal navigation schema')
        elif state == 'broken':
            raise sqlite3.DatabaseError('malformed Mega Portugal navigation schema')
        NAV_SCHEMA_READY = True
        return conn
    except Exception as exc:
        _nav_close(conn)
        NAV_SCHEMA_READY = False
        if allow_recovery and _nav_database_damage(exc):
            if _nav_quarantine_database():
                return _nav_connect(allow_recovery=False)
        raise


def _nav_source_from_row(row):
    if not row:
        return None
    return {
        'source_url': _to_text(row[0]),
        'source_type': _to_text(row[1]),
        'content_hash': _to_text(row[2]),
        'etag': _to_text(row[3]),
        'last_modified': _to_text(row[4]),
        'epg_url': _to_text(row[5]),
        'fetched_at': int(row[6] or 0),
        'checked_at': int(row[7] or 0),
        'payload_size': int(row[8] or 0),
        'schema_version': _to_text(row[9]),
    }


def _nav_source_schema_current(source):
    """Confirma que o cache foi montado pela versão atual do índice.

    A estrutura da tabela pode continuar válida entre versões, mas os dados
    internos também têm contrato. Quando esse contrato muda, a fonte é
    rebaixada para revalidação completa uma única vez, sem apagar o último
    cache utilizável caso a rede esteja indisponível.
    """
    try:
        return bool(source) and _to_text(source.get('schema_version', '')).strip() == NAV_SCHEMA_VERSION
    except Exception:
        return False


def _nav_read_source(source_url):
    conn = None
    try:
        conn = _nav_connect()
        row = conn.execute(
            'SELECT source_url, source_type, content_hash, etag, last_modified, epg_url, '
            'fetched_at, checked_at, payload_size, schema_version '
            'FROM nav_sources WHERE source_url=?',
            (_to_text(source_url).strip(),)
        ).fetchone()
        return _nav_source_from_row(row)
    except Exception as exc:
        log('Navegação cache leitura falhou: {}'.format(exc), level=2)
        return None
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _nav_touch_source(source_url, etag=None, last_modified=None, checked_at=None):
    conn = None
    try:
        now_ts = int(checked_at or time.time())
        conn = _nav_connect()
        if etag is None and last_modified is None:
            conn.execute('UPDATE nav_sources SET checked_at=? WHERE source_url=?', (now_ts, _to_text(source_url).strip()))
        else:
            existing = conn.execute(
                'SELECT etag, last_modified FROM nav_sources WHERE source_url=?',
                (_to_text(source_url).strip(),)
            ).fetchone() or ('', '')
            conn.execute(
                'UPDATE nav_sources SET checked_at=?, etag=?, last_modified=? WHERE source_url=?',
                (now_ts, _to_text(etag if etag is not None else existing[0]),
                 _to_text(last_modified if last_modified is not None else existing[1]),
                 _to_text(source_url).strip())
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
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _nav_source_is_recent(source, now_ts=None):
    if not source or not _nav_source_schema_current(source):
        return False
    if now_ts is None:
        now_ts = int(time.time())
    try:
        return (int(now_ts) - int(source.get('checked_at') or 0)) <= int(NAV_VALIDATION_SECONDS)
    except Exception:
        return False


def _nav_source_is_usable(source, now_ts=None):
    if not source:
        return False
    if now_ts is None:
        now_ts = int(time.time())
    try:
        return (int(now_ts) - int(source.get('fetched_at') or 0)) <= int(NAV_STALE_MAX_AGE_SECONDS)
    except Exception:
        return False


def _nav_response_headers(response):
    headers = getattr(response, 'headers', {}) or {}
    return _to_text(headers.get('ETag', '') or headers.get('Etag', '')), _to_text(headers.get('Last-Modified', ''))


def _get_text_response(url, timeout=DEFAULT_TIMEOUT, validators=None):
    """Baixa master/M3U com revalidação HTTP quando o host oferece suporte.

    Em servidores sem ETag/Last-Modified ainda baixa a fonte e compara SHA-256,
    evitando reconstruir as tabelas quando o conteúdo é idêntico.
    """
    last_exc = None
    source_url = _to_text(url).strip()
    candidates = [source_url]
    if source_url and 'proxy.liyao.space' not in source_url:
        candidates.append('https://proxy.liyao.space/------{}'.format(source_url))
    request_headers = {}
    validators = validators or {}
    etag = _to_text(validators.get('etag', '')).strip()
    last_modified = _to_text(validators.get('last_modified', '')).strip()
    if etag:
        request_headers['If-None-Match'] = etag
    if last_modified:
        request_headers['If-Modified-Since'] = last_modified

    for candidate in candidates:
        try:
            response = SESSION.get(candidate, timeout=timeout, headers=request_headers or None)
            status_code = int(getattr(response, 'status_code', 0) or 0)
            response_etag, response_last_modified = _nav_response_headers(response)
            if status_code == 304:
                return {'status': 'not_modified', 'status_code': status_code, 'text': '',
                        'etag': response_etag or etag, 'last_modified': response_last_modified or last_modified}
            if status_code == 200:
                try:
                    response.encoding = 'utf-8'
                except Exception:
                    pass
                text = _to_text(getattr(response, 'text', ''))
                if text:
                    return {'status': 'ok', 'status_code': status_code, 'text': text,
                            'etag': response_etag, 'last_modified': response_last_modified}
                last_exc = 'resposta vazia'
                log('M3U vazio para {}'.format(candidate), level=2)
                continue
            last_exc = 'HTTP {}'.format(status_code)
            log('M3U HTTP {} para {}'.format(status_code, candidate), level=2)
        except Exception as exc:
            last_exc = exc
            log('M3U falhou em {}: {}'.format(candidate, exc), level=2)
    return {'status': 'error', 'status_code': 0, 'text': '', 'etag': '', 'last_modified': '', 'error': _to_text(last_exc)}


def _get_text(url, timeout=DEFAULT_TIMEOUT):
    result = _get_text_response(url, timeout=timeout)
    if result.get('status') == 'ok':
        return result.get('text', '')
    raise Exception('Erro ao baixar recurso M3U: {}'.format(result.get('error') or url))


def _parse_m3u_text(text):
    channels = []
    groups = []
    groups_seen = set()
    group_counts = {}
    channels_by_group = {}
    current = None
    epg_url = ''

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        if line.startswith('#EXTM3U'):
            for attr in ('x-tvg-url', 'url-tvg', 'tvg-url'):
                value = _extract_attr(line, attr)
                if value:
                    epg_url = value
                    break
            continue

        if line.startswith('#EXTINF'):
            group = _extract_attr(line, 'group-title') or 'Outros'
            logo = _extract_attr(line, 'tvg-logo')
            tvg_id = _extract_attr(line, 'tvg-id')
            tvg_name = _extract_attr(line, 'tvg-name')
            name = line.split(',')[-1].strip() or tvg_name or 'Canal'
            current = {
                'group': group or 'Outros',
                'name': name,
                'logo': logo,
                'tvg_id': tvg_id,
                'tvg_name': tvg_name or name,
                # O identificador definitivo é montado quando a URL da stream
                # é lida. Nome sozinho colidia em listas reais com repetidos
                # (ex.: mesma emissora em grupos/feeds diferentes).
                'id': ''
            }
            if current['group'] not in groups_seen:
                groups_seen.add(current['group'])
                groups.append(current['group'])
                channels_by_group[current['group']] = []
                group_counts[current['group']] = 0
        elif current and (line.startswith('http://') or line.startswith('https://')):
            current['url'] = line
            # ID opaco, curto e estável por canal. Usa grupo + identificação
            # editorial + stream para diferenciar canais com o mesmo nome.
            identity = u'{}\x1f{}\x1f{}'.format(
                _to_text(current.get('group') or 'Outros'),
                _to_text(current.get('tvg_id') or current.get('tvg_name') or current.get('name') or 'Canal'),
                _to_text(line)
            )
            digest = hashlib.sha256(identity.encode('utf-8')).digest()[:18]
            current['id'] = base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
            entry = current.copy()
            channels.append(entry)
            group_name = entry.get('group') or 'Outros'
            channels_by_group.setdefault(group_name, []).append(entry)
            group_counts[group_name] = group_counts.get(group_name, 0) + 1
            current = None

    return {
        'channels': channels,
        'groups': groups,
        'group_counts': group_counts,
        'channels_by_group': channels_by_group,
        'epg_url': epg_url,
        'text_hash': _hash_text(text),
        'fetched_at': int(time.time()),
    }


def _nav_store_master(master_url, items, content_hash, etag='', last_modified='', checked_at=None):
    conn = None
    try:
        now_ts = int(checked_at or time.time())
        conn = _nav_connect()
        conn.execute('BEGIN')
        conn.execute('DELETE FROM nav_master_items WHERE master_url=?', (_to_text(master_url).strip(),))
        master_rows = [
            (_to_text(master_url).strip(), int(position), _to_text(item_url).strip())
            for position, item_url in enumerate(list(items or []), 1)
            if _to_text(item_url).strip()
        ]
        if master_rows:
            conn.executemany(
                'INSERT INTO nav_master_items(master_url, position, item_url) VALUES (?, ?, ?)',
                master_rows
            )
        conn.execute(
            'INSERT OR REPLACE INTO nav_sources('
            'source_url, source_type, content_hash, etag, last_modified, epg_url, fetched_at, checked_at, payload_size, schema_version'
            ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (_to_text(master_url).strip(), 'master', _to_text(content_hash), _to_text(etag), _to_text(last_modified), '',
             now_ts, now_ts, len(_to_text('\n'.join(items or [])).encode('utf-8')), NAV_SCHEMA_VERSION)
        )
        conn.commit()
        return True
    except Exception as exc:
        log('Navegação cache master gravação falhou: {}'.format(exc), level=2)
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _nav_store_m3u(m3u_url, meta, content_hash, etag='', last_modified='', checked_at=None):
    conn = None
    try:
        now_ts = int(checked_at or time.time())
        source_url = _to_text(m3u_url).strip()
        channels = list((meta or {}).get('channels') or [])
        groups = list((meta or {}).get('groups') or [])
        group_counts = dict((meta or {}).get('group_counts') or {})
        conn = _nav_connect()
        conn.execute('BEGIN')
        conn.execute('DELETE FROM nav_groups WHERE source_url=?', (source_url,))
        conn.execute('DELETE FROM nav_channels WHERE source_url=?', (source_url,))
        group_rows = [
            (source_url, _to_text(group_name), int(group_position), int(group_counts.get(group_name, 0) or 0))
            for group_position, group_name in enumerate(groups, 1)
        ]
        if group_rows:
            conn.executemany(
                'INSERT INTO nav_groups(source_url, group_name, position, channel_count) VALUES (?, ?, ?, ?)',
                group_rows
            )
        positions = {}
        channel_rows = []
        for channel in channels:
            group_name = _to_text(channel.get('group') or 'Outros')
            positions[group_name] = int(positions.get(group_name, 0)) + 1
            channel_rows.append(
                (source_url, group_name, int(positions[group_name]), _to_text(channel.get('name') or 'Canal'),
                 _to_text(channel.get('logo')), _to_text(channel.get('tvg_id')), _to_text(channel.get('tvg_name') or channel.get('name') or 'Canal'),
                 _to_text(channel.get('id')), _to_text(channel.get('url')))
            )
        if channel_rows:
            conn.executemany(
                'INSERT INTO nav_channels('
                'source_url, group_name, position, name, logo, tvg_id, tvg_name, channel_id, stream_url'
                ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                channel_rows
            )
        conn.execute(
            'INSERT OR REPLACE INTO nav_sources('
            'source_url, source_type, content_hash, etag, last_modified, epg_url, fetched_at, checked_at, payload_size, schema_version'
            ') VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (source_url, 'm3u', _to_text(content_hash), _to_text(etag), _to_text(last_modified),
             _to_text((meta or {}).get('epg_url', '')), now_ts, now_ts,
             int((meta or {}).get('payload_size', 0) or 0), NAV_SCHEMA_VERSION)
        )
        conn.commit()
        return True
    except Exception as exc:
        log('Navegação cache M3U gravação falhou: {}'.format(exc), level=2)
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        return False
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _nav_load_master_items(master_url):
    conn = None
    try:
        conn = _nav_connect()
        rows = conn.execute(
            'SELECT item_url FROM nav_master_items WHERE master_url=? ORDER BY position',
            (_to_text(master_url).strip(),)
        ).fetchall()
        return [_to_text(row[0]).strip() for row in rows if _to_text(row[0]).strip()]
    except Exception:
        return []
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _nav_load_groups(m3u_url):
    conn = None
    try:
        conn = _nav_connect()
        rows = conn.execute(
            'SELECT group_name, channel_count FROM nav_groups WHERE source_url=? ORDER BY position',
            (_to_text(m3u_url).strip(),)
        ).fetchall()
        groups = []
        counts = {}
        for row in rows:
            group_name = _to_text(row[0]) or 'Outros'
            groups.append(group_name)
            counts[group_name] = int(row[1] or 0)
        return groups, counts
    except Exception:
        return [], {}
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _nav_load_group_channels(m3u_url, group_name=None):
    conn = None
    try:
        conn = _nav_connect()
        source_url = _to_text(m3u_url).strip()
        if group_name is None:
            rows = conn.execute(
                'SELECT group_name, name, logo, tvg_id, tvg_name, channel_id, stream_url '
                'FROM nav_channels WHERE source_url=? ORDER BY group_name, position',
                (source_url,)
            ).fetchall()
        else:
            rows = conn.execute(
                'SELECT group_name, name, logo, tvg_id, tvg_name, channel_id, stream_url '
                'FROM nav_channels WHERE source_url=? AND group_name=? ORDER BY position',
                (source_url, _to_text(group_name))
            ).fetchall()
        channels = []
        for row in rows:
            channels.append({
                'group': _to_text(row[0]) or 'Outros',
                'name': _to_text(row[1]) or 'Canal',
                'logo': _to_text(row[2]),
                'tvg_id': _to_text(row[3]),
                'tvg_name': _to_text(row[4]) or _to_text(row[1]) or 'Canal',
                'id': _to_text(row[5]),
                'url': _to_text(row[6]),
            })
        return channels
    except Exception:
        return []
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _nav_refresh_master(force=False):
    now_ts = int(time.time())
    existing = _nav_read_source(MASTER_URL)
    if existing and _nav_source_is_recent(existing, now_ts=now_ts) and not force:
        return existing
    # Se o contrato interno mudou, 304 não basta: precisamos do conteúdo
    # completo uma vez para rematerializar o índice no formato novo.
    validators = existing if _nav_source_schema_current(existing) else {}
    result = _get_text_response(MASTER_URL, validators=validators)
    if result.get('status') == 'not_modified' and existing and _nav_source_schema_current(existing):
        _nav_touch_source(MASTER_URL, result.get('etag'), result.get('last_modified'), checked_at=now_ts)
        existing['checked_at'] = now_ts
        return existing
    if result.get('status') == 'ok':
        text = result.get('text', '')
        content_hash = _hash_text(text)
        if (existing and _nav_source_schema_current(existing) and content_hash
                and content_hash == existing.get('content_hash', '')):
            _nav_touch_source(MASTER_URL, result.get('etag'), result.get('last_modified'), checked_at=now_ts)
            existing['checked_at'] = now_ts
            return existing
        items = [line.strip() for line in text.splitlines() if line.strip().startswith(('http://', 'https://'))]
        if not items:
            raise Exception('Master sem listas válidas')
        _nav_store_master(MASTER_URL, items, content_hash, result.get('etag'), result.get('last_modified'), checked_at=now_ts)
        return _nav_read_source(MASTER_URL)
    if existing and _nav_source_is_usable(existing, now_ts=now_ts):
        _nav_touch_source(MASTER_URL, checked_at=now_ts)
        log('Master remoto indisponível; reutilizando cache local válido.', level=2)
        existing['checked_at'] = now_ts
        return existing
    raise Exception('Erro ao baixar master: {}'.format(result.get('error') or MASTER_URL))


def _nav_refresh_m3u(m3u_url, force=False):
    source_url = _to_text(m3u_url).strip()
    if not source_url:
        raise Exception('Lista M3U vazia')
    now_ts = int(time.time())
    memory = M3U_META_CACHE.get(source_url)
    if memory and _nav_source_is_recent(memory, now_ts=now_ts) and not force:
        return memory
    existing = _nav_read_source(source_url)
    if existing and _nav_source_is_recent(existing, now_ts=now_ts) and not force:
        M3U_META_CACHE[source_url] = existing
        return existing
    # Cache de versão anterior precisa receber 200 uma vez para reconstruir
    # IDs/índice; não aceitar 304 até terminar essa migração leve.
    validators = existing if _nav_source_schema_current(existing) else {}
    result = _get_text_response(source_url, validators=validators)
    if result.get('status') == 'not_modified' and existing and _nav_source_schema_current(existing):
        _nav_touch_source(source_url, result.get('etag'), result.get('last_modified'), checked_at=now_ts)
        existing['checked_at'] = now_ts
        M3U_META_CACHE[source_url] = existing
        return existing
    if result.get('status') == 'ok':
        text = result.get('text', '')
        content_hash = _hash_text(text)
        if (existing and _nav_source_schema_current(existing) and content_hash
                and content_hash == existing.get('content_hash', '')):
            _nav_touch_source(source_url, result.get('etag'), result.get('last_modified'), checked_at=now_ts)
            existing['checked_at'] = now_ts
            M3U_META_CACHE[source_url] = existing
            return existing
        meta = _parse_m3u_text(text)
        meta['payload_size'] = len(_to_text(text).encode('utf-8'))
        _nav_store_m3u(source_url, meta, content_hash, result.get('etag'), result.get('last_modified'), checked_at=now_ts)
        source = _nav_read_source(source_url) or {}
        M3U_META_CACHE[source_url] = source
        return source
    if existing and _nav_source_is_usable(existing, now_ts=now_ts):
        _nav_touch_source(source_url, checked_at=now_ts)
        log('M3U remoto indisponível; reutilizando cache local válido: {}'.format(source_url), level=2)
        existing['checked_at'] = now_ts
        M3U_META_CACHE[source_url] = existing
        return existing
    raise Exception('Erro ao baixar lista M3U: {}'.format(result.get('error') or source_url))


def _get_meta(m3u_url):
    source = _nav_refresh_m3u(m3u_url)
    groups, group_counts = _nav_load_groups(m3u_url)
    channels = _nav_load_group_channels(m3u_url, group_name=None)
    by_group = {}
    for channel in channels:
        by_group.setdefault(channel.get('group') or 'Outros', []).append(channel)
    return {
        'channels': channels,
        'groups': groups,
        'group_counts': group_counts,
        'channels_by_group': by_group,
        'epg_url': _to_text((source or {}).get('epg_url', '')),
        'text_hash': _to_text((source or {}).get('content_hash', '')),
        'fetched_at': int((source or {}).get('fetched_at', 0) or 0),
        'checked_at': int((source or {}).get('checked_at', 0) or 0),
    }


def parse_m3u(m3u_url):
    meta = _get_meta(m3u_url)
    return meta.get('channels', []), meta.get('groups', [])


def get_group_index(m3u_url):
    _nav_refresh_m3u(m3u_url)
    return _nav_load_groups(m3u_url)


def get_group_channels(m3u_url, group_name):
    _nav_refresh_m3u(m3u_url)
    return _nav_load_group_channels(m3u_url, group_name)


def _derive_epg_urls_from_m3u(m3u_url):
    derived = []
    source = _to_text(m3u_url).strip()
    if not source:
        return derived
    try:
        from urllib.parse import urlsplit, urlunsplit
    except Exception:
        return derived
    try:
        parts = urlsplit(source)
        path = parts.path or ''
        if '/' not in path:
            return derived
        base_path = path.rsplit('/', 1)[0] + '/'
        for filename in ('xmltv.php', 'epg.php'):
            candidate = urlunsplit((parts.scheme, parts.netloc, base_path + filename, '', ''))
            if candidate not in derived:
                derived.append(candidate)
    except Exception:
        return derived
    return derived


def get_m3u_epg_url(m3u_url):
    source = _nav_refresh_m3u(m3u_url)
    epg_url = _to_text((source or {}).get('epg_url', ''))
    if epg_url:
        return epg_url
    derived = _derive_epg_urls_from_m3u(m3u_url)
    return derived[0] if derived else ''


def get_lists(force=False):
    now_ts = int(time.time())
    try:
        cached_items = LISTS_CACHE.get('items') or []
        fetched_at = int(LISTS_CACHE.get('fetched_at') or 0)
        if (not force) and cached_items and (now_ts - fetched_at) <= LISTS_CACHE_SECONDS:
            return list(cached_items)
    except Exception:
        pass

    items = [
        'https://oneplayhd.com/listas_oneplay/lista02.txt',
        'https://oneplayhd.com/listas_oneplay/lista05.txt',
    ]
    try:
        LISTS_CACHE['items'] = list(items)
        LISTS_CACHE['fetched_at'] = now_ts
    except Exception:
        pass
    return items


def clear_navigation_cache():
    """Limpa somente master/M3U/grupos/canais; não toca XMLTV nem EPG SQLite."""
    removed = 0
    conn = None
    try:
        conn = _nav_connect()
        for table in ('nav_channels', 'nav_groups', 'nav_master_items', 'nav_sources'):
            cursor = conn.execute('DELETE FROM {}'.format(table))
            try:
                removed += int(cursor.rowcount or 0)
            except Exception:
                pass
        conn.commit()
    except Exception:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
    try:
        M3U_META_CACHE.clear()
        LISTS_CACHE['items'] = []
        LISTS_CACHE['fetched_at'] = 0
    except Exception:
        pass
    return removed


def navigation_cache_status():
    """Diagnóstico leve usado em auditorias e sem acessar a rede."""
    conn = None
    try:
        conn = _nav_connect()
        source_count = int(conn.execute('SELECT COUNT(*) FROM nav_sources').fetchone()[0] or 0)
        group_count = int(conn.execute('SELECT COUNT(*) FROM nav_groups').fetchone()[0] or 0)
        channel_count = int(conn.execute('SELECT COUNT(*) FROM nav_channels').fetchone()[0] or 0)
        return {'sources': source_count, 'groups': group_count, 'channels': channel_count, 'db_file': NAV_DB_FILE}
    except Exception:
        return {'sources': 0, 'groups': 0, 'channels': 0, 'db_file': NAV_DB_FILE}
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass

def basename(path):
    idx = path.rfind('/') + 1
    return path[idx:]



def _replace_file(src, dst):
    """Publicação atômica com pequena tolerância a handles abertos.

    os.replace é atômico nas portas Python 3 suportadas pelo Kodi. Em Windows
    pode falhar enquanto a UI fecha a conexão de leitura; repetimos por pouco
    tempo sem apagar o banco antigo. O fallback antigo só é usado onde
    os.replace não existe.
    """
    last_exc = None
    if hasattr(os, 'replace'):
        for attempt in range(5):
            try:
                os.replace(src, dst)
                return
            except Exception as exc:
                last_exc = exc
                try:
                    time.sleep(0.05 * (attempt + 1))
                except Exception:
                    pass
        raise last_exc

    backup = '{}.bak.{}'.format(dst, int(time.time() * 1000))
    moved_old = False
    try:
        if os.path.exists(dst):
            os.rename(dst, backup)
            moved_old = True
        os.rename(src, dst)
        if moved_old and os.path.exists(backup):
            os.remove(backup)
    except Exception:
        try:
            if moved_old and os.path.exists(backup) and not os.path.exists(dst):
                os.rename(backup, dst)
        except Exception:
            pass
        raise


def _unique_temp_path(path):
    try:
        pid = os.getpid()
    except Exception:
        pid = 0
    try:
        stamp = int(time.time() * 1000)
    except Exception:
        stamp = int(time.time())
    return '{}.tmp.{}.{}'.format(path, pid, stamp)

def _source_key(m3u_url, epg_url):
    seed = '{}|{}|{}'.format(_to_text(m3u_url).strip(), _to_text(epg_url).strip(), EPG_CACHE_VERSION)
    return hashlib.md5(seed.encode('utf-8')).hexdigest()


def _source_label(m3u_url, epg_url=''):
    target = _to_text(m3u_url).strip()
    try:
        options = get_lists()
        for index, option in enumerate(options, 1):
            if _to_text(option).strip() == target:
                return 'lista{}'.format(index)
    except Exception:
        pass

    for candidate in (_to_text(epg_url).strip(), target):
        if not candidate:
            continue
        base = os.path.splitext(basename(candidate.split('?', 1)[0].split('#', 1)[0]))[0]
        base = re.sub(r'[^a-zA-Z0-9]+', '', _normalize_key(base).replace(' ', ''))
        if base:
            return base.lower()

    return 'lista_' + _source_key(m3u_url, epg_url)[:8]


def _xmltv_file_for_source(m3u_url, epg_url):
    return os.path.join(EPG_XMLTV_DIR, '{}.xml'.format(_source_label(m3u_url, epg_url)))


def _xmltv_meta_file_for_source(m3u_url, epg_url):
    return os.path.join(EPG_XMLTV_DIR, '{}.meta.json'.format(_source_label(m3u_url, epg_url)))


def _sqlite_file_for_source(m3u_url, epg_url, day_key):
    """Arquivo SQLite local do EPG para uma lista e o dia atual.

    O banco concentra aliases + programação sem duplicar a mesma grade por
    alias, como acontecia no índice JSON. Isso reduz leitura em disco e RAM
    quando a UI abre apenas uma categoria.
    """
    return os.path.join(EPG_SQLITE_DIR, '{}_{}.db'.format(_source_label(m3u_url, epg_url), day_key))


def _index_file_for_source(m3u_url, epg_url, day_key):
    """Nome histórico usado pelo service; agora aponta para SQLite."""
    return _sqlite_file_for_source(m3u_url, epg_url, day_key)


def _file_is_fresh(path):
    try:
        return os.path.exists(path) and (time.time() - os.path.getmtime(path) <= _get_epg_ttl())
    except Exception:
        return False


def _parse_xmltv_datetime(value):
    if not value:
        return None
    raw = _to_text(value).strip()
    if not raw:
        return None
    base = raw.split(' ', 1)[0]
    tz_part = raw[len(base):].strip()
    try:
        tm = time.strptime(base[:14], '%Y%m%d%H%M%S')
        ts = calendar.timegm(tm)
        if tz_part and len(tz_part) >= 5 and tz_part[0] in '+-' and tz_part[1:5].isdigit():
            sign = -1 if tz_part.startswith('-') else 1
            try:
                digits = tz_part[1:5]
                offset = (int(digits[:2]) * 60 + int(digits[2:4])) * 60
                ts -= sign * offset
            except Exception:
                pass
        else:
            ts -= BRAZIL_UTC_OFFSET_SECONDS
        return int(ts)
    except Exception:
        return None


def _format_brazil_time(timestamp):
    try:
        ts = int(timestamp) + BRAZIL_UTC_OFFSET_SECONDS
        return time.strftime('%H:%M', time.gmtime(ts))
    except Exception:
        return ''


def _local_date_key(timestamp):
    try:
        ts = int(timestamp) + BRAZIL_UTC_OFFSET_SECONDS
        return time.strftime('%Y%m%d', time.gmtime(ts))
    except Exception:
        return ''


def _format_brazil_day_label(timestamp, now_ts=None):
    try:
        if now_ts is None:
            now_ts = int(time.time())
        item_key = _local_date_key(timestamp)
        today_key = _local_date_key(now_ts)
        tomorrow_key = _local_date_key(int(now_ts) + 86400)
        if item_key == today_key:
            return 'Hoje'
        if item_key == tomorrow_key:
            return 'Amanhã'
        ts = int(timestamp) + BRAZIL_UTC_OFFSET_SECONDS
        return time.strftime('%d/%m', time.gmtime(ts))
    except Exception:
        return ''


def _local_day_window(now_ts=None):
    if now_ts is None:
        now_ts = int(time.time())
    local_now = int(now_ts) + BRAZIL_UTC_OFFSET_SECONDS
    local_tm = time.gmtime(local_now)
    day_key = time.strftime('%Y%m%d', local_tm)
    day_start_local_epoch = calendar.timegm((local_tm.tm_year, local_tm.tm_mon, local_tm.tm_mday, 0, 0, 0, 0, 0, 0))
    day_start_utc = day_start_local_epoch - BRAZIL_UTC_OFFSET_SECONDS
    # Janela contínua: dia atual + amanhã. Antes era só o dia atual (24h de calendário),
    # por isso no fim da noite a grade ia ficando curta até virar o dia.
    day_end_utc = day_start_utc + (max(1, int(EPG_INDEX_DAYS)) * 86400)
    return day_key, int(day_start_utc), int(day_end_utc)


def _build_epg_candidates(epg_url, m3u_url=''):
    seen = set()
    base_candidates = []

    def add_candidate(url):
        url = _to_text(url).strip()
        if not url or url in seen:
            return
        seen.add(url)
        base_candidates.append(url)

    add_candidate(epg_url)
    for derived in _derive_epg_urls_from_m3u(m3u_url):
        add_candidate(derived)

    expanded = []
    for url in list(base_candidates):
        lowered = _to_text(url).lower()
        expanded.append(url)
        if 'xmltv.php' in lowered:
            expanded.append(re.sub(r'xmltv\.php', 'epg.php', url, flags=re.IGNORECASE))
        elif 'epg.php' in lowered:
            expanded.append(re.sub(r'epg\.php', 'xmltv.php', url, flags=re.IGNORECASE))

    final_candidates = []
    seen_final = set()
    for url in expanded:
        url = _to_text(url).strip()
        if not url or url in seen_final:
            continue
        seen_final.add(url)
        final_candidates.append(url)
        if 'proxy.liyao.space' not in url:
            proxied = 'https://proxy.liyao.space/------{}'.format(url)
            if proxied not in seen_final:
                seen_final.add(proxied)
                final_candidates.append(proxied)
    return final_candidates


def _response_to_file(response, destination_path, chunk_size=64 * 1024):
    """Grava a resposta HTTP em disco por blocos e retorna (bytes, magic).

    O XMLTV pode ter dezenas de MB. Ler ``response.content`` e depois
    descompactar em memória multiplica o pico de RAM em Android/ARM. Esta
    rotina mantém somente um bloco em memória e sempre fecha a resposta.
    """
    total = 0
    head = b''
    try:
        iterator = response.iter_content(chunk_size=chunk_size)
    except Exception:
        iterator = None
    try:
        with io.open(destination_path, 'wb') as fh:
            if iterator is not None:
                for chunk in iterator:
                    if not chunk:
                        continue
                    if not isinstance(chunk, bytes):
                        chunk = _to_text(chunk).encode('utf-8')
                    if len(head) < 2:
                        head += chunk[:2 - len(head)]
                    fh.write(chunk)
                    total += len(chunk)
            else:
                # Compatibilidade com doubles de teste ou clientes legados.
                content = getattr(response, 'content', b'') or b''
                if not isinstance(content, bytes):
                    content = _to_text(content).encode('utf-8')
                head = content[:2]
                fh.write(content)
                total = len(content)
    finally:
        try:
            response.close()
        except Exception:
            pass
    return total, head


def _download_epg_to_file(epg_url, destination_path, m3u_url=''):
    """Baixa XMLTV diretamente para arquivo temporário, com gzip em streaming.

    Cada candidato trabalha em seu próprio arquivo temporário. Um candidato
    malformado nunca apaga o resultado válido de um candidato anterior nem o
    XMLTV já promovido pelo chamador.
    """
    candidates = _build_epg_candidates(epg_url, m3u_url=m3u_url)
    last_exc = None
    for candidate in candidates:
        raw_path = _unique_temp_path(destination_path + '.download')
        completed = False
        try:
            res = SESSION.get(candidate, timeout=max(DEFAULT_TIMEOUT, 20), stream=True)
            if int(getattr(res, 'status_code', 0) or 0) != 200:
                log('EPG HTTP {} para {}'.format(getattr(res, 'status_code', 0), candidate), level=2)
                try:
                    res.close()
                except Exception:
                    pass
                continue

            total, magic = _response_to_file(res, raw_path)
            if total <= 0:
                raise IOError('EPG vazio em {}'.format(candidate))

            if magic == b'\x1f\x8b':
                with gzip.open(raw_path, 'rb') as source, io.open(destination_path, 'wb') as target:
                    while True:
                        chunk = source.read(64 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
            else:
                _replace_file(raw_path, destination_path)

            if not os.path.exists(destination_path) or os.path.getsize(destination_path) <= 0:
                raise IOError('arquivo XMLTV temporário vazio')
            completed = True
            return destination_path
        except Exception as exc:
            last_exc = exc
            log('EPG falhou em {}: {}'.format(candidate, exc), level=2)
        finally:
            try:
                if os.path.exists(raw_path):
                    os.remove(raw_path)
            except Exception:
                pass
            # destination_path é exclusivo da tentativa de refresh. Limpa apenas
            # saída parcial da tentativa atual, jamais o XMLTV já promovido.
            if not completed:
                try:
                    if os.path.exists(destination_path):
                        os.remove(destination_path)
                except Exception:
                    pass
    raise Exception('Erro ao baixar EPG: {}'.format(last_exc or epg_url))


def _download_epg_payload(epg_url, m3u_url=''):
    """Compatibilidade interna: retorna bytes apenas quando chamado externamente.

    O fluxo principal usa ``_download_epg_to_file``. Esta função fica para
    extensões antigas, mas não é usada pelo aquecimento/produção.
    """
    temp_path = _unique_temp_path(os.path.join(EPG_XMLTV_DIR, 'epg_payload.xml'))
    try:
        _download_epg_to_file(epg_url, temp_path, m3u_url=m3u_url)
        with io.open(temp_path, 'rb') as fh:
            return fh.read()
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass


def _build_xmltv_meta(xmltv_file):
    first_ts = None
    last_ts = None
    total_programmes = 0
    for event, elem in ET.iterparse(xmltv_file, events=('end',)):
        if elem.tag != 'programme':
            elem.clear()
            continue
        start_ts = _parse_xmltv_datetime(elem.get('start'))
        stop_ts = _parse_xmltv_datetime(elem.get('stop'))
        if start_ts is not None:
            if first_ts is None or start_ts < first_ts:
                first_ts = int(start_ts)
        if stop_ts is not None:
            if last_ts is None or stop_ts > last_ts:
                last_ts = int(stop_ts)
        total_programmes += 1
        elem.clear()

    first_day = ''
    last_day = ''
    if first_ts is not None:
        first_day = _local_day_window(first_ts)[0]
    if last_ts is not None:
        last_day = _local_day_window(max(0, int(last_ts) - 1))[0]

    return {
        'version': EPG_XMLTV_META_VERSION,
        'generated_at': int(time.time()),
        'raw_mtime': int(os.path.getmtime(xmltv_file)),
        'raw_size': int(os.path.getsize(xmltv_file)),
        'first_ts': int(first_ts or 0),
        'last_ts': int(last_ts or 0),
        'first_day': first_day,
        'last_day': last_day,
        'programme_count': int(total_programmes),
    }


def _read_xmltv_meta(meta_file, xmltv_file):
    if not os.path.exists(meta_file):
        return None
    try:
        with io.open(meta_file, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except Exception as exc:
        log('Falha ao ler metadados XMLTV {}: {}'.format(meta_file, exc), level=2)
        return None
    try:
        if data.get('version') != EPG_XMLTV_META_VERSION:
            return None
        if int(data.get('raw_mtime') or 0) != int(os.path.getmtime(xmltv_file)):
            return None
        if int(data.get('raw_size') or 0) != int(os.path.getsize(xmltv_file)):
            return None
        return data
    except Exception:
        return None


def _write_xmltv_meta(meta_file, data):
    try:
        temp_path = _unique_temp_path(meta_file)
        with io.open(temp_path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False)
        _replace_file(temp_path, meta_file)
    except Exception as exc:
        log('Falha ao gravar metadados XMLTV {}: {}'.format(meta_file, exc), level=2)


def _ensure_xmltv_meta(m3u_url, epg_url, xmltv_file):
    meta_file = _xmltv_meta_file_for_source(m3u_url, epg_url)
    meta = _read_xmltv_meta(meta_file, xmltv_file)
    if meta is not None:
        return meta
    try:
        meta = _build_xmltv_meta(xmltv_file)
        _write_xmltv_meta(meta_file, meta)
        return meta
    except Exception as exc:
        log('Falha gerando metadados do XMLTV {}: {}'.format(xmltv_file, exc), level=2)
        return None


def _xmltv_has_relevant_coverage(meta, day_key, day_start_utc, day_end_utc, now_ts=None):
    """Confirma se o XMLTV tem programação útil dentro da janela local.

    A versão anterior era conservadora demais e recusava XMLTV quando o primeiro
    programa começava depois da meia-noite local. Para EPG real, o critério
    correto é sobreposição de janela: existir programação futura entre o início
    de hoje e o fim de amanhã.
    """
    if not isinstance(meta, dict):
        return False
    first_ts = int(meta.get('first_ts') or 0)
    last_ts = int(meta.get('last_ts') or 0)
    if last_ts <= 0:
        return False
    if first_ts and first_ts >= int(day_end_utc):
        return False
    if last_ts <= int(day_start_utc):
        return False
    meta_day = _to_text(meta.get('last_day')).strip()
    if meta_day and day_key > meta_day:
        return False
    if now_ts is not None and last_ts <= int(now_ts):
        return False
    return True


def _xmltv_tomorrow_coverage_seconds(meta, day_start_utc):
    """Calcula quantos segundos de amanhã o XMLTV parece cobrir.

    É uma checagem por last_ts do XMLTV, não por quantidade de programas, para
    ser leve e funcionar antes de montar todo o índice JSON.
    """
    try:
        tomorrow_start_utc = int(day_start_utc) + 86400
        last_ts = int(meta.get('last_ts') or 0)
        coverage = max(0, last_ts - tomorrow_start_utc)
        return int(coverage)
    except Exception:
        return 0


def _xmltv_has_tomorrow_coverage(meta, day_key, day_start_utc, day_end_utc, now_ts=None):
    """Retorna True quando o XMLTV tem amanhã com cobertura mínima útil.

    Antes bastava existir qualquer item de amanhã. Na prática isso aceitava
    XMLTV com apenas madrugada/primeiras horas do dia seguinte, e a grade
    completa ficava com "Amanhã" muito curto. O critério agora exige uma
    cobertura mínima conservadora de amanhã; se o provedor ainda não publicou,
    a navegação manual/boot/ativação/virada podem revalidar sem travar o addon.
    """
    if int(EPG_INDEX_DAYS) <= 1:
        return True
    if not _xmltv_has_relevant_coverage(meta, day_key, day_start_utc, day_end_utc, now_ts=now_ts):
        return False
    try:
        return _xmltv_tomorrow_coverage_seconds(meta, day_start_utc) >= int(EPG_MIN_TOMORROW_COVERAGE_SECONDS)
    except Exception:
        return False


def _xmltv_covers_future_window(meta, day_key, day_start_utc, day_end_utc, now_ts=None):
    if now_ts is None:
        now_ts = int(time.time())
    if not _xmltv_has_relevant_coverage(meta, day_key, day_start_utc, day_end_utc, now_ts=now_ts):
        return False
    last_ts = int(meta.get('last_ts') or 0)
    required_until = min(int(day_end_utc), int(now_ts) + int(EPG_MIN_FUTURE_COVERAGE_SECONDS))
    return last_ts >= required_until


def _xmltv_ready_for_today_tomorrow(meta, day_key, day_start_utc, day_end_utc, now_ts=None):
    if now_ts is None:
        now_ts = int(time.time())
    return (
        _xmltv_covers_future_window(meta, day_key, day_start_utc, day_end_utc, now_ts=now_ts)
        and _xmltv_has_tomorrow_coverage(meta, day_key, day_start_utc, day_end_utc, now_ts=now_ts)
    )


def _xmltv_recently_checked(meta, now_ts=None):
    if not isinstance(meta, dict):
        return False
    if now_ts is None:
        now_ts = int(time.time())
    try:
        # Usa o mtime real do XMLTV, não o generated_at do meta.json.
        # generated_at pode ser recriado ao auditar um XML antigo e, se usado
        # como marcador, faz o addon acreditar que acabou de baixar o XMLTV,
        # pulando a tentativa de buscar a grade de amanhã.
        marker = int(meta.get('raw_mtime') or 0)
        if marker <= 0:
            marker = int(meta.get('generated_at') or 0)
        return marker > 0 and (int(now_ts) - marker) < int(EPG_SHORT_FUTURE_REFRESH_GRACE_SECONDS)
    except Exception:
        return False


def _refresh_xmltv_source(m3u_url, epg_url, xmltv_file):
    ensure_dir(EPG_XMLTV_DIR)
    temp_path = _unique_temp_path(xmltv_file)
    try:
        _download_epg_to_file(epg_url, temp_path, m3u_url=m3u_url)
        _replace_file(temp_path, xmltv_file)
    finally:
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
    return _ensure_xmltv_meta(m3u_url, epg_url, xmltv_file)


def _ensure_local_xmltv(m3u_url, epg_url, day_key=None, day_start_utc=None, day_end_utc=None, force_refresh=False):
    xmltv_file = _xmltv_file_for_source(m3u_url, epg_url)
    if day_key is None or day_start_utc is None or day_end_utc is None:
        day_key, day_start_utc, day_end_utc = _local_day_window()
    now_ts = int(time.time())

    if not os.path.exists(xmltv_file):
        _refresh_xmltv_source(m3u_url, epg_url, xmltv_file)
        return xmltv_file

    meta = _ensure_xmltv_meta(m3u_url, epg_url, xmltv_file)
    is_fresh = _file_is_fresh(xmltv_file)

    # Revalidação dirigida: usada quando uma lista precisa confirmar
    # se o provedor já publicou mais programação de Amanhã. Aqui ignoramos a janela de graça e
    # consultamos a XMLTV novamente. Se a fonte ainda não publicou mais grade,
    # o XML antigo/novo continua sendo usado sem travar a navegação.
    if force_refresh:
        try:
            new_meta = _refresh_xmltv_source(m3u_url, epg_url, xmltv_file)
            if (_xmltv_ready_for_today_tomorrow(new_meta, day_key, day_start_utc, day_end_utc, now_ts=now_ts)
                    or _xmltv_has_relevant_coverage(new_meta, day_key, day_start_utc, day_end_utc, now_ts=now_ts)):
                return xmltv_file
        except Exception as exc:
            if meta and _xmltv_has_relevant_coverage(meta, day_key, day_start_utc, day_end_utc, now_ts=now_ts):
                log('EPG rechecagem de amanhã falhou, reutilizando XML com cobertura futura: {}'.format(exc), level=2)
                return xmltv_file
            raise
        return xmltv_file

    if is_fresh and _xmltv_ready_for_today_tomorrow(meta, day_key, day_start_utc, day_end_utc, now_ts=now_ts):
        return xmltv_file

    # Se o provedor ainda não publicou amanhã, não martelamos o servidor a cada abertura recente.
    # Porém a checagem recente passa a usar o mtime real do XMLTV; meta.json recriado
    # não mascara um XML antigo sem amanhã.
    if (is_fresh and _xmltv_has_relevant_coverage(meta, day_key, day_start_utc, day_end_utc, now_ts=now_ts)
            and _xmltv_recently_checked(meta, now_ts=now_ts)):
        return xmltv_file

    try:
        new_meta = _refresh_xmltv_source(m3u_url, epg_url, xmltv_file)
        if (_xmltv_ready_for_today_tomorrow(new_meta, day_key, day_start_utc, day_end_utc, now_ts=now_ts)
                or _xmltv_has_relevant_coverage(new_meta, day_key, day_start_utc, day_end_utc, now_ts=now_ts)):
            return xmltv_file
    except Exception as exc:
        # Se o XML antigo ainda tem programação futura, seguimos com ele para não travar a UI.
        if meta and _xmltv_has_relevant_coverage(meta, day_key, day_start_utc, day_end_utc, now_ts=now_ts):
            log('EPG refresh falhou, reutilizando XML com cobertura futura: {}'.format(exc), level=2)
            return xmltv_file
        raise

    return xmltv_file


def _read_service_manifest():
    try:
        if not os.path.exists(EPG_SERVICE_MANIFEST_FILE):
            return {}
        with io.open(EPG_SERVICE_MANIFEST_FILE, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        if data.get('version') != EPG_SERVICE_MANIFEST_VERSION:
            return {}
        if data.get('cache_version') != EPG_CACHE_VERSION:
            return {}
        return data
    except Exception:
        return {}




def _read_service_manifest_for_lookup():
    """Lê manifesto mesmo quando o cache_version mudou, só para recuperar URLs.

    A validação pesada continua usando _read_service_manifest(), que exige a
    versão atual do cache. Esta função é deliberadamente limitada: preserva
    entries/last_urls de manifestos antigos para evitar baixar M3U apenas para
    redescobrir x-tvg-url após uma atualização do addon. Se o índice/JSON for
    antigo, a checagem normal ainda retorna indice_invalido e reconstrói.
    """
    data = _read_service_manifest()
    if data:
        return data
    try:
        if not os.path.exists(EPG_SERVICE_MANIFEST_FILE):
            return {}
        with io.open(EPG_SERVICE_MANIFEST_FILE, 'r', encoding='utf-8') as fh:
            raw = json.load(fh)
        if not isinstance(raw, dict):
            return {}
        if raw.get('version') != EPG_SERVICE_MANIFEST_VERSION:
            return {}
        out = {}
        entries = raw.get('entries')
        if isinstance(entries, dict):
            out['entries'] = entries
        last_urls = raw.get('last_urls')
        if isinstance(last_urls, list):
            out['last_urls'] = last_urls
        out['_stale_cache_version'] = raw.get('cache_version') != EPG_CACHE_VERSION
        out['_previous_cache_version'] = raw.get('cache_version')
        return out
    except Exception:
        return {}


def _write_service_manifest(data):
    try:
        ensure_dir(EPG_CACHE_DIR)
        if not isinstance(data, dict):
            data = {}
        data['version'] = EPG_SERVICE_MANIFEST_VERSION
        data['cache_version'] = EPG_CACHE_VERSION
        data['updated_at'] = int(time.time())
        temp_path = _unique_temp_path(EPG_SERVICE_MANIFEST_FILE)
        with io.open(temp_path, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False)
        _replace_file(temp_path, EPG_SERVICE_MANIFEST_FILE)
        return True
    except Exception as exc:
        log('Falha gravando manifesto EPG do service: {}'.format(exc), level=2)
        return False


def remember_service_epg_lists(m3u_urls):
    """Grava a última relação de listas usada pelo service.

    Isso permite que, no próximo boot, o service verifique o cache já existente
    sem precisar baixar lista01/lista02/etc só para redescobrir o x-tvg-url.
    """
    try:
        urls = []
        for url in list(m3u_urls or []):
            url = _to_text(url).strip()
            if url and url not in urls:
                urls.append(url)
        if not urls:
            return False
        data = _read_service_manifest_for_lookup()
        entries = data.get('entries') if isinstance(data, dict) else {}
        if not isinstance(entries, dict):
            entries = {}
        data = {'entries': entries, 'last_urls': urls}
        return _write_service_manifest(data)
    except Exception:
        return False


def remember_service_epg_source(m3u_url, epg_url):
    try:
        m3u_url = _to_text(m3u_url).strip()
        epg_url = _to_text(epg_url).strip()
        if not m3u_url or not epg_url:
            return False
        data = _read_service_manifest_for_lookup()
        entries = data.get('entries') if isinstance(data, dict) else {}
        if not isinstance(entries, dict):
            entries = {}
        entries[m3u_url] = {
            'm3u_url': m3u_url,
            'epg_url': epg_url,
            'source_key': _source_key(m3u_url, epg_url),
            'source_label': _source_label(m3u_url, epg_url),
            'warmed_at': int(time.time()),
        }
        data['entries'] = entries
        urls = list(data.get('last_urls') or [])
        if m3u_url not in urls:
            urls.append(m3u_url)
        data['last_urls'] = urls
        return _write_service_manifest(data)
    except Exception:
        return False


def _epg_cache_status_for_pair(m3u_url, epg_url):
    now_ts = int(time.time())
    day_key, day_start_utc, day_end_utc = _local_day_window(now_ts)
    xmltv_file = _xmltv_file_for_source(m3u_url, epg_url)
    index_file = _index_file_for_source(m3u_url, epg_url, day_key)

    if not os.path.exists(xmltv_file):
        return {'ready': False, 'reason': 'xmltv_ausente', 'm3u_url': m3u_url, 'epg_url': epg_url}
    if not os.path.exists(index_file):
        return {'ready': False, 'reason': 'indice_ausente', 'm3u_url': m3u_url, 'epg_url': epg_url}
    if not _file_is_fresh(xmltv_file):
        return {'ready': False, 'reason': 'xmltv_vencido', 'm3u_url': m3u_url, 'epg_url': epg_url}
    if not _file_is_fresh(index_file):
        return {'ready': False, 'reason': 'indice_vencido', 'm3u_url': m3u_url, 'epg_url': epg_url}

    try:
        meta = _ensure_xmltv_meta(m3u_url, epg_url, xmltv_file)
    except Exception:
        meta = None
    if not _xmltv_has_relevant_coverage(meta, day_key, day_start_utc, day_end_utc, now_ts=now_ts):
        return {'ready': False, 'reason': 'cobertura_insuficiente', 'm3u_url': m3u_url, 'epg_url': epg_url}

    # Para o propósito atual, o cache ideal precisa ter hoje + amanhã com
    # cobertura mínima útil. Se o XMLTV foi baixado recentemente e o provedor
    # ainda não publicou amanhã completo, aceitamos temporariamente para não
    # martelar o servidor.
    if not _xmltv_covers_future_window(meta, day_key, day_start_utc, day_end_utc, now_ts=now_ts):
        if not _xmltv_recently_checked(meta, now_ts=now_ts):
            return {'ready': False, 'reason': 'pouca_cobertura_futura', 'm3u_url': m3u_url, 'epg_url': epg_url}
    tomorrow_limited_recent = False
    if not _xmltv_has_tomorrow_coverage(meta, day_key, day_start_utc, day_end_utc, now_ts=now_ts):
        if not _xmltv_recently_checked(meta, now_ts=now_ts):
            return {'ready': False, 'reason': 'amanha_cobertura_curta', 'm3u_url': m3u_url, 'epg_url': epg_url}
        # XMLTV foi conferido/baixado há pouco e a própria fonte ainda não trouxe
        # amanhã suficiente. Nessa janela, usar o cache limitado é melhor que
        # ficar reconstruindo o mesmo JSON a cada boot/navegação. O status fica
        # explícito para auditoria, mas sem travar o fluxo.
        tomorrow_limited_recent = True

    cached = _read_cached_sqlite_index(index_file, xmltv_file, day_key)
    if cached is None:
        return {'ready': False, 'reason': 'indice_invalido', 'm3u_url': m3u_url, 'epg_url': epg_url}

    reason = 'cache_valido'
    if tomorrow_limited_recent:
        reason = 'cache_valido_amanha_curto_recente'

    return {
        'ready': True,
        'reason': reason,
        'm3u_url': m3u_url,
        'epg_url': epg_url,
        'xmltv_file': xmltv_file,
        'index_file': index_file,
        'day_key': day_key,
        'tomorrow_coverage_seconds': int(cached.get('tomorrow_coverage_seconds') or 0) if isinstance(cached, dict) else 0,
        'tomorrow_coverage_ok': bool(cached.get('tomorrow_coverage_ok')) if isinstance(cached, dict) else False,
    }


def epg_cache_status_for_list(m3u_url, allow_m3u_fetch=True):
    """Verifica se o EPG de uma lista já está pronto sem baixar XMLTV.

    Quando allow_m3u_fetch=False, usa apenas o manifesto local gravado pelo
    aquecimento anterior. Assim o boot consegue pular lista01..lista08 sem
    baixar a própria lista M3U para descobrir o x-tvg-url.
    """
    m3u_url = _to_text(m3u_url).strip()
    if not m3u_url:
        return {'ready': False, 'reason': 'lista_vazia', 'm3u_url': m3u_url, 'epg_url': ''}

    epg_url = ''
    data = _read_service_manifest_for_lookup()
    entries = data.get('entries') if isinstance(data, dict) else {}
    if isinstance(entries, dict):
        entry = entries.get(m3u_url) or {}
        if isinstance(entry, dict):
            epg_url = _to_text(entry.get('epg_url')).strip()

    if not epg_url and allow_m3u_fetch:
        epg_url = get_m3u_epg_url(m3u_url)

    if not epg_url:
        return {'ready': False, 'reason': 'epg_url_desconhecida', 'm3u_url': m3u_url, 'epg_url': ''}

    return _epg_cache_status_for_pair(m3u_url, epg_url)


def _scan_existing_epg_cache_status(max_lists=None):
    """Fallback sem manifesto: detecta SQLite do dia já montado.

    Usado após atualização do addon. Não baixa M3U/XMLTV apenas para descobrir
    a URL de EPG quando o banco local e o XMLTV correspondente já estão válidos.
    """
    try:
        expected = int(max_lists) if max_lists is not None else 0
    except Exception:
        expected = 0
    if expected <= 0:
        expected = 1
    day_key, day_start_utc, day_end_utc = _local_day_window()
    ready = 0
    details = []
    try:
        ensure_dir(EPG_SQLITE_DIR)
        filenames = os.listdir(EPG_SQLITE_DIR)
    except Exception:
        filenames = []

    for filename in filenames:
        if ready >= expected:
            break
        lowered = (filename or '').lower()
        if not lowered.endswith('.xml'):
            continue
        stem = filename[:-4]
        xmltv_file = os.path.join(EPG_CACHE_DIR, filename)
        index_file = os.path.join(EPG_SQLITE_DIR, '{}_{}.db'.format(stem, day_key))
        if not os.path.exists(index_file):
            continue
        if not _file_is_fresh(xmltv_file) or not _file_is_fresh(index_file):
            continue
        try:
            data = _read_cached_sqlite_index(index_file, xmltv_file, day_key)
            if not data:
                continue
            ready += 1
            details.append({'ready': True, 'reason': 'cache_existente_sem_manifesto', 'xmltv_file': xmltv_file, 'index_file': index_file})
        except Exception:
            continue

    return {
        'ready': bool(ready >= expected),
        'total': expected,
        'ready_count': ready,
        'missing_count': max(0, expected - ready),
        'missing': [] if ready >= expected else [{'url': '', 'reason': 'manifesto_ausente_cache_insuficiente'}],
        'details': details,
        'scanned_without_manifest': True,
    }

def service_epg_cache_status(max_lists=None):
    """Resumo do cache conhecido pelo service, sem acesso à rede."""
    data = _read_service_manifest_for_lookup()
    urls = list(data.get('last_urls') or []) if isinstance(data, dict) else []
    if not urls:
        return _scan_existing_epg_cache_status(max_lists=max_lists)
    if max_lists is not None:
        try:
            urls = urls[:max(0, int(max_lists))]
        except Exception:
            pass
    total = len(urls)
    ready = 0
    missing = []
    details = []
    for url in urls:
        status = epg_cache_status_for_list(url, allow_m3u_fetch=False)
        details.append(status)
        if status.get('ready'):
            ready += 1
        else:
            missing.append({'url': url, 'reason': status.get('reason', 'desconhecido')})
    return {
        'ready': bool(total > 0 and ready == total),
        'total': total,
        'ready_count': ready,
        'missing_count': max(0, total - ready),
        'missing': missing,
        'details': details,
    }


def _local_day_key_offset(offset_days=0, now_ts=None):
    """Retorna day_key local BRT com deslocamento em dias."""
    try:
        base_ts = int(time.time()) if now_ts is None else int(now_ts)
        offset = int(offset_days) * 86400
    except Exception:
        base_ts = int(time.time())
        offset = 0
    local_now = base_ts + BRAZIL_UTC_OFFSET_SECONDS + offset
    try:
        return time.strftime('%Y%m%d', time.gmtime(local_now))
    except Exception:
        return ''


def cleanup_old_epg_index_cache(keep_days=1, now_ts=None):
    """Remove índices EPG antigos e índices JSON legados.

    A arquitetura atual grava somente ``*_YYYYMMDD.db``. Qualquer JSON diário
    de versões anteriores é removido inclusive se for do dia atual, evitando
    duplicação de cache e desperdício de espaço em TV Box. XMLTV, meta.json e
    service_manifest.json não entram nesta limpeza.
    """
    try:
        keep_days = int(keep_days)
    except Exception:
        keep_days = 1
    if keep_days < 1:
        keep_days = 1

    keep_keys = set()
    for offset in range(0, keep_days):
        key = _local_day_key_offset(-offset, now_ts=now_ts)
        if key:
            keep_keys.add(key)
    if not keep_keys:
        return {'removed': 0, 'kept': 0, 'ignored': 0, 'keep_keys': []}

    oldest_keep = min(keep_keys)
    # Base de índice: .db atual; .json é legado e deve sair sempre.
    pattern = re.compile(r'^(.*)_(\d{8})\.(db|json)(?:-(wal|shm))?$', re.IGNORECASE)
    removed = 0
    kept = 0
    ignored = 0

    try:
        ensure_dir(EPG_SQLITE_DIR)
        filenames = os.listdir(EPG_SQLITE_DIR)
    except Exception:
        filenames = []

    for filename in filenames:
        try:
            match = pattern.match(filename or '')
            if not match:
                ignored += 1
                continue
            day_key = match.group(2)
            ext = match.group(3).lower()
            path = os.path.join(EPG_SQLITE_DIR, filename)

            # JSON com chave diária é legado, mesmo se for do dia atual.
            should_remove = (ext == 'json') or (day_key not in keep_keys and day_key < oldest_keep)
            if not should_remove:
                kept += 1
                continue

            if os.path.isfile(path):
                os.remove(path)
                removed += 1
            else:
                ignored += 1
        except Exception:
            ignored += 1

    return {
        'removed': removed,
        'kept': kept,
        'ignored': ignored,
        'keep_keys': sorted(list(keep_keys)),
    }

def clear_epg_cache():
    removed = 0
    seen = set()
    for base_path in (EPG_CACHE_DIR, EPG_XMLTV_DIR, EPG_INDEX_DIR):
        if base_path in seen:
            continue
        seen.add(base_path)
        try:
            ensure_dir(base_path)
            for filename in os.listdir(base_path):
                path = os.path.join(base_path, filename)
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                        removed += 1
                    elif os.path.isdir(path):
                        shutil.rmtree(path)
                        removed += 1
                except Exception:
                    pass
        except Exception:
            pass
    return removed


def _epg_channel_keys(channel):
    keys = []
    for value in (channel.get('tvg_id'), channel.get('tvg_name'), channel.get('name')):
        for alias in _channel_alias_forms(value):
            if alias and alias not in keys:
                keys.append(alias)
    return keys


def _score_channel_match(channel_tokens, alias_tokens):
    if not channel_tokens or not alias_tokens:
        return 0.0
    overlap = len(channel_tokens & alias_tokens)
    if overlap <= 0:
        return 0.0
    union = len(channel_tokens | alias_tokens)
    if union <= 0:
        return 0.0
    coverage = float(overlap) / float(len(channel_tokens))
    jaccard = float(overlap) / float(union)
    return (coverage * 0.65) + (jaccard * 0.35)


def _resolve_epg_entry_for_channel(channel, channel_entries):
    direct_keys = _epg_channel_keys(channel)
    for key in direct_keys:
        match = channel_entries.get(key)
        if match:
            return match, 'direct', key

    candidate_tokens = []
    for key in direct_keys:
        tokens = set(_tokenize_key(key))
        if tokens:
            candidate_tokens.append(tokens)

    if not candidate_tokens:
        return None, '', ''

    best_score = 0.0
    best_match = None
    best_alias = ''
    for alias, entry in channel_entries.items():
        alias_tokens = set(_tokenize_key(alias))
        if not alias_tokens:
            continue
        alias_score = 0.0
        for channel_token_set in candidate_tokens:
            score = _score_channel_match(channel_token_set, alias_tokens)
            if score > alias_score:
                alias_score = score
        if alias_score > best_score:
            best_score = alias_score
            best_match = entry
            best_alias = alias

    if best_match is not None and best_score >= 0.86:
        return best_match, 'fuzzy', best_alias
    return None, '', ''


def _find_text(element, tag_name):
    for child in list(element):
        if child.tag == tag_name and child.text:
            return _to_text(child.text).strip()
    return ''


def _determine_current_next(schedule, now_ts):
    current = None
    upcoming = None
    for item in schedule:
        start_ts = int(item.get('start') or 0)
        stop_ts = int(item.get('stop') or 0)
        if start_ts <= now_ts < stop_ts:
            current = item
        elif start_ts > now_ts and upcoming is None:
            upcoming = item
        if current and upcoming:
            break
    return current, upcoming


def _sqlite_connect(path, read_only=False):
    if sqlite3 is None:
        raise RuntimeError('sqlite3 indisponível no ambiente Kodi')
    # URI mode não é uniforme em todas as portas Kodi. Abrir normalmente e
    # ativar query_only quando disponível é mais compatível que exigir URI.
    conn = sqlite3.connect(path, timeout=3.0 if read_only else 8.0)
    try:
        conn.execute('PRAGMA busy_timeout={}'.format(2500 if read_only else 8000))
    except Exception:
        pass
    try:
        conn.execute('PRAGMA foreign_keys=OFF')
    except Exception:
        pass
    if read_only:
        try:
            conn.execute('PRAGMA query_only=ON')
        except Exception:
            pass
    return conn


def _sqlite_set_meta(conn, values):
    rows = []
    for key, value in (values or {}).items():
        rows.append((_to_text(key), _to_text(value)))
    if rows:
        conn.executemany('INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)', rows)


def _sqlite_get_meta(conn):
    out = {}
    try:
        for key, value in conn.execute('SELECT key, value FROM meta'):
            out[_to_text(key)] = _to_text(value)
    except Exception:
        return {}
    return out


def _sqlite_int(meta, key, default=0):
    try:
        return int((meta or {}).get(key) or default)
    except Exception:
        return default


def _create_epg_sqlite_schema(conn):
    conn.execute('CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    conn.execute('CREATE TABLE channels (channel_id TEXT PRIMARY KEY, source_name TEXT NOT NULL DEFAULT \'\')')
    # Um alias pode existir em mais de um canal. Mantemos todos e usamos
    # prioridade + nome de origem na consulta, evitando que um apelido genérico
    # (ex.: "discovery alt") sobrescreva outro canal válido.
    conn.execute('CREATE TABLE aliases (alias TEXT NOT NULL, channel_id TEXT NOT NULL, rank INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(alias, channel_id))')
    conn.execute('CREATE INDEX idx_alias_lookup ON aliases(alias, rank DESC)')
    conn.execute('CREATE INDEX idx_alias_channel ON aliases(channel_id)')
    conn.execute('CREATE TABLE programs (channel_id TEXT NOT NULL, start_ts INTEGER NOT NULL, stop_ts INTEGER NOT NULL, title TEXT NOT NULL DEFAULT \'\', desc TEXT NOT NULL DEFAULT \'\')')
    conn.execute('CREATE INDEX idx_programs_channel_start ON programs(channel_id, start_ts, stop_ts)')
    conn.execute('CREATE INDEX idx_programs_window ON programs(start_ts, stop_ts)')


def _flush_sqlite_program_batch(conn, rows):
    if not rows:
        return
    conn.executemany(
        'INSERT INTO programs(channel_id, start_ts, stop_ts, title, desc) VALUES (?, ?, ?, ?, ?)',
        rows
    )
    del rows[:]


def _cleanup_sqlite_sidecars(path):
    for suffix in ('-wal', '-shm', '-journal'):
        try:
            sidecar = path + suffix
            if os.path.exists(sidecar):
                os.remove(sidecar)
        except Exception:
            pass


def _build_day_epg_sqlite(xmltv_file, now_ts, day_key, day_start_utc, day_end_utc, db_file):
    """Monta SQLite local sem duplicar a grade por aliases.

    O XMLTV é percorrido uma única vez. A UI depois consulta somente os canais
    da categoria aberta em vez de desserializar todo o índice JSON da lista.
    """
    ensure_dir(EPG_SQLITE_DIR)
    temp_path = _unique_temp_path(db_file)
    _cleanup_sqlite_sidecars(temp_path)
    try:
        if os.path.exists(temp_path):
            os.remove(temp_path)
    except Exception:
        pass

    tomorrow_start_utc = int(day_start_utc) + 86400
    tomorrow_until_utc = 0
    has_tomorrow = False
    programme_count = 0
    conn = None
    rows = []
    try:
        conn = _sqlite_connect(temp_path, read_only=False)
        # Evita arquivos WAL/SHM e reduz manutenção extra em armazenamento fraco.
        conn.execute('PRAGMA journal_mode=DELETE')
        conn.execute('PRAGMA synchronous=NORMAL')
        # FILE reduz pico de RAM em Android/ARM/iOS durante criação de índices grandes.
        # Caso a build ignore a preferência, o SQLite usa seu padrão seguro.
        try:
            conn.execute('PRAGMA temp_store=FILE')
        except Exception:
            pass
        _create_epg_sqlite_schema(conn)
        conn.execute('BEGIN')

        for event, elem in ET.iterparse(xmltv_file, events=('end',)):
            if elem.tag == 'channel':
                channel_id = _normalize_key(elem.get('id'))
                if channel_id:
                    primary_name = ''
                    alias_ranks = {channel_id: 100}
                    display_index = 0
                    for child in list(elem):
                        if child.tag == 'display-name' and child.text:
                            display_name = _to_text(child.text).strip()
                            if display_name and not primary_name:
                                primary_name = display_name
                            if not display_name:
                                continue
                            display_index += 1
                            raw_alias = _normalize_key(display_name)
                            if raw_alias:
                                alias_ranks[raw_alias] = max(alias_ranks.get(raw_alias, 0), 92 if display_index == 1 else 88)
                            forms = _channel_alias_forms(display_name)
                            for form_index, alias in enumerate(forms):
                                if not alias:
                                    continue
                                # Formas mais derivadas são úteis como fallback,
                                # mas não devem vencer um nome de canal exato.
                                rank = max(35, (86 if display_index == 1 else 80) - (form_index * 8))
                                alias_ranks[alias] = max(alias_ranks.get(alias, 0), rank)
                    conn.execute(
                        'INSERT OR REPLACE INTO channels(channel_id, source_name) VALUES (?, ?)',
                        (channel_id, primary_name or channel_id)
                    )
                    for alias, rank in alias_ranks.items():
                        if alias:
                            conn.execute('INSERT OR REPLACE INTO aliases(alias, channel_id, rank) VALUES (?, ?, ?)', (alias, channel_id, int(rank)))
                elem.clear()
                continue

            if elem.tag != 'programme':
                # Filhos de <channel> e <programme> precisam permanecer até o
                # elemento-pai ser processado; limpar aqui apagaria display-name,
                # title e desc antes da leitura.
                continue

            channel_attr = _normalize_key(elem.get('channel'))
            if not channel_attr:
                elem.clear()
                continue
            start_ts = _parse_xmltv_datetime(elem.get('start'))
            stop_ts = _parse_xmltv_datetime(elem.get('stop'))
            if start_ts is None or stop_ts is None or stop_ts <= day_start_utc or start_ts >= day_end_utc:
                elem.clear()
                continue

            title = _find_text(elem, 'title')
            desc = _find_text(elem, 'desc')
            rows.append((channel_attr, int(start_ts), int(stop_ts), title, desc))
            programme_count += 1
            if start_ts >= tomorrow_start_utc:
                has_tomorrow = True
                if stop_ts > tomorrow_until_utc:
                    tomorrow_until_utc = int(stop_ts)
            if len(rows) >= 500:
                _flush_sqlite_program_batch(conn, rows)
            elem.clear()

        _flush_sqlite_program_batch(conn, rows)
        tomorrow_coverage_seconds = max(0, int(tomorrow_until_utc or 0) - int(tomorrow_start_utc))
        metadata = {
            'schema_version': EPG_SQLITE_SCHEMA_VERSION,
            'version': EPG_CACHE_VERSION,
            'generated_at': int(now_ts),
            'day_key': day_key,
            'window_start_utc': int(day_start_utc),
            'window_end_utc': int(day_end_utc),
            'has_tomorrow': 1 if has_tomorrow else 0,
            'tomorrow_until_utc': int(tomorrow_until_utc or 0),
            'tomorrow_coverage_seconds': int(tomorrow_coverage_seconds),
            'tomorrow_coverage_ok': 1 if tomorrow_coverage_seconds >= int(EPG_MIN_TOMORROW_COVERAGE_SECONDS) else 0,
            'tomorrow_min_required_seconds': int(EPG_MIN_TOMORROW_COVERAGE_SECONDS),
            'raw_mtime': int(os.path.getmtime(xmltv_file)),
            'raw_size': int(os.path.getsize(xmltv_file)),
            'programme_count': int(programme_count),
        }
        _sqlite_set_meta(conn, metadata)
        conn.commit()
        conn.close()
        conn = None
        _cleanup_sqlite_sidecars(temp_path)
        _replace_file(temp_path, db_file)
        return metadata
    except Exception as exc:
        try:
            if conn is not None:
                conn.rollback()
        except Exception:
            pass
        raise exc
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        _cleanup_sqlite_sidecars(temp_path)
        try:
            if os.path.exists(temp_path):
                os.remove(temp_path)
        except Exception:
            pass
        # Libera objetos ElementTree e batches antes de o service seguir para
        # outra lista; importante em Android/TV Box e portas ARM com pouca RAM.
        try:
            gc.collect()
        except Exception:
            pass


def _read_cached_sqlite_index(db_file, xmltv_file, day_key):
    if not _file_is_fresh(db_file):
        return None
    conn = None
    try:
        conn = _sqlite_connect(db_file, read_only=True)
        meta = _sqlite_get_meta(conn)
        if not meta:
            return None
        if meta.get('schema_version') != EPG_SQLITE_SCHEMA_VERSION:
            return None
        if meta.get('version') != EPG_CACHE_VERSION:
            return None
        if meta.get('day_key') != day_key:
            return None
        if _sqlite_int(meta, 'raw_mtime') != int(os.path.getmtime(xmltv_file)):
            return None
        if _sqlite_int(meta, 'raw_size') != int(os.path.getsize(xmltv_file)):
            return None
        return {
            'version': meta.get('version', ''),
            'day_key': meta.get('day_key', ''),
            'raw_mtime': _sqlite_int(meta, 'raw_mtime'),
            'raw_size': _sqlite_int(meta, 'raw_size'),
            'tomorrow_coverage_seconds': _sqlite_int(meta, 'tomorrow_coverage_seconds'),
            'tomorrow_coverage_ok': bool(_sqlite_int(meta, 'tomorrow_coverage_ok')),
            'programme_count': _sqlite_int(meta, 'programme_count'),
        }
    except Exception as exc:
        log('Falha ao ler índice SQLite EPG {}: {}'.format(db_file, exc), level=2)
        return None
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _ensure_epg_sqlite_index(m3u_url, epg_url, force_xmltv_refresh=False):
    now_ts = int(time.time())
    day_key, day_start_utc, day_end_utc = _local_day_window(now_ts)
    xmltv_file = _ensure_local_xmltv(
        m3u_url, epg_url, day_key=day_key, day_start_utc=day_start_utc,
        day_end_utc=day_end_utc, force_refresh=force_xmltv_refresh
    )
    db_file = _sqlite_file_for_source(m3u_url, epg_url, day_key)
    cached = _read_cached_sqlite_index(db_file, xmltv_file, day_key)
    if cached is not None:
        return db_file, cached
    built = _build_day_epg_sqlite(xmltv_file, now_ts, day_key, day_start_utc, day_end_utc, db_file)
    return db_file, built


def _sql_chunks(values, size=800):
    values = list(values or [])
    for index in range(0, len(values), max(1, int(size))):
        yield values[index:index + max(1, int(size))]


def _lookup_direct_aliases(conn, aliases):
    """Retorna todos os candidatos diretos por alias, ordenados por prioridade."""
    mapping = {}
    unique = []
    for alias in aliases or []:
        alias = _to_text(alias).strip()
        if alias and alias not in unique:
            unique.append(alias)
    for chunk in _sql_chunks(unique):
        placeholders = ','.join(['?'] * len(chunk))
        query = 'SELECT alias, channel_id, rank FROM aliases WHERE alias IN ({}) ORDER BY alias, rank DESC'.format(placeholders)
        for alias, channel_id, rank in conn.execute(query, chunk):
            alias = _to_text(alias)
            mapping.setdefault(alias, []).append((_to_text(channel_id), int(rank or 0)))
    return mapping

def _lookup_all_aliases(conn):
    try:
        return [(_to_text(alias), _to_text(channel_id), int(rank or 0)) for alias, channel_id, rank in conn.execute('SELECT alias, channel_id, rank FROM aliases')]
    except Exception:
        return []


def _lookup_source_names(conn, channel_ids):
    out = {}
    values = []
    for value in channel_ids or []:
        value = _to_text(value).strip()
        if value and value not in values:
            values.append(value)
    for chunk in _sql_chunks(values):
        placeholders = ','.join(['?'] * len(chunk))
        for channel_id, source_name in conn.execute('SELECT channel_id, source_name FROM channels WHERE channel_id IN ({})'.format(placeholders), chunk):
            out[_to_text(channel_id)] = _to_text(source_name)
    return out

def _resolve_sqlite_channels(conn, channels):
    """Resolve canais M3U para XMLTV com desambiguação conservadora.

    Primeiro consulta apenas aliases solicitados pela categoria. Quando um alias
    genérico aponta para mais de um canal, o nome/tvg-id do canal M3U recebe
    prioridade sobre formas simplificadas do XMLTV. Fuzzy só roda para os que
    não obtiveram candidato direto.
    """
    all_keys = []
    channel_keys = {}
    for channel in channels or []:
        cid = channel.get('id')
        keys = _epg_channel_keys(channel)
        channel_keys[cid] = keys
        all_keys.extend(keys)

    direct = _lookup_direct_aliases(conn, all_keys)
    candidate_ids = []
    for items in direct.values():
        for source_channel, rank in items:
            if source_channel not in candidate_ids:
                candidate_ids.append(source_channel)
    source_names = _lookup_source_names(conn, candidate_ids)

    resolved = {}
    modes = {}
    unresolved = []
    for channel in channels or []:
        cid = channel.get('id')
        keys = channel_keys.get(cid, [])
        scores = {}
        for key_position, key in enumerate(keys):
            candidates = direct.get(key, [])
            # Alias de uma palavra (ex.: "record") é muito genérico quando
            # aponta para mais de um canal. Nessa situação, deixa o fuzzy
            # conservador decidir ou mantém sem EPG em vez de mostrar grade errada.
            if len(_tokenize_key(key)) < 2 and len(candidates) > 1:
                continue
            for source_channel, rank in candidates:
                # Rank do alias + leve preferência por chave mais específica
                # (tvg-id vem antes de tvg-name/nome no _epg_channel_keys).
                score = int(rank) * 100 + max(0, 20 - key_position)
                source_forms = set(_channel_alias_forms(source_names.get(source_channel, '')))
                source_forms.add(_normalize_key(source_channel))
                if key in source_forms:
                    score += 1000
                scores[source_channel] = scores.get(source_channel, 0) + score
        if scores:
            selected = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[0][0]
            resolved[cid] = selected
            modes[cid] = 'direct'
        else:
            unresolved.append(channel)

    if not unresolved:
        return resolved, modes

    all_aliases = _lookup_all_aliases(conn)
    all_source_names = _lookup_source_names(conn, [source for _, source, _ in all_aliases])
    for channel in unresolved:
        cid = channel.get('id')
        candidate_tokens = []
        for key in channel_keys.get(cid, []):
            tokens = set(_tokenize_key(key))
            if tokens:
                candidate_tokens.append(tokens)
        if not candidate_tokens:
            continue
        best_score = 0.0
        best_channel = ''
        for alias, source_channel, rank in all_aliases:
            alias_tokens = set(_tokenize_key(alias))
            if not alias_tokens:
                continue
            alias_score = 0.0
            for candidate in candidate_tokens:
                score = _score_channel_match(candidate, alias_tokens)
                if score > alias_score:
                    alias_score = score
            # Nome de origem é um sinal mais confiável que alias derivado.
            source_tokens = set(_tokenize_key(all_source_names.get(source_channel, '')))
            for candidate in candidate_tokens:
                source_score = _score_channel_match(candidate, source_tokens)
                if source_score > alias_score:
                    alias_score = source_score
            weighted = alias_score + (float(rank) / 100000.0)
            if weighted > best_score:
                best_score = weighted
                best_channel = source_channel
        if best_channel and best_score >= 0.86:
            resolved[cid] = best_channel
            modes[cid] = 'fuzzy'
    return resolved, modes

def _load_sqlite_channel_schedules(conn, source_channels, window_start, window_end):
    schedules = {}
    source_names = {}
    wanted = []
    for value in source_channels or []:
        value = _to_text(value).strip()
        if value and value not in wanted:
            wanted.append(value)
    if not wanted:
        return schedules, source_names

    for chunk in _sql_chunks(wanted):
        placeholders = ','.join(['?'] * len(chunk))
        name_query = 'SELECT channel_id, source_name FROM channels WHERE channel_id IN ({})'.format(placeholders)
        for channel_id, source_name in conn.execute(name_query, chunk):
            source_names[_to_text(channel_id)] = _to_text(source_name)
        program_query = (
            'SELECT channel_id, start_ts, stop_ts, title, desc FROM programs '
            'WHERE channel_id IN ({}) AND stop_ts > ? AND start_ts < ? '
            'ORDER BY channel_id, start_ts, stop_ts'
        ).format(placeholders)
        params = list(chunk) + [int(window_start), int(window_end)]
        for channel_id, start_ts, stop_ts, title, desc in conn.execute(program_query, params):
            channel_id = _to_text(channel_id)
            schedules.setdefault(channel_id, []).append({
                'title': _to_text(title),
                'desc': _to_text(desc),
                'start': int(start_ts or 0),
                'stop': int(stop_ts or 0),
            })
    return schedules, source_names


def get_epg_for_channels(m3u_url, channels):
    epg_url = get_m3u_epg_url(m3u_url)
    if not epg_url or not channels:
        return {}

    db_file, metadata = _ensure_epg_sqlite_index(m3u_url, epg_url)
    conn = None
    try:
        conn = _sqlite_connect(db_file, read_only=True)
        resolved_channels, modes = _resolve_sqlite_channels(conn, channels)
        day_key, window_start, window_end = _local_day_window()
        schedules, source_names = _load_sqlite_channel_schedules(
            conn, list(resolved_channels.values()), window_start, window_end
        )
        now_ts = int(time.time())
        resolved = {}
        matched_direct = 0
        matched_fuzzy = 0
        schedule_cache = {}
        for channel in channels:
            cid = channel.get('id')
            source_channel = resolved_channels.get(cid)
            if not source_channel:
                continue
            schedule = schedule_cache.get(source_channel)
            if schedule is None:
                schedule = list(schedules.get(source_channel) or [])
                schedule_cache[source_channel] = schedule
            if not schedule:
                continue
            current, upcoming = _determine_current_next(schedule, now_ts)
            resolved[cid] = {
                'current': current,
                'next': upcoming,
                'schedule': schedule,
                'day_key': day_key,
                'source_channel': source_channel,
                'source_name': source_names.get(source_channel, source_channel),
            }
            if modes.get(cid) == 'direct':
                matched_direct += 1
            elif modes.get(cid) == 'fuzzy':
                matched_fuzzy += 1
        try:
            log('EPG [{}] canais={} direto={} fuzzy={} sem_match={} backend=sqlite_categoria'.format(
                _source_key(m3u_url, epg_url)[:8],
                len(channels),
                matched_direct,
                matched_fuzzy,
                max(0, len(channels) - len(resolved))
            ))
        except Exception:
            pass
        return resolved
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def warm_epg_cache_for_list(m3u_url, force_xmltv_refresh=False):
    """Prepara XMLTV e SQLite diário do EPG para uma lista M3U.

    O service usa esse caminho em segundo plano. Quando não existir cache,
    a navegação manual preserva o fallback e monta apenas a lista acessada.
    """
    epg_url = get_m3u_epg_url(m3u_url)
    if not epg_url:
        return False
    _ensure_epg_sqlite_index(m3u_url, epg_url, force_xmltv_refresh=force_xmltv_refresh)
    remember_service_epg_source(m3u_url, epg_url)
    return True

def warm_epg_cache_for_lists(m3u_urls, max_lists=None, wait_fn=None):
    """Aquece o cache EPG de várias listas, uma por vez.

    wait_fn, quando fornecida, deve retornar True para abortar o processamento.
    """
    processed = 0
    warmed = 0
    failed = 0
    urls = list(m3u_urls or [])
    if max_lists is not None:
        try:
            urls = urls[:max(0, int(max_lists))]
        except Exception:
            pass
    total = len(urls)
    for index, url in enumerate(urls, 1):
        if wait_fn is not None:
            try:
                if wait_fn(0):
                    break
            except Exception:
                pass
        processed += 1
        try:
            if warm_epg_cache_for_list(url):
                warmed += 1
        except Exception as exc:
            failed += 1
            try:
                log('Falha no aquecimento EPG da lista {}: {}'.format(index, exc), level=2)
            except Exception:
                pass
        if wait_fn is not None and index < total:
            try:
                if wait_fn(8):
                    break
            except Exception:
                pass
    return {'processed': processed, 'warmed': warmed, 'failed': failed}


def _epg_item_matches(a, b):
    if not a or not b:
        return False
    return (
        int(a.get('start') or 0) == int(b.get('start') or 0) and
        int(a.get('stop') or 0) == int(b.get('stop') or 0) and
        _to_text(a.get('title') or '') == _to_text(b.get('title') or '')
    )


def _build_list_epg_plot(entry):
    if not entry:
        return ''
    schedule = entry.get('schedule') or []
    current = entry.get('current') or {}
    now_ts = int(time.time())
    lines = []
    if schedule:
        lines.append('[COLOR gold]Guia de programação - EPG[/COLOR]')
        last_day_label = ''
        for item in schedule:
            stop_ts = int(item.get('stop') or 0)
            start_ts = int(item.get('start') or 0)
            if stop_ts and stop_ts <= now_ts:
                continue
            day_label = _format_brazil_day_label(start_ts or stop_ts, now_ts=now_ts)
            if day_label and day_label != last_day_label:
                lines.append('[COLOR gray]{}[/COLOR]'.format(day_label))
                last_day_label = day_label
            start_label = _format_brazil_time(start_ts) if start_ts else ''
            title = item.get('title', '') or 'Sem título'
            if current and _epg_item_matches(item, current):
                lines.append('[COLOR aquamarine][{}] {}[/COLOR]'.format(start_label, title) if start_label else '[COLOR aquamarine]{}[/COLOR]'.format(title))
            else:
                lines.append('[{}] {}'.format(start_label, title) if start_label else title)
    return '\n'.join([line for line in lines if line])


def describe_epg_entry(entry):
    if not entry:
        return {
            'label_suffix': '',
            'plot': '',
            'current_title': '',
            'next_title': '',
            'current_start': '',
            'next_start': '',
            'current_desc': '',
        }

    current = entry.get('current') or {}
    upcoming = entry.get('next') or {}
    label_suffix = current.get('title', '') if current.get('title') else ''
    current_start = _format_brazil_time(current.get('start')) if current.get('start') else ''
    next_start = _format_brazil_time(upcoming.get('start')) if upcoming.get('start') else ''
    current_desc = _to_text(current.get('desc') or '').strip()
    next_desc = _to_text(upcoming.get('desc') or '').strip()

    blocks = []
    if current.get('title'):
        if current_start:
            blocks.append('[COLOR aquamarine]Agora: [{}] {}[/COLOR]'.format(current_start, current.get('title', '')))
        else:
            blocks.append('[COLOR aquamarine]Agora: {}[/COLOR]'.format(current.get('title', '')))
        if current_desc:
            blocks.append('[COLOR white]{}[/COLOR]'.format(current_desc))

    if upcoming.get('title'):
        if blocks:
            blocks.append('')
        if next_start:
            blocks.append('[COLOR aquamarine]Próximo: [{}] {}[/COLOR]'.format(next_start, upcoming.get('title', '')))
        else:
            blocks.append('[COLOR aquamarine]Próximo: {}[/COLOR]'.format(upcoming.get('title', '')))
        if next_desc:
            blocks.append('[COLOR white]{}[/COLOR]'.format(next_desc))

    return {
        'label_suffix': label_suffix,
        'plot': '\n'.join(blocks),
        'current_title': current.get('title', ''),
        'next_title': upcoming.get('title', ''),
        'current_start': current_start,
        'next_start': next_start,
        'current_desc': current_desc,
        'next_desc': next_desc,
    }


def is_epg_program_current(program, now_ts=None):
    """Retorna True se o item da grade estiver em exibição agora."""
    try:
        if now_ts is None:
            now_ts = int(time.time())
        start_ts = int((program or {}).get('start') or 0)
        stop_ts = int((program or {}).get('stop') or 0)
        if start_ts <= 0:
            return False
        if stop_ts <= start_ts:
            stop_ts = start_ts + 3600
        return start_ts <= int(now_ts) < stop_ts
    except Exception:
        return False


def format_epg_program_range(program):
    """Formata horário de um programa no fuso operacional do addon."""
    try:
        program = program or {}
        start_ts = int(program.get('start') or 0)
        stop_ts = int(program.get('stop') or 0)
        start_label = _format_brazil_time(start_ts) if start_ts else ''
        stop_label = _format_brazil_time(stop_ts) if stop_ts else ''
        if start_label and stop_label:
            return '{} - {}'.format(start_label, stop_label)
        return start_label or stop_label
    except Exception:
        return ''


def format_epg_program_day(program, now_ts=None):
    """Retorna Hoje/Amanhã/dd/mm para separadores da Programação Completa."""
    try:
        program = program or {}
        start_ts = int(program.get('start') or program.get('stop') or 0)
        if not start_ts:
            return ''
        return _format_brazil_day_label(start_ts, now_ts=now_ts)
    except Exception:
        return ''


def get_full_epg_for_channel(m3u_url, channel, limit=0, include_current=True):
    """Retorna a programação completa disponível para um canal M3U.

    limit=0 mantém a grade completa da janela já indexada (hoje + amanhã),
    evitando cortar a seção de amanhã em canais com programação muito fragmentada.

    Usa o mesmo índice/cache do EPG em background. Se o cache ainda não
    estiver pronto, mantém o comportamento conservador do addon e deixa
    get_epg_for_channels preparar sob demanda como fallback.
    """
    if not m3u_url or not channel:
        return []
    try:
        epg_map = get_epg_for_channels(m3u_url, [channel])
        entry = epg_map.get(channel.get('id')) or {}
        schedule = list(entry.get('schedule') or [])
    except Exception:
        return []
    if not schedule:
        return []

    now_ts = int(time.time())
    max_items = max(0, int(limit or 0))
    out = []
    for item in schedule:
        if not isinstance(item, dict):
            continue
        try:
            start_ts = int(item.get('start') or 0)
            stop_ts = int(item.get('stop') or 0)
        except Exception:
            start_ts, stop_ts = 0, 0
        if start_ts <= 0:
            continue
        if stop_ts <= start_ts:
            stop_ts = start_ts + 3600
            item = dict(item)
            item['stop'] = stop_ts
        if stop_ts <= now_ts:
            continue
        if not include_current and start_ts <= now_ts < stop_ts:
            continue
        out.append(item)
        if max_items and len(out) >= max_items:
            break
    return out

def format_epg_entry(entry):
    meta = describe_epg_entry(entry)
    return meta.get('label_suffix', ''), _build_list_epg_plot(entry)


def _strip_stream_headers(url):
    if not url:
        return url
    if '|' in url:
        return url.split('|', 1)[0]
    if '%7C' in url:
        return url.split('%7C', 1)[0]
    return url


def convert_to_m3u8(url):
    if not url:
        return url

    url = _strip_stream_headers(url)
    lowered = url.lower()
    if ('.m3u8' not in lowered and '/hl' not in lowered and url.count('/') > 4 and
            '.mp4' not in lowered and '.avi' not in lowered):
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            host_part = '{}://{}'.format(parsed.scheme, parsed.netloc)
            remainder = url.split(host_part, 1)[1]
            url = host_part + '/live' + remainder
            filename = basename(url)
            if filename.lower().endswith('.ts'):
                url = url[:-3] + '.m3u8'
            else:
                url = url + '.m3u8'
        except Exception as exc:
            log('convert_to_m3u8 falhou para {}: {}'.format(url, exc), level=2)
    return url


def convert_to_ts(url):
    if not url:
        return url
    url = _strip_stream_headers(url)
    try:
        from urllib.parse import urlsplit, urlunsplit
        parts = urlsplit(url)
        path = parts.path or ''
        if '/live/' in path:
            path = path.replace('/live/', '/', 1)
        path = re.sub(r'\.m3u8$', '', path, flags=re.IGNORECASE)
        return urlunsplit((parts.scheme, parts.netloc, path, parts.query, parts.fragment))
    except Exception as exc:
        log('convert_to_ts falhou para {}: {}'.format(url, exc), level=2)
        return _strip_stream_headers(url)
