
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ART ENCHERES QUEBEC - COLLECTEUR V8.6

OBJECTIFS
---------
- Lire les 8 catalogues historiques BYDealers.
- Conserver toutes les notices deja presentes.
- Eviter les fausses estimations issues de dates.
- Eviter les faux conflits d'extraction.
- Completer les titres multilignes avec prudence.
- Conserver les identifiants historiques.
- Ne jamais inventer de prix marteau.
- Generer quatre fichiers JSON.

FICHIERS PRODUITS
-----------------
data/oeuvres.json
data/rapport-collecte.json
data/rapport-qualite.json
data/rapport-differences.json

DEPENDANCES
-----------
requests>=2.32,<3
pymupdf>=1.24,<2
"""

import hashlib
import json
import re
import time
import traceback
import unicodedata

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import fitz
import requests


# ============================================================
# CONFIGURATION
# ============================================================

VERSION = "V8.6"

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

DB_FILE = DATA / "oeuvres.json"
REPORT_FILE = DATA / "rapport-collecte.json"
QUALITY_FILE = DATA / "rapport-qualite.json"
DIFF_FILE = DATA / "rapport-differences.json"

USER_AGENT = "ArtEncheresQuebec/8.6"
TIMEOUT = 45
MAX_BYTES = 40 * 1024 * 1024
PAUSE_SECONDS = 2

MIN_HISTORICAL_RECORDS = 491

CATALOGUES = {
    "Printemps 2023":
        "https://bydealers.com/wp-content/uploads/2023/04/"
        "Catalogue_108p_Printemps2023_Web_Spread.pdf",

    "Automne 2023":
        "https://bydealers.com/wp-content/uploads/2026/02/"
        "Catalogue_Fix_Automne_2023-1.pdf",

    "Printemps 2024":
        "https://bydealers.com/wp-content/uploads/2024/04/"
        "Catalogue_BYD_Printemps2024_Double.pdf",

    "Automne 2024":
        "https://bydealers.com/wp-content/uploads/2024/10/"
        "Catalogue_106p_Automne2024_Web_Double_pages.pdf",

    "Printemps 2025":
        "https://bydealers.com/wp-content/uploads/2025/12/"
        "Catalogue_Printemps2025_Web_Double_pages.pdf",

    "Automne 2025":
        "https://bydealers.com/wp-content/uploads/2026/02/"
        "Catalogue_Fall_2025-1.pdf",

    "Printemps 2026":
        "https://bydealers.com/wp-content/uploads/2026/04/"
        "Catalogue_Printemps2026.pdf",

    "Succession printemps 2026":
        "https://bydealers.com/wp-content/uploads/2026/05/"
        "Catalogue_102p_Printemps_succession2026_Web_simple-1.pdf",
}

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/pdf,*/*"
})

ROBOTS_CACHE = {}


# ============================================================
# EXPRESSIONS REGULIERES
# ============================================================

BIO_RE = re.compile(
    r"^\s*([12]\s*\d\s*\d\s*\d)"
    r"\s*[-–—]\s*"
    r"([12]\s*\d\s*\d\s*\d)?\s*$"
)

LOT_RE = re.compile(
    r"^\s*(\d{1,3})\s+(.{2,250})$"
)

DIM_RE = re.compile(
    r"\d{1,4}(?:[.,]\d+)?"
    r"\s*[x×]\s*"
    r"\d{1,4}(?:[.,]\d+)?"
    r"(?:\s*[x×]\s*\d{1,4}(?:[.,]\d+)?)?"
    r"\s*cm\b",
    re.I
)

EST_LABEL_RE = re.compile(
    r"\b(?:estimation|estimate|estimated|"
    r"évaluation|evaluation)\b",
    re.I
)

MONEY_RANGE_RE = re.compile(
    r"(?<![\d])"
    r"(\d{1,3}(?:[\s\u00a0,.]\d{3})+|\d{4,8})"
    r"\s*[-–—]\s*"
    r"(\d{1,3}(?:[\s\u00a0,.]\d{3})+|\d{4,8})"
    r"\s*(?:\$|CAD)?",
    re.I
)

TITLE_ENDINGS = ("/", ":", "-", "–", "—", ",")

STOP_TITLE_RE = re.compile(
    r"^(?:huile|oil|acrylique|acrylic|"
    r"encre|ink|aquarelle|watercolour|"
    r"lithographie|lithograph|"
    r"sérigraphie|serigraph|"
    r"sculpture|bronze|"
    r"signé|signed|daté|dated|"
    r"provenance|exposition|exhibition|"
    r"bibliographie|literature|"
    r"estimation|estimate)\b",
    re.I
)

# Deux identifiants provenant de la V8.4/V8.5
# pour lesquels une fourchette d'annees a ete
# interpretee comme une estimation.
SUSPECT_ESTIMATE_IDS = {
    "c5aca100f04adb09c7ee",
    "c60ac7b3013233ec7a51",
}


# ============================================================
# UTILITAIRES
# ============================================================

def timestamp():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    text = unicodedata.normalize(
        "NFKC",
        str(value or "")
    )

    return re.sub(r"\s+", " ", text).strip()


def normalize(value):
    text = unicodedata.normalize(
        "NFKD",
        clean(value)
    )

    return "".join(
        char for char in text
        if not unicodedata.combining(char)
    ).casefold()


def digest(*parts):
    value = "|".join(
        normalize(part)
        for part in parts
    )

    return hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()[:20]


def normalize_lot(value):
    try:
        return f"{int(str(value).strip()):02d}"
    except (TypeError, ValueError):
        return clean(value)


def passage_key(item):
    if item.get("url") and item.get("lot"):
        return digest(
            item.get("maison", "BYDealers"),
            item["url"],
            normalize_lot(item["lot"])
        )

    return clean(
        item.get("id_passage")
        or item.get("id")
    )


def load_database():
    if not DB_FILE.exists():
        return []

    data = json.loads(
        DB_FILE.read_text(encoding="utf-8")
    )

    if isinstance(data, dict):
        data = data.get("oeuvres", [])

    if not isinstance(data, list):
        raise ValueError(
            "oeuvres.json doit contenir une liste."
        )

    if not all(isinstance(x, dict) for x in data):
        raise ValueError(
            "La base contient des notices invalides."
        )

    return data


def save_json(path, content):
    temporary = path.with_suffix(
        path.suffix + ".tmp"
    )

    temporary.write_text(
        json.dumps(
            content,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    temporary.replace(path)


# ============================================================
# VERIFICATION DES ESTIMATIONS
# ============================================================

def is_year_range(low, high):
    return (
        1800 <= low <= 2100
        and 1800 <= high <= 2100
    )


def parse_amount(text):
    digits = re.sub(
        r"\D",
        "",
        str(text)
    )

    return int(digits) if digits else None


def parse_estimate(text):
    """
    Extraction prudente.

    Conditions :
    - Deux montants coherents.
    - Pas de fourchette d'annees.
    - Minimum 1 000 CAD.
    - Maximum 100 millions CAD.

    Une estimation ne sera acceptee par
    extract_estimate() que dans un contexte
    explicitement associe aux estimations.
    """

    text = clean(text)

    if DIM_RE.search(text):
        return None

    candidates = []

    for match in MONEY_RANGE_RE.finditer(text):
        low = parse_amount(match.group(1))
        high = parse_amount(match.group(2))

        if low is None or high is None:
            continue

        if is_year_range(low, high):
            continue

        if not (
            1000 <= low <= high <= 100_000_000
        ):
            continue

        if high > low * 20:
            continue

        candidates.append((low, high))

    candidates = list(
        dict.fromkeys(candidates)
    )

    if len(candidates) == 1:
        return candidates[0]

    return None


def extract_estimate(section):
    """
    Cherche seulement les estimations
    explicitement identifiees.

    Ne parcourt pas les lots suivants.
    """

    candidates = []

    for i, line in enumerate(section):
        text = line["text"]

        if not EST_LABEL_RE.search(text):
            continue

        # Estimation sur la meme ligne.
        value = parse_estimate(text)

        if value:
            candidates.append(value)
            continue

        # Estimation sur les deux lignes suivantes.
        for j in range(
            i + 1,
            min(i + 3, len(section))
        ):
            candidate = section[j]["text"]

            if (
                LOT_RE.fullmatch(candidate)
                or BIO_RE.fullmatch(candidate)
                or DIM_RE.search(candidate)
                or STOP_TITLE_RE.search(candidate)
            ):
                break

            value = parse_estimate(candidate)

            if value:
                candidates.append(value)
                break

    candidates = list(
        dict.fromkeys(candidates)
    )

    if len(candidates) == 1:
        return candidates[0], False

    if len(candidates) > 1:
        return None, True

    return None, False


# ============================================================
# NETTOYAGE DES ESTIMATIONS HISTORIQUES
# ============================================================

def correct_historical_estimates(database):
    """
    Retire uniquement les deux fausses
    estimations identifiees.

    Une estimation historique valide
    ne sera jamais supprimee.
    """

    corrections = []

    for identifier in SUSPECT_ESTIMATE_IDS:
        item = database.get(identifier)

        if item is None:
            continue

        low = item.get("estimation_min")
        high = item.get("estimation_max")

        if not (
            isinstance(low, (int, float))
            and isinstance(high, (int, float))
        ):
            continue

        if not is_year_range(low, high):
            continue

        corrections.append({
            "id_passage": identifier,
            "ancienne_estimation": [low, high],
            "motif": "annees_interpretees_comme_prix"
        })

        item["estimation_min"] = None
        item["estimation_max"] = None
        item["estimation"] = None
        item["type_prix"] = "inconnu"
        item["conflit_estimation"] = False

    return corrections


# ============================================================
# ROBOTS.TXT ET TELECHARGEMENT
# ============================================================

def authorized(url):
    parsed = urlparse(url)

    if (
        parsed.scheme != "https"
        or parsed.netloc != "bydealers.com"
    ):
        return False

    origin = "https://bydealers.com"

    if origin not in ROBOTS_CACHE:
        response = SESSION.get(
            origin + "/robots.txt",
            timeout=TIMEOUT,
            allow_redirects=False
        )

        if response.status_code == 200:
            robot = RobotFileParser()
            robot.parse(
                response.text.splitlines()
            )
            ROBOTS_CACHE[origin] = robot

        elif response.status_code in (404, 410):
            ROBOTS_CACHE[origin] = True

        else:
            ROBOTS_CACHE[origin] = False

    rule = ROBOTS_CACHE[origin]

    if isinstance(rule, bool):
        return rule

    return rule.can_fetch(
        USER_AGENT,
        url
    )


def download_pdf(url):
    if not authorized(url):
        raise PermissionError(
            "Acces non autorise au catalogue."
        )

    with SESSION.get(
        url,
        timeout=TIMEOUT,
        stream=True,
        allow_redirects=False
    ) as response:

        if response.is_redirect:
            raise RuntimeError(
                "Redirection non autorisee."
            )

        response.raise_for_status()

        data = bytearray()

        for chunk in response.iter_content(
            chunk_size=65536
        ):
            if not chunk:
                continue

            data.extend(chunk)

            if len(data) > MAX_BYTES:
                raise ValueError(
                    "Catalogue PDF trop volumineux."
                )

    if not bytes(data).startswith(b"%PDF"):
        raise ValueError(
            "Le document recu n'est pas un PDF."
        )

    time.sleep(PAUSE_SECONDS)

    return bytes(data)


# ============================================================
# EXTRACTION DES LIGNES PDF
# ============================================================

def page_lines(page):
    result = []

    blocks = page.get_text(
        "dict",
        sort=False
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

            bbox = line.get("bbox")

            if not text or not bbox:
                continue

            result.append({
                "text": text,
                "x": bbox[0],
                "y": bbox[1],
                "x2": bbox[2],
                "y2": bbox[3]
            })

    return result


def split_columns(page):
    lines = page_lines(page)

    if page.rect.width <= 850:
        return [lines]

    midpoint = page.rect.width / 2

    left = []
    right = []

    for line in lines:
        center = (
            line["x"] + line["x2"]
        ) / 2

        if center < midpoint:
            left.append(line)
        else:
            right.append(line)

    return [left, right]


# ============================================================
# DETECTION DES ARTISTES
# ============================================================

def valid_biography(text):
    match = BIO_RE.fullmatch(
        clean(text)
    )

    if not match:
        return False

    born = int(
        re.sub(r"\D", "", match.group(1))
    )

    died = None

    if match.group(2):
        died = int(
            re.sub(r"\D", "", match.group(2))
        )

    return (
        1700 <= born <= 2026
        and (
            died is None
            or born <= died <= 2026
        )
    )


def valid_artist(text):
    text = clean(text)
    words = text.split()

    if not (
        5 <= len(text) <= 85
        and 2 <= len(words) <= 8
    ):
        return False

    if re.search(
        r"\d|https?://|[!?;]",
        text
    ):
        return False

    blocked = {
        "production",
        "while",
        "loosening",
        "knots",
        "recent",
        "recently",
        "following",
        "collection",
        "provenance",
        "exhibition",
        "exposition",
        "museum",
        "musee",
        "artist",
        "artiste",
        "because",
        "through",
        "catalogue"
    }

    particles = {
        "de", "du", "des",
        "la", "le",
        "van", "von",
        "der", "den",
        "da", "di",
        "del", "and", "et"
    }

    for i, word in enumerate(words):
        base = word.strip("()")

        if normalize(base) in blocked:
            return False

        if not re.match(
            r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’().-]*$",
            word
        ):
            return False

        if i > 0 and normalize(base) in particles:
            continue

        if not base or not base[0].isupper():
            return False

    return True


def lot_header(text):
    match = LOT_RE.fullmatch(
        clean(text)
    )

    if not match:
        return None

    number = int(match.group(1))
    title = clean(match.group(2))

    if not (
        1 <= number <= 500
        and re.search(r"[A-Za-zÀ-ÿ]", title)
    ):
        return None

    return number, title


# ============================================================
# DETECTION DES LOTS
# ============================================================

def detect_lots(lines, historical_artists):
    entries = []

    for i, line in enumerate(lines):
        header = lot_header(
            line["text"]
        )

        if not header:
            continue

        number, title = header
        artist = None

        for bio_index in range(
            max(0, i - 3),
            i
        ):
            if not valid_biography(
                lines[bio_index]["text"]
            ):
                continue

            for artist_index in range(
                max(0, bio_index - 2),
                bio_index
            ):
                candidate = clean(
                    lines[artist_index]["text"]
                )

                if valid_artist(candidate):
                    artist = candidate
                    break

            if artist:
                break

        if not artist:
            previous = historical_artists.get(
                normalize_lot(number)
            )

            has_biography = any(
                valid_biography(lines[j]["text"])
                for j in range(
                    max(0, i - 3),
                    i
                )
            )

            if previous and has_biography:
                artist = previous

        if artist:
            entries.append({
                "index": i,
                "lot": number,
                "titre": title,
                "artiste": artist
            })

    return entries


# ============================================================
# RECONSTRUCTION DES TITRES
# ============================================================

def reconstruct_title(lines, entry, end):
    original = clean(
        entry["titre"]
    )

    if not original.endswith(
        TITLE_ENDINGS
    ):
        return original

    index = entry["index"]

    if index + 1 >= end:
        return original

    first = lines[index]
    second = lines[index + 1]

    continuation = clean(
        second["text"]
    )

    if not continuation:
        return original

    if len(continuation) > 120:
        return original

    if (
        lot_header(continuation)
        or valid_biography(continuation)
        or DIM_RE.search(continuation)
        or STOP_TITLE_RE.search(continuation)
        or EST_LABEL_RE.search(continuation)
    ):
        return original

    if not re.search(
        r"[A-Za-zÀ-ÿ]",
        continuation
    ):
        return original

    vertical_gap = (
        second["y"] - first["y2"]
    )

    if not (
        -3 <= vertical_gap <= 24
    ):
        return original

    if abs(
        second["x"] - first["x"]
    ) > 45:
        return original

    result = clean(
        original + " " + continuation
    )

    if len(result) > 250:
        return original

    return result


def safe_title_extension(old, new):
    old = clean(old)
    new = clean(new)

    if not old.endswith(
        TITLE_ENDINGS
    ):
        return False

    if len(new) <= len(old):
        return False

    if not normalize(new).startswith(
        normalize(old)
    ):
        return False

    extension = new[len(old):].strip()

    if not extension or len(extension) > 120:
        return False

    if (
        STOP_TITLE_RE.search(extension)
        or DIM_RE.search(extension)
        or EST_LABEL_RE.search(extension)
    ):
        return False

    return True


# ============================================================
# EXTRACTION DES METADONNEES
# ============================================================

def extract_metadata(lines, entries, index):
    entry = entries[index]

    start = entry["index"] + 1

    if index + 1 < len(entries):
        end = entries[index + 1]["index"]
    else:
        end = len(lines)

    section = lines[
        start:min(end, start + 140)
    ]

    dimensions = None

    for line in section:
        match = DIM_RE.search(
            line["text"]
        )

        if match:
            dimensions = clean(
                match.group()
            )
            break

    estimation, conflict = extract_estimate(
        section
    )

    title = reconstruct_title(
        lines,
        entry,
        end
    )

    return (
        dimensions,
        estimation,
        conflict,
        title
    )


# ============================================================
# CREATION D'UNE NOTICE
# ============================================================

def create_notice(
    url,
    sale,
    page,
    entry,
    dimensions,
    estimation,
    conflict,
    title
):
    lot = normalize_lot(
        entry["lot"]
    )

    identifier = digest(
        "BYDealers",
        url,
        lot
    )

    year = re.search(
        r"20\d{2}",
        sale
    )

    low, high = (
        estimation
        if estimation
        else (None, None)
    )

    return {
        "id": identifier,
        "id_passage": identifier,
        "id_oeuvre": digest(
            "oeuvre",
            identifier
        ),

        "maison": "BYDealers",
        "artiste": entry["artiste"],
        "titre": title,
        "lot": lot,
        "dimensions": dimensions,

        "vente": sale,
        "annee_vente": (
            int(year.group())
            if year else None
        ),

        "image": None,

        "prix": None,
        "prix_marteau": None,
        "prix_vendu": None,

        "estimation_min": low,
        "estimation_max": high,

        "estimation": (
            f"{low:,} - {high:,} CAD"
            if estimation
            else None
        ),

        "type_prix": (
            "estimation"
            if estimation
            else "inconnu"
        ),

        "devise": "CAD",
        "statut": "catalogue_historique",

        "url": url,
        "source_page": url,
        "source_page_pdf": page,

        "origine": "catalogue_pdf_v8_6",

        "confiance_extraction": (
            90
            if dimensions and estimation and not conflict
            else 65
        ),

        "conflit_estimation": conflict,
        "prix_realise_verifie": False,
        "date_collecte": timestamp()
    }


# ============================================================
# ANALYSE D'UN PDF
# ============================================================

def parse_pdf(data, url, sale, known):
    detected = {}
    page_errors = []

    with fitz.open(
        stream=data,
        filetype="pdf"
    ) as document:

        total_pages = len(document)

        for page_index in range(total_pages):
            try:
                page = document[page_index]

                for lines in split_columns(page):
                    entries = detect_lots(
                        lines,
                        known
                    )

                    for i, entry in enumerate(entries):
                        (
                            dimensions,
                            estimation,
                            conflict,
                            title
                        ) = extract_metadata(
                            lines,
                            entries,
                            i
                        )

                        item = create_notice(
                            url,
                            sale,
                            page_index + 1,
                            entry,
                            dimensions,
                            estimation,
                            conflict,
                            title
                        )

                        key = passage_key(item)

                        if key not in detected:
                            detected[key] = item
                            continue

                        previous = detected[key]

                        old_score = sum([
                            bool(previous.get("dimensions")),
                            previous.get("estimation_min") is not None,
                            not previous.get("conflit_estimation")
                        ])

                        new_score = sum([
                            bool(item.get("dimensions")),
                            item.get("estimation_min") is not None,
                            not item.get("conflit_estimation")
                        ])

                        if new_score > old_score:
                            detected[key] = item

            except Exception as exc:
                page_errors.append({
                    "page": page_index + 1,
                    "erreur": str(exc)
                })

                traceback.print_exc()

    return (
        list(detected.values()),
        total_pages,
        page_errors
    )


# ============================================================
# FUSION SANS PERTE
# ============================================================

def merge_notice(old, new):
    """
    Regles :
    - Ne jamais remplacer un prix realise.
    - Ne jamais remplacer un identifiant.
    - Ne jamais remplacer une estimation
      historique par une estimation differente.
    - Conserver les differences pour verification.
    - Completer les titres uniquement si
      la continuation est coherente.
    """

    merged = dict(old)

    additions = []
    conflicts = []
    title_corrections = []

    for field in (
        "artiste",
        "titre",
        "dimensions"
    ):
        current = old.get(field)
        incoming = new.get(field)

        if incoming and not current:
            merged[field] = incoming
            additions.append(field)
            continue

        if not current or not incoming:
            continue

        if normalize(current) == normalize(incoming):
            continue

        if field == "artiste":
            if not valid_artist(incoming):
                continue

            # Les noms d'artistes historiques
            # sont prioritaires.
            # Les differences restent documentees.
            conflicts.append({
                "champ": field,
                "ancien": current,
                "extrait": incoming
            })
            continue

        if field == "titre":
            if safe_title_extension(
                current,
                incoming
            ):
                merged[field] = incoming

                title_corrections.append({
                    "champ": "titre",
                    "ancien": current,
                    "nouveau": incoming,
                    "motif": "continuation_multiligne"
                })

                continue

            if normalize(current).startswith(
                normalize(incoming)
            ):
                continue

            conflicts.append({
                "champ": field,
                "ancien": current,
                "extrait": incoming
            })
            continue

        if field == "dimensions":
            # Ne pas remplacer une dimension
            # historique contradictoire.
            conflicts.append({
                "champ": field,
                "ancien": current,
                "extrait": incoming
            })

    for field in (
        "vente",
        "annee_vente",
        "source_page_pdf"
    ):
        if (
            merged.get(field) is None
            and new.get(field) is not None
        ):
            merged[field] = new[field]
            additions.append(field)

    old_estimation = (
        old.get("estimation_min"),
        old.get("estimation_max")
    )

    new_estimation = (
        new.get("estimation_min"),
        new.get("estimation_max")
    )

    if all(
        value is not None
        for value in new_estimation
    ):
        if all(
            value is None
            for value in old_estimation
        ):
            for field in (
                "estimation_min",
                "estimation_max",
                "estimation"
            ):
                merged[field] = new[field]

            merged["type_prix"] = "estimation"
            additions.append("estimation")

        elif old_estimation != new_estimation:
            conflicts.append({
                "champ": "estimation",
                "ancien": old_estimation,
                "extrait": new_estimation,
                "verification": "manuelle_requise"
            })

    if new.get("conflit_estimation"):
        conflicts.append({
            "champ": "estimation",
            "motif": "plusieurs_estimations_detectees"
        })

    merged["id_passage"] = passage_key(old)

    return (
        merged,
        additions,
        conflicts,
        title_corrections
    )


# ============================================================
# RAPPROCHEMENTS D'OEUVRES
# ============================================================

def dimensions_key(value):
    match = DIM_RE.search(
        clean(value)
    )

    if not match:
        return None

    numbers = re.findall(
        r"\d+(?:[.,]\d+)?",
        match.group()
    )

    return tuple(
        sorted(
            round(
                float(number.replace(",", ".")),
                1
            )
            for number in numbers
        )
    )


def find_potential_matches(records):
    groups = defaultdict(list)

    for item in records:
        dimensions = dimensions_key(
            item.get("dimensions")
        )

        if not (
            dimensions
            and item.get("artiste")
            and item.get("titre")
        ):
            continue

        key = (
            normalize(item["artiste"]),
            normalize(item["titre"]),
            dimensions
        )

        groups[key].append(item)

    matches = []

    for group in groups.values():
        if len({
            passage_key(x)
            for x in group
        }) < 2:
            continue

        same_catalogue = (
            len({
                x.get("url")
                for x in group
            }) == 1
        )

        matches.append({
            "artiste": group[0]["artiste"],
            "titre": group[0]["titre"],
            "dimensions": group[0]["dimensions"],

            "verification": "requise",

            "risque": (
                "lots_distincts_meme_catalogue"
                if same_catalogue
                else "possible_remise_en_vente"
            ),

            "passages": [
                {
                    "id_passage": passage_key(x),
                    "vente": x.get("vente"),
                    "lot": x.get("lot"),
                    "url": x.get("url")
                }
                for x in group
            ]
        })

    return matches


# ============================================================
# RAPPORT DE QUALITE
# ============================================================

def build_quality_report(records, matches):
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

        if clean(
            item.get("titre")
        ).endswith(TITLE_ENDINGS):
            problems.append(
                "titre_possiblement_tronque"
            )

        if item.get("conflit_estimation"):
            problems.append(
                "conflit_estimation"
            )

        if problems:
            anomalies.append({
                "id": item.get("id"),
                "id_passage": passage_key(item),
                "artiste": item.get("artiste"),
                "titre": item.get("titre"),
                "problemes": problems,
                "url": item.get("url"),
                "page": item.get("source_page_pdf")
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
            x.get("estimation_min") is not None
            for x in records
        ),

        "prix_marteau_verifies": sum(
            x.get("prix_realise_verifie") is True
            for x in records
        ),

        "rapprochements_potentiels": matches,

        "nombre_rapprochements": len(matches),

        "anomalies": anomalies,

        "nombre_notices_a_controler": len(anomalies)
    }


# ============================================================
# EXECUTION PRINCIPALE
# ============================================================

def main():
    original = load_database()

    print(
        f"{VERSION} : "
        f"{len(original)} notices historiques",
        flush=True
    )

    database = {}

    for item in original:
        key = passage_key(item)

        if not key:
            raise ValueError(
                "Notice sans identifiant."
            )

        if key in database:
            raise ValueError(
                f"Doublon historique : {key}"
            )

        database[key] = dict(item)

    historical_keys = set(database)

    # Correction ciblee des deux fausses
    # estimations historiques.
    corrected_estimates = (
        correct_historical_estimates(database)
    )

    detected = set()

    new_passages = []
    additions = []
    conflicts = []
    title_corrections = []
    catalogues = []

    stats = {
        "pages_consultees": 0,
        "pages_accessibles": 0,
        "pdf_lus": 0,
        "oeuvres_detectees": 0,
        "nouvelles_oeuvres": 0,
        "erreurs_ou_blocages": 0
    }

    # --------------------------------------------------------
    # COLLECTE
    # --------------------------------------------------------

    for sale, url in CATALOGUES.items():
        print(
            f"\nCATALOGUE : {sale}",
            flush=True
        )

        stats["pages_consultees"] += 1

        try:
            data = download_pdf(url)

            stats["pages_accessibles"] += 1

            historical_artists = {
                normalize_lot(x.get("lot")):
                    x.get("artiste")
                for x in database.values()
                if (
                    x.get("url") == url
                    and x.get("lot")
                    and x.get("artiste")
                )
            }

            found, pages, page_errors = parse_pdf(
                data,
                url,
                sale,
                historical_artists
            )

            stats["pdf_lus"] += 1
            stats["oeuvres_detectees"] += len(found)

            stats["erreurs_ou_blocages"] += len(
                page_errors
            )

            catalogues.append({
                "vente": sale,

                "statut": (
                    "ok"
                    if found and not page_errors
                    else "a_verifier"
                ),

                "oeuvres": len(found),
                "pages": pages,
                "erreurs_pages": page_errors
            })

            print(
                f"Pages : {pages} | "
                f"Lots : {len(found)} | "
                f"Erreurs : {len(page_errors)}",
                flush=True
            )

            for item in found:
                key = passage_key(item)
                detected.add(key)

                if key in database:
                    (
                        merged,
                        added,
                        problems,
                        corrected_titles
                    ) = merge_notice(
                        database[key],
                        item
                    )

                    database[key] = merged

                    if added:
                        additions.append({
                            "id_passage": key,
                            "champs": added
                        })

                    if problems:
                        conflicts.append({
                            "id_passage": key,
                            "vente": sale,
                            "lot": item["lot"],
                            "conflits": problems
                        })

                    if corrected_titles:
                        title_corrections.append({
                            "id_passage": key,
                            "vente": sale,
                            "lot": item["lot"],
                            "corrections": corrected_titles
                        })

                else:
                    database[key] = item
                    new_passages.append(key)

                    stats["nouvelles_oeuvres"] += 1

        except Exception as exc:
            stats["erreurs_ou_blocages"] += 1

            print(
                f"ERREUR : {sale} : {exc}",
                flush=True
            )

            traceback.print_exc()

            catalogues.append({
                "vente": sale,
                "statut": "erreur",
                "oeuvres": 0,
                "erreur": str(exc)
            })

    # --------------------------------------------------------
    # PROTECTION HISTORIQUE
    # --------------------------------------------------------

    if not detected:
        raise RuntimeError(
            "Aucun lot detecte. "
            "Sauvegarde annulee."
        )

    records = sorted(
        database.values(),
        key=lambda x: (
            normalize(x.get("artiste")),
            normalize(x.get("titre")),
            str(x.get("annee_vente") or ""),
            normalize_lot(x.get("lot"))
        )
    )

    if len(records) < len(original):
        raise RuntimeError(
            "Regression du nombre de notices."
        )

    if (
        len(original) >= MIN_HISTORICAL_RECORDS
        and len(records) < MIN_HISTORICAL_RECORDS
    ):
        raise RuntimeError(
            "Les 491 notices historiques "
            "ne sont plus presentes."
        )

    missing = sorted(
        historical_keys - detected
    )

    matches = find_potential_matches(
        records
    )

    quality = build_quality_report(
        records,
        matches
    )

    # --------------------------------------------------------
    # RAPPORT DES DIFFERENCES
    # --------------------------------------------------------

    differences = {
        "version": VERSION,
        "date": timestamp(),

        "historique_avant": len(original),

        "detectes_cette_execution": len(detected),

        "non_redetectes": len(missing),

        "lots_non_redetectes": [
            {
                "id_passage": key,
                "vente": database[key].get("vente"),
                "lot": database[key].get("lot"),
                "artiste": database[key].get("artiste"),
                "titre": database[key].get("titre")
            }
            for key in missing
        ],

        "nouveaux_passages": new_passages,

        "champs_completes": additions,

        "conflits_a_verifier": conflicts,

        "corrections_appliquees": title_corrections,

        "estimations_historiques_corrigees":
            corrected_estimates
    }

    # --------------------------------------------------------
    # RAPPORT DE COLLECTE
    # --------------------------------------------------------

    report = {
        "version": VERSION,
        "date": timestamp(),

        "sources": {
            "BYDealers": stats,

            "Heffel": {
                "statut": "connecteur_a_developper",
                "oeuvres_detectees": 0
            },

            "Gelineau": {
                "statut": "connecteur_a_developper",
                "oeuvres_detectees": 0
            },

            "Iegor": {
                "statut": "connecteur_a_developper",
                "oeuvres_detectees": 0
            }
        },

        "catalogues": catalogues,

        "total_oeuvres": len(records),

        "avec_estimation": quality[
            "avec_estimation"
        ],

        "avec_dimensions": quality[
            "avec_dimensions"
        ],

        "rapprochements_potentiels": len(
            matches
        ),

        "lots_historiques_non_redetectes": len(
            missing
        ),

        "conflits_a_verifier": len(
            conflicts
        ),

        "corrections_titres": len(
            title_corrections
        ),

        "estimations_corrigees": len(
            corrected_estimates
        ),

        "note": (
            "Estimations de catalogues seulement. "
            "Aucun prix marteau invente."
        )
    }

    # --------------------------------------------------------
    # VERIFICATIONS AVANT SAUVEGARDE
    # --------------------------------------------------------

    ids = [
        passage_key(x)
        for x in records
    ]

    if len(ids) != len(set(ids)):
        raise RuntimeError(
            "Doublons dans les identifiants."
        )

    for item in records:
        low = item.get("estimation_min")
        high = item.get("estimation_max")

        if (
            isinstance(low, (int, float))
            and isinstance(high, (int, float))
            and is_year_range(low, high)
        ):
            raise RuntimeError(
                "Estimation suspecte restante : "
                + passage_key(item)
            )

    # --------------------------------------------------------
    # SAUVEGARDE
    # --------------------------------------------------------

    save_json(
        DB_FILE,
        records
    )

    save_json(
        REPORT_FILE,
        report
    )

    save_json(
        QUALITY_FILE,
        quality
    )

    save_json(
        DIFF_FILE,
        differences
    )

    # --------------------------------------------------------
    # RESULTATS
    # --------------------------------------------------------

    print("\n====================================")
    print(f"ART ENCHERES QUEBEC - {VERSION}")
    print("====================================")

    print(
        "Notices :", len(records)
    )

    print(
        "Lots redetectes :", len(detected)
    )

    print(
        "Estimations :", quality["avec_estimation"]
    )

    print(
        "Dimensions :", quality["avec_dimensions"]
    )

    print(
        "Conflits :", len(conflicts)
    )

    print(
        "Titres corriges :", len(title_corrections)
    )

    print(
        "Estimations historiques corrigees :",
        len(corrected_estimates)
    )

    print(
        "Lots non redetectes :", len(missing)
    )

    print(
        "Rapprochements :", len(matches)
    )

    print(
        "Prix marteau verifies :",
        quality["prix_marteau_verifies"]
    )

    print("====================================")


if __name__ == "__main__":
    main()
