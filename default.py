# -*- coding: utf-8 -*-
from __future__ import unicode_literals  # ← precisa vir logo no início no Python 2

import sys

# Kodi 19+ usa Python 3. O addon não depende de six/kodi-six para as rotas
# internas, evitando falha de instalação em builds enxutas.
text_type = str
PY3 = True
from urllib.parse import urlparse, parse_qs, urlencode, parse_qsl, unquote_plus, quote, urljoin
import requests
import re
import os
import base64
import unicodedata
import logging
import time
import atexit
import io
import json

# inputstreamhelper é opcional. O caminho de produção usa diretamente
# inputstream.adaptive, que é a dependência declarada do addon.
try:
    import inputstreamhelper
except ImportError:
    inputstreamhelper = None

try:
    from kodi_six import xbmc, xbmcplugin, xbmcgui, xbmcaddon, xbmcvfs
except ImportError:
    import xbmc
    import xbmcplugin
    import xbmcgui
    import xbmcaddon
    import xbmcvfs

from resources.lib import proxy


class _LazyModule(object):
    """Adia módulos de catálogo/TV até a rota realmente precisar deles."""
    def __init__(self, module_name):
        self.module_name = module_name
        self.module = None

    def _resolve(self):
        if self.module is None:
            self.module = __import__(self.module_name, fromlist=['*'])
        return self.module

    def __getattr__(self, name):
        return getattr(self._resolve(), name)


tmdb = _LazyModule('resources.lib.tmdb')
m3u = _LazyModule('resources.lib.m3u')
pluto = _LazyModule('resources.lib.pluto')
subtitles = _LazyModule('resources.lib.subtitles')
from resources.lib.common import (
    make_session, DEFAULT_TIMEOUT, DEFAULT_USER_AGENT,
    get_setting_bool as common_setting_bool,
    set_setting_bool as common_set_setting_bool,
    get_setting_text as common_setting_text,
    get_setting_enum_value as common_setting_enum_value,
    log as common_log,
    log_debug as common_log_debug,
    sync_legacy_setting_mirrors,
    ensure_dir
)
PORT = proxy.PORT
URL_HLSRETRY = "http://127.0.0.1:{}/hlsretry?url=".format(PORT)
URL_TS_DOWNLOADER = "http://127.0.0.1:{}/tsdownloader?url=".format(PORT)
ADDON_HANDLE = int(sys.argv[1])
BASE_URL = sys.argv[0]
ADDON = xbmcaddon.Addon()
TRANSLATE = xbmcvfs.translatePath
# Sempre trabalhe com caminhos traduzidos pelo Kodi. Isso evita depender de
# como cada porta expõe special:// em Android, Linux embarcado, macOS e iOS.
homeDir = TRANSLATE(ADDON.getAddonInfo('path'))
addonIcon = os.path.join(homeDir, 'icon.png')
addonFanart = os.path.join(homeDir, 'fanart.jpg')
profile = ensure_dir(TRANSLATE(ADDON.getAddonInfo('profile')))

HTTP_SESSION = make_session()
sync_legacy_setting_mirrors(addon=ADDON)

MEGA_DESC = '''
[B][COLOR white]MEGA[/COLOR] [COLOR red]PORTUGAL[/COLOR][/B]
[COLOR white]TV ao vivo portuguesa com Sport TV, RTP, SIC, TVI, CNN Portugal e muito mais.[/COLOR]
    '''
_DNS_READY = False
AUTOPLAY_START_TIMEOUT_MS = 12000
AUTOPLAY_STABLE_PLAY_MS = 4000
AUTOPLAY_POLL_MS = 250
AUTOPLAY_RETRY_SETTLE_MS = 700
AUTOPLAY_PREFLIGHT_TIMEOUT = (3, 5)
AUTOPLAY_PREFLIGHT_SKIP_CODES = {401, 403, 404, 410}

# VOD progressivo vindo do catálogo TMDB é o único fluxo que pode entregar
# MKV/MP4 remoto diretamente ao CFileCache do Kodi. Algumas origens aceitam
# abrir o arquivo mas retornam Range incoerente ao demuxer; isso prende o Stop.
# A verificação abaixo é curta, em memória e exclusiva dessa rota.
VOD_RANGE_PREFLIGHT_TIMEOUT = (3, 5)
VOD_RANGE_PREFLIGHT_TTL_OK = 600.0
VOD_RANGE_PREFLIGHT_TTL_UNSAFE = 300.0
_VOD_RANGE_PREFLIGHT_CACHE = {}


def _cleanup_runtime_refs():
    global ADDON
    try:
        ADDON = None
    except Exception:
        pass

atexit.register(_cleanup_runtime_refs)

def ensure_customdns():
    """Sincroniza DNS alternativo somente quando o usuário o escolheu.

    A rotina é reversível: desligar a opção devolve o getaddrinfo nativo sem
    tocar em um resolvedor que outro componente possa ter instalado depois.
    """
    global _DNS_READY
    try:
        from resources.lib import dns as dns_helper
        _DNS_READY = bool(dns_helper.apply_configured_override(addon=ADDON))
    except Exception:
        _DNS_READY = False
    return _DNS_READY

ensure_customdns()

def _configure_python_logging():
    try:
        debug_enabled = ADDON.getSetting('mododebug').lower() == 'true'
    except Exception:
        debug_enabled = False
    # Não altera o logger raiz do processo Kodi: ele é compartilhado por
    # todos os addons e portas. Ajustamos somente bibliotecas usadas aqui.
    for logger_name in ('urllib3', 'urllib3.connectionpool', 'requests', 'requests.packages.urllib3', 'chardet'):
        try:
            logger = logging.getLogger(logger_name)
            logger.setLevel(logging.WARNING if debug_enabled else logging.ERROR)
        except Exception:
            pass


def _set_listitem_info(item, *args, **kwargs):
    info_type = 'video'
    info = {}
    if args:
        if len(args) >= 1 and args[0]:
            info_type = args[0]
        if len(args) >= 2 and isinstance(args[1], dict):
            info.update(args[1])
    info_type = kwargs.get('type', info_type) or 'video'
    extra_info = kwargs.get('infoLabels')
    if isinstance(extra_info, dict):
        info.update(extra_info)

    normalized = {}
    for key, value in info.items():
        try:
            normalized[str(key).strip().lower()] = value
        except Exception:
            normalized[key] = value

    try:
        lowered_type = str(info_type).strip().lower()
    except Exception:
        lowered_type = 'video'

    if lowered_type == 'video':
        # Não manter VideoInfoTag em cache global: no Android o objeto é nativo do
        # ListItem e pode ficar inválido quando a janela Kodi é destruída. Cada item
        # resolve seu próprio tag uma vez por chamada, sem reter ponteiros nativos.
        try:
            tag = item.getVideoInfoTag()
        except Exception:
            tag = None
        if tag:
            try:
                title = normalized.get('title')
                if title not in (None, '') and hasattr(tag, 'setTitle'):
                    tag.setTitle(str(title))
                plot = normalized.get('plot')
                if plot not in (None, '') and hasattr(tag, 'setPlot'):
                    tag.setPlot(str(plot))
                year = normalized.get('year')
                if year not in (None, '', 0) and hasattr(tag, 'setYear'):
                    try:
                        tag.setYear(int(year))
                    except Exception:
                        pass
                genre = normalized.get('genre')
                if genre not in (None, '') and hasattr(tag, 'setGenres'):
                    if isinstance(genre, (list, tuple)):
                        tag.setGenres([str(x) for x in genre if x not in (None, '')])
                    else:
                        tag.setGenres([str(genre)])
                tvshowtitle = normalized.get('tvshowtitle')
                if tvshowtitle not in (None, '') and hasattr(tag, 'setTvShowTitle'):
                    tag.setTvShowTitle(str(tvshowtitle))
                season = normalized.get('season')
                if season not in (None, '') and hasattr(tag, 'setSeason'):
                    try:
                        tag.setSeason(int(season))
                    except Exception:
                        pass
                episode = normalized.get('episode')
                if episode not in (None, '') and hasattr(tag, 'setEpisode'):
                    try:
                        tag.setEpisode(int(episode))
                    except Exception:
                        pass
                tagline = normalized.get('tagline')
                if tagline not in (None, '') and hasattr(tag, 'setTagLine'):
                    tag.setTagLine(str(tagline))
            except Exception:
                pass
        try:
            mediatype = normalized.get('mediatype')
            if mediatype not in (None, ''):
                item.setProperty('mediatype', str(mediatype))
        except Exception:
            pass
        return

    try:
        _set_listitem_info(item, info_type, info)
    except Exception:
        pass


_configure_python_logging()

# Wrappers locais mantidos para leitura simples no roteador principal.
def _setting_bool(key, default=False):
    return common_setting_bool(key, default, addon=ADDON)

def _set_setting_bool(key, value):
    common_set_setting_bool(key, value, addon=ADDON)

def _setting_text(key, default=''):
    return common_setting_text(key, default, addon=ADDON)


def _setting_enum_value(key, values, default=None):
    return common_setting_enum_value(key, values, default, addon=ADDON)


def _normalize_text(value):
    try:
        value = value or ''
        if not isinstance(value, text_type):
            value = text_type(value)
        value = unicodedata.normalize('NFKD', value)
        value = ''.join(ch for ch in value if not unicodedata.combining(ch))
        return value.strip().lower()
    except Exception:
        try:
            return text_type(value).strip().lower()
        except Exception:
            return ''





def _log_debug(message):
    common_log_debug(message, addon=ADDON)

def _log_warning(message):
    common_log(message, level=getattr(xbmc, 'LOGWARNING', 2))

def _log_error(message):
    common_log(message, level=getattr(xbmc, 'LOGERROR', 4))

def _notify(title, message, icon=None, milliseconds=2800):
    try:
        if icon is None:
            icon = xbmcgui.NOTIFICATION_INFO
        xbmcgui.Dialog().notification(title, message, icon, milliseconds)
    except:
        pass

def _show_error(message, title='Mega Portugal', dialog=False):
    if dialog:
        try:
            xbmcgui.Dialog().ok(title, message)
            return
        except:
            pass
    if _setting_bool('notificarerros', True):
        _notify(title, message, xbmcgui.NOTIFICATION_ERROR, 3500)


def _tv_proxy_wait_title():
    try:
        return ADDON.getAddonInfo('name') or 'Mega Portugal'
    except Exception:
        return 'Mega Portugal'


def _notify_tv_proxy_wait(milliseconds=6500):
    _notify(_tv_proxy_wait_title(), 'AGUARDE....', addonIcon, milliseconds)


def _open_tv_proxy_wait_dialog():
    title = _tv_proxy_wait_title()
    try:
        dialog = xbmcgui.DialogProgressBG()
        dialog.create(title, 'AGUARDE....')
        try:
            dialog.update(0, title, 'AGUARDE....')
        except TypeError:
            try:
                dialog.update(0, 'AGUARDE....')
            except Exception:
                pass
        return dialog
    except Exception:
        _notify(title, 'AGUARDE....', addonIcon, 9000)
        return None


def _update_tv_proxy_wait_dialog(dialog, percent=0):
    if not dialog:
        return
    try:
        dialog.update(max(0, min(100, int(percent))), _tv_proxy_wait_title(), 'AGUARDE....')
    except TypeError:
        try:
            dialog.update(max(0, min(100, int(percent))), 'AGUARDE....')
        except Exception:
            pass
    except Exception:
        pass


def _close_tv_proxy_wait_dialog(dialog):
    if not dialog:
        return
    try:
        dialog.close()
    except Exception:
        pass


def _wait_tv_proxy_playback_started(player, dialog=None, timeout_ms=18000, min_wait_ms=1200):
    start = time.time()
    last_notify = 0
    timeout_ms = max(2500, int(timeout_ms))
    min_wait_ms = max(0, int(min_wait_ms))
    while True:
        elapsed_ms = int((time.time() - start) * 1000)
        if elapsed_ms >= timeout_ms:
            return False
        percent = int((elapsed_ms * 100) / timeout_ms)
        _update_tv_proxy_wait_dialog(dialog, percent)
        if not dialog and (time.time() - last_notify) >= 3.0:
            _notify_tv_proxy_wait(4000)
            last_notify = time.time()
        if elapsed_ms >= min_wait_ms:
            try:
                if player.isPlayingVideo() or player.isPlaying():
                    _update_tv_proxy_wait_dialog(dialog, 100)
                    return True
            except Exception:
                pass
        try:
            xbmc.sleep(250)
        except Exception:
            time.sleep(0.25)



def _movie_navigation_mode():
    return _setting_enum_value('fluxofilmes', ['direct', 'details'], 'direct')


def _ensure_pluto_session(params=None):
    if isinstance(params, dict):
        incoming = (params.get('psid') or '').strip()
    else:
        incoming = (params or '').strip()
    return pluto.ensure_session(incoming)

def _pluto_entry_mode():
    return _setting_enum_value('pluto_entrada', ['channels', 'categories'], 'categories')


def _pluto_country_code():
    return _setting_enum_value('pluto_country', ['auto', 'br', 'us', 'mx', 'es', 'fr', 'it', 'de', 'gb', 'ca', 'au', 'ar', 'co', 'cl'], 'auto')


def _pluto_country_folder_mode_enabled():
    return False


def _pluto_all_countries_enabled():
    return False


def _pluto_merge_duplicates():
    return _setting_bool('pluto_merge_duplicates', True)


def _pluto_prefer_selected_country():
    return _setting_bool('pluto_prefer_selected_country', True)


def _pluto_epg_timezone_mode():
    return _setting_enum_value('pluto_epg_timezone', ['auto', 'selected_country'], 'auto')


def _pluto_retry_countries(params=None, include_auto_fallback=True):
    params = dict(params or {})
    countries = []
    current = pluto.normalize_country_code(_pluto_effective_country(params))
    if current not in countries:
        countries.append(current)
    explicit_country = bool(params.get('country_code'))
    if include_auto_fallback and not explicit_country and current != 'auto':
        countries.append('auto')
    return countries


def _pluto_call_with_fallback(fetcher, label, params=None, include_auto_fallback=True):
    params = dict(params or {})
    countries = _pluto_retry_countries(params, include_auto_fallback=include_auto_fallback)
    last_exc = None
    for idx, country_code in enumerate(countries):
        try_params = dict(params)
        try_params['country_code'] = country_code
        try:
            result = fetcher(try_params)
        except Exception as exc:
            last_exc = exc
            _log_warning('Pluto {} falhou no país {}: {}'.format(label, country_code, exc))
            continue
        if result:
            if idx > 0:
                _log_warning('Pluto {} recuperado via fallback de país {}.'.format(label, country_code))
            return result
        if idx + 1 < len(countries):
            _log_warning('Pluto {} sem dados no país {}. Tentando fallback.'.format(label, country_code))
    if last_exc is not None:
        raise last_exc
    return []


def _pluto_resolve_live_url(params):
    params = dict(params or {})
    slug = params.get('slug', '')
    channel_id = params.get('channel_id', '')
    requested_country = pluto.normalize_country_code(_pluto_effective_country(params))
    last_country = requested_country
    for country_code in _pluto_retry_countries(params):
        last_country = country_code
        url = pluto.resolve_stream_url(slug=slug, channel_id=channel_id, country=country_code)
        if url:
            if country_code != requested_country:
                _log_warning('Pluto live recuperado via fallback de país {}.'.format(country_code))
            return url, country_code
        if country_code != 'auto':
            _log_warning('Pluto live sem URL no país {}. Tentando fallback.'.format(country_code))
    return None, last_country


def _pluto_resolve_vod_url(params):
    params = dict(params or {})
    stitched_url = params.get('stitched_url', '')
    requested_country = pluto.normalize_country_code(_pluto_effective_country(params))
    last_country = requested_country
    for country_code in _pluto_retry_countries(params):
        last_country = country_code
        url = pluto.resolve_vod_stream_url(stitched_url=stitched_url, country=country_code)
        if url:
            if country_code != requested_country:
                _log_warning('Pluto VOD recuperado via fallback de país {}.'.format(country_code))
            return url, country_code
        if country_code != 'auto':
            _log_warning('Pluto VOD sem URL no país {}. Tentando fallback.'.format(country_code))
    return None, last_country


def _stream_priority_mode():
    return _setting_text('qualidadestream', '0')

def _current_skin_signature():
    skin_id = ''
    skin_theme = ''
    try:
        skin_id = xbmc.getSkinDir() or ''
    except Exception:
        pass
    try:
        skin_theme = xbmc.getInfoLabel('Skin.CurrentTheme') or ''
    except Exception:
        pass
    signature = '{}|{}'.format(skin_id, skin_theme).strip('|')
    return signature or 'default'


def _apply_default_view_once(flag_key, view, delay_ms=40, repeat=2):
    signature_key = '{}_skin_signature'.format(flag_key)
    current_signature = _current_skin_signature()
    last_signature = _setting_text(signature_key, '')

    if last_signature != current_signature:
        _set_setting_bool(flag_key, False)
        try:
            ADDON.setSetting(signature_key, current_signature)
        except Exception:
            pass

    if _setting_bool(flag_key, False):
        return
    try:
        setview(view)
    except Exception:
        return
    extra_retries = max(0, int(repeat) - 1)
    for _ in range(extra_retries):
        try:
            xbmc.sleep(delay_ms)
        except Exception:
            pass
        try:
            setview(view)
        except Exception:
            break
    _set_setting_bool(flag_key, True)
    try:
        ADDON.setSetting(signature_key, current_signature)
    except Exception:
        pass

def _complete_plugin_action(succeeded=True):
    for builtin in ('Dialog.Close(busydialog)', 'Dialog.Close(busydialognocancel)'):
        try:
            xbmc.executebuiltin(builtin)
        except:
            pass
    try:
        xbmcplugin.endOfDirectory(ADDON_HANDLE, succeeded=succeeded, updateListing=False, cacheToDisc=False)
    except TypeError:
        try:
            xbmcplugin.endOfDirectory(ADDON_HANDLE, succeeded=succeeded)
        except:
            pass
    except:
        pass

def _add_settings_item(plot='Abrir as configurações do addon Mega Portugal.'):
    label = 'Configurações'
    icon = TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'configuracoes.png'))
    li = xbmcgui.ListItem(label)
    li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
    _set_listitem_info(li, 'video', {'mediatype': 'video'})
    _set_listitem_info(li, type="Video", infoLabels={"Title": label, "Plot": plot})
    li.setProperty('IsPlayable', 'false')
    xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=build_url({'action': 'open_settings'}), listitem=li, isFolder=False)


def _add_pluto_shortcut(label, action, plot, icon_name='pluto.png'):
    icon = TRANSLATE(os.path.join(homeDir, 'resources', 'images', icon_name))
    if not os.path.exists(icon):
        icon = TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'pluto.png'))
    li = xbmcgui.ListItem(label)
    li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
    _set_listitem_info(li, 'video', {'mediatype': 'video'})
    _set_listitem_info(li, type="Video", infoLabels={"Title": label, "Plot": plot})
    li.setProperty('IsPlayable', 'false')
    pluto_session_id = pluto.new_session_id()
    xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=build_url({'action': action, 'psid': pluto_session_id}), listitem=li, isFolder=True)


def _add_pluto_item(plot=None):
    label = 'Pluto TV'
    icon = TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'pluto.png'))
    if not os.path.exists(icon):
        icon = TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'tv.png'))
    if plot is None:
        mode_label = 'categorias' if _pluto_entry_mode() == 'categories' else 'canais'
        plot = 'Abrir Pluto TV em {} com player dedicado separado do fluxo oficial.'.format(mode_label)
    li = xbmcgui.ListItem(label)
    li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
    _set_listitem_info(li, 'video', {'mediatype': 'video'})
    _set_listitem_info(li, type="Video", infoLabels={"Title": label, "Plot": plot})
    li.setProperty('IsPlayable', 'false')
    pluto_session_id = pluto.new_session_id()
    xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=build_url({'action': 'pluto_entry', 'psid': pluto_session_id}), listitem=li, isFolder=True)

def clear_subtitles_cache(notify=True):
    removed = 0
    try:
        removed = subtitles.clear_subtitles_cache(addon=ADDON)
    except Exception as exc:
        _log_debug('Erro limpando cache de legendas: {}'.format(exc))
    if notify:
        if removed:
            _notify('Mega Portugal', 'Cache de legendas limpo.', xbmcgui.NOTIFICATION_INFO, 2500)
        else:
            _notify('Mega Portugal', 'Nenhuma legenda em cache para limpar.', xbmcgui.NOTIFICATION_INFO, 2500)
    return removed

def clear_epg_cache(notify=True):
    removed = 0
    try:
        removed = m3u.clear_epg_cache()
    except Exception as exc:
        _log_debug('Erro limpando cache de EPG: {}'.format(exc))
    if notify:
        if removed:
            _notify('Mega Portugal', 'Cache de EPG limpo.', xbmcgui.NOTIFICATION_INFO, 2500)
        else:
            _notify('Mega Portugal', 'Nenhum arquivo de EPG em cache para limpar.', xbmcgui.NOTIFICATION_INFO, 2500)
    return removed


def clear_navigation_cache(notify=True):
    removed = 0
    try:
        removed = m3u.clear_navigation_cache()
    except Exception as exc:
        _log_debug('Erro limpando cache de navegação: {}'.format(exc))
    if notify:
        _notify('Mega Portugal', 'Cache de navegação limpo.' if removed else 'Nenhum cache de navegação para limpar.', xbmcgui.NOTIFICATION_INFO, 2500)
    return removed


def clear_catalog_cache(notify=True):
    removed = 0
    try:
        removed += int(tmdb.clear_tmdb_cache() or 0)
    except Exception as exc:
        _log_debug('Erro limpando cache TMDB: {}'.format(exc))
    try:
        removed += int(pluto.clear_pluto_persistent_cache() or 0)
    except Exception as exc:
        _log_debug('Erro limpando cache Pluto: {}'.format(exc))
    if notify:
        _notify('Mega Portugal', 'Cache de catálogo e Pluto limpo.' if removed else 'Nenhum cache de catálogo para limpar.', xbmcgui.NOTIFICATION_INFO, 2500)
    return removed


def clear_tv_history(notify=True):
    keys = ('last_tv_list_url', 'last_tv_list_name', 'last_tv_group', 'last_tv_group_url')
    for key in keys:
        try:
            ADDON.setSetting(key, '')
        except:
            pass
    if notify:
        _notify('Mega Portugal', 'Atalhos recentes da TV ao vivo foram limpos.', xbmcgui.NOTIFICATION_INFO, 2500)


def _show_text_dialog(title, text_value):
    body = text_value or 'Nenhuma informação disponível.'
    try:
        xbmcgui.Dialog().textviewer(title, body)
    except Exception:
        try:
            xbmcgui.Dialog().ok(title, body)
        except Exception:
            pass


def open_settings():
    opened = False
    try:
        ADDON.openSettings()
        opened = True
    except Exception:
        try:
            xbmc.executebuiltin('Addon.OpenSettings({})'.format(ADDON.getAddonInfo('id')))
            opened = True
        except Exception:
            try:
                _show_error('Não foi possível abrir as configurações do addon.', title='Mega Portugal', dialog=True)
            except:
                pass
    _complete_plugin_action(succeeded=opened)


def _resolve_tv_mode():
    try:
        mode = ADDON.getSetting('modocanais')
    except:
        mode = ''
    # Valores válidos na configuração: 0=Perguntar sempre, 1=M3U8, 2=MPEGTS.
    if mode == '1':
        return 0
    if mode == '2':
        return 1
    # Compat legado: se algum perfil antigo ainda trouxer "3", trata como MPEGTS.
    if mode == '3':
        return 1
    try:
        selected = xbmcgui.Dialog().select('Escolha uma opção', ['MODO M3U8', 'MODO MPEGTS'])
    except:
        selected = -1
    if selected == 0:
        return 0
    if selected == 1:
        return 1
    return -1

def _tv_epg_enabled():
    return True


def _pluto_epg_enabled():
    return _setting_bool('pluto_epg_ativo', True)


def _apply_tv_epg(channel, epg_map):
    name = channel.get('name', 'Canal')
    plot = 'Abrir o canal para reprodução.'
    if not _tv_epg_enabled():
        return name, plot
    label_suffix, epg_plot = m3u.format_epg_entry(epg_map.get(channel.get('id')))
    if label_suffix and _setting_bool('tv_epg_nomelista', True):
        name = '{} - [COLOR aquamarine]{}[/COLOR]'.format(name, label_suffix)
    if epg_plot:
        plot = epg_plot
    return name, plot


def _get_tv_epg_metadata(m3u_url, channel):
    metadata = {'plot': 'Abrir o canal para reprodução.', 'current_title': '', 'next_title': ''}
    if not _tv_epg_enabled() or not m3u_url or not channel:
        return metadata
    try:
        epg_map = m3u.get_epg_for_channels(m3u_url, [channel])
        entry = epg_map.get(channel.get('id'))
        if not entry:
            return metadata
        meta = m3u.describe_epg_entry(entry)
        metadata.update({
            'plot': meta.get('plot') or metadata['plot'],
            'current_title': meta.get('current_title', ''),
            'next_title': meta.get('next_title', ''),
        })
    except Exception as exc:
        _log_debug('Falha ao buscar metadados EPG do canal em reprodução: {}'.format(exc))
    return metadata



def setview(name):
    mode = {'Wall': '500',
            'List': '50',
            'Poster': '51',
            'Shift': '53',
            'InfoWall': '54',
            'WideList': '55',
            'Fanart': '502'
            }.get(name, '50')
    view = 'Container.SetViewMode(%s)'%mode
    xbmc.executebuiltin(view)

def _end_directory_with_view(content=None, view=None, succeeded=True, cache=True, delay_ms=40, repeat=1, default_once_key=None):
    if content:
        try:
            xbmcplugin.setContent(ADDON_HANDLE, content)
        except Exception:
            pass
    try:
        xbmcplugin.endOfDirectory(ADDON_HANDLE, succeeded=succeeded, updateListing=False, cacheToDisc=cache)
    except TypeError:
        try:
            xbmcplugin.endOfDirectory(ADDON_HANDLE, succeeded=succeeded)
        except Exception:
            pass
    except Exception:
        pass
    if view:
        if default_once_key:
            _apply_default_view_once(default_once_key, view, delay_ms=delay_ms, repeat=repeat)
        else:
            try:
                setview(view)
            except Exception:
                return
            extra_retries = max(0, int(repeat) - 1)
            for _ in range(extra_retries):
                try:
                    xbmc.sleep(delay_ms)
                except Exception:
                    pass
                try:
                    setview(view)
                except Exception:
                    break

def build_url(query):
    if PY3:
        return BASE_URL + '?' + urlencode(query, encoding='utf-8')
    else:
        try:
            query = {k: (v.encode('utf-8') if isinstance(v, unicode) else v) for k, v in query.items()}
        except NameError:
            pass
        return BASE_URL + '?' + urlencode(query)

def home():
    items = [('Pesquisar', 'Buscar filmes e séries pelo nome do conteúdo.', TRANSLATE(os.path.join(homeDir, 'resources','images','pesquisar.png')), {'action': 'search'}),
             ('TV ao vivo', 'Entrar nos canais de televisão portugueses.', TRANSLATE(os.path.join(homeDir, 'resources','images','tv.png')), {'action': 'tv'}),
             ('Filmes', 'Explorar filmes em alta, top avaliados e lançamentos.', TRANSLATE(os.path.join(homeDir, 'resources','images','filmes.png')), {'action': 'menu_movies'}),
             ('Séries', 'Explorar séries em alta, top avaliadas e lançamentos.', TRANSLATE(os.path.join(homeDir, 'resources','images','series.png')), {'action': 'menu_series'}),
             ('Configurações', 'Abrir as configurações do addon Mega Portugal.', TRANSLATE(os.path.join(homeDir, 'resources','images','configuracoes.png')), {'action': 'open_settings'})]

    for label, description, icon, query in items:
        url = build_url(query)
        li = xbmcgui.ListItem(label)
        li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
        _set_listitem_info(li, 'video', {'mediatype': 'video'})
        _set_listitem_info(li, type="Video", infoLabels={"Title": label, "Plot": description})
        if label == 'Configurações':
            li.setProperty('IsPlayable', 'false')
        is_folder = label != 'Configurações'
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=is_folder)

    _end_directory_with_view(content='movies', view='List', delay_ms=40, repeat=2, default_once_key='default_view_home_list_applied')

# --- As funções em TV (m3u, proxy, etc) não foram alteradas ---
def menu_tv():
    options = m3u.get_lists()
    icon = TRANSLATE(os.path.join(homeDir, 'resources','images','tv.png'))

    list_names = {
        options[0] if len(options) > 0 else '': 'Sport TV (1-6+)',
        options[1] if len(options) > 1 else '': 'Canais Portugal',
    }

    for option in options:
        name = list_names.get(option, 'Lista')
        url = build_url({'action': 'openm3u', 'url': option, 'source_name': name})
        li = xbmcgui.ListItem(name)
        _set_listitem_info(li, 'video', {'mediatype': 'video'})
        _set_listitem_info(li, type="Video", infoLabels={"Title": name, "Plot": 'Abrir a lista de {} com EPG incluído.'.format(name.lower())})
        li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)

    _add_settings_item()
    _end_directory_with_view(content='movies', view='List', delay_ms=40, repeat=2, default_once_key='default_view_tv_list_applied')

def openm3u(params):
    url = params.get('url')
    source_name = params.get('source_name', 'Lista')
    if not url:
        _show_error('URL da lista M3U inválida.', title='Kodi', dialog=True)
        return
    try:
        ordered_groups, group_counts = m3u.get_group_index(url)
        if not ordered_groups:
            _show_error('Nenhum grupo encontrado na lista.', title='Kodi', dialog=True)
            return

        if 'lista02' in url:
            allowed_groups = {'Portugal Sportv'}
        elif 'lista05' in url:
            allowed_groups = {'PT PORTUGAL'}
        else:
            allowed_groups = set(ordered_groups)

        icon = TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'tv.png'))

        for group in ordered_groups:
            if group not in allowed_groups:
                continue
            count = group_counts.get(group, 0)
            if count <= 0:
                continue
            url_ = build_url({'action': 'opengroup', 'url': url, 'group': group})
            label = '{} ({})'.format(group, count)
            li = xbmcgui.ListItem(label)
            _set_listitem_info(li, 'video', {'title': label, 'plot': 'Abrir a categoria {} de canais.'.format(group), 'mediatype': 'video'})
            li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
            xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url_, listitem=li, isFolder=True)
        _end_directory_with_view(content='movies', view='List', delay_ms=40, repeat=2, default_once_key='default_view_tv_list_applied')
    except Exception:
        _show_error('Erro ao processar a lista M3U.', title='Kodi', dialog=True)

def opengroup(params):   
    url = params.get('url')
    group = params.get('group')
    if group:
        try:
            group = group.decode('utf-8')
        except:
            pass
    if not url:
        _show_error('URL da lista M3U inválida.', title='Kodi', dialog=True)
        return
    try:
        channel_filter = m3u.get_group_channels(url, group)
        if not channel_filter:
            _show_error('Nenhum canal encontrado na lista.', title='Kodi', dialog=True)
            return
        epg_map = {}
        if _tv_epg_enabled():
            try:
                epg_map = m3u.get_epg_for_channels(url, channel_filter)
            except Exception as exc:
                _log_debug('Falha ao carregar EPG da TV ao vivo: {}'.format(exc))
                epg_map = {}
        for channel in channel_filter:
            display_name, display_plot = _apply_tv_epg(channel, epg_map)
            url_ = build_url({'action': 'play_proxy', 'name': channel['name'], 'url': channel['url'], 'icon': channel['logo'], 'm3u_url': url, 'tvg_id': channel.get('tvg_id', ''), 'tvg_name': channel.get('tvg_name', ''), 'group': channel.get('group', '')})
            li = xbmcgui.ListItem(display_name)
            _set_listitem_info(li, 'video', {'title': display_name, 'plot': display_plot, 'mediatype': 'video'})
            li.setArt({'icon': channel['logo'], 'thumb': channel['logo'], 'poster': channel['logo'], 'fanart': addonFanart})
            li.setProperty('IsPlayable', 'false')
            if _tv_epg_enabled() and (channel.get('tvg_id') or channel.get('tvg_name') or channel.get('name')):
                try:
                    full_epg_url = build_url({
                        'action': 'show_full_epg',
                        'm3u_url': url,
                        'channel_id': channel.get('id', ''),
                        'name': channel.get('name', 'Canal'),
                        'tvg_id': channel.get('tvg_id', ''),
                        'tvg_name': channel.get('tvg_name', ''),
                        'group': channel.get('group', '')
                    })
                    li.addContextMenuItems([('Ver Programação Completa', 'Container.Update({})'.format(full_epg_url))])
                except Exception:
                    pass
            xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url_, listitem=li, isFolder=False)
        _end_directory_with_view(content='movies', view='List', delay_ms=40, repeat=2, default_once_key='default_view_tv_list_applied')
    except Exception as e:
        _show_error('Erro ao processar a lista M3U.', title='Kodi', dialog=True)


def show_full_epg(params):
    """Mostra a Programação Completa do canal em uma pasta do Kodi.

    Inspirado no fluxo do OnePlayVIP, mas adaptado ao Matrix/M3U:
    - abre pelo menu de contexto com Container.Update;
    - usa o cache/índice XMLTV do resources/lib/m3u.py;
    - não reproduz itens da grade;
    - mantém fallback: se o cache ainda não existir, tenta carregar pelo mesmo
      método usado ao abrir a listagem do canal.
    """
    m3u_url = params.get('m3u_url', '')
    channel_name = params.get('name', '') or params.get('tvg_name', '') or 'Canal'
    channel = {
        'id': params.get('channel_id', '') or base64.urlsafe_b64encode(channel_name.encode('utf-8')).decode('ascii'),
        'name': channel_name,
        'tvg_id': params.get('tvg_id', ''),
        'tvg_name': params.get('tvg_name', '') or channel_name,
        'group': params.get('group', '')
    }

    try:
        if not _tv_epg_enabled():
            _notify('EPG', 'O EPG está desativado nas configurações.', xbmcgui.NOTIFICATION_WARNING, 3500)
            _end_directory_with_view(content='movies', succeeded=True, cache=False)
            return
        if not m3u_url:
            _notify('EPG', 'Lista M3U inválida para programação.', xbmcgui.NOTIFICATION_WARNING, 3500)
            _end_directory_with_view(content='movies', succeeded=True, cache=False)
            return

        try:
            xbmc.executebuiltin('ActivateWindow(busydialognocancel)')
        except Exception:
            pass
        try:
            programs = m3u.get_full_epg_for_channel(m3u_url, channel, limit=0, include_current=True)
        finally:
            try:
                xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
            except Exception:
                pass

        if not programs:
            _notify('EPG', 'Sem programação para este canal. Aguarde o cache em background.', xbmcgui.NOTIFICATION_WARNING, 4500)
            _end_directory_with_view(content='movies', succeeded=True, cache=False)
            return

        icon = TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'tv.png'))
        now_ts = int(time.time())
        last_day = ''
        count = 0

        for program in programs:
            if not isinstance(program, dict):
                continue
            title = (program.get('title') or 'Sem título').strip() or 'Sem título'
            desc = (program.get('desc') or '').strip()
            day_label = m3u.format_epg_program_day(program, now_ts=now_ts)
            if day_label and day_label != last_day:
                sep_label = '[COLOR gray]{}[/COLOR]'.format(day_label)
                sep = xbmcgui.ListItem(sep_label)
                _set_listitem_info(sep, 'video', {'title': sep_label, 'plot': 'Separador da programação.', 'mediatype': 'video'})
                sep.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
                sep.setProperty('IsPlayable', 'false')
                xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=build_url({'action': 'noop'}), listitem=sep, isFolder=False)
                last_day = day_label

            time_label = m3u.format_epg_program_range(program)
            label = '{} | {}'.format(time_label, title) if time_label else title
            if m3u.is_epg_program_current(program, now_ts=now_ts):
                label = '[COLOR aquamarine]{}[/COLOR]'.format(label)

            plot_lines = []
            if channel_name:
                plot_lines.append('[COLOR aquamarine]{}[/COLOR]'.format(channel_name))
            if time_label:
                plot_lines.append('[COLOR gold]{}[/COLOR]'.format(time_label))
            if desc:
                plot_lines.append('')
                plot_lines.append(desc)
            plot = '\n'.join(plot_lines) if plot_lines else 'Item da programação.'

            li = xbmcgui.ListItem(label)
            _set_listitem_info(li, 'video', {'title': title, 'plot': plot, 'mediatype': 'video'})
            li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
            li.setProperty('IsPlayable', 'false')
            xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=build_url({'action': 'noop'}), listitem=li, isFolder=False)
            count += 1

        if not count:
            _notify('EPG', 'Nenhum programa encontrado para este canal.', xbmcgui.NOTIFICATION_WARNING, 3500)
        _end_directory_with_view(content='videos', view='List', delay_ms=40, repeat=2, default_once_key='default_view_tv_list_applied')
    except Exception as exc:
        _log_debug('Erro ao abrir Programação Completa: {}'.format(exc))
        try:
            xbmc.executebuiltin('Dialog.Close(busydialognocancel)')
        except Exception:
            pass
        _show_error('Não foi possível abrir a Programação Completa.', title='EPG', dialog=False)
        _end_directory_with_view(content='movies', succeeded=True, cache=False)

def play_proxy(params):
    url = params.get('url')
    if not url:
        _show_error('URL inválida.', title='Kodi', dialog=True)
        return
    if _setting_bool('confirmartv', False):
        try:
            channel_name = params.get('name', 'Canal')
            message = 'Deseja reproduzir este canal?'
            if channel_name:
                message += '\n' + channel_name
            confirmed = xbmcgui.Dialog().yesno('Mega Portugal', message)
        except Exception:
            confirmed = True
        if not confirmed:
            return
    original_name = params.get('name', 'Canal')
    op = _resolve_tv_mode()
    if op not in (0, 1):
        return
    wait_dialog = _open_tv_proxy_wait_dialog()
    try:
        raw_url = unquote_plus(url)
        if op == 0:
            name = original_name + ' (M3U8)'
            url = m3u.convert_to_m3u8(raw_url)
            proxy_url = URL_HLSRETRY + quote(url)
        elif op == 1:
            name = original_name + ' (TS)'
            url = m3u.convert_to_ts(raw_url)
            proxy_url = URL_TS_DOWNLOADER + quote(url)
        proxy_ok = proxy.wait_for_service_proxy(timeout=3.0)
        if not proxy_ok:
            proxy_ok = proxy.start_proxy()
        if not proxy_ok:
            url = raw_url
        else:
            url = proxy_url
        channel_meta = {
            'name': original_name,
            'tvg_id': params.get('tvg_id', ''),
            'tvg_name': params.get('tvg_name', '') or original_name,
            'group': params.get('group', ''),
            'id': base64.urlsafe_b64encode(original_name.encode('utf-8')).decode('ascii')
        }
        epg_meta = _get_tv_epg_metadata(params.get('m3u_url', ''), channel_meta)
        plot = epg_meta.get('plot') or 'Abrir o canal para reprodução.'
        li = xbmcgui.ListItem(name)
        _set_listitem_info(li, 'video', {
            'title': name,
            'plot': plot,
            'mediatype': 'video'
        })
        _set_listitem_info(li, type="Video", infoLabels={
            "Title": name,
            "Plot": plot
        })
        li.setArt({'icon': params.get('icon', ''), 'thumb': params.get('icon', ''), 'poster': params.get('icon', ''), 'fanart': addonFanart})
        player = xbmc.Player()
        player.play(item=url, listitem=li)
        _wait_tv_proxy_playback_started(player, wait_dialog)
    finally:
        _close_tv_proxy_wait_dialog(wait_dialog)

# --- Funções de Filmes e Séries (navegação) não foram alteradas ---

def _pluto_effective_country(params=None):
    params = params or {}
    return (params.get('country_code') or _pluto_country_code())



def _pluto_effective_all_countries(params=None):
    params = params or {}
    if params.get('country_code'):
        return False
    if 'all_countries' in params:
        return str(params.get('all_countries', '')).lower() in ('1', 'true', 'yes')
    return False



def _pluto_requires_country_folders(params=None):
    params = params or {}
    if params.get('country_code'):
        return False
    return _pluto_country_code() == 'auto'



def _pluto_country_art(country_code):
    fallback = TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'pluto.png'))
    flag_path = TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'flags', '{}.png'.format((country_code or '').lower())))
    try:
        if os.path.exists(flag_path):
            return flag_path
    except Exception:
        pass
    return fallback



def _add_pluto_country_folders(action, media_title, plot_template, extra_params=None):
    extra_params = extra_params or {}
    for country_code, country_name in pluto.get_supported_countries():
        if country_code == 'auto':
            continue
        label = country_name
        plot = plot_template.format(country=country_name, media=media_title)
        art = _pluto_country_art(country_code)
        li = xbmcgui.ListItem(label)
        _set_listitem_info(li, 'video', {'title': label, 'plot': plot, 'mediatype': 'video'})
        _set_listitem_info(li, type="Video", infoLabels={"Title": label, "Plot": plot})
        li.setArt({'icon': art, 'thumb': art, 'poster': art, 'fanart': addonFanart})
        query = {'action': action, 'country_code': country_code, 'psid': pluto.new_session_id()}
        query.update(extra_params)
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=build_url(query), listitem=li, isFolder=True)



def _get_pluto_channels(params=None):
    return _pluto_call_with_fallback(
        lambda try_params: pluto.get_channels(
            hide_no_logo=_setting_bool('pluto_ocultarsemlogo', False),
            include_epg=_pluto_epg_enabled(),
            country=_pluto_effective_country(try_params),
            all_countries=_pluto_effective_all_countries(try_params),
            merge_duplicates=_pluto_merge_duplicates(),
            prioritize_country=_pluto_prefer_selected_country(),
            timezone_mode=_pluto_epg_timezone_mode()
        ),
        label='canais',
        params=params
    )


def _get_pluto_channels_grouped(params=None):
    return _pluto_call_with_fallback(
        lambda try_params: pluto.get_channels_grouped_by_category(
            hide_no_logo=_setting_bool('pluto_ocultarsemlogo', False),
            include_epg=_pluto_epg_enabled(),
            country=_pluto_effective_country(try_params),
            all_countries=_pluto_effective_all_countries(try_params),
            merge_duplicates=_pluto_merge_duplicates(),
            prioritize_country=_pluto_prefer_selected_country(),
            timezone_mode=_pluto_epg_timezone_mode()
        ),
        label='categorias canais',
        params=params
    )


def _get_pluto_channels_for_category(category, params=None):
    return _pluto_call_with_fallback(
        lambda try_params: pluto.get_channels_for_category(
            category=category,
            hide_no_logo=_setting_bool('pluto_ocultarsemlogo', False),
            include_epg=_pluto_epg_enabled(),
            country=_pluto_effective_country(try_params),
            all_countries=_pluto_effective_all_countries(try_params),
            merge_duplicates=_pluto_merge_duplicates(),
            prioritize_country=_pluto_prefer_selected_country(),
            timezone_mode=_pluto_epg_timezone_mode()
        ),
        label='canais por categoria ({})'.format(category),
        params=params
    )


def _get_pluto_vod_categories(media_type, params=None):
    return _pluto_call_with_fallback(
        lambda try_params: pluto.get_vod_categories(
            media_type=media_type,
            country=_pluto_effective_country(try_params),
            all_countries=_pluto_effective_all_countries(try_params),
            merge_duplicates=_pluto_merge_duplicates(),
            prioritize_country=_pluto_prefer_selected_country()
        ),
        label='categorias {}'.format(media_type),
        params=params
    )


def _get_pluto_vod_items(category_key, media_type, params=None):
    return _pluto_call_with_fallback(
        lambda try_params: pluto.get_vod_items(
            category_key=category_key,
            media_type=media_type,
            country=_pluto_effective_country(try_params),
            all_countries=_pluto_effective_all_countries(try_params),
            merge_duplicates=_pluto_merge_duplicates(),
            prioritize_country=_pluto_prefer_selected_country()
        ),
        label='itens {} ({})'.format(media_type, category_key),
        params=params
    )


def _get_pluto_series_seasons(series_id, params=None):
    return _pluto_call_with_fallback(
        lambda try_params: pluto.get_series_seasons(
            series_id,
            country=_pluto_effective_country(try_params)
        ),
        label='temporadas série {}'.format(series_id),
        params=params
    )


def pluto_entry(params=None):
    params = params or {}
    psid = _ensure_pluto_session(params)
    if _pluto_requires_country_folders(params):
        _add_pluto_country_folders('pluto_entry', 'Pluto TV', 'Abrir {media} do país {country}.')
        _end_directory_with_view(content='videos', view='List', delay_ms=40, repeat=2, default_once_key='default_view_tv_list_applied')
        return
    if _pluto_entry_mode() == 'categories':
        channels_pluto_categories(params=params, psid=psid)
        return
    channels_pluto(params=params, psid=psid)


def channels_pluto(params=None, psid=''):
    params = params or {}
    try:
        channels = _get_pluto_channels(params)
    except Exception as exc:
        _log_error('Falha ao carregar Pluto TV: {}'.format(exc))
        _show_error('Não foi possível carregar os canais do Pluto TV.', dialog=True)
        return
    if not channels:
        _show_error('Nenhum canal do Pluto TV foi encontrado.', dialog=True)
        return
    for channel in channels:
        name = channel.get('display_name') or channel.get('name') or 'Pluto TV'
        desc = channel.get('description', '')
        thumb = channel.get('thumb', '')
        slug = channel.get('slug', '')
        channel_id = channel.get('channel_id', '')
        if not (slug or channel_id):
            continue
        icon = thumb or TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'pluto.png'))
        url_ = build_url({'action': 'play_pluto', 'name': name, 'description': desc, 'icon': icon, 'slug': slug, 'channel_id': channel_id, 'psid': psid, 'country_code': channel.get('country_code', _pluto_country_code())})
        li = xbmcgui.ListItem(name)
        _set_listitem_info(li, 'video', {'title': name, 'plot': desc, 'mediatype': 'video'})
        _set_listitem_info(li, type="Video", infoLabels={"Title": name, "Plot": desc})
        li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
        li.setProperty('IsPlayable', 'true')
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url_, listitem=li, isFolder=False)
    _end_directory_with_view(content='videos', view='List', delay_ms=40, repeat=2, default_once_key='default_view_tv_list_applied')


def channels_pluto_categories(params=None, psid=''):
    params = params or {}
    try:
        grouped = _get_pluto_channels_grouped(params)
    except Exception as exc:
        _log_debug('Falha ao carregar categorias do Pluto TV: {}'.format(exc))
        _show_error('Não foi possível carregar as categorias do Pluto TV.', dialog=True)
        return
    if not grouped:
        _show_error('Nenhuma categoria do Pluto TV foi encontrada.', dialog=True)
        return
    icon = TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'pluto.png'))
    for category, channels in grouped:
        label = '{} ({})'.format(category, len(channels))
        li = xbmcgui.ListItem(label)
        _set_listitem_info(li, 'video', {'title': label, 'plot': 'Abrir canais da categoria {} no Pluto TV.'.format(category), 'mediatype': 'video'})
        _set_listitem_info(li, type="Video", infoLabels={"Title": label, "Plot": 'Abrir canais da categoria {} no Pluto TV.'.format(category)})
        li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=build_url({'action': 'channels_pluto_category', 'category': category, 'psid': psid, 'country_code': _pluto_effective_country(params)}), listitem=li, isFolder=True)
    _end_directory_with_view(content='videos', view='List', delay_ms=40, repeat=2, default_once_key='default_view_tv_list_applied')


def channels_pluto_category(params, psid=''):
    category = params.get('category', '')
    try:
        channels = _get_pluto_channels_for_category(category, params)
    except Exception as exc:
        _log_error('Falha ao carregar categoria Pluto {}: {}'.format(category, exc))
        _show_error('Não foi possível carregar os canais dessa categoria do Pluto TV.', dialog=True)
        return
    if not channels:
        _show_error('Nenhum canal foi encontrado nessa categoria do Pluto TV.', dialog=True)
        return
    for channel in channels:
        name = channel.get('display_name') or channel.get('name') or 'Pluto TV'
        desc = channel.get('description', '')
        thumb = channel.get('thumb', '')
        slug = channel.get('slug', '')
        channel_id = channel.get('channel_id', '')
        if not (slug or channel_id):
            continue
        icon = thumb or TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'pluto.png'))
        url_ = build_url({'action': 'play_pluto', 'name': name, 'description': desc, 'icon': icon, 'slug': slug, 'channel_id': channel_id, 'psid': psid, 'country_code': channel.get('country_code', _pluto_country_code())})
        li = xbmcgui.ListItem(name)
        _set_listitem_info(li, 'video', {'title': name, 'plot': desc, 'mediatype': 'video'})
        _set_listitem_info(li, type="Video", infoLabels={"Title": name, "Plot": desc})
        li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
        li.setProperty('IsPlayable', 'true')
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url_, listitem=li, isFolder=False)
    _end_directory_with_view(content='videos', view='List', delay_ms=40, repeat=2, default_once_key='default_view_tv_list_applied')



def _pluto_mark_unresolved():
    """Finaliza explicitamente a rota plugin quando o Pluto não pode resolver.

    Sem este retorno o Kodi trata a URL do plugin como item reproduzível que
    simplesmente nunca respondeu, exibindo "skipping unplayable item" sem a
    causa real. A falha continua visível ao usuário, mas o fluxo é encerrado
    de forma determinística em Kodi 19+.
    """
    try:
        failed_item = xbmcgui.ListItem()
        failed_item.setProperty('IsPlayable', 'false')
        xbmcplugin.setResolvedUrl(handle=ADDON_HANDLE, succeeded=False, listitem=failed_item)
    except Exception:
        pass


def _inputstream_adaptive_available():
    """Detecta InputStream Adaptive sem depender do helper estar no sys.path.

    O helper é útil para instalações que ainda precisam instalar o binário,
    mas o player Pluto não deve ser bloqueado quando o Adaptive já está
    presente e o helper não foi declarado por outro addon.
    """
    try:
        if xbmc.getCondVisibility('System.HasAddon(inputstream.adaptive)'):
            return True
    except Exception:
        pass
    try:
        xbmcaddon.Addon('inputstream.adaptive')
        return True
    except Exception:
        return False


def _prepare_pluto_hls_inputstream():
    """Retorna o addon InputStream apto para HLS ou string vazia.

    Prioriza o Adaptive já instalado. Só chama InputStream Helper como fallback
    para instalação/validação em builds onde o binário ainda não existe; isso
    evita diálogos e falsos negativos em Kodi 19/20/21 quando o Helper não
    estava declarado na árvore de dependências do plugin.
    """
    if _inputstream_adaptive_available():
        return 'inputstream.adaptive'

    if inputstreamhelper is not None:
        try:
            helper = inputstreamhelper.Helper('hls')
            if helper.check_inputstream():
                addon_name = getattr(helper, 'inputstream_addon', '') or 'inputstream.adaptive'
                return addon_name
        except Exception as exc:
            _log_warning('Pluto: InputStream Helper falhou ao preparar HLS: {}'.format(exc))

    return ''


def _split_pluto_stream_headers(stream_url):
    stream_url = stream_url or ''
    if '|' in stream_url:
        base_url, headers = stream_url.split('|', 1)
        return base_url, headers
    return stream_url, ''


def _configure_pluto_hls_item(list_item, stream_url, inputstream_addon, headers=''):
    """Configura um ListItem HLS de forma idêntica para live e VOD."""
    list_item.setPath(stream_url)
    list_item.setProperty('IsPlayable', 'true')
    list_item.setProperty('inputstream', inputstream_addon)
    # Compatibilidade com builds Matrix/Leia que ainda leem o alias legado.
    list_item.setProperty('inputstreamaddon', inputstream_addon)
    list_item.setProperty('inputstream.adaptive.manifest_type', 'hls')
    header_value = headers or 'User-Agent={}'.format(DEFAULT_USER_AGENT)
    list_item.setProperty('inputstream.adaptive.stream_headers', header_value)
    list_item.setProperty('inputstream.adaptive.manifest_headers', header_value)
    list_item.setMimeType('application/x-mpegURL')
    list_item.setContentLookup(False)

def play_pluto(params):
    slug = params.get('slug', '')
    channel_id = params.get('channel_id', '')
    name = params.get('name', 'Pluto TV')
    icon = params.get('icon', '')
    description = params.get('description', '')
    _ensure_pluto_session(params.get('psid', ''))
    url, resolved_country = _pluto_resolve_live_url(params)
    if not url:
        _log_error('Pluto live sem URL válida para {}.'.format(name))
        _show_error('URL do canal Pluto inválida.', dialog=True)
        return
    if inputstreamhelper is None:
        _show_error('O módulo InputStream Helper não está disponível para reproduzir o Pluto TV.', dialog=True)
        return
    try:
        helper = inputstreamhelper.Helper('hls')
        if not helper.check_inputstream():
            _show_error('O InputStream Adaptive não está disponível para reproduzir o Pluto TV.', dialog=True)
            return
    except Exception as exc:
        _log_error('Falha no inputstream do Pluto TV: {}'.format(exc))
        _show_error('Falha ao inicializar o player dedicado do Pluto TV.', dialog=True)
        return

    headers = ''
    if '|' in url:
        url, headers = url.split('|', 1)

    li = xbmcgui.ListItem(path=url)
    li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
    _set_listitem_info(li, 'video', {'title': name, 'plot': description, 'mediatype': 'video'})
    _set_listitem_info(li, type="Video", infoLabels={"Title": name, "Plot": description})
    li.setProperty('inputstream', helper.inputstream_addon)
    li.setProperty('inputstream.adaptive.manifest_type', 'hls')
    li.setProperty('inputstream.adaptive.live_delay', '0')
    li.setProperty('inputstream.adaptive.manifest_update_parameter', 'full')
    li.setMimeType('application/x-mpegURL')
    if headers:
        li.setProperty('inputstream.adaptive.stream_headers', headers)
        li.setProperty('inputstream.adaptive.manifest_headers', headers)
    li.setContentLookup(False)
    xbmcplugin.setResolvedUrl(handle=ADDON_HANDLE, succeeded=True, listitem=li)

def pluto_movies(params=None):
    params = params or {}
    psid = _ensure_pluto_session(params)
    if _pluto_requires_country_folders(params):
        _add_pluto_country_folders('pluto_movies', 'Filmes - Pluto TV', 'Abrir {media} do país {country}.')
        _end_directory_with_view(content='movies', view='List', delay_ms=40, repeat=2)
        return
    try:
        categories = _get_pluto_vod_categories('movie', params)
    except Exception as exc:
        _log_error('Falha ao carregar categorias Pluto Filmes: {}'.format(exc))
        _show_error('Não foi possível carregar as categorias do Pluto Filmes.', dialog=True)
        return
    if not categories:
        _show_error('Nenhuma categoria do Pluto Filmes foi encontrada.', dialog=True)
        return
    icon = TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'pluto.png'))
    for category in categories:
        label = '{} ({})'.format(category.get('name', 'Pluto Filmes'), category.get('item_count', 0))
        plot = 'Abrir a categoria {} do Pluto Filmes.'.format(category.get('name', 'Pluto Filmes'))
        if _pluto_effective_all_countries(params):
            plot += ' Fonte base: {}.'.format(category.get('country_label', 'Pluto'))
        li = xbmcgui.ListItem(label)
        _set_listitem_info(li, 'video', {'title': label, 'plot': plot, 'mediatype': 'video'})
        _set_listitem_info(li, type="Video", infoLabels={"Title": label, "Plot": plot})
        li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=build_url({'action': 'pluto_vod_category', 'media_type': 'movie', 'category_key': category.get('key', ''), 'psid': psid, 'country_code': _pluto_effective_country(params)}),
            listitem=li,
            isFolder=True
        )
    _end_directory_with_view(content='movies', view='List', delay_ms=40, repeat=2)


def pluto_series(params=None):
    params = params or {}
    psid = _ensure_pluto_session(params)
    if _pluto_requires_country_folders(params):
        _add_pluto_country_folders('pluto_series', 'Séries - Pluto TV', 'Abrir {media} do país {country}.')
        _end_directory_with_view(content='tvshows', view='List', delay_ms=40, repeat=2)
        return
    try:
        categories = _get_pluto_vod_categories('series', params)
    except Exception as exc:
        _log_error('Falha ao carregar categorias Pluto Séries: {}'.format(exc))
        _show_error('Não foi possível carregar as categorias do Pluto Séries.', dialog=True)
        return
    if not categories:
        _show_error('Nenhuma categoria do Pluto Séries foi encontrada.', dialog=True)
        return
    icon = TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'pluto.png'))
    for category in categories:
        label = '{} ({})'.format(category.get('name', 'Pluto Séries'), category.get('item_count', 0))
        plot = 'Abrir a categoria {} do Pluto Séries.'.format(category.get('name', 'Pluto Séries'))
        if _pluto_effective_all_countries(params):
            plot += ' Fonte base: {}.'.format(category.get('country_label', 'Pluto'))
        li = xbmcgui.ListItem(label)
        _set_listitem_info(li, 'video', {'title': label, 'plot': plot, 'mediatype': 'video'})
        _set_listitem_info(li, type="Video", infoLabels={"Title": label, "Plot": plot})
        li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=build_url({'action': 'pluto_vod_category', 'media_type': 'series', 'category_key': category.get('key', ''), 'psid': psid, 'country_code': _pluto_effective_country(params)}),
            listitem=li,
            isFolder=True
        )
    _end_directory_with_view(content='tvshows', view='List', delay_ms=40, repeat=2)


def pluto_vod_category(params=None):
    params = params or {}
    psid = _ensure_pluto_session(params)
    category_key = params.get('category_key', '')
    media_type = params.get('media_type', 'movie')
    try:
        items = _get_pluto_vod_items(category_key, media_type, params)
    except Exception as exc:
        _log_error('Falha ao carregar itens VOD Pluto [{}]: {}'.format(media_type, exc))
        _show_error('Não foi possível carregar os itens desta categoria do Pluto.', dialog=True)
        return
    if not items:
        _show_error('Nenhum item foi encontrado nesta categoria do Pluto.', dialog=True)
        return
    default_icon = TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'pluto.png'))
    for item in items:
        name = item.get('name', 'Pluto')
        plot = item.get('summary', '') or item.get('description', '')
        icon = item.get('poster', '') or default_icon
        fanart = item.get('fanart', '') or addonFanart
        if media_type == 'movie':
            target_url = build_url({
                'action': 'play_pluto_vod',
                'media_type': media_type,
                'name': name,
                'description': plot,
                'icon': icon,
                'fanart': fanart,
                'stitched_url': item.get('stitched_url', ''),
                'country_code': item.get('country_code', _pluto_country_code()),
                'psid': psid,
            })
            is_folder = False
        else:
            target_url = build_url({
                'action': 'pluto_vod_seasons',
                'series_id': item.get('id', ''),
                'name': name,
                'description': plot,
                'icon': icon,
                'fanart': fanart,
                'country_code': item.get('country_code', _pluto_country_code()),
                'psid': psid,
            })
            is_folder = True
        li = xbmcgui.ListItem(name)
        _set_listitem_info(li, 'video', {'title': name, 'plot': plot, 'mediatype': 'video'})
        _set_listitem_info(li, type="Video", infoLabels={"Title": name, "Plot": plot})
        li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': fanart})
        if not is_folder:
            li.setProperty('IsPlayable', 'true')
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=target_url, listitem=li, isFolder=is_folder)
    if media_type == 'movie':
        _end_directory_with_view(content='movies', view='List', delay_ms=40, repeat=2)
    else:
        _end_directory_with_view(content='tvshows', view='List', delay_ms=40, repeat=2)


def pluto_vod_seasons(params=None):
    params = params or {}
    psid = _ensure_pluto_session(params)
    series_id = params.get('series_id', '')
    country_code = _pluto_effective_country(params)
    try:
        seasons = _get_pluto_series_seasons(series_id, params)
    except Exception as exc:
        _log_debug('Falha ao carregar temporadas Pluto [{}]: {}'.format(series_id, exc))
        _show_error('Não foi possível carregar as temporadas desta série do Pluto.', dialog=True)
        return
    if not seasons:
        _show_error('Nenhuma temporada foi encontrada para esta série do Pluto.', dialog=True)
        return
    default_icon = params.get('icon', '') or TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'pluto.png'))
    for season in seasons:
        label = season.get('name', 'Temporada {}'.format(season.get('season_number', 0)))
        plot = season.get('description', '') or 'Abrir episódios desta temporada do Pluto.'
        li = xbmcgui.ListItem(label)
        _set_listitem_info(li, 'video', {'title': label, 'plot': plot, 'mediatype': 'video'})
        _set_listitem_info(li, type="Video", infoLabels={"Title": label, "Plot": plot})
        li.setArt({'icon': default_icon, 'thumb': default_icon, 'poster': default_icon, 'fanart': params.get('fanart', addonFanart)})
        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=build_url({
                'action': 'pluto_vod_episodes',
                'series_id': series_id,
                'season_number': str(season.get('season_number', 0)),
                'name': params.get('name', 'Séries - Pluto TV'),
                'icon': params.get('icon', default_icon),
                'fanart': params.get('fanart', addonFanart),
                'country_code': country_code,
                'psid': psid,
            }),
            listitem=li,
            isFolder=True
        )
    _end_directory_with_view(content='seasons', view='List', delay_ms=40, repeat=2)


def pluto_vod_episodes(params=None):
    params = params or {}
    psid = _ensure_pluto_session(params)
    series_id = params.get('series_id', '')
    country_code = _pluto_effective_country(params)
    season_number = int(params.get('season_number', '0') or 0)
    try:
        seasons = _get_pluto_series_seasons(series_id, params)
    except Exception as exc:
        _log_debug('Falha ao carregar episódios Pluto [{}]: {}'.format(series_id, exc))
        _show_error('Não foi possível carregar os episódios desta temporada do Pluto.', dialog=True)
        return
    selected = None
    for season in seasons:
        if int(season.get('season_number', 0) or 0) == season_number:
            selected = season
            break
    if not selected:
        _show_error('Nenhum episódio foi encontrado para esta temporada do Pluto.', dialog=True)
        return
    default_icon = params.get('icon', '') or TRANSLATE(os.path.join(homeDir, 'resources', 'images', 'pluto.png'))
    for episode in selected.get('episodes', []):
        title = '{} - S{}E{}'.format(params.get('name', 'Séries - Pluto TV'), str(season_number).zfill(2), str(episode.get('episode_number', 0)).zfill(2))
        ep_name = episode.get('name', '')
        plot = episode.get('summary', '') or episode.get('description', '')
        icon = episode.get('poster', '') or default_icon
        fanart = episode.get('fanart', '') or params.get('fanart', addonFanart)
        li = xbmcgui.ListItem(title)
        _set_listitem_info(li, 'video', {'title': title, 'plot': plot, 'mediatype': 'episode'})
        _set_listitem_info(li, type="Video", infoLabels={"Title": title, "Plot": plot})
        li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': fanart})
        li.setProperty('IsPlayable', 'true')
        xbmcplugin.addDirectoryItem(
            handle=ADDON_HANDLE,
            url=build_url({
                'action': 'play_pluto_vod',
                'media_type': 'episode',
                'name': '{} - {}'.format(title, ep_name) if ep_name else title,
                'description': plot,
                'icon': icon,
                'fanart': fanart,
                'stitched_url': episode.get('stitched_url', ''),
                'country_code': country_code,
                'psid': psid,
            }),
            listitem=li,
            isFolder=False
        )
    _end_directory_with_view(content='episodes', view='WideList', delay_ms=40, repeat=2)


def play_pluto_vod(params):
    name = params.get('name', 'Pluto')
    icon = params.get('icon', '')
    description = params.get('description', '')
    country_code = _pluto_effective_country(params)
    _ensure_pluto_session(params.get('psid', ''))
    url, resolved_country = _pluto_resolve_vod_url(params)
    if not url:
        _log_error('Pluto VOD sem URL válida para {}.'.format(name))
        _show_error('URL do conteúdo Pluto inválida.', dialog=True)
        return
    if inputstreamhelper is None:
        _show_error('O módulo InputStream Helper não está disponível para reproduzir o Pluto.', dialog=True)
        return
    try:
        helper = inputstreamhelper.Helper('hls')
        if not helper.check_inputstream():
            _show_error('O InputStream Adaptive não está disponível para reproduzir o Pluto.', dialog=True)
            return
    except Exception as exc:
        _log_error('Falha ao preparar o player do Pluto VOD: {}'.format(exc))
        _show_error('Falha ao preparar o player do Pluto.', dialog=True)
        return
    headers = ''
    if '|User-Agent=' in url:
        url, headers = url.split('|', 1)
    li = xbmcgui.ListItem(name)
    _set_listitem_info(li, 'video', {'title': name, 'plot': description, 'mediatype': 'video'})
    _set_listitem_info(li, type="Video", infoLabels={"Title": name, "Plot": description})
    li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': params.get('fanart', addonFanart)})
    li.setPath(url)
    li.setProperty('IsPlayable', 'true')
    try:
        addon_name = helper.inputstream_addon
    except Exception:
        addon_name = 'inputstream.adaptive'
    li.setProperty('inputstream', addon_name)
    li.setProperty('inputstreamaddon', addon_name)
    li.setProperty('inputstream.adaptive.manifest_type', 'hls')
    li.setProperty('inputstream.adaptive.stream_headers', headers or 'User-Agent={}'.format(DEFAULT_USER_AGENT))
    li.setProperty('inputstream.adaptive.manifest_headers', headers or 'User-Agent={}'.format(DEFAULT_USER_AGENT))
    li.setMimeType('application/x-mpegURL')
    li.setContentLookup(False)
    xbmcplugin.setResolvedUrl(handle=ADDON_HANDLE, succeeded=True, listitem=li)

def menu_movies():  
    items = [
        ('Filmes - Em Alta', 'Ver os filmes em tendência na semana.', {'action': 'list', 'type': 'movie', 'category': 'trending'}),
        ('Filmes - Top Avaliados', 'Ver os filmes mais bem avaliados.', {'action': 'list', 'type': 'movie', 'category': 'top'}),
        ('Filmes - Em Cartaz', 'Ver os filmes atualmente em cartaz conforme a região configurada.', {'action': 'list', 'type': 'movie', 'category': 'now_playing'}),
        ('Filmes - Próximos Lançamentos', 'Ver os próximos lançamentos de filmes conforme a região configurada.', {'action': 'list', 'type': 'movie', 'category': 'upcoming'}),
    ]
    for label, description, query in items:
        url = build_url(query)
        icon = TRANSLATE(os.path.join(homeDir, 'resources','images','filmes.png'))
        li = xbmcgui.ListItem(label)
        _set_listitem_info(li, 'video', {'mediatype': 'video'})
        _set_listitem_info(li, type="Video", infoLabels={"Title": label, "Plot": description})
        li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    _add_settings_item('Abrir as configurações do addon Mega Portugal a partir da área de filmes.')
    _end_directory_with_view(content='movies', view='List', delay_ms=40, repeat=2)

def menu_series():  
    items = [
        ('Séries - Em Alta', 'Ver as séries em tendência na semana.', {'action': 'list', 'type': 'series', 'category': 'trending'}),
        ('Séries - Top Avaliadas', 'Ver as séries mais bem avaliadas.', {'action': 'list', 'type': 'series', 'category': 'top'}),
        ('Séries - No Ar', 'Ver séries com episódios em exibição nos próximos dias.', {'action': 'list', 'type': 'series', 'category': 'on_the_air'}),
        ('Séries - Estreias Recentes', 'Ver séries com estreia inicial recente.', {'action': 'list', 'type': 'series', 'category': 'recent_premieres'}),
    ]    
    for label, description, query in items:
        url = build_url(query)
        icon = TRANSLATE(os.path.join(homeDir, 'resources','images','series.png'))
        li = xbmcgui.ListItem(label)
        _set_listitem_info(li, 'video', {'mediatype': 'video'})
        _set_listitem_info(li, type="Video", infoLabels={"Title": label, "Plot": description})
        li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
    _add_settings_item('Abrir as configurações do addon Mega Portugal a partir da área de séries.')
    _end_directory_with_view(content='tvshows', view='List', delay_ms=40, repeat=2)            

def list_items(params):  
    # ... (sem alterações aqui)
    next_button = False
    page = int(params.get('page', '1'))
    items = tmdb.get_items(params.get('type'), params.get('category'), page)
    if items:
        next_button = True

    for item in items:
        title = '{} ({})'.format(item['title'], item['year']) if item['year'] else item['title']
        fanart = item['background'] if item.get('background') else ''
        url = build_url({'action': 'details', 'type': params.get('type'), 'id': item['id'], 'name': item['title'], 'year': item['year'], 'fanart': fanart})
        li = xbmcgui.ListItem(title)
        if item.get('poster') or item.get('background'):
            li.setArt({
                'thumb': item['poster'] if item.get('poster') else None,
                'icon': item['poster'] if item.get('poster') else None,
                'poster': item['poster'] if item.get('poster') else None,
                'fanart': item['background'] if item.get('background') else None
            })
        _set_listitem_info(li, 'video', {'title': title, 'plot': item.get('description', ''), 'year': int(item['year']) if item['year'] else 0, 'mediatype': 'video'})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)

    next_page_url = build_url({
        'action': 'list',
        'type': params.get('type'),
        'category': params.get('category'),
        'page': str(page + 1)
    })
    if next_button:
        next_li = xbmcgui.ListItem('[Próxima Página]')
        icon = TRANSLATE(os.path.join(homeDir, 'resources','images','proximo.png'))
        next_li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
        _set_listitem_info(next_li, 'video', {'mediatype': 'video'})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=next_page_url, listitem=next_li, isFolder=True)

    if params.get('type') == 'series':
        _end_directory_with_view(content='tvshows', view='List', delay_ms=40, repeat=2)
    else:
        _end_directory_with_view(content='movies', view='List', delay_ms=40, repeat=2)

def list_seasons(params):
    # ... (sem alterações aqui)
    tmdb_id = params['id']
    seasons = tmdb.get_seasons(params['type'], tmdb_id)

    for season in seasons:
        name = season['name']
        url = build_url({
            'action': 'list_episodes',
            'type': params['type'],
            'id': tmdb_id,
            'season_number': str(season['season_number']),
            'poster': params['poster'],
            'name': params['name'],
            'year': params['year']
        })
        li = xbmcgui.ListItem(name)
        if season.get('poster'):
            li.setArt({'thumb': season['poster'], 'icon': season['poster'], 'poster': season['poster']})
        _set_listitem_info(li, 'video', {'title': name, 'plot': season.get('description', ''), 'year': int(season['year']) if season['year'] else 0, 'mediatype': 'video'})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)

    if params.get('type') == 'series':
        _end_directory_with_view(content='seasons', view='List', delay_ms=40, repeat=2)
    else:
        _end_directory_with_view(content='movies', view='List', delay_ms=40, repeat=2)

def list_episodes(params):
    # ... (sem alterações aqui, exceto a action no build_url)
    tmdb_id = params['id']
    season_number = params['season_number']
    episodes = tmdb.get_episodes(params['type'], tmdb_id, season_number)

    for episode in episodes:
        name = '{} - S{}E{}'.format(params['name'], season_number.zfill(2), str(episode['episode_number']).zfill(2))
        url = build_url({
            'action': 'list_streams', ## MODIFICADO: de 'audio' para 'list_streams'
            'type': params['type'],
            'id': tmdb_id,
            'season_number': season_number,
            'episode_number': str(episode['episode_number']),
            'name': params['name'],
            'poster': params['poster'],
            'year': params['year'],
            'fanart': params.get('fanart', '')
        })
        li = xbmcgui.ListItem(name)
        if episode.get('poster'):
            li.setArt({'thumb': episode['poster'], 'icon': episode['poster'], 'poster': episode['poster']})
        _set_listitem_info(li, 'video', {'title': name, 'plot': episode.get('description', ''), 'year': int(episode['year']) if episode['year'] else 0, 'mediatype': 'video'})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)

    if params.get('type') == 'series':
        _end_directory_with_view(content='episodes', view='WideList', delay_ms=40, repeat=2)
    else:
        _end_directory_with_view(content='movies', view='List', delay_ms=40, repeat=2)

def _stream_title_text(stream):
    return stream.get('title', stream.get('description', '')).replace('\n', ' ').replace('\t', '').replace('SKYFLIX API', 'Mega Portugal').strip()

def _stream_bad(stream_title, stream_url, raw_name):
    lowered_title = stream_title.lower()
    lowered_url = (stream_url or '').lower()
    lowered_name = (raw_name or '').lower()
    if 'showbox' in lowered_name:
        return True
    if 'pixeldrain.dev' in lowered_url or 'hindi' in lowered_url:
        return True
    if 'beta' in lowered_url and '.top' in lowered_url:
        return True
    if 'hindi' in lowered_title:
        return True
    return False


def _stream_is_download_only(stream):
    """Identifica rótulos explícitos de fonte para download, sem excluí-la do menu.

    Algumas fontes ainda retornam um vídeo válido, então não são apagadas. Elas
    apenas deixam de ganhar prioridade automática e ficam no final da lista.
    """
    values = (
        stream.get('name', '') or '',
        stream.get('title', '') or '',
        stream.get('description', '') or '',
    )
    text = ' '.join(values).lower().replace('_', ' ')
    markers = (
        'download only', 'download-only', 'somente download', 'apenas download',
        'para download', 'baixar somente', '10gbps download',
    )
    return any(marker in text for marker in markers)

_QUALITY_TOKEN_PATTERNS = {
    '4k': (r'(^|[^0-9a-z])(4k|2160p?|uhd)([^0-9a-z]|$)',),
    '1080': (r'(^|[^0-9a-z])(1080p?|fhd)([^0-9a-z]|$)',),
    '720': (r'(^|[^0-9a-z])(720p?)([^0-9a-z]|$)',),
    'light': (r'(^|[^0-9a-z])(480p?|360p?|sd)([^0-9a-z]|$)',),
}
_DIRECT_MEDIA_HINTS = ('.m3u8', '.mp4', '.mkv', '.webm', '.mpd', '.avi', '.mov', '.m4v', '.ts', '/hls/', '/mp4/', '/mkv/', '/webm/', '/dash/')


def _encode_stream_payload(payload):
    raw = json.dumps(payload, separators=(',', ':'))
    encoded = base64.urlsafe_b64encode(raw.encode('utf-8')).decode('ascii')
    return encoded.rstrip('=')


def _stream_catalog_url(imdb_id, type_, season=None, episode=None, qualities=None, sort='desc', content_tags=None, force_manifest_token=False):
    normalized_qualities = list(qualities) if qualities else []
    normalized_tags = list(content_tags) if content_tags else []
    use_manifest_token = force_manifest_token or (
        sort == 'desc' and
        normalized_qualities == _HDHUB_DEFAULT_QUALITIES and
        normalized_tags == _HDHUB_DEFAULT_CONTENT_TAGS
    )
    if use_manifest_token:
        token = _HDHUB_MANIFEST_FIXED_TOKEN
    else:
        payload = {'torbox': 'unset', 'sort': sort}
        if normalized_qualities:
            payload['qualities'] = ','.join(normalized_qualities)
        if normalized_tags:
            payload['content'] = ','.join(normalized_tags)
        token = _encode_stream_payload(payload)
    if type_ == 'series':
        return 'https://hdhub.thevolecitor.qzz.io/{}/stream/series/{}:{}:{}.json'.format(token, imdb_id, season, episode)
    return 'https://hdhub.thevolecitor.qzz.io/{}/stream/movie/{}.json'.format(token, imdb_id)


_HDHUB_MANIFEST_FIXED_TOKEN = 'eyJ0b3Jib3giOiJ1bnNldCIsInF1YWxpdGllcyI6IjIxNjBwLDEwODBwLDcyMHAsNDgwcCIsInNvcnQiOiJkZXNjIiwiY29udGVudCI6ImxhdGluLGFzaWFuIn0'
_HDHUB_DEFAULT_CONTENT_TAGS = ['latin', 'asian']
_HDHUB_DEFAULT_QUALITIES = ['2160p', '1080p', '720p', '480p']


def _hdhub_content_tags():
    return list(_HDHUB_DEFAULT_CONTENT_TAGS)


def _legacy_fixed_stream_catalog_url(imdb_id, type_, season=None, episode=None):
    return _stream_catalog_url(
        imdb_id,
        type_,
        season=season,
        episode=episode,
        qualities=_HDHUB_DEFAULT_QUALITIES,
        sort='desc',
        content_tags=_HDHUB_DEFAULT_CONTENT_TAGS,
        force_manifest_token=True
    )


def _fetch_streams_from_catalog(url, audit_label):
    try:
        r = HTTP_SESSION.get(url, headers={'User-Agent': DEFAULT_USER_AGENT, 'Accept': 'application/json'}, timeout=DEFAULT_TIMEOUT)
        status = getattr(r, 'status_code', 0)
        r.raise_for_status()
        data = r.json()
        streams = data.get('streams', []) or []
        _log_debug('Catálogo {} retornou {} streams (HTTP {}).'.format(audit_label, len(streams), status))
        return streams
    except requests.exceptions.RequestException as e:
        status = getattr(getattr(e, 'response', None), 'status_code', 'sem-status')
        _log_debug('Erro ao buscar streams [{}|HTTP {}]: {}'.format(audit_label, status, e))
    except ValueError as e:
        _log_debug('JSON inválido ao buscar streams [{}]: {}'.format(audit_label, e))
    return []


def _stream_request_profiles(type_):
    autoplay_enabled = _setting_bool('autoplaystream', False)
    mode = _stream_priority_mode()
    if autoplay_enabled:
        if mode == '1':
            return [
                ('best-2160', ['2160p']),
                ('best-1080', ['1080p']),
                ('best-720', ['720p']),
                ('best-480', ['480p']),
                ('best-manifest-default', _HDHUB_DEFAULT_QUALITIES),
                ('best-unfiltered', None),
            ]
        if mode == '2':
            return [
                ('light-480', ['480p']),
                ('light-720', ['720p']),
                ('light-1080', ['1080p']),
                ('light-2160', ['2160p']),
                ('light-manifest-default', _HDHUB_DEFAULT_QUALITIES),
                ('light-unfiltered', None),
            ]
        return [
            ('auto-1080-720', ['1080p', '720p']),
            ('auto-2160', ['2160p']),
            ('auto-480', ['480p']),
            ('auto-manifest-default', _HDHUB_DEFAULT_QUALITIES),
            ('auto-unfiltered', None),
        ]
    return [
        ('manual-manifest-default', _HDHUB_DEFAULT_QUALITIES),
        ('manual-1080-720', ['1080p', '720p']),
        ('manual-2160', ['2160p']),
        ('manual-480', ['480p']),
        ('manual-unfiltered', None),
    ]


def _collect_streams_for_mode(imdb_id, type_, season=None, episode=None):
    profiles = _stream_request_profiles(type_)
    merged = []
    seen = set()
    audit_rows = []

    def _merge_stream_batch(streams, request_rank, label, qualities):
        added = 0
        for stream in streams:
            stream_url = stream.get('url') or ''
            dedupe_key = stream_url.strip() or '{}|{}|{}'.format(stream.get('name', ''), stream.get('title', ''), stream.get('description', ''))
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            stream_copy = dict(stream)
            stream_copy['__request_label'] = label
            stream_copy['__request_rank'] = request_rank
            stream_copy['__request_qualities'] = list(qualities) if qualities else []
            merged.append(stream_copy)
            added += 1
        return added

    manifest_default_attempted = False
    for request_rank, (label, qualities) in enumerate(profiles):
        is_manifest_default = list(qualities or []) == _HDHUB_DEFAULT_QUALITIES
        if is_manifest_default:
            manifest_default_attempted = True
        url = _stream_catalog_url(
            imdb_id,
            type_,
            season=season,
            episode=episode,
            qualities=qualities,
            content_tags=_hdhub_content_tags(),
            force_manifest_token=is_manifest_default
        )
        streams = _fetch_streams_from_catalog(url, label)
        audit_rows.append('{}={}'.format(label, len(streams)))
        _merge_stream_batch(streams, request_rank, label, qualities)

    # Compatibilidade herdada da 3.3.0: se nenhuma consulta devolver resultado
    # e o perfil padrão oficial ainda não tiver sido tentado, usa o token fixo
    # clássico apenas como último fallback de catálogo.
    if not merged and not manifest_default_attempted:
        legacy_label = 'manifest-fixed-105'
        legacy_url = _legacy_fixed_stream_catalog_url(imdb_id, type_, season=season, episode=episode)
        legacy_streams = _fetch_streams_from_catalog(legacy_url, legacy_label)
        audit_rows.append('{}={}'.format(legacy_label, len(legacy_streams)))
        _merge_stream_batch(legacy_streams, len(profiles), legacy_label, _HDHUB_DEFAULT_QUALITIES)

    if audit_rows:
        _log_debug('Auditoria catálogo [{}] => {}'.format('autoplay' if _setting_bool('autoplaystream', False) else 'manual', ', '.join(audit_rows)))
    return merged


def _log_stream_inventory(streams, stage_label):
    explicit = direct_unknown = opaque = auto_like = dublado_like = 0
    for stream in streams:
        info = _stream_quality_info(stream)
        if info['quality_key']:
            explicit += 1
        elif info['is_direct_media']:
            direct_unknown += 1
        else:
            opaque += 1
        if info['is_auto']:
            auto_like += 1
        if info['is_dublado']:
            dublado_like += 1
    _log_debug('Inventário {} -> total={} explicitas={} diretas_indefinidas={} opacas={} auto_like={} dublado_like={}'.format(stage_label, len(streams), explicit, direct_unknown, opaque, auto_like, dublado_like))


def _stream_quality_match(scan_text):
    scan_text = (scan_text or '').lower()
    for quality_key in ('4k', '1080', '720', 'light'):
        for pattern in _QUALITY_TOKEN_PATTERNS[quality_key]:
            if re.search(pattern, scan_text):
                return quality_key
    return None


def _stream_quality_info(stream):
    name = stream.get('name', '') or ''
    desc = stream.get('description', '') or ''
    title = stream.get('title', '') or ''
    url = stream.get('url', '') or ''
    parsed = urlparse(url)
    host = (parsed.netloc or '').lower()
    path = unquote_plus((parsed.path or '').lower())
    combined = '{} {} {}'.format(name, desc, title).strip().lower()
    quality_key = _stream_quality_match('{} {}'.format(combined, path))
    is_auto = '- auto' in combined or '(auto)' in combined or combined.endswith(' auto')
    is_dublado = 'dublado' in combined
    is_direct_media = any(hint in path for hint in _DIRECT_MEDIA_HINTS)
    is_opaque = not is_direct_media and quality_key is None
    if quality_key == '4k':
        label = '4K'
    elif quality_key == '1080':
        label = '1080p'
    elif quality_key == '720':
        label = '720p'
    elif quality_key == 'light':
        label = '480/360/SD'
    elif is_direct_media:
        label = 'indefinida-direta'
    elif is_auto:
        label = 'auto-opaca'
    elif is_dublado:
        label = 'dublado-opaca'
    else:
        label = 'indefinida-opaca'
    return {
        'combined': combined,
        'host': host,
        'path': path,
        'quality_key': quality_key,
        'quality_label': label,
        'is_auto': is_auto,
        'is_dublado': is_dublado,
        'is_direct_media': is_direct_media,
        'is_opaque': is_opaque,
    }


def _stream_name_type(stream):
    info = _stream_quality_info(stream)
    if _stream_is_download_only(stream):
        return '[B][COLOR gray](Download)[/COLOR][/B]'
    if info['quality_key'] == '4k':
        return '[B][COLOR orange](4K)[/COLOR][/B]'
    if info['quality_key'] == '1080':
        return '[B][COLOR gold](1080p)[/COLOR][/B]'
    if info['quality_key'] == '720':
        return '[B][COLOR deepskyblue](720p)[/COLOR][/B]'
    if info['quality_key'] == 'light':
        return '[B][COLOR springgreen](480/360p)[/COLOR][/B]'
    if info['is_dublado']:
        return '[B][COLOR yellow](Dublado)[/COLOR][/B]'
    if info['is_direct_media']:
        return '[B][COLOR silver](Indefinida)[/COLOR][/B]'
    return '[B][COLOR gray](Não auditável)[/COLOR][/B]'


def _stream_request_rank(stream):
    try:
        return int(stream.get('__request_rank', 999))
    except Exception:
        return 999


def _stream_request_label(stream):
    return stream.get('__request_label', 'sem-perfil')


def _stream_priority_bucket(stream, mode=None):
    info = _stream_quality_info(stream)
    mode = _stream_priority_mode() if mode is None else mode
    quality_key = info['quality_key']

    # Fonte explicitamente marcada como download continua acessível no menu,
    # porém não disputa prioridade com streams para reprodução.
    if _stream_is_download_only(stream):
        return 90 + (_stream_request_rank(stream) / 100.0)

    if quality_key:
        if mode == '1':
            explicit_order = {'4k': 0, '1080': 1, '720': 2, 'light': 3}
        elif mode == '2':
            explicit_order = {'light': 0, '720': 1, '1080': 2, '4k': 3}
        else:
            explicit_order = {'1080': 0, '720': 1, '4k': 2, 'light': 3}
        return explicit_order.get(quality_key, 9)

    request_rank = _stream_request_rank(stream)
    if info['is_direct_media']:
        base = 10 if info['is_auto'] else 11 if info['is_dublado'] else 12
        return base + (request_rank / 100.0)
    base = 20 if info['is_auto'] else 21 if info['is_dublado'] else 22
    return base + (request_rank / 100.0)


def _stream_sort_key(stream):
    info = _stream_quality_info(stream)
    autoplay_enabled = _setting_bool('autoplaystream', False)
    if autoplay_enabled:
        return (_stream_priority_bucket(stream), _stream_request_rank(stream), info['host'], info['combined'])
    return (_stream_priority_bucket(stream), info['host'], info['combined'])


def _stream_priority_mode_label(mode=None):
    mode = _stream_priority_mode() if mode is None else mode
    if mode == '1':
        return 'Melhor qualidade'
    if mode == '2':
        return 'Mais leve'
    return 'Automático'


def _log_stream_priority_preview(streams, mode=None, limit=8):
    mode = _stream_priority_mode() if mode is None else mode
    mode_label = _stream_priority_mode_label(mode)
    preview = []
    for stream in streams[:limit]:
        info = _stream_quality_info(stream)
        bucket = _stream_priority_bucket(stream, mode)
        host = info['host'] or 'sem-host'
        preview.append('[{}|{}|{}|{}] {}'.format(bucket, _stream_request_label(stream), info['quality_label'], host, info['combined'][:100]))
    if preview:
        _log_debug('Ordenação de streams ({}) => {}'.format(mode_label, ' | '.join(preview)))


def _build_autoplay_candidates(valid_streams, params, type_):
    candidates = []
    for stream in valid_streams:
        stream_url = stream.get('url')
        if not stream_url:
            continue
        # Não inicia autoplay em fonte declarada pelo próprio catálogo como
        # download. Ela continua disponível para escolha manual no fim da lista.
        if _stream_is_download_only(stream):
            _log_debug('Autoplay ignorou fonte marcada como download: {}'.format(_stream_title_text(stream)))
            continue
        stream_title = _stream_title_text(stream)
        stream_info = _stream_quality_info(stream)
        item_name = _build_stream_item_name(params, type_, stream_title, _stream_name_type(stream))
        candidates.append({
            'url': stream_url,
            'title': stream_title,
            'name': item_name,
            'quality_label': stream_info['quality_label'],
            'bucket': _stream_priority_bucket(stream),
            'host': stream_info['host'],
            'request_label': _stream_request_label(stream),
            'request_rank': _stream_request_rank(stream),
            'is_direct_media': stream_info['is_direct_media'],
            'is_opaque': stream_info['is_opaque'],
        })
    return candidates

def _build_stream_item_name(params, type_, stream_title, name_type):
    if type_ == 'series':
        if name_type:
            return '{} - S{}E{} {} ({})'.format(params['name'], params['season_number'].zfill(2), params['episode_number'].zfill(2), name_type, stream_title)
        return '{} - S{}E{} ({})'.format(params['name'], params['season_number'].zfill(2), params['episode_number'].zfill(2), stream_title)
    if name_type:
        return '{} ({}) {} - {}'.format(params['name'], params['year'], name_type, stream_title)
    return '{} ({}) - {}'.format(params['name'], params['year'], stream_title)

def _build_play_params(params, imdb_id, type_, stream_url, item_name):
    play_params = {
        'action': 'play_item_with_subtitles',
        'video_url': stream_url,
        'imdb_id': imdb_id,
        'type': type_,
        'name': item_name,
        'poster': params['poster'],
        'fanart': params.get('fanart', '')
    }
    if type_ == 'series':
        play_params['season'] = params['season_number']
        play_params['episode'] = params['episode_number']
    return play_params

## MODIFICADO: A função 'audio_options' foi renomeada e completamente reescrita
def list_streams(params):    
    type_ = params['type']
    id_ = params['id']
    imdb_id = tmdb.get_imdb_id_tmdb(id_, type_)

    if not imdb_id:
        _show_error('Não foi possível encontrar o ID do IMDB para este item.', title='Erro', dialog=True)
        return

    streams = _collect_streams_for_mode(
        imdb_id,
        type_,
        season=params.get('season_number'),
        episode=params.get('episode_number')
    )
    if not streams:
        _show_error('Não foi possível obter a lista de streams. A fonte pode estar offline.', title='Erro', dialog=True)
        return

    _log_stream_inventory(streams, 'bruto')

    valid_streams = []
    for stream in streams:
        stream_url = stream.get('url')
        if not stream_url:
            continue
        stream_title = _stream_title_text(stream)
        #if _stream_bad(stream_title, stream_url, stream.get('name', '')):
        #    continue
        valid_streams.append(stream)

    if not valid_streams:
        _show_error('Nenhum stream válido foi encontrado após os filtros.', title='Sem Fontes', dialog=True)
        return

    valid_streams.sort(key=_stream_sort_key)
    _log_debug('Streams válidos após filtros: {}'.format(len(valid_streams)))
    _log_stream_inventory(valid_streams, 'validos')
    _log_stream_priority_preview(valid_streams)

    if _setting_bool('autoplaystream', False):
        autoplay_candidates = _build_autoplay_candidates(valid_streams, params, type_)
        if not autoplay_candidates:
            _show_error('Nenhuma fonte reproduzível foi encontrada para o autoplay.', title='Sem Fontes', dialog=True)
            return
        first_candidate = autoplay_candidates[0]
        _notify('Mega Portugal', 'Autoplay ativado: tentando fontes por prioridade.', xbmcgui.NOTIFICATION_INFO, 2400)
        autoplay_params = _build_play_params(params, imdb_id, type_, first_candidate.get('url'), first_candidate.get('name'))
        autoplay_params['direct_play'] = '1'
        autoplay_params['autoplay_candidates'] = autoplay_candidates
        play_item_with_subtitles(autoplay_params)
        return

    for stream in valid_streams:
        stream_title = _stream_title_text(stream)
        stream_url = stream.get('url')
        item_name = _build_stream_item_name(params, type_, stream_title, _stream_name_type(stream))
        play_params = _build_play_params(params, imdb_id, type_, stream_url, item_name)
        url_to_play = build_url(play_params)

        li = xbmcgui.ListItem(item_name)
        li.setArt({'thumb': params['poster'], 'icon': params['poster'], 'fanart': params.get('fanart', '')})
        _set_listitem_info(li, 'video', {'title': item_name, 'plot': stream_title})
        li.setProperty('IsPlayable', 'true')
        _set_listitem_info(li, 'video', {'mediatype': 'video'})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url_to_play, listitem=li, isFolder=False)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)
    xbmcplugin.setContent(ADDON_HANDLE, 'videos')
    setview('WideList')

def get_and_download_subtitles(imdb_id, type_, season=None, episode=None):
    try:
        subtitle_paths = subtitles.get_and_download_subtitles(
            imdb_id,
            type_,
            season=season,
            episode=episode,
            addon=ADDON,
            clear_before=True
        )
    except Exception as exc:
        _log_debug('Erro ao buscar legendas: {}'.format(exc))
        subtitle_paths = []

    if not subtitle_paths and _setting_bool('legendasauto', True) and _setting_bool('notificarsemlegenda', False):
        _notify('Mega Portugal', 'Nenhuma legenda encontrada.', xbmcgui.NOTIFICATION_INFO, 2500)

    return subtitle_paths


def _is_adaptive_manifest_url(video_url, label=''):
    try:
        probe = '{} {}'.format(video_url or '', label or '').lower()
    except Exception:
        probe = ''
    return ('.m3u8' in probe or '.mpd' in probe or 'application/dash+xml' in probe)


def _parse_http_content_range(value):
    """Converte Content-Range em (inicio, fim, total) sem aceitar valores ambíguos."""
    try:
        match = re.match(r'^\s*bytes\s+(\d+)\s*-\s*(\d+)\s*/\s*(\d+)\s*$', value or '', re.I)
        if not match:
            return None
        start, end, total = [int(piece) for piece in match.groups()]
        if start < 0 or end < start or total <= end:
            return None
        return start, end, total
    except Exception:
        return None


def _vod_range_cache_key(video_url):
    try:
        return (video_url or '').strip()
    except Exception:
        return ''


def _vod_range_cache_get(video_url):
    key = _vod_range_cache_key(video_url)
    now = time.time()
    try:
        item = _VOD_RANGE_PREFLIGHT_CACHE.get(key)
        if not item or item.get('until', 0) <= now:
            _VOD_RANGE_PREFLIGHT_CACHE.pop(key, None)
            return None
        return bool(item.get('ok')), item.get('reason', '')
    except Exception:
        return None


def _vod_range_cache_put(video_url, ok, reason=''):
    key = _vod_range_cache_key(video_url)
    if not key:
        return
    try:
        ttl = VOD_RANGE_PREFLIGHT_TTL_OK if ok else VOD_RANGE_PREFLIGHT_TTL_UNSAFE
        _VOD_RANGE_PREFLIGHT_CACHE[key] = {
            'ok': bool(ok),
            'reason': reason or '',
            'until': time.time() + ttl,
        }
    except Exception:
        pass


def _vod_probe_range(video_url, range_value):
    """Consulta somente cabeçalhos da faixa solicitada, sem passar mídia pelo proxy."""
    response = None
    try:
        response = HTTP_SESSION.get(
            video_url,
            headers={
                'User-Agent': DEFAULT_USER_AGENT,
                'Accept': '*/*',
                'Accept-Encoding': 'identity',
                'Connection': 'close',
                'Range': range_value,
            },
            timeout=VOD_RANGE_PREFLIGHT_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )
        status = int(getattr(response, 'status_code', 0) or 0)
        content_type = (getattr(response, 'headers', {}).get('Content-Type', '') or '').lower()
        content_range = (getattr(response, 'headers', {}).get('Content-Range', '') or '')
        # Não basta o servidor anunciar 206/Content-Range: algumas origens
        # problemáticas devolvem o cabeçalho e encerram o corpo em EOF. Ler
        # um único byte valida que aquela faixa realmente pode ser consumida.
        body_started = False
        for chunk in response.iter_content(chunk_size=1):
            if chunk:
                body_started = True
                break
        return status, content_type, _parse_http_content_range(content_range), body_started
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass


def _prepare_tmdb_direct_playback_url(video_url, label=''):
    """Bloqueia somente arquivo TMDB direto cuja origem comprovadamente quebra Range.

    Pluto permanece fora desta rota porque resolve HLS/InputStream em funções
    próprias. Fontes saudáveis continuam indo diretamente ao VideoPlayer, sem
    encapsular dados no proxy local nem alterar suas URLs/headers.
    """
    try:
        raw_url = (video_url or '').strip()
        parsed = urlparse(raw_url)
    except Exception:
        return ''
    if not raw_url:
        return ''
    if (parsed.scheme or '').lower() not in ('http', 'https'):
        return raw_url
    if _is_adaptive_manifest_url(raw_url, label):
        return raw_url

    cached = _vod_range_cache_get(raw_url)
    if cached is not None:
        ok, reason = cached
        if not ok:
            _log_debug('Fonte TMDB direta mantida fora do player por Range inseguro em cache: {}'.format(reason or 'invalido'))
            return ''
        return raw_url

    try:
        status, content_type, first_range, first_body = _vod_probe_range(raw_url, 'bytes=0-0')
    except requests.RequestException as exc:
        # Falha transitória de preflight não prova Range inválido. Mantemos a
        # compatibilidade da 3.3.4 e deixamos o player tentar a fonte direta.
        _log_debug('Pré-teste de Range indisponível; mantendo fonte direta: {}'.format(exc.__class__.__name__))
        return raw_url
    except Exception as exc:
        _log_debug('Pré-teste de Range inesperado; mantendo fonte direta: {}'.format(exc.__class__.__name__))
        return raw_url

    if content_type.startswith(('text/', 'application/json', 'application/xml', 'text/html')):
        _vod_range_cache_put(raw_url, False, 'conteudo-nao-video')
        return ''
    if status != 206 or not first_range or first_range[0] != 0 or not first_body:
        _vod_range_cache_put(raw_url, False, 'range-inicial-invalido')
        return ''

    _start, _end, total = first_range
    # MKV/WebM costuma consultar o índice no final. Reproduzimos essa busca
    # antes de abrir o player; se a origem devolver EOF/faixa errada, ela é
    # recusada aqui e não durante o Stop do Kodi.
    if total > 1:
        tail = total - 1
        try:
            status, _ctype_tail, final_range, tail_body = _vod_probe_range(raw_url, 'bytes={0}-{0}'.format(tail))
        except requests.RequestException as exc:
            _log_debug('Pré-teste final indisponível; mantendo fonte direta: {}'.format(exc.__class__.__name__))
            return raw_url
        except Exception as exc:
            _log_debug('Pré-teste final inesperado; mantendo fonte direta: {}'.format(exc.__class__.__name__))
            return raw_url
        if status != 206 or not final_range or final_range[0] != tail or final_range[2] != total or not tail_body:
            _vod_range_cache_put(raw_url, False, 'range-final-invalido')
            return ''

    _vod_range_cache_put(raw_url, True, '')
    return raw_url

def _resolved_failure():
    try:
        failed = xbmcgui.ListItem()
        xbmcplugin.setResolvedUrl(handle=ADDON_HANDLE, succeeded=False, listitem=failed)
    except Exception:
        pass


def _build_runtime_play_item(params, video_url, name, subtitle_paths=None):
    li = xbmcgui.ListItem(name)
    li.setArt({'thumb': params.get('poster'), 'icon': params.get('poster'), 'fanart': params.get('fanart')})
    _set_listitem_info(li, 'video', {'title': name})
    li.setPath(video_url)
    if subtitle_paths:
        try:
            li.setSubtitles(subtitle_paths)
        except Exception:
            pass
    return li


def _player_is_playing(player):
    try:
        return bool(player.isPlaying())
    except Exception:
        return False


def _wait_for_playback_start(player, monitor, timeout_ms=AUTOPLAY_START_TIMEOUT_MS):
    deadline = time.time() + (max(timeout_ms, 0) / 1000.0)
    while time.time() < deadline:
        if monitor.abortRequested():
            return False
        if _player_is_playing(player):
            return True
        xbmc.sleep(AUTOPLAY_POLL_MS)
    return _player_is_playing(player)


def _apply_subtitles_when_ready(player, subtitle_paths):
    if not subtitle_paths:
        return
    for _ in range(40):
        xbmc.sleep(150)
        try:
            if player.isPlaying():
                player.setSubtitles(subtitle_paths[0])
                return
        except Exception:
            return


def _autoplay_preflight_result(url):
    parsed = urlparse(url or '')
    scheme = (parsed.scheme or '').lower()
    if scheme not in ('http', 'https'):
        return None

    try:
        response = HTTP_SESSION.get(
            url,
            headers={'User-Agent': DEFAULT_USER_AGENT},
            timeout=AUTOPLAY_PREFLIGHT_TIMEOUT,
            allow_redirects=True,
            stream=True
        )
        status = int(getattr(response, 'status_code', 0) or 0)
        final_url = getattr(response, 'url', url) or url
        try:
            response.close()
        except Exception:
            pass
        return {'status': status, 'url': final_url}
    except requests.RequestException as exc:
        _log_debug('Autoplay preflight falhou para {}: {}'.format(url, exc))
        return None
    except Exception as exc:
        _log_debug('Autoplay preflight inesperado para {}: {}'.format(url, exc))
        return None


def _autoplay_should_skip_preflight(candidate_url):
    probe = _autoplay_preflight_result(candidate_url)
    if not probe:
        return False, None, None
    status = probe.get('status')
    final_url = probe.get('url')
    if status in AUTOPLAY_PREFLIGHT_SKIP_CODES:
        return True, status, final_url
    return False, status, final_url


def _wait_for_playback_stability(player, monitor, stable_ms=AUTOPLAY_STABLE_PLAY_MS):
    if stable_ms <= 0:
        return _player_is_playing(player)
    deadline = time.time() + (stable_ms / 1000.0)
    while time.time() < deadline:
        if monitor.abortRequested():
            return False
        if not _player_is_playing(player):
            return False
        xbmc.sleep(AUTOPLAY_POLL_MS)
    return _player_is_playing(player)


def _play_with_autoplay_fallback(params, subtitle_paths, candidates):
    player = xbmc.Player()
    monitor = xbmc.Monitor()
    total = len(candidates)
    failed_hosts = {}

    def _candidate_host(url):
        try:
            return (urlparse(url or '').netloc or '').lower()
        except Exception:
            return ''

    def _mark_failed_host(host, reason):
        if not host:
            return
        failed_hosts[host] = reason
        _log_debug('Autoplay marcou host como problemático nesta sessão: {} ({})'.format(host, reason))

    for index, candidate in enumerate(candidates, 1):
        candidate_url = candidate.get('url')
        candidate_name = candidate.get('name') or params.get('name', 'Reproduzindo')
        if not candidate_url:
            continue
        candidate_host = _candidate_host(candidate_url)
        if candidate_host and candidate_host in failed_hosts:
            _log_debug('Autoplay pulou tentativa {}/{} no host {} por falha anterior nesta execução ({})'.format(
                index,
                total,
                candidate_host,
                failed_hosts.get(candidate_host)
            ))
            continue

        _log_debug('Autoplay tentativa {}/{} -> bucket={} perfil={} rank={} quality={} direct={} opaque={} host={} url={}'.format(
            index,
            total,
            candidate.get('bucket'),
            candidate.get('request_label'),
            candidate.get('request_rank'),
            candidate.get('quality_label'),
            candidate.get('is_direct_media'),
            candidate.get('is_opaque'),
            candidate.get('host'),
            candidate_url
        ))

        try:
            player.stop()
        except Exception:
            pass
        xbmc.sleep(AUTOPLAY_RETRY_SETTLE_MS if index > 1 else 150)

        skip_candidate, preflight_status, preflight_url = _autoplay_should_skip_preflight(candidate_url)
        if skip_candidate:
            _mark_failed_host(candidate_host, 'preflight-{}'.format(preflight_status))
            _log_debug('Autoplay preflight pulou tentativa {}/{} por status {} -> {}'.format(index, total, preflight_status, preflight_url or candidate_url))
            if index < total:
                _notify('Mega Portugal', 'Fonte recusada pelo servidor. Tentando a próxima...', xbmcgui.NOTIFICATION_WARNING, 2200)
                continue
            break

        playback_url = _prepare_tmdb_direct_playback_url(candidate_url, candidate_name)
        if not playback_url:
            _mark_failed_host(candidate_host, 'range-inseguro')
            _log_debug('Autoplay pulou tentativa {}/{}: origem direta sem Range seguro.'.format(index, total))
            if index < total:
                _notify('Mega Portugal', 'Fonte incompatível com reprodução segura. Tentando a próxima...', xbmcgui.NOTIFICATION_WARNING, 2400)
                continue
            break

        li = _build_runtime_play_item(params, playback_url, candidate_name, subtitle_paths=subtitle_paths)

        try:
            player.play(item=playback_url, listitem=li)
        except Exception as exc:
            _log_debug('Falha ao iniciar autoplay na tentativa {}/{}: {}'.format(index, total, exc))
            if index < total:
                _notify('Mega Portugal', 'Fonte indisponível. Tentando a próxima...', xbmcgui.NOTIFICATION_WARNING, 2300)
                continue
            break

        started = _wait_for_playback_start(player, monitor)
        if not started:
            _log_debug('Autoplay sem início de reprodução na tentativa {}/{}.'.format(index, total))
            _mark_failed_host(candidate_host, 'no-start')
            try:
                player.stop()
            except Exception:
                pass
            if index < total:
                _notify('Mega Portugal', 'Fonte offline. Tentando a próxima...', xbmcgui.NOTIFICATION_WARNING, 2300)
                continue
            break

        _apply_subtitles_when_ready(player, subtitle_paths)

        if _wait_for_playback_stability(player, monitor):
            if index > 1:
                _notify('Mega Portugal', 'Fonte alternativa iniciada com sucesso.', xbmcgui.NOTIFICATION_INFO, 2200)
            return True

        _log_debug('Autoplay detectou queda precoce na tentativa {}/{}.'.format(index, total))
        _mark_failed_host(candidate_host, 'early-drop')
        try:
            player.stop()
        except Exception:
            pass
        if index < total:
            _notify('Mega Portugal', 'Fonte instável. Tentando a próxima...', xbmcgui.NOTIFICATION_WARNING, 2300)
            continue
        break

    _show_error('Nenhuma fonte iniciou a reprodução com sucesso. Tente abrir manualmente a lista de fontes.', title='Autoplay', dialog=True)
    return False


def play_item_with_subtitles(params):
    video_url = params.get('video_url')
    name = params.get('name', 'Reproduzindo')
    imdb_id = params.get('imdb_id')
    type_ = params.get('type')

    subtitle_paths = []
    dialog = None
    if _setting_bool('legendasauto', True):
        dialog = xbmcgui.DialogProgress()
        dialog.create('Mega Portugal', 'Buscando legendas...')
        dialog.update(50)

        subtitle_paths = get_and_download_subtitles(
            imdb_id,
            type_,
            params.get('season'),
            params.get('episode')
        )

        try:
            dialog.close()
        except Exception:
            pass

    if params.get('direct_play') == '1':
        autoplay_candidates = params.get('autoplay_candidates') or []
        if autoplay_candidates:
            _play_with_autoplay_fallback(params, subtitle_paths, autoplay_candidates)
            return

    playback_url = _prepare_tmdb_direct_playback_url(video_url, name)
    if not playback_url:
        _show_error('Esta fonte não aceita busca segura para VOD. Escolha outra qualidade ou servidor.', title='Fonte incompatível', dialog=True)
        _resolved_failure()
        return

    li = _build_runtime_play_item(params, playback_url, name, subtitle_paths=subtitle_paths)

    if params.get('direct_play') == '1':
        player = xbmc.Player()
        player.play(item=playback_url, listitem=li)
        _apply_subtitles_when_ready(player, subtitle_paths)
        return

    xbmcplugin.setResolvedUrl(handle=ADDON_HANDLE, succeeded=True, listitem=li) # Forma correta para itens "IsPlayable=true"

def _movie_streams_params(params, meta):
    return {
        'action': 'list_streams',
        'type': params['type'],
        'id': params['id'],
        'name': meta['name'],
        'poster': meta['poster'],
        'year': meta['year'],
        'fanart': params.get('fanart', meta['background'])
    }


def _render_movie_details_menu(params, meta):
    content_label = 'Ver fontes'
    content_plot = 'Abrir a lista de fontes disponíveis para este filme.'
    stream_params = _movie_streams_params(params, meta)
    item_name = '{} ({})'.format(meta['name'], meta['year']) if meta['year'] else meta['name']

    li_streams = xbmcgui.ListItem(content_label)
    _set_listitem_info(li_streams, 'video', {'title': content_label, 'plot': content_plot, 'mediatype': 'video'})
    _set_listitem_info(li_streams, type="Video", infoLabels={"Title": content_label, "Plot": content_plot})
    li_streams.setArt({'thumb': meta.get('poster'), 'icon': meta.get('poster'), 'poster': meta.get('poster'), 'fanart': meta.get('background')})
    xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=build_url(stream_params), listitem=li_streams, isFolder=True)

    info_url = build_url({'action': 'show_text_dialog', 'title': item_name, 'text': meta.get('description', '')})
    li_info = xbmcgui.ListItem('Sinopse')
    _set_listitem_info(li_info, 'video', {'title': item_name, 'plot': meta.get('description', ''), 'mediatype': 'video'})
    _set_listitem_info(li_info, type="Video", infoLabels={"Title": item_name, "Plot": meta.get('description', '')})
    li_info.setArt({'thumb': meta.get('poster'), 'icon': meta.get('poster'), 'poster': meta.get('poster'), 'fanart': meta.get('background')})
    xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=info_url, listitem=li_info, isFolder=False)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')
    setview('List')


def show_details(params):
    meta = tmdb.get_meta(params['type'], params['id'])
    name = '{} ({})'.format(meta['name'], meta['year']) if meta['year'] else meta['name']
    if params['type'] == 'series':
        url = build_url({'action': 'list_seasons', 'type': params['type'], 'id': params['id'], 'name': meta['name'], 'poster': meta['poster'], 'year': meta['year'], 'fanart': params.get('fanart', meta['background'])})
        li = xbmcgui.ListItem(name)
        _set_listitem_info(li, 'video', {'title': name, 'plot': meta['description'], 'year': int(meta['year']) if meta['year'] else 0})
        li.setArt({'thumb': meta['poster'], 'poster': meta['poster'], 'fanart': meta['background']})
        _set_listitem_info(li, 'video', {'mediatype': 'video'})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)
        xbmcplugin.endOfDirectory(ADDON_HANDLE)
        xbmcplugin.setContent(ADDON_HANDLE, 'movies')
        setview('List')
    else: # movie
        if _movie_navigation_mode() == 'details':
            _render_movie_details_menu(params, meta)
            return
        play_params = _movie_streams_params(params, meta)
        list_streams(play_params)

def search(params):
    # ... (sem alterações aqui)
    if not params.get('query'):
        keyboard = xbmc.Keyboard('', 'Digite o nome')
        keyboard.doModal()
        if not keyboard.isConfirmed():
            return
        query = keyboard.getText()
        if not query:
            dialog = xbmcgui.Dialog()
            _show_error('Por favor, insira um termo de busca válido.', title='Erro', dialog=True)
            return
        if isinstance(query, text_type):
            query = query.encode('utf-8') if sys.version_info[0] == 2 else query
        params = {'action': 'search', 'query': query, 'page': '1'}
        search(params)
        return

    query = params.get('query')
    if not query:
        dialog = xbmcgui.Dialog()
        _show_error('Termo de busca inválido.', title='Erro', dialog=True)
        return     
    page = int(params.get('page', '1'))
    next_button = False
    search_types = ['movie', 'series']
    current_results = {}
    for type_ in search_types:
        items = tmdb.search(type_, query, page)
        current_results[type_] = items
        for item in items:
            fanart = item['background'] if item.get('background') else ''
            base_title = '{} ({})'.format(item['title'], item['year']) if item['year'] else item['title']
            prefix = 'Filme' if type_ == 'movie' else 'Série'
            title = '{}: {}'.format(prefix, base_title)
            url = build_url({'action': 'details', 'type': type_, 'id': item['id'], 'name': item['title'], 'year': item['year'], 'fanart': fanart})
            li = xbmcgui.ListItem(title)
            if item.get('poster') or item.get('background'):
                li.setArt({
                    'thumb': item['poster'] if item.get('poster') else None,
                    'poster': item['poster'] if item.get('poster') else None,
                    'fanart': item['background'] if item.get('background') else None
                })
            _set_listitem_info(li, 'video', {'title': title, 'plot': item.get('description', ''), 'year': int(item['year']) if item['year'] else 0, 'mediatype': 'video'})
            xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=url, listitem=li, isFolder=True)

    for type_ in search_types:
        if tmdb.search(type_, query, page + 1):
            next_button = True
            break

    next_page_url = build_url({
        'action': 'search',
        'query': query,
        'page': str(page + 1)
    })
    if next_button:
        next_li = xbmcgui.ListItem('[Próxima Página]')
        icon = TRANSLATE(os.path.join(homeDir, 'resources','images','proximo.png'))
        next_li.setArt({'icon': icon, 'thumb': icon, 'poster': icon, 'fanart': addonFanart})
        _set_listitem_info(next_li, 'video', {'mediatype': 'video'})
        xbmcplugin.addDirectoryItem(handle=ADDON_HANDLE, url=next_page_url, listitem=next_li, isFolder=True)

    xbmcplugin.endOfDirectory(ADDON_HANDLE)
    xbmcplugin.setContent(ADDON_HANDLE, 'movies')
    setview('List')     

def router(paramstring):
    params = dict(parse_qsl(paramstring, keep_blank_values=True))
    action = params.get('action')

    ## MODIFICADO: Atualização do roteador para as novas ações
    if action is None:
        start_mode = _setting_text('paginainicial', '0')
        if start_mode == '1':
            menu_tv()
        elif start_mode == '2':
            menu_movies()
        elif start_mode == '3':
            menu_series()
        else:
            home()
    elif action == 'menu_movies':
        menu_movies()
    elif action == 'tv':
        menu_tv()
    elif action == 'openm3u':
        openm3u(params)
    elif action == 'pluto_entry':
        pluto_entry(params)
    elif action == 'channels_pluto':
        channels_pluto(params, psid=(params.get('psid') or ''))
    elif action == 'channels_pluto_categories':
        channels_pluto_categories(params, psid=(params.get('psid') or ''))
    elif action == 'channels_pluto_category':
        channels_pluto_category(params, psid=(params.get('psid') or ''))
    elif action == 'play_pluto':
        play_pluto(params)
    elif action == 'pluto_movies':
        pluto_movies(params)
    elif action == 'pluto_series':
        pluto_series(params)
    elif action == 'pluto_vod_category':
        pluto_vod_category(params)
    elif action == 'pluto_vod_seasons':
        pluto_vod_seasons(params)
    elif action == 'pluto_vod_episodes':
        pluto_vod_episodes(params)
    elif action == 'play_pluto_vod':
        play_pluto_vod(params)
    elif action == 'opengroup':
        opengroup(params)
    elif action == 'play_proxy':
        play_proxy(params)
    elif action == 'show_full_epg':
        show_full_epg(params)
    elif action == 'noop':
        _complete_plugin_action(succeeded=True)
    elif action == 'menu_series':
        menu_series()
    elif action == 'list':
        list_items(params)
    elif action == 'details':
        show_details(params)
    elif action == 'search':
        search(params)
    elif action == 'open_settings':
        open_settings()
    elif action == 'clear_subtitles_cache':
        clear_subtitles_cache(notify=True)
        _complete_plugin_action(succeeded=True)
    elif action == 'clear_epg_cache':
        clear_epg_cache(notify=True)
        _complete_plugin_action(succeeded=True)
    elif action == 'clear_navigation_cache':
        clear_navigation_cache(notify=True)
        _complete_plugin_action(succeeded=True)
    elif action == 'clear_catalog_cache':
        clear_catalog_cache(notify=True)
        _complete_plugin_action(succeeded=True)
    elif action == 'clear_tv_history':
        clear_tv_history(notify=True)
        _complete_plugin_action(succeeded=True)
    elif action == 'show_text_dialog':
        _show_text_dialog(params.get('title', 'Mega Portugal'), params.get('text', ''))
        _complete_plugin_action(succeeded=True)
    elif action == 'list_seasons':
        list_seasons(params)
    elif action == 'list_episodes':
        list_episodes(params)
    elif action == 'list_streams': # Rota antiga 'audio' agora é 'list_streams'
        list_streams(params)
    elif action == 'play_item_with_subtitles': # Rota nova para reprodução com legendas
        play_item_with_subtitles(params)
    else:
        home()

if __name__ == '__main__':
    router(sys.argv[2][1:])