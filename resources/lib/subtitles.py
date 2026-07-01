# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import os

from resources.lib.common import (
    ensure_dir, make_session, log, DEFAULT_TIMEOUT, get_addon,
    get_setting_bool, get_setting_enum_value, log_debug
)

try:
    from kodi_six import xbmc, xbmcaddon, xbmcvfs
except ImportError:
    import xbmc
    import xbmcaddon
    import xbmcvfs

SESSION = make_session()
API_TEMPLATE = 'https://opensubtitles.stremio.homes/{lang}/ai-translated=false%7Cfrom=all%7CCauto-adjustment=true'


def _resolve_addon(addon=None):
    resolved = get_addon(addon)
    if resolved is not None:
        return resolved
    return xbmcaddon.Addon()


def _subtitles_log_debug(message, addon=None):
    log_debug(message, addon=addon, component='Subtitles')


def get_subtitle_dir(addon=None):
    addon = _resolve_addon(addon)
    profile_dir = xbmcvfs.translatePath(addon.getAddonInfo('profile'))
    return ensure_dir(os.path.join(profile_dir, 'subtitles'))


def get_subtitle_language_code(addon=None):
    addon = _resolve_addon(addon)
    return get_setting_enum_value('idioma_legenda', ['pt-br', 'en', 'es'], 'pt-br', addon=addon)


def get_subtitle_language_candidates(addon=None):
    addon = _resolve_addon(addon)
    preferred = get_subtitle_language_code(addon)
    fallback = get_setting_enum_value('fallback_legenda', ['', 'pt-pt', 'en', 'es'], '', addon=addon)
    candidates = []
    for code in (preferred, fallback):
        if code and code not in candidates:
            candidates.append(code)
    return candidates or ['pt-br']


def clear_subtitles_cache(addon=None):
    addon = _resolve_addon(addon)
    subtitle_dir = get_subtitle_dir(addon)
    removed = 0
    try:
        if os.path.isdir(subtitle_dir):
            for filename in os.listdir(subtitle_dir):
                path = os.path.join(subtitle_dir, filename)
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                        removed += 1
                except Exception as exc:
                    _subtitles_log_debug('Falha ao remover legenda {}: {}'.format(path, exc), addon)
    except Exception as exc:
        _subtitles_log_debug('Erro limpando cache de legendas: {}'.format(exc), addon)
    return removed


def build_subtitles_api_url(imdb_id, media_type, lang, season=None, episode=None):
    base = API_TEMPLATE.format(lang=lang)
    if media_type == 'series':
        return '{}/subtitles/series/{}:{}:{}.json'.format(base, imdb_id, season, episode)
    return '{}/subtitles/movie/{}.json'.format(base, imdb_id)


def fetch_subtitles_metadata(imdb_id, media_type, lang, season=None, episode=None, session=None):
    session = session or SESSION
    url = build_subtitles_api_url(imdb_id, media_type, lang, season=season, episode=episode)
    response = session.get(url, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    payload = response.json()
    subtitles = payload.get('subtitles') or []
    return subtitles


def download_subtitle(url, destination_path, session=None):
    session = session or SESSION
    response = session.get(url, timeout=DEFAULT_TIMEOUT)
    response.raise_for_status()
    with open(destination_path, 'wb') as file_handle:
        file_handle.write(response.content)
    return destination_path


def get_and_download_subtitles(imdb_id, media_type, season=None, episode=None, addon=None, clear_before=True):
    addon = _resolve_addon(addon)
    if not imdb_id or not media_type:
        _subtitles_log_debug('Busca de legenda ignorada por ausência de imdb_id ou media_type.', addon)
        return []

    if media_type == 'series' and (season in (None, '') or episode in (None, '')):
        _subtitles_log_debug('Busca de legenda para série ignorada por falta de season/episode.', addon)
        return []

    if not get_setting_bool('legendasauto', True, addon=addon):
        _subtitles_log_debug('Legendas automáticas desativadas pelo usuário.', addon)
        return []

    subtitle_dir = get_subtitle_dir(addon)
    if clear_before and get_setting_bool('limparlegendas', True, addon=addon):
        clear_subtitles_cache(addon)

    language_candidates = get_subtitle_language_candidates(addon)
    local_paths = []

    for subtitle_lang in language_candidates:
        try:
            subtitles = fetch_subtitles_metadata(
                imdb_id,
                media_type,
                subtitle_lang,
                season=season,
                episode=episode,
                session=SESSION,
            )
        except Exception as exc:
            log('[STREMIO] Erro ao consultar legenda {} para {}: {}'.format(subtitle_lang, imdb_id, exc), level=xbmc.LOGWARNING)
            continue

        if not subtitles:
            _subtitles_log_debug('Nenhuma legenda encontrada para {} no idioma {}.'.format(imdb_id, subtitle_lang), addon)
            continue

        for index, subtitle in enumerate(subtitles):
            subtitle_url = subtitle.get('url')
            if not subtitle_url:
                continue
            destination = os.path.join(subtitle_dir, 'sub_{}_{}.vtt'.format(subtitle_lang.replace('-', '_'), index))
            try:
                download_subtitle(subtitle_url, destination, session=SESSION)
                local_paths.append(destination)
            except Exception as exc:
                log('[STREMIO] Falha ao baixar legenda {}: {}'.format(subtitle_url, exc), level=xbmc.LOGWARNING)

        if local_paths:
            _subtitles_log_debug('Legendas encontradas usando o idioma {}.'.format(subtitle_lang), addon)
            return local_paths

    return []


def get_stremio_subtitle(imdb_id, season=None, episode=None, lang='pt-br'):
    media_type = 'series' if season and episode else 'movie'
    try:
        subtitles = fetch_subtitles_metadata(imdb_id, media_type, lang, season=season, episode=episode, session=SESSION)
        if not subtitles:
            log('[STREMIO] No subtitles found for {}'.format(imdb_id))
            return None
        subtitle_url = subtitles[0].get('url')
        if not subtitle_url:
            return None
        subtitle_dir = get_subtitle_dir()
        extension = '.vtt'
        destination = os.path.join(subtitle_dir, '{}_{}{}'.format(imdb_id, lang, extension))
        return download_subtitle(subtitle_url, destination, session=SESSION)
    except Exception as exc:
        log('[STREMIO] Subtitle error: {}'.format(exc), level=xbmc.LOGWARNING)
        return None
