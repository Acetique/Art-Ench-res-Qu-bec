
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ART ENCHERES QUEBEC — COLLECTEUR V8.4

Fonctions :
- Lecture des 8 catalogues publics BYDealers
- Respect de robots.txt
- Extraction artiste, titre, dimensions et estimations
- Reconstruction prudente des titres multilignes
- Rejet des faux noms d'artistes
- Conservation des notices historiques
- Aucun prix marteau invente
- Rapports JSON de collecte, qualite et differences

Dependances :
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

VERSION = "V8.4"

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

DATA.mkdir(parents=True, exist_ok=True)

DB = DATA / "oeuvres.json"
REPORT = DATA / "rapport-collecte.json"
QUALITY = DATA / "rapport-qualite.json"
DIFF = DATA / "rapport-differences.json"

USER_AGENT = (
    "ArtEncheresQuebec/8.4 "
    "(catalogues publics; contact: GitHub repository)"
)

TIMEOUT = 40
MAX_BYTES = 40 * 1024 * 1024
PAUSE_SECONDS = 2

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

EST = re.compile(
    r"(?<!\d)"
    r"(\d[\d\s\u00a0,.]*)"
    r"\s*[-–—]\s*"
    r"(\d[\d\s\u00a0,.]*)"
    r"\s*(?:\$|CAD)?",
    re.I
)

PRICE_CONTEXT = re.compile(
    r"\b(estimation|estimate|est\.?|"
    r"estimated|evaluation|évaluation)\b",
    re.I
)

TITLE_ENDINGS = ("/", ":", "-", "–", "—", ",")

STOP_TITLE = re.compile(
    r"^(?:"
    r"huile|oil|acrylique|acrylic|"
    r"encre|ink|aquarelle|watercolour|"
    r"lithographie|lithograph|"
    r"sérigraphie|serigraph|"
    r"sculpture|bronze|"
    r"signé|signed|"
    r"daté|dated|"
    r"provenance|"
    r"exposition|exhibition|"
    r"bibliographie|literature|"
    r"estimation|estimate"
    r")\b",
    re.I
)


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

    text = "".join(
        " " if unicodedata.category(c).startswith("C") else c
        for c in text
    )

    return re.sub(r"\s+", " ", text).strip()


def keytext(value):
    text = unicodedata.normalize(
        "NFKD",
        clean(value)
    )

    return "".join(
        c for c in text
        if not unicodedata.combining(c)
    ).casefold()


def digest(*parts):
    source = "|".join(
        keytext(part)
        for part in parts
    )

    return hashlib.sha256(
        source.encode("utf-8")
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


def write_json(path, obj):
    temporary = path.with_suffix(
        path.suffix + ".tmp"
    )

    temporary.write_text(
        json.dumps(
            obj,
            ensure_ascii=False,
            indent=2
        ) + "\n",
        encoding="utf-8"
    )

    temporary.replace(path)


def load_db():
    if not DB.exists():
        return []

    obj = json.loads(
        DB.read_text(encoding="utf-8")
    )

    if isinstance(obj, dict):
        obj = obj.get("oeuvres", [])

    if not isinstance(obj, list):
        raise ValueError(
            "oeuvres.json doit contenir une liste."
        )

    if not all(isinstance(x, dict) for x in obj):
        raise ValueError(
            "Toutes les notices doivent etre des objets JSON."
        )

    return obj


# ============================================================
# ACCES RESPONSABLE AUX PDF
# ============================================================

def allowed(url):
    parsed = urlparse(url)

    if (
        parsed.scheme != "https"
        or parsed.netloc != "bydealers.com"
    ):
        return False

    origin = (
        f"{parsed.scheme}://{parsed.netloc}"
    )

    if origin not in ROBOTS:
        response = SESSION.get(
            origin + "/robots.txt",
            timeout=TIMEOUT,
            allow_redirects=False
        )

        if response.status_code in (401, 403):
            return False

        if response.status_code == 200:
            parser = RobotFileParser()

            parser.parse(
                response.text.splitlines()
            )

            ROBOTS[origin] = parser

        elif response.status_code in (404, 410):
            ROBOTS[origin] = True

        else:
            return False

    rule = ROBOTS[origin]

    if isinstance(rule, bool):
        return rule

    return rule.can_fetch(
        USER_AGENT,
        url
    )


def fetch_pdf(url):
    if not allowed(url):
        raise PermissionError(
            "Acces non autorise ou robots.txt indisponible."
        )

    with SESSION.get(
        url,
        timeout=TIMEOUT,
        stream=True,
        allow_redirects=False
    ) as response:

        if response.is_redirect:
            raise RuntimeError(
                "Redirection PDF refusee."
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
                    "PDF trop volumineux."
                )

    if not bytes(data).startswith(b"%PDF"):
        raise ValueError(
            "La reponse ne contient pas un PDF."
        )

    time.sleep(PAUSE_SECONDS)

    return bytes(data)


# ============================================================
# LECTURE DES LIGNES PDF
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

            if not text:
                continue

            bbox = line.get("bbox")

            if not bbox:
                continue

            x0, y0, x1, y1 = bbox

            result.append({
                "text": text,
                "x": x0,
                "y": y0,
                "x2": x1,
                "y2": y1
            })

    return result


def columns(page):
    lines = page_lines(page)

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
# VALIDATION DES ARTISTES
# ============================================================

def artist_name(text):
    return clean(
        re.sub(
            r"\s*\(\s*suite\s*\)\s*$",
            "",
            text,
            flags=re.I
        )
    )


def is_bio(text):
    match = BIO.fullmatch(
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

    died = None

    if match.group(2):
        died = int(
            re.sub(
                r"\D",
                "",
                match.group(2)
            )
        )

    return (
        1700 <= born <= 2026
        and (
            died is None
            or born <= died <= 2026
        )
    )


def is_artist(text):
    text = artist_name(text)
    words = text.split()

    if not (
        5 <= len(text) <= 85
        and 2 <= len(words) <= 8
    ):
        return False

    if re.search(
        r"\d|@|https?://|[!?;«»“”]",
        text
    ):
        return False

    if text.endswith((",", ":", ".")):
        return False

    blocked = {
        "collection", "private",
        "particuliere", "membre",
        "received", "following",
        "artist", "artiste",
        "oeuvre", "musee", "museum",
        "provenance", "acquired",
        "acquis", "exposition",
        "exhibited", "production",
        "while", "loosening",
        "knots", "recent",
        "recently", "then",
        "during", "because",
        "where", "which",
        "whose", "through",
        "national", "beaux",
        "arts", "catalogue"
    }

    normalized = [
        keytext(word).strip(
            "',()."
        )
        for word in words
    ]

    if any(
        word in blocked
        for word in normalized
    ):
        return False

    particles = {
        "de", "du", "des",
        "la", "le", "van",
        "von", "der", "den",
        "da", "di", "del",
        "and", "et"
    }

    for i, word in enumerate(words):
        stripped = word.strip("()")

        if not re.match(
            r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’().-]*$",
            word
        ):
            return False

        if i > 0 and keytext(stripped) in particles:
            continue

        if not stripped:
            return False

        if not stripped[0].isupper():
            return False

    return True


# ============================================================
# DETECTION DES LOTS
# ============================================================

def lot_header(text):
    match = LOT.fullmatch(
        clean(text)
    )

    if not match:
        return None

    number = int(
        match.group(1)
    )

    title = clean(
        match.group(2)
    )

    if not (
        1 <= number <= 500
        and re.search(
            r"[A-Za-zÀ-ÿ]",
            title
        )
    ):
        return None

    return number, title


def detect_entries(lines, known=None):
    entries = []

    for i, line in enumerate(lines):
        header = lot_header(
            line["text"]
        )

        if not header:
            continue

        artist = None

        for bio_i in range(
            max(0, i - 3),
            i
        ):
            if not is_bio(
                lines[bio_i]["text"]
            ):
                continue

            for artist_i in range(
                max(0, bio_i - 2),
                bio_i
            ):
                candidate = artist_name(
                    lines[artist_i]["text"]
                )

                if is_artist(candidate):
                    artist = candidate
                    break

            if artist:
                break

        if artist:
            entries.append({
                "index": i,
                "lot": header[0],
                "titre": header[1],
                "artiste": artist
            })

    # Filet de securite pour les lots historiques.
    if known:
        detected_lots = {
            lot_number(x["lot"])
            for x in entries
        }

        for i, line in enumerate(lines):
            header = lot_header(
                line["text"]
            )

            if not header:
                continue

            lot = lot_number(
                header[0]
            )

            if lot in detected_lots:
                continue

            expected = known.get(lot)

            if not expected:
                continue

            has_bio = any(
                is_bio(lines[j]["text"])
                for j in range(
                    max(0, i - 3),
                    i
                )
            )

            if has_bio:
                entries.append({
                    "index": i,
                    "lot": header[0],
                    "titre": header[1],
                    "artiste": expected
                })

                detected_lots.add(lot)

    return sorted(
        entries,
        key=lambda x: x["index"]
    )


# ============================================================
# RECONSTRUCTION PRUDENTE DES TITRES
# ============================================================

def title_continuation(text):
    text = clean(text)

    if not text:
        return False

    if len(text) > 120:
        return False

    if is_bio(text):
        return False

    if lot_header(text):
        return False

    if DIM.search(text):
        return False

    if PRICE_CONTEXT.search(text):
        return False

    if STOP_TITLE.search(text):
        return False

    if re.match(
        r"^\d{4}\s*[-–—]",
        text
    ):
        return False

    if not re.search(
        r"[A-Za-zÀ-ÿ]",
        text
    ):
        return False

    return True


def reconstruct_title(lines, entry, next_index):
    """
    Reconstruit un titre uniquement lorsque sa fin
    indique clairement une continuation.

    Le titre reconstruit reste une proposition :
    une valeur historique differente est preservee.
    """

    original = clean(
        entry["titre"]
    )

    if not original.endswith(
        TITLE_ENDINGS
    ):
        return original

    index = entry["index"]

    if index + 1 >= next_index:
        return original

    first = lines[index]
    following = lines[index + 1]

    candidate = clean(
        following["text"]
    )

    if not title_continuation(candidate):
        return original

    # Verifier la proximite verticale.
    vertical_gap = (
        following["y"]
        - first["y2"]
    )

    if not (
        -3 <= vertical_gap <= 24
    ):
        return original

    # Eviter de rattacher une autre colonne.
    if abs(
        following["x"] - first["x"]
    ) > 45:
        return original

    reconstructed = clean(
        original + " " + candidate
    )

    if len(reconstructed) > 250:
        return original

    return reconstructed


# ============================================================
# ESTIMATIONS
# ============================================================

def money(value):
    digits = re.sub(
        r"\D",
        "",
        value
    )

    if not digits:
        return None

    return int(digits)


def estimate(text):
    match = EST.search(
        clean(text)
    )

    if not match:
        return None

    low = money(
        match.group(1)
    )

    high = money(
        match.group(2)
    )

    if low is None or high is None:
        return None

    if not (
        100 <= low <= high <= 100_000_000
    ):
        return None

    if high > low * 20:
        return None

    return low, high


def extract_estimates(section):
    """
    Cherche les estimations a proximite
    d'un libelle explicite.

    Ne transforme pas une fourchette quelconque
    en prix d'estimation.
    """

    candidates = []

    for i, line in enumerate(section):
        text = line["text"]

        if not PRICE_CONTEXT.search(text):
            continue

        for candidate in section[
            i:min(i + 5, len(section))
        ]:
            result = estimate(
                candidate["text"]
            )

            if result:
                candidates.append(result)
                break

    unique = list(
        dict.fromkeys(candidates)
    )

    if len(unique) == 1:
        return unique[0], False

    if len(unique) > 1:
        return None, True

    return None, False


# ============================================================
# EXTRACTION DES METADONNEES
# ============================================================

def metadata(lines, entries, index):
    entry = entries[index]

    start = entry["index"] + 1

    if index + 1 < len(entries):
        end = entries[index + 1]["index"]
    else:
        end = len(lines)

    section = lines[
        start:min(end, start + 140)
    ]

    dims = None

    for line in section:
        match = DIM.search(
            line["text"]
        )

        if match:
            dims = clean(
                match.group()
            )
            break

    est, conflict = extract_estimates(
        section
    )

    title = reconstruct_title(
        lines,
        entry,
        end
    )

    return dims, est, conflict, title


# ============================================================
# CREATION DES NOTICES
# ============================================================

def notice(
    url,
    sale,
    page_no,
    entry,
    dims,
    est,
    conflict,
    title
):
    lot = lot_number(
        entry["lot"]
    )

    passage = digest(
        "BYDealers",
        url,
        lot
    )

    year = re.search(
        r"20\d{2}",
        sale
    )

    low, high = (
        est if est
        else (None, None)
    )

    return {
        "id": passage,
        "id_passage": passage,
        "id_oeuvre": digest(
            "oeuvre-provisoire",
            passage
        ),

        "maison": "BYDealers",
        "artiste": entry["artiste"],
        "titre": title,
        "lot": lot,
        "dimensions": dims,

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
            if est else None
        ),

        "type_prix": (
            "estimation"
            if est else "inconnu"
        ),

        "devise": "CAD",
        "statut": "catalogue_historique",

        "url": url,
        "source_page": url,
        "source_page_pdf": page_no,

        "origine": "catalogue_pdf_v8_4",

        "confiance_extraction": (
            85 if dims and est and not conflict
            else 65
        ),

        "conflit_estimation": conflict,
        "prix_realise_verifie": False,
        "date_collecte": timestamp()
    }


# ============================================================
# LECTURE COMPLETE D'UN CATALOGUE
# ============================================================

def parse_pdf(data, url, sale, known=None):
    detected = {}
    page_errors = []

    with fitz.open(
        stream=data,
        filetype="pdf"
    ) as doc:

        total_pages = len(doc)

        for page_index in range(total_pages):
            try:
                page = doc[page_index]

                for lines in columns(page):
                    entries = detect_entries(
                        lines,
                        known
                    )

                    for j, entry in enumerate(entries):
                        (
                            dims,
                            est,
                            conflict,
                            title
                        ) = metadata(
                            lines,
                            entries,
                            j
                        )

                        item = notice(
                            url,
                            sale,
                            page_index + 1,
                            entry,
                            dims,
                            est,
                            conflict,
                            title
                        )

                        key = passage_key(
                            item
                        )

                        if key not in detected:
                            detected[key] = item
                        else:
                            previous = detected[key]

                            previous_score = sum([
                                bool(previous.get("dimensions")),
                                previous.get("estimation_min") is not None,
                                not previous.get("conflit_estimation")
                            ])

                            new_score = sum([
                                bool(item.get("dimensions")),
                                item.get("estimation_min") is not None,
                                not item.get("conflit_estimation")
                            ])

                            if new_score > previous_score:
                                detected[key] = item

            except Exception as exc:
                page_errors.append({
                    "page": page_index + 1,
                    "erreur": str(exc)
                })

                print(
                    f"ERREUR page {page_index + 1}: "
                    f"{type(exc).__name__}: {exc}",
                    flush=True
                )

                traceback.print_exc()

    return (
        list(detected.values()),
        total_pages,
        page_errors
    )


# ============================================================
# NORMALISATION DES NOTICES HISTORIQUES
# ============================================================

def normalize_existing(item):
    result = dict(item)
    changes = []

    for field in (
        "artiste",
        "titre",
        "dimensions"
    ):
        if isinstance(
            result.get(field),
            str
        ):
            if field == "artiste":
                cleaned = artist_name(
                    result[field]
                )
            else:
                cleaned = clean(
                    result[field]
                )

            if cleaned != result[field]:
                result[field] = cleaned
                changes.append(field)

    if (
        result.get("estimation_min") is not None
        and result.get("estimation_max") is not None
        and result.get("type_prix") == "inconnu"
    ):
        result["type_prix"] = "estimation"
        changes.append("type_prix")

    if not result.get("id_passage"):
        result["id_passage"] = passage_key(
            result
        )
        changes.append("id_passage")

    if not result.get("id_oeuvre"):
        result["id_oeuvre"] = digest(
            "oeuvre-provisoire",
            passage_key(result)
        )
        changes.append("id_oeuvre")

    return result, changes


# ============================================================
# FUSION SANS PERTE DE DONNEES
# ============================================================

def merge_existing(old, new):
    merged = dict(old)

    additions = []
    conflicts = []
    proposals = []

    for field in (
        "artiste",
        "titre",
        "dimensions"
    ):
        incoming = new.get(field)
        current = old.get(field)

        if incoming and not current:
            merged[field] = incoming
            additions.append(field)

        elif (
            incoming
            and current
            and keytext(incoming) != keytext(current)
        ):
            # Un nom d'artiste invalide n'est
            # pas un conflit documentaire.
            if (
                field == "artiste"
                and not is_artist(incoming)
            ):
                continue

            if field == "titre":
                # Proposer une extension de titre,
                # sans ecraser la valeur historique.
                if (
                    len(incoming) > len(current)
                    and keytext(incoming).startswith(
                        keytext(current)
                    )
                ):
                    proposals.append({
                        "champ": "titre",
                        "ancien": current,
                        "proposition": incoming,
                        "motif": "titre_plus_complet"
                    })

                    continue

                # Ne pas signaler comme conflit
                # un titre simplement tronque.
                if (
                    len(incoming) < len(current)
                    and keytext(current).startswith(
                        keytext(incoming)
                    )
                ):
                    continue

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

    old_pair = (
        old.get("estimation_min"),
        old.get("estimation_max")
    )

    new_pair = (
        new.get("estimation_min"),
        new.get("estimation_max")
    )

    if (
        all(v is not None for v in new_pair)
        and not new.get("conflit_estimation")
    ):
        if all(v is None for v in old_pair):
            for field in (
                "estimation_min",
                "estimation_max",
                "estimation"
            ):
                merged[field] = new[field]

            merged["type_prix"] = "estimation"
            additions.append("estimation")

        elif old_pair != new_pair:
            conflicts.append({
                "champ": "estimation",
                "ancien": old_pair,
                "extrait": new_pair
            })

    if new.get("conflit_estimation"):
        conflicts.append({
            "champ": "estimation",
            "motif": (
                "plusieurs estimations detectees"
            )
        })

    merged["id_passage"] = passage_key(old)

    # Conserver :
    # - id historique
    # - id_oeuvre
    # - prix marteau
    # - prix vendu
    # - provenance
    # - autres champs existants

    return (
        merged,
        additions,
        conflicts,
        proposals
    )


# ============================================================
# RAPPROCHEMENT DES OEUVRES
# ============================================================

def dimensions_key(value):
    match = DIM.search(
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
                float(x.replace(",", ".")),
                1
            )
            for x in numbers
        )
    )


def potential_matches(records):
    groups = defaultdict(list)

    for item in records:
        dims = dimensions_key(
            item.get("dimensions")
        )

        if (
            dims
            and item.get("artiste")
            and item.get("titre")
        ):
            key = (
                keytext(item["artiste"]),
                keytext(item["titre"]),
                dims
            )

            groups[key].append(item)

    result = []

    for group in groups.values():
        passages = {
            passage_key(x)
            for x in group
        }

        if len(passages) < 2:
            continue

        same_catalogue = (
            len({
                x.get("url")
                for x in group
            }) == 1
        )

        result.append({
            "artiste": group[0]["artiste"],
            "titre": group[0]["titre"],
            "dimensions": group[0].get("dimensions"),
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

    return result


# ============================================================
# RAPPORT DE QUALITE
# ============================================================

def quality_report(records, matches):
    anomalies = []

    for item in records:
        issues = []

        if not item.get("dimensions"):
            issues.append(
                "dimensions_absentes"
            )

        if item.get("estimation_min") is None:
            issues.append(
                "estimation_absente"
            )

        if clean(
            item.get("titre")
        ).endswith(TITLE_ENDINGS):
            issues.append(
                "titre_possiblement_tronque"
            )

        if item.get("conflit_estimation"):
            issues.append(
                "conflit_estimation"
            )

        if issues:
            anomalies.append({
                "id": item.get("id"),
                "id_passage": passage_key(item),
                "artiste": item.get("artiste"),
                "titre": item.get("titre"),
                "problemes": issues,
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
            bool(x.get("prix_realise_verifie"))
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
    original = load_db()

    print(
        f"{VERSION}: {len(original)} "
        "notices historiques chargees.",
        flush=True
    )

    db = {}
    cleanup = []

    for old in original:
        item, changed = normalize_existing(
            old
        )

        key = passage_key(item)

        if not key:
            raise ValueError(
                "Notice sans identifiant exploitable."
            )

        if key in db:
            raise ValueError(
                f"Doublon historique : {key}"
            )

        db[key] = item

        if changed:
            cleanup.append({
                "id_passage": key,
                "champs": changed
            })

    historical = set(db)

    seen = set()
    new_keys = []
    additions = []
    conflicts = []
    proposals = []
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
            data = fetch_pdf(url)

            stats["pages_accessibles"] += 1

            known = {
                lot_number(x.get("lot")): x.get("artiste")
                for x in db.values()
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
                known
            )

            stats["pdf_lus"] += 1
            stats["oeuvres_detectees"] += len(found)

            if page_errors:
                stats["erreurs_ou_blocages"] += len(
                    page_errors
                )

            print(
                f"Pages : {pages} | "
                f"Lots : {len(found)} | "
                f"Erreurs : {len(page_errors)}",
                flush=True
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

            for item in found:
                key = passage_key(item)

                seen.add(key)

                if key in db:
                    (
                        merged,
                        added,
                        problems,
                        suggested
                    ) = merge_existing(
                        db[key],
                        item
                    )

                    db[key] = merged

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

                    if suggested:
                        proposals.append({
                            "id_passage": key,
                            "vente": sale,
                            "lot": item["lot"],
                            "propositions": suggested
                        })

                else:
                    db[key] = item

                    new_keys.append(key)

                    stats["nouvelles_oeuvres"] += 1

        except Exception as exc:
            stats["erreurs_ou_blocages"] += 1

            print(
                f"ERREUR CATALOGUE {sale}: "
                f"{type(exc).__name__}: {exc}",
                flush=True
            )

            traceback.print_exc()

            catalogues.append({
                "vente": sale,
                "statut": "erreur",
                "oeuvres": 0,
                "erreur": (
                    f"{type(exc).__name__}: {exc}"
                )
            })

    # --------------------------------------------------------
    # SECURITE
    # --------------------------------------------------------

    if not seen:
        raise RuntimeError(
            "Aucun lot extrait. "
            "Aucun fichier ne sera remplace."
        )

    records = sorted(
        db.values(),
        key=lambda x: (
            keytext(x.get("artiste")),
            keytext(x.get("titre")),
            str(x.get("annee_vente") or ""),
            lot_number(x.get("lot"))
        )
    )

    if len(records) < len(original):
        raise RuntimeError(
            "Regression du nombre de notices. "
            "Sauvegarde annulee."
        )

    # --------------------------------------------------------
    # ANALYSE
    # --------------------------------------------------------

    matches = potential_matches(
        records
    )

    quality = quality_report(
        records,
        matches
    )

    missing = sorted(
        historical - seen
    )

    by_origin = Counter(
        db[key].get("origine", "inconnue")
        for key in missing
    )

    differences = {
        "version": VERSION,
        "date": timestamp(),

        "historique_avant": len(original),

        "detectes_cette_execution": len(seen),

        "non_redetectes": len(missing),

        "non_redetectes_par_origine": dict(
            by_origin
        ),

        "lots_non_redetectes": [
            {
                "id_passage": key,
                "vente": db[key].get("vente"),
                "lot": db[key].get("lot"),
                "artiste": db[key].get("artiste"),
                "titre": db[key].get("titre"),
                "url": db[key].get("url")
            }
            for key in missing
        ],

        "nouveaux_passages": new_keys,

        "nettoyages": cleanup,

        "champs_completes": additions,

        "conflits_a_verifier": conflicts,

        "propositions_corrections": proposals
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

        "avec_estimation": quality[
            "avec_estimation"
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

        "propositions_corrections": len(
            proposals
        ),

        "note": (
            "Estimations de catalogues uniquement. "
            "Aucun prix marteau deduit ou invente."
        )
    }

    # --------------------------------------------------------
    # SAUVEGARDE
    # --------------------------------------------------------

    write_json(
        DB,
        records
    )

    write_json(
        REPORT,
        report
    )

    write_json(
        QUALITY,
        quality
    )

    write_json(
        DIFF,
        differences
    )

    # --------------------------------------------------------
    # RESUME
    # --------------------------------------------------------

    print(
        "\n========================================",
        flush=True
    )

    print(
        f"ART ENCHERES QUEBEC — {VERSION}",
        flush=True
    )

    print(
        "========================================",
        flush=True
    )

    print(
        f"Notices : {len(records)}",
        flush=True
    )

    print(
        f"Lots detectes : {len(seen)}",
        flush=True
    )

    print(
        f"Lots non redetectes : {len(missing)}",
        flush=True
    )

    print(
        f"Estimations : {quality['avec_estimation']}",
        flush=True
    )

    print(
        f"Dimensions : {quality['avec_dimensions']}",
        flush=True
    )

    print(
        f"Conflits : {len(conflicts)}",
        flush=True
    )

    print(
        f"Corrections proposees : {len(proposals)}",
        flush=True
    )

    print(
        f"Rapprochements : {len(matches)}",
        flush=True
    )

    print(
        f"Prix marteau verifies : "
        f"{quality['prix_marteau_verifies']}",
        flush=True
    )

    print(
        "========================================",
        flush=True
    )


if __name__ == "__main__":
    main()
