
import hashlib
import json
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

AGENT = "ArtEncheresQuebec/3.0"
TIMEOUT = 25
DELAY = 2
MAX_PAGES = 30

SOURCES = {
    "BYDealers": [
        "https://bydealers.com/fr/"
    ],
    "Heffel": [
        "https://www.heffel.com/",
        "https://www.heffel.com/Auction/"
    ],
    "Gelineau": [
        "https://www.encangelineau.com/"
    ]
}

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s"
)

session = requests.Session()
session.headers.update({
    "User-Agent": AGENT,
    "Accept": "text/html,application/xhtml+xml"
})

robots_cache = {}


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    return re.sub(
        r"\s+", " ", str(value or "")
    ).strip()


def make_id(source, url, lot=""):
    raw = f"{source}|{url}|{lot}"
    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:20]


def allowed(url):
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        return False

    origin = (
        f"{parsed.scheme}://{parsed.netloc}"
    )

    if origin not in robots_cache:
        rp = RobotFileParser()
        rp.set_url(origin + "/robots.txt")

        try:
            response = session.get(
                origin + "/robots.txt",
                timeout=TIMEOUT
            )

            if response.status_code != 200:
                logging.warning(
                    "robots.txt non verifiable: %s HTTP %s",
                    origin, response.status_code
                )
                return False

            rp.parse(response.text.splitlines())
            robots_cache[origin] = rp

        except requests.RequestException as exc:
            logging.warning(
                "robots.txt inaccessible: %s", exc
            )
            return False

    return robots_cache[origin].can_fetch(
        AGENT, url
    )


def fetch(url):
    if not allowed(url):
        logging.warning(
            "Acces non autorise ou non verifiable: %s",
            url
        )
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
            "Erreur HTTP: %s - %s",
            url, exc
        )
        return None


def price_number(value):
    if value is None:
        return None

    raw = re.sub(
        r"[^\d.,]", "", str(value)
    )

    if not raw:
        return None

    if "," in raw and "." in raw:
        raw = raw.replace(",", "")
    elif "," in raw:
        raw = raw.replace(",", ".")

    try:
        return float(raw)
    except ValueError:
        return None


def load_database():
    if not OUTPUT.exists():
        return {}

    content = json.loads(
        OUTPUT.read_text(encoding="utf-8")
    )

    if isinstance(content, dict):
        content = content.get("oeuvres", [])

    if not isinstance(content, list):
        raise ValueError(
            "Format de oeuvres.json invalide"
        )

    return {
        w["id"]: w
        for w in content
        if isinstance(w, dict) and w.get("id")
    }


def image_from(soup, url):
    tag = soup.find(
        "meta", property="og:image"
    )

    if tag and tag.get("content"):
        return urljoin(
            url, tag["content"]
        )

    return None


def json_objects(value):
    if isinstance(value, list):
        for item in value:
            yield from json_objects(item)

    elif isinstance(value, dict):
        yield value

        for key in ("@graph", "itemListElement"):
            if key in value:
                yield from json_objects(value[key])


def extract_jsonld(soup, source, page_url):
    results = []

    for script in soup.find_all(
        "script", type="application/ld+json"
    ):
        try:
            payload = json.loads(
                script.string or script.get_text()
            )
        except (ValueError, TypeError):
            continue

        for obj in json_objects(payload):
            types = obj.get("@type", [])
            if isinstance(types, str):
                types = [types]

            if not set(types).intersection({
                "VisualArtwork", "Painting", "Product"
            }):
                continue

            title = clean(obj.get("name"))
            if not title:
                continue

            creator = obj.get("creator") or {}
            if isinstance(creator, dict):
                artist = clean(creator.get("name"))
            elif isinstance(creator, str):
                artist = clean(creator)
            else:
                artist = ""

            offers = obj.get("offers") or {}
            if isinstance(offers, list):
                offers = offers[0] if offers else {}
            if not isinstance(offers, dict):
                offers = {}

            url = urljoin(
                page_url,
                obj.get("url") or page_url
            )

            img = obj.get("image")
            if isinstance(img, list):
                img = img[0] if img else None
            if isinstance(img, dict):
                img = img.get("url")

            price = price_number(
                offers.get("price")
            )

            results.append({
                "id": make_id(source, url, title),
                "maison": source,
                "artiste": artist,
                "titre": title,
                "lot": None,
                "dimensions": None,
                "image": (
                    urljoin(page_url, img)
                    if isinstance(img, str)
                    else None
                ),
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
                "date_collecte": now()
            })

    return results


def heffel_results(soup, page_url):
    """
    Lit uniquement les tableaux de resultats
    dont la page confirme les ventes.
    Prix Heffel = prime acheteur incluse.
    """

    page_text = clean(
        soup.get_text(" ", strip=True)
    ).lower()

    confirmed = (
        "prices include buyer's premium"
        in page_text
        and "lots were sold" in page_text
    )

    if not confirmed:
        return []

    results = []

    for table in soup.find_all("table"):
        for row in table.find_all("tr"):
            cells = row.find_all(
                ["td", "th"], recursive=False
            )

            if len(cells) < 3:
                continue

            lot = clean(
                cells[0].get_text(" ", strip=True)
            )
            description = clean(
                cells[1].get_text(" ", strip=True)
            )
            price_text = clean(
                cells[-1].get_text(" ", strip=True)
            )

            if not re.fullmatch(
                r"[A-Za-z]?\d{1,5}", lot
            ):
                continue

            if not re.search(
                r"\$\s*[\d,]+", price_text
            ):
                continue

            price = price_number(price_text)

            if price is None:
                continue

            # Le tableau ne permet pas toujours
            # de separer artiste et titre.
            # Conserver la description originale.
            artist = ""
            title = description

            link = cells[1].find(
                "a", href=True
            )

            lot_url = (
                urljoin(page_url, link["href"])
                if link else page_url
            )

            results.append({
                "id": make_id(
                    "Heffel", page_url, lot
                ),
                "maison": "Heffel",
                "artiste": artist,
                "titre": title,
                "lot": lot,
                "dimensions": None,
                "image": None,
                "prix": price,
                "prix_marteau": None,
                "prix_vendu": price,
                "type_prix": "prix_vendu",
                "prix_inclut_prime": True,
                "devise": "CAD",
                "statut": "vendu",
                "url": lot_url,
                "source_page": page_url,
                "date_collecte": now()
            })

    return results


def discover_links(soup, base_url, source):
    links = []
    base_host = urlparse(base_url).hostname

    for a in soup.find_all("a", href=True):
        url = urljoin(base_url, a["href"])
        parsed = urlparse(url)

        if parsed.hostname != base_host:
            continue

        text = clean(
            a.get_text(" ", strip=True)
        ).lower()

        path = parsed.path.lower()

        if source == "Heffel":
            relevant = (
                "results_e" in path
                or "auction results" in text
                or "auction result" in text
            )

        elif source == "BYDealers":
            relevant = any(
                word in (path + " " + text)
                for word in (
                    "catalogue",
                    "encher",
                    "auction",
                    "oeuvre"
                )
            )

        else:
            relevant = any(
                word in (path + " " + text)
                for word in (
                    "encan",
                    "auction",
                    "catalogue",
                    "tableau"
                )
            )

        if relevant and url not in links:
            links.append(url)

    return links[:MAX_PAGES]


def merge_work(database, work):
    key = work["id"]

    if key not in database:
        database[key] = work
        return True

    old = database[key]

    for field, value in work.items():
        if field == "date_collecte":
            continue

        if value is not None and value != "":
            if old.get(field) in (None, ""):
                old[field] = value

    return False


def collect_source(source, urls, database):
    queue = list(urls)
    visited = set()

    stats = {
        "pages_consultees": 0,
        "pages_accessibles": 0,
        "oeuvres_detectees": 0,
        "nouvelles_oeuvres": 0,
        "erreurs_ou_blocages": 0
    }

    while queue and len(visited) < MAX_PAGES:
        url = queue.pop(0)

        if url in visited:
            continue

        visited.add(url)
        stats["pages_consultees"] += 1

        logging.info(
            "%s: %s", source, url
        )

        soup = fetch(url)

        if soup is None:
            stats["erreurs_ou_blocages"] += 1
            continue

        stats["pages_accessibles"] += 1

        works = extract_jsonld(
            soup, source, url
        )

        if source == "Heffel":
            works.extend(
                heffel_results(soup, url)
            )

        stats["oeuvres_detectees"] += len(works)

        for work in works:
            if merge_work(database, work):
                stats["nouvelles_oeuvres"] += 1

        for link in discover_links(
            soup, url, source
        ):
            if (
                link not in visited
                and link not in queue
            ):
                queue.append(link)

    return stats


def main():
    DATA.mkdir(
        parents=True, exist_ok=True
    )

    database = load_database()

    report = {
        "version": "V3",
        "date": now(),
        "sources": {},
        "total_oeuvres": 0
    }

    for source, urls in SOURCES.items():
        logging.info(
            "Debut collecte: %s", source
        )

        stats = collect_source(
            source, urls, database
        )

        report["sources"][source] = stats

        logging.info(
            "%s: %s oeuvres detectees",
            source,
            stats["oeuvres_detectees"]
        )

    works = sorted(
        database.values(),
        key=lambda w: (
            w.get("maison", ""),
            w.get("titre", "")
        )
    )

    OUTPUT.write_text(
        json.dumps(
            works,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    report["total_oeuvres"] = len(works)

    REPORT.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            indent=2
        ),
        encoding="utf-8"
    )

    logging.info(
        "COLLECTE TERMINEE: %s oeuvres",
        len(works)
    )

    for source, stats in report["sources"].items():
        logging.info(
            "%s: %s pages accessibles, "
            "%s nouvelles oeuvres",
            source,
            stats["pages_accessibles"],
            stats["nouvelles_oeuvres"]
        )


if __name__ == "__main__":
    main()
