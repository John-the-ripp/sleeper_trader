"""Simulateur : "si j'avais achete 1000 EUR a chaque grosse chute, combien
j'aurais gagne ?" - en achetant a l'ASK et en revendant au BID.

Strategie testee (type Anoto / DOWN-UP) :
  - ACHAT a la cloture du jour ou le titre a chute d'au moins `chute` %
    (sur 1 a 2 seances). Au prix ASK = cloture x (1 + spread/2).
    Aucune info du futur n'est utilisee : on ne sait pas si la chute continue.
  - VENTE des que le prix permet de revendre au BID avec `cible` % de gain
    (ordre limite), sinon a la cloture apres `duree` seances, au BID.
    Stop optionnel : vente si le BID perd `stop` % par rapport a l'achat.
  - Frais : 1 EUR par ordre (tarif Trade Republic), donc 2 EUR par aller-retour.

LIMITE IMPORTANTE : TR ne fournit pas l'historique du bid/ask. On applique le
spread ACTUEL du titre a tout l'historique. Sur un titre dont le spread varie
beaucoup (petites valeurs, le soir), le resultat reel peut etre pire.
"""

from __future__ import annotations

import json
import math

from tr_data import BASE

FICHIER_SERIES = BASE / "series.json"
FRAIS = 1.0  # EUR par ordre (TR)
GOLD = 20.0  # % : seuil "baisse fort et remonte fort" (profil Anoto)
DEFAUTS = {"montant": 1000.0, "chute": 20.0, "cible": 15.0, "duree": 5, "stop": 0.0}


def backtest(jours: list[list], spread_pct: float, montant: float = 1000.0, chute: float = 20.0,
             cible: float = 15.0, duree: int = 5, stop: float = 0.0, fenetre: int = 2) -> dict:
    """jours = [[date, bas, haut, cloture], ...] (prix de seance, voir picks.jours_depuis_bougies)."""
    demi = spread_pct / 200  # la moitie du spread de chaque cote du prix "milieu"
    trades, i = [], 1
    while i < len(jours):
        clot = jours[i][3]
        avant = [j[3] for j in jours[max(0, i - fenetre):i]]
        sommet = max(avant) if avant else 0
        if sommet <= 0 or (clot - sommet) / sommet * 100 > -chute:
            i += 1
            continue

        ask = clot * (1 + demi)
        qte = math.floor((montant - FRAIS) / ask) if ask > 0 else 0
        if qte <= 0:
            i += 1
            continue
        prix_cible = ask * (1 + cible / 100)
        prix_stop = ask * (1 - stop / 100) if stop else None
        vente, raison, k_sortie = None, None, None
        for k in range(i + 1, min(len(jours), i + 1 + duree)):
            bid_haut, bid_bas = jours[k][2] * (1 - demi), jours[k][1] * (1 - demi)
            if prix_stop and bid_bas <= prix_stop:  # prudence : le stop est teste avant la cible
                vente, raison, k_sortie = prix_stop, "stop", k
                break
            if bid_haut >= prix_cible:
                vente, raison, k_sortie = prix_cible, "cible", k
                break
        ouvert = False
        if vente is None:
            k_sortie = min(len(jours) - 1, i + duree)
            vente = jours[k_sortie][3] * (1 - demi)
            if k_sortie == i:  # achat le dernier jour connu : rien a revendre encore
                ouvert, raison = True, "ouvert"
            else:
                ouvert = k_sortie - i < duree
                raison = "ouvert" if ouvert else "duree"

        pnl = qte * vente - qte * ask - 2 * FRAIS
        trades.append({
            "achat": jours[i][0], "prix_achat": round(ask, 6), "qte": qte,
            "vente": jours[k_sortie][0], "prix_vente": round(vente, 6), "raison": raison,
            "seances": k_sortie - i, "pnl": round(pnl, 2), "pnl_pct": round(pnl / (qte * ask) * 100, 1),
        })
        i = k_sortie + 1  # une position a la fois

    fermes = [t for t in trades if t["raison"] != "ouvert"]
    gagnants = [t for t in fermes if t["pnl"] > 0]
    # cout du spread sur tous les allers-retours : ce que "coute" l'ecart ask/bid
    cout_spread = sum(t["qte"] * t["prix_achat"] * (spread_pct / 100) / (1 + demi) for t in fermes)
    return {
        "trades": trades, "n": len(fermes), "gagnants": len(gagnants),
        "taux": round(len(gagnants) / len(fermes) * 100) if fermes else None,
        "net": round(sum(t["pnl"] for t in fermes), 2),
        "net_pct": round(sum(t["pnl"] for t in fermes) / montant * 100, 1),
        "cout_spread": round(cout_spread, 2), "frais": 2 * FRAIS * len(fermes),
        "ouvert": next((t for t in trades if t["raison"] == "ouvert"), None),
    }


def charger_series() -> dict:
    try:
        return json.loads(FICHIER_SERIES.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"heure": None, "titres": {}}


_CACHE_RANGES: dict = {}


def ranges(spread_max: float = 20, asie: bool = False, montant: float = 1000.0) -> dict:
    """Titres en range (rebond regulier sur le meme plancher), calcules depuis
    les series du dernier tour (~1 s, garde en cache jusqu'au tour suivant)."""
    import motifs  # import local : motifs n'a pas besoin du simulateur
    data = charger_series()
    if _CACHE_RANGES.get("heure") != data["heure"]:
        tous = []
        for isin, t in data["titres"].items():
            jours = [{"date": d, "bas": b, "haut": h, "cloture": c} for d, b, h, c in t["j"]]
            r = motifs.plancher(jours, t["bid"])
            if r and not r["casse"]:
                tous.append({"isin": isin, "nom": t["nom"], "bid": t["bid"], "ask": t["ask"], "spread_pct": t["spread"],
                             "hors_fuseau": t["hf"], "dispo_ask": round(t["ask"] * (t.get("ask_size") or 0)),
                             "clotures": [j[3] for j in t["j"]], "bas_j": [j[1] for j in t["j"]], **r})
        _CACHE_RANGES.update(heure=data["heure"], tous=tous)
    lignes = []
    for l in _CACHE_RANGES["tous"]:
        if l["spread_pct"] > spread_max or (l["hors_fuseau"] and not asie):
            continue
        demi = l["spread_pct"] / 200
        # un cycle "ideal" : achat a l'ask au plancher, revente au bid au plafond, 2 x 1 EUR de frais
        brut = l["objectif"] * (1 - demi) / (l["plancher"] * (1 + demi)) - 1
        gain_cycle = round(brut * 100 - 2 * FRAIS / montant * 100, 1)
        # GOLD = "baisse fort ET remonte fort" (type Anoto) : rebond moyen >= 20 % sur un range d'au moins 20 %
        profil_gold = l["rebond_moy"] >= GOLD and l["largeur_pct"] >= GOLD
        # gold qui clignote seulement si c'est PROUVE (>= 3 rebonds) ; sinon "gold a confirmer"
        cat = ("gold" if l["reussis"] >= 3 else "gold_nc") if profil_gold else "fort" if l["rebond_moy"] >= 15 else "modere"
        lignes.append({**l, "gain_cycle_pct": gain_cycle, "gain_cycle_eur": round(montant * gain_cycle / 100, 2), "categorie": cat})
    # d'abord ceux qui sont AU plancher maintenant (zone d'achat), puis les plus rentables et reguliers
    ordre = {"gold": 0, "gold_nc": 1, "fort": 2, "modere": 3}
    lignes.sort(key=lambda l: (ordre[l["categorie"]], not l["pres_du_plancher"], not l["regulier"], -l["reussis"], -l["gain_cycle_pct"]))
    return {"heure": data["heure"], "total": len(_CACHE_RANGES["tous"]), "lignes": lignes[:300]}


def classement(params: dict, spread_max: float = 20, asie: bool = False, liquide: bool = False, limite: int = 200) -> dict:
    """Rejoue la strategie sur tout l'univers (series du dernier tour) : < 1 s."""
    data = charger_series()
    lignes = []
    for isin, t in data["titres"].items():
        if t["spread"] > spread_max or (t["hf"] and not asie):
            continue
        dispo = t["ask"] * (t.get("ask_size") or 0)
        if liquide and dispo < params["montant"]:
            continue
        r = backtest(t["j"], t["spread"], **params)
        if not r["n"]:
            continue
        lignes.append({"isin": isin, "nom": t["nom"], "bid": t["bid"], "ask": t["ask"], "spread_pct": t["spread"],
                       "dispo_ask": round(dispo), "hors_fuseau": t["hf"], "clotures": [j[3] for j in t["j"]],
                       **{k: r[k] for k in ("n", "gagnants", "taux", "net", "net_pct", "cout_spread", "frais", "ouvert")}})
    lignes.sort(key=lambda l: -l["net"])
    return {"heure": data["heure"], "params": params, "total": len(data["titres"]), "trouves": len(lignes), "lignes": lignes[:limite]}
