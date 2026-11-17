"""Conceito: `description` nao duplica o campo de definicao do template.

Com ROLE NOTE DEFINITION, o compilador poe a definicao tambem em `description`.
O grafo gravava as duas propriedades com o mesmo texto (`definicao` e
`description`) — 791 de 791 conceitos no Fraudes Digitais (2026-11-17).
"""

from synesis_graph.core import _extract_concepts


def _props(entry: dict, scalar_fields: list[str]) -> dict:
    concepts = _extract_concepts(
        {"c": entry}, scalar_fields=scalar_fields, graph_fields=[], value_maps={}
    )
    return concepts[0]["props"]


def test_description_equal_to_template_field_is_dropped():
    props = _props({"description": "Texto.", "definicao": "Texto."}, ["definicao"])
    assert props["definicao"] == "Texto."
    assert props["description"] is None


def test_description_kept_when_no_field_repeats_it():
    # Projeto legado: a definicao so existe em `description`.
    props = _props({"description": "Texto."}, ["topico"])
    assert props["description"] == "Texto."


def test_description_kept_when_field_differs():
    props = _props({"description": "Definicao.", "nota": "Outra coisa."}, ["nota"])
    assert props["description"] == "Definicao."
    assert props["nota"] == "Outra coisa."
