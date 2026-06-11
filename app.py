#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Palpyt Radar - servico web (PostgreSQL / Neon).

- Minera noticias por RSS e pontua a relevancia.
- Posta as quentes no grupo do Telegram com botoes Aprovar/Rejeitar.
- 1o voto DECIDE e trava; aprovados sao encaminhados para a equipe de postagem.
- Aprendizado por feedback (cada decisao conta uma vez).
- Serve o painel, a pagina de aprovacoes e as APIs.

Persistencia em Postgres: defina a variavel de ambiente DATABASE_URL com a
string de conexao do Neon (ou outro Postgres). Assim os dados NAO se perdem
quando o Render reinicia/reimplanta.

Rodar local:  pip install -r requirements.txt
              (defina DATABASE_URL)  ->  python app.py
"""

import os
import time
import html
import re
import json
import calendar
import threading
from urllib.parse import quote_plus

import feedparser
import requests
import psycopg2
from flask import Flask, jsonify, send_file, request

try:
    from flask_cors import CORS
    _HAS_CORS = True
except Exception:
    _HAS_CORS = False

# ============================================================
#  CONFIGURACAO  (mexa so aqui)
# ============================================================
DATABASE_URL = os.environ.get("DATABASE_URL", "")          # Postgres (Neon)

TELEGRAM_TOKEN   = os.environ.get("PALPYT_TG_TOKEN", "")   # token do @BotFather
TELEGRAM_CHAT_ID = os.environ.get("PALPYT_TG_CHAT",  "")   # id do GRUPO de curadoria
TELEGRAM_SECRET  = os.environ.get("PALPYT_TG_SECRET", "")  # opcional, protege o webhook
TELEGRAM_POSTAGEM_CHAT = os.environ.get("PALPYT_TG_POSTAGEM", "")  # grupo dos aprovados

INTERVALO_MIN  = 5     # varredura automatica de fundo (minutos)
CACHE_MIN      = 4     # nao re-minera mais rapido que isso ao receber visitas
JANELA_HORAS   = 3     # idade maxima padrao das noticias
SCORE_TELEGRAM = 60    # so manda no grupo acima dessa relevancia

APRENDIZADO_PASSO = 3   # quanto cada decisao move o peso de uma caracteristica
APRENDIZADO_MAX   = 30  # limite do ajuste aprendido

BEATS = [
    {"nome": "Mercado/Economia", "prioridade": 12,
     "q": "Ibovespa OR dólar OR Selic OR juros OR inflação OR Petrobras OR bolsa when:2h"},
    {"nome": "Política BR", "prioridade": 11,
     "q": "Lula OR STF OR Congresso OR Bolsonaro OR ministro OR eleição when:2h"},
    {"nome": "Mundo/Guerra", "prioridade": 13,
     "q": "guerra OR ataque OR Trump OR Israel OR Ucrânia OR Rússia OR China when:2h"},
    {"nome": "Futebol", "prioridade": 9,
     "q": "futebol OR Flamengo OR Palmeiras OR Corinthians OR seleção OR transferência when:2h"},
    {"nome": "Celebridades/Fofoca", "prioridade": 8,
     "q": "Virginia OR Anitta OR famosos OR affair OR polêmica OR término OR Neymar when:2h"},
    {"nome": "Brasil Geral", "prioridade": 7,
     "q": "site:g1.globo.com OR site:cnnbrasil.com.br when:1h"},
    {"nome": "Leo Dias", "prioridade": 10, "janela": 48,
     "q": "site:portalleodias.com when:24h"},
]

PALAVRAS_QUENTES = {
    "urgente": 28, "agora": 12, "exclusivo": 20, "bomba": 18,
    "morre": 22, "morto": 22, "morta": 22, "morreu": 22, "mortos": 22,
    "oficial": 16, "oficializa": 16, "confirma": 14, "confirmado": 14,
    "anuncia": 12, "anúncio": 12, "vaza": 16, "vazou": 16, "vazamento": 16,
    "ataque": 18, "guerra": 18, "explosão": 18, "bombardeio": 18,
    "renúncia": 18, "renuncia": 18, "preso": 16, "presa": 16, "prisão": 16,
    "demitido": 14, "afastado": 12, "recorde": 12, "histórico": 10,
    "polêmica": 10, "escândalo": 16, "denúncia": 12, "dispara": 12,
    "despenca": 14, "surpresa": 10, "inédito": 10, "liberado": 8, "vence": 8,
}
VOCAB = set(PALAVRAS_QUENTES.keys())

FEEDS_DIRETOS = [
    {"nome": "InfoMoney",  "url": "https://www.infomoney.com.br/feed/"},
    {"nome": "CNN Brasil", "url": "https://www.cnnbrasil.com.br/feed/"},
    {"nome": "Leo Dias",   "url": "https://portalleodias.com/feed/", "prioridade": 10, "janela": 48},
]

# Choquei via Instagram->RSS (ex.: RSS.app). Cole a URL na variavel PALPYT_CHOQUEI_FEED.
_choquei_feed = os.environ.get("PALPYT_CHOQUEI_FEED", "")
if _choquei_feed:
    FEEDS_DIRETOS.append({"nome": "Choquei", "url": _choquei_feed, "prioridade": 10, "janela": 48})

# ============================================================
#  BANCO (PostgreSQL)
# ============================================================
_lock = threading.Lock()
_cache = {"ts": 0, "items": []}
_AJUSTES = {}


def _conn():
    con = psycopg2.connect(DATABASE_URL)
    con.autocommit = True
    return con


def db_exec(sql, params=()):
    con = _conn()
    try:
        con.cursor().execute(sql, params)
    finally:
        con.close()


def db_one(sql, params=()):
    con = _conn()
    try:
        cur = con.cursor()
        cur.execute(sql, params)
        return cur.fetchone()
    finally:
        con.close()


def db_all(sql, params=()):
    con = _conn()
    try:
        cur = con.cursor()
        cur.execute(sql, params)
        return cur.fetchall()
    finally:
        con.close()


def init_db():
    con = _conn()
    try:
        cur = con.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS vistos (id TEXT PRIMARY KEY, ts BIGINT)")
        cur.execute("""CREATE TABLE IF NOT EXISTS noticias_enviadas
                       (chave TEXT PRIMARY KEY, titulo TEXT, beat TEXT, keywords TEXT,
                        score INTEGER, ts BIGINT, link TEXT,
                        decisao TEXT, decidido_por TEXT, decidido_em BIGINT)""")
        cur.execute("""CREATE TABLE IF NOT EXISTS votos
                       (chave TEXT, user_id TEXT, voto INTEGER, nome TEXT, ts BIGINT,
                        PRIMARY KEY (chave, user_id))""")
        cur.execute("""CREATE TABLE IF NOT EXISTS aprendizado
                       (feature TEXT PRIMARY KEY, ajuste DOUBLE PRECISION, saldo INTEGER)""")
    finally:
        con.close()


# ============================================================
#  MOTOR DE MINERACAO
# ============================================================
def gnews_url(query):
    if "when:" not in query:
        query += " when:2h"
    return f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=pt-BR&gl=BR&ceid=BR:pt-419"


def _norm(t):
    t = (t or "").lower()
    t = re.sub(r"[^a-z0-9á-úãõâêôç ]", "", t)
    return re.sub(r"\s+", " ", t).strip()


def _chave(titulo):
    import hashlib
    return hashlib.md5(_norm(titulo).encode("utf-8")).hexdigest()


def _epoch(entry):
    t = getattr(entry, "published_parsed", None) or getattr(entry, "updated_parsed", None)
    return calendar.timegm(t) if t else None


def _fonte(entry, fallback):
    src = getattr(entry, "source", None)
    if src and getattr(src, "title", None):
        return src.title
    return fallback


def _features(titulo, beat):
    t = titulo.lower()
    feats = ["beat:" + beat]
    for w in VOCAB:
        if w in t:
            feats.append("kw:" + w)
    return feats


def _calor_base(titulo, minutos, prioridade):
    score = 30 + prioridade
    if minutos is None:
        score += 5
    elif minutos <= 15:
        score += 35
    elif minutos <= 30:
        score += 28
    elif minutos <= 60:
        score += 20
    elif minutos <= 120:
        score += 10
    elif minutos <= 180:
        score += 3
    t = titulo.lower()
    score += min(sum(p for w, p in PALAVRAS_QUENTES.items() if w in t), 30)
    return score


def _coletar():
    fontes = [(b["nome"], b["prioridade"], gnews_url(b["q"]), b.get("janela", JANELA_HORAS)) for b in BEATS]
    for f in FEEDS_DIRETOS:
        fontes.append((f["nome"], f.get("prioridade", 8), f["url"], f.get("janela", JANELA_HORAS)))

    achadas = {}
    agora = time.time()
    for nome, prioridade, url, janela in fontes:
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            print(f"[aviso] falha em {nome}: {e}")
            continue
        for entry in feed.entries[:25]:
            titulo = html.unescape(getattr(entry, "title", "")).strip()
            link = getattr(entry, "link", "")
            if not titulo or not link:
                continue
            ep = _epoch(entry)
            mins = (agora - ep) / 60.0 if ep else None
            if mins is not None and mins > janela * 60:
                continue
            feats = _features(titulo, nome)
            base = _calor_base(titulo, mins, prioridade)
            aprendido = sum(_AJUSTES.get(f, 0) for f in feats)
            score = max(0, min(100, int(base + aprendido)))
            chave = _chave(titulo)
            atual = achadas.get(chave)
            if atual and atual["score"] >= score:
                continue
            achadas[chave] = {
                "titulo": titulo, "link": link, "fonte": _fonte(entry, nome),
                "beat": nome, "epoch": ep or int(agora), "score": score,
                "chave": chave, "feats": feats,
            }
    return sorted(achadas.values(), key=lambda x: x["score"], reverse=True)[:80]


# ============================================================
#  TELEGRAM
# ============================================================
def _tg(method, payload):
    if not TELEGRAM_TOKEN:
        return {}
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
                          json=payload, timeout=15)
        return r.json()
    except Exception as e:
        print(f"[aviso] telegram {method}: {e}")
        return {}


def _teclado(chave):
    return {"inline_keyboard": [[
        {"text": "Aprovar ✅", "callback_data": "ap|" + chave},
        {"text": "Rejeitar ❌", "callback_data": "rj|" + chave},
    ]]}


def _enviar_telegram(itens):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    for n in itens:
        db_exec("""INSERT INTO noticias_enviadas (chave,titulo,beat,keywords,score,ts,link)
                   VALUES (%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (chave) DO UPDATE SET titulo=EXCLUDED.titulo, beat=EXCLUDED.beat,
                     keywords=EXCLUDED.keywords, score=EXCLUDED.score, ts=EXCLUDED.ts, link=EXCLUDED.link""",
                (n["chave"], n["titulo"], n["beat"], json.dumps(n["feats"]),
                 n["score"], int(time.time()), n.get("link", "")))
        texto = (f"{n['beat']}  (relevância {n['score']})\n\n"
                 f"{n['titulo']}\n\n{n['fonte']}\n{n['link']}")
        _tg("sendMessage", {"chat_id": TELEGRAM_CHAT_ID, "text": texto,
                            "reply_markup": _teclado(n["chave"]),
                            "disable_web_page_preview": False})


def _encaminhar_postagem(n, quem):
    if not (TELEGRAM_TOKEN and TELEGRAM_POSTAGEM_CHAT):
        return
    texto = (f"✅ APROVADA para postagem\n\n{n['titulo']}\n\n"
             f"{n['beat']} · relevância {n['score']}\n{n.get('link') or ''}\n\n"
             f"(aprovada por {quem})")
    _tg("sendMessage", {"chat_id": TELEGRAM_POSTAGEM_CHAT, "text": texto,
                        "disable_web_page_preview": False})


# ============================================================
#  APRENDIZADO + DECISAO
# ============================================================
def _load_ajustes():
    global _AJUSTES
    try:
        rows = db_all("SELECT feature, ajuste FROM aprendizado")
        _AJUSTES = {f: a for f, a in rows}
    except Exception as e:
        print(f"[aviso] load_ajustes: {e}")


def _recompute_aprendizado():
    con = _conn()
    try:
        cur = con.cursor()
        cur.execute("SELECT keywords, decisao FROM noticias_enviadas WHERE decisao IS NOT NULL")
        rows = cur.fetchall()
        tally = {}
        for kw_json, decisao in rows:
            try:
                feats = json.loads(kw_json) if kw_json else []
            except Exception:
                feats = []
            s = 1 if decisao == "aprovada" else -1
            for f in feats:
                tally[f] = tally.get(f, 0) + s
        cur.execute("DELETE FROM aprendizado")
        for f, saldo in tally.items():
            aj = max(-APRENDIZADO_MAX, min(APRENDIZADO_MAX, APRENDIZADO_PASSO * saldo))
            cur.execute("INSERT INTO aprendizado (feature,ajuste,saldo) VALUES (%s,%s,%s)", (f, aj, saldo))
    finally:
        con.close()
    _load_ajustes()


def _registrar_decisao(chave, aprovar, quem):
    """O PRIMEIRO voto decide e trava. Votos posteriores sao ignorados."""
    con = _conn()
    try:
        cur = con.cursor()
        cur.execute("""SELECT decisao, decidido_por, titulo, beat, link, score
                       FROM noticias_enviadas WHERE chave=%s""", (chave,))
        row = cur.fetchone()
        if not row:
            return {"status": "desconhecida", "ja_decidida": False, "novo": False}
        decisao, por, titulo, beat, link, score = row
        if decisao:
            return {"status": decisao, "decidido_por": por, "ja_decidida": True, "novo": False}
        nova = "aprovada" if aprovar else "rejeitada"
        cur.execute("UPDATE noticias_enviadas SET decisao=%s, decidido_por=%s, decidido_em=%s WHERE chave=%s",
                    (nova, quem, int(time.time()), chave))
        cur.execute("""INSERT INTO votos (chave,user_id,voto,nome,ts) VALUES (%s,%s,%s,%s,%s)
                       ON CONFLICT (chave,user_id) DO UPDATE SET voto=EXCLUDED.voto,
                         nome=EXCLUDED.nome, ts=EXCLUDED.ts""",
                    (chave, str(quem), 1 if aprovar else -1, str(quem), int(time.time())))
    finally:
        con.close()
    _recompute_aprendizado()
    if nova == "aprovada":
        _encaminhar_postagem({"titulo": titulo, "beat": beat, "link": link, "score": score}, quem)
    return {"status": nova, "decidido_por": quem, "ja_decidida": False, "novo": True}


# ============================================================
#  CICLO PRINCIPAL
# ============================================================
def minerar(force=False):
    with _lock:
        if not force and (time.time() - _cache["ts"]) < CACHE_MIN * 60 and _cache["items"]:
            return _cache["items"]
        itens = _coletar()
        novos = []
        agora = int(time.time())
        con = _conn()
        try:
            cur = con.cursor()
            for n in itens:
                if n["score"] < SCORE_TELEGRAM:
                    continue
                cur.execute("SELECT 1 FROM vistos WHERE id=%s", (n["chave"],))
                if cur.fetchone():
                    continue
                cur.execute("INSERT INTO vistos (id, ts) VALUES (%s,%s) ON CONFLICT (id) DO NOTHING",
                            (n["chave"], agora))
                novos.append(n)
            cur.execute("DELETE FROM vistos WHERE ts < %s", (agora - 4 * 86400,))
        finally:
            con.close()
        _cache["items"] = itens
        _cache["ts"] = time.time()
    if novos:
        _enviar_telegram(novos)
    return itens


# ============================================================
#  SERVIDOR WEB
# ============================================================
app = Flask(__name__)
if _HAS_CORS:
    CORS(app)


@app.route("/")
def home():
    return send_file("index.html")


@app.route("/historico")
def historico():
    return send_file("historico.html")


@app.route("/api/noticias")
def api_noticias():
    itens = minerar(force=request.args.get("force") == "1")
    agora = time.time()
    saida = [{
        "titulo": n["titulo"], "link": n["link"], "fonte": n["fonte"],
        "beat": n["beat"], "score": n["score"],
        "min": int(max(0, (agora - n["epoch"]) / 60)),
    } for n in itens]
    return jsonify({"atualizado": int(agora), "total": len(saida), "itens": saida})


@app.route("/api/historico")
def api_historico():
    rows = db_all("""SELECT chave, titulo, beat, score, ts, link, decisao, decidido_por, decidido_em
                     FROM noticias_enviadas ORDER BY ts DESC LIMIT 300""")
    out = [{
        "chave": r[0], "titulo": r[1], "beat": r[2], "score": r[3], "ts": r[4],
        "link": r[5] or "", "status": r[6] or "pendente",
        "decidido_por": r[7] or "", "decidido_em": r[8] or 0,
    } for r in rows]
    return jsonify({"total": len(out), "itens": out})


@app.route("/api/aprendizado")
def api_aprendizado():
    itens = sorted(_AJUSTES.items(), key=lambda x: x[1], reverse=True)
    return jsonify({"pesos": [{"caracteristica": f, "ajuste": round(a, 1)} for f, a in itens]})


@app.route("/api/voto", methods=["POST"])
def api_voto():
    body = request.get_json(force=True, silent=True) or {}
    chave = body.get("chave")
    if not chave:
        return jsonify({"erro": "campo 'chave' é obrigatório"}), 400
    aprovar = str(body.get("voto", "")).lower() in ("1", "ap", "aprovar", "aprovado", "sim", "true")
    usuario = str(body.get("usuario") or "web")
    res = _registrar_decisao(chave, aprovar, usuario)
    return jsonify({"ok": True, "chave": chave, "status": res["status"],
                    "decidido_por": res.get("decidido_por", ""), "ja_decidida": res["ja_decidida"]})


@app.route("/telegram/setup")
def tg_setup():
    if not TELEGRAM_TOKEN:
        return "Configure PALPYT_TG_TOKEN primeiro.", 400
    host = request.headers.get("X-Forwarded-Host") or request.host
    payload = {"url": f"https://{host}/telegram/webhook",
               "allowed_updates": ["callback_query", "message"]}
    if TELEGRAM_SECRET:
        payload["secret_token"] = TELEGRAM_SECRET
    return jsonify(_tg("setWebhook", payload))


@app.route("/telegram/webhook", methods=["POST"])
def tg_webhook():
    if TELEGRAM_SECRET and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != TELEGRAM_SECRET:
        return "forbidden", 403
    upd = request.get_json(force=True, silent=True) or {}
    cq = upd.get("callback_query")
    if cq:
        data = cq.get("data", "")
        msg = cq.get("message", {}) or {}
        chat_id = (msg.get("chat") or {}).get("id")
        msg_id = msg.get("message_id")
        frm = cq.get("from", {}) or {}
        if "|" in data:
            acao, chave = data.split("|", 1)
            quem = frm.get("first_name") or str(frm.get("id"))
            res = _registrar_decisao(chave, acao == "ap", quem)
            if res["ja_decidida"]:
                _tg("answerCallbackQuery", {"callback_query_id": cq.get("id"),
                    "text": f"Já foi {res['status']} por {res.get('decidido_por','alguém')}. Voto ignorado."})
            elif res["novo"]:
                rotulo = "✅ APROVADA" if res["status"] == "aprovada" else "❌ REJEITADA"
                extra = " — encaminhada para postagem" if res["status"] == "aprovada" else ""
                if chat_id and msg_id:
                    novo_texto = (msg.get("text", "") + f"\n\n— {rotulo} por {quem}{extra}")
                    _tg("editMessageText", {"chat_id": chat_id, "message_id": msg_id,
                                            "text": novo_texto, "reply_markup": {"inline_keyboard": []},
                                            "disable_web_page_preview": True})
                _tg("answerCallbackQuery", {"callback_query_id": cq.get("id"),
                                            "text": "Decisão registrada. Obrigado!"})
            else:
                _tg("answerCallbackQuery", {"callback_query_id": cq.get("id"),
                                            "text": "Notícia não encontrada."})
    return jsonify({"ok": True})


# ============================================================
#  VARREDURA DE FUNDO + START
# ============================================================
def _loop():
    while True:
        try:
            minerar(force=True)
        except Exception as e:
            print(f"[aviso] loop: {e}")
        time.sleep(INTERVALO_MIN * 60)


def _start_bg():
    if not DATABASE_URL:
        print("[ERRO] DATABASE_URL nao definida. Configure o Postgres (Neon) para persistir os dados.")
        return
    try:
        init_db()
    except Exception as e:
        print(f"[aviso] init_db: {e}")
    _load_ajustes()
    if os.environ.get("PALPYT_NO_BG") != "1":
        threading.Thread(target=_loop, daemon=True).start()


_start_bg()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), use_reloader=False)
