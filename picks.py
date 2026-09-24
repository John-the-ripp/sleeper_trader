"""Screener de "pics" : trouve les titres qui font REGULIEREMENT de gros
ecarts dans la journee (type Spermosens / Biophytis), sur tout l'univers TR.

Remplace regularite.py. Difference avec scan.py : scan.py regarde l'instant
present (ce qui bouge maintenant), picks.py regarde le mois ecoule (qui bouge
SOUVENT). Les deux alimentent le cockpit.

Pourquoi l'amplitude intra-jour (haut-bas du jour) et pas cloture/cloture :
un titre peut faire +40 % a 10h et revenir a 17h. En cloture/cloture on ne
voit rien, alors que c'est exactement le pic qu'on veut attraper.

Regles anti-faux-positifs, toutes apprises sur de vrais cas :
  - prix < PLANCHER : les prints sous 1 centime sur LSX sont souvent des
    artefacts (un point a 0,001 au milieu d'une serie a 5 EUR) ;
  - spread live trop large : cotation "fantome" du teneur de marche. TR a deja
    annule a posteriori un achat fait sur ce genre de prix (EcoRub) ;
  - trop de jours plats : ligne morte, le "pic" est un bug de cotation ;
  - EXCLUS : titres aux donnees cassees confirmees plusieurs fois.
"""

from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import motifs
from tr_client import fetch_many_history, fetch_many_tickers
from tr_data import BASE, charger_isins

PARIS = ZoneInfo("Europe/Paris")
FICHIER = BASE / "picks.json"
FICHIER_MVT = BASE / "mouvements.json"

SEUIL_PIC = 20.0        # amplitude du jour (%) a partir de laquelle on parle de pic
PLANCHER = 0.01         # EUR : en dessous, les prints LSX ne sont pas fiables
SPREAD_MAX = 50.0       # au-dela, on ne garde meme pas le titre (fantome certain)
PLATS_MAX = 50.0        # % de jours sans aucun mouvement toleres
SEANCE = (9 * 60, 17 * 60 + 30)  # minutes depuis minuit (Paris) : seuls prix fiables
MECANIQUE_MAX = 70.0    # % de jours a pic au meme horaire au-dela duquel c'est suspect
# Pays dont la bourse est fermee pendant la seance europeenne : la cotation
# L&S n'y est adossee a aucun vrai marche (ESPRIT : 0,031 a 10h puis 0,048
# chaque jour). Signales, masques par defaut dans le front.
HORS_FUSEAU = {"KY", "BM", "VG", "HK", "CN", "SG", "JP", "AU", "NZ", "ID", "MY", "TH", "IN", "KR", "TW"}
EXCLUS = {"DE000A31C222", "FR0004170017"}  # Hausvorteil, LNA Sante : donnees cassees
LOT = 250               # ISIN par connexion websocket
PARALLELE = 6           # lots traites en meme temps

ETAT: dict = {"en_cours": False, "progression": 0, "total": 0, "erreur": None}


def en_seance(t: datetime) -> bool:
    minutes = t.hour * 60 + t.minute
    return SEANCE[0] <= minutes <= SEANCE[1]


def despike(points: list[tuple]) -> list[tuple]:
    """Retire les points isoles qui partent et reviennent : un point qui s'ecarte
    de >= 50 % alors que ses deux voisins sont d'accord a 5 % pres.
    Vu le 24/09 : Maison Clio Blue 0,645 -> 1,94 -> 0,65, BayWa 8,36 -> 3,41
    -> 8,44. Un vrai marche ne fait pas x3 puis revient au centime en 4h."""
    garde = list(points)
    for largeur in (1, 2):  # un point isole, puis deux points isoles d'affilee (GoPro, 11 88 0)
        i = 1
        while i + largeur < len(garde):
            a, c = garde[i - 1][1], garde[i + largeur][1]
            milieu = [p[1] for p in garde[i:i + largeur]]
            voisins_accord = abs(a - c) / min(a, c) <= 0.05
            ecart = all(max(a, b) / min(a, b) >= 1.5 for b in milieu)
            if voisins_accord and ecart:
                del garde[i:i + largeur]  # pas de i += 1 : le point suivant prend la place
            else:
                i += 1
    return garde


def jours_depuis_bougies(aggregates: list[dict]) -> list[dict]:
    """Bougies 4h de TR -> une ligne par jour {date, bas, haut, cloture}.

    On ne garde que des PRIX OBSERVES PENDANT LA SEANCE (9h-17h30) : l'ouverture
    d'une bougie est le prix a son heure de debut, sa cloture le prix 4h plus
    tard. Les meches sont ignorees. Trois pieges verifies le 24/09 :
      - meches : prints isoles (Big Technologies, un print a 0,705 dans une
        journee a 1,08-1,12) ;
      - soiree : BFI Finance cote 0,041 le jour et 0,051 chaque soir ;
      - bougie 14h-18h : sa cloture tombe APRES la fermeture de Stockholm/Paris,
        au prix de nuit de L&S (KebNi 0,067 le jour -> 0,0256 a 18h, -62 %).
    Le dernier prix de seance de la veille est ajoute a chaque jour : un gap
    d'une seance a l'autre compte donc aussi comme un pic."""
    serie: dict = {}  # instant -> prix (open d'une bougie et close de la precedente = meme instant)
    for c in aggregates:
        try:
            debut = datetime.fromtimestamp(c["time"] / 1000, PARIS)
            fin = debut + timedelta(hours=4)
            points = [(debut, float(c["open"])), (fin, float(c["close"]))]
        except (KeyError, TypeError, ValueError):
            continue
        for t, prix in points:
            if en_seance(t):
                serie[t] = prix

    par_jour: dict = defaultdict(list)
    for t, prix in despike(sorted(serie.items())):
        par_jour[t.date()].append((t.hour, prix))

    jours, veille = [], None
    for d, points in sorted(par_jour.items()):
        if veille is not None:
            points = [(-1, veille)] + points  # heure -1 = cloture de la veille
        prix = [p[1] for p in points]
        du_jour = points[1:] if veille is not None else points
        prix_jour = [p[1] for p in du_jour]
        jours.append({
            "date": d.isoformat(),
            "bas": min(prix),
            "haut": max(prix),
            "cloture": prix[-1],
            # a quelle heure tombent le creux et le sommet DU JOUR (sans la
            # veille, sinon le motif horaire d'un titre mecanique est brouille)
            "h_bas": du_jour[prix_jour.index(min(prix_jour))][0],
            "h_haut": du_jour[prix_jour.index(max(prix_jour))][0],
        })
        veille = prix[-1]
    return jours


def analyser(jours: list[dict], seuil: float = SEUIL_PIC) -> dict | None:
    """Mesures de "picosite" d'un titre sur la periode. None si inexploitable.

    Fonction pure (pas de reseau) : c'est elle qu'on teste, et c'est elle
    qu'il faut modifier si on veut changer la definition d'un pic."""
    if len(jours) < 5:
        return None
    if min(j["bas"] for j in jours) < PLANCHER:
        return None

    amplitudes = [(j["haut"] - j["bas"]) / j["bas"] * 100 for j in jours]
    jours_pic = sum(1 for a in amplitudes if a >= seuil)
    plats_pct = sum(1 for a in amplitudes if a == 0) / len(jours) * 100

    # Chute -> rebond : une cloture qui perd >= seuil, suivie dans les 3 jours
    # d'un haut qui repasse >= seuil au-dessus du creux. C'est le setup
    # "acheter la chute, revendre le rebond".
    rebonds = 0
    for i in range(1, len(jours)):
        prev = jours[i - 1]["cloture"]
        if prev <= 0 or (jours[i]["cloture"] - prev) / prev * 100 > -seuil:
            continue
        creux = jours[i]["bas"]
        suite = jours[i + 1:i + 4]
        if any((j["haut"] - creux) / creux * 100 >= seuil for j in suite):
            rebonds += 1

    # Pic "mecanique" : le creux et le sommet tombent a la MEME heure presque
    # tous les jours (BATM : creux a 14h chaque jour, International Metals :
    # 0,02 le matin / 0,027 a 14h chaque jour). Un vrai marche ne fait pas
    # ca, c'est la cotation L&S qui change de regime selon l'heure.
    jours_a_pic = [j for j, a in zip(jours, amplitudes) if a >= seuil]
    mecanique_pct = 0.0
    if len(jours_a_pic) >= 3:
        motifs = [(j["h_bas"], j["h_haut"]) for j in jours_a_pic]
        plus_frequent = max(set(motifs), key=motifs.count)
        mecanique_pct = motifs.count(plus_frequent) / len(motifs) * 100

    return {
        "mecanique_pct": round(mecanique_pct),
        "mecanique": mecanique_pct >= MECANIQUE_MAX,
        "jours_mesures": len(jours),
        "jours_pic": jours_pic,
        "freq_pct": round(jours_pic / len(jours) * 100, 1),
        "amp_mediane": round(statistics.median(amplitudes), 1),
        "amp_max": round(max(amplitudes), 1),
        "rebonds": rebonds,
        "plats_pct": round(plats_pct, 1),
        "bas_mois": min(j["bas"] for j in jours),
        "haut_mois": max(j["haut"] for j in jours),
    }


def variations(jours: list[dict], bid: float, spread: float, maintenant: datetime) -> dict | None:
    """Variation sur la seance, 5 seances et le mois. None si inexploitable.

    Prix actuel = bid live SEULEMENT pendant la seance et si le spread est
    serre ; sinon le dernier prix de seance (le bid du soir/de nuit est la
    fourchette defensive de L&S : Biophytis 0,0288 a 23h pour 0,050 en vrai).
    Reference "seance" = dernier prix de seance de la VEILLE, pas le champ
    `pre` de TR (deja vu perime : H2 Core -> faux +700 %)."""
    if len(jours) < 2:
        return None
    aujourd_hui = maintenant.date().isoformat()
    live = maintenant.weekday() < 5 and en_seance(maintenant) and spread <= 20
    if live:
        courant = bid
        passes = [j for j in jours if j["date"] < aujourd_hui]  # seances terminees
    else:
        courant = jours[-1]["cloture"]
        passes = jours[:-1]
    if not passes or courant < PLANCHER:
        return None

    def var(ref: float | None) -> float | None:
        return round((courant - ref) / ref * 100, 1) if ref and ref >= PLANCHER else None

    # Historique qui s'arrete : Gossamer Bio avait un historique fini le 10/09
    # (~12 EUR) et un bid live a 0,12 -> faux "-99 % sur la seance". La
    # variation du jour n'a de sens que si la reference est la seance
    # precedente (<= 4 jours calendaires, pour couvrir un week-end).
    trou = (maintenant.date() - date.fromisoformat(passes[-1]["date"])).days
    ref_1j = passes[-1]["cloture"] if trou <= 4 else None
    ref_5j = passes[-5]["cloture"] if len(passes) >= 5 else None
    ref_1m = jours[0]["cloture"]
    return {
        "courant": courant, "source": "live" if live else "seance", "trou_jours": trou,
        "ref_1j": ref_1j, "var_1j": var(ref_1j), "date_1j": passes[-1]["date"],
        "ref_5j": ref_5j, "var_5j": var(ref_5j),
        "ref_1m": ref_1m, "var_1m": var(ref_1m),
    }


def score(m: dict, spread_pct: float) -> float:
    """Plus c'est haut, plus le titre pique souvent ET proprement.
    Les poids viennent du screener chute-rebond valide le 07/09."""
    return round(m["jours_pic"] + 3 * m["rebonds"] - 0.3 * m["plats_pct"] - 0.3 * spread_pct, 1)


def _prix(t: dict | None, champ: str) -> float | None:
    try:
        return float(t[champ]["price"])
    except (KeyError, TypeError, ValueError):
        return None


async def un_tour_picks(seuil: float = SEUIL_PIC) -> dict:
    noms = charger_isins()
    isins = [i for i in noms if i not in EXCLUS]
    ETAT.update(en_cours=True, progression=0, total=len(isins), erreur=None)

    lignes, mouvements = [], []
    maintenant = datetime.now(PARIS)
    # Pourquoi en parallele : un lot attend son timeout complet des qu'UN seul
    # titre ne repond pas (et il y en a dans presque chaque lot). En sequentiel
    # ca fait ~45 lots x 40 s. Le semaphore limite a PARALLELE lots a la fois
    # pour ne pas ouvrir 90 websockets d'un coup chez TR.
    sem = asyncio.Semaphore(PARALLELE)

    async def traiter(lot: list[str]) -> None:
        async with sem:
            # Historique et prix live en meme temps : deux connexions par lot
            hist, live = await asyncio.gather(
                fetch_many_history(lot, range="1m", timeout=30),
                fetch_many_tickers(lot, timeout=20),
            )
        _garder(lot, hist, live)
        ETAT["progression"] += len(lot)

    def _garder(lot, hist, live) -> None:
        for isin in lot:
            h, t = hist.get(isin), live.get(isin)
            bid, ask = _prix(t, "bid"), _prix(t, "ask")
            if not h or not bid or not ask or bid < PLANCHER:
                continue
            spread = (ask - bid) / bid * 100
            if spread > SPREAD_MAX:
                continue
            jours = jours_depuis_bougies(h.get("aggregates", []))
            v = variations(jours, bid, spread, maintenant)
            mo = motifs.chute_rebond(jours)
            # resume du motif, sans le detail des occurrences (garde le JSON leger)
            motif = {k: mo[k] for k in ("chutes", "rebonds", "taux", "rebond_moyen", "motif", "chute_en_cours")}
            if v is not None:
                mouvements.append({
                    "isin": isin, "nom": noms[isin], "bid": bid, "ask": ask,
                    "spread_pct": round(spread, 1), "hors_fuseau": isin[:2] in HORS_FUSEAU,
                    "clotures": [round(j["cloture"], 6) for j in jours], "motif": motif, **v,
                })
            m = analyser(jours, seuil)
            if m is None or m["jours_pic"] < 2 or m["plats_pct"] > PLATS_MAX:
                continue
            lignes.append({
                "isin": isin,
                "nom": noms[isin],
                "bid": bid,
                "ask": ask,
                "spread_pct": round(spread, 1),
                # Position du prix dans la fourchette du mois : 0 = au plus bas,
                # 100 = au plus haut. Proche de 0 = zone d'achat potentielle.
                "position_pct": round((bid - m["bas_mois"]) / (m["haut_mois"] - m["bas_mois"]) * 100)
                if m["haut_mois"] > m["bas_mois"] else None,
                "score": score(m, spread),
                "motif_cr": motif,
                "hors_fuseau": isin[:2] in HORS_FUSEAU,
                "clotures": [round(j["cloture"], 6) for j in jours],
                # amplitude de chaque jour, pour l'histogramme "profil de pics" du front
                "profil": [{"d": j["date"], "a": round((j["haut"] - j["bas"]) / j["bas"] * 100, 1)} for j in jours],
                **m,
            })

    await asyncio.gather(*(traiter(isins[k:k + LOT]) for k in range(0, len(isins), LOT)))

    lignes.sort(key=lambda l: -l["score"])
    resultat = {"heure": datetime.now(PARIS).isoformat(timespec="seconds"), "seuil": seuil, "lignes": lignes}
    FICHIER.write_text(json.dumps(resultat, ensure_ascii=False), encoding="utf-8")
    FICHIER_MVT.write_text(json.dumps({"heure": resultat["heure"], "lignes": mouvements}, ensure_ascii=False), encoding="utf-8")
    return resultat


def charger_mouvements() -> dict:
    try:
        return json.loads(FICHIER_MVT.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"heure": None, "lignes": []}


def charger_dernier() -> dict:
    try:
        return json.loads(FICHIER.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"heure": None, "seuil": SEUIL_PIC, "lignes": []}


async def lancer(seuil: float = SEUIL_PIC) -> None:
    if ETAT["en_cours"]:
        return
    try:
        await un_tour_picks(seuil)
    except Exception as exc:
        ETAT["erreur"] = f"{type(exc).__name__}: {exc}"
    finally:
        ETAT["en_cours"] = False


async def boucle_picks() -> None:
    """Un tour complet (~40 s) sert aux picks ET aux chutes/hausses. Toutes les
    15 min pendant la seance (les mouvements du jour doivent rester frais),
    toutes les 6 h sinon. Tout de suite si le resultat sur disque est absent."""
    while True:
        age = time.time() - FICHIER_MVT.stat().st_mtime if FICHIER_MVT.exists() else float("inf")
        maintenant = datetime.now(PARIS)
        intervalle = 15 * 60 if maintenant.weekday() < 5 and en_seance(maintenant) else 6 * 3600
        if age > intervalle:
            await lancer()
        await asyncio.sleep(60)


if __name__ == "__main__":
    t0 = time.time()
    r = asyncio.run(un_tour_picks())
    print(f"{len(r['lignes'])} picks en {time.time() - t0:.0f}s")
    for l in r["lignes"][:15]:
        print(f"{l['score']:>6} {l['nom'][:32]:<32} pics={l['jours_pic']:>2}/{l['jours_mesures']} "
              f"rebonds={l['rebonds']} spread={l['spread_pct']}% bid={l['bid']}")
