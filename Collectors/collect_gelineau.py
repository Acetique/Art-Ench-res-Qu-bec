#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Art Enchères Québec — collecteur Gélineau V3 (HTML public HiBid).
Dépendances: requests, beautifulsoup4. Ne contourne aucune restriction d'accès.
"""
import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / 'data' / 'gelineau.json'
REPORT = ROOT / 'data' / 'rapport-gelineau.json'
BASE = 'https://hibid.com'
COMPANY = BASE + '/company/87825/encans-gelineau-et-fils-inc'
# Catalogues publics confirmés; d'autres sont découverts sur la page de l'entreprise.
SEEDS = [
    BASE + '/quebec/catalog/784138/encans-g-lineau-and-fils-no-71',
    BASE + '/canada/catalog/772786/encans-g-lineau-and-fils-no-70',
    BASE + '/quebec/catalog/537190/encans-g-lineau-and-fils-no-52',
    BASE + '/quebec/catalog/502294/encans-g-lineau-and-fils-no-47',
    BASE + '/quebec/catalog/406312/encans-g-lineau-and-fils-no-37',
    BASE + '/quebec/catalog/349662/encans-g-lineau-and-fils-no-30',
    BASE + '/quebec/catalog/309514/encans-g-lineau-and-fils-no-25',
]
HEADERS = {'User-Agent': 'ArtEncheresQuebec/1.0 (+public-catalog-research)', 'Accept-Language': 'fr-CA,fr;q=0.9,en;q=0.8'}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)
PAUSE = 2
MAX_PAGES = 30
MAX_EMPTY_PAGES = 2
MAX_CATALOGUES = 60


def stamp():
    return datetime.now(timezone.utc).isoformat()


def canonical(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in ('hibid.com', 'www.hibid.com', 'canada.hibid.com'):
        return None
    return url.split('#', 1)[0]


def allowed(url):
    parsed = urlparse(url)
    origin = f'{parsed.scheme}://{parsed.netloc}'
    robots = RobotFileParser()
    robots.set_url(origin + '/robots.txt')
    try:
        resp = SESSION.get(origin + '/robots.txt', timeout=20)
        if resp.status_code == 200:
            robots.parse(resp.text.splitlines())
            return robots.can_fetch(HEADERS['User-Agent'], url)
        return resp.status_code in (404, 410)
    except requests.RequestException:
        return False


ROBOTS = {}


def fetch(url):
    url = canonical(url)
    if not url:
        raise ValueError('URL externe non autorisée')
    host = urlparse(url).netloc
    # robots.txt peut varier selon le chemin; ne pas cacher une permission globale.
    if url not in ROBOTS:
        ROBOTS[url] = allowed(url)
    if not ROBOTS[url]:
        raise PermissionError('robots.txt inaccessible ou accès interdit: ' + host)
    resp = SESSION.get(url, timeout=35, allow_redirects=False)
    if resp.status_code in (301, 302, 303, 307, 308):
        raise RuntimeError('Redirection non suivie: ' + url)
    resp.raise_for_status()
    if 'html' not in resp.headers.get('Content-Type', '').lower():
        raise ValueError('Réponse non HTML: ' + url)
    time.sleep(PAUSE)
    return BeautifulSoup(resp.text, 'html.parser')


def clean(text):
    return re.sub(r'\s+', ' ', text or '').strip()


def money(text):
    val = re.sub(r'[^\d.,]', '', text)
    if not val:
        return None
    if '.' in val and len(val.rsplit('.', 1)[-1]) == 2:
        val = val.rsplit('.', 1)[0]
    elif ',' in val and len(val.rsplit(',', 1)[-1]) == 2:
        val = val.rsplit(',', 1)[0]
    val = re.sub(r'\D', '', val)
    return int(val) if val else None


PRICE_RANGE = re.compile(r'([\d][\d, .]*)\s*[-–]\s*([\d][\d, .]*)\s*(?:CAD|CDN|Can)?', re.I)
REALIZED = re.compile(r'(?:Price\s+Realized|Prix\s+r[ée]alis[ée]|Sold\s+for)\s*:?\s*\$?\s*([\d][\d, .]*)\s*(?:CAD|CDN|Can)?', re.I)
LOT = re.compile(r'\bLot\s*#?\s*(\d+[a-z]?)\s*[|:\-]?\s*(.*)', re.I)


def catalog_id(url):
    m = re.search(r'/(?:catalog|auction)/(\d+)', url)
    return m.group(1) if m else None


def discover(soup):
    found = []
    for a in soup.select('a[href]'):
        url = canonical(urljoin(BASE, a.get('href', '')))
        if url and catalog_id(url) and url not in found:
            found.append(url)
    return found


def lot_blocks(soup):
    """N'accepte que les éléments contenant un lien explicite vers un lot."""
    seen = set()
    for a in soup.select('a[href*="/lot/"]'):
        href = canonical(urljoin(BASE, a.get('href', '')))
        if not href or not re.search(r'/lot/\d+', href) or href in seen:
            continue
        seen.add(href)
        block = a
        for parent in list(a.parents)[:6]:
            txt = clean(parent.get_text(' ', strip=True))
            if re.search(r'\bLot\s*\d+', txt, re.I) and len(txt) < 1600:
                block = parent
                if PRICE_RANGE.search(txt) or 'Price Realized' in txt or 'Est.' in txt:
                    break
        yield href, block


def image_url(block):
    for img in block.select('img'):
        for key in ('data-src', 'data-lazy-src', 'src'):
            raw = img.get(key)
            if raw and not raw.startswith('data:'):
                url = urljoin(BASE, raw)
                if urlparse(url).scheme == 'https':
                    return url
    return None


def enrich(item):
    """Nettoie les données existantes sans confondre mise et vente finale."""
    item = dict(item)
    raw = clean(item.get('titre', ''))
    past = item.get('statut') == 'vente_passee'
    sale = REALIZED.search(raw) if past else None
    if sale:
        value = money(sale.group(1))
        if value is not None:
            item['prix_vendu'] = value
            item['prix'] = value
            item['type_prix'] = 'prix_vendu_affiche'
            item['source_prix'] = 'Sold for / Price Realized (HiBid)'
            # « Sold for » n'est pas toujours une preuve du marteau hors frais.
            # On conserve le prix_marteau antérieur uniquement s'il était vérifié.
            if not item.get('prix_realise_verifie'):
                item['prix_marteau'] = None
            item['prix_realise_verifie'] = True
    if not past:
        bid = re.search(r'\$\s*([\d,]+(?:\.\d{2})?)\s*CAD\s*(\d+)\s*Bids?', raw, re.I)
        if bid:
            item['enchere_actuelle'] = money(bid.group(1))
            item['nombre_encheres'] = int(bid.group(2))
    # Le nom affiché sur HiBid précède les dimensions et les informations commerciales.
    name = re.sub(r'^Online\s+Only\s+', '', raw, flags=re.I)
    name = re.split(r'\b(?:Sold\s+for|Price\s+Realized|Date\s+Sold|Est\.|Encans\s+Gelineau)\b|\$\s*[\d,]', name, maxsplit=1, flags=re.I)[0]
    name = re.sub(r'\s+\d+(?:[ /\d]*)(?:\s*x\s*[/\d ]+)?\s*\"?\s*$', '', name).strip(' -|')
    # Ne pas inventer l'artiste si la notice n'est pas clairement attribuable.
    if name and not re.match(r'^(?:Inconnu|Unknown)\b', name, re.I):
        item['titre'] = name
        if not item.get('artiste') or item['artiste'].startswith('Online Only'):
            item['artiste'] = name
    else:
        item['titre'] = name or raw
        item['artiste'] = None
    return item


def parse_lot(url, block, auction_id, auction_title, is_past):
    text = clean(block.get_text(' ', strip=True))
    match = LOT.search(text)
    if not match:
        return None
    number = match.group(1)
    title = clean(match.group(2))[:500]
    if not title:
        return None
    est_match = re.search(r'\bEst\.?\s*' + PRICE_RANGE.pattern, text, re.I)
    low = money(est_match.group(1)) if est_match else None
    high = money(est_match.group(2)) if est_match else None
    if low is not None and (high is None or low > high):
        low = high = None
    item_id = hashlib.sha256(f'gelineau|{auction_id}|{number}'.encode()).hexdigest()[:20]
    item = {
        'id': item_id, 'id_passage': item_id, 'maison': 'Gélineau et Fils',
        'vente': auction_title, 'id_vente': auction_id, 'lot': number,
        'titre': title, 'artiste': None,
        'estimation_min': low, 'estimation_max': high,
        'estimation': f'{low:,} - {high:,} CAD' if low is not None else None,
        'prix_marteau': None, 'prix_vendu': None, 'prix': None,
        'prix_realise_verifie': False,
        'type_prix': 'estimation' if low is not None else 'inconnu',
        'statut': 'vente_passee' if is_past else 'en_cours',
        'devise': 'CAD', 'image': image_url(block), 'url': url,
        'date_collecte': stamp(),
    }
    return enrich(item)


def page_number(url):
    for k, v in parse_qsl(urlparse(url).query, keep_blank_values=True):
        if k.lower() in ('page', 'p') and v.isdigit():
            return int(v)
    return 1


def set_page(url, number):
    parts = urlparse(url)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if k.lower() not in ('page', 'p')]
    params.append(('page', str(number)))
    return urlunparse(parts._replace(query=urlencode(params)))


def page_links(soup, url):
    auction = catalog_id(url)
    links = {}
    for a in soup.select('a[href]'):
        target = canonical(urljoin(url, a['href']))
        if target and catalog_id(target) == auction and re.search(r'[?&](?:page|p)=\d+', target, re.I):
            links[page_number(target)] = target
    return links


def catalog_candidates(soup, source_url):
    """Liens catalogues trouvés dans le HTML; ne pas inventer d'identifiants."""
    out = []
    for a in soup.select('a[href]'):
        target = canonical(urljoin(source_url, a.get('href', '')))
        if not target or not catalog_id(target):
            continue
        label = clean(a.get_text(' ', strip=True))
        # Page entreprise: ses catalogues sont pertinents. Ailleurs: exiger Gélineau.
        if source_url == COMPANY or re.search(r'g[ée]lineau|gelineau', label + ' ' + target, re.I):
            out.append(target)
    return out


def unique_lot_ids(soup):
    ids = set()
    for a in soup.select('a[href*="/lot/"]'):
        href = canonical(urljoin(BASE, a.get('href', '')))
        m = re.search(r'/lot/(\d+)', href or '')
        if m:
            ids.add(m.group(1))
    return ids


def collect_pages(url, first_soup, auction_id, title, past, items, errors):
    """Suit les liens de pagination et teste page=N tant que les lots changent."""
    seen_lots = set()
    visited_urls = set()
    queue = {1: url}
    queue.update(page_links(first_soup, url))
    tested = 0
    consecutive_empty = 0
    page_number_next = 2
    pages_with_lots = 0
    while tested < MAX_PAGES:
        candidates = sorted((n, u) for n, u in queue.items() if u not in visited_urls)
        if candidates:
            number, page_url = candidates[0]
        else:
            # HiBid ne publie pas toujours de liens HTML de pagination.
            # Tester prudemment la page suivante et s'arrêter si elle répète la première.
            while page_number_next in queue:
                page_number_next += 1
            number, page_url = page_number_next, set_page(url, page_number_next)
            page_number_next += 1
        visited_urls.add(page_url)
        tested += 1
        try:
            page = first_soup if page_url == url else fetch(page_url)
        except (PermissionError, requests.HTTPError, RuntimeError, ValueError) as exc:
            errors.append({'catalogue': auction_id, 'page': number, 'erreur': str(exc)})
            if number > 1:
                break
            raise
        except requests.RequestException as exc:
            errors.append({'catalogue': auction_id, 'page': number, 'erreur': str(exc)})
            break
        ids = unique_lot_ids(page)
        fresh_ids = ids - seen_lots
        if not fresh_ids:
            consecutive_empty += 1
            print(f'  Page {number}: aucun nouveau lot ({len(ids)} identifiants), arrêt possible', flush=True)
            if consecutive_empty >= MAX_EMPTY_PAGES:
                break
            continue
        consecutive_empty = 0
        seen_lots.update(fresh_ids)
        pages_with_lots += 1
        count_before = len(items)
        for lot_url, block in lot_blocks(page):
            match = re.search(r'/lot/(\d+)', lot_url)
            if not match or match.group(1) not in fresh_ids:
                continue
            item = parse_lot(lot_url, block, auction_id, title, past)
            if item:
                items[item['id']] = item
        queue.update({n: u for n, u in page_links(page, page_url).items() if u not in visited_urls})
        print(f'  Page {number}: {len(fresh_ids)} nouveaux identifiants, {len(items)-count_before} fiches', flush=True)
        # Si page 1 a moins de 100 lots et aucun lien suivant, une deuxième page
        # peut néanmoins exister; le test est limité et contrôlé.
    return pages_with_lots, tested


def main():
    print('ART ENCHERES QUEBEC — COLLECTEUR GELINEAU V3.0', flush=True)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    previous = []
    if OUTPUT.exists():
        raw = OUTPUT.read_text(encoding='utf-8-sig').strip()
        if not raw:
            print('Base Gelineau vide : initialisation de la premiere collecte.', flush=True)
        else:
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    'data/gelineau.json contient du texte non vide mais un JSON invalide. '
                    'Sauvegarder ou corriger ce fichier avant de relancer : ' + str(exc)
                ) from exc
            if isinstance(obj, list):
                previous = obj
            elif isinstance(obj, dict) and isinstance(obj.get('oeuvres'), list):
                previous = obj['oeuvres']
            elif isinstance(obj, dict) and isinstance(obj.get('lots'), list):
                previous = obj['lots']
            else:
                raise RuntimeError('Structure de data/gelineau.json non reconnue : sauvegarde interrompue.')
            if not all(isinstance(item, dict) for item in previous):
                raise RuntimeError('Notices historiques invalides : sauvegarde interrompue.')
    catalogs = list(SEEDS)
    errors = []
    try:
        home = fetch(COMPANY)
        for candidate in catalog_candidates(home, COMPANY):
            if catalog_id(candidate) not in {catalog_id(u) for u in catalogs}:
                catalogs.append(candidate)
    except Exception as exc:
        errors.append({'catalogue': 'entreprise', 'erreur': str(exc)})
    # Recherche de catalogues supplémentaires à partir des pages de catalogues.
    items = {}
    visited_catalogs = set()
    page_count = 0
    attempted = 0
    index = 0
    while index < len(catalogs) and attempted < MAX_CATALOGUES:
        url = catalogs[index]
        index += 1
        auction_id = catalog_id(url)
        if not auction_id or auction_id in visited_catalogs:
            continue
        visited_catalogs.add(auction_id)
        attempted += 1
        try:
            soup = fetch(url)
            for candidate in catalog_candidates(soup, url):
                if catalog_id(candidate) not in visited_catalogs and catalog_id(candidate) not in {catalog_id(u) for u in catalogs}:
                    catalogs.append(candidate)
            heading = soup.find('h1')
            title = clean(heading.get_text(' ', strip=True)) if heading else f'Gélineau {auction_id}'
            body = clean(soup.get_text(' ', strip=True))
            past = bool(re.search(r'\bPast Auction\b|Encan termin[ée]', body, re.I))
            print(f'Catalogue {auction_id} : {title}', flush=True)
            actual, tested = collect_pages(url, soup, auction_id, title, past, items, errors)
            page_count += actual
            print(f'  Bilan : {actual} pages distinctes sur {tested} testées', flush=True)
        except Exception as exc:
            errors.append({'catalogue': url, 'erreur': str(exc)})
            print('AVERTISSEMENT :', url, exc, flush=True)
    # La base existante est conservée, sans remplacer des résultats vérifiés par des champs vides.
    merged = {r['id']: r for r in previous if isinstance(r, dict) and r.get('id')}
    for key, fresh in items.items():
        old = merged.get(key, {})
        for field in ('prix_marteau', 'prix_vendu', 'prix', 'image', 'estimation_min', 'estimation_max'):
            if fresh.get(field) is None and old.get(field) is not None:
                fresh[field] = old[field]
        if old.get('prix_realise_verifie') and fresh.get('prix_marteau') is not None:
            fresh['prix_realise_verifie'] = True
        merged[key] = {**old, **fresh}
    merged = {key: enrich(value) for key, value in merged.items()}
    report = {'date': stamp(), 'catalogues_tentes': attempted, 'pages_distinctes': page_count,
              'lots_nouveaux_detectes': len(items), 'catalogues_decouverts': len({catalog_id(u) for u in catalogs}), 'lots_conserves': len(merged),
              'prix_vente_affiches': sum(x.get('prix_vendu') is not None for x in merged.values()),
              'prix_marteau_verifies': sum(x.get('prix_marteau') is not None for x in merged.values()),
              'images_referencees': sum(bool(x.get('image')) for x in merged.values()),
              'erreurs': errors}
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    if not items and not previous:
        print('ECHEC : aucun lot extrait. Vérifier le rapport; HTML dynamique ou accès bloqué.', flush=True)
        raise SystemExit(1)
    OUTPUT.write_text(json.dumps(list(merged.values()), ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == '__main__':
    main()
