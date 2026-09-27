"""Capa de seguridad de memex: cripto del vault de credenciales + sesiones de login.

Vive FUERA de `memex.ingestors` a propósito: el descifrado de credenciales ocurre acá (y en
`memex.sources.resolver`), nunca dentro de un ingestor. Así el ingestor recibe los valores ya
resueltos vía el `env` map sin saber su origen, y el aislamiento de ADR-001 se mantiene. Por la
misma razón este paquete SÍ puede tocar `memex.db`.
"""
