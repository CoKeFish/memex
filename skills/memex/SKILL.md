---
name: memex
description: Registrar y consultar la vida personal del usuario en memex (salud/bienestar, finanzas) llamando sus CLIs deterministas. Usar cuando el usuario reporta algo que hizo (comida, ejercicio, higiene, una factura/gasto) o pide un resumen/estado de sus datos.
---

# memex — registrar y consultar la vida del usuario

memex es la memoria de datos personales del usuario. Vos (el agente) entendés el lenguaje natural y
llamás sus **comandos deterministas** con campos ya estructurados. memex guarda, deduplica, resuelve
identidades y conecta los datos. **Vos no interpretás adentro de memex ni creás relaciones** — solo
traducís el mensaje a una o más llamadas a comandos.

Todo va por **un** comando: `memex <grupo> <comando> [opciones]` (grupos: `bienestar`, `finance`).
`memex help` lista lo que podés hacer.

Reparto: **vos pensás y orquestás; memex guarda, calcula y conecta.** El único razonamiento abierto
(qué hacer, narrar un reporte, leer una imagen) es tuyo; el de dominio (identidad, dedup, racha,
aristas) es de memex.

## Regla de correlación (`--event`)
Si UN mensaje produce VARIOS hechos (p. ej. una factura de un almuerzo = un gasto + una comida),
generá UN id de evento corto (p. ej. `evt-123`) y pasalo con `--event <id>` a CADA comando del
mensaje. memex teje las relaciones entre los hechos que comparten evento. Si el mensaje produce un
solo hecho, no hace falta `--event`.

Todos aceptan `--json`. **La respuesta JSON es la ÚLTIMA línea de stdout** (las anteriores son logs):
parseá la última línea.

## Descubrir los comandos
Si dudás de qué hay o cómo se llama un flag, preguntale a la propia CLI:
```
memex help                   # todos los comandos del agente
memex bienestar help         # detalle del grupo bienestar
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
  existe** (no la crea) y deduplica contra la alerta del banco del mismo cargo.
- Para una FOTO de factura: **vos la leés** (tu visión) y pasás monto/comercio/fecha. memex no lee la
  imagen en este flujo.

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

## Ejemplo — "acá está la factura del almuerzo, comí una milanesa"
```
# 1) un id de evento para este mensaje (con N hechos)
ev="evt-lunch-001"
# 2) el gasto (vos leíste la factura con tu visión)
memex finance   register --event "$ev" --amount 18000 --currency COP --counterparty "Rest X"
# 3) la comida (del texto)
memex bienestar register --event "$ev" --category comida --activity almuerzo --description "milanesa"
```
memex conecta el gasto y la comida con una arista de "mismo_evento" automáticamente.

> La arista de "mismo_evento" se teje **sola al registrar** — no hace falta nada más para que la
> conexión exista. `POST /graph/build` solo re-deriva TODO el grafo en frío (respaldo), no es
> requisito. Un mensaje con UN solo hecho (solo una factura, o solo una comida) **no** necesita
> `--event`: cada comando es su propio hecho.

## Qué NO hacer
- No mandes texto crudo a memex para que "lo procese" — vos estructurás, memex guarda.
- No intentes crear relaciones/aristas a mano: pasás `--event` y memex las teje.
- No registres lo que el usuario NO dijo (no inventes una comida si solo mandó la factura).
