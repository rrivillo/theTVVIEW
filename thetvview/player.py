"""Lanzamiento de reproductores externos (mpv/mplayer/vlc), solo stdlib.

Seguridad:
- NUNCA shell=True: el comando siempre es una lista de argv.
- El binario se resuelve con shutil.which (config.find_player).
- Solo se traducen opciones EXTVLCOPT de una lista blanca; todo lo demás
  (incluidos los KODIPROP, específicos de Kodi) se ignora con aviso,
  nunca crashea ni pasa strings arbitrarios al reproductor.

TODO(player):
- Soportar más claves EXTVLCOPT (p. ej. network-caching por reproductor).
- Detectar si el reproductor sigue vivo y reportar códigos de salida.
"""

from __future__ import annotations

import shutil
import subprocess

from . import config
from .models import Channel


class PlayerError(Exception):
    """Error amigable al lanzar un reproductor."""


# Segundos bajo los cuales una muerte del reproductor se considera
# "al instante" (el stream ni siquiera llegó a abrir).
EARLY_EXIT_SECONDS: float = 4.0


def explain_early_exit(returncode: int | None, elapsed_seconds: float) -> str | None:
    """Explica una muerte prematura del reproductor, o None si fue normal.

    - returncode None: sigue vivo (no hay nada que explicar).
    - returncode 0 o duración normal: fin normal, sin mensaje extra.
    - Otro código en <EARLY_EXIT_SECONDS: el stream no abrió. En la
      práctica casi siempre es el proveedor (401/línea caducada o
      bloqueada, URL caída), no el comando local.
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


def command_for(
    channel: Channel,
    player_name: str,
    player_path: str | None = None,
    headless: bool = False,
) -> list[str]:
    """Construye la línea de comando (lista argv) para reproducir `channel`.

    Lanza PlayerError si el reproductor no está disponible o no está
    soportado. Nunca incluye metadatos no validados sin lista blanca.

    Args:
        channel: canal a reproducir.
        player_name: 'mpv', 'mplayer' o 'vlc'.
        player_path: ruta al binario (si None, se resuelve con find_player).
        headless: si True, añade drivers nulos/dummy para tests o
            entornos sin display (no abre ventana ni requiere GUI).
    """
    path = player_path or config.find_player(player_name)
    if not path:
        raise PlayerError(
            f"Reproductor '{player_name}' no encontrado. "
            "Instálalo (p. ej. 'sudo apt install mpv') o elige otro."
        )
    args, _ = _option_args(channel, player_name)
    url = channel.url
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
    return [path, *headless_args, *codec_args, *gpu_args, *args, *title_args, url]


def launch(
    channel: Channel, player_name: str | None = None, headless: bool = False
) -> subprocess.Popen[bytes]:
    """Lanza el reproductor para `channel` y devuelve el proceso.

    Si no se indica `player_name`, usa el primero detectado en orden de
    preferencia (config.SUPPORTED_PLAYERS). Bloquea solo lo que tarda el
    fork/exec; la TUI queda libre mientras el reproductor esté abierto.

    Args:
        headless: si True, construye el comando con drivers nulos/dummy
            (ver _headless_args) para tests/CI sin display.
    """
    candidates = (
        (player_name,) if player_name else config.SUPPORTED_PLAYERS
    )
    last_error: PlayerError | None = None
    for name in candidates:
        path = config.find_player(name)
        if path:
            cmd = command_for(channel, name, player_path=path, headless=headless)
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
