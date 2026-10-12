
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ART ENCHERES QUEBEC - COLLECTEUR V8.5

- 8 catalogues BYDealers
- Preservation des notices historiques
- Extraction prudente des estimations
- Rejet des fourchettes d'annees
- Verification des limites de chaque lot
- Reconstruction des titres multilignes
- Corrections de titres seulement si fiables
- Aucun prix marteau invente
- Rapports de collecte, qualite et differences
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


VERSION = "V8.5"

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

DB = DATA / "oeuvres.json"
REPORT = DATA / "rapport-collecte.json"
QUALITY = DATA / "rapport-qualite.json"
DIFF = DATA / "rapport-differences.json"

USER_AGENT = "ArtEncheresQuebec/8.5"
TIMEOUT = 45
MAX_BYTES = 40 * 1024 * 1024
PAUSE = 2

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
SESSION.headers.update({"User-Agent": USER_AGENT})
ROBOTS = {}

BIO_RE = re.compile(
    r"^\s*([12]\s*\d\s*\d\s*\d)"
    r"\s*[-–—]\s*([12]\s*\d\s*\d\s*\d)?\s*$"
)

LOT_RE = re.compile(r"^\s*(\d{1,3})\s+(.{2,250})$")

DIM_RE = re.compile(
    r"\d{1,4}(?:[.,]\d+)?\s*[x×]\s*"
    r"\d{1,4}(?:[.,]\d+)?"
    r"(?:\s*[x×]\s*\d{1,4}(?:[.,]\d+)?)?\s*cm\b",
    re.I
)

RANGE_RE = re.compile(
    r"(?<![\d])"
    r"(\d[\d\s\u00a0,.]*)"
    r"\s*[-–—]\s*"
    r"(\d[\d\s\u00a0,.]*)"
    r"\s*(?:\$|CAD)?",
    re.I
)

EST_LABEL = re.compile(
    r"\b(estimation|estimate|estimated|"
    r"évaluation|evaluation)\b",
    re.I
)

EST_AMOUNT = re.compile(
    r"(?<!\d)"
    r"(\d{1,3}(?:[ ,.\u00a0]\d{3})+|\d{4,8})"
    r"\s*[-–—]\s*"
    r"(\d{1,3}(?:[ ,.\u00a0]\d{3})+|\d{4,8})"
    r"\s*(?:\$|CAD)?",
    re.I
)

STOP_TITLE = re.compile(
    r"^(huile|oil|acrylique|acrylic|"
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

TITLE_ENDINGS = ("/", ":", "-", "–", "—", ",")


# ==========================================================
# UTILITAIRES
# ==========================================================

def now():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    value = unicodedata.normalize(
        "NFKC", str(value or "")
    )
    return re.sub(r"\s+", " ", value).strip()


def norm(value):
    value = unicodedata.normalize(
        "NFKD", clean(value)
    )
    return "".join(
        c for c in value
        if not unicodedata.combining(c)
    ).casefold()


def hash_id(*parts):
    raw = "|".join(norm(x) for x in parts)
    return hashlib.sha256(
        raw.encode("utf-8")
    ).hexdigest()[:20]


def lot_num(value):
    try:
        return f"{int(str(value).strip()):02d}"
    except (ValueError, TypeError):
        return clean(value)


def passage_id(item):
    if item.get("url") and item.get("lot"):
        return hash_id(
            item.get("maison", "BYDealers"),
            item["url"],
            lot_num(item["lot"])
        )
    return clean(
        item.get("id_passage") or item.get("id")
    )


def read_db():
    if not DB.exists():
        return []

    obj = json.loads(DB.read_text("utf-8"))
    if isinstance(obj, dict):
        obj = obj.get("oeuvres", [])

    if not isinstance(obj, list):
        raise ValueError("Format de base invalide")

    return obj


def save(path, obj):
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps(
            obj, ensure_ascii=False, indent=2
        ) + "\n",
        encoding="utf-8"
    )
    temp.replace(path)


# ==========================================================
# ROBOTS ET TELECHARGEMENT
# ==========================================================

def authorized(url):
    parsed = urlparse(url)

    if (
        parsed.scheme != "https"
        or parsed.netloc != "bydealers.com"
    ):
        return False

    origin = "https://bydealers.com"

    if origin not in ROBOTS:
        r = SESSION.get(
            origin + "/robots.txt",
            timeout=TIMEOUT,
            allow_redirects=False
        )

        if r.status_code == 200:
            robot = RobotFileParser()
            robot.parse(r.text.splitlines())
            ROBOTS[origin] = robot
        elif r.status_code in (404, 410):
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
            "Acces refuse par robots.txt"
        )

    with SESSION.get(
        url,
        timeout=TIMEOUT,
        stream=True,
        allow_redirects=False
    ) as response:
        if response.is_redirect:
            raise RuntimeError("Redirection non autorisee")

        response.raise_for_status()

        data = bytearray()

        for chunk in response.iter_content(65536):
            if chunk:
                data.extend(chunk)
            if len(data) > MAX_BYTES:
                raise ValueError("PDF trop volumineux")

    if not bytes(data).startswith(b"%PDF"):
        raise ValueError("Reponse non PDF")

    time.sleep(PAUSE)
    return bytes(data)


# ==========================================================
# LECTURE PDF
# ==========================================================

def page_lines(page):
    result = []

    for block in page.get_text(
        "dict", sort=False
    ).get("blocks", []):

        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):
            txt = clean(" ".join(
                span.get("text", "")
                for span in line.get("spans", [])
            ))

            bbox = line.get("bbox")

            if txt and bbox:
                result.append({
                    "text": txt,
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
    left, right = [], []

    for line in lines:
        center = (line["x"] + line["x2"]) / 2
        if center < midpoint:
            left.append(line)
        else:
            right.append(line)

    return [left, right]


# ==========================================================
# ARTISTES ET LOTS
# ==========================================================

def valid_bio(text):
    match = BIO_RE.fullmatch(clean(text))
    if not match:
        return False

    born = int(re.sub(r"\D", "", match[1]))
    died = (
        int(re.sub(r"\D", "", match[2]))
        if match[2] else None
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

    if not (5 <= len(text) <= 85):
        return False
    if not (2 <= len(words) <= 8):
        return False
    if re.search(r"\d|https?://|[!?;]", text):
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

        if norm(base) in blocked:
            return False

        if not re.match(
            r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’().-]*$",
            word
        ):
            return False

        if i and norm(base) in particles:
            continue

        if not base or not base[0].isupper():
            return False

    return True


def lot_header(text):
    match = LOT_RE.fullmatch(clean(text))
    if not match:
        return None

    n = int(match[1])
    title = clean(match[2])

    if (
        1 <= n <= 500
        and re.search(r"[A-Za-zÀ-ÿ]", title)
    ):
        return n, title

    return None


def detect_lots(lines, historical_artists):
    found = []

    for i, line in enumerate(lines):
        header = lot_header(line["text"])
        if not header:
            continue

        number, title = header
        artist = None

        for b in range(max(0, i - 3), i):
            if not valid_bio(lines[b]["text"]):
                continue

            for a in range(max(0, b - 2), b):
                candidate = clean(lines[a]["text"])

                if valid_artist(candidate):
                    artist = candidate
                    break

            if artist:
                break

        if not artist:
            previous = historical_artists.get(
                lot_num(number)
            )

            has_bio = any(
                valid_bio(lines[j]["text"])
                for j in range(max(0, i - 3), i)
            )

            if previous and has_bio:
                artist = previous

        if artist:
            found.append({
                "index": i,
                "lot": number,
                "titre": title,
                "artiste": artist
            })

    return found


# ==========================================================
# TITRES MULTILIGNES
# ==========================================================

def reconstruct_title(lines, entry, end):
    original = clean(entry["titre"])

    if not original.endswith(TITLE_ENDINGS):
        return original

    i = entry["index"]

    if i + 1 >= end:
        return original

    first = lines[i]
    second = lines[i + 1]
    continuation = clean(second["text"])

    if not continuation or len(continuation) > 120:
        return original

    if (
        lot_header(continuation)
        or valid_bio(continuation)
        or DIM_RE.search(continuation)
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

    title = clean(
        original + " " + continuation
    )

    return title if len(title) <= 250 else original


def safe_title_extension(old, new):
    old = clean(old)
    new = clean(new)

    if not old.endswith(TITLE_ENDINGS):
        return False

    if len(new) <= len(old):
        return False

    if not norm(new).startswith(norm(old)):
        return False

    extension = new[len(old):].strip()

    if not extension or len(extension) > 120:
        return False

    if (
        STOP_TITLE.search(extension)
        or DIM_RE.search(extension)
        or EST_LABEL.search(extension)
    ):
        return False

    return True


# ==========================================================
# ESTIMATIONS V8.5
# ==========================================================

def amount(value):
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else None


def valid_price_range(text):
    """
    Rejette notamment :
    - 1968-1969
    - 1952-1953
    - dimensions
    - numeros de lots
    - intervalles trop petits

    Une estimation sans contexte explicite doit
    comporter une indication monetaire.
    """

    text = clean(text)

    if DIM_RE.search(text):
        return None

    matches = list(EST_AMOUNT.finditer(text))

    candidates = []

    for match in matches:
        low = amount(match[1])
        high = amount(match[2])

        if low is None or high is None:
            continue

        if (
            1800 <= low <= 2100
            and 1800 <= high <= 2100
        ):
            continue

        if not (
            1000 <= low <= high <= 100_000_000
        ):
            continue

        if high > low * 20:
            continue

        candidates.append((low, high))

    unique = list(dict.fromkeys(candidates))

    return unique[0] if len(unique) == 1 else None


def extract_estimate(section):
    """
    Priorite :
    1. Fourchette sur ligne avec libelle estimation.
    2. Fourchette dans les deux lignes suivantes.

    Ne cherche jamais dans le lot suivant.
    Ne prend jamais une fourchette d'annees.
    """

    candidates = []

    for i, line in enumerate(section):
        if not EST_LABEL.search(line["text"]):
            continue

        # Ligne portant le libelle.
        result = valid_price_range(line["text"])

        if result:
            candidates.append(result)
            continue

        # Deux lignes suivantes seulement.
        for j in range(i + 1, min(i + 3, len(section))):
            txt = section[j]["text"]

            if (
                lot_header(txt)
                or valid_bio(txt)
                or DIM_RE.search(txt)
                or STOP_TITLE.search(txt)
            ):
                break

            result = valid_price_range(txt)

            if result:
                candidates.append(result)
                break

    unique = list(dict.fromkeys(candidates))

    if len(unique) == 1:
        return unique[0], False

    if len(unique) > 1:
        return None, True

    return None, False


def extract_metadata(lines, entries, index):
    entry = entries[index]

    start = entry["index"] + 1

    end = (
        entries[index + 1]["index"]
        if index + 1 < len(entries)
        else len(lines)
    )

    section = lines[start:min(end, start + 140)]

    dims = None

    for line in section:
        match = DIM_RE.search(line["text"])
        if match:
            dims = clean(match.group())
            break

    est, conflict = extract_estimate(section)
    title = reconstruct_title(lines, entry, end)

    return dims, est, conflict, title


# ==========================================================
# NOTICES
# ==========================================================

def make_notice(
    url, sale, page, entry,
    dims, est, conflict, title
):
    lot = lot_num(entry["lot"])
    pid = hash_id("BYDealers", url, lot)

    year = re.search(r"20\d{2}", sale)

    low, high = (
        est if est else (None, None)
    )

    return {
        "id": pid,
        "id_passage": pid,
        "id_oeuvre": hash_id("oeuvre", pid),
        "maison": "BYDealers",
        "artiste": entry["artiste"],
        "titre": title,
        "lot": lot,
        "dimensions": dims,
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
            if est else None
        ),
        "type_prix": (
            "estimation" if est else "inconnu"
        ),
        "devise": "CAD",
        "statut": "catalogue_historique",
        "url": url,
        "source_page": url,
        "source_page_pdf": page,
        "origine": "catalogue_pdf_v8_5",
        "confiance_extraction": (
            90 if dims and est and not conflict
            else 65
        ),
        "conflit_estimation": conflict,
        "prix_realise_verifie": False,
        "date_collecte": now()
    }


def parse_pdf(data, url, sale, known):
    found = {}
    errors = []

    with fitz.open(
        stream=data, filetype="pdf"
    ) as doc:

        pages = len(doc)

        for page_index in range(pages):
            try:
                page = doc[page_index]

                for lines in split_columns(page):
                    entries = detect_lots(lines, known)

                    for i, entry in enumerate(entries):
                        dims, est, conflict, title = (
                            extract_metadata(
                                lines, entries, i
                            )
                        )

                        item = make_notice(
                            url, sale, page_index + 1,
                            entry, dims, est,
                            conflict, title
                        )

                        key = passage_id(item)

                        if key not in found:
                            found[key] = item
                        else:
                            previous = found[key]

                            old_score = sum([
                                bool(previous["dimensions"]),
                                previous["estimation_min"]
                                is not None,
                                not previous["conflit_estimation"]
                            ])

                            new_score = sum([
                                bool(item["dimensions"]),
                                item["estimation_min"]
                                is not None,
                                not item["conflit_estimation"]
                            ])

                            if new_score > old_score:
                                found[key] = item

            except Exception as exc:
                errors.append({
                    "page": page_index + 1,
                    "erreur": str(exc)
                })
                traceback.print_exc()

    return list(found.values()), pages, errors


# ==========================================================
# FUSION HISTORIQUE
# ==========================================================

def merge(old, new):
    result = dict(old)

    added = []
    conflicts = []
    proposals = []
    corrected = []

    for field in ("artiste", "titre", "dimensions"):
        previous = old.get(field)
        incoming = new.get(field)

        if incoming and not previous:
            result[field] = incoming
            added.append(field)
            continue

        if not previous or not incoming:
            continue

        if norm(previous) == norm(incoming):
            continue

        if field == "artiste":
            if not valid_artist(incoming):
                continue

        if field == "titre":
            if safe_title_extension(previous, incoming):
                # On applique seulement les extensions
                # repondant aux regles conservatrices.
                result[field] = incoming

                corrected.append({
                    "champ": "titre",
                    "ancien": previous,
                    "nouveau": incoming,
                    "motif": "extension_multiligne"
                })
                continue

            if norm(previous).startswith(norm(incoming)):
                continue

        conflicts.append({
            "champ": field,
            "ancien": previous,
            "extrait": incoming
        })

    for field in (
        "vente", "annee_vente",
        "source_page_pdf"
    ):
        if (
            result.get(field) is None
            and new.get(field) is not None
        ):
            result[field] = new[field]
            added.append(field)

    old_est = (
        old.get("estimation_min"),
        old.get("estimation_max")
    )

    new_est = (
        new.get("estimation_min"),
        new.get("estimation_max")
    )

    if all(v is not None for v in new_est):
        if all(v is None for v in old_est):
            for field in (
                "estimation_min",
                "estimation_max",
                "estimation"
            ):
                result[field] = new[field]

            result["type_prix"] = "estimation"
            added.append("estimation")

        elif old_est != new_est:
            conflicts.append({
                "champ": "estimation",
                "ancien": old_est,
                "extrait": new_est
            })

    if new.get("conflit_estimation"):
        conflicts.append({
            "champ": "estimation",
            "motif": "estimations_multiples"
        })

    # Ne jamais modifier l'identite historique.
    result["id_passage"] = passage_id(old)

    # Les autres champs historiques, notamment
    # les prix marteau, restent intacts.
    return result, added, conflicts, proposals, corrected


# ==========================================================
# RAPPROCHEMENTS
# ==========================================================

def dimension_key(value):
    match = DIM_RE.search(clean(value))
    if not match:
        return None

    nums = re.findall(
        r"\d+(?:[.,]\d+)?", match.group()
    )

    return tuple(sorted(
        round(float(x.replace(",", ".")), 1)
        for x in nums
    ))


def find_matches(records):
    groups = defaultdict(list)

    for item in records:
        dims = dimension_key(
            item.get("dimensions")
        )

        if (
            dims
            and item.get("artiste")
            and item.get("titre")
        ):
            key = (
                norm(item["artiste"]),
                norm(item["titre"]),
                dims
            )
            groups[key].append(item)

    matches = []

    for group in groups.values():
        if len({
            passage_id(x) for x in group
        }) < 2:
            continue

        same_sale = (
            len({x.get("url") for x in group}) == 1
        )

        matches.append({
            "artiste": group[0]["artiste"],
            "titre": group[0]["titre"],
            "dimensions": group[0]["dimensions"],
            "verification": "requise",
            "risque": (
                "lots_distincts_meme_catalogue"
                if same_sale
                else "possible_remise_en_vente"
            ),
            "passages": [
                {
                    "id_passage": passage_id(x),
                    "vente": x.get("vente"),
                    "lot": x.get("lot"),
                    "url": x.get("url")
                }
                for x in group
            ]
        })

    return matches


# ==========================================================
# QUALITE
# ==========================================================

def build_quality(records, matches):
    anomalies = []

    for item in records:
        issues = []

        if not item.get("dimensions"):
            issues.append("dimensions_absentes")

        if item.get("estimation_min") is None:
            issues.append("estimation_absente")

        if clean(item.get("titre")).endswith(
            TITLE_ENDINGS
        ):
            issues.append(
                "titre_possiblement_tronque"
            )

        if item.get("conflit_estimation"):
            issues.append("conflit_estimation")

        if issues:
            anomalies.append({
                "id": item.get("id"),
                "id_passage": passage_id(item),
                "artiste": item.get("artiste"),
                "titre": item.get("titre"),
                "problemes": issues,
                "url": item.get("url"),
                "page": item.get("source_page_pdf")
            })

    return {
        "version": VERSION,
        "date": now(),
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


# ==========================================================
# PROGRAMME PRINCIPAL
# ==========================================================

def main():
    original = read_db()

    print(
        f"{VERSION} - {len(original)} "
        "notices historiques",
        flush=True
    )

    database = {}

    for item in original:
        key = passage_id(item)

        if not key or key in database:
            raise ValueError(
                f"Identifiant historique invalide : {key}"
            )

        database[key] = dict(item)

    historical = set(database)

    detected = set()
    new_passages = []
    additions = []
    conflicts = []
    proposals = []
    corrections = []
    catalogues = []

    stats = {
        "pages_consultees": 0,
        "pages_accessibles": 0,
        "pdf_lus": 0,
        "oeuvres_detectees": 0,
        "nouvelles_oeuvres": 0,
        "erreurs_ou_blocages": 0
    }

    for sale, url in CATALOGUES.items():
        print(f"\nCATALOGUE : {sale}", flush=True)

        stats["pages_consultees"] += 1

        try:
            data = download_pdf(url)
            stats["pages_accessibles"] += 1

            known = {
                lot_num(x.get("lot")): x.get("artiste")
                for x in database.values()
                if (
                    x.get("url") == url
                    and x.get("lot")
                    and x.get("artiste")
                )
            }

            found, pages, errors = parse_pdf(
                data, url, sale, known
            )

            stats["pdf_lus"] += 1
            stats["oeuvres_detectees"] += len(found)
            stats["erreurs_ou_blocages"] += len(errors)

            catalogues.append({
                "vente": sale,
                "statut": (
                    "ok" if found and not errors
                    else "a_verifier"
                ),
                "oeuvres": len(found),
                "pages": pages,
                "erreurs_pages": errors
            })

            print(
                f"Lots={len(found)} "
                f"Pages={pages} "
                f"Erreurs={len(errors)}",
                flush=True
            )

            for item in found:
                key = passage_id(item)
                detected.add(key)

                if key in database:
                    (
                        merged, added, problems,
                        suggested, corrected
                    ) = merge(database[key], item)

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

                    if suggested:
                        proposals.append({
                            "id_passage": key,
                            "propositions": suggested
                        })

                    if corrected:
                        corrections.append({
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

    if not detected:
        raise RuntimeError(
            "Aucune collecte reussie. "
            "Sauvegarde annulee."
        )

    records = sorted(
        database.values(),
        key=lambda x: (
            norm(x.get("artiste")),
            norm(x.get("titre")),
            str(x.get("annee_vente") or ""),
            lot_num(x.get("lot"))
        )
    )

    if len(records) < len(original):
        raise RuntimeError(
            "Regression de la base historique"
        )

    matches = find_matches(records)
    quality = build_quality(records, matches)

    missing = sorted(historical - detected)

    differences = {
        "version": VERSION,
        "date": now(),
        "historique_avant": len(original),
        "detectes_cette_execution": len(detected),
        "non_redetectes": len(missing),
        "lots_non_redetectes": [
            {
                "id_passage": key,
                "vente": database[key].get("vente"),
                "lot": database[key].get("lot"),
                "titre": database[key].get("titre")
            }
            for key in missing
        ],
        "nouveaux_passages": new_passages,
        "champs_completes": additions,
        "conflits_a_verifier": conflicts,
        "propositions_corrections": proposals,
        "corrections_appliquees": corrections
    }

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
        "catalogues": catalogues,
        "total_oeuvres": len(records),
        "avec_estimation": quality["avec_estimation"],
        "rapprochements_potentiels": len(matches),
        "lots_historiques_non_redetectes": len(missing),
        "conflits_a_verifier": len(conflicts),
        "corrections_titres": len(corrections),
        "note": (
            "Aucun prix marteau invente. "
            "Les montants de catalogue sont "
            "des estimations uniquement."
        )
    }

    # Sauvegarde seulement apres les controles
    # de base. Le workflow effectue ensuite
    # les controles stricts avant publication.

    save(DB, records)
    save(REPORT, report)
    save(QUALITY, quality)
    save(DIFF, differences)

    print("\n==============================")
    print(f"ART ENCHERES QUEBEC {VERSION}")
    print("==============================")
    print("Notices :", len(records))
    print("Lots detectes :", len(detected))
    print("Estimations :", quality["avec_estimation"])
    print("Dimensions :", quality["avec_dimensions"])
    print("Conflits :", len(conflicts))
    print("Titres corriges :", len(corrections))
    print("Non redetectes :", len(missing))
    print("Rapprochements :", len(matches))
    print("==============================")


if __name__ == "__main__":
    main()
