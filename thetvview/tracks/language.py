"""Normalización de idiomas (SDD §23), tabla **offline** y sin locale.

Reglas:

- ``es``, ``es-ES``, ``spa``, ``Spanish`` y ``Español`` tienen que acabar en
  la misma representación (``code="es"``) **cuando se puede**, y el valor
  original **nunca** se destruye (``original_code``).
- No depende del locale del sistema: la app se comporta igual en Windows sin
  ``setlocale`` ni en una terminal con ``LANG=C``.
- "No asumir que una etiqueta textual siempre representa correctamente el
  idioma" (SDD §23): sólo se reconocen **nombres exactos** de la tabla. Un
  ``NAME`` tipo "Commentary" o "Dub" no se interpreta como idioma, se
  conserva como etiqueta.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

__all__ = [
    "LanguageInfo",
    "LANGUAGES",
    "normalize_language",
    "normalize_code",
    "base_language",
    "language_label",
    "matches_language",
    "same_language",
    "is_known_language",
]


@dataclass(frozen=True)
class LanguageInfo:
    """Idioma normalizado.

    ``code`` es el código canónico de dos letras cuando se reconoce;
    ``original_code`` es exactamente lo que declaración el proveedor;
    ``label`` es el nombre nativo, listo para la UI.
    """

    code: str | None = None
    original_code: str | None = None
    label: str | None = None
    #: Región ISO-3166 si la variante la traía (``pt-BR`` → ``BR``).
    region: str | None = None

    def __str__(self) -> str:
        return self.label or self.code or self.original_code or ""


#: Tabla offline: código canónico -> (nombre en inglés, nombre nativo,
#: código ISO-639-2/B, código ISO-639-2/T).
#:
#: Los códigos de tres letras son los que aparecen en DASH (``ContentComponent
#: ContentType="audio" Language="spa"``) y en algunos ``LANGUAGE`` de HLS.
#: Se aceptan las dos variantes de la 639-2 (B biblioteca / T terminología).
_TABLE: dict[str, tuple[str, str, str, str]] = {
    "ar": ("Arabic", "العربية", "ara", "ara"),
    "bg": ("Bulgarian", "български", "bul", "bul"),
    "bn": ("Bengali", "বাংলা", "ben", "ben"),
    "bs": ("Bosnian", "Bosanski", "bos", "bos"),
    "ca": ("Catalan", "Català", "cat", "cat"),
    "cs": ("Czech", "Čeština", "ces", "cze"),
    "cy": ("Welsh", "Cymraeg", "cym", "wel"),
    "da": ("Danish", "Dansk", "dan", "dan"),
    "de": ("German", "Deutsch", "deu", "ger"),
    "el": ("Greek", "Ελληνικά", "ell", "gre"),
    "en": ("English", "English", "eng", "eng"),
    "eo": ("Esperanto", "Esperanto", "epo", "epo"),
    "es": ("Spanish", "Español", "spa", "spa"),
    "et": ("Estonian", "Eesti", "est", "est"),
    "eu": ("Basque", "Euskera", "eus", "baq"),
    "fa": ("Persian", "فارسی", "fas", "per"),
    "fi": ("Finnish", "Suomi", "fin", "fin"),
    "fil": ("Filipino", "Filipino", "fil", "fil"),
    "fr": ("French", "Français", "fra", "fre"),
    "ga": ("Irish", "Gaeilge", "gle", "gle"),
    "gl": ("Galician", "Galego", "glg", "glg"),
    "he": ("Hebrew", "עברית", "heb", "heb"),
    "hi": ("Hindi", "हिन्दी", "hin", "hin"),
    "hr": ("Croatian", "Hrvatski", "hrv", "hrv"),
    "hu": ("Hungarian", "Magyar", "hun", "hun"),
    "hy": ("Armenian", "Հայերեն", "hye", "arm"),
    "id": ("Indonesian", "Bahasa Indonesia", "ind", "ind"),
    "is": ("Icelandic", "Íslenska", "isl", "ice"),
    "it": ("Italian", "Italiano", "ita", "ita"),
    "ja": ("Japanese", "日本語", "jpn", "jpn"),
    "ka": ("Georgian", "ქართული", "kat", "geo"),
    "kk": ("Kazakh", "Қазақ тілі", "kaz", "kaz"),
    "ko": ("Korean", "한국어", "kor", "kor"),
    "lt": ("Lithuanian", "Lietuvių", "lit", "lit"),
    "lv": ("Latvian", "Latviešu", "lav", "lav"),
    "mk": ("Macedonian", "Македонски", "mkd", "mac"),
    "ms": ("Malay", "Bahasa Melayu", "msa", "may"),
    "mt": ("Maltese", "Malti", "mlt", "mlt"),
    "nb": ("Norwegian Bokmål", "Norsk bokmål", "nob", "nob"),
    "nl": ("Dutch", "Nederlands", "nld", "dut"),
    "nn": ("Norwegian Nynorsk", "Norsk nynorsk", "nno", "nno"),
    "no": ("Norwegian", "Norsk", "nor", "nor"),
    "pl": ("Polish", "Polski", "pol", "pol"),
    "pt": ("Portuguese", "Português", "por", "por"),
    "ro": ("Romanian", "Română", "ron", "rum"),
    "ru": ("Russian", "Русский", "rus", "rus"),
    "sk": ("Slovak", "Slovenčina", "slk", "slo"),
    "sl": ("Slovenian", "Slovenščina", "slv", "slv"),
    "sq": ("Albanian", "Shqip", "sqi", "alb"),
    "sr": ("Serbian", "Српски", "srp", "srp"),
    "sv": ("Swedish", "Svenska", "swe", "swe"),
    "sw": ("Swahili", "Kiswahili", "swa", "swa"),
    "ta": ("Tamil", "தமிழ்", "tam", "tam"),
    "te": ("Telugu", "తెలుగు", "tel", "tel"),
    "th": ("Thai", "ไทย", "tha", "tha"),
    "tr": ("Turkish", "Türkçe", "tur", "tur"),
    "uk": ("Ukrainian", "Українська", "ukr", "ukr"),
    "ur": ("Urdu", "اردو", "urd", "urd"),
    "vi": ("Vietnamese", "Tiếng Việt", "vie", "vie"),
    "zh": ("Chinese", "中文", "zho", "chi"),
}

#: Códigos sin contenido lingüístico. **No** se traducen: la UI los trata
#: aparte para no mentir ("Sin idioma" en vez de "Desconocido").
NON_LINGUISTIC: dict[str, str] = {
    "mul": "Varios idiomas",
    "und": "Sin idioma",
    "zxx": "Sin idioma",
}

#: `code -> (english, native)`, construido una vez para las consultas.
LANGUAGES: dict[str, tuple[str, str]] = {
    code: (english, native) for code, (english, native, _b, _t) in _TABLE.items()
}


def _build_aliases() -> dict[str, str]:
    """Alias normalizados (minúsculas, sin acentos ni puntuación) -> canónico."""
    aliases: dict[str, str] = {}

    def add(alias: str, canonical: str) -> None:
        key = _fold(alias)
        if key and key not in aliases:
            aliases[key] = canonical

    for code, (english, native, code_b, code_t) in _TABLE.items():
        add(code, code)
        add(code_b, code)
        add(code_t, code)
        add(english, code)
        add(native, code)
    return aliases


def _fold(text: str) -> str:
    """Minúsculas, sin acentos, sin separadores.

    ``"Español"`` y ``"espanol"`` caen en la misma clave, pero **sólo** como
    candidatos: la resolución exacta (con acentos) siempre tiene prioridad.
    """
    folded = unicodedata.normalize("NFD", text.lower())
    return "".join(
        ch if "a" <= ch <= "z" else " " for ch in folded if not unicodedata.combining(ch)
    )


_ALIASES: dict[str, str] = _build_aliases()
#: Versión "dura" (con y sin acentos) para nombres propios de la tabla.
_ALIASES_EXACT: dict[str, str] = {}
for _code, (_en, _native) in LANGUAGES.items():
    for _alias in (_code, _en, _native):
        _ALIASES_EXACT.setdefault(_alias.lower(), _code)
        _ALIASES_EXACT.setdefault(_fold(_alias), _code)
for _code, (_en, _native, _b, _t) in _TABLE.items():
    for _alias in (_code, _en, _native, _b, _t):
        _ALIASES_EXACT.setdefault(_alias.lower(), _code)
        _ALIASES_EXACT.setdefault(_fold(_alias), _code)


def _split_region(raw: str) -> tuple[str, str | None]:
    """``"pt-BR"`` → ``("pt", "BR")``. Acepta ``_`` y espacios."""
    text = raw.strip()
    for sep in ("-", "_", " "):
        if sep in text:
            head, _, tail = text.partition(sep)
            if head and tail:
                region = tail.strip()
                return head.strip(), region.upper() if region else None
    return text, None


def normalize_language(raw: str | None) -> LanguageInfo | None:
    """Normaliza un idioma declarado por el proveedor.

    >>> normalize_language("es-ES").code
    'es'
    >>> normalize_language("spa").label
    'Español'
    >>> normalize_language("Spanish").original_code
    'Spanish'

    Devuelve ``None`` si `raw` está vacío. Si no se reconoce, devuelve un
    :class:`LanguageInfo` con ``code=None`` y ``original_code`` intacto: el
    dato del proveedor no se pierde nunca.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None

    head, region = _split_region(text)

    canonical = _lookup(head)
    if canonical is None:
        # Último intento: nombre nativo con sufijo regional pegado
        # ("Español (España)") -> sólo si la parte antes del paréntesis
        # resuelve. Sin red y sin heurísticas raras.
        paren = text.split("(", 1)[0].strip()
        if paren and paren != head:
            canonical = _lookup(paren)

    if canonical is None:
        return LanguageInfo(code=None, original_code=text, label=None, region=region)

    english, native = LANGUAGES[canonical]
    label = native
    if region and region != canonical.upper():
        label = f"{native} ({region})"
    return LanguageInfo(
        code=canonical,
        original_code=text,
        label=label,
        region=region,
    )


def _lookup(token: str) -> str | None:
    """Resuelve un token suelto a código canónico (o None)."""
    candidate = token.strip()
    if not candidate:
        return None
    lowered = candidate.lower()
    hit = _ALIASES_EXACT.get(lowered)
    if hit:
        return hit
    return _ALIASES.get(_fold(lowered))


def normalize_code(raw: str | None) -> str | None:
    """Sólo el código canónico ("es"), o None si no se reconoce."""
    info = normalize_language(raw)
    if info is None or info.code is None:
        return None
    return info.code


def is_known_language(raw: str | None) -> bool:
    """True si `raw` resuelve a un idioma de la tabla."""
    return normalize_code(raw) is not None


def base_language(code: str | None) -> str | None:
    """Parte base de una variante: ``pt-BR`` → ``pt``."""
    if not code:
        return None
    text = str(code).strip()
    if not text:
        return None
    head, _region = _split_region(text)
    canonical = _lookup(head) or _lookup(text)
    if canonical:
        return canonical
    return head.lower() or None


def language_label(raw: str | None) -> str | None:
    """Nombre nativo del idioma, o el valor crudo si no se reconoce."""
    info = normalize_language(raw)
    if info is None:
        return None
    return info.label or info.original_code


def same_language(a: str | None, b: str | None) -> bool:
    """True si `a` y `b` son **exactamente** el mismo idioma.

    ``es`` y ``es-ES`` **no** son iguales aquí: la cadena de preferencia
    (§12) va de exacto a base, y este es el primer paso.
    """
    if not a or not b:
        return False
    info_a = normalize_language(a)
    info_b = normalize_language(b)
    if info_a is None or info_b is None:
        return False
    if info_a.code and info_b.code and info_a.code == info_b.code:
        # Mismo canónico y misma región declarada → identical.
        return (info_a.region or "") == (info_b.region or "")
    if info_a.code or info_b.code:
        return False
    # Ninguno reconocido: sólo si el bruto coincide literal (p. ej. "Spa").
    return (info_a.original_code or "").lower() == (info_b.original_code or "").lower()


def matches_language(preferred: str | None, candidate: str | None) -> bool:
    """¿La pista `candidate` encaja con la preferencia `preferred`?

    Dos niveles, en este orden (el segundo es el "idioma base" del §12):

    1. coincidencia exacta de idioma **y** región;
    2. coincidencia de idioma base (``es-ES`` encaja con ``es``).

    Nunca por etiqueta textual: ``"Spanish audio"`` no es un idioma.
    """
    if not preferred or not candidate:
        return False
    if same_language(preferred, candidate):
        return True
    base_pref = base_language(preferred)
    base_cand = base_language(candidate)
    if not base_pref or not base_cand:
        return False
    return base_pref == base_cand