# Operação e contratos

## Ambiente e armazenamento

`make setup` instala o ambiente de Python 3.12 com `uv.lock` e o cliente com `web/package-lock.json`. Microdados, NetCDF, banco DuckDB e resultados intermediários ficam em `data/` (ignorado pelo Git). Camadas: `raw/` imutável por requisição → `staging/` normalizada → `marts/` agregada. `folego.warehouse.connect_warehouse(spatial=True)` instala/carrega a extensão oficial DuckDB spatial quando a análise precisar dela.

Downloads comuns são atômicos, cacheados e acompanhados de URL, SHA-256 e instante de recuperação. Requisições CAMS são identificadas pelo hash do JSON; preserve data e hora da rodada em cada requisição para não confundir previsões arquivadas. O cache não é uma política automática de retenção ou de atualização de fontes mutáveis.

## Credenciais e fontes

Para criar a conta ADS e salvar a chave fora do Git, siga [o guia de configuração Copernicus](copernicus-setup.md). O comando `uv run folego configure-ads` recebe a chave com entrada oculta e cria `~/.cdsapirc` com permissão `600`, sem sobrescrever configuração existente.


- SIH: FTP público DATASUS; a conversão DBC usa o leitor `pyreaddbc` mantido pelo projeto PySUS, sem a dependência transitiva de DuckDB < 2, instalado com `uv sync --extra sih`, que pode exigir bibliotecas nativas do leitor pyreaddbc. O download nacional histórico é volumoso; comece por competência/UF e faça inventário dos arquivos.
- FIRMS: MAP_KEY em `~/.config/folego/firms-key` (permissão 600) ou `FIRMS_MAP_KEY` no ambiente. A chave não entra em manifestos, logs nem mensagens de erro. O histórico usa o arquivo VIIRS SNPP de processamento padrão (`folego fires`).
- MERRA-2: login Earthdata em `~/.netrc` (permissão 600), com a aplicação NASA GESDISC DATA ARCHIVE autorizada no perfil. A cada execução o comando pede ao Earthdata o token Bearer da conta (o URS devolve o existente ou cria um, válido por cerca de 60 dias, que pode ser revogado no perfil). O token fica só em memória e não vai para a busca no CMR. Redirecionamentos para fora dos hosts da NASA são bloqueados antes do envio.
- CAMS: configure o cliente `cdsapi` para o ADS, aceite os termos do dataset e exporte a requisição pela interface oficial. Use `uv sync --extra climate`. API e nomes de parâmetros podem mudar; o código recebe o JSON oficial, sem inventar parâmetros ou substituir silenciosamente variáveis.
- ERA5: conta CDS, termos aceitos e requisição oficial. ERA5 é reanálise histórica; previsões operacionais exigem outro produto meteorológico e seu arquivo de rodadas.
- InfoGripe: indique explicitamente a URL do snapshot oficial. A compatibilidade de granularidade deve ser verificada antes de acrescentar o termo SRAG.

Nunca coloque chaves em arquivos versionados, argumentos de processos ou documentação. `.env.example` contém apenas nomes. O projeto não carrega `.env` automaticamente; use seu gerenciador de segredos.

## Territorialização

`folego territory --refresh` consulta IBGE e pagina a API nacional do Ministério da Saúde com páginas de 500 linhas. Nessa API, `offset` é o índice da página (0, 1, 2…), não o deslocamento em registros. As páginas são baixadas como um conjunto em `data/raw/territory/health-pages/` e só substituem o cache anterior quando todas chegam íntegras; páginas sobrepostas, página curta antes da última, código municipal inválido ou mais de 40 páginas interrompem a execução. A tabela `municipalities.parquet` só é regravada após a conferência de cobertura. Falta de correspondência interrompe a execução por padrão; `--allow-incomplete` serve apenas para investigação. O complemento de Boa Esperança do Norte foi conferido na Resolução CIB/MT nº 507/2025; a população de 2022 permanece ausente, pois não se deve inventar um denominador de um município novo.

O snapshot público tem 5.571 municípios e 439 regiões. A malha mínima da API IBGE contém 5.570 polígonos, anterior ao município novo; o manifesto declara isso. Os municípios de origem e o novo território estão na região Teles Pires. Não use a malha simplificada para análise de exposição fina; ela atende apenas ao mapa público.

Para atualizar os arquivos territoriais públicos, regenere a correspondência, confira a cobertura e exporte os formatos JSON/CSV/Parquet e o manifesto. A publicação automática desses snapshots foi deliberadamente evitada: mudanças de limites exigem revisão. `folego geography` dissolve os polígonos oficiais pela tabela em staging. O comando `folego demo` regenera os exemplos determinísticos, sem alterar a data para parecer uma previsão nova.

## Exposição a PM2,5

```sh
uv run --extra climate folego exposure data/raw/cams/cams-global-reanalysis-eac4/<arquivo>.nc
```

O comando gera `data/marts/exposure/pm25-daily.parquet` com `date`, `health_region_code`, `population` e `pm25` (µg/m³). Cada dia local (UTC−3) é a média das oito análises de 3 horas do EAC4; dias incompletos são descartados. A ponderação usa a população do WorldPop 2020 (grade de 1 km, sem restrição, CC BY 4.0): cada pixel populado vai para a região de saúde que contém seu centro e para o ponto mais próximo da grade de 0,75° do CAMS. Pixels populados a mais de meio passo de qualquer ponto da grade são encaixados no ponto mais próximo só até um passo (0,75°) — caso de Fernando de Noronha, a leste do limite −33 do pedido nacional — e contabilizados no manifesto; além disso o comando falha. Também falha se o raster não estiver em EPSG:4326, se alguma região ficar sem pixel populado ou se houver valores ausentes. Ao lado do Parquet fica `pm25-daily.parquet.source.json`, com SHA-256 e proveniência do NetCDF, do raster e da malha, período, encaixes e atribuições. O raster WorldPop tem SHA-256 fixado no código; downloads em cache são conferidos contra o hash registrado antes de cada uso.

Limitações: as fronteiras vêm da malha simplificada das regiões; pedidos futuros devem usar limite leste −32 para cobrir Noronha; o WorldPop 2020 é fixo no tempo e diverge do Censo 2022 em algumas regiões (razão total WorldPop/IBGE entre 0,71 e 1,62, mediana 1,04), o que afeta a distribuição interna dos pesos; o PM2,5 é total, não apenas de fumaça.

Atribuição: WorldPop (www.worldpop.org), School of Geography and Environmental Science, University of Southampton — Population Counts, Unconstrained individual countries 2000-2020 (1 km), Brasil, 2020 ([hub.worldpop.org](https://hub.worldpop.org/Global1_2000-2020)). Confirmar o DOI do recorte nacional antes de citar em publicação.

## Meteorologia, IPCA e feriados

```sh
uv run --extra climate folego meteorology data/raw/era5/*.nc
uv run folego ipca --refresh
uv run folego calendar --start 2012-01-01 --end 2025-12-31
```

`meteorology` lê os arquivos anuais do ERA5 (2 m, a cada 3 h, grade de 0,75°) e gera `data/marts/exposure/meteorology-daily.parquet` com `temperature` (°C) e `humidity` (umidade relativa, %), ponderadas pela mesma população do PM2,5 e com manifesto de proveniência. A umidade vem da fórmula de Magnus (Alduchov & Eskridge, 1996) aplicada a cada passo antes da média diária. O último dia local de cada ano é completado com o primeiro horário do arquivo seguinte. Ponto de orvalho acima da temperatura (neblina, pontos sobre o mar) é limitado à temperatura e contabilizado no manifesto; excesso acima de 5 K indica arquivo corrompido e interrompe a execução, assim como anos faltando ou sobrepostos entre os arquivos. No CDS, pedir a grade de 0,75° dobra o custo: cabe um ano por pedido, e a fila aceita poucos pedidos simultâneos por usuário.

`ipca` baixa a tabela SIDRA 1737, variável 2266 (número-índice, dez/1993 = 100), confere os rótulos das colunas e recusa meses repetidos, lacunas ou valores ausentes. Sem `--refresh`, usa o retrato mais recente em `data/raw/ipca/`; com `--refresh`, grava um novo arquivo datado (`sidra-1737-AAAAMMDD.json`) sem apagar os anteriores, para que deflações já feitas continuem rastreáveis. Fonte: IBGE, Sistema Nacional de Índices de Preços ao Consumidor.

`calendar` gera, para cada região e dia, `holiday` (feriados públicos nacionais e do estado da região, pelo pacote `holidays`) e `optional_holiday` (pontos facultativos como o Carnaval). Feriados municipais não estão cobertos. O pacote está fixado em `holidays==0.108` porque o calendário pode mudar entre versões; a versão usada fica no manifesto. Ele exige `python-dateutil` < 3, o que rebaixou essa dependência de 4.0.0 para 2.9.0.post0 no `uv.lock`.

Os três comandos gravam, ao lado do Parquet, um `.source.json` com hashes das entradas, período e atribuições; saída e manifesto são substituídos juntos.

## Extração nacional do SIH

```sh
uv run --extra sih folego sih-national --start 2012-01 --end 2026-10
```

Para cada UF e competência, baixa o `RD<UF><AAMM>.dbc` do FTP do DATASUS, guarda só as AIH com diagnóstico principal nos capítulos I (circulatório), J (respiratório), S e T (controles negativos), de qualquer residência, em `data/staging/sih/national/<ano>/<UF>/<AAAAMM>.parquet`, e apaga o DBC. O manifesto `.source.json` de cada partição registra URL, data e SHA-256 do DBC original, contagem de registros lidos e mantidos e o hash do Parquet; com isso o arquivo pode ser baixado de novo e conferido. Partições já extraídas são reaproveitadas, e um Parquet alterado depois da extração interrompe aquela partição. Competências ainda não publicadas aparecem em `unpublished` no `coverage.json`, e falhas em `failed`, sem interromper as outras partições; basta rodar de novo. `--keep-source` mantém os DBC. Um ano completo de DBC nacional tem cerca de 900 MB, e o recorte em Parquet fica em torno de 6% disso.

Arquivo ausente no FTP só conta como `unpublished` depois da última competência extraída daquela UF; lacuna antes dela, ou uma execução que não extrai nada (como quando o diretório do FTP muda e tudo responde 550), sai em `failed` com código de erro. `coverage.json` é recalculado a partir de todos os manifestos em disco, e cada execução fica registrada em `runs/`. Os manifestos guardam o commit do código e as versões de `pyreaddbc`, `dbfread` e `polars`. Só uma execução por pasta de dados roda por vez, e os arquivos são criados legíveis apenas pelo operador.

Riscos aceitos: o FTP do DATASUS não tem TLS, então o hash registrado prova o que foi lido, mas não a origem; a conferência mensal contra o TabNet é o controle de origem. O DATASUS republica arquivos revisados com o mesmo nome, e um DBC apagado não pode ser recuperado byte a byte. `N_AIH` fica nos Parquets locais porque a deduplicação de revisões entre competências depende dele; nada com esse campo é publicado. A extração nacional também apaga os DBC de 2019 baixados pelo piloto; rodar o piloto de novo baixa os arquivos outra vez.

## Desfechos diários, conferência TabNet e painel

```sh
uv run folego outcomes
uv run folego tabnet-national
uv run folego panel --regions 11004
```

`outcomes` lê todas as partições do SIH nacional e grava `data/marts/outcomes/daily-admissions.parquet`, com contagens diárias por data de internação, região de saúde de residência, faixa etária e desfecho. Os desfechos são:
- `respiratory` (J00–J99, inclui asma e pneumonia), `asthma` (J45–J46), `pneumonia` (J12–J18) e `circulatory` (I00–I99);
- `negative_control` (S00–S99, T00–T98).

O tratamento das AIH é o mesmo do piloto:
- só AIH normais (tipo 1);
- repetições idênticas entre competências contam uma vez;
- números de AIH com conteúdo clínico divergente ficam em quarentena.

O SIH registra os residentes do DF pela região administrativa (5300xx–5301xx); esses códigos vão para Brasília (530010). Se mais de 0,1% das internações ficar sem região de saúde, a execução para.

O pagamento (`VAL_TOT`) é corrigido pelo IPCA do mês de processamento até o último mês da série. A janela de datas certificada:
- começa na primeira competência e cobre só a sequência contínua de competências com arquivos das 27 UF;
- exclui os últimos `--maturity-months` meses (padrão 6), porque internações recentes continuam chegando em competências posteriores.

O arquivo guarda só as células com internação; o manifesto registra a janela e a reconciliação. Ele também guarda o hash de todas as partições usadas, o commit do código e se há uma conferência TabNet aprovada para anos da janela (`tabnet_validation.validated`). Se uma competência no meio da série estiver incompleta enquanto meses posteriores estão completos, a execução para; basta rodar `sih-national` de novo. O pagamento considera só as AIH normais, então as AIH de continuação de internações longas não entram. Desfechos e painel têm células com menos de 5 internações e são privados (`classification` no manifesto): só podem ser publicados depois de passar por `suppress_small_cells`.

Primeira execução nacional (competências 2012-01 a 2026-07, maturidade de 6 meses, internações de 2012-01-01 a 2026-01-31):
- 50,9 milhões de internações;
- nenhuma AIH em conflito ou duplicada;
- nenhuma residência sem região;
- 7 registros com idade inválida.

Conferência TabNet: AC 2012, RO 2019, AM 2013, SP 2016, BA 2021 e RS 2024 bateram 12 de 12 meses, com erro zero. Os relatórios ficam em `data/marts/validation/history/`; cópias versionadas da conferência e da medição de maturação estão em `docs/evidence/`.

`tabnet-national` compara, para pares UF:ANO (padrão: RO 2019, AM 2013, SP 2016, BA 2021, RS 2024), as AIH respiratórias normais por UF de residência e mês de processamento com o TabNet, usando todos os arquivos estaduais do ano. Aprova com erro relativo abaixo de 1% em cada mês e no ano; caso contrário, sai com erro. Primeira conferência parcial: AC 2012 bateu 12 de 12 meses, sem diferença.

`panel` junta desfechos, PM2,5, meteorologia e feriados no formato abaixo, no intervalo coberto por todas as exposições, gravando uma região por vez para caber na memória. A execução para se houver lacuna de dias numa exposição, região sem nenhuma internação no arquivo de desfechos (códigos de região divergentes) ou exposições fora da janela certificada. As internações são zeradas apenas dentro da janela certificada e ficam nulas fora dela. `mean_payment` é o pagamento corrigido dividido pelas internações do dia.

## SRAG, maturação e estudo regional

```sh
uv run folego srag --start 2012 --end 2025
uv run folego maturity
uv run folego panel --regions 11004 --srag data/marts/srag/daily-srag.parquet
uv run folego study --region 11004
```

`srag` descobre nas páginas do Portal de Dados Abertos do SUS o arquivo mais recente de cada ano do SIVEP-Gripe (CSV até 2018, Parquet a partir de 2019). Só são aceitos os hosts oficiais. Cada arquivo é reduzido a contagens diárias de SRAG hospitalizada por data de início dos sintomas, região de saúde de residência e classificação final, e depois é apagado; o manifesto guarda URL e hash. A coluna `viral` soma influenza, COVID-19 e outros vírus. A SRAG sem especificação fica separada, porque a própria fumaça pode causá-la e ajustar por ela removeria parte do efeito. Um arquivo do ano anterior e um do seguinte também são lidos, quando publicados, porque o arquivo é por ano de notificação. O ano corrente não é aceito, porque ainda está em notificação. A extração para se não houver internações, se mais de 2% das residências ficarem sem região ou se mais de 1% das datas de início forem inválidas; o arquivo bruto é apagado mesmo quando a extração falha. A classificação final (1 influenza, 2 outro vírus, 3 outro agente, 4 não especificado, 5 COVID-19) é tratada como estável entre os dicionários de 2012 a 2025; isso ainda precisa ser conferido nos dicionários de cada período. No painel, `--srag` adiciona `srag` = log(1 + casos virais com início nos 7 dias anteriores ao dia, sem incluí-lo). O dia corrente fica de fora porque a SRAG viral do próprio dia é em parte o desfecho e em parte mediadora do PM2,5.

`maturity` mede, para os meses de internação com pelo menos 24 competências posteriores, a fração dos registros que chega até k meses depois. Resultado nacional: 48% no próprio mês, 98,6% em 3 meses e 99,99% em 6.

`study` ajusta, para cada desfecho (todas as idades, 0–4 e 60+), o modelo quasi-Poisson com base cruzada do PM2,5 e confundidores. O relatório em `data/marts/studies/<região>.json` traz:
- o risco relativo cumulativo de +10 µg/m³ sustentado em todos os lags, com IC 95%;
- o mesmo risco com a tendência temporal de 4 e 10 graus de liberdade por ano (o principal usa 7), para mostrar quanto ele depende da suavização;
- a dispersão e a autocorrelação dos resíduos (ACF e Ljung-Box);
- o teste conjunto de exposição, cuja significância no controle negativo (S/T) sinaliza confusão residual;
- quando há SRAG no painel, o efeito com e sem o ajuste por SRAG;
- um backtest de 2019 a 2024 (MAE/MAPE semanais).

O relatório é de pesquisa (`validated=false`) e guarda o commit do código e o hash do painel; cada execução é mantida em `history/`. O campo `confounders` descreve a especificação usada e cada estrato traz o `trend_df` efetivo. Relatórios sem `confounders` (anteriores a 2026-11-21) usavam 6 graus de tendência e três harmônicos também na estimativa do efeito e não são comparáveis com os novos. O backtest usa exposições observadas, e uma dobra sem histórico suficiente aparece com erro sem invalidar as outras.

## Focos de queimada e MERRA-2

```sh
uv run folego fires --start 2012-01-20 --end 2025-12-31
uv run --extra climate folego merra2 --start 2012-01 --end 2025-12
uv run folego panel --regions 11004 --merra2 data/marts/exposure/pm25-merra2-daily.parquet --fires data/marts/exposure/fires-daily.parquet
uv run folego study --region 11004 --exposure pm25_merra2
```

`fires` percorre o arquivo FIRMS VIIRS S-NPP (375 m, de 2012-01-20 em diante) em blocos de 5 dias, o máximo da API, sobre o retângulo do Brasil. Ficam só os focos de vegetação (tipo 0) com confiança nominal ou alta. Cada foco vai para a região de saúde que contém o ponto e para o dia local (UTC−3). Saem `fire_count` e `frp` (potência radiativa) por região e dia. Os focos dentro do retângulo mas fora do Brasil (Bolívia, Paraguai, Peru) são contados à parte. Só o agregado de cada bloco é gravado, com o hash da resposta; nenhum dado de foco individual fica em disco. O dia local D vai das 03 UTC de D às 03 UTC de D+1, então o comando busca um dia UTC além do fim. Ele recusa datas que o arquivo de processamento padrão ainda não cobre, em vez de preenchê-las com zero. Dias de pane do satélite S-NPP aparecem como zero focos; é uma limitação conhecida. Em cada nova execução, os blocos já baixados são reaproveitados só se os parâmetros e o hash conferirem. Quando a cota de transações por 10 minutos se esgota, o comando espera e tenta de novo.

`merra2` baixa do OPeNDAP do Earthdata Cloud (o OPeNDAP próprio do GES DISC foi aposentado) só as cinco espécies de aerossol e a janela do Brasil, um dia por vez. Calcula o PM2,5 de Buchard et al. (2016) — poeira e sal marinho < 2,5 µm, carbono negro, 1,4 × carbono orgânico e 1,375 × sulfato — e a média do dia local a partir das 24 médias horárias. Grava um NetCDF diário por mês e descarta os dados horários. Um mês só é gravado completo: se o primeiro dia do mês seguinte ainda não foi publicado, o comando para. A ponderação populacional é a mesma do CAMS. Num teste com agosto de 2019, a Madeira-Mamoré chegou a 146 µg/m³.

`merra2 --product weather` faz o mesmo com o produto M2T1NXSLV: temperatura (T2M) e umidade relativa calculada a cada hora a partir de T2M e do ponto de orvalho (T2MDEW), pela mesma fórmula de Magnus e com o mesmo limite de ponto de orvalho da série do ERA5. Grava `data/marts/exposure/meteorology-merra2-daily.parquet` com as colunas do ERA5, para servir de `--meteorology` no painel enquanto o ERA5 não sai da fila do Copernicus. Diferenças em relação ao ERA5 desta série: dados horários (não a cada 3 h) numa grade de 0,5° × 0,625° (não 0,75°). Os meses intermediários ficam em `data/staging/merra2-slv/`. Fonte: GMAO, MERRA-2 tavg1_2d_slv_Nx, DOI 10.5067/VJAFPLI1CSIV. Um estudo feito com essa meteorologia registra `meteorology: MERRA-2` no relatório e grava em `<região>-met_merra2.json`, para não sobrescrever o resultado com ERA5; ao reportar, cite a fonte usada. No teste com agosto de 2019, Porto Velho teve média de 29 °C e 55% de umidade.

No painel, `--merra2` e `--fires` acrescentam essas colunas sem mudar o intervalo da análise principal; os dias que essas fontes não cobrem ficam nulos. No estudo, `--exposure pm25_merra2` repete a análise com o MERRA-2 no lugar do CAMS e grava o relatório em `data/marts/studies/<região>-pm25_merra2.json`. O relatório registra em `pm25_source` a fonte da coluna `pm25` do painel e em `exposure_source` a da coluna modelada.

Sem CAMS e ERA5, o painel aceita os marts do MERRA-2 como fontes principais: um mart de PM2,5 com a coluna `pm25_merra2` vira `pm25`, e a meteorologia do MERRA-2 entra no lugar da do ERA5. Para que os relatórios não se sobrescrevam, o estudo acrescenta ao nome `-met_merra2` quando a meteorologia é do MERRA-2, `-pm25main_merra2` quando o PM2,5 principal é do MERRA-2 e `-unknown_source` quando a fonte não é reconhecida no manifesto do painel. O download do MERRA-2 repete cada requisição até 4 vezes em erro de rede, 429 ou 5xx, e avisa cada nova tentativa na saída de erro.

## Painel diário preparado para pesquisa

O comando `folego forecast` recebe dois Parquets privados com uma linha por `date` (tipo Date), `health_region_code`, `age_group`, `outcome`. As chaves devem ser únicas e as datas completas e contíguas em cada grupo:

| Coluna | Histórico | Futuro |
| --- | --- | --- |
| `pm25` | observado até a origem, µg/m³ | previsão arquivada de 1–5 dias |
| `temperature` | média diária, °C | previsão diária, °C |
| `humidity` | umidade relativa, 0–100 | previsão diária, 0–100 |
| `holiday` | indicador 0/1, calendário local verificado | mesmo calendário |
| `admissions` | inteiro não negativo; ausente quando indisponível | não usado |
| `mean_payment` | média de AIH já corrigida pelo IPCA e estrato | não usado |
| `srag` | opcional, fonte e granularidade auditadas | obrigatório se usado no treino |

O histórico vai até `--issued-on`, a exposição é completa e o desfecho só é usado até origem − 62 dias. O futuro começa no dia seguinte. Pelo menos 365 linhas válidas devem existir no treino de cada estrato. O custo é obtido pela média ponderada dos pagamentos por número de internações. Grupos com ajuste não identificável ou dados inválidos interrompem a execução; não são convertidos em risco zero.

Saída em `data/marts/research-forecast.json`: `mode=research`, `validated=false`, resultados por estrato, corte de treino e incerteza. É um artefato de pesquisa, não é consumido pelo painel. Não há serialização insegura por pickle; cada execução ajusta o modelo a partir dos painéis fornecidos.

## Automação

- `ci.yml`: lint/formatação, testes Python, teste do painel em desktop/celular com axe, build e deploy Pages na `main` após sucesso. Permissões de publicação ficam somente no job de deploy.
- `daily.yml`: às 09:20 UTC, valida o snapshot e tenta atualizar a correspondência, guardando relatório como artefato. Timeout/erro HTTP da fonte produz estado `degraded`, aviso explícito e `freshness_verified=false`; o snapshot validado é preservado. Cobertura incompleta ou conteúdo inválido continuam interrompendo o job. O primeiro teste no GitHub encontrou timeout de conexão ao IBGE; isso não impede o painel estático. Caso as variáveis `FOLEGO_HISTORY_URL` e `FOLEGO_FUTURE_URL` estejam configuradas com painéis agregados preparados, executa uma previsão de pesquisa e guarda o resultado privado ao contexto de acesso dos Actions. Não coloque URLs com credenciais nessas variáveis; este modo só atende arquivos públicos sem autenticação. Avalie a política de acesso de artefatos antes de usar dados reais.
- O cron não baixa automaticamente décadas de SIH, não treina um modelo nacional validado e não publica previsões reais sem revisão. A operação CAMS diária completa depende da integração científica listada em `validation.md`.

## Publicação

O Vite gera `web/dist`, servido no GitHub Pages. Caminhos relativos permitem hospedar sob `/folego/`. Dados públicos ficam em `web/public/data` e o build os copia sem transformação. MapLibre e PMTiles são dependências travadas; a malha atual é GeoJSON local, sem serviços de tiles ou contas externas.

`folego export arquivo.parquet` publica somente o esquema agregado permitido, um desfecho por arquivo e idades disjuntas. Não combine resultados com totais que revelem células suprimidas. Os microdados jamais devem ir ao diretório público. O site atual aceita somente o contrato de demonstração, de propósito; adicionar publicação real exige cumprir os critérios científicos e adaptar o contrato de forma auditável.

## Piloto SIH real e conferência no TabNet

```sh
uv run --extra sih folego pilot --config configs/pilots/madeira-mamore-2019.json \
  --require-tabnet --publish-report docs/pilots/madeira-mamore-2019.md
```

O comando consulta os 12 meses de processamento das 27 UFs, mantendo o recorte **por residência** nos cinco municípios. Não limite a coleta à UF de residência: em 2019, isso perderia 45 das 2.677 internações respiratórias do piloto. O volume bruto comprimido é aproximadamente 883 MiB. Há reserva mínima de disco para descompressão e cache por SHA-256 da fonte, lista de municípios e versão do leitor. A projeção usa os metadados DBF do `dbfread` e filtra o município antes de decodificar/reter os demais campos; não materializa todas as colunas e todos os pacientes do Brasil.

Resultados locais em `data/staging/pilots/madeira-mamore-2019/`: partições selecionadas, controle mensal por processamento, série diária observada por data da internação e linhas segregadas. Relatório completo em `data/marts/pilots/madeira-mamore-2019/report.json`. O controle TabNet usa registros respiratórios de AIH normal, todas as idades, antes da reconciliação de episódios. A série diária exclui continuidades, colapsa repetições idênticas e segrega identificadores com informações divergentes.

A consulta de referência é construída a partir das opções do formulário oficial, incluindo os índices dos municípios e do capítulo CID, sem presumir índices fixos. O retorno deve confirmar os mesmos filtros e os 12 períodos no registro de consulta que o TabNet inclui no HTML. O parser suporta células HTML com fechamento implícito e meses por extenso. Formulário, resposta, requisição e SHA-256 ficam em `data/raw/tabnet/`. `--require-tabnet` faz o comando falhar se a comparação não for aceita; `--skip-tabnet` permite investigar somente a ingestão, sem alegar validação.

A exportação do relatório mantém só totais amplos e proveniência dos arquivos públicos. Identificadores, contagens por arquivo da coorte e células diárias não são publicados; a tabela mensal inteira é omitida se houver contagem menor que 5. O recorte por ano de processamento não certifica uma coorte madura por data da internação: dados do início podem se referir a admissões anteriores, e admissões do fim podem aparecer em competências posteriores. Por isso não há preenchimento de zeros nem liberação para ajuste do modelo neste piloto.
