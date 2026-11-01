"""Public Python entry point for the same tools exposed by the MCP server."""
import copy

from .client import Client, CallError
from .contracts import load_contract_engine
from .packs import PackReader
from .runtime import load_runtime


class Product:
    def __init__(self, profile, **client_options):
        self.client = Client(profile, **client_options)
        try:
            self.adapters = [a for a in (load_contract_engine(self.client), PackReader(self.client), load_runtime(self.client)) if a]
            names = [d['name'] for d in self.tools()]
            if len(names) != len(set(names)): raise CallError('Duplicate public tool names; resolve the integration configuration')
        except Exception:
            self.client.close()
            raise

    def tools(self):
        result = []
        for operation in self.client.operations.values():
            if operation.get('internal'): continue
            schema = copy.deepcopy(operation['input_schema'])
            read_only = operation.get('read_only', False)
            if not read_only:
                schema.setdefault('properties', {})['_confirm'] = {'type':'boolean','default':False,
                    'description':'True sends the reviewed write once. False only previews.'}
            result.append({'name':operation['name'],'description':operation['description'],
                           'read_only':read_only,'input_schema':schema})
        return result + [d for a in self.adapters for d in a.definitions()]

    def call(self, name, arguments=None):
        arguments = dict(arguments or {})
        for adapter in self.adapters:
            if any(d['name'] == name for d in adapter.definitions()): return adapter.call(name, arguments)
        if name not in self.client.operations or self.client.operations[name].get('internal'):
            raise CallError('Tool is not published; trial and install a reviewed pack first')
        confirm = arguments.pop('_confirm', False)
        return self.client.call(name, arguments, confirm=confirm)

    def close(self): self.client.close()
    def __enter__(self): return self
    def __exit__(self, *args): self.close()
