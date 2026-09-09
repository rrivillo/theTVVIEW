# SDD — Soporte de fuentes Xtream para visor IPTV asistido por IA

**Estado:** Draft  
**Versión:** 0.1  
**Fecha:** 2026-09-08  
**Stack objetivo:** Python 3.11+ / CLI / arquitectura modular

---

## 1. Objetivo

Añadir soporte para fuentes IPTV basadas en **Xtream Codes API** al visor IPTV existente.

El usuario podrá proporcionar una fuente mediante:

1. URL M3U/M3U8 existente.
2. Credenciales Xtream:
   - servidor/base URL
   - username
   - password

El sistema deberá detectar, validar, consultar y normalizar ambas modalidades hacia un mismo modelo interno.

La IA no deberá depender del formato de origen.

---

## 2. Principios de diseño

### 2.1. Xtream es un provider, no un modelo de datos

El resto de la aplicación no debe saber si un canal procede de:

```text
M3U
Xtream
otro provider futuro
```

Todos deben terminar convertidos a los mismos objetos internos.

### 2.2. Separación de responsabilidades

```text
CLI
 │
 ▼
Source Detector
 │
 ├── M3U Provider
 │
 └── Xtream Provider
          │
          ▼
     Normalizer
          │
          ▼
   Domain Model
          │
          ├── Search
          ├── Filters
          ├── EPG
          ├── AI
          └── Player
```

### 2.3. No almacenar contraseñas innecesariamente

Las credenciales Xtream deben mantenerse en memoria durante la sesión siempre que sea posible.

Si se implementa persistencia de fuentes, la contraseña deberá almacenarse de forma segura y nunca aparecer en:

- logs
- errores
- historial de comandos
- prompts enviados a la IA
- mensajes de diagnóstico

---

# 3. Modelo conceptual

Una fuente IPTV debe representarse como:

```python
class IPTVSource:
    id: str
    name: str
    provider_type: ProviderType
    config: ProviderConfig
```

Donde:

```python
ProviderType = {
    "m3u",
    "xtream"
}
```

---

# 4. Configuración Xtream

```python
@dataclass
class XtreamConfig:
    server_url: str
    username: str
    password: str
```

El sistema debe normalizar `server_url`.

Ejemplos conceptuales:

```text
http://example.com:8080
https://example.com
http://example.com:8080/
```

Deben convertirse internamente a una representación canónica.

El sistema no debe asumir que:

```text
http://example.com:8080
```

y

```text
https://example.com:8080
```

son equivalentes.

---

# 5. API Xtream

El provider deberá encapsular completamente las llamadas a la API.

Conceptualmente:

```text
XtreamProvider
│
├── authenticate()
├── get_server_info()
├── get_live_categories()
├── get_live_streams()
├── get_vod_categories()
├── get_vod_streams()
├── get_series_categories()
├── get_series()
├── get_series_info()
└── get_epg()
```

La aplicación no deberá construir URLs Xtream dispersas por el código.

En su lugar:

```python
provider.get_live_streams()
```

será preferible a:

```python
requests.get(
    f"{url}/player_api.php?username={user}&password={password}&action=get_live_streams"
)
```

---

# 6. Cliente HTTP

Crear una capa independiente:

```python
class IPTVHttpClient:
    async def get(...)
    async def request(...)
```

Responsabilidades:

- timeouts
- retries controlados
- HTTP errors
- redirects
- encoding
- user-agent
- rate limiting
- cancelación
- parsing JSON

El provider Xtream no debería implementar lógica HTTP de bajo nivel.

---

# 7. Autenticación

Xtream normalmente utiliza:

```text
server_url
username
password
```

El cliente deberá realizar una llamada inicial de validación mediante la API del provider.

Resultado interno:

```python
@dataclass
class AuthenticationResult:
    success: bool
    server_info: ServerInfo | None
    error: ProviderError | None
```

No se considerará una fuente válida simplemente porque el servidor responda HTTP 200.

Debe verificarse que la respuesta tenga una estructura Xtream válida.

---

# 8. Modelo normalizado

Todos los providers deberán producir modelos comunes.

## Channel

```python
@dataclass
class Channel:
    id: str
    name: str
    stream_url: str
    category_id: str | None
    category_name: str | None
    logo_url: str | None
    tvg_id: str | None
    tvg_name: str | None
    language: str | None
    country: str | None
    metadata: dict
```

## Category

```python
@dataclass
class Category:
    id: str
    name: str
    type: ContentType
```

## ContentType

```python
class ContentType(Enum):
    LIVE = "live"
    VOD = "vod"
    SERIES = "series"
```

---

# 9. Normalización

Xtream puede entregar nombres de campos diferentes a los utilizados por M3U.

Por ello:

```text
Xtream response
       │
       ▼
XtreamNormalizer
       │
       ▼
Channel / Category / Movie / Series
```

El resto de la aplicación sólo consume los modelos normalizados.

---

# 10. Detección automática

El CLI podrá aceptar una entrada genérica:

```bash
iptv add <input>
```

El detector deberá determinar si corresponde a:

```text
M3U URL
Xtream credentials
Xtream URL
archivo M3U
entrada desconocida
```

Ejemplo:

```text
Input
 │
 ├── URL con estructura M3U → M3UProvider
 │
 ├── servidor + username + password → XtreamProvider
 │
 └── desconocido → pedir información adicional
```

La detección debe ser heurística y nunca enviar credenciales a servicios externos únicamente para intentar adivinar el formato.

---

# 11. CLI

Ejemplo de interfaz:

```bash
iptv source add
```

El asistente preguntará:

```text
Tipo de fuente:
> M3U
> Xtream
> Auto-detect
```

Para Xtream:

```text
Server URL:
Username:
Password:
```

Alternativamente:

```bash
iptv source add xtream \
  --server "http://example.com:8080" \
  --username "user" \
  --password-stdin
```

Se recomienda evitar:

```bash
--password "secret"
```

porque puede quedar registrado en el historial del shell.

---

# 12. Validación

Antes de guardar una fuente:

```text
1. Normalizar URL
2. Validar formato
3. Conectar
4. Autenticar
5. Obtener server info
6. Obtener al menos una categoría o contenido
7. Registrar fuente
```

Resultado:

```text
✓ Xtream server detected
✓ Authentication successful
✓ 124 categories
✓ 8,421 live streams

Source added: my-provider
```

---

# 13. Gestión de errores

Crear errores específicos:

```python
class ProviderError(Exception):
    pass

class AuthenticationError(ProviderError):
    pass

class InvalidSourceError(ProviderError):
    pass

class NetworkError(ProviderError):
    pass

class RateLimitError(ProviderError):
    pass

class UnsupportedProviderError(ProviderError):
    pass
```

La CLI debe convertir estos errores en mensajes humanos.

Ejemplo:

```text
✗ Authentication failed

The server was reachable, but the supplied credentials
were rejected.
```

Nunca:

```text
Authentication failed for user=X password=Y
```

---

# 14. Caché

El sistema debería evitar descargar repetidamente grandes catálogos.

Ejemplo:

```text
Xtream API
    │
    ▼
Provider cache
    │
    ▼
Normalized catalog
```

La caché podrá tener TTL configurable:

```python
CATEGORY_TTL = ...
CHANNEL_TTL = ...
EPG_TTL = ...
```

La contraseña no debe almacenarse en la caché de catálogo.

---

# 15. EPG

El soporte EPG deberá ser independiente del provider.

```python
class EPGService:
    async def get_program(...)
```

Xtream puede ser una fuente de EPG, pero la UI sólo debe recibir:

```python
@dataclass
class Program:
    channel_id: str
    title: str
    description: str | None
    start: datetime
    end: datetime
```

---

# 16. Integración con IA

La IA deberá trabajar exclusivamente sobre el modelo normalizado.

Ejemplo:

Usuario:

```text
Busca canales de noticias en español
```

La IA recibe:

```text
Channel[]
```

No:

```text
Xtream API response
```

Esto permite cambiar:

```text
Xtream → M3U → otro provider
```

sin modificar las funciones de IA.

La IA puede ayudar con:

- búsqueda semántica
- clasificación
- recomendaciones
- resolución de nombres
- filtros
- detección de duplicados
- agrupación de contenido

No deberá recibir passwords ni URLs de autenticación completas.

---

# 17. Seguridad

## Requisitos

Las credenciales deberán:

- mantenerse fuera de logs;
- excluirse de prompts;
- excluirse de errores;
- excluirse de telemetría;
- no aparecer en listados de procesos cuando sea evitable;
- no almacenarse en texto plano salvo que el usuario lo solicite explícitamente.

Debe implementarse una función:

```python
redact_secret(value)
```

Ejemplo:

```text
http://example.com:8080/player_api.php?username=foo&password=bar
```

debe aparecer en logs como:

```text
http://example.com:8080/player_api.php?username=foo&password=[REDACTED]
```

---

# 18. Tests

## Unit tests

### URL normalization

```text
server
server/
http://server
http://server:8080
```

### Credential validation

Casos:

- username vacío
- password vacío
- server vacío
- URL inválida

### Response parsing

Probar:

- respuesta válida
- JSON inválido
- campos ausentes
- tipos incorrectos
- respuesta vacía

### Error handling

Probar:

- timeout
- DNS failure
- HTTP 401/403
- HTTP 429
- HTTP 5xx

### Secret redaction

Garantizar que:

```text
password
```

nunca aparezca en logs.

---

# 19. Tests de integración

Usar un servidor HTTP falso/local.

Nunca utilizar credenciales reales durante los tests.

Casos mínimos:

```text
authenticate success
authenticate failure
get categories
get live streams
get VOD
get series
network timeout
malformed response
```

---

# 20. Estructura propuesta del proyecto

```text
src/
└── iptv/
    ├── cli/
    │   ├── commands.py
    │   └── prompts.py
    │
    ├── domain/
    │   ├── channel.py
    │   ├── category.py
    │   ├── content.py
    │   └── epg.py
    │
    ├── providers/
    │   ├── base.py
    │   ├── m3u.py
    │   └── xtream/
    │       ├── client.py
    │       ├── provider.py
    │       ├── models.py
    │       ├── parser.py
    │       └── normalizer.py
    │
    ├── services/
    │   ├── catalog.py
    │   ├── search.py
    │   ├── epg.py
    │   └── ai.py
    │
    ├── security/
    │   ├── secrets.py
    │   └── redaction.py
    │
    └── main.py

tests/
├── unit/
└── integration/
```

---

# 21. Interfaz Provider

Definir una abstracción:

```python
class IPTVProvider(ABC):

    @abstractmethod
    async def connect(self) -> None:
        ...

    @abstractmethod
    async def get_categories(self) -> list[Category]:
        ...

    @abstractmethod
    async def get_live_channels(self) -> list[Channel]:
        ...

    @abstractmethod
    async def get_vod(self) -> list[Movie]:
        ...

    @abstractmethod
    async def get_series(self) -> list[Series]:
        ...
```

Xtream implementará esta interfaz.

M3U implementará la misma interfaz en la medida que sus datos lo permitan.

---

# 22. Criterios de aceptación

La implementación se considera terminada cuando:

- [ ] El CLI permite crear una fuente Xtream.
- [ ] Las credenciales no aparecen en logs.
- [ ] Se valida correctamente la conexión.
- [ ] Se obtienen categorías.
- [ ] Se obtienen canales live.
- [ ] Los canales se convierten al modelo común.
- [ ] La búsqueda funciona igual para M3U y Xtream.
- [ ] La IA recibe únicamente modelos normalizados.
- [ ] Los errores de red y autenticación son distinguibles.
- [ ] Existen tests unitarios.
- [ ] Existen tests de integración con servidor falso.
- [ ] Una fuente Xtream puede eliminarse sin dejar credenciales en cachés/logs.
- [ ] La arquitectura permite añadir otro provider sin modificar el dominio.

---

# 23. Fuera de alcance — primera versión

No implementar inicialmente:

- reproducción de vídeo;
- gestión de múltiples dispositivos;
- bypass de límites de conexión;
- evasión de autenticación;
- scraping de credenciales;
- extracción de cuentas de terceros;
- proxies para ocultar el origen;
- modificación de servidores IPTV.

El objetivo de esta fase es exclusivamente **consumir fuentes Xtream proporcionadas por el usuario y normalizarlas para el visor**.

---

# 24. Roadmap

### Fase 1 — Foundation

```text
Provider interface
Domain models
HTTP client
Secret redaction
```

### Fase 2 — Xtream

```text
Authentication
Server info
Categories
Live streams
VOD
Series
```

### Fase 3 — Normalización

```text
Xtream → Domain
M3U → Domain
```

### Fase 4 — CLI

```text
source add
source list
source test
source remove
```

### Fase 5 — IA

```text
semantic search
natural-language filters
recommendations
```

### Fase 6 — EPG + player

```text
EPG
channel details
stream playback
```

---

# 25. Decisión arquitectónica

La decisión principal de este SDD es:

> **Xtream debe ser implementado como un adaptador/provider que transforma una API IPTV externa en el modelo de dominio interno de la aplicación.**

Esto evita que el proyecto termine acoplado a Xtream y permite posteriormente añadir:

```text
M3U
Xtream
Stalker/Ministra
Jellyfin
Plex
otros providers
```

sin modificar la capa de IA ni la interfaz principal.