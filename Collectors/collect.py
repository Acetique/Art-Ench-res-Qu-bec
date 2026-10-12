
"""
ART ENCHERES QUEBEC V7
Collecte de notices historiques BYDealers.

- Analyse spatiale des pages PDF
- Estimations rattachees aux lots
- Conservation des anciennes notices
- Rapprochement prudent des oeuvres
- Controle qualite
- Aucune invention de prix marteau
"""

import hashlib
import json
import logging
import re
import time
import unicodedata

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import fitz
import requests


ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

DB_PATH = DATA / "oeuvres.json"
REPORT_PATH = DATA / "rapport-collecte.json"
QUALITY_PATH = DATA / "rapport-qualite.json"

VERSION = "V7"
AGENT = "ArtEncheresQuebec/7.0"
DELAY = 2
TIMEOUT = 25
MAX_BYTES = 25 * 1024 * 1024

CATALOGUES = {
    "Printemps 2023": (
        "https://bydealers.com/wp-content/uploads/"
        "2023/04/Catalogue_108p_Printemps2023_Web_Spread.pdf"
    ),
    "Automne 2023": (
        "https://bydealers.com/wp-content/uploads/"
        "2026/02/Catalogue_Fix_Automne_2023-1.pdf"
    ),
    "Printemps 2024": (
        "https://bydealers.com/wp-content/uploads/"
        "2024/04/Catalogue_BYD_Printemps2024_Double.pdf"
    ),
    "Automne 2024": (
        "https://bydealers.com/wp-content/uploads/"
        "2024/10/Catalogue_106p_Automne2024_Web_Double_pages.pdf"
    ),
    "Printemps 2025": (
        "https://bydealers.com/wp-content/uploads/"
        "2025/12/Catalogue_Printemps2025_Web_Double_pages.pdf"
    ),
    "Automne 2025": (
        "https://bydealers.com/wp-content/uploads/"
        "2026/02/Catalogue_Fall_2025-1.pdf"
    ),
    "Printemps 2026": (
        "https://bydealers.com/wp-content/uploads/"
        "2026/04/Catalogue_Printemps2026.pdf"
    ),
    "Succession printemps 2026": (
        "https://bydealers.com/wp-content/uploads/"
        "2026/05/Catalogue_102p_Printemps_succession2026_Web_simple-1.pdf"
    ),
}

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)

session = requests.Session()
session.headers.update({
    "User-Agent": AGENT,
    "Accept": "application/pdf",
})

robots_cache = {}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    value = str(value or "")
    value = "".join(
        " " if ord(ch) < 32 else ch
        for ch in value
    )
    return re.sub(r"\s+", " ", value).strip()


def normalize(value):
    value = unicodedata.normalize(
        "NFKD", clean(value)
    )
    return "".join(
        c for c in value
        if not unicodedata.combining(c)
    ).casefold()


def digest(*parts):
    value = "|".join(
        normalize(p) for p in parts
    )
    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()[:20]


def number(value):
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def year_from_sale(sale):
    match = re.search(r"20\d{2}", sale)
    return int(match.group()) if match else None


def robots_allowed(url):
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    if origin not in robots_cache:
        try:
            response = session.get(
                origin + "/robots.txt",
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

            robots_cache[origin] = parser

        except requests.RequestException:
            return False

    return robots_cache[origin].can_fetch(
        AGENT, url
    )


def download_pdf(url):
    if not robots_allowed(url):
        logging.warning(
            "Acces interdit/non verifiable : %s",
            url,
        )
        return None

    try:
        response = session.get(
            url,
            timeout=TIMEOUT,
            stream=True,
            allow_redirects=False,
        )

        if response.is_redirect:
            response.close()
            return None

        response.raise_for_status()

        if int(
            response.headers.get(
                "Content-Length", "0"
            ) or 0
        ) > MAX_BYTES:
            response.close()
            return None

        chunks = []
        size = 0

        try:
            for chunk in response.iter_content(65536):
                size += len(chunk)

                if size > MAX_BYTES:
                    return None

                chunks.append(chunk)
        finally:
            response.close()

        content = b"".join(chunks)
        time.sleep(DELAY)

        return (
            content
            if content.startswith(b"%PDF")
            else None
        )

    except requests.RequestException as exc:
        logging.warning(
            "Telechargement impossible : %s",
            exc,
        )
        return None


# ===========================================
# REGLES D'EXTRACTION
# ===========================================

BIO = re.compile(
    r"^\s*((?:[12]\s*)\d\s*\d\s*\d)"
    r"\s*[-–—]\s*"
    r"((?:[12]\s*)\d\s*\d\s*\d)?\s*$"
)

LOT = re.compile(
    r"^\s*(\d{1,3})\s+(.{3,160})$"
)

DIMENSIONS = re.compile(
    r"\d{1,3}(?:[.,]\d+)?"
    r"\s*[x×]\s*"
    r"\d{1,3}(?:[.,]\d+)?"
    r"(?:\s*[x×]\s*"
    r"\d{1,3}(?:[.,]\d+)?)?"
    r"\s*cm\b",
    re.I,
)

ESTIMATION = re.compile(
    r"(\d[\d\s\u00a0]*)"
    r"\s*[-–—]\s*"
    r"(\d[\d\s\u00a0]*)"
    r"\s*(?:\$|CAD)",
    re.I,
)

YEAR = re.compile(
    r"^(?:c\.?\s*)?(?:18|19|20)\d{2}"
    r"(?:\s*[-–]\s*(?:18|19|20)\d{2})?$",
    re.I,
)

HEADINGS = {
    "provenance",
    "estimation/estimate",
    "estimate",
    "exposition/exhibition",
    "exhibition",
    "bibliographie/literature",
    "literature",
}


def is_biography(text):
    match = BIO.fullmatch(clean(text))

    if not match:
        return False

    born = number(match.group(1))
    died = number(match.group(2))

    if not born or not 1700 <= born <= 2026:
        return False

    return (
        died is None
        or born <= died <= 2026
    )


def is_artist(text):
    text = clean(text)

    if not 5 <= len(text) <= 90:
        return False

    if len(text.split()) < 2:
        return False

    if re.search(r"\d|https?://|@", text):
        return False

    if normalize(text) in HEADINGS:
        return False

    return bool(
        re.search(r"[A-Za-zÀ-ÿ]", text)
    )


def is_title(text):
    text = clean(text)

    return (
        3 <= len(text) <= 160
        and bool(
            re.search(r"[A-Za-zÀ-ÿ]", text)
        )
        and normalize(text) not in HEADINGS
        and not YEAR.fullmatch(text)
    )


# ===========================================
# LIGNES ET COORDONNEES
# ===========================================

def extract_lines(page):
    result = []

    for block in page.get_text(
        "dict", sort=False
    ).get("blocks", []):
        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):
            text = clean(" ".join(
                span.get("text", "")
                for span in line.get("spans", [])
            ))

            if not text:
                continue

            box = line["bbox"]

            result.append({
                "text": text,
                "x": float(box[0]),
                "y": float(box[1]),
                "x2": float(box[2]),
                "y2": float(box[3]),
            })

    return result


def page_columns(page):
    lines = extract_lines(page)
    width = float(page.rect.width)

    # Une page tres large est generalement
    # une double page de catalogue.
    if width > 850:
        middle = width / 2

        left = [
            x for x in lines
            if (x["x"] + x["x2"]) / 2
            < middle
        ]

        right = [
            x for x in lines
            if (x["x"] + x["x2"]) / 2
            >= middle
        ]

        return [left, right]

    return [lines]


def find_entries(lines):
    """
    Le regroupement initial garde l'ordre
    des blocs du PDF, comme dans la V6.
    """

    entries = []

    for i in range(2, len(lines)):
        if not is_biography(
            lines[i - 1]["text"]
        ):
            continue

        artist = lines[i - 2]["text"]

        if not is_artist(artist):
            continue

        match = LOT.match(
            lines[i]["text"]
        )

        if not match:
            continue

        lot = int(match.group(1))
        title = clean(match.group(2))

        if not 1 <= lot <= 500:
            continue

        if not is_title(title):
            continue

        # Titres sur plusieurs lignes.
        if i + 1 < len(lines):
            following = lines[i + 1]["text"]

            if (
                title.endswith(("/", "-", ","))
                and is_title(following)
                and not LOT.match(following)
                and not DIMENSIONS.search(following)
            ):
                title = clean(
                    title + " " + following
                )

        entries.append({
            "artist": artist,
            "lot": lot,
            "title": title,
            "start": i,
            "x": lines[i]["x"],
            "y": lines[i]["y"],
        })

    return entries


def find_metadata(lines, entries, index):
    entry = entries[index]

    start = entry["start"] + 1

    if index + 1 < len(entries):
        end = entries[index + 1]["start"] - 2
    else:
        end = len(lines)

    end = min(
        end,
        start + 100,
    )

    dimensions = None
    estimate_min = None
    estimate_max = None

    section = lines[start:end]

    for j, line in enumerate(section):
        text = line["text"]

        if dimensions is None:
            match = DIMENSIONS.search(text)

            if match:
                dimensions = match.group()

        if normalize(text).startswith(
            ("estimation/estimate", "estimate")
        ):
            for candidate in section[
                j + 1:j + 5
            ]:
                match = ESTIMATION.search(
                    candidate["text"]
                )

                if not match:
                    continue

                low = number(match.group(1))
                high = number(match.group(2))

                if (
                    low is not None
                    and high is not None
                    and 100 <= low <= high
                ):
                    estimate_min = low
                    estimate_max = high

                break

    return (
        dimensions,
        estimate_min,
        estimate_max,
    )


# ===========================================
# NOTICES
# ===========================================

def create_notice(
    url,
    sale,
    page,
    entry,
    dimensions,
    estimate_min,
    estimate_max,
):
    lot = str(entry["lot"]).zfill(2)

    # ID du passage aux encheres.
    # Stable meme si le titre est corrige.
    passage_id = digest(
        "BYDealers", url, lot
    )

    confidence = 75

    if dimensions:
        confidence += 10

    if estimate_min is not None:
        confidence += 10

    return {
        "id": passage_id,
        "id_passage": passage_id,
        "id_oeuvre": None,
        "maison": "BYDealers",
        "artiste": entry["artist"],
        "titre": entry["title"],
        "lot": lot,
        "dimensions": dimensions,
        "vente": sale,
        "annee_vente": year_from_sale(sale),
        "image": None,
        "prix": None,
        "prix_marteau": None,
        "prix_vendu": None,
        "estimation_min": estimate_min,
        "estimation_max": estimate_max,
        "estimation": (
            f"{estimate_min:,} - "
            f"{estimate_max:,} CAD"
            if estimate_min is not None
            and estimate_max is not None
            else None
        ),
        "type_prix": (
            "estimation"
            if estimate_min is not None
            else "inconnu"
        ),
        "devise": "CAD",
        "statut": "catalogue_historique",
        "url": url,
        "source_page": url,
        "source_page_pdf": page,
        "origine": "catalogue_pdf_v7",
        "confiance_extraction": confidence,
        "prix_realise_verifie": False,
        "date_collecte": timestamp(),
    }


def parse_pdf(content, url, sale):
    results = []

    with fitz.open(
        stream=content,
        filetype="pdf",
    ) as document:

        for page_number, page in enumerate(
            document, start=1
        ):
            for lines in page_columns(page):
                entries = find_entries(lines)

                for index, entry in enumerate(
                    entries
                ):
                    (
                        dimensions,
                        low,
                        high,
                    ) = find_metadata(
                        lines, entries, index
                    )

                    results.append(
                        create_notice(
                            url,
                            sale,
                            page_number,
                            entry,
                            dimensions,
                            low,
                            high,
                        )
                    )

    return results


# ===========================================
# BASE EXISTANTE
# ===========================================

def load_existing():
    if not DB_PATH.exists():
        return []

    data = json.loads(
        DB_PATH.read_text(
            encoding="utf-8"
        )
    )

    if isinstance(data, dict):
        data = data.get("oeuvres", [])

    if not isinstance(data, list):
        raise ValueError(
            "oeuvres.json invalide"
        )

    return [
        item for item in data
        if isinstance(item, dict)
    ]


def passage_key(item):
    url = item.get("url") or ""
    lot = item.get("lot")

    if url and lot is not None:
        try:
            lot = str(int(lot)).zfill(2)
        except (ValueError, TypeError):
            lot = str(lot)

        return digest(
            item.get("maison", ""),
            url,
            lot,
        )

    return item.get("id") or digest(
        item.get("maison", ""),
        item.get("artiste", ""),
        item.get("titre", ""),
    )


def merge_records(old, fresh):
    result = dict(old)

    # La nouvelle extraction remplace les
    # champs descriptifs seulement quand
    # elle dispose d'une valeur.
    fields = (
        "artiste",
        "titre",
        "dimensions",
        "estimation_min",
        "estimation_max",
        "estimation",
        "type_prix",
        "vente",
        "annee_vente",
        "source_page_pdf",
        "confiance_extraction",
    )

    for field in fields:
        value = fresh.get(field)

        if value is not None:
            result[field] = value

    for field, value in fresh.items():
        if field not in result:
            result[field] = value

    result["id_passage"] = passage_key(
        fresh
    )

    # Ne jamais transformer une estimation
    # en prix marteau.
    if not result.get(
        "prix_realise_verifie"
    ):
        result["prix_marteau"] = None
        result["prix_vendu"] = None

    result["origine"] = (
        "catalogue_pdf_v7"
    )

    return result


# ===========================================
# RAPPROCHEMENT DES OEUVRES
# ===========================================

def dimension_key(value):
    if not value:
        return None

    match = DIMENSIONS.search(value)

    if not match:
        return None

    nums = re.findall(
        r"\d+(?:[.,]\d+)?",
        match.group(),
    )

    values = sorted(
        round(
            float(x.replace(",", ".")),
            1,
        )
        for x in nums
    )

    return tuple(values)


def identity_key(item):
    artist = normalize(
        item.get("artiste")
    )

    title = normalize(
        item.get("titre")
    )

    dimensions = dimension_key(
        item.get("dimensions")
    )

    if not (
        artist
        and title
        and dimensions
    ):
        return None

    # Normalisation legere :
    # on ne suppose pas que deux artistes
    # aux noms proches sont identiques.
    title = re.sub(
        r"[^a-z0-9]+", " ", title
    ).strip()

    return (
        artist,
        title,
        dimensions,
    )


def group_works(records):
    groups = defaultdict(list)

    for item in records:
        key = identity_key(item)

        if key:
            groups[key].append(item)

    possible_repeats = []

    for key, members in groups.items():
        work_id = digest(
            "oeuvre",
            key[0],
            key[1],
            str(key[2]),
        )

        for member in members:
            member["id_oeuvre"] = work_id

        if len(members) > 1:
            possible_repeats.append({
                "id_oeuvre": work_id,
                "artiste": members[0].get(
                    "artiste"
                ),
                "titre": members[0].get(
                    "titre"
                ),
                "dimensions": members[0].get(
                    "dimensions"
                ),
                "nombre_passages": len(members),
                "passages": [
                    {
                        "vente": m.get("vente"),
                        "lot": m.get("lot"),
                        "url": m.get("url"),
                    }
                    for m in members
                ],
                "verification": "requise",
            })

    return possible_repeats


# ===========================================
# CONTROLE QUALITE
# ===========================================

def quality_report(records, repeats):
    issues = []

    for item in records:
        problems = []

        if not item.get("dimensions"):
            problems.append(
                "dimensions_absentes"
            )

        if item.get(
            "estimation_min"
        ) is None:
            problems.append(
                "estimation_absente"
            )

        if item.get(
            "titre", ""
        ).endswith(("/", "-", ",")):
            problems.append(
                "titre_possiblement_tronque"
            )

        if item.get(
            "confiance_extraction", 0
        ) < 90:
            problems.append(
                "controle_manuel_recommande"
            )

        if problems:
            issues.append({
                "id": item.get("id"),
                "artiste": item.get(
                    "artiste"
                ),
                "titre": item.get(
                    "titre"
                ),
                "problemes": problems,
                "url": item.get("url"),
            })

    return {
        "version": VERSION,
        "date": timestamp(),
        "total_oeuvres": len(records),
        "avec_dimensions": sum(
            bool(x.get("dimensions"))
            for x in records
        ),
        "avec_estimation": sum(
            x.get("estimation_min")
            is not None
            for x in records
        ),
        "prix_marteau_verifies": sum(
            bool(x.get(
                "prix_realise_verifie"
            ))
            for x in records
        ),
        "rapprochements_potentiels": repeats,
        "nombre_rapprochements": len(repeats),
        "anomalies": issues,
        "nombre_notices_a_controler": len(
            issues
        ),
    }


# ===========================================
# EXECUTION
# ===========================================

def main():
    existing = load_existing()

    database = {
        passage_key(item): item
        for item in existing
    }

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
        logging.info(
            "Catalogue : %s", sale
        )

        stats["pages_consultees"] += 1

        content = download_pdf(url)

        if content is None:
            stats[
                "erreurs_ou_blocages"
            ] += 1

            catalog_reports.append({
                "vente": sale,
                "statut": "indisponible",
                "oeuvres": 0,
            })

            continue

        stats["pages_accessibles"] += 1
        stats["pdf_lus"] += 1

        try:
            works = parse_pdf(
                content, url, sale
            )

        except Exception as exc:
            logging.warning(
                "Erreur extraction : %s",
                exc,
            )

            stats[
                "erreurs_ou_blocages"
            ] += 1
            continue

        stats[
            "oeuvres_detectees"
        ] += len(works)

        catalog_reports.append({
            "vente": sale,
            "statut": "ok",
            "oeuvres": len(works),
        })

        for item in works:
            key = passage_key(item)

            if key in database:
                database[key] = merge_records(
                    database[key],
                    item,
                )
            else:
                database[key] = item
                stats[
                    "nouvelles_oeuvres"
                ] += 1

    records = sorted(
        database.values(),
        key=lambda x: (
            normalize(x.get("artiste")),
            normalize(x.get("titre")),
            str(x.get("annee_vente", "")),
        ),
    )

    repeats = group_works(records)

    quality = quality_report(
        records, repeats
    )

    report = {
        "version": VERSION,
        "date": timestamp(),
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
        "avec_estimation": quality[
            "avec_estimation"
        ],
        "rapprochements_potentiels": len(
            repeats
        ),
        "note": (
            "Notices de catalogues historiques. "
            "Les estimations ne sont pas "
            "des prix marteau."
        ),
    }

    for path, data in (
        (DB_PATH, records),
        (REPORT_PATH, report),
        (QUALITY_PATH, quality),
    ):
        path.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    print()
    print("ART ENCHERES QUEBEC V7")
    print("======================")
    print("Total :", len(records))
    print(
        "Estimations :",
        quality["avec_estimation"],
    )
    print(
        "Rapprochements :",
        len(repeats),
    )
    print(
        "Notices a controler :",
        quality[
            "nombre_notices_a_controler"
        ],
    )


if __name__ == "__main__":
    main()
