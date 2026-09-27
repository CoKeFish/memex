"""Documentos que el usuario le ENTREGA a memex: extractos, facturas, contratos.

No es un módulo de dominio (no extrae nada): es la vía de entrada para un archivo suelto, la
contraparte de los ingestores que van a buscar datos solos. Lo que el archivo diga lo extraen los
módulos de siempre a partir del texto que saque el OCR.

Tres formas de entregarlo, todas terminan acá:
- **Telegram**: le mandás el PDF al bot. Funciona desde el teléfono y sigue funcionando cuando
  memex viva en un VPS, que es lo que una carpeta local no puede hacer.
- **Consola**: `memex-documentos add <archivo>`.
- **Carpeta vigilada**: el plugin `carpeta` del cliente local (solo sirve en la máquina de casa).
"""
