
import json
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
OUTPUT = DATA / "oeuvres.json"
REPORT = DATA / "rapport-collecte.json"

SOURCES = {
    "BYDealers": [
        "https://bydealers.com/fr/decouvrez-les-oeuvres/"
    ],
    "Heffel": [
        "https://www.heffel.com/Auction/"
    ],
    "Gelineau": [
        "https://www.encangelineau.com/"
    ]
}

AGENT = "ArtEncheresQuebec/2.0"
HEADERS = {"User-Agent": AGENT}
TIMEOUT = 20
DELAY = 2

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)

session = requests.Session()
session.headers.update(HEADERS)
robots_cache = {}


def permitted(url):
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    if origin not in robots_cache:
        rp = RobotFileParser()
        rp.set_url(origin + "/robots.txt")

        try:
            rp.read()
            robots_cache[origin] = rp
        except Exception as exc:
            logging.warning(
                "robots.txt inaccessible: %s", exc
            )
            return False

    return robots_cache[origin].can_fetch(
        AGENT, url
    )


def fetch(url):
    if not permitted(url):
        logging.warning("Acces non autorise: %s", url)
        return None

    try:
        response = session.get(
            url, timeout=TIMEOUT
        )
        response.raise_for_status()

        content_type = response.headers.get(
            "Content-Type", ""
        ).lower()

        if "html" not in content_type:
            return None

        time.sleep(DELAY)
        return BeautifulSoup(
            response.text, "html.parser"
        )

    except requests.RequestException as exc:
        logging.warning(
            "Erreur de collecte %s: %s",
            url, exc
        )
        return None


def load_existing():
    if not OUTPUT.exists():
        return []

    try:
        data = json.loads(
            OUTPUT.read_text(encoding="utf-8")
        )

        if isinstance(data, list):
            return data

        if isinstance(data, dict):
            return data.get("oeuvres", [])

    except (OSError, ValueError) as exc:
        logging.error("JSON existant invalide: %s", exc)
        raise

    return []


def make_id(source, url):
    raw = source + "|" + url
    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:20]


def normalize_price(value):
    if value is None:
        return None

    try:
        return float(value)
    except (ValueError, TypeError):
        cleaned = re.sub(
            r"[^\d.,]", "", str(value)
        )

        if not cleaned:
            return None

        if "," in cleaned and "." in cleaned:
            cleaned = cleaned.replace(",", "")
        elif "," in cleaned:
            cleaned = cleaned.replace(",", ".")

        try:
            return float(cleaned)
        except ValueError:
            return None


def structured_objects(data):
    if isinstance(data, list):
        for item in data:
            yield from structured_objects(item)

    elif isinstance(data, dict):
        yield data

        if "@graph" in data:
            yield from structured_objects(
                data["@graph"]
            )


def extract_structured(soup, source, page_url):
    works = []

    scripts = soup.find_all(
        "script",
        type="application/ld+json"
    )

    for script in scripts:
        try:
            payload = json.loads(
                script.string or script.get_text()
            )
        except (ValueError, TypeError):
            continue

        for obj in structured_objects(payload):
            types = obj.get("@type", [])
            if isinstance(types, str):
                types = [types]

            valid_types = {
                "VisualArtwork",
                "Painting",
                "Product"
            }

            if not valid_types.intersection(types):
                continue

            title = obj.get("name")
            if not title:
                continue

            url = urljoin(
                page_url,
                obj.get("url") or page_url
            )

            creator = obj.get("creator") or {}
            if isinstance(creator, dict):
                artist = creator.get("name", "")
            elif isinstance(creator, list):
                artist = ", ".join(
                    c.get("name", "")
                    for c in creator
                    if isinstance(c, dict)
                )
            else:
                artist = str(creator)

            image = obj.get("image")
            if isinstance(image, list):
                image = image[0] if image else None
            if isinstance(image, dict):
                image = image.get("url")

            offers = obj.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}

            price = normalize_price(
                offers.get("price")
            )

            works.append({
                "id": make_id(source, url),
                "maison": source,
                "artiste": artist,
                "titre": title,
                "image": (
                    urljoin(page_url, image)
                    if isinstance(image, str)
                    else None
                ),
                "dimensions": None,
                "prix": price,
                "prix_marteau": None,
                "prix_vendu": None,
                "type_prix": (
                    "prix_affiche"
                    if price is not None
                    else "inconnu"
                ),
                "devise": offers.get(
                    "priceCurrency", "CAD"
                ),
                "statut": "a_verifier",
                "url": url,
                "source_page": page_url,
                "date_collecte": datetime.now(
                    timezone.utc
                ).isoformat()
            })

    return works


def collect():
    existing = load_existing()

    database = {
        work["id"]: work
        for work in existing
        if isinstance(work, dict)
        and work.get("id")
    }

    report = {
        "date": datetime.now(
            timezone.utc
        ).isoformat(),
        "sources": {},
        "nouvelles_oeuvres": 0
    }

    for source, urls in SOURCES.items():
        found = 0
        accessible = 0

        for url in urls:
            logging.info(
                "Consultation: %s - %s",
                source, url
            )

            soup = fetch(url)

            if soup is None:
                continue

            accessible += 1

            works = extract_structured(
                soup, source, url
            )

            for work in works:
                key = work["id"]

                if key not in database:
                    database[key] = work
                    report["nouvelles_oeuvres"] += 1
                    found += 1
                else:
                    old = database[key]
                    for field, value in work.items():
                        if value is not None and (
                            field not in old
                            or old[field] in (None, "")
                        ):
                            old[field] = value

        report["sources"][source] = {
            "pages_accessibles": accessible,
            "nouvelles_oeuvres": found
        }

    DATA.mkdir(parents=True, exist_ok=True)

    OUTPUT.write_text(
        json.dumps(
            list(database.values()),
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    report["total_oeuvres"] = len(database)

    REPORT.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    logging.info(
        "Termine: %s oeuvres, %s nouvelles",
        len(database),
        report["nouvelles_oeuvres"]
    )


if __name__ == "__main__":
    collect()

