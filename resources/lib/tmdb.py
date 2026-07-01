# -*- coding: utf-8 -*-
from __future__ import unicode_literals

text_type = str
from urllib.parse import quote
from datetime import datetime
import time
from resources.lib.common import (
    make_session, log, DEFAULT_TIMEOUT, safe_text, get_addon,
    get_setting_bool, get_setting_enum_value
)
from resources.lib import cache_store


API_KEY = "92c1507cc18d85290e7a0b96abb37316"
TMDB_BASE_URL = "https://api.themoviedb.org/3"
SESSION = make_session()


def _addon():
    return get_addon()


def _language():
    return get_setting_enum_value('tmdb_language', ['pt-BR', 'en-US', 'es-ES'], 'pt-BR', addon=_addon())


def _region():
    return get_setting_enum_value('tmdb_region', ['BR', 'US', 'PT', 'ES', 'MX'], 'BR', addon=_addon())


def _items_per_page():
    raw = get_setting_enum_value('itensporpagina', ['20', '40', '60'], '20', addon=_addon())
    try:
        return max(20, min(60, int(raw)))
    except Exception:
        return 20


def _passes_filters(item):
    if item.get('adult'):
        return False
    if get_setting_bool('ocultarsemposter', False, addon=_addon()):
        if not item.get('poster_path') and not item.get('backdrop_path') and not item.get('still_path'):
            return False
    return True


TMDB_CACHE_NAMESPACE = 'tmdb_json_v1'
TMDB_STALE_FALLBACK_SECONDS = 7 * 86400


def _tmdb_cache_ttl(url):
    lowered = safe_text(url).lower()
    if '/external_ids' in lowered:
        return 7 * 86400
    if '/season/' in lowered:
        return 6 * 3600
    if '/search/' in lowered or '/trending/' in lowered:
        return 10 * 60
    if '/discover/' in lowered or '/now_playing' in lowered or '/upcoming' in lowered or '/on_the_air' in lowered:
        return 15 * 60
    return 12 * 3600


def _tmdb_cache_key(url):
    # A URL contém a chave pública da API; no banco fica somente o digest.
    return cache_store.make_key(url)


def _tmdb_stale_usable(record):
    if not record:
        return False
    try:
        return (int(time.time()) - int(record.get('updated_at') or 0)) <= TMDB_STALE_FALLBACK_SECONDS
    except Exception:
        return False


def get_json(url):
    """JSON TMDB com cache persistente, revalidação condicional e fallback seguro.

    Conteúdo fresco abre direto do SQLite. Quando vence, manda ETag/Last-Modified
    se o endpoint oferecer; 304 apenas renova o prazo. Falha remota nunca apaga
    o último JSON válido.
    """
    cache_key = _tmdb_cache_key(url)
    ttl = _tmdb_cache_ttl(url)
    record = cache_store.get_record(TMDB_CACHE_NAMESPACE, cache_key)
    if cache_store.is_fresh(record):
        value = record.get('value') if isinstance(record, dict) else {}
        return value if isinstance(value, dict) else {}

    headers = {}
    if record:
        etag = safe_text(record.get('etag', '')).strip()
        last_modified = safe_text(record.get('last_modified', '')).strip()
        if etag:
            headers['If-None-Match'] = etag
        if last_modified:
            headers['If-Modified-Since'] = last_modified

    try:
        response = SESSION.get(url, timeout=DEFAULT_TIMEOUT, headers=headers or None)
        try:
            response.encoding = 'utf-8'
        except Exception:
            pass
        status_code = int(getattr(response, 'status_code', 0) or 0)
        if status_code == 304 and record:
            cache_store.touch_record(TMDB_CACHE_NAMESPACE, cache_key, ttl,
                                     etag=getattr(response, 'headers', {}).get('ETag', record.get('etag', '')),
                                     last_modified=getattr(response, 'headers', {}).get('Last-Modified', record.get('last_modified', '')))
            value = record.get('value') if isinstance(record, dict) else {}
            return value if isinstance(value, dict) else {}
        if status_code != 200:
            log('TMDB HTTP {} for {}'.format(status_code, url), level=2)
            if _tmdb_stale_usable(record):
                value = record.get('value') if isinstance(record, dict) else {}
                return value if isinstance(value, dict) else {}
            return {}
        payload = response.json()
        if not isinstance(payload, dict):
            return {}
        headers_out = getattr(response, 'headers', {}) or {}
        cache_store.set_record(
            TMDB_CACHE_NAMESPACE, cache_key, payload, ttl,
            etag=headers_out.get('ETag', '') or headers_out.get('Etag', ''),
            last_modified=headers_out.get('Last-Modified', '')
        )
        return payload
    except Exception as exc:
        log('TMDB request failed: {}'.format(exc), level=4)
        if _tmdb_stale_usable(record):
            value = record.get('value') if isinstance(record, dict) else {}
            return value if isinstance(value, dict) else {}
        return {}


def clear_tmdb_cache():
    return cache_store.clear_namespace(TMDB_CACHE_NAMESPACE)


def _media_kind(type_):
    return 'tv' if type_ == 'series' else 'movie'




def _latest_series_start_year():
    try:
        return max(2000, int(datetime.utcnow().year) - 1)
    except Exception:
        return 2025

def _build_item(item, type_):
    year_key = 'release_date' if type_ == 'movie' else 'first_air_date'
    year = (item.get(year_key) or '')[:4]
    return {
        'id': text_type(item.get('id', '')),
        'title': safe_text(item.get('title') or item.get('name') or ''),
        'poster': "https://image.tmdb.org/t/p/w500{}".format(item['poster_path']) if item.get('poster_path') else None,
        'background': "https://image.tmdb.org/t/p/original{}".format(item['backdrop_path']) if item.get('backdrop_path') else None,
        'description': safe_text(item.get('overview', '')),
        'year': year
    }


def _fetch_paged_results(url_builder, type_, logical_page):
    per_page = _items_per_page()
    pages_needed = max(1, int(per_page / 20))
    start_page = ((max(1, logical_page) - 1) * pages_needed) + 1
    items = []
    for tmdb_page in range(start_page, start_page + pages_needed):
        data = get_json(url_builder(tmdb_page))
        results = data.get('results', [])
        if not results:
            continue
        for item in results:
            if item.get('id') and _passes_filters(item):
                items.append(_build_item(item, type_))
    return items[:per_page]


def get_items(type_, category, page=1):
    media = _media_kind(type_)
    language = _language()
    region = _region()

    def build_url(tmdb_page):
        if category == 'trending':
            return TMDB_BASE_URL + "/trending/{}/week?api_key={}&language={}&page={}".format(media, API_KEY, language, tmdb_page)
        elif category == 'top':
            return TMDB_BASE_URL + "/{}/top_rated?api_key={}&language={}&page={}".format(media, API_KEY, language, tmdb_page)
        elif category == 'now_playing' or (category == 'latest' and media == 'movie'):
            url = TMDB_BASE_URL + "/movie/now_playing?api_key={}&language={}&page={}".format(API_KEY, language, tmdb_page)
            if region:
                url += '&region={}'.format(region)
            return url
        elif category == 'upcoming':
            url = TMDB_BASE_URL + "/movie/upcoming?api_key={}&language={}&page={}".format(API_KEY, language, tmdb_page)
            if region:
                url += '&region={}'.format(region)
            return url
        elif category == 'on_the_air':
            return TMDB_BASE_URL + "/tv/on_the_air?api_key={}&language={}&page={}".format(API_KEY, language, tmdb_page)
        elif category == 'recent_premieres' or (category == 'latest' and media == 'tv'):
            start_year = _latest_series_start_year()
            end_year = start_year + 1
            url = TMDB_BASE_URL + "/discover/tv?api_key={}&language={}&page={}".format(API_KEY, language, tmdb_page)
            url += '&sort_by=first_air_date.desc'
            url += '&include_null_first_air_dates=false'
            url += '&first_air_date.gte={}-01-01'.format(start_year)
            url += '&first_air_date.lte={}-12-31'.format(end_year)
            return url
        else:
            url = TMDB_BASE_URL + "/discover/{}?api_key={}&language={}&page={}".format(media, API_KEY, language, tmdb_page)
            if media == 'movie' and region:
                url += '&region={}'.format(region)
            return url

    return _fetch_paged_results(build_url, type_, int(page))


def get_meta(type_, tmdb_id):
    media = _media_kind(type_)
    url = TMDB_BASE_URL + "/{}/{}?api_key={}&language={}".format(media, tmdb_id, API_KEY, _language())
    data = get_json(url)
    year_key = 'first_air_date' if type_ == 'series' else 'release_date'
    year = (data.get(year_key) or '')[:4]
    return {
        'name': safe_text(data.get('name') if type_ == 'series' else data.get('title')),
        'description': safe_text(data.get('overview', '')),
        'poster': "https://image.tmdb.org/t/p/w500{}".format(data['poster_path']) if data.get('poster_path') else None,
        'background': "https://image.tmdb.org/t/p/original{}".format(data['backdrop_path']) if data.get('backdrop_path') else None,
        'year': year
    }


def get_seasons(type_, tmdb_id):
    if type_ != 'series':
        return []

    url = TMDB_BASE_URL + "/tv/{}?api_key={}&language={}".format(tmdb_id, API_KEY, _language())
    data = get_json(url)
    seasons = []
    for season in data.get('seasons', []):
        if season.get('season_number') is None:
            continue
        year = (season.get('air_date') or '')[:4]
        if get_setting_bool('ocultarsemposter', False, addon=_addon()) and not season.get('poster_path'):
            continue
        seasons.append({
            'season_number': season['season_number'],
            'name': safe_text(season.get('name') or 'Temporada {}'.format(season['season_number'])),
            'poster': "https://image.tmdb.org/t/p/w500{}".format(season['poster_path']) if season.get('poster_path') else None,
            'description': safe_text(season.get('overview', '')),
            'episode_count': season.get('episode_count', 0),
            'year': year
        })
    return seasons


def get_episodes(type_, tmdb_id, season_number):
    if type_ != 'series':
        return []

    url = TMDB_BASE_URL + "/tv/{}/season/{}?api_key={}&language={}".format(tmdb_id, season_number, API_KEY, _language())
    data = get_json(url)
    episodes = []
    for episode in data.get('episodes', []):
        number = episode.get('episode_number')
        if number is None:
            continue
        year = (episode.get('air_date') or '')[:4]
        if get_setting_bool('ocultarsemposter', False, addon=_addon()) and not episode.get('still_path'):
            continue
        episodes.append({
            'episode_number': number,
            'name': safe_text(episode.get('name') or 'Episódio {}'.format(number)),
            'poster': "https://image.tmdb.org/t/p/w500{}".format(episode['still_path']) if episode.get('still_path') else None,
            'description': safe_text(episode.get('overview', '')),
            'id': text_type(episode.get('id', '')),
            'year': year
        })
    return episodes


def search(type_, query, page=1):
    query = safe_text(query).strip()
    if not query:
        return []

    encoded_query = quote(query, safe='')
    media = _media_kind(type_)
    language = _language()

    def build_url(tmdb_page):
        return TMDB_BASE_URL + "/search/{}?api_key={}&language={}&query={}&page={}".format(
            media, API_KEY, language, encoded_query, tmdb_page
        )

    return _fetch_paged_results(build_url, type_, int(page))


def get_imdb_id_tmdb(tmdb_id, media_type):
    media = 'tv' if media_type == 'series' else media_type
    url = TMDB_BASE_URL + "/{}/{}/external_ids?api_key={}".format(text_type(media), text_type(tmdb_id), text_type(API_KEY))
    data = get_json(url)
    return data.get('imdb_id') or ''
