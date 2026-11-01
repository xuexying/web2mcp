"""Small, deterministic JSON mapping language. No eval, imports, I/O or clock."""
import json
import math
from datetime import date, datetime, timedelta

from .client import CallError


FUNCTIONS = {'concat': (1, 50), 'join': (2, 2), 'coalesce': (1, 50),
             'add': (1, 50), 'multiply': (2, 2), 'sum': (1, 1),
             'number': (1, 1), 'string': (1, 1), 'round': (2, 2),
             'json': (1, 1), 'date_add_days': (2, 2), 'eq': (2, 2),
             'merge': (2, 5), 'all': (1, 1), 'length': (1, 1),
             'object_set': (3, 3), 'odata_quote': (1, 1), 'find_one': (3, 3), 'fingerprint': (1, 1),
             'datetime_valid': (1, 1), 'filter_eq': (3, 3), 'filter_ne': (3, 3),
             'substring': (3, 3), 'datetime_format': (2, 2), 'integer_string': (1, 1)}


def pointer(value, path):
    if path == '': return value
    if not isinstance(path, str) or not path.startswith('/'):
        raise CallError('Expected JSON pointer')
    try:
        for part in path[1:].split('/'):
            part = part.replace('~1', '/').replace('~0', '~')
            if isinstance(value, list):
                if not part.isdigit(): raise ValueError()
                value = value[int(part)]
            else: value = value[part]
        return value
    except (KeyError, IndexError, ValueError, TypeError):
        raise CallError('Contract reference missing: ' + path) from None


def validate_template(value, steps=(), *, item=False, depth=0):
    """Reject typos and forward references before any HTTP request is made."""
    if depth > 32: raise CallError('Mapping nesting exceeds 32')
    if isinstance(value, list):
        for child in value: validate_template(child, steps, item=item, depth=depth+1)
    if not isinstance(value, dict): return
    special = {key for key in value if key.startswith('$')}
    if special:
        kind = next(iter(special))
        shapes = {'$arg': {'$arg'}, '$step': {'$step', 'pointer'}, '$item': {'$item'},
                  '$literal': {'$literal'}, '$fn': {'$fn', 'args'},
                  '$map': {'$map', 'value'}, '$get': {'$get', 'pointer', 'default'},
                  '$if': {'$if', 'then', 'else'}}
        if kind not in shapes or set(value) != shapes[kind]: raise CallError('Invalid mapping expression')
        if kind == '$literal': return
        if kind in ('$arg', '$item', '$step', '$get'):
            path = value[kind] if kind in ('$arg', '$item') else value['pointer']
            if not isinstance(path, str) or path and not path.startswith('/'): raise CallError('Expected JSON pointer')
        if kind == '$item' and not item: raise CallError('$item is only valid inside $map')
        if kind == '$step' and value['$step'] not in steps: raise CallError('Unknown or forward step reference')
        if kind in ('$arg', '$item', '$step'): return
        if kind == '$fn':
            if not isinstance(value['$fn'], str) or value['$fn'] not in FUNCTIONS or not isinstance(value['args'], list):
                raise CallError('Unsupported mapping function')
            low, high = FUNCTIONS[value['$fn']]
            if not low <= len(value['args']) <= high: raise CallError('Wrong mapping function arity')
            validate_template(value['args'], steps, item=item, depth=depth+1)
            return
        if kind == '$map':
            validate_template(value['$map'], steps, item=item, depth=depth+1)
            validate_template(value['value'], steps, item=True, depth=depth+1)
            return
        if kind == '$get':
            validate_template(value['$get'], steps, item=item, depth=depth+1)
            validate_template(value['default'], steps, item=item, depth=depth+1)
            return
    for child in value.values(): validate_template(child, steps, item=item, depth=depth+1)


def _number(value):
    if isinstance(value, bool): raise ValueError()
    result = float(value)
    if not math.isfinite(result): raise ValueError()
    return result


def _function(name, args):
    if name == 'integer_string':
        number = _number(args[0])
        if not number.is_integer() or abs(number) > 9007199254740991: raise ValueError()
        return str(int(number))
    if name in ('filter_eq', 'filter_ne'):
        if not isinstance(args[0], list) or len(args[0]) > 1000 or not isinstance(args[1], str): raise ValueError()
        # Missing fields never match null; Python bool/int equality is not JSON equality.
        return [row for row in args[0] if isinstance(row, dict) and args[1] in row
                and ((type(row[args[1]]) is type(args[2]) and row[args[1]] == args[2]) == (name == 'filter_eq'))]
    if name == 'substring':
        if not isinstance(args[0], str) or any(type(i) is not int for i in args[1:]) or not 0 <= args[1] <= args[2] <= 10000: raise ValueError()
        return args[0][args[1]:args[2]]
    if name == 'datetime_format':
        if args[1] not in ('%Y-%m-%d %H:%M', '%Y-%m', '%Y', '%m'): raise ValueError()
        parsed=datetime.fromisoformat(args[0])
        if parsed.tzinfo is None: raise ValueError()
        return parsed.strftime(args[1])
    if name == 'datetime_valid':
        try: return isinstance(args[0], str) and datetime.fromisoformat(args[0]).tzinfo is not None
        except ValueError: return False
    if name == 'fingerprint':
        from .evidence import fingerprint
        return fingerprint(args[0])
    if name == 'find_one':
        if not isinstance(args[0], list) or len(args[0]) > 1000 or not isinstance(args[1], str): raise ValueError()
        matches = [x for x in args[0] if isinstance(x, dict) and x.get(args[1]) == args[2]]
        if len(matches) != 1: raise ValueError()
        return matches[0]
    if name == 'object_set':
        if not isinstance(args[0], dict) or not isinstance(args[1], str): raise ValueError()
        return {**args[0], args[1]: args[2]}
    if name == 'odata_quote':
        if not isinstance(args[0], str): raise ValueError()
        return "'" + args[0].replace("'", "''") + "'"
    if name == 'merge':
        if not all(isinstance(x, dict) for x in args): raise ValueError()
        result = {}
        for value in args: result.update(value)
        return result
    if name == 'all':
        if not isinstance(args[0], list) or not all(type(x) is bool for x in args[0]): raise ValueError()
        return all(args[0])
    if name == 'length':
        if not isinstance(args[0], (list, dict, str)): raise ValueError()
        return len(args[0])
    if name == 'concat': return ''.join(str(x) for x in args)
    if name == 'join':
        if not isinstance(args[0], str) or not isinstance(args[1], list): raise ValueError()
        return args[0].join(str(x) for x in args[1])
    if name == 'coalesce': return next((x for x in args if x is not None), None)
    if name == 'add': return sum(_number(x) for x in args)
    if name == 'multiply': return _number(args[0]) * _number(args[1])
    if name == 'sum':
        if not isinstance(args[0], list): raise ValueError()
        return sum(_number(x) for x in args[0])
    if name == 'number': return _number(args[0])
    if name == 'string': return str(args[0])
    if name == 'round':
        if type(args[1]) is not int or not -10 <= args[1] <= 10: raise ValueError()
        return round(_number(args[0]), args[1])
    if name == 'json': return json.dumps(args[0], ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    if name == 'date_add_days':
        if type(args[1]) is not int or abs(args[1]) > 36600: raise ValueError()
        return (date.fromisoformat(args[0]) + timedelta(days=args[1])).isoformat()
    if name == 'eq': return args[0] == args[1]
    raise CallError('Unsupported mapping function')


def bind(template, arguments, steps):
    validate_template(template, steps)
    budget = [20000]
    def resolve(value, item=None):
        budget[0] -= 1
        if budget[0] < 0: raise CallError('Mapping work budget exceeded')
        if isinstance(value, list): return [resolve(x, item) for x in value]
        if not isinstance(value, dict): return value
        if '$literal' in value: return value['$literal']
        if '$arg' in value: return pointer(arguments, value['$arg'])
        if '$step' in value: return pointer(steps[value['$step']], value['pointer'])
        if '$item' in value: return pointer(item, value['$item'])
        if '$get' in value:
            source = resolve(value['$get'], item)
            try: return pointer(source, value['pointer'])
            except CallError: return resolve(value['default'], item)
        if '$if' in value:
            condition = resolve(value['$if'], item)
            if type(condition) is not bool: raise CallError('$if needs a boolean condition')
            return resolve(value['then'] if condition else value['else'], item)
        if '$map' in value:
            source = resolve(value['$map'], item)
            if not isinstance(source, list) or len(source) > 1000: raise CallError('$map needs at most 1000 rows')
            return [resolve(value['value'], row) for row in source]
        if '$fn' in value:
            try:
                result = _function(value['$fn'], [resolve(x, item) for x in value['args']])
                if isinstance(result, float) and not math.isfinite(result): raise ValueError()
                if isinstance(result, str) and len(result) > 1_000_000: raise ValueError()
                return result
            except (ValueError, TypeError, OverflowError):
                raise CallError('Invalid operands for mapping function ' + value['$fn']) from None
        return {key: resolve(child, item) for key, child in value.items()}
    return resolve(template)
