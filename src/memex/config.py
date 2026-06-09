from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MEMEX_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str
    auth_enforced: bool = False
    api_token: str = ""
    log_level: str = "INFO"

    # --- Log sink: persistencia consultable de structlog (ver migración 0020 / ADR-007) ---
    # Un processor no bloqueante persiste cada evento >= log_persist_level a la tabla `log_events`
    # vía una cola en memoria + escritor por lotes. `log_persist=False` deja el sink inerte (tests).
    # La cola es acotada (queue_max): al llenarse descarta el más nuevo y lo CUENTA (no silent cap).
    log_persist: bool = True
    log_persist_level: str = "INFO"
    log_persist_batch_size: int = 100
    log_persist_flush_ms: int = 1000
    log_persist_queue_max: int = 10000
    log_persist_retention_days: int = 30

    # --- Grafo de relaciones: tope de fan-out de la co-ocurrencia (ver relations/deterministic) ---
    # Un correo con MÁS de `cooccurrence_cap` vértices se salta en el paso DETERMINISTA (ahí la
    # co-ocurrencia todos-contra-todos es ruido, C(n,2)). Configurable porque las identidades son
    # el tipo que más rompe el tope (hilos densos de gente): subirlo conserva esos hilos. El mismo
    # valor es el umbral por encima del cual el handler LLM de identidades (relations_llm) releva.
    cooccurrence_cap: int = 8

    # --- Clusterización del grafo: cúmulos (detección de comunidades + validador LLM) ---
    # Detección Louvain (networkx) sobre aristas confirmed + validación LLM por cúmulo. On-demand y
    # apagado por default. Pistas excluidas (peso 0); subir `cluster_w_pista` tras medir. Ver
    # relations/clustering.py.
    cluster_resolution: float = 1.0  # Louvain: >1 cúmulos chicos, <1 grandes
    cluster_seed: int = 42  # determinismo (con networkx pineado)
    cluster_min_size: int = 3  # mínimo de vértices de un cúmulo
    cluster_w_confirmed: float = 1.0  # peso de arista confirmed real
    cluster_w_cooc_confirmed: float = 0.6  # peso de co-ocurrencia confirmada por LLM
    cluster_w_pista: float = 0.0  # peso de pista (0 = excluida); knob para medir
    cluster_pair_weight_max: float = 3.0  # tope al sumar multi-aristas del par
    cluster_recurse_factor: float = 2.0  # re-clusteriza un oversize a esta x resolution
    cluster_recurse_max_depth: int = 2  # profundidad de esa recursión
    cluster_match_jaccard: float = 0.5  # umbral inclusivo detectado vs persistido
    cluster_stable_jaccard: float = 0.9  # deriva >= esto en confirmed: no re-valida
    cluster_reject_memo_jaccard: float = 0.85  # cerca de un rechazo memo: se suprime
    cluster_dissolve_grace: int = 0  # corridas que un confirmed sobrevive sin match
    cluster_max_members: int = 80  # cúmulo mayor a esto no va al LLM (skip + log)
    cluster_min_confidence: float = 0.6  # keep exige confianza >= esto
    cluster_validate_limit: int = 25  # cúmulos por corrida del validador LLM

    # --- Sistema de calidad: detección automática de remitentes no relevantes ("por métricas") ---
    # El job `relevance` (apagado por default) marca como CANDIDATO a un remitente email con volumen
    # >= quality_min_messages y % de relevancia <= quality_max_relevance_pct. Sin auto-aplicar: la
    # acción la confirma el humano (Fase 3). Conservadores a propósito (no proponer filtrar algo que
    # a veces importa); son LA perilla de calibración — no hay umbral hardcodeado en el código.
    quality_min_messages: int = 5
    quality_max_relevance_pct: float = 10.0
    # Juez LLM de zona gris (Fase 5): apagado por default; on-demand (cuesta). Advisory: emite un
    # veredicto de relevancia para informar la cola, NUNCA acciona.
    quality_llm: bool = False

    # --- Vault de credenciales (ver credentials-vault-architecture / ADR auth+vault) ---
    # Llave maestra ÚNICA del servidor, global, configurada una sola vez (Doppler:
    # MEMEX_SECRET_KEY). Envuelve un DEK por-usuario que cifra los secretos de los ingestors.
    # NO es por-usuario; agregar usuarios no la toca. Recomendado: `openssl rand -base64 48`.
    # Vacía → el vault no opera (el fallback env-var-by-name sigue), pero el API igual arranca.
    secret_key: str = ""

    # --- Sesiones / login del dashboard ---
    session_ttl_seconds: int = 60 * 60 * 24 * 14  # 14 días
    cookie_name: str = "memex_session"
    # `Lax` (no `Strict`): el callback de OAuth es una navegación top-level GET que viene de Google
    # (cross-site); con `Strict` el browser NO mandaría la cookie de sesión y el callback no podría
    # validar al usuario. `Lax` la manda en navegaciones top-level GET y sigue bloqueándola en POST
    # cross-site → CSRF en las mutaciones (POST) mitigado.
    cookie_samesite: str = "lax"
    # `Secure` exige HTTPS. En dev (Vite proxy sobre http) va False; en prod poner
    # MEMEX_COOKIE_SECURE=true para que la cookie de sesión solo viaje por TLS.
    cookie_secure: bool = False

    # --- Parámetros Argon2id (hash de contraseña de login) ---
    argon2_time_cost: int = 3
    argon2_memory_cost: int = 65536  # 64 MiB
    argon2_parallelism: int = 4

    # --- OAuth web (botón "Conectar con Google") ---
    # Base pública desde donde el browser llega al API, para armar el redirect_uri exacto que se
    # registra en Google (ej. dev/túnel SSH: http://localhost:8787 ; VPS: https://<dominio>).
    oauth_redirect_base_url: str = ""
    # Ruta al client_secret.json del cliente OAuth tipo "Aplicación web" (identidad de la app, una
    # sola, no per-usuario). Vacía → endpoints OAuth dan 503; el resto del dashboard sigue.
    google_oauth_client_secret_json: str = ""


settings = Settings()
