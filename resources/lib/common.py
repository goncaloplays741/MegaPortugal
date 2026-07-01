# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import io
import os
import time
import requests

text_type = str

try:
    from kodi_six import xbmc, xbmcaddon, xbmcvfs
except ImportError:
    import xbmc
    import xbmcaddon
    import xbmcvfs

try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
except Exception:
    HTTPAdapter = None
    Retry = None

DEFAULT_TIMEOUT = 12
DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
    '(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36'
)


def get_addon(addon=None):
    if addon is not None:
        return addon
    try:
        return xbmcaddon.Addon()
    except Exception:
        return None


def get_profile_dir(addon=None):
    addon = get_addon(addon)
    if addon is None:
        return ''
    return xbmcvfs.translatePath(addon.getAddonInfo('profile'))


def _path_exists(path):
    if not path:
        return False
    try:
        if xbmcvfs.exists(path):
            return True
    except Exception:
        pass
    try:
        return os.path.exists(path)
    except Exception:
        return False


def ensure_dir(path):
    """Cria diretório no perfil de forma tolerante entre plataformas Kodi.

    Em Android/iOS/tvOS e builds embarcadas, o caminho traduzido pelo Kodi é a
    autoridade. Tenta primeiro a VFS do Kodi e só então o filesystem Python.
    Nunca deixa falha de diretório derrubar a navegação; o chamador decide se
    precisa seguir sem cache local.
    """
    if not path:
        return path
    if _path_exists(path):
        return path
    try:
        if xbmcvfs.mkdirs(path):
            return path
    except Exception:
        pass
    try:
        if not os.path.exists(path):
            os.makedirs(path)
    except Exception:
        pass
    return path


PROFILE_DIR = ensure_dir(get_profile_dir())
LOG_FILE = os.path.join(PROFILE_DIR, 'megaportugal_debug.log')


def safe_text(value):
    if value is None:
        return ''
    try:
        if isinstance(value, bytes):
            return value.decode('utf-8', 'replace')
    except Exception:
        pass
    try:
        if isinstance(value, text_type):
            return value
    except Exception:
        pass
    try:
        return str(value)
    except Exception:
        return repr(value)


def _to_text(value):
    return safe_text(value)


def get_setting_text(key, default='', addon=None):
    addon = get_addon(addon)
    if addon is None:
        return default
    try:
        value = addon.getSetting(key)
        return value if value not in (None, '') else default
    except Exception:
        return default


def get_setting_bool(key, default=False, addon=None):
    try:
        value = get_setting_text(key, 'true' if default else 'false', addon=addon)
        return safe_text(value).lower() == 'true'
    except Exception:
        return default


def set_setting_bool(key, value, addon=None):
    addon = get_addon(addon)
    if addon is None:
        return
    try:
        addon.setSetting(key, 'true' if value else 'false')
    except Exception:
        pass


def get_setting_int(key, default=0, addon=None):
    try:
        return int(get_setting_text(key, str(default), addon=addon))
    except Exception:
        return default


def get_setting_enum_value(key, values, default=None, addon=None):
    if not values:
        return default
    try:
        index = int(get_setting_text(key, '0', addon=addon))
        if 0 <= index < len(values):
            return values[index]
    except Exception:
        pass
    return values[0] if default is None else default


def debug_enabled(addon=None):
    return get_setting_bool('mododebug', False, addon=addon)


def log(message, level=None):
    if level is None:
        level = xbmc.LOGINFO
    text = '[Mega Portugal] {}'.format(_to_text(message))
    try:
        xbmc.log(text, level=level)
    except Exception:
        pass
    try:
        ensure_dir(PROFILE_DIR)
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        with io.open(LOG_FILE, 'a', encoding='utf-8') as fh:
            fh.write(u'{} {}\n'.format(timestamp, _to_text(message)))
    except Exception:
        pass


def log_debug(message, addon=None, component=None):
    if not debug_enabled(addon=addon):
        return
    if component:
        prefix = '[Mega Portugal DEBUG][{}] '.format(component)
    else:
        prefix = '[Mega Portugal DEBUG] '
    formatted = prefix + safe_text(message)
    try:
        xbmc.log(formatted, level=xbmc.LOGINFO)
    except Exception:
        pass
    try:
        ensure_dir(PROFILE_DIR)
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        with io.open(LOG_FILE, 'a', encoding='utf-8') as fh:
            fh.write(u'{} {}\n'.format(timestamp, formatted))
    except Exception:
        pass


def make_session(user_agent=None, retries=2):
    session = requests.Session()
    session.headers.update({'User-Agent': user_agent or DEFAULT_USER_AGENT})
    if HTTPAdapter and Retry:
        try:
            retry = Retry(
                total=retries,
                connect=retries,
                read=retries,
                backoff_factor=0.35,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=frozenset(['GET', 'HEAD'])
            )
        except TypeError:
            retry = Retry(
                total=retries,
                connect=retries,
                read=retries,
                backoff_factor=0.35,
                status_forcelist=[429, 500, 502, 503, 504],
                method_whitelist=frozenset(['GET', 'HEAD'])
            )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=8)
        session.mount('http://', adapter)
        session.mount('https://', adapter)
    return session



def sync_legacy_setting_mirrors(addon=None):
    addon = get_addon(addon)
    if addon is None:
        return
    try:
        adult_enabled = get_setting_bool('tv_adult_protect', False, addon=addon)
        guard_value = get_setting_bool('tv_adult_protect_guard_state', adult_enabled, addon=addon)
        if guard_value != adult_enabled:
            set_setting_bool('tv_adult_protect_guard_state', adult_enabled, addon=addon)
    except Exception:
        pass
