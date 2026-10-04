"""Qué **transportes** abre cada reproductor (SDD-M §27, plan Fase 0).

Verificado **empíricamente** contra los binarios de esta máquina, con el argv
que construye la propia app (:func:`thetvview.player.core.command_for`) y
streams de laboratorio servidos en local. No es la tabla del §27 del SDD-M
copiada de la documentación: donde el SDD-M dice una cosa y aquí la máquina
dice otra, **manda la máquina** y la diferencia está anotada.

Método (importante, porque un «sí» sin método no es un dato):

- Se lanza el reproductor **con el comando que el usuario recibiría**, en modo
  headless, contra un stream de laboratorio.
- Un caso sólo cuenta como ``OK`` si la salida **describe el medio que está
  decodificando** (códec, dimensión, reloj avanzando). Que no falle no es lo
  mismo que abra: un reproductor puede quedarse esperando en silencio.
- Lo que no se puede comprobar en esta máquina queda ``verificado=False`` y
  **no se ofrece**: la app no promete una capacidad que no ha medido.

Medido el 2026-10-04 en esta máquina (mpv 0.40.0, VLC 3.0.24, mplayer 1.5,
ffmpeg 7.1.5), Linux:

===========================  ======  ======  =========
caso                         mpv     VLC     mplayer
===========================  ======  ======  =========
HLS master (variantes)       OK      OK      OK
HLS media playlist           OK      OK      OK
HLS URL sin extensión        OK      OK      FALLO
MPEG-TS/HTTP ``.ts``         OK      OK      OK
MPEG-TS/HTTP sin extensión   OK      OK      OK
RTMP                         OK      OK      OK
RTMPS (TLS)                  OK      OK      FALLO
RTSP                         OK      OK      OK
RTSP con credenciales        OK      OK      OK
UDP unicast                  OK      FALLO  OK
HTTP con User-Agent          OK      OK      OK
===========================  ======  ======  =========

Los cuatro ❌ **no son de la app**: son de los binarios, y cada uno tiene su
motivo en ``notas`` para que el diagnóstico lo explique en vez de inventarse una
causa. Los tres de mplayer y el de VLC son limitaciones **medidas** de estos
builds, con su mensaje literal al lado.

Cuatro hallazgos que el §27 del SDD-M **no** predecía y que mandan sobre él:

- **mplayer no abre HLS si la URL no acaba en ``.m3u8``** (ni tiene ``.m3u8?``
  en la query). Su libavformat dice literalmente *«Not detecting m3u8/hls with
  non standard extension and non standard mime type»*. El caso es real: hay
  servidores que sirven el manifiesto en ``/live/canal``. Con la extensión
  funciona, porque entonces la app le antepone ``ffmpeg://`` para que
  resuelva las rutas relativas.
- **mplayer no abre RTMPS**: dice *«No stream found to handle url
  rtmps://…»*. No es que rechace el TLS: es que su lista de protocolos de
  entrada no incluye ``rtmps``. mpv y VLC sí.
- **VLC no abre UDP**: abre el socket y se queda ahí. Su filtro ``prefetch``
  pide 16 MiB antes de pasar un solo paquete al demuxer (*«using 16777216
  bytes buffer, 16777216 bytes read»*) y con un flujo en vivo eso tarda, pero
  además al llenarse **no** se lo entrega: comprobado durante 150 s con el
  buffer lleno, sin una sola línea de demuxer ni decodificador. mpv y mplayer
  sí.
- **mplayer no necesita credenciales dentro de la URL para RTSP**: con
  ``rtsp://usuario:clave@…`` abre el stream igual que mpv y VLC. Aun así la app
  **no** pone credenciales en la URL (decisión D2): el mecanismo de referencia
  opaca es mejor, y que un binario lo tolerase no es un motivo para filtrar.

Lo que **no** se ha podido verificar aquí, y por tanto no se ofrece:

- **RTSPS** (``rtsps://``): ni ffmpeg ni VLC tienen servidor RTSP sobre TLS en
  esta máquina, así que no hay contra qué probar. Aunque el cliente lo sepa
  abrir, la app no lo promete sin medirlo.
- **UDP multicast**: se verificó **UDP unicast**, que es otra cosa. Esta máquina
  no tiene ruta multicast utilizable (``ip route add 224.0.0.0/4`` da
  ``Operation not permitted`` sin privilegios, y el bucle local multicast no
  devuelve lo que se envía en el mismo proceso). La diferencia importa: un grupo
  multicast es red privada por definición (decisión de la Fase 6) y porque
  «el reproductor abre UDP» y «la red deja pasar el grupo» son dos fallos
  distintos (SDD-M §14). :func:`thetvview.platform_check.multicast_supported`
  responde a lo segundo, y sólo a lo segundo.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..streams.transport import Transport

__all__ = [
    "PlayerProtocolSupport",
    "PLAYER_PROTOCOL_SUPPORT",
    "TRANSPORTS",
    "supports",
    "support_for",
    "verified",
    "candidates",
    "MAX_UNVERIFIED_OFFER",
]

#: Todos los transportes que la tabla cubre, en el orden en que la UI los
#: nombra. Un transporte que no esté en la lista **no se ofrece**.
TRANSPORTS: tuple[str, ...] = tuple(t.value for t in Transport)

#: Si un transporte está ``verificado=False`` en **todos** los reproductores,
#: la app no lo ofrece. El corte es global a propósito (SDD-M §14): el
#: multicast depende de la red, no del binario, así que que uno de los tres lo
#: abra no demuestra nada.
MAX_UNVERIFIED_OFFER: int = 0


@dataclass(frozen=True)
class PlayerProtocolSupport:
    """Transports que abre un reproductor, con su motivo si algo falla."""

    name: str
    #: Transporte → lo abre. Ausente = no lo abre (o no se ha medido).
    transports: frozenset[str]
    #: Transporte → por qué no lo abre. Va a los modales y a los tests: el
    #: §51 del SDD de pistas pide *«documentar la limitación y proporcionar el
    #: fallback correspondiente»*, y un ❌ sin motivo es un fallo sin
    #: explicación.
    notas: dict[str, str] | None = None
    #: Transporte → está medido en esta máquina. Lo que no lo está no se ofrece.
    medido: frozenset[str] | None = None
    notes: str = ""

    def can_play(self, transport: Transport | str) -> bool:
        """¿Puede este reproductor abrir este transporte?

        False si no lo sabes: preferimos que el router pruebe el siguiente
        reproductor a prometer una capacidad sin medir (§51).
        """
        clave = _clave(transport)
        if clave not in self.transports:
            return False
        medido = self.medido if self.medido is not None else self.transports
        return clave in medido

    def why_not(self, transport: Transport | str) -> str:
        """Motivo de no poderlo, o "" si puede."""
        clave = _clave(transport)
        if self.can_play(clave):
            return ""
        if self.notas and clave in self.notas:
            return self.notas[clave]
        if self.medido is not None and clave not in self.medido:
            return (
                "no verificado en esta máquina: no se ofrece hasta medirlo "
                "aquí (el reproductor puede saberlo, pero no lo sabemos nosotros)"
            )
        return "no soportado por este reproductor"


def _clave(transport: Transport | str) -> str:
    if isinstance(transport, Transport):
        return transport.value
    return str(transport or "").strip().lower()


_COMUN = frozenset({"http", "https", "rtmp", "rtmps", "rtsp", "udp"})

#: Lo que abre **los tres**, medido. common evita tres copias de la misma
#: respuesta medida y deja sitio para lo que **no** abren.
_MPV_COMUN = _COMUN
_VLC_COMUN = _COMUN
_MPLAYER_COMUN = frozenset({"http", "https", "rtmp", "rtsp", "udp"})

#: VLC abre el socket UDP pero se queda en el ``prefetch`` de 16 MiB sin llegar
#: nunca a pasar un paquete al demuxer. Es lo medido, con su mensaje al lado.
_VLC_SIN_UDP = frozenset({"http", "https", "rtmp", "rtmps", "rtsp"})

_MCAST_NOTA = (
    "no verificado aquí: esta máquina no tiene ruta multicast utilizable, así "
    "que no hay forma de medirlo (el fallo sería de red, no del binario)"
)
_RTSPS_NOTA = (
    "no verificado aquí: no hay servidor RTSP sobre TLS en esta máquina contra "
    "el que medirlo, y la app no ofrece lo que no ha comprobado"
)

PLAYER_PROTOCOL_SUPPORT: dict[str, PlayerProtocolSupport] = {
    "mpv": PlayerProtocolSupport(
        name="mpv",
        transports=_MPV_COMUN,
        medido=_MPV_COMUN,
        notas={"rtsps": _RTSPS_NOTA},
        notes=(
            "Es el único de los tres con control en caliente (IPC) y abre "
            "todo lo medido: HLS, MPEG-TS/HTTP, RTMP, RTMPS, RTSP y UDP."
        ),
    ),
    "vlc": PlayerProtocolSupport(
        name="vlc",
        transports=_VLC_SIN_UDP,
        medido=_VLC_SIN_UDP,
        notas={
            "udp": (
                "no abre UDP: abre el socket y se queda esperando en su "
                "filtro prefetch de 16 MiB sin llegar a decodificar (medido "
                "el 2026-10-04). mpv y mplayer sí lo abren"
            ),
            "rtsps": _RTSPS_NOTA,
        },
        notes=(
            "Abre HTTP/HTTPS, RTMP, RTMPS y RTSP. En HLS es el único que ve "
            "los subtítulos declarados aparte, porque trae su propio demuxer "
            "adaptativo (ver player/capabilities.py). No abre UDP."
        ),
    ),
    "mplayer": PlayerProtocolSupport(
        name="mplayer",
        transports=_MPLAYER_COMUN,
        medido=_MPLAYER_COMUN,
        notas={
            "rtmps": (
                "no abre RTMPS: su lista de protocolos de entrada no incluye "
                "rtmps, así que dice «No stream found to handle url» sin llegar "
                "a negociar el TLS. mpv y VLC sí lo abren (medido el 2026-10-04)"
            ),
            "rtsps": _RTSPS_NOTA,
        },
        notes=(
            "Abre HTTP/HTTPS, RTMP, RTSP (con o sin credenciales) y UDP "
            "unicast. **No** abre RTMPS, y en HLS sólo si la URL acaba en "
            "`.m3u8`: su libavformat no detecta un manifiesto al que no le "
            "digan que lo es («Not detecting m3u8/hls with non standard "
            "extension»). Con la extensión, la app le antepone `ffmpeg://` para "
            "que resuelva las rutas relativas del manifiesto."
        ),
    ),
}

#: Multicast no se ha podido medir en ningún reproductor de esta máquina.
_MEDIDOS_GLOBAL = frozenset(
    t for t in TRANSPORTS if any(s.can_play(t) for s in PLAYER_PROTOCOL_SUPPORT.values())
)


def support_for(player_name: str | None) -> PlayerProtocolSupport | None:
    """Capacidades de transporte de `player_name`, o None si no lo conocemos."""
    if not player_name:
        return None
    return PLAYER_PROTOCOL_SUPPORT.get(str(player_name).strip().lower())


def supports(player_name: str | None, transport: Transport | str) -> bool:
    """¿Puede este reproductor abrir este transporte, según lo medido?"""
    soporte = support_for(player_name)
    return bool(soporte and soporte.can_play(transport))


def verified(transport: Transport | str) -> bool:
    """True si el transporte se ha medido en **algún** reproductor de aquí.

    Si nadie lo ha medido, la app no lo ofrece en absoluto: es la regla que
    convierte «no lo sé» en «no está», en vez de en un ``try`` que falla en la
    cara del usuario.
    """
    return _clave(transport) in _MEDIDOS_GLOBAL


def candidates(transport: Transport | str) -> tuple[str, ...]:
    """Reproductores que pueden abrir este transporte, por prioridad.

    La prioridad es la de :data:`thetvview.config.SUPPORTED_PLAYERS` (mpv,
    mplayer, vlc), que es el orden que el usuario ya conoce. Un reproductor
    que no abre el transporte **no aparece**: así es como se cumple el AC-009
    («un backend incompatible no bloquea a los compatibles») sin que el
    router tenga que recorrer la lista y descartarla a mano.
    """
    from ..config import SUPPORTED_PLAYERS

    return tuple(
        name
        for name in SUPPORTED_PLAYERS
        if supports(name, transport)
    )