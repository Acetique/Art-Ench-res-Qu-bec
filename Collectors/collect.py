
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ART ENCHERES QUEBEC — COLLECTEUR V8.7

Corrections :
- Suppression des caracteres PDF invisibles.
- Rejet des initiales seules comme artistes.
- Conservation des artistes historiques fiables.
- Reconstruction prudente des titres multilignes.
- Rejet des fourchettes d'annees comme estimations.
- Conservation des 491 notices historiques.
- Preservation des identifiants et prix marteau.
- Detection des vrais conflits.
- Quatre rapports JSON compatibles avec le workflow.
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

VERSION = "V8.7"

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

DB = DATA / "oeuvres.json"
REPORT = DATA / "rapport-collecte.json"
QUALITY = DATA / "rapport-qualite.json"
DIFF = DATA / "rapport-differences.json"

USER_AGENT = "ArtEncheresQuebec/8.7"
TIMEOUT = 45
MAX_BYTES = 40 * 1024 * 1024
PAUSE = 2

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

ROBOTS = {}

SUSPECT_ESTIMATE_IDS = {
    "c5aca100f04adb09c7ee",
    "c60ac7b3013233ec7a51",
}


# ============================================================
# EXPRESSIONS REGULIERES
# ============================================================

BIO = re.compile(
    r"^\s*([12]\s*\d\s*\d\s*\d)"
    r"\s*[-–—]\s*"
    r"([12]\s*\d\s*\d\s*\d)?\s*$"
)

LOT = re.compile(
    r"^\s*(\d{1,3})\s+(.{2,250})$"
)

DIM = re.compile(
    r"\d{1,4}(?:[.,]\d+)?"
    r"\s*[x×]\s*"
    r"\d{1,4}(?:[.,]\d+)?"
    r"(?:\s*[x×]\s*\d{1,4}(?:[.,]\d+)?)?"
    r"\s*cm\b",
    re.I
)

EST_LABEL = re.compile(
    r"\b(?:estimation|estimate|estimated|"
    r"évaluation|evaluation)\b",
    re.I
)

EST_RANGE = re.compile(
    r"(?<!\d)"
    r"(\d{1,3}(?:[\s\u00a0,.]\d{3})+|\d{4,8})"
    r"\s*[-–—]\s*"
    r"(\d{1,3}(?:[\s\u00a0,.]\d{3})+|\d{4,8})"
    r"\s*(?:\$|CAD)?",
    re.I
)

TITLE_ENDINGS = (
    "/", ":", "-", "–", "—", ","
)

STOP_TITLE = re.compile(
    r"^(?:huile|oil|acrylique|acrylic|"
    r"encre|ink|aquarelle|watercolour|"
    r"lithographie|lithograph|"
    r"sérigraphie|serigraph|"
    r"sculpture|bronze|signé|signed|"
    r"daté|dated|provenance|"
    r"exposition|exhibition|"
    r"bibliographie|literature|"
    r"estimation|estimate)\b",
    re.I
)


# ============================================================
# UTILITAIRES
# ============================================================

def timestamp():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    """
    V8.7 : suppression des caracteres de controle
    PDF, dont U+0007 (BEL).

    Conserve les lettres accentuees et la
    ponctuation normale des titres.
    """

    text = unicodedata.normalize(
        "NFKC", str(value or "")
    )

    text = "".join(
        " " if unicodedata.category(c).startswith("C")
        else c
        for c in text
    )

    return re.sub(r"\s+", " ", text).strip()


def normalize(value):
    text = unicodedata.normalize(
        "NFKD", clean(value)
    )

    return "".join(
        c for c in text
        if not unicodedata.combining(c)
    ).casefold()


def digest(*parts):
    raw = "|".join(
        normalize(part) for part in parts
    )

    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:20]


def lot_number(value):
    try:
        return f"{int(str(value).strip()):02d}"
    except (ValueError, TypeError):
        return clean(value)


def passage_key(item):
    if item.get("url") and item.get("lot"):
        return digest(
            item.get("maison", "BYDealers"),
            item["url"],
            lot_number(item["lot"])
        )

    return clean(
        item.get("id_passage")
        or item.get("id")
    )


def load_database():
    if not DB.exists():
        return []

    data = json.loads(
        DB.read_text(encoding="utf-8")
    )

    if isinstance(data, dict):
        data = data.get("oeuvres", [])

    if not isinstance(data, list):
        raise ValueError(
            "Format de oeuvres.json invalide"
        )

    if not all(isinstance(x, dict) for x in data):
        raise ValueError(
            "La base contient des notices invalides"
        )

    return data


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


# ============================================================
# ESTIMATIONS
# ============================================================

def is_year_range(low, high):
    return (
        1800 <= low <= 2100
        and 1800 <= high <= 2100
    )


def money(value):
    digits = re.sub(r"\D", "", str(value))
    return int(digits) if digits else None


def parse_estimate(text):
    text = clean(text)

    if DIM.search(text):
        return None

    candidates = []

    for match in EST_RANGE.finditer(text):
        low = money(match.group(1))
        high = money(match.group(2))

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

    unique = list(dict.fromkeys(candidates))

    if len(unique) == 1:
        return unique[0]

    return None


def extract_estimate(section):
    """
    Cherche une estimation dans le contexte
    d'un libelle explicite.

    Ne depasse jamais les limites du lot.
    """

    candidates = []

    for i, line in enumerate(section):
        text = line["text"]

        if not EST_LABEL.search(text):
            continue

        value = parse_estimate(text)

        if value:
            candidates.append(value)
            continue

        for j in range(
            i + 1,
            min(i + 3, len(section))
        ):
            next_text = section[j]["text"]

            if (
                lot_header(next_text)
                or is_biography(next_text)
                or DIM.search(next_text)
                or STOP_TITLE.search(next_text)
            ):
                break

            value = parse_estimate(next_text)

            if value:
                candidates.append(value)
                break

    unique = list(dict.fromkeys(candidates))

    if len(unique) == 1:
        return unique[0], False

    if len(unique) > 1:
        return None, True

    return None, False


def correct_historical_estimates(database):
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
            and is_year_range(low, high)
        ):
            continue

        corrections.append({
            "id_passage": identifier,
            "ancienne_estimation": [low, high],
            "motif": "fourchette_annees"
        })

        item["estimation_min"] = None
        item["estimation_max"] = None
        item["estimation"] = None
        item["type_prix"] = "inconnu"
        item["conflit_estimation"] = False

    return corrections


# ============================================================
# AUTORISATIONS ET TELECHARGEMENT
# ============================================================

def authorized(url):
    parsed = urlparse(url)

    if (
        parsed.scheme != "https"
        or parsed.netloc != "bydealers.com"
    ):
        return False

    origin = "https://bydealers.com"

    if origin not in ROBOTS:
        response = SESSION.get(
            origin + "/robots.txt",
            timeout=TIMEOUT,
            allow_redirects=False
        )

        if response.status_code == 200:
            robot = RobotFileParser()
            robot.parse(response.text.splitlines())
            ROBOTS[origin] = robot

        elif response.status_code in (404, 410):
            ROBOTS[origin] = True

        else:
            ROBOTS[origin] = False

    rule = ROBOTS[origin]

    if isinstance(rule, bool):
        return rule

    return rule.can_fetch(USER_AGENT, url)


def download_pdf(url):
    if not authorized(url):
        raise PermissionError(
            "Acces non autorise au PDF"
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

        data = bytearray()

        for chunk in response.iter_content(65536):
            if chunk:
                data.extend(chunk)

            if len(data) > MAX_BYTES:
                raise ValueError(
                    "PDF trop volumineux"
                )

    if not bytes(data).startswith(b"%PDF"):
        raise ValueError(
            "Le document recu n'est pas un PDF"
        )

    time.sleep(PAUSE)
    return bytes(data)


# ============================================================
# EXTRACTION PDF
# ============================================================

def page_lines(page):
    result = []

    blocks = page.get_text(
        "dict", sort=False
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
# ARTISTES
# ============================================================

def is_biography(text):
    match = BIO.fullmatch(clean(text))

    if not match:
        return False

    born = int(
        re.sub(r"\D", "", match.group(1))
    )

    died = (
        int(re.sub(r"\D", "", match.group(2)))
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


def is_artist(text):
    """
    V8.7 :
    - Rejette A. L., J. D. et autres initiales.
    - Exige un nom suffisamment developpe.
    - Accepte les particules de noms.
    """

    text = clean(text)

    text = re.sub(
        r"\s*\(\s*suite\s*\)\s*$",
        "",
        text,
        flags=re.I
    )

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

    # Rejeter les noms composes uniquement
    # d'initiales : A. L., J. D., etc.
    if all(
        re.fullmatch(r"[A-Za-zÀ-ÿ]\.", word)
        for word in words
    ):
        return False

    # Au moins un mot de deux lettres ou plus
    # qui ne soit pas seulement une initiale.
    if not any(
        len(re.sub(r"[^A-Za-zÀ-ÿ]", "", word)) >= 2
        for word in words
    ):
        return False

    blocked = {
        "production", "while", "loosening",
        "knots", "recent", "recently",
        "following", "collection", "provenance",
        "exhibition", "exposition", "museum",
        "musee", "artist", "artiste",
        "because", "through", "catalogue"
    }

    particles = {
        "de", "du", "des", "la", "le",
        "van", "von", "der", "den",
        "da", "di", "del", "and", "et"
    }

    for i, word in enumerate(words):
        base = word.strip("()")

        if normalize(base) in blocked:
            return False

        if not re.fullmatch(
            r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’().-]*",
            word
        ):
            return False

        if i and normalize(base) in particles:
            continue

        if not base or not base[0].isupper():
            return False

    return True


def lot_header(text):
    match = LOT.fullmatch(clean(text))

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


def detect_lots(lines, known):
    entries = []

    for i, line in enumerate(lines):
        header = lot_header(line["text"])

        if not header:
            continue

        number, title = header
        artist = None

        for bio_i in range(max(0, i - 3), i):
            if not is_biography(
                lines[bio_i]["text"]
            ):
                continue

            for artist_i in range(
                max(0, bio_i - 2),
                bio_i
            ):
                candidate = clean(
                    lines[artist_i]["text"]
                )

                if is_artist(candidate):
                    artist = candidate
                    break

            if artist:
                break

        expected = known.get(
            lot_number(number)
        )

        if expected and artist:
            # Si le PDF indique un artiste
            # different, conserver l'historique
            # et signaler la difference seulement
            # si le nom extrait est plausible.
            if normalize(expected) != normalize(artist):
                artist = expected

        elif expected and not artist:
            # Filet de securite pour les lots
            # deja identifies historiquement.
            has_bio = any(
                is_biography(lines[j]["text"])
                for j in range(max(0, i - 3), i)
            )

            if has_bio:
                artist = expected

        if artist:
            entries.append({
                "index": i,
                "lot": number,
                "titre": title,
                "artiste": artist
            })

    return entries


# ============================================================
# TITRES MULTILIGNES
# ============================================================

def reconstruct_title(lines, entry, end):
    original = clean(entry["titre"])

    if not original.endswith(TITLE_ENDINGS):
        return original

    i = entry["index"]

    if i + 1 >= end:
        return original

    first = lines[i]
    second = lines[i + 1]

    continuation = clean(
        second["text"]
    )

    if not continuation:
        return original

    if len(continuation) > 120:
        return original

    if (
        lot_header(continuation)
        or is_biography(continuation)
        or DIM.search(continuation)
        or STOP_TITLE.search(continuation)
        or EST_LABEL.search(continuation)
    ):
        return original

    if not re.search(
        r"[A-Za-zÀ-ÿ]", continuation
    ):
        return original

    gap = second["y"] - first["y2"]

    if not (-3 <= gap <= 24):
        return original

    if abs(second["x"] - first["x"]) > 45:
        return original

    result = clean(
        original + " " + continuation
    )

    return result if len(result) <= 250 else original


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
        STOP_TITLE.search(extension)
        or DIM.search(extension)
        or EST_LABEL.search(extension)
    ):
        return False

    return True


# ============================================================
# METADONNEES
# ============================================================

def extract_metadata(lines, entries, index):
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

    dimensions = None

    for line in section:
        match = DIM.search(line["text"])

        if match:
            dimensions = clean(match.group())
            break

    estimation, conflict = extract_estimate(
        section
    )

    title = reconstruct_title(
        lines, entry, end
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
    url, sale, page, entry,
    dimensions, estimation, conflict, title
):
    lot = lot_number(entry["lot"])

    identifier = digest(
        "BYDealers", url, lot
    )

    year = re.search(r"20\d{2}", sale)

    low, high = (
        estimation
        if estimation
        else (None, None)
    )

    return {
        "id": identifier,
        "id_passage": identifier,
        "id_oeuvre": digest(
            "oeuvre-provisoire", identifier
        ),

        "maison": "BYDealers",
        "artiste": entry["artiste"],
        "titre": clean(title),
        "lot": lot,
        "dimensions": dimensions,

        "vente": sale,
        "annee_vente": (
            int(year.group()) if year else None
        ),

        "image": None,
        "prix": None,
        "prix_marteau": None,
        "prix_vendu": None,

        "estimation_min": low,
        "estimation_max": high,

        "estimation": (
            f"{low:,} - {high:,} CAD"
            if estimation else None
        ),

        "type_prix": (
            "estimation" if estimation
            else "inconnu"
        ),

        "devise": "CAD",
        "statut": "catalogue_historique",

        "url": url,
        "source_page": url,
        "source_page_pdf": page,

        "origine": "catalogue_pdf_v8_7",

        "confiance_extraction": (
            90 if dimensions and estimation
            and not conflict else 65
        ),

        "conflit_estimation": conflict,
        "prix_realise_verifie": False,
        "date_collecte": timestamp()
    }


# ============================================================
# ANALYSE PDF
# ============================================================

def parse_pdf(data, url, sale, known):
    detected = {}
    page_errors = []

    with fitz.open(
        stream=data, filetype="pdf"
    ) as document:

        pages = len(document)

        for page_index in range(pages):
            try:
                page = document[page_index]

                for lines in split_columns(page):
                    entries = detect_lots(
                        lines, known
                    )

                    for i, entry in enumerate(entries):
                        (
                            dimensions,
                            estimation,
                            conflict,
                            title
                        ) = extract_metadata(
                            lines, entries, i
                        )

                        item = create_notice(
                            url, sale, page_index + 1,
                            entry, dimensions,
                            estimation, conflict, title
                        )

                        key = passage_key(item)

                        if key not in detected:
                            detected[key] = item
                            continue

                        old = detected[key]

                        old_score = sum([
                            bool(old.get("dimensions")),
                            old.get("estimation_min")
                            is not None,
                            not old.get("conflit_estimation")
                        ])

                        new_score = sum([
                            bool(item.get("dimensions")),
                            item.get("estimation_min")
                            is not None,
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

    return list(detected.values()), pages, page_errors


# ============================================================
# NETTOYAGE HISTORIQUE
# ============================================================

def normalize_existing(item):
    result = dict(item)
    changes = []

    for field in (
        "artiste", "titre", "dimensions"
    ):
        value = result.get(field)

        if not isinstance(value, str):
            continue

        cleaned = clean(value)

        if cleaned != value:
            result[field] = cleaned
            changes.append(field)

    if not result.get("id_passage"):
        result["id_passage"] = passage_key(result)
        changes.append("id_passage")

    if not result.get("id_oeuvre"):
        result["id_oeuvre"] = digest(
            "oeuvre-provisoire",
            passage_key(result)
        )
        changes.append("id_oeuvre")

    return result, changes


# ============================================================
# FUSION V8.7
# ============================================================

def merge_notice(old, new):
    """
    Ne remplace jamais les donnees historiques
    contradictoires sans verification.

    Les caracteres de controle ne creent
    plus de faux conflits.
    """

    merged = dict(old)

    additions = []
    conflicts = []
    corrections = []

    for field in (
        "artiste", "titre", "dimensions"
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
            # Ignorer les initiales ou noms
            # insuffisamment fiables.
            if not is_artist(incoming):
                continue

            conflicts.append({
                "champ": "artiste",
                "ancien": current,
                "extrait": incoming
            })
            continue

        if field == "titre":
            if safe_title_extension(
                current, incoming
            ):
                merged[field] = incoming

                corrections.append({
                    "champ": "titre",
                    "ancien": current,
                    "nouveau": incoming,
                    "motif": "extension_multiligne"
                })
                continue

            if normalize(current).startswith(
                normalize(incoming)
            ):
                continue

            conflicts.append({
                "champ": "titre",
                "ancien": current,
                "extrait": incoming
            })
            continue

        if field == "dimensions":
            # Conserver la dimension historique.
            conflicts.append({
                "champ": "dimensions",
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

    old_est = (
        old.get("estimation_min"),
        old.get("estimation_max")
    )

    new_est = (
        new.get("estimation_min"),
        new.get("estimation_max")
    )

    if (
        all(x is not None for x in new_est)
        and not new.get("conflit_estimation")
    ):
        if all(x is None for x in old_est):
            for field in (
                "estimation_min",
                "estimation_max",
                "estimation"
            ):
                merged[field] = new[field]

            merged["type_prix"] = "estimation"
            additions.append("estimation")

        elif old_est != new_est:
            conflicts.append({
                "champ": "estimation",
                "ancien": old_est,
                "extrait": new_est,
                "verification": "manuelle_requise"
            })

    if new.get("conflit_estimation"):
        conflicts.append({
            "champ": "estimation",
            "motif": "estimations_multiples"
        })

    merged["id_passage"] = passage_key(old)

    # Ne jamais modifier les champs historiques :
    # id, id_oeuvre, prix_marteau, prix_vendu,
    # prix_realise_verifie.

    return (
        merged,
        additions,
        conflicts,
        corrections
    )


# ============================================================
# RAPPROCHEMENTS
# ============================================================

def dimensions_key(value):
    match = DIM.search(clean(value))

    if not match:
        return None

    numbers = re.findall(
        r"\d+(?:[.,]\d+)?",
        match.group()
    )

    return tuple(sorted(
        round(float(x.replace(",", ".")), 1)
        for x in numbers
    ))


def potential_matches(records):
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
            passage_key(x) for x in group
        }) < 2:
            continue

        same_catalogue = (
            len({x.get("url") for x in group}) == 1
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

def quality_report(records, matches):
    anomalies = []

    for item in records:
        problems = []

        if not item.get("dimensions"):
            problems.append("dimensions_absentes")

        if item.get("estimation_min") is None:
            problems.append("estimation_absente")

        if clean(item.get("titre")).endswith(
            TITLE_ENDINGS
        ):
            problems.append(
                "titre_possiblement_tronque"
            )

        if item.get("conflit_estimation"):
            problems.append("conflit_estimation")

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
# PROGRAMME PRINCIPAL
# ============================================================

def main():
    original = load_database()

    print(
        f"{VERSION} — "
        f"{len(original)} notices historiques",
        flush=True
    )

    database = {}
    cleanup = []

    for old in original:
        item, changed = normalize_existing(old)
        key = passage_key(item)

        if not key:
            raise ValueError(
                "Notice sans identifiant"
            )

        if key in database:
            raise ValueError(
                f"Doublon historique : {key}"
            )

        database[key] = item

        if changed:
            cleanup.append({
                "id_passage": key,
                "champs": changed
            })

    historical = set(database)

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
    # COLLECTE DES CATALOGUES
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

            known = {
                lot_number(x.get("lot")):
                    x.get("artiste")
                for x in database.values()
                if (
                    x.get("url") == url
                    and x.get("lot")
                    and x.get("artiste")
                )
            }

            found, pages, page_errors = parse_pdf(
                data, url, sale, known
            )

            stats["pdf_lus"] += 1
            stats["oeuvres_detectees"] += len(found)

            stats["erreurs_ou_blocages"] += len(
                page_errors
            )

            catalogues.append({
                "vente": sale,
                "statut": (
                    "ok" if found and not page_errors
                    else "a_verifier"
                ),
                "oeuvres": len(found),
                "pages": pages,
                "erreurs_pages": page_errors
            })

            print(
                f"Pages={pages} "
                f"Lots={len(found)} "
                f"Erreurs={len(page_errors)}",
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
                        corrected
                    ) = merge_notice(
                        database[key], item
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

                    if corrected:
                        title_corrections.append({
                            "id_passage": key,
                            "vente": sale,
                            "lot": item["lot"],
                            "corrections": corrected
                        })

                else:
                    database[key] = item
                    new_passages.append(key)

                    stats["nouvelles_oeuvres"] += 1

        except Exception as exc:
            stats["erreurs_ou_blocages"] += 1

            traceback.print_exc()

            catalogues.append({
                "vente": sale,
                "statut": "erreur",
                "oeuvres": 0,
                "erreur": str(exc)
            })

    # --------------------------------------------------------
    # VALIDATION HISTORIQUE
    # --------------------------------------------------------

    if not detected:
        raise RuntimeError(
            "Aucun lot detecte : sauvegarde annulee"
        )

    records = sorted(
        database.values(),
        key=lambda x: (
            normalize(x.get("artiste")),
            normalize(x.get("titre")),
            str(x.get("annee_vente") or ""),
            lot_number(x.get("lot"))
        )
    )

    if len(records) < len(original):
        raise RuntimeError(
            "Perte de notices historiques"
        )

    identifiers = [
        passage_key(x) for x in records
    ]

    if len(identifiers) != len(set(identifiers)):
        raise RuntimeError(
            "Identifiants dupliques"
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
                "Estimation suspecte : "
                + passage_key(item)
            )

    # --------------------------------------------------------
    # RAPPORTS
    # --------------------------------------------------------

    missing = sorted(
        historical - detected
    )

    matches = potential_matches(records)

    quality = quality_report(
        records, matches
    )

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
        "nettoyages": cleanup,
        "champs_completes": additions,
        "conflits_a_verifier": conflicts,
        "corrections_appliquees": title_corrections,
        "estimations_historiques_corrigees":
            corrected_estimates
    }

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
        "avec_estimation": quality["avec_estimation"],
        "avec_dimensions": quality["avec_dimensions"],
        "rapprochements_potentiels": len(matches),
        "lots_historiques_non_redetectes": len(missing),
        "conflits_a_verifier": len(conflicts),
        "corrections_titres": len(title_corrections),
        "estimations_corrigees": len(corrected_estimates),

        "note": (
            "Estimations de catalogues seulement. "
            "Aucun prix marteau invente."
        )
    }

    # --------------------------------------------------------
    # SAUVEGARDE
    # --------------------------------------------------------

    save_json(DB, records)
    save_json(REPORT, report)
    save_json(QUALITY, quality)
    save_json(DIFF, differences)

    # --------------------------------------------------------
    # RESUME
    # --------------------------------------------------------

    print("\n====================================")
    print(f"ART ENCHERES QUEBEC — {VERSION}")
    print("====================================")

    print("Notices :", len(records))
    print("Lots detectes :", len(detected))
    print("Estimations :", quality["avec_estimation"])
    print("Dimensions :", quality["avec_dimensions"])
    print("Conflits :", len(conflicts))
    print("Titres corriges :", len(title_corrections))
    print("Nettoyages :", len(cleanup))
    print("Estimations corrigees :", len(corrected_estimates))
    print("Non redetectes :", len(missing))
    print("Rapprochements :", len(matches))

    print("====================================")


if __name__ == "__main__":
    main()
