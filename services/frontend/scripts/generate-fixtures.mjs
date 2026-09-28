import { readFile, writeFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
const bytes = await readFile(new URL('../../../api/openapi.json', import.meta.url));
const spec = JSON.parse(bytes);
const response = (path, method = 'get') => Object.values(spec.paths[path][method].responses['200'].content['application/json'].examples)[0].value;
const value = { notice: 'DEVELOPMENT FIXTURE: invented KG assertions, not a live scientific result.', contractSha256: createHash('sha256').update(bytes).digest('hex'),
 gaps: response('/v1/knowledge-gaps'), gapAccounts: response('/v1/knowledge-gaps/{gap_id}/accounts'), suggestions: response('/v1/mechanisms/suggest', 'post'), account: response('/v1/accounts/{dapper_id}'), claim: response('/v1/claims/{dapper_id}'), paragraph: response('/v1/paragraphs/{dapper_id}'), events: response('/v1/jobs/{job_id}/events') };
await writeFile(new URL('../src/lib/fixtures/contract.json', import.meta.url), JSON.stringify(value, null, 2) + '\n');
