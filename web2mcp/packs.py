"""Portable JSON integration packs: aliases, read recipes and guarded writes."""
import copy
import re
from pathlib import Path

import jsonschema

from .client import CallError
from .contracts import assert_checks, check_contract, check_deadline
from .evidence import fingerprint
from .expressions import bind, validate_template
from .storage import load_json, save_private


def _name(value):
    if not isinstance(value, str) or not re.fullmatch('[a-zA-Z][a-zA-Z0-9_]{0,25}', value):
        raise CallError('Pack and recipe names must be identifiers of at most 26 characters')
    return value


def _schema(value):
    try: jsonschema.Draft202012Validator.check_schema(value)
    except jsonschema.SchemaError: raise CallError('Invalid pack JSON schema') from None
    if not isinstance(value, dict) or value.get('type') != 'object':
        raise CallError('Tool inputs must be object schemas')


def _validate(value, schema):
    try: jsonschema.validate(value, schema)
    except jsonschema.ValidationError as error:
        raise CallError('Recipe value invalid: ' + str(error.validator)) from None


def _op_hash(client, names):
    return fingerprint({name: client.operations[name] for name in sorted(names)})


def _tool_names(pack):
    return [r['name'] for r in pack['reads']] + [c['name']+'_'+suffix for c in pack['contracts'] for suffix in ('prepare', 'submit', 'status')]


def compile_pack(client, pack):
    """Resolve aliases against THIS profile; a pack never carries credentials or URLs."""
    if not isinstance(pack, dict) or pack.get('version') != 1:
        raise CallError('Unsupported integration pack version')
    allowed = {'version', 'name', 'description', 'bindings', 'reads', 'contracts', 'trials'}
    if set(pack) - allowed: raise CallError('Unknown integration pack keys')
    name = _name(pack.get('name'))
    bindings = pack.get('bindings', {})
    if not isinstance(bindings, dict) or not bindings or len(bindings) > 100:
        raise CallError('Pack needs 1..100 observed operation bindings')
    for alias, operation in bindings.items():
        _name(alias)
        if not isinstance(operation, str) or operation not in client.operations:
            raise CallError('Pack binding has no observed operation: ' + alias)
    reads, contracts = copy.deepcopy(pack.get('reads', [])), copy.deepcopy(pack.get('contracts', []))
    if not isinstance(reads, list) or not isinstance(contracts, list) or not 1 <= len(reads)+len(contracts) <= 40:
        raise CallError('Pack needs 1..40 recipes/contracts')
    local_names = [_name(recipe.get('name')) for recipe in reads + contracts]
    if len(local_names) != len(set(local_names)): raise CallError('Recipe names must be unique within a pack')
    used = set()
    def resolve(step, read_only):
        alias = step.get('operation')
        if alias not in bindings: raise CallError('Recipe references an unbound alias')
        step['operation'] = bindings[alias]; used.add(step['operation'])
        if client.operations[step['operation']].get('read_only') is not read_only:
            raise CallError('Operation read/write classification does not match the recipe')
    for recipe in reads:
        recipe['name'] = name + '__' + _name(recipe.get('name'))
        _schema(recipe.get('input_schema'))
        if 'output' not in recipe or 'output_schema' not in recipe: raise CallError('Read recipe needs output and output_schema')
        try: jsonschema.Draft202012Validator.check_schema(recipe['output_schema'])
        except jsonschema.SchemaError: raise CallError('Invalid output schema') from None
        if not isinstance(recipe.get('steps'), list) or not 1 <= len(recipe['steps']) <= 20:
            raise CallError('Read recipe needs 1..20 steps')
        known = set()
        for step in recipe['steps']:
            ident = _name(step.get('id'))
            if ident in known: raise CallError('Duplicate recipe step')
            resolve(step, True)
            if not step.get('assertions'): raise CallError('Read steps need explicit assertions')
            validate_template(step.get('arguments', {}), known)
            validate_template(step['assertions'], known | {'_response'})
            known.add(ident)
        validate_template(recipe['output'], known)
    for contract in contracts:
        local_name = _name(contract.get('name'))
        contract['name'] = name + '__' + local_name
        # Existing contracts limit names to 70; retain that bound for filenames/tools.
        _schema(contract.get('input_schema'))
        for step in contract.get('preflight', []) + contract.get('verify', []): resolve(step, True)
        resolve(contract.get('write', {}), False)
        check_contract(contract, client)
        op_names = {s['operation'] for s in contract['preflight']+contract['verify']} | {contract['write']['operation']}
        contract['operations_hash'] = _op_hash(client, op_names)
        contract['reviewed'] = True
    result = {'version': 1, 'name': name, 'description': pack.get('description', name),
              'source_hash': fingerprint(pack), 'operations_hash': _op_hash(client, used),
              'operations': sorted(used), 'reads': reads, 'contracts': contracts}
    names = _tool_names(result)
    if len(names) != len(set(names)): raise CallError('Duplicate pack tool names')
    if set(names) & set(client.operations): raise CallError('Pack tool collides with a raw operation')
    return result


def installed_packs(client):
    for path in sorted((client.root/'packs').glob('*.json')):
        pack = load_json(path)
        if not pack.get('reviewed'): continue
        if any(n not in client.operations for n in pack['operations']) or _op_hash(client, pack['operations']) != pack['operations_hash']:
            raise CallError('Observed operations changed; re-trial and install pack ' + pack['name'])
        yield path, pack


def _run_read(client, recipe, arguments):
    _validate(arguments, recipe['input_schema'])
    steps = {}
    for step in recipe['steps']:
        response = client.call(step['operation'], bind(step.get('arguments', {}), arguments, steps))
        check_deadline(response, step, arguments, steps)
        assert_checks(response, step['assertions'], arguments, steps)
        steps[step['id']] = response
    output = bind(recipe['output'], arguments, steps)
    _validate(output, recipe['output_schema'])
    return output


def trial_pack(client, pack):
    """Only read/prepare here. Never send business writes during an installation trial."""
    compiled = compile_pack(client, pack)
    trials = pack.get('trials', {})
    results = []
    for recipe in compiled['reads'] + compiled['contracts']:
        short = recipe['name'][len(compiled['name'])+2:]
        cases = trials.get(short, [])
        if not isinstance(cases, list) or not 1 <= len(cases) <= 20: raise CallError('Supply 1..20 trial cases for ' + short)
        if recipe['input_schema'].get('properties') and len({fingerprint(c.get('arguments', {})) for c in cases}) < 2:
            raise CallError('Parameterized recipes need two distinct trial arguments: ' + short)
        for case in cases:
            arguments = case.get('arguments', {})
            _validate(arguments, recipe['input_schema'])
            if not case.get('assertions'): raise CallError('Trial cases need expected-result assertions')
            if 'steps' in recipe:
                output = _run_read(client, recipe, arguments)
            else:
                steps = {}
                for step in recipe['preflight']:
                    response = client.call(step['operation'], bind(step.get('arguments', {}), arguments, steps))
                    check_deadline(response, step, arguments, steps)
                    assert_checks(response, step['assertions'], arguments, steps)
                    steps[step['id']] = response
                output = bind(recipe['write']['arguments'], arguments, steps)
                client._request(recipe['write']['operation'], output)  # validates; does NOT send
                bind(recipe['lock_key'], arguments, steps)
            assert_checks(output, case['assertions'], arguments)
        results.append({'tool': recipe['name'], 'cases': len(cases), 'mode': 'read' if 'steps' in recipe else 'prepare_only'})
    receipt = {'pack': compiled['name'], 'source_hash': compiled['source_hash'],
               'operations_hash': compiled['operations_hash'], 'passed': True, 'results': results,
               'business_writes_sent': 0}
    save_private(client.root/'pack_trials'/(compiled['name']+'.json'), receipt)
    return receipt


def install_pack(client, pack, *, reviewed=False):
    compiled = compile_pack(client, pack)
    if reviewed is not True: raise CallError('Review the pack semantics before installation')
    trial_path = client.root/'pack_trials'/(compiled['name']+'.json')
    if not trial_path.exists(): raise CallError('Run trial-pack before installation')
    receipt = load_json(trial_path)
    if not receipt.get('passed') or any(receipt[k] != compiled[k] for k in ('source_hash', 'operations_hash')):
        raise CallError('Pack or operations changed since the successful trial')
    # Reject collisions with legacy contracts and other packs before atomic publication.
    occupied = set()
    for path in (client.root/'contracts').glob('*.json'):
        contract = load_json(path)
        occupied.update(contract['name']+'_'+suffix for suffix in ('prepare', 'submit', 'status'))
    for path in (client.root/'packs').glob('*.json'):
        other = load_json(path)
        if other['name'] != compiled['name']: occupied.update(_tool_names(other))
    if occupied & set(_tool_names(compiled)): raise CallError('Pack tools collide with installed tools')
    compiled['reviewed'] = True
    save_private(client.root/'packs'/(compiled['name']+'.json'), compiled)
    return {'pack': compiled['name'], 'tools': _tool_names(compiled), 'reconnect_required': True,
            'business_writes_sent': 0, 'trial': receipt['results']}


class PackReader:
    def __init__(self, client):
        self.client = client
        self.recipes = {}; self.sources = {}
        for path, pack in installed_packs(client):
            for recipe in pack['reads']:
                self.recipes[recipe['name']] = recipe
                self.sources[recipe['name']] = (path, fingerprint(pack), pack)

    def definitions(self):
        return [{'name': r['name'], 'description': r.get('description', r['name']),
                 'read_only': True, 'input_schema': r['input_schema']} for r in self.recipes.values()]

    def call(self, name, arguments):
        if name not in self.recipes: raise CallError('Unknown pack read tool')
        path, digest, pack = self.sources[name]
        if fingerprint(load_json(path)) != digest or _op_hash(self.client, pack['operations']) != pack['operations_hash']:
            raise CallError('Pack or operation changed; reconnect and re-trial')
        return _run_read(self.client, self.recipes[name], arguments)


def draft_pack(client, name):
    """Mechanical starting point; the connected AI must add semantics and trial expectations."""
    _name(name)
    bindings = {}; reads = []
    for index, operation in enumerate(client.operations.values()):
        alias = 'op_' + str(index + 1)
        bindings[alias] = operation['name']
        if operation.get('read_only') is not True: continue
        reads.append({'name': 'read_'+alias, 'description': 'DRAFT: review business meaning and map useful output',
                      'input_schema': copy.deepcopy(operation['input_schema']),
                      'steps': [{'id': 'result', 'operation': alias, 'arguments': {'$arg': ''},
                                 'assertions': [{'pointer': '/status', 'op': 'eq', 'value': 200}]}],
                      'output': {'$step': 'result', 'pointer': '/data'},
                      'output_schema': copy.deepcopy(operation.get('response_schema', {}))})
    return {'version': 1, 'name': name, 'description': 'Draft only: add business assertions, mappings and trial cases.',
            'bindings': bindings, 'reads': reads, 'contracts': [], 'trials': {r['name']: [] for r in reads}}
