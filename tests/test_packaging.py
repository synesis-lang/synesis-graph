"""Contrato de empacotamento — o que sai no sdist antes de ir para o PyPI.

Por que existe:
    Uma publicacao no PyPI e IRREVERSIVEL: o nome fica reservado para sempre e
    uma versao ja enviada nunca pode ser sobrescrita. Se o artefato sair com
    metadata de licenca errada ou faltando arquivo, a correcao custa queimar o
    numero da versao. Estes testes constroem o sdist de verdade e inspecionam o
    PKG-INFO gerado, em vez de confiar no que o pyproject.toml declara.

O que `twine check` NAO pega (e por isso este arquivo existe):
    A sintaxe legada `license = {text = "..."}` compila sem erro mas emite o
    campo obsoleto `License:` em vez de `License-Expression:`. O twine passa,
    o PyPI recebe, e a licenca aparece vazia no indice. Foi o que aconteceu com
    as versoes ja publicadas de `synesis` e `synesis-lsp` (license: None no
    PyPI). O teste abaixo falha nesse cenario.

Custo: o build leva alguns segundos, entao a fixture e `session`-scoped e o
modulo inteiro pula se `build` nao estiver instalado.
"""

from __future__ import annotations

import subprocess
import sys
import tarfile
from email.parser import Parser
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


def _pyproject() -> dict:
    """The parsed pyproject, with the 3.10 fallback in one place.

    `tomllib` only entered the stdlib in 3.11, but the package declares
    `requires-python = ">=3.10"` and CI runs the matrix on all three. Without
    this fallback the tests break only on the floor — that took down 3 of 9 jobs
    in run 31452586097, and again in 33134063688 when a new test imported
    `tomllib` directly instead of coming through here.
    """
    try:
        import tomllib
    except ModuleNotFoundError:  # Python 3.10
        import tomli as tomllib

    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def _declared_version() -> str:
    """Versao do pyproject.toml — a fonte unica de verdade."""
    return _pyproject()["project"]["version"]

pytestmark = pytest.mark.skipif(
    subprocess.run(
        [sys.executable, "-c", "import build"], capture_output=True
    ).returncode
    != 0,
    reason="pacote `build` nao instalado (extra [dev]/[release])",
)


@pytest.fixture(scope="session")
def sdist_metadata(tmp_path_factory) -> tuple[dict[str, list[str]], list[str]]:
    """Constroi o sdist e devolve (campos do PKG-INFO, lista de arquivos).

    Usa `python -m build` COM isolamento: o ambiente local pode ter
    setuptools < 77, que ignora a forma string do PEP 639 e produziria um
    artefato diferente do que o CI gera.
    """
    outdir = tmp_path_factory.mktemp("dist")
    proc = subprocess.run(
        [sys.executable, "-m", "build", "--sdist", "--outdir", str(outdir)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"build falhou:\n{proc.stdout}\n{proc.stderr}"

    tarballs = list(outdir.glob("*.tar.gz"))
    assert len(tarballs) == 1, f"esperado 1 sdist, encontrado {tarballs}"

    with tarfile.open(tarballs[0]) as tar:
        names = tar.getnames()
        pkg_info_name = next(n for n in names if n.endswith("PKG-INFO"))
        raw = tar.extractfile(pkg_info_name).read().decode("utf-8")

    # PKG-INFO e um documento RFC 822; campos podem repetir (License-File).
    message = Parser().parsestr(raw)
    fields: dict[str, list[str]] = {}
    # message.items() preserva repeticoes (License-File aparece N vezes); um
    # dict(message) colapsaria para a ultima ocorrencia e o teste de
    # empacotamento das licencas passaria por acidente.
    for key, value in message.items():
        fields.setdefault(key, []).append(value)
    return fields, names


class TestLicenseMetadata:
    def test_uses_pep639_license_expression(self, sdist_metadata):
        fields, _ = sdist_metadata
        assert fields.get("License-Expression") == [
            "AGPL-3.0-only AND LicenseRef-Synesis-data-output-exception"
        ]

    def test_no_obsolete_license_field(self, sdist_metadata):
        """`License:` legado convive com o novo campo e faz o PyPI mostrar vazio."""
        fields, _ = sdist_metadata
        assert "License" not in fields, (
            "campo obsoleto `License:` presente — sintaxe legada no pyproject; "
            "`twine check` NAO detecta isso"
        )

    def test_both_license_files_declared(self, sdist_metadata):
        fields, _ = sdist_metadata
        declared = {Path(v).name for v in fields.get("License-File", [])}
        assert declared == {"LICENSE", "LICENSE.exception"}

    def test_both_license_files_packaged(self, sdist_metadata):
        """A excecao so vale se o arquivo dela viajar junto com o pacote."""
        _, names = sdist_metadata
        packaged = {Path(n).name for n in names if Path(n).name.startswith("LICENSE")}
        assert {"LICENSE", "LICENSE.exception"} <= packaged


class TestSdistContents:
    def test_html_template_is_packaged(self, sdist_metadata):
        """Sem o template, o backend HTML nao renderiza e 84 testes pulam."""
        _, names = sdist_metadata
        assert any(n.endswith("templates/graph.html.tmpl") for n in names)

    def test_legacy_shim_is_packaged(self, sdist_metadata):
        """`synesis2graph.py` e declarado em py-modules; se sumir, quebra imports antigos."""
        _, names = sdist_metadata
        assert any(Path(n).name == "synesis2graph.py" for n in names)

    def test_no_secrets_or_local_artifacts(self, sdist_metadata):
        """config.toml carrega senha real; .db/.html sao artefatos locais."""
        _, names = sdist_metadata
        offenders = [
            n
            for n in names
            if Path(n).name == "config.toml"
            or n.endswith((".db", ".html", ".env", ".vsix"))
        ]
        assert offenders == [], f"artefatos indevidos no sdist: {offenders}"


class TestVersionConsistency:
    def test_sdist_version_matches_pyproject(self, sdist_metadata):
        fields, _ = sdist_metadata
        assert fields.get("Version") == [_declared_version()]

    def test_citation_version_matches_pyproject(self):
        """CITATION.cff defasado ja aconteceu duas vezes no ecossistema."""
        import yaml

        citation = yaml.safe_load((REPO_ROOT / "CITATION.cff").read_text(encoding="utf-8"))
        assert citation["version"] == _declared_version()

    def test_changelog_documents_current_version(self):
        """Publicar uma versao ausente do CHANGELOG deixa o historico mentindo."""
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        assert f"## [{_declared_version()}]" in changelog


class TestOptionalExtras:
    """Os extras sao citados em oito mensagens de erro do codigo.

    Cada uma manda o pesquisador rodar `pip install "synesis-graph[<extra>]"`.
    Um nome que nao exista no pyproject transforma a mensagem acionavel numa
    pista falsa, e o erro so aparece na maquina de quem ja estava travado.
    """

    def _extras(self) -> dict:
        return _pyproject()["project"]["optional-dependencies"]

    def test_every_extra_named_in_an_error_message_exists(self):
        """Fonte da verdade: o proprio codigo, nao uma lista mantida a mao."""
        import re

        declared = set(self._extras())
        referenced = set()
        for path in (REPO_ROOT / "synesis_graph").rglob("*.py"):
            for match in re.finditer(
                r"synesis-graph\[([a-z0-9_-]+)\]", path.read_text(encoding="utf-8")
            ):
                referenced.add(match.group(1))

        assert referenced, "nenhuma mensagem encontrada — o regex quebrou?"
        assert referenced <= declared, f"extras citados e inexistentes: {referenced - declared}"

    def test_the_local_engine_is_installed_by_default(self):
        """Decisao de 0.10.0: sem extra a instalar para o caminho local.

        O publico e o pesquisador qualitativo. "Instale o extra certo" e uma
        etapa a mais para errar antes de ver qualquer resultado, e o preco
        (~67 MB) e menor do que essa friccao. O extra permanece declarado para
        nao quebrar quem seguiu instrucoes antigas.
        """
        base = " ".join(_pyproject()["project"]["dependencies"])
        assert "arcadedb-embedded" in base, "o motor local deve vir na instalacao base"
        assert "arcadedb-embedded" in self._extras(), "o extra antigo deve seguir valido"
