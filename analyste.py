"""Analyse IA d'un titre : chiffres + motif + news -> resume par le LLM.

Principe (RAG "a la main") : le modele ne cherche rien lui-meme. On lui
donne TOUT ce qu'il doit savoir (variations datees, motif detecte, drapeaux
de fiabilite, titres d'articles dates) et on lui demande de relier les
mouvements aux news. Il ne peut donc pas inventer de chiffres : ils viennent
de nous, et le prompt lui interdit d'en ajouter.

Config dans .env : VLLM_BASE_URL, VLLM_API_KEY, MODEL_NAME (API compatible
OpenAI : marche avec vLLM, Ollama, ou n'importe quel fournisseur compatible).
"""

from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime

import requests
from dotenv import load_dotenv

import motifs
import news
import picks
import tweets
from tr_data import BASE, historiques, tickers

load_dotenv(BASE / ".env")

CACHE: dict[str, tuple[float, dict]] = {}  # isin -> (instant, resultat)
CACHE_S = 3600

SYSTEME = """Tu es un analyste boursier prudent qui aide un particulier à comprendre les mouvements d'une action.
Règles strictes :
- Réponds en français, de façon concise (220 mots maximum).
- Utilise UNIQUEMENT les données fournies. N'invente aucun chiffre, aucune news, aucune date.
- Quand tu relies un mouvement à une news, cite la date et la source. Si aucune news n'explique un mouvement, dis-le clairement.
- Les tweets (X) sont des signaux d'attention, pas des faits : cite le compte (@...) et ne les confonds pas avec des news.
- Les prix viennent de cotations Lang & Schwarz, parfois peu fiables sur les petites valeurs : tiens compte des drapeaux fournis.
- Ne donne aucun conseil d'achat ou de vente.
Format (titres en gras, puces courtes) :
**En bref** — 1 à 2 phrases.
**Ce qui explique les mouvements** — puces « date : mouvement → news (source) ».
**Motif chute → rebond** — le motif est-il crédible ? combien d'occurrences ? échantillon suffisant ?
**Points de vigilance** — spread, liquidité, fiabilité des cotations, taille de l'échantillon."""


def _llm(messages: list[dict], max_tokens: int = 900) -> str:
    base = os.environ.get("VLLM_BASE_URL", "").rstrip("/")
    if not base:
        raise RuntimeError("VLLM_BASE_URL absent du fichier .env")
    r = requests.post(
        base + "/chat/completions",
        headers={"Authorization": f"Bearer {os.environ.get('VLLM_API_KEY', '')}"},
        json={"model": os.environ.get("MODEL_NAME", "gemma4-26b"), "messages": messages,
              "temperature": 0.2, "max_tokens": max_tokens},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()


def _contexte(nom: str, isin: str, t: dict, jours: list[dict], m: dict, flags: list[str], articles: list[dict],
              posts: list[dict]) -> str:
    bid, ask = picks._prix(t, "bid"), picks._prix(t, "ask")
    lignes = [f"Titre : {nom} (ISIN {isin})", f"Aujourd'hui : {datetime.now(picks.PARIS):%A %d/%m/%Y %H:%M}"]
    if bid and ask:
        lignes.append(f"Cotation actuelle : bid {bid} / ask {ask} EUR, spread {(ask - bid) / bid * 100:.1f} %")
    if flags:
        lignes.append("Drapeaux de fiabilité : " + " ; ".join(flags))

    lignes.append("\nSéances (prix de séance 9h-17h30, EUR) : date | clôture | variation vs veille | amplitude du jour")
    for i, j in enumerate(jours):
        var = f"{(j['cloture'] - jours[i - 1]['cloture']) / jours[i - 1]['cloture'] * 100:+.1f} %" if i else "—"
        amp = (j["haut"] - j["bas"]) / j["bas"] * 100 if j["bas"] else 0
        lignes.append(f"{j['date']} | {j['cloture']} | {var} | {amp:.1f} %")

    lignes.append(f"\nMotif chute ≥ {motifs.CHUTE:.0f} % puis rebond ≥ {motifs.REBOND:.0f} % le lendemain (calculé, pas à recalculer) :")
    lignes.append(f"{m['rebonds']} rebond(s) sur {m['chutes']} forte(s) chute(s) terminée(s)"
                  + (f", taux {m['taux']} %, rebond moyen {m['rebond_moyen']} %" if m["taux"] is not None else ""))
    for o in m["occurrences"]:
        suite = "lendemain pas encore connu" if o["rebond"] is None else f"lendemain {o['rebond']:+.1f} %"
        lignes.append(f"- {o['date']} : {o['chute']:+.1f} % → {suite}")

    lignes.append(f"\nNews des 30 derniers jours ({len(articles)}) :")
    for a in articles:
        lignes.append(f"- {a['date'][:10]} | {a['source']} | {a['titre']}")
    if not articles:
        lignes.append("(aucune news trouvée)")

    lignes.append(f"\nTweets récents sur X ({len(posts)}) : date | compte | likes | texte")
    for p in posts:
        lignes.append(f"- {p['date'][:10]} | @{p['compte']} | {p['likes']} likes | {' '.join(p['texte'].split())[:280]}")
    if not posts:
        lignes.append("(aucun tweet trouvé)")
    return "\n".join(lignes)


async def analyser(isin: str, nom: str, force: bool = False) -> dict:
    if not force and isin in CACHE and time.time() - CACHE[isin][0] < CACHE_S:
        return {**CACHE[isin][1], "cache": True}

    # TR (async), Google News et X (bloquants -> threads) en parallele
    hist, live, articles, posts = await asyncio.gather(
        historiques([isin], range="1m", timeout=10),
        tickers([isin], timeout=8),
        asyncio.to_thread(news.chercher, nom),
        asyncio.to_thread(tweets.chercher, nom, isin),
    )
    jours = picks.jours_depuis_bougies((hist.get(isin) or {}).get("aggregates", []))
    m = motifs.chute_rebond(jours)
    t = live.get(isin) or {}

    pick = next((l for l in picks.charger_dernier()["lignes"] if l["isin"] == isin), None)
    mvt = next((l for l in picks.charger_mouvements()["lignes"] if l["isin"] == isin), None)
    flags = []
    if pick and pick.get("mecanique"):
        flags.append("pics mécaniques (même horaire chaque jour : artefact de cotation probable)")
    if isin[:2] in picks.HORS_FUSEAU:
        flags.append("bourse d'origine fermée pendant la séance européenne")
    if mvt and mvt.get("trou_jours", 0) > 4:
        flags.append(f"historique interrompu depuis {mvt['trou_jours']} jours")

    resume, erreur = None, None
    try:
        resume = await asyncio.to_thread(_llm, [
            {"role": "system", "content": SYSTEME},
            {"role": "user", "content": _contexte(nom, isin, t, jours, m, flags, articles, posts)},
        ])
    except Exception as exc:  # le reste (news + motif) reste utile sans le LLM
        erreur = f"{type(exc).__name__}: {exc}"[:300]

    resultat = {
        "isin": isin, "heure": datetime.now(picks.PARIS).isoformat(timespec="seconds"),
        "modele": os.environ.get("MODEL_NAME"), "recherche": news.nom_de_recherche(nom),
        "resume": resume, "erreur": erreur, "motif": m, "news": articles, "tweets": posts,
    }
    if resume:
        CACHE[isin] = (time.time(), resultat)
    return {**resultat, "cache": False}
