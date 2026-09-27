"""Troceo de documentos largos en la extracción.

Un extracto bancario es UNA unidad de trabajo con decenas de movimientos. Con una sola llamada la
respuesta del modelo se corta (`finish_reason="length"`), y como una respuesta truncada se trata
como error reintentable, el documento no daba NINGÚN dato: medido con un extracto real de 33
movimientos, el más chico de los 19. Partiendo el texto en tramos, cada llamada entra holgada.
"""

from __future__ import annotations

from decimal import Decimal

from memex.llm import LLMResult, LLMUsage
from memex.modules.orchestrator import _DOC_MAX_CHUNKS, _merge_results, split_text_chunks


def _texto(lineas: int, ancho: int = 70) -> str:
    return "\n".join(f"{i:03d} " + "x" * ancho for i in range(lineas))


def test_un_texto_corto_no_se_trocea() -> None:
    texto = _texto(3)
    assert split_text_chunks(texto) == [texto]


def test_el_corte_respeta_los_fines_de_linea() -> None:
    """Cortar a la mitad de una línea partiría un movimiento en dos: el modelo inventaría o
    perdería el dato."""
    texto = _texto(200)
    tramos = split_text_chunks(texto, max_chars=500)

    assert len(tramos) > 1
    # Nada se pierde ni se duplica: pegados vuelven a ser el original.
    assert "".join(tramos) == texto
    for tramo in tramos[:-1]:
        assert tramo.endswith("\n"), "un tramo cortó a mitad de línea"


def test_cada_tramo_respeta_el_tope() -> None:
    tramos = split_text_chunks(_texto(120), max_chars=400)
    assert all(len(t) <= 400 for t in tramos)


def test_una_linea_mas_larga_que_el_tope_queda_sola() -> None:
    """No se parte una línea aunque exceda: partirla es peor que un tramo grande."""
    larga = "y" * 900
    tramos = split_text_chunks(f"corta\n{larga}\notra\n", max_chars=300)
    assert larga + "\n" in tramos


def test_el_troceo_nunca_recorta_el_texto() -> None:
    """Un techo que recorta perdería movimientos EN SILENCIO. El troceo devuelve todo; quien decide
    que son demasiados tramos es el extractor, y falla con el motivo a la vista."""
    texto = _texto(4000)
    tramos = split_text_chunks(texto, max_chars=100)
    assert len(tramos) > _DOC_MAX_CHUNKS
    assert "".join(tramos) == texto


def _result(content: str, *, prompt: int, completion: int, cost: str, ms: int) -> LLMResult:
    return LLMResult(
        content=content,
        model="m",
        usage=LLMUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            cache_hit_tokens=0,
        ),
        cost_usd=Decimal(cost),
        latency_ms=ms,
        finish_reason="stop",
    )


def test_el_costo_de_varios_tramos_se_suma() -> None:
    """Lo que se registra debe ser lo que de verdad se gastó, no el de la última llamada."""
    fundido = _merge_results(
        [
            _result("a", prompt=100, completion=50, cost="0.001", ms=300),
            _result("b", prompt=120, completion=60, cost="0.002", ms=400),
        ]
    )
    assert fundido.usage.prompt_tokens == 220
    assert fundido.usage.completion_tokens == 110
    assert fundido.usage.total_tokens == 330
    assert fundido.cost_usd == Decimal("0.003")
    assert fundido.latency_ms == 700
    assert fundido.model == "m"


def test_el_fundido_conserva_el_finish_reason_del_ultimo() -> None:
    cortada = _result("b", prompt=1, completion=1, cost="0", ms=1)
    object.__setattr__(cortada, "finish_reason", "length")
    fundido = _merge_results([_result("a", prompt=1, completion=1, cost="0", ms=1), cortada])
    assert fundido.finish_reason == "length"


def _registro(fecha: str, concepto: str, valor: str) -> str:
    """Un movimiento como lo trae un extracto en texto: fecha / descripción / valor / saldo."""
    return f"{fecha}\n{concepto}\n{valor}\n$1,000.00\n"


def test_el_corte_no_separa_la_fecha_de_su_movimiento() -> None:
    """Con registros multilínea, el tramo corta en un INICIO de registro: ninguna fecha queda
    huérfana al final de un tramo ni una descripción sin su fecha al principio del siguiente."""
    registros = [
        _registro("01/12/2025", f"COMPRA PAQUETE {i:03d}", "$-8,000.00") for i in range(40)
    ]
    texto = "Fecha del movimiento\n" + "".join(registros)
    chunks = split_text_chunks(texto, max_chars=300)
    assert len(chunks) > 1
    for ch in chunks[:-1]:
        ultima = ch.rstrip("\n").splitlines()[-1]
        assert not ultima.startswith("01/12/2025"), f"fecha huérfana al final del tramo: {ch!r}"
    for ch in chunks[1:]:
        assert ch.startswith("01/12/2025"), f"tramo que arranca sin fecha: {ch[:40]!r}"
    assert "".join(chunks) == texto


def test_sin_registros_con_fecha_corta_como_antes() -> None:
    lineas = [f"linea {i}\n" for i in range(50)]
    texto = "".join(lineas)
    chunks = split_text_chunks(texto, max_chars=100)
    assert all(len(c) <= 100 for c in chunks)
    assert "".join(chunks) == texto
