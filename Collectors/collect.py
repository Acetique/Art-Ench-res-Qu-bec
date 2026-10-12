
"""
ART ENCHERES QUEBEC V5
Collecteur prudent de catalogues publics.

Fonctions :
- Respect de robots.txt
- Lecture HTML et PDF
- Detection stricte de lots PDF
- Extraction d'artistes, titres et dimensions lorsque possible
- Rejet des faux lots les plus evidents
- Suppression des anciennes detections PDF V4 non verifiees
- Preservation des donnees existantes non issues des PDF V4
- Rapport JSON detaille

Les catalogues ne constituent PAS des preuves de vente.
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


# ==========================================================
# CONFIGURATION
# ==========================================================

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

OUTPUT_FILE = DATA_DIR / "oeuvres.json"
REPORT_FILE = DATA_DIR / "rapport-collecte.json"

USER_AGENT = "ArtEncheresQuebec/5.0"

TIMEOUT = 25
REQUEST_DELAY = 2

MAX_PAGES_PER_SOURCE = 30
MAX_PDFS_PER_SOURCE = 5
MAX_PDF_PAGES = 160
MAX_FILE_BYTES = 20 * 1024 * 1024

SOURCES = {
    "BYDealers": [
        "https://bydealers.com/fr/",
        "https://bydealers.com/browse-auction-catalogues/",
    ],
    "Heffel": [
        "https://www.heffel.com/Auction/",
    ],
    "Gelineau": [
        "https://www.encangelineau.com/",
    ],
}

session = requests.Session()

session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.5",
})

robots_cache = {}

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)


# ==========================================================
# OUTILS GENERAUX
# ==========================================================

def now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    return re.sub(
        r"\s+",
        " ",
        str(value or "")
    ).strip()


def normalize(value):
    text = unicodedata.normalize("NFKD", clean(value))
    return "".join(
        c for c in text
        if not unicodedata.combining(c)
    ).lower()


def canonical(url):
    return urldefrag(url)[0]


def make_id(source, url, lot, title=""):
    raw = f"{source}|{canonical(url)}|{lot}|{title}"
    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:20]


def parse_price(value):
    text = re.sub(
        r"[^\d.,]",
        "",
        str(value or "")
    )

    if not text:
        return None

    if "," in text and "." in text:
        if text.rfind(".") > text.rfind(","):
            text = text.replace(",", "")
        else:
            text = text.replace(".", "").replace(",", ".")

    elif "," in text:
        if len(text.split(",")[-1]) <= 2:
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")

    try:
        return float(text)
    except ValueError:
        return None


# ==========================================================
# ROBOTS.TXT
# ==========================================================

def allowed(url):
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return False

    origin = f"{parsed.scheme}://{parsed.netloc}"

    if origin not in robots_cache:

        robots_url = origin + "/robots.txt"

        try:
            response = session.get(
                robots_url,
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

                logging.info(
                    "robots.txt absent: %s",
                    origin
                )

            else:

                logging.warning(
                    "robots.txt non verifiable: %s HTTP %s",
                    origin,
                    response.status_code,
                )

                return False

        except requests.RequestException as exc:

            logging.warning(
                "Erreur robots.txt %s: %s",
                origin,
                exc,
            )

            return False

    return robots_cache[origin].can_fetch(
        USER_AGENT,
        url,
    )


# ==========================================================
# TELECHARGEMENT
# ==========================================================

def fetch(url):

    if not allowed(url):

        logging.warning(
            "Acces refuse ou non verifiable: %s",
            url
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

            logging.info(
                "Redirection non suivie: %s",
                url
            )

            response.close()
            return None

        response.raise_for_status()

        size = int(
            response.headers.get(
                "Content-Length", "0"
            ) or 0
        )

        if size > MAX_FILE_BYTES:
            response.close()
            return None

        chunks = []
        total = 0

        try:
            for chunk in response.iter_content(65536):

                total += len(chunk)

                if total > MAX_FILE_BYTES:
                    return None

                chunks.append(chunk)

        finally:
            response.close()

        body = b"".join(chunks)

        time.sleep(REQUEST_DELAY)

        content_type = response.headers.get(
            "Content-Type", ""
        ).lower()

        if (
            "pdf" in content_type
            or body.startswith(b"%PDF")
        ):
            return "pdf", body

        if (
            "html" in content_type
            or body.lstrip().lower().startswith(
                (b"<!doctype", b"<html")
            )
        ):
            return (
                "html",
                BeautifulSoup(
                    body,
                    "html.parser"
                ),
            )

        return None

    except requests.RequestException as exc:

        logging.warning(
            "Erreur HTTP %s: %s",
            url,
            exc
        )

        return None


# ==========================================================
# VALIDATION DES TITRES ET ARTISTES
# ==========================================================

REJECT_PATTERNS = [
    r"^\d[\d\s.,$€-]*$",
    r"^\d{1,2}\s+(mai|may|juin|june|octobre|october)",
    r"^https?://",
    r"^www\.",
    r"doi\.org",
    r"^\d{4}\s*:",
    r"^\d{4}\s*/",
    r"^\d{4}\s*,",
    r"^\d{4}\s*[-–]\s*\d{4}",
    r"^page\s+\d+",
    r"^isbn",
    r"^issn",
    r"^bibliograph",
    r"^exposition",
    r"^exhibition",
    r"^catalogue",
    r"^copyright",
    r"^telephone",
    r"^t[eé]l[eé]phone",
    r"^estimation",
    r"^estimate",
    r"^collection",
    r"^provenance",
    r"^mus[eé]e",
]

REJECT_RE = [
    re.compile(x, re.I)
    for x in REJECT_PATTERNS
]

DIMENSION_RE = re.compile(
    r"\b\d{1,3}(?:[.,]\d+)?\s*"
    r"(?:x|×)\s*"
    r"\d{1,3}(?:[.,]\d+)?"
    r"(?:\s*(?:x|×)\s*"
    r"\d{1,3}(?:[.,]\d+)?)?"
    r"\s*(?:cm|po|in|inches|pouces)?\b",
    re.I,
)

YEAR_RE = re.compile(
    r"^(?:18|19|20)\d{2}$"
)

LOT_HEADER_RE = re.compile(
    r"^(?:lot|lot no|lot n[o°.]*)\s*"
    r"[:#-]?\s*(\d{1,4})$",
    re.I,
)

NUMBER_ONLY_RE = re.compile(
    r"^\d{1,4}$"
)

ARTIST_LINE_RE = re.compile(
    r"^[A-ZÀ-ÖØ-Ý][A-ZÀ-ÖØ-Ý'’ .-]{5,80}$"
)


def valid_title(title):

    title = clean(title)

    if len(title) < 4 or len(title) > 140:
        return False

    for pattern in REJECT_RE:
        if pattern.search(title):
            return False

    if "@" in title:
        return False

    if title.count("/") >= 3:
        return False

    if not re.search(
        r"[A-Za-zÀ-ÿ]",
        title
    ):
        return False

    return True


def valid_artist(artist):

    artist = clean(artist)

    if not 5 <= len(artist) <= 80:
        return False

    if re.search(r"\d", artist):
        return False

    if not ARTIST_LINE_RE.fullmatch(artist):
        return False

    banned = (
        "CATALOGUE",
        "PROVENANCE",
        "BIBLIOGRAPHIE",
        "ESTIMATION",
        "EXPOSITION",
        "COLLECTION",
        "DIMENSIONS",
        "AUCTION",
        "BYDEALERS",
        "HEFFEL",
    )

    return not any(
        word in normalize(artist).upper()
        for word in banned
    )


# ==========================================================
# CONSTRUCTION DES OEUVRES
# ==========================================================

def make_work(
    source,
    url,
    lot,
    artist,
    title,
    dimensions=None,
    estimate=None,
    page=None,
    confidence=0,
):

    result = {
        "id": make_id(
            source,
            url,
            lot,
            title,
        ),
        "maison": source,
        "artiste": clean(artist),
        "titre": clean(title),
        "lot": str(lot) if lot else None,
        "dimensions": dimensions,
        "image": None,
        "prix": None,
        "prix_marteau": None,
        "prix_vendu": None,
        "estimation": estimate,
        "type_prix": "inconnu",
        "devise": "CAD",
        "statut": "a_verifier",
        "url": url,
        "source_page": url,
        "source_page_pdf": page,
        "origine": "catalogue_pdf_v5",
        "confiance_extraction": confidence,
        "date_collecte": now(),
    }

    return result


# ==========================================================
# EXTRACTION DES PDF
# ==========================================================

def parse_pdf(body, source, url):

    works = []

    try:
        document = fitz.open(
            stream=body,
            filetype="pdf",
        )

        logging.info(
            "PDF ouvert: %s (%s pages)",
            url,
            len(document),
        )

        for page_index in range(
            min(
                len(document),
                MAX_PDF_PAGES,
            )
        ):

            page = document[page_index]

            lines = [
                clean(line)
                for line in page.get_text(
                    "text"
                ).splitlines()
                if clean(line)
            ]

            for i, line in enumerate(lines):

                lot_match = LOT_HEADER_RE.match(line)

                if not lot_match:
                    continue

                lot = lot_match.group(1)

                if not 1 <= int(lot) <= 1000:
                    continue

                # Le titre et l'artiste doivent
                # apparaitre apres l'entete du lot.
                nearby = lines[i + 1:i + 9]

                if len(nearby) < 2:
                    continue

                artist = None
                title = None
                dimensions = None

                for candidate in nearby:

                    if (
                        artist is None
                        and valid_artist(candidate)
                    ):
                        artist = candidate
                        continue

                    if (
                        artist is not None
                        and title is None
                        and valid_title(candidate)
                        and candidate != artist
                    ):
                        title = candidate

                    if dimensions is None:

                        dim = DIMENSION_RE.search(
                            candidate
                        )

                        if dim:
                            dimensions = dim.group(0)

                if not artist or not title:
                    continue

                confidence = 70

                if dimensions:
                    confidence += 15

                if source == "BYDealers":
                    confidence += 5

                record = make_work(
                    source=source,
                    url=url,
                    lot=lot,
                    artist=artist,
                    title=title,
                    dimensions=dimensions,
                    page=page_index + 1,
                    confidence=confidence,
                )

                works.append(record)

        document.close()

    except Exception as exc:

        logging.warning(
            "Erreur lecture PDF %s: %s",
            url,
            exc,
        )

    return works


# ==========================================================
# EXTRACTION HTML JSON-LD
# ==========================================================

def jsonld_objects(data):

    if isinstance(data, list):

        for item in data:
            yield from jsonld_objects(item)

    elif isinstance(data, dict):

        yield data

        if "@graph" in data:
            yield from jsonld_objects(
                data["@graph"]
            )


def parse_jsonld(soup, source, url):

    works = []

    for script in soup.find_all(
        "script",
        type="application/ld+json",
    ):

        try:
            data = json.loads(
                script.string
                or script.get_text()
            )
        except (ValueError, TypeError):
            continue

        for item in jsonld_objects(data):

            types = item.get(
                "@type", []
            )

            if isinstance(types, str):
                types = [types]

            if not isinstance(types, list):
                continue

            if not set(types).intersection({
                "VisualArtwork",
                "Painting",
                "Product",
            }):
                continue

            title = clean(
                item.get("name")
            )

            if not valid_title(title):
                continue

            creator = item.get(
                "creator", ""
            )

            if isinstance(creator, dict):
                artist = clean(
                    creator.get("name")
                )
            elif isinstance(creator, str):
                artist = clean(creator)
            else:
                artist = ""

            if not valid_artist(artist):
                continue

            item_url = urljoin(
                url,
                item.get("url") or url,
            )

            record = make_work(
                source=source,
                url=item_url,
                lot=None,
                artist=artist,
                title=title,
                confidence=70,
            )

            record["origine"] = "html_jsonld_v5"

            image = item.get("image")

            if isinstance(image, list):
                image = image[0] if image else None

            if isinstance(image, dict):
                image = image.get("url")

            if isinstance(image, str):
                record["image"] = urljoin(
                    url, image
                )

            works.append(record)

    return works


# ==========================================================
# DECOUVERTE DES LIENS
# ==========================================================

def discover_links(soup, base, source):

    host = urlparse(base).hostname
    results = []

    for a in soup.find_all(
        "a",
        href=True,
    ):

        url = canonical(
            urljoin(
                base,
                a["href"],
            )
        )

        parsed = urlparse(url)

        if parsed.hostname != host:
            continue

        if parsed.scheme not in (
            "http",
            "https",
        ):
            continue

        path = parsed.path.lower()

        label = normalize(
            a.get_text(
                " ",
                strip=True,
            )
        )

        is_pdf = path.endswith(".pdf")

        if source == "BYDealers":

            relevant = (
                is_pdf
                and any(
                    x in path
                    for x in (
                        "catalogue",
                        "bydealers",
                        "byd_",
                    )
                )
            )

            if not is_pdf:
                relevant = any(
                    x in path + " " + label
                    for x in (
                        "catalogue",
                        "auction",
                        "enchere",
                        "oeuvre",
                    )
                )

        elif source == "Heffel":

            relevant = any(
                x in path + " " + label
                for x in (
                    "result",
                    "auction",
                    "lot",
                )
            )

        else:

            relevant = any(
                x in path + " " + label
                for x in (
                    "catalogue",
                    "encan",
                    "auction",
                    "tableau",
                )
            )

        if relevant and url not in results:
            results.append(url)

    return results


# ==========================================================
# BASE DE DONNEES
# ==========================================================

def load_database():

    if not OUTPUT_FILE.exists():
        return {}, 0

    raw = json.loads(
        OUTPUT_FILE.read_text(
            encoding="utf-8"
        )
    )

    if isinstance(raw, list):
        records = raw

    elif isinstance(raw, dict):
        records = raw.get(
            "oeuvres", []
        )

    else:
        raise ValueError(
            "Format JSON invalide"
        )

    if not isinstance(records, list):
        raise ValueError(
            "Liste oeuvres invalide"
        )

    database = {}
    removed = 0

    for record in records:

        if not isinstance(record, dict):
            continue

        # Nettoyage des detections PDF
        # non verifiees de V4.
        if (
            record.get("origine")
            == "catalogue_pdf_non_verifie"
            and record.get("statut")
            == "a_verifier"
        ):
            removed += 1
            continue

        record_id = record.get("id")

        if record_id:
            database[record_id] = record

    return database, removed


def merge(database, record):

    record_id = record["id"]

    if record_id not in database:

        database[record_id] = record
        return True

    old = database[record_id]

    for key, value in record.items():

        if key == "date_collecte":
            continue

        if (
            value is not None
            and old.get(key) in (
                None,
                "",
            )
        ):
            old[key] = value

    return False


# ==========================================================
# COLLECTE PAR MAISON
# ==========================================================

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
        < MAX_PAGES_PER_SOURCE
    ):

        url = queue.pop(0)

        if url in visited:
            continue

        if (
            url.lower().endswith(".pdf")
            and stats["pdf_lus"]
            >= MAX_PDFS_PER_SOURCE
        ):
            continue

        visited.add(url)

        stats["pages_consultees"] += 1

        logging.info(
            "%s: %s",
            source,
            url,
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

            works = parse_jsonld(
                content,
                source,
                url,
            )

            discovered = discover_links(
                content,
                url,
                source,
            )

            for link in discovered:

                if (
                    link not in visited
                    and link not in queue
                ):
                    queue.append(link)

        stats[
            "oeuvres_detectees"
        ] += len(works)

        for record in works:

            if merge(
                database,
                record,
            ):
                stats[
                    "nouvelles_oeuvres"
                ] += 1

    return stats


# ==========================================================
# EXECUTION PRINCIPALE
# ==========================================================

def main():

    DATA_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    database, removed = load_database()

    logging.info(
        "Anciennes entrees PDF V4 retirees: %s",
        removed,
    )

    report = {
        "version": "V5",
        "date": now(),
        "anciennes_entrees_pdf_v4_retirees": removed,
        "sources": {},
        "total_oeuvres": 0,
    }

    for source, seeds in SOURCES.items():

        logging.info(
            "Debut collecte: %s",
            source,
        )

        stats = collect_source(
            source,
            seeds,
            database,
        )

        report["sources"][source] = stats

    values = sorted(
        database.values(),
        key=lambda item: (
            item.get("maison") or "",
            item.get("artiste") or "",
            item.get("titre") or "",
        ),
    )

    OUTPUT_FILE.write_text(
        json.dumps(
            values,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    report["total_oeuvres"] = len(values)

    REPORT_FILE.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    logging.info(
        "COLLECTE V5 TERMINEE: %s oeuvres",
        len(values),
    )

    for source, stats in report[
        "sources"
    ].items():

        logging.info(
            "%s: %s pages accessibles, "
            "%s PDF lus, %s nouvelles oeuvres",
            source,
            stats["pages_accessibles"],
            stats["pdf_lus"],
            stats["nouvelles_oeuvres"],
        )


if __name__ == "__main__":
    main()
