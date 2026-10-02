"""Política anti-SSRF (SDD §11, gap B3), solo stdlib.

Todo destino de red pasa por aquí. Bloquea por defecto redes privadas,
internas, de enlace local y de metadata (``169.254.169.254``), tanto por
IP literal como por nombre.

Política
--------

Bloqueado por defecto (``allow_private=False``):

- ``127.0.0.0/8``, ``10.0.0.0/8``, ``172.16.0.0/12``, ``192.168.0.0/16``
- ``169.254.0.0/16`` (enlace local **y** metadata de nube)
- ``100.64.0.0/10`` (CGNAT), ``198.18.0.0/15`` (benchmarking)
- ``0.0.0.0/8``, ``192.0.2/24``, ``198.51.100/24``, ``203.0.113/24``
- ``224.0.0.0/4`` (multicast), ``240.0.0.0/4`` (reservado + broadcast)
- ``::1``, ``::``, ``fc00::/7`` (ULA), ``fe80::/10``, ``ff00::/8``
- IPv4-mapped (``::ffff:127.0.0.1``) → se **desenvuelve** y se comprueba
  la IPv4 de dentro
- nombres: ``localhost``, ``*.localhost``, ``*.local``, ``*.internal``,
  ``*.localdomain``, ``metadata.google.internal``

Excepción (SDD §11 «Excepción», **nunca silenciosa**)
----------------------------------------------------

Un servidor IPTV en el propio LAN o en ``127.0.0.1`` puede ser legítimo.
Se acepta **solo** con ``allow_private=True``, que viene del flag por fuente
``allow_private_network`` de ``playlists.json`` y exige confirmación en la
TUI. Incluso con esa excepción siguen bloqueados: enlace local
(``169.254/16``, ``fe80::/10``), multicast, reservados, unspecified y los
hosts de metadata de nube.

DNS rebinding
-------------

:func:`resolve_and_check` comprueba **todas** las direcciones que devuelve
``getaddrinfo(AF_UNSPEC)``, no solo la primera: si un nombre devuelve una
pública y una privada, la petición no sale. La resolución va en un hilo
daemon con timeout para que un DNS colgado no congele la TUI.
"""

from __future__ import annotations

import ipaddress
import queue
import socket
import threading
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from typing import Iterable

from .errors import NetworkError, SSRFBlockedError
from .limits import get_limits
from .url_policy import UrlParts, validate_url

__all__ = [
    "check_ip",
    "check_host",
    "resolve_and_check",
    "check_url",
    "is_blocked_ip",
    "blocked_reason",
]


def _nets(items: Iterable[str]) -> tuple[IPv4Network | IPv6Network, ...]:
    return tuple(ipaddress.ip_network(item) for item in items)


def _in(ip: IPv4Address | IPv6Address, nets: tuple[IPv4Network | IPv6Network, ...]) -> bool:
    """Pertenencia a una lista de redes.

    Ojo: ``ip in tuple_de_redes`` usa ``==`` y **siempre** da False; hay que
    recurrir al ``__contains__`` de cada red.
    """
    return any(ip in net for net in nets)


#: Redes siempre bloqueadas (IPv4).
_BLOCKED_V4: tuple[IPv4Network | IPv6Network, ...] = _nets(
    (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
    )
)

#: Redes siempre bloqueadas (IPv6).
_BLOCKED_V6: tuple[IPv4Network | IPv6Network, ...] = _nets(
    (
        "::/128",
        "::1/128",
        "100::/64",
        "2001:db8::/32",
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)

#: Con ``allow_private=True`` se permiten estas (loopback, LAN, ULA, CGNAT)…
_PRIVATE_OK_V4: tuple[IPv4Network | IPv6Network, ...] = _nets(
    ("10.0.0.0/8", "127.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10")
)
_PRIVATE_OK_V6: tuple[IPv4Network | IPv6Network, ...] = _nets(("::1/128", "fc00::/7"))

#: …pero estos **siguen** bloqueados aunque se permita la red privada:
#: enlace local (metadata de nube), multicast, reservado y unspecified.
_HARD_BLOCKED_V4: tuple[IPv4Network | IPv6Network, ...] = _nets(
    ("169.254.0.0/16", "0.0.0.0/8", "224.0.0.0/4", "240.0.0.0/4")
)
_HARD_BLOCKED_V6: tuple[IPv4Network | IPv6Network, ...] = _nets(
    ("::/128", "fe80::/10", "ff00::/8", "100::/64")
)

#: Hostnames bloqueados **siempre**, venga o no la excepción.
_METADATA_HOSTS: frozenset[str] = frozenset(
    {
        "metadata.google.internal",
        "instance-data",
        "metadata.goog",
        "169.254.169.254",
    }
)
_METADATA_SUFFIXES: tuple[str, ...] = (".metadata.google.internal",)

#: Nombres locales: solo se bloquean sin la excepción.
_LOCAL_HOSTS: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "ip6-allnodes",
    }
)
_LOCAL_SUFFIXES: tuple[str, ...] = (
    ".localhost",
    ".local",
    ".localdomain",
    ".internal",
)

_REASONS_V4 = {
    "loopback": "dirección de loopback",
    "link": "dirección de enlace local (incluye metadata de nube)",
    "private": "dirección de red privada",
    "multicast": "dirección multicast",
    "reserved": "dirección reservada",
    "unspecified": "dirección sin especificar",
    "cgnat": "dirección de red compartida (CGNAT)",
    "doc": "dirección de documentación",
}

_CGNAT_NETS = _nets(("100.64.0.0/10",))
_DOC_NETS = _nets(("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24"))


# ---------------------------------------------------------------------------
# Comprobaciones de IP
# ---------------------------------------------------------------------------


def _unwrap(ip: IPv4Address | IPv6Address) -> IPv4Address | IPv6Address:
    """Desenvuelve IPv4-mapped (``::ffff:127.0.0.1`` → ``127.0.0.1``)."""
    if isinstance(ip, IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _reason_v4(ip: IPv4Address) -> str | None:
    """Motivo de bloqueo de una IPv4, o None si es destino permitido."""
    if _in(ip, _HARD_BLOCKED_V4):
        if ip.is_link_local:
            return _REASONS_V4["link"]
        if ip.is_multicast:
            return _REASONS_V4["multicast"]
        if ip.is_unspecified:
            return _REASONS_V4["unspecified"]
        return _REASONS_V4["reserved"]
    if ip.is_loopback:
        return _REASONS_V4["loopback"]
    if ip.is_link_local:
        return _REASONS_V4["link"]
    if ip.is_multicast:
        return _REASONS_V4["multicast"]
    if ip.is_unspecified:
        return _REASONS_V4["unspecified"]
    if _in(ip, _BLOCKED_V4):
        if _in(ip, _CGNAT_NETS):
            return _REASONS_V4["cgnat"]
        if _in(ip, _DOC_NETS):
            return _REASONS_V4["doc"]
        return _REASONS_V4["private"]
    return None


def _reason_v6(ip: IPv6Address) -> str | None:
    if _in(ip, _HARD_BLOCKED_V6):
        if ip.is_link_local:
            return _REASONS_V4["link"]
        if ip.is_multicast:
            return _REASONS_V4["multicast"]
        if ip.is_unspecified:
            return _REASONS_V4["unspecified"]
        return _REASONS_V4["reserved"]
    if ip.is_loopback:
        return _REASONS_V4["loopback"]
    if ip.is_link_local:
        return _REASONS_V4["link"]
    if ip.is_multicast:
        return _REASONS_V4["multicast"]
    if ip.is_unspecified:
        return _REASONS_V4["unspecified"]
    if _in(ip, _BLOCKED_V6):
        return _REASONS_V4["private"]
    return None


def _apply_exception(
    ip: IPv4Address | IPv6Address, reason: str | None, allow_private: bool
) -> str | None:
    """Revisa si la excepción `allow_private` salva esta dirección."""
    if reason is None or not allow_private:
        return reason
    saveable = (
        _REASONS_V4["private"],
        _REASONS_V4["cgnat"],
        _REASONS_V4["loopback"],
    )
    if reason not in saveable:
        return reason
    if isinstance(ip, IPv4Address):
        return None if _in(ip, _PRIVATE_OK_V4) else reason
    return None if _in(ip, _PRIVATE_OK_V6) else reason


def is_blocked_ip(
    ip: str | IPv4Address | IPv6Address, *, allow_private: bool = False
) -> bool:
    """True si la IP no puede usarse como destino."""
    return blocked_reason(ip, allow_private=allow_private) is not None


def blocked_reason(
    ip: str | IPv4Address | IPv6Address, *, allow_private: bool = False
) -> str | None:
    """Devuelve el motivo de bloqueo, o None si el destino está permitido."""
    if isinstance(ip, str):
        try:
            address: IPv4Address | IPv6Address = ipaddress.ip_address(
                ip.split("%", 1)[0]
            )
        except ValueError:
            return "dirección no válida"
    else:
        address = ip
    address = _unwrap(address)
    if isinstance(address, IPv4Address):
        reason = _reason_v4(address)
    else:
        reason = _reason_v6(address)
    return _apply_exception(address, reason, allow_private)


def check_ip(
    ip: str | IPv4Address | IPv6Address, *, allow_private: bool = False
) -> None:
    """Lanza :class:`SSRFBlockedError` si `ip` no es un destino permitido."""
    reason = blocked_reason(ip, allow_private=allow_private)
    if reason is not None:
        raise SSRFBlockedError(f"Destino bloqueado por seguridad: {reason}.")


# ---------------------------------------------------------------------------
# Nombres de host
# ---------------------------------------------------------------------------


_DOTTED_NUMERIC_RE = __import__("re").compile(r"^[0-9.]+$")
_HEX_NUMERIC_RE = __import__("re").compile(r"^0x[0-9a-fA-F]+(?:\.[0-9a-fA-F]+)*$")


def _looks_numeric(host: str) -> bool:
    """True si el host parece un número/dirección pero no es un literal IP.

    Cubre ``0177.0.0.1`` (octal), ``2130706433`` (entero), ``0x7f.1`` y
    ``127.1``: formas que el resolver del SO sí entiende y que
    ``ipaddress()`` rechaza. Se bloquean **siempre**, con o sin
    ``allow_private``: el riesgo es la ambigüedad, no la privacidad.
    Sólo mira dígitos, puntos y prefijos hexadecimales, así que
    ``cafe.example`` o ``3m.example`` no se ven afectados.
    """
    if not host or len(host) > 64:
        return False
    return bool(
        _DOTTED_NUMERIC_RE.match(host) or _HEX_NUMERIC_RE.match(host)
    )


def _norm_name(host: str) -> str:
    name = (host or "").strip().rstrip(".").lower()
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    return name


def check_host(host: str, *, allow_private: bool = False) -> None:
    """Comprueba un host **sin resolver** (IP literal o nombre).

    Para saber a qué IP apunta realmente un nombre hace falta
    :func:`resolve_and_check`; aquí solo se miran literales y nombres
    localmente bloqueables.
    """
    name = _norm_name(host)
    if not name:
        raise SSRFBlockedError("Destino bloqueado por seguridad: host vacío.")
    try:
        ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        pass
    else:
        check_ip(name, allow_private=allow_private)
        return

    if _looks_numeric(name):
        # 0177.0.0.1, 2130706433, 0x7f.1, 127.1...: el resolver del SO los
        # interpreta (a menudo en octal) mientras ipaddress() los rechaza.
        raise SSRFBlockedError(
            "Destino bloqueado por seguridad: dirección numérica ambigua."
        )

    low = name
    if low in _METADATA_HOSTS or any(low.endswith(s) for s in _METADATA_SUFFIXES):
        raise SSRFBlockedError(
            "Destino bloqueado por seguridad: endpoint de metadata de la nube."
        )
    if not allow_private:
        if low in _LOCAL_HOSTS or any(low.endswith(s) for s in _LOCAL_SUFFIXES):
            raise SSRFBlockedError(
                "Destino bloqueado por seguridad: nombre local o interno."
            )


# ---------------------------------------------------------------------------
# Resolución DNS
# ---------------------------------------------------------------------------


def _getaddrinfo(host: str, port: int | None, timeout: float):  # type: ignore[no-untyped-def]
    """``getaddrinfo`` con timeout en hilo daemon (nunca congela la TUI)."""
    box: "queue.Queue[tuple[str, object]]" = queue.Queue(maxsize=1)

    def _run() -> None:
        try:
            box.put(("ok", socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_STREAM)))
        except BaseException as exc:  # noqa: BLE001 - se re-lanza en el hilo caller
            box.put(("err", exc))

    threading.Thread(target=_run, name="thetvview-dns", daemon=True).start()
    try:
        kind, payload = box.get(timeout=timeout)
    except queue.Empty as exc:
        raise NetworkError(
            f"Tiempo de espera agotado al conectar: no se pudo resolver «{host}» "
            f"en {timeout:g}s."
        ) from exc
    if kind == "err":
        raise payload  # type: ignore[misc]
    return payload  # type: ignore[no-any-return]


def resolve_and_check(
    host: str,
    port: int | None = None,
    *,
    allow_private: bool = False,
    timeout: float | None = None,
) -> list[str]:
    """Resuelve `host` y valida **todas** las IPs resultantes.

    Devuelve la lista de IPs válidas (para que el caller pueda fijarse en una).
    Lanza :class:`SSRFBlockedError` si alguna IP no está permitida y
    :class:`NetworkError` si el DNS falla o tarda demasiado.
    """
    name = _norm_name(host)
    if not name:
        raise SSRFBlockedError("Destino bloqueado por seguridad: host vacío.")
    if timeout is None:
        timeout = get_limits().connect_timeout
    # Literal: no hace falta DNS.
    try:
        literal = ipaddress.ip_address(name.split("%", 1)[0])
    except ValueError:
        literal = None
    if literal is not None:
        check_ip(literal, allow_private=allow_private)
        return [str(literal)]

    check_host(name, allow_private=allow_private)
    try:
        infos = _getaddrinfo(name, port, timeout)
    except NetworkError:
        raise
    except OSError as exc:
        raise NetworkError(
            f"No se pudo conectar con el servidor: no se resolvió «{name}»."
        ) from exc

    found: list[str] = []
    for info in infos:
        sockaddr = info[4]
        address = str(sockaddr[0]).split("%", 1)[0]
        check_ip(address, allow_private=allow_private)
        if address not in found:
            found.append(address)
    if not found:
        raise NetworkError(
            f"No se pudo conectar con el servidor: «{name}» no tiene direcciones."
        )
    return found


# ---------------------------------------------------------------------------
# URL completa
# ---------------------------------------------------------------------------


def check_url(
    url: str,
    purpose: str = "metadata",
    *,
    allow_private: bool = False,
    resolve: bool = True,
) -> UrlParts:
    """Valida la URL (SDD §10) **y** su destino (SDD §11).

    Args:
        url: URL candidata.
        purpose: ``"metadata"`` o ``"stream"``.
        allow_private: excepción por fuente (nunca global).
        resolve: si False, no se resuelve el DNS (solo literales y nombres).

    Returns:
        Las partes ya validadas de la URL.

    Raises:
        InvalidUrlError: la URL no cumple la política de esquema/forma.
        SSRFBlockedError: el destino es privado/interno/de metadata.
        NetworkError: el DNS falló o tardó demasiado.
    """
    parts = validate_url(url, purpose=purpose)
    if not parts.host:
        # ffmpeg:// con ruta relativa: no hay destino de red que comprobar.
        return parts
    if resolve:
        resolve_and_check(
            parts.host,
            parts.port,
            allow_private=allow_private,
        )
    else:
        check_host(parts.host, allow_private=allow_private)
    return parts
