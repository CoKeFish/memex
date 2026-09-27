# Módulo identidades — arquitectura

El **directorio inteligente de personas, organizaciones y productos** de memex: una agenda que se
arma sola a partir de lo que aparece en tus mensajes, más lo que sincronizás de Google/Outlook.
(*Producto* = apps, marcas, herramientas o IAs — ej. Steam, Claude, Unity.)

Su trabajo central, en una frase: cuando algo menciona a una persona, organización o producto,
**decidir si ya lo conocemos o es nuevo** — y evitar tener la misma cosa dos veces.

## Arquitectura

```mermaid
flowchart TB
  subgraph entra["CÓMO ENTRA LA INFO"]
    ext["Mensajes<br/>(la extracción encuentra nombres y orgs)"]
    card["Tarjeta manual<br/>(registrás un contacto a mano)"]
    sync["Tus contactos<br/>(Google / Outlook)"]
  end

  motor["¿YA LO CONOZCO O ES NUEVO?<br/>motor de resolución"]
  dir[("DIRECTORIO<br/>personas · orgs · productos")]
  fusion["FUSIÓN<br/>une duplicados (la IA desempata lo dudoso)"]

  ext --> motor
  card --> motor
  motor --> dir
  sync -. "directo, sin pasar por el motor" .-> dir
  dir <--> fusion

  dir --> grafo["GRAFO DE RELACIONES<br/>conexiones entre personas, orgs y otros datos"]
  dir --> otros["OTROS MÓDULOS<br/>ej. finanzas: ¿quién es esta contraparte?"]

  classDef store fill:#fde68a,stroke:#b45309,color:#1f2937;
  class dir store;
```

**De un vistazo:** la info entra por tres lados (mensajes, tarjeta manual, tus contactos). Los dos
primeros pasan por el **motor** que decide "¿conocido o nuevo?"; los contactos entran **directo**. El
resultado vive en el **directorio**, que una rutina de **fusión** mantiene limpio de duplicados. Y el
directorio **sirve a dos clientes**: el grafo de relaciones y otros módulos que preguntan "¿quién es
este?".

## Responsabilidades

1. **Reconocer quién es quién** — cuando un mensaje menciona a una persona, organización o producto,
   decide si ya está en el directorio o es alguien nuevo.
2. **No duplicar** — si algo se parece mucho a algo que ya existe, lo une; lo dudoso lo deja en cola
   para que la IA lo resuelva después.
3. **Guardar las señas** — correos, teléfonos, usuarios de redes, dominios: las marcas que identifican
   a cada quien.
4. **Conectar personas con organizaciones** — afiliación ("X está en Y") y jerarquía ("esta org
   pertenece a aquella").
5. **Traer tus contactos** — sincroniza directo desde Google/Outlook, sin pasar por el resto del
   sistema.
6. **Dejar rastro** — cada avistamiento queda como evidencia ligada al mensaje de donde salió.
7. **Servir a los demás** — le responde a otros módulos "¿quién es esta contraparte?" (ej. finanzas) y
   alimenta el grafo de relaciones.

## Cómo decide: ¿ya lo conozco o es nuevo?

Es el corazón del módulo. Decide **en capas**, de lo más seguro a lo más dudoso — como un
bibliotecario que primero busca por el número de carné (infalible) y solo si no lo encuentra compara
nombres parecidos.

```mermaid
flowchart TD
  start["La extracción entrega los nombres y orgs<br/>que encontró en un mensaje"]
  start --> known{"¿Lo reconozco por una señal FUERTE?<br/>(mismo correo · mismo usuario · mismo nombre exacto)"}
  known -->|sí| record["Lo anoto como avistamiento,<br/>apuntando a quien ya conozco"]
  known -->|no| fuzzy{"¿Se PARECE a alguien que ya tengo?<br/>(nombre casi igual)"}
  fuzzy -->|"muy parecido"| reuse["Es el mismo: lo reuso<br/>y guardo el nombre como alias"]
  fuzzy -->|"parecido a medias"| grey["Creo uno nuevo provisional<br/>y lo dejo en cola para que la IA desempate"]
  fuzzy -->|"no se parece"| new["Es nuevo: lo creo"]
  reuse --> record
  grey --> record
  new --> record
  record --> done["Se guarda junto con el resto del mensaje"]
  grey -. "más tarde" .-> ai["La IA decide: ¿son el mismo?<br/>→ los fusiona o los deja separados"]
  done -. "aparte" .-> edges["Se tejen las conexiones del grafo<br/>(afiliación, pertenencia, co-ocurrencia)"]

  classDef later fill:#e5e7eb,stroke:#6b7280,color:#374151;
  class ai,edges later;
```

En palabras:
1. **Señal fuerte primero.** Si la mención trae un correo, un usuario de red o un nombre exacto que ya
   tenemos → es esa identidad, sin dudar. (Que un correo venga de un banco **no** convierte a ese
   banco las otras cosas mencionadas adentro: el remitente no se usa para resolver a *terceros*. Los
   `noreply@`/`notifications@` no sirven como clave de identidad.)
2. **Si no, parecido por nombre.** Compara contra los que ya existen: **muy parecido** → es el mismo;
   **parecido a medias** → crea uno nuevo pero lo marca como "dudoso" para revisar; **nada que ver** →
   nuevo.
3. **Siempre deja rastro.** Pase lo que pase, guarda el avistamiento ligado al mensaje.
4. **Lo dudoso y las conexiones se resuelven después** — no en el momento (ver Precisiones).

## Las tres vías de entrada

| Vía | En simple |
|---|---|
| **Mensajes (extracción)** | La principal y automática. La extracción saca nombres/orgs de cada mensaje; el motor decide conocido-vs-nuevo y guarda el avistamiento. |
| **Tarjeta manual** | Vos registrás un contacto a mano (`memex identidad add`). Usa el mismo motor, pero no es "un avistamiento en un mensaje". Es la única vía por la que entra el teléfono. |
| **Tus contactos (Google/Outlook)** | Trae tu agenda directo al directorio, **sin** pasar por el motor ni la IA. |

## Precisiones (lo no obvio)

- **Reconocer ≠ conectar.** Al procesar un mensaje, el módulo solo decide quién es quién y lo guarda.
  Las **conexiones del grafo** (afiliación, pertenencia, co-ocurrencia) se tejen **aparte**, después.
- **Lo dudoso no se decide en caliente.** Cuando dos cosas se parecen "a medias", no se arriesga: las
  deja en una cola y **la IA las desempata más tarde**, con sesgo a mantenerlas separadas si no está
  segura.
- **Tus contactos entran sin IA.** La sincronización con Google/Outlook escribe directo; no gasta
  motor ni LLM.
- **El remitente sí se registra — como su propio avistamiento.** Además de lo que la extracción saca
  del cuerpo, cada mensaje deja un avistamiento de **quién lo envió** (paso determinista, sin IA). La
  política cambia por medio: correo corporativo → la **organización** del dominio; correo personal
  (gmail/outlook…) → la **persona** dueña de la dirección; chat → la **persona** que escribió; cuenta
  social → su **organización**. Es contenido cierto del mensaje, por eso entra con confianza máxima.
  (⚠️ En dominios institucionales compartidos esto sobre-colapsa — ver *Limitaciones conocidas*.)
- **El teléfono solo entra por tarjeta manual** — no se extrae de los mensajes.

---

## Apéndice técnico

Para quien mantiene el código. Vive en `src/memex/modules/identidades/`. Es un `InterestModule`
(consume `EMAIL`/`CHAT`/`SOCIAL`); resuelve con un mecanismo propio (señales fuertes en O(1) +
trigramas `pg_trgm`), sin LLM en línea.

### Equivalencias con el diagrama

| En el diagrama | En el código |
|---|---|
| Motor de resolución (señal fuerte) | `KnownIndex.resolve` — cascada email → dominio→org → handle → nombre → alias (`resolve.py`) |
| Parecido por nombre | `find_fuzzy_candidates` (`pg_trgm`) + umbrales `HIGH=0.92` / `LOW=0.55` (`fuzzy.py`) |
| Reconocer al procesar un mensaje | `IdentidadesModule.persist` → `dedup` (`module.py:97`) |
| Remitente como avistamiento (paso 5) | `weave_sender_structure` (`senders.py`) — remitente de 1ª clase, política por medio |
| Avistamiento (rastro) | fila en `mod_identidades_mentions` |
| Cola de dudosos | `mod_identidades_merge_candidates` |
| La IA desempata | `run_merge_phase2` + `disambiguate_pair` (`dedup_llm.py`), en el ciclo del scheduler |
| Fusión | `merge_identities` (`merge.py`) — cascada completa + auditoría |
| Tarjeta manual | `register_card` (`memex identidad add`) |
| Tus contactos | `run_sync` + `GooglePeopleClient` (`sync.py`, `providers/`) |
| Conexiones del grafo | `_materialize_afiliacion`/`_pertenencia` + `weave_afiliacion` (`relations/deterministic.py`); `generate_cooccurrence` (`relations/cooccurrence.py`); overflow por `run_cooccurrence_llm` |
| Servir a otros módulos | `provide_domain` → `IdentidadesDomainReader` (`domain.py`), vía `ctx.deps['identidades']` |
| Avance del cursor | lo hace el **orquestador** (`_insert_cursor`) en la misma tx que `persist` |

### Qué guarda (tablas)

Identidad **unificada** con discriminador `kind` (`persona|organizacion|producto`) — migraciones
**0033** (reemplazó las tablas separadas de 0029) + **0057** (agregó `producto`):

- `mod_identidades` — el directorio. Columnas generadas para match: `name_norm`, `org_core`.
  `parent_identity_id` = jerarquía (mig **0035**).
- `mod_identidades_identifiers` — correos/teléfonos/handles/dominios/`platform_id` por-fuente (cada
  valor con su plataforma; la UNIQUE es por-identidad, no global por usuario).
- `mod_identidades_person_orgs` — afiliación persona↔org.
- `mod_identidades_mentions` — los avistamientos (evidencia + a quién se resolvió).
- `mod_identidades_merge_candidates` — la cola del desempate LLM.
- `mod_identidades_sites` — sedes de orgs. · `_provider_accounts`/`_sync_runs` — cursor y stats del sync.

Costura con finanzas: `mod_finance_*` referencian `mod_identidades(id)` por `counterparty_identity_id`.

### Archivos clave

| Archivo | Rol |
|---|---|
| `module.py` | Clase `IdentidadesModule`: contrato, `persist`→`dedup`, creación/alias/candidato, menciones, superficie pública y alta manual. |
| `resolve.py` | Motor determinista: `KnownIndex` + cascada de señales fuertes. |
| `senders.py` | Resuelve y persiste el **remitente** de cada mensaje como avistamiento de 1ª clase (política por medio). |
| `fuzzy.py` | Dedup difuso (`pg_trgm` + levenshtein) + umbrales. |
| `normalize.py` | Normalización espejo del SQL (`normalize_match`, `org_core`, `norm_identifier`, `is_role_email`). |
| `schema.py` · `prompt.py` | El contrato de extracción (`IdentityItem`) y el prompt del LLM. |
| `merge.py` · `dedup_llm.py` | Fusión atómica y el desempate LLM (FASE 2). |
| `hierarchy.py` · `relations_llm.py` | Jerarquía (`run_organize`) y co-ocurrencia overflow. |
| `domain.py` | `IdentidadesDomainReader` para módulos dependientes. |
| `sync.py` · `providers/` | Sync de proveedores + cliente Google People + OAuth. |
| `relations/deterministic.py` · `relations/vertices.py` | Tejido de aristas y proyección de vértices. |
| `scheduler/jobs.py` | `run_identidades_cycle`: `sync → merge → organize → co-ocurrencia`. |
| `migrations/0033`, `0035`, `0057`, `0060` | Schema unificado · jerarquía · kind `producto` · `event_id` en menciones. |

### Limitaciones conocidas

Una auditoría de robustez (2026-06-16) documentó riesgos del módulo. Los dos de mayor severidad:

- **Dominio institucional → una sola org (y al sub-nodo equivocado).** El remitente por dominio
  (eTLD+1) colapsa toda una institución en una org e **ignora el `kind`**: personas con correo
  corporativo se funden en la organización del dominio. Reproducido en vivo: 10 remitentes distintos
  `@salesiana.edu.co` (7 personas + oficinas) → un solo departamento.
- **`merge.py` re-apunta la jerarquía sin guard anti-ciclo**, que puede crear un ciclo al fundir un
  ancestro dentro de un descendiente (`would_create_cycle` no se invoca en el merge).

---

*Tipo: Reference/Explanation (arquitectura). Última verificación del contenido contra el código:
**2026-06-17**. Si tocás `resolve.py` / `senders.py` / `merge.py` o el esquema, re-verificá esta página.*

## Reglas de calidad del directorio (2026-09-02)

Medidas sobre 30 correos con etiquetas (experimento `experiments/exp-identidades-calidad/`, fuera del
repo): esperadas halladas 30→32/38, padres correctos 1→5/7, fichas «ruido» 13→2/37, mismo modelo.

- **Principios generales (2026-09-21, `docs/principios-generales.md`).** Vínculo es relación
  DIRECTA con el dueño: quien aparece por hacer su trabajo dentro de algo que otro le presta o le
  anuncia (ponente, quien lo atiende, la organización que un boletín cita) es `mencion`. El
  remitente de chat es UNA identidad: «Nombre | Org» nace como «Nombre» con el visible de alias y
  la organización en `metadata.org_hint` (`normalize.split_display_name`; `hygiene` lo aplica a
  las fichas viejas). Cada mención guarda su vínculo (`metadata.link`); `hygiene` elimina las
  fichas de extracción sostenidas solo por menciones «solo nombrada» y las huérfanas sin mención
  ni identificador.
- **Vínculo antes que nombre.** La extracción declara cómo se relaciona cada identidad con el dueño
  (`IdentityItem.link`: remitente · contraparte · organizador · firmante · miembro · mencion). Una
  `mencion` (empresa de un digest, marca comparada, patrocinador, procesador de pago, herramienta de la
  org) **no crea ficha**: se ata si ya existe con similitud alta; si no, queda como avistamiento sin
  identidad (`resolution_method='unresolved'`). El ruido del directorio era del contrato, no del modelo.
- **Sub-unidades bajo un dominio compartido.** Una mención de organización que resuelve por email o
  dominio a una org cuyo nombre no se le parece (ni trigramas ni contención) es una sub-unidad: se crea
  como **hija** del dueño del dominio y el email pasa a ella (`Carrera de Ingeniería Electrónica ⊂
  Pontificia Universidad Salesiana`). Antes colapsaba en la raíz y la sub-unidad nunca existía.
- **Resolver tipado y sin skip.** Las relaciones traen `relation_kind` ∈ `parte_de` · `fabricado_por`
  · `miembro_de`; «usa / disponible en / menciona / patrocina» se descartan. Los ids absorbidos por una
  fusión de la misma decisión se re-apuntan al superviviente. El resolver mira **todos** los correos de
  la ventana: el skip incremental lo apagaba en 29 de 30 correos (cuesta ~USD 0,0001 por correo).
- **Dominios relay.** `stripe.com`, `luma-mail.com`, `customeriomail.com`… envían por cuenta de terceros:
  no identifican a la org de la mención ni al remitente (`normalize.RELAY_DOMAINS`), simétrico a los
  emails role.
- **Afiliar sin romper la ventana.** `_affiliate` re-apunta un id absorbido (`metadata.merged_from`) y
  omite uno inexistente con log; era el FK que dejó 40 correos en `failing`.
