# Bloqueo semántico: candidatos a «misma cosa» por significado

## El problema

memex registra la misma cosa del mundo por varias vías: el plan de un trámite, un correo de la
entidad, un chat. Detectar que dos registros son uno era **léxico**: tokens compartidos, `difflib`,
trigramas de Postgres. Esa regla hacía de compuerta antes del juez LLM, y lo que no pasaba la
compuerta nunca llegaba al juez. «Reclamar el pasaporte ordinario» y «Recoger y revisar el
pasaporte» no comparten ninguna palabra útil (el nombre del asunto, «pasaporte», se descarta a
propósito) y nacieron como dos tareas (2026-09-24). Lo mismo pasa con «grabar el video» /
«preparar el video de postulación», «postulación» / «inscribirte».

## Qué dice la literatura

Es *entity matching* con blocking. Los trabajos que revisamos (DeepBlocker, PVLDB 2021; Sparkly,
PVLDB 2023; Zeakis et al., PVLDB 2023; SC-Block 2023; Ditto, PVLDB 2020; Peeters & Bizer, ADBIS
2023 y EDBT 2025; ComEM, COLING 2025) convergen en lo mismo:

- **Bloqueo** barato con alto recall + **matcher** preciso y caro. Se miden por separado: el bloqueo
  solo por recall (que el par verdadero esté entre los candidatos).
- Candidatos por **top-k con piso**, no por umbral global: el umbral se degrada al cambiar los
  datos; k pequeño calibrado hasta recall ≥ 99 %.
- **Léxico ∪ semántico** rinde más que cualquiera solo. Embeddings de oraciones (SBERT-like), no
  de palabras.
- El matcher LLM zero-shot con reglas de dominio en el prompt iguala a modelos entrenados con miles
  de ejemplos y no se cae con entidades nuevas.
- Riesgos: los embeddings no distinguen números («página 42» / «página 43») ni negaciones; los
  jueces se sobre-confían. Vetos deterministas antes del juez.

## Qué hicimos

`src/memex/semantic/`:

- **Embedder** (`embedder.py`): modelo LOCAL multilingüe `paraphrase-multilingual-MiniLM-L12-v2`
  vía fastembed (ONNX, CPU, ~240 MB, ~1,4 ms por título, sin red en runtime; se hornea en la
  imagen del API). `MEMEX_SEMANTIC_PROVIDER=off` lo apaga sin tocar código. Tests con
  `FixedEmbedder` (vectores dados), nunca con el modelo real.
- **Store** (`store.py`): tabla `semantic_vectors` (migración 0112): un vector por `(user, slug,
  ref_id, modelo)` con el hash del texto (no se re-embebe lo que no cambió). `neighbors` calcula
  el coseno en Python sobre el ALCANCE que el llamador ya filtró (las tareas vivas de un asunto,
  las completadas después de una fecha): decenas o cientos de filas, sin pgvector a propósito.
- **Tareas** (`modules/tareas/module.py`): `add` y `update` (título) guardan el vector; `merge` y
  `delete` lo borran. `find_similar_candidates` suma, después de las candidatas léxicas, las vecinas
  por significado (`semantic=True`, `score` = coseno, top-k `MEMEX_SEMANTIC_TOP_K`=3, piso
  `MEMEX_SEMANTIC_MIN_SCORE`=0,62) y antes de los pasos del plan; `find_completed_cover` usa la
  completada más cercana por significado cuando no hay parecido por tokens. **Veto** determinista:
  dos títulos con números y sin ninguno en común no son candidatos por significado.
- **El juez decide siempre.** El bloqueo semántico nunca funde por su cuenta; cada llamada al juez
  queda en `llm_calls` con `semantic`/`plan_step` y el `score`, para calibrar.
- CLI: `memex-tareas embed` embebe lo existente (una vez tras activar); `recheck --ids …` revisa
  pendientes con las vías nuevas.

## Evidencia (datos reales del dueño, 2026-09-24)

71 tareas vivas, 789 pares dentro del mismo asunto, modelo MiniLM:

| Par | Coseno |
|---|---|
| Reclamar el pasaporte ordinario ~ Recoger y revisar el pasaporte | 0,848 |
| Verificar los datos y el estado del pasaporte ~ Recoger y revisar el pasaporte | 0,887 |
| Inscribirte en HackSolsticio ~ Postulación a HackSolsticio | 0,731 |
| Grabar el video de 60 s ~ Grabar los videos | 0,685 |
| Percentil 95 de los pares normales del mismo asunto | 0,555 |
| Percentil 99 | 0,775 |

Con piso 0,62 y top-3, los duplicados del pasaporte entran; «Solicitar el pasaporte
presencialmente» (0,881) también entra y el juez lo separa, como ya hacía. `multilingual-e5-large`
da puntajes más altos pero comprimidos (p95 = 0,897): peor como compuerta, 10× más lento.

## Cómo calibrar

`llm_calls` con `purpose = tareas_judge` guarda por par: `score`, `semantic`, `plan_step`, `same`,
`confidence`. Cada tanto: contar cuántos «same» tenían `score` bajo el piso (recall del bloqueo) y
cuántos candidatos semánticos el juez rechazó (costo). Subir `top_k` si faltan; subir el piso si
sobran. Cambiar de modelo = cambiar `MEMEX_SEMANTIC_MODEL` y la línea del Dockerfile, y correr
`memex-tareas embed` (los vectores viven por modelo; los viejos no compiten).

## Segundo consumidor: asuntos (2026-09-25)

`asuntos/gemelos.py` usa el mismo store con slug `asuntos` (texto = nombre + descripción, porque el
nombre solo no alcanza: «Beca Solsticio 2026 (FIB)» / «HackSolsticio Lisbon 2026» dan 0,52 y con la
descripción 0,81) para proponer, antes de que el asignador abra un asunto nuevo, los del catálogo
que podrían ser la misma cosa; decide el juez `asuntos_judge`. Medido sobre los 83 asuntos del
dueño: p95 de pares distintos 0,45, p99 0,60, pero dos hackathons distintos dan 0,83 — por eso el
coseno propone y nunca decide. Asuntos usa un piso propio de 0,70 (el par real da 0,81; con 0,62
un hackathon de otra ciudad y otra fecha entraba a 0,68 y gastaba juez) y las candidatas por
entidades o tokens compartidos van al juez antes que las semánticas. Los vectores del catálogo se refrescan en cada lote
(`refresh_vectors`), sin comando aparte.

## Siguiente paso natural

Los otros módulos con compuerta léxica (calendario: señal débil de título; identidades: una tercera
fuente de `merge_candidates`; asuntos: pares del curador) pueden consultar el mismo store con su
`slug`. Y el juez en modo «selección» (una nueva contra la lista de candidatas, «cuál o ninguna»,
ComEM 2025) reduce llamadas y falsos positivos frente al par a par.
