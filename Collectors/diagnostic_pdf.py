
"""
ART ENCHERES QUEBEC - DIAGNOSTIC PDF V6

Objectif :
- Lire les catalogues PDF BYDealers accessibles
- Examiner les lignes de texte extraites
- Identifier les numeros de lots potentiels
- Recuperer les positions des blocs de texte
- Produire data/diagnostic-pdf.json

Ne modifie PAS data/oeuvres.json.
"""

import json
import logging
import re
import time

from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import fitz
import requests


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUTPUT = DATA_DIR / "diagnostic-pdf.json"

USER_AGENT = "ArtEncheresQuebec/6.0"
TIMEOUT = 30
DELAY = 2

MAX_PDFS = 3
MAX_PAGES_PER_PDF = 12
MAX_FILE_BYTES = 25 * 1024 * 1024

CATALOGUES = [
    "https://bydealers.com/wp-content/uploads/2026/04/Catalogue_Printemps2026.pdf",
    "https://bydealers.com/wp-content/uploads/2026/05/Catalogue_102p_Printemps_succession2026_Web_simple-1.pdf",
    "https://bydealers.com/wp-content/uploads/2026/02/Catalogue_Fall_2025-1.pdf",
]

session = requests.Session()
session.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/pdf,*/*;q=0.5",
})

logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s: %(message)s",
)


def now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def robots_allowed(url):
    parsed = urlparse(url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    robots_url = origin + "/robots.txt"

    try:
        response = session.get(
            robots_url,
            timeout=TIMEOUT,
            allow_redirects=False,
        )

        if response.status_code in (404, 410):
            logging.info("robots.txt absent: %s", origin)
            return True

        if response.status_code != 200:
            logging.warning(
                "robots.txt non verifiable HTTP %s: %s",
                response.status_code,
                robots_url,
            )
            return False

        parser = RobotFileParser()
        parser.parse(response.text.splitlines())

        return parser.can_fetch(USER_AGENT, url)

    except requests.RequestException as exc:
        logging.warning("Erreur robots.txt: %s", exc)
        return False


def download_pdf(url):
    if not robots_allowed(url):
        return None

    try:
        response = session.get(
            url,
            timeout=TIMEOUT,
            stream=True,
            allow_redirects=False,
        )

        if response.is_redirect:
            logging.warning("Redirection non suivie: %s", url)
            response.close()
            return None

        response.raise_for_status()

        chunks = []
        total = 0

        try:
            for chunk in response.iter_content(65536):
                total += len(chunk)

                if total > MAX_FILE_BYTES:
                    logging.warning("PDF trop volumineux: %s", url)
                    return None

                chunks.append(chunk)
        finally:
            response.close()

        body = b"".join(chunks)

        if not body.startswith(b"%PDF"):
            logging.warning("Fichier non PDF: %s", url)
            return None

        time.sleep(DELAY)
        return body

    except requests.RequestException as exc:
        logging.warning("Erreur telechargement: %s", exc)
        return None


def classify_line(text):
    text = clean(text)

    if re.fullmatch(r"\d{1,4}", text):
        return "numero_isole"

    if re.match(r"^LOT\s*[:#-]?\s*\d+", text, re.I):
        return "entete_lot"

    if re.search(
        r"\d+(?:[.,]\d+)?\s*[x×]\s*"
        r"\d+(?:[.,]\d+)?\s*(?:cm|po|in)?",
        text,
        re.I,
    ):
        return "dimensions"

    if re.search(r"\d[\d\s,.]*\s*\$", text):
        return "montant"

    if re.fullmatch(
        r"[A-ZÀ-ÖØ-Ý][A-ZÀ-ÖØ-Ý'’ .-]{5,80}",
        text,
    ):
        return "majuscules_artiste_possible"

    if re.search(r"\b(?:18|19|20)\d{2}\b", text):
        return "annee_ou_reference"

    return "texte"


def analyze_page(page, page_number):
    lines = []
    blocks = []

    raw = page.get_text("dict")

    for block in raw.get("blocks", []):
        if block.get("type") != 0:
            continue

        block_lines = []

        for line in block.get("lines", []):
            parts = [
                span.get("text", "")
                for span in line.get("spans", [])
            ]

            text = clean(" ".join(parts))

            if not text:
                continue

            bbox = [
                round(v, 1)
                for v in line.get("bbox", [0, 0, 0, 0])
            ]

            entry = {
                "texte": text,
                "type": classify_line(text),
                "position": bbox,
            }

            lines.append(entry)
            block_lines.append(entry)

        if block_lines:
            blocks.append({
                "position": [
                    round(v, 1)
                    for v in block.get("bbox", [0, 0, 0, 0])
                ],
                "lignes": block_lines,
            })

    candidates = []

    for index, line in enumerate(lines):
        if line["type"] not in (
            "numero_isole",
            "entete_lot",
        ):
            continue

        candidates.append({
            "numero_potentiel": line["texte"],
            "position": line["position"],
            "contexte_avant": [
                item["texte"]
                for item in lines[max(0, index - 3):index]
            ],
            "contexte_apres": [
                item["texte"]
                for item in lines[index + 1:index + 9]
            ],
        })

    return {
        "page": page_number,
        "largeur": round(page.rect.width, 1),
        "hauteur": round(page.rect.height, 1),
        "nombre_lignes": len(lines),
        "candidats_lots": candidates[:30],
        "lignes": lines[:180],
        "blocs": blocks[:60],
    }


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    report = {
        "version": "Diagnostic PDF V6",
        "date": now(),
        "catalogues": [],
    }

    for url in CATALOGUES[:MAX_PDFS]:
        logging.info("Analyse du catalogue: %s", url)

        entry = {
            "url": url,
            "statut": "echec",
            "nombre_pages": 0,
            "pages_analysees": [],
        }

        body = download_pdf(url)

        if body is None:
            report["catalogues"].append(entry)
            continue

        try:
            document = fitz.open(
                stream=body,
                filetype="pdf",
            )

            entry["nombre_pages"] = len(document)

            for index in range(
                min(len(document), MAX_PAGES_PER_PDF)
            ):
                page = document[index]

                entry["pages_analysees"].append(
                    analyze_page(page, index + 1)
                )

            entry["statut"] = "ok"
            document.close()

        except Exception as exc:
            entry["erreur"] = str(exc)
            logging.warning("Erreur PDF: %s", exc)

        report["catalogues"].append(entry)

    OUTPUT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logging.info(
        "Diagnostic termine: %s",
        OUTPUT,
    )

    for catalogue in report["catalogues"]:
        logging.info(
            "Catalogue: %s | Statut: %s | Pages: %s",
            catalogue["url"],
            catalogue["statut"],
            len(catalogue["pages_analysees"]),
        )


if __name__ == "__main__":
    main()

