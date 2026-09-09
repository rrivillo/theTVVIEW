# Plan — Xtream en theTVVIEW (TUI curses, stdlib-only)

Fecha: 2026-09-08
Base: `SDD — Soporte de fuentes Xtream... v0.1` adaptado a `AGENTS.md` + código real.
Decisiones usuario: Híbrido (user en disco, password en sesión) / Live+VOD+Series v1 / XMLTV + short_epg v1 / módulos planos en `thetvview/`.

## 1. Gap SDD vs proyecto real
- SDD propone: Python 3.11+, CLI `iptv source add`, `async` + `requests`, `src/iptv/...`, IA. No aplica tal cual.
- Proyecto real: Python 3.13.5, TUI curses `python -m thetvview`, sync `urllib` con timeout, `requirements.txt` vacío, `no shell=True`, `thetvview/*.py` planos + `thetvview/ui/`, `data/playlists.json`, `unittest`.
- Modelos reales: `Channel(name,url,tvg_id,tvg_name,tvg_logo,group,radio,attrs,extra_options)`, `Playlist(name,channels,source,epg_url)`, `Program`, `PlaylistEntry(name,source,added)`. Hay duplicado legacy en raíz `*.py` — canónico es `thetvview/` (lo usa `__main__.py` y README). No tocar raíz.
- Reuso obligatorio: `epg_parser` (cache TTL 12h, `.gz`, `now_playing/next_programme`), `player.py` (argv sin shell, whitelist EXTVLCOPT), `groups/favorites/resolutions`, patrón `_read/_write` atómico de `playlist_manager`.

## 2. Diseño adaptado
### 2.1 HTTP sync stdlib
Nuevo `thetvview/xtream_client.py`: `urllib.request` + `timeout` (def. 10s), `User-Agent: theTVVIEW/1.0`, `json.loads`, mapea a `XtreamError{Network,Auth,RateLimit,InvalidResponse}`. Sin `requests`, sin `async`, sin lógica Xtream fuera del provider.

### 2.2 Errores + secretos
Nuevo `thetvview/xtream_errors.py`: `ProviderError(AuthenticationError, InvalidSourceError, NetworkError, RateLimitError, UnsupportedProviderError)`.
Nuevo `thetvview/xtream_security.py`: `redact_secret(url)`, `normalize_server_url()` (strip `/`, conserva esquema/puerto, `http`≠`https`), validación `server/user/pass` no vacíos. Nunca loggear password ni URL con `password=`.

### 2.3 Provider + modelos
- `thetvview/xtream_config.py`: `@dataclass XtreamConfig(server_url, username, password="")`.
- `thetvview/xtream_provider.py`: `player_api.php?username=&password=&action=` → `authenticate/get_server_info/get_live_categories/get_live_streams/get_vod_categories/get_vod_streams/get_series_categories/get_series/get_series_info/get_short_epg`. Construye stream URLs: `/live/u/p/id.ext`, `/movie/u/p/id.ext`, `/series/u/p/id.ext`.
- Normalización a dominio existente: Live → `Channel` (category→`group`, logo→`tvg_logo`, `tvg_id`=`"xtream:<id>"`, `attrs={"xtream_id","category_id","content_type":"live"}`). VOD/Series → nuevos `@dataclass Movie/Series` mínimos en `thetvview/xtream_models.py` + `Category(id,name,type)` con `ContentType(LIVE,VOD,SERIES)`. Búsqueda/filtros/grupos/favoritos/player siguen usando `Channel`; VOD/Series con pantallas propias reutilizando widgets.
- EPG: mantener `XMLTV` intacto + adaptador `short_epg → Program/Epg` en `thetvview/xtream_epg.py` (parse `epg_listings`, tz UTC si falta).

### 2.4 Persistencia híbrida (backward compatible)
Extender `PlaylistEntry` en `thetvview/playlist_manager.py`: `kind: "m3u"|"xtream" (=m3u por defecto)`, `server_url, username` opcionales, jamás `password` en disco. `load()` ignora entradas viejas sin romper; `add_xtream()/get_credentials()` pide password en memoria (curses prompt sin eco) cada sesión. `remove()` borra entrada + cache `data/xtream_cache/<hash>/` sin dejar secretos.

### 2.5 Cache
`data/xtream_cache/` con TTL como `epg_parser`: `CATEGORY_TTL=24h, CHANNEL_TTL=6h, EPG_TTL=2h`, `force_refresh` con `r`. Nunca cachear password.

### 2.6 TUI curses
`thetvview/ui/screens.py` + `app.py`: tecla `a` → selector `M3U / Xtream`; form Xtream (server, user, pass sin eco); validación 7 pasos SDD como mensajes `✓/✗` humanos sin secretos; no bloquear en resize, timeouts siempre. `source list/test/remove` como acciones del catálogo existente.

## 3. Fases
- F0 Decisiones: cerradas.
- F1 Foundation: `xtream_errors, xtream_client, xtream_security`, `normalize/validación/redact`, config `XTREAM_CACHE_DIR`, tests unit.
- F2 Provider: auth + live + categorías + VOD + series + short_epg, tests con `http.server` falso local.
- F3 Normalización: `xtream_models/normalizer`, mapea a `Channel/Movie/Series`, no rompe `groups/favorites/resolutions/player`.
- F4 Persistencia: `PlaylistEntry v2` + `add_xtream`, migración invisible, remove limpia cache.
- F5 TUI: selector, forms, test/remove, mensajes, prompts password.
- F6 Verificación: `python -m unittest -v` verde + criterios aceptación.

## 4. Tests (stdlib `unittest` + `http.server`, sin credenciales reales)
Unit: normalización URL, validación vacíos, parsing válido/inválido/vacío, `redact_secret`, errores 401/403/429/5xx/timeout/DNS.
Integración: fake server → auth ok/fail, categories, live, vod, series, timeout, malformed.

## 5. Aceptación v1
Crear fuente Xtream desde TUI, sin password en logs/errores/disco, auth validada (no solo HTTP 200), categorías+live+vod+series normalizados, búsqueda igual M3U/Xtream, player funciona, EPG XMLTV intacto + short_epg visible, errores red/autenticación distinguibles, `unittest` verde, remove sin restos, añadir otro provider futuro sin tocar dominio/IA.

## 6. Fuera de alcance
Reproducción nueva, multidispositivo, bypass límites, scraping/proxies, IA semántica (solo dejar modelo normalizado listo).
