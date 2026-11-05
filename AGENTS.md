# Convenções do Fôlego

- Todos os commits humanos deste repositório devem usar `CivisBR <compact.chipmunk.sqpx@mask.me>` como autor e committer, conforme o padrão do civisbr/visse. Nunca use a identidade pessoal do proprietário.
- Configure essa identidade apenas neste repositório com `git config --local`; não altere a identidade global.
- Código e identificadores em inglês; documentação e interface em português.
- Use mensagens de commit com prefixos como `feat:`, `fix:`, `test:` e `docs:`, em inglês.
- Antes de alterar modelagem, escreva um teste sintético com efeito conhecido. Não publique previsões reais sem os critérios de `docs/validation.md`.
- Microdados e credenciais ficam fora do Git. Preserve a identificação dos dados sintéticos e os arquivos originais de marca e referência.
