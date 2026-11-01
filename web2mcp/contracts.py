"""Declarative, bounded execution with preconditions, pinned state and read-back checks."""
import json
import os
import re
import time
import uuid
import copy
from datetime import datetime, timezone, timedelta
from pathlib import Path

import jsonschema

from .client import CallError
from .audit import redact
from .evidence import fingerprint
from .storage import load_json, save_private


from .expressions import bind, pointer, validate_template


def assert_checks(response, checks, arguments=None, steps=None):
    context = {**(steps or {}), '_response': response}
    for check in checks:
        expected = bind(check.get('value'), arguments or {}, context)
        op = check.get('op', 'eq')
        if op not in ('eq', 'ne', 'in', 'exists', 'gt', 'ge', 'lt', 'le'):
            raise CallError('Unsupported contract assertion')
        try:
            actual = bind(check['actual'], arguments or {}, context) if 'actual' in check else pointer(response, check['pointer'])
            exists = True
        except CallError:
            exists = False; actual = None
        try:
            passed = (exists == bool(expected)) if op == 'exists' else exists and {
                'eq': lambda: actual == expected, 'ne': lambda: actual != expected,
                'in': lambda: actual in expected, 'gt': lambda: actual > expected,
                'ge': lambda: actual >= expected, 'lt': lambda: actual < expected,
                'le': lambda: actual <= expected}[op]()
        except (TypeError, ValueError): passed = False
        if not passed:
            raise CallError('Contract check failed at ' + check.get('pointer', 'computed assertion'), reason='precondition_or_verification')


def check_deadline(response, definition, arguments=None, steps=None):
    """Deadline is read from the server and rechecked at prepare and submit."""
    path = definition.get('deadline_pointer')
    expression = definition.get('deadline_actual')
    if path is None and expression is None:
        if 'deadline_actual' in definition: raise CallError('Missing deadline expression')
        return
    if path is not None and expression is not None:
        raise CallError('Specify one deadline source')
    try:
        value = pointer(response, path) if path is not None else bind(expression, arguments or {}, {**(steps or {}), '_response':response})
        deadline = datetime.fromisoformat(value)
        now = datetime.now(timezone.utc)
        if deadline.tzinfo is None or deadline <= now: raise ValueError()
        maximum = definition.get('max_remaining_seconds')
        if maximum is not None and (deadline - now).total_seconds() > maximum:
            raise CallError('Workflow deadline is outside the allowed action window', reason='precondition_or_verification')
    except (TypeError, ValueError):
        raise CallError('Workflow deadline expired or invalid', reason='precondition_or_verification') from None


def check_contract(contract, client):
    required = {'version', 'name', 'input_schema', 'preflight', 'write', 'verify', 'lock_key'}
    if not isinstance(contract, dict) or not required <= contract.keys() or contract['version'] != 1:
        raise CallError('Invalid contract structure/version')
    if not re.fullmatch('[a-zA-Z][a-zA-Z0-9_]{0,69}', contract['name']): raise CallError('Invalid contract name')
    if 'lock_namespace' in contract and (not isinstance(contract['lock_namespace'], str) or not re.fullmatch('[a-zA-Z][a-zA-Z0-9_]{0,69}', contract['lock_namespace'])):
        raise CallError('Invalid contract lock namespace')
    jsonschema.Draft202012Validator.check_schema(contract['input_schema'])
    if not contract['preflight'] or not contract['verify'] or len(contract['preflight'])+len(contract['verify']) > 20:
        raise CallError('Contract must have bounded preflight and verification steps')
    ids = set()
    for step in contract['preflight'] + contract['verify']:
        if not isinstance(step.get('id'), str) or not step['id'] or step['id'] == 'write' or step['id'] in ids:
            raise CallError('Step IDs must be unique and cannot use reserved write ID')
        ids.add(step['id'])
        operation = client.operations.get(step.get('operation'))
        if not operation or operation.get('read_only') is not True: raise CallError('Checks must call reviewed read-only operations')
        if not step.get('assertions'): raise CallError('Every check step needs explicit assertions')
    write = contract['write']; operation = client.operations.get(write.get('operation'))
    if not operation or operation.get('read_only') is not False: raise CallError('Write operation must be explicitly marked write')
    allow = write.get('allowed_body_fields')
    if write.get('bodyless') is True:
        # Some reviewed workflow actions use GET/DELETE without a body. They
        # still require the same preview, resource lock and read-back checks.
        arguments = write.get('arguments')
        if not isinstance(arguments, dict) or 'body' in arguments or operation.get('body') is not None or allow != []:
            raise CallError('Bodyless write must omit the body and allow no body fields')
    else:
        if not isinstance(write.get('arguments', {}).get('body'), dict): raise CallError('Write must specify a complete object body')
        if not isinstance(allow, list) or set(write['arguments']['body']) != set(allow):
            raise CallError('Write body must exactly match allowed_body_fields')
        if any(key.startswith('$') for key in write['arguments']['body']):
            raise CallError('Write body must declare literal top-level field names')
    if not contract['lock_key']: raise CallError('A stable resource lock_key is required')
    known = set()
    for phase in ('preflight', 'verify'):
        if phase == 'verify':
            validate_template(write['arguments'], known)
            validate_template(contract['lock_key'], known)
            known.add('write')
        for step in contract[phase]:
            validate_template(step.get('arguments', {}), known)
            validate_template(step['assertions'], known | {'_response'})
            if step.get('id') == '_response': raise CallError('Reserved assertion response ID')
            if 'deadline_pointer' in step and (not isinstance(step['deadline_pointer'], str) or not step['deadline_pointer'].startswith('/')):
                raise CallError('Invalid deadline pointer')
            if 'deadline_actual' in step:
                if 'deadline_pointer' in step: raise CallError('Specify one deadline source')
                if step['deadline_actual'] is None: raise CallError('Missing deadline expression')
                validate_template(step['deadline_actual'], known | {'_response'})
            if 'max_remaining_seconds' in step and (not {'deadline_pointer','deadline_actual'} & step.keys() or type(step['max_remaining_seconds']) is not int or not 1 <= step['max_remaining_seconds'] <= 366 * 86400):
                raise CallError('Invalid maximum deadline window')
            known.add(step['id'])
    attempts = contract.get('verification_attempts', 1)
    interval = contract.get('verification_interval_seconds', 1)
    if type(attempts) is not int or not 1 <= attempts <= 6:
        raise CallError('Verification attempts must be between 1 and 6')
    if type(interval) not in (int, float) or not 0 <= interval <= 5:
        raise CallError('Verification interval must be between 0 and 5 seconds')
    if 'verify_on_status' in contract and type(contract['verify_on_status']) is not bool:
        raise CallError('verify_on_status must be a boolean')
    rules = contract.get('argument_date_rules', [])
    if not isinstance(rules, list):
        raise CallError('Argument date rules must be a list')
    for rule in rules:
        if (not isinstance(rule, dict) or set(rule) != {'pointer', 'utc_offset_minutes', 'days_from_today'}
            or not isinstance(rule['pointer'], str) or not rule['pointer'].startswith('/')
            or type(rule['utc_offset_minutes']) is not int or not -720 <= rule['utc_offset_minutes'] <= 840
            or type(rule['days_from_today']) is not int or not -366 <= rule['days_from_today'] <= 366):
            raise CallError('Invalid argument date rule')
    return contract


def check_argument_dates(contract, arguments):
    for rule in contract.get('argument_date_rules', []):
        zone = timezone(timedelta(minutes=rule['utc_offset_minutes']))
        expected = (datetime.now(zone).date() + timedelta(days=rule['days_from_today'])).isoformat()
        if pointer(arguments, rule['pointer']) != expected:
            raise CallError('Application date changed; prepare again with the current date', reason='precondition_or_verification')


def install_contract(client, contract, *, reviewed=False):
    check_contract(contract, client)
    value = dict(contract)
    # Keep the reviewed execution environment as part of the contract identity.
    value['operations_hash'] = fingerprint({name: client.operations[name] for name in
        {step['operation'] for step in contract['preflight']+contract['verify']} | {contract['write']['operation']}})
    value['reviewed'] = reviewed is True
    save_private(client.root/'contracts'/(contract['name']+'.json'), value)
    return {'contract': contract['name'], 'reviewed': value['reviewed'], 'sha256': fingerprint(value)}


class ContractEngine:
    def __init__(self, client):
        self.client = client
        self.private_memory = client.manifest.get('contract_privacy') == 'metadata_only'
        if self.private_memory and not client.manifest.get('sensitive_responses'):
            raise CallError('Metadata-only contracts require sensitive response protection')
        self.memory_plans = {}
        self.memory_receipts = {}
        self.contracts = {}
        self.pack_sources = {}
        for path in sorted((client.root/'contracts').glob('*.json')):
            contract = check_contract(load_json(path), client)
            if contract.get('reviewed'): self.contracts[contract['name']] = contract
        from .packs import installed_packs
        for path, pack in installed_packs(client):
            for contract in pack['contracts']:
                name = contract['name']
                if name in self.contracts: raise CallError('Duplicate contract tool name')
                self.contracts[name] = contract
                self.pack_sources[name] = (path, fingerprint(pack))

    def definitions(self):
        result = []
        for name, contract in self.contracts.items():
            result += [
                {'name': name+'_prepare', 'description': contract.get('description', name)+'; validate and preview without writing.', 'read_only': True, 'input_schema': contract['input_schema']},
                {'name': name+'_submit', 'description': 'Submit a reviewed preview once, then verify business state. No automatic retries.', 'read_only': False,
                 'input_schema': {'type':'object', 'properties':{'preview_id':{'type':'string','pattern':'^[a-f0-9]{32}$'},'confirm':{'type':'boolean','const':True}}, 'required':['preview_id','confirm'],'additionalProperties':False}},
                {'name': name+'_status', 'description': ('Read execution outcome and verify current business state without retrying the write.' if contract.get('verify_on_status') else 'Read local execution outcome for a preview; does not retry or change business state.'), 'read_only':True,
                 'input_schema': {'type':'object','properties':{'preview_id':{'type':'string','pattern':'^[a-f0-9]{32}$'}},'required':['preview_id'],'additionalProperties':False}},
            ]
        return result

    def call(self, name, arguments):
        definition = next((item for item in self.definitions() if item['name'] == name), None)
        if definition is None: raise CallError('Unknown contract tool')
        try: jsonschema.validate(arguments, definition['input_schema'])
        except jsonschema.ValidationError as error: raise CallError('Contract arguments invalid: '+error.validator) from None
        base, action = name.rsplit('_', 1)
        if action == 'prepare': return self.prepare(base, arguments)
        if action == 'submit': return self.submit(base, arguments['preview_id'])
        return self.status(base, arguments['preview_id'])

    def _contract(self, name):
        contract = self.contracts[name]
        if name in self.pack_sources:
            path, digest = self.pack_sources[name]
            if fingerprint(load_json(path)) != digest: raise CallError('Pack changed; reconnect and prepare again')
        else:
            current = load_json(self.client.root/'contracts'/(name+'.json'))
            if fingerprint(current) != fingerprint(contract): raise CallError('Contract changed; reconnect and prepare again')
        op_names = {step['operation'] for step in contract['preflight']+contract['verify']} | {contract['write']['operation']}
        if fingerprint({name: self.client.operations[name] for name in op_names}) != contract['operations_hash']:
            raise CallError('Operation definitions changed since contract review')
        return contract

    def _steps(self, definitions, arguments, steps=None):
        steps = dict(steps or {}); pinned = {}
        for definition in definitions:
            response = self.client.call(definition['operation'], bind(definition.get('arguments', {}), arguments, steps))
            check_deadline(response, definition, arguments, steps)
            assert_checks(response, definition['assertions'], arguments, steps)
            steps[definition['id']] = response
            paths = definition.get('pin', ['/data'])
            pinned[definition['id']] = {path:pointer(response,path) for path in paths}
        return steps, fingerprint(pinned)

    def prepare(self, name, arguments):
        contract = self._contract(name)
        if self.private_memory and len(self.memory_plans) >= 256:
            raise CallError('Private preview budget reached; reconnect before preparing more')
        arguments = copy.deepcopy(arguments)
        check_argument_dates(contract, arguments)
        steps, state_hash = self._steps(contract['preflight'], arguments)
        write_args = bind(contract['write']['arguments'], arguments, steps)
        self.client._request(contract['write']['operation'], write_args)  # schema/scope check only
        lock_value = bind(contract['lock_key'], arguments, steps)
        # One logical business action per stable lock key, including unknown outcomes.
        # For recurring actions include an explicit cycle/idempotency ID in lock_key.
        lock_hash = fingerprint({'contract':contract.get('lock_namespace', name), 'resource':lock_value})
        ident = uuid.uuid4().hex
        plan = {'preview_id':ident, 'contract':name, 'contract_hash':fingerprint(contract), 'created_at':time.time(),
                'arguments':arguments, 'preflight_hash':state_hash, 'write_arguments':write_args, 'lock_hash':lock_hash}
        if contract.get('verify_on_status'):
            # Private evidence for later read-only reconciliation. Never rerun
            # preflight against the now-completed node or replay its write.
            plan['verification_context'] = steps
        if self.private_memory:
            self.memory_plans[ident] = plan
            persisted = {k:plan[k] for k in ('preview_id','contract','contract_hash','created_at','preflight_hash','lock_hash')}
            persisted.update(privacy='metadata_only', arguments_hmac=self.client.audit.arguments_hash(arguments))
        else: persisted = plan
        save_private(self.client.root/'contract_plans'/(ident+'.json'), persisted)
        return {'preview_id':ident, 'ready':True, 'expires_in_seconds':900, 'operation':contract['write']['operation'],
                'write_arguments':copy.deepcopy(write_args), 'verification_steps':[step['id'] for step in contract['verify']]}

    def _plan(self, name, ident):
        if not re.fullmatch('[a-f0-9]{32}', ident): raise CallError('Invalid preview ID')
        path = self.client.root/'contract_plans'/(ident+'.json')
        if not path.exists(): raise CallError('Preview not found')
        plan = load_json(path)
        if plan['contract'] != name: raise CallError('Preview belongs to another contract')
        if plan.get('privacy') == 'metadata_only' and ident in self.memory_plans:
            return self.memory_plans[ident]
        return plan

    def _save_result(self, journal, result):
        if self.private_memory:
            error = result.get('error')
            allowed = ('preview_id','state','business_verified','readback_verified','automatic_retry',
                       'write_response_received','http_status')
            result = {k:v for k,v in result.items() if k in allowed}
            if isinstance(error, dict):
                result['error'] = {k:error[k] for k in ('reason','outcome','http_status','call_id') if k in error}
        save_private(journal, result)

    def submit(self, name, ident):
        contract = self._contract(name); plan = self._plan(name, ident)
        if 'arguments' not in plan:
            raise CallError('Private preview is no longer in memory; check status, never replay an attempted write', reason='private_preview_lost')
        check_argument_dates(contract, plan['arguments'])
        if time.time()-plan['created_at'] > 900 or fingerprint(contract) != plan['contract_hash']:
            raise CallError('Preview expired or contract changed')
        steps, state_hash = self._steps(contract['preflight'], plan['arguments'])
        if state_hash != plan['preflight_hash'] or bind(contract['write']['arguments'], plan['arguments'], steps) != plan['write_arguments']:
            raise CallError('Business state changed since preview; prepare again')
        journal = self.client.root/'contract_submissions'/(plan['lock_hash']+'.json')
        journal.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try: fd=os.open(journal, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
        except FileExistsError: raise CallError('Already attempted; check status, do not retry', reason='already_attempted') from None
        with os.fdopen(fd,'w') as out:
            json.dump({'preview_id':ident,'state':'sending_or_unknown'},out);out.flush();os.fsync(out.fileno())
        write_response_received = False
        try:
            response = self.client.call(contract['write']['operation'], plan['write_arguments'], confirm=True)
            write_response_received = True
            # Keep a private receipt so an HTTP 200 business error can be
            # diagnosed without replaying the write. Credential fields are masked.
            if self.private_memory: self.memory_receipts[ident] = response
            else:
                save_private(self.client.root/'diagnostics'/('contract-'+ident+'.json'),
                             {'operation':contract['write']['operation'], 'response':redact(response)})
            save_private(journal, {'preview_id':ident,'state':'response_received','http_status':response['status']})
            steps['write'] = response
            # Some workflows persist their result asynchronously. Only re-read
            # failed business assertions; never repeat the write or an HTTP error.
            for attempt in range(contract.get('verification_attempts', 1)):
                try:
                    verified, _ = self._steps(contract['verify'], plan['arguments'], steps)
                    break
                except CallError as error:
                    if error.reason != 'precondition_or_verification' or attempt + 1 >= contract.get('verification_attempts', 1):
                        raise
                    time.sleep(contract.get('verification_interval_seconds', 1))
            result = {'preview_id':ident,'state':'verified','business_verified':True,
                      'verification':{step['id']:verified[step['id']] for step in contract['verify']}}
            self._save_result(journal,result)
            return result
        except Exception as error:
            state = {'preview_id':ident,'state':'needs_check','business_verified':False,'automatic_retry':False,
                     'write_response_received':write_response_received,
                     'error':error.as_dict() if isinstance(error,CallError) else {'reason':'verification_or_internal'}}
            if write_response_received:
                # A failed read-back assertion does not mean the preceding write
                # was never sent. Preserve that distinction for callers.
                state['error']['outcome'] = 'write_response_received'
            self._save_result(journal,state)
            raise CallError('Submission needs read-back review; not safe to repeat', reason='verification_or_unknown',
                            outcome='write_response_received' if write_response_received else 'unknown') from None

    def status(self, name, ident):
        plan = self._plan(name, ident)
        journal = self.client.root/'contract_submissions'/(plan['lock_hash']+'.json')
        if not journal.exists(): return {'preview_id':ident,'state':'not_submitted'}
        result = load_json(journal)
        contract = self._contract(name)
        if not contract.get('verify_on_status'): return result
        if result.get('preview_id') != ident:
            return {**result, 'requested_preview_id':ident, 'automatic_retry':False,
                    'readback_error':{'reason':'another_preview_owns_submission'}}
        if plan.get('privacy') == 'metadata_only':
            return {**result, 'previous_state':result.get('state'), 'state':'needs_check',
                    'readback_verified':False, 'business_verified':False, 'automatic_retry':False,
                    'readback_error':{'reason':'private_context_lost_read_record_directly'}}
        if plan['contract_hash'] != fingerprint(contract) or 'verification_context' not in plan:
            raise CallError('Reconciliation evidence changed; read the record directly, do not resubmit')
        context = dict(plan['verification_context'])
        receipt = self.client.root/'diagnostics'/('contract-'+ident+'.json')
        if self.private_memory:
            if ident in self.memory_receipts: context['write'] = self.memory_receipts[ident]
        elif receipt.exists(): context['write'] = load_json(receipt)['response']
        try:
            verified, _ = self._steps(contract['verify'], plan['arguments'], context)
        except CallError as error:
            # Failure is not evidence of a safe retry, even if HTTP says 404.
            return {**result, 'previous_state':result.get('state'), 'state':'needs_check',
                    'business_verified':False, 'readback_verified':False, 'automatic_retry':False,
                    'readback_error':error.as_dict()}
        result = {'preview_id':ident,'state':'verified','business_verified':True,
                  'readback_verified':True,'automatic_retry':False,
                  'verification':{s['id']:verified[s['id']] for s in contract['verify']}}
        self._save_result(journal,result)
        return result


def load_contract_engine(client):
    return ContractEngine(client) if (client.root/'contracts').exists() or (client.root/'packs').exists() else None
