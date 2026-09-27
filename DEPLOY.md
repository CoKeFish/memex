# Deploy — login + vault de credenciales + "Conectar con Google"

Contrato para montar la feature de **cuentas/credenciales** (login del dashboard, vault cifrado,
botón OAuth de Gmail). **Local y VPS NO difieren en código — solo en config (env vars).** Cambiar de
uno a otro = cambiar variables, nunca tocar código.

## Variables de entorno (el contrato)

| Variable | Local (dev) | VPS (prod) | Qué es |
|---|---|---|---|
| `MEMEX_DATABASE_URL` | `…@localhost:5454/memex` | la del VPS | Postgres |
| `MEMEX_SECRET_KEY` | una de prueba | la real (Doppler), **set 1 vez** | Master key del vault. `openssl rand -base64 48`. Global, no per-usuario. |
| `MEMEX_AUTH_ENFORCED` | `false` | `true` | Login real del dashboard |
| `MEMEX_COOKIE_SECURE` | `false` | `true` | Cookie de sesión solo por HTTPS |
| `MEMEX_OAUTH_REDIRECT_BASE_URL` | `http://localhost:5180` | `https://<dominio>` | Origen público del dashboard (para el redirect de OAuth) |
| `MEMEX_GOOGLE_OAUTH_CLIENT_SECRET_JSON` | tu `client_secret.json` **Desktop** | el **Web** | Identidad de la app ante Google |

Detalle de cada una y los defaults opcionales (cookie name, TTL, Argon2): ver `.env.example`.

## Pasos para montar en el VPS (una vez)

1. **Migrar la DB:** `alembic upgrade head` (crea `user_credentials`, `sessions`, `accounts`,
   `account_secrets`, `sources.account_id` + back-fill). Aditivo, no rompe datos.
2. **Setear las env vars** de la tabla en Doppler (sobre todo `MEMEX_SECRET_KEY`, una sola vez).
3. **Crear el cliente OAuth "Web"** en el MISMO proyecto de Google Cloud y registrar el redirect
   `https://<dominio>/api/oauth/google/callback`. Apuntar `MEMEX_GOOGLE_OAUTH_CLIENT_SECRET_JSON` a
   ese JSON.
4. **Onboarding:** el primer usuario se registra en `/login` (signup). No hace falta tocar env por
   cada usuario nuevo — solo se registran.

> **OAuth: por qué Desktop local y Web en el VPS.** En local el retorno es `http://localhost/…`
> (loopback), que el cliente **Desktop** acepta sin registrar nada. En el VPS el retorno es una URL
> pública `https://<dominio>/…`, que solo el cliente **Web** admite (con el redirect registrado).
> El código es el mismo; cambian las 2 env vars de OAuth.

## Verificación (smoke E2E)

Levantar API + dashboard y recorrer el camino feliz **con servicios reales** (no mocks):
`signup → /cuenta → Conectar con Google → consent → token cifrado en el vault + source Gmail creada
→ Fetch trae correos`. Un secreto del vault nunca sale por el API (solo `configured` + `last4`) ni
queda en texto plano en la DB.

Los tests automáticos (suite `pytest`, Google mockeado) ya cubren el comportamiento; el smoke E2E
valida la integración real con Google una vez configurado el cliente Web (o el Desktop en local).

## Notas de seguridad

- El vault **no es zero-knowledge a propósito**: el servidor descifra con `MEMEX_SECRET_KEY` para que
  los ingestors corran **desatendidos**. Quien tenga esa key + la DB puede descifrar — protegé ambas.
- Aislamiento multi-tenant: cada query va scopeada por `user_id`; cruzar cuentas ajenas da 404; cada
  usuario tiene su propio DEK. Un usuario logueado nunca ve credenciales de otro.
- Olvidar contraseña = reset normal **sin perder credenciales** (la key es del servidor, no de la
  contraseña).
