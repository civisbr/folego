# Fôlego — previsão de internações por fumaça de queimadas no SUS

> Quanto a fumaça das queimadas vai custar ao SUS na semana que vem, em leitos e em reais, em cada região de saúde do Brasil?

## 1. Contexto e propósito

Na temporada de queimadas (aprox. julho–outubro), a fumaça aumenta internações respiratórias, principalmente de crianças e idosos, inclusive a centenas ou milhares de km do fogo. Hospitais e secretarias descobrem o pico quando o pronto-socorro já lotou.

O Fôlego é um sistema **aberto, nacional e contínuo** que:
1. estima, a partir de dados históricos, o efeito da fumaça (PM2.5) sobre internações respiratórias por região de saúde;
2. usa a previsão de fumaça dos próximos 1–5 dias para estimar o **excesso esperado de internações e o custo associado (R$)**;
3. publica isso num painel público e numa API/arquivos abertos.

**O que NÃO é:** não é diagnóstico individual, não é ferramenta clínica, não substitui a vigilância epidemiológica oficial.

## 2. Base científica (ler antes de modelar)

- **Rocha & Sant'Anna (2022)**, *World Development* 151. Winds of fire and smoke: air pollution and health in the Brazilian Amazon. DOI 10.1016/j.worlddev.2021.105722. Efeitos fixos município×mês + variável instrumental pela direção do vento. +1 DP de PM2.5 → +1,5% na taxa mensal de internação respiratória; +14% quando a média mensal passa de 75 µg/m³. Efeito maior em crianças e idosos e não linear. Sem efeito em mortalidade.
- **Chemosphere 367 (2024)**, 143688. Predicting health impacts of wildfire smoke in Amazonas basin, Brazil. DOI 10.1016/j.chemosphere.2024.143688. Redes neurais prevendo internações cardiorrespiratórias com lags de 0–7 dias. A previsão em si **não é inédita**: nosso diferencial é escala nacional, operação contínua, custo em R$ e abertura.
- **Fiocruz/WWF-Brasil (2021)**: ~R$ 1 bilhão em 10 anos com internações respiratórias atribuíveis à fumaça na Amazônia.
- **Gasparrini (2011)**, *J. Stat. Softw.* 43(8). Pacote `dlnm`: referência metodológica para modelos de defasagem distribuída não linear.

## 3. Fontes de dados

| Dado | Fonte | Acesso | Observações |
|---|---|---|---|
| Internações (AIH reduzida) | DATASUS SIH/SUS, arquivos `RDUFAAMM.dbc` | FTP DATASUS; lib `pysus` | Campos-chave: `MUNIC_RES`, `DT_INTER`, `DIAG_PRINC` (CID-10), `IDADE`, `COD_IDADE`, `VAL_TOT`. Atraso de publicação ~2 meses. |
| Focos de fogo | NASA FIRMS (VIIRS 375 m, MODIS) | API com MAP_KEY gratuita; arquivo histórico | Também o BDQueimadas/INPE. |
| PM2.5 histórico | CAMS Global Reanalysis (EAC4), via Copernicus ADS | API `cdsapi` (conta gratuita) | ~0,75°. Avaliar também o MERRA-2 (NASA). |
| PM2.5 por município (atalho) | INPE SISAM | Web | **Verificar** disponibilidade e formato de exportação antes de depender dele. |
| PM2.5 previsão 1–5 dias | CAMS Global Atmospheric Composition Forecasts (ADS) | API `cdsapi` | Rodada diária. É a entrada operacional. |
| Meteorologia (confundidores) | ERA5 (CDS) e/ou estações INMET | API `cdsapi`; portal INMET | Temperatura média/mín./máx., umidade relativa. |
| Vírus respiratórios (confundidor) | InfoGripe/Fiocruz | Repositório público | SRAG por semana epidemiológica. **Verificar** granularidade. |
| Malha territorial | IBGE (municípios), Ministério da Saúde (regiões de saúde) | Downloads oficiais | Regiões de saúde = unidade de análise principal. |
| População | IBGE (Censo 2022 + estimativas anuais) | SIDRA / downloads | Denominador para taxas, por faixa etária. |

**Pegadinha conhecida:** o DATASUS usa código de município com **6 dígitos**, o IBGE com **7** (dígito verificador). Normalizar tudo para 6 dígitos numa tabela de correspondência logo na ingestão.

## 4. Definições do modelo

- **Desfecho principal:** contagem diária de internações com `DIAG_PRINC` em J00–J99 (capítulo X), por região de saúde × faixa etária (0–4, 5–14, 15–59, 60+).
- **Desfechos secundários:** J45–J46 (asma), J12–J18 (pneumonia); circulatórias I00–I99 como análise de sensibilidade.
- **Controle negativo:** um grupo de CIDs sem relação plausível com fumaça (ex.: fraturas, S00–T98). Se o modelo achar "efeito" ali, há viés.
- **Exposição:** PM2.5 médio diário ponderado pela população da região de saúde (fazer a média ponderada dos pixels por população, não a média simples da área).
- **Modelo base:** regressão quasi-Poisson ou binomial negativa com:
  - defasagem distribuída de PM2.5, lags 0–7 dias (cross-basis no estilo DLNM);
  - spline de temperatura e de umidade;
  - spline de tempo (sazonalidade e tendência), dia da semana, feriados;
  - SRAG/InfoGripe quando disponível.
- **Excesso atribuível:** internações observadas (ou previstas) menos o contrafactual com PM2.5 no nível de referência (ex.: percentil 25 sazonal ou o guideline OMS de 15 µg/m³ diário).
- **Custo:** excesso × custo médio por AIH (`VAL_TOT`) da mesma região, faixa etária e grupo de CID, em valores corrigidos pelo IPCA. Deixar claro que `VAL_TOT` é o valor pago pelo SUS, não o custo real hospitalar.

## 5. Modo operacional (previsão)

O SIH chega com ~2 meses de atraso, então a previsão **não depende do SIH em tempo real**:
1. o modelo é treinado com o histórico (ex.: 2012–ano anterior);
2. diariamente, baixar a previsão CAMS de PM2.5 (1–5 dias) e a previsão meteorológica;
3. aplicar os coeficientes → excesso esperado de internações e custo por região de saúde;
4. classificar em níveis de alerta (ex.: normal / atenção / alerta / emergência) por limiares de excesso relativo;
5. quando chegar o SIH, comparar previsão × realizado e publicar o desempenho.

## 6. Validação (critérios de aceite científicos)

- Backtest com origem móvel: temporadas de 2019 a 2024, treinando só com o passado.
- Métricas: MAE e MAPE semanais por região; para alertas, precisão/recall dos picos (semanas no percentil 90 de excesso).
- O efeito estimado deve ser da mesma ordem que Rocha & Sant'Anna. Divergência grande exige investigação.
- O controle negativo não deve mostrar efeito significativo.
- Sensibilidade: CAMS vs MERRA-2 como fonte de PM2.5.

## 7. Arquitetura e stack

- **Linguagem:** Python 3.12, gerenciado com `uv`. O ecossistema geo/epi está em Python.
- **Armazenamento:** Parquet particionado + DuckDB (extensão `spatial`). Camadas `raw/` → `staging/` → `marts/`.
- **Processamento:** Polars e DuckDB SQL; `xarray` + `rioxarray` para NetCDF/GRIB; `geopandas`/`shapely` só onde necessário.
- **Modelagem:** `statsmodels` (GLM), cross-basis implementada em `folego/models/crossbasis.py`. Opcional: validar coeficientes contra o `dlnm` em R num notebook separado.
- **Orquestração:** começar com `make` + scripts idempotentes; GitHub Actions com cron diário para o modo operacional.
- **Front:** site estático (Cloudflare Pages ou GitHub Pages) com MapLibre GL + PMTiles para o mapa; dados servidos como Parquet/JSON pequenos. Sem backend no MVP.

### Estrutura do repositório

```
folego/
  PROJECT.md
  pyproject.toml
  Makefile
  folego/
    ingest/        # sih.py, firms.py, cams.py, era5.py, ibge.py, infogripe.py
    transform/     # exposure.py (PM2.5 ponderado por população), outcomes.py, codes.py
    models/        # crossbasis.py, glm.py, attributable.py, forecast.py
    evaluate/      # backtest.py, metrics.py, negative_control.py
    publish/       # export_web.py (gera JSON/Parquet/PMTiles para o site)
  data/            # gitignored: raw/, staging/, marts/
  notebooks/       # exploração; nada de lógica de produção aqui
  web/             # site estático
  tests/
```

## 8. Fases e entregáveis

**Fase 0 — Fundação**
- Repo, `uv`, lint (`ruff`), testes (`pytest`), Makefile.
- Tabela de correspondência município 6↔7 dígitos e município → região de saúde.
- Aceite: `make test` verde; tabela cobre 100% dos municípios.

**Fase 1 — Ingestão SIH**
- Baixar o RD de 2012 em diante para todas as UFs, converter para Parquet e filtrar o capítulo J e os controles.
- Agregar em contagem diária por município e por região de saúde × faixa etária.
- Aceite: totais anuais batem com o TabNet (diferença < 1%).

**Fase 2 — Exposição**
- PM2.5 diário do CAMS EAC4, ponderado por população, por região de saúde.
- Focos FIRMS agregados (contagem e FRP) como variável descritiva.
- Aceite: série de PM2.5 de 2019 mostra os picos conhecidos (Amazônia em ago–set).

**Fase 3 — Modelo explicativo**
- GLM com cross-basis + confundidores; curva exposição-resposta e efeito por lag.
- Aceite: critérios da seção 6, exceto o backtest.

**Fase 4 — Previsão e backtest**
- Pipeline de previsão com o CAMS forecast e backtest 2019–2024.
- Aceite: relatório de desempenho publicado no repo.

**Fase 5 — Painel público**
- Mapa por região de saúde com nível de alerta, excesso previsto e custo; página "como funciona" com limitações.
- Aceite: site no ar, atualizado por cron diário.

**Fase 6 (opcional)**
- Recorte por hospital/estabelecimento (`CNES`) para planejamento de leitos.
- Relatório sazonal "custo da fumaça" por estado.

## 9. Limitações a declarar publicamente

- Associação estimada com dados ecológicos; inferência causal limitada ao desenho do modelo.
- Sub-registro e codificação imperfeita de CID no SIH; o SIH cobre só o SUS (não a saúde suplementar).
- O PM2.5 por satélite e reanálise tem erro, especialmente em áreas urbanas sem calibração em solo.
- Baixa umidade e vírus sazonais coincidem com a seca; o controle é imperfeito.

## 10. Ética e dados

- Os microdados do SIH são públicos e anonimizados, mas **publicar só agregados**, suprimindo células com contagem < 5.
- Licença do código: MIT ou Apache-2.0. Dados derivados: CC-BY 4.0, citando as fontes originais.
- Atribuir corretamente NASA FIRMS, Copernicus/CAMS, DATASUS e IBGE em todo produto público.

## 11. Convenções para o Claude Code

- Código e identificadores em inglês; documentação e textos do site em português.
- Toda função de ingestão é idempotente e cacheia o download em `data/raw/`.
- Nada de lógica de negócio em notebooks; notebooks só importam do pacote.
- Testes para: normalização de códigos, filtros de CID, agregação temporal, cross-basis (comparar com caso sintético de efeito conhecido).
- Antes de implementar um modelo, escrever um teste com dados sintéticos onde o efeito verdadeiro é conhecido, e verificar se o modelo o recupera.
