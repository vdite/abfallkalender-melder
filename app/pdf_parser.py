"""Liest Abfuhrtermine und Straßen→Bezirk-Zuordnung aus dem Abfallkalender-PDF
der Stadt Rietberg. Die Tonnenart steckt nur in der Farbe des Kästchens rechts
neben der Bezirksangabe (z. B. „1+3“ ▮ grau = Restmüll für Bezirk 1 und 3)."""

import re
from datetime import date

import pdfplumber

MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]
WOCHENTAGE = {"Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"}

# Referenzfarben (RGB 0..1) der Legende; zugeordnet wird die nächstliegende
TONNEN = {
    "restmuell": ("Restmüll (schwarze Tonne)", (0.80, 0.80, 0.80)),
    "bio": ("Bio (grüne Tonne)", (0.50, 0.81, 0.50)),
    "saisonbio": ("Saison-Biotonne", (0.44, 0.19, 0.07)),
    "gelb": ("Gelbe Tonne", (1.00, 0.95, 0.50)),
    "papier": ("Papier (blaue Tonne)", (0.50, 0.63, 1.00)),
}
SONSTIGE = [(1.0, 0.0, 0.0), (0.79, 0.88, 1.0), (0.9, 0.9, 0.9), (1.0, 1.0, 1.0)]  # Schadstoff-S, Ferien, Zeilen
# Bezirksangabe am Wortende, ggf. an Text geklebt („Heiligabend2+4“) oder mit „!“ für Verschiebung
LABEL = re.compile(r"(?:^|(?<=[^\d.+!]))!?(\d(?:\.\d)?(?:\+\d(?:\.\d)?)*)!?$")
STRASSE = re.compile(r"([A-ZÄÖÜa-z][^\n]*?)\s*\.{3,}\s*(\d\.\d)(?!\d)")


def rgb(farbe):
    if not farbe:
        return None
    if len(farbe) == 4:
        c, m, y, k = farbe
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    if len(farbe) == 3:
        return tuple(farbe)
    if len(farbe) == 1:
        return (farbe[0],) * 3
    return None


def tonnenart(farbe):
    f = rgb(farbe)
    if f is None:
        return None
    abstand = lambda ref: sum((a - b) ** 2 for a, b in zip(f, ref))
    art, ref = min(((k, v[1]) for k, v in TONNEN.items()), key=lambda kv: abstand(kv[1]))
    if min(abstand(s) for s in SONSTIGE) < abstand(ref):
        return None
    return art if abstand(ref) < 0.05 else None


def auf_seite(obj, seite):
    return 0 <= obj["x0"] <= seite.width and 0 <= obj["top"] <= seite.height


def lies_seite(seite, jahr):
    woerter = [w for w in seite.extract_words() if auf_seite(w, seite)]
    kandidaten = [w for w in woerter if w["text"] in MONATE]
    if not kandidaten:
        return []
    oben = min(w["top"] for w in kandidaten)  # nur die Spaltenköpfe, nicht „03. Januar“ im Fließtext
    spalten = [(MONATE.index(w["text"]) + 1, w["x0"]) for w in kandidaten if w["top"] - oben < 3]
    if len(spalten) < 2:
        return []
    spalten.sort(key=lambda s: s[1])
    breite = min(b[1] - a[1] for a, b in zip(spalten, spalten[1:]))

    def spalte(x):
        for monat, x0 in spalten:
            if x0 - 8 <= x < x0 - 8 + breite:
                return monat, x0
        return None, None

    # Tageszeilen: Tageszahl am linken Spaltenrand, direkt gefolgt vom Wochentag
    # (Ziffern können getrennt extrahiert werden: „3 1“ = 31)
    zeilen = {}
    for wt in woerter:
        monat, x0 = spalte(wt["x0"])
        if not monat or wt["text"] not in WOCHENTAGE or wt["x0"] > x0 + 25:
            continue
        ziffern = sorted((w for w in woerter if w["text"].isdigit() and x0 - 8 <= w["x0"] and w["x1"] <= wt["x0"]
                          and abs(w["top"] - wt["top"]) < 3), key=lambda w: w["x0"])
        if ziffern and 1 <= int("".join(w["text"] for w in ziffern)) <= 31:
            zeilen.setdefault(monat, []).append((int("".join(w["text"] for w in ziffern)), (wt["top"] + wt["bottom"]) / 2))

    labels = [w for w in woerter if LABEL.search(w["text"]) and spalte(w["x0"])[0] and w["x0"] > spalte(w["x0"])[1] + 20]
    termine = []
    for r in seite.rects:
        art = tonnenart(r.get("non_stroking_color"))
        if not art or not auf_seite(r, seite) or r["width"] > 20:
            continue
        monat, _ = spalte(r["x0"])
        if not monat:
            continue
        mitte = (r["top"] + r["bottom"]) / 2
        tag = min(zeilen.get(monat, []), key=lambda z: abs(z[1] - mitte), default=None)
        if not tag or abs(tag[1] - mitte) > 6:
            continue
        # zugehörige Bezirke: nächste Angabe links in derselben Zeile (Saison-Bio teilt sich die der grünen Tonne)
        links = [w for w in labels if spalte(w["x0"])[0] == monat and w["x1"] <= r["x0"] + 1
                 and abs((w["top"] + w["bottom"]) / 2 - mitte) < 6]
        if not links:
            continue
        label = max(links, key=lambda w: w["x1"])["text"]
        ausrufe = [w for w in woerter if w["text"] == "!" and spalte(w["x0"])[0] == monat
                   and abs((w["top"] + w["bottom"]) / 2 - mitte) < 6]
        termine.append({
            "datum": date(jahr, monat, tag[0]).isoformat(), "art": art,
            "bezirke": LABEL.search(label).group(1).split("+"), "verschoben": "!" in label or bool(ausrufe),
        })
    return termine


def lies_strassen(text):
    strassen = {}
    for zeile in text.splitlines():
        for name, bezirk in STRASSE.findall(zeile):
            name = re.sub(r"^[A-ZÄÖÜ] (?=[A-ZÄÖÜ])", "", name.strip())
            if len(name) > 2:
                strassen[name] = bezirk
    return strassen


def lies_pdf(pfad):
    """Liefert {"jahr", "termine": [...], "strassen": {name: bezirk}}."""
    termine, strassen, jahr = [], {}, None
    with pdfplumber.open(pfad) as pdf:
        for seite in pdf.pages:
            seite = seite.within_bbox(seite.bbox)  # Seiten enthalten auch Inhalte außerhalb des sichtbaren Bereichs
            text = seite.extract_text() or ""
            if jahr is None and (m := re.search(r"Abfallkalender\D{0,40}(20\d\d)", text)):
                jahr = int(m.group(1))
            if "Bezirkseinteilung" in text:
                strassen.update(lies_strassen(text))
            elif jahr:
                termine += lies_seite(seite, jahr)
    if not jahr:
        raise ValueError("Jahr im PDF nicht gefunden")
    eindeutig = {(t["datum"], t["art"], tuple(t["bezirke"])): t for t in termine}
    termine = sorted(eindeutig.values(), key=lambda t: (t["datum"], t["art"]))
    if not termine:
        raise ValueError("keine Abfuhrtermine im PDF erkannt")
    return {"jahr": jahr, "termine": termine, "strassen": dict(sorted(strassen.items()))}


def passt(termin, bezirk):
    """„1+3“ meint Hauptbezirke (Rest/Bio), „1.1+1.2“ Unterbezirke (Gelb/Papier)."""
    haupt = bezirk.split(".")[0]
    return any(b == bezirk or b == haupt for b in termin["bezirke"])
