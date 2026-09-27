"""Un comando `memex …` dicho en palabras para el chat: plantillas por verbo y un fallback
legible."""

from __future__ import annotations

from memex.assistant.humanize import describe


def test_contacto_gasto_tarea_y_asunto() -> None:
    assert (
        describe(
            "memex identidad add --name 'Efraín Mendoza' --kind persona "
            "--email efrain.mendoza@gmail.com --phone +573000000001 --org ASOVEC "
            "--role 'Presidente nacional fundador' --json"
        )
        == "Guardar contacto: Efraín Mendoza (persona) · ASOVEC · efrain.mendoza@gmail.com · "
        "+573000000001"
    )
    assert (
        describe(
            "memex finance register --amount 195600 --currency COP --direction egreso "
            "--category servicios --counterparty 'FONDO ROTATORIO DEL MIN'"
        )
        == "Registrar gasto: 195.600 COP a FONDO ROTATORIO DEL MIN (servicios)"
    )
    assert (
        describe(
            'uv run memex tareas add --title "Postular a la beca" --due 2026-10-01 --asunto 279'
        )
        == "Crear tarea: «Postular a la beca» (para 2026-10-01) · asunto 279"
    )
    assert describe("memex asuntos plan --id 278 --context 'x'").startswith(
        "Armar el plan del asunto #278"
    )
    assert describe("memex asuntos prioridad --id 278 --level alta --reason r") == (
        "Prioridad alta al asunto #278"
    )
    assert describe("memex asuntos nota --id 277 --text 'el viernes voy'") == (
        "Anotar en el asunto #277: «el viernes voy»"
    )
    assert describe("memex tareas done --id 266") == "Marcar hecha la tarea #266"
    assert describe("memex tareas merge --into 278 --absorb 272") == "Fundir la tarea #272 en #278"
    assert describe("memex asuntos silence 276") == "Silenciar el asunto #276"
    assert describe("memex calendario add --title 'Reunión' --date 2026-09-18 --time 15:30") == (
        "Agendar: «Reunión» · 2026-09-18 15:30"
    )


def test_la_salida_del_cli_sin_codigos_internos() -> None:
    from memex.assistant.humanize import plain_detail

    assert (
        plain_detail("creada: id=1730 'Efraín Mendoza' (persona) · afiliada a #1729 'ASOVEC'.")
        == "creada: 'Efraín Mendoza' (persona) · afiliada a 'ASOVEC'."
    )
    assert plain_detail("ya existía: id=1730 (se sumó el teléfono)\nsegunda línea\ntercera") == (
        "ya existía: (se sumó el teléfono) · segunda línea"
    )
    assert plain_detail('{"id": 9, "title": "x"}') == ""  # JSON: no es para el chat
    assert plain_detail("   ") == ""
    assert plain_detail("x" * 400).endswith("…") and len(plain_detail("x" * 400)) <= 220


def test_fallback_legible_y_texto_raro() -> None:
    assert describe("memex identidad add-id --id 5 --email a@b.c") == "identidad add-id: 5, a@b.c"
    assert describe("memex x") == "memex x"
    assert describe("memex tareas add --title 'comilla \"suelta").startswith("Crear tarea")
    assert len(describe("memex tareas add --title '" + "x" * 500 + "'")) <= 160
