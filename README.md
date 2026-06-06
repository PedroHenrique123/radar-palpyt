# Palpyt Radar — versão web (backend Python)

Um pequeno serviço que minera notícias por RSS, calcula a **relevância** de cada uma, mostra num painel simples e (opcional) empurra as quentes no **Telegram**. Roda sozinho, 24/7.

Arquivos: `app.py` · `index.html` · `requirements.txt` · este `README.md`

---

## 1. Testar no seu computador (5 min)

1. Instale o Python (python.org) se ainda não tiver.
2. Na pasta dos arquivos, abra o terminal e rode:
   ```
   pip install -r requirements.txt
   python app.py
   ```
3. Abra **http://localhost:5000** no navegador. Pronto, o painel está rodando.

> A primeira carga leva alguns segundos (ele vai buscar as notícias). Depois fica rápido.

---

## 2. Publicar na web de graça (Render, sem terminal)

A ideia: colocar os arquivos no GitHub e ligar no Render. Tudo pelo navegador.

### a) Subir os arquivos no GitHub
1. Crie uma conta grátis em **github.com**.
2. Clique em **New repository** → nome `palpyt-radar` → **Create**.
3. Na página do repositório, clique em **Add file → Upload files** e arraste os 4 arquivos (`app.py`, `index.html`, `requirements.txt`, `README.md`) → **Commit changes**.

### b) Ligar no Render
1. Crie uma conta grátis em **render.com** (dá pra entrar com o GitHub).
2. **New + → Web Service** → conecte o repositório `palpyt-radar`.
3. Preencha:
   - **Language / Runtime:** Python
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `gunicorn app:app --bind 0.0.0.0:$PORT --workers 1`
   - **Instance Type:** Free
4. Clique em **Create Web Service**. Em 1–2 min ele te dá uma URL pública tipo `https://palpyt-radar.onrender.com`.

> **Atenção (plano grátis):** se ninguém acessa por um tempo, o serviço "dorme" e a próxima visita demora uns segundos pra acordar. A solução está no passo 4.

---

## 3. Ligar os avisos no Telegram (opcional)

1. No Telegram, fale com **@BotFather** → `/newbot` → guarde o **token**.
2. Fale com **@userinfobot** → ele te dá o seu **chat id** (número). Mande uma mensagem pro seu bot primeiro, pra liberar.
3. No Render: **Environment → Add Environment Variable**, adicione:
   - `PALPYT_TG_TOKEN` = seu token
   - `PALPYT_TG_CHAT` = seu chat id
4. Salve. A partir daí, toda notícia nova com relevância alta cai no seu Telegram.

---

## 4. Deixar de pé 24/7 (truque grátis)

Pra ele não dormir e continuar varrendo e avisando sozinho:

1. Crie uma conta grátis em **cron-job.org**.
2. Crie um job que acessa **`https://SUA-URL.onrender.com/api/noticias`** a cada **5 minutos**.

Isso mantém o serviço acordado, força a varredura e dispara os avisos no Telegram — mesmo sem ninguém olhando o painel.

---

## 5. Ajustar ao seu gosto

Tudo no topo do `app.py`, no bloco **CONFIGURAÇÃO**:

- `BEATS` — suas categorias. Cada uma é uma busca. Use `OR` para alternativas, aspas para nomes (`"Nome da Pessoa"`) e `site:portal.com.br` para travar num veículo.
- `SCORE_TELEGRAM` — quão quente precisa estar pra avisar no Telegram (suba para receber menos).
- `INTERVALO_MIN` / `JANELA_HORAS` — frequência da varredura e idade máxima das notícias.
- `PALAVRAS_QUENTES` — palavras que aumentam a relevância.

Depois de mudar, salve e suba de novo no GitHub (Add file → Upload files, sobrescrevendo). O Render reimplanta sozinho.

---

## 6. Domínio próprio (depois)

No Render, em **Settings → Custom Domains**, dá pra apontar algo como `radar.palpyt.com` (precisa de acesso ao DNS do domínio da empresa).

---

### Como a relevância é calculada
Nota de 0 a 100 = recência (quanto mais recente, mais alta) + prioridade da categoria + palavras de urgência ("urgente", "morre", "oficial", "vaza", "guerra"...). O banco local lembra o que já foi enviado, então você não recebe a mesma notícia duas vezes no Telegram.
