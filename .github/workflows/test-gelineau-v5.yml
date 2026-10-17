#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Diagnostic HiBid Gélineau V5.1 — lecture seule, aucun changement à gelineau.json.
Dépendance : requests. Vérifie robots.txt; en cas d'indisponibilité, s'arrête.
"""
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.robotparser import RobotFileParser

import requests

AUCTION_ID = 784138
API = 'https://hibid-api.io/sr/main/v1/search/lot'
ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / 'data' / 'test-gelineau-v5.json'
SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': 'ArtEncheresQuebec/1.0 (public-catalog-research)',
    'Accept': 'application/json',
    'Content-Type': 'application/json',
    'Origin': 'https://hibid.com',
    'Referer': 'https://hibid.com/quebec/catalog/784138/encans-g-lineau-and-fils-no-71',
})


def verify_robots(report):
    url = 'https://hibid-api.io/robots.txt'
    for attempt in range(1, 4):
        try:
            response = SESSION.get(url, timeout=20, allow_redirects=False)
            status = response.status_code
            report['robots']['tentatives'].append({'essai': attempt, 'http': status})
            if status == 200:
                rp = RobotFileParser()
                rp.parse(response.text.splitlines())
                permitted = rp.can_fetch(SESSION.headers['User-Agent'], API)
                report['robots']['resultat'] = 'autorise' if permitted else 'interdit'
                if not permitted:
                    raise PermissionError('robots.txt interdit cet endpoint')
                return
            if status in (404, 410):
                report['robots']['resultat'] = 'absent'
                return
            if status not in (408, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524):
                raise PermissionError(f'robots.txt: HTTP {status}; arrêt')
        except requests.RequestException as exc:
            report['robots']['tentatives'].append({'essai': attempt, 'erreur': str(exc)})
        if attempt < 3:
            time.sleep(3 * attempt)
    report['robots']['resultat'] = 'indisponible'
    raise RuntimeError('robots.txt indisponible après 3 essais; aucun appel API effectué')


def fetch_page(page):
    # Paramètres page/size confirmés dans Chrome DevTools le 17 octobre 2026.
    # auctioneerId n'est pas reproduit : la nécessité de ce filtre reste à tester.
    payload = {
        'query': '',
        'options': {
            'auctionId': AUCTION_ID,
            'categoryId': None,
            'country': None,
            'location': None,
            'lotType': [0],
            'maxBid': None,
            'minBid': None,
            'page': page,
            'portallocation': None,
            'shipping': False,
            'sitetype': 2,
            'size': 100,
            'sortOrder': 'LOT_NUMBER',
            'state': None,
            'status': ['ALL'],
        },
    }
    response = SESSION.post(API, json=payload, timeout=35, allow_redirects=False)
    if response.is_redirect:
        raise RuntimeError(f'Redirection HTTP {response.status_code} non suivie')
    response.raise_for_status()
    data = response.json().get('data', {})
    if not isinstance(data, dict) or not isinstance(data.get('lots'), list):
        raise ValueError('Réponse inattendue : data.lots absent')
    ids = [str(item['id']) for item in data['lots'] if isinstance(item, dict) and item.get('id')]
    return data, ids


def main():
    report = {
        'version': '5.1-test',
        'date': datetime.now(timezone.utc).isoformat(),
        'catalogue': AUCTION_ID,
        'endpoint': API,
        'parametres_pagination': 'options.page et options.size (observés dans DevTools)',
        'filtre_auctioneerId': 'omis; impact non vérifié',
        'robots': {'resultat': 'non_verifie', 'tentatives': []},
        'pages': [],
        'identifiants_uniques': 0,
        'pagination_confirmee': False,
        'erreurs': [],
    }
    seen = set()
    try:
        verify_robots(report)
        for number in range(1, 4):
            data, ids = fetch_page(number)
            fresh = set(ids) - seen
            seen.update(ids)
            entry = {
                'page_demandee': number,
                'page_retournee': data.get('pageNumber'),
                'total_lots_annonces': data.get('totalCount'),
                'total_pages_annoncees': data.get('totalPages'),
                'nombre_ids': len(ids),
                'nouveaux_ids': len(fresh),
                'premiers_ids': ids[:3],
            }
            report['pages'].append(entry)
            print(f"Page {number}: {len(ids)} identifiants, {len(fresh)} nouveaux", flush=True)
            if number > 1 and not fresh:
                report['erreurs'].append('La page répète les identifiants précédents')
                break
            time.sleep(2)
        report['identifiants_uniques'] = len(seen)
        report['pagination_confirmee'] = (
            len(report['pages']) == 3
            and [p['page_retournee'] for p in report['pages']] == [1, 2, 3]
            and len(seen) == 219
            and [p['nombre_ids'] for p in report['pages']] == [100, 100, 19]
        )
    except (requests.RequestException, PermissionError, RuntimeError, ValueError, KeyError) as exc:
        report['erreurs'].append(f'{type(exc).__name__}: {exc}')
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    if not report['pagination_confirmee']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
