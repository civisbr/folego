# Piloto real SIH — $health_region, competências de $year

Execução: $generated_at.

**São internações respiratórias observadas; não são internações atribuídas à fumaça nem previsões.**

Coorte por residência: $municipalities. Código regional: `$region`.

Foram processados **$files_processed/$files_expected arquivos RD**, das **$states UFs**,
abrangendo os 12 meses de processamento. O leitor percorreu $scanned_records registros
e reteve apenas os municípios da coorte.

Internações respiratórias normais no recorte de processamento: **$respiratory_records**.
Realizadas fora da UF de residência: **$outside_state**.
Os arquivos da UF de residência, isoladamente, não cobrem esses atendimentos.

## Conferência independente no TabNet

Estado: **$tabnet_status**. Critério: diferença relativa inferior a 1% no total anual e em cada mês.

A consulta usa local de residência, os $municipality_count municípios da coorte, capítulo CID-10 X,
conteúdo Internações, todas as idades e os 12 meses de processamento. As opções são
descobertas no formulário oficial; o escopo e o período retornados são conferidos no
registro de consulta incluído na resposta do TabNet.

$monthly_table

## Contratos e limitações desta etapa

- Competência é o mês de processamento/pagamento, e não a data da internação. A conferência
  TabNet usa as linhas de processamento, antes da reconciliação de episódios.
- A série diária privada usa a data de internação, exclui AIH de continuidade e reconcilia
  identificadores repetidos. Repetições clínicas idênticas são colapsadas; identificadores
  com dados divergentes são segregados para revisão, sem escolher uma versão silenciosamente.
- O recorte de um ano de processamento contém internações de anos anteriores e deixa de fora
  internações processadas depois do fim da janela. A coorte por data de internação ainda
  **não está madura**; não foram preenchidos zeros nem liberado uso em treinamento.
- A lista municipal é do snapshot territorial atual, cujo SHA-256 está no manifesto.
  A consistência da regionalização histórica ainda precisa ser conferida.
- Pagamentos locais são valores nominais de VAL_TOT. Não houve correção por IPCA,
  atribuição à fumaça, cálculo de excesso ou estimativa de custo hospitalar real.
- Microdados, identificadores e células diárias por idade ficam somente em `data/`, fora
  do Git e do site. A publicação contém totais amplos e metadados dos arquivos de origem.
  A tabela mensal inteira é omitida caso tenha alguma contagem menor que 5.

## Reproduzir

```sh
uv sync --frozen --extra sih
uv run --extra sih folego pilot --config configs/pilots/$name.json --require-tabnet --publish-report docs/pilots/$name.md
```

O download nacional deste ano ocupa aproximadamente $compressed_mib MiB comprimidos.
Downloads e projeções são cacheados; a reserva de disco é verificada antes da descompressão.

[Manifesto e resultados em JSON]($json_filename). O manifesto inclui URL, tamanho e SHA-256
dos $files_processed arquivos oficiais, além da consulta TabNet e do hash de sua resposta.

Fontes: [DATASUS — transferência de arquivos](https://datasus.saude.gov.br/transferencia-de-arquivos/)
e [TabNet por residência — Rondônia](https://tabnet.datasus.gov.br/cgi/deftohtm.exe?sih/cnv/nrro.def).

## Próximas etapas

Ampliar a janela de competências e estudar o atraso de processamento; integrar CAMS
e população; completar meteorologia, confundidores e IPCA; ajustar e avaliar o modelo
com o passado. O ambiente desta execução ainda não tinha acesso ADS/CAMS configurado.
O painel público permanece em demonstração até cumprir os critérios científicos.
