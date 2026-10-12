

"""
ART ENCHERES QUEBEC V6
Collecteur public de notices d'oeuvres.

Extraction adaptee aux catalogues PDF BYDealers :
ARTISTE
1 9 0 4 - 1 9 9 0
04 Album de famille
1958
Huile sur toile
71,8 x 122,6 cm
Estimation/Estimate
80 000 - 100 000 $

Ne publie pas de prix marteau non verifies.
Ne reproduit pas les images des catalogues.
"""

import hashlib
import json
import logging
import re
import time
import unicodedata

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urldefrag, urlparse
from urllib.robotparser import RobotFileParser

import fitz
import requests
from bs4 import BeautifulSoup


# =====================================================
# CONFIGURATION
# =====================================================

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUTPUT = DATA / "oeuvres.json"
REPORT = DATA / "rapport-collecte.json"

VERSION = "V6"
AGENT = "ArtEncheresQuebec/6.0"

TIMEOUT = 25
DELAY = 2
MAX_BYTES = 25 * 1024 * 1024

MAX_PAGES_SOURCE = 30
MAX_PDFS_SOURCE = 8
MAX_PAGES_PDF = 200

SOURCES = {
    "BYDealers": [
        "https://bydealers.com/fr/",
        "https://bydealers.com/browse-auction-catalogues/",
        "https://bydealers.com/wp-content/uploads/2026/04/Catalogue_Printemps2026.pdf",
        "https://bydealers.com/wp-content/uploads/2026/05/Catalogue_102p_Printemps_succession2026_Web_simple-1.pdf",
        "https://bydealers.com/wp-content/uploads/2026/02/Catalogue_Fall_2025-1.pdf",
    ],
    "Heffel": [
        "https://www.heffel.com/Auction/",
    ],
    "Gelineau": [
        "https://www.encangelineau.com/",
    ],
}

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)

session = requests.Session()
session.headers.update({
    "User-Agent": AGENT,
    "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.5",
})

robots_cache = {}


# =====================================================
# UTILITAIRES
# =====================================================

def now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    text = str(value or "")
    text = "".join(
        " " if ord(ch) < 32 else ch
        for ch in text
    )
    return re.sub(r"\s+", " ", text).strip()


def normalized(value):
    value = unicodedata.normalize(
        "NFKD", clean(value)
    )
    return "".join(
        ch for ch in value
        if not unicodedata.combining(ch)
    ).lower()


def canonical(url):
    return urldefrag(url)[0]


def work_id(source, url, lot, title):
    raw = "|".join([
        source,
        canonical(url),
        str(lot or ""),
        normalized(title),
    ])
    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:20]


def amount(value):
    digits = re.sub(
        r"[^\d]",
        "",
        str(value or "")
    )
    return int(digits) if digits else None


# =====================================================
# ROBOTS.TXT ET ACCES PUBLIC
# =====================================================

def allowed(url):
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return False

    origin = (
        f"{parsed.scheme}://{parsed.netloc}"
    )

    if origin not in robots_cache:
        try:
            response = session.get(
                origin + "/robots.txt",
                timeout=TIMEOUT,
                allow_redirects=False,
            )

            if response.status_code == 200:
                rp = RobotFileParser()
                rp.parse(response.text.splitlines())
                robots_cache[origin] = rp

            elif response.status_code in (404, 410):
                rp = RobotFileParser()
                rp.parse([])
                robots_cache[origin] = rp

            else:
                logging.warning(
                    "robots.txt non verifiable: %s",
                    origin,
                )
                return False

        except requests.RequestException:
            logging.warning(
                "Erreur robots.txt: %s", origin
            )
            return False

    return robots_cache[origin].can_fetch(
        AGENT, url
    )


def fetch(url):
    if not allowed(url):
        logging.warning(
            "Acces refuse/non verifiable: %s",
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

        length = int(
            response.headers.get(
                "Content-Length", "0"
            ) or 0
        )

        if length > MAX_BYTES:
            response.close()
            return None

        parts = []
        total = 0

        try:
            for chunk in response.iter_content(65536):
                total += len(chunk)

                if total > MAX_BYTES:
                    return None

                parts.append(chunk)
        finally:
            response.close()

        body = b"".join(parts)
        time.sleep(DELAY)

        if body.startswith(b"%PDF"):
            return "pdf", body

        content_type = response.headers.get(
            "Content-Type", ""
        ).lower()

        if (
            "html" in content_type
            or body.lstrip().lower().startswith(
                (b"<!doctype", b"<html")
            )
        ):
            return (
                "html",
                BeautifulSoup(
                    body, "html.parser"
                ),
            )

        return None

    except requests.RequestException as exc:
        logging.warning(
            "Erreur HTTP %s: %s", url, exc
        )
        return None


# =====================================================
# MOTIFS PDF BYDEALERS
# =====================================================

# Exemple : 1 9 0 4 - 1 9 9 0
BIO_RE = re.compile(
    r"^\s*((?:[12]\s*)\d\s*\d\s*\d)"
    r"\s*[-–—]\s*"
    r"((?:[12]\s*)\d\s*\d\s*\d)\s*$"
)

# Exemple : 04 Album de famille
LOT_RE = re.compile(
    r"^\s*(\d{1,3})\s+(.{3,150})$"
)

# Exemple : Lot 22 : Guido Molinari...
COVER_LOT_RE = re.compile(
    r"^\s*Lot\s+(\d{1,3})\s*:",
    re.I,
)

DIM_RE = re.compile(
    r"\d{1,3}(?:[.,]\d+)?"
    r"\s*[×x]\s*"
    r"\d{1,3}(?:[.,]\d+)?"
    r"(?:\s*[×x]\s*"
    r"\d{1,3}(?:[.,]\d+)?)?"
    r"\s*cm\b",
    re.I,
)

EST_RE = re.compile(
    r"([\d][\d\s\u00a0]*)"
    r"\s*[-–—]\s*"
    r"([\d][\d\s\u00a0]*)"
    r"\s*(?:\$|CAD)",
    re.I,
)

YEAR_RE = re.compile(
    r"^(?:c\.\s*)?(?:18|19|20)\d{2}"
    r"(?:\s*[-–]\s*(?:18|19|20)\d{2})?$",
    re.I,
)

STOP_WORDS = {
    "provenance",
    "bibliographie",
    "bibliographie/literature",
    "literature",
    "estimation/estimate",
    "estimate",
    "exposition",
    "exposition/exhibition",
    "exhibition",
}

BAD_ARTISTS = {
    "bydealers",
    "catalogue",
    "important canadian art",
    "art canadien important",
    "estimation/estimate",
    "provenance",
    "bibliographie/literature",
}


def biography(text):
    match = BIO_RE.fullmatch(clean(text))

    if not match:
        return False

    first = amount(match.group(1))
    second = amount(match.group(2))

    if not first or not second:
        return False

    return (
        1700 <= first <= 2026
        and 1700 <= second <= 2026
        and second >= first
    )


def valid_artist(text):
    text = clean(text)

    if not 5 <= len(text) <= 85:
        return False

    if normalized(text) in BAD_ARTISTS:
        return False

    if re.search(r"\d|@|https?://", text):
        return False

    if "/" in text:
        return False

    if not re.search(
        r"[A-Za-zÀ-ÿ]", text
    ):
        return False

    return len(text.split()) >= 2


def valid_title(text):
    text = clean(text)

    if not 3 <= len(text) <= 150:
        return False

    if not re.search(
        r"[A-Za-zÀ-ÿ]", text
    ):
        return False

    if normalized(text) in STOP_WORDS:
        return False

    if "@" in text or "http" in text.lower():
        return False

    return True


# =====================================================
# CREATION DES NOTICES
# =====================================================

def create_work(
    source,
    url,
    lot,
    artist,
    title,
    dimensions,
    estimate_min,
    estimate_max,
    page_number,
    confidence,
):
    return {
        "id": work_id(
            source, url, lot, title
        ),
        "maison": source,
        "artiste": clean(artist),
        "titre": clean(title),
        "lot": str(lot).zfill(2),
        "dimensions": dimensions,
        "image": None,
        "prix": None,
        "prix_marteau": None,
        "prix_vendu": None,
        "estimation_min": estimate_min,
        "estimation_max": estimate_max,
        "estimation": (
            f"{estimate_min:,} - {estimate_max:,} CAD"
            if estimate_min is not None
            and estimate_max is not None
            else None
        ),
        "type_prix": "estimation"
        if estimate_min is not None
        else "inconnu",
        "devise": "CAD",
        "statut": "catalogue",
        "url": url,
        "source_page": url,
        "source_page_pdf": page_number,
        "origine": "catalogue_pdf_v6",
        "confiance_extraction": confidence,
        "prix_realise_verifie": False,
        "date_collecte": now(),
    }


# =====================================================
# LECTURE DES LIGNES PDF
# =====================================================

def page_lines(page):
    """
    PyMuPDF fournit le texte et sa position.
    On conserve l'ordre d'extraction des blocs,
    utile dans les catalogues a plusieurs colonnes.
    """

    result = []

    data = page.get_text(
        "dict", sort=False
    )

    for block in data.get("blocks", []):
        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):
            text = clean(" ".join(
                span.get("text", "")
                for span in line.get("spans", [])
            ))

            if not text:
                continue

            result.append({
                "text": text,
                "bbox": line.get(
                    "bbox", [0, 0, 0, 0]
                ),
            })

    return result


def title_from_lines(lines, start):
    """
    Detecte les titres sur une ou deux lignes.
    Ne prend pas une date ou une description
    technique pour la suite du titre.
    """

    first = lines[start]["text"]

    match = LOT_RE.match(first)

    if not match:
        return None, None

    lot = int(match.group(1))
    title = clean(match.group(2))

    if not 1 <= lot <= 500:
        return None, None

    if not valid_title(title):
        return None, None

    if start + 1 < len(lines):
        following = lines[start + 1]["text"]

        continuation = (
            title.endswith("/")
            or title.endswith("-")
            or title.endswith(",")
        )

        if (
            continuation
            and valid_title(following)
            and not YEAR_RE.fullmatch(following)
            and not DIM_RE.search(following)
            and not biography(following)
            and not LOT_RE.match(following)
        ):
            title = clean(
                title + " " + following
            )

    return lot, title


def nearby_metadata(lines, index):
    """
    Cherche dimensions et estimation dans la
    notice, sans franchir le prochain artiste
    suivi d'une ligne biographique.
    """

    dimensions = None
    estimate_min = None
    estimate_max = None

    end = min(
        len(lines), index + 90
    )

    for j in range(index + 1, end):
        text = lines[j]["text"]

        if (
            biography(text)
            and j > 0
            and valid_artist(
                lines[j - 1]["text"]
            )
        ):
            break

        if dimensions is None:
            dim = DIM_RE.search(text)

            if dim:
                dimensions = dim.group(0)

        if normalized(text).startswith(
            ("estimation/estimate", "estimate")
        ):
            for k in range(
                j + 1,
                min(j + 4, len(lines)),
            ):
                price_match = EST_RE.search(
                    lines[k]["text"]
                )

                if price_match:
                    low = amount(
                        price_match.group(1)
                    )
                    high = amount(
                        price_match.group(2)
                    )

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


# =====================================================
# EXTRACTION PDF V6
# =====================================================

def parse_pdf(body, source, url):
    works = []

    try:
        doc = fitz.open(
            stream=body,
            filetype="pdf",
        )

        logging.info(
            "PDF : %s (%s pages)",
            url, len(doc)
        )

        for page_index in range(
            min(len(doc), MAX_PAGES_PDF)
        ):
            lines = page_lines(
                doc[page_index]
            )

            for i in range(
                2, len(lines)
            ):
                # Structure verifiee dans
                # les catalogues BYDealers :
                # artiste, dates, lot + titre.
                if not biography(
                    lines[i - 1]["text"]
                ):
                    continue

                artist = lines[i - 2]["text"]

                if not valid_artist(artist):
                    continue

                lot, title = title_from_lines(
                    lines, i
                )

                if lot is None:
                    continue

                (
                    dimensions,
                    estimate_min,
                    estimate_max,
                ) = nearby_metadata(
                    lines, i
                )

                confidence = 75

                if dimensions:
                    confidence += 10

                if estimate_min is not None:
                    confidence += 10

                work = create_work(
                    source=source,
                    url=url,
                    lot=lot,
                    artist=artist,
                    title=title,
                    dimensions=dimensions,
                    estimate_min=estimate_min,
                    estimate_max=estimate_max,
                    page_number=page_index + 1,
                    confidence=confidence,
                )

                works.append(work)

        doc.close()

    except Exception as exc:
        logging.warning(
            "Erreur PDF %s: %s",
            url, exc
        )

    return works


# =====================================================
# HTML : DONNEES STRUCTUREES
# =====================================================

def jsonld_items(value):
    if isinstance(value, list):
        for item in value:
            yield from jsonld_items(item)

    elif isinstance(value, dict):
        yield value

        if "@graph" in value:
            yield from jsonld_items(
                value["@graph"]
            )


def parse_html(soup, source, url):
    works = []

    for script in soup.find_all(
        "script",
        type="application/ld+json",
    ):
        try:
            raw = json.loads(
                script.string
                or script.get_text()
            )
        except (TypeError, ValueError):
            continue

        for item in jsonld_items(raw):
            types = item.get("@type", [])

            if isinstance(types, str):
                types = [types]

            if not isinstance(types, list):
                continue

            if not set(types).intersection({
                "VisualArtwork",
                "Painting",
            }):
                continue

            title = clean(item.get("name"))

            creator = item.get("creator")

            if isinstance(creator, dict):
                artist = clean(
                    creator.get("name")
                )
            elif isinstance(creator, str):
                artist = clean(creator)
            else:
                artist = ""

            if (
                not valid_artist(artist)
                or not valid_title(title)
            ):
                continue

            item_url = urljoin(
                url,
                item.get("url") or url,
            )

            work = create_work(
                source,
                item_url,
                "0",
                artist,
                title,
                None,
                None,
                None,
                None,
                70,
            )

            work["lot"] = None
            work["origine"] = "html_jsonld_v6"
            work["statut"] = "a_verifier"
            work["type_prix"] = "inconnu"

            # Pas de republication automatique
            # des photographies.
            works.append(work)

    return works


# =====================================================
# DECOUVERTE DE CATALOGUES
# =====================================================

def discover(soup, base, source):
    found = []
    hostname = urlparse(base).hostname

    for link in soup.find_all(
        "a", href=True
    ):
        url = canonical(
            urljoin(
                base, link["href"]
            )
        )

        parsed = urlparse(url)

        if parsed.hostname != hostname:
            continue

        if parsed.scheme not in (
            "http", "https"
        ):
            continue

        path = normalized(parsed.path)
        label = normalized(
            link.get_text(
                " ", strip=True
            )
        )

        is_pdf = path.endswith(".pdf")

        if source == "BYDealers":
            relevant = (
                is_pdf
                and "catalogue" in path
            ) or (
                not is_pdf
                and any(
                    word in path + " " + label
                    for word in (
                        "catalogue",
                        "auction",
                        "enchere",
                    )
                )
            )

        elif source == "Heffel":
            relevant = any(
                word in path + " " + label
                for word in (
                    "auction",
                    "result",
                    "catalogue",
                )
            )

        else:
            relevant = any(
                word in path + " " + label
                for word in (
                    "encan",
                    "catalogue",
                    "auction",
                )
            )

        if relevant and url not in found:
            found.append(url)

    return found


# =====================================================
# BASE DE DONNEES
# =====================================================

def load_database():
    if not OUTPUT.exists():
        return {}, 0

    raw = json.loads(
        OUTPUT.read_text(
            encoding="utf-8"
        )
    )

    if isinstance(raw, dict):
        raw = raw.get("oeuvres", [])

    if not isinstance(raw, list):
        raise ValueError(
            "Format oeuvres.json invalide"
        )

    database = {}
    removed = 0

    obsolete = {
        "catalogue_pdf_non_verifie",
        "catalogue_pdf_v5",
    }

    for record in raw:
        if not isinstance(record, dict):
            continue

        if (
            record.get("origine") in obsolete
            and record.get("statut")
            in ("a_verifier", "catalogue")
        ):
            removed += 1
            continue

        key = record.get("id")

        if not key:
            key = work_id(
                record.get("maison", ""),
                record.get("url", ""),
                record.get("lot", ""),
                record.get("titre", ""),
            )
            record["id"] = key

        database[key] = record

    return database, removed


def merge(database, record):
    key = record["id"]

    if key not in database:
        database[key] = record
        return True

    existing = database[key]

    for field, value in record.items():
        if field == "date_collecte":
            continue

        if (
            value is not None
            and existing.get(field)
            in (None, "")
        ):
            existing[field] = value

    return False


# =====================================================
# COLLECTE
# =====================================================

def collect_source(
    source,
    seeds,
    database,
):
    queue = [
        canonical(url)
        for url in seeds
    ]

    visited = set()

    stats = {
        "pages_consultees": 0,
        "pages_accessibles": 0,
        "pdf_lus": 0,
        "oeuvres_detectees": 0,
        "nouvelles_oeuvres": 0,
        "erreurs_ou_blocages": 0,
    }

    while (
        queue
        and len(visited)
        < MAX_PAGES_SOURCE
    ):
        url = queue.pop(0)

        if url in visited:
            continue

        if (
            url.lower().endswith(".pdf")
            and stats["pdf_lus"]
            >= MAX_PDFS_SOURCE
        ):
            continue

        visited.add(url)
        stats["pages_consultees"] += 1

        logging.info(
            "%s : %s", source, url
        )

        result = fetch(url)

        if result is None:
            stats[
                "erreurs_ou_blocages"
            ] += 1
            continue

        kind, content = result

        stats["pages_accessibles"] += 1

        if kind == "pdf":
            stats["pdf_lus"] += 1

            works = parse_pdf(
                content,
                source,
                url,
            )

        else:
            works = parse_html(
                content,
                source,
                url,
            )

            for link in discover(
                content,
                url,
                source,
            ):
                if (
                    link not in visited
                    and link not in queue
                ):
                    queue.append(link)

        stats[
            "oeuvres_detectees"
        ] += len(works)

        for work in works:
            if merge(
                database, work
            ):
                stats[
                    "nouvelles_oeuvres"
                ] += 1

    return stats


# =====================================================
# PROGRAMME PRINCIPAL
# =====================================================

def main():
    DATA.mkdir(
        parents=True,
        exist_ok=True,
    )

    database, removed = load_database()

    report = {
        "version": VERSION,
        "date": now(),
        "anciennes_entrees_retirees": removed,
        "sources": {},
        "total_oeuvres": 0,
        "note": (
            "Les estimations de catalogue "
            "ne sont pas des prix de vente."
        ),
    }

    for source, seeds in SOURCES.items():
        logging.info(
            "COLLECTE %s", source
        )

        report["sources"][source] = (
            collect_source(
                source,
                seeds,
                database,
            )
        )

    records = sorted(
        database.values(),
        key=lambda item: (
            item.get("maison") or "",
            item.get("artiste") or "",
            item.get("titre") or "",
        ),
    )

    OUTPUT.write_text(
        json.dumps(
            records,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    report["total_oeuvres"] = len(records)

    REPORT.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    logging.info(
        "================================"
    )
    logging.info(
        "ART ENCHERES QUEBEC V6"
    )
    logging.info(
        "TOTAL : %s oeuvres",
        len(records),
    )

    for source, stats in report[
        "sources"
    ].items():
        logging.info(
            "%s : %s oeuvres detectees, "
            "%s nouvelles, %s PDF",
            source,
            stats["oeuvres_detectees"],
            stats["nouvelles_oeuvres"],
            stats["pdf_lus"],
        )


if __name__ == "__main__":
    main()
