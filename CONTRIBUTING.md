# Contribuir com o Fôlego

1. Descreva o problema ou hipótese em uma issue, especialmente para mudanças metodológicas.
2. Use Python 3.12 com uv e mantenha `uv.lock` atualizado. No site, use `npm ci` com o lockfile versionado.
3. Escreva código e identificadores em inglês; documentação e interface em português.
4. Antes de alterar um modelo, escreva um teste sintético com efeito conhecido e demonstre sua recuperação. Não transforme teste sintético em evidência de validação externa.
5. Execute `make check` e, para mudanças no site, `npm --prefix web test`.
6. Use commits como `feat: add regional exposure aggregation` e `fix: preserve observed exposure history`. Não reescreva commits publicados.

Downloads devem ser idempotentes, cacheados em `data/raw/` e rastreáveis. Não versione microdados, credenciais, bancos locais ou modelos serializados com pickle. Lógica de produção pertence ao pacote `folego`, não a notebooks.

Mudanças de fonte, malha ou regionalização exigem conferência de cobertura e proveniência. Não associe municípios ausentes por semelhança de nome. Complementos documentados ficam em `folego/resources/territory-overrides.json`, com ato oficial e vigência. Para séries anteriores, use correspondências compatíveis com o ano; o snapshot atual não resolve automaticamente alterações históricas.

Na publicação, preserve a identificação de demonstração até cumprir os critérios em `docs/validation.md`. Não use valores sintéticos em análises epidemiológicas. Publicações observadas devem ter grupos disjuntos, sem subtotais sobrepostos, e suprimir contagens menores que 5 e medidas financeiras relacionadas.
