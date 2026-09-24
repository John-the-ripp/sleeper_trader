"""Cockpit sleeper-trader : API FastAPI + front statique.

Lancer :  .venv\\Scripts\\python -m uvicorn main:app --port 8000
puis ouvrir http://127.0.0.1:8000

Deux boucles tournent en fond (lifespan) :
  - scan.boucle_scan   : ce qui bouge MAINTENANT (toutes les 5 min)
  - picks.boucle_picks : qui bouge SOUVENT sur le mois (toutes les 6 h)
Les routes ne font que lire leurs resultats, sauf /watchlist et /action qui
interrogent TR a la demande (quelques ISIN, ~1-2 s)."""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

import analyste
import explications
import picks
import scan
from tr_client import fetch_many_history, fetch_many_tickers, fetch_one
from tr_data import BASE, charger_isins

WATCHLIST = BASE / "watchlist.json"
NOMS = charger_isins()  # 11k noms en memoire : la recherche ne touche pas le reseau


@asynccontextmanager
async def lifespan(_app):
    # Remplace @app.on_event("startup"), deprecie dans FastAPI.
    picks.APRES_TOUR[:] = [explications.tour_explications]
    taches = [asyncio.create_task(scan.boucle_scan()), asyncio.create_task(picks.boucle_picks()),
              # au demarrage, explique deja ce qui peut l'etre avec le dernier tour sur disque
              asyncio.create_task(explications.tour_explications())]
    yield
    for t in taches:
        t.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(BASE / "templates" / "index.html")


# ------------------------------------------------------------------ etat

def seance() -> dict:
    """Pourquoi c'est utile : hors seance, les quotes LSX sont des fourchettes
    defensives du teneur de marche. Le front affiche un bandeau d'alerte."""
    now = datetime.now(picks.PARIS)
    ouvre = now.weekday() < 5
    minutes = now.hour * 60 + now.minute
    return {
        "heure": now.isoformat(timespec="seconds"),
        "lsx": ouvre and 7 * 60 + 30 <= minutes < 23 * 60,
        "euronext": ouvre and 9 * 60 <= minutes < 17 * 60 + 30,
    }


@app.get("/api/etat")
def etat():
    return {
        "seance": seance(),
        "scan": {k: scan.DERNIER_SCAN.get(k) for k in ("heure", "en_cours", "tours", "erreur")},
        "picks": picks.ETAT,
        "ia": explications.ETAT,
        "univers": len(NOMS),
    }


# ----------------------------------------------------------------- picks

@app.get("/api/picks")
def route_picks():
    return {**picks.charger_dernier(), "etat": picks.ETAT}


@app.post("/api/picks/relancer")
async def relancer_picks(seuil: float = picks.SEUIL_PIC):
    if picks.ETAT["en_cours"]:
        return {"ok": False, "raison": "deja en cours"}
    asyncio.create_task(picks.lancer(seuil))
    return {"ok": True}


# ------------------------------------------------------- chutes / hausses

@app.get("/api/mouvements")
def route_mouvements(periode: str = "1j", sens: str = "baisse", spread_max: float = 20,
                     prix_max: float | None = None, asie: bool = False, trous: bool = False,
                     motif: bool = False, limite: int = 200):
    """Tri fait cote serveur : ~7000 titres en memoire, on n'envoie que le haut
    du classement au navigateur."""
    if periode not in ("1j", "5j", "1m") or sens not in ("baisse", "hausse"):
        raise HTTPException(400, "periode = 1j|5j|1m, sens = baisse|hausse")
    cle = "var_" + periode
    data = picks.charger_mouvements()
    lignes = [l for l in data["lignes"]
              if l.get(cle) is not None and l["spread_pct"] <= spread_max
              and (prix_max is None or l["courant"] <= prix_max) and (asie or not l["hors_fuseau"])
              # historique TR interrompu (> 4 jours) : reference perimee, masque par defaut
              and (trous or l.get("trou_jours", 0) <= 4)
              # "motif" : seulement les titres qui rebondissent d'habitude le lendemain d'une chute
              and (not motif or (l.get("motif") or {}).get("motif"))
              and (l[cle] < 0 if sens == "baisse" else l[cle] > 0)]
    lignes.sort(key=lambda l: l[cle], reverse=(sens == "hausse"))
    trouves = len(lignes)
    lignes = lignes[:limite]
    if periode == "1j" and lignes:  # les explications portent sur le mouvement du jour
        expl = explications.charger(lignes[0]["date_jour"])
        lignes = [{**l, "explication": expl.get(l["isin"])} for l in lignes]
    return {"heure": data["heure"], "total": len(data["lignes"]), "trouves": trouves, "lignes": lignes}


# ------------------------------------------------------------------ live

@app.get("/api/live")
def route_live():
    return scan.DERNIER_SCAN


# ------------------------------------------------------------- watchlist

def lire_watchlist() -> list[str]:
    try:
        return json.loads(WATCHLIST.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []


@app.get("/api/watchlist")
async def route_watchlist():
    isins = lire_watchlist()
    if not isins:
        return {"lignes": []}
    live = await fetch_many_tickers(isins, timeout=8)
    par_isin = {l["isin"]: l for l in picks.charger_dernier()["lignes"]}
    lignes = []
    for isin in isins:
        t = live.get(isin) or {}
        p = par_isin.get(isin, {})
        lignes.append({
            "isin": isin,
            "nom": NOMS.get(isin, isin),
            "bid": picks._prix(t, "bid"),
            "ask": picks._prix(t, "ask"),
            "pre": picks._prix(t, "pre"),
            "maj": (t.get("bid") or {}).get("time"),
            # repris du dernier screener, pour voir ou on est dans la fourchette
            "bas_mois": p.get("bas_mois"),
            "haut_mois": p.get("haut_mois"),
            "score": p.get("score"),
            "jours_pic": p.get("jours_pic"),
        })
    return {"lignes": lignes}


@app.post("/api/watchlist/{isin}")
def ajouter(isin: str):
    w = lire_watchlist()
    if isin not in w:
        w.append(isin)
        WATCHLIST.write_text(json.dumps(w), encoding="utf-8")
    return w


@app.delete("/api/watchlist/{isin}")
def retirer(isin: str):
    w = [i for i in lire_watchlist() if i != isin]
    WATCHLIST.write_text(json.dumps(w), encoding="utf-8")
    return w


# ---------------------------------------------------------- fiche titre

@app.get("/api/action/{isin}")
async def route_action(isin: str, range: str = "1m"):
    if range not in ("1d", "5d", "1m", "3m", "1y"):
        raise HTTPException(400, "range invalide")

    async def instrument():
        try:
            return await fetch_one(lambda tr: tr.instrument(isin), timeout=8)
        except Exception:
            return {}

    info, live, hist = await asyncio.gather(
        instrument(),
        fetch_many_tickers([isin], timeout=8),
        fetch_many_history([isin], range=range, timeout=10),
    )
    t = live.get(isin) or {}
    bougies = []
    for c in (hist.get(isin) or {}).get("aggregates", []):
        try:
            bougies.append({"time": c["time"] // 1000, "open": float(c["open"]), "high": float(c["high"]),
                            "low": float(c["low"]), "close": float(c["close"])})
        except (KeyError, TypeError, ValueError):
            continue
    pick = next((l for l in picks.charger_dernier()["lignes"] if l["isin"] == isin), None)
    return {
        "isin": isin,
        "nom": info.get("name") or NOMS.get(isin, isin),
        "symbole": info.get("intlSymbol"),
        "pays": (info.get("company") or {}).get("countryOfOrigin"),
        "type": info.get("typeId"),
        "bid": picks._prix(t, "bid"),
        "ask": picks._prix(t, "ask"),
        "pre": picks._prix(t, "pre"),
        "maj": (t.get("bid") or {}).get("time"),
        "bougies": bougies,
        "pick": pick,
        "suivi": isin in lire_watchlist(),
    }


@app.get("/api/analyse/{isin}")
async def route_analyse(isin: str, force: bool = False):
    """News (Google News) + motif chute->rebond + resume par le LLM de .env.
    Cache 1 h par titre ; force=true pour relancer."""
    return await analyste.analyser(isin, NOMS.get(isin, isin), force=force)


@app.post("/api/expliquer/{isin}")
async def route_expliquer(isin: str):
    """Bouton "Pourquoi ?" : explique le mouvement du jour d'un titre (force)."""
    l = next((l for l in picks.charger_mouvements()["lignes"] if l["isin"] == isin), None)
    if l is None or l.get("var_1j") is None:
        raise HTTPException(404, "pas de variation du jour pour ce titre")
    veille = (l.get("var_veille") or 0) <= explications.CHUTE_HIER
    return await explications.expliquer(l, avec_veille=veille, force=True)


@app.get("/api/alertes")
def route_alertes(depuis: int = 0):
    """Alertes plus recentes que l'id `depuis` (le navigateur garde le dernier vu)."""
    a = explications.charger_alertes()
    return {"dernier_id": a[-1]["id"] if a else 0,
            "alertes": [x for x in reversed(a) if x["id"] > depuis][:50]}


@app.get("/api/rebonds")
def route_rebonds(jour: str | None = None):
    """Chute >= 10 % hier puis hausse >= 10 % aujourd'hui, avec l'explication IA."""
    return {**explications.rapport_rebonds(jour), "jours": explications.jours_disponibles()}


@app.get("/api/recherche")
def recherche(q: str):
    q = q.strip().lower()
    if len(q) < 2:
        return []
    return [{"isin": i, "nom": n} for i, n in NOMS.items()
            if q in n.lower() or q in i.lower()][:12]


@app.get("/health")
def health():
    return {"status": "ok"}
