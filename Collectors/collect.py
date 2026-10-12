
#!/usr/bin/env python3
"""Art Encheres Quebec V8.2 — collecte prudente des catalogues BYDealers."""

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

VERSION = "V8.2"
ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)

DB = DATA / "oeuvres.json"
REPORT = DATA / "rapport-collecte.json"
QUALITY = DATA / "rapport-qualite.json"
DIFF = DATA / "rapport-differences.json"

USER_AGENT = "ArtEncheresQuebec/8.2 (catalogue public; contact: GitHub repository)"
TIMEOUT = 40
MAX_BYTES = 40 * 1024 * 1024
PAUSE_SECONDS = 2

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
SESSION.headers.update({
    "User-Agent": USER_AGENT,
    "Accept": "application/pdf,*/*",
})

ROBOTS = {}

BIO = re.compile(
    r"^\s*([12]\s*\d\s*\d\s*\d)\s*[-–—]\s*"
    r"([12]\s*\d\s*\d\s*\d)?\s*$"
)

LOT = re.compile(r"^\s*(\d{1,3})\s+(.{2,250})$")

DIM = re.compile(
    r"\d{1,4}(?:[.,]\d+)?\s*[x×]\s*"
    r"\d{1,4}(?:[.,]\d+)?"
    r"(?:\s*[x×]\s*\d{1,4}(?:[.,]\d+)?)?"
    r"\s*cm\b",
    re.I,
)

EST = re.compile(
    r"(?<!\d)(\d[\d\s\u00a0,.]*)\s*[-–—]\s*"
    r"(\d[\d\s\u00a0,.]*)\s*(?:\$|CAD)?",
    re.I,
)


# ============================================================
# UTILITAIRES
# ============================================================

def timestamp():
    return datetime.now(timezone.utc).isoformat()


def clean(value):
    s = unicodedata.normalize("NFKC", str(value or ""))
    s = "".join(
        " " if unicodedata.category(c).startswith("C") else c
        for c in s
    )
    return re.sub(r"\s+", " ", s).strip()


def keytext(value):
    s = unicodedata.normalize("NFKD", clean(value))
    return "".join(
        c for c in s if not unicodedata.combining(c)
    ).casefold()


def digest(*parts):
    raw = "|".join(keytext(p) for p in parts)
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


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
            lot_number(item["lot"]),
        )
    return clean(item.get("id_passage") or item.get("id"))


def load_db():
    if not DB.exists():
        return []

    obj = json.loads(DB.read_text(encoding="utf-8"))

    if isinstance(obj, dict):
        obj = obj.get("oeuvres", [])

    if not isinstance(obj, list) or not all(
        isinstance(x, dict) for x in obj
    ):
        raise ValueError(
            "data/oeuvres.json doit contenir une liste de notices."
        )

    return obj


def write_json(path, obj):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(obj, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(path)


# ============================================================
# ACCES AUX CATALOGUES
# ============================================================

def allowed(url):
    parsed = urlparse(url)

    if parsed.scheme != "https" or parsed.netloc != "bydealers.com":
        return False

    origin = f"{parsed.scheme}://{parsed.netloc}"

    if origin not in ROBOTS:
        response = SESSION.get(
            origin + "/robots.txt",
            timeout=TIMEOUT,
            allow_redirects=False,
        )

        if response.status_code in (401, 403):
            return False

        if response.status_code == 200:
            parser = RobotFileParser()
            parser.parse(response.text.splitlines())
            ROBOTS[origin] = parser

        elif response.status_code in (404, 410):
            ROBOTS[origin] = True

        else:
            return False

    rule = ROBOTS[origin]

    if isinstance(rule, bool):
        return rule

    return rule.can_fetch(USER_AGENT, url)


def fetch_pdf(url):
    if not allowed(url):
        raise PermissionError(
            "Acces non autorise par robots.txt "
            "ou verification indisponible"
        )

    with SESSION.get(
        url,
        timeout=TIMEOUT,
        stream=True,
        allow_redirects=False,
    ) as response:

        response.raise_for_status()

        if response.is_redirect:
            raise RuntimeError(
                "Redirection PDF non suivie par precaution"
            )

        data = bytearray()

        for chunk in response.iter_content(chunk_size=65536):
            data.extend(chunk)

            if len(data) > MAX_BYTES:
                raise ValueError("PDF trop volumineux")

    if not bytes(data).startswith(b"%PDF"):
        raise ValueError("Reponse non PDF")

    time.sleep(PAUSE_SECONDS)

    return bytes(data)


# ============================================================
# EXTRACTION PDF
# ============================================================

def page_lines(page):
    result = []

    blocks = page.get_text(
        "dict",
        sort=False,
    ).get("blocks", [])

    for block in blocks:
        if block.get("type") != 0:
            continue

        for line in block.get("lines", []):
            txt = clean(
                " ".join(
                    s.get("text", "")
                    for s in line.get("spans", [])
                )
            )

            if txt and line.get("bbox"):
                x0, y0, x1, y1 = line["bbox"]

                result.append({
                    "text": txt,
                    "x": x0,
                    "y": y0,
                    "x2": x1,
                    "y2": y1,
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
        center = (line["x"] + line["x2"]) / 2

        if center < middle:
            left.append(line)
        else:
            right.append(line)

    return [left, right]


def artist_name(text):
    return clean(
        re.sub(
            r"\s*\(\s*suite\s*\)\s*$",
            "",
            text,
            flags=re.I,
        )
    )


def is_bio(text):
    match = BIO.fullmatch(clean(text))

    if not match:
        return False

    born = int(re.sub(r"\D", "", match.group(1)))

    died = (
        int(re.sub(r"\D", "", match.group(2)))
        if match.group(2)
        else None
    )

    return (
        1700 <= born <= 2026
        and (died is None or born <= died <= 2026)
    )


def is_artist(text):
    text = artist_name(text)

    return (
        5 <= len(text) <= 110
        and len(text.split()) >= 2
        and not re.search(r"\d|@|https?://", text)
    )


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


def detect_entries(lines):
    entries = []

    for i, line in enumerate(lines):
        header = lot_header(line["text"])

        if not header:
            continue

        artist = None

        # Tolere des lignes supplementaires entre
        # le nom de l'artiste, les dates et le lot.
        for bio_i in range(max(0, i - 3), i):
            if not is_bio(lines[bio_i]["text"]):
                continue

            for artist_i in range(
                max(0, bio_i - 2),
                bio_i,
            ):
                if is_artist(lines[artist_i]["text"]):
                    artist = artist_name(
                        lines[artist_i]["text"]
                    )
                    break

            if artist:
                break

        if artist:
            entries.append({
                "index": i,
                "lot": header[0],
                "titre": header[1],
                "artiste": artist,
            })

    return entries


# ============================================================
# DIMENSIONS ET ESTIMATIONS
# ============================================================

def money(value):
    digits = re.sub(r"\D", "", value)
    return int(digits) if digits else None


def estimate(text):
    match = EST.search(clean(text))

    if not match:
        return None

    lo = money(match.group(1))
    hi = money(match.group(2))

    if lo is None or hi is None:
        return None

    if not (100 <= lo <= hi <= 100_000_000):
        return None

    if hi > lo * 20:
        return None

    return lo, hi


def metadata(lines, entries, index):
    start = entries[index]["index"] + 1

    end = (
        entries[index + 1]["index"]
        if index + 1 < len(entries)
        else len(lines)
    )

    section = lines[start:min(end, start + 140)]

    dims = None
    estimates = []

    for i, item in enumerate(section):
        text = item["text"]

        if dims is None:
            match = DIM.search(text)

            if match:
                dims = clean(match.group())

        if (
            "estimation" in keytext(text)
            or "estimate" in keytext(text)
        ):
            for candidate in section[i:i + 5]:
                found = estimate(candidate["text"])

                if found:
                    estimates.append(found)
                    break

    unique = list(dict.fromkeys(estimates))

    return (
        dims,
        unique[0] if len(unique) == 1 else None,
        len(unique) > 1,
    )


# ============================================================
# CREATION DES NOTICES
# ============================================================

def notice(url, sale, page_no, entry, dims, est, conflict):
    lot = lot_number(entry["lot"])
    passage = digest("BYDealers", url, lot)

    year = re.search(r"20\d{2}", sale)
    lo, hi = est if est else (None, None)

    return {
        "id": passage,
        "id_passage": passage,
        "id_oeuvre": digest("oeuvre-provisoire", passage),
        "maison": "BYDealers",
        "artiste": entry["artiste"],
        "titre": clean(entry["titre"]),
        "lot": lot,
        "dimensions": dims,
        "vente": sale,
        "annee_vente": int(year.group()) if year else None,
        "image": None,
        "prix": None,
        "prix_marteau": None,
        "prix_vendu": None,
        "estimation_min": lo,
        "estimation_max": hi,
        "estimation": (
            f"{lo:,} - {hi:,} CAD"
            if est
            else None
        ),
        "type_prix": (
            "estimation" if est else "inconnu"
        ),
        "devise": "CAD",
        "statut": "catalogue_historique",
        "url": url,
        "source_page": url,
        "source_page_pdf": page_no,
        "origine": "catalogue_pdf_v8_2",
        "confiance_extraction": (
            85 if dims and est and not conflict else 65
        ),
        "conflit_estimation": conflict,
        "prix_realise_verifie": False,
        "date_collecte": timestamp(),
    }


def parse_pdf(data, url, sale):
    detected = {}
    page_errors = []

    with fitz.open(
        stream=data,
        filetype="pdf",
    ) as doc:

        total_pages = len(doc)

        for page_index in range(total_pages):
            try:
                page = doc[page_index]

                for lines in columns(page):
                    entries = detect_entries(lines)

                    for j, entry in enumerate(entries):
                        dims, est, conflict = metadata(
                            lines,
                            entries,
                            j,
                        )

                        item = notice(
                            url,
                            sale,
                            page_index + 1,
                            entry,
                            dims,
                            est,
                            conflict,
                        )

                        k = passage_key(item)

                        if k not in detected or (
                            detected[k]["estimation_min"] is None
                            and est is not None
                        ):
                            detected[k] = item

            except Exception as exc:
                page_errors.append({
                    "page": page_index + 1,
                    "erreur": str(exc),
                })

                print(
                    f"ERREUR page {page_index + 1}: "
                    f"{type(exc).__name__}: {exc}"
                )

                traceback.print_exc()

    return list(detected.values()), total_pages, page_errors


# ============================================================
# NETTOYAGE DE LA BASE HISTORIQUE
# ============================================================

def normalize_existing(item):
    x = dict(item)
    changes = []

    for field in ("artiste", "titre", "dimensions"):
        if isinstance(x.get(field), str):
            cleaned = (
                artist_name(x[field])
                if field == "artiste"
                else clean(x[field])
            )

            if cleaned != x[field]:
                x[field] = cleaned
                changes.append(field)

    if (
        x.get("estimation_min") is not None
        and x.get("estimation_max") is not None
    ):
        if x.get("type_prix") == "inconnu":
            x["type_prix"] = "estimation"
            changes.append("type_prix")

    if not x.get("id_passage"):
        x["id_passage"] = passage_key(x)
        changes.append("id_passage")

    if not x.get("id_oeuvre"):
        x["id_oeuvre"] = digest(
            "oeuvre-provisoire",
            passage_key(x),
        )
        changes.append("id_oeuvre")

    # Conserver le champ id historique :
    # des liens existants peuvent y faire reference.
    return x, changes


# ============================================================
# FUSION SANS ECRASER LES DONNEES CONTRADICTOIRES
# ============================================================

def merge_existing(old, new):
    merged = dict(old)
    conflicts = []
    additions = []

    for field in ("artiste", "titre", "dimensions"):
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
            conflicts.append({
                "champ": field,
                "ancien": current,
                "extrait": incoming,
            })

    for field in (
        "vente",
        "annee_vente",
        "source_page_pdf",
    ):
        if (
            merged.get(field) is None
            and new.get(field) is not None
        ):
            merged[field] = new[field]
            additions.append(field)

    old_pair = (
        old.get("estimation_min"),
        old.get("estimation_max"),
    )

    new_pair = (
        new.get("estimation_min"),
        new.get("estimation_max"),
    )

    if (
        all(v is not None for v in new_pair)
        and not new.get("conflit_estimation")
    ):
        if all(v is None for v in old_pair):
            for field in (
                "estimation_min",
                "estimation_max",
                "estimation",
            ):
                merged[field] = new[field]

            merged["type_prix"] = "estimation"
            additions.append("estimation")

        elif old_pair != new_pair:
            conflicts.append({
                "champ": "estimation",
                "ancien": old_pair,
                "extrait": new_pair,
            })

    if new.get("conflit_estimation"):
        conflicts.append({
            "champ": "estimation",
            "motif": "plusieurs estimations detectees",
        })

    merged["id_passage"] = passage_key(old)

    # Ne pas remplacer :
    # id, id_oeuvre, prix_marteau, prix_vendu,
    # prix_realise_verifie.
    return merged, additions, conflicts


# ============================================================
# RAPPROCHEMENTS POTENTIELS
# ============================================================

def dimensions_key(value):
    match = DIM.search(clean(value))

    if not match:
        return None

    numbers = re.findall(
        r"\d+(?:[.,]\d+)?",
        match.group(),
    )

    return tuple(sorted(
        round(float(x.replace(",", ".")), 1)
        for x in numbers
    ))


def potential_matches(records):
    groups = defaultdict(list)

    for item in records:
        dims = dimensions_key(item.get("dimensions"))

        if (
            dims
            and item.get("artiste")
            and item.get("titre")
        ):
            groups[(
                keytext(item["artiste"]),
                keytext(item["titre"]),
                dims,
            )].append(item)

    result = []

    for group in groups.values():
        if len({
            passage_key(x) for x in group
        }) < 2:
            continue

        same_catalogue = (
            len({x.get("url") for x in group}) == 1
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
                    "url": x.get("url"),
                }
                for x in group
            ],
        })

    return result


# ============================================================
# RAPPORT DE QUALITE
# ============================================================

def quality_report(records, matches):
    anomalies = []

    for x in records:
        issues = []

        if not x.get("dimensions"):
            issues.append("dimensions_absentes")

        if x.get("estimation_min") is None:
            issues.append("estimation_absente")

        if clean(x.get("titre")).endswith(
            ("/", ":", "-", ",")
        ):
            issues.append("titre_possiblement_tronque")

        if x.get("conflit_estimation"):
            issues.append("conflit_estimation")

        if issues:
            anomalies.append({
                "id": x.get("id"),
                "id_passage": passage_key(x),
                "artiste": x.get("artiste"),
                "titre": x.get("titre"),
                "problemes": issues,
                "url": x.get("url"),
                "page": x.get("source_page_pdf"),
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
        "nombre_notices_a_controler": len(anomalies),
    }


# ============================================================
# COLLECTE PRINCIPALE
# ============================================================

def main():
    original = load_db()

    print(
        f"{VERSION}: {len(original)} notices existantes",
        flush=True,
    )

    db = {}
    cleanup = []

    for old in original:
        item, changed = normalize_existing(old)
        k = passage_key(item)

        if not k:
            raise ValueError(
                "Notice sans identifiant exploitable"
            )

        if k in db:
            raise ValueError(
                f"Doublon de passage preexistant: {k}"
            )

        db[k] = item

        if changed:
            cleanup.append({
                "id_passage": k,
                "champs": changed,
            })

    historical = set(db)
    seen = set()
    new_keys = []
    additions = []
    conflicts = []
    catalogues = []

    stats = {
        "pages_consultees": 0,
        "pages_accessibles": 0,
        "pdf_lus": 0,
        "oeuvres_detectees": 0,
        "nouvelles_oeuvres": 0,
        "erreurs_ou_blocages": 0,
    }

    for sale, url in CATALOGUES.items():
        print(
            f"\nCATALOGUE: {sale}",
            flush=True,
        )

        stats["pages_consultees"] += 1

        try:
            data = fetch_pdf(url)
            stats["pages_accessibles"] += 1

            found, pages, page_errors = parse_pdf(
                data,
                url,
                sale,
            )

            stats["pdf_lus"] += 1
            stats["oeuvres_detectees"] += len(found)

            if page_errors:
                stats["erreurs_ou_blocages"] += len(page_errors)

            print(
                f"pages={pages} "
                f"lots={len(found)} "
                f"erreurs_pages={len(page_errors)}",
                flush=True,
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
                "erreurs_pages": page_errors,
            })

            for item in found:
                k = passage_key(item)
                seen.add(k)

                if k in db:
                    merged, added, problems = merge_existing(
                        db[k],
                        item,
                    )

                    db[k] = merged

                    if added:
                        additions.append({
                            "id_passage": k,
                            "champs": added,
                        })

                    if problems:
                        conflicts.append({
                            "id_passage": k,
                            "vente": sale,
                            "lot": item["lot"],
                            "conflits": problems,
                        })

                else:
                    db[k] = item
                    new_keys.append(k)
                    stats["nouvelles_oeuvres"] += 1

        except Exception as exc:
            stats["erreurs_ou_blocages"] += 1

            print(
                f"ERREUR CATALOGUE {sale}: "
                f"{type(exc).__name__}: {exc}",
                flush=True,
            )

            traceback.print_exc()

            catalogues.append({
                "vente": sale,
                "statut": "erreur",
                "oeuvres": 0,
                "erreur": (
                    f"{type(exc).__name__}: {exc}"
                ),
            })

    # Ne pas publier une collecte vide.
    if not seen:
        raise RuntimeError(
            "Aucun lot extrait : "
            "aucun JSON ne sera remplace."
        )

    records = sorted(
        db.values(),
        key=lambda x: (
            keytext(x.get("artiste")),
            keytext(x.get("titre")),
            str(x.get("annee_vente") or ""),
            lot_number(x.get("lot")),
        ),
    )

    matches = potential_matches(records)
    quality = quality_report(records, matches)

    missing = sorted(historical - seen)

    by_origin = Counter(
        db[k].get("origine", "inconnue")
        for k in missing
    )

    differences = {
        "version": VERSION,
        "date": timestamp(),
        "historique_avant": len(original),
        "detectes_cette_execution": len(seen),
        "non_redetectes": len(missing),
        "non_redetectes_par_origine": dict(by_origin),
        "lots_non_redetectes": [
            {
                "id_passage": k,
                "vente": db[k].get("vente"),
                "lot": db[k].get("lot"),
                "artiste": db[k].get("artiste"),
                "titre": db[k].get("titre"),
                "url": db[k].get("url"),
            }
            for k in missing
        ],
        "nouveaux_passages": new_keys,
        "nettoyages": cleanup,
        "champs_completes": additions,
        "conflits_a_verifier": conflicts,
    }

    report = {
        "version": VERSION,
        "date": timestamp(),
        "sources": {
            "BYDealers": stats,
            "Heffel": {
                "statut": "connecteur_a_developper",
                "oeuvres_detectees": 0,
            },
            "Gelineau": {
                "statut": "connecteur_a_developper",
                "oeuvres_detectees": 0,
            },
        },
        "catalogues": catalogues,
        "total_oeuvres": len(records),
        "avec_estimation": quality["avec_estimation"],
        "rapprochements_potentiels": len(matches),
        "lots_historiques_non_redetectes": len(missing),
        "conflits_a_verifier": len(conflicts),
        "note": (
            "Estimations de catalogues uniquement; "
            "aucun prix marteau deduit."
        ),
    }

    if len(records) < len(original):
        raise RuntimeError(
            "Regression du nombre de notices; "
            "sauvegarde annulee."
        )

    write_json(DB, records)
    write_json(REPORT, report)
    write_json(QUALITY, quality)
    write_json(DIFF, differences)

    print(
        f"\n{VERSION}: "
        f"{len(records)} notices, "
        f"{len(seen)} passages detectes, "
        f"{len(missing)} non redetectes, "
        f"{quality['avec_estimation']} estimations, "
        f"{len(conflicts)} conflits, "
        f"{len(cleanup)} notices nettoyees.",
        flush=True,
    )


if __name__ == "__main__":
    main()
