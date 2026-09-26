"""FLOUZMULATEUR : un LLM trade un portefeuille virtuel pour trouver les motifs qui se repetent.

Deux portefeuilles de CAPITAL EUR, au VRAI prix du moment (achat a l'ask, vente au bid,
1 EUR de frais par ordre, comme paper.py) :
  - "llm"   : le LLM de .env choisit les achats / ventes. Pour chaque achat il DOIT dire sur
              quel motif il parie, son objectif et son stop. Il joue toutes les LLM_S secondes
              en seance.
  - "robot" : regles fixes sur les oscillateurs (achat pres du support, vente a la resistance,
              stop sous le support). Sert d'etalon : le LLM fait-il mieux que des regles simples ?

Le CODE garde la main sur l'execution : il verifie budget, liquidite et spread, execute au
prix live, et declenche objectifs / stops toutes les VEILLE_S secondes (le LLM ne regarde les
prix que toutes les 30 min). Le but n'est pas de gagner en fictif mais de MESURER : chaque
trade porte un motif, et stats_motifs() dit lesquels se repetent et rapportent.

On ne trade qu'en seance (9h-17h30, jours ouvres) : le soir et le week-end, les cotations L&S
des petites valeurs sont des fourchettes defensives qui fausseraient tout. Hors seance, le
LLM peut jouer "a blanc" (decisions affichees, rien d'execute).
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from datetime import datetime, timedelta


import picks
import recommandations
from tr_data import BASE, tickers

FICHIER = BASE / "flouz.json"
CAPITAL = 1000.0
MISE_MAX = 200.0
POSITIONS_MAX = 5
FRAIS = 1.0
SPREAD_MAX = 20.0
DUREE_MAX = 10          # seances ; au-dela, le robot sort (le LLM decide lui-meme)
VEILLE_S = 300          # objectifs / stops / achats du robot : toutes les 5 min
LLM_S = 1800            # decision du LLM : toutes les 30 min en seance
MOTIFS = {
    "rebond_support": "oscillateur : achat pres du support, vente a la resistance",
    "downup": "grosse chute (>= 20 %) d'un titre qui la rachete d'habitude en quelques seances",
    "chute_rebond": "forte chute du jour sur un titre qui rebondit souvent le lendemain",
    "cassure": "le prix sort du range par le haut : on suit le mouvement",
    "autre": "motif propose par le LLM (a decrire dans 'motif_detail')",
}
_verrou = asyncio.Lock()
ETAT: dict = {"en_cours": False, "dernier_llm": None, "erreur": None}


# ------------------------------------------------------------------ stockage
def _neuf() -> dict:
    return {"cash": CAPITAL, "positions": [], "fermees": [], "journal": []}


def charger() -> dict:
    try:
        d = json.loads(FICHIER.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    d.setdefault("debut", datetime.now(picks.PARIS).isoformat(timespec="seconds"))
    for nom in ("llm", "robot"):
        d.setdefault(nom, _neuf())
    return d


def _sauver(d: dict) -> None:
    FICHIER.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")


def reinitialiser() -> dict:
    d = {"debut": datetime.now(picks.PARIS).isoformat(timespec="seconds"), "llm": _neuf(), "robot": _neuf()}
    _sauver(d)
    return d


def _seances(depuis: str, maintenant: datetime) -> int:
    """Jours ouvres entre l'achat et maintenant."""
    d0 = datetime.fromisoformat(depuis).date()
    n, j = 0, d0
    while j < maintenant.date():
        j += timedelta(days=1)
        n += j.weekday() < 5
    return n


# ------------------------------------------------------------------ candidats
def candidats(limite: int = 25) -> list[dict]:
    """Titres interessants a trader, avec les faits calcules que le LLM et le robot utilisent."""
    par_isin: dict[str, dict] = {}
    # 1. oscillateurs (support / resistance), tous marches, jusqu'a 2 EUR
    for l in recommandations.oscillateurs(prix_max=2.0, spread_max=SPREAD_MAX)["lignes"]:
        par_isin[l["isin"]] = {
            "isin": l["isin"], "nom": l["nom"], "bourse": l["bourse"], "motifs": ["rebond_support"],
            "support": l["support"], "resistance": l["resistance"], "position_pct": l["position_pct"],
            "allers": f"{l['n']} allers de +{l['hausse_med']} % (min +{l['hausse_min']} %), cycle ~{l['periode']} seances",
            "net_cycle": l["net_cycle"], "regularite": l["regularite"], "score": l["score"],
        }
    # 2. DOWN/UP : chute rachetee d'habitude, en phase DOWN maintenant = moment guette
    for l in picks.charger_downup().get("lignes", []):
        if not l.get("downup") or l.get("spread_pct", 99) > SPREAD_MAX:
            continue
        c = par_isin.setdefault(l["isin"], {"isin": l["isin"], "nom": l["nom"], "motifs": [], "score": 0})
        c["motifs"].append("downup")
        c["downup"] = (f"{l['reussis']}/{l['cycles']} chutes rachetees, chute moy {l['chute_moy']} %, rebond moy "
                       f"+{l['rebond_moy']} % en {l['delai_moy']} seances" + (" ; EN PHASE DOWN MAINTENANT" if l["en_phase_down"] else ""))
        c["score"] += 60 if l["en_phase_down"] else 10
    # 3. fortes chutes du jour sur un titre qui rebondit d'habitude le lendemain
    for l in picks.charger_mouvements().get("lignes", []):
        m = l.get("motif") or {}
        if (l.get("var_1j") or 0) > -15 or l.get("spread_pct", 99) > SPREAD_MAX or not m.get("motif"):
            continue
        c = par_isin.setdefault(l["isin"], {"isin": l["isin"], "nom": l["nom"], "motifs": [], "score": 0})
        c["motifs"].append("chute_rebond")
        c["chute_rebond"] = f"{l['var_1j']:+.1f} % aujourd'hui ; {m['rebonds']}/{m['chutes']} chutes suivies d'un rebond (moy +{m['rebond_moyen']} %)"
        c["score"] += 40
    return sorted(par_isin.values(), key=lambda c: -c.get("score", 0))[:limite]


async def _cotes(isins: list[str]) -> dict[str, tuple[float | None, float | None, float]]:
    """isin -> (bid, ask, dispo_ask en EUR)."""
    live = await tickers(sorted(set(isins)), timeout=15) if isins else {}
    res = {}
    for isin, t in live.items():
        t = t or {}
        ask = picks._prix(t, "ask")
        res[isin] = (picks._prix(t, "bid"), ask, (ask or 0) * ((t.get("ask") or {}).get("size") or 0))
    return res


# ------------------------------------------------------------------ execution (commune aux 2 portefeuilles)
def _acheter(p: dict, c: dict, cote: tuple, montant: float, motif: str, raison: str,
             objectif: float | None, stop: float | None, detail: str | None, maintenant: datetime) -> str | None:
    """Renvoie None si execute, sinon la raison du refus."""
    bid, ask, dispo = cote
    if not bid or not ask:
        return "pas de cotation"
    spread = (ask - bid) / bid * 100
    if spread > SPREAD_MAX:
        return f"spread {spread:.1f} % > {SPREAD_MAX:g} %"
    if len(p["positions"]) >= POSITIONS_MAX:
        return f"deja {POSITIONS_MAX} positions"
    if any(x["isin"] == c["isin"] for x in p["positions"]):
        return "deja en portefeuille"
    montant = min(montant, MISE_MAX, p["cash"])
    if dispo and dispo < montant:
        return f"seulement {dispo:.0f} EUR disponibles a l'ask"
    qte = math.floor((montant - FRAIS) / ask)
    if qte <= 0:
        return "mise trop faible"
    cout = round(qte * ask + FRAIS, 2)
    p["cash"] = round(p["cash"] - cout, 2)
    p["positions"].append({
        "id": max([x["id"] for x in p["positions"] + p["fermees"]], default=0) + 1,
        "isin": c["isin"], "nom": c["nom"], "qte": qte, "prix_achat": ask, "investi": cout, "spread_achat": round(spread, 1),
        "achat": maintenant.isoformat(timespec="seconds"), "motif": motif if motif in MOTIFS else "autre",
        "motif_detail": detail, "raison": raison, "objectif": objectif, "stop": stop,
    })
    return None


def _vendre(p: dict, pos: dict, bid: float, raison: str, maintenant: datetime) -> None:
    recette = round(pos["qte"] * bid - FRAIS, 2)
    p["cash"] = round(p["cash"] + recette, 2)
    p["positions"].remove(pos)
    p["fermees"].append({**pos, "vente": maintenant.isoformat(timespec="seconds"), "prix_vente": bid,
                         "raison_vente": raison, "pnl": round(recette - pos["investi"], 2),
                         "pnl_pct": round((recette / pos["investi"] - 1) * 100, 1),
                         "seances": _seances(pos["achat"], maintenant)})


def _journal(p: dict, texte: str, maintenant: datetime, a_blanc: bool = False) -> None:
    p["journal"].append({"heure": maintenant.isoformat(timespec="seconds"), "texte": texte, "a_blanc": a_blanc})
    p["journal"] = p["journal"][-200:]


# ------------------------------------------------------------------ veille : objectifs, stops, robot
async def veille(maintenant: datetime | None = None) -> None:
    maintenant = maintenant or datetime.now(picks.PARIS)
    d = charger()
    cands = {c["isin"]: c for c in candidats(40)}
    tenus = [x["isin"] for n in ("llm", "robot") for x in d[n]["positions"]]
    cotes = await _cotes(tenus + list(cands))

    # objectifs et stops (declares par le LLM, calcules pour le robot)
    for n in ("llm", "robot"):
        p = d[n]
        for pos in list(p["positions"]):
            bid = (cotes.get(pos["isin"]) or (None,))[0]
            if not bid:
                continue
            if pos.get("objectif") and bid >= pos["objectif"]:
                _vendre(p, pos, bid, f"objectif {pos['objectif']:g} atteint", maintenant)
            elif pos.get("stop") and bid <= pos["stop"]:
                _vendre(p, pos, bid, f"stop {pos['stop']:g} touche", maintenant)
            elif n == "robot" and _seances(pos["achat"], maintenant) >= DUREE_MAX:
                _vendre(p, pos, bid, f"{DUREE_MAX} seances sans objectif", maintenant)

    # achats du robot : oscillateur revenu pres de son support
    r = d["robot"]
    for c in cands.values():
        if "rebond_support" not in c["motifs"] or len(r["positions"]) >= POSITIONS_MAX or r["cash"] < 50:
            continue
        bid, ask, _ = cotes.get(c["isin"]) or (None, None, 0)
        if ask and ask <= c["support"] * 1.10:
            refus = _acheter(r, c, cotes[c["isin"]], MISE_MAX, "rebond_support",
                             f"ask {ask:g} a moins de 10 % du support {c['support']:g}",
                             round(c["resistance"] * 0.97, 6), round(c["support"] * 0.85, 6), None, maintenant)
            if refus is None:
                _journal(r, f"Achat {c['nom']} a {ask:g} (support {c['support']:g}, resistance {c['resistance']:g})", maintenant)
    _sauver(d)


# ------------------------------------------------------------------ le LLM joue
SYSTEME = f"""Tu gères un portefeuille VIRTUEL d'actions (penny stocks surtout) pour découvrir quels motifs de prix se répètent et rapportent.
Achat au prix ask, vente au prix bid, {FRAIS:g} € de frais par ordre. Maximum {POSITIONS_MAX} positions, {MISE_MAX:g} € par achat.
Motifs possibles : {json.dumps(MOTIFS, ensure_ascii=False)}.
Règles :
- Utilise UNIQUEMENT les données fournies (prix, supports, résistances, statistiques calculées). N'invente aucun chiffre.
- Tu peux ne rien faire : un bon trader attend. N'achète que si l'écart entre prix d'achat et objectif couvre largement le spread.
- Chaque achat DOIT avoir un motif, un objectif (prix bid de vente) et un stop (prix bid de sortie), cohérents avec l'ask actuel.
- Tu peux vendre une position à tout moment si ton hypothèse ne tient plus.
- Explore : si tu repères un motif qui n'est pas dans la liste, utilise "autre" et décris-le dans "motif_detail".
Réponds par un objet JSON et rien d'autre :
{{"ventes": [{{"id": 1, "raison": "..."}}],
 "achats": [{{"isin": "...", "montant": 150, "motif": "rebond_support", "motif_detail": null, "objectif": 0.08, "stop": 0.05, "raison": "20 mots max"}}],
 "journal": "3 phrases max : ce que tu as fait et pourquoi, ce que tu as appris sur les motifs"}}"""


def _contexte(d: dict, cands: list[dict], cotes: dict, maintenant: datetime) -> str:
    p = d["llm"]
    lignes = [f"Maintenant : {maintenant:%A %d/%m/%Y %H:%M}", f"Liquidités : {p['cash']:.2f} €"]
    lignes.append(f"\nPositions ouvertes ({len(p['positions'])}) :")
    for x in p["positions"]:
        bid = (cotes.get(x["isin"]) or (None,))[0]
        pnl = f"{(x['qte'] * bid - FRAIS) / x['investi'] * 100 - 100:+.1f} % si vendu maintenant" if bid else "pas de cotation"
        lignes.append(f"- id {x['id']} {x['nom']} ({x['isin']}) : {x['qte']} titres achetés {x['prix_achat']:g} le {x['achat'][:10]}, "
                      f"motif {x['motif']}, objectif {x['objectif']}, stop {x['stop']}, bid actuel {bid} → {pnl}")
    if not p["positions"]:
        lignes.append("(aucune)")
    fermees = p["fermees"][-10:]
    if fermees:
        lignes.append("\nDerniers trades fermés (pour apprendre) :")
        lignes += [f"- {x['nom']} motif {x['motif']} : {x['pnl_pct']:+.1f} % en {x['seances']} séances ({x['raison_vente']})" for x in fermees]
    stats = stats_motifs(p["fermees"])
    if stats:
        lignes.append("\nBilan par motif jusqu'ici : " + " ; ".join(
            f"{s['motif']} {s['gagnants']}/{s['n']} gagnants, {s['pnl']:+.2f} €" for s in stats))
    lignes.append("\nCandidats (faits calculés) :")
    for c in cands:
        bid, ask, dispo = cotes.get(c["isin"]) or (None, None, 0)
        if not ask:
            continue
        sp = (ask - bid) / bid * 100 if bid else None
        f = [f"{c['nom']} ({c['isin']}{', ' + c['bourse'] if c.get('bourse') else ''}) : bid {bid:g} / ask {ask:g}"
             + (f", spread {sp:.1f} %" if sp is not None else "") + f", {dispo:.0f} € dispo à l'ask", f"motifs {', '.join(c['motifs'])}"]
        if "support" in c:
            f.append(f"support {c['support']:g}, résistance {c['resistance']:g}, position {c['position_pct']} % du range, "
                     f"{c['allers']}, gain net par cycle {c['net_cycle']} %, {c['regularite']}")
        for k in ("downup", "chute_rebond"):
            if c.get(k):
                f.append(c[k])
        lignes.append("- " + " | ".join(f))
    return "\n".join(lignes)


def _json_du_llm(texte: str) -> dict:
    m = re.search(r"\{.*\}", texte, re.DOTALL)
    try:
        return json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        return {}


def _nombre(x) -> float | None:
    try:
        return float(str(x).replace(",", ".")) if x not in (None, "", "null") else None
    except ValueError:
        return None


async def tour_llm(a_blanc: bool | None = None) -> dict:
    """Le LLM decide. a_blanc=None : automatique (a blanc hors seance)."""
    import analyste  # import tardif (analyste charge beaucoup de modules)
    maintenant = datetime.now(picks.PARIS)
    en_seance = maintenant.weekday() < 5 and picks.en_seance(maintenant)
    a_blanc = (not en_seance) if a_blanc is None else a_blanc
    async with _verrou:
        ETAT.update(en_cours=True, erreur=None)
        try:
            d = charger()
            cands = candidats()
            cotes = await _cotes([x["isin"] for x in d["llm"]["positions"]] + [c["isin"] for c in cands])
            texte = await asyncio.to_thread(analyste._llm, [
                {"role": "system", "content": SYSTEME},
                {"role": "user", "content": _contexte(d, cands, cotes, maintenant)},
            ], 1800)
            dec = _json_du_llm(texte)
            p, faits, par_isin = d["llm"], [], {c["isin"]: c for c in cands}
            for v in dec.get("ventes") or []:
                pos = next((x for x in p["positions"] if str(x["id"]) == str(v.get("id"))), None)
                bid = (cotes.get(pos["isin"]) or (None,))[0] if pos else None
                if not pos or not bid:
                    faits.append(f"vente id {v.get('id')} refusée (position ou cotation introuvable)")
                    continue
                if not a_blanc:
                    _vendre(p, pos, bid, "LLM : " + str(v.get("raison") or "")[:120], maintenant)
                faits.append(f"{'[à blanc] ' if a_blanc else ''}vente {pos['nom']} à {bid:g}")
            for a in dec.get("achats") or []:
                c = par_isin.get(a.get("isin"))
                if not c:
                    faits.append(f"achat {a.get('isin')} refusé (pas dans les candidats)")
                    continue
                obj, stop = _nombre(a.get("objectif")), _nombre(a.get("stop"))
                ask = (cotes.get(c["isin"]) or (None, None))[1]
                if not obj or not stop or not ask or not (stop < ask < obj):
                    faits.append(f"achat {c['nom']} refusé (objectif/stop incohérents avec l'ask {ask})")
                    continue
                if a_blanc:
                    faits.append(f"[à blanc] achat {c['nom']} à {ask:g}, objectif {obj:g}, stop {stop:g} ({a.get('motif')})")
                    continue
                refus = _acheter(p, c, cotes[c["isin"]], _nombre(a.get("montant")) or MISE_MAX, str(a.get("motif")),
                                 str(a.get("raison") or "")[:160], obj, stop, a.get("motif_detail"), maintenant)
                faits.append(f"achat {c['nom']} " + (f"à {ask:g} ({a.get('motif')})" if refus is None else f"refusé : {refus}"))
            _journal(p, (str(dec.get("journal") or texte[:300])).strip() + ("\n→ " + " ; ".join(faits) if faits else ""),
                     maintenant, a_blanc)
            _sauver(d)
            ETAT["dernier_llm"] = maintenant.isoformat(timespec="seconds")
            return {"decisions": dec, "faits": faits, "a_blanc": a_blanc}
        except Exception as exc:
            ETAT["erreur"] = f"{type(exc).__name__}: {exc}"[:300]
            raise
        finally:
            ETAT["en_cours"] = False


async def boucle() -> None:
    dernier_llm = 0.0
    while True:
        maintenant = datetime.now(picks.PARIS)
        if maintenant.weekday() < 5 and picks.en_seance(maintenant):
            try:
                await veille(maintenant)
                if asyncio.get_running_loop().time() - dernier_llm >= LLM_S:
                    dernier_llm = asyncio.get_running_loop().time()
                    await tour_llm(a_blanc=False)
            except Exception as exc:  # une panne (TR, LLM) ne doit pas tuer la boucle
                ETAT["erreur"] = f"{type(exc).__name__}: {exc}"[:300]
        await asyncio.sleep(VEILLE_S)


# ------------------------------------------------------------------ bilan
def stats_motifs(fermees: list[dict]) -> list[dict]:
    par: dict[str, list[dict]] = {}
    for x in fermees:
        cle = x["motif"] if x["motif"] != "autre" else "autre : " + (x.get("motif_detail") or "?")[:60]
        par.setdefault(cle, []).append(x)
    res = []
    for motif, xs in par.items():
        g = [x for x in xs if x["pnl"] > 0]
        res.append({"motif": motif, "n": len(xs), "gagnants": len(g), "taux": round(len(g) / len(xs) * 100),
                    "pnl": round(sum(x["pnl"] for x in xs), 2), "pnl_moy_pct": round(sum(x["pnl_pct"] for x in xs) / len(xs), 1),
                    "seances_moy": round(sum(x["seances"] for x in xs) / len(xs), 1)})
    return sorted(res, key=lambda s: -s["pnl"])


async def etat() -> dict:
    d = charger()
    cotes = await _cotes([x["isin"] for n in ("llm", "robot") for x in d[n]["positions"]])
    maintenant = datetime.now(picks.PARIS)
    out = {"debut": d["debut"], "capital": CAPITAL, "etat": ETAT, "motifs": MOTIFS,
           "en_seance": maintenant.weekday() < 5 and picks.en_seance(maintenant),
           "regles": {"mise_max": MISE_MAX, "positions_max": POSITIONS_MAX, "spread_max": SPREAD_MAX, "llm_min": LLM_S // 60}}
    for n in ("llm", "robot"):
        p = d[n]
        valeur = p["cash"]
        for x in p["positions"]:
            bid = (cotes.get(x["isin"]) or (None,))[0]
            x["bid"] = bid
            x["valeur"] = round(x["qte"] * bid - FRAIS, 2) if bid else None
            x["pnl_latent_pct"] = round((x["valeur"] / x["investi"] - 1) * 100, 1) if bid else None
            x["seances"] = _seances(x["achat"], maintenant)
            valeur += x["valeur"] if x["valeur"] is not None else x["investi"]
        out[n] = {**p, "valeur": round(valeur, 2), "pnl": round(valeur - CAPITAL, 2), "pnl_pct": round((valeur / CAPITAL - 1) * 100, 2),
                  "stats": stats_motifs(p["fermees"]), "journal": list(reversed(p["journal"][-30:])),
                  "fermees": list(reversed(p["fermees"][-50:]))}
    return out
