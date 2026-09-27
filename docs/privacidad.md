# Privacidad del repo: sin datos personales en el árbol

El repo se comparte (estuvo público en GitHub hasta el 2026-09-26; hoy es privado). Todo lo que
entra en un commit puede terminar afuera: código, tests, docs, docstrings y el historial entero.
Regla: **el árbol describe categorías, nunca casos reales**. Nombres de personas, correos,
teléfonos, ids de chats, organizaciones y programas de la vida del dueño no aparecen ni como
ejemplo ni como fixture.

## Qué pasó (auditoría 2026-09-26)

Un barrido del árbol y del historial encontró: datos reales de un tercero en un test (nombre,
correo, celular, organización), el id real de un grupo de Telegram en dos tests, nombres de
personas de los chats como fixtures, el nombre completo del dueño en un docstring y en tests
(ya públicos), una dirección postal en un test de correo, y organizaciones, becas, trámites y
montos reales en unos 80 archivos de tests y docs. Ningún secreto ni token (eso ya lo cubría
`gitleaks` en pre-commit y `.gitignore` bloquea datos).

Se reemplazó todo por equivalentes ficticios de forma y largo parecidos (para que las pruebas
basadas en tokens sigan valiendo) y se puso una guarda para que no vuelva a entrar.

## La guarda

- **Lista externa** (`memex.privacy`): `$MEMEX_PERSONAL_DENYLIST` o
  `~/.memex/personal-denylist.txt`. Un término por línea, `#` comenta, `re:` = expresión regular;
  el resto se compara por palabra completa sin distinguir mayúsculas. Vive fuera del repo a
  propósito: si estuviera adentro, sería justo lo que no queremos publicar. Sin archivo (otra
  máquina, CI) la lista es vacía y la guarda deja pasar con aviso.
- **Pre-commit** (`scripts/check_personal_data.py`): revisa los archivos de cada commit contra la
  lista; con hallazgos, el commit falla e imprime `archivo:línea: término`. Para revisar todo el
  árbol: `uv run python scripts/check_personal_data.py --all`.
- **Prompts** (`tests/test_principios.py`): ninguna regla de un prompt nombra marcas genéricas ni
  nada de la lista externa. Los prompts describen categorías («un curso», «un programa», «un
  aviso de seguridad de una cuenta»), no casos.

## Cómo escribir fixtures y docs

- Personas: nombres inventados («Marta - Cooperativa», «Danilo Salgado»). Correos en
  `example.com`; teléfonos `+57 300 000 00xx`; ids de chat `-1001234567890`.
- Organizaciones y programas: inventados o genéricos («el instituto», «la beca para un evento»).
- Si un caso real motivó una regla, el docstring cuenta la FORMA del caso («la lámina de una sesión
  se fundió con la de otra por título idéntico»), no el nombre del curso ni de la persona.
- Cualquier identificador nuevo que no quieras ver publicado: agregalo a la lista externa.

## Antes de subir

Lo ya publicado (hasta 1e014df) contiene el nombre completo del dueño en un docstring y en tests;
se limpió en el árbol, pero sigue en el historial remoto. Para subir de nuevo sin arrastrar el
historial local: aplastar los commits locales en uno limpio sobre `origin/main`, o reescribir la
historia (`git filter-repo`) y forzar. Con el repo privado, el riesgo es acotado.
