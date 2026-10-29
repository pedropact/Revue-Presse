# Revue de presse Québec–Canada

App que, uma vez por dia, lê notícias de sites do Quebec e do Canadá (em francês e inglês), classifica por tema, junta as notícias que falam do mesmo fato em sites diferentes, resume cada uma em francês com um modelo de IA pequeno e publica as 3 principais de cada tema num site/PWA no Cloudflare Pages.

**Custo: zero.** Roda no GitHub Actions (grátis), publica no Cloudflare Pages (grátis) e se instala no celular sem App Store nem Google Play.

## Como funciona

1. **Coleta** (`src/collecte.py`): lê os feeds RSS listados em `config.yaml` (últimas 30 h).
2. **Temas** (`src/analyse.py`): o modelo multilíngue `paraphrase-multilingual-MiniLM-L12-v2` compara cada notícia com as descrições dos 6 temas, com bônus por palavra-chave e pela seção do feed. Em "Relations extérieures", a notícia precisa mencionar o Canadá ou o Quebec.
3. **Agrupamento**: notícias com títulos semanticamente próximos, em FR ou EN, viram um único "sujet".
4. **Ranking**: número de veículos diferentes que cobriram o fato + recência + pertinência ao tema.
5. **Texto completo**: baixado só para as notícias selecionadas e só de sites sem paywall. Para os sites com paywall, usa-se o título e a chamada do RSS.
6. **Resumo** (`src/resume.py`): Qwen2.5‑1.5B‑Instruct (GGUF, CPU via llama.cpp) escreve título + resumo em francês, fundindo as fontes.
7. **Site** (`src/site_web.py`): gera `public/` (HTML + `data.json` + PWA), publicado via `wrangler`.

## Instalação (uma vez só, ~15 min)

### 1. GitHub
1. Crie um repositório **privado** (ex.: `revue-presse`) e envie este código para ele.
2. Repositório privado tem 2.000 min/mês grátis de Actions. Cada execução deve levar entre 15 e 40 min, o que dá conta de uma execução por dia.

### 2. Cloudflare
1. Crie uma conta grátis em cloudflare.com.
2. **Account ID**: no painel, em *Workers & Pages*, aparece na coluna da direita.
3. **API Token**: *My Profile → API Tokens → Create Token → Custom token* com a permissão **Account → Cloudflare Pages → Edit**.

### 3. Segredos no GitHub
Em *Settings → Secrets and variables → Actions → New repository secret*, crie:
- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_ACCOUNT_ID`

Opcional: crie a variável (aba *Variables*) `CF_PROJET` para mudar o nome do projeto. O nome padrão é `revue-presse`, e ele define o endereço `https://revue-presse.pages.dev`. Se esse nome já estiver em uso no Cloudflare, escolha outro.

### 4. Primeira execução
1. *Actions → Revue de presse quotidienne → Run workflow*.
2. Para um teste rápido, marque **Sans modèle de résumé**.
3. Depois disso, roda sozinho todo dia: começa às 6h05 e fica pronto por volta das 7h (horário de Montreal, com ajuste automático do horário de verão).

### 5. Instalar no celular
Abra o endereço `https://<projeto>.pages.dev`:
- **iPhone (Safari)**: Compartilhar → *Adicionar à Tela de Início*.
- **Android (Chrome)**: menu ⋮ → *Instalar app*.

### 6. (Opcional) Deixar o site privado
O endereço `.pages.dev` é público, embora ninguém o encontre sem saber o link e o site esteja marcado como `noindex`. Para exigir login:
1. Cloudflare → seu projeto Pages → *Settings → Access policy*. O **Cloudflare Access** é grátis até 50 usuários.
2. Escolha login por código enviado ao seu e-mail.

## Ajustes

Tudo fica em `config.yaml`:
- **Sources**: adicione ou remova feeds. Marque `paywall: true` para sites pagos.
- **Temas**: edite `descriptions` (frases que descrevem o tema) e `mots_cles`.
- **Sensibilidade**:
  - `seuil_theme`: suba esse valor para ter menos notícias fora do tema.
  - `seuil_regroupement`: abaixe esse valor para juntar mais notícias no mesmo fato.
- **Qualidade do resumo**: troque para `Qwen2.5-3B-Instruct`. Os resumos ficam melhores, mas a execução leva cerca de 2x mais tempo.

Para checar quais feeds respondem, rode localmente:

```bash
pip install -r requirements.txt
python src/main.py --verifier-flux
```

Para gerar o site sem IA (rápido):

```bash
python src/main.py --sans-ia
```
