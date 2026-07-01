# -*- coding: utf-8 -*-
import urllib.parse
import requests
import ipaddress
import http.client
import re
import time
import logging
import threading
import queue
import socket
import select
import sys
import hashlib
from collections import deque

try:
    import xbmc
    import xbmcaddon
except ImportError:
    from kodi_six import xbmc, xbmcaddon

from urllib.parse import urljoin
from requests.exceptions import ConnectionError, RequestException, ReadTimeout, ChunkedEncodingError
try:
    from urllib3.exceptions import IncompleteRead
except ImportError:
    from urllib2 import HTTPError as IncompleteRead  # type: ignore

try:
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from socketserver import ThreadingMixIn
except ImportError:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer  # type: ignore
    from SocketServer import ThreadingMixIn  # type: ignore

PORT = 8089
DEFAULT_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"

# Pequenos buffers de continuidade: apenas memória de processo, nunca persistidos em disco.
IP_CACHE_TS = {}
IP_CACHE_MP4 = {}
_STREAM_CACHE_STAMPS = {'ts': {}, 'mp4': {}}
_STREAM_CACHE_LOCK = threading.RLock()
_STREAM_CACHE_KEY_LIMIT = 24
_SESSION_HOST_LIMIT = 128
_SESSION_SOURCE_LIMIT = 256
# Redirects/tokenized origins often expire quickly. Keep learned source routes
# short-lived and process-local so a stale redirect never leaks to another channel.
_SESSION_SOURCE_TTL = 45.0
_SERVER_THREAD = None
_SERVER_STARTED = False
_SERVER = None
_SERVER_LOCK = threading.Lock()
_SERVER_READY = threading.Event()
_SERVER_ERROR = ''
# Sinal e registros exclusivamente em memória para encerrar handlers que
# estejam presos em leitura de origem quando o Kodi está fechando.
_PROXY_STOP_EVENT = threading.Event()
_ACTIVE_NETWORK_LOCK = threading.RLock()
_ACTIVE_SESSIONS = set()
_ACTIVE_RESPONSES = set()

# Fontes VOD diretas podem parecer reproduzíveis, mas algumas ignoram Range
# ou encerram uma conexão parcialmente. O Kodi usa CFileCache para MKV/MP4
# remotos e pode ficar preso ao parar um arquivo desse tipo. O guard é local à
# sessão e evita repetir uma origem que já provou não ser seek-safe.
_VOD_GUARD_LOCK = threading.RLock()
_VOD_UNSAFE_SOURCES = {}
_VOD_UNSAFE_TTL = 300.0
_VOD_UNSAFE_LIMIT = 256

_TS_STATUS_LOG = {}
_ORIGIN_HEALTH = {}
# Bloqueio curto, exclusivamente em memória, por URL completa do canal.
# Evita martelar uma mesma origem que devolveu 401/403 repetidamente, sem
# contaminar outros canais do mesmo host Xtream.
_SOURCE_REJECTIONS = {}
_SOURCE_REJECTION_LOCK = threading.RLock()
_SOURCE_REJECTION_LIMIT = 256
_SOURCE_REJECTION_WINDOW = 60.0
_SOURCE_REJECTION_THRESHOLD = 2
_SOURCE_REJECTION_COOLDOWN = 20.0
_LOG_BURSTS = {}
_SESSION_CACHE = {
    'good_hosts': {},
    'bad_hosts': {},
    'working_user_agent': {},
    'last_working_url': {},
    'content_type': {},
    'source_profile': {},
    'source_stamps': {},
}
# Settings são consultadas em lote. O service invalida este snapshot quando
# Kodi dispara onSettingsChanged(), então o cache pode ser mais longo sem
# atrasar uma alteração manual do usuário.
_SETTINGS_CACHE_TTL = 30.0
_SETTINGS_CACHE = {
    'ts': 0,
    'addon': None,
    'mode': 'balance',
    'xtream_hardening': True,
    'host_memory': False,
    'dns_override': False,
    'debug': False,
}
_USER_AGENT_POOL = [
    DEFAULT_USER_AGENT,
    'Mozilla/5.0 (Linux; Android 10; SM-A505G) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0',
    'ExoPlayerLib/2.18.7',
]
_PROXY_POLICIES = {
    # O padrão de produção é balanceado: recupera microfalhas sem alongar
    # indefinidamente a troca de canal nem manter threads presas em origem morta.
    'fast': {
        'hls_playlist_retries': 2,
        'hls_segment_retries': 2,
        'ts_max_retries': 8,
        'ts_retry_budget': 30.0,
        'timeout_connect': 4,
        'timeout_read': 10,
        'segment_read_timeout': 6,
        'cooldown_base': 0.10,
    },
    'balance': {
        'hls_playlist_retries': 3,
        'hls_segment_retries': 3,
        'ts_max_retries': 12,
        'ts_retry_budget': 45.0,
        'timeout_connect': 5,
        'timeout_read': 12,
        'segment_read_timeout': 8,
        'cooldown_base': 0.15,
    },
    'stable': {
        'hls_playlist_retries': 4,
        'hls_segment_retries': 4,
        'ts_max_retries': 16,
        'ts_retry_budget': 75.0,
        'timeout_connect': 6,
        'timeout_read': 18,
        'segment_read_timeout': 10,
        'cooldown_base': 0.20,
        'ts_keepalive_window': 12.0,
        'ts_null_packets': 64,
        'ts_fill_sleep': 0.08,
    },
}


# completa defaults legados sem forçar mudança de comportamento em outros pontos
for _mode_name, _mode_policy in _PROXY_POLICIES.items():
    _mode_policy.setdefault('ts_keepalive_window', 6.0 if _mode_name == 'fast' else 8.0 if _mode_name == 'balance' else 12.0)
    _mode_policy.setdefault('ts_null_packets', 24 if _mode_name == 'fast' else 40 if _mode_name == 'balance' else 64)
    _mode_policy.setdefault('ts_fill_sleep', 0.05 if _mode_name == 'fast' else 0.06 if _mode_name == 'balance' else 0.08)
    _mode_policy.setdefault('ts_startup_keepalive_window', 3.0 if _mode_name == 'fast' else 5.0 if _mode_name == 'balance' else 7.0)
    _mode_policy.setdefault('ts_startup_read_timeout', 4.0 if _mode_name == 'fast' else 5.0 if _mode_name == 'balance' else 6.0)
    _mode_policy.setdefault('ts_startup_handoff_window', 4.0 if _mode_name == 'fast' else 5.0 if _mode_name == 'balance' else 6.0)
    _mode_policy.setdefault('ts_startup_handoff_bytes', 262144 if _mode_name == 'fast' else 393216 if _mode_name == 'balance' else 524288)
    _mode_policy.setdefault('hls_playlist_retries', 2 if _mode_name == 'fast' else 3 if _mode_name == 'balance' else 4)
    _mode_policy.setdefault('hls_segment_retries', 2 if _mode_name == 'fast' else 3 if _mode_name == 'balance' else 4)
    _mode_policy.setdefault('ts_max_retries', 8 if _mode_name == 'fast' else 12 if _mode_name == 'balance' else 16)
    _mode_policy.setdefault('ts_retry_budget', 30.0 if _mode_name == 'fast' else 45.0 if _mode_name == 'balance' else 75.0)
    _mode_policy.setdefault('segment_read_timeout', 6 if _mode_name == 'fast' else 8 if _mode_name == 'balance' else 10)


# VOD direto usa timeout de leitura curto: diferente de Live, um arquivo
# remoto com Range quebrado não deve manter o Kodi preso em FileCache após Stop.
for _mode_name, _mode_policy in _PROXY_POLICIES.items():
    _mode_policy.setdefault('vod_connect_timeout', 5.0)
    _mode_policy.setdefault('vod_read_timeout', 5.0 if _mode_name != 'stable' else 6.0)

# O proxy usa xbmc.log para seus eventos. Não toca no logger raiz, handlers
# ou sys.modules globais: todos são compartilhados pelo processo Kodi e outros
# addons podem depender deles, especialmente em Android/ARM e builds embarcadas.


def _safe_text(value):
    try:
        return str(value)
    except Exception:
        try:
            return repr(value)
        except Exception:
            return 'erro_desconhecido'


def _emit_log(message, level=logging.INFO):
    try:
        if level >= logging.ERROR:
            xbmc.log('[MegaPortugalProxy] ' + message, level=xbmc.LOGERROR)
        elif level >= logging.WARNING:
            xbmc.log('[MegaPortugalProxy] ' + message, level=xbmc.LOGWARNING)
        elif level >= logging.INFO:
            xbmc.log('[MegaPortugalProxy] ' + message, level=xbmc.LOGINFO)
        else:
            xbmc.log('[MegaPortugalProxy] ' + message, level=xbmc.LOGDEBUG)
    except Exception:
        pass


def _throttled_log(key, interval, level, message):
    now = time.time()
    last = _TS_STATUS_LOG.get(key, 0)
    if (now - last) >= interval:
        _TS_STATUS_LOG[key] = now
        _emit_log(message, level)


def _burst_log(key, window, level, template):
    now = time.time()
    item = _LOG_BURSTS.get(key)
    if not item or (now - item['start']) > window:
        item = {'start': now, 'count': 0}
    item['count'] += 1
    _LOG_BURSTS[key] = item
    if item['count'] == 1:
        _emit_log(template % 1, level)
    elif item['count'] in (5, 15, 30) or (now - item['start']) >= window:
        _emit_log(template % item['count'], level)
        item['start'] = now
        item['count'] = 0


def _log_ts_status_once(status_code):
    _burst_log('status:%s' % status_code, 45, logging.INFO, '[TS Downloader] HTTP {} recorrente; %sx no período.'.format(status_code))


def _log(msg, level=None):
    try:
        if level is None:
            level = xbmc.LOGINFO
        # evita poluir produção com debug sem perder mensagens úteis
        if level == xbmc.LOGDEBUG and not _settings_snapshot().get('debug'):
            return
        xbmc.log('[MegaPortugalProxy] ' + msg, level=level)
    except Exception:
        pass


def invalidate_settings_cache():
    """Força leitura nova na próxima requisição após mudança no Kodi."""
    _SETTINGS_CACHE['ts'] = 0
    _SETTINGS_CACHE['addon'] = None


def _addon_handle():
    now = time.time()
    if _SETTINGS_CACHE.get('addon') is not None and (now - _SETTINGS_CACHE.get('ts', 0)) < _SETTINGS_CACHE_TTL:
        return _SETTINGS_CACHE.get('addon')
    try:
        _SETTINGS_CACHE['addon'] = xbmcaddon.Addon('plugin.video.Mega.Portugal')  # type: ignore[name-defined]
        _SETTINGS_CACHE['ts'] = now
    except Exception:
        _SETTINGS_CACHE['addon'] = None
    return _SETTINGS_CACHE.get('addon')


def _settings_snapshot(force=False):
    now = time.time()
    if not force and (now - _SETTINGS_CACHE.get('ts', 0)) < _SETTINGS_CACHE_TTL:
        return _SETTINGS_CACHE
    addon = _addon_handle()
    mode = 'balance'
    xtream_hardening = True
    host_memory = False
    dns_override = False
    debug = False
    try:
        if addon:
            raw = (addon.getSetting('proxy_mode') or '').strip().lower()
            enum_map = {'0': 'fast', '1': 'balance', '2': 'stable', 'rápido': 'fast', 'rapido': 'fast', 'balanceado': 'balance', 'estável': 'stable', 'estavel': 'stable'}
            raw = enum_map.get(raw, raw)
            if raw in _PROXY_POLICIES:
                mode = raw
            xtream_hardening = (addon.getSetting('proxy_xtream_hardening') or 'true').lower() == 'true'
            host_memory = (addon.getSetting('proxy_host_memory') or 'false').lower() == 'true'
            dns_override = (addon.getSetting('proxy_dns_override') or 'false').lower() == 'true'
            debug = (addon.getSetting('proxy_debug') or 'false').lower() == 'true'
    except Exception:
        pass
    _SETTINGS_CACHE.update({
        'ts': now,
        'mode': mode,
        'xtream_hardening': xtream_hardening,
        'host_memory': host_memory,
        'dns_override': dns_override,
        'debug': debug,
    })
    return _SETTINGS_CACHE


def _get_mode():
    mode = _settings_snapshot().get('mode', 'balance')
    return mode if mode in _PROXY_POLICIES else 'balance'


def _policy():
    return _PROXY_POLICIES.get(_get_mode(), _PROXY_POLICIES['balance'])


def _classify_source(url):
    try:
        parsed = urllib.parse.urlparse(url or '')
        host = (parsed.netloc or '').lower()
        path = (parsed.path or '').lower()
        query = (parsed.query or '').lower()
    except Exception:
        host = path = query = ''
    joined = '%s%s?%s' % (host, path, query)
    is_ts = path.endswith('.ts') or '/hls/' in path or '/hl' in path
    is_m3u8 = path.endswith('.m3u8') or 'mpegurl' in joined
    tokenized = 'token=' in query or 'wmsauth' in query or 'auth=' in query
    is_xtream = '/live/' in path or '/hls/' in path or '/movie/' in path or '/series/' in path or tokenized
    bad_xtream = is_xtream and (is_ts or is_m3u8) and tokenized
    if bad_xtream:
        return 'xtream_bad'
    if is_xtream:
        return 'xtream_good'
    if is_m3u8:
        return 'hls_generic'
    if is_ts:
        return 'ts_generic'
    return 'generic'


def _source_memory_key(url):
    """Chave de memória por origem completa, nunca apenas por host.

    A query é preservada: tokens/sessões diferentes não podem reaproveitar uma
    URL final de outro canal. Fragmentos não participam da requisição HTTP e
    são removidos para manter a chave estável.
    """
    try:
        parsed = urllib.parse.urlsplit(url or '')
        scheme = (parsed.scheme or '').lower()
        netloc = (parsed.netloc or '').lower()
        if scheme not in ('http', 'https') or not netloc:
            return ''
        path = parsed.path or '/'
        query = ('?' + parsed.query) if parsed.query else ''
        return '%s://%s%s%s' % (scheme, netloc, path, query)
    except Exception:
        return ''


def _drop_source_memory(key):
    if not key:
        return
    for cache_name in ('working_user_agent', 'last_working_url', 'content_type', 'source_profile', 'source_stamps'):
        try:
            _SESSION_CACHE.get(cache_name, {}).pop(key, None)
        except Exception:
            pass


def _source_cache_get(cache_name, url):
    """Lê a memória de uma origem, respeitando flag, TTL e limite de sessão."""
    if not _settings_snapshot().get('host_memory'):
        return None
    key = _source_memory_key(url)
    if not key:
        return None
    stamps = _SESSION_CACHE.get('source_stamps', {})
    stamp = float(stamps.get(key, 0) or 0)
    if not stamp or (time.monotonic() - stamp) > _SESSION_SOURCE_TTL:
        _drop_source_memory(key)
        return None
    value = _SESSION_CACHE.get(cache_name, {}).get(key)
    if value is not None:
        stamps[key] = time.monotonic()
    return value


def _source_cache_set(cache_name, url, value):
    """Guarda dado efêmero para a mesma URL de origem, não para o domínio."""
    if not _settings_snapshot().get('host_memory'):
        return
    key = _source_memory_key(url)
    if not key or value is None:
        return
    _SESSION_CACHE.setdefault(cache_name, {})[key] = value
    _SESSION_CACHE.setdefault('source_stamps', {})[key] = time.monotonic()
    _trim_session_memory()


def _forget_source_memory(url, cache_names=('last_working_url',)):
    key = _source_memory_key(url)
    if not key:
        return
    for cache_name in cache_names:
        try:
            _SESSION_CACHE.get(cache_name, {}).pop(key, None)
        except Exception:
            pass
    # Mantém outros aprendizados ainda úteis, mas evita que um redirect/token
    # vencido seja escolhido de novo após 401/403/404/410.
    remaining = any(_SESSION_CACHE.get(name, {}).get(key) is not None for name in ('working_user_agent', 'content_type', 'source_profile', 'last_working_url'))
    if not remaining:
        _SESSION_CACHE.get('source_stamps', {}).pop(key, None)


def _source_profile(url):
    profile = _classify_source(url)
    cached = _source_cache_get('source_profile', url)
    return cached or profile


def _remember_source_profile(url, profile):
    if profile:
        _source_cache_set('source_profile', url, profile)


def _host_from_url(url):
    try:
        return urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return ''


def _trim_session_memory():
    """Limita caches de host e de origem em sessões Kodi muito longas."""
    hosts = _SESSION_CACHE.get('good_hosts', {})
    if len(hosts) > _SESSION_HOST_LIMIT:
        ordered = sorted(hosts.items(), key=lambda item: float((item[1] or {}).get('last_success', 0) or 0))
        for host, _item in ordered[:max(1, len(hosts) - _SESSION_HOST_LIMIT)]:
            for cache_name in ('good_hosts', 'bad_hosts'):
                try:
                    _SESSION_CACHE.get(cache_name, {}).pop(host, None)
                except Exception:
                    pass

    stamps = _SESSION_CACHE.get('source_stamps', {})
    if len(stamps) > _SESSION_SOURCE_LIMIT:
        ordered = sorted(stamps.items(), key=lambda item: float(item[1] or 0))
        for source_key, _stamp in ordered[:max(1, len(stamps) - _SESSION_SOURCE_LIMIT)]:
            _drop_source_memory(source_key)


def _cache_store(kind, key, chunk):
    if kind not in ('ts', 'mp4') or not key or not chunk:
        return
    cache = IP_CACHE_TS if kind == 'ts' else IP_CACHE_MP4
    stamps = _STREAM_CACHE_STAMPS[kind]
    with _STREAM_CACHE_LOCK:
        bucket = cache.setdefault(key, deque(maxlen=12))
        bucket.append(chunk)
        stamps[key] = time.monotonic()
        if len(cache) > _STREAM_CACHE_KEY_LIMIT:
            ordered = sorted(stamps.items(), key=lambda item: item[1])
            for stale_key, _stamp in ordered[:max(1, len(cache) - _STREAM_CACHE_KEY_LIMIT)]:
                cache.pop(stale_key, None)
                stamps.pop(stale_key, None)


def _cache_read(kind, key, limit=4):
    if kind not in ('ts', 'mp4') or not key:
        return []
    cache = IP_CACHE_TS if kind == 'ts' else IP_CACHE_MP4
    stamps = _STREAM_CACHE_STAMPS[kind]
    with _STREAM_CACHE_LOCK:
        chunks = list(cache.get(key, []))[-max(1, int(limit or 1)):]
        if chunks:
            stamps[key] = time.monotonic()
        return chunks


def _host_stats(host):
    item = _SESSION_CACHE['good_hosts'].get(host)
    if item is None:
        item = {'success_count': 0, 'fail_count': 0, 'cooldown_until': 0, 'last_error': '', 'last_success': 0}
        _SESSION_CACHE['good_hosts'][host] = item
    return item


def _host_is_available(host):
    if not host or not _settings_snapshot().get('host_memory'):
        return True
    return time.time() >= _host_stats(host).get('cooldown_until', 0)


def _host_backoff(host):
    if not host or not _settings_snapshot().get('host_memory'):
        return
    item = _host_stats(host)
    wait = item.get('cooldown_until', 0) - time.time()
    if wait > 0:
        time.sleep(min(0.8, wait))


def _mark_host_ok(host, final_url=None, user_agent=None, content_type=None, source_url=None):
    if not host or not _settings_snapshot().get('host_memory'):
        return
    item = _host_stats(host)
    item['success_count'] = item.get('success_count', 0) + 1
    item['fail_count'] = 0
    item['cooldown_until'] = 0
    item['last_error'] = ''
    item['last_success'] = time.time()
    _SESSION_CACHE['bad_hosts'].pop(host, None)

    memory_source = source_url or final_url
    if final_url and _is_valid_upstream_url(final_url):
        _source_cache_set('last_working_url', memory_source, final_url)
    if user_agent:
        _source_cache_set('working_user_agent', memory_source, user_agent)
    if content_type:
        _source_cache_set('content_type', memory_source, content_type)
    _trim_session_memory()


def _mark_host_fail(host, err='unknown', soft=False):
    if not host or not _settings_snapshot().get('host_memory'):
        return
    item = _host_stats(host)
    item['fail_count'] = item.get('fail_count', 0) + 1
    item['last_error'] = str(err)
    base = _policy().get('cooldown_base', 0.15)
    threshold = 5 if soft else 3
    if item['fail_count'] >= threshold:
        item['cooldown_until'] = time.time() + min(3.0, base * item['fail_count'])
        _SESSION_CACHE['bad_hosts'][host] = item['cooldown_until']


def _smart_retry_delay(err_kind, attempt):
    if err_kind == '406':
        return min(0.45, 0.08 * max(1, attempt))
    if err_kind == 'timeout':
        return min(1.0, 0.20 * max(1, attempt))
    if err_kind == 'connection':
        return min(0.8, 0.16 * max(1, attempt))
    return min(0.75, 0.14 * max(1, attempt))


def _pick_user_agent(host, reconnects=0, prefer_stream=False, source_url=''):
    if source_url and reconnects <= 1:
        cached = _source_cache_get('working_user_agent', source_url)
        if cached:
            return cached
    idx = reconnects % len(_USER_AGENT_POOL)
    ua = _USER_AGENT_POOL[idx]
    if prefer_stream and reconnects >= 3:
        ua = 'Lavf/60.3.100'
    return ua


def _validate_upstream_response(response, expect_playlist=False, expect_stream=False, url=''):
    try:
        ctype = (response.headers.get('content-type') or '').lower()
        final_url = response.url or url
    except Exception:
        ctype = ''
        final_url = url
    if response.status_code not in (200, 206):
        return False, ctype
    if not _is_valid_upstream_url(final_url):
        return False, ctype
    # Página HTML de login/erro não é playlist nem stream, mesmo que a origem
    # tenha respondido 200. Isso evita passar lixo para o player Kodi.
    if 'text/html' in ctype:
        return False, ctype
    if expect_stream and ('json' in ctype or 'xml' in ctype):
        return False, ctype
    profile = _source_profile(url)
    if expect_stream and profile in ('xtream_bad', 'xtream_good'):
        # Muitos painéis Xtream devolvem content-type genérico mesmo quando o
        # caminho termina em .m3u8. A confirmação do corpo é feita pelo sniff
        # abaixo; não rejeitar apenas pelo cabeçalho evita falso "offline".
        if not ctype or 'application/octet-stream' in ctype or 'video/' in ctype or 'audio/' in ctype or 'text/plain' in ctype:
            return True, ctype or 'application/octet-stream'
    return True, ctype


def _xtream_tolerance_active(url):
    """Aplica tolerância extra a streams Xtream com ou sem token.

    Antes, a tolerância era aplicada somente a URLs tokenizadas. Vários
    servidores Xtream problemáticos usam /live/usuario/senha/id.m3u8 sem token
    e recebiam o caminho rígido, gerando falsos 502 no início da reprodução.
    """
    try:
        if not _settings_snapshot().get('xtream_hardening'):
            return False
        return _source_profile(url) in ('xtream_bad', 'xtream_good')
    except Exception:
        return False


def _decode_manifest_bytes(payload):
    try:
        return (payload or b'').decode('utf-8-sig', errors='replace')
    except Exception:
        return ''


def _looks_like_hls_manifest(text):
    """Aceita HLS padrão e variações legadas comuns de painéis Xtream."""
    try:
        clean = (text or '').lstrip('\ufeff\x00 \t\r\n')
    except Exception:
        clean = ''
    if clean.startswith('#EXTM3U'):
        return True
    # Alguns painéis servem manifesto válido sem a linha inicial. Só aceita se
    # houver marcadores estruturais suficientes; HTML/JSON/texto solto continua
    # bloqueado pelo sniff e pela validação acima.
    if '#EXT-X-' in clean and ('#EXTINF:' in clean or '#EXT-X-STREAM-INF' in clean):
        return True
    if '#EXTINF:' in clean and ('\n' in clean or '\r' in clean):
        return True
    return False


def _looks_like_ts_payload(payload):
    """Detecta MPEG-TS sem confiar em extensão ou content-type da origem."""
    try:
        data = bytes(payload or b'')
    except Exception:
        return False
    if len(data) < 376:
        return False
    # TS pode usar pacote de 188, 192 (M2TS) ou 204 bytes. Procura ao menos
    # três sincronismos espaçados, permitindo pequeno desalinhamento inicial.
    for packet_size in (188, 192, 204):
        max_offset = min(packet_size, max(0, len(data) - (packet_size * 2)))
        for offset in range(max_offset + 1):
            positions = [offset, offset + packet_size, offset + (packet_size * 2)]
            if all(pos < len(data) and data[pos] == 0x47 for pos in positions):
                return True
    return False


def _looks_like_fmp4_payload(payload):
    try:
        data = bytes(payload or b'')[:96]
    except Exception:
        return False
    return (len(data) >= 12 and data[4:8] == b'ftyp') or (b'moof' in data[:64] and b'mdat' in data[:96])


def _safe_upstream_content_length(response, maximum=128 * 1024 * 1024):
    """Retorna Content-Length finito e plausível para mídia direta/segmentos.

    O proxy não inventa tamanho. Quando a origem já forneceu um comprimento
    confiável, repassá-lo evita que o FFmpeg trate cada segmento HLS fechado
    normalmente como stream HTTP infinito encerrado prematuramente.
    """
    try:
        raw = (response.headers.get('content-length') or '').strip()
        if not raw or not raw.isdigit():
            return ''
        value = int(raw)
        if 0 < value <= int(maximum):
            return str(value)
    except Exception:
        pass
    return ''


def _direct_media_kind(payload, content_type=''):
    if _looks_like_ts_payload(payload):
        return 'ts'
    if _looks_like_fmp4_payload(payload):
        return 'mp4'
    # Não libera qualquer octet-stream: só aceita media declarada pela origem,
    # pois respostas de erro em texto não podem virar "canal tocando".
    ctype = (content_type or '').lower()
    if ctype.startswith('video/') or ctype.startswith('audio/'):
        return 'media'
    return ''


def _read_manifest_body(iterator, first_chunk, limit=524288):
    """Lê manifesto pequeno com limite rígido para nunca segurar live em RAM."""
    parts = []
    total = 0
    if first_chunk:
        parts.append(first_chunk)
        total += len(first_chunk)
    if total > limit:
        return None
    for chunk in iterator:
        if not chunk:
            continue
        parts.append(chunk)
        total += len(chunk)
        if total > limit:
            return None
    try:
        return b''.join(parts)
    except Exception:
        return None


def _dns_override():
    # Mantido somente para integrações antigas. Nunca roda por request; o
    # service sincroniza a configuração uma vez no boot/evento de settings.
    try:
        from resources.lib import dns as dns_helper
        return bool(dns_helper.apply_configured_override())
    except Exception:
        return False


def _get_client_ip(headers, client_address):
    # O servidor só escuta loopback. Não confie em X-Forwarded-For enviado pelo
    # próprio cliente local, pois isso fragmentaria caches sem trazer benefício.
    return client_address[0] if client_address else '127.0.0.1'


def _get_cache_key(client_ip, url):
    return '%s:%s' % (client_ip, url)


def _is_valid_upstream_url(url):
    try:
        parsed = urllib.parse.urlparse(url or '')
        if (parsed.scheme or '').lower() not in ('http', 'https'):
            return False
        host = (parsed.hostname or '').strip().lower().rstrip('.')
        if not host:
            return False
        if host in ('localhost', 'localhost.localdomain', 'ip6-localhost') or host.endswith('.localhost'):
            return False
        # Bloqueia literal loopback/unspecified/link-local/multicast, inclusive
        # IPv6, sem bloquear streams legítimos na rede local (10/8, 192.168/16).
        try:
            address = ipaddress.ip_address(host)
            if address.is_loopback or address.is_unspecified or address.is_link_local or address.is_multicast:
                return False
        except ValueError:
            pass
        return True
    except Exception:
        return False


def _origin_key(url):
    try:
        p = urllib.parse.urlparse(url)
        return '%s://%s' % (p.scheme or 'http', p.netloc or '')
    except Exception:
        return url or 'unknown'


def _source_rejection_key(url):
    """Retorna chave opaca por URL completa, sem agrupar por host.

    Tokens e caminhos diferentes podem coexistir no mesmo servidor Xtream;
    por isso o circuito nunca usa somente dominio/porta.
    """
    try:
        payload = _safe_text(url or '').encode('utf-8', 'ignore')
    except Exception:
        payload = repr(url).encode('utf-8', 'ignore')
    try:
        return hashlib.sha256(payload).hexdigest()
    except Exception:
        return _safe_text(url or '')


def _trim_source_rejections(now=None):
    now = float(now or time.time())
    with _SOURCE_REJECTION_LOCK:
        stale = [key for key, item in _SOURCE_REJECTIONS.items()
                 if (now - float(item.get('updated', 0) or 0)) > 180.0]
        for key in stale:
            _SOURCE_REJECTIONS.pop(key, None)
        if len(_SOURCE_REJECTIONS) > _SOURCE_REJECTION_LIMIT:
            ordered = sorted(_SOURCE_REJECTIONS.items(), key=lambda pair: pair[1].get('updated', 0))
            for key, _ in ordered[:max(0, len(_SOURCE_REJECTIONS) - _SOURCE_REJECTION_LIMIT)]:
                _SOURCE_REJECTIONS.pop(key, None)


def _source_rejection_active(url):
    now = time.time()
    key = _source_rejection_key(url)
    with _SOURCE_REJECTION_LOCK:
        _trim_source_rejections(now)
        item = _SOURCE_REJECTIONS.get(key)
        if not item:
            return False
        return float(item.get('cooldown_until', 0) or 0) > now


def _mark_source_rejection(url, status_code):
    """Registra 401/403 consecutivos e retorna True quando abre circuito."""
    now = time.time()
    key = _source_rejection_key(url)
    with _SOURCE_REJECTION_LOCK:
        _trim_source_rejections(now)
        item = _SOURCE_REJECTIONS.get(key)
        if not item or (now - float(item.get('updated', 0) or 0)) > _SOURCE_REJECTION_WINDOW:
            item = {'count': 0, 'updated': now, 'cooldown_until': 0, 'status': status_code}
        item['count'] = int(item.get('count', 0) or 0) + 1
        item['updated'] = now
        item['status'] = status_code
        if item['count'] >= _SOURCE_REJECTION_THRESHOLD:
            item['cooldown_until'] = now + _SOURCE_REJECTION_COOLDOWN
        _SOURCE_REJECTIONS[key] = item
        return float(item.get('cooldown_until', 0) or 0) > now


def _clear_source_rejection(url):
    key = _source_rejection_key(url)
    with _SOURCE_REJECTION_LOCK:
        _SOURCE_REJECTIONS.pop(key, None)


def _origin_state(url):
    key = _origin_key(url)
    state = _ORIGIN_HEALTH.get(key)
    now = time.time()
    if not state or (now - state.get('updated', 0)) > 180:
        state = {
            'updated': now,
            'fail_count': 0,
            '406_count': 0,
            'timeout_count': 0,
            'good_count': 0,
            'cooldown_until': 0,
            'last_error': ''
        }
        _ORIGIN_HEALTH[key] = state
    return state


def _mark_origin_ok(url):
    state = _origin_state(url)
    state['updated'] = time.time()
    state['good_count'] = state.get('good_count', 0) + 1
    state['fail_count'] = 0
    state['406_count'] = 0
    state['timeout_count'] = 0
    state['last_error'] = ''
    state['cooldown_until'] = 0


def _mark_origin_fail(url, code=None, exc_name=None):
    state = _origin_state(url)
    state['updated'] = time.time()
    state['fail_count'] = state.get('fail_count', 0) + 1
    if code == 406:
        state['406_count'] = state.get('406_count', 0) + 1
    if exc_name in ('ReadTimeout', 'ConnectionError', 'ChunkedEncodingError'):
        state['timeout_count'] = state.get('timeout_count', 0) + 1
    state['last_error'] = str(code or exc_name or 'unknown')
    fail_count = state.get('fail_count', 0)
    if fail_count >= 6:
        cooldown = min(2.0, 0.15 * fail_count)
        state['cooldown_until'] = time.time() + cooldown


def _apply_origin_cooldown(url):
    state = _origin_state(url)
    now = time.time()
    cooldown_until = state.get('cooldown_until', 0)
    if cooldown_until > now:
        time.sleep(min(0.75, cooldown_until - now))


def _build_upstream_headers(original_headers, reconnects=0, response_code=None, prefer_stream=False, url=''):
    headers = dict(original_headers or {})
    profile = _source_profile(url)
    headers.pop('Host', None)
    headers.pop('Connection', None)
    headers.pop('Content-Length', None)

    if reconnects > 0:
        headers.setdefault('User-Agent', DEFAULT_USER_AGENT)
        headers['Connection'] = 'close'

    if prefer_stream:
        headers.setdefault('Accept', '*/*')
        headers['Icy-MetaData'] = '1'
        headers['Accept-Encoding'] = 'identity'

    if _xtream_tolerance_active(url):
        # M3U8/Xtream ruim frequentemente interpreta Range e revalidação como
        # pedido parcial inválido. Para manifestos e streams, uma requisição
        # limpa/identity é mais compatível que repassar cabeçalhos do Kodi.
        headers.pop('Range', None)
        headers.pop('If-Range', None)
        headers.pop('If-None-Match', None)
        headers.pop('If-Modified-Since', None)
        headers['Accept'] = '*/*'
        headers['Accept-Encoding'] = 'identity'
        headers['Connection'] = 'close'

    if response_code == 406:
        for noisy in ('Accept', 'Accept-Language', 'Origin', 'Referer', 'Sec-Fetch-Dest', 'Sec-Fetch-Mode', 'Sec-Fetch-Site'):
            headers.pop(noisy, None)
        headers['Accept'] = '*/*'
        headers['Connection'] = 'close'
        if reconnects >= 2:
            headers.pop('Range', None)
        if reconnects >= 3 and profile != 'xtream_bad':
            headers['User-Agent'] = _pick_user_agent(_host_from_url(url), reconnects, prefer_stream=prefer_stream, source_url=url)

    if reconnects >= 4:
        headers.pop('Accept-Encoding', None)
        headers['Cache-Control'] = 'no-cache'
        headers['Pragma'] = 'no-cache'

    return headers



def _timeout_tuple(url, prefer_stream=False, segment=False):
    policy = _policy()
    connect = policy.get('timeout_connect', 5)
    read = policy.get('timeout_read', 12)
    profile = _source_profile(url)
    if segment:
        # Segmento HLS precisa falhar rápido para o Kodi avançar/requisitar de
        # novo; não pode herdar o timeout maior do TS contínuo.
        return (connect, min(read, float(policy.get('segment_read_timeout', 8) or 8)))
    if _xtream_tolerance_active(url):
        return (min(connect, 4), max(read, 20 if prefer_stream else 16))
    if profile == 'hls_generic' and prefer_stream:
        return (connect, max(read, 16))
    return (connect, read)


def _is_probably_live_segment(url):
    u = (url or '').lower()
    return '.ts' in u or '/hls/' in u or '/hl' in u

def _rewrite_m3u8_urls(playlist_content, playlist_url, host=None):
    """Reescreve referências HLS para o loopback do proxy.

    Cobre linhas de mídia e atributos URI= usados por chaves AES, mapas fMP4,
    faixas de áudio e playlists-filhas. Sem depender de extensão .ts.
    """
    local_host = '127.0.0.1:%d' % PORT

    def proxy_url(candidate):
        candidate = (candidate or '').strip()
        if not candidate or candidate.startswith('#') or candidate == '/':
            return candidate
        try:
            absolute_url = urljoin(playlist_url, candidate)
            if not _is_valid_upstream_url(absolute_url):
                return candidate
            return 'http://%s/hlsretry?url=%s' % (
                local_host,
                urllib.parse.quote(absolute_url, safe='')
            )
        except Exception:
            return candidate

    def rewrite_uri_attribute(match):
        quote_char = match.group(1) or ''
        value = match.group(2)
        rewritten = proxy_url(value)
        return 'URI=%s%s%s' % (quote_char, rewritten, quote_char)

    out = []
    uri_quoted = re.compile(r'URI=(["\'])(.*?)(?:\1)', re.IGNORECASE)
    uri_unquoted = re.compile(r'URI=([^,\s]+)', re.IGNORECASE)
    for raw_line in (playlist_content or '').splitlines():
        line = raw_line.strip()
        if not line:
            out.append(raw_line)
            continue
        if line.startswith('#'):
            changed = uri_quoted.sub(rewrite_uri_attribute, raw_line)
            # Alguns manifests malformados trazem URI sem aspas; trata sem
            # tocar no restante da diretiva.
            if changed == raw_line:
                changed = uri_unquoted.sub(lambda m: 'URI=' + proxy_url(m.group(1)), raw_line)
            out.append(changed)
        else:
            out.append(proxy_url(line))
    suffix = '\n' if playlist_content and playlist_content.endswith(('\n', '\r')) else ''
    return '\n'.join(out) + suffix


def _stream_response(response, client_ip, url, sess, initial_chunks=None, iterator=None, stream_kind=None):
    cache_key = _get_cache_key(client_ip, url) if any(ext in url.lower() for ext in ['.mp4', '.m3u8']) else client_ip
    mode_ts = [stream_kind == 'ts']

    def source_chunks():
        for chunk in (initial_chunks or []):
            if chunk:
                yield chunk
        active_iterator = iterator if iterator is not None else response.iter_content(chunk_size=4096)
        for chunk in active_iterator:
            if chunk:
                yield chunk

    def generate_chunks():
        try:
            for chunk in source_chunks():
                if stream_kind == 'mp4' or (stream_kind is None and '.mp4' in url.lower()):
                    cache = IP_CACHE_MP4
                elif stream_kind == 'ts' or (stream_kind is None and ('.ts' in url.lower() or '/hl' in url.lower() or '.m4s' in url.lower())):
                    mode_ts[0] = True
                    cache = IP_CACHE_TS
                else:
                    cache = None
                if cache is not None:
                    _cache_store('ts' if mode_ts[0] else 'mp4', cache_key, chunk)
                yield chunk
        except (IncompleteRead, ConnectionError, ChunkedEncodingError):
            for chunk in _cache_read('ts' if mode_ts[0] else 'mp4', cache_key, limit=4):
                yield chunk
        finally:
            try:
                response.close()
            except Exception:
                pass
            try:
                sess.close()
            except Exception:
                pass
    return generate_chunks()


_TS_NULL_PACKET = b"\x47\x1f\xff\x10" + (b"\xff" * 184)


def _ts_null_chunk(packet_count=None):
    try:
        packets = int(packet_count or _policy().get('ts_null_packets', 40))
    except Exception:
        packets = 40
    packets = max(1, min(256, packets))
    return _TS_NULL_PACKET * packets


def _should_ts_keepalive(last_good_at, startup_keepalive_until=0):
    if last_good_at:
        window = float(_policy().get('ts_keepalive_window', 8.0) or 8.0)
        return (time.time() - last_good_at) <= max(1.5, window)
    return bool(startup_keepalive_until and time.time() <= startup_keepalive_until)


def _ts_keepalive_once(last_good_at, reconnects=0, startup_keepalive_until=0):
    if not _should_ts_keepalive(last_good_at, startup_keepalive_until=startup_keepalive_until):
        return None
    base_packets = int(_policy().get('ts_null_packets', 40) or 40)
    startup_mode = not bool(last_good_at)
    if startup_mode:
        packets = min(128, max(8, (base_packets // 2) + (min(reconnects, 4) * 4)))
    else:
        # sobe leve durante reconnects repetidos, sem virar mangueira de lixo
        packets = min(256, max(8, base_packets + (min(reconnects, 6) * 8)))
    return _ts_null_chunk(packets)


def _ts_keepalive_sleep(reconnects=0):
    base = float(_policy().get('ts_fill_sleep', 0.06) or 0.06)
    return min(0.25, base + (min(reconnects, 6) * 0.01))


def _stream_cache(client_ip, url):
    if not url:
        return None
    cache_key = _get_cache_key(client_ip, url) if any(ext in url.lower() for ext in ['.mp4', '.m3u8']) else client_ip
    if '.mp4' in url.lower():
        kind = 'mp4'
    elif '.ts' in url.lower() or '/hl' in url.lower() or '.m4s' in url.lower():
        kind = 'ts'
    else:
        return None
    chunks = _cache_read(kind, cache_key, limit=4)
    if not chunks:
        return None

    def generate_cached_chunks():
        for chunk in chunks:
            yield chunk
    return generate_cached_chunks()


def _vod_guard_key(url):
    try:
        parsed = urllib.parse.urlsplit(url or '')
        clean = '%s://%s%s?%s' % (
            (parsed.scheme or '').lower(),
            (parsed.netloc or '').lower(),
            parsed.path or '/',
            parsed.query or ''
        )
        return hashlib.sha256(clean.encode('utf-8', 'ignore')).hexdigest()
    except Exception:
        return _source_rejection_key(url)


def _trim_vod_guard(now=None):
    now = float(now or time.time())
    with _VOD_GUARD_LOCK:
        stale = [key for key, item in _VOD_UNSAFE_SOURCES.items()
                 if float(item.get('until', 0) or 0) <= now]
        for key in stale:
            _VOD_UNSAFE_SOURCES.pop(key, None)
        if len(_VOD_UNSAFE_SOURCES) > _VOD_UNSAFE_LIMIT:
            ordered = sorted(_VOD_UNSAFE_SOURCES.items(), key=lambda pair: pair[1].get('updated', 0))
            for key, _item in ordered[:max(1, len(_VOD_UNSAFE_SOURCES) - _VOD_UNSAFE_LIMIT)]:
                _VOD_UNSAFE_SOURCES.pop(key, None)


def _vod_source_is_blocked(url):
    now = time.time()
    key = _vod_guard_key(url)
    with _VOD_GUARD_LOCK:
        _trim_vod_guard(now)
        item = _VOD_UNSAFE_SOURCES.get(key)
        return bool(item and float(item.get('until', 0) or 0) > now)


def _mark_vod_source_unsafe(url, reason):
    now = time.time()
    key = _vod_guard_key(url)
    with _VOD_GUARD_LOCK:
        _trim_vod_guard(now)
        _VOD_UNSAFE_SOURCES[key] = {
            'updated': now,
            'until': now + _VOD_UNSAFE_TTL,
            'reason': _safe_text(reason)[:120],
        }
    _throttled_log('vod-unsafe:%s' % key, 30, logging.INFO,
                   '[VOD Seguro] Fonte direta sem Range confiável; bloqueada somente nesta sessão para evitar travamento no Stop.')


def _clear_vod_source_guard(url):
    with _VOD_GUARD_LOCK:
        _VOD_UNSAFE_SOURCES.pop(_vod_guard_key(url), None)


def _parse_single_byte_range(raw_header):
    """Retorna (inicio, fim|None), None sem Range e False para Range inválido."""
    if not raw_header:
        return None
    try:
        value = _safe_text(raw_header).strip().lower()
    except Exception:
        return False
    match = re.match(r'^bytes=(\d+)-(\d*)$', value)
    if not match:
        return False
    start = int(match.group(1))
    end = int(match.group(2)) if match.group(2) else None
    if end is not None and end < start:
        return False
    return (start, end)


def _parse_content_range(raw_header):
    try:
        value = _safe_text(raw_header).strip().lower()
    except Exception:
        return None
    match = re.match(r'^bytes\s+(\d+)-(\d+)/(\d+)$', value)
    if not match:
        return None
    start, end, total = [int(part) for part in match.groups()]
    if total <= 0 or start < 0 or end < start or end >= total:
        return None
    return (start, end, total)


def _vod_response_is_seek_safe(response, requested_range):
    """Valida Range real antes de entregar um arquivo VOD direto ao Kodi.

    O Kodi pode pedir posições altas ao abrir MKV/WebM. Servidores que
    ignoram Range retornam 200/EOF e deixam o CFileCache preso no Stop.
    Aqui a fonte precisa provar 206 + Content-Range coerente; sem isso ela
    falha de forma limpa, sem colocar a UI do Kodi em risco.
    """
    try:
        if int(getattr(response, 'status_code', 0) or 0) != 206:
            return False, 'status_%s' % _safe_text(getattr(response, 'status_code', ''))
        parsed = _parse_content_range(response.headers.get('content-range') or '')
        if not parsed:
            return False, 'content_range_invalido'
        start, end, total = parsed
        expected_start, expected_end = requested_range
        if start != expected_start:
            return False, 'range_inicio_incoerente'
        if expected_end is not None and end > expected_end:
            return False, 'range_fim_incoerente'
        raw_length = (response.headers.get('content-length') or '').strip()
        # Algumas CDNs enviam 206 chunked sem Content-Length, mas com
        # Content-Range preciso. O intervalo já informa o tamanho exato;
        # aceitá-lo mantém compatibilidade sem relaxar a validação do seek.
        if raw_length:
            if not raw_length.isdigit():
                return False, 'content_length_invalido'
            length = int(raw_length)
            if length <= 0 or length != (end - start + 1):
                return False, 'content_length_incoerente'
        else:
            length = end - start + 1
        return True, {'start': start, 'end': end, 'total': total, 'length': length}
    except Exception as exc:
        return False, 'range_excecao_%s' % exc.__class__.__name__


def _vod_content_type_is_safe(response):
    try:
        content_type = (response.headers.get('content-type') or '').lower()
    except Exception:
        content_type = ''
    if 'text/html' in content_type or 'json' in content_type or 'xml' in content_type:
        return False, content_type
    return True, content_type or 'application/octet-stream'


def _vod_timeout_tuple():
    policy = _policy()
    connect = max(2.0, min(6.0, float(policy.get('vod_connect_timeout', 5.0) or 5.0)))
    read = max(3.0, min(8.0, float(policy.get('vod_read_timeout', 5.0) or 5.0)))
    return (connect, read)


def build_vod_playback_url(url):
    """Retorna a rota loopback apropriada para VOD HTTP/HTTPS.

    M3U8 continua no caminho HLS já consolidado. Arquivos diretos passam por
    /vod para validar seek/Range e permitir cancelamento previsível ao Stop.
    """
    if not _is_valid_upstream_url(url):
        return ''
    try:
        parsed = urllib.parse.urlsplit(url)
        path = (parsed.path or '').lower()
        query = (parsed.query or '').lower()
        endpoint = 'hlsretry' if ('.m3u8' in path or 'm3u8' in query) else 'vod'
        return 'http://127.0.0.1:%d/%s?url=%s' % (
            PORT,
            endpoint,
            urllib.parse.quote(url, safe='')
        )
    except Exception:
        return ''


class _ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    # Threads HTTP nunca podem segurar o encerramento do Kodi. Requests de
    # streaming podem estar bloqueadas na origem quando o usuário fecha o app.
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def handle(self):
        try:
            BaseHTTPRequestHandler.handle(self)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    def finish(self):
        try:
            BaseHTTPRequestHandler.finish(self)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    def log_message(self, format, *args):
        return

    def _send_json(self, payload, status=200):
        data = payload.encode('utf-8') if isinstance(payload, str) else payload
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'close')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def _send_text(self, payload, status=200, content_type='text/plain; charset=utf-8'):
        data = payload.encode('utf-8') if isinstance(payload, str) else payload
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Connection', 'close')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def do_GET(self):
        self._dispatch()

    def do_HEAD(self):
        self._dispatch(head_only=True)

    def _dispatch(self, head_only=False):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == '/':
            self._send_json('{"message": "Mega Portugal Proxy ativo"}')
            return
        if path == '/hlsretry':
            self._handle_hlsretry(parsed, head_only=head_only)
            return
        if path == '/tsdownloader':
            self._handle_tsdownloader(parsed, head_only=head_only)
            return
        if path == '/vod':
            self._handle_vod(parsed, head_only=head_only)
            return
        self._send_text('Not Found', status=404)

    def _filtered_request_headers(self):
        upstream = {}
        for k, v in self.headers.items():
            lk = k.lower()
            if lk in ('host', 'content-length', 'connection'):
                continue
            upstream[k] = v
        return upstream

    def _handle_vod(self, parsed, head_only=False):
        """Relay seguro para arquivos VOD HTTP diretos.

        Não é um retry agressivo: para MKV/MP4 remotos o requisito é faixa
        byte-a-byte válida. Caso a origem não suporte seek corretamente, o
        proxy encerra com erro controlado em vez de permitir que CFileCache do
        Kodi entre em loop e congele a interface no Stop.
        """
        params = urllib.parse.parse_qs(parsed.query)
        url = params.get('url', [None])[0]
        if not url:
            self._send_json('{"detail":"No URL provided"}', status=400)
            return
        if not _is_valid_upstream_url(url):
            self._send_json('{"detail":"Invalid upstream URL"}', status=400)
            return
        if _vod_source_is_blocked(url):
            self._send_json('{"detail":"Fonte VOD bloqueada nesta sessão por Range inseguro"}', status=502)
            return

        client_range = _parse_single_byte_range(self.headers.get('Range'))
        if client_range is False:
            self._send_json('{"detail":"Range não suportado"}', status=416)
            return
        # Kodi pode abrir sem Range. Forçar bytes=0- mantém tamanho e seek
        # explícitos; a origem precisa responder 206 coerente.
        requested_range = client_range if client_range is not None else (0, None)
        start, end = requested_range
        upstream_range = 'bytes=%d-%s' % (start, '' if end is None else str(end))
        session = requests.Session()
        response = None
        _register_active_session(session)
        try:
            headers = self._filtered_request_headers()
            for key in ('Range', 'If-Range', 'If-None-Match', 'If-Modified-Since',
                        'Accept-Encoding', 'Connection', 'Host', 'Content-Length'):
                headers.pop(key, None)
            headers['Range'] = upstream_range
            headers['Accept'] = '*/*'
            headers['Accept-Encoding'] = 'identity'
            headers['Connection'] = 'close'
            headers['User-Agent'] = _pick_user_agent(_host_from_url(url), 0, prefer_stream=False, source_url=url)
            response = session.get(
                url,
                headers=headers,
                allow_redirects=True,
                stream=True,
                timeout=_vod_timeout_tuple(),
            )
            _register_active_response(response)
            effective_url = response.url or url
            if not _is_valid_upstream_url(effective_url):
                _mark_vod_source_unsafe(url, 'redirect_invalido')
                self._send_json('{"detail":"Redirect VOD inválido"}', status=502)
                return
            safe_content, content_type = _vod_content_type_is_safe(response)
            valid_range, range_info = _vod_response_is_seek_safe(response, requested_range)
            if not safe_content or not valid_range:
                _mark_vod_source_unsafe(url, content_type if not safe_content else range_info)
                self._send_json('{"detail":"Fonte VOD sem Range seguro para reprodução"}', status=502)
                return

            _clear_vod_source_guard(url)
            _mark_origin_ok(effective_url)
            _mark_host_ok(_host_from_url(effective_url), final_url=effective_url,
                          user_agent=headers.get('User-Agent'), content_type=content_type,
                          source_url=url)
            self.send_response(206)
            self.send_header('Content-Type', content_type)
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (
                range_info['start'], range_info['end'], range_info['total']
            ))
            self.send_header('Content-Length', str(range_info['length']))
            self.send_header('Connection', 'close')
            self.end_headers()
            if head_only:
                return

            sent = 0
            client_closed = False
            reader_stop = threading.Event()
            reader_events = queue.Queue(maxsize=8)

            def _reader():
                try:
                    for chunk in response.iter_content(chunk_size=16384):
                        if reader_stop.is_set() or _PROXY_STOP_EVENT.is_set():
                            return
                        if chunk:
                            while not reader_stop.is_set() and not _PROXY_STOP_EVENT.is_set():
                                try:
                                    reader_events.put(('chunk', chunk), timeout=0.15)
                                    break
                                except queue.Full:
                                    continue
                except (ReadTimeout, ChunkedEncodingError, ConnectionError) as exc:
                    try:
                        reader_events.put(('error', exc), timeout=0.15)
                    except Exception:
                        pass
                except Exception as exc:
                    try:
                        reader_events.put(('error', exc), timeout=0.15)
                    except Exception:
                        pass
                finally:
                    try:
                        reader_events.put(('done', None), timeout=0.15)
                    except Exception:
                        pass

            def _local_client_closed():
                # Detecta FIN do cliente loopback sem tocar no buffer HTTP. Isso
                # permite cancelar uma origem VOD bloqueada mesmo quando ela não
                # envia bytes e iter_content ainda está aguardando o timeout.
                try:
                    readable, _writable, _errors = select.select([self.connection], [], [], 0)
                    if not readable:
                        return False
                    flags = getattr(socket, 'MSG_PEEK', 0)
                    data = self.connection.recv(1, flags)
                    return data == b''
                except (BlockingIOError, InterruptedError):
                    return False
                except Exception:
                    return False

            reader_thread = threading.Thread(target=_reader, name='MegaPortugalVodRelay', daemon=True)
            reader_thread.start()
            finished = False
            try:
                while not finished:
                    if _PROXY_STOP_EVENT.is_set():
                        return
                    if _local_client_closed():
                        client_closed = True
                        return
                    try:
                        kind, payload = reader_events.get(timeout=0.12)
                    except queue.Empty:
                        continue
                    if kind == 'chunk':
                        try:
                            self.wfile.write(payload)
                            sent += len(payload)
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            client_closed = True
                            return
                    elif kind == 'error':
                        _mark_origin_fail(effective_url, exc_name=payload.__class__.__name__)
                        _mark_vod_source_unsafe(url, payload.__class__.__name__)
                        _throttled_log('vod-read:%s' % _vod_guard_key(url), 20, logging.INFO,
                                       '[VOD Seguro] Origem parou durante leitura; conexão local encerrada sem travar o Kodi.')
                        return
                    elif kind == 'done':
                        finished = True
            finally:
                reader_stop.set()
                if (not client_closed and not _PROXY_STOP_EVENT.is_set() and
                        sent != range_info['length']):
                    _mark_vod_source_unsafe(url, 'eof_prematuro')
                    _throttled_log('vod-eof:%s' % _vod_guard_key(url), 20, logging.INFO,
                                   '[VOD Seguro] Origem encerrou antes do tamanho declarado; fonte isolada nesta sessão.')
        except RequestException as exc:
            _mark_origin_fail(url, exc_name=exc.__class__.__name__)
            _mark_vod_source_unsafe(url, exc.__class__.__name__)
            self._send_json('{"detail":"Falha controlada ao abrir fonte VOD"}', status=502)
        except Exception as exc:
            _mark_vod_source_unsafe(url, exc.__class__.__name__)
            _throttled_log('vod-unexpected:%s' % _vod_guard_key(url), 20, logging.WARNING,
                           '[VOD Seguro] Falha inesperada em fonte direta; encerrada de forma protegida.')
            try:
                self._send_json('{"detail":"Falha controlada na fonte VOD"}', status=502)
            except Exception:
                pass
        finally:
            if response is not None:
                _unregister_active_response(response)
                _close_response_transport(response)
            _unregister_active_session(session)
            try:
                session.close()
            except Exception:
                pass

    def _handle_hlsretry(self, parsed, head_only=False):
        params = urllib.parse.parse_qs(parsed.query)
        # parse_qs já devolve o parâmetro decodificado uma vez. Decodificar de
        # novo quebra tokens assinados que contêm %2F, %3D ou %25.
        url = params.get('url', [None])[0]
        client_ip = _get_client_ip(self.headers, self.client_address)
        if not url:
            self._send_json('{"detail": "No URL provided"}', status=400)
            return
        if not _is_valid_upstream_url(url):
            self._send_json('{"detail": "Invalid upstream URL"}', status=400)
            return
        if _source_rejection_active(url):
            _throttled_log('hls-rejection-cooldown:%s' % _source_rejection_key(url), 20, logging.INFO,
                           '[HLS Proxy] Origem recusou este canal repetidamente; pausa curta aplicada.')
            self._send_json('{"detail": "Origem recusou temporariamente este canal"}', status=502)
            return

        lower_url = url.lower()
        is_segment = _is_probably_live_segment(url) or '.m4s' in lower_url or '.mp4' in lower_url
        session = requests.Session()
        _register_active_session(session)
        original_headers = self._filtered_request_headers()
        retry_key = 'hls_segment_retries' if is_segment else 'hls_playlist_retries'
        max_retries = max(1, int(_policy().get(retry_key, 3) or 3))
        attempts = 0
        tried_without_range = False
        media_type = 'video/mp4' if '.mp4' in lower_url else 'video/mp2t' if ('.ts' in lower_url or '/hl' in lower_url) else 'application/octet-stream'
        response_headers = {}
        last_status = None

        try:
            while attempts < max_retries and not _PROXY_STOP_EVENT.is_set():
                response = None
                try:
                    headers = _build_upstream_headers(original_headers, reconnects=attempts, response_code=last_status, prefer_stream=is_segment, url=url)
                    range_header = headers.get('Range')
                    # Manifesto nunca precisa de Range. Alguns painéis Xtream
                    # devolvem resposta truncada ou TS direto quando recebem
                    # Range em /live/...m3u8, gerando falso 502 no primeiro play.
                    if '.m3u8' in lower_url:
                        headers.pop('Range', None)
                        headers.pop('If-Range', None)
                        range_header = None
                    elif ('.mp4' in lower_url or '.m4s' in lower_url) and range_header and tried_without_range:
                        headers.pop('Range', None)

                    host = _host_from_url(url)
                    if not _host_is_available(host):
                        _host_backoff(host)
                    headers['User-Agent'] = _pick_user_agent(host, attempts, prefer_stream=is_segment, source_url=url)
                    if _PROXY_STOP_EVENT.is_set():
                        return
                    response = session.get(
                        url,
                        headers=headers,
                        allow_redirects=True,
                        stream=True,
                        timeout=_timeout_tuple(url, prefer_stream=is_segment, segment=is_segment),
                    )
                    _register_active_response(response)

                    expected_playlist = '.m3u8' in lower_url
                    ok_response, content_type = _validate_upstream_response(
                        response,
                        expect_playlist=expected_playlist,
                        expect_stream=is_segment,
                        url=url,
                    )
                    if ok_response:
                        effective_url = response.url or url
                        effective_lower = effective_url.lower()
                        is_playlist_hint = ('mpegurl' in (content_type or '') or 'x-directory/normal' in (content_type or '') or '.m3u8' in effective_lower)

                        # Kodi faz Stat/HEAD antes do play. Não consuma nem
                        # valide o corpo neste estágio: vários provedores
                        # Xtream contam isso como uma conexão extra e alguns
                        # não devolvem manifesto completo em HEAD.
                        if head_only:
                            media_type = ('application/vnd.apple.mpegurl' if is_playlist_hint else
                                          ('video/mp4' if '.mp4' in effective_lower else
                                           'video/mp2t' if ('.ts' in effective_lower or '/hl' in effective_lower) else
                                           (content_type or 'application/octet-stream')))
                            self.send_response(206 if response.status_code == 206 else 200)
                            self.send_header('Content-Type', media_type)
                            self.send_header('Connection', 'close')
                            self.end_headers()
                            return

                        if is_playlist_hint:
                            body_iterator = response.iter_content(chunk_size=4096)
                            try:
                                first_chunk = next(body_iterator)
                            except StopIteration:
                                first_chunk = b''
                            preview_text = _decode_manifest_bytes(first_chunk)

                            if _looks_like_hls_manifest(preview_text):
                                payload = _read_manifest_body(body_iterator, first_chunk)
                                playlist_content = _decode_manifest_bytes(payload) if payload is not None else ''
                                if _looks_like_hls_manifest(playlist_content):
                                    _mark_origin_ok(effective_url)
                                    _clear_source_rejection(url)
                                    _mark_host_ok(_host_from_url(effective_url), final_url=effective_url, user_agent=headers.get('User-Agent'), content_type=content_type, source_url=url)
                                    _remember_source_profile(url, _classify_source(effective_url))
                                    rewritten = _rewrite_m3u8_urls(playlist_content, effective_url)
                                    self._send_text(rewritten, content_type='application/vnd.apple.mpegurl')
                                    return

                            # Compatibilidade conservadora: alguns Xtream
                            # entregam MPEG-TS/fMP4 direto em rota .m3u8. Só
                            # passa se o payload tiver assinatura binária de
                            # mídia; HTML/JSON/texto de erro continuam 502.
                            direct_kind = _direct_media_kind(first_chunk, content_type)
                            if direct_kind:
                                _mark_origin_ok(effective_url)
                                _clear_source_rejection(url)
                                _mark_host_ok(_host_from_url(effective_url), final_url=effective_url, user_agent=headers.get('User-Agent'), content_type=content_type, source_url=url)
                                _remember_source_profile(url, _classify_source(effective_url))
                                media_type = ('video/mp2t' if direct_kind == 'ts' else
                                              'video/mp4' if direct_kind == 'mp4' else
                                              (content_type or 'application/octet-stream'))
                                _throttled_log('hls-direct-media:%s' % _origin_key(effective_url), 30, logging.INFO,
                                               '[HLS Proxy] Fonte .m3u8 retornou mídia direta; compatibilidade Xtream aplicada.')
                                self.send_response(206 if response.status_code == 206 else 200)
                                self.send_header('Content-Type', media_type)
                                content_length = _safe_upstream_content_length(response)
                                if content_length:
                                    self.send_header('Content-Length', content_length)
                                self.send_header('Connection', 'close')
                                self.end_headers()
                                for chunk in _stream_response(response, client_ip, effective_url, session,
                                                              initial_chunks=[first_chunk], iterator=body_iterator,
                                                              stream_kind=direct_kind):
                                    if _PROXY_STOP_EVENT.is_set():
                                        return
                                    try:
                                        self.wfile.write(chunk)
                                    except Exception:
                                        return
                                return

                            last_status = 'invalid_playlist'
                            _mark_origin_fail(effective_url, code='invalid_playlist')
                            attempts += 1
                            level = logging.INFO if _xtream_tolerance_active(url) else logging.WARNING
                            _throttled_log('hls-invalid-playlist:%s' % _origin_key(effective_url), 30, level,
                                           '[HLS Proxy] A origem respondeu conteúdo que não é playlist HLS válida.')
                            time.sleep(_smart_retry_delay('connection', attempts))
                            continue

                        _mark_origin_ok(effective_url)
                        _clear_source_rejection(url)
                        _mark_host_ok(_host_from_url(effective_url), final_url=effective_url, user_agent=headers.get('User-Agent'), content_type=content_type, source_url=url)
                        _remember_source_profile(url, _classify_source(effective_url))
                        media_type = ('video/mp4' if '.mp4' in effective_lower else
                                      'video/mp2t' if ('.ts' in effective_lower or '/hl' in effective_lower) else
                                      (content_type or 'application/octet-stream'))
                        response_headers = {k: v for k, v in response.headers.items() if k.lower() in ['content-type', 'accept-ranges', 'content-range']}
                        status = 206 if response.status_code == 206 else 200
                        self.send_response(status)
                        self.send_header('Content-Type', media_type)
                        for key, value in response_headers.items():
                            if key.lower() != 'content-type':
                                self.send_header(key, value)
                        content_length = _safe_upstream_content_length(response)
                        if content_length:
                            self.send_header('Content-Length', content_length)
                        self.send_header('Connection', 'close')
                        self.end_headers()
                        for chunk in _stream_response(response, client_ip, effective_url, session):
                            if _PROXY_STOP_EVENT.is_set():
                                return
                            try:
                                self.wfile.write(chunk)
                            except Exception:
                                return
                        return

                    status_code = getattr(response, 'status_code', 502)
                    if status_code == 416 and range_header and not tried_without_range:
                        tried_without_range = True
                        last_status = 416
                        continue
                    last_status = status_code
                    _mark_origin_fail(getattr(response, 'url', None) or url, code=status_code)
                    attempts += 1
                    level = logging.INFO if _source_profile(url) == 'xtream_bad' else logging.WARNING
                    if status_code == 406:
                        _burst_log('hls-406:%s' % _origin_key(url), 60, logging.INFO, '[HLS Proxy] Origem respondeu 406; fallback discreto acionado (%sx).')
                    else:
                        _throttled_log('hls-status:%s:%s' % (_origin_key(url), status_code), 20, level, '[HLS Proxy] Resposta HTTP %s da origem.' % status_code)
                    if status_code in (401, 403) and _mark_source_rejection(url, status_code):
                        _throttled_log('hls-rejection-open:%s' % _source_rejection_key(url), 20, logging.INFO,
                                       '[HLS Proxy] 401/403 repetido no mesmo canal; encerrando tentativas desta origem por alguns segundos.')
                        break
                    time.sleep(_smart_retry_delay('406' if status_code == 406 else 'connection', attempts))
                except RequestException as exc:
                    last_status = exc.__class__.__name__
                    _mark_origin_fail(url, exc_name=exc.__class__.__name__)
                    _mark_host_fail(_host_from_url(url), err=exc.__class__.__name__, soft=(exc.__class__.__name__ in ('ReadTimeout', 'ConnectionError', 'ChunkedEncodingError')))
                    attempts += 1
                    level = logging.INFO if _source_profile(url) == 'xtream_bad' else logging.WARNING
                    _throttled_log('hls-exc:%s:%s' % (_origin_key(url), exc.__class__.__name__), 15, level, '[HLS Proxy] Origem instável; retry inteligente em curso (%s).' % _safe_text(exc.__class__.__name__))
                    time.sleep(_smart_retry_delay('timeout' if exc.__class__.__name__ in ('ReadTimeout', 'ChunkedEncodingError') else 'connection', attempts))
                except Exception as exc:
                    last_status = exc.__class__.__name__
                    _mark_origin_fail(url, exc_name=exc.__class__.__name__)
                    attempts += 1
                    _throttled_log('hls-unexpected:%s' % _origin_key(url), 20, logging.WARNING, '[HLS Proxy] Falha inesperada ao buscar a origem; tentativa controlada em curso.')
                    time.sleep(_smart_retry_delay('connection', attempts))
                finally:
                    if response is not None:
                        _unregister_active_response(response)
                        try:
                            response.close()
                        except Exception:
                            pass

            # Só usa pequeno cache local após esgotar as tentativas; antes a
            # versão antiga devolvia cache na primeira falha e anulava retries.
            if is_segment:
                cached = list(_stream_cache(client_ip, url) or [])
                if cached:
                    self.send_response(200)
                    self.send_header('Content-Type', media_type)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    if not head_only:
                        for chunk in cached:
                            try:
                                self.wfile.write(chunk)
                            except Exception:
                                return
                    return
            self._send_json('{"detail": "Falha ao conectar após tentativas controladas"}', status=502)
        finally:
            _unregister_active_session(session)
            try:
                session.close()
            except Exception:
                pass

    def _handle_tsdownloader(self, parsed, head_only=False):
        params = urllib.parse.parse_qs(parsed.query)
        # parse_qs já executa a decodificação correta do parâmetro URL.
        url = params.get('url', [None])[0]
        if not url:
            self._send_json("{\"error\": \"Missing url parameter\"}", status=400)
            return
        if not _is_valid_upstream_url(url):
            self._send_json("{\"error\": \"Invalid upstream URL\"}", status=400)
            return
        if _source_rejection_active(url):
            _throttled_log('ts-rejection-cooldown:%s' % _source_rejection_key(url), 20, logging.INFO,
                           '[TS Downloader] Origem recusou este canal repetidamente; pausa curta aplicada.')
            self._send_json("{\"error\": \"Origem recusou temporariamente este canal\"}", status=502)
            return

        original_headers = self._filtered_request_headers()
        stop_ts = [False]
        session = requests.Session()
        _register_active_session(session)
        resolved_url = ['']
        last_status = [None]

        def _resolve_stream_url(force=False):
            """Escolhe a rota sem fazer uma requisição de sonda duplicada.

            A versão anterior fazia GET de probe e, em seguida, outro GET para
            transmitir. Em uma origem 401/403 isso dobrava o número de acessos
            e alongava a troca de canal. O request de streaming já segue
            redirects e devolve response.url; após um sucesso esse endereço é
            reaproveitado somente para a conexão atual.
            """
            if resolved_url[0] and not force:
                return resolved_url[0]
            if not force:
                cached = _source_cache_get('last_working_url', url)
                if cached and _is_valid_upstream_url(cached):
                    resolved_url[0] = cached
                    return resolved_url[0]
            return url

        def _retry_allowed(reconnects, retry_started_at):
            max_retries = max(1, int(_policy().get('ts_max_retries', 12) or 12))
            budget = max(5.0, float(_policy().get('ts_retry_budget', 45.0) or 45.0))
            if reconnects > max_retries:
                return False
            if retry_started_at and (time.monotonic() - retry_started_at) > budget:
                return False
            return True

        def generate_ts():
            reconnects = 0
            retry_started_at = 0.0
            last_good_at = 0.0
            last_chunk_at = 0.0
            keepalive_hits = 0
            startup_keepalive_until = time.time() + float(_policy().get('ts_startup_keepalive_window', 5.0) or 5.0)

            def _continue_after_failure(kind, target_url):
                nonlocal reconnects, retry_started_at, last_chunk_at, keepalive_hits
                reconnects += 1
                if not retry_started_at:
                    retry_started_at = time.monotonic()
                if not _retry_allowed(reconnects, retry_started_at):
                    _throttled_log('ts-retry-exhausted:%s' % _origin_key(target_url), 30, logging.WARNING, '[TS Downloader] Origem não recuperou no limite seguro; encerrando a conexão local para o Kodi tentar novamente.')
                    return False, None
                filler = _ts_keepalive_once(last_good_at, reconnects, startup_keepalive_until=startup_keepalive_until)
                if filler is not None:
                    keepalive_hits += 1
                    if keepalive_hits in (1, 5, 15):
                        message = '[TS Downloader] Mantendo stream vivo durante microqueda da origem.' if last_good_at else '[TS Downloader] Segurando conexão local enquanto a origem acorda.'
                        _throttled_log('ts-keepalive:%s' % _origin_key(target_url), 20, logging.INFO, message)
                    last_chunk_at = time.time()
                return True, filler

            while not stop_ts[0] and not _PROXY_STOP_EVENT.is_set():
                response = None
                target_url = resolved_url[0] or url
                try:
                    target_url = _resolve_stream_url(force=(reconnects > 0 and reconnects % 4 == 0))
                    _apply_origin_cooldown(target_url)
                    request_headers = _build_upstream_headers(original_headers, reconnects=reconnects, response_code=last_status[0], prefer_stream=True, url=target_url)
                    host = _host_from_url(target_url)
                    if not _host_is_available(host):
                        _host_backoff(host)
                    request_headers['User-Agent'] = _pick_user_agent(host, reconnects, prefer_stream=True, source_url=url)
                    timeout_tuple = _timeout_tuple(target_url, prefer_stream=True)
                    if not last_good_at:
                        timeout_tuple = (timeout_tuple[0], min(float(timeout_tuple[1]), float(_policy().get('ts_startup_read_timeout', 5.0) or 5.0)))
                    if _PROXY_STOP_EVENT.is_set():
                        return
                    response = session.get(target_url, headers=request_headers, stream=True, timeout=timeout_tuple, allow_redirects=True)
                    _register_active_response(response)
                    status_code = response.status_code
                    last_status[0] = status_code

                    ok_response, content_type = _validate_upstream_response(response, expect_stream=True, url=target_url)
                    if ok_response:
                        effective_url = response.url or target_url
                        resolved_url[0] = effective_url
                        _mark_origin_ok(effective_url)
                        _clear_source_rejection(url)
                        _mark_host_ok(_host_from_url(effective_url), final_url=effective_url, user_agent=request_headers.get('User-Agent'), content_type=content_type, source_url=url)
                        _remember_source_profile(url, _classify_source(effective_url))
                        reconnects = 0
                        retry_started_at = 0.0
                        keepalive_hits = 0
                        received_data = False
                        for chunk in response.iter_content(chunk_size=4096):
                            if stop_ts[0] or _PROXY_STOP_EVENT.is_set():
                                _throttled_log('ts-stop-client', 60, logging.DEBUG, '[TS Downloader] Stream interrompido pelo cliente.')
                                return
                            if not chunk:
                                continue
                            received_data = True
                            now = time.time()
                            last_good_at = now
                            last_chunk_at = now
                            startup_keepalive_until = 0
                            yield chunk
                        if stop_ts[0]:
                            return
                        # A origem fechou o stream limpo; para Live TV isso é
                        # uma queda e recebe o mesmo retry limitado das demais.
                        _mark_origin_fail(effective_url, exc_name='StreamEnded')
                        allowed, filler = _continue_after_failure('eof', effective_url)
                        if not allowed:
                            return
                        if filler is not None:
                            yield filler
                            time.sleep(_ts_keepalive_sleep(reconnects))
                        else:
                            time.sleep(_smart_retry_delay('connection', reconnects))
                        continue

                    _mark_origin_fail(response.url or target_url, code=status_code)
                    _mark_host_fail(_host_from_url(response.url or target_url), err=status_code, soft=(status_code == 406))
                    if status_code == 406:
                        _throttled_log('ts-406:%s' % _origin_key(target_url), 30, logging.INFO, '[TS Downloader] 406 tratado como ruído transitório; retry controlado.')
                        delay_kind = '406'
                    elif status_code in (401, 403, 404, 410):
                        _throttled_log('ts-auth-or-missing:%s:%s' % (_origin_key(target_url), status_code), 20, logging.INFO if _source_profile(target_url) == 'xtream_bad' else logging.WARNING, '[TS Downloader] Origem recusou ou expirou a rota; renovando somente este canal.')
                        _forget_source_memory(url)
                        resolved_url[0] = ''
                        if status_code in (401, 403) and _mark_source_rejection(url, status_code):
                            _throttled_log('ts-rejection-open:%s' % _source_rejection_key(url), 20, logging.INFO,
                                           '[TS Downloader] 401/403 repetido no mesmo canal; encerrando tentativas desta origem por alguns segundos.')
                            return
                        delay_kind = 'connection'
                    else:
                        _throttled_log('ts-status:%s:%s' % (_origin_key(target_url), status_code), 20, logging.INFO if _source_profile(target_url) == 'xtream_bad' else logging.WARNING, '[TS Downloader] Resposta HTTP %s da origem.' % status_code)
                        if reconnects >= 3:
                            resolved_url[0] = ''
                        delay_kind = 'connection'
                    allowed, filler = _continue_after_failure(delay_kind, target_url)
                    if not allowed:
                        return
                    if filler is not None:
                        yield filler
                        time.sleep(_ts_keepalive_sleep(reconnects))
                    else:
                        time.sleep(_smart_retry_delay(delay_kind, reconnects))
                except (ReadTimeout, ChunkedEncodingError, ConnectionError) as exc:
                    _mark_origin_fail(resolved_url[0] or url, exc_name=exc.__class__.__name__)
                    _mark_host_fail(_host_from_url(resolved_url[0] or url), err=exc.__class__.__name__, soft=True)
                    resolved_url[0] = ''
                    if reconnects <= 1:
                        _throttled_log('ts-reconnect-net-soft:%s' % _origin_key(url), 12, logging.INFO, '[TS Downloader] Origem instável; reconectando sem drama (%s).' % _safe_text(exc.__class__.__name__))
                    else:
                        _throttled_log('ts-reconnect-net-hard:%s' % _origin_key(url), 20, logging.INFO if _source_profile(url) == 'xtream_bad' else logging.WARNING, '[TS Downloader] Instabilidade persistente da origem; retry progressivo (%s).' % _safe_text(exc.__class__.__name__))
                    allowed, filler = _continue_after_failure('timeout', url)
                    if not allowed:
                        return
                    if filler is not None:
                        yield filler
                        time.sleep(_ts_keepalive_sleep(reconnects))
                    else:
                        time.sleep(_smart_retry_delay('timeout', reconnects))
                except GeneratorExit:
                    stop_ts[0] = True
                    return
                except Exception as exc:
                    message = str(exc).lower()
                    if 'broken pipe' in message or 'connection reset' in message:
                        stop_ts[0] = True
                        _throttled_log('ts-client-disconnect-exc', 60, logging.DEBUG, '[TS Downloader] Cliente desconectou durante o stream.')
                        return
                    _mark_origin_fail(resolved_url[0] or url, exc_name=exc.__class__.__name__)
                    _mark_host_fail(_host_from_url(resolved_url[0] or url), err=exc.__class__.__name__, soft=True)
                    resolved_url[0] = ''
                    _throttled_log('ts-stream-error:%s' % _origin_key(url), 15, logging.INFO if _source_profile(url) == 'xtream_bad' else logging.WARNING, '[TS Downloader] Erro no stream; reconectando com mínimo de interferência: %s' % _safe_text(exc.__class__.__name__))
                    allowed, filler = _continue_after_failure('connection', url)
                    if not allowed:
                        return
                    if filler is not None:
                        yield filler
                        time.sleep(_ts_keepalive_sleep(reconnects))
                    else:
                        time.sleep(_smart_retry_delay('connection', reconnects))
                finally:
                    if response is not None:
                        _unregister_active_response(response)
                        try:
                            response.close()
                        except Exception:
                            pass
                    if last_chunk_at and (time.time() - last_chunk_at) > max(2.0, float(_policy().get('ts_keepalive_window', 8.0) or 8.0) + 1.0):
                        last_good_at = 0.0
                        keepalive_hits = 0
            _throttled_log('ts-stream-closed', 60, logging.DEBUG, '[TS Downloader] Stream encerrado pelo cliente.')

        self.send_response(200)
        self.send_header('Content-Type', 'video/mp2t')
        self.send_header('Connection', 'close')
        self.end_headers()
        if head_only:
            _unregister_active_session(session)
            try:
                session.close()
            except Exception:
                pass
            return

        stream_started_at = time.time()
        bytes_sent_to_client = [0]
        disconnect_note = {'kind': None}

        def _is_startup_handoff():
            window = float(_policy().get('ts_startup_handoff_window', 5.0) or 5.0)
            byte_cap = int(_policy().get('ts_startup_handoff_bytes', 393216) or 393216)
            return ((time.time() - stream_started_at) <= window and bytes_sent_to_client[0] <= byte_cap)

        def _note_client_disconnect(where='close'):
            if disconnect_note['kind'] is not None:
                return
            if _is_startup_handoff():
                disconnect_note['kind'] = 'startup-handoff'
                _throttled_log('ts-startup-handoff', 30, logging.INFO, '[TS Downloader] Handoff local de startup detectado; o Kodi trocou a conexão inicial pela definitiva (normal).')
            else:
                disconnect_note['kind'] = 'client-close'
                _throttled_log('ts-client-close', 60, logging.DEBUG if where == 'close' else logging.INFO, '[TS Downloader] Cliente local encerrou a conexão do TS.')

        try:
            for chunk in generate_ts():
                try:
                    self.wfile.write(chunk)
                    bytes_sent_to_client[0] += len(chunk)
                except Exception:
                    stop_ts[0] = True
                    _note_client_disconnect(where='write')
                    return
        finally:
            stop_ts[0] = True
            _unregister_active_session(session)
            try:
                session.close()
            except Exception:
                pass
            _note_client_disconnect(where='close')


def _register_active_session(session):
    if session is None:
        return
    with _ACTIVE_NETWORK_LOCK:
        _ACTIVE_SESSIONS.add(session)


def _unregister_active_session(session):
    if session is None:
        return
    with _ACTIVE_NETWORK_LOCK:
        _ACTIVE_SESSIONS.discard(session)


def _register_active_response(response):
    if response is None:
        return
    with _ACTIVE_NETWORK_LOCK:
        _ACTIVE_RESPONSES.add(response)


def _unregister_active_response(response):
    if response is None:
        return
    with _ACTIVE_NETWORK_LOCK:
        _ACTIVE_RESPONSES.discard(response)


def _close_response_transport(response):
    """Fecha a resposta e, quando disponível, o socket subjacente.

    requests/urllib3 normalmente acorda iter_content() com response.close(),
    mas alguns builds antigos deixam recv() bloqueado até o read timeout. A
    travessia é propositalmente defensiva e limitada a atributos internos
    conhecidos; falhas são ignoradas para manter compatibilidade entre urllib3.
    """
    pending = [response]
    seen = set()
    sockets = []
    while pending:
        current = pending.pop()
        if current is None:
            continue
        marker = id(current)
        if marker in seen:
            continue
        seen.add(marker)
        if isinstance(current, socket.socket):
            sockets.append(current)
            continue
        for name in ('raw', '_fp', 'fp', '_connection', 'connection', '_sock', 'sock'):
            try:
                child = getattr(current, name, None)
            except Exception:
                child = None
            if child is not None:
                pending.append(child)
    for sock in sockets:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except Exception:
            pass
        try:
            sock.close()
        except Exception:
            pass
    try:
        response.close()
    except Exception:
        pass


def _close_active_network_objects():
    """Fecha requests em voo antes de encerrar o servidor local.

    Sem isso uma origem que parou de enviar bytes pode manter uma thread em
    iter_content até o timeout de leitura, exatamente no momento de saída do
    Kodi. O socket subjacente também recebe shutdown best-effort para acordar
    builds urllib3 que não interrompem recv() só com Session.close().
    """
    with _ACTIVE_NETWORK_LOCK:
        responses = list(_ACTIVE_RESPONSES)
        sessions = list(_ACTIVE_SESSIONS)
    for response in responses:
        _close_response_transport(response)
    for session in sessions:
        try:
            session.close()
        except Exception:
            pass


def _port_in_use():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(0.35)
    try:
        return sock.connect_ex(('127.0.0.1', PORT)) == 0
    finally:
        try:
            sock.close()
        except Exception:
            pass


def _probe_our_proxy():
    connection = None
    try:
        connection = http.client.HTTPConnection('127.0.0.1', PORT, timeout=0.45)
        connection.request('GET', '/', headers={'Connection': 'close'})
        response = connection.getresponse()
        body = response.read(256)
        return response.status == 200 and b'Mega Portugal Proxy ativo' in body
    except Exception:
        return False
    finally:
        try:
            if connection is not None:
                connection.close()
        except Exception:
            pass


def is_proxy_running():
    # Não basta a porta responder: outro addon/processo pode estar em 8088.
    return _probe_our_proxy()


def _server_run():
    global _SERVER, _SERVER_ERROR
    server = None
    try:
        server = _ThreadedHTTPServer(('127.0.0.1', PORT), _ProxyHandler)
        _SERVER = server
        _SERVER_ERROR = ''
        _SERVER_READY.set()
        server.serve_forever(poll_interval=0.2)
    except Exception as exc:
        _SERVER_ERROR = _safe_text(exc)
        _SERVER_READY.set()
        _log('Falha ao iniciar proxy: %s' % _SERVER_ERROR, level=xbmc.LOGERROR)
    finally:
        try:
            if server is not None:
                server.server_close()
        except Exception:
            pass
        if _SERVER is server:
            _SERVER = None


def start_proxy():
    global _SERVER_THREAD, _SERVER_STARTED
    _PROXY_STOP_EVENT.clear()
    with _SERVER_LOCK:
        if is_proxy_running():
            _SERVER_STARTED = True
            return True
        if _SERVER_THREAD and _SERVER_THREAD.is_alive():
            wait_for_existing = True
        else:
            wait_for_existing = False
            if _port_in_use():
                _log('A porta local %d já está ocupada por outro serviço; o proxy Mega Portugal não será assumido.' % PORT, level=xbmc.LOGERROR)
                return False
            _SERVER_READY.clear()
            _SERVER_THREAD = threading.Thread(target=_server_run, name='MegaPortugalProxyServer', daemon=True)
            _SERVER_THREAD.start()

    # Fora do lock: duas reproduções podem aguardar o mesmo servidor com segurança.
    for _ in range(24):
        if is_proxy_running():
            _SERVER_STARTED = True
            _log('Proxy nativo ativo na porta %d' % PORT)
            return True
        if _SERVER_READY.is_set() and _SERVER_ERROR:
            break
        time.sleep(0.15)
    if wait_for_existing:
        _log('Proxy nativo não respondeu durante inicialização compartilhada.', level=xbmc.LOGERROR)
    else:
        detail = (': %s' % _SERVER_ERROR) if _SERVER_ERROR else ''
        _log('Proxy nativo não respondeu na porta %d%s' % (PORT, detail), level=xbmc.LOGERROR)
    return False


def wait_for_service_proxy(timeout=3.0):
    """Aguarda o servidor criado pelo service.py sem criar thread no plugin.

    Cada execução de default.py recebe seu próprio interpretador Python no
    Kodi. Criar o servidor dentro dela faz o invoker permanecer vivo após o
    play. O service é o único dono do ciclo de vida do servidor.
    """
    try:
        timeout = max(0.2, float(timeout))
    except Exception:
        timeout = 3.0
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_proxy_running():
            return True
        time.sleep(0.10)
    return is_proxy_running()


def stop_proxy(timeout=1.5):
    """Para somente o servidor pertencente a este interpretador.

    Não toca em uma porta 8088 de outro addon/processo. A chamada é usada no
    finally do service.py para o Kodi não precisar matar o interpretador à
    força no desligamento.
    """
    global _SERVER, _SERVER_THREAD, _SERVER_STARTED, _SERVER_ERROR
    try:
        timeout = max(0.2, float(timeout))
    except Exception:
        timeout = 1.5
    with _SERVER_LOCK:
        server = _SERVER
        thread = _SERVER_THREAD
        _SERVER_STARTED = False
    _PROXY_STOP_EVENT.set()
    _close_active_network_objects()
    if server is None:
        return True
    try:
        server.shutdown()
    except Exception:
        pass
    if thread is not None and thread is not threading.current_thread():
        try:
            thread.join(timeout)
        except Exception:
            pass
    with _SERVER_LOCK:
        if _SERVER_THREAD is thread and (thread is None or not thread.is_alive()):
            _SERVER_THREAD = None
        if _SERVER is server and (thread is None or not thread.is_alive()):
            _SERVER = None
        if _SERVER is None:
            _SERVER_ERROR = ''
            _SERVER_READY.clear()
    with _ACTIVE_NETWORK_LOCK:
        _ACTIVE_SESSIONS.clear()
        _ACTIVE_RESPONSES.clear()
    return not bool(thread and thread.is_alive())


def kodiproxy():
    # Compatibilidade para chamadas legadas. O service deve ser o dono do
    # servidor; esta função continua disponível apenas para integrações antigas.
    return start_proxy()
