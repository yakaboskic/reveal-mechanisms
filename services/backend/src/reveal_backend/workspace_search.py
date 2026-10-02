"""Literal search over authorized saved summaries, before cursor pagination."""


def normalize(query):
    return ' '.join(query.split()).casefold()


def matches(query, *values):
    def strings(value):
        if isinstance(value, str):
            yield value
        elif isinstance(value, dict):
            for child in value.values(): yield from strings(child)
        elif isinstance(value, list):
            for child in value: yield from strings(child)
    text = normalize(' '.join(text for value in values for text in strings(value)))
    return all(word in text for word in query.split())


def filter_summaries(items, query, kind):
    if not query: return items
    result = []
    for item in items:
        gap = item.get('knowledge_gap') or {}
        attribution = item.get('attribution') or {}
        fields = [gap.get('id'), gap.get('name'), gap.get('text'), attribution.get('display_name')]
        if kind == 'account':
            account = item['account']
            fields.extend(account.get(key) for key in ('id', 'name', 'closing_remarks'))
        else:
            fields.extend([item.get('id'), item.get('summary'),
                [{key: anchor.get(key) for key in ('name', 'trait', 'source_id', 'mechanism_id')}
                 for anchor in item.get('anchors', [])]])
        if matches(query, *fields): result.append(item)
    return result
