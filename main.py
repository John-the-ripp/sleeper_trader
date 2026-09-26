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
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import analyste
import live
import origine
import explications
import flouz
import picks
import recommandations
import regles
import paper
import simulateur
import scan
import tweets
from tr_client import fetch_one
from tr_data import BASE, charger_isins, historiques, place, tickers

WATCHLIST = BASE / "watchlist.json"
NOMS = charger_isins()  # 11k noms en memoire : la recherche ne touche pas le reseau


@asynccontextmanager
async def lifespan(_app):
    # Remplace @app.on_event("startup"), deprecie dans FastAPI.
    picks.APRES_TOUR[:] = [explications.tour_explications, explications.alertes_downup, explications.alertes_plancher,
                           recommandations.calculer, explications.journal_cotes, explications.prechauffer_origine]
    taches = [asyncio.create_task(scan.boucle_scan()), asyncio.create_task(picks.boucle_picks()),
              # au demarrage, explique deja ce qui peut l'etre avec le dernier tour sur disque
              asyncio.create_task(explications.tour_explications()),
              asyncio.create_task(regles.boucle_regles()),
              asyncio.create_task(live.boucle()),
              asyncio.create_task(tweets.boucle_notif()),
              asyncio.create_task(flouz.boucle()),  # FLOUZMULATEUR : objectifs/stops/robot 5 min, LLM 30 min (en seance)  # notification a chaque nouveau tweet suivi
              asyncio.create_task(explications.prechauffer_origine())]  # cache LSX/origine des le demarrage
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
    live = await tickers(isins, timeout=8)
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
        tickers([isin], timeout=8),
        historiques([isin], range=range, timeout=10),
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
        "place": place(isin),  # LSX (Lang & Schwarz) ou TDG (Tradegate)
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


@app.get("/api/tweets")
async def route_tweets(jours: int = 7, consigne: str = tweets.CONSIGNE_DEFAUT):
    """Onglet News Twitter : titres TR cites dans les tweets des comptes de comptes_x.json,
    avec les lectures IA deja faites pour cette consigne (sans appeler le LLM)."""
    cotations = {l["isin"]: l for l in picks.charger_mouvements()["lignes"]}
    data = await asyncio.to_thread(tweets.signaux, NOMS, cotations, max(1, min(jours, 30)))
    return tweets.annoter(data, consigne)


@app.post("/api/tweets/lire")
async def route_tweets_lire(jours: int = 7, consigne: str = tweets.CONSIGNE_DEFAUT):
    """Bouton "Lire avec l'IA" : le LLM lit les tweets pas encore lus pour cette consigne."""
    cotations = {l["isin"]: l for l in picks.charger_mouvements()["lignes"]}
    data = await asyncio.to_thread(tweets.signaux, NOMS, cotations, max(1, min(jours, 30)))
    return await asyncio.to_thread(tweets.lire, data, consigne.strip()[:300] or tweets.CONSIGNE_DEFAUT)


@app.get("/api/alertes")
def route_alertes(depuis: int = 0, limite: int = 50):
    """Alertes plus recentes que l'id `depuis` (le navigateur garde le dernier vu).
    limite=3000 pour la page Historique."""
    a = explications.charger_alertes()
    return {"dernier_id": a[-1]["id"] if a else 0, "total": len(a),
            "alertes": [x for x in reversed(a) if x["id"] > depuis][:max(1, min(limite, 3000))]}


# --------------------------------------------------- alertes personnalisees

class Regle(BaseModel):
    isin: str
    type: str   # var_bas | var_haut | prix_bas | prix_haut
    seuil: float


@app.get("/api/regles")
def route_regles():
    return {"regles": [{**r, "libelle": regles.libelle(r), "cours": regles.ETAT["cours"].get(r["isin"])} for r in regles.charger()],
            "heure": regles.ETAT["heure"], "actif": regles.ETAT["actif"], "etendu": regles.config().get("etendu", False)}


@app.post("/api/regles/config")
def config_regles(etendu: bool):
    """etendu=true : surveille 7h30-23h (heures LSX) au lieu de 9h-17h30."""
    c = regles.regler(etendu)
    regles.ETAT["actif"] = regles.surveille(datetime.now(picks.PARIS))
    return c


@app.post("/api/regles")
async def creer_regle(r: Regle):
    try:
        cree = regles.ajouter(r.isin, NOMS.get(r.isin, r.isin), r.type, r.seuil)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if regles.ETAT["actif"]:
        asyncio.create_task(regles.verifier())  # verifie tout de suite, sans attendre 30 s
    return cree


@app.delete("/api/regles/{id_}")
def supprimer_regle(id_: int):
    regles.supprimer(id_)
    return {"ok": True}


@app.post("/api/regles/{id_}/rearmer")
def rearmer_regle(id_: int):
    regles.rearmer(id_)
    return {"ok": True}


# ------------------------------------------------------------- simulateur

@app.get("/api/simulation")
def route_simulation(montant: float = 1000, chute: float = 20, cible: float = 15, duree: int = 5, stop: float = 0,
                     spread_max: float = 20, asie: bool = False, liquide: bool = False):
    """Rejoue "achat a l'ask apres une chute, revente au bid" sur tout l'univers."""
    p = {"montant": montant, "chute": chute, "cible": cible, "duree": max(1, min(duree, 20)), "stop": stop}
    return simulateur.classement(p, spread_max=spread_max, asie=asie, liquide=liquide)


@app.get("/api/simuler/{isin}")
def route_simuler(isin: str, montant: float = 1000, chute: float = 20, cible: float = 15, duree: int = 5, stop: float = 0):
    t = simulateur.charger_series()["titres"].get(isin)
    if not t:
        raise HTTPException(404, "pas de donnees de seance pour ce titre (prix trop bas, spread > 50 % ou historique court)")
    r = simulateur.backtest(t["j"], t["spread"], montant=montant, chute=chute, cible=cible, duree=max(1, min(duree, 20)), stop=stop)
    return {**r, "spread_pct": t["spread"], "ask": t["ask"], "bid": t["bid"], "dispo_ask": round(t["ask"] * (t.get("ask_size") or 0)),
            "jours": len(t["j"]), "du": t["j"][0][0], "au": t["j"][-1][0]}


# --------------------------------------------------- positions fictives

class Achat(BaseModel):
    isin: str
    montant: float = 1000


@app.get("/api/paper")
async def route_paper():
    return await paper.etat()


@app.post("/api/paper/acheter")
async def paper_acheter(a: Achat):
    try:
        return await paper.acheter(a.isin, NOMS.get(a.isin, a.isin), a.montant)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/paper/{id_}/vendre")
async def paper_vendre(id_: int):
    try:
        return await paper.vendre(id_)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/paper/{id_}")
def paper_supprimer(id_: int):
    paper.supprimer(id_)
    return {"ok": True}


# -------------------------------------------------------- recommandations

# ------------------------------------------------ FLOUZMULATEUR (le LLM trade en virtuel)

@app.get("/api/flouz")
async def route_flouz():
    return await flouz.etat()


@app.post("/api/flouz/tour")
async def route_flouz_tour(a_blanc: bool | None = None):
    """Fait jouer le LLM maintenant (a blanc hors seance, sauf a_blanc=false)."""
    if flouz.ETAT["en_cours"]:
        raise HTTPException(409, "le LLM est deja en train de jouer")
    try:
        return await flouz.tour_llm(a_blanc)
    except Exception as exc:
        raise HTTPException(502, f"LLM indisponible : {exc}")


@app.post("/api/flouz/reinitialiser")
def route_flouz_reinit():
    flouz.reinitialiser()
    return {"ok": True}


@app.get("/api/recommandations")
async def route_recommandations():
    r = recommandations.charger()
    if r["heure"] is None:  # jamais calcule : on le fait tout de suite (~5 s)
        r = await recommandations.calculer()
    return r


@app.get("/api/oscillateurs")
def route_oscillateurs(prix_min: float = 0.001, prix_max: float = 0.10, rebond_min: float = 30,
                       spread_max: float = 25, net_min: float = 10):
    """Penny stocks qui oscillent regulierement support <-> resistance (type Erda, Anoto)."""
    return recommandations.oscillateurs(prix_min, prix_max, rebond_min, spread_max, net_min)


# ------------------------------------------------ rebond sur plancher

@app.get("/api/ranges")
def route_ranges(spread_max: float = 20, asie: bool = False, montant: float = 1000, rentables: bool = True):
    r = simulateur.ranges(spread_max=spread_max, asie=asie, montant=montant)
    if rentables:  # un range plus etroit que le spread ne rapporte rien
        r["lignes"] = [l for l in r["lignes"] if l["gain_cycle_pct"] > 0]
    # + les titres type Anoto (DOWN/UP) qui chutent ET rebondissent de >= 20 % : categorie gold aussi
    deja = {l["isin"] for l in r["lignes"]}
    r["anoto_like"] = [
        {**d, "categorie": "gold" if d["reussis"] >= 3 else "gold_nc", "origine": origine.resume(d["isin"])}
        for d in picks.charger_downup()["lignes"]
        if d["downup"] and (d["rebond_moy"] or 0) >= simulateur.GOLD and (d["chute_moy"] or 0) <= -simulateur.GOLD
        and d["spread_pct"] <= spread_max and (asie or not d["hors_fuseau"]) and d["isin"] not in deja
    ]
    return r


# ------------------------------------------------------- DOWN/UP Anoto-like

@app.get("/api/downup")
def route_downup(spread_max: float = 20, asie: bool = False, tous: bool = False):
    """tous=false : seulement les titres classes DOWN/UP (>= 2 cycles, >= 66 %).
    Les titres en phase DOWN (chute pas encore rachetee) passent en tete."""
    data = picks.charger_downup()
    lignes = [l for l in data["lignes"] if l["spread_pct"] <= spread_max and (asie or not l["hors_fuseau"])
              and (tous or l["downup"])]
    lignes.sort(key=lambda l: (not (l["downup"] and l["en_phase_down"]), -l["reussis"], -(l["taux"] or 0)))
    return {"heure": data["heure"], "total": len(data["lignes"]), "lignes": lignes[:300]}


@app.get("/api/rebonds/avant")
def route_avant_rebond():
    """Titres qui chutent aujourd'hui, classes selon leur historique de rebond (a acheter AVANT)."""
    return explications.avant_rebond()


@app.get("/api/rebonds")
def route_rebonds(jour: str | None = None):
    """Chute >= 10 % hier puis hausse >= 10 % aujourd'hui, avec l'explication IA."""
    return {**explications.rapport_rebonds(jour), "jours": explications.jours_disponibles()}


@app.get("/api/flux/{isin}")
async def flux(isin: str, request: Request):
    """Server-Sent Events : la page recoit chaque changement de bid/ask du titre
    en moins d'une seconde, tant que la fiche est ouverte."""
    async def evenements():
        live.ajouter(isin)
        vu, dernier_envoi, debut = -1, time.time(), time.time()
        try:
            # Duree de vie limitee a 30 s : le navigateur (EventSource) se reconnecte tout seul
            # en 1-3 s. Sans ca, `uvicorn --reload` attend la fermeture de ce flux infini avant
            # de redemarrer et le serveur reste bloque tant qu'une fiche est ouverte.
            yield "retry: 1000\n\n"
            while time.time() - debut < 30 and not await request.is_disconnected():
                c = live.COURS.get(isin)
                if c and c["seq"] != vu:
                    vu, dernier_envoi = c["seq"], time.time()
                    yield f"data: {json.dumps(c)}\n\n"
                elif time.time() - dernier_envoi > 15:
                    dernier_envoi = time.time()
                    yield ": ping\n\n"  # garde la connexion ouverte (proxys, navigateur)
                await asyncio.sleep(0.25)
        finally:
            live.retirer(isin)
    return StreamingResponse(evenements(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/origine/{isin}")
async def route_origine(isin: str):
    """LSX vs vrai marche (yfinance, en EUR) : ecart, decrochages, rattrapages, verdict."""
    return await asyncio.to_thread(origine.comparer, isin)


@app.get("/api/prevision/{isin}")
async def route_prevision(isin: str):
    """Risque de decrochage du bid LSX par heure et jour de semaine."""
    return await origine.prevision(isin)


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
