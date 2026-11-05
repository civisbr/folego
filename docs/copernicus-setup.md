# Criar conta e configurar o Copernicus ADS

Esta configuração libera o próximo passo do piloto: baixar PM2,5 histórico do CAMS.
A conta e o aceite das licenças são feitos pelo próprio usuário no portal oficial.

1. Abra o [Atmosphere Data Store](https://ads.atmosphere.copernicus.eu/) e clique em
   **Login – Register**. Siga o cadastro da conta ECMWF e confirme seu e-mail quando solicitado.
2. Entre no ADS e complete as informações obrigatórias do perfil com seus dados reais.
   O campo organização é opcional no [perfil ADS](https://ads.atmosphere.copernicus.eu/profile).
3. Abra [CAMS global reanalysis (EAC4)](https://ads.atmosphere.copernicus.eu/datasets/cams-global-reanalysis-eac4?tab=download),
   vá à aba **Download**, leia os termos e aceite a licença ao final do formulário.
   O [guia oficial](https://ads.atmosphere.copernicus.eu/how-to-api) exige esse aceite manual.
4. Abra o [perfil ADS](https://ads.atmosphere.copernicus.eu/profile) e encontre **API key**.
   Copie somente o valor da chave; não a envie na conversa nem a salve no repositório.
5. No terminal desta máquina, na pasta do Fôlego, execute:

   ```sh
   uv run folego configure-ads
   ```

   Cole a chave quando solicitado. A entrada fica oculta. O comando cria `~/.cdsapirc`
   com permissão `600`, preservando qualquer arquivo de configuração preexistente.

O formato segue a documentação oficial:

```yaml
url: https://ads.atmosphere.copernicus.eu/api
key: "SEU_TOKEN"
```

O comando salva a configuração local; ele não afirma que a autenticação ou a licença
já foram validadas. O próximo download pequeno do CAMS fará essa verificação. Não é
necessário instalar pacotes manualmente agora; a dependência opcional `climate` do
projeto será usada na etapa de ingestão ambiental.

Depois do histórico, também será preciso aceitar a licença de
[CAMS global atmospheric composition forecasts](https://ads.atmosphere.copernicus.eu/datasets/cams-global-atmospheric-composition-forecasts?tab=download)
para o modo de previsão operacional.

Fontes: [configuração oficial CDSAPI para ADS](https://ads.atmosphere.copernicus.eu/how-to-api)
e [perfil ADS](https://ads.atmosphere.copernicus.eu/profile). Conferidas em 5 de novembro de 2026.
