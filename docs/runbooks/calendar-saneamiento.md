# Runbook: saneamiento del calendario tras el incidente del forget (2026-06)

**Qué pasó.** El reproceso masivo de 2026-06-08/09 borró los ~568 eventos de Google de
`mod_calendar_events` (`forget_inbox_rows` eliminaba toda fila con `source_inbox_ids = []`; los
eventos de proveedor nacen así). La consolidación no tombstoneaba huérfanos, quedaron ~568
consolidados fantasma vivos y ~33 conflictos pending entre fantasmas. Ambos bugs ya están
corregidos en código; **este runbook repara los DATOS** de la DB de dev.

**Peligro que evita.** La cuenta Google (id=1) tiene `write_back=true` y nunca corrió un push.
Con los fantasmas vivos, un `push` (o el ciclo del scheduler, que termina en push) los **crearía
en el Google Calendar real**. No correr `push` ni habilitar el job `calendar` del scheduler antes
de completar los pasos 1–4.

Todo se corre con `doppler run --` (llaves de OAuth + DB), user 1, cuenta 1.

---

## GATE 0 — precondiciones

- [ ] Rama mergeada a main y `alembic upgrade head` aplicado en dev (necesita la 0059).
- [ ] Suite verde.
- [ ] **OK explícito del dueño** (los pasos 2+ tocan la API real de Google; el 3 gasta LLM).
- [ ] Respaldo: `pg_dump` de la DB dev.

```powershell
docker exec memex-postgres pg_dump -U memex -d memex -F c -f /tmp/memex-pre-saneamiento.dump
docker cp memex-postgres:/tmp/memex-pre-saneamiento.dump .\backups\
```

## Paso 1 — Tombstonear fantasmas (local, sin red, sin costo)

```powershell
doppler run -- memex-calendar-sync consolidate --user 1
```

Esperado: `huerfanos≈568`, `conflictos=0`. Verificar:

```sql
SELECT count(*) FROM mod_calendar_consolidated WHERE deleted AND deleted_source = 'orphaned';
-- ≈ 568
SELECT count(*) FROM mod_calendar_conflicts WHERE status = 'pending';
-- 0
SELECT count(*) FROM mod_calendar_consolidated WHERE NOT deleted;
-- ≈ 14 (solo los de extracción que sobreviven)
```

## Paso 2 — Recuperar los eventos de Google (lee la API real, no escribe)

El pull incremental NO devuelve lo borrado localmente (para el cursor delta esos eventos están
«sin cambios»): hace falta `--full`. La ventana default es 183 días atrás / 365 adelante
(ajustable con `--past-days/--future-days`).

```powershell
doppler run -- memex-calendar-sync pull --user 1 --account 1 --full
```

Esperado: `created` en el orden de los ~568 (depende de la ventana). Luego reconsolidar para que
la capa visible refleje lo recuperado (los consolidados fantasma de instancias idénticas NO
reviven: los eventos nuevos tienen ids nuevos y forman consolidados nuevos):

```powershell
doppler run -- memex-calendar-sync consolidate --user 1
```

Sanity en `/calendario`: la agenda debe mostrar próximos eventos reales.

## Paso 3 — Dedup FASE 2 + merge (GASTA LLM — gate del dueño)

Los 35 eventos de extracción van a marcar pares candidatos contra las copias recuperadas de
Google. Resolverlos con el LLM y reconsolidar:

```powershell
doppler run -- memex-calendar-sync dedup --user 1 --limit 1000
doppler run -- memex-calendar-sync consolidate --user 1
# opcional (enriquecido de títulos/lugares de grupos multi-copia):
doppler run -- memex-calendar-sync merge --user 1 --limit 1000
```

## Paso 4 — Estimar qué crearía el push (obligatorio antes del paso 5)

```sql
SELECT count(*) FROM mod_calendar_consolidated c
WHERE c.user_id = 1 AND NOT c.deleted AND NOT EXISTS (
  SELECT 1 FROM mod_calendar_event_links l
  JOIN mod_calendar_events e ON e.id = l.event_id
  WHERE l.consolidated_id = c.id AND e.provider_account_id = 1
    AND (e.metadata->>'memex_consolidated_id') IS NULL);
```

Ese número es la cantidad de eventos que el push CREARÍA en Google (consolidados sin evento
propio de la cuenta: los de extracción pura). Debe ser CHICO (decenas como mucho). **Si da
cientos → ABORTAR** y revisar los pasos 1–3.

## Paso 5 — Write-back (ESCRIBE en el Google real — gate explícito del dueño)

```powershell
doppler run -- memex-calendar-sync push --user 1 --account 1
```

Esperado: mayoría `saltados` (la cuenta ya tiene sus propios eventos → no se duplican),
`creados` ≈ la estimación del paso 4, `borrados = 0` (los fantasmas no tienen fila de writeback,
se saltan). Después, ingerir los ecos que el push dejó marcados:

```powershell
doppler run -- memex-calendar-sync pull --user 1 --account 1
doppler run -- memex-calendar-sync consolidate --user 1
```

## Paso 6 — Sync automática (decisión aparte, NUNCA antes de 1–5)

El ciclo del scheduler (`pull → dedup LLM → consolidate → merge → push`) corre cada 30 min e
**incluye push**. Habilitarlo solo con los datos ya saneados y el paso 5 verificado:

```powershell
# habilitar el job calendar en scheduler_settings + prender el daemon (PATCH /processing/scheduler
# o SQL directo); verificar después con:
doppler run -- memex-calendar-sync sync-status --user 1
```

---

## Verificación final

- `memex calendario list` muestra los próximos eventos reales.
- `memex calendario sync-status` → `Estado: OK — funcionando`.
- `/calendario`: agenda cronológica, conflictos colapsados por serie (un item con `×N`), panel
  de sync con la tira de estado en verde.
