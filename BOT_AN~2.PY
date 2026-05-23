#!/usr/bin/env python3
"""
Bot Annunci Casa Torino - versione GitHub Actions
--------------------------------------------------
Cerca su Immobiliare.it bilocali/trilocali in vendita sotto 140k a Torino Sud
e nei comuni Moncalieri/Nichelino/Beinasco, ristrutturati.

Stato persistente salvato come messaggio PINNED nella chat Telegram
(non serve filesystem persistente).

Segreti letti da variabili d'ambiente (configurati in GitHub Secrets):
  TELEGRAM_TOKEN
  TELEGRAM_CHAT_ID
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

# ============================================================
# CONFIGURAZIONE
# ============================================================

TOKEN = os.environ["TELEGRAM_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

PREZZO_MAX = 140_000
LOCALI_MIN = 2
LOCALI_MAX = 3
MAX_PAGES = 40
MAX_STATE_IDS = 500  # mantiene gli ultimi 500 ID per non superare i 4096 char Telegram

MACROZONE_TORINO_SUD = {
    "Lingotto, Nizza Millefonti",
    "Mirafiori Nord, Santa Rita",
    "Mirafiori Sud",
    "Filadelfia",
}
COMUNI_CINTURA_SUD = {"Moncalieri", "Nichelino", "Beinasco"}

RIS_RE = re.compile(
    r"\b(ristruttura(?:to|ta|zione)|ottime\s+condiz|finiture\s+nuove|nuova\s+ristruttur)",
    re.IGNORECASE,
)
DA_RIS_RE = re.compile(
    r"\b(da\s+ristruttura(?:re|zione)|necessita\s+(?:di\s+)?ristruttura)",
    re.IGNORECASE,
)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
    "Referer": "https://www.immobiliare.it/vendita-case/torino-provincia/",
}
API = "https://www.immobiliare.it/api-next/search-list/listings/"
STATE_PREFIX = "BOT_STATE_V1:"


# ============================================================
# HTTP
# ============================================================

def http_json(url, data=None, method=None, timeout=20, extra_headers=None):
    headers = dict(HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    last_err = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 + attempt * 2)
    print(f"[ERR] HTTP {url[:120]} -> {last_err}", file=sys.stderr)
    return None


def tg(method, **params):
    url = f"https://api.telegram.org/bot{TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        print(f"[ERR] TG {method}: {e}", file=sys.stderr)
        return {"ok": False, "error": str(e)}


# ============================================================
# STATE (Telegram pinned message)
# ============================================================

def load_state():
    r = http_json(f"https://api.telegram.org/bot{TOKEN}/getChat?chat_id={CHAT_ID}")
    if not r or not r.get("ok"):
        return set(), None
    pinned = (r.get("result") or {}).get("pinned_message") or {}
    text = pinned.get("text", "")
    if text.startswith(STATE_PREFIX):
        try:
            d = json.loads(text[len(STATE_PREFIX):])
            return set(d.get("seen_ids", [])), pinned.get("message_id")
        except Exception:  # noqa: BLE001
            pass
    return set(), None


def save_state(seen, msg_id):
    ids = sorted(seen, key=lambda x: int(x) if x.isdigit() else 0)[-MAX_STATE_IDS:]
    text = STATE_PREFIX + json.dumps({"seen_ids": ids})
    if msg_id:
        r = tg("editMessageText", chat_id=CHAT_ID, message_id=msg_id, text=text)
        if r.get("ok"):
            return msg_id
    r = tg("sendMessage", chat_id=CHAT_ID, text=text, disable_notification="true")
    new_id = (r.get("result") or {}).get("message_id")
    if new_id:
        tg("pinChatMessage", chat_id=CHAT_ID, message_id=new_id, disable_notification="true")
    return new_id


# ============================================================
# SCRAPING
# ============================================================

def fetch_page(page):
    qs = urllib.parse.urlencode({
        "idContratto": 1, "idCategoria": 1,
        "fkRegione": "pmn", "idProvincia": "TO",
        "prezzoMassimo": PREZZO_MAX,
        "localiMinimo": LOCALI_MIN, "localiMassimo": LOCALI_MAX,
        "criterio": "data", "ordine": "desc",
        "pag": page, "paramsCount": 5,
        "path": "/vendita-case/torino-provincia/",
        "__lang": "it",
    })
    return http_json(API + "?" + qs)


def in_zone(loc):
    city = (loc.get("city") or "").strip()
    macro = (loc.get("macrozone") or "").strip()
    if city == "Torino" and macro in MACROZONE_TORINO_SUD:
        return True, f"Torino - {macro}"
    if city in COMUNI_CINTURA_SUD:
        return True, city
    return False, ""


def is_ristrutturato(ann):
    p = ann["realEstate"]["properties"][0]
    texts = [
        ann["realEstate"].get("title", "") or "",
        p.get("description", "") or "",
        p.get("caption", "") or "",
        " ".join(f.get("label", "") for f in (p.get("featureList") or []) if isinstance(f, dict)),
    ]
    t = " ".join(texts)
    if DA_RIS_RE.search(t):
        return False
    return bool(RIS_RE.search(t))


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt(ann, zona):
    r = ann["realEstate"]
    p = r["properties"][0]
    aid = str(r.get("id"))
    price = r.get("price", {}).get("value")
    price_s = f"€ {price:,}".replace(",", ".") if price else "n/d"
    floor = (p.get("floor") or {}).get("value") or "n/d"
    adv = (r.get("advertiser") or {}).get("agency") or {}
    agency = adv.get("displayName") or "Privato"
    msg = (
        f"🏠 <b>{esc(r.get('title','')[:90])}</b>\n"
        f"💰 <b>{esc(price_s)}</b> · {esc(p.get('rooms','n/d'))} loc. · "
        f"{esc(p.get('surface','n/d'))} · piano {esc(floor)} · "
        f"{esc(p.get('bathrooms','n/d'))} bagni\n"
        f"📍 {esc(zona)}\n"
        f"🏢 {esc(agency)}\n"
        f"🔗 <a href=\"https://www.immobiliare.it/annunci/{aid}/\">Vedi annuncio</a>"
    )
    return aid, msg


# ============================================================
# MAIN
# ============================================================

def main():
    print(f"[INFO] Start {time.strftime('%Y-%m-%d %H:%M:%S')}")
    seen, state_msg = load_state()
    print(f"[INFO] State: {len(seen)} ids già visti, pinned msg_id={state_msg}")

    matched = []
    total = 0
    for page in range(1, MAX_PAGES + 1):
        d = fetch_page(page)
        if not d:
            print(f"[WARN] Pagina {page} fallita, stop.")
            break
        results = d.get("results", [])
        if not results:
            break
        total += len(results)
        for r in results:
            try:
                loc = r["realEstate"]["properties"][0].get("location", {})
            except (KeyError, IndexError):
                continue
            ok, zona = in_zone(loc)
            if not ok or not is_ristrutturato(r):
                continue
            aid, msg = fmt(r, zona)
            if aid in seen:
                continue
            matched.append((aid, msg))
        if len(results) < 25:
            break

    print(f"[INFO] Scansionati {total}, nuovi match {len(matched)}")

    if matched:
        header = (
            f"🔔 <b>{len(matched)} nuovi annunci</b> a Torino Sud / cintura sud\n"
            f"(bi/trilocali · max € {PREZZO_MAX:,}".replace(",", ".") + " · ristrutturati)"
        )
        tg("sendMessage", chat_id=CHAT_ID, text=header, parse_mode="HTML")
        for aid, msg in matched:
            r = tg("sendMessage", chat_id=CHAT_ID, text=msg, parse_mode="HTML")
            if r.get("ok"):
                seen.add(aid)
                time.sleep(0.5)
        save_state(seen, state_msg)
    else:
        tg(
            "sendMessage",
            chat_id=CHAT_ID,
            text=f"✅ Scan {time.strftime('%d/%m %H:%M')} – {total} annunci visti, nessun nuovo match.",
        )

    print(f"[INFO] End {time.strftime('%Y-%m-%d %H:%M:%S')}")


if __name__ == "__main__":
    main()
