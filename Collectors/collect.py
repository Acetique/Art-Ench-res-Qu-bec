
import hashlib
import json
import re
import time
import unicodedata
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import fitz
import requests

VERSION = "V8"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

DB = DATA / "oeuvres.json"
REPORT = DATA / "rapport-collecte.json"
QUALITY = DATA / "rapport-qualite.json"

AGENT = "ArtEncheresQuebec/8.0"
TIMEOUT = 30
MAX_PDF = 30 * 1024 * 1024
DELAY = 2

CATALOGUES = {
    "Printemps 2023": "https://bydealers.com/wp-content/uploads/2023/04/Catalogue_108p_Printemps2023_Web_Spread.pdf",
    "Automne 2023": "https://bydealers.com/wp-content/uploads/2026/02/Catalogue_Fix_Automne_2023-1.pdf",
    "Printemps 2024": "https://bydealers.com/wp-content/uploads/2024/04/Catalogue_BYD_Printemps2024_Double.pdf",
    "Automne 2024": "https://bydealers.com/wp-content/uploads/2024/10/Catalogue_106p_Automne2024_Web_Double_pages.pdf",
    "Printemps 2025": "https://bydealers.com/wp-content/uploads/2025/12/Catalogue_Printemps2025_Web_Double_pages.pdf",
    "Automne 2025": "https://bydealers.com/wp-content/uploads/2026/02/Catalogue_Fall_2025-1.pdf",
    "Printemps 2026": "https://bydealers.com/wp-content/uploads/2026/04/Catalogue_Printemps2026.pdf",
    "Succession printemps 2026": "https://bydealers.com/wp-content/uploads/2026/05/Catalogue_102p_Printemps_succession2026_Web_simple-1.pdf",
}

HTTP = requests.Session()
HTTP.headers["User-Agent"] = AGENT
ROBOTS = {}


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(s):
    s = str(s or "")
    s = "".join(
        " " if ord(c) < 32 else c
        for c in s
    )
    return re.sub(r"\s+", " ", s).strip()


def norm(s):
    s = unicodedata.normalize("NFKD", clean(s))
    return "".join(
        c for c in s
        if not unicodedata.combining(c)
    ).casefold()


def uid(*parts):
    raw = "|".join(norm(x) for x in parts)
    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:20]


def lot_norm(value):
    try:
        return str(int(value)).zfill(2)
    except (TypeError, ValueError):
        return clean(value)


def passage_id(item):
    if item.get("url") and item.get("lot"):
        return uid(
            item.get("maison"),
            item["url"],
            lot_norm(item["lot"]),
        )
    return item.get("id") or uid(
        item.get("maison"),
        item.get("artiste"),
        item.get("titre"),
    )


def allowed(url):
    u = urlparse(url)
    host = f"{u.scheme}://{u.netloc}"

    if host not in ROBOTS:
        try:
            response = HTTP.get(
                host + "/robots.txt",
                timeout=TIMEOUT,
                allow_redirects=False,
            )
            if response.status_code == 200:
                parser = RobotFileParser()
                parser.parse(response.text.splitlines())
            elif response.status_code in (404, 410):
                parser = RobotFileParser()
                parser.parse([])
            else:
                return False
            ROBOTS[host] = parser
        except requests.RequestException:
            return False

    return ROBOTS[host].can_fetch(AGENT, url)


def download(url):
    if not allowed(url):
        print("Acces non autorise :", url)
        return None

    try:
        with HTTP.get(
            url,
            timeout=TIMEOUT,
            stream=True,
            allow_redirects=False,
        ) as response:
            if response.is_redirect:
                return None
            response.raise_for_status()
            size = 0
            chunks = []
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_PDF:
                    return None
                chunks.append(chunk)

        content = b"".join(chunks)
        time.sleep(DELAY)
        return content if content.startswith(b"%PDF") else None

    except requests.RequestException as exc:
        print("Erreur telechargement :", exc)
        return None


BIO = re.compile(
    r"^\s*([12]\s*\d\s*\d\s*\d)"
    r"\s*[-–—]\s*"
    r"([12]\s*\d\s*\d\s*\d)?\s*$"
)
LOT = re.compile(r"^\s*(\d{1,3})\s+(.{3,180})$")
DIM = re.compile(
    r"\d{1,4}(?:[.,]\d+)?\s*[x×]"
    r"\s*\d{1,4}(?:[.,]\d+)?"
    r"(?:\s*[x×]\s*\d{1,4}(?:[.,]\d+)?)?"
    r"\s*cm\b", re.I
)
MONEY = re.compile(
    r"(?<!\d)(\d[\d\s\u00a0,\.]*)"
    r"\s*[-–—]\s*"
    r"(\d[\d\s\u00a0,\.]*)"
    r"\s*(?:\$|CAD)?", re.I
)
YEAR = re.compile(r"^(?:c\.?\s*)?(?:18|19|20)\d{2}$", re.I)


def amount(s):
    digits = re.sub(r"\D", "", s)
    return int(digits) if digits else None


def estimate(text):
    match = MONEY.search(clean(text))
    if not match:
        return None

    a = amount(match.group(1))
    b = amount(match.group(2))

    if a is None or b is None:
        return None
    if not (100 <= a <= b <= 100000000):
        return None
    if b > a * 20:
        return None

    return a, b


def lines_from_page(page):
    output = []
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            text = clean(" ".join(
                span.get("text", "")
                for span in line.get("spans", [])
            ))
            if text:
                x0, y0, x1, y1 = line["bbox"]
                output.append({
                    "text": text,
                    "x": x0,
                    "y": y0,
                    "x2": x1,
                    "y2": y1,
                })
    return output


def columns(page):
    lines = lines_from_page(page)
    width = page.rect.width

    if width <= 850:
        return [sorted(
            lines, key=lambda x: (x["y"], x["x"])
        )]

    middle = width / 2
    result = [[], []]

    for line in lines:
        center = (line["x"] + line["x2"]) / 2
        side = 0 if center < middle else 1
        result[side].append(line)

    return [
        sorted(part, key=lambda x: (x["y"], x["x"]))
        for part in result
    ]


def biography(s):
    m = BIO.fullmatch(clean(s))
    if not m:
        return False
    born = amount(m.group(1))
    died = amount(m.group(2))
    return (
        born is not None
        and 1700 <= born <= 2026
        and (died is None or born <= died <= 2026)
    )


def artist_ok(s):
    s = clean(s)
    return (
        5 <= len(s) <= 100
        and len(s.split()) >= 2
        and not re.search(r"\d|https?://|@", s)
    )


def find_lots(lines):
    entries = []
    seen = set()

    for i in range(2, len(lines)):
        if not biography(lines[i - 1]["text"]):
            continue

        artist = lines[i - 2]["text"]
        match = LOT.fullmatch(lines[i]["text"])

        if not artist_ok(artist) or not match:
            continue

        lot = int(match.group(1))
        title = clean(match.group(2))

        if not 1 <= lot <= 500:
            continue
        if not re.search(r"[A-Za-zÀ-ÿ]", title):
            continue

        artist = re.sub(
            r"\s*\(\s*suite\s*\)\s*$",
            "", artist, flags=re.I
        )

        key = (lot, round(lines[i]["y"], 1))
        if key in seen:
            continue
        seen.add(key)

        entries.append({
            "artiste": artist,
            "titre": title,
            "lot": lot,
            "index": i,
            "y": lines[i]["y"],
        })

    return entries


def metadata(lines, entries, position):
    entry = entries[position]
    start = entry["index"] + 1

    if position + 1 < len(entries):
        end = entries[position + 1]["index"] - 2
    else:
        end = len(lines)

    section = lines[start:min(end, start + 110)]
    dimensions = None
    candidates = []

    for i, line in enumerate(section):
        text = line["text"]

        if dimensions is None:
            found = DIM.search(text)
            if found:
                dimensions = found.group()

        label = bool(re.search(
            r"\b(?:estimation|estimate)\b",
            norm(text)
        ))

        if label:
            # L'estimation peut etre sur la meme
            # ligne ou sur les lignes suivantes.
            for following in section[i:i + 5]:
                value = estimate(following["text"])
                if value:
                    candidates.append(value)
                    break

    # Si plusieurs valeurs contradictoires sont
    # trouvees, ne pas attribuer de montant.
    unique = list(dict.fromkeys(candidates))
    estimation = unique[0] if len(unique) == 1 else None

    return dimensions, estimation, len(unique) > 1


def notice(url, sale, page_number, entry, dims, est, conflict):
    lot = lot_norm(entry["lot"])
    ident = uid("BYDealers", url, lot)
    year_match = re.search(r"20\d{2}", sale)

    return {
        "id": ident,
        "id_passage": ident,
        "id_oeuvre": None,
        "maison": "BYDealers",
        "artiste": entry["artiste"],
        "titre": entry["titre"],
        "lot": lot,
        "dimensions": dims,
        "vente": sale,
        "annee_vente": int(year_match.group()) if year_match else None,
        "image": None,
        "prix": None,
        "prix_marteau": None,
        "prix_vendu": None,
        "estimation_min": est[0] if est else None,
        "estimation_max": est[1] if est else None,
        "estimation": (
            f"{est[0]:,} - {est[1]:,} CAD"
            if est else None
        ),
        "type_prix": "estimation" if est else "inconnu",
        "devise": "CAD",
        "statut": "catalogue_historique",
        "url": url,
        "source_page": url,
        "source_page_pdf": page_number,
        "origine": "catalogue_pdf_v8",
        "confiance_extraction": (
            95 if dims and est and not conflict
            else 85 if dims else 75
        ),
        "conflit_estimation": conflict,
        "prix_realise_verifie": False,
        "date_collecte": now(),
    }


def parse_pdf(data, url, sale):
    output = []
    with fitz.open(stream=data, filetype="pdf") as doc:
        for page_no, page in enumerate(doc, 1):
            for part in columns(page):
                entries = find_lots(part)
                for i, entry in enumerate(entries):
                    dims, est, conflict = metadata(
                        part, entries, i
                    )
                    output.append(notice(
                        url, sale, page_no,
                        entry, dims, est, conflict
                    ))
    return output


def load_old():
    if not DB.exists():
        return []
    data = json.loads(DB.read_text(encoding="utf-8"))
    if isinstance(data, dict):
        data = data.get("oeuvres", [])
    if not isinstance(data, list):
        raise ValueError("oeuvres.json invalide")
    return [x for x in data if isinstance(x, dict)]


def merge(old, new):
    result = dict(old)

    # Ne pas effacer les valeurs historiques
    # avec des champs vides.
    for field in (
        "artiste", "titre", "dimensions",
        "vente", "annee_vente",
        "source_page_pdf",
    ):
        if new.get(field) is not None:
            result[field] = new[field]

    # Remplacer une estimation uniquement
    # si la nouvelle extraction est complete
    # et ne presente pas de conflit.
    if (
        new.get("estimation_min") is not None
        and not new.get("conflit_estimation")
    ):
        for field in (
            "estimation_min", "estimation_max",
            "estimation", "type_prix",
        ):
            result[field] = new[field]

    for field, value in new.items():
        if field not in result:
            result[field] = value

    result["id_passage"] = passage_id(new)
    result["origine"] = "catalogue_pdf_v8"
    result["conflit_estimation"] = new.get(
        "conflit_estimation", False
    )
    result["confiance_extraction"] = new.get(
        "confiance_extraction", 75
    )

    # Les prix realises existants sont
    # preserves; aucune estimation n'est
    # convertie en prix de vente.
    return result


def dim_key(value):
    m = DIM.search(clean(value))
    if not m:
        return None
    nums = re.findall(
        r"\d+(?:[.,]\d+)?", m.group()
    )
    return tuple(sorted(
        round(float(x.replace(",", ".")), 1)
        for x in nums
    ))


def matching(records):
    groups = defaultdict(list)

    for record in records:
        record["id_oeuvre"] = record.get(
            "id_oeuvre"
        ) or uid("oeuvre-provisoire", passage_id(record))

        dims = dim_key(record.get("dimensions"))
        artist = norm(record.get("artiste"))
        title = norm(record.get("titre"))

        if not dims or not artist or not title:
            continue

        groups[(artist, title, dims)].append(record)

    matches = []

    for group in groups.values():
        if len(group) < 2:
            continue

        sales = {
            (x.get("url"), lot_norm(x.get("lot")))
            for x in group
        }
        if len(sales) < 2:
            continue

        same_catalogue = len({
            x.get("url") for x in group
        }) == 1

        matches.append({
            "artiste": group[0].get("artiste"),
            "titre": group[0].get("titre"),
            "dimensions": group[0].get("dimensions"),
            "verification": "requise",
            "risque": (
                "lots_distincts_meme_catalogue"
                if same_catalogue
                else "possible_remise_en_vente"
            ),
            "passages": [{
                "id_passage": passage_id(x),
                "vente": x.get("vente"),
                "lot": x.get("lot"),
                "url": x.get("url"),
            } for x in group],
        })

    return matches


def quality(records, matches):
    anomalies = []

    for x in records:
        problems = []

        if not x.get("dimensions"):
            problems.append("dimensions_absentes")
        if x.get("estimation_min") is None:
            problems.append("estimation_absente")
        if x.get("conflit_estimation"):
            problems.append("conflit_estimation")
        if clean(x.get("titre")).endswith(
            ("/", "-", ",", ":")
        ):
            problems.append("titre_possiblement_tronque")
        if "( suite )" in clean(x.get("artiste")).lower():
            problems.append("nom_artiste_a_normaliser")

        if problems:
            anomalies.append({
                "id": passage_id(x),
                "artiste": x.get("artiste"),
                "titre": x.get("titre"),
                "problemes": problems,
                "url": x.get("url"),
                "page": x.get("source_page_pdf"),
            })

    return {
        "version": VERSION,
        "date": now(),
        "total_oeuvres": len(records),
        "avec_dimensions": sum(
            bool(x.get("dimensions")) for x in records
        ),
        "avec_estimation": sum(
            x.get("estimation_min") is not None
            for x in records
        ),
        "prix_marteau_verifies": sum(
            bool(x.get("prix_realise_verifie"))
            for x in records
        ),
        "rapprochements_potentiels": matches,
        "nombre_rapprochements": len(matches),
        "anomalies": anomalies,
        "nombre_notices_a_controler": len(anomalies),
    }


def save(path, obj):
    path.write_text(
        json.dumps(obj, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )


def main():
    old = load_old()
    database = {passage_id(x): x for x in old}

    stats = {
        "pages_consultees": 0,
        "pages_accessibles": 0,
        "pdf_lus": 0,
        "oeuvres_detectees": 0,
        "nouvelles_oeuvres": 0,
        "erreurs_ou_blocages": 0,
    }
    catalog_reports = []

    for sale, url in CATALOGUES.items():
        print("Analyse :", sale)
        stats["pages_consultees"] += 1
        content = download(url)

        if not content:
            stats["erreurs_ou_blocages"] += 1
            catalog_reports.append({
                "vente": sale,
                "statut": "indisponible",
                "oeuvres": 0,
            })
            continue

        stats["pages_accessibles"] += 1
        stats["pdf_lus"] += 1

        try:
            found = parse_pdf(content, url, sale)
        except Exception as exc:
            print("Erreur PDF :", exc)
            stats["erreurs_ou_blocages"] += 1
            continue

        stats["oeuvres_detectees"] += len(found)
        catalog_reports.append({
            "vente": sale,
            "statut": "ok",
            "oeuvres": len(found),
        })

        for item in found:
            key = passage_id(item)
            if key in database:
                database[key] = merge(database[key], item)
            else:
                database[key] = item
                stats["nouvelles_oeuvres"] += 1

    records = sorted(
        database.values(),
        key=lambda x: (
            norm(x.get("artiste")),
            norm(x.get("titre")),
            str(x.get("annee_vente") or ""),
        )
    )

    matches = matching(records)
    checks = quality(records, matches)

    report = {
        "version": VERSION,
        "date": now(),
        "sources": {
            "BYDealers": stats,
            "Heffel": {
                "statut": "connecteur_a_developper",
                "oeuvres_detectees": 0,
            },
            "Gelineau": {
                "statut": "connecteur_a_developper",
                "oeuvres_detectees": 0,
            },
        },
        "catalogues": catalog_reports,
        "total_oeuvres": len(records),
        "avec_estimation": checks["avec_estimation"],
        "rapprochements_potentiels": len(matches),
        "note": (
            "Estimations de catalogues seulement. "
            "Aucun prix marteau n'est deduit."
        ),
    }

    save(DB, records)
    save(REPORT, report)
    save(QUALITY, checks)

    print("\nART ENCHERES QUEBEC V8")
    print("Total :", len(records))
    print("Estimations :", checks["avec_estimation"])
    print("Rapprochements :", len(matches))
    print(
        "Notices a controler :",
        checks["nombre_notices_a_controler"]
    )


if __name__ == "__main__":
    main()
