# Roblox News → Discord — GitHub Actions

Esta versão foi feita para rodar fora do seu PC usando GitHub Actions.

Fontes:
- `@LeaksEvents`
- `@Bloxy_News`

IMPORTANTE: esta versão não usa Bearer Token nem créditos da API oficial do X.
Ela usa o feed público de syndication/timeline do X. Esse mecanismo não é uma
API oficial documentada para automação e pode ser limitado ou mudar de formato.

## Como colocar no GitHub

1. Crie um repositório no GitHub. Para manter os custos no mínimo, deixe-o público.
2. Envie todos os arquivos desta pasta para o repositório.
3. No repositório, abra:
   Settings → Secrets and variables → Actions
4. Clique em:
   New repository secret
5. Nome:
   `DISCORD_WEBHOOK_URL`
6. Em Secret, cole o webhook NOVO do Discord.
7. Vá em:
   Actions → Roblox News → Run workflow
8. Depois disso, o workflow também ficará agendado a cada 15 minutos.

O workflow não precisa do seu PC ligado.

## O que ele faz

A cada execução:
1. lê `@LeaksEvents`
2. lê `@Bloxy_News`
3. compara os IDs com `data/seen.json`
4. manda posts novos para o Discord
5. salva os IDs já enviados no próprio repositório

## Primeira execução

O padrão é `POST_EXISTING_ON_FIRST_RUN=false` para não despejar o histórico
inteiro no canal.

Para um teste controlado, altere temporariamente o valor no workflow para
`true` e `POSTS_PER_SOURCE` para `1`.

## Observações

- O agendamento do GitHub Actions pode atrasar em relação ao minuto exato.
- O X pode responder `429` ou alterar o feed público.
- O workflow está em uma execução curta e termina; ele não fica ocupando seu PC.
- Em repositórios públicos, workflows agendados podem ser desativados após
  60 dias sem atividade no repositório, segundo a documentação do GitHub.

## Estrutura

```text
.github/
  workflows/
    check.yml
data/
  seen.json
main.py
requirements.txt
README.md
```
