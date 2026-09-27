"""Lado SERVIDOR de WhatsApp: el cliente vivo (`runtime.py`) y todo el SQL (`store.py`).

La frontera ADR-001 manda: `memex.ingestors.whatsapp` (config + parser) no toca DB ni API; lo que
persiste, vincula y vuelca la bandeja vive acá, como `memex.documentos` para los archivos.
"""
