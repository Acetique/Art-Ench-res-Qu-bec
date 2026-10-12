
# ============================================================
# ART ENCHERES QUEBEC - COLLECTEUR V8.1
# ============================================================
# Sources : catalogues PDF publics BYDealers
#
# Objectifs :
# - Conserver les notices historiques
# - Detecter les lots dans les catalogues
# - Recuperer les dimensions et estimations
# - Ne jamais inventer de prix marteau
# - Ne jamais fusionner automatiquement deux oeuvres
# - Produire des rapports de collecte et de qualite
# - Afficher les erreurs completes dans GitHub Actions
# ============================================================

import hashlib
import json
import re
import time
import traceback
import unicodedata

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import fitz
import requests


# ============================================================
# CONFIGURATION
# ============================================================

VERSION = "V8.1"

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

DB_FILE = DATA / "oeuvres.json"
REPORT_FILE = DATA / "rapport-collecte.json"
QUALITY_FILE = DATA / "rapport-qualite.json"

USER_AGENT = "ArtEncheresQuebec/8.1"
TIMEOUT = 35
MAX_PDF_BYTES = 35 * 1024 * 1024
REQUEST_DELAY = 2

CATALOGUES = {
    "Printemps 2023":
        "https://bydealers.com/wp-content/uploads/2023/04/Catalogue_108p_Printemps2023_Web_Spread.pdf",

    "Automne 2023":
        "https://bydealers.com/wp-content/uploads/2026/02/Catalogue_Fix_Automne_2023-1.pdf",

    "Printemps 2024":
        "https://bydealers.com/wp-content/uploads/2024/04/Catalogue_BYD_Printemps2024_Double.pdf",

    "Automne 2024":
        "https://bydealers.com/wp-content/uploads/2024/10/Catalogue_106p_Automne2024_Web_Double_pages.pdf",

    "Printemps 2025":
        "https://bydealers.com/wp-content/uploads/2025/12/Catalogue_Printemps2025_Web_Double_pages.pdf",

    "Automne 2025":
        "https://bydealers.com/wp-content/uploads/2026/02/Catalogue_Fall_2025-1.pdf",

    "Printemps 2026":
        "https://bydealers.com/wp-content/uploads/2026/04/Catalogue_Printemps2026.pdf",

    "Succession printemps 2026":
        "https://bydealers.com/wp-content/uploads/2026/05/Catalogue_102p_Printemps_succession2026_Web_simple-1.pdf",
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/pdf,application/octet-stream,*/*",
})

ROBOTS_CACHE = {}


# ============================================================
# OUTILS GENERAUX
# ============================================================

def now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    value = str(value or "")
    value = value.replace("\u00a0", " ")
    return re.sub(r"\s+", " ", value).strip()


def normalize(value):
    value = unicodedata.normalize("NFKD", clean(value))
    value = "".join(
        c for c in value
        if not unicodedata.combining(c)
    )
    return value.casefold()


def make_id(*values):
    raw = "|".join(normalize(v) for v in values)
    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:20]


def normalize_lot(value):
    value = clean(value)

    try:
        return str(int(value)).zfill(2)
    except (TypeError, ValueError):
        return value


def save_json(path, data):
    temp = path.with_suffix(path.suffix + ".tmp")

    temp.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temp.replace(path)


def load_database():
    if not DB_FILE.exists():
        return []

    with DB_FILE.open(encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, dict):
        data = data.get("oeuvres", [])

    if not isinstance(data, list):
        raise ValueError(
            "Le fichier oeuvres.json doit contenir une liste."
        )

    return [
        item for item in data
        if isinstance(item, dict)
    ]


def record_id(item):
    # Un passage aux encheres est identifie par
    # sa maison, son catalogue et son numero de lot.

    if item.get("url") and item.get("lot"):
        return make_id(
            item.get("maison", "BYDealers"),
            item["url"],
            normalize_lot(item["lot"]),
        )

    return (
        item.get("id_passage")
        or item.get("id")
        or make_id(
            item.get("maison"),
            item.get("artiste"),
            item.get("titre"),
            item.get("lot"),
        )
    )


# ============================================================
# RESPECT DES REGLES D'ACCES
# ============================================================

def robots_allowed(url):
    parsed = urlparse(url)

    if parsed.scheme != "https":
        return False

    host = f"{parsed.scheme}://{parsed.netloc}"

    if host not in ROBOTS_CACHE:
        try:
            response = SESSION.get(
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
                print(
                    "robots.txt indisponible :",
                    response.status_code,
                )
                return False

            ROBOTS_CACHE[host] = parser

        except requests.RequestException as exc:
            print("Erreur robots.txt :", exc)
            return False

    return ROBOTS_CACHE[host].can_fetch(
        USER_AGENT, url
    )


def download_pdf(url):
    if not robots_allowed(url):
        print("Acces non autorise :", url)
        return None

    try:
        with SESSION.get(
            url,
            timeout=TIMEOUT,
            stream=True,
            allow_redirects=False,
        ) as response:

            if response.is_redirect:
                print("Redirection non suivie :", url)
                return None

            response.raise_for_status()

            chunks = []
            size = 0

            for chunk in response.iter_content(
                chunk_size=65536
            ):
                if not chunk:
                    continue

                size += len(chunk)

                if size > MAX_PDF_BYTES:
                    print("PDF trop volumineux :", url)
                    return None

                chunks.append(chunk)

        content = b"".join(chunks)

        if not content.startswith(b"%PDF"):
            print("Le contenu recu n'est pas un PDF.")
            return None

        time.sleep(REQUEST_DELAY)
        return content

    except requests.RequestException as exc:
        print("Erreur de telechargement :", exc)
        return None


# ============================================================
# EXPRESSIONS REGULIERES
# ============================================================

BIO_PATTERN = re.compile(
    r"^\s*([12]\s*\d\s*\d\s*\d)"
    r"\s*[-–—]\s*"
    r"([12]\s*\d\s*\d\s*\d)?\s*$"
)

LOT_PATTERN = re.compile(
    r"^\s*(\d{1,3})\s+(.{3,200})$"
)

DIM_PATTERN = re.compile(
    r"\d{1,4}(?:[.,]\d+)?"
    r"\s*[x×]\s*"
    r"\d{1,4}(?:[.,]\d+)?"
    r"(?:\s*[x×]\s*\d{1,4}(?:[.,]\d+)?)?"
    r"\s*cm\b",
    re.IGNORECASE,
)

ESTIMATE_PATTERN = re.compile(
    r"(?<!\d)"
    r"(\d[\d\s\u00a0,\.]*)"
    r"\s*[-–—]\s*"
    r"(\d[\d\s\u00a0,\.]*)"
    r"\s*(?:\$|CAD)?",
    re.IGNORECASE,
)


# ============================================================
# VALIDATION DES DONNEES
# ============================================================

def parse_number(value):
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else None


def valid_biography(text):
    match = BIO_PATTERN.fullmatch(clean(text))

    if not match:
        return False

    born = parse_number(match.group(1))
    died = parse_number(match.group(2))

    if born is None:
        return False

    if born < 1700 or born > 2026:
        return False

    if died is not None:
        if died < born or died > 2026:
            return False

    return True


def valid_artist(text):
    text = clean(text)

    if not 5 <= len(text) <= 110:
        return False

    if len(text.split()) < 2:
        return False

    if re.search(r"https?://|@", text):
        return False

    if re.search(r"\d", text):
        return False

    return True


def normalize_artist(text):
    text = clean(text)

    text = re.sub(
        r"\s*\(\s*suite\s*\)\s*$",
        "",
        text,
        flags=re.IGNORECASE,
    )

    return clean(text)


def parse_estimate(text):
    text = clean(text)

    match = ESTIMATE_PATTERN.search(text)

    if not match:
        return None

    minimum = parse_number(match.group(1))
    maximum = parse_number(match.group(2))

    if minimum is None or maximum is None:
        return None

    if minimum < 100:
        return None

    if maximum < minimum:
        return None

    if maximum > 100000000:
        return None

    if maximum > minimum * 20:
        return None

    return minimum, maximum


# ============================================================
# EXTRACTION DES LIGNES PDF
# ============================================================

def extract_lines(page):
    """
    Extrait les lignes dans l'ordre des blocs PDF.

    Important :
    ne pas trier toutes les lignes uniquement
    selon leur position verticale.
    """

    output = []

    blocks = page.get_text(
        "dict",
        sort=False,
    ).get("blocks", [])

    for block in blocks:
        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):
            spans = line.get("spans", [])

            text = clean(
                " ".join(
                    span.get("text", "")
                    for span in spans
                )
            )

            if not text:
                continue

            bbox = line.get("bbox")

            if not bbox or len(bbox) != 4:
                continue

            x0, y0, x1, y1 = bbox

            output.append({
                "text": text,
                "x": float(x0),
                "y": float(y0),
                "x2": float(x1),
                "y2": float(y1),
            })

    return output


def split_columns(page):
    """
    Conserve l'ordre interne du PDF.

    Les pages tres larges sont traitees
    en deux zones gauche et droite.
    """

    lines = extract_lines(page)

    if page.rect.width <= 850:
        return [lines]

    middle = page.rect.width / 2

    left = []
    right = []

    for line in lines:
        center = (
            line["x"] + line["x2"]
        ) / 2

        if center < middle:
            left.append(line)
        else:
            right.append(line)

    return [left, right]


# ============================================================
# DETECTION DES LOTS
# ============================================================

def find_lots(lines):
    """
    Reconnaissance principale :
    artiste / dates / numero de lot + titre.

    On conserve les lignes dans l'ordre
    du contenu PDF.
    """

    entries = []
    seen = set()

    for i in range(2, len(lines)):
        biography_line = lines[i - 1]["text"]
        artist_line = lines[i - 2]["text"]
        lot_line = lines[i]["text"]

        if not valid_biography(biography_line):
            continue

        if not valid_artist(artist_line):
            continue

        match = LOT_PATTERN.fullmatch(lot_line)

        if not match:
            continue

        lot = int(match.group(1))
        title = clean(match.group(2))

        if not 1 <= lot <= 500:
            continue

        if not re.search(r"[A-Za-zÀ-ÿ]", title):
            continue

        key = (
            lot,
            round(lines[i]["x"], 1),
            round(lines[i]["y"], 1),
        )

        if key in seen:
            continue

        seen.add(key)

        entries.append({
            "artiste": normalize_artist(artist_line),
            "titre": title,
            "lot": lot,
            "index": i,
            "y": lines[i]["y"],
        })

    return entries


# ============================================================
# DIMENSIONS ET ESTIMATIONS
# ============================================================

def extract_metadata(lines, entries, position):
    entry = entries[position]

    start = entry["index"] + 1

    if position + 1 < len(entries):
        end = entries[position + 1]["index"] - 2
    else:
        end = len(lines)

    end = max(start, end)

    section = lines[start:min(end, start + 120)]

    dimensions = None
    candidates = []

    for i, line in enumerate(section):
        text = line["text"]

        if dimensions is None:
            match = DIM_PATTERN.search(text)

            if match:
                dimensions = clean(match.group())

        normalized = normalize(text)

        if (
            "estimation" in normalized
            or "estimate" in normalized
        ):
            for next_line in section[i:i + 5]:
                found = parse_estimate(
                    next_line["text"]
                )

                if found:
                    candidates.append(found)
                    break

    unique = list(dict.fromkeys(candidates))

    conflict = len(unique) > 1

    estimate = (
        unique[0]
        if len(unique) == 1
        else None
    )

    return dimensions, estimate, conflict


# ============================================================
# CREATION D'UNE NOTICE
# ============================================================

def create_notice(
    url,
    sale,
    page_number,
    entry,
    dimensions,
    estimate,
    conflict,
):
    lot = normalize_lot(entry["lot"])

    ident = make_id(
        "BYDealers",
        url,
        lot,
    )

    year_match = re.search(
        r"20\d{2}",
        sale,
    )

    year = (
        int(year_match.group())
        if year_match
        else None
    )

    minimum = (
        estimate[0]
        if estimate
        else None
    )

    maximum = (
        estimate[1]
        if estimate
        else None
    )

    estimate_text = (
        f"{minimum:,} - {maximum:,} CAD"
        if estimate
        else None
    )

    return {
        "id": ident,
        "id_passage": ident,
        "id_oeuvre": None,

        "maison": "BYDealers",
        "artiste": entry["artiste"],
        "titre": entry["titre"],
        "lot": lot,

        "dimensions": dimensions,
        "vente": sale,
        "annee_vente": year,

        "image": None,

        "prix": None,
        "prix_marteau": None,
        "prix_vendu": None,

        "estimation_min": minimum,
        "estimation_max": maximum,
        "estimation": estimate_text,

        "type_prix": (
            "estimation"
            if estimate
            else "inconnu"
        ),

        "devise": "CAD",
        "statut": "catalogue_historique",

        "url": url,
        "source_page": url,
        "source_page_pdf": page_number,

        "origine": "catalogue_pdf_v8_1",

        "confiance_extraction": (
            95
            if dimensions and estimate and not conflict
            else 85
            if dimensions
            else 75
        ),

        "conflit_estimation": conflict,
        "prix_realise_verifie": False,
        "date_collecte": now(),
    }


# ============================================================
# LECTURE COMPLETE DU PDF
# ============================================================

def parse_pdf(content, url, sale):
    results = []

    document = fitz.open(
        stream=content,
        filetype="pdf",
    )

    try:
        print(
            "Pages PDF :",
            len(document),
        )

        for page_number in range(len(document)):
            try:
                page = document.load_page(page_number)

                parts = split_columns(page)

                for part in parts:
                    entries = find_lots(part)

                    for index, entry in enumerate(entries):
                        dimensions, estimate, conflict = (
                            extract_metadata(
                                part,
                                entries,
                                index,
                            )
                        )

                        item = create_notice(
                            url=url,
                            sale=sale,
                            page_number=page_number + 1,
                            entry=entry,
                            dimensions=dimensions,
                            estimate=estimate,
                            conflict=conflict,
                        )

                        results.append(item)

            except Exception as exc:
                print(
                    "Erreur sur la page PDF",
                    page_number + 1,
                    ":",
                    str(exc),
                )

                traceback.print_exc()

                # Continuer les autres pages.
                continue

    finally:
        document.close()

    # Eviter deux detections du meme lot
    # dans un meme catalogue.
    unique = {}

    for item in results:
        key = record_id(item)

        if key not in unique:
            unique[key] = item

        else:
            previous = unique[key]

            if (
                previous.get("estimation_min") is None
                and item.get("estimation_min") is not None
            ):
                unique[key] = item

    return list(unique.values())


# ============================================================
# FUSION AVEC LA BASE EXISTANTE
# ============================================================

def merge_notice(old, new):
    merged = dict(old)

    # Conserver les informations historiques
    # qui ne sont pas retrouvees.
    for field in (
        "artiste",
        "titre",
        "dimensions",
        "vente",
        "annee_vente",
        "source_page_pdf",
    ):
        if new.get(field) is not None:
            merged[field] = new[field]

    # Une nouvelle estimation ne remplace
    # l'ancienne que si elle est complete
    # et non contradictoire.
    if (
        new.get("estimation_min") is not None
        and new.get("estimation_max") is not None
        and not new.get("conflit_estimation")
    ):
        for field in (
            "estimation_min",
            "estimation_max",
            "estimation",
            "type_prix",
        ):
            merged[field] = new[field]

    # Completer les champs absents.
    for field, value in new.items():
        if field not in merged:
            merged[field] = value

    merged["id_passage"] = record_id(new)
    merged["origine"] = "catalogue_pdf_v8_1"
    merged["conflit_estimation"] = new.get(
        "conflit_estimation",
        False,
    )
    merged["confiance_extraction"] = new.get(
        "confiance_extraction",
        75,
    )

    # Ne jamais ecraser les prix marteau
    # deja enregistres.
    return merged


# ============================================================
# DETECTION DES RAPPROCHEMENTS
# ============================================================

def dimension_key(value):
    match = DIM_PATTERN.search(
        clean(value)
    )

    if not match:
        return None

    numbers = re.findall(
        r"\d+(?:[.,]\d+)?",
        match.group(),
    )

    return tuple(sorted(
        round(
            float(x.replace(",", ".")),
            1,
        )
        for x in numbers
    ))


def find_matches(records):
    groups = defaultdict(list)

    for item in records:
        if not item.get("id_oeuvre"):
            item["id_oeuvre"] = make_id(
                "oeuvre-provisoire",
                record_id(item),
            )

        artist = normalize(
            item.get("artiste")
        )
        title = normalize(
            item.get("titre")
        )
        dimensions = dimension_key(
            item.get("dimensions")
        )

        if not artist or not title or not dimensions:
            continue

        key = (
            artist,
            title,
            dimensions,
        )

        groups[key].append(item)

    matches = []

    for group in groups.values():
        if len(group) < 2:
            continue

        passages = {
            record_id(item)
            for item in group
        }

        if len(passages) < 2:
            continue

        catalogues = {
            item.get("url")
            for item in group
        }

        same_catalogue = (
            len(catalogues) == 1
        )

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

            "passages": [
                {
                    "id_passage": record_id(item),
                    "vente": item.get("vente"),
                    "lot": item.get("lot"),
                    "url": item.get("url"),
                }
                for item in group
            ],
        })

    return matches


# ============================================================
# RAPPORT QUALITE
# ============================================================

def build_quality(records, matches):
    anomalies = []

    for item in records:
        problems = []

        if not item.get("dimensions"):
            problems.append(
                "dimensions_absentes"
            )

        if item.get("estimation_min") is None:
            problems.append(
                "estimation_absente"
            )

        if item.get("conflit_estimation"):
            problems.append(
                "conflit_estimation"
            )

        title = clean(
            item.get("titre")
        )

        if title.endswith(
            ("/", "-", ",", ":")
        ):
            problems.append(
                "titre_possiblement_tronque"
            )

        artist = clean(
            item.get("artiste")
        )

        if re.search(
            r"\(\s*suite\s*\)",
            artist,
            flags=re.IGNORECASE,
        ):
            problems.append(
                "nom_artiste_a_normaliser"
            )

        if problems:
            anomalies.append({
                "id": record_id(item),
                "artiste": item.get("artiste"),
                "titre": item.get("titre"),
                "problemes": problems,
                "url": item.get("url"),
                "page": item.get("source_page_pdf"),
            })

    return {
        "version": VERSION,
        "date": now(),

        "total_oeuvres": len(records),

        "avec_dimensions": sum(
            bool(item.get("dimensions"))
            for item in records
        ),

        "avec_estimation": sum(
            item.get("estimation_min") is not None
            for item in records
        ),

        "prix_marteau_verifies": sum(
            bool(item.get("prix_realise_verifie"))
            for item in records
        ),

        "rapprochements_potentiels": matches,
        "nombre_rapprochements": len(matches),

        "anomalies": anomalies,
        "nombre_notices_a_controler": len(anomalies),
    }


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def main():
    print("=" * 60)
    print("ART ENCHERES QUEBEC", VERSION)
    print("=" * 60)

    old_records = load_database()

    database = {
        record_id(item): item
        for item in old_records
    }

    print(
        "Notices historiques :",
        len(database),
    )

    stats = {
        "pages_consultees": 0,
        "pages_accessibles": 0,
        "pdf_lus": 0,
        "oeuvres_detectees": 0,
        "nouvelles_oeuvres": 0,
        "erreurs_ou_blocages": 0,
    }

    catalogue_reports = []

    for sale, url in CATALOGUES.items():
        print()
        print("-" * 60)
        print("CATALOGUE :", sale)
        print("URL :", url)

        stats["pages_consultees"] += 1

        content = download_pdf(url)

        if content is None:
            stats["erreurs_ou_blocages"] += 1

            catalogue_reports.append({
                "vente": sale,
                "statut": "telechargement_impossible",
                "oeuvres": 0,
            })

            continue

        stats["pages_accessibles"] += 1

        try:
            found = parse_pdf(
                content,
                url,
                sale,
            )

            stats["pdf_lus"] += 1
            stats["oeuvres_detectees"] += len(found)

            print(
                "Notices detectees :",
                len(found),
            )

            catalogue_reports.append({
                "vente": sale,
                "statut": (
                    "ok"
                    if found
                    else "aucun_lot_detecte"
                ),
                "oeuvres": len(found),
            })

            for item in found:
                key = record_id(item)

                if key in database:
                    database[key] = merge_notice(
                        database[key],
                        item,
                    )

                else:
                    database[key] = item
                    stats["nouvelles_oeuvres"] += 1

        except Exception as exc:
            stats["erreurs_ou_blocages"] += 1

            print()
            print("ERREUR EXTRACTION PDF")
            print("Catalogue :", sale)
            print("Type :", type(exc).__name__)
            print("Message :", str(exc))
            traceback.print_exc()

            catalogue_reports.append({
                "vente": sale,
                "statut": "erreur_extraction",
                "oeuvres": 0,
                "erreur": str(exc),
            })

            continue

    # ========================================================
    # PROTECTION CONTRE UNE COLLECTE ENTIEREMENT ECHOUEE
    # ========================================================

    if stats["oeuvres_detectees"] == 0:
        raise RuntimeError(
            "AUCUNE NOTICE EXTRAITE. "
            "Les fichiers JSON existants ne seront "
            "pas remplaces. Consulter les journaux."
        )

    # ========================================================
    # PREPARATION DES RESULTATS
    # ========================================================

    records = sorted(
        database.values(),
        key=lambda item: (
            normalize(item.get("artiste")),
            normalize(item.get("titre")),
            str(item.get("annee_vente") or ""),
            normalize_lot(item.get("lot")),
        ),
    )

    matches = find_matches(records)

    quality = build_quality(
        records,
        matches,
    )

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

        "catalogues": catalogue_reports,

        "total_oeuvres": len(records),
        "avec_estimation": quality["avec_estimation"],

        "rapprochements_potentiels": len(matches),

        "note": (
            "Les estimations des catalogues ne sont "
            "pas des prix marteau verifies."
        ),
    }

    # ========================================================
    # SAUVEGARDE
    # ========================================================

    save_json(DB_FILE, records)
    save_json(REPORT_FILE, report)
    save_json(QUALITY_FILE, quality)

    print()
    print("=" * 60)
    print("RESULTATS", VERSION)
    print("=" * 60)

    print("Notices :", len(records))
    print(
        "Nouvelles notices :",
        stats["nouvelles_oeuvres"],
    )
    print(
        "Notices detectees :",
        stats["oeuvres_detectees"],
    )
    print(
        "Estimations :",
        quality["avec_estimation"],
    )
    print(
        "Dimensions :",
        quality["avec_dimensions"],
    )
    print(
        "Rapprochements :",
        quality["nombre_rapprochements"],
    )
    print(
        "Notices a controler :",
        quality["nombre_notices_a_controler"],
    )
    print(
        "Erreurs :",
        stats["erreurs_ou_blocages"],
    )

    print()
    print("Collecte terminee.")


if __name__ == "__main__":
    main()
