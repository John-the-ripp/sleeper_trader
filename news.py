"""Recuperation des news d'un titre via Google News RSS (gratuit, sans cle).

Le modele LLM ne va pas sur internet lui-meme : c'est ce module qui cherche,
puis on donne les titres d'articles au modele pour qu'il les relie aux
mouvements de prix (voir analyste.py).
"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

# Bouts de libelles TR qui polluent la recherche : "Biophytis Actions Nominatives",
# "MAAT PHARMA S.A. EO-,1", "Sono Group N.V. Aand.op naam EO...", "Diageo (ADR)".
BRUIT = re.compile(
    r"\s*(\(ADR\).*|Actions? Nom.*|Aand\.? ?op naam.*|\bEO[-, ].*|\bNA O\.N\..*|\bO\.N\..*|"
    r"\bInhaber.*|\bReg(istered)?\.? Shares.*|\bClass [A-Z]\b.*)$",
    re.IGNORECASE,
)


def nom_de_recherche(nom: str) -> str:
    propre = BRUIT.sub("", nom).strip(" -,.")
    return propre or nom


def _flux(requete: str, hl: str, gl: str, ceid: str) -> list[dict]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f'"{requete}" when:30d', "hl": hl, "gl": gl, "ceid": ceid})
    r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    items = []
    for it in ET.fromstring(r.content).findall(".//item"):
        try:
            quand = parsedate_to_datetime(it.findtext("pubDate"))
        except (TypeError, ValueError):
            continue
        source = it.findtext("source") or ""
        titre = it.findtext("title") or ""
        # Google ajoute " - Source" a la fin du titre : doublon avec le champ source
        if source and titre.endswith(" - " + source):
            titre = titre[: -len(source) - 3]
        items.append({"date": quand.astimezone(timezone.utc).isoformat(), "source": source,
                      "titre": titre, "lien": it.findtext("link") or ""})
    return items


def chercher(nom: str, limite: int = 15) -> list[dict]:
    """News des 30 derniers jours, FR + EN, dedoublonnees, plus recentes d'abord."""
    requete = nom_de_recherche(nom)
    vus, news = set(), []
    for hl, gl, ceid in (("fr", "FR", "FR:fr"), ("en-US", "US", "US:en")):
        try:
            flux = _flux(requete, hl, gl, ceid)
        except (requests.RequestException, ET.ParseError):
            continue
        for n in flux:
            cle = n["titre"].lower()[:60]
            if cle not in vus:
                vus.add(cle)
                news.append(n)
    news.sort(key=lambda n: n["date"], reverse=True)
    return news[:limite]


if __name__ == "__main__":
    import sys
    for n in chercher(" ".join(sys.argv[1:]) or "Biophytis"):
        print(datetime.fromisoformat(n["date"]).strftime("%d/%m"), n["source"], "|", n["titre"])
