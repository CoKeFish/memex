"""HTML de un correo → texto, con el `alt` de las imágenes adentro.

Vive acá y no dentro del parser de IMAP porque hay DOS caminos de entrada de correo con el mismo
problema: el IMAP (Gmail) y el plugin de Outlook del cliente local (el correo de la universidad).
Un boletín puede llevar todo lo que importa dentro de una imagen —el nombre del evento, la fecha,
el botón de inscripción— y ese texto solo está en el `alt`.

Descargar y OCR-ear esas imágenes se midió el 2026-09-04 y NO conviene por default: cuesta entre
20 y 40 veces lo que la extracción de texto, avisa al remitente que abriste el correo (las URLs
llevan token) y 8 de cada 20 imágenes no tienen texto. El `alt` es gratis, no toca la red y no
le avisa a nadie.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from io import StringIO

from memex.logging import get_logger

_log = get_logger("memex.core.html_text")

#: Caracteres de ancho cero que los boletines usan como relleno del «preheader» (para que el
#: cliente de correo no muestre el cuerpo en la vista previa): U+034F (combining grapheme joiner),
#: ZWSP/ZWNJ/ZWJ, word joiner e invisibles matemáticos, fillers hangul/braille, marcas LRM/RLM.
#: Medido 2026-09-04 en 300 correos: 110 traían tiras así y eran el 11,8 % del texto HTML.
_INVISIBLE = (
    "\u034f\u200b\u200c\u200d\u2060\u2061\u2062\u2063\u2064"
    "\u180e\u2800\u3164\u115f\u1160\uffa0\u200e\u200f"
)
#: Una TIRA = 3 o más de esos caracteres seguidos, con CUALQUIER espacio en blanco entre medio
#: (`\s`: NBSP, el figure space U+2007 que usa Google, saltos de línea del HTML fuente…). La 1.ª
#: versión solo admitía espacio/tab/NBSP y dejaba pasar 41 de 110 tiras (2026-09-04).
_PADDING_RUN = re.compile(f"\\s*(?:[{_INVISIBLE}]\\s*){{3,}}")
#: Guion suave (U+00AD) y BOM (U+FEFF) se quitan SIEMPRE: nunca son contenido; quitar el guion
#: suave deja la palabra entera («in\u00adfor\u00admation» → «information»). Medido: 0 usos dentro
#: de palabra en la muestra; 7.214 en relleno.
_ALWAYS_DROP = re.compile("[\u00ad\ufeff]")


def strip_invisible_padding(text: str) -> str:
    """Quita el relleno invisible sin tocar usos legítimos: un ZWJ suelto arma emojis compuestos
    (👨‍👩‍👧) y un ZWNJ suelto separa letras en persa/árabe; ambos van de a UNO entre caracteres
    visibles, nunca en tiras de 3+ con espacios, así que la regla de tira los conserva."""
    return _PADDING_RUN.sub(" ", _ALWAYS_DROP.sub("", text))


#: `alt` que no dice nada: el rótulo genérico que ponen las plantillas de boletín. Se comparan en
#: minúscula y sin espacios de sobra. Todo lo demás con 3+ caracteres se toma como contenido.
ALT_NOISE = frozenset(
    {
        "image",
        "img",
        "imagen",
        "logo",
        "icon",
        "icono",
        "banner",
        "photo",
        "foto",
        "picture",
        "avatar",
        "header",
        "footer",
        "spacer",
        "divider",
        "separator",
        "no title",
        "sin titulo",
        "sin título",
        "untitled",
    }
)


class HtmlTextExtractor(HTMLParser):
    """HTML → texto. Incluye el `alt` de las imágenes: en un boletín eso suele ser TODO el
    contenido (el nombre del evento, la fecha, el botón), y sale gratis y sin tocar la red."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._buf = StringIO()
        self._skip_depth = 0
        self._last_alt = ""
        self.alts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style", "head"):
            self._skip_depth += 1
        elif tag == "img":
            self._write_alt(attrs)
        elif tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self._buf.write("\n")

    def _write_alt(self, attrs: list[tuple[str, str | None]]) -> None:
        """Escribe el `alt` si aporta. Descarta el genérico y el repetido seguido (el logo de
        cabecera aparece cinco veces en la misma plantilla)."""
        if self._skip_depth:
            return
        raw = next((v for k, v in attrs if k == "alt" and v), "") or ""
        alt = re.sub(r"\s+", " ", raw).strip()
        if len(alt) < 3 or alt.lower() in ALT_NOISE or alt == self._last_alt:
            return
        self._last_alt = alt
        self.alts.append(alt)
        self._buf.write(f"\n{alt}\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in ("p", "div", "tr", "li", "h1", "h2", "h3", "h4"):
            self._buf.write("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._buf.write(data)

    def text(self) -> str:
        return self._buf.getvalue()


def html_to_text(html: str) -> tuple[str, list[str]]:
    """Devuelve (texto, textos de las imágenes). Los `alt` van INLINE en el texto y también
    aparte, porque cuando el cuerpo elegido es el de texto plano hay que sumárselos."""
    parser = HtmlTextExtractor()
    try:
        parser.feed(html)
        parser.close()
    except Exception as e:
        # El cuerpo queda PARCIAL (solo lo acumulado hasta el fallo); sin rastro, el recorte
        # del texto del mensaje era invisible.
        _log.warning(
            "html_to_text.partial",
            exc_type=type(e).__name__,
            exc_msg=str(e),
            html_len=len(html),
            extracted_len=len(parser.text()),
        )
    raw = strip_invisible_padding(parser.text())
    lines = [re.sub(r"\s+", " ", line).strip() for line in raw.split("\n")]
    return "\n".join(line for line in lines if line), parser.alts


def with_image_text(body: str, alts: list[str]) -> str:
    """Suma al cuerpo el texto de las imágenes que no esté ya dicho.

    El cuerpo de texto plano de un correo NUNCA puede traer el texto de una imagen, así que elegir
    entre texto y HTML «por el más largo» lo pierde siempre. Sumarlo no pierde nada.
    """
    faltantes = [a for a in dict.fromkeys(alts) if a not in body]
    if not faltantes:
        return body
    return body.rstrip() + "\n" + "\n".join(faltantes)
