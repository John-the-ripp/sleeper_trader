"""LSX vs marche d'origine : "pourquoi Anoto chute sur LSX ?"

Constat du 24/09 (hypothese de l'utilisateur, verifiee) : les "chutes" d'Anoto
sur LSX (-43 % le 03/09, -27 % le 14/09, -31 % le 18/09) n'existaient PAS a
Stockholm (INQ.ST, meme titre renomme) : c'est le BID de Lang & Schwarz qui
tombait 35 a 51 % sous le vrai prix, puis le rattrapait en 1 a 5 seances.
Les bougies LSX de TR suivent le bid (verifie : derniere bougie = bid).

Ce module compare, pour un titre :
  - le prix du marche d'origine (yfinance, converti en EUR) ;
  - le bid/ask LSX ;
et en deduit l'ecart actuel, l'historique des decrochages et leur rattrapage.

Nuance importante : TR ne donne pas l'historique de l'ASK. Un bid qui decroche
= L&S elargit son spread vers le bas : utile pour NE PAS VENDRE au mauvais
moment, mais ce n'est une occasion d'ACHAT que si l'ask decroche aussi.
Pour le verifier dans le temps, `journaliser` enregistre bid/ask a chaque tour.
"""

from __future__ import annotations

import json
import time
from datetime import datetime

import yfinance as yf

import picks
from simulateur import charger_series
from tr_data import BASE

FICHIER_MAP = BASE / "yahoo_map.json"
FICHIER_JOURNAL = BASE / "cotes_journal.jsonl"
DECROCHE = -20.0   # bid LSX au moins 20 % sous le vrai prix = "decrochage"
RATTRAPE = -15.0   # rattrape quand l'ecart remonte au-dessus de -15 % : meme aligne, le bid reste
                   # sous le vrai prix d'un demi-spread (et notre "cloture" LSX est le prix de 14h)
DELAI = 7          # en 7 seances max (Friwo : decrochage du 24/08, realigne le 02/09)
_CACHE: dict[str, tuple[float, dict]] = {}
_FX: dict[str, tuple[float, dict]] = {}


def _hist(ticker, **kw):
    """history() sans les lignes vides : apres minuit, Yahoo renvoie deja une ligne
    pour le nouveau jour avec un prix NaN (constat du 25/09 a 00h26), qui cassait le JSON."""
    h = ticker.history(**kw)
    return h[h["Close"].notna() & (h["Close"] > 0)] if not h.empty else h


def _map() -> dict:
    try:
        return json.loads(FICHIER_MAP.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def symbole(isin: str) -> str | None:
    """ISIN -> symbole Yahoo (INQ.ST pour Anoto). Garde en cache sur disque."""
    m = _map()
    if isin in m:
        return m[isin]
    try:
        quotes = yf.Search(isin, max_results=5).quotes
    except Exception:
        quotes = []
    sym = next((q["symbol"] for q in quotes if q.get("quoteType") in (None, "EQUITY")), None) or (quotes[0]["symbol"] if quotes else None)
    m[isin] = sym
    FICHIER_MAP.write_text(json.dumps(m, indent=1), encoding="utf-8")
    return sym


def _taux_eur(devise: str) -> dict:
    """{date: taux} pour convertir la devise en EUR (1 si deja EUR). GBp = pence."""
    if devise in ("EUR", None):
        return {}
    base, div = ("GBP", 100) if devise == "GBp" else (devise, 1)
    if base in _FX and time.time() - _FX[base][0] < 3600:
        taux = _FX[base][1]
    else:
        h = _hist(yf.Ticker(f"{base}EUR=X"), period="2mo", interval="1d")
        taux = {d.date().isoformat(): float(v) for d, v in h["Close"].items()}
        _FX[base] = (time.time(), taux)
    return {d: v / div for d, v in taux.items()}


def _vers_eur(prix: float, jour: str, taux: dict) -> float:
    if not taux:
        return prix
    cle = max((d for d in taux if d <= jour), default=min(taux))
    return prix * taux[cle]


def comparer(isin: str) -> dict:
    """Ecart LSX / origine jour par jour + maintenant + historique des rattrapages."""
    if isin in _CACHE and time.time() - _CACHE[isin][0] < 1800:
        return _CACHE[isin][1]
    sym = symbole(isin)
    s = charger_series()["titres"].get(isin)
    if not sym or not s:
        return {"isin": isin, "symbole": sym, "dispo": False,
                "raison": "titre introuvable sur Yahoo" if not sym else "pas de séances LSX exploitables"}
    t = yf.Ticker(sym)
    h = _hist(t, period="1mo", interval="1d")
    if h.empty:
        return {"isin": isin, "symbole": sym, "dispo": False, "raison": "pas d'historique Yahoo"}
    devise = (t.fast_info or {}).get("currency")
    taux = _taux_eur(devise)
    origine = {d.date().isoformat(): _vers_eur(float(c), d.date().isoformat(), taux) for d, c in h["Close"].items()}

    jours = []
    for d, _bas, _haut, clot in s["j"]:
        if d in origine and origine[d] > 0:
            jours.append({"date": d, "lsx": clot, "origine": round(origine[d], 6),
                          "ecart": round((clot / origine[d] - 1) * 100, 1)})

    # historique : EPISODES de decrochage (jours consecutifs a <= -20 %) et leur rattrapage.
    # (l'ancienne version sautait 6 seances apres un episode et ratait les suivants)
    evts, i = [], 0
    while i < len(jours):
        if jours[i]["ecart"] > DECROCHE:
            i += 1
            continue
        debut = i
        suite = jours[debut + 1:debut + 1 + DELAI]
        k = next((n for n, j in enumerate(suite) if j["ecart"] >= RATTRAPE), None)
        pire = min(j["ecart"] for j in jours[debut:debut + 1 + (k if k is not None else len(suite))])
        evts.append({"date": jours[debut]["date"], "ecart": jours[debut]["ecart"], "pire": pire, "lsx": jours[debut]["lsx"],
                     "rattrape": None if (k is None and len(suite) < DELAI) else k is not None,
                     "delai": k + 1 if k is not None else None,
                     "gain_bid": round((suite[k]["lsx"] / jours[debut]["lsx"] - 1) * 100, 1) if k is not None else None})
        # on reprend apres le rattrapage (ou apres l'episode s'il n'est pas rattrape)
        if k is not None:
            i = debut + 1 + k + 1
        else:
            i = debut + 1
            while i < len(jours) and jours[i]["ecart"] <= DECROCHE:
                i += 1
    termines = [e for e in evts if e["rattrape"] is not None]
    ok = [e for e in termines if e["rattrape"]]

    dernier_origine = h["Close"].iloc[-1]
    ref = _vers_eur(float(dernier_origine), h.index[-1].date().isoformat(), taux)
    res = {
        "isin": isin, "symbole": sym, "devise": devise, "dispo": True,
        "origine_eur": round(ref, 6), "origine_date": h.index[-1].date().isoformat(),
        "bid": s["bid"], "ask": s["ask"],
        "ecart_bid": round((s["bid"] / ref - 1) * 100, 1), "ecart_ask": round((s["ask"] / ref - 1) * 100, 1),
        "jours": jours, "evenements": evts,
        "decrochages": len(termines), "rattrapes": len(ok),
        "delai_moy": round(sum(e["delai"] for e in ok) / len(ok), 1) if ok else None,
        "gain_moy": round(sum(e["gain_bid"] for e in ok) / len(ok), 1) if ok else None,
    }
    res["verdict"] = _verdict(res)
    _CACHE[isin] = (time.time(), res)
    return res


def _verdict(r: dict) -> dict:
    b, a = r["ecart_bid"], r["ecart_ask"]
    hist = (f"Ce mois-ci, le bid LSX a décroché {r['decrochages']} fois sous le vrai prix et l'a rattrapé "
            f"{r['rattrapes']} fois" + (f", en {r['delai_moy']:g} séance(s) en moyenne (bid +{r['gain_moy']:g} % au rattrapage)."
                                        if r["rattrapes"] else "."))
    if b <= DECROCHE and a <= RATTRAPE:
        return {"niveau": "achat", "titre": "LSX sous le vrai prix, ask compris",
                "texte": f"Le bid est {b:+.0f} % et l'ask {a:+.0f} % par rapport au marché d'origine : acheter sur LSX coûte moins cher "
                         f"que le vrai prix. {hist} Attention : un prix trop éloigné peut être annulé par TR (« erreur de cotation », cas EcoRub)."}
    if b <= DECROCHE:
        return {"niveau": "ne_pas_vendre", "titre": "C'est LSX qui décroche, pas le titre",
                "texte": f"Le bid LSX est {b:+.0f} % sous le vrai prix, mais l'ask ({a:+.0f} %) ne l'est pas : L&S élargit son spread vers le bas. "
                         f"Vendre maintenant = vendre trop bas. {hist}"}
    if b >= 15:
        return {"niveau": "surcote", "titre": "LSX au-dessus du vrai prix",
                "texte": f"Le bid LSX est {b:+.0f} % au-dessus du marché d'origine : risque de rechute sur LSX. {hist}"}
    return {"niveau": "aligne", "titre": "LSX aligné sur le marché d'origine",
            "texte": f"Écart bid {b:+.0f} % / ask {a:+.0f} % : normal. {hist}"}


def resume(isin: str) -> dict | None:
    """Version courte pour les cartes, lue dans le CACHE uniquement (rempli en arriere-plan
    apres chaque tour par `prechauffer`) : la vue Ranges ne doit pas attendre Yahoo (16 s)."""
    r = _CACHE.get(isin, (0, None))[1]
    if r is None:
        return None
    if not r.get("dispo"):
        return None
    return {k: r[k] for k in ("symbole", "origine_eur", "ecart_bid", "ecart_ask", "decrochages", "rattrapes", "delai_moy", "gain_moy")} | {
        "niveau": r["verdict"]["niveau"]}


def journaliser(lignes: list[dict]) -> None:
    """Enregistre bid/ask des titres suivis a chaque tour : dans quelques semaines on
    saura si l'ASK decroche aussi lors des chutes LSX (TR ne fournit pas cet historique)."""
    heure = datetime.now(picks.PARIS).isoformat(timespec="seconds")
    with FICHIER_JOURNAL.open("a", encoding="utf-8") as f:
        for l in lignes:
            f.write(json.dumps({"t": heure, "isin": l["isin"], "bid": l["bid"], "ask": l["ask"]}) + "\n")


# ------------------------------------------------------------ prevision
# "Peut-on prevoir que ca va chuter ?" (24/09) : les decrochages suivent un rythme
# propre a chaque titre. Anoto : aligne 9h-16h (-2 a -7 %), decroche a 17h (-37 %,
# -47 %) quand Stockholm ferme. Friwo : decroche 16 jours sur 23, surtout lundi/mardi.
# On mesure donc, pour chaque (jour de semaine, heure), la part des observations ou le
# bid LSX est a <= -20 % du vrai prix. Sources : bougies LSX 4 h sur un mois + bougies
# horaires sur 5 jours, comparees au vrai prix horaire (yfinance) ; apres la fermeture
# du marche d'origine, on compare a sa derniere cotation du jour.
HEURES = range(8, 23)
_CACHE_PREV: dict[str, tuple[float, dict]] = {}
_CACHE_REEL: dict[str, tuple[float, tuple]] = {}


def _reel_horaire(sym: str, taux: dict) -> list[tuple[datetime, float]]:
    h = _hist(yf.Ticker(sym), period="1mo", interval="1h")
    return [(t.tz_convert("Europe/Paris").to_pydatetime(), _vers_eur(float(c), t.date().isoformat(), taux))
            for t, c in h["Close"].items()]


def _reel_a(reel: list[tuple[datetime, float]], t: datetime) -> float | None:
    """Derniere cotation reelle connue a l'instant t (la veille si le marche n'a pas encore ouvert)."""
    avant = [p for d, p in reel if d <= t]
    return avant[-1] if avant else None


async def prevision(isin: str) -> dict:
    import asyncio
    from datetime import timedelta

    from tr_data import historiques

    if isin in _CACHE_PREV and time.time() - _CACHE_PREV[isin][0] < 1800:
        return _CACHE_PREV[isin][1]
    sym = await asyncio.to_thread(symbole, isin)
    if not sym:
        return {"dispo": False, "raison": "titre introuvable sur Yahoo"}
    devise = await asyncio.to_thread(lambda: (yf.Ticker(sym).fast_info or {}).get("currency"))
    taux = await asyncio.to_thread(_taux_eur, devise)
    reel = await asyncio.to_thread(_reel_horaire, sym, taux)
    if not reel:
        return {"dispo": False, "raison": "pas d'historique horaire Yahoo"}
    mois, semaine = await asyncio.gather(historiques([isin], range="1m", timeout=12), historiques([isin], range="5d", timeout=12))

    points = []  # (instant, bid LSX)
    for c in (mois.get(isin) or {}).get("aggregates", []):  # bougies 4 h : prix a la cloture de la bougie
        points.append((datetime.fromtimestamp(c["time"] / 1000, picks.PARIS) + timedelta(hours=4), float(c["close"])))
    for c in (semaine.get(isin) or {}).get("aggregates", []):  # bougies 1 h
        points.append((datetime.fromtimestamp(c["time"] / 1000, picks.PARIS) + timedelta(hours=1), float(c["close"])))

    grille: dict = {}
    for t, bid in points:
        if t.weekday() > 4 or t.hour not in HEURES:
            continue
        ref = _reel_a(reel, t)
        if not ref:
            continue
        case = grille.setdefault(f"{t.weekday()}-{t.hour}", {"n": 0, "dec": 0})
        case["n"] += 1
        case["dec"] += (bid / ref - 1) * 100 <= DECROCHE
    par_heure = {h: [grille.get(f"{j}-{h}", {"n": 0, "dec": 0}) for j in range(5)] for h in HEURES}
    heure = {h: (sum(c["dec"] for c in v), sum(c["n"] for c in v)) for h, v in par_heure.items()}
    jour = {j: (sum(grille.get(f"{j}-{h}", {"dec": 0})["dec"] for h in HEURES), sum(grille.get(f"{j}-{h}", {"n": 0})["n"] for h in HEURES)) for j in range(5)}
    total_dec, total_n = sum(d for d, _ in heure.values()), sum(n for _, n in heure.values())

    # heures "a risque" : au moins 3 observations et >= 40 % de decrochages
    risque = [h for h, (d, n) in heure.items() if n >= 3 and d / n >= 0.4]
    # heures "sures" pour vendre : au moins 4 observations et aucun decrochage (Friwo : 18h-22h, 0 sur 28)
    sures = [h for h, (d, n) in heure.items() if n >= 4 and d == 0]
    res = {"dispo": True, "symbole": sym, "grille": grille, "n": total_n,
           "taux_global": round(total_dec / total_n * 100) if total_n else None,
           "par_heure": {h: {"dec": d, "n": n} for h, (d, n) in heure.items()},
           "par_jour": {j: {"dec": d, "n": n} for j, (d, n) in jour.items()},
           "heures_risque": risque, "heures_sures": sures}
    res["message"] = _message_prevision(res)
    _CACHE_PREV[isin] = (time.time(), res)
    return res


def _message_prevision(r: dict) -> str:
    JOURS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi"]
    if not r["n"]:
        return "Pas assez d'observations pour prévoir."
    if r["taux_global"] >= 60:
        return (f"Le bid LSX est décroché {r['taux_global']} % du temps : c'est son état normal. "
                "Les moments où il est aligné sur le vrai prix sont l'exception : c'est là qu'il faut vendre.")
    if r["heures_risque"]:
        h = r["heures_risque"]
        pire = max(h, key=lambda x: r["par_heure"][x]["dec"] / r["par_heure"][x]["n"])
        d, n = r["par_heure"][pire]["dec"], r["par_heure"][pire]["n"]
        jours = [JOURS[j] for j, v in r["par_jour"].items() if v["n"] >= 3 and v["dec"] / v["n"] >= 0.4]
        sures = f" Heures les plus sûres pour vendre : {', '.join(f'{x}h' for x in r['heures_sures'])}." if r["heures_sures"] else ""
        return (f"Décrochages fréquents vers {', '.join(f'{x}h' for x in h)} (à {pire}h : {d} fois sur {n})"
                + (f", surtout le {' et le '.join(jours)}" if jours else "") + ". Évite de vendre sur LSX à ces heures-là." + sures)
    return f"Pas d'heure à risque nette : le bid décroche {r['taux_global']} % du temps, sans rythme clair."


def prix_reel(isin: str) -> tuple[float, str] | None:
    """Derniere cotation du marche d'origine en EUR (cache 2 min). Apres la fermeture :
    la cloture du jour. Yahoo peut avoir ~15 min de retard."""
    if isin in _CACHE_REEL and time.time() - _CACHE_REEL[isin][0] < 120:
        return _CACHE_REEL[isin][1]
    sym = symbole(isin)
    if not sym:
        return None
    t = yf.Ticker(sym)
    h = _hist(t, period="5d", interval="1m")
    if h.empty:
        return None
    taux = _taux_eur((t.fast_info or {}).get("currency"))
    quand = h.index[-1]
    val = (_vers_eur(float(h["Close"].iloc[-1]), quand.date().isoformat(), taux), quand.tz_convert("Europe/Paris").isoformat(timespec="minutes"))
    _CACHE_REEL[isin] = (time.time(), val)
    return val


def prechauffer(isins: list[str]) -> None:
    """Calcule a l'avance l'ecart LSX / origine (appele apres chaque tour, dans un thread)."""
    for isin in isins:
        try:
            comparer(isin)
        except Exception:
            continue
