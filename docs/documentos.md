# Documentos: entregárselos a memex

Para lo que descargás a mano y querés que memex lea: **extractos bancarios**, facturas, contratos.
Tres vías, todas terminan en el mismo lugar.

| Vía | Cómo | Cuándo conviene |
|---|---|---|
| **Telegram** | Le mandás el archivo al bot | **La principal.** Desde el teléfono, y sigue funcionando cuando memex viva en un VPS |
| **Dashboard** | Panel «Subir un documento» en `/carga` | Varios de una, arrastrando desde el explorador |
| **Consola** | `memex-documentos add <archivo> --procesar` | Desde la máquina donde corre memex |
| **Carpeta vigilada** | Dejarlo en la carpeta del plugin `carpeta` | Solo en la máquina de casa (una carpeta local no existe en un VPS) |

Por Telegram el bot responde qué sacó del documento. Por consola, `--procesar` hace lo mismo en el
acto; sin ese flag el archivo queda esperando al procesamiento normal.

## Cómo funciona

```
Telegram / consola / carpeta → inbox + MinIO
                                    ↓
                              OCR (texto del PDF)
                                    ↓
                        extracción → finanzas, calendario…
```

- **La categoría de ingesta es `documentos`** (`SourceKind.DOCUMENTOS`), al lado de correo, chat,
  social y notas. Hoy la consume **finanzas**. Cada vía es su propia fuente (`documentos-telegram`,
  `documentos-consola`, la del plugin `carpeta`), así se ve por dónde entró cada cosa.
- **El archivo ES el mensaje**: el payload (`DocumentPayload`) solo lo identifica (nombre, tipo,
  tamaño, sha256); el contenido llega por el OCR y se concatena al render. Un PDF **digital** se lee
  con su capa de texto: sin llave de visión y sin costo.
- **Identidad por contenido**: el `external_id` lleva el sha256 del archivo. Mandar el mismo
  documento dos veces, o renombrado, NO lo duplica — da igual si lo mandaste por Telegram y después
  por consola. El plugin de carpeta además **no mueve ni toca tus archivos**.
- Entran `.pdf`, `.zip`, `.png`, `.jpg`, `.webp` (hasta 10 MB). Lo demás se rechaza diciendo por qué
  (por Telegram y consola) o se ignora en silencio (en la carpeta, que puede tener cualquier cosa).

## Ponerlo a andar

**Telegram**: nada que configurar. Le mandás el PDF al bot y te responde qué sacó de él.

**Dashboard**: en `/carga`, panel «Subir un documento». Podés seleccionar varios; van de a
uno para ver el progreso, y cada uno dice cuántos datos salieron.

**Consola**:

```powershell
memex-documentos add "C:\Users\vos\Downloads\extracto.pdf" --procesar
memex-documentos add (Get-ChildItem .\Downloads\*.pdf).FullName --procesar   # varios de una
```

**Carpeta vigilada** (opcional, solo sirve en la máquina de casa):

1. Creá la carpeta, p. ej. `C:/Users/vos/Documents/memex`.
2. Instalá y habilitá el plugin del cliente local:

```powershell
uv run memex-local-client plugin install .\plugins\carpeta
uv run memex-local-client plugin enable carpeta
```

3. Configuralo en `~/.memex-local-client/plugins/carpeta/config.toml`:

```toml
folder = "C:/Users/vos/Documents/memex"
recursive = true
max_mb = 10
```

## Límites que conviene saber

| Caso | Qué pasa |
|---|---|
| PDF **digital** (con capa de texto) | Se lee entero, **sin tope de páginas** y sin costo. |
| PDF **escaneado** | Solo las **primeras 5 páginas**, y necesita proveedor de visión (`OCR_API_KEY`). Subilo con `MEMEX_OCR_PDF_MAX_PAGES`. |
| PDF **con contraseña** | Hay que cargarla en `MEMEX_ATTACHMENT_PASSWORDS` (lista separada por comas). Sin eso queda en `error`. |
| **CSV / Excel** | Sin parser. Un CSV **dentro de un ZIP** sí se lee. |
| Documento con **muchísimos movimientos** | Resuelto: se extrae por TRAMOS. Verificado con extractos reales de 33 y de 145 movimientos (145/145, USD 0,011). |

La ventana de procesamiento para `documentos` es **1**: cada documento se procesa solo, para que el
modelo lo lea entero.

## Qué pasa después de entregar un documento

Se lee (OCR si hace falta), se clasifica y se extrae; y enseguida se **consolida finanzas**, así los
movimientos del extracto aparecen en `/finanzas` en el acto. Lo único que queda para después es el
**juez** de duplicados (LLM): un movimiento que tenga un par candidato sin resolver (el mismo cobro
visto también por correo) queda `pending` hasta que pase el job `finance` del scheduler (cada
`MEMEX_SCHEDULER_INTERVAL_FINANCE`, 10 min en la instalación del dueño; juez por codex, costo 0) o
se corra `memex-finance dedup` a mano. No se corre al subir a propósito: son llamadas LLM por par y
no deben colgar la subida ni la respuesta del bot.
