"""Normalize only visible hosted-search source text; never infer document access."""
from urllib.parse import urlsplit

EXCERPT_LIMIT = 12000
FIELDS = ('snippet', 'excerpt', 'description')


def normalize_native_source(value):
    if not isinstance(value, dict) or not isinstance(value.get('url'), str):
        return None
    try:
        parsed = urlsplit(value['url'])
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    # Preserve original field names (bounded), concatenate distinct nonempty text
    # in this documented order. No coercion of null, objects, or numbers to text.
    source = {'url': value['url']}
    if isinstance(value.get('title'), str):
        source['title'] = value['title'][:2000]
    parts, lengths = [], {}
    previous_lengths = value.get('visible_field_lengths') or {}
    for key in FIELDS:
        raw = value.get(key)
        if not isinstance(raw, str):
            continue
        source[key] = raw[:EXCERPT_LIMIT]
        prior = previous_lengths.get(key) if isinstance(previous_lengths, dict) else None
        lengths[key] = max(len(raw), prior if isinstance(prior, int) and not isinstance(prior, bool) else 0)
        text = raw.strip()
        if text and text not in parts:
            parts.append(text)
    merged = '\n\n'.join(parts)
    if not lengths:
        return source
    source.update(visible_excerpt=merged[:EXCERPT_LIMIT], visible_field_lengths=lengths,
                  excerpt_merge_order=list(FIELDS),
                  excerpt_truncated=len(merged) > EXCERPT_LIMIT or any(n > EXCERPT_LIMIT for n in lengths.values()))
    return source
