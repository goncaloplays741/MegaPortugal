# -*- coding: utf-8 -*-
import socket
import struct
import random
import logging
import sys
import json
import time
import os
import io
try:
    from kodi_six import xbmc, xbmcaddon, xbmcvfs
except ImportError:
    import xbmc
    import xbmcaddon
    import xbmcvfs
PY2 = sys.version_info[0] == 2
ADDON_ = xbmcaddon.Addon()
TRANSLATE_ = xbmc.translatePath if PY2 else xbmcvfs.translatePath
try:
    from resources.lib.common import ensure_dir
except Exception:
    def ensure_dir(path):
        try:
            if path and not os.path.exists(path):
                os.makedirs(path)
        except Exception:
            pass
        return path
profile = ensure_dir(TRANSLATE_(ADDON_.getAddonInfo('profile')))
CACHE_FILE = os.path.join(profile, 'dns_cache.json')


_DNS_PATCH_STATE = {'installed': False, 'original_getaddrinfo': socket.getaddrinfo, 'resolver': None, 'instance': None}


class customdns:
    def __init__(self, cache_file=CACHE_FILE, cache_ttl=3600):
        self.dns_server = [
            '94.140.14.140', # adguard
            '94.140.14.141', # adguard
            '208.67.222.222',# OpenDNS
            '208.67.220.220',# OpenDNS
            '1.1.1.1',       # Cloudflare
            '8.8.8.8'        # Google DNS
        ]
        self.original_getaddrinfo = _DNS_PATCH_STATE.get('original_getaddrinfo', socket.getaddrinfo)
        self.cache_file = cache_file
        self.cache_ttl = cache_ttl  # Tempo de expiração em segundos
        self.cache = self._load_cache()
        self.debug_mode = False
        self.mode_logger = False

        # Override DNS only once per runtime. Guardamos a função exata para
        # poder restaurar com segurança se o usuário desligar a opção depois.
        if not _DNS_PATCH_STATE.get('installed'):
            resolver = self._resolver
            _DNS_PATCH_STATE['original_getaddrinfo'] = socket.getaddrinfo
            _DNS_PATCH_STATE['resolver'] = resolver
            _DNS_PATCH_STATE['instance'] = self
            socket.getaddrinfo = resolver
            _DNS_PATCH_STATE['installed'] = True

    def _load_cache(self):
        """Carrega o cache DNS sem quebrar em arquivo parcial/corrompido."""
        try:
            if os.path.exists(self.cache_file):
                with io.open(self.cache_file, 'r', encoding='utf-8') as f:
                    cache = json.load(f)
                    if not isinstance(cache, dict):
                        return {}
                    current_time = time.time()
                    return {
                        domain: data for domain, data in cache.items()
                        if isinstance(data, dict) and float(data.get('expires', 0) or 0) > current_time
                    }
            return {}
        except Exception as e:
            logging.error("Erro ao carregar cache: {}".format(e))
            return {}

    def _save_cache(self):
        """Salva JSON de forma atômica no perfil privado do Kodi."""
        temp_file = '{}.tmp.{}.{}'.format(
            self.cache_file,
            os.getpid() if hasattr(os, 'getpid') else 0,
            int(time.time() * 1000)
        )
        try:
            ensure_dir(os.path.dirname(self.cache_file))
            with io.open(temp_file, 'w', encoding='utf-8') as f:
                json.dump(self.cache, f, ensure_ascii=False, separators=(',', ':'))
            if hasattr(os, 'replace'):
                os.replace(temp_file, self.cache_file)
            else:
                if os.path.exists(self.cache_file):
                    os.remove(self.cache_file)
                os.rename(temp_file, self.cache_file)
        except Exception as e:
            logging.error("Erro ao salvar cache: {}".format(e))
            try:
                if os.path.exists(temp_file):
                    os.remove(temp_file)
            except Exception:
                pass

    def is_valid_ipv4(self, ip):
        try:
            socket.inet_aton(ip)
            return True
        except socket.error:
            return False

    def is_valid_ipv6(self, ip):
        try:
            if hasattr(socket, 'inet_pton'):
                socket.inet_pton(socket.AF_INET6, ip)
                return True
            return False
        except socket.error:
            return False

    def _build_dns_query(self, domain):
        transaction_id = random.randint(0, 65535)
        flags = 0x0100
        questions = 1
        header = struct.pack('>HHHHHH', transaction_id, flags, questions, 0, 0, 0)

        if PY2:
            qname = b''.join(chr(len(part)) + part for part in domain.split('.')) + b'\x00'
        else:
            qname = b''.join(bytes([len(part)]) + part.encode() for part in domain.split('.')) + b'\x00'

        qtype = 1  # A record
        qclass = 1  # IN
        question = qname + struct.pack('>HH', qtype, qclass)
        return header + question

    def _parse_dns_response(self, data):
        answer_count = struct.unpack(">H", data[6:8])[0]
        offset = 12
        while data[offset] != 0:
            offset += 1
        offset += 5  # null + qtype + qclass

        for _ in range(answer_count):
            offset += 2  # name (pointer)
            rtype, rclass, ttl, rdlength = struct.unpack(">HHIH", data[offset:offset+10])
            offset += 10
            if rtype == 1 and rdlength == 4:  # A record
                ip_parts = struct.unpack(">BBBB", data[offset:offset+4])
                return ".".join(map(str, ip_parts))
            offset += rdlength
        return None

    def resolve(self, domain, dns_custom):
        # Verifica o cache
        if domain in self.cache:
            if self.cache[domain]['expires'] > time.time():
                if self.mode_logger:
                    logging.info("Cache hit for {}: {}".format(domain, self.cache[domain]['ip']))
                return self.cache[domain]['ip']
            else:
                # Remove entrada expirada
                del self.cache[domain]
                self._save_cache()

        try:
            domain_clean = domain.strip('.')
            if self.mode_logger:
                logging.debug("Resolvendo {} via DNS {}".format(domain_clean, dns_custom))

            query = self._build_dns_query(domain_clean)

            if self.is_valid_ipv6(dns_custom):
                s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
                addr = (dns_custom, 53, 0, 0)
            else:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                addr = (dns_custom, 53)

            s.settimeout(3)
            s.sendto(query, addr)
            data, _ = s.recvfrom(512)
            s.close()

            ip = self._parse_dns_response(data)
            if ip:
                # Salva no cache com timestamp de expiração
                self.cache[domain] = {
                    'ip': ip,
                    'expires': time.time() + self.cache_ttl
                }
                self._save_cache()
                if self.mode_logger:
                    logging.debug("Resolved {} to {}".format(domain, ip))
                return ip
        except Exception as e:
            if self.mode_logger:
                logging.error("Erro ao resolver {} com {}: {}".format(domain, dns_custom, e))
        return None

    def _resolver(self, host, port, *args, **kwargs):
        """Resolver DNS que respeita a intenção do socket chamador.

        Mantém o resolvedor nativo para IPv6 explícito, localhost, .local e
        tipos de socket não convencionais. Assim não interfere em descoberta
        local e APIs nativas em Android, iOS/tvOS, Windows, Linux e ARM.
        """
        try:
            family = args[0] if len(args) > 0 else kwargs.get('family', 0)
            socktype = args[1] if len(args) > 1 else kwargs.get('type', 0)
            proto = args[2] if len(args) > 2 else kwargs.get('proto', 0)
            flags = args[3] if len(args) > 3 else kwargs.get('flags', 0)
            host_text = host.decode('utf-8', 'ignore') if isinstance(host, bytes) else str(host or '').strip()
            lowered = host_text.lower().rstrip('.')
            ai_numeric = getattr(socket, 'AI_NUMERICHOST', 0)
            if (not host_text or lowered in ('localhost', 'localhost.localdomain') or
                    lowered.endswith('.local') or lowered.endswith('.lan') or
                    family == getattr(socket, 'AF_INET6', 10) or
                    (flags and ai_numeric and (flags & ai_numeric))):
                return self.original_getaddrinfo(host, port, *args, **kwargs)
            if self.is_valid_ipv4(host_text) or self.is_valid_ipv6(host_text):
                return self.original_getaddrinfo(host, port, *args, **kwargs)
            if socktype not in (0, socket.SOCK_STREAM, socket.SOCK_DGRAM):
                return self.original_getaddrinfo(host, port, *args, **kwargs)
            for dns_server in self.dns_server:
                ip = self.resolve(host_text, dns_server)
                if ip:
                    resolved_type = socktype or socket.SOCK_STREAM
                    resolved_proto = proto or (socket.IPPROTO_UDP if resolved_type == socket.SOCK_DGRAM else socket.IPPROTO_TCP)
                    return [(socket.AF_INET, resolved_type, resolved_proto, '', (ip, port))]
            if self.mode_logger:
                logging.warning("Falha ao resolver {}, fallback para getaddrinfo".format(host_text))
        except Exception as e:
            if self.mode_logger:
                logging.error("Erro no resolver para {}: {}".format(host, e))
        return self.original_getaddrinfo(host, port, *args, **kwargs)


def _override_enabled(addon=None):
    try:
        addon = addon or xbmcaddon.Addon()
        return (addon.getSetting('proxy_dns_override') or 'false').strip().lower() == 'true'
    except Exception:
        return False


def disable_customdns():
    """Restaura o resolvedor nativo somente se o OnePlay ainda for o dono.

    Não desfaz um monkeypatch diferente que tenha sido instalado depois por
    outro addon/processo durante a mesma sessão Kodi.
    """
    try:
        if not _DNS_PATCH_STATE.get('installed'):
            return False
        resolver = _DNS_PATCH_STATE.get('resolver')
        original = _DNS_PATCH_STATE.get('original_getaddrinfo')
        if resolver is not None and socket.getaddrinfo is resolver and original is not None:
            socket.getaddrinfo = original
        _DNS_PATCH_STATE['installed'] = False
        _DNS_PATCH_STATE['resolver'] = None
        _DNS_PATCH_STATE['instance'] = None
        return True
    except Exception:
        return False


def apply_configured_override(addon=None):
    """Aplica ou remove o DNS alternativo conforme a configuração explícita.

    Retorna True somente quando o override OnePlay permanece ativo. Com a
    opção desligada, a rede do Kodi segue usando o DNS normal do sistema.
    """
    if not _override_enabled(addon=addon):
        disable_customdns()
        return False
    try:
        if not _DNS_PATCH_STATE.get('installed'):
            customdns()
        return bool(_DNS_PATCH_STATE.get('installed'))
    except Exception:
        return False
