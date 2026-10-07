# Acciones contextuales — la convención

La barra inferior delata la pantalla en la que estás. Este documento explica cómo
se declara **qué acciones existen ahí**, para que añadir una sea un cambio en
**un** sitio y no una edición a mano de tres.

El principio que lo gobierna es *define once, render everywhere*:

> Una acción se declara **una vez** y de ella salen la barra, la ayuda y —a
> futuro— el manejo de teclas. Y al revés también: **la barra nunca declara
> nada que la pantalla no haga**. Si la definición se equivoca, la ayuda `?` y
> `handle_key()` siguen siendo la verdad; la barra es una vista, no una fuente
> de autoridad.

---

## 1. Qué es una `Action`

`thetvview/ui/actions.py`:

```python
@dataclass(frozen=True, slots=True)
class Action:
    key: str            # "Enter", "?", "←→" — case exacto, nunca normalizado
    label: str          # vocabulario cerrado (V)
    priority: int = P.SECUNDARIA
    essential: bool = False   # no desaparece mientras haya espacio
    enabled: bool = True      # declarada pero no disponible ahora
```

Tres campos que se parecen y **no** cumplen lo mismo:

| Campo | Pregunta que responde | Efecto |
| --- | --- | --- |
| `priority` | *¿qué se descarta primero?* | Ordena el catálogo |
| `essential` | *¿esto no puede desaparecer?* | Garantiza un hueco |
| `enabled` | *¿está disponible ahora mismo?* | Oculta sin borrar la definición |

`? Ayuda` tiene la **prioridad más baja y `essential=True` a la vez**, y no es
una contradicción: son responsabilidades distintas. La prioridad baja la coloca
al final de la barra; `essential` garantiza que sobreviva al ajuste.

`enabled` es lo que evita que la barra mienta. Antes, «no disponible» significaba
«no lo escribo en la cadena», y por eso podía aparecer `F Favorito` sobre una
tecla que en realidad quita. Con `enabled`, la acción **vive siempre en el
catálogo** y sólo cambian su disponibilidad y su etiqueta.

### Vocabulario cerrado

`class V` es la lista de etiquetas permitidas. `validate()` rechaza con
`AssertionError` cualquier etiqueta que no esté ahí.

Existe para terminar con una deriva concreta: la misma operación se llamaba
`Ver` en un sitio, `Reproducir` en otro y `Play` en un tercero. Si necesitas una
etiqueta nueva, se añade **una vez** en `V` y en ningún otro lugar.

Etiquetas que existen y **no** están en la barra (`resolution`, `t Tema`,
`u Deshacer`, `Ratón`, `q Salir`): son globales o de baja frecuencia, y la
ayuda `?` es donde viven.

---

## 2. La escala de prioridades

```text
class P:
    PRIMARIA      = 100   # Enter Ver / Abrir
    FRECUENTE     =  90   # / Buscar
    CONTEXTUAL    =  80   # f Favorito/Quitar
    ORGANIZACION  =  70   # g Grupos, r Recientes
    SECUNDARIA    =  50   # e EPG, p Reproductor
    NAVEGACION    =  20   # Esc Volver
    AYUDA         =  10   # ? Ayuda
```

Son constantes, no números mágicos: `priority=P.CONTEXTUAL` se lee solo dentro de
seis meses, y `priority=75` no dice nada.

---

## 3. Cómo se declara: `Screen.actions()`

Cada pantalla implementa `actions()` junto a `shortcuts()`:

```python
def actions(self) -> list[Action]:
    if self.searching:
        return [
            Action("Enter", V.CONFIRMAR, P.PRIMARIA),
            Action("Ctrl-U", V.VACIAR, P.FRECUENTE),
            Action("Esc", V.CANCELAR, P.NAVEGACION),
        ]
    channel = self.current_channel()
    hay = channel is not None
    return [
        Action("Enter", V.VER, P.PRIMARIA, enabled=hay),
        Action("/", V.BUSCAR, P.FRECUENTE),
        Action("f", _favorite_label(self._fav_urls, channel), P.CONTEXTUAL, enabled=hay),
        Action("g", V.GRUPO, P.ORGANIZACION),
        Action("e", V.EPG, P.SECUNDARIA, enabled=hay),
        Action("Esc", V.VOLVER, P.NAVEGACION),
        Action("?", V.AYUDA, P.AYUDA, essential=True),
    ]
```

Tres reglas al escribirlas:

1. **Se escribe mirando `handle_key` y `shortcuts` de esa misma pantalla**, no de
   memoria. La invariante de §5 lo comprueba, pero el sitio donde se evita el
   error es aquí.
2. **Nada nuevo**: `actions()` describe lo que la pantalla *ya hace*. Una tecla
   que no responde no se anuncia (nunca se añade comportamiento aquí).
3. **Dos formas de «no disponible», y la diferencia importa**:
   - **no se declara** la acción si su tecla tampoco aparece en `shortcuts()`
     en ese estado (p. ej. `i Info` sin sesión de pistas viva);
   - se declara con `enabled=False` si la tecla se anuncia pero ahora no hay nada
     sobre lo que actuar (p. ej. `f Favorito` sin canales).

### Etiquetas dinámicas

Se derivan del estado real **en el momento de construir la lista**:

| Pantalla | Dinámica |
| --- | --- |
| Canales | `f` → `Favorito` / `Quitar` según el canal bajo el cursor |
| Favoritos | `f` → siempre `Quitar` (todo lo que hay ahí es favorito) |
| Catálogo | `C Contraseña X` sólo si la lista es Xtream |
| EPG | `Enter Ver` / `Enter Archivo` según `has_catchup` |
| Pistas | `Enter Ver` / `Enter Reproductor` según si el player ya está elegido |
| Reproduciendo | `i Info` sólo con sesión de pistas viva |
| Canales / Grupos | en búsqueda: `Enter Confirmar · Ctrl-U Vaciar · Esc Cancelar` |

---

## 4. Cómo se ajusta al ancho: `fit_actions`

La lógica vive en `ui/actions.py`, **no** en el widget: un componente visual no
decide qué se muestra, sólo lo pinta.

```text
1. descartar enabled=False
2. ordenar por priority desc, estable (el orden de declaración desempata)
3. agregar mientras quepa según cell_width
4. 2ª pasada con sólo las essential: si no cabe, sacrifican la no esencial menos
   importante ya presente (nunca a la más importante)
5. nunca truncar una etiqueta a medias
```

Dos topes que hacen la barra breve de verdad: **máximo 6 acciones visibles** y
**sin wrap** (siempre una línea).

`Enter Ver · F Favorito` jamás `Enter Ver · F Favorit…`: lo que no cabe se
descarta entero. Si además sobran acciones, se marca con `…`, que dice «hay más»
sin fingir que se puede leer media etiqueta.

### La medida: `cell_width`

`ui/textwidth.py` existe porque `len()` **no** es una medida de anchura. La línea
del pie contiene `★ ▶ ↑ ↓ ← → …`, y todos son *East Asian Ambiguous*: un carácter
de Python que ocupa una celda en una terminal moderna y dos en una CJK. Medir con
`len()` es incorrecto por construcción.

```python
from .textwidth import cell_width, clip_cells
```

Regla, sin excepciones por símbolo: `W`/`F` → 2 celdas, marcas combinantes → 0,
resto → 1. `clip_cells(s, max)` trunca sin partir nunca un carácter ancho.

Preferencia de la convención: **si ya hay equivalente ASCII en el proyecto, se
prefiere**. `★ ▶` se conservan porque `shortcuts()` los usa y los tests los
asertan; para arte *nuevo* de la barra se evalúa ASCII primero.

---

## 5. La invariante que lo sostiene

```text
keys(actions()) ⊆ keys(shortcuts()) ∧ ⊆ handle_key()
```

Todo lo que la barra anuncia, la pantalla ya lo hace. Está comprobado en
`tests/test_actions_por_pantalla.py`, pantalla por pantalla, y en las dos
mitades: contra `shortcuts()` y contra `handle_key()` (esa segunda es
observacional: se envía la tecla y se mira si algo cambió).

Si añades una acción que no cumple, **falla la suite**. No es una convención de
buenas intenciones.

### Y una más, que no es de la barra: **quién atiende la tecla**

Un botón que se puede pulsar pero no hace nada es peor que un botón que no
existe. `App._tecla_global()` decide el reparto:

1. mientras se escribe (`searching`), las globales van al campo de texto;
2. `q`, `t`, `?`, `p`, `u`, `!` y `Esc` son de la app;
3. **`r` es global (Recientes) salvo que la pantalla la reclame** en su
   `actions()`.

El punto 3 salió de un bug real: el reparto estaba escrito como un
`isinstance(EpgScreen)` para que `r` recargara la guía, y por el mismo camino
`r Limpiar` en Recientes nunca llegaba a limpiar nada — en su lugar apilaba otra
pantalla Recientes. Ahora la pregunta no es «¿qué clase es esta pantalla?» sino
**«¿la pantalla declara esa tecla?»**, que es la misma fuente de verdad y se
mantiene sola.

De ahí sale la otra mitad de la garantía: como el reparto **usa** el catálogo,
una pantalla no puede «ganarse» una tecla que no atiende sin que lo detecte la
invariante de arriba. Ambas cosas se comprueban en
`tests/test_despacho_teclas.py`, que pulsa teclas por el mismo camino que
`App.run`.

---

## 6. Añadir una acción a una pantalla nueva

Checklist. Los pasos 1 y 2 no se saltan:

1. **La tecla ya responde.** Si `handle_key` no la atiende, primero ahí. Este
   sistema nunca introduce comportamiento: describe el existente. Y si la tecla
   es además global (`r`), comprueba que **llega** a la pantalla: el reparto es
   de `App`, no tuyo (§5).
2. **Anúnciala en `shortcuts()`.** `actions()` no puede declarar más que
   `shortcuts()`.
3. **Elige la etiqueta de `V`.** ¿No está? Añádela a `V` en `ui/actions.py`, una
   sola vez. Si no la usas en ninguna pantalla, no la añadas.
4. **Elige la prioridad de `P`.** ¿Es la acción principal de aquí? `PRIMARIA`.
   ¿La usarás siempre? `FRECUENTE`. ¿Depende del contexto? `CONTEXTUAL`.
5. **Decide `essential`.** Sólo para lo que el usuario no debería dejar de ver
   (`? Ayuda`). Casi nada lo es.
6. **`enabled` para el estado.** Si la acción existe siempre pero a veces no hay
   nada sobre lo que actuar: `enabled=<condición>`.
7. **Si hay un estado que cambia las acciones** (búsqueda, sin Reproductor…),
   devuélvelo desde `actions()`. Se evalúa en cada frame, así que no cuela cache.
8. **Corre la suite.** `python -m unittest`. La invariante de §5 revisa sola.

Nada de esto toca el widget. `FooterBar` ya sabe pintarlo.

---

## 7. Pies de modal

Los modales llevan su propio pie (`Enter Confirmar · Esc Cancelar`), porque
mientras hay un modal abierto la barra de la pantalla padre queda tapada y el
usuario necesita saber qué teclas valen **ahí dentro**, no en la pantalla de
detrás.

Es además el sitio donde vive la **configuración**: los ajustes de theTVVIEW son
modales y teclas globales (`t` tema, `p` reproductor), no una pantalla de
ajustes. El catálogo es el mismo `Action`, así que no hay un segundo camino:

```python
modal = Modal("Confirmar", f"¿Eliminar '{name}'?", ["Cancelar", "Eliminar"],
              actions=modal_actions(confirmar=V.ELIMINAR))
```

El modal **crece un poco de ancho** para que el pie quepa entero antes que
recortarlo a medias; sólo si la terminal es más estrecha se ajusta, y entonces
es `fit_actions` quien decide qué sobrevive.

`SearchModal` sigue admitiendo un `hint` de texto (hay callers que lo pasan a
propósito); si no se pasa, la pista se deriva de su `actions()`.

### El modal de error: `wrap` y `keys`

El modal de error es el único que usa las dos extensiones opt-in de `Modal`:

```python
Modal(titulo, cuerpo, ["Aceptar"], acciones, wrap=True,
      keys={ord("R"): "retry", ord("D"): "diagnose"})
```

- **`wrap=True`** envuelve el mensaje al ancho disponible con `wrap_block` en vez
  de recortarlo con `clip_cells`, y hace que el alto se ajuste a la ventana. Sin
  ancho fijo: el ancho sale del bloque ya envuelto. Sólo el modal de error lo
  activa; los otros veinte `Modal` se comportan exactamente igual que antes.
- **`keys`** ata un atajo a un resultado de `handle_key`, y se consulta **antes**
  que la navegación de botones. Es lo que hace que «shortcuts mostrados =
  implementados» sea un invariante comprobable por test y no una promesa
  (§5): sin `keys`, el pie podría anunciar `R Reintentar` y la `R` no haría
  nada.

Y hay una regla que este pie respeta y que conviene conocer **antes** de
construir cualquier otro:

> **Una acción sólo se declara si tiene un callback detrás.**

`App.show_error(err, retry=…, diagnose=…)` construye el pie a partir de lo que
recibe: sin `retry` no existe `R`, sin `diagnose` no existe `D`. No es una
limitación del modal, es la razón de que el pie no pueda mentir.

`StatusBar.show(…, modal=False)` es el otro lado de la misma regla: el modal de
error abre **su** modal, así que la barra no escala también a un
`Modal("Error", …)` genérico que lo taparía.

---

## 8. Referencia rápida

```text
thetvview/ui/actions.py     Action, P, V, validate, fit_actions
thetvview/ui/textwidth.py   cell_width, char_width, clip_cells
thetvview/ui/errormsg.py    UiError, describe_playlist_error,
                            describe_playback_error → qué se le dice al usuario
thetvview/ui/screens.py     Screen.actions() → catálogo de la pantalla
thetvview/ui/app.py         App._despachar_tecla() → quién atiende cada tecla
                            App.show_error() → el pie del modal de error
thetvview/ui/widgets.py     FooterBar.set_actions, Modal(actions=..., wrap=,
                            keys=...), modal_actions() → pie por defecto

tests/test_actions.py               modelo, ajuste y pies de modal
tests/test_actions_por_pantalla.py  la invariante, por pantalla
tests/test_despacho_teclas.py       el reparto global/local, de verdad
tests/test_error_messages.py        atajos truthful, seguridad, terminal corta
docs/actions.md                     este documento
```

Si algo de aquí queda viejo, el sitio para decirlo es el propio código: un
comentario que miente es peor que un comentario que falta.