# Grafos de conocimiento desde texto: estado del arte (2023–2026) y qué aplicar a memex

> Tipo Diátaxis: **Explanation**. Snapshot de investigación con fecha — los hechos citados congelan a la fecha; las recomendaciones son insumo de diseño, no decisiones (cada una que se adopte → tarea/ADR propio).
- Fecha: 2026-06-09
- Método: barrido deep-research (5 ángulos, 24 fuentes primarias, 119 claims extraídos, 25 verificados adversarialmente 3-0/2-1, 0 refutados) + 3 pasadas dirigidas sobre Graphiti/Zep, familia GraphRAG y pipelines incrementales (fuentes primarias + código fuente de los sistemas).
- Sistema objetivo: el grafo de relaciones de memex (`src/memex/relations/`, `mod_identidades`) — vértices proyectados de tablas de dominio, aristas any-to-any `pista→confirmed/rejected`, dedup difuso de identidades con LLM en zona gris, Louvain + reconciliación por firma/Jaccard + validador LLM de cúmulos.

## TL;DR

La literatura 2023–2026 **valida el diseño actual de memex pieza por pieza**: extracción tipada schema-guided (no OpenIE libre), capa determinista con LLM solo en zona gris (patrón formalizado), re-clustering total + matching post-hoc por firma/Jaccard (exactamente lo que los papers de community detection dinámica terminan haciendo), inbox como procedencia y no como vértice (= episodios de Graphiti), y merge de identidades con rewire de aristas (que **ninguno** de GraphRAG/LightRAG/Graphiti tiene como operación de primera clase). Las mejoras con mejor retorno no están en el pipeline de construcción sino en sus bordes: un *grounder* por-arista antes de confirmar (−35% alucinaciones en el dato de producción de Apple), batch de la zona gris del dedup (4–7× menos costo de API), auditoría humana muestreada de aristas (el juez LLM sobreestima), una heurística anti-deriva de identidad de cúmulos, y Personalized PageRank como herramienta de retrieval para Hermes. La bi-temporalidad estilo Graphiti es portable a Postgres con 3 columnas y vale diseñarla, pero diferirla hasta tener aristas cuya verdad cambia en el tiempo.

## 1. Extracción de entidades y relaciones desde texto

**El campo convergió en schema-guided dinámico, no en OpenIE libre.** La taxonomía estándar divide en schema-guided (estructura, normalización, consistencia) vs schema-free/OpenIE (descubrimiento abierto); los sistemas en producción tratan el esquema como dial: [ODKE+ de Apple](https://arxiv.org/pdf/2509.04696) genera snippets de ontología por tipo de entidad y los inyecta al prompt (195 predicados, 19M hechos extraídos); [OneKE](https://github.com/zjunlp/OneKE) (WWW 2025) ofrece esquema por defecto / Pydantic predefinido / auto-deducción. Los módulos tipados de memex ya son este paradigma.

**Los LLM zero-shot extraen triples relevantes pero no exhaustivos, y la exactitud es la limitación principal** ([estudio multi-modelo, 6 LLMs](https://arxiv.org/pdf/2510.11297), preprint, un dominio): la verificación aguas abajo sigue siendo necesaria.

**Fine-tunear el extractor por dominio destruye la generalización.** Mistral-7B con QLoRA multiplica el desempeño in-domain (G-F1 34.68 vs 18.72 few-shot) pero en out-of-domain pierde contra el modelo original con los mismos ejemplos (G-F1 4.00 vs 12.00) ([Frontiers in Big Data 2025](https://pmc.ncbi.nlm.nih.gov/articles/PMC12237976/), peer-reviewed; dirección corroborada por NAACL 2024). Para fuentes heterogéneas (correo→chat→transacción es un shift mayor que el del experimento): prompting + esquema, no fine-tuning.

**Modelos compactos especializados son alternativa real para NER barato:** [GLiNER2](https://arxiv.org/html/2507.18546v1) (encoder 205M, EMNLP 2025) corre en CPU, interfaz declarativa por esquema, y empata con GPT-4o en NER zero-shot (CrossNER F1 0.590 vs 0.599) con ~2.6× speedup. Caveats: benchmarks de los autores, inglés-céntrico, y los métodos de razonamiento 2025-26 (ReasoningNER ~72 F1) ya superan a ambos.

**La precisión se gana después del extractor, no en él.** ODKE+: un segundo LLM «grounder» que exige que cada hecho esté explícitamente soportado por el contexto redujo extracciones alucinadas 35%; la etapa de corroboración subió la precisión factual de 91% → 98.8% (auto-reportado, un despliegue). Las «gleanings» de GraphRAG (re-preguntar "¿faltaron entidades?" con logit bias) son la palanca opuesta — recall — y están **contraindicadas** para memex mientras el problema vigente sea sobre-extracción (H-8).

## 2. Resolución y deduplicación de entidades

**El régimen de memex es out-of-distribution, y ahí los LLM aplastan a los modelos afinados.** GPT-4 zero-shot iguala o supera a PLMs fine-tuneados con miles de pares (Walmart-Amazon 89.67 vs 86.39 F1 de Ditto); al transferir el PLM a entidades no vistas cae 36–56 puntos de F1 y GPT-4 le gana por 40–68 ([EDBT 2025](https://arxiv.org/abs/2310.11244)). Un sistema personal multi-fuente vive permanentemente en ese régimen → LLM en zona gris es la elección correcta, no un compromiso.

**«LLM solo en zona gris» está formalizado** como verificación selectiva bajo presupuesto: elegir las preguntas de matching que maximizan reducción de incertidumbre por unidad de costo, greedy con garantía ≥ 1−1/√e ([BoostER](https://arxiv.org/abs/2401.03426), WWW 2024 demo).

**Batch prompting: 4–7× menos costo de API con accuracy igual o mejor** ([BATCHER, ICDE 2024](https://arxiv.org/abs/2312.03987)): agrupar varias preguntas de matching en una llamada amortiza la descripción de tarea + demos. Caveat: el prompt caching de proveedores (2024+) erosiona parte del ahorro (tokens cacheados se descuentan, no son gratis).

**Tooling sin LLM para cuando pg_trgm se quede corto:** [Splink](https://github.com/moj-analytical-services/splink) (MoJ UK, activo) = record linkage probabilístico Fellegi-Sunter **no supervisado** (EM, sin labels), para matching multi-campo (nombre+email+teléfono+alias). [UniBlocker](https://arxiv.org/abs/2404.14831) (confianza baja, preprint): blocking denso zero-shot si el volumen de pares explota.

## 3. Grafos temporales/episódicos (Graphiti/Zep)

Fuentes: [paper de Zep](https://arxiv.org/abs/2501.13956) + [código de graphiti](https://github.com/getzep/graphiti) (los detalles finos vienen del código, que diverge del paper en algunos puntos).

- **Modelo de 3 subgrafos:** episodios (dato crudo, provenance, nunca se descarta) → entidades resueltas → comunidades. El episodio de Graphiti es el `inbox` de memex: procedencia, no vértice.
- **La arista-hecho es bi-temporal con 4 timestamps:** `valid_at`/`invalid_at` (línea del **evento**: cuándo el hecho empezó/dejó de ser verdad; los llena el LLM con el `reference_time` del episodio) + `created_at`/`expired_at` (línea **transaccional**: ingesta/superación). Además guarda el hecho como frase NL (`fact`) + embedding + episodios que lo respaldan.
- **Invalidación, nunca borrado:** una sola llamada LLM decide duplicados (vs aristas del mismo par de nodos) y contradicciones (vs candidatos por búsqueda híbrida global); luego lógica determinista: si hay solape temporal, `vieja.invalid_at = nueva.valid_at` y `expired_at = now()`. La arista queda consultable históricamente. Caveat: hechos sin `valid_at` nunca se invalidan por esta vía.
- **Ingesta incremental nativa:** por episodio, sin batch recompute. Antes del LLM hay capa determinista de dedup (normalización exacta → gate de entropía → MinHash/LSH Jaccard 0.9) — espejo del patrón memex.
- **Comunidades por label propagation** (elegido sobre Leiden por su extensión dinámica trivial), con admisión explícita de que la actualización dinámica deriva y "periodic community refreshes remain necessary".
- **Lectura sin LLM:** cosine + BM25 + BFS, rerankers (RRF/MMR/cross-encoder).
- **Benchmarks auto-reportados y flojos:** en DMR el baseline trivial de "conversación completa" ya saca 98%; la latencia "−90%" es mayormente el ahorro del LLM de respuesta con contexto corto. No publican costo de construcción (2–4 llamadas LLM por episodio).

**Portabilidad a Postgres único: total para lo que importa.** Graphiti soporta 4 motores porque toda la inteligencia vive en Python; el motor solo da storage + vector + full-text + traversal. Lo único que se pierde sin motor de grafo es ergonomía en traversals profundos (BFS → `WITH RECURSIVE` acotado a 1–3 saltos, suficiente a escala personal). Columnas para `relation_edges` si algún día se quiere: `valid_at`, `invalid_at`, `expired_at` (+ `created_at` ya existe), opcional `invalidated_by` (FK que Graphiti ni tiene — mejora coherente con la traza de memex). Queries canónicas: vigente = `expired_at IS NULL AND (invalid_at IS NULL OR invalid_at > now())`; verdad en T = `valid_at <= T AND (invalid_at IS NULL OR invalid_at > T)`.

## 4. Familia GraphRAG (Microsoft GraphRAG, LightRAG, HippoRAG, nano-graphrag)

- **[Microsoft GraphRAG](https://arxiv.org/pdf/2404.16130):** chunk → extracción LLM por chunk (+gleanings) → resúmenes de elemento → **Leiden jerárquico** → resúmenes de comunidad por nivel → query global map-reduce. Costo real: indexar 1M tokens = 281 min de gpt-4-turbo; ~$0.34 por 38k tokens (blog de Azure); el modo global quema 10⁴–10⁵ tokens por query. Gana 72–83% vs vector RAG en preguntas de *sensemaking* global, pero el [survey "When to use Graphs in RAG"](https://arxiv.org/html/2506.05690v2) muestra que "frequently underperforms vanilla RAG" en fact retrieval simple (caso extremo: 16% vs 65%).
- **Leiden vs Louvain** ([Traag et al. 2019](https://arxiv.org/abs/1810.08473)): Louvain puede producir comunidades arbitrariamente mal conectadas (hasta 25% mal conectadas, 16% desconectadas); Leiden garantiza conexas + subset-óptimas. El split por componentes conexas que memex ya hace recupera la garantía de conectividad; lo que no recupera es (a) el caso "dos grupos densos unidos por una arista puente" (que el validador LLM cubre mejor, por semántica) y (b) la jerarquía multinivel (solo paga si se quieren resúmenes por nivel). **Ganancia de migrar ≈ 0 a esta escala.**
- **[LightRAG](https://arxiv.org/abs/2410.05779):** entidades + aristas con keywords, retrieval dual-level, **sin comunidades** — su insert incremental es unión de conjuntos con dedup por nombre exacto (estrictamente peor que el dedup difuso de identidades de memex). Su argumento de costo contra GraphRAG (~14M tokens por update solo en re-generar reports) modela al rival pre-incremental, pero ilustra el punto: **el costo de mantenimiento está en los resúmenes LLM, no en el clustering**.
- **[HippoRAG 1/2](https://arxiv.org/html/2502.14802):** OpenIE + aristas de sinonimia + **Personalized PageRank** desde nodos semilla (damping 0.5, especificidad 1/frecuencia-documental). Sin resúmenes de comunidad — integra conocimiento nuevo "simply adding edges". HippoRAG 2 le gana a GraphRAG en multi-hop (MuSiQue F1 48.6 vs 38.5) con prompts ~10³ tokens vs 10⁴–10⁵. La idea exportable más sólida de toda la familia.
- **[nano-graphrag](https://github.com/gusye1234/nano-graphrag):** el core de GraphRAG cabe en ~1.100 líneas. Lección de alcance, no de diseño.

**Veredicto:** la familia GraphRAG resuelve un problema que memex no tiene — fabricar estructura desde texto crudo. El grafo de memex nace estructurado de módulos tipados; el matching por string exacto de GraphRAG/LightRAG es inferior al dedup de identidades existente, y el validador de cúmulos ya produce el equivalente del community report (nombre + descripción) a escala donde regenerarlo cuesta centavos.

## 5. Pipelines incrementales

- **GraphRAG `update` (v0.4+):** corre el pipeline solo sobre docs nuevos, mergea entidades **por título exacto** y **concatena** las comunidades del delta con IDs desplazados — no re-clusteriza el grafo unido; el drift estructural se acumula hasta re-indexar. Más débil que su propio diseño ([issue #741](https://github.com/microsoft/graphrag/issues/741)), que planeaba re-resumir solo comunidades con membresía cambiada y thresholds de drift.
- **Leiden dinámico** ([Sahu et al.](https://arxiv.org/abs/2410.15451)): variantes incrementales dan 3.9–6.1× speedup **del clustering** preservando modularidad (±0.002) — pero la **identidad** de las comunidades no sale del algoritmo: los propios autores añaden tracking post-hoc por mayoría de miembros. No existen garantías formales de estabilidad de particiones (degeneración de modularidad). **La reconciliación por firma/Jaccard de memex es exactamente el patrón que la literatura termina construyendo**; y como el re-cluster total con networkx cuesta milisegundos a esta escala, las variantes incrementales resuelven un problema que memex no tiene.
- **El consenso de facto:** (a) extraer solo el delta; (b) merge determinista barato o LLM puntual on-arrival; (c) re-pasar el LLM **solo por lo que cambió de membresía**; (d) rebuild completo como válvula, no como rutina. El gating por `blob_signature` de `reconcile.py` ya implementa (c).
- **Punto ciego documentado del matching 1:1:** splits y merges de comunidades — la literatura de dynamic community detection los trata como eventos de primera clase (birth/death/merge/split). El modelo partidor de memex cubre la re-partición de un blob que derivó; el caso a revisar es la herencia de identidad cuando un blob se parte en **dos blobs** (¿qué hijo hereda el nombre?).
- **Late-arriving merges (descubrir que dos nodos existentes son el mismo): ninguno de los tres sistemas lo tiene.** GraphRAG y LightRAG acumulan duplicados para siempre; Graphiti solo resuelve on-arrival. El merge de identidades de memex con rewire de `relation_edges` (`modules/identidades/merge.py`) está adelante del estado del arte open-source en este punto; conviene mantenerlo como operación explícita del substrato.

## 6. Evaluación de calidad del grafo

El eslabón débil del campo. (a) El **LLM-as-judge sobreestima**: GPT-4.1 asignó puntajes máximos a extracciones defectuosas ([2510.11297](https://arxiv.org/pdf/2510.11297)); (b) las métricas estructurales (G-F1, T-F1, GED) requieren grafos de referencia que no existen fuera de benchmarks; (c) la práctica de producción es **auditoría humana muestreada** (ODKE+: ~2.000 triples/semana contra barra de ≥95% de precisión). Esto coincide con el hallazgo propio del benchmark de extracción de memex: recall/costo son medibles, la precision/relevancia no tiene ground truth cristalina (es lo que mantiene H-8 en observación).

## 7. Recomendaciones para memex

### Validado por la evidencia — no tocar

| Pieza de memex | Equivalente en el estado del arte |
|---|---|
| Extracción tipada por módulo (schema en el prompt) | Schema-guided dinámico (ODKE+/OneKE), el paradigma ganador |
| Dedup identidades: determinista + LLM zona gris | Verificación selectiva bajo presupuesto (BoostER); LLM domina en OOD (EDBT 2025) |
| Re-cluster total + firma de blob + sync Jaccard | El tracking post-hoc que los papers de Leiden dinámico añaden; gating LLM por cambio = consenso |
| inbox = procedencia, no vértice | Episodios de Graphiti (provenance crudo) |
| Merge de identidades con rewire de aristas | Ninguno de GraphRAG/LightRAG/Graphiti lo tiene first-class |
| Louvain + split por componentes conexas | Recupera la garantía de conectividad de Leiden; el resto no paga a esta escala |
| Validador que nombra/describe cúmulos | Community report en miniatura, al costo correcto |

No adoptar: Leiden/jerarquía (ganancia ≈ 0), label propagation dinámico (memex re-clusteriza gratis), community reports estilo GraphRAG (su costo de mantenimiento es el anti-patrón), keywords de LightRAG, fine-tuning de extractores (mata OOD), gleanings (recall cuando el problema es sobre-extracción).

### Mejoras accionables (por retorno/esfuerzo)

1. **Grounder por-arista en las promociones LLM.** Exigir que toda arista que un LLM confirma venga con soporte textual explícito (cita de la evidencia) y rechazar sin él — endurecer los prompts de `relations_llm.py` (que ya pasa evidencia de menciones) y del validador de cúmulos; obligatorio para la futura Fase 4 (extracción de relaciones por LLM). Dato ancla: −35% alucinaciones, precisión 91→98.8% (ODKE+).
2. **Auditoría muestreada de aristas confirmadas.** El juez LLM sobreestima; la barra real la pone un humano sobre una muestra. Superficie propuesta: `GET /graph/edges/sample?n=20` + vista en el dashboard (rol debug) o CLI `memex-graph audit`, con marca OK/mala por arista (reusar el patrón de relevancia manual). Cierra el loop que H-8 tiene abierto.
3. **Batch de la zona gris del dedup de identidades.** Agrupar los pares dudosos en una llamada (BATCHER: 4–7× menos costo, accuracy igual o mejor) en vez del loop por-ítem de `dedup_llm.py`. Con DeepSeek el ahorro absoluto hoy es chico; el patrón vale al crecer el volumen.
4. **Heurística anti-deriva de identidad de cúmulos.** Un cúmulo puede conservar su ID corrida tras corrida mientras su membresía se aleja de la que el validador bautizó. Guardar la membresía vista al confirmar y disparar `needs_revalidation` cuando `Jaccard(actual, vista) <` umbral, aunque la firma del blob matchee por herencia.
5. **PPR como retrieval del grafo para Hermes.** `networkx.pagerank(personalization={semillas})` + peso de especificidad 1/frecuencia-documental ≈ 10 líneas, determinista, sin LLM en el camino — la evidencia más sólida de la familia GraphRAG (HippoRAG 2 gana multi-hop con prompts 30–300× menores). Superficie: `GET /graph/context?seed=slug:id&k=...`. Implementar cuando Hermes necesite "dame el subgrafo relevante".
6. **Bi-temporalidad en `relation_edges` — diseñada, diferida.** 3 columnas (`valid_at`, `invalid_at`, `expired_at`) + `invalidated_by`, semántica invalidar-no-borrar (ya es la filosofía append-only del proyecto). Adoptarla cuando exista la primera arista cuya verdad cambia en el tiempo (candidata natural: afiliación persona↔org). Sin ese caso de uso, son columnas muertas.
7. **Observación (sin acción):** GLiNER2 como pre-extractor CPU si algún día se quiere NER masivo barato (caveat: inglés-céntrico); Splink si el matching de identidades pasa a multi-campo y pg_trgm se queda corto.

### Preguntas abiertas

- Criterio operativo de precision/relevancia sin ground truth (el mismo hueco de H-8): la auditoría muestreada (rec. 2) es el primer paso instrumental, no la respuesta.
- Si algún día se quieren vistas multi-granularidad ("mi vida en temas → subtemas"), reabrír jerarquía de cúmulos (Leiden recursivo o partición recursiva del validador).

## Fuentes principales

Extracción: [survey 2510.20345](https://arxiv.org/html/2510.20345v1) · [ODKE+ (Apple)](https://arxiv.org/pdf/2509.04696) · [OneKE](https://github.com/zjunlp/OneKE) · [estudio 6-LLMs 2510.11297](https://arxiv.org/pdf/2510.11297) · [FT vs prompting (Frontiers)](https://pmc.ncbi.nlm.nih.gov/articles/PMC12237976/) · [GLiNER2](https://arxiv.org/html/2507.18546v1)
ER: [LLM matchers (EDBT 2025)](https://arxiv.org/abs/2310.11244) · [BoostER](https://arxiv.org/abs/2401.03426) · [BATCHER (ICDE 2024)](https://arxiv.org/abs/2312.03987) · [Splink](https://github.com/moj-analytical-services/splink) · [UniBlocker](https://arxiv.org/abs/2404.14831)
Temporal: [Zep/Graphiti](https://arxiv.org/abs/2501.13956) · [código graphiti](https://github.com/getzep/graphiti)
GraphRAG: [MS GraphRAG](https://arxiv.org/pdf/2404.16130) · [LightRAG](https://arxiv.org/abs/2410.05779) · [HippoRAG 2](https://arxiv.org/html/2502.14802) · [survey 2506.05690](https://arxiv.org/html/2506.05690v2) · [Leiden (Traag 2019)](https://arxiv.org/abs/1810.08473)
Incremental: [GraphRAG issue #741 + código update/](https://github.com/microsoft/graphrag/issues/741) · [Leiden dinámico 2410.15451](https://arxiv.org/abs/2410.15451)
