#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Palpyt Radar - servico web + aprovacao no grupo do Telegram + aprendizado.

- Minera noticias por RSS e pontua a relevancia.
- Posta as quentes no grupo do Telegram com botoes Aprovar/Rejeitar.
- Cada voto ajusta os pesos (categorias e palavras) -> as proximas noticias
  ficam mais alinhadas ao gosto da equipe.
- Serve tambem o painel web e a API.

Rodar local:   pip install -r requirements.txt  ->  python app.py
"""

import os
import time
import html
import re
import json
import calendar
import sqlite3
import hashlib
import threading
from urllib.parse import quote_plus

import feedparser
import requests
from flask import Flask, jsonify, send_file, request

# ============================================================
#  CONFIGURACAO  (mexa so aqui)
# ============================================================
TELEGRAM_TOKEN   = os.environ.get("PALPYT_TG_TOKEN", "")   # token do @BotFather
TELEGRAM_CHAT_ID = os.environ.get("PALPYT_TG_CHAT",  "")   # id do GRUPO (negativo)
TELEGRAM_SECRET  = os.environ.get("PALPYT_TG_SECRET", "")  # opcional, protege o webhook

INTERVALO_MIN  = 5     # varredura automatica de fundo (minutos)
CACHE_MIN      = 4     # nao re-minera mais rapido que isso ao receber visitas
JANELA_HORAS   = 3     # ignora noticias mais velhas que isso
SCORE_TELEGRAM = 60    # so manda no grupo acima dessa relevancia

# aprendizado por feedback
APRENDIZADO_PASSO = 3   # quanto cada voto move o peso de uma caracteristica
APRENDIZADO_MAX   = 30  # limite do ajuste aprendido (pra nao "explodir")

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

FEEDS_DIRETOS = {
    "InfoMoney":  "https://www.infomoney.com.br/feed/",
    "CNN Brasil": "https://www.cnnbrasil.com.br/feed/",
}

DB_PATH = os.environ.get("PALPYT_DB", "palpyt_radar.db")

# ============================================================
#  BANCO
# ============================================================
_lock = threading.Lock()
_cache = {"ts": 0, "items": []}
_AJUSTES = {}   # feature -> ajuste aprendido (em memoria)


def _db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS vistos (id TEXT PRIMARY KEY, ts INTEGER)")
    con.execute("""CREATE TABLE IF NOT EXISTS noticias_enviadas
                   (chave TEXT PRIMARY KEY, titulo TEXT, beat TEXT,
                    keywords TEXT, score INTEGER, ts INTEGER)""")
    con.execute("""CREATE TABLE IF NOT EXISTS votos
                   (chave TEXT, user_id INTEGER, voto INTEGER, nome TEXT, ts INTEGER,
                    PRIMARY KEY (chave, user_id))""")
    con.execute("""CREATE TABLE IF NOT EXISTS aprendizado
                   (feature TEXT PRIMARY KEY, ajuste REAL, saldo INTEGER)""")
    return con


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
    """Caracteristicas da noticia que o aprendizado observa."""
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
    fontes = [(b["nome"], b["prioridade"], gnews_url(b["q"])) for b in BEATS]
    for nome, url in FEEDS_DIRETOS.items():
        fontes.append((nome, 8, url))

    achadas = {}
    agora = time.time()
    for nome, prioridade, url in fontes:
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
            if mins is not None and mins > JANELA_HORAS * 60:
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
    return sorted(achadas.values(), key=lambda x: x["score"], reverse=True)[:45]


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


def _teclado(chave, ap=0, rj=0):
    txa = f"Aprovar ✅ ({ap})" if ap else "Aprovar ✅"
    txr = f"Rejeitar ❌ ({rj})" if rj else "Rejeitar ❌"
    return {"inline_keyboard": [[
        {"text": txa, "callback_data": "ap|" + chave},
        {"text": txr, "callback_data": "rj|" + chave},
    ]]}


def _enviar_telegram(itens):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    for n in itens:
        con = _db()
        con.execute("INSERT OR REPLACE INTO noticias_enviadas VALUES (?,?,?,?,?,?)",
                    (n["chave"], n["titulo"], n["beat"], json.dumps(n["feats"]),
                     n["score"], int(time.time())))
        con.commit()
        con.close()
        texto = (f"{n['beat']}  (relevância {n['score']})\n\n"
                 f"{n['titulo']}\n\n{n['fonte']}\n{n['link']}")
        _tg("sendMessage", {
            "chat_id": TELEGRAM_CHAT_ID, "text": texto,
            "reply_markup": _teclado(n["chave"]),
            "disable_web_page_preview": False,
        })


def _contagem(chave):
    con = _db()
    ap = con.execute("SELECT COUNT(*) FROM votos WHERE chave=? AND voto>0", (chave,)).fetchone()[0]
    rj = con.execute("SELECT COUNT(*) FROM votos WHERE chave=? AND voto<0", (chave,)).fetchone()[0]
    con.close()
    return ap, rj


# ============================================================
#  APRENDIZADO POR FEEDBACK
# ============================================================
def _load_ajustes():
    global _AJUSTES
    con = _db()
    rows = con.execute("SELECT feature, ajuste FROM aprendizado").fetchall()
    con.close()
    _AJUSTES = {f: a for f, a in rows}


def _recompute_aprendizado():
    """Recalcula os pesos do zero a partir de todos os votos (idempotente)."""
    con = _db()
    rows = con.execute("""SELECT n.keywords, v.voto FROM votos v
                          JOIN noticias_enviadas n ON n.chave = v.chave""").fetchall()
    tally = {}
    for kw_json, voto in rows:
        try:
            feats = json.loads(kw_json)
        except Exception:
            feats = []
        for f in feats:
            tally[f] = tally.get(f, 0) + (1 if voto > 0 else -1)
    con.execute("DELETE FROM aprendizado")
    for f, saldo in tally.items():
        aj = max(-APRENDIZADO_MAX, min(APRENDIZADO_MAX, APRENDIZADO_PASSO * saldo))
        con.execute("INSERT INTO aprendizado VALUES (?,?,?)", (f, aj, saldo))
    con.commit()
    con.close()
    _load_ajustes()


# ============================================================
#  CICLO PRINCIPAL
# ============================================================
def minerar(force=False):
    with _lock:
        if not force and (time.time() - _cache["ts"]) < CACHE_MIN * 60 and _cache["items"]:
            return _cache["items"]
        itens = _coletar()
        con = _db()
        novos = []
        for n in itens:
            if n["score"] < SCORE_TELEGRAM:
                continue
            if con.execute("SELECT 1 FROM vistos WHERE id=?", (n["chave"],)).fetchone():
                continue
            con.execute("INSERT OR IGNORE INTO vistos VALUES (?,?)", (n["chave"], int(time.time())))
            novos.append(n)
        con.execute("DELETE FROM vistos WHERE ts < ?", (int(time.time()) - 4 * 86400,))
        con.commit()
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


@app.route("/")
def home():
    return send_file("index.html")


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


@app.route("/api/aprendizado")
def api_aprendizado():
    """Transparencia: o que a equipe ensinou ate agora."""
    itens = sorted(_AJUSTES.items(), key=lambda x: x[1], reverse=True)
    fmt = [{"caracteristica": f, "ajuste": round(a, 1)} for f, a in itens]
    return jsonify({"pesos": fmt})


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
            voto = 1 if acao == "ap" else -1
            con = _db()
            con.execute("INSERT OR REPLACE INTO votos VALUES (?,?,?,?,?)",
                        (chave, frm.get("id"), voto, frm.get("first_name", ""), int(time.time())))
            con.commit()
            con.close()
            _recompute_aprendizado()
            ap, rj = _contagem(chave)
            if chat_id and msg_id:
                _tg("editMessageReplyMarkup", {"chat_id": chat_id, "message_id": msg_id,
                                               "reply_markup": _teclado(chave, ap, rj)})
            _tg("answerCallbackQuery", {"callback_query_id": cq.get("id"),
                                        "text": "Voto registrado. Obrigado!"})
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
    _load_ajustes()
    if os.environ.get("PALPYT_NO_BG") != "1":
        threading.Thread(target=_loop, daemon=True).start()


_start_bg()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), use_reloader=False)
