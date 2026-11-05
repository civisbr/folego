# Estado de implementação e validação

Primeira versão, novembro de 2026. **Não há desempenho epidemiológico real a reportar.** Os resultados de testes sintéticos verificam implementação, não demonstram eficácia operacional ou inferência causal.

| Entrega / aceite do projeto | Estado e evidência |
| --- | --- |
| Python 3.12, uv, Ruff, pytest, Makefile | Implementados; `make check` executa validações locais. |
| Correspondência de 100% dos municípios | Snapshot com 5.571/5.571, 439 regiões; teste nacional e manifesto versionados. Um complemento com resolução oficial da SES/MT. |
| SIH RD: download e conversão DBC → Parquet | Adaptador implementado com dependência opcional; download nacional desde 2012 não executado. |
| Desfechos CID/idade/data/residência | Implementados e testados com exemplos controlados; concordância TabNet < 1% pendente. |
| PM2,5 ponderado pela população | Operação testada; extração dos rasters e overlay populacional nacional pendentes. |
| FIRMS, CAMS, ERA5, InfoGripe | Adaptadores e cache; integração autenticada e validação de granularidade pendentes. |
| Modelo com lags e confundidores | Implementado; testes sintéticos recuperam efeito conhecido e exercitam fluxo de previsão. Ajuste científico por região pendente. |
| Excesso, pagamentos e incerteza | Implementados com simulação conjunta e preservação do histórico observado. IPCA real precisa ser fornecido. |
| Controle negativo | Teste Wald conjunto implementado; avaliação com SIH real pendente. |
| Backtest 2019–2024 | Divisões temporais e executor de métricas implementados; estudo real e relatório de MAE/MAPE/precisão/recall pendentes. |
| CAMS × MERRA-2 | Sensibilidade pendente; adaptador MERRA-2 não implementado. |
| Painel e arquivos abertos | Implementados, com mapa real e valores sintéticos explícitos; publicação via GitHub Pages. |
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

Executados localmente: 20 testes pytest aprovados; 8 testes Playwright aprovados (quatro cenários em desktop e celular), incluindo navegação, filtros, downloads, resposta a falhas, verificação axe de acessibilidade e renderização efetiva dos polígonos do mapa. Ruff, formatação e build de produção aprovados. Auditoria npm sem vulnerabilidades conhecidas na instalação verificada. A inspeção visual e as capturas em `docs/dashboard*.png` registram o resultado. Esses controles não substituem teste com usuários nem validação epidemiológica.

A primeira execução remota aprovou os jobs Python, web e deploy. A auditoria online adicional encontrou timeout de conexão ao IBGE no runner; o tratamento distingue fonte indisponível (`degraded`, sem confirmação de atualização) de erro de validação (`failed`), preservando a versão pública anterior.
