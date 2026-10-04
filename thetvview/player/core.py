"""Lanzamiento de reproductores externos (mpv/mplayer/vlc), solo stdlib.

Es ``thetvview.player.core``; la API pública se reexporta en
``thetvview/player/__init__.py`` para que ``from thetvview.player import
launch`` siga siendo exactamente igual que antes.

Seguridad:
- NUNCA shell=True: el comando siempre es una lista de argv.
- Toda URL pasa por la política de esquema `purpose="stream"` (SDD §10)
  ANTES de construir nada: un M3U hostil con `file://`, `javascript:` o
  una línea que empieza por `-` no llega al reproductor (gap B1).
- El separador `--` se antepone a la URL: aunque algo se colara, el
  reproductor deja de interpretar la URL como opciones (B1, segunda capa).
- El binario se resuelve con shutil.which (config.find_player).
- Solo se traducen opciones EXTVLCOPT de una lista blanca; todo lo demás
  (incluidos los KODIPROP, específicos de Kodi) se ignora con aviso,
  nunca crashea ni pasa strings arbitrarios al reproductor.
- El User-Agent que ve el proveedor es `DEFAULT_USER_AGENT`, el mismo que
  usa la app, salvo que el canal declare el suyo: hay CDNs que sólo
  sirven los segmentos a esa cabecera (ver `_default_user_agent_args`).

TODO(player):
- Soportar más claves EXTVLCOPT (p. ej. network-caching por reproductor).
- Detectar si el reproductor sigue vivo y reportar códigos de salida.
"""

from __future__ import annotations

import shutil
import subprocess
from contextlib import contextmanager
from typing import Any

from .. import config
from ..models import Channel
from ..security.errors import InvalidUrlError
from ..security.safe_http import DEFAULT_USER_AGENT
from ..security.url_policy import (
    PURPOSE_STREAM,
    authorize_camara_url,
    validate_url,
)
from .errors import PlayerError, StreamError, explain
from .router import NoCompatibleBackend, select_backend
from .track_args import track_args


#: ``PlayerError`` vive en :mod:`thetvview.player.errors` (para que el router
#: pueda heredar de él sin ciclo de imports) y aquí se reexporta sin más: para
#: todo el código que usa ``player.PlayerError`` sigue siendo la misma clase.


# Segundos bajo los cuales una muerte del reproductor se considera
# "al instante" (el stream ni siquiera llegó a abrir).
EARLY_EXIT_SECONDS: float = 4.0


def explain_early_exit(returncode: int | None, elapsed_seconds: float) -> str | None:
    """Explica una muerte prematura del reproductor, o None si fue normal.

    - returncode None: sigue vivo (no hay nada que explicar).
    - returncode 0 o duración normal: fin normal, sin mensaje extra.
    - Otro código en <EARLY_EXIT_SECONDS: el stream no abrió.

    El texto **no** ha cambiado, y a propósito: ya decía lo importante
    («suele ser el proveedor») y cambiarlo haría que los tests y los usuarios
    que loconocendiedieran dos veces lo mismo. Lo que ha cambiado es que ahora
    existe :func:`thetvview.player.errors.explain`, que además **clasifica**
    el código y dice qué revisar; :func:`diagnose_early_exit` da esa versión
    cuando hay salida del reproductor que mirar.
    """
    if returncode is None:
        return None
    try:
        elapsed = float(elapsed_seconds)
    except (TypeError, ValueError):
        return None
    if returncode == 0 or elapsed >= EARLY_EXIT_SECONDS:
        return None
    return (
        f"Se cerró al instante (código {returncode}): el stream no llegó "
        "a abrir. Suele ser el proveedor (401/línea caducada o bloqueada, "
        "URL caída). Prueba otro canal; si fallan todos, pide al proveedor "
        "que revise la línea."
    )


def diagnose_early_exit(
    returncode: int | None,
    elapsed_seconds: float,
    detalle: str = "",
) -> StreamError | None:
    """Como :func:`explain_early_exit`, pero **clasificado** (SDD-M §20).

    Devuelve None en los mismos casos que devuelve None aquélla (sigue vivo,
    o terminó con normalidad), para que llamarla siempre sea seguro.

    Args:
        detalle: lo que escribió el reproductor. **Pasa por
            :func:`~thetvview.security.redaction.redact_text` antes** si
            puede contener una URL con token: el §22 del SDD-M no se
            implementa con un módulo de logging (decisión D5), sino con
            redacción en el punto donde nace el texto.
    """
    if explain_early_exit(returncode, elapsed_seconds) is None:
        return None
    return explain(returncode, detalle)


# Claves EXTVLCOPT que traducimos, por reproductor (claves canónicas).
#   mpv/mplayer: flag separado del valor; vlc: flag con '=' incrustado.
# Convenciones reales: "http-user-agent" y "http-referrer" son las claves
# habituales en listas IPTV; "user-agent"/"referrer"/"referer" se aceptan
# como alias y siempre se emite la opción nativa de cada reproductor.
_OPTION_FLAGS: dict[str, tuple[str, str, str]] = {
    # clave_canonica: (flag_mpv, flag_mplayer, flag_vlc)
    "http-user-agent": ("--user-agent=", "-user-agent ", "--http-user-agent="),
    "http-referrer": ("--referrer=", "-http-referrer ", "--http-referrer="),
}

# Alias frecuentes en EXTVLCOPT -> clave canónica.
_KEY_ALIASES: dict[str, str] = {
    "http-user-agent": "http-user-agent",
    "user-agent": "http-user-agent",
    "user_agent": "http-user-agent",
    "http-referrer": "http-referrer",
    "referrer": "http-referrer",
    "referer": "http-referrer",
    "http-referer": "http-referrer",
}


def _safe_value(value: str) -> bool:
    """True si el valor es razonable pasar como argumento único."""
    return bool(value) and value.isprintable() and not value.startswith("-")


def _default_user_agent_args(
    player_name: str, ya_emitidos: list[str]
) -> list[str]:
    """Identifica al reproductor como la app cuando el canal no fija UA.

    Medido sobre listas reales: hay proveedores cuyo CDN responde **403 a
    cualquier** User-Agent de reproductor (mpv, ffmpeg e incluso el de
    Chrome) y sólo entrega los segmentos al que usa la propia app. Sin
    esto, un canal que el sondeo lee perfectamente se queda reintentando
    segmentos indefinidamente — el mismo síntoma que una línea caída, pero
    no lo es, y sin este dato no hay forma de distinguirlo.

    Se manda el mismo :data:`~thetvview.security.safe_http.DEFAULT_USER_AGENT`
    que usa la app para sus peticiones: una sola identidad en todo el
    código, y ninguna cabecera inventada.

    Si el canal ya declara `#EXTVLCOPT:http-user-agent=…`, **gana el del
    proveedor** y aquí no se añade nada.
    """
    mpv_flag, mplayer_flag, vlc_flag = _OPTION_FLAGS["http-user-agent"]
    flag = {
        "mpv": mpv_flag,
        "mplayer": mplayer_flag.strip(),
        "vlc": vlc_flag,
    }.get(player_name)
    if not flag or not _safe_value(DEFAULT_USER_AGENT):
        return []
    if any(arg.startswith(flag) or arg == flag for arg in ya_emitidos):
        return []
    if player_name == "mplayer":
        return [flag, DEFAULT_USER_AGENT]
    return [f"{flag}{DEFAULT_USER_AGENT}"]


def _codec_h264_args(player_name: str) -> list[str]:
    """Argumentos para priorizar h264 como códec de video.

    h264 es el estándar de facto en IPTV/streaming. Estos argumentos
    fuerzan al reproductor a usar h264 cuando esté disponible, con
    fallback automático a otros codecs si h264 no funciona.

    - mpv: --hwdec=auto-safe + --hwdec-codecs=h264
    - mplayer: -vc ffh264 (fuerza decodificador ffh264)
    - vlc: --avcodec-hw=any + --avcodec-codec=h264
    """
    if player_name == "mpv":
        # --vd=ffh264: fuerza usar el decodificador ffh264 de ffmpeg
        # Si ffh264 no está disponible o falla, mpv intenta otros automáticamente
        return ["--vd=ffh264"]
    if player_name == "mplayer":
        # -vc ffh264,ffmpeg2,ffmpeg1,h264,divx5:
        #   Lista de codecs en orden de preferencia. mplayer intenta el
        #   primero que esté disponible y funcione; si falla, prueba el
        #   siguiente automáticamente. ffh264 (ffmpeg) es el más fiable
        #   para IPTV HLS, ffmpeg2/1 son backups, h264/divx5 como fallback.
        return ["-vc", "ffh264,ffmpeg2,ffmpeg1,h264,divx5"]
    if player_name == "vlc":
        # avcodec-hw=any: permite HW decode si disponible
        # avcodec-codec=h264: prioriza h264 en el decodificador
        return [
            "--avcodec-hw=any",
            "--avcodec-codec=h264",
        ]
    return []


def _headless_args(player_name: str) -> list[str]:
    """Argumentos headless por reproductor (sin ventana ni display).

    Pensados para tests/CI y entornos sin GUI: no abren ventana,
    no requieren DISPLAY/Wayland y no tocan el audio real.

    - mpv: --vo=null (salida de vídeo nula) + --ao=null (audio nulo).
    - vlc: --intf dummy (equivale a cvlc) + --vout dummy + --aout dummy.
    - mplayer: -vo null -ao null (drivers nulos, no necesitan X11).
    """
    if player_name == "mpv":
        return ["--vo=null", "--ao=null"]
    if player_name == "vlc":
        return ["--intf", "dummy", "--vout", "dummy", "--aout", "dummy"]
    if player_name == "mplayer":
        return ["-vo", "null", "-ao", "null"]
    return []


def _title_args(channel: Channel, player_name: str) -> list[str]:
    """Argumentos de título por reproductor a partir de channel.name.

    - mpv: --title y --force-media-title (ventana + metadata OSD).
    - vlc: --input-title-format
    - mplayer: -title (dos argv)

    Saneado: recorta, elimina saltos, filtra no imprimibles, trunca a 120.
    Si el nombre empieza con '-' en mplayer se prefija con '.' para no
    ser interpretado como flag.
    """
    raw = channel.name.strip()
    if not raw:
        return []
    # Reemplaza no imprimibles / saltos por espacio y colapsa.
    cleaned = "".join(ch if ch.isprintable() and ch not in "\n\r" else " " for ch in raw).strip()
    # Colapsar espacios dobles
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return []
    if len(cleaned) > 120:
        cleaned = cleaned[:120].rstrip()
    if player_name == "mpv":
        # Ambos: --title controla ventana, --force-media-title sobreescribe metadata del stream
        return [f"--title={cleaned}", f"--force-media-title={cleaned}"]
    if player_name == "vlc":
        return [f"--input-title-format={cleaned}"]
    if player_name == "mplayer":
        val = cleaned
        if val.startswith("-"):
            val = "." + val
        return ["-title", val]
    return []


def _option_args(
    channel: Channel, player_name: str
) -> tuple[list[str], list[str]]:
    """Traduce extra_options a argumentos. Devuelve (args, avisos)."""
    args: list[str] = []
    warnings: list[str] = []
    for tag, raw in channel.extra_options:
        if tag != "EXTVLCOPT":
            warnings.append(f"Ignorada opción {tag}: {raw}")  # p. ej. KODIPROP
            continue
        key, sep, value = raw.partition("=")
        if not sep:
            warnings.append(f"Ignorada EXTVLCOPT malformada: {raw}")
            continue
        canonical = _KEY_ALIASES.get(key.strip().lower())
        value = value.strip()
        if canonical is None or not _safe_value(value):
            warnings.append(f"Ignorada EXTVLCOPT no soportada: {raw}")
            continue
        mpv_flag, mplayer_flag, vlc_flag = _OPTION_FLAGS[canonical]
        if player_name == "mpv":
            args.append(f"{mpv_flag}{value}")
        elif player_name == "mplayer":
            flag, _, _ = mplayer_flag.strip().partition(" ")
            args.extend([flag, value])
        elif player_name == "vlc":
            args.append(f"{vlc_flag}{value}")
    return args, warnings


def _es_url_de_camara_resuelta(url: str) -> bool:
    """True si `url` es una URL de cámara que la app acaba de reconstruir.

    Se distingue por la **referencia opaca que se resolvió justo antes**, no
    por su forma: un `rtsp://user:pass@…` que venga de un M3U se convierte en
    `ipcam://…` antes de llegar aquí (`thetvview.ui.screens.play_channel`), así
    que cualquier `rtsp://` con userinfo que llega a este punto la construyó la
    app. Lo que se mira es que quede marcado como tal.
    """
    return _RESUELTAS_DE_CAMARA.get(url, False)


#: URLs de cámara que `play_channel` acaba de reconstruir con su credencial.
#:
#: Es un registro con vida de una reproducción, no un permiso permanente: lo
#: rellena y vacía `play_channel` alrededor de la llamada. Existe porque el
#: criterio «¿esta URL con userinfo es de la app?» no puede decidirse mirando
#: sólo la cadena —cualquier `rtsp://` podría haberla escrito cualquiera— y
#: prefiero que la excepción sea **explícita y efímera** a que se deduzca.
_RESUELTAS_DE_CAMARA: dict[str, bool] = {}


@contextmanager
def url_de_camara_resuelta(url: str):  # noqa: ANN201 - CM sin tipo de retorno
    """Marca `url` como reconstruida por la app durante el bloque.

    Uso (único): ``play_channel``, entre resolver la referencia opaca y
    construir el argv. Es un ``try/finally`` para que la marca no sobreviva a
    la reproducción: si se quedara, una URL con credenciales que llegara más
    tarde por otro camino pasaría el filtro sin haber pasado por aquí.
    """
    _RESUELTAS_DE_CAMARA[url] = True
    try:
        yield
    finally:
        _RESUELTAS_DE_CAMARA.pop(url, None)


def command_for(
    channel: Channel,
    player_name: str,
    player_path: str | None = None,
    headless: bool = False,
    *,
    selection: Any = None,
    capabilities: Any = None,
    proxy_url: str | None = None,
    ipc_path: str | None = None,
) -> list[str]:
    """Construye la línea de comando (lista argv) para reproducir `channel`.

    Lanza PlayerError si el reproductor no está disponible o no está
    soportado, o si la URL no supera la política de esquema `stream`.
    Nunca incluye metadatos no validados sin lista blanca.

    El argv termina siempre en ``["--", url]``: el `--` impide que una
    URL que empiece por `-` (o que el reproductor interprete como opción)
    ejecute parámetros arbitrarios (gap B1). **Ningún argumento nuevo mueve
    ese `--`**; todo lo de pistas va antes.

    Args:
        channel: canal a reproducir.
        player_name: 'mpv', 'mplayer' o 'vlc'.
        player_path: ruta al binario (si None, se resuelve con find_player).
        headless: si True, añade drivers nulos/dummy para tests o
            entornos sin display (no abre ventana ni requiere GUI).
        selection: :class:`~thetvview.tracks.models.PlaybackSelection` con lo
            que el usuario eligió. ``None`` = comportamiento de siempre.
        capabilities: :class:`~thetvview.tracks.models.MediaCapabilities` del
            stream, para traducir ids de pista a índices.
        proxy_url: master fijado servido por
            :mod:`thetvview.streams.pin_proxy`. Cuando está, **es** la URL
            que se reproduce y no se pasan índices de pista (el manifiesto ya
            lleva la elección; ver :func:`track_args`).
        ipc_path: socket de control de mpv. Sólo para mpv y sólo si se
            quiere poder cambiar de pista en caliente.

    Nota de seguridad: ``proxy_url`` **también** pasa por
    ``validate_url(PURPOSE_STREAM)`` antes de construir nada. Es un host
    loopback http, que la política admite, pero el filtro se aplica igual: si
    alguien fabricase un ``proxy_url`` con esquema raro, sale con el mismo
    mensaje que cualquier otra URL (H4, gap B1).
    """
    # B1: validación de esquema ANTES de tocar el sistema de ficheros ni
    # construir un solo argv. file://, javascript:, data: o una línea que
    # empiece por `-` salen de aquí con un mensaje apto para modal.
    target_url = channel.url
    if proxy_url:
        try:
            validate_url(proxy_url, PURPOSE_STREAM)
        except InvalidUrlError as exc:
            raise PlayerError(str(exc)) from exc
        target_url = proxy_url
    try:
        # Una URL de cámara llega aquí **reconstruida** por la app a partir del
        # keyring, con su userinfo puesto: `validate_url` la rechaza a
        # propósito, porque protege las URLs que vienen de una lista. La
        # excepción es `authorize_camara_url`, que acepta sólo `rtsp://` y sólo
        # si la app pudo construirla. Ver su docstring para por qué el hueco
        # existe y por qué no se abre nada más.
        if _es_url_de_camara_resuelta(target_url):
            authorize_camara_url(target_url)
        else:
            validate_url(target_url, PURPOSE_STREAM)
    except InvalidUrlError as exc:
        raise PlayerError(str(exc)) from exc

    path = player_path or config.find_player(player_name)
    if not path:
        raise PlayerError(
            f"Reproductor '{player_name}' no encontrado. "
            "Instálalo (p. ej. 'sudo apt install mpv') o elige otro."
        )
    args, _ = _option_args(channel, player_name)
    # Si el canal no fija User-Agent, el reproductor se identifica como la
    # app: hay CDNs que sólo sirven los segmentos a esa cabecera.
    args += _default_user_agent_args(player_name, args)
    # Opciones de pistas (lista blanca de player/track_args.py). Con el proxy
    # de fijado no se pasan: la elección ya está en el manifiesto servido.
    track_opts = track_args(
        player_name,
        selection,
        capabilities,
        pinned_via_proxy=bool(proxy_url),
    )
    ipc_opts = _ipc_args(player_name, ipc_path)
    url = target_url
    if player_name == "mplayer":
        # Fix lag/congelamiento en IPTV HLS:
        # - `-nocache` (sin cache) forzaba reproducción en tiempo real sin
        #   buffering -> cualquier jitter de red causa lags/congelamiento.
        # - La cache masiva previa (32MB / 50% -> prefill ~16MB) causaba el
        #   problema opuesto: timeout en HLS live (segmentos cada ~6s).
        # - Solución equilibrada: cache de 8MB (8192 kB) amortigua jitter sin
        #   requerir prefill excesivo (cache-min por defecto 20% -> ~1.6MB).
        #   Mantiene arranque rápido y evita "Cache empty" / stalls.
        # - `-forceidx` reindexa streams sin índice (inútil para live HLS)
        #   y provocaba stalls.
        # - `-autosync 5` + `-mc 0.1` era demasiado agresivo para jitter.
        # - probesize 32k era insuficiente para AAC/mp2 en HLS -> "Prediction
        #   is not allowed" / "Could not find codec parameters".
        args = [
            "-demuxer", "lavf",       # Forzar libavformat (MPEG-TS/HLS IPTV)
            "-cache", "8192",         # Cache 8 MB mínimo para evitar lags
            "-prefer-ipv4",           # Evita delays por probing IPv6
            "-framedrop",             # Drop frames si VO saturado
            "-osdlevel", "0",         # Sin OSD
            "-lavfdopts", "probesize=500000:analyzeduration=10000000",
            "-ac", "ffaac,ffmp2float,ffmp3float,ffac3,mpg123,mad",
        ] + args
        # Workaround HLS: mplayer antepone "mp:" a URLs relativas del
        # playlist (ej. /showtimetv/...), rompiendo con "Protocol not found".
        # Prefijo ffmpeg:// fuerza el manejo via libavformat que resuelve
        # correctamente URLs relativas contra la base.
        # Detecta tanto .m3u8 (HLS) como .m3u (playlists genéricos).
        # Los streams .ts se reproducen directos (ya van con -demuxer lavf),
        # sin prefijo ffmpeg://.
        url_lower = url.lower()
        if not url_lower.startswith("ffmpeg://") and (
            url_lower.endswith(".m3u8") or url_lower.endswith(".m3u")
            or ".m3u8?" in url_lower or ".m3u?" in url_lower
        ):
            url = "ffmpeg://" + url
    codec_args = _codec_h264_args(player_name)
    title_args = _title_args(channel, player_name)
    # gpu-api=opengl: prioriza OpenGL como API de renderizado de video.
    # Si OpenGL no está disponible, mpv hace fallback automático a
    # Vulkan u otras APIs soportadas. En headless se omite: --vo=null
    # no necesita GPU y forzar opengl podría fallar sin display.
    gpu_args = ["--gpu-api=opengl"] if player_name == "mpv" and not headless else []
    headless_args = _headless_args(player_name) if headless else []
    # `--` marca el fin de las opciones: a partir de aquí sólo va la URL.
    # Ninguna opción de pista se cuela detrás: B1 es innegociable.
    return [
        path,
        *headless_args,
        *codec_args,
        *gpu_args,
        *ipc_opts,
        *args,
        *track_opts,
        *title_args,
        "--",
        url,
    ]


def _ipc_args(player_name: str, ipc_path: str | None) -> list[str]:
    """Argumentos para abrir el canal de control de mpv.

    Sólo mpv; sólo si hay ruta. Con un reproductor distinto se ignora la
    ruta en lugar de pasar una opción que ese binario no entiende (F5d).
    """
    if player_name != "mpv" or not ipc_path:
        return []
    valor = str(ipc_path).strip()
    if not valor or not valor.isprintable() or valor.startswith("-"):
        return []
    return [f"--input-ipc-server={valor}"]


def launch(
    channel: Channel,
    player_name: str | None = None,
    headless: bool = False,
    *,
    selection: Any = None,
    capabilities: Any = None,
    proxy_url: str | None = None,
    ipc_path: str | None = None,
    failures: set[str] | None = None,
    preferred: str | None = None,
) -> subprocess.Popen[bytes]:
    """Lanza el reproductor para `channel` y devuelve el proceso.

    Si no se indica `player_name`, la elección la hace
    :func:`thetvview.player.router.select_backend`, que decide por
    **capacidad** (qué transporte abre cada binario, medido en
    :mod:`thetvview.player.protocols`) y no por «el primero que encuentre»
    (SDD-M §8). Antes de esta fase era un bucle sobre
    ``config.SUPPORTED_PLAYERS``; para HLS el resultado es el mismo, y para
    ``rtsp://`` deja de abrir un reproductor que se quedaría esperando.

    Con ``player_name`` explícito no se consulta el router: el usuario ya
    eligió, y su elección manda aunque la tabla diga que ese binario no abre el
    transporte (puede saber algo que la tabla no; el diagnóstico se lo explica).

    Los argumentos ``selection``/``capabilities``/``proxy_url``/``ipc_path`` y
    los nuevos ``failures``/``preferred`` son opcionales, y su valor por
    defecto es exactamente el comportamiento anterior: sin ellos el comando
    construido es byte a byte el de siempre (§32 del SDD de pistas).

    Args:
        headless: si True, construye el comando con drivers nulos/dummy
            (ver _headless_args) para tests/CI sin display.
        failures: nombres de reproductores que ya fallaron con **este**
            canal. Evita el ciclo mpv→vlc→mpv→vlc del §26.
        preferred: reproductor preferido por el usuario; va primero si puede.

    Raises:
        PlayerError: no hay reproductor disponible o la URL no pasa la
            política. El mensaje está redactado y es apto para un modal.
        NoCompatibleBackend: el transporte no lo abre ningún reproductor
            instalado. Se propaga **sin** envolver para que la UI distinga
            «no se puede» de «falló» (SDD-M §14).
    """
    if player_name:
        candidates = (player_name,)
    else:
        elegido = select_backend(
            _routing_url(channel, proxy_url),
            preferred=preferred,
            failures=failures or (),
        )
        candidates = (elegido.name,)

    last_error: PlayerError | None = None
    for name in candidates:
        path = config.find_player(name)
        if path:
            cmd = command_for(
                channel,
                name,
                player_path=path,
                headless=headless,
                selection=selection,
                capabilities=capabilities,
                proxy_url=proxy_url,
                ipc_path=ipc_path,
            )
            return subprocess.Popen(  # noqa: S603 - argv sin shell
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        last_error = PlayerError(
            "No hay reproductor disponible. Instala uno de: "
            + ", ".join(config.SUPPORTED_PLAYERS)
        )
    raise last_error or PlayerError("No hay reproductor disponible.")


def _routing_url(channel: Channel, proxy_url: str | None) -> str:
    """URL con la que se decide el reproductor.

    Con el proxy de fijado de calidad lo que se reproduce es su master (loopback
    http), no la URL del canal: decidir por la del canal elegiría un
    reproductor capaz de abrir el RTSP original cuando lo que se va a abrir es
    un HTTP en local. Es lo que ya hace ``command_for`` con ``target_url``.
    """
    return proxy_url or channel.url
