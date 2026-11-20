"""eaggl's supplied-factor gene-set projection, computed from the packed gene-set matrix (numpy and scipy).

eaggl (the pinned packages/pigean: eaggl/factor.py _project_gene_set_factors_from_loaded_gene_factors and
eaggl/state.py _project_H_with_fixed_W) projects a 0/1 genes x gene-sets matrix V onto a trait's fixed factors
W (genes x K):

  joint     H <- clip(H * (W'V) / (W'(WH) + 1e-10), 0, 1), starting from H = U[0, 1) * max(V) drawn from numpy's
            global RNG right after seeding, until the relative Frobenius change of H is below 1e-4 or after 100
            updates;
  marginal  clip(V'W / diag(W'W), 0, 1).

eaggl builds dense genes x gene-sets arrays and recomputes W'(V * 1) and W'(WH) at every update: 98% of its run
time (a 5,000-gene-set chunk of T2D, K = 13: 303 s). Both are fixed linear maps, so here W'V is computed once
from the sparse V and W'(WH) as (W'W)H (0.1 s). Only the floating-point summation order differs, and the %.4g
loadings and the top factor equal eaggl's: `projection_workflow.py check-projection` compares them against the
pinned eaggl before any trait is projected.
"""

import numpy as np
from scipy import sparse

EPS = 1e-10  # eaggl's eps
TOL = 1e-4  # the tol eaggl passes for the gene-set projection
MAX_ITER = 100  # eaggl's n_iter default


def load_pool(indptr_file, indices_file):
    """The packed gene sets (pack-annotations), memory-mapped: column j holds the gene rows of gene set j."""
    indptr = np.load(indptr_file, mmap_mode="r")
    indices = np.load(indices_file, mmap_mode="r")
    if indptr.ndim != 1 or indices.ndim != 1 or indptr[0] != 0 or indptr[-1] != len(indices):
        raise ValueError("%s and %s are not one packed gene-set matrix" % (indptr_file, indices_file))
    return indptr, indices


def gene_set_matrix(indptr, indices, columns, n_genes):
    """The 0/1 genes x gene-sets matrix of the given gene-set columns (a range or a list), in that order."""
    columns = np.asarray(columns, dtype=np.int64)
    starts, ends = np.asarray(indptr[columns]), np.asarray(indptr[columns + 1])
    lengths = ends - starts
    ptr = np.concatenate(([0], np.cumsum(lengths))).astype(np.int64)
    if len(columns) and np.all(columns[1:] == columns[:-1] + 1):  # a contiguous range: one slice of the pool
        rows = np.asarray(indices[starts[0]:ends[-1]], dtype=np.int32)
    else:
        rows = np.concatenate([np.asarray(indices[s:e], dtype=np.int32) for s, e in zip(starts, ends)] or
                              [np.zeros(0, dtype=np.int32)])
    return sparse.csc_matrix((np.ones(len(rows)), rows, ptr), shape=(n_genes, len(columns)))


def project(W, V, seed):
    """(joint, marginal, updates, last relative change): joint and marginal are gene sets x factors.

    W is genes x K (float64, the factor-file values), V the genes x gene-sets 0/1 matrix of one chunk. The RNG is
    seeded here exactly as one eaggl run is (--seed), so a chunk gets the starting H eaggl would draw for it.
    """
    K, M = W.shape[1], V.shape[1]
    np.random.seed(seed)
    H = np.random.random((K, M)) * (V.max() if V.nnz else 0.0)
    WtV = np.asarray(V.T @ W).T  # K x M
    WtW = W.T @ W
    change = 0.0
    for update in range(1, MAX_ITER + 1):
        new = np.clip(np.maximum(H * (WtV / (WtW @ H + EPS)), 0), 0, 1)
        change = np.linalg.norm(new - H, "fro") / (np.linalg.norm(H, "fro") + EPS)
        H = new
        if change < TOL:
            break
    denominators = np.einsum("ij,ij->j", W, W)
    marginal = np.asarray(V.T @ W, dtype=float)
    np.divide(marginal, denominators, out=marginal, where=denominators > 0)
    marginal[:, denominators == 0] = 0.0
    np.clip(marginal, 0.0, 1.0, out=marginal)
    return H.T, marginal, update, float(change)


def top_factors(joint):
    """eaggl's `cluster` per gene set: the first factor with the highest joint loading, or -1 if none is above 0."""
    top = np.argmax(joint, axis=1)
    top[joint.max(axis=1) <= 0] = -1
    return top
