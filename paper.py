"""Positions fictives ("paper trading") au VRAI prix du moment.

Achat = ask live au moment du clic, vente = bid live au moment du clic,
1 EUR de frais par ordre (tarif TR). Rien n'est envoye a Trade Republic :
c'est un bac a sable pour verifier si un rebond arrive vraiment avant d'y
mettre de l'argent.

Garde-fou : on refuse d'acheter quand le spread depasse SPREAD_MAX. Le soir
et sur les petites valeurs, l'ask est une fourchette defensive du teneur de
marche : "acheter" dessus fausserait tout le suivi.
"""

from __future__ import annotations

import json
import math
from datetime import datetime

import picks
from tr_data import BASE, tickers

FICHIER = BASE / "paper.json"
FRAIS = 1.0
SPREAD_MAX = 25.0


def charger() -> list[dict]:
    try:
        return json.loads(FICHIER.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _sauver(pos: list[dict]) -> None:
    FICHIER.write_text(json.dumps(pos, ensure_ascii=False, indent=1), encoding="utf-8")


async def _cotation(isin: str) -> tuple[float | None, float | None]:
    t = (await tickers([isin], timeout=8)).get(isin) or {}
    return picks._prix(t, "bid"), picks._prix(t, "ask")


async def acheter(isin: str, nom: str, montant: float) -> dict:
    bid, ask = await _cotation(isin)
    if not ask or not bid:
        raise ValueError("pas de cotation disponible pour ce titre en ce moment")
    spread = (ask - bid) / bid * 100
    if spread > SPREAD_MAX:
        raise ValueError(f"spread de {spread:.1f} % : cotation pas exploitable en ce moment (max {SPREAD_MAX:g} %)")
    qte = math.floor((montant - FRAIS) / ask)
    if qte <= 0:
        raise ValueError("mise trop faible pour acheter au moins 1 titre")
    maintenant = datetime.now(picks.PARIS)
    pos = charger()
    p = {"id": max((x["id"] for x in pos), default=0) + 1, "isin": isin, "nom": nom, "qte": qte,
         "prix_achat": ask, "spread_achat": round(spread, 1), "investi": round(qte * ask + FRAIS, 2),
         "achat": maintenant.isoformat(timespec="seconds"),
         "hors_seance": not (maintenant.weekday() < 5 and picks.en_seance(maintenant)),
         "vente": None, "prix_vente": None, "pnl": None}
    pos.append(p)
    _sauver(pos)
    return p


async def vendre(id_: int) -> dict:
    pos = charger()
    p = next((x for x in pos if x["id"] == id_ and x["vente"] is None), None)
    if p is None:
        raise ValueError("position introuvable ou deja vendue")
    bid, _ = await _cotation(p["isin"])
    if not bid:
        raise ValueError("pas de prix acheteur (bid) en ce moment")
    p.update(vente=datetime.now(picks.PARIS).isoformat(timespec="seconds"), prix_vente=bid,
             pnl=round(p["qte"] * bid - FRAIS - p["investi"], 2))
    _sauver(pos)
    return p


def supprimer(id_: int) -> None:
    _sauver([x for x in charger() if x["id"] != id_])


async def etat() -> dict:
    """Positions + valeur de revente AU BID des positions ouvertes."""
    pos = charger()
    ouvertes = [p for p in pos if p["vente"] is None]
    live = await tickers(sorted({p["isin"] for p in ouvertes}), timeout=8) if ouvertes else {}
    for p in ouvertes:
        t = live.get(p["isin"]) or {}
        bid, ask = picks._prix(t, "bid"), picks._prix(t, "ask")
        p["bid"], p["ask"] = bid, ask
        # valeur si je revends MAINTENANT au bid, frais de vente compris
        p["pnl_latent"] = round(p["qte"] * bid - FRAIS - p["investi"], 2) if bid else None
        p["pnl_latent_pct"] = round(p["pnl_latent"] / p["investi"] * 100, 1) if bid else None
    fermees = [p for p in pos if p["vente"]]
    return {"positions": sorted(pos, key=lambda p: (p["vente"] is not None, p["id"] * -1)),
            "realise": round(sum(p["pnl"] for p in fermees), 2),
            "latent": round(sum(p.get("pnl_latent") or 0 for p in ouvertes), 2),
            "investi": round(sum(p["investi"] for p in ouvertes), 2)}
