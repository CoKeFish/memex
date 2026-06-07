---
name: memex
description: Registrar y consultar la vida personal del usuario en memex (salud/bienestar, finanzas, directorio de contactos/identidades) llamando sus CLIs deterministas. Usar cuando el usuario reporta algo que hizo (comida, ejercicio, higiene, una factura/gasto), comparte una tarjeta de contacto / vCard, o pide un resumen/estado de sus datos.
---

# memex — registrar y consultar la vida del usuario

memex es la memoria de datos personales del usuario. Vos (el agente) entendés el lenguaje natural y
llamás sus **comandos deterministas** con campos ya estructurados. memex guarda, deduplica, resuelve
identidades y conecta los datos. **Vos no interpretás adentro de memex ni creás relaciones** — solo
traducís el mensaje a una o más llamadas a comandos.

Todo va por **un** comando: `memex <grupo> <comando> [opciones]` (grupos: `bienestar`, `finance`,
`identidad`). `memex help` lista lo que podés hacer.

Reparto: **vos pensás y orquestás; memex guarda, calcula y conecta.** El único razonamiento abierto
(qué hacer, narrar un reporte, leer una imagen) es tuyo; el de dominio (identidad, dedup, racha,
aristas) es de memex.

## Evento multi-hecho: `start` … `end`
Si UN mensaje produce VARIOS hechos que dependen entre sí (p. ej. una factura = un gasto + una comida
+ el comercio como identidad), **abrí un evento** y cerralo cuando registraste todo:
```
memex start                                   # abre el evento (lo siguiente se ENCOLA, no persiste)
memex identidad add --name "Rest X" --kind organizacion
memex finance   register --amount 50000 --currency COP --counterparty "Rest X"
memex bienestar register --category comida --activity almuerzo
memex end                                     # procesa TODO junto, atómico
```
Al cerrar (`end`), memex procesa los hechos **en orden de dependencia** en una sola transacción:
crea la identidad primero y **ata el gasto a ese comercio por id** (la arista `contraparte`), deduplica
y consolida. Si un hecho es inválido, **se revierte todo** y el evento queda abierto (reintentá `end`).
- **Un evento abierto a la vez.** `memex cancel` descarta el abierto sin guardar nada.
- Para que el gasto se ate al comercio, usá el **mismo nombre** en `--counterparty` y en el `--name`
  de la identidad.
- Un mensaje con **un solo hecho** NO necesita `start`/`end`: registrá directo.

`--event <id>` (sin `start`/`end`) sigue existiendo para correlacionar hechos sueltos por el grafo
(`mismo_evento`), pero **no** ata identidad↔gasto: para eso usá `start`/`end`.

Todos aceptan `--json`. **La respuesta JSON es la ÚLTIMA línea de stdout** (las anteriores son logs):
parseá la última línea.

## Descubrir los comandos
Si dudás de qué hay o cómo se llama un flag, preguntale a la propia CLI:
```
memex help                   # todos los comandos del agente
memex bienestar help         # detalle del grupo bienestar
memex identidad help         # detalle del grupo identidad
memex bienestar register -h  # flags completos de un comando puntual
```

## Registrar — bienestar (comida, higiene, ejercicio, grooming, salud)
```
memex bienestar register --category <comida|higiene|ejercicio|grooming|salud|otros> \
    --activity "<acto>" [--description "<detalle>"] [--occurred-at <ISO8601>] [--event <id>] [--json]
```
- `--activity`: etiqueta CORTA y consistente del acto ("almuerzo", "gimnasio", "cepillado"). Es la
  clave con la que se miden los hábitos: usá SIEMPRE el mismo término para el mismo acto.
- Sin `--occurred-at` se asume ahora.

## Registrar — finanzas (gastos/ingresos)
```
memex finance register --amount <num> --currency <ISO4217> [--direction <ingreso|egreso>] \
    [--category ...] [--counterparty "<comercio>"] [--occurred-at <ISO>] [--event <id>] [--json]
```
- `--amount`: número positivo, SIN separadores de miles. `--currency`: USD, COP, ARS, …
- `--counterparty`: el comercio/persona. memex lo resuelve a una identidad del directorio **si ya
  existe** (no la crea) y deduplica contra la alerta del banco del mismo cargo. Para CREAR la
  identidad desde una tarjeta de contacto, usá `memex identidad add` (abajo).
- Para una FOTO de factura: **vos la leés** (tu visión) y pasás monto/comercio/fecha. memex no lee la
  imagen en este flujo.

## Registrar — identidades (tarjeta de contacto / vCard)
Cuando el usuario comparte una **tarjeta de contacto** (foto, vCard, o datos sueltos de alguien),
**vos la leés** (tu visión) y pasás los campos ya estructurados:
```
memex identidad add --name "<nombre>" --kind <persona|organizacion> \
    [--email <e>] [--phone <t>] [--handle <@>] [--org "<empresa>"] [--role "<rol>"] [--json]
```
- **Resolve-or-create:** memex resuelve contra el directorio (email/handle/nombre + similitud) y
  **crea solo si no existe** — re-registrar la misma tarjeta NO duplica. Vos no deduplicás.
- `--org` (solo para una **persona**) teje la afiliación persona↔organización: si la empresa no
  existe, la crea, y conecta a ambas en el grafo.
- memex **no lee la imagen/vCard**: eso es tuyo; memex guarda y canoniza.
- La identidad queda como vértice del grafo: un gasto futuro a ese comercio/persona se enlazará por
  contraparte.

## Consultar / reportar (solo lectura, JSON)
```
memex bienestar list      [--since <ISO>] [--until <ISO>] [--category ...] [--days N] --json
memex bienestar summary   [--days N] --json
memex bienestar adherence [--periods N] [--tz <IANA>] --json
```
Con estos datos armás vos la respuesta en lenguaje natural (la narrativa, el "estimado" y el juicio
son tuyos; memex da los hechos limpios).

## Hábitos (definirlos cuando el usuario lo pide explícito)
```
memex bienestar habit add --name "<nombre>" --cadence <daily|weekly> [--target N] --activity "<acto>"
memex bienestar habit list
memex bienestar habit rm --id <n>
```

## Ejemplo — "acá está la factura del almuerzo en Rest X, comí una milanesa"
```
memex start                                                          # 1) abrí el evento
memex identidad add --name "Rest X" --kind organizacion             # 2) el comercio (de la factura)
memex finance   register --amount 18000 --currency COP --counterparty "Rest X"   # 3) el gasto
memex bienestar register --category comida --activity almuerzo --description "milanesa"  # 4) la comida
memex end                                                            # 5) procesa todo junto
```
Al cerrar, memex crea el comercio, **ata el gasto a esa identidad** (arista `contraparte`), conecta
gasto↔comida (`mismo_evento`), deduplica y consolida — todo atómico. Si no hubiera comercio que
inferir, omitís el paso 2 (el gasto queda sin contraparte, que es lo correcto).

> Un mensaje con UN solo hecho (solo una factura, o solo una comida) **no** necesita `start`/`end`:
> registrá directo. `POST /graph/build` solo re-deriva TODO el grafo en frío (respaldo), no es requisito.

## Qué NO hacer
- No mandes texto crudo a memex para que "lo procese" — vos estructurás, memex guarda.
- No intentes crear relaciones/aristas a mano: abrís un evento (`start`/`end`) y memex las teje.
- No registres lo que el usuario NO dijo (no inventes una comida si solo mandó la factura).
- No dejes un evento abierto: cerralo con `end` (o descartalo con `cancel`) cuando termines el mensaje.
