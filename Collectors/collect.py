
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ART ENCHERES QUEBEC
COLLECTEUR HISTORIQUE V8.10

Sources :
    BYDealers - 8 catalogues historiques

Objectifs :
    - Conserver les 491 notices historiques
    - Extraire les artistes et les titres
    - Completer les estimations manquantes
    - Completer les dimensions manquantes
    - Extraire les images integrees aux PDF
    - Detecter les possibles remises en vente
    - Preserver les identifiants historiques
    - Preserver les prix marteau verifies
    - Produire les quatre fichiers JSON
    - Ne jamais inventer de prix de vente

Dependances :
    requests
    pymupdf

Execution :
    python Collectors/collect.py
"""

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

import pymupdf as fitz
import requests


# ============================================================
# CONFIGURATION
# ============================================================

VERSION = "V8.10.1"

ROOT = Path(__file__).resolve().parent.parent

DATA = ROOT / "data"
IMAGES = ROOT / "images" / "bydealers"

DATA.mkdir(parents=True, exist_ok=True)
IMAGES.mkdir(parents=True, exist_ok=True)

DB_PATH = DATA / "oeuvres.json"

REPORT_PATH = DATA / "rapport-collecte.json"
QUALITY_PATH = DATA / "rapport-qualite.json"
DIFF_PATH = DATA / "rapport-differences.json"

USER_AGENT = "ArtEncheresQuebec/8.10.1"

TIMEOUT = 60
MAX_PDF_BYTES = 50 * 1024 * 1024

REQUEST_PAUSE = 2

# L'extraction d'images est facultative.
# Les images sont seulement publiees si leur
# reutilisation est autorisee.
EXTRACT_IMAGES = True
PUBLISH_IMAGES = False

MIN_IMAGE_WIDTH = 250
MIN_IMAGE_HEIGHT = 250
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# Une image non attribuee avec certitude a un lot
# n'est jamais associee automatiquement.
IMAGE_MATCHING_STRICT = True

# Protection de la base historique.
MIN_HISTORICAL_RECORDS = 491

# Les chiffres ci-dessous sont des references,
# pas des resultats garantis.
BASELINE_ESTIMATIONS = 284
BASELINE_DIMENSIONS = 482


# ============================================================
# CATALOGUES BYDEALERS
# ============================================================

CATALOGUES = {

    "Printemps 2023":
        "https://bydealers.com/wp-content/uploads/"
        "2023/04/"
        "Catalogue_108p_Printemps2023_Web_Spread.pdf",

    "Automne 2023":
        "https://bydealers.com/wp-content/uploads/"
        "2026/02/"
        "Catalogue_Fix_Automne_2023-1.pdf",

    "Printemps 2024":
        "https://bydealers.com/wp-content/uploads/"
        "2024/04/"
        "Catalogue_BYD_Printemps2024_Double.pdf",

    "Automne 2024":
        "https://bydealers.com/wp-content/uploads/"
        "2024/10/"
        "Catalogue_106p_Automne2024_Web_Double_pages.pdf",

    "Printemps 2025":
        "https://bydealers.com/wp-content/uploads/"
        "2025/12/"
        "Catalogue_Printemps2025_Web_Double_pages.pdf",

    "Automne 2025":
        "https://bydealers.com/wp-content/uploads/"
        "2026/02/"
        "Catalogue_Fall_2025-1.pdf",

    "Printemps 2026":
        "https://bydealers.com/wp-content/uploads/"
        "2026/04/"
        "Catalogue_Printemps2026.pdf",

    "Succession printemps 2026":
        "https://bydealers.com/wp-content/uploads/"
        "2026/05/"
        "Catalogue_102p_Printemps_succession2026_"
        "Web_simple-1.pdf",
}


# ============================================================
# SESSION HTTP
# ============================================================

SESSION = requests.Session()

SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/pdf,*/*",
})

ROBOTS_CACHE = {}


# ============================================================
# EXPRESSIONS REGULIERES
# ============================================================

RE_LOT = re.compile(
    r"^\s*(\d{1,3})\s+(.{2,250})$"
)

RE_BIO = re.compile(
    r"^\s*([12]\s*\d\s*\d\s*\d)"
    r"\s*[-–—]\s*"
    r"([12]\s*\d\s*\d\s*\d)?\s*$"
)

RE_DIMENSIONS = re.compile(
    r"(?<!\d)"
    r"(\d{1,4}(?:[.,]\d+)?)"
    r"\s*[x×]\s*"
    r"(\d{1,4}(?:[.,]\d+)?)"
    r"(?:\s*[x×]\s*"
    r"(\d{1,4}(?:[.,]\d+)?))?"
    r"\s*(cm|mm|in|inches|po|pouces)\b",
    re.I
)

RE_DIMENSIONS_WITHOUT_UNIT = re.compile(
    r"(?<!\d)"
    r"(\d{1,4}(?:[.,]\d+)?)"
    r"\s*[x×]\s*"
    r"(\d{1,4}(?:[.,]\d+)?)"
    r"(?:\s*[x×]\s*"
    r"(\d{1,4}(?:[.,]\d+)?))?"
    r"(?!\d)",
    re.I
)

RE_ESTIMATE_LABEL = re.compile(
    r"\b("
    r"estimation|estimate|estimated|"
    r"évaluation|evaluation"
    r")\b",
    re.I
)

RE_PRICE_RANGE = re.compile(
    r"(?<![\d.,])"
    r"(\$?\s*\d[\d\s\u00a0,.]*\d|\$?\s*\d{3,})"
    r"\s*[-–—]\s*"
    r"(\$?\s*\d[\d\s\u00a0,.]*\d|\$?\s*\d{3,})"
    r"\s*(?:CAD|CA\$|\$)?",
    re.I
)

RE_PRICE_SINGLE = re.compile(
    r"(?<!\d)"
    r"\$\s*(\d[\d\s\u00a0,.]*)",
    re.I
)

RE_TECHNIQUE = re.compile(
    r"^(huile|oil|acrylique|acrylic|"
    r"encre|ink|aquarelle|watercolour|"
    r"lithographie|lithograph|"
    r"sérigraphie|serigraph|"
    r"bronze|sculpture|"
    r"signé|signed|daté|dated|"
    r"provenance|exposition|exhibition|"
    r"bibliographie|literature|"
    r"estimation|estimate)\b",
    re.I
)

TITLE_ENDINGS = (
    "/", ":", "-", "–", "—", ","
)


# ============================================================
# OUTILS GENERAUX
# ============================================================

def now():
    return datetime.now(
        timezone.utc
    ).isoformat()


def clean(value):
    """
    Supprime les caracteres de controle
    provenant de l'extraction PDF.
    """

    text = unicodedata.normalize(
        "NFKC",
        str(value or "")
    )

    text = "".join(
        " "
        if unicodedata.category(c).startswith("C")
        else c
        for c in text
    )

    return re.sub(
        r"\s+",
        " ",
        text
    ).strip()


def normalize(value):
    text = unicodedata.normalize(
        "NFKD",
        clean(value)
    )

    return "".join(
        c for c in text
        if not unicodedata.combining(c)
    ).casefold()


def hash_id(*values):
    text = "|".join(
        normalize(v)
        for v in values
    )

    return hashlib.sha256(
        text.encode("utf-8")
    ).hexdigest()[:20]


def lot_number(value):
    try:
        return f"{int(str(value).strip()):02d}"
    except (ValueError, TypeError):
        return clean(value)


def passage_id(item):
    existing = item.get("id_passage")

    if existing:
        return str(existing)

    return hash_id(
        item.get("maison", "BYDealers"),
        item.get("url"),
        lot_number(item.get("lot"))
    )


def save_json(path, data):
    temporary = path.with_suffix(
        path.suffix + ".tmp"
    )

    temporary.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    temporary.replace(path)


def load_database():
    if not DB_PATH.exists():
        return []

    with DB_PATH.open(
        "r",
        encoding="utf-8"
    ) as file:
        data = json.load(file)

    if isinstance(data, dict):
        data = data.get("oeuvres", [])

    if not isinstance(data, list):
        raise ValueError(
            "Format de base de donnees invalide"
        )

    if not all(
        isinstance(item, dict)
        for item in data
    ):
        raise ValueError(
            "Notices historiques invalides"
        )

    return data


# ============================================================
# VERIFICATION DES AUTORISATIONS
# ============================================================

def access_authorized(url):
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
    if not access_authorized(url):
        raise PermissionError(
            "Acces non autorise au catalogue"
        )

    with SESSION.get(
        url,
        timeout=TIMEOUT,
        stream=True,
        allow_redirects=False
    ) as response:

        if response.is_redirect:
            raise RuntimeError(
                "Redirection non autorisee"
            )

        response.raise_for_status()

        content = bytearray()

        for chunk in response.iter_content(65536):
            if chunk:
                content.extend(chunk)

            if len(content) > MAX_PDF_BYTES:
                raise ValueError(
                    "Catalogue PDF trop volumineux"
                )

    if not bytes(content).startswith(b"%PDF"):
        raise ValueError(
            "Le fichier recu n'est pas un PDF"
        )

    time.sleep(REQUEST_PAUSE)

    return bytes(content)


# ============================================================
# EXTRACTION DES LIGNES PDF
# ============================================================

def extract_page_lines(page):
    lines = []

    blocks = page.get_text(
        "dict",
        sort=False
    ).get("blocks", [])

    for block in blocks:
        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):
            spans = line.get("spans", [])

            text = clean(" ".join(
                span.get("text", "")
                for span in spans
            ))

            bbox = line.get("bbox")

            if not text or not bbox:
                continue

            lines.append({
                "text": text,
                "x": bbox[0],
                "y": bbox[1],
                "x2": bbox[2],
                "y2": bbox[3],
            })

    return lines


def split_columns(page):
    lines = extract_page_lines(page)

    if page.rect.width <= 850:
        return [
            sorted(
                lines,
                key=lambda x: (
                    round(x["y"], 1),
                    x["x"]
                )
            )
        ]

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

    return [
        sorted(
            left,
            key=lambda x: (
                round(x["y"], 1),
                x["x"]
            )
        ),
        sorted(
            right,
            key=lambda x: (
                round(x["y"], 1),
                x["x"]
            )
        )
    ]


# ============================================================
# DETECTION DES ARTISTES
# ============================================================

def is_biography(text):
    match = RE_BIO.fullmatch(
        clean(text)
    )

    if not match:
        return False

    born = int(
        re.sub(
            r"\D",
            "",
            match.group(1)
        )
    )

    died = (
        int(
            re.sub(
                r"\D",
                "",
                match.group(2)
            )
        )
        if match.group(2)
        else None
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
        5 <= len(text) <= 100
        and 2 <= len(words) <= 10
    ):
        return False

    if re.search(
        r"\d|https?://|[!?;]",
        text
    ):
        return False

    if all(
        re.fullmatch(
            r"[A-Za-zÀ-ÿ]\.",
            word
        )
        for word in words
    ):
        return False

    blocked = {
        "production",
        "collection",
        "provenance",
        "exhibition",
        "exposition",
        "museum",
        "musee",
        "catalogue",
        "literature",
        "bibliographie"
    }

    for word in words:
        if normalize(word) in blocked:
            return False

    return any(
        len(
            re.sub(
                r"[^A-Za-zÀ-ÿ]",
                "",
                word
            )
        ) >= 2
        for word in words
    )


def detect_lot_header(text):
    match = RE_LOT.fullmatch(
        clean(text)
    )

    if not match:
        return None

    number = int(match.group(1))
    title = clean(match.group(2))

    if not (
        1 <= number <= 500
        and re.search(
            r"[A-Za-zÀ-ÿ]",
            title
        )
    ):
        return None

    return number, title


def detect_lots(lines, known_artists):
    """Repère les lots; un artiste historique ne valide PAS un faux titre."""
    entries = []
    for index, line in enumerate(lines):
        header = detect_lot_header(line["text"])
        if not header:
            continue
        number, title = header
        if not plausible_title(title):
            continue
        artist = None
        bio_near = False
        for bio_index in range(max(0, index - 3), index):
            if not is_biography(lines[bio_index]["text"]):
                continue
            bio_near = True
            for artist_index in range(max(0, bio_index - 2), bio_index):
                candidate = clean(lines[artist_index]["text"])
                if valid_artist(candidate):
                    artist = candidate
                    break
            if artist:
                break
        historical_artist = known_artists.get(lot_number(number))
        if historical_artist and bio_near:
            artist = historical_artist
        # L'artiste historique seul ne suffit pas: le numero du lot peut
        # aussi apparaitre dans une bibliographie ou une biographie.
        if not artist or not bio_near:
            continue
        entries.append({
            "index": index, "lot": number, "titre": title,
            "artiste": artist, "x": line["x"], "y": line["y"],
            "x2": line["x2"], "y2": line["y2"],
        })
    return entries


# ============================================================
# RECONSTRUCTION DES TITRES
# ============================================================

def reconstruct_title(lines, entry, end):
    title = clean(entry["titre"])

    if not title.endswith(TITLE_ENDINGS):
        return title

    index = entry["index"]

    if index + 1 >= end:
        return title

    first = lines[index]
    second = lines[index + 1]

    continuation = clean(
        second["text"]
    )

    if not continuation or not plausible_title(continuation):
        return title

    if len(continuation) > 120:
        return title

    if (
        detect_lot_header(continuation)
        or is_biography(continuation)
        or RE_DIMENSIONS.search(continuation)
        or RE_TECHNIQUE.search(continuation)
        or RE_ESTIMATE_LABEL.search(continuation)
    ):
        return title

    gap = second["y"] - first["y2"]

    if not (-3 <= gap <= 24):
        return title

    if abs(
        second["x"] - first["x"]
    ) > 45:
        return title

    result = clean(
        title + " " + continuation
    )

    if len(result) > 250:
        return title

    return result


def safe_title_extension(old, new):
    old = clean(old)
    new = clean(new)

    if not old.endswith(TITLE_ENDINGS):
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
        RE_TECHNIQUE.search(extension)
        or RE_DIMENSIONS.search(extension)
    ):
        return False

    return True


# ============================================================
# ESTIMATIONS V8.10
# ============================================================

def parse_money(value):
    text = clean(value)

    text = re.sub(
        r"(?i)CAD|CA\$|\$",
        "",
        text
    )

    text = text.replace(
        "\u00a0",
        " "
    )

    text = re.sub(
        r"[\s,.]",
        "",
        text
    )

    if not text.isdigit():
        return None

    return int(text)


def valid_estimate(low, high):
    if low is None or high is None:
        return False

    if not (
        1000 <= low <= high <= 100_000_000
    ):
        return False

    if high > low * 20:
        return False

    # Eviter les annees de creation
    # confondues avec les prix.
    if (
        1800 <= low <= 2100
        and 1800 <= high <= 2100
    ):
        return False

    return True


def parse_estimate_range(text):
    text = clean(text)

    results = []

    for match in RE_PRICE_RANGE.finditer(text):
        low = parse_money(
            match.group(1)
        )

        high = parse_money(
            match.group(2)
        )

        if valid_estimate(low, high):
            results.append(
                (low, high)
            )

    return list(
        dict.fromkeys(results)
    )


def estimate_candidates(section):
    """N'accepte que les fourchettes explicitement etiquetees et proches."""
    candidates = []
    for index, line in enumerate(section):
        label = line["text"]
        if not RE_ESTIMATE_LABEL.search(label):
            continue
        # Le prix sur la meme ligne que le libelle est prioritaire.
        values = parse_estimate_range(label)
        for value in values:
            candidates.append({"valeur": value, "source": "libelle_explicite",
                               "confiance": 100, "y": line["y"]})
        if values:
            continue
        # La ligne suivante doit etre physiquement adjacente.
        for offset in (1, 2):
            pos = index + offset
            if pos >= len(section):
                break
            next_line = section[pos]
            next_text = next_line["text"]
            if (next_line["y"] - line["y2"] > 28 or
                    abs(next_line["x"] - line["x"]) > 75 or
                    detect_lot_header(next_text) or is_biography(next_text) or
                    RE_DIMENSIONS.search(next_text) or RE_TECHNIQUE.search(next_text)):
                break
            for value in parse_estimate_range(next_text):
                candidates.append({"valeur": value, "source": "ligne_adjacente",
                                   "confiance": 95, "y": next_line["y"]})
    unique = {}
    for candidate in candidates:
        value = candidate["valeur"]
        if value not in unique or candidate["confiance"] > unique[value]["confiance"]:
            unique[value] = candidate
    return list(unique.values())


def extract_estimate(section):
    candidates = estimate_candidates(
        section
    )

    if len(candidates) == 1:
        candidate = candidates[0]

        # Les candidats de faible confiance
        # doivent etre verifies manuellement.
        if candidate["confiance"] < 90:
            return None, True, candidates

        return (
            candidate["valeur"],
            False,
            candidates
        )

    if len(candidates) > 1:
        return None, True, candidates

    return None, False, []


# ============================================================
# DIMENSIONS V8.10
# ============================================================

def format_dimension(value):
    number = float(
        value.replace(",", ".")
    )

    if number.is_integer():
        return str(int(number))

    return (
        f"{number:.2f}"
        .rstrip("0")
        .rstrip(".")
        .replace(".", ",")
    )


def extract_dimensions(section):
    for line in section:
        text = line["text"]

        match = RE_DIMENSIONS.search(
            text
        )

        if not match:
            continue

        unit = match.group(4).lower()

        numbers = [
            match.group(1),
            match.group(2)
        ]

        if match.group(3):
            numbers.append(
                match.group(3)
            )

        try:
            values = [
                float(
                    x.replace(",", ".")
                )
                for x in numbers
            ]
        except ValueError:
            continue

        if any(
            value <= 0 or value > 10000
            for value in values
        ):
            continue

        if unit == "mm":
            values = [
                value / 10
                for value in values
            ]

        elif unit in (
            "in", "inches", "po", "pouces"
        ):
            values = [
                value * 2.54
                for value in values
            ]

        return (
            " x ".join(
                format_dimension(
                    str(value)
                )
                for value in values
            ) + " cm"
        )

    return None


# ============================================================
# FILTRES CONSERVATEURS V8.10
# ============================================================

RE_FALSE_TITLE = re.compile(
    r"(?i)(\b(?:mai|may|juin|june|juillet|july|octobre|october|"
    r"novembre|november|decembre|décembre|fevrier|février|"
    r"exposition|exhibition|catalogue|provenance|bibliographie|"
    r"literature|art canadien important|important canadian art)\b|"
    r"\b(?:19|20)\d{2}\b|\b\d+\s*[x×]\s*\d+|"
    r"\b(?:cm|inches|triptyque|triptych)\b)"
)


def plausible_title(title):
    value = clean(title)
    if not (2 <= len(value) <= 140):
        return False
    if RE_FALSE_TITLE.search(value):
        return False
    if RE_ESTIMATE_LABEL.search(value) or RE_TECHNIQUE.match(value):
        return False
    if value.startswith(("-", "–", "—")):
        return False
    return True


def safe_estimate_evidence(section, value):
    """N'accepte que les montants adjacents a une mention explicite.

    Ne garantit pas l'attribution au lot: les estimations nouvelles
    sont soumises a revue et ne sont pas publiees automatiquement.
    """
    for index, line in enumerate(section):
        if not RE_ESTIMATE_LABEL.search(line["text"]):
            continue
        for offset in (0, 1):
            pos = index + offset
            if pos >= len(section):
                continue
            candidate_line = section[pos]["text"]
            if RE_DIMENSIONS.search(candidate_line):
                continue
            if value in parse_estimate_range(candidate_line):
                return True
    return False


# ============================================================
# METADONNEES D'UN LOT
# ============================================================

def extract_lot_metadata(
    lines,
    entries,
    index
):
    entry = entries[index]

    start = entry["index"] + 1

    end = (
        entries[index + 1]["index"]
        if index + 1 < len(entries)
        else len(lines)
    )

    section = lines[
        start:min(end, start + 140)
    ]

    dimensions = extract_dimensions(
        section
    )

    estimation, conflict, candidates = (
        extract_estimate(section)
    )

    title = reconstruct_title(
        lines,
        entry,
        end
    )

    return {
        "dimensions": dimensions,
        "estimation": estimation,
        "conflit_estimation": conflict,
        "candidats_estimation": candidates,
        "titre": title
    }


# ============================================================
# IMAGES PDF V8.10
# ============================================================

def image_candidates(page, document):
    """
    Recupere les images integrees a une page.

    Cette fonction ne prouve pas quelle
    image correspond a quel lot.
    """

    candidates = []

    seen = set()

    for image_info in page.get_images(
        full=True
    ):
        xref = image_info[0]

        if xref in seen:
            continue

        seen.add(xref)

        try:
            pix = fitz.Pixmap(
                document,
                xref
            )

            width = pix.width
            height = pix.height

            if (
                width < MIN_IMAGE_WIDTH
                or height < MIN_IMAGE_HEIGHT
            ):
                continue

            rects = page.get_image_rects(
                xref
            )

            if not rects:
                continue

            candidates.append({
                "xref": xref,
                "width": width,
                "height": height,
                "rects": [
                    {
                        "x0": rect.x0,
                        "y0": rect.y0,
                        "x1": rect.x1,
                        "y1": rect.y1
                    }
                    for rect in rects
                ]
            })

        except Exception:
            continue

    return candidates


def save_pdf_image(document, xref, identifier):
    """
    Sauvegarde une image seulement si
    sa publication est autorisee.

    Le parametre PUBLISH_IMAGES est
    volontairement desactive par defaut.
    """

    if not (
        EXTRACT_IMAGES
        and PUBLISH_IMAGES
    ):
        return None

    try:
        pix = fitz.Pixmap(
            document,
            xref
        )

        if pix.n not in (1, 3):
            pix = fitz.Pixmap(
                fitz.csRGB,
                pix
            )

        image_data = pix.tobytes(
            "png"
        )

        if len(image_data) > MAX_IMAGE_BYTES:
            return None

        filename = (
            identifier + ".png"
        )

        destination = IMAGES / filename

        destination.write_bytes(
            image_data
        )

        return (
            "images/bydealers/"
            + filename
        )

    except Exception:
        return None


def match_image_to_lot(
    page,
    document,
    item,
    entries_on_page
):
    """
    Association prudente.

    Une image n'est associee que lorsqu'il
    n'existe qu'un seul lot detecte dans
    la zone examinee et une seule image
    candidate.

    Sinon, aucune association automatique.
    """

    if not PUBLISH_IMAGES:
        return None

    if IMAGE_MATCHING_STRICT:
        if len(entries_on_page) != 1:
            return None

    candidates = image_candidates(
        page,
        document
    )

    if len(candidates) != 1:
        return None

    candidate = candidates[0]

    return save_pdf_image(
        document,
        candidate["xref"],
        passage_id(item)
    )


# ============================================================
# CREATION D'UNE NOTICE
# ============================================================

def create_notice(
    url,
    sale,
    page_number,
    entry,
    metadata
):
    lot = lot_number(
        entry["lot"]
    )

    identifier = hash_id(
        "BYDealers",
        url,
        lot
    )

    year = re.search(
        r"20\d{2}",
        sale
    )

    estimation = metadata[
        "estimation"
    ]

    low, high = (
        estimation
        if estimation
        else (None, None)
    )

    return {
        "id": identifier,
        "id_passage": identifier,

        "id_oeuvre": hash_id(
            "oeuvre-provisoire",
            identifier
        ),

        "maison": "BYDealers",
        "artiste": entry["artiste"],
        "titre": metadata["titre"],
        "lot": lot,

        "dimensions": metadata[
            "dimensions"
        ],

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
        "source_page_pdf": page_number,

        "origine": "catalogue_pdf_v8_10",

        "confiance_extraction": (
            95
            if estimation
            and metadata["dimensions"]
            and not metadata["conflit_estimation"]
            else 70
        ),

        "conflit_estimation": metadata[
            "conflit_estimation"
        ],

        "prix_realise_verifie": False,

        "date_collecte": now()
    }


# ============================================================
# EXTRACTION D'UN CATALOGUE
# ============================================================

def parse_catalogue(
    pdf_data,
    url,
    sale,
    known_artists
):
    detected = {}

    page_errors = []
    image_candidates_count = 0

    with fitz.open(
        stream=pdf_data,
        filetype="pdf"
    ) as document:

        total_pages = len(document)

        for page_index in range(total_pages):
            try:
                page = document[
                    page_index
                ]

                columns = split_columns(
                    page
                )

                all_entries = []

                for lines in columns:
                    entries = detect_lots(
                        lines,
                        known_artists
                    )

                    all_entries.extend(
                        entries
                    )

                    for index, entry in enumerate(
                        entries
                    ):
                        metadata = extract_lot_metadata(
                            lines,
                            entries,
                            index
                        )

                        item = create_notice(
                            url,
                            sale,
                            page_index + 1,
                            entry,
                            metadata
                        )

                        key = passage_id(
                            item
                        )

                        if key not in detected:
                            detected[key] = item

                        else:
                            old = detected[key]

                            old_score = sum([
                                bool(
                                    old.get("dimensions")
                                ),
                                old.get("estimation_min")
                                is not None,
                            ])

                            new_score = sum([
                                bool(
                                    item.get("dimensions")
                                ),
                                item.get("estimation_min")
                                is not None,
                            ])

                            if new_score > old_score:
                                detected[key] = item

                if EXTRACT_IMAGES:
                    candidates = image_candidates(
                        page,
                        document
                    )

                    image_candidates_count += len(
                        candidates
                    )

                    if (
                        PUBLISH_IMAGES
                        and len(all_entries) == 1
                        and len(candidates) == 1
                    ):
                        entry = all_entries[0]

                        key = hash_id(
                            "BYDealers",
                            url,
                            lot_number(
                                entry["lot"]
                            )
                        )

                        if key in detected:
                            image = save_pdf_image(
                                document,
                                candidates[0]["xref"],
                                key
                            )

                            if image:
                                detected[key][
                                    "image"
                                ] = image

            except Exception as exc:
                page_errors.append({
                    "page": page_index + 1,
                    "erreur": str(exc)
                })

                traceback.print_exc()

    return (
        list(detected.values()),
        total_pages,
        page_errors,
        image_candidates_count
    )


# ============================================================
# NORMALISATION DE L'HISTORIQUE
# ============================================================

def normalize_historical(item):
    result = dict(item)

    changes = []

    for field in (
        "artiste",
        "titre",
        "dimensions"
    ):
        value = result.get(field)

        if not isinstance(value, str):
            continue

        cleaned = clean(value)

        if cleaned != value:
            result[field] = cleaned
            changes.append(field)

    if not result.get("id_passage"):
        result["id_passage"] = passage_id(
            result
        )
        changes.append(
            "id_passage"
        )

    if not result.get("id_oeuvre"):
        result["id_oeuvre"] = hash_id(
            "oeuvre-provisoire",
            result["id_passage"]
        )
        changes.append(
            "id_oeuvre"
        )

    return result, changes


# ============================================================
# FUSION HISTORIQUE V8.10
# ============================================================

def merge_notice(old, new):
    merged = dict(old)

    additions = []
    conflicts = []
    corrections = []

    # --------------------------------------------------------
    # ARTISTE
    # --------------------------------------------------------

    old_artist = old.get(
        "artiste"
    )

    new_artist = new.get(
        "artiste"
    )

    if not old_artist and new_artist:
        merged["artiste"] = new_artist
        additions.append("artiste")

    elif (
        old_artist
        and new_artist
        and normalize(old_artist)
        != normalize(new_artist)
    ):
        if valid_artist(new_artist):
            conflicts.append({
                "champ": "artiste",
                "ancien": old_artist,
                "extrait": new_artist
            })

    # --------------------------------------------------------
    # TITRE
    # --------------------------------------------------------

    old_title = old.get(
        "titre"
    )

    new_title = new.get(
        "titre"
    )

    if not old_title and new_title:
        merged["titre"] = new_title
        additions.append("titre")

    elif (
        old_title
        and new_title
        and normalize(old_title)
        != normalize(new_title)
    ):
        if safe_title_extension(
            old_title,
            new_title
        ):
            # Une extension plausible reste une proposition de revue.
            corrections.append({
                "champ": "titre",
                "ancien": old_title,
                "nouveau": new_title,
                "motif": "titre_multiligne_a_verifier"
            })

        elif (
            plausible_title(new_title)
            and not normalize(old_title).startswith(normalize(new_title))
        ):
            conflicts.append({
                "champ": "titre",
                "ancien": old_title,
                "extrait": new_title
            })

    # --------------------------------------------------------
    # DIMENSIONS
    # --------------------------------------------------------

    if (
        not merged.get("dimensions")
        and new.get("dimensions")
    ):
        merged["dimensions"] = new[
            "dimensions"
        ]

        additions.append(
            "dimensions"
        )

    # --------------------------------------------------------
    # ESTIMATIONS
    # --------------------------------------------------------

    old_low = merged.get(
        "estimation_min"
    )

    old_high = merged.get(
        "estimation_max"
    )

    new_low = new.get(
        "estimation_min"
    )

    new_high = new.get(
        "estimation_max"
    )

    if (
        old_low is None
        and old_high is None
        and valid_estimate(
            new_low,
            new_high
        )
        and not new.get(
            "conflit_estimation"
        )
    ):
        # Ne jamais attribuer une estimation nouvelle sans revue
        # humaine de la page PDF et du lot.
        conflicts.append({
            "champ": "estimation_nouvelle_a_verifier",
            "ancien": None,
            "extrait": [new_low, new_high]
        })

    elif (
        old_low is not None
        and old_high is not None
        and new_low is not None
        and new_high is not None
        and (
            old_low != new_low
            or old_high != new_high
        )
    ):
        conflicts.append({
            "champ": "estimation",
            "ancien": [
                old_low,
                old_high
            ],
            "extrait": [
                new_low,
                new_high
            ]
        })

    # --------------------------------------------------------
    # IMAGE
    # --------------------------------------------------------

    if (
        not merged.get("image")
        and new.get("image")
    ):
        merged["image"] = new[
            "image"
        ]

        additions.append(
            "image"
        )

    # --------------------------------------------------------
    # METADONNEES
    # --------------------------------------------------------

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

            additions.append(
                field
            )

    # --------------------------------------------------------
    # PRESERVATION HISTORIQUE
    # --------------------------------------------------------

    for field in (
        "id",
        "id_passage",
        "id_oeuvre",
        "prix",
        "prix_marteau",
        "prix_vendu",
        "prix_realise_verifie"
    ):
        if field in old:
            merged[field] = old[field]

    return (
        merged,
        additions,
        conflicts,
        corrections
    )


# ============================================================
# CORRECTION DES FAUSSES ESTIMATIONS
# ============================================================

def correct_invalid_estimates(database):
    corrections = []

    for key, item in database.items():
        low = item.get(
            "estimation_min"
        )

        high = item.get(
            "estimation_max"
        )

        if not (
            isinstance(low, (int, float))
            and isinstance(high, (int, float))
        ):
            continue

        if (
            1800 <= low <= 2100
            and 1800 <= high <= 2100
        ):
            corrections.append({
                "id_passage": key,
                "ancienne_estimation": [
                    low,
                    high
                ],
                "motif": "annees_confondues_avec_prix"
            })

            item["estimation_min"] = None
            item["estimation_max"] = None
            item["estimation"] = None
            item["type_prix"] = "inconnu"

    return corrections


# ============================================================
# RAPPROCHEMENT ENTRE VENTES
# ============================================================

def dimension_signature(value):
    match = RE_DIMENSIONS.search(
        clean(value)
    )

    if not match:
        return None

    numbers = [
        match.group(1),
        match.group(2)
    ]

    if match.group(3):
        numbers.append(
            match.group(3)
        )

    try:
        return tuple(sorted(
            round(
                float(
                    number.replace(",", ".")
                ),
                1
            )
            for number in numbers
        ))

    except ValueError:
        return None


def find_repeated_works(records):
    groups = defaultdict(list)

    for item in records:
        artist = normalize(
            item.get("artiste")
        )

        title = normalize(
            item.get("titre")
        )

        dimensions = dimension_signature(
            item.get("dimensions")
        )

        if not (
            artist
            and title
            and dimensions
        ):
            continue

        key = (
            artist,
            title,
            dimensions
        )

        groups[key].append(
            item
        )

    matches = []

    for group in groups.values():
        if len(group) < 2:
            continue

        passages = {
            passage_id(item)
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
            "artiste": group[0].get(
                "artiste"
            ),

            "titre": group[0].get(
                "titre"
            ),

            "dimensions": group[0].get(
                "dimensions"
            ),

            "verification": "requise",

            "risque": (
                "lots_distincts_meme_catalogue"
                if same_catalogue
                else "possible_remise_en_vente"
            ),

            "passages": [
                {
                    "id_passage": passage_id(
                        item
                    ),
                    "vente": item.get(
                        "vente"
                    ),
                    "lot": item.get(
                        "lot"
                    ),
                    "url": item.get(
                        "url"
                    )
                }
                for item in group
            ]
        })

    return matches


# ============================================================
# RAPPORT DE QUALITE
# ============================================================

def build_quality_report(
    records,
    matches
):
    anomalies = []

    for item in records:
        problems = []

        if not item.get(
            "dimensions"
        ):
            problems.append(
                "dimensions_absentes"
            )

        if item.get(
            "estimation_min"
        ) is None:
            problems.append(
                "estimation_absente"
            )

        if not item.get(
            "image"
        ):
            problems.append(
                "image_absente"
            )

        if clean(
            item.get("titre")
        ).endswith(TITLE_ENDINGS):
            problems.append(
                "titre_possiblement_tronque"
            )

        if problems:
            anomalies.append({
                "id": item.get(
                    "id"
                ),

                "id_passage": passage_id(
                    item
                ),

                "artiste": item.get(
                    "artiste"
                ),

                "titre": item.get(
                    "titre"
                ),

                "problemes": problems,

                "url": item.get(
                    "url"
                ),

                "page": item.get(
                    "source_page_pdf"
                )
            })

    return {
        "version": VERSION,
        "date": now(),

        "total_oeuvres": len(
            records
        ),

        "avec_dimensions": sum(
            bool(
                item.get("dimensions")
            )
            for item in records
        ),

        "avec_estimation": sum(
            item.get("estimation_min")
            is not None
            and item.get("estimation_max")
            is not None
            for item in records
        ),

        "avec_image": sum(
            bool(
                item.get("image")
            )
            for item in records
        ),

        "prix_marteau_verifies": sum(
            item.get(
                "prix_realise_verifie"
            ) is True
            for item in records
        ),

        "rapprochements_potentiels": matches,

        "nombre_rapprochements": len(
            matches
        ),

        "anomalies": anomalies,

        "nombre_notices_a_controler": len(
            anomalies
        )
    }


# ============================================================
# VALIDATION AVANT SAUVEGARDE
# ============================================================

def validate_records(
    records,
    historical_count
):
    if len(records) < historical_count:
        raise RuntimeError(
            "Perte de notices historiques"
        )

    identifiers = [
        passage_id(item)
        for item in records
    ]

    if len(identifiers) != len(
        set(identifiers)
    ):
        raise RuntimeError(
            "Identifiants dupliques"
        )

    for item in records:
        low = item.get(
            "estimation_min"
        )

        high = item.get(
            "estimation_max"
        )

        if (
            low is not None
            and high is not None
            and not valid_estimate(
                low,
                high
            )
        ):
            raise RuntimeError(
                "Estimation invalide : "
                + passage_id(item)
            )

        if item.get(
            "prix_realise_verifie"
        ) is True:
            if (
                item.get("prix_marteau") is None
                and item.get("prix_vendu") is None
            ):
                raise RuntimeError(
                    "Prix declare verifie sans montant"
                )


# ============================================================
# PROGRAMME PRINCIPAL
# ============================================================

def main():
    print("")
    print("======================================")
    print("ART ENCHERES QUEBEC")
    print("COLLECTEUR V8.10")
    print("======================================")

    historical_records = load_database()

    historical_count = len(
        historical_records
    )

    print(
        "Notices historiques :",
        historical_count
    )

    if (
        historical_count > 0
        and historical_count
        < MIN_HISTORICAL_RECORDS
    ):
        raise RuntimeError(
            "Base historique incomplete : "
            + str(historical_count)
        )

    database = {}
    cleanups = []

    for old in historical_records:
        item, changed = normalize_historical(
            old
        )

        key = passage_id(
            item
        )

        if key in database:
            raise RuntimeError(
                "Doublon historique : "
                + key
            )

        database[key] = item

        if changed:
            cleanups.append({
                "id_passage": key,
                "champs": changed
            })

    historical_ids = set(
        database
    )

    estimate_corrections = []  # V8.10: aucune correction destructive automatique

    detected_ids = set()

    new_passages = []
    additions = []
    conflicts = []
    title_corrections = []

    catalogue_reports = []

    stats = {
        "pages_consultees": 0,
        "pages_accessibles": 0,
        "pdf_lus": 0,
        "oeuvres_detectees": 0,
        "nouvelles_oeuvres": 0,
        "erreurs_ou_blocages": 0,
        "images_candidates": 0
    }

    # --------------------------------------------------------
    # COLLECTE
    # --------------------------------------------------------

    for sale, url in CATALOGUES.items():
        print("")
        print(
            "Catalogue :",
            sale,
            flush=True
        )

        stats["pages_consultees"] += 1

        try:
            pdf_data = download_pdf(
                url
            )

            stats["pages_accessibles"] += 1

            known_artists = {
                lot_number(
                    item.get("lot")
                ): item.get("artiste")

                for item in database.values()

                if (
                    item.get("url") == url
                    and item.get("lot")
                    and item.get("artiste")
                )
            }

            (
                found,
                pages,
                page_errors,
                image_count
            ) = parse_catalogue(
                pdf_data,
                url,
                sale,
                known_artists
            )

            stats["pdf_lus"] += 1

            stats["oeuvres_detectees"] += len(
                found
            )

            stats["images_candidates"] += (
                image_count
            )

            stats["erreurs_ou_blocages"] += len(
                page_errors
            )

            catalogue_reports.append({
                "vente": sale,

                "statut": (
                    "ok"
                    if found and not page_errors
                    else "a_verifier"
                ),

                "oeuvres": len(
                    found
                ),

                "pages": pages,

                "erreurs_pages": page_errors,

                "images_candidates": image_count
            })

            print(
                "Pages :",
                pages
            )

            print(
                "Lots :",
                len(found)
            )

            print(
                "Images candidates :",
                image_count
            )

            for item in found:
                key = passage_id(
                    item
                )

                detected_ids.add(
                    key
                )

                if key in database:
                    (
                        merged,
                        completed,
                        problems,
                        corrected
                    ) = merge_notice(
                        database[key],
                        item
                    )

                    database[key] = merged

                    if completed:
                        additions.append({
                            "id_passage": key,
                            "champs": completed
                        })

                    if problems:
                        conflicts.append({
                            "id_passage": key,
                            "vente": sale,
                            "lot": item.get(
                                "lot"
                            ),
                            "url": item.get("url"),
                            "page_pdf": item.get("source_page_pdf"),
                            "conflits": problems
                        })

                    if corrected:
                        title_corrections.append({
                            "id_passage": key,
                            "vente": sale,
                            "lot": item.get(
                                "lot"
                            ),
                            "corrections": corrected
                        })

                else:
                    database[key] = item

                    new_passages.append(
                        key
                    )

                    stats[
                        "nouvelles_oeuvres"
                    ] += 1

        except Exception as exc:
            stats[
                "erreurs_ou_blocages"
            ] += 1

            traceback.print_exc()

            catalogue_reports.append({
                "vente": sale,
                "statut": "erreur",
                "oeuvres": 0,
                "erreur": str(exc)
            })

    # --------------------------------------------------------
    # SECURITE DE LA COLLECTE
    # --------------------------------------------------------

    if not detected_ids:
        raise RuntimeError(
            "Aucun lot detecte"
        )

    # Une collecte incomplete ne doit pas
    # remplacer les rapports valides.
    if (
        stats["pdf_lus"] != len(CATALOGUES)
        or stats["erreurs_ou_blocages"] != 0
    ):
        raise RuntimeError(
            "Collecte incomplete : "
            "sauvegarde annulee"
        )

    missing_ids = sorted(
        historical_ids - detected_ids
    )

    if missing_ids:
        print("\nAUDIT V8.10.1 : lots historiques non redetectes :", len(missing_ids))
        for missing_id in missing_ids:
            old = database.get(missing_id, {})
            print(json.dumps({
                "id_passage": missing_id,
                "vente": old.get("vente"),
                "lot": old.get("lot"),
                "artiste": old.get("artiste"),
                "titre": old.get("titre"),
                "url": old.get("url"),
            }, ensure_ascii=False))
        print("Ces notices restent dans la base, sans nouvelle validation PDF.")
        print("Les champs historiques de ces lots ne sont pas modifies.")

    records = sorted(
        database.values(),

        key=lambda item: (
            normalize(
                item.get("artiste")
            ),

            normalize(
                item.get("titre")
            ),

            str(
                item.get("annee_vente")
                or ""
            ),

            lot_number(
                item.get("lot")
            )
        )
    )

    validate_records(
        records,
        historical_count
    )

    # --------------------------------------------------------
    # RAPPROCHEMENTS
    # --------------------------------------------------------

    matches = find_repeated_works(
        records
    )

    # --------------------------------------------------------
    # QUALITE
    # --------------------------------------------------------

    quality = build_quality_report(
        records,
        matches
    )

    # --------------------------------------------------------
    # DIFFERENCES
    # --------------------------------------------------------

    differences = {
        "version": VERSION,
        "date": now(),

        "historique_avant": historical_count,

        "detectes_cette_execution": len(
            detected_ids
        ),

        "non_redetectes": len(
            missing_ids
        ),

        "lots_non_redetectes": missing_ids,
        "details_lots_non_redetectes": [
            {"id_passage": key, "vente": database[key].get("vente"),
             "lot": database[key].get("lot"), "artiste": database[key].get("artiste"),
             "titre": database[key].get("titre"), "url": database[key].get("url")}
            for key in missing_ids
        ],

        "nouveaux_passages": new_passages,

        "nettoyages": cleanups,

        "champs_completes": additions,

        "conflits_a_verifier": conflicts,

        "corrections_appliquees": title_corrections,

        "estimations_historiques_corrigees":
            estimate_corrections
    }

    # --------------------------------------------------------
    # RAPPORT DE COLLECTE
    # --------------------------------------------------------

    report = {
        "version": VERSION,
        "date": now(),

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

        "catalogues": catalogue_reports,

        "total_oeuvres": len(
            records
        ),

        "avec_estimation": quality[
            "avec_estimation"
        ],

        "avec_dimensions": quality[
            "avec_dimensions"
        ],

        "avec_image": quality[
            "avec_image"
        ],

        "images_candidates": stats[
            "images_candidates"
        ],

        "rapprochements_potentiels": len(
            matches
        ),

        "lots_historiques_non_redetectes": len(
            missing_ids
        ),

        "conflits_a_verifier": len(
            conflicts
        ),

        "corrections_titres": len(
            title_corrections
        ),

        "estimations_corrigees": len(
            estimate_corrections
        ),

        "note": (
            "Les prix marteau ne sont jamais "
            "inventes. Les rapprochements entre "
            "oeuvres doivent etre verifies. Les nouvelles "
            "estimations sont mises en revue avant publication."
        )
    }

    # --------------------------------------------------------
    # SAUVEGARDE
    # --------------------------------------------------------

    save_json(
        DB_PATH,
        records
    )

    save_json(
        REPORT_PATH,
        report
    )

    save_json(
        QUALITY_PATH,
        quality
    )

    save_json(
        DIFF_PATH,
        differences
    )

    # --------------------------------------------------------
    # RESULTATS
    # --------------------------------------------------------

    print("")
    print("======================================")
    print("RESULTATS V8.10")
    print("======================================")

    print(
        "Notices :",
        len(records)
    )

    print(
        "Lots detectes :",
        len(detected_ids)
    )

    print(
        "Estimations :",
        quality["avec_estimation"]
    )

    print(
        "Dimensions :",
        quality["avec_dimensions"]
    )

    print(
        "Images publiees :",
        quality["avec_image"]
    )

    print(
        "Images candidates :",
        stats["images_candidates"]
    )

    print(
        "Conflits :",
        len(conflicts)
    )

    print(
        "Champs completes :",
        len(additions)
    )

    print(
        "Lots non redetectes :",
        len(missing_ids)
    )

    print(
        "Rapprochements :",
        len(matches)
    )

    print(
        "Prix marteau verifies :",
        quality["prix_marteau_verifies"]
    )

    print("")
    print("COLLECTE V8.10 TERMINEE")
    print("======================================")


if __name__ == "__main__":
    main()
