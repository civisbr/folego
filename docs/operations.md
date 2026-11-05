# Operação e contratos

## Ambiente e armazenamento

`make setup` instala o ambiente de Python 3.12 com `uv.lock` e o cliente com `web/package-lock.json`. Microdados, NetCDF, banco DuckDB e resultados intermediários ficam em `data/` (ignorado pelo Git). Camadas: `raw/` imutável por requisição → `staging/` normalizada → `marts/` agregada. `folego.warehouse.connect_warehouse(spatial=True)` instala/carrega a extensão oficial DuckDB spatial quando a análise precisar dela.

Downloads comuns são atômicos, cacheados e acompanhados de URL, SHA-256 e instante de recuperação. Requisições CAMS são identificadas pelo hash do JSON; preserve data e hora da rodada em cada requisição para não confundir previsões arquivadas. O cache não é uma política automática de retenção ou de atualização de fontes mutáveis.

## Credenciais e fontes

- SIH: FTP público DATASUS; a conversão DBC usa o leitor `pyreaddbc` mantido pelo projeto PySUS, sem a dependência transitiva de DuckDB < 2, instalado com `uv sync --extra sih`, que pode exigir bibliotecas nativas do leitor pyreaddbc. O download nacional histórico é volumoso; comece por competência/UF e faça inventário dos arquivos.
- FIRMS: `FIRMS_MAP_KEY` no ambiente. A chave não entra no manifesto; o adaptador não deve ser chamado com logging de URLs sensíveis. O modo implementado usa VIIRS SNPP NRT; arquivos históricos precisam de ingestão própria.
- CAMS: configure o cliente `cdsapi` para o ADS, aceite os termos do dataset e exporte a requisição pela interface oficial. Use `uv sync --extra climate`. API e nomes de parâmetros podem mudar; o código recebe o JSON oficial, sem inventar parâmetros ou substituir silenciosamente variáveis.
- ERA5: conta CDS, termos aceitos e requisição oficial. ERA5 é reanálise histórica; previsões operacionais exigem outro produto meteorológico e seu arquivo de rodadas.
- InfoGripe: indique explicitamente a URL do snapshot oficial. A compatibilidade de granularidade deve ser verificada antes de acrescentar o termo SRAG.

Nunca coloque chaves em arquivos versionados, argumentos de processos ou documentação. `.env.example` contém apenas nomes. O projeto não carrega `.env` automaticamente; use seu gerenciador de segredos.

## Territorialização

`folego territory --refresh` consulta IBGE e pagina a API nacional do Ministério da Saúde com páginas de 500 linhas. Falta de correspondência interrompe a execução por padrão; `--allow-incomplete` serve apenas para investigação. O complemento de Boa Esperança do Norte foi conferido na Resolução CIB/MT nº 507/2025; a população de 2022 permanece ausente, pois não se deve inventar um denominador de um município novo.

O snapshot público tem 5.571 municípios e 439 regiões. A malha mínima da API IBGE contém 5.570 polígonos, anterior ao município novo; o manifesto declara isso. Os municípios de origem e o novo território estão na região Teles Pires. Não use a malha simplificada para análise de exposição fina; ela atende apenas ao mapa público.

Para atualizar os arquivos territoriais públicos, regenere a correspondência, confira a cobertura e exporte os formatos JSON/CSV/Parquet e o manifesto. A publicação automática desses snapshots foi deliberadamente evitada: mudanças de limites exigem revisão. `folego geography` dissolve os polígonos oficiais pela tabela em staging. O comando `folego demo` regenera os exemplos determinísticos, sem alterar a data para parecer uma previsão nova.

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
