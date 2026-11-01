"""Multi-sample inference. Observations are evidence, never proof of requirements."""
import hashlib
import json
import re
from collections import Counter, defaultdict
from urllib.parse import urlsplit


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()


def kind(value):
    if value is None: return 'null'
    if isinstance(value, bool): return 'boolean'
    if isinstance(value, int): return 'integer'
    if isinstance(value, float): return 'number'
    if isinstance(value, str): return 'string'
    if isinstance(value, list): return 'array'
    return 'object'


def infer_schema(values):
    if not values:
        return {}
    groups = defaultdict(list)
    for value in values:
        groups[kind(value)].append(value)
    if 'integer' in groups and 'number' in groups:
        groups['number'] += groups.pop('integer')
    if len(groups) > 1:
        return {'anyOf': [infer_schema(group) for group in groups.values()]}
    typ, samples = next(iter(groups.items()))
    schema = {'type': typ}
    if typ == 'object':
        keys = list(dict.fromkeys(k for obj in samples for k in obj))
        schema['properties'] = {key: infer_schema([obj[key] for obj in samples if key in obj]) for key in keys}
        # Explicit x- annotations, not JSON Schema required/enum constraints.
        schema['x-observation'] = {'sample_count': len(samples), 'presence': {key: sum(key in obj for obj in samples) for key in keys},
                                  'always_present_candidates': [key for key in keys if all(key in obj for obj in samples)]}
    elif typ == 'array':
        schema['items'] = infer_schema([item for array in samples for item in array])
    elif typ in ('string', 'integer', 'number', 'boolean'):
        unique = list(dict.fromkeys(json.dumps(value, ensure_ascii=False) for value in samples))
        schema['x-observation'] = {'sample_count': len(samples), 'distinct_count': len(unique),
                                  'constant_candidate': len(samples) > 1 and len(unique) == 1}
        # Deliberately do not copy sampled values into public candidate metadata.
    return schema


def schema_changes(expected, value, path=''):
    """Report structural drift without leaking values; additions are compatible."""
    errors, warnings = [], []
    if 'anyOf' in expected:
        options = [schema_changes(option, value, path) for option in expected['anyOf']]
        clean = [result for result in options if not result[0]]
        return min(clean, key=lambda pair: len(pair[1])) if clean else ([{'path': path, 'reason': 'type_or_shape_changed'}], [])
    typ = expected.get('type')
    actual = kind(value)
    if typ and actual != typ and not (typ == 'number' and actual == 'integer'):
        return ([{'path': path, 'reason': 'type_changed', 'expected': typ, 'actual': actual}], [])
    if isinstance(value, dict):
        props = expected.get('properties', {})
        for key in expected.get('required', []):
            if key not in value: errors.append({'path': path + '/' + key, 'reason': 'required_missing'})
        for key in expected.get('x-observation', {}).get('always_present_candidates', []):
            if key not in value and key not in expected.get('required', []):
                warnings.append({'path': path + '/' + key, 'reason': 'previously_observed_field_missing'})
        for key in props.keys() & value.keys():
            e, w = schema_changes(props[key], value[key], path + '/' + key)
            errors.extend(e); warnings.extend(w)
    elif isinstance(value, list):
        for item in value:
            e, w = schema_changes(expected.get('items', {}), item, path + '/*')
            errors.extend(e); warnings.extend(w)
    def unique(items):
        return list({json.dumps(item, sort_keys=True): item for item in items}.values())[:100]
    return unique(errors), unique(warnings)


ID = re.compile(r'(?:[0-9]+|[a-f0-9]{24}|[a-f0-9]{32}|[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})$', re.I)


def group_exchanges(exchanges):
    """Only generalize varying ID-shaped segments; never merge /search with /42."""
    groups = defaultdict(list)
    for exchange in exchanges:
        request = exchange['request']; parsed = urlsplit(request['url'])
        segments = parsed.path.split('/')
        signature = tuple('<id>' if ID.fullmatch(segment) else segment for segment in segments)
        groups[(request['method'].upper(), f'{parsed.scheme}://{parsed.netloc}', signature)].append(exchange)
    results = []
    for (method, origin, _), samples in groups.items():
        paths = [urlsplit(sample['request']['url']).path.split('/') for sample in samples]
        template = list(paths[0]); parameters = {}
        for index in range(len(template)):
            values = list(dict.fromkeys(path[index] for path in paths))
            if len(values) > 1 and all(ID.fullmatch(value) for value in values):
                prior = template[index-1] if index else ''
                name = re.sub('[^a-zA-Z0-9_]', '_', prior).rstrip('s') + '_id'
                if not re.fullmatch('[a-zA-Z_][a-zA-Z0-9_]*', name): name = 'id'
                if name in parameters: name += '_' + str(index)
                parameters[name] = {'segment': index, 'distinct_samples': len(values), 'inference': 'identifier_candidate'}
                template[index] = '{' + name + '}'
        results.append((method, origin, '/'.join(template), samples, parameters))
    return results
