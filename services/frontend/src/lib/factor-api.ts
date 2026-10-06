import createClient from "openapi-fetch";
import type { paths } from "./api.generated";
import { unwrap, type Schema } from "./client";
import { withRequestDeadline } from "./request-deadline";

const client = createClient<paths>({ baseUrl: "/api/backend", credentials: "same-origin" });
const combined = (deadline: AbortSignal, caller?: AbortSignal) => caller ? AbortSignal.any([deadline, caller]) : deadline;

export const factorApi = {
  detail: (source_id: string, source_revision?: string, caller?: AbortSignal) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/factors/{source_id}", {
    params: { path: { source_id }, query: { source_revision } }, signal: combined(signal, caller),
  })), "The factor is taking longer than expected to load. Please retry."),
  loadings: (query: { source_id: string; source_revision: string; generation_id?: string; gnomad_import_id?: string; kind: "gene" | "gene_set"; metric: "joint" | "marginal"; sort?: Schema<"FactorLoadings">["sort"]; q?: string; offset?: number; limit?: number }, caller?: AbortSignal) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/factor-loadings", {
    params: { query }, signal: combined(signal, caller),
  })), "The factor loadings are taking longer than expected to load. Please retry."),
  geneSet: (gene_set_id: string, generation_id?: string, caller?: AbortSignal) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/catalog/gene-sets/{gene_set_id}", {
    params: { path: { gene_set_id }, query: { generation_id } }, signal: combined(signal, caller),
  })), "The gene-set provenance is taking longer than expected to load. Please retry."),
  savedGeneSet: (dapper_id: string, caller?: AbortSignal) => withRequestDeadline(async signal => unwrap(await client.GET("/v1/gene-sets/{dapper_id}", {
    params: { path: { dapper_id } }, signal: combined(signal, caller),
  })), "The saved gene set is taking longer than expected to load. Please retry."),
};
