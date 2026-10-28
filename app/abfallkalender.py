"""Abfallkalender-Melder: liest den Abfallkalender (PDF) der Stadt Rietberg und
meldet am Vortag zur eingestellten Uhrzeit, welche Tonne raus muss – per SMTP
und/oder Pushover. Konfiguration über die Weboberfläche, gespeichert in DATA_DIR."""

import argparse
import html
import json
import os
import re
import smtplib
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from email import message_from_bytes
from email.message import EmailMessage
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pdf_parser import TONNEN, lies_pdf, passt

WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
FARBEN = {"restmuell": "#3f3f46", "bio": "#16a34a", "saisonbio": "#7c4a1e", "gelb": "#eab308", "papier": "#2563eb"}

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data"))
CONFIG_FILE = DATA_DIR / "config.json"
STATE_FILE = DATA_DIR / "status.json"
KALENDER_FILE = DATA_DIR / "kalender.json"
PORT = int(os.environ.get("PORT", "8080"))
ICON_FILE = Path(__file__).with_name("icon.png")
AKTUALISIEREN_NACH = timedelta(days=7)  # PDF regelmäßig neu laden, falls die Stadt nachbessert

STANDARD = {
    "pdf_url": "https://www.rietberg.de/fileadmin/user_upload/RATHAUS/abfallentsorgung/Abfallkalender_Rietberg_2026.pdf",
    "bezirk": "", "strasse": "", "tonnen": list(TONNEN),
    "melde_uhrzeit": "18:00", "zeitzone": "Europe/Berlin", "kanaele": [],
    "pushover_token": "", "pushover_user": "",
    "smtp_host": "", "smtp_port": 587, "smtp_sicherheit": "starttls",
    "smtp_user": "", "smtp_passwort": "", "smtp_from": "", "smtp_to": "",
}
GEHEIM = ("pushover_token", "smtp_passwort")

sperre = threading.Lock()


# ---------- Speicher ----------

def lade_json(pfad, standard):
    try:
        return {**standard, **json.loads(pfad.read_text())}
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(standard)


def speichere_json(pfad, daten):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = pfad.with_suffix(".tmp")
    tmp.write_text(json.dumps(daten, ensure_ascii=False, indent=2))
    tmp.replace(pfad)


def lade_config():
    return lade_json(CONFIG_FILE, STANDARD)


def lade_status():
    return lade_json(STATE_FILE, {
        "gemeldet": [], "letzte_pruefung": None, "letztes_ergebnis": "",
        "letzter_abruf_versuch": None, "abruf_fehler": "", "ablauf_gemeldet": None,
    })


def lade_kalender():
    return lade_json(KALENDER_FILE, {"url": "", "abgerufen": None, "jahr": None, "termine": [], "strassen": {}})


def jetzt(cfg):
    try:
        return datetime.now(ZoneInfo(cfg["zeitzone"]))
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now(ZoneInfo("Europe/Berlin"))


# ---------- Kalender ----------

def aktualisiere_kalender(url):
    """Lädt das PDF, liest es aus und speichert das Ergebnis. Bei Fehlern bleibt der alte Stand."""
    status = lade_status()
    status["letzter_abruf_versuch"] = datetime.now().isoformat(timespec="seconds")
    try:
        anfrage = urllib.request.Request(url, headers={"User-Agent": "abfallkalender-melder"})
        with urllib.request.urlopen(anfrage, timeout=60) as r, tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(r.read(50_000_000))
            tmp.flush()
            daten = lies_pdf(tmp.name)
    except Exception as e:
        status["abruf_fehler"] = f"PDF konnte nicht gelesen werden: {e}"
        speichere_json(STATE_FILE, status)
        log(status["abruf_fehler"])
        raise RuntimeError(status["abruf_fehler"]) from e
    daten.update(url=url, abgerufen=datetime.now().isoformat(timespec="seconds"))
    speichere_json(KALENDER_FILE, daten)
    status["abruf_fehler"] = ""
    speichere_json(STATE_FILE, status)
    log(f"Kalender {daten['jahr']} geladen: {len(daten['termine'])} Termine, {len(daten['strassen'])} Straßen")
    return daten


def kalender_veraltet(cfg, kal, status):
    if not cfg["pdf_url"]:
        return False
    versuch = status["letzter_abruf_versuch"]
    if versuch and datetime.now() - datetime.fromisoformat(versuch) < timedelta(hours=1):
        return False  # nach Fehlschlag nicht im 30-Sekunden-Takt erneut laden
    if kal["url"] != cfg["pdf_url"] or not kal["abgerufen"]:
        return True
    return datetime.now() - datetime.fromisoformat(kal["abgerufen"]) > AKTUALISIEREN_NACH


def termine_fuer(cfg, kal):
    """{datum: [termin, ...]} für Bezirk und gewählte Tonnen."""
    treffer = {}
    if not cfg["bezirk"]:
        return treffer
    for t in kal["termine"]:
        if t["art"] in cfg["tonnen"] and passt(t, cfg["bezirk"]):
            treffer.setdefault(date.fromisoformat(t["datum"]), []).append(t)
    return dict(sorted(treffer.items()))


def schluessel(tag, art):
    return f"{tag.isoformat()}|{art}"


def tonnen_text(termine):
    return ", ".join(TONNEN[t["art"]][0] for t in termine)


def formatiere(tag, termine, cfg):
    titel = "Morgen: " + " + ".join(TONNEN[t["art"]][0].split(" (")[0] for t in termine)
    zeilen = [f"{WOCHENTAGE[tag.weekday()]}, {tag:%d.%m.%Y} – Bezirk {cfg['bezirk']}"]
    zeilen += [f"• {TONNEN[t['art']][0]}" for t in termine]
    if any(t["verschoben"] for t in termine):
        zeilen.append("(verschobener Termin, z. B. wegen Feiertag)")
    zeilen.append("Bitte bis morgen früh an die Straße stellen.")
    return titel, "\n".join(zeilen)


def finde_bezirk(eingabe, strassen):
    """Akzeptiert „1.2“, „Gartenstraße (1.2)“ oder einen Straßennamen aus dem PDF."""
    if m := re.search(r"\b([1-9]\.[1-9])\b", eingabe):
        return m.group(1)
    suche = eingabe.lower().replace("str.", "straße").strip()
    for name, bezirk in strassen.items():
        if name.lower() == suche:
            return bezirk
    kandidaten = {b for n, b in strassen.items() if n.lower().startswith(suche)}
    if len(kandidaten) == 1:
        return kandidaten.pop()
    raise ValueError(f"Bezirk zu „{eingabe}“ nicht eindeutig gefunden – bitte z. B. 1.2 eingeben")


# ---------- Versand ----------

def sende_pushover(cfg, titel, text):
    if not (cfg["pushover_token"] and cfg["pushover_user"]):
        raise RuntimeError("Pushover-Token oder User-Key fehlt")
    daten = urllib.parse.urlencode({
        "token": cfg["pushover_token"], "user": cfg["pushover_user"],
        "title": titel, "message": text,
    }).encode()
    with urllib.request.urlopen("https://api.pushover.net/1/messages.json", data=daten, timeout=30) as r:
        if r.status != 200:
            raise RuntimeError(f"Pushover antwortete mit HTTP {r.status}")


def sende_smtp(cfg, titel, text):
    if not (cfg["smtp_host"] and cfg["smtp_to"]):
        raise RuntimeError("SMTP-Server oder Empfänger fehlt")
    msg = EmailMessage()
    msg["Subject"] = titel
    msg["From"] = cfg["smtp_from"] or cfg["smtp_user"]
    msg["To"] = cfg["smtp_to"]
    msg.set_content(text)

    port = int(cfg["smtp_port"])
    if cfg["smtp_sicherheit"] == "ssl":
        server = smtplib.SMTP_SSL(cfg["smtp_host"], port, timeout=30)
    else:
        server = smtplib.SMTP(cfg["smtp_host"], port, timeout=30)
    with server:
        if cfg["smtp_sicherheit"] == "starttls":
            server.starttls()
        if cfg["smtp_user"]:
            server.login(cfg["smtp_user"], cfg["smtp_passwort"])
        server.send_message(msg)


SENDER = {"pushover": sende_pushover, "smtp": sende_smtp}


def sende(cfg, titel, text):
    """Sendet über alle aktiven Kanäle. Liefert (erfolg, fehlerliste)."""
    if not cfg["kanaele"]:
        return False, ["Kein Kanal aktiviert"]
    erfolg, fehler = False, []
    for kanal in cfg["kanaele"]:
        try:
            SENDER[kanal](cfg, titel, text)
            log(f"Gesendet via {kanal}: {titel}")
            erfolg = True
        except Exception as e:  # ein Kanal-Fehler soll die anderen nicht blockieren
            fehler.append(f"{kanal}: {e}")
            log(f"Fehler beim Senden via {kanal}: {e}")
    return erfolg, fehler


# ---------- Prüfung ----------

def log(text):
    print(f"{jetzt(lade_config()):%Y-%m-%d %H:%M:%S} {text}", flush=True)


def pruefe(trocken=False, heute=None):
    with sperre:
        cfg, status, kal = lade_config(), lade_status(), lade_kalender()
        heute = heute or jetzt(cfg).date()
        morgen = heute + timedelta(days=1)
        gemeldet = set(status["gemeldet"])

        offen = [t for t in termine_fuer(cfg, kal).get(morgen, []) if schluessel(morgen, t["art"]) not in gemeldet]
        if not offen:
            ergebnis = "Morgen keine Abfuhr."
        else:
            titel, text = formatiere(morgen, offen, cfg)
            if trocken:
                print(f"[Testlauf] {titel}\n{text}")
                return
            erfolg, fehler = sende(cfg, titel, text)
            if erfolg:
                gemeldet.update(schluessel(morgen, t["art"]) for t in offen)
            ergebnis = f"{titel} gemeldet." if erfolg else "Senden fehlgeschlagen"
            if fehler:
                ergebnis += " Fehler: " + "; ".join(fehler)

        # Einmalig erinnern, wenn der Kalender ausläuft und ein neues PDF nötig ist
        letzter = max((t["datum"] for t in kal["termine"]), default=None)
        if (not trocken and letzter and kal["jahr"] != status["ablauf_gemeldet"]
                and date.fromisoformat(letzter) - heute <= timedelta(days=14)):
            ok, _ = sende(cfg, "Abfallkalender läuft aus",
                          f"Der Abfallkalender {kal['jahr']} endet am {date.fromisoformat(letzter):%d.%m.%Y}. "
                          "Bitte in der Weboberfläche den Link zum neuen PDF eintragen.")
            if ok:
                status["ablauf_gemeldet"] = kal["jahr"]

        log(ergebnis)
        if trocken:
            return
        status.update(
            gemeldet=sorted(k for k in gemeldet if k[:10] >= heute.isoformat()),
            letzte_pruefung=jetzt(cfg).strftime("%Y-%m-%d %H:%M"),
            letztes_ergebnis=ergebnis,
        )
        speichere_json(STATE_FILE, status)


def zeitplaner():
    while True:
        try:
            cfg, status, kal = lade_config(), lade_status(), lade_kalender()
            if kalender_veraltet(cfg, kal, status):
                try:
                    aktualisiere_kalender(cfg["pdf_url"])
                except RuntimeError:
                    pass  # schon geloggt und im Status vermerkt
            n = jetzt(cfg)
            faellig = n.strftime("%H:%M") >= cfg["melde_uhrzeit"]
            heute_geprueft = (status["letzte_pruefung"] or "").startswith(n.date().isoformat())
            if cfg["bezirk"] and cfg["kanaele"] and faellig and not heute_geprueft:
                pruefe()
        except Exception as e:
            log(f"Fehler im Zeitplaner: {e}")
        time.sleep(30)


# ---------- Weboberfläche ----------

SEITE = """<!doctype html>
<html lang="de"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Abfallkalender-Melder</title>
<link rel="icon" type="image/png" href="/icon.png">
<style>
:root{{--bg:#f5f6f8;--card:#fff;--fg:#1d2330;--mut:#667085;--line:#e3e6eb;--acc:#2563eb;--ok:#15803d;--err:#b91c1c}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151b;--card:#1b1f27;--fg:#e6e8ec;--mut:#98a1b3;--line:#2c323d;--acc:#60a5fa;--ok:#4ade80;--err:#f87171}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif}}
main{{max-width:760px;margin:0 auto;padding:24px 16px}}h1{{font-size:22px;margin:0 0 4px}}h2{{font-size:16px;margin:0 0 12px}}
.mut{{color:var(--mut);font-size:13px}}.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin:16px 0}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:6px 12px}}
label{{display:block}}.feld{{margin:10px 0}}.feld>span{{display:block;font-size:13px;color:var(--mut);margin-bottom:3px}}
input[type=text],input[type=url],input[type=number],input[type=time],input[type=password],select{{width:100%;padding:8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}}
.zeile{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:0 12px}}
button{{padding:9px 16px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--fg);font:inherit;cursor:pointer}}
button.prim{{background:var(--acc);border-color:var(--acc);color:#fff}}.knoepfe{{display:flex;flex-wrap:wrap;gap:8px}}
.hinweis{{padding:10px 14px;border-radius:8px;border:1px solid var(--line);background:var(--card)}}
.ok{{color:var(--ok)}}.err{{color:var(--err)}}ul{{margin:0;padding-left:18px}}li{{margin:4px 0}}
.tonne{{display:inline-block;width:10px;height:10px;border-radius:2px;margin:0 4px 0 2px;vertical-align:baseline}}
</style></head><body><main>
<h1>Abfallkalender-Melder</h1>
<div class="mut">Meldet am Vortag um {melde_uhrzeit} Uhr, welche Tonne raus muss. Letzte Prüfung: {letzte} – {ergebnis}</div>
{meldung}
<form method="post" action="/speichern">
<div class="card"><h2>Abfallkalender</h2>
<label class="feld"><span>Link zum PDF</span><input type="url" name="pdf_url" value="{pdf_url}" required></label>
<div class="mut">{kalender_info}</div>
<label class="feld"><span>Straße oder Bezirk (z. B. „Gartenstraße“ oder „1.2“)</span>
<input type="text" name="bezirk" value="{bezirk_wert}" list="strassen" autocomplete="off"></label>
<datalist id="strassen">{strassen}</datalist>
<div class="feld"><span class="mut">Melden für</span><div class="grid">{tonnen}</div></div>
</div>
<div class="card"><h2>Zeitplan</h2><div class="zeile">
<label class="feld"><span>Meldung am Vortag um</span><input type="time" name="melde_uhrzeit" value="{melde_uhrzeit}"></label>
<label class="feld"><span>Zeitzone</span><input type="text" name="zeitzone" value="{zeitzone}"></label>
</div></div>
<div class="card"><h2>Pushover</h2>
<label><input type="checkbox" name="kanaele" value="pushover" {pushover_an}> Pushover aktiv</label>
<div class="zeile">
<label class="feld"><span>API-Token</span><input type="password" name="pushover_token" placeholder="{pushover_token_ph}" autocomplete="off"></label>
<label class="feld"><span>User-Key</span><input type="text" name="pushover_user" value="{pushover_user}"></label>
</div></div>
<div class="card"><h2>E-Mail (SMTP)</h2>
<label><input type="checkbox" name="kanaele" value="smtp" {smtp_an}> E-Mail aktiv</label>
<div class="zeile">
<label class="feld"><span>Server</span><input type="text" name="smtp_host" value="{smtp_host}"></label>
<label class="feld"><span>Port</span><input type="number" name="smtp_port" value="{smtp_port}"></label>
<label class="feld"><span>Verschlüsselung</span><select name="smtp_sicherheit">{sicherheit}</select></label>
</div><div class="zeile">
<label class="feld"><span>Benutzer</span><input type="text" name="smtp_user" value="{smtp_user}" autocomplete="off"></label>
<label class="feld"><span>Passwort</span><input type="password" name="smtp_passwort" placeholder="{smtp_passwort_ph}" autocomplete="new-password"></label>
</div><div class="zeile">
<label class="feld"><span>Absender</span><input type="text" name="smtp_from" value="{smtp_from}"></label>
<label class="feld"><span>Empfänger (kommagetrennt)</span><input type="text" name="smtp_to" value="{smtp_to}"></label>
</div></div>
<div class="knoepfe"><button class="prim" type="submit">Speichern</button>
<button type="submit" formaction="/test">Testnachricht senden</button>
<button type="submit" formaction="/jetzt">Jetzt prüfen &amp; melden</button>
<button type="submit" formaction="/neu_laden">PDF neu laden</button>
<button type="submit" formaction="/zuruecksetzen">Gemeldete zurücksetzen</button></div>
</form>
<div class="card"><h2>Nächste Abfuhrtermine</h2>{vorschau}</div>
<div class="card"><h2>Einstellungen sichern &amp; übertragen</h2>
<form method="get" action="/export" class="feld">
<label><input type="checkbox" name="geheim" value="1" checked> Zugangsdaten mitexportieren (API-Token, SMTP-Passwort)</label>
<div class="mut">Die Datei enthält sie dann im Klartext. Gut aufbewahren, nicht weitergeben.</div>
<div class="knoepfe" style="margin-top:8px"><button type="submit">Exportieren</button></div>
</form>
<form method="post" action="/import" enctype="multipart/form-data" class="feld">
<input type="file" name="datei" accept=".json,application/json" required>
<div class="mut">Fehlen in der Datei Zugangsdaten, bleiben die gespeicherten erhalten. Die Liste der gemeldeten Termine wird nicht übertragen.</div>
<div class="knoepfe" style="margin-top:8px"><button type="submit">Importieren</button></div>
</form></div>
</main></body></html>"""


def e(wert):
    return html.escape(str(wert), quote=True)


def wann(tag, heute):
    tage = (tag - heute).days
    return "heute" if tage == 0 else "morgen" if tage == 1 else f"in {tage} Tagen"


def rendere(meldung=""):
    cfg, status, kal = lade_config(), lade_status(), lade_kalender()
    heute = jetzt(cfg).date()
    gemeldet = set(status["gemeldet"])

    if kal["abgerufen"]:
        kalender_info = e(f"Kalender {kal['jahr']}: {len(kal['termine'])} Termine, {len(kal['strassen'])} Straßen – "
                          f"geladen {kal['abgerufen'].replace('T', ' ')[:16]}")
    else:
        kalender_info = "Noch nicht geladen."
    if status["abruf_fehler"]:
        kalender_info += f'<br><span class="err">{e(status["abruf_fehler"])}</span>'

    tonnen = "".join(
        f'<label><input type="checkbox" name="tonnen" value="{k}" {"checked" if k in cfg["tonnen"] else ""}>'
        f'<span class="tonne" style="background:{FARBEN[k]}"></span>{e(name)}</label>'
        for k, (name, _) in TONNEN.items()
    )
    strassen = "".join(f'<option value="{e(n)} ({e(b)})">' for n, b in kal["strassen"].items())
    sicherheit = "".join(
        f'<option value="{w}" {"selected" if cfg["smtp_sicherheit"] == w else ""}>{t}</option>'
        for w, t in (("starttls", "STARTTLS (587)"), ("ssl", "SSL/TLS (465)"), ("none", "keine"))
    )

    if not cfg["bezirk"]:
        vorschau = '<div class="mut">Noch kein Bezirk gewählt.</div>'
    else:
        punkte = []
        for tag, termine in termine_fuer(cfg, kal).items():
            if tag < heute or len(punkte) >= 10:
                continue
            erledigt = all(schluessel(tag, t["art"]) in gemeldet for t in termine)
            marke = ' <span class="ok">✓ gemeldet</span>' if erledigt else ""
            arten = " ".join(
                f'<span class="tonne" style="background:{FARBEN[t["art"]]}"></span>{e(TONNEN[t["art"]][0])}'
                + (" <b>!</b>" if t["verschoben"] else "") for t in termine)
            punkte.append(f"<li><b>{WOCHENTAGE[tag.weekday()]}, {tag:%d.%m.%Y}</b> ({wann(tag, heute)}){marke}<br>{arten}</li>")
        vorschau = f"<ul>{''.join(punkte)}</ul>" if punkte else '<div class="mut">Keine weiteren Termine im Kalender – neuen PDF-Link eintragen.</div>'

    if meldung:
        klasse = "err" if meldung.startswith("Fehler") else "ok"
        meldung = f'<div class="card hinweis {klasse}">{e(meldung)}</div>'

    bezirk_wert = f"{cfg['strasse']} ({cfg['bezirk']})" if cfg["strasse"] else cfg["bezirk"]
    return SEITE.format(
        meldung=meldung, tonnen=tonnen, strassen=strassen, sicherheit=sicherheit, vorschau=vorschau,
        kalender_info=kalender_info, bezirk_wert=e(bezirk_wert),
        letzte=e(status["letzte_pruefung"] or "noch nie"), ergebnis=e(status["letztes_ergebnis"] or "–"),
        pdf_url=e(cfg["pdf_url"]), melde_uhrzeit=e(cfg["melde_uhrzeit"]), zeitzone=e(cfg["zeitzone"]),
        pushover_an="checked" if "pushover" in cfg["kanaele"] else "",
        smtp_an="checked" if "smtp" in cfg["kanaele"] else "",
        pushover_token_ph="gespeichert – leer lassen zum Behalten" if cfg["pushover_token"] else "",
        smtp_passwort_ph="gespeichert – leer lassen zum Behalten" if cfg["smtp_passwort"] else "",
        **{k: e(cfg[k]) for k in ("pushover_user", "smtp_host", "smtp_port", "smtp_user", "smtp_from", "smtp_to")},
    )


def speichere_formular(form):
    """Speichert das Formular. Liefert True, wenn sich der PDF-Link geändert hat."""
    cfg = lade_config()
    feld = lambda k: form.get(k, [""])[0].strip()

    uhrzeit = feld("melde_uhrzeit") or "18:00"
    datetime.strptime(uhrzeit, "%H:%M")
    zeitzone = feld("zeitzone") or "Europe/Berlin"
    ZoneInfo(zeitzone)
    url = feld("pdf_url") or STANDARD["pdf_url"]
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        raise ValueError("PDF-Link muss mit http:// oder https:// beginnen")

    eingabe = feld("bezirk")
    strassen = lade_kalender()["strassen"]
    bezirk = finde_bezirk(eingabe, strassen) if eingabe else ""
    # Straßennamen nur merken, wenn er laut PDF zum Bezirk gehört (reine Anzeige)
    name = re.sub(r"\(?\b[1-9]\.[1-9]\b\)?", "", eingabe).strip().lower().replace("str.", "straße")
    strasse = next((n for n, b in strassen.items() if n.lower() == name and b == bezirk), "")

    cfg.update(
        pdf_url=url, bezirk=bezirk, strasse=strasse,
        tonnen=[t for t in form.get("tonnen", []) if t in TONNEN],
        kanaele=[k for k in form.get("kanaele", []) if k in SENDER],
        melde_uhrzeit=uhrzeit, zeitzone=zeitzone,
        smtp_port=int(feld("smtp_port") or 587),
        smtp_sicherheit=feld("smtp_sicherheit") if feld("smtp_sicherheit") in ("starttls", "ssl", "none") else "starttls",
        **{k: feld(k) for k in ("pushover_user", "smtp_host", "smtp_user", "smtp_from", "smtp_to")},
    )
    for k in GEHEIM:  # leeres Passwortfeld = gespeicherten Wert behalten
        if feld(k):
            cfg[k] = feld(k)
    with sperre:
        speichere_json(CONFIG_FILE, cfg)
    return lade_kalender()["url"] != url


def exportiere(mit_geheim):
    cfg = {k: lade_config()[k] for k in STANDARD}
    if not mit_geheim:
        for k in GEHEIM:
            cfg[k] = ""
    return json.dumps({"app": "abfallkalender-melder", **cfg}, ensure_ascii=False, indent=2)


def importiere(rumpf, typ):
    teile = message_from_bytes(f"Content-Type: {typ}\r\n\r\n".encode() + rumpf)
    teile = teile.get_payload() if teile.is_multipart() else []
    datei = next((t for t in teile if t.get_param("name", header="content-disposition") == "datei"), None)
    if datei is None:
        raise ValueError("keine Datei erhalten")
    try:
        daten = json.loads(datei.get_payload(decode=True).decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("Datei ist kein gültiges JSON")
    if not isinstance(daten, dict) or "pdf_url" not in daten:
        raise ValueError("keine Abfallkalender-Melder-Konfiguration")
    # Wie ein abgeschicktes Formular behandeln, damit dieselben Prüfungen greifen
    form = {}
    for k in STANDARD:
        wert = daten.get(k)
        if isinstance(wert, list):
            form[k] = [str(w) for w in wert]
        elif wert is not None:
            form[k] = [str(wert)]
    if daten.get("strasse") and daten.get("bezirk"):
        form["bezirk"] = [f"{daten['strasse']} ({daten['bezirk']})"]
    return speichere_formular(form)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def antworte(self, inhalt, typ="text/html; charset=utf-8", dateiname=None):
        daten = inhalt.encode() if isinstance(inhalt, str) else inhalt
        self.send_response(200)
        self.send_header("Content-Type", typ)
        if dateiname:
            self.send_header("Content-Disposition", f'attachment; filename="{dateiname}"')
            self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(daten)))
        self.end_headers()
        self.wfile.write(daten)

    def weiter(self, meldung):
        self.send_response(303)
        self.send_header("Location", "/?" + urllib.parse.urlencode({"m": meldung}))
        self.end_headers()

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/icon.png" and ICON_FILE.exists():
            self.antworte(ICON_FILE.read_bytes(), "image/png")
            return
        if url.path == "/export":
            mit_geheim = urllib.parse.parse_qs(url.query).get("geheim") == ["1"]
            name = f"abfallkalender-melder-{date.today():%Y-%m-%d}.json"
            self.antworte(exportiere(mit_geheim), "application/json; charset=utf-8", name)
            return
        if url.path != "/":
            self.send_error(404)
            return
        self.antworte(rendere(urllib.parse.parse_qs(url.query).get("m", [""])[0]))

    def do_POST(self):
        laenge = int(self.headers.get("Content-Length", 0))
        if laenge > 1_000_000:
            self.send_error(413)
            return
        rumpf = self.rfile.read(laenge)
        try:
            if self.path == "/import":
                neu_laden = importiere(rumpf, self.headers.get("Content-Type", ""))
                meldung = "Einstellungen importiert."
            else:
                neu_laden = speichere_formular(urllib.parse.parse_qs(rumpf.decode(), keep_blank_values=True))
                meldung = "Gespeichert."
            if neu_laden or self.path == "/neu_laden":
                kal = aktualisiere_kalender(lade_config()["pdf_url"])
                meldung += f" Kalender {kal['jahr']} geladen ({len(kal['termine'])} Termine)."

            if self.path in ("/speichern", "/import", "/neu_laden"):
                self.weiter(meldung)
            elif self.path == "/test":
                ok, fehler = sende(lade_config(), "Abfallkalender-Melder Test", "Wenn du das liest, funktioniert der Versand. 🗑️")
                self.weiter("Testnachricht gesendet." if ok and not fehler else "Fehler: " + "; ".join(fehler))
            elif self.path == "/jetzt":
                pruefe()
                self.weiter(lade_status()["letztes_ergebnis"])
            elif self.path == "/zuruecksetzen":
                with sperre:
                    status = lade_status()
                    status["gemeldet"] = []
                    speichere_json(STATE_FILE, status)
                self.weiter("Gemeldete Termine zurückgesetzt – sie werden bei der nächsten Prüfung erneut gesendet.")
            else:
                self.send_error(404)
        except RuntimeError as fehler:
            self.weiter(f"Fehler: {fehler}")
        except (ValueError, ZoneInfoNotFoundError) as fehler:
            self.weiter(f"Fehler: Ungültige Eingabe ({fehler})")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--trocken", action="store_true", help="anstehende Meldung nur ausgeben, nichts senden")
    p.add_argument("--datum", type=date.fromisoformat, help="mit --trocken: so tun, als wäre heute dieses Datum (JJJJ-MM-TT)")
    args = p.parse_args()

    if args.trocken:
        cfg, kal = lade_config(), lade_kalender()
        if kal["url"] != cfg["pdf_url"] or not kal["termine"]:
            aktualisiere_kalender(cfg["pdf_url"])
        pruefe(trocken=True, heute=args.datum)
        return

    threading.Thread(target=zeitplaner, daemon=True).start()
    log(f"Abfallkalender-Melder läuft – Weboberfläche auf Port {PORT}, Daten in {DATA_DIR.resolve()}")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
