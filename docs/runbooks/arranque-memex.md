# Runbook: arranque automático de memex

Objetivo: que memex vuelva a estar operativo solo tras un reinicio del PC, sin lanzar procesos a
mano en una terminal. Antes, cuatro piezas (bot de Telegram, daemon de notificaciones, dashboard,
cliente local) corrían como procesos sueltos de una sesión y morían al cerrarla.

## Qué arranca solo, y cómo

| Pieza | Mecanismo |
|---|---|
| Postgres, MinIO, API | Contenedores compose con `restart: unless-stopped`. |
| Dashboard (nginx) | Contenedor `dashboard` (`frontend/Dockerfile`), sin profile → arranca con el API. Sirve el build de producción y proxya `/api/*` al contenedor `api`. Puerto **5173**. |
| Scheduler, ingest-scheduler, asistente, notificaciones | Contenedores compose tras profiles; `COMPOSE_PROFILES` (en `.env`) los suma al `up`. Cada uno **idlea** hasta que su toggle lo arma: el scheduler, por el toggle de `/procesamiento` (tabla `scheduler_settings`; hoy `finance` cada 10 min: juez de duplicados + consolidación de finanzas; el intervalo sale de `MEMEX_SCHEDULER_INTERVAL_FINANCE` en `.env`). |
| Docker Desktop | Ya configurado con *Start when you sign in* (`AutoStart: true`). Al iniciar sesión en Windows, levanta los contenedores con `restart: unless-stopped`. |
| Cliente local (Outlook desktop) | `memex-local-client autostart enable`: Tarea Programada ONLOGON si hay permisos de admin; si no (lo normal en una máquina personal), un `.cmd` en la carpeta de Inicio, que corre igual al iniciar sesión y sin ventana. |

Consecuencia: **un reinicio del PC no necesita ningún comando**. Docker Desktop arranca con el
login y restaura los contenedores; la Tarea Programada arranca el cliente local. El script de abajo
solo se corre cuando cambia el código o un secreto.

## Levantar / recrear (cuando cambia algo)

```powershell
.\scripts\memex_up.ps1          # o -Build para reconstruir imágenes
```

`memex_up.ps1` corre `doppler run -- docker compose up -d`: inyecta los secretos (DEEPSEEK, token
del bot, ANTHROPIC, OCR) desde Doppler. Al crear los contenedores esos valores quedan grabados en
su definición y **sobreviven a los reboots** sin volver a inyectarlos.

> **GOTCHA:** no corras `docker compose up -d` sin Doppler mientras los contenedores existen: compose
> vería el env de secretos vaciarse y **recrearía** los contenedores sin ellos (bot y brain caídos).
> Recreá siempre con `memex_up.ps1`.

## Cliente local (una vez)

```powershell
uv run memex-local-client autostart enable    # tarea programada, o carpeta de Inicio sin admin
uv run memex-local-client autostart status    # dice cuál de los dos quedó
```

`enable` intenta primero la Tarea Programada; `schtasks /SC ONLOGON` **exige elevación**, así que
sin admin cae solo al `.cmd` de la carpeta de Inicio (`shell:startup`) y lo dice. `disable` quita
los dos, para que nunca queden dos daemons compitiendo.

## Verificar que funciona

```powershell
docker compose ps                 # api, dashboard, scheduler, ingest-scheduler, assistant,
                                  # notifications: todos Up
# Simular un reinicio: reiniciar los contenedores y ver que reconectan.
docker compose restart
curl http://localhost:8787/version           # API responde
curl -I http://localhost:5173                # dashboard sirve el SPA (200)
curl http://localhost:5173/api/version       # el proxy nginx → API funciona
```

Los daemons quedan corriendo pero **sin actividad** hasta que se arman sus toggles
(`/procesamiento`, `/carga`, `memex-notifications enable`): eso es la cadencia automática, un paso
aparte de este arranque.

### Verificado (2026-09-12)

Tumbando el motor (`wsl --terminate docker-desktop`) los 9 contenedores volvieron solos, con los
secretos intactos: el bot siguió en polling y el daemon de notificaciones arrancó armado, sin
reinyectar Doppler. Tras un reinicio limpio de Docker Desktop (lo que hace un reboot), además,
`:8787` y `:5173` responden desde Windows y el proxy `/api` del dashboard llega al API.

> **Gotcha operativo:** matar la VM de golpe (`wsl --terminate`) deja el reenvío de puertos de
> Docker Desktop *stale*: los contenedores corren y se ven entre sí, pero `localhost:8787` y
> `localhost:5173` dan connection refused aunque `docker compose ps` muestre el mapeo. No pasa en un
> reboot normal (Docker arranca de cero). Si ocurre: cerrar Docker Desktop **y** `com.docker.backend`,
> `wsl --shutdown`, y relanzar Docker Desktop.
