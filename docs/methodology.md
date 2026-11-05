# Metodologia

O desenho científico completo está em [PROJECT.md](../PROJECT.md). A interface pública repete suas limitações em “Como funciona”. Este documento descreve especificamente o que o código atual faz.

## Unidade, desfecho e exposição

Unidade principal: região de saúde × faixa etária (0–4, 5–14, 15–59, 60+) × dia da internação. O município é o de **residência**, não o do hospital. Códigos IBGE têm 7 dígitos e DATASUS têm 6; a correspondência nacional acompanha o snapshot oficial. Nove códigos com dígito verificador excepcional, presentes na lista oficial do IBGE, têm uma lista explícita de exceções.

Internações respiratórias: J00–J99. Asma: J45–J46; pneumonia: J12–J18; circulatórias: I00–I99. Controle negativo proposto: S00–T98, cuja adequação deve ser investigada (traumas também podem se alterar em desastres). AIH de continuidade (`IDENT=5`) não conta como nova internação. Identificadores repetidos geram erro: revisões e competências precisam ser conciliadas antes de combinar arquivos. Não some anos de competência como se fossem anos de internação sem essa reconciliação.

A agregação exclui idade desconhecida; o uso científico exige um relatório de completude. Zeros só podem ser introduzidos em uma janela certificada de cobertura SIH completa. Ausência de arquivo ou exposição não equivale a zero.

`population_weighted_exposure` recebe interseções pixel × região com população alocada. Calcula Σ(PM2,5 × população)/Σ(população). A extração dos rasters, a harmonização da grade populacional e a sobreposição espacial ainda precisam ser integradas e auditadas. Não substitua população por área. Os valores CAMS em kg/m³ precisam ser multiplicados por 10⁹ para µg/m³.

**PM2,5 total não é exclusivamente fumaça.** Um modelo com PM2,5 total estima associação com partículas totais. A atribuição específica às queimadas exige exposição de fumaça, contrafactual apropriado ou análise adicional de fontes.

## Modelo implementado

GLM Poisson com escala quasi-Poisson estimada pela dispersão de Pearson, limitada inferiormente a 1. Matriz de desenho inclui intercepto; erros de rank e dados não finitos interrompem o ajuste. A dispersão do GLM não corrige, sozinha, autocorrelação residual; diagnóstico e estimadores de incerteza adequados ainda devem ser avaliados.

A base cruzada usa funções lineares por partes para exposição (nós fixos 15, 35 e 75 µg/m³) e polinômios de Legendre de grau 2 para lags 0–7. A base de exposição é centrada em 15 µg/m³ e escalada por 10. Os primeiros 7 dias permanecem ausentes, sem preenchimento artificial. Essa parametrização é inspirada em DLNM e **não equivale numericamente** ao padrão `ns` × `ns` do pacote R `dlnm`.

Confundidores: splines cúbicas de temperatura/umidade, spline cúbica natural de tempo, três harmônicos anuais, dia da semana e indicador de feriado fornecido pelo painel de entrada. O termo SRAG é opcional e exige dados compatíveis por local e tempo. Nós e matrizes de spline são aprendidos exclusivamente no treino e reutilizados na previsão.

## Previsão e contrafactual

O executor recebe painéis diários já preparados e completos, ajusta um modelo por região/idade/desfecho, respeita uma defasagem conservadora de publicação do SIH de 62 dias e exige pelo menos 365 observações de treino válidas. Exposições observadas até a origem alimentam os lags. O cenário contrafactual preserva esse histórico e substitui apenas os 1–5 dias futuros por 15 µg/m³. Ele estima a parcela evitável pela mudança da exposição futura, não todo o efeito residual de exposições passadas.

Excesso = Σ[E(Y | exposição prevista) − E(Y | cenário de referência)]. Valores negativos são preservados; truncá-los após o ajuste introduziria viés. O pagamento adicional é o excesso multiplicado pelo valor médio por AIH do estrato, com médias diárias ponderadas pela contagem. Os índices de IPCA e o mês-base devem ser fornecidos e documentados antes da execução. `VAL_TOT` é remuneração do SUS, não custo hospitalar real.

Sorteios conjuntos multivariados dos coeficientes, com semente fixa, geram um intervalo de 95% para o excesso médio condicional. Não se somam limites independentes de intervalos probabilísticos. O intervalo não inclui todo o erro de previsão de contagens, exposição, confundidores, especificação ou pagamento. **As faixas da demonstração visual são artificiais e não usam essa interpretação.**

## Validação necessária

A referência de aceitação é [docs/validation.md](validation.md). Origens móveis precisam usar somente dados disponíveis na origem e previsões ambientais arquivadas. O código reserva 62 dias para publicação clínica, mas isso é uma aproximação; um calendário real de disponibilidade por competência é preferível. MAE/MAPE são calculados após agregação semanal, excluindo semanas incompletas. MAPE exclui denominadores zero e informa quantos foram excluídos. Precisão/recall de picos exigem limiar de P90 definido no treino.

## Fontes primárias

- [Gasparrini (2011), Journal of Statistical Software](https://www.jstatsoft.org/article/view/v043i08).
- [CAMS Global Atmospheric Composition Forecasts](https://ads.atmosphere.copernicus.eu/datasets/cams-global-atmospheric-composition-forecasts?tab=overview) e [documentação ECMWF](https://confluence.ecmwf.int/pages/viewpage.action?pageId=673323626).
- [DATASUS](https://datasus.saude.gov.br/) e [API de Dados Abertos do SUS](https://apidadosabertos.saude.gov.br/).
- [IBGE Localidades](https://servicodados.ibge.gov.br/api/docs/localidades).
- [Resolução CIB/MT nº 507/2025](https://www.saude.mt.gov.br/storage/files/jlxe9jkkPp4k6viKKrRmoz1EGsHuGqETtZfc5cfA.pdf), usada no complemento de Boa Esperança do Norte.

Os títulos, autores e anos de Rocha & Sant’Anna (DOI 10.1016/j.worlddev.2021.105722) e Tadano et al. (DOI 10.1016/j.chemosphere.2024.143688) foram conferidos nos metadados Crossref. A leitura e a revisão quantitativa dos artigos completos ainda são necessárias antes de calibrar ou comparar efeitos. Nenhum efeito publicado foi inserido como coeficiente fixo nesta implementação.
