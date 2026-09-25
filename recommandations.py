"""Recommandations : les meilleurs potentiels de "descend puis remonte",
une fois le spread paye.

Constat du 24/09 qui a fixe les regles : en prix de seance (9h-17h30, sans
les cotations du soir), AUCUN titre ne fait +/-20 % chaque jour avec une
marge apres spread. Les plus actifs (Friwo, Anoto, World Copper) atteignent
20 % une seance sur 3, et leur ecart median (~8 %) est plus petit que leur
spread. Les "22 jours sur 23 a +20 %" du debut etaient des artefacts L&S.
On classe donc les VRAIS potentiels en combinant plusieurs signaux.

Ce n'est PAS un conseil d'investissement : c'est un classement statistique
sur 1 mois de cotations LSX, filtre par tout ce qu'on a appris a verifier
(spread, artefacts du soir, motifs mecaniques, historique interrompu...).

Deux etapes, pour rester leger :
  1. pre-selection sur series.json (tout l'univers, instantane) : frequence
     des seances a >= 20 %, marge nette apres spread, regularite par semaine ;
  2. confirmation des meilleurs sur les 5 derniers jours en bougies HORAIRES
     (range=5d) : l'amplitude est-elle toujours la ? le creux et le sommet
     tombent-ils toujours a la meme heure (signe d'artefact) ?
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

import picks
import motifs
from simulateur import backtest, charger_series
from tr_data import historiques
from tr_data import BASE

FICHIER = BASE / "recommandations.json"
SEUIL = 20.0          # "grosse seance" (affiche pour info)
SEUIL_ACTIF = 10.0    # seance "active" : assez d'ecart pour trader apres un spread serre
SPREAD_MAX = 20.0
MISE = 1000.0
N_CONFIRMES = 60      # nombre de candidats verifies en bougies horaires
ETAT: dict = {"en_cours": False}


def _preselection() -> list[dict]:
    data = charger_series()
    aujourd_hui = datetime.now(picks.PARIS).date()
    cands = []
    for isin, t in data["titres"].items():
        j = t["j"]
        if t["spread"] > SPREAD_MAX or t["hf"] or len(j) < 10:
            continue
        if (aujourd_hui - date.fromisoformat(j[-1][0])).days > 4:  # historique interrompu
            continue
        amps = [(h - b) / b * 100 if b > 0 else 0 for _, b, h, _ in j]
        freq = sum(a >= SEUIL_ACTIF for a in amps) / len(amps)
        med = statistics.median(amps)
        marge = med - t["spread"]  # ce qui reste d'une seance typique apres l'aller-retour ask/bid
        jours = [{"date": d, "bas": b, "haut": h, "cloture": c} for d, b, h, c in j]
        sim = backtest(j, t["spread"], montant=MISE, chute=15, cible=10, duree=5)
        rg = motifs.plancher(jours, t["bid"])
        du = motifs.down_up(jours)
        timing = []
        if rg and not rg["casse"] and rg["pres_du_plancher"] and rg["reussis"] >= 2:
            timing.append(f"au plancher de son range ({rg['reussis']}/{rg['testes']} rebonds sur {rg['plancher']:g} €)")
        if du["downup"] and du["en_phase_down"]:
            timing.append(f"en phase DOWN d'un motif DOWN/UP ({du['reussis']}/{du['cycles']} rebonds)")
        actif = freq >= 0.3 and marge > 2
        rentable = sim["n"] >= 2 and sim["net"] > 0
        if not (actif or rentable or timing):
            continue
        # regularite : part des semaines (>= 3 seances) avec au moins 2 seances a >= 20 %
        semaines = defaultdict(list)
        for (d, *_), a in zip(j, amps):
            semaines[date.fromisoformat(d).isocalendar()[:2]].append(a)
        pleines = [v for v in semaines.values() if len(v) >= 3]
        regu = sum(sum(a >= SEUIL for a in v) >= 2 for v in pleines) / len(pleines) if pleines else 0
        dispo = t["ask"] * (t.get("ask_size") or 0)
        cands.append({
            "isin": isin, "nom": t["nom"], "bid": t["bid"], "ask": t["ask"], "spread_pct": t["spread"],
            "dispo_ask": round(dispo), "liquide": dispo >= MISE,
            "seances": len(j), "seances_pic": sum(a >= SEUIL for a in amps), "seances_actives": sum(a >= SEUIL_ACTIF for a in amps),
            "freq": round(freq * 100), "sim_net": sim["net"], "sim_n": sim["n"], "sim_gagnants": sim["gagnants"],
            "timing": timing,
            "amp_mediane": round(med, 1), "marge_nette": round(marge, 1), "regularite": round(regu * 100),
            "profil": [{"d": d, "a": round(a, 1)} for (d, *_), a in zip(j, amps)],
            "plancher": rg["plancher"] if rg and not rg["casse"] else None,
            "clotures": [x[3] for x in j],
        })
    cands.sort(key=lambda c: -(c["freq"] * 0.4 + min(max(c["marge_nette"], 0), 20) + min(max(c["sim_net"], 0), 400) / 20 + 15 * len(c["timing"])))
    return cands


def _horaire(aggregates: list[dict]) -> dict:
    """5 derniers jours en bougies horaires : amplitude par seance (prix 9h-17h30,
    points isoles retires) et heure du creux / du sommet."""
    serie = {}
    for c in aggregates:
        try:
            debut = datetime.fromtimestamp(c["time"] / 1000, picks.PARIS)
            for t, p in ((debut, float(c["open"])), (debut + timedelta(hours=1), float(c["close"]))):
                if picks.en_seance(t):
                    serie[t] = p
        except (KeyError, TypeError, ValueError):
            continue
    par_jour = defaultdict(list)
    for t, p in picks.despike(sorted(serie.items())):
        par_jour[t.date()].append((t.hour, p))
    jours = []
    for d, pts in sorted(par_jour.items()):
        prix = [p for _, p in pts]
        if len(prix) < 3 or min(prix) <= 0:
            continue
        jours.append({"date": d.isoformat(), "amp": (max(prix) - min(prix)) / min(prix) * 100,
                      "h_bas": pts[prix.index(min(prix))][0], "h_haut": pts[prix.index(max(prix))][0]})
    pics = [j for j in jours if j["amp"] >= SEUIL_ACTIF]
    motif = Counter((j["h_bas"], j["h_haut"]) for j in pics).most_common(1)
    meca = bool(motif) and len(pics) >= 3 and motif[0][1] / len(pics) >= 0.8
    heures_bas = Counter(j["h_bas"] for j in pics).most_common(1)
    heures_haut = Counter(j["h_haut"] for j in pics).most_common(1)
    return {"jours_5j": len(jours), "pics_5j": len(pics), "mecanique": meca,
            "amp_5j": round(statistics.median([j["amp"] for j in jours]), 1) if jours else None,
            "h_creux": heures_bas[0][0] if heures_bas else None, "h_sommet": heures_haut[0][0] if heures_haut else None}


def _score(c: dict) -> int:
    s = 20 * min(c["freq"] / 60, 1)                           # seances actives (>= 10 %)
    s += 20 * min(max(c["marge_nette"], 0) / 10, 1)           # marge typique apres spread
    s += 25 * min(max(c["sim_net"], 0) / 300, 1)              # gain simule ask/bid sur 1 mois
    s += 15 * min(len(c["timing"]), 1)                        # signal d'entree maintenant
    s += 10 * (c["pics_5j"] / max(1, c["jours_5j"]))          # toujours actif cette semaine
    s += 5 * c["regularite"] / 100
    s += 5 * c["liquide"]
    return round(s)


def _raisons(c: dict) -> tuple[list[str], list[str]]:
    pour, contre = [], []
    for t in c["timing"]:
        pour.append("signal maintenant : " + t)
    if c["sim_n"]:
        (pour if c["sim_net"] > 0 else contre).append(
            f"simulation 1000 € : {c['sim_gagnants']}/{c['sim_n']} trades gagnants, {c['sim_net']:+.0f} € net (achat ask, vente bid)")
    pour.append(f"{c['seances_actives']}/{c['seances']} séances à ≥ {SEUIL_ACTIF:g} % d'écart, dont {c['seances_pic']} à ≥ {SEUIL:g} %")
    (pour if c["marge_nette"] > 0 else contre).append(
        f"écart médian {c['amp_mediane']:g} % pour un spread de {c['spread_pct']:g} % → marge {c['marge_nette']:+g} %")
    if c["regularite"] >= 75:
        pour.append(f"régulier : {c['regularite']} % des semaines ont au moins 2 grosses séances")
    if c["jours_5j"]:
        (pour if c["pics_5j"] >= c["jours_5j"] / 2 else contre).append(
            f"cette semaine : {c['pics_5j']}/{c['jours_5j']} séances à ≥ {SEUIL_ACTIF:g} % (bougies horaires)")
    if c["h_creux"] is not None and c["h_sommet"] is not None:
        pour.append(f"creux souvent vers {c['h_creux']}h, sommet vers {c['h_sommet']}h")
    if c["spread_pct"] > 10:
        contre.append(f"spread de {c['spread_pct']:g} % : l'aller-retour coûte cher")
    if not c["liquide"]:
        contre.append(f"seulement {c['dispo_ask']:,} € disponibles au prix ask".replace(",", " "))
    if c["regularite"] < 50:
        contre.append("irrégulier d'une semaine à l'autre")
    return pour, contre


async def calculer() -> dict:
    if ETAT["en_cours"]:
        return charger()
    ETAT["en_cours"] = True
    try:
        cands = _preselection()
        top = cands[:N_CONFIRMES]
        hist = await historiques([c["isin"] for c in top], range="5d", timeout=30) if top else {}
        recos = []
        for c in top:
            h = _horaire((hist.get(c["isin"]) or {}).get("aggregates", []))
            c.update(h)
            if h["mecanique"]:  # creux et sommet toujours aux memes heures : artefact probable
                continue
            c["score"] = _score(c)
            c["niveau"] = "fort" if c["score"] >= 70 else "bon" if c["score"] >= 50 else "a_surveiller"
            c["pour"], c["contre"] = _raisons(c)
            recos.append(c)
        recos.sort(key=lambda c: -c["score"])
        res = {"heure": datetime.now(picks.PARIS).isoformat(timespec="seconds"), "seuil": SEUIL, "seuil_actif": SEUIL_ACTIF,
               "preselectionnes": len(cands), "verifies": len(top), "lignes": recos}
        FICHIER.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        return res
    finally:
        ETAT["en_cours"] = False


def charger() -> dict:
    try:
        return json.loads(FICHIER.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"heure": None, "lignes": [], "preselectionnes": 0, "verifies": 0, "seuil": SEUIL}
