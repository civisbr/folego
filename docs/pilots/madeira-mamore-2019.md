# Piloto real SIH — MADEIRA-MAMORE, competências de 2019

Execução: 2026-11-05T12:11:22.829346+00:00.

**São internações respiratórias observadas; não são internações atribuídas à fumaça nem previsões.**

Coorte por residência: Guajará-Mirim, Porto Velho, Nova Mamoré, Candeias do Jamari, Itapuã do Oeste. Código regional: `11004`.

Foram processados **324/324 arquivos RD**, das **27 UFs**,
abrangendo os 12 meses de processamento. O leitor percorreu 12.356.283 registros
e reteve apenas os municípios da coorte.

Internações respiratórias normais no recorte de processamento: **2677**.
Realizadas fora da UF de residência: **45**.
Os arquivos da UF de residência, isoladamente, não cobrem esses atendimentos.

## Conferência independente no TabNet

Estado: **aprovada**. Critério: diferença relativa inferior a 1% no total anual e em cada mês.

A consulta usa local de residência, os 5 municípios da coorte, capítulo CID-10 X,
conteúdo Internações, todas as idades e os 12 meses de processamento. As opções são
descobertas no formulário oficial; o escopo e o período retornados são conferidos no
registro de consulta incluído na resposta do TabNet.

| Competência | Fôlego | TabNet | Diferença |
| --- | ---: | ---: | ---: |
| 2019-01 | 184 | 184 | 0.00% |
| 2019-02 | 193 | 193 | 0.00% |
| 2019-03 | 238 | 238 | 0.00% |
| 2019-04 | 235 | 235 | 0.00% |
| 2019-05 | 261 | 261 | 0.00% |
| 2019-06 | 243 | 243 | 0.00% |
| 2019-07 | 239 | 239 | 0.00% |
| 2019-08 | 269 | 269 | 0.00% |
| 2019-09 | 237 | 237 | 0.00% |
| 2019-10 | 213 | 213 | 0.00% |
| 2019-11 | 195 | 195 | 0.00% |
| 2019-12 | 170 | 170 | 0.00% |

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
uv run --extra sih folego pilot --config configs/pilots/madeira-mamore-2019.json --require-tabnet --publish-report docs/pilots/madeira-mamore-2019.md
```

O download nacional deste ano ocupa aproximadamente 882.5 MiB comprimidos.
Downloads e projeções são cacheados; a reserva de disco é verificada antes da descompressão.

[Manifesto e resultados em JSON](madeira-mamore-2019.json). O manifesto inclui URL, tamanho e SHA-256
dos 324 arquivos oficiais, além da consulta TabNet e do hash de sua resposta.

Fontes: [DATASUS — transferência de arquivos](https://datasus.saude.gov.br/transferencia-de-arquivos/)
e [TabNet por residência — Rondônia](https://tabnet.datasus.gov.br/cgi/deftohtm.exe?sih/cnv/nrro.def).

## Próximas etapas

Ampliar a janela de competências e estudar o atraso de processamento; integrar CAMS
e população; completar meteorologia, confundidores e IPCA; ajustar e avaliar o modelo
com o passado. O ambiente desta execução ainda não tinha acesso ADS/CAMS configurado.
O painel público permanece em demonstração até cumprir os critérios científicos.
