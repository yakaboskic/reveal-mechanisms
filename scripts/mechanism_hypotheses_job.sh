#!/usr/bin/env bash
# From candidate genes and their strongest gene sets in the claims repository to a deposited DAPPER Scientific
# Account of mechanistic hypotheses.
#
#   scripts/mechanism_hypotheses_job.sh reveal-key.json [run-directory]
#
# The companion of scientific_account_job.sh. That one asks whether each candidate gene has published evidence for
# the disease. This one asks how it might act: it takes the same top PIGEAN validation-challenge genes, each with the
# gene sets PIGEAN weighs most for it, and asks a REVEAL agent to propose mechanistic hypotheses anchored on each gene
# and those sets, and to assess each hypothesis from papers it finds and downloads itself.
#
# reveal-key.json is your key handoff: {"api_url": "...", "api_key": "rvl_..."}. Keep it private.
# Needs curl, jq and python3 (3.9+), and agent_job.py beside this file.
#
# One HTTP call per step; each call is printed before it runs:
#   1. GET  {reveal}/v1/me                   check the key and show whose workspace it is
#   2. POST {claims}/v1/graphql              the candidate genes, each with its top gene sets (no key)
#   3. POST {reveal}/v1/jobs                 submit the job (the body is printed and saved as request.json)
#   4. GET  {reveal}/v1/jobs/{id}/events     watch the agent work (agent_job.py --job does the polling)
#   5. GET  {reveal}/v1/jobs/{id}            read the result; stop unless the account was deposited
#   6. GET  {reveal}/v1/jobs/{id}/account    the DAPPER document, which REVEAL rebuilds from the claims repository
#                                            (stop unless its source is claims_repository); saved as account.json
#   7. GET  {claims}/v1/deposits/{id}        read the deposit back from the claims repository (no key)
#      GET  {claims}/v1/objects/{id}         and the account object it stored
#
# EVERY SUCCESSFUL RUN MAKES A PERMANENT DEPOSIT. A deposit cannot be recalled or deleted.
# Running this again with the same run directory does not start a second job: the request and its
# Idempotency-Key are kept there, so the API returns the same job and the script carries on from where it
# stopped. Use a new run directory for a new job.
#
# Change what is asked with environment variables (defaults below):
#   PHENOTYPE, COLLECTION, DISEASE, TOP_GENES, SETS_PER_GENE, HYPOTHESES_PER_GENE, PRIOR_MAX, BUDGET_USD,
#   TOOL_CALLS, CLAIMS_API_URL
set -euo pipefail

HANDOFF=${1:?usage: mechanism_hypotheses_job.sh reveal-key.json [run-directory]}
RUN=${2:-mechanism-run}
HERE=$(cd "$(dirname "$0")" && pwd)

PHENOTYPE=${PHENOTYPE:-IBD}                        # the claims repository's phenotype code
COLLECTION=${COLLECTION:-mouse_msigdb}             # the PIGEAN gene-set library the scores and sets come from
DISEASE=${DISEASE:-inflammatory bowel disease}     # how to name it to the agent
TOP_GENES=${TOP_GENES:-3}                          # candidates, strongest first
SETS_PER_GENE=${SETS_PER_GENE:-5}                  # each gene's strongest gene sets to anchor its hypotheses on
HYPOTHESES_PER_GENE=${HYPOTHESES_PER_GENE:-2}      # at most this many hypotheses per gene
PRIOR_MAX=${PRIOR_MAX:-0.5}                        # keep genes whose gene-set prior is below this

# Limits. A run stopped at its budget, timeout or turn limit returns no account, so the budget is generous. The
# operator sets each limit's ceiling; a value above one is refused with 422 and the message names the ceiling.
# The agent searches and downloads papers itself on the open network (the job's defaults). TOOL_CALLS counts its
# claims-repository lookups.
BUDGET_USD=${BUDGET_USD:-5}
TOOL_CALLS=${TOOL_CALLS:-$((30 * TOP_GENES))}

# The claims repository REVEAL deposits in on QA. Reading it needs no key.
CLAIMS_API_URL=${CLAIMS_API_URL:-https://api-qa.hugeampkpnbi.org/api/claims}

for tool in curl jq python3; do command -v "$tool" >/dev/null || { echo "This script needs $tool." >&2; exit 2; }; done
[ -f "$HERE/agent_job.py" ] || { echo "Put agent_job.py beside this script." >&2; exit 2; }
REVEAL_API_URL=$(jq -er .api_url "$HANDOFF")
REVEAL_API_KEY=$(jq -er .api_key "$HANDOFF")
REVEAL_API_URL=${REVEAL_API_URL%/}
mkdir -p "$RUN"

step() { printf '\n== %s\n' "$1"; }
show() { printf '$ %s\n' "$1"; }       # the command as you would type it; the key is never printed
reveal() {                             # reveal METHOD PATH [curl options]: one authenticated REVEAL call
  local method=$1 path=$2; shift 2
  curl -sS --fail-with-body -X "$method" "$REVEAL_API_URL$path" \
    -H "Authorization: Bearer $REVEAL_API_KEY" -H 'Accept: application/json' "$@"
}

step '1. Check the key'
show 'curl "$REVEAL_API_URL/v1/me" -H "Authorization: Bearer $REVEAL_API_KEY"'
ME=$(reveal GET /v1/me) || { echo "$ME" >&2; echo "The key was refused. Check api_url and api_key in $HANDOFF." >&2; exit 1; }
jq '{user_id, principal_kind, workspace_expires_at}' <<< "$ME"

if [ -s "$RUN/request.json" ]; then
  step "2. Candidate genes and gene sets: reusing $RUN/candidates.json and $RUN/request.json from the earlier run"
else
  step "2. Candidate genes for $PHENOTYPE and their top $COLLECTION gene sets from the claims repository"
  # The validation-challenge query (the pigean collection's top-genes-with-sets template, run with runQuery): genes
  # ranked by PIGEAN combined evidence, keeping those whose gene-set prior is below PRIOR_MAX, each with the gene
  # sets that hold it, strongest marginal effect first. (GET $CLAIMS_API_URL/v1/queries describes the template.)
  jq -n --arg phenotype "$PHENOTYPE" --arg collection "$COLLECTION" --argjson priorMax "$PRIOR_MAX" \
    --argjson topGenes "$TOP_GENES" --argjson setsPerGene "$SETS_PER_GENE" '{
      query: ("query Candidates($variables: JSON) { runQuery(collection: \"pigean\", query: \"top-genes-with-sets\", "
        + "variables: $variables, claims: [\"measurement\"]) }"),
      variables: {variables: {trait: $phenotype, library: $collection, priorMax: $priorMax, topGenes: $topGenes,
                              setsPerGene: $setsPerGene}}
    }' > "$RUN/candidates-query.json"
  jq -r .query "$RUN/candidates-query.json"
  show "curl -X POST $CLAIMS_API_URL/v1/graphql -H 'Content-Type: application/json' -d @$RUN/candidates-query.json"
  curl -sS --fail-with-body -X POST "$CLAIMS_API_URL/v1/graphql" -H 'Content-Type: application/json' \
    -d @"$RUN/candidates-query.json" > "$RUN/candidates-response.json"
  jq -e '.data.runQuery.rows | length > 0' "$RUN/candidates-response.json" >/dev/null \
    || { echo "The claims repository returned no candidates:" >&2; cat "$RUN/candidates-response.json" >&2; exit 1; }
  jq '[.data.runQuery.rows[]
       | {gene: .subject.value, combined: .metrics.combined, direct: .metrics.log_bf, prior: .metrics.prior,
          claim_id: .claims.measurement.id, trait: .object.id, trait_label: .object.label,
          sets: [.sets[] | {name: .edge.to.value, id: .edge.to.id, source: .row.columns.source,
                            beta_uncorrected: .row.metrics.beta_uncorrected, beta: .row.metrics.beta,
                            claim_id: .row.claims.measurement.id}]}]' \
    "$RUN/candidates-response.json" > "$RUN/candidates.json"
  jq -e 'all(.[]; .trait | test("^[A-Za-z][A-Za-z0-9+._-]*:\\S+$"))' "$RUN/candidates.json" >/dev/null \
    || { echo "A candidate has no trait identifier:" >&2; cat "$RUN/candidates.json" >&2; exit 1; }

  # The job: a question and a prompt, and no claims. With no claims the agent writes its own Propositions (here, the
  # hypotheses) and assesses each with Claims from what it reads. attribute_requester is left out, so the account is
  # attributed to the "REVEAL Mechanisms agent service" organization; add "attribute_requester": true to be named.
  jq --arg disease "$DISEASE" --arg phenotype "$PHENOTYPE" --arg collection "$COLLECTION" --arg priorMax "$PRIOR_MAX" \
    --argjson per_gene "$HYPOTHESES_PER_GENE" --argjson budget "$BUDGET_USD" --argjson tool_calls "$TOOL_CALLS" '
    def describe_set: "    - \(.name) (\(.source); marginal effect beta_uncorrected \(.beta_uncorrected), "
                      + "joint beta \(.beta); repository claim \(.claim_id))";
    def describe_gene: "- \(.gene) (HGNC.SYMBOL:\(.gene)): combined \(.combined), direct GWAS \(.direct), gene-set prior "
                       + "\(.prior); repository claim \(.claim_id)\n"
                       + (if (.sets | length) > 0 then "  Its strongest gene sets:\n" + (.sets | map(describe_set) | join("\n"))
                          else "  The \($collection) library holds no gene set for this gene." end);
    {
      kind: "agent",
      deliverable: "scientific_account",
      question: ("By what mechanisms could the top PIGEAN candidate genes for \($disease) (\(map(.gene) | join(", "))) "
                 + "contribute to the disease, judging from the gene sets PIGEAN weighs most for each?"),
      prompt: ("These genes are the top of the claims repository PIGEAN validation challenge for \($disease) "
               + "(\($phenotype), \(.[0].trait) in the repository), run with the \($collection) gene-set library: the strongest "
               + "combined evidence among genes whose gene-set prior is below \($priorMax). PIGEAN scores are on a log scale. "
               + "Each gene is listed with the gene sets that contain it and carry the most weight for the disease. "
               + "Sets named mp_* are mouse phenotypes (MGI Mammalian Phenotype terms: mice with a mutation in a member gene "
               + "show the phenotype); GOBP_* are GO biological processes; other names are MSigDB signatures (for example "
               + "GSE* expression contrasts and cell-type markers).\n\n"
               + (map(describe_gene) | join("\n\n"))
               + "\n\nYour task is mechanistic hypotheses, not whether each gene is associated with the disease. For each gene:\n"
               + "1. Work out what its gene sets have in common: the biological process, cell type or tissue they point to. "
               + "Look a set up (MSigDB, MGI, the GEO series) when its name alone does not say what it contains.\n"
               + "2. Propose up to \($per_gene) hypotheses for how the gene could contribute to \($disease) through that "
               + "process: what the gene does, in which cells, and how that would lead to intestinal inflammation. Make "
               + "each hypothesis specific enough that a paper could support or contradict it.\n"
               + "3. Search the literature and the web for evidence on each link in the hypothesis: the gene in that "
               + "process, the process in \($disease) (including Crohn disease and ulcerative colitis), and knockout or "
               + "patient-tissue findings. Download what you quote. Prefer primary studies.\n"
               + "4. Write each hypothesis as one Proposition, naming the gene sets it rests on in its statement, and "
               + "assess it with Claims for or against from what you read; every Proposition needs at least one Claim. "
               + "Say plainly which links are supported by the papers and which are inferred from the gene sets alone.\n\n"
               + "For a gene with no gene sets listed, say so, and base its hypotheses on what the papers show instead. "
               + "In a Proposition triple, name the gene as HGNC.SYMBOL:<symbol> and the disease as \(.[0].trait), with a "
               + "biolink predicate; name gene sets in the statement text only. You can look the repository claims up "
               + "with query_claims; they are why these genes and sets were chosen, not evidence to cite."),
      limits: {max_budget_usd: $budget, tool_calls: $tool_calls}
    }' "$RUN/candidates.json" > "$RUN/request.json"
fi
jq -r '.[] | "  \(.gene)\tcombined \(.combined)\tprior \(.prior)\t\(.sets | length) gene sets: \(.sets | map(.name) | join(", "))"' \
  "$RUN/candidates.json"

step '3. Submit the job'
cat "$RUN/request.json"
# One key per run directory: sending the request again returns the same job instead of starting another.
[ -s "$RUN/idempotency-key" ] || python3 -c 'import uuid; print(uuid.uuid4())' > "$RUN/idempotency-key"
show 'curl -X POST "$REVEAL_API_URL/v1/jobs" -H "Authorization: Bearer $REVEAL_API_KEY" \'
show '    -H "Content-Type: application/json" -H "Idempotency-Key: $(cat '"$RUN"'/idempotency-key)" -d @'"$RUN"'/request.json'
reveal POST /v1/jobs -H 'Content-Type: application/json' -H "Idempotency-Key: $(cat "$RUN/idempotency-key")" \
  -d @"$RUN/request.json" > "$RUN/job.json" || { cat "$RUN/job.json" >&2; echo >&2; exit 1; }
JOB=$(jq -er .id "$RUN/job.json")
echo "Job $JOB accepted."

step '4. Watch it (Ctrl-C stops watching, not the job; run this script again to carry on)'
show "python3 agent_job.py --handoff $HANDOFF --job $JOB --out $RUN/answer.md"
python3 "$HERE/agent_job.py" --handoff "$HANDOFF" --job "$JOB" --out "$RUN/answer.md" \
  || { echo "The job did not succeed, so there is no account and nothing was deposited." >&2; exit 1; }

step '5. Read the result'
show 'curl "$REVEAL_API_URL/v1/jobs/'"$JOB"'" -H "Authorization: Bearer $REVEAL_API_KEY"'
reveal GET "/v1/jobs/$JOB" > "$RUN/job.json"
jq '.result.account | {account_id, dapper_version, objects, claims, value_locators,
                       papers: [.papers[] | {path, url, evidence_items}], deposit}' "$RUN/job.json"
DEPOSIT_STATUS=$(jq -r '.result.account.deposit.status' "$RUN/job.json")
if [ "$DEPOSIT_STATUS" != deposited ]; then
  echo "The account was built but not deposited: $(jq -c '.result.account.deposit' "$RUN/job.json")" >&2
  reveal GET "/v1/jobs/$JOB/account" | jq '.document' > "$RUN/account.json" || true
  echo "It is kept with the job (source job_artifact) and saved in $RUN/account.json. There is no automatic retry." >&2
  exit 1
fi
ACCOUNT_ID=$(jq -er '.result.account.account_id' "$RUN/job.json")
DEPOSIT_ID=$(jq -er '.result.account.deposit.deposit_id' "$RUN/job.json")

step '6. The DAPPER document, read back through REVEAL from the claims repository'
show 'curl "$REVEAL_API_URL/v1/jobs/'"$JOB"'/account" -H "Authorization: Bearer $REVEAL_API_KEY"'
reveal GET "/v1/jobs/$JOB/account" > "$RUN/account-response.json" \
  || { cat "$RUN/account-response.json" >&2; echo >&2; echo "REVEAL could not serve the deposited account back." >&2; exit 1; }
SOURCE=$(jq -r '.source' "$RUN/account-response.json")
if [ "$SOURCE" != claims_repository ]; then
  echo "REVEAL served the account from '$SOURCE', not from the claims repository, although it is deposited." >&2
  exit 1
fi
jq '.document' "$RUN/account-response.json" > "$RUN/account.json"
jq -r '.document | to_entries | map("\(.value | length) \(.key)") | join(", ")' "$RUN/account-response.json"
echo "Source: $SOURCE. Saved as $RUN/account.json"
# The hypotheses: each Proposition the agent wrote, with its triple when it gave one.
jq -r '.document.propositions // [] | .[] | "  - \(.statement)"
       + (if .subject_entity then "\n      [\(.subject_entity) \(.relation) \(.object_entity)]" else "" end)' \
  "$RUN/account-response.json"

step '7. Read the deposit back from the claims repository (no key needed)'
show "curl $CLAIMS_API_URL/v1/deposits/$DEPOSIT_ID"
curl -sS --fail-with-body "$CLAIMS_API_URL/v1/deposits/$DEPOSIT_ID" > "$RUN/deposit.json"
jq '{deposit_id, depositor, received_at, object_count,
     created: ([.objects[] | select(.status == "created")] | length),
     already_held: ([.objects[] | select(.status == "exists")] | length),
     classes: ([.objects[].class_name] | group_by(.) | map("\(length) \(.[0])") | join(", "))}' "$RUN/deposit.json"
show "curl $CLAIMS_API_URL/v1/objects/$ACCOUNT_ID"
curl -sS --fail-with-body -H 'Accept: application/json' "$CLAIMS_API_URL/v1/objects/$ACCOUNT_ID" > "$RUN/deposited-account.json"
echo "The repository holds $ACCOUNT_ID ($(wc -c < "$RUN/deposited-account.json" | tr -d ' ') bytes, saved as $RUN/deposited-account.json)."

printf '\nDone. Job %s deposited %s as %s.\n' "$JOB" "$ACCOUNT_ID" "$DEPOSIT_ID"
echo "Open it: $CLAIMS_API_URL/v1/objects/$ACCOUNT_ID"
