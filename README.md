# Art Enchères Québec V2

Site statique bilingue à développer ultérieurement, avec collecte automatique prudente pour BYDealers, Heffel et Gélineau.

## Mise en ligne (GitHub Pages)
1. Créer un dépôt GitHub et y téléverser **le contenu** de ce dossier, y compris `.github/workflows/collecte.yml`.
2. Settings → Pages → Deploy from a branch → `main` → `/ (root)` → Save.
3. Actions → Collecte publique quotidienne → Run workflow pour lancer une première collecte. Ensuite exécution quotidienne (horaire indicatif GitHub).
4. Le site charge `data/oeuvres.json` à chaque ouverture.

## Exécution locale
`pip install -r requirements.txt` puis `python collectors/run.py`.
Pour consulter le site localement : `python -m http.server 8000`, puis http://localhost:8000/.

## Limites importantes
- Le collecteur respecte robots.txt, explore au maximum 21 pages candidates par source et n'utilise que les métadonnées publiques JSON-LD d'œuvres. Il **ne garantit aucun résultat** pour une maison qui ne les publie pas ou bloque l'accès automatisé.
- Il ne se connecte pas aux comptes, ne contourne ni CAPTCHA ni protection, et ne récupère pas les résultats vendus à partir de textes ambigus.
- Une offre affichée n'est pas un **prix marteau**. Les champs « vendu » et « prix marteau » ne sont renseignés que par des données confirmées (p. ex. CSV vérifié).
- Les images sont référencées par URL source, non recopiées sur le serveur. Certains sites bloquent leur affichage externe.
- Les statuts collectés sont « en vente » si la disponibilité JSON-LD l'indique, sinon « inconnu ». Les anciens lots ne sont pas automatiquement déclarés vendus.
- Si un site interdit la collecte ou change son format, il faut une permission/API ou un connecteur adapté et autorisé.
