# msxbarepi-issues

Espelho público de issues e status do projeto MSXBarePI.

## Sincronização

O script [`scripts/sync_github_state.py`](/Users/sandersouza/Developer/msxbarepi-issues/scripts/sync_github_state.py)
espelha do repo `sandersouza/msxbarepi` para `sandersouza/msxbarepi-issues`:

- labels do repositório;
- milestones;
- issues abertas e fechadas;
- itens do project `3` para o project `5`;
- campos single-select compartilhados do project, incluindo `Status` e também `Priority`/`Size` quando existirem nos dois lados.

O mapeamento é idempotente. Cada milestone/issue sincronizada recebe um marcador oculto no corpo/descrição para que as próximas execuções atualizem o item correto em vez de duplicar.

## Pré-requisitos

- `gh` autenticado com escopos `repo` e `project`;
- acesso aos repositórios `sandersouza/msxbarepi` e `sandersouza/msxbarepi-issues`;
- acesso aos projects `https://github.com/users/sandersouza/projects/3` e `https://github.com/users/sandersouza/projects/5`.

## Nota sobre MCP

No ambiente do Codex, operações manuais de manutenção devem priorizar GitHub MCP e usar `gh` apenas como fallback, especialmente para Project v2 quando a operação não estiver disponível no MCP.

Este script continua usando `gh` porque ele é um utilitário standalone executado no terminal, fora do runtime do MCP do Codex.

## Uso

Dry-run:

```bash
python3 scripts/sync_github_state.py --dry-run
```

Sincronização real:

```bash
python3 scripts/sync_github_state.py
```

Flags úteis:

- `--skip-labels`: não sincroniza labels.
- `--skip-project`: não sincroniza os projects.
- `--source-owner`, `--source-repo`, `--target-owner`, `--target-repo`: sobrescrevem os padrões.
- `--source-project` e `--target-project`: sobrescrevem os números dos projects.

## Observações

- O script sobrescreve título, corpo, labels, assignees, milestone e estado das issues espelhadas.
- Issues extras já existentes no repo de destino e sem marcador de sincronização não são removidas.
- Milestones são abertas durante a atualização e depois retornam ao estado de origem para evitar falhas de vínculo durante o espelhamento das issues.
