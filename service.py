# -*- coding: utf-8 -*-
from __future__ import unicode_literals

"""Service do Mega Portugal.

Prepara o cache EPG das listas M3U em background.
Compatível com Kodi 19+ / Python 3.
"""

import sys
import time

try:
    from kodi_six import xbmc, xbmcaddon, xbmcgui
except ImportError:
    import xbmc
    import xbmcaddon
    import xbmcgui

try:
    from resources.lib import m3u
    from resources.lib.common import get_setting_bool, log
except Exception:
    m3u = None
    get_setting_bool = None
    log = None

try:
    from resources.lib import proxy
except Exception:
    proxy = None

try:
    from resources.lib import dns as dns_helper
except Exception:
    dns_helper = None

ADDON_ID = 'plugin.video.Mega.Portugal'
STARTUP_DELAY_SECS = 0
SETTING_WATCH_INTERVAL_SECS = 5
# Sem evento onSettingsChanged, faz uma confirmação leve e esparsa.
# Isso evita reler settings.xml a cada ciclo em Kodi 21 sem perder a
# reação imediata normal quando o Kodi entrega o evento.
SETTING_FALLBACK_POLL_SECS = 30
# O service não faz manutenção pesada cíclica. Ele aquece no boot/ativação
# e força nova validação apenas quando o dia civil vira.
DAY_ROLLOVER_CHECK_INTERVAL_SECS = 60
PENDING_REFRESH_RETRY_SECS = 15 * 60
INTER_LIST_PAUSE_SECS = 0



def _log(message, level=None):
    try:
        if log is not None:
            log('[service.py] {}'.format(message), level=level)
            return
    except Exception:
        pass
    try:
        xbmc.log('[{}][service.py] {}'.format(ADDON_ID, message), level or xbmc.LOGDEBUG)
    except Exception:
        pass




def _start_proxy_service():
    """Inicia o proxy no interpretador persistente do service.py."""
    if proxy is None:
        _log('Proxy nativo indisponível no service; reprodução por proxy aguardará próxima inicialização.', getattr(xbmc, 'LOGWARNING', None))
        return False
    try:
        ok = bool(proxy.start_proxy())
        if ok:
            _log('Proxy nativo pronto no service.', getattr(xbmc, 'LOGDEBUG', None))
        else:
            _log('Proxy nativo não iniciou no service.', getattr(xbmc, 'LOGWARNING', None))
        return ok
    except Exception as exc:
        _log('Proxy nativo terminou com aviso ao iniciar: {}'.format(exc), getattr(xbmc, 'LOGWARNING', None))
        return False


def _stop_proxy_service():
    if proxy is None:
        return True
    try:
        ok = bool(proxy.stop_proxy(timeout=1.5))
        _log('Proxy nativo encerrado limpo={}.'.format('sim' if ok else 'não'), getattr(xbmc, 'LOGDEBUG', None))
        return ok
    except Exception as exc:
        _log('Proxy nativo terminou com aviso ao encerrar: {}'.format(exc), getattr(xbmc, 'LOGWARNING', None))
        return False

def _sync_optional_dns():
    """Sincroniza o DNS alternativo apenas no boot/evento de configuração."""
    if dns_helper is None:
        return False
    try:
        return bool(dns_helper.apply_configured_override())
    except Exception as exc:
        _log('DNS alternativo terminou com aviso ao sincronizar: {}'.format(exc), getattr(xbmc, 'LOGDEBUG', None))
        return False


def _notify_epg_background_enabled():
    """Mostra aviso curto apenas quando o usuário ativa o EPG durante a sessão.

    Não notifica no boot para não incomodar quem já deixou o EPG ligado.
    """
    try:
        xbmcgui.Dialog().notification(
            'Mega Portugal EPG',
            'EPG em background ativado. Cache em segundo plano.',
            getattr(xbmcgui, 'NOTIFICATION_INFO', ''),
            5000
        )
    except Exception:
        pass



def _today_key():
    """Retorna a data local usada para detectar virada de dia do EPG.

    Usa o relógio local do ambiente onde o Kodi está rodando. Isso evita
    depender de timezone externo e acompanha a data civil que o usuário vê.
    """
    try:
        return time.strftime('%Y%m%d', time.localtime())
    except Exception:
        try:
            return time.strftime('%Y%m%d')
        except Exception:
            return ''


def _is_any_playback_active():
    """Detecta qualquer reprodução ativa no Kodi.

    Em aparelhos fracos, qualquer processamento de XMLTV durante reprodução
    pode causar travamento, engasgo ou lentidão. Por isso o service trata
    vídeo, áudio, rádio, stream ou qualquer player ativo como bloqueio seguro.
    """
    try:
        player = xbmc.Player()
        try:
            if hasattr(player, 'isPlaying') and bool(player.isPlaying()):
                return True
        except Exception:
            pass
        try:
            if hasattr(player, 'isPlayingVideo') and bool(player.isPlayingVideo()):
                return True
        except Exception:
            pass
        try:
            if hasattr(player, 'isPlayingAudio') and bool(player.isPlayingAudio()):
                return True
        except Exception:
            pass
    except Exception:
        pass
    return False


def _defer_if_playing(action_label):
    """Retorna True quando há reprodução e o trabalho pesado deve ser adiado."""
    try:
        if _is_any_playback_active():
            _log('{} adiado: reprodução ativa no Kodi; aguardando ociosidade.'.format(action_label), getattr(xbmc, 'LOGINFO', None))
            return True
    except Exception:
        pass
    return False

def _addon():
    try:
        return xbmcaddon.Addon(ADDON_ID)
    except Exception:
        try:
            return xbmcaddon.Addon()
        except Exception:
            return None


def _epg_enabled(addon=None):
    addon = addon or _addon()
    if addon is None:
        return False
    try:
        if get_setting_bool is not None:
            return bool(get_setting_bool('tv_epg_ativo', False, addon=addon))
    except Exception:
        pass
    try:
        return (addon.getSetting('tv_epg_ativo') or 'false').lower() == 'true'
    except Exception:
        return False


def _wait_for_abort(monitor, seconds):
    """Espera abortar sem travar quando seconds <= 0.

    Em alguns ambientes Kodi, waitForAbort(0) pode se comportar como espera
    indefinida. O service chamava essa função com 0 antes de cada lista,
    então o aquecimento parava logo após "processando=8" e nunca entrava
    na lista01. Para cheque imediato, usa abortRequested().
    """
    try:
        secs = float(seconds)
    except Exception:
        secs = 0.0

    if secs <= 0:
        try:
            if monitor is not None and hasattr(monitor, 'abortRequested'):
                return bool(monitor.abortRequested())
        except Exception:
            pass
        return False

    try:
        if monitor is not None:
            return bool(monitor.waitForAbort(secs))
    except Exception:
        pass
    try:
        xbmc.sleep(int(secs * 1000))
    except Exception:
        try:
            time.sleep(secs)
        except Exception:
            pass
    return False


class MegaPortugalServiceMonitor(xbmc.Monitor):

    def __init__(self):
        try:
            xbmc.Monitor.__init__(self)
        except Exception:
            pass
        self.settings_changed = False
        self._last_settings_log_ts = 0

    def onSettingsChanged(self):
        try:
            self.settings_changed = True
            try:
                if proxy is not None:
                    proxy.invalidate_settings_cache()
            except Exception:
                pass
            now_ts = time.time()
            # Kodi pode disparar vários eventos iguais em sequência ao abrir/fechar settings.
            # Mantém o diagnóstico, mas evita poluir o debug com dezenas de linhas repetidas.
            if (now_ts - float(getattr(self, '_last_settings_log_ts', 0) or 0)) >= 5:
                self._last_settings_log_ts = now_ts
                _log('Mudança de configuração detectada pelo Kodi.', getattr(xbmc, 'LOGDEBUG', None))
        except Exception:
            pass


def _new_monitor():
    try:
        return MegaPortugalServiceMonitor()
    except Exception:
        try:
            return xbmc.Monitor()
        except Exception:
            return None


def _consume_settings_changed(monitor):
    try:
        changed = bool(getattr(monitor, 'settings_changed', False))
        if changed:
            monitor.settings_changed = False
        return changed
    except Exception:
        return False


def _safe_list_label(index, url):
    try:
        label = 'lista{:02d}'.format(int(index))
    except Exception:
        label = 'lista{}'.format(index)
    try:
        raw = str(url or '').split('?', 1)[0].rstrip('/').rsplit('/', 1)[-1]
        raw = raw.replace('.txt', '').replace('.m3u8', '').replace('.m3u', '').strip()
        if raw:
            label = '{} ({})'.format(label, raw)
    except Exception:
        pass
    return label


def _unique_urls(values):
    urls = []
    seen = set()
    try:
        iterable = list(values or [])
    except Exception:
        iterable = []
    for value in iterable:
        url = _to_text_safe(value).strip()
        if not url or url in seen:
            continue
        seen.add(url)
        urls.append(url)
    return urls


def _remember_epg_source_from_status(url, status):
    try:
        if not isinstance(status, dict):
            return False
        epg_url = _to_text_safe(status.get('epg_url', '')).strip()
        if not epg_url:
            return False
        return bool(m3u.remember_service_epg_source(url, epg_url))
    except Exception:
        return False


def _recover_status_with_m3u(url, status):
    """Recupera manifesto ausente sem baixar XMLTV desnecessariamente.

    Quando o manifesto não sabe a epg_url, o service não consegue validar o
    JSON existente só com o master.txt. Nesse caso, faz uma leitura leve da
    própria M3U para descobrir o x-tvg-url. Se o índice/JSON já existir e for
    válido, ele é reutilizado e o manifesto é reconstruído.
    """
    try:
        reason = _to_text_safe((status or {}).get('reason', '')).strip() if isinstance(status, dict) else ''
    except Exception:
        reason = ''
    if reason != 'epg_url_desconhecida':
        return status
    try:
        recovered = m3u.epg_cache_status_for_list(url, allow_m3u_fetch=True)
        _remember_epg_source_from_status(url, recovered)
        return recovered
    except Exception:
        return status


def _count_ready_urls(urls):
    ready = 0
    missing = []
    details = []
    for url in list(urls or []):
        try:
            status = m3u.epg_cache_status_for_list(url, allow_m3u_fetch=False)
            status = _recover_status_with_m3u(url, status)
        except Exception:
            status = {'ready': False, 'reason': 'verificacao_falhou', 'm3u_url': url, 'epg_url': ''}
        details.append(status)
        if isinstance(status, dict) and status.get('ready'):
            _remember_epg_source_from_status(url, status)
            ready += 1
        else:
            try:
                reason = _to_text_safe(status.get('reason', 'desconhecido'))
            except Exception:
                reason = 'desconhecido'
            missing.append({'url': url, 'reason': reason})
    return ready, missing, details




def _status_reason(status):
    try:
        if isinstance(status, dict):
            return _to_text_safe(status.get('reason', '')).strip()
    except Exception:
        pass
    return ''



def _to_text_safe(value):
    try:
        return str(value or '')
    except Exception:
        return ''


def prepare_epg_cache_once(monitor=None):
    addon = _addon()
    if addon is None:
        _log('EPG background ignorado: addon indisponível.', getattr(xbmc, 'LOGDEBUG', None))
        return False
    if m3u is None:
        _log('EPG background ignorado: módulo m3u indisponível.', getattr(xbmc, 'LOGDEBUG', None))
        return False
    if not _epg_enabled(addon):
        _log('EPG background ignorado: EPG desligado nas configurações.', getattr(xbmc, 'LOGDEBUG', None))
        return False
    if _defer_if_playing('EPG background'):
        return False

    # Limpeza enxuta do cache: evita acumular índices EPG de dias anteriores.
    # Mantém somente o JSON do dia atual; o dia anterior vira sobra após a virada.
    try:
        cleanup = m3u.cleanup_old_epg_index_cache(keep_days=1)
        removed = int(cleanup.get('removed', 0) or 0) if isinstance(cleanup, dict) else 0
        kept = int(cleanup.get('kept', 0) or 0) if isinstance(cleanup, dict) else 0
        if removed > 0:
            _log('Limpeza EPG: removidos={} mantidos={} criterio=manter_somente_hoje.'.format(removed, kept), getattr(xbmc, 'LOGINFO', None))
    except Exception as exc:
        _log('Limpeza EPG ignorada: {}'.format(exc), getattr(xbmc, 'LOGDEBUG', None))

    # A partir desta versão, o service não usa limite fixo de listas.
    # Ele lê o master.txt, conta quantas listas existem e aquece somente
    # as pendentes/inválidas. Ler o master é leve e permite detectar lista
    # nova/removida sem voltar a baixar todas as M3U/XMLTV no boot.
    try:
        _log('EPG background: lendo master.txt...', getattr(xbmc, 'LOGDEBUG', None))
        lists = m3u.get_lists()
    except Exception as exc:
        _log('Falha ao ler master.txt para aquecimento EPG: {}'.format(exc), getattr(xbmc, 'LOGWARNING', None))
        return False

    urls = _unique_urls(lists)
    if not urls:
        _log('EPG background: master.txt não retornou listas válidas.', getattr(xbmc, 'LOGWARNING', None))
        return False

    total_found = len(lists or [])
    total = len(urls)
    if total != total_found:
        _log('EPG background: master.txt possui {} entradas; {} URLs únicas serão verificadas.'.format(total_found, total), getattr(xbmc, 'LOGDEBUG', None))

    try:
        m3u.remember_service_epg_lists(urls)
    except Exception:
        pass

    ready_count, missing, details = _count_ready_urls(urls)
    if total > 0 and ready_count == total:
        _log('EPG background: cache existente válido para {} listas detectadas no master; nada para baixar.'.format(total), getattr(xbmc, 'LOGINFO', None))
        return True

    first_reason = ''
    try:
        if missing:
            first_reason = _to_text_safe(missing[0].get('reason', ''))
    except Exception:
        first_reason = ''
    _log('EPG background: cache conhecido incompleto/expirado; prontas={} pendentes={} motivo={}.'.format(
        ready_count, max(0, total - ready_count), first_reason or 'verificacao'
    ), getattr(xbmc, 'LOGDEBUG', None))

    processed = 0
    warmed = 0
    skipped = 0
    failed = 0
    aborted = False

    _log('EPG background iniciado imediatamente: listas_encontradas={} processando={}.'.format(total_found, total), getattr(xbmc, 'LOGINFO', None))

    for index, url in enumerate(urls, 1):
        if _wait_for_abort(monitor, 0):
            aborted = True
            break

        processed += 1
        label = _safe_list_label(index, url)

        try:
            status = m3u.epg_cache_status_for_list(url, allow_m3u_fetch=False)
            status = _recover_status_with_m3u(url, status)
        except Exception:
            status = {'ready': False, 'reason': 'verificacao_falhou'}
        if isinstance(status, dict) and status.get('ready'):
            skipped += 1
            _remember_epg_source_from_status(url, status)
            reason_ready = ''
            try:
                reason_ready = _to_text_safe(status.get('reason', ''))
            except Exception:
                reason_ready = ''
            if reason_ready and reason_ready != 'cache_valido':
                _log('EPG background: {} possui cache utilizável ({}); pulando download temporariamente.'.format(label, reason_ready), getattr(xbmc, 'LOGINFO', None))
            else:
                _log('EPG background: {} já possui cache válido; pulando download.'.format(label), getattr(xbmc, 'LOGINFO', None))
            continue

        reason = ''
        try:
            reason = _to_text_safe(status.get('reason', ''))
        except Exception:
            reason = ''
        _log('EPG background: iniciando {} de {}{}.'.format(
            label, total, ' (motivo: {})'.format(reason) if reason else ''
        ), getattr(xbmc, 'LOGINFO', None))

        started = time.time()
        try:
            ok = bool(m3u.warm_epg_cache_for_list(url))
            elapsed = max(0.0, time.time() - started)
            if ok:
                warmed += 1
                _log('EPG background: {} concluída em {:.1f}s.'.format(label, elapsed), getattr(xbmc, 'LOGINFO', None))
            else:
                _log('EPG background: {} sem URL de EPG válida.'.format(label), getattr(xbmc, 'LOGDEBUG', None))
        except Exception as exc:
            failed += 1
            elapsed = max(0.0, time.time() - started)
            _log('EPG background: falha em {} após {:.1f}s: {}'.format(label, elapsed, exc), getattr(xbmc, 'LOGWARNING', None))

        if index < total and INTER_LIST_PAUSE_SECS > 0:
            _log('EPG background: pausa conservadora de {}s antes da próxima lista.'.format(INTER_LIST_PAUSE_SECS), getattr(xbmc, 'LOGDEBUG', None))
            if _wait_for_abort(monitor, INTER_LIST_PAUSE_SECS):
                aborted = True
                break

    _log('EPG background finalizado: processadas={} aquecidas={} puladas={} falhas={} abortado={}.'.format(
        processed, warmed, skipped, failed, 'sim' if aborted else 'não'
    ), getattr(xbmc, 'LOGINFO', None))
    return bool(warmed or skipped)

def run_epg_refresh_once():
    monitor = _new_monitor()
    prepare_epg_cache_once(monitor=monitor)


def run_service_loop():
    monitor = _new_monitor()
    _sync_optional_dns()
    _start_proxy_service()
    try:
        _log('Service iniciado em background; verificando cache EPG imediatamente.', getattr(xbmc, 'LOGDEBUG', None))

        # Pedido operacional: ao iniciar o Kodi ou ao ativar o EPG, verificar logo.
        # Se o cache já existir e estiver válido, não baixa listas/XMLTV de novo.
        # Se estiver ausente/vencido/incompleto, aquece uma lista por vez.
        if STARTUP_DELAY_SECS > 0 and _wait_for_abort(monitor, STARTUP_DELAY_SECS):
            _log('Service encerrado durante a pausa inicial.', getattr(xbmc, 'LOGDEBUG', None))
            return

        last_epg_enabled = _epg_enabled()
        last_refresh_ts = 0
        last_refresh_day = _today_key()
        last_day_check_ts = 0
        pending_refresh_reason = ''
        pending_refresh_day = ''
        pending_refresh_last_attempt_ts = 0
        last_settings_poll_ts = time.time()
        _log('Service ativo; EPG ligado={}.'.format('sim' if last_epg_enabled else 'não'), getattr(xbmc, 'LOGDEBUG', None))

        if last_epg_enabled:
            if _is_any_playback_active():
                pending_refresh_reason = 'boot'
                pending_refresh_day = _today_key()
                _log('EPG inicial adiado: reprodução ativa no Kodi; será executado quando parar.', getattr(xbmc, 'LOGINFO', None))
            else:
                try:
                    prepare_epg_cache_once(monitor=monitor)
                except Exception as exc:
                    _log('Service EPG inicial terminou com aviso: {}'.format(exc), getattr(xbmc, 'LOGDEBUG', None))
                last_refresh_ts = time.time()
                last_refresh_day = _today_key()
        else:
            _log('EPG desligado no boot; service ficará aguardando ativação.', getattr(xbmc, 'LOGDEBUG', None))

        while True:
            if _wait_for_abort(monitor, SETTING_WATCH_INTERVAL_SECS):
                break

            try:
                settings_changed = _consume_settings_changed(monitor)
                now_ts = time.time()
                # Kodi normalmente entrega onSettingsChanged. Só lê settings.xml
                # periodicamente quando esse evento não veio, reduzindo I/O e ruído
                # em builds Kodi 21 que registram formato legado em debug.
                should_poll_settings = settings_changed or ((now_ts - float(last_settings_poll_ts or 0)) >= SETTING_FALLBACK_POLL_SECS)
                if should_poll_settings:
                    # DNS alternativo é uma escolha explícita; sincronizar aqui
                    # permite ligar/desligar sem reiniciar o Kodi e sem polling
                    # por requisição de reprodução.
                    _sync_optional_dns()
                    epg_enabled = _epg_enabled()
                    last_settings_poll_ts = now_ts
                else:
                    epg_enabled = last_epg_enabled

                # Caso crítico: EPG estava desligado e foi ligado com o Kodi já aberto.
                # Ao perceber ligado, inicia sem pausa artificial. O loop de 1s fica
                # apenas como fallback para quando o evento do Kodi não acordar na hora.
                if epg_enabled and not last_epg_enabled:
                    _log('EPG ativado durante a sessão; iniciando aquecimento imediatamente.', getattr(xbmc, 'LOGINFO', None))
                    _notify_epg_background_enabled()
                    if _is_any_playback_active():
                        pending_refresh_reason = 'ativacao_epg'
                        pending_refresh_day = _today_key()
                        _log('Aquecimento após ativar EPG adiado: reprodução ativa no Kodi.', getattr(xbmc, 'LOGINFO', None))
                    else:
                        try:
                            prepare_epg_cache_once(monitor=monitor)
                        except Exception as exc:
                            _log('Aquecimento após ativar EPG terminou com aviso: {}'.format(exc), getattr(xbmc, 'LOGDEBUG', None))
                        last_refresh_ts = now_ts
                        last_refresh_day = _today_key()

                # Virada de dia: quando amanhã vira hoje, força a validação da nova
                # janela Hoje + Amanhã. Isso substitui a manutenção pesada de 6 em 6h.
                # A checagem é leve e só roda a leitura pesada quando a data muda.
                current_day = last_refresh_day
                if epg_enabled and (not last_day_check_ts or (now_ts - last_day_check_ts) >= DAY_ROLLOVER_CHECK_INTERVAL_SECS):
                    current_day = _today_key()
                    last_day_check_ts = now_ts

                if epg_enabled and last_epg_enabled and last_refresh_day and current_day and current_day != last_refresh_day:
                    if _is_any_playback_active():
                        if pending_refresh_reason != 'virada_dia' or pending_refresh_day != current_day:
                            _log('Virada de dia detectada no EPG: {} -> {}; atualização adiada por reprodução ativa.'.format(last_refresh_day, current_day), getattr(xbmc, 'LOGINFO', None))
                        pending_refresh_reason = 'virada_dia'
                        pending_refresh_day = current_day
                    else:
                        _log('Virada de dia detectada no EPG: {} -> {}; atualizando janela hoje+amanha.'.format(last_refresh_day, current_day), getattr(xbmc, 'LOGINFO', None))
                        try:
                            prepare_epg_cache_once(monitor=monitor)
                        except Exception as exc:
                            _log('Service EPG na virada do dia terminou com aviso: {}'.format(exc), getattr(xbmc, 'LOGDEBUG', None))
                        last_refresh_ts = now_ts
                        last_refresh_day = current_day
                        pending_refresh_reason = ''
                        pending_refresh_day = ''
                        pending_refresh_last_attempt_ts = 0

                # Se algum processamento foi adiado por reprodução ativa, tenta novamente
                # somente quando o player estiver ocioso, com throttle para evitar loop.
                if epg_enabled and pending_refresh_reason:
                    if not _is_any_playback_active() and (now_ts - float(pending_refresh_last_attempt_ts or 0)) >= int(PENDING_REFRESH_RETRY_SECS):
                        target_day = pending_refresh_day or _today_key()
                        _log('Executando EPG pendente após fim da reprodução: motivo={} dia={}.'.format(pending_refresh_reason, target_day), getattr(xbmc, 'LOGINFO', None))
                        pending_refresh_last_attempt_ts = now_ts
                        try:
                            ok_pending = bool(prepare_epg_cache_once(monitor=monitor))
                        except Exception as exc:
                            ok_pending = False
                            _log('EPG pendente terminou com aviso: {}'.format(exc), getattr(xbmc, 'LOGDEBUG', None))
                        if ok_pending:
                            last_refresh_ts = now_ts
                            last_refresh_day = target_day
                            pending_refresh_reason = ''
                            pending_refresh_day = ''
                            pending_refresh_last_attempt_ts = 0

                # Revisão limpa baseada na 3.3.4 original:
                # A rechecagem automática de 2 em 2 horas foi removida do loop.
                # A navegação manual já chama get_epg_for_channels() para a lista acessada
                # e o módulo m3u.py revalida XMLTV/índice quando o cache está ausente,
                # vencido, insuficiente ou fora da janela de graça. Isso é mais leve
                # para MXQ: só a lista que o usuário abriu é verificada.
                #
                # Permanecem como gatilhos automáticos:
                # - boot/primeira ativação do EPG;
                # - ativação do EPG durante a sessão;
                # - virada do dia;
                # - retomada de pendência quando a reprodução parar.
                #
                # As listas com Amanhã curto/insuficiente que não forem abertas manualmente
                # ficam para o próximo gatilho principal, evitando processamento de fundo.
                if settings_changed and epg_enabled:
                    _log('Configuração alterada com EPG ativo; mantendo cache até ativação/virada/navegação manual.', getattr(xbmc, 'LOGDEBUG', None))

                last_epg_enabled = epg_enabled
            except Exception as exc:
                _log('Monitoramento do service EPG terminou com aviso: {}'.format(exc), getattr(xbmc, 'LOGDEBUG', None))

    finally:
        _stop_proxy_service()


if __name__ == '__main__':
    try:
        if any(str(arg or '').lower() == 'epg_refresh_once' for arg in sys.argv[1:]):
            run_epg_refresh_once()
        else:
            run_service_loop()
    except Exception as exc:
        _log('Service finalizou com aviso: {}'.format(exc), getattr(xbmc, 'LOGDEBUG', None))
