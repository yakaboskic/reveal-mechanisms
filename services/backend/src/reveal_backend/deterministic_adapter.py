"""Explicit development adapter. Output describes a test, never a live finding."""
import asyncio
import json
from .agent_execution import ExecutionResult
from .evidence_package import canonical_json

class DeterministicAdapter:
    async def execute(self, request, emit, cancelled, checkpoint):
        request.output_dir.mkdir(parents=True,exist_ok=True)
        await emit('warning',{'message':'DEVELOPMENT SIMULATION: this validates application plumbing and is not a live scientific result.'})
        await emit('agent_started',{'message':'Starting deterministic development validation.'})
        for message in ('Reading the frozen input.','Checking explicit source lineage.','Preparing the development result.'):
            if await cancelled(): return ExecutionResult('cancelled',request.output_dir)
            await emit('agent_message',{'message':message}); await asyncio.sleep(0.4)
        inputs=json.loads(request.input_path.read_text())
        if request.kind=='paragraph':
            account=inputs['account_document']['scientific_accounts'][0]
            content={'format':'reveal.paragraph-output/1','segments':[{'text':account['closing_remarks'],'citations':inputs['allowed_citations'][:1]}]}
            path=request.output_dir/'paragraph.json'; path.write_bytes(canonical_json(content))
            return ExecutionResult('succeeded',request.output_dir,paragraph_path=path)
        package=inputs; gap=package['dapper_context']['knowledge_gaps'][0]
        source=next((s['dapper_file_id'] for s in package['source_artifacts'].values() if isinstance(s.get('origin'),str) and s['origin'].startswith(('https://dev.cfdeknowledge.org/api/','https://cfde-dev.hugeampkpnbi.org/api/'))),None)
        if not source: return ExecutionResult('failed',request.output_dir,reason='No captured CFDE artifact is available to validate lineage.')
        draft={'prefixes':package['prefixes'],'knowledge_gaps':[gap],
            'propositions':[{'id':'urn:reveal:dev:proposition','statement':'This development run verifies that the selected source capture can be carried through the application. It does not establish a biological mechanism.','proposition_kind':'BIOLOGICAL_INTERPRETATION'}],
            'evidence_items':[{'id':'urn:reveal:dev:evidence','target_proposition':'urn:reveal:dev:proposition','direction':'SUPPORTS','context':'Development transport validation only.','explanation':'The captured CFDE file provides source lineage for the application test; it is not interpreted as a scientific finding.','was_derived_from':[source]}],
            'claims':[{'id':'urn:reveal:dev:claim','proposition':'urn:reveal:dev:proposition','statement':'Development simulation: evidence capture and application persistence were exercised.','direction':'SUPPORTS','status':'proposed','has_evidence':['urn:reveal:dev:evidence']}],
            'scientific_accounts':[{'id':'urn:reveal:dev:account','name':'Development simulation — not a scientific result','question':gap['id'],'context':'Deterministic application acceptance test.','component_claims':['urn:reveal:dev:claim'],'closing_remarks':'Development simulation complete. The selected question and captured evidence passed through the application. No live agent reviewed the evidence, and no biological conclusion should be drawn from this test.'}]}
        path=request.output_dir/'account-1.json'; path.write_bytes(canonical_json(draft))
        return ExecutionResult('succeeded',request.output_dir,account_paths=(path,))
