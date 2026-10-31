"""Ideal-point estimation from roll-call votes, one fit per chamber and term.

Model (two-dimensional item-response ideal-point model, the probabilistic
counterpart of NOMINATE; Clinton, Jackman & Rivers 2004):

    P(member i votes yes on vote j) = logistic(alpha_j + beta_j . x_i)

x_i is the member's position (2 numbers), alpha_j and beta_j describe the
vote. Only yes/no votes enter; abstentions, absences and non-votes are
treated as missing. Fitted by penalised maximum likelihood (standard normal
prior on x, N(0, 5^2) on alpha and beta) with alternating Newton steps.

The axes have no built-in meaning or sign. After fitting, positions are
centred, whitened to unit standard deviation, rotated so dimension 1 matches a one-dimensional
fit, and both dimensions are oriented so that the largest
club/group sits on the positive side. That orientation is a convention only.
See docs/methodology in the generated site for the full description.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from collections import Counter
from datetime import datetime, timezone

import numpy as np

log = logging.getLogger(__name__)

MIN_VOTES = 20          # members with fewer yes/no votes in the term are not placed
LOPSIDED = 0.025        # drop votes where the minority is under 2.5 % of yes+no
PRIOR_X = 1.0
PRIOR_ITEM = 25.0
CLUB_KIND = {"psp": "club", "senat": "club", "ep": "ep_group"}
BOOTSTRAP = 50          # resamples of the votes for the uncertainty of each position

DDL = """
CREATE TABLE IF NOT EXISTS ideal_point (
    person_id  INTEGER NOT NULL REFERENCES person(id),
    chamber    TEXT NOT NULL,
    term       TEXT NOT NULL,
    dim1 REAL, dim2 REAL, se1 REAL, se2 REAL,
    n_votes    INTEGER,
    PRIMARY KEY (person_id, chamber, term)
);
CREATE TABLE IF NOT EXISTS spectrum_fit (
    chamber TEXT NOT NULL, term TEXT NOT NULL,
    fitted_at TEXT, detail TEXT,              -- JSON: fit statistics and settings
    PRIMARY KEY (chamber, term)
);
"""


def sigmoid(z):
    return 1.0 / (1.0 + np.exp(-np.clip(z, -30, 30)))


def load_matrix(conn: sqlite3.Connection, chamber: str, term: str):
    """Return (person_ids, vote_ids, Y) with Y = 1 yes, 0 no, nan otherwise."""
    rows = conn.execute(
        "SELECT v.person_id, v.vote_event_id, v.option FROM vote v JOIN vote_event e ON e.id = v.vote_event_id"
        " WHERE e.chamber=? AND e.term=? AND e.outcome != 'void' AND v.option IN ('yes','no')",
        (chamber, term),
    ).fetchall()
    people = sorted({r[0] for r in rows})
    votes = sorted({r[1] for r in rows})
    pi = {p: k for k, p in enumerate(people)}
    vi = {v: k for k, v in enumerate(votes)}
    Y = np.full((len(people), len(votes)), np.nan)
    for p, v, o in rows:
        Y[pi[p], vi[v]] = 1.0 if o == "yes" else 0.0
    return np.array(people), np.array(votes), Y


def filter_matrix(people, votes, Y):
    """Drop lopsided votes and members with too few votes, until stable."""
    while True:
        yes = np.nansum(Y, axis=0)
        n = np.sum(~np.isnan(Y), axis=0)
        minority = np.minimum(yes, n - yes)
        keep_v = (n > 0) & (minority >= np.maximum(LOPSIDED * n, 1))
        Y, votes = Y[:, keep_v], votes[keep_v]
        keep_p = np.sum(~np.isnan(Y), axis=1) >= MIN_VOTES
        Y, people = Y[keep_p], people[keep_p]
        if keep_v.all() and keep_p.all():
            return people, votes, Y


def fit(Y: np.ndarray, dims: int = 2, iters: int = 300, tol: float = 1e-5, init: np.ndarray | None = None):
    """Penalised ML ideal points. Returns x (n x d), alpha (m), beta (m x d), cov (n x d x d).
    `init` warm-starts the member positions (used by the bootstrap)."""
    n, m = Y.shape
    M = ~np.isnan(Y)
    Y0 = np.where(M, Y, 0.0)
    if init is None:
        # start: leading singular vectors of the centred vote matrix (missing = vote mean)
        colmean = np.nanmean(Y, axis=0)
        Z = np.where(M, Y, colmean) - colmean
        U, S, _ = np.linalg.svd(Z, full_matrices=False)
        x = U[:, :dims] * np.sqrt(n)
    else:
        x = init.copy()
    x = (x - x.mean(0)) / x.std(0)
    alpha = np.zeros(m)
    beta = np.zeros((m, dims))
    I_d = np.eye(dims)
    prev = None
    for it in range(iters):
        # votes given members: Newton step on (alpha_j, beta_j) for every j at once
        Xt = np.hstack([np.ones((n, 1)), x])                      # n x (d+1)
        for _ in range(2):
            theta = np.column_stack([alpha, beta])                 # m x (d+1)
            p = sigmoid(Xt @ theta.T)                              # n x m
            r = M * (Y0 - p)
            w = M * p * (1 - p)
            g = Xt.T @ r - theta.T / PRIOR_ITEM                    # (d+1) x m
            H = _weighted_outer(w.T, Xt) + np.eye(dims + 1) / PRIOR_ITEM
            step = np.linalg.solve(H, g.T[..., None])[..., 0]
            theta = theta + _clip(step, 1.0)
            alpha, beta = theta[:, 0], theta[:, 1:]
        # members given votes: Newton step on x_i for every i at once
        for _ in range(2):
            p = sigmoid(alpha[None, :] + x @ beta.T)
            r = M * (Y0 - p)
            w = M * p * (1 - p)
            g = r @ beta - x / PRIOR_X
            H = _weighted_outer(w, beta) + I_d / PRIOR_X
            x = x + _clip(np.linalg.solve(H, g[..., None])[..., 0], 0.5)
        # pin the scale (mean 0, sd 1 per dimension) and move it into the vote parameters,
        # which leaves every predicted probability unchanged; stops joint ML from drifting
        mu, sd = x.mean(0), x.std(0)
        alpha = alpha + beta @ mu
        beta = beta * sd
        x = (x - mu) / sd
        p = sigmoid(alpha[None, :] + x @ beta.T)
        ll = float(np.sum(M * (Y0 * np.log(p + 1e-12) + (1 - Y0) * np.log(1 - p + 1e-12))))
        log.debug("iter %d ll %.1f max|beta| %.2f", it, ll, np.abs(beta).max())
        if prev is not None and abs(ll - prev) < tol * abs(prev):
            break
        prev = ll
    w = M * p * (1 - p)
    H = _weighted_outer(w, beta) + I_d / PRIOR_X
    cov = np.linalg.inv(H)
    return x, alpha, beta, cov, {"iterations": it + 1, "log_likelihood": ll}


def _weighted_outer(W, V):
    """sum_k W[r, k] * outer(V[k], V[k]) for every row r, as one matrix product
    (same result as einsum("ka,kb,rk->rab"), much faster)."""
    d = V.shape[1]
    return (W @ (V[:, :, None] * V[:, None, :]).reshape(len(V), d * d)).reshape(-1, d, d)


def _clip(step, limit):
    """Shorten Newton steps longer than `limit` (plain Newton can overshoot on separable votes)."""
    norm = np.linalg.norm(step, axis=-1, keepdims=True)
    return step * np.minimum(1.0, limit / np.maximum(norm, 1e-12))


def classification(Y, x, alpha, beta):
    M = ~np.isnan(Y)
    pred = (alpha[None, :] + x @ beta.T) > 0
    correct = np.sum(M & (pred == (Y == 1)))
    total = int(M.sum())
    yes = np.nansum(Y, axis=0)
    n = M.sum(0)
    minority = np.minimum(yes, n - yes).sum()
    errors = total - correct
    return {"correct": float(correct / total), "apre": float((minority - errors) / minority)}


def normalise(x, cov, x1d, club_of, people):
    """Centre and whiten the 2-D positions, then rotate so that dimension 1 lines up
    with the one-dimensional fit (the single axis that best explains the votes) and
    dimension 2 is the remaining, uncorrelated direction. Orientation: the largest
    club's median is positive on both dimensions."""
    xc = x - x.mean(0)
    evals, evecs = np.linalg.eigh(np.cov(xc.T))
    W = evecs @ np.diag(evals ** -0.5) @ evecs.T          # whitening matrix
    z = xc @ W
    u = np.array([np.cov(z[:, 0], x1d)[0, 1], np.cov(z[:, 1], x1d)[0, 1]])
    u = u / np.linalg.norm(u)
    R = np.array([[u[0], u[1]], [-u[1], u[0]]])
    A = R @ W
    xn = xc @ A.T
    covn = np.einsum("ab,ibc,dc->iad", A, cov, A)
    clubs = Counter(club_of.get(int(p)) for p in people if club_of.get(int(p)))
    if clubs:
        largest = clubs.most_common(1)[0][0]
        members = [k for k, p in enumerate(people) if club_of.get(int(p)) == largest]
        if np.median(xn[members, 0]) < 0:
            xn[:, 0] *= -1
            covn[:, 0, 1] *= -1
            covn[:, 1, 0] *= -1
        if np.median(xn[members, 1]) < 0:
            xn[:, 1] *= -1
            covn[:, 0, 1] *= -1
            covn[:, 1, 0] *= -1
    return xn, covn, largest if clubs else None


def bootstrap(Y, x_start, x_ref, groups=None, B: int = BOOTSTRAP, seed: int = 0):
    """Standard errors from resampling sitting days with replacement and refitting.

    The analytic errors from fit() treat the vote parameters as known and the
    votes as independent, which makes them too narrow: votes on the same day
    are often on one bill and follow the same split. Each resample therefore
    draws whole days (`groups`: a day index per vote column; default one group
    per vote) with replacement, refits from the full-data solution, and maps
    the result onto the reported scale with a least-squares affine
    transformation (the model is only identified up to one). The spread of a
    member's position across the resamples is their standard error.
    Returns (se n x 2, B used)."""
    rng = np.random.default_rng(seed)
    n, m = Y.shape
    groups = np.arange(m) if groups is None else np.asarray(groups)
    _, groups = np.unique(groups, return_inverse=True)
    members = [np.flatnonzero(groups == k) for k in range(groups.max() + 1)]
    draws = []
    for _ in range(B):
        cols = np.concatenate([members[k] for k in rng.integers(0, len(members), len(members))])
        xb = fit(Y[:, cols], iters=25, tol=1e-4, init=x_start)[0]
        Xb = np.hstack([np.ones((n, 1)), xb])
        coef = np.linalg.lstsq(Xb, x_ref, rcond=None)[0]
        draws.append(Xb @ coef)
    return np.std(np.array(draws), axis=0, ddof=1), B


def last_club(conn, chamber: str, term: str) -> dict[int, str]:
    """Club/group each member belonged to last within the term's votes."""
    end = conn.execute("SELECT max(date) FROM vote_event WHERE chamber=? AND term=?", (chamber, term)).fetchone()[0]
    start = conn.execute("SELECT min(date) FROM vote_event WHERE chamber=? AND term=?", (chamber, term)).fetchone()[0]
    out = {}
    for pid, abbr, name in conn.execute(
        "SELECT person_id, org_abbr, org_name FROM affiliation WHERE chamber=? AND kind=?"
        " AND (start_date IS NULL OR start_date <= ?) AND (end_date IS NULL OR end_date >= ?)"
        " ORDER BY start_date",
        (chamber, CLUB_KIND[chamber], end, start),
    ):
        out[pid] = abbr or name
    return out


def run(conn: sqlite3.Connection, chamber: str, term: str) -> dict:
    conn.executescript(DDL)
    people, votes, Y = load_matrix(conn, chamber, term)
    n0, m0 = Y.shape
    people, votes, Y = filter_matrix(people, votes, Y)
    if len(people) < 10 or len(votes) < 20:
        log.warning("%s %s: too little data (%d members, %d votes)", chamber, term, *Y.shape)
        return {}
    x, alpha, beta, cov, info = fit(Y)
    stats2 = classification(Y, x, alpha, beta)
    x1, a1, b1, _, _ = fit(Y, dims=1)
    stats1 = classification(Y, x1, a1, b1)
    clubs = last_club(conn, chamber, term)
    xn, covn, anchor = normalise(x, cov, x1[:, 0], clubs, people)
    se_analytic = np.sqrt(np.stack([covn[:, 0, 0], covn[:, 1, 1]], axis=1))
    day = dict(conn.execute("SELECT id, date FROM vote_event WHERE chamber=? AND term=?", (chamber, term)))
    se, n_boot = bootstrap(Y, x, xn, [day[v] for v in votes]) if BOOTSTRAP else (se_analytic, 0)
    # cross-check against a model-free method: first principal component of the vote matrix
    colmean = np.nanmean(Y, axis=0)
    Z = np.where(np.isnan(Y), colmean, Y) - colmean
    pc1 = np.linalg.svd(Z, full_matrices=False)[0][:, 0]
    r_pca = float(abs(np.corrcoef(pc1, xn[:, 0])[0, 1]))
    detail = {
        "members_total": int(n0), "votes_total": int(m0),
        "members_used": int(Y.shape[0]), "votes_used": int(Y.shape[1]),
        "cells": int((~np.isnan(Y)).sum()),
        "classification_2d": stats2, "classification_1d": stats1,
        "corr_dim1_pca": r_pca, "orientation_club": anchor,
        "min_votes": MIN_VOTES, "lopsided": LOPSIDED, **info,
        "bootstrap": n_boot,
        "se_median_bootstrap": float(np.median(se[:, 0])),
        "se_median_analytic": float(np.median(se_analytic[:, 0])),
    }
    conn.execute("DELETE FROM ideal_point WHERE chamber=? AND term=?", (chamber, term))
    nv = (~np.isnan(Y)).sum(1)
    conn.executemany(
        "INSERT INTO ideal_point (person_id, chamber, term, dim1, dim2, se1, se2, n_votes) VALUES (?,?,?,?,?,?,?,?)",
        [(int(p), chamber, term, float(xn[k, 0]), float(xn[k, 1]),
          float(se[k, 0]), float(se[k, 1]), int(nv[k]))
         for k, p in enumerate(people)],
    )
    conn.execute(
        "INSERT OR REPLACE INTO spectrum_fit (chamber, term, fitted_at, detail) VALUES (?,?,?,?)",
        (chamber, term, datetime.now(timezone.utc).isoformat(timespec="seconds"), json.dumps(detail)),
    )
    conn.commit()
    log.info("%s %s: %d members x %d votes, %.1f %% correct (1D %.1f %%), APRE %.2f, r(PCA)=%.3f,"
             " median SE dim1 %.3f (analytic %.3f)",
             chamber, term, *Y.shape, 100 * stats2["correct"], 100 * stats1["correct"], stats2["apre"], r_pca,
             detail["se_median_bootstrap"], detail["se_median_analytic"])
    return detail


def run_all(conn: sqlite3.Connection) -> None:
    for chamber, term in conn.execute("SELECT DISTINCT chamber, term FROM vote_event ORDER BY chamber, term").fetchall():
        run(conn, chamber, term)
