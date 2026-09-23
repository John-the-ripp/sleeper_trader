"""Detecte les titres qui font des mouvements >= seuil de facon REGULIERE
(plusieurs fois dans le mois), pas juste une fois. Different de scan.py :
celui-ci regarde l'historique, pas l'instant present.

Volontairement applique a un sous-ensemble (les candidats deja trouves par
scan.py), pas a tout l'univers : recuperer l'historique (aggregate_history_light)
est plus lourd qu'un simple ticker, sur 11000 titres ce serait trop long."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone

from tr_client import fetch_many_history


def grouper_par_jour(aggregates: list[dict]) -> list[tuple]:
    """Regroupe les bougies TR par jour -> [(date, bas, haut)], trie."""
    par_jour = defaultdict(list)
    for c in aggregates:
        try:
            d = datetime.fromtimestamp(c["time"] / 1000, timezone.utc).date()
            par_jour[d].append((float(c["low"]), float(c["high"])))
        except (KeyError, TypeError, ValueError):
            continue
    out = []
    for d, valeurs in sorted(par_jour.items()):
        bas = min(v[0] for v in valeurs)
        haut = max(v[1] for v in valeurs)
        out.append((d, bas, haut))
    return out


def compter_occurrences(jours: list[tuple], seuil_pct: float) -> int:
    """Nb de jours ou l'amplitude (haut-bas)/bas depasse seuil_pct."""
    n = 0
    for _date, bas, haut in jours:
        if bas > 0 and (haut - bas) / bas * 100 >= seuil_pct:
            n += 1
    return n


async def un_tour_regularite(
    isins: list[str], seuil_pct: float = 30.0, min_occurrences: int = 3, range: str = "1m"
) -> list[dict]:
    """Pour chaque ISIN, regarde son historique et garde ceux qui depassent
    seuil_pct au moins min_occurrences fois dans la periode `range`."""
    hist = await fetch_many_history(isins, range=range, timeout=60)

    lignes = []
    for isin, h in hist.items():
        if not h:
            continue
        jours = grouper_par_jour(h.get("aggregates", []))
        if len(jours) < 5:  # pas assez de donnees pour juger de la regularite
            continue
        occurrences = compter_occurrences(jours, seuil_pct)
        if occurrences >= min_occurrences:
            lignes.append({
                "isin": isin,
                "occurrences": occurrences,
                "jours_mesures": len(jours),
                "taux_pct": round(occurrences / len(jours) * 100, 1),
            })
    lignes.sort(key=lambda l: -l["occurrences"])
    return lignes
