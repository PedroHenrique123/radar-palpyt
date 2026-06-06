#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Palpyt Radar - servico web
Minera noticias por RSS, calcula relevancia, serve o painel e a API,
e (opcional) empurra as quentes pro Telegram.

Rodar local:   pip install -r requirements.txt  ->  python app.py
Abrir:         http://localhost:5000
"""

import os
import time
import html
import re
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
TELEGRAM_TOKEN   = os.environ.get("PALPYT_TG_TOKEN", "")   # opcional
TELEGRAM_CHAT_ID = os.environ.get("PALPYT_TG_CHAT",  "")   # opcional

INTERVALO_MIN = 5      # varredura automatica de fundo (minutos)
CACHE_MIN     = 4      # nao re-minera mais rapido que isso ao receber visitas
JANELA_HORAS  = 3      # ignora noticias mais velhas que isso
SCORE_TELEGRAM = 60    # so manda no Telegram acima desse calor

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
    "despenca": 14, "surpresa": 10, "inédito": 10,
}

FEEDS_DIRETOS = {
    "InfoMoney":  "https://www.infomoney.com.br/feed/",
    "CNN Brasil": "https://www.cnnbrasil.com.br/feed/",
}

DB_PATH = os.environ.get("PALPYT_DB", "palpyt_radar.db")

# ============================================================
#  MOTOR
# ============================================================
_lock = threading.Lock()
_cache = {"ts": 0, "items": []}


def _db():
    con = sqlite3.connect(DB_PATH)
    con.execute("CREATE TABLE IF NOT EXISTS vistos (id TEXT PRIMARY KEY, ts INTEGER)")
    return con


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


def _calor(titulo, minutos, prioridade):
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
    return max(0, min(100, score))


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
            score = _calor(titulo, mins, prioridade)
            chave = _chave(titulo)
            atual = achadas.get(chave)
            if atual and atual["score"] >= score:
                continue
            achadas[chave] = {
                "titulo": titulo, "link": link, "fonte": _fonte(entry, nome),
                "beat": nome, "epoch": ep or int(agora), "score": score, "chave": chave,
            }
    itens = sorted(achadas.values(), key=lambda x: x["score"], reverse=True)
    return itens[:45]


def _enviar_telegram(itens_novos):
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    for n in itens_novos:
        texto = (f"🔥 {n['beat']} (relevância {n['score']})\n\n"
                 f"{n['titulo']}\n\n{n['fonte']}\n{n['link']}")
        try:
            requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": texto},
                timeout=15)
        except Exception as e:
            print(f"[aviso] telegram: {e}")


def minerar(force=False):
    with _lock:
        if not force and (time.time() - _cache["ts"]) < CACHE_MIN * 60 and _cache["items"]:
            return _cache["items"]
        itens = _coletar()
        # detecta novidades quentes para o Telegram
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
    force = request.args.get("force") == "1"
    itens = minerar(force=force)
    agora = time.time()
    saida = [{
        "titulo": n["titulo"], "link": n["link"], "fonte": n["fonte"],
        "beat": n["beat"], "score": n["score"],
        "min": int(max(0, (agora - n["epoch"]) / 60)),
    } for n in itens]
    return jsonify({"atualizado": int(agora), "total": len(saida), "itens": saida})


# varredura de fundo (mantem Telegram funcionando mesmo sem ninguem no painel)
def _loop():
    while True:
        try:
            minerar(force=True)
        except Exception as e:
            print(f"[aviso] loop: {e}")
        time.sleep(INTERVALO_MIN * 60)


def _start_bg():
    if os.environ.get("PALPYT_NO_BG") == "1":
        return
    threading.Thread(target=_loop, daemon=True).start()


_start_bg()

if __name__ == "__main__":
    porta = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=porta, use_reloader=False)
