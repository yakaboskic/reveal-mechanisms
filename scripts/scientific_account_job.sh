#!/usr/bin/env bash
# From candidate genes in the claims repository to a deposited DAPPER Scientific Account.
#
#   scripts/scientific_account_job.sh reveal-key.json [run-directory]
#
# It asks the claims repository for the top PIGEAN validation-challenge genes for a disease (IBD by default:
# strongest combined evidence among genes with a low gene-set prior), asks a REVEAL agent to check each one
# against the published literature, and reads the resulting account back from the claims repository.
#
# reveal-key.json is your key handoff: {"api_url": "...", "api_key": "rvl_..."}. Keep it private.
# Needs curl, jq and python3 (3.9+), and agent_job.py beside this file.
#
# One HTTP call per step; each call is printed before it runs:
#   1. GET  {reveal}/v1/me                   check the key and show whose workspace it is
#   2. POST {claims}/v1/graphql              the candidate genes, with their scores, claim ids and trait id (no key)
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
#   PHENOTYPE, DISEASE, TOP_GENES, PRIOR_MAX, LEAVE_TO_AGENT, BUDGET_USD, TOOL_CALLS,
#   CLAIMS_API_URL
set -euo pipefail

HANDOFF=${1:?usage: scientific_account_job.sh reveal-key.json [run-directory]}
RUN=${2:-account-run}
HERE=$(cd "$(dirname "$0")" && pwd)

PHENOTYPE=${PHENOTYPE:-IBD}                        # the claims repository's phenotype code
COLLECTION=${COLLECTION:-mouse_msigdb}             # the PIGEAN gene-set library the scores come from (cfde-inc-v2 is retired)
DISEASE=${DISEASE:-inflammatory bowel disease}     # how to name it to the agent
TOP_GENES=${TOP_GENES:-3}                          # candidates to check, strongest first (at most 20)
LEAVE_TO_AGENT=${LEAVE_TO_AGENT:-0}                # send the last N claims without a triple, for the agent to supply
PRIOR_MAX=${PRIOR_MAX:-0.5}                        # keep genes whose gene-set prior is below this

# Limits. A run stopped at its budget, timeout or turn limit returns no account, so the budget is generous: a
# three-gene run has cost $1.30 to $2.10. The operator sets each limit's ceiling; a value above one is refused
# with 422 and the message names the ceiling.
# The agent fetches papers itself with Bash on the open network (the job's defaults), so no paper allowance is
# sent. TOOL_CALLS counts its claims-repository lookups and grows with TOP_GENES.
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
  step "2. Candidate genes: reusing $RUN/candidates.json and $RUN/request.json from the earlier run"
else
  step "2. Candidate genes for $PHENOTYPE ($COLLECTION gene sets) from the claims repository"
  # The validation-challenge query: the pigean collection's top-genes-with-sets query template, run with runQuery.
  # Genes ranked by PIGEAN combined evidence, keeping those whose gene-set prior is below PRIOR_MAX. Each row
  # serves its measurement Claim, which the repository holds, with its scores. (GET $CLAIMS_API_URL/v1/queries
  # describes the template's params and rows.)
  jq -n --arg phenotype "$PHENOTYPE" --arg collection "$COLLECTION" --argjson priorMax "$PRIOR_MAX" --argjson topGenes "$TOP_GENES" '{
      query: ("query Candidates($variables: JSON) { runQuery(collection: \"pigean\", query: \"top-genes-with-sets\", "
        + "variables: $variables, claims: [\"measurement\"]) }"),
      variables: {variables: {trait: $phenotype, library: $collection, priorMax: $priorMax, topGenes: $topGenes,
                              setsPerGene: 1}}
    }' > "$RUN/candidates-query.json"
  jq -r .query "$RUN/candidates-query.json"
  show "curl -X POST $CLAIMS_API_URL/v1/graphql -H 'Content-Type: application/json' -d @$RUN/candidates-query.json"
  curl -sS --fail-with-body -X POST "$CLAIMS_API_URL/v1/graphql" -H 'Content-Type: application/json' \
    -d @"$RUN/candidates-query.json" > "$RUN/candidates-response.json"
  jq -e '.data.runQuery.rows | length > 0' "$RUN/candidates-response.json" >/dev/null \
    || { echo "The claims repository returned no candidates:" >&2; cat "$RUN/candidates-response.json" >&2; exit 1; }
  jq '[.data.runQuery.rows[]
       | {gene: .subject.value, combined: .metrics.combined, direct: .metrics.log_bf, prior: .metrics.prior,
          claim_id: .claims.measurement.id, trait: .claims.measurement.triple.object}]' \
    "$RUN/candidates-response.json" > "$RUN/candidates.json"

  jq -e 'all(.[]; .trait | test("^[A-Za-z][A-Za-z0-9+._-]*:\\S+$"))' "$RUN/candidates.json" >/dev/null \
    || { echo "A candidate has no trait identifier to build its claim from:" >&2; cat "$RUN/candidates.json" >&2; exit 1; }

  # The job: a question, one claim per gene for the agent to assess for or against, and how to do the work.
  # attribute_requester is left out, so the account does not name you: it is attributed to the
  # "REVEAL Mechanisms agent service" organization. Add "attribute_requester": true to be named as a Person.
  jq --arg disease "$DISEASE" --arg phenotype "$PHENOTYPE" --arg collection "$COLLECTION" --arg priorMax "$PRIOR_MAX" --argjson leave "$LEAVE_TO_AGENT" \
    --argjson budget "$BUDGET_USD" --argjson tool_calls "$TOOL_CALLS" 'length as $length_all | def length_all: $length_all; {
      kind: "agent",
      deliverable: "scientific_account",
      question: ("Which of the top PIGEAN candidate genes for \($disease) (\(map(.gene) | join(", "))) "
                 + "have published evidence of a role in the disease?"),
      # Each claim carries its structured form, which REVEAL stores on the Proposition: the gene as
      # HGNC.SYMBOL:<symbol>, and the trait id that the repository claim uses (for IBD, KPN.TRAIT:0000505), so the
      # account can be found in the repository by that trait id. The last LEAVE_TO_AGENT claims are sent as plain
      # statements instead, to see the agent supply the triple from the identifiers in the prompt.
      claims: (to_entries | map(.value as $c
        | {statement: "\($c.gene) is associated with \($disease)."}
          + (if .key < (length_all - $leave) then {subject_entity: "HGNC.SYMBOL:\($c.gene)",
                                                    relation: "biolink:associated_with",
                                                    object_entity: $c.trait} else {} end))),
      prompt: ("These genes are the top of the claims repository PIGEAN validation challenge for \($disease) "
               + "(\($phenotype)), run with the \($collection) gene-set library: the strongest combined evidence among genes whose gene-set prior is below "
               + "\($priorMax), so their support is mostly direct GWAS signal. PIGEAN scores are on a log scale.\n\n"
               + (map("- \(.gene) (HGNC.SYMBOL:\(.gene)): combined \(.combined), direct GWAS \(.direct), gene-set prior "
                      + "\(.prior); repository claim \(.claim_id)") | join("\n"))
               + "\n\nIn the claims repository \($disease) is \(.[0].trait)."
               + "\n\nFor each gene, find out from published studies whether it has a role in \($disease), "
               + "including Crohn disease and ulcerative colitis where relevant: human genetics, expression in "
               + "patient tissue, or functional work in cells or animal models. Assess each claim for or against "
               + "from what you read. Prefer primary studies. Say plainly when you found nothing for a gene, and "
               + "say what you did not read. You can look the repository claims up with query_claims; they are "
               + "the reason these genes were chosen, not evidence to cite."),
      limits: {max_budget_usd: $budget, tool_calls: $tool_calls}
    }' "$RUN/candidates.json" > "$RUN/request.json"
fi
jq -r '.[] | "  \(.gene)\tcombined \(.combined)\tdirect \(.direct)\tprior \(.prior)\t\(.claim_id)"' "$RUN/candidates.json"

step '3. Submit the job'
cat "$RUN/request.json"
# One key per run directory: sending the request again returns the same job instead of starting another.
[ -s "$RUN/idempotency-key" ] || python3 -c 'import uuid; print(uuid.uuid4())' > "$RUN/idempotency-key"
show 'curl -X POST "$REVEAL_API_URL/v1/jobs" -H "Authorization: Bearer $REVEAL_API_KEY" \'
show '    -H "Content-Type: application/json" -H "Idempotency-Key: $(cat '"$RUN"'/idempotency-key)" -d @'"$RUN"'/request.json'
reveal POST /v1/jobs -H 'Content-Type: application/json' -H "Idempotency-Key: $(cat "$RUN/idempotency-key")" \
  -d @"$RUN/request.json" > "$RUN/job.json" || { cat "$RUN/job.json" >&2; echo >&2; exit 1; }
JOB=$(jq -er .id "$RUN/job.json")
echo "Job $JOB accepted."      # a repeated request returns the job as first accepted; step 4 shows where it is now

step '4. Watch it (Ctrl-C stops watching, not the job; run this script again to carry on)'
# The account itself is read in step 6, which checks where it came from.
show "python3 agent_job.py --handoff $HANDOFF --job $JOB --out $RUN/answer.md"
python3 "$HERE/agent_job.py" --handoff "$HANDOFF" --job "$JOB" --out "$RUN/answer.md" \
  || { echo "The job did not succeed, so there is no account and nothing was deposited." >&2; exit 1; }

step '5. Read the result'
show 'curl "$REVEAL_API_URL/v1/jobs/'"$JOB"'" -H "Authorization: Bearer $REVEAL_API_KEY"'
reveal GET "/v1/jobs/$JOB" > "$RUN/job.json"
jq '.result.account | {account_id, dapper_version, objects, claims, value_locators, requested_claims,
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
# REVEAL keeps no copy of a deposited account: it rebuilds the document from the deposit, checks it hashes to the
# document it sent (document_sha256) and says where it came from. Anything but claims_repository is a failure here.
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
