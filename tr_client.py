"""Client Trade Republic corrigé, basé sur Zarathustra2/TradeRepublicApi.

La librairie upstream envoie un handshake websocket `connect 21 {...}` que le
serveur refuse aujourd'hui (`failed 34`). Cette classe garde toute la surface
d'API de `TRApi` (ticker, instrument, portfolio, timeline...) et corrige :

  1. le handshake  -> protocole 31 + payload client complet ;
  2. le parsing    -> `str.split()` sans limite dans `start()` casse le JSON qui
                      contient des espaces (news, descriptions) ; on découpe en
                      3 champs max et on ne tokenise que les deltas ;
  3. aggregate_history_light -> `resolution` fait échouer la souscription en
                      silence ; on l'omet par défaut ;
  4. la robustesse -> exceptions du callback isolées, keepalive `echo`.

Vérifié le 2026-09-04 : ticker, instrument, stockDetails, neonSearch,
aggregateHistoryLight et homeInstrumentExchange répondent SANS authentification.
"""

from __future__ import annotations

import asyncio
import json
import time

import websockets
from trapi.api import TRApi, TRapiException  # noqa: F401  (ré-export pratique)

# Version de protocole acceptée par le serveur. Testé le 2026-09-04 :
# 21 et 22 -> "failed 34" ; 26, 30, 31 et 32 -> "connected".
PROTOCOL_VERSION = 31

CLIENT_INFO = {
    "platformId": "webtrading",
    "platformVersion": "chrome - 133.0.0",
    "clientId": "app.traderepublic.com",
    "clientVersion": "1.27.4",
}


class TrClient(TRApi):
    def __init__(self, number: str = "", pin: str = "", locale: str = "fr"):
        super().__init__(number, pin, locale=locale)
        self._keepalive_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ ws

    async def connect(self) -> None:
        """Ouvre le websocket avec un handshake que le serveur accepte."""
        if self.ws is not None:
            return

        self.ws = await websockets.connect(
            "wss://api.traderepublic.com",
            ping_interval=20,
            ping_timeout=20,
            max_size=None,  # stockDetails dépasse largement 1 Mo
        )

        payload = {"locale": self.locale, **CLIENT_INFO}
        await self.ws.send(f"connect {PROTOCOL_VERSION} {json.dumps(payload)}")
        response = await self.ws.recv()

        if response != "connected":
            await self.ws.close()
            self.ws = None
            raise TRapiException(f"handshake refusé par le serveur : {response!r}")

        self._keepalive_task = asyncio.create_task(self._keepalive())

    async def _keepalive(self) -> None:
        """`echo` périodique : évite que le serveur coupe une session inactive."""
        try:
            while self.ws is not None:
                await asyncio.sleep(30)
                await self.ws.send(f"echo {int(time.time() * 1000)}")
        except (asyncio.CancelledError, Exception):
            return

    async def close(self) -> None:
        if self._keepalive_task is not None:
            self._keepalive_task.cancel()
            self._keepalive_task = None
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
        self.started = False

    async def sub(self, payload_key, callback, **kwargs):
        """Identique à l'upstream, mais délègue la connexion à `connect()`."""
        await self.connect()
        return await super().sub(payload_key, callback, **kwargs)

    # --------------------------------------------------------------- fixes

    async def aggregate_history_light(
        self, isin, range="1d", resolution=None, exchange="LSX", callback=print
    ):
        """Historique agrégé.

        `resolution` est volontairement à None : le serveur ne répond plus rien
        (ni donnée ni erreur) quand ce champ est présent.
        """
        if range not in self.range_list:
            raise TRapiException(f"range doit valoir l'un de {self.range_list}")
        if exchange not in self.exchange_list:
            raise TRapiException(f"exchange doit valoir l'un de {self.exchange_list}")

        payload = {
            "type": "aggregateHistoryLight",
            "range": range,
            "id": f"{isin}.{exchange}",
        }
        if resolution is not None:
            payload["resolution"] = resolution

        return await self.sub(
            "aggregateHistoryLight",
            payload=payload,
            callback=callback,
            key=f"aggregateHistoryLight {isin} {exchange} {range}",
        )

    async def start(self, receive_one: bool = False):
        """Boucle de réception.

        Réécrite : l'upstream fait `str(data).split()` sans limite, ce qui
        éclate tout JSON contenant des espaces puis le recolle en écrasant les
        suites d'espaces et les retours à la ligne.
        """
        async with self.mu:
            if self.started:
                raise TRapiException("le client est déjà démarré")
            self.started = True

        try:
            while True:
                raw = str(await self.get_data())
                parts = raw.split(None, 2)

                # Trames de service : le serveur renvoie « echo <timestamp> » en
                # réponse au keepalive, et « connected » à la connexion.
                if len(parts) < 2 or not parts[0].isdigit():
                    continue

                sub_id, state = parts[0], parts[1]
                body = parts[2] if len(parts) > 2 else ""

                if state == "C":  # fin de souscription
                    continue
                if state == "E":
                    raise TRapiException(f"erreur serveur sur la sub {sub_id} : {body}")
                if state == "D":  # delta à appliquer sur la dernière réponse
                    body = self.decode_updates(sub_id, body.split())
                elif state != "A":
                    raise TRapiException(f"état inconnu {state!r} : {body}")

                self.latest_response[sub_id] = body

                try:
                    obj = json.loads(body)
                except json.JSONDecodeError:
                    continue

                key = next((k for k, v in self.dict.items() if v == sub_id), None)
                if isinstance(obj, list):
                    for item in obj:
                        if isinstance(item, dict):
                            item["key"] = key
                elif isinstance(obj, dict):
                    obj["key"] = key

                if receive_one:
                    return obj

                callback = self.callbacks.get(sub_id)
                if callback is None:
                    continue
                try:
                    callback(obj)
                except Exception as exc:  # un callback cassé ne tue pas le flux
                    print(f"[callback {key}] {type(exc).__name__}: {exc}")
        finally:
            self.started = False
            if receive_one:
                self.callbacks = {}
                self.latest_response = {}


async def fetch_one(coro_factory, locale: str = "fr", timeout: float = 20.0):
    """Exécute une souscription one-shot et rend le premier objet reçu.

    `coro_factory` reçoit le client et rend la coroutine de souscription :

        await fetch_one(lambda tr: tr.instrument("NL0010273215"))
    """
    tr = TrClient(locale=locale)
    try:
        await coro_factory(tr)
        return await asyncio.wait_for(tr.start(receive_one=True), timeout=timeout)
    finally:
        await tr.close()


async def fetch_many_tickers(isins, exchange: str = "LSX", locale: str = "fr", timeout: float = 25.0) -> dict:
    """Récupère le ticker de plusieurs ISIN sur UNE SEULE connexion websocket.

    Bien plus léger qu'un `fetch_one` par ISIN : une connexion pour tout le lot
    au lieu d'une par titre. Rend `{isin: payload_ou_None}` — `None` quand le
    titre n'a pas répondu dans le délai (marché fermé, exchange absent, ...).
    """
    tr = TrClient(locale=locale)
    results: dict[str, dict | None] = {isin: None for isin in isins}
    pending = set(isins)
    done = asyncio.Event()

    def make_cb(isin: str):
        def cb(payload):
            results[isin] = payload
            pending.discard(isin)
            if not pending:
                done.set()

        return cb

    try:
        for isin in isins:
            await tr.ticker(isin, exchange=exchange, callback=make_cb(isin))

        loop_task = asyncio.create_task(tr.start())
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            loop_task.cancel()
    finally:
        await tr.close()

    return results


async def fetch_many_history(
    isins, range: str = "5d", exchange: str = "LSX", locale: str = "fr", timeout: float = 25.0
) -> dict:
    """Comme `fetch_many_tickers`, mais pour `aggregateHistoryLight` (une
    connexion pour tout le lot). Rend `{isin: payload_ou_None}`.
    """
    tr = TrClient(locale=locale)
    results: dict[str, dict | None] = {isin: None for isin in isins}
    pending = set(isins)
    done = asyncio.Event()

    def make_cb(isin: str):
        def cb(payload):
            results[isin] = payload
            pending.discard(isin)
            if not pending:
                done.set()

        return cb

    try:
        for isin in isins:
            await tr.aggregate_history_light(isin, range=range, exchange=exchange, callback=make_cb(isin))

        loop_task = asyncio.create_task(tr.start())
        try:
            await asyncio.wait_for(done.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            loop_task.cancel()
    finally:
        await tr.close()

    return results
