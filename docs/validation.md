# Estado de implementação e validação

Primeira versão, novembro de 2026. **Não há previsão operacional validada.** Há estimativas de pesquisa com dados reais (efeito do PM2,5 e backtest com exposição observada); não demonstram eficácia operacional nem, sozinhas, inferência causal.

| Entrega / aceite do projeto | Estado e evidência |
| --- | --- |
| Python 3.12, uv, Ruff, pytest, Makefile | Implementados; `make check` executa validações locais. |
| Correspondência de 100% dos municípios | Snapshot com 5.571/5.571, 439 regiões; teste nacional e manifesto versionados. Um complemento com resolução oficial da SES/MT. |
| SIH RD: download e conversão DBC → Parquet | Série nacional extraída (`folego sih-national`): 4.776 arquivos, das competências 2012-01 a 2026-07 nas 27 UFs, com os capítulos I, J, S e T de todas as residências; DBC apagados e hash/URL preservados. |
| Desfechos CID/idade/data/residência | Série nacional diária por região de saúde (`folego outcomes`): 50,9 milhões de internações de 2012-01-01 a 2026-01-31, nenhuma AIH em conflito, nenhuma residência sem região. Conferência TabNet com erro zero em 12 de 12 meses para AC 2012, AM 2013, SP 2016, RO 2019, BA 2021 e RS 2024. Maturação medida (`folego maturity`): 98,6% dos registros chegam em 3 meses e 99,99% em 6, o que justifica o corte de 6 meses. |
| PM2,5 ponderado pela população | Série diária CAMS EAC4 2012–2025 das 439 regiões (`data/marts/exposure/pm25-daily.parquet`), ponderada pelo WorldPop 2020. Aceite da fase 2: em 2019, médias mensais de agosto e setembro de 70 e 63 µg/m³ em Rondônia, 50 e 56 no Acre e 36 e 60 em Mato Grosso, contra 7–16 µg/m³ de abril a julho. |
| FIRMS, CAMS, ERA5, SRAG | CAMS, ERA5 e MERRA-2 (PM2,5 e meteorologia) baixados e agregados para 2012–2025. Feriados por região (`folego calendar`), SRAG hospitalizada (`folego srag`, SIVEP-Gripe) e focos FIRMS VIIRS (`folego fires`). O painel `smoke-cams-era5` (115 regiões, 2012-01-08 a 2025-12-30) junta todas as fontes. |
| Modelo com lags e confundidores | Estudo regional (`folego study`, versão 7 do modelo) rodado nas 115 regiões com meteorologia ERA5 e, como sensibilidades, meteorologia MERRA-2 e PM2,5 MERRA-2; meta-análise de efeitos aleatórios (`folego pool`). RR respiratório combinado 1,015 (IC 1,007–1,023) por +10 µg/m³, mesma ordem de Rocha & Sant'Anna (2022): ver [metodologia](methodology.md#comparação-com-a-literatura) e `docs/evidence/`. |
| Excesso, pagamentos e incerteza | Implementados com simulação conjunta e preservação do histórico observado. Pagamentos do SIH corrigidos pelo IPCA (SIDRA) do mês de processamento. |
| Controle negativo | Lesões (S/T) no nulo na meta-análise: RR 0,998 (IC 0,993–1,003) em todas as idades e 0,999 em 60+. Na versão 6, sem clima defasado, o 60+ saía 0,985; a correção está documentada na metodologia. |
| Backtest 2019–2024 | Rodado nas 115 regiões (origens de 1º de julho, previsão jul–out, treino até origem − 62 dias), contra o mesmo modelo sem PM2,5. O PM2,5 não melhora a previsão semanal: razão mediana de MAE 1,001–1,009, melhor em 40–47% das dobras; MAPE respiratório mediano 33%. Usa exposição observada, não previsões arquivadas, portanto é otimista. Ver [metodologia](methodology.md#validação-necessária) e `docs/evidence/backtest-smoke-cams-era5-knots15_35.json`. |
| CAMS × MERRA-2 | Estudo das 115 regiões repetido com o PM2,5 do MERRA-2: respiratório 1,010 (IC 1,001–1,018) contra 1,015 com o CAMS, 0–4 anos 1,023 contra 1,021, controle negativo no nulo. Asma muda de sinal entre as fontes e não é robusta. Ver [metodologia](methodology.md#comparação-com-a-literatura). |
| Painel e arquivos abertos | Implementados, com mapa real e valores sintéticos explícitos; publicação via GitHub Pages. A página Estudo mostra os RR combinados reais (`web/public/data/study.json`). O painel de previsão continua em `mode=demonstration`. |
| Atualização diária | Auditoria territorial agendada. Previsão de pesquisa depende de painéis preparados configurados; não é uma operação científica nacional ativa. |
| PMTiles | Protocolo registrado no cliente; snapshot atual usa GeoJSON local (~450 kB). Geração e hospedagem PMTiles não implementadas nem necessárias para este volume. |
| Privacidade | Exportador restringe esquema, proíbe microdados, suprime medidas de células < 5 e impede desfechos sobrepostos/idades agregadas. Revisão de risco antes de novas agregações. |

## Evidência sintética

- Exposição aleatória com contagens Poisson e efeito conhecido de 0,08 na escala log por 10 µg/m³ no lag zero. O teste exige correlação > 0,99 e erro relativo médio < 3% na média esperada.
- Previsão completa com mais de três anos de dados artificiais, spline de confundidores, lags e pagamento médio. Alterar desfechos posteriores ao corte de disponibilidade não modifica a previsão.
- Lacunas de datas interrompem o fluxo; alterar exposição futura não modifica linhas passadas da base cruzada.
- Cenários idênticos produzem excesso e incerteza nulos; não há preenchimento dos lags iniciais.
- Cobertura de todos os códigos territoriais oficiais do snapshot, inclusive dígitos verificadores excepcionais e o município novo.

## Antes de ativar previsões públicas reais

1. Congelar e documentar versões de fontes, regionalizações e denominadores populacionais compatíveis com cada ano. O snapshot atual não reconstrói municípios antigos automaticamente.
2. Processar SIH, conciliar revisões e comparar totais por ano de internação com TabNet. Auditar códigos e idades ausentes.
3. Preparar exposição populacional e meteorologia, verificar picos conhecidos em 2019 e resolver a distinção PM2,5 total versus fumaça.
4. Ajustar por estrato, diagnosticar dispersão/resíduos/autocorrelação e comparar efeitos com a literatura conferida.
5. Fazer backtest com previsões ambientais arquivadas e disponibilidade clínica respeitada; publicar métricas por região e seu denominador.
6. Executar controle negativo, análise CAMS/MERRA-2, calibração de incerteza e alertas, e revisão científica.
7. Só então adicionar um caminho de publicação operacional na interface. O cliente atual exige `mode=demonstration`; a troca precisa ser deliberada e acompanhada da evidência de validação.

## Verificação da primeira entrega

Executados localmente: 22 testes pytest aprovados (com a opção `sih`); 8 testes Playwright aprovados (quatro cenários em desktop e celular), incluindo navegação, filtros, downloads, resposta a falhas, verificação axe de acessibilidade e renderização efetiva dos polígonos do mapa. Ruff, formatação e build de produção aprovados. Auditoria npm sem vulnerabilidades conhecidas na instalação verificada. A inspeção visual e as capturas em `docs/dashboard*.png` registram o resultado. Esses controles não substituem teste com usuários nem validação epidemiológica.

A primeira execução remota aprovou os jobs Python, web e deploy. A auditoria online adicional encontrou timeout de conexão ao IBGE no runner; o tratamento distingue fonte indisponível (`degraded`, sem confirmação de atualização) de erro de validação (`failed`), preservando a versão pública anterior.

## Atualização das dependências solicitada na revisão

PRs Dependabot #1–#9 integrados: DuckDB 2.0.0, PyArrow 26.0.1, statsmodels 0.15.0 e Vite 8.3.5, além das cinco revisões de GitHub Actions. Os lockfiles foram conciliados e reinstalados. O PySUS completo exige DuckDB < 2; a conversão DBC passou a usar diretamente `pyreaddbc` 2.0.4 e `dbfread`, sem forçar downgrade do PySUS ou do DuckDB. O leitor nativo foi exercitado com o arquivo público de teste `sids.dbc` do projeto AlertaDengue/pyreaddbc (100 registros), armazenado apenas em `data/raw/reader-validation/`.

O conjunto atualizado passou por testes de efeito conhecido, previsão completa, conversão/cache SIH e interoperabilidade Arrow → Parquet → DuckDB persistente. Os oito cenários de navegador também passaram com Vite 8. As APIs usadas foram confrontadas com as [notas de statsmodels](https://www.statsmodels.org/stable/release/index.html), a [migração DuckDB 2](https://duckdb.org/docs/2.0/operations_manual/migration), as [notas Arrow 26](https://arrow.apache.org/blog/2026/10/08/26.0.0-release/) e a [migração Vite 8](https://vite.dev/guide/migration).

A conciliação final também atualizou pandas para 3.1.1 e as dependências transitivas liberadas pela retirada do PySUS completo. Os acessores de calendário do pandas foram adaptados aos nomes atuais, removendo avisos de depreciação sem alterar o desenho do modelo.

## Primeiro aceite de ingestão com dados reais

O [relatório Madeira-Mamoré / competências de 2019](pilots/madeira-mamore-2019.md) documenta o processamento de 324 arquivos RD, 12.356.283 registros examinados e a seleção dos residentes dos cinco municípios da coorte. Foram conferidas 2.677 internações respiratórias normais com o TabNet, com diferença de 0% no total e em cada um dos 12 meses. A cobertura das 27 UFs inclui 45 internações fora de Rondônia que seriam perdidas usando apenas arquivos RO.

A validação é de ingestão **por competência de processamento**. Não valida o modelo, o efeito da fumaça, pagamentos atribuíveis, uma série madura por data de internação ou as outras regiões e anos. As condições do relatório devem ser preservadas na interpretação. O acesso ADS/CAMS ainda precisa ser configurado para prosseguir com a exposição ambiental.

A entrega do piloto passou por 39 testes locais, incluindo projeção DBF comparada ao leitor de referência, truncamento, separação entre processamento e internação, reconciliação de identificadores, filtros e HTML legado do TabNet, cobertura das 27 UFs e proteção da publicação. Lint, formatação e build do site também passaram. O comando de configuração ADS tem testes de permissão do arquivo, preservação de configuração existente e ausência de impressão da chave.
