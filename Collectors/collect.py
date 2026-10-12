
def is_artist(text):
    """Filtre les phrases de biographies et les initiales seules."""
    text = artist_name(text)
    words = text.split()

    if not (5 <= len(text) <= 85 and 2 <= len(words) <= 7):
        return False

    if re.search(r'\d|@|https?://|[.!?;«»“”]', text):
        return False

    if text[-1] in ',:':
        return False

    blocked = {
        'collection', 'private', 'particuliere', 'membre',
        'received', 'following', 'artist', 'oeuvre',
        'artiste', 'musee', 'museum', 'devolved',
        'current', 'provenance', 'acquired', 'acquis',
        'the', 'with', 'pour', 'dans', 'cette',
        'that', 'from', 'depuis', 'etait',
        'etude', 'exposition', 'exhibited'
    }

    normalized = [
        keytext(w).strip("',()")
        for w in words
    ]

    if any(w in blocked for w in normalized):
        return False

    if not all(
        re.match(r"^[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'’().-]*$", w)
        for w in words
    ):
        return False

    if not words[0][0].isupper():
        return False

    return True
