"""Small project-specific OpenAPI 3.0 generator. No third-party build tools."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
spec = json.loads((ROOT / 'openapi.json').read_text())
out = ROOT / 'service/generated'
out.mkdir(parents=True, exist_ok=True)
(out / '__init__.py').write_text('')
header = '# Generated from OpenAPI by scripts/generate.py. Do not edit.\n'
models = [header, 'from __future__ import annotations', 'from typing import Any, Annotated, Literal, Optional', 'from decimal import Decimal', 'from uuid import UUID', 'from pydantic import BaseModel, ConfigDict, Field, AwareDatetime, BeforeValidator', 'from service.common import decimal_number', '']
schemas = dict(spec['components']['schemas'])

def annotation(schema):
    if '$ref' in schema:
        result = schema['$ref'].split('/')[-1]
    elif 'enum' in schema:
        result = 'Literal[' + ', '.join(repr(v) for v in schema['enum']) + ']'
    elif schema['type'] == 'array':
        result = 'list[' + annotation(schema['items']) + ']'
    elif schema['type'] == 'object':
        result = 'dict[str, Any]'
    elif schema.get('format') == 'uuid':
        result = 'UUID'
    elif schema.get('format') == 'date-time':
        result = 'AwareDatetime'
    elif schema.get('format') == 'decimal':
        result = 'Annotated[Decimal, BeforeValidator(decimal_number)]'
    else:
        result = {'string':'str','integer':'int','number':'float','boolean':'bool'}[schema['type']]
    return result

operations=[]
for path, methods in spec['paths'].items():
    for method, operation in methods.items():
        id=operation['operationId']; names={}
        for source in ('query','path'):
            params=[p for p in operation.get('parameters',[]) if p['in']==source]
            if params:
                name=id.title().replace('_','')+source.title()
                props={p['name']:p['schema'] for p in params}
                schemas[name]={'type':'object','properties':props,'required':[p['name'] for p in params if p.get('required')], 'x-query':source=='query'}
                names[source]=name
        if operation.get('requestBody'):
            names['body']=operation['requestBody']['content']['application/json']['schema']['$ref'].split('/')[-1]
        success=next(k for k in operation['responses'] if k.startswith('2'))
        operations.append((path,method,id,names,operation['x-roles'],int(success)))

for name,schema in schemas.items():
    models.extend([f'class {name}(BaseModel):', '    model_config = ConfigDict(extra="forbid")'])
    for field,prop in schema['properties'].items():
        required=field in schema.get('required',[])
        typ=annotation(prop)
        if prop.get('nullable') or (not required and 'default' not in prop): typ=f'Optional[{typ}]'
        constraints=[]
        for key,arg in [('minimum','ge'),('maximum','le'),('minLength','min_length'),('maxLength','max_length'),('minItems','min_length'),('maxItems','max_length'),('pattern','pattern'),('multipleOf','multiple_of')]:
            if key in prop:
                value=f'Decimal({str(prop[key])!r})' if prop.get('format')=='decimal' else repr(prop[key])
                constraints.append(f'{arg}={value}')
        if prop.get('type') in ('string','integer','boolean') and not prop.get('format') and 'enum' not in prop and not schema.get('x-query'):
            constraints.append('strict=True')
        default='...' if required else repr(prop.get('default'))
        models.append(f'    {field}: {typ} = Field({default}'+(' ,'+', '.join(constraints) if constraints else '')+')')
    models.append('')
models.append('for _model in list(globals().values()):\n    if isinstance(_model, type) and issubclass(_model, BaseModel) and _model is not BaseModel:\n        _model.model_rebuild()\n')
(out/'models.py').write_text('\n'.join(models))
routes=[header,'from fastapi import APIRouter, Request','from service.generated import models','','def create_router(dispatch):','    router = APIRouter()','']
for path,method,id,names,roles,status in operations:
    args=', '.join(f'{k}_model=models.{v}' for k,v in names.items())
    routes.extend([f'    @router.api_route({path!r}, methods=[{method.upper()!r}])',
                   f'    async def {id}(request: Request):',
                   f'        return await dispatch(request, {id!r}, roles={roles!r}, status={status}'+(', '+args if args else '')+')', ''])
routes.append('    return router')
(out/'routes.py').write_text('\n'.join(routes))
print(f'Generated {len(schemas)} DTO models and {len(operations)} handlers from OpenAPI.')
