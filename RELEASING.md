# Releasing

Este documento descreve o processo de lançamento de uma nova versão deste
pacote. O mesmo padrão se aplica aos outros repositórios do ecossistema
Synesis (`synesis`, `synesis-lsp`, `synesis-vscode`, `synesis-coder`) — ao
adotar esta automação em outro repositório, replique o job `publish` do
`.github/workflows/ci.yml` deste repositório e ajuste apenas nomes de
pacote/ambiente onde necessário.

## O que é automático

Ao empurrar uma tag `vX.Y.Z`, o workflow `publish` (`.github/workflows/ci.yml`):

1. Aguarda `test`, `lint` e `build` passarem.
2. Builda o pacote e publica no PyPI via Trusted Publishing (OIDC) — sem
   token armazenado.
3. Extrai a seção `## [X.Y.Z]` do `CHANGELOG.md` e cria uma **GitHub Release**
   com esse texto como corpo, usando a própria tag como nome de versão.

Nada disso roda em `push` para `main`/`develop` — só em tags que comecem com
`v` (`if: startsWith(github.ref, 'refs/tags/v')`).

## O que você precisa fazer manualmente, antes de criar a tag

Nesta ordem:

1. **Atualize a versão** em `pyproject.toml` (`[project].version`).
2. **Atualize `CITATION.cff`** — campo `version` e `date-released`.
3. **Adicione a entrada no `CHANGELOG.md` (e `CHANGELOG.pt.md`)** com o
   cabeçalho exato `## [X.Y.Z] - AAAA-MM-DD`. Este cabeçalho é a chave que o
   workflow usa para extrair o corpo da Release — se ele não existir ou tiver
   formatação diferente, o step `Extract changelog section` falha
   deliberadamente (`::error::` + `exit 1`) em vez de criar uma Release vazia
   ou com o texto errado.
4. **Rode a suíte local antes de comitar**, incluindo
   `tests/test_packaging.py` — ele builda o sdist de verdade e valida licença,
   conteúdo do pacote e consistência de versão entre `pyproject.toml`,
   `CITATION.cff` e `CHANGELOG.md`. Uma publicação no PyPI é irreversível: o
   número da versão não pode ser reaproveitado se o artefato sair errado.
5. **Commit e push** dessas mudanças em um PR normal, mergeado em `main` antes
   da tag.

## Criando a tag (dispara a publicação)

```bash
git checkout main
git pull
git tag vX.Y.Z
git push origin vX.Y.Z
```

Isso dispara o workflow. Acompanhe em Actions → CI. Se algo falhar **antes**
do step `Publish to PyPI`, a tag pode ser apagada e recriada sem problema:

```bash
git tag -d vX.Y.Z
git push origin :refs/tags/vX.Y.Z
# corrija o problema, empurre a correção para main, depois recrie a tag
```

**Depois** que `Publish to PyPI` roda com sucesso, a versão está no PyPI para
sempre — não delete/recrie a tag para "tentar de novo"; trate como uma nova
versão (`X.Y.Z+1`).

## Se a criação da Release falhar mas o PyPI já publicou

Isso pode acontecer — são steps sequenciais no mesmo job, e um problema no
`CHANGELOG.md` (cabeçalho ausente, por exemplo) só é detectado depois que o
pacote já foi enviado ao PyPI. Nesse caso o pacote está publicado e correto;
falta só a Release. Crie manualmente pela interface do GitHub
(Releases → Draft a new release), usando a tag já existente e colando a
seção correspondente do `CHANGELOG.md` como corpo. Depois corrija o
`CHANGELOG.md` para a próxima versão não repetir o problema.

## Checklist rápido

- [ ] `pyproject.toml` com a versão nova
- [ ] `CITATION.cff` com `version` e `date-released` sincronizados
- [ ] `CHANGELOG.md` e `CHANGELOG.pt.md` com `## [X.Y.Z] - AAAA-MM-DD`
- [ ] `pytest` local passando, incluindo `test_packaging.py`
- [ ] PR mergeado em `main`
- [ ] `git tag vX.Y.Z && git push origin vX.Y.Z`
- [ ] Acompanhar o run em Actions até `publish` completar
- [ ] Conferir a Release em github.com/&lt;org&gt;/&lt;repo&gt;/releases
