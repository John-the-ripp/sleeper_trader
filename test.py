"""Affiche les infos d'une action a partir de son ISIN.

    python test.py US0378331005          -> resume
    python test.py US0378331005 --full   -> JSON brut complet
"""

import asyncio
import json
import sys

from tr_client import fetch_one

args = [a for a in sys.argv[1:] if not a.startswith("--")]
ISIN = args[0] if args else "US0378331005"  
FULL = "--full" in sys.argv


async def main():
    instrument = await fetch_one(lambda tr: tr.instrument(ISIN))
    ticker = await fetch_one(lambda tr: tr.ticker(ISIN, exchange="LSX"))

    if FULL:
        print(json.dumps({"instrument": instrument, "ticker": ticker}, indent=2, ensure_ascii=False))
        return

    cap = instrument.get("marketCap") or {}
    print(f"Nom        : {instrument.get('name')} ({instrument.get('shortName')})")
    print(f"ISIN / WKN : {instrument.get('isin')} / {instrument.get('wkn')}")
    print(f"Symbole    : {instrument.get('intlSymbol')}")
    print(f"Type       : {instrument.get('typeId')}")
    print(f"Pays       : {(instrument.get('company') or {}).get('countryOfOrigin')}")
    print(f"Capi.      : {cap.get('value')} {cap.get('currencyId')}")
    print(f"Tradable   : {instrument.get('tradable')}")
    print()
    for champ in ("bid", "ask", "last", "open", "pre"):
        print(f"{champ:<5} : {(ticker.get(champ) or {}).get('price')}")


asyncio.run(main())
