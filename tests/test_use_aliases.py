"""Etapa U (ROLE USE) no grafo: o preterido não vira nó; menções e cadeias vão ao alvo."""

from pathlib import Path
from unittest.mock import MagicMock

from synesis_graph.core import compile_project

TEMPLATE = """\
ITEM FIELDS
    REQUIRED trecho
    OPTIONAL codigo, cadeia
END ITEM FIELDS

FIELD trecho TYPE QUOTATION
    SCOPE ITEM
END FIELD

FIELD codigo TYPE CODE
    SCOPE ITEM
END FIELD

FIELD cadeia TYPE CHAIN
    SCOPE ITEM
    ARITY >= 2
    RELATIONS
        USA: usa
    END RELATIONS
END FIELD

ONTOLOGY FIELDS
    REQUIRED definicao
    OPTIONAL usar
END ONTOLOGY FIELDS

FIELD definicao TYPE TEXT
    SCOPE ONTOLOGY
    ROLE NOTE DEFINITION
END FIELD

FIELD usar TYPE CODE
    SCOPE ONTOLOGY
    ROLE USE
END FIELD
"""

ANNOTATIONS = """\
SOURCE @a1
END SOURCE

ITEM @a1
    trecho: x
    codigo: pig-butchering, pig_butchering
    cadeia: pig-butchering -> USA -> cripto
END ITEM

ITEM @a1
    trecho: y
    cadeia: pig-butchering -> USA -> pig_butchering
END ITEM
"""

ONTOLOGY = """\
ONTOLOGY pig_butchering
    definicao: Golpe de investimento.
END ONTOLOGY

ONTOLOGY pig-butchering
    usar: pig_butchering
END ONTOLOGY

ONTOLOGY cripto
    definicao: Criptoativo.
END ONTOLOGY
"""


def _project(root: Path) -> Path:
    files = {"t.synt": TEMPLATE, "a.syn": ANNOTATIONS, "o.syno": ONTOLOGY,
             "r.bib": "@article{a1, title={T}, author={A}, year={2024}}\n"}
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    synp = root / "p.synp"
    synp.write_text('PROJECT p\nTEMPLATE "t.synt"\nINCLUDE BIBLIOGRAPHY "r.bib"\n'
                    'INCLUDE ANNOTATIONS "a.syn"\nINCLUDE ONTOLOGY "o.syno"\nEND PROJECT\n',
                    encoding="utf-8")
    return synp


def test_alias_is_not_a_node_and_mentions_go_to_the_target(tmp_path):
    payload = compile_project(_project(tmp_path), MagicMock())
    names = {c["props"]["name"] for c in payload.concepts}
    assert "pig-butchering" not in names
    target = next(c for c in payload.concepts if c["props"]["name"] == "pig_butchering")
    assert target["props"]["aliases"] == ["pig-butchering"]
    mentioned = [m["concept"] for m in payload.mentions]
    assert "pig-butchering" not in mentioned
    # o primeiro ITEM cita os dois nomes: uma menção só ao alvo
    first_item = [m for m in payload.mentions if m["concept"] == "pig_butchering"]
    assert len({tuple(sorted(m.items())) for m in first_item}) == len(first_item)
    # a cadeia que colapsa (pig-butchering -> USA -> pig_butchering) sai; a outra vai ao alvo
    pairs = {(c["source"], c["target"]) for c in payload.chains}
    assert ("pig_butchering", "cripto") in pairs
    assert all(s != t for s, t in pairs)


def test_compilation_error_lists_lines_not_characters(tmp_path):
    synp = _project(tmp_path)
    (tmp_path / "t.synt").write_text(TEMPLATE.replace("    ARITY >= 2\n", ""), encoding="utf-8")
    error = compile_project(synp, MagicMock())
    assert error.diagnostics and all(len(line) > 1 for line in error.diagnostics)
