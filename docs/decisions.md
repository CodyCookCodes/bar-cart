# Decision log

Architecture decisions for Bar Cart, newest last. Each entry records the context, the decision, the alternatives considered, and the trade-offs accepted.

---

## ADR-001: v1 uses two sources, with IBA as the authority
**Date:** 2026-09-28 · **Status:** Accepted

**Context:** TheCocktailDB has breadth (~637 drinks) but crowd-sourced, inconsistent recipes. The IBA publishes 102 official specs but nothing else: no images, no glassware, no alcoholic flag.

**Decision:** Use both in v1. TheCocktailDB is the main catalog; for drinks in both sources, the IBA recipe wins on measures, method, garnish and category.

**Alternatives considered:**
- IBA only: trustworthy, but too small a catalog to make "what can I make?" useful
- TheCocktailDB only: broad, but recipe quality is inconsistent even for the classics

**Consequences:**
- ✅ Broad catalog with trustworthy specs for the classics
- ✅ Matching the two sources is a real entity-resolution problem
- ⚠️ More work in v1: a second ingestion script and a matching model
- ⚠️ Unmatched or ambiguous drinks need a review path instead of a guess

---

## ADR-002: One master IBA file with lifecycle flags
**Date:** 2026-09-30 · **Status:** Accepted

**Context:** The IBA list changes rarely but without notice: drinks are added, removed, or have their specs edited. The scraper needs to track what appeared, changed or disappeared from week to week, and a person should be able to see that history easily.

**Decision:** The scraper maintains a single master file, `iba_cocktails.json`, with one record per cocktail ever seen. Each record carries lifecycle columns: `status`, `first_seen_at`, `last_seen_at`, `updated_at` and `removed_at`. Removed drinks are flagged, never deleted. The file is committed to git.

**Alternatives considered:**
- A dated snapshot file per run (`scraped_at=YYYY-MM-DD/`): full history, but a folder that grows every week, and answering "what changed?" means diffing files by hand

**Consequences:**
- ✅ One file to inspect; `git log -p` on it shows every weekly change
- ✅ First-seen and removal history is kept in the data itself
- ✅ Loading is simple: each load replaces the raw table with the current file
- ⚠️ A recipe edit overwrites the previous version in the file. Older versions live in git history, and later in a dbt snapshot (BC-46).
- ⚠️ A bad scrape could wrongly mark drinks as removed. Mitigation: a safety guard aborts the run if the site lists fewer than half of the known drinks.