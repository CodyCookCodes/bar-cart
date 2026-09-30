# Bar Cart

**Know what you can make tonight, and what to buy next.**

Bar Cart helps home bartenders get the most out of the bottles they already own. Tell it what's on your bar cart, and it shows every cocktail you can make right now, the drinks you're one ingredient away from, and which few bottles would unlock the most new recipes.

Under the hood it's an end-to-end ELT pipeline: Python ingestion from two sources (TheCocktailDB API and the International Bartenders Association website), Snowflake as the warehouse, dbt for modeling and testing, and a small web app on top.

> **Status:** in progress. See [Roadmap](#roadmap).

---

## Questions the product answers

| # | User question | Answered by |
|---|---|---|
| 1 | What can I make with what's on my bar cart? | `mart_makeable_drinks` |
| 2 | If I buy *N* new bottles, which set unlocks the most new cocktails? | `mart_next_bottle`, `mart_bottle_bundles` |
| 3 | How do I make this drink? | `dim_drink`, `fct_recipe_step` |
| 4 | Which bar tools am I missing, and what would they unlock? | `mart_missing_tools` |

**Notes on question 2:** the app suggests 3–5 alternative bundles of *N* bottles, each with a different ingredient mix (for example, vodka + cranberry + grapefruit vs. whiskey + bitters + sweet vermouth). Checking every combination grows quickly with *N*, so bundles are built with a greedy approach: repeatedly add the bottle that unlocks the most new drinks. This is a standard approximation to the set-cover problem.

**Saving your bar:** a user's list of bottles is *app state*, not ingested data. In v1 it lives in a seed file (`my_bar.csv`). In v2 the app writes it to a table so users can update it quickly, for example after a party.

---

## Data sources

| | TheCocktailDB | International Bartenders Association (IBA) |
|---|---|---|
| **What** | Crowd-sourced cocktail database (~637 drinks) | The 102 official IBA cocktails |
| **Role** | **Main catalog.** Breadth of drinks, images, glassware | **Authority.** Official recipe, method, garnish, category |
| **How** | REST API, premium key (`cocktaildb_pull.py`) | Web scraper (`scraper.py`) |
| **Cadence** | Weekly full pull | Weekly quick check; monthly `--full` re-scrape |
| **Change handling** | Full refresh of raw; changes tracked downstream with a dbt snapshot | Master file tracks `status`, `first_seen_at`, `updated_at`, `removed_at` |
| **Known quirks** | Ingredients stored in 15 numbered columns; measures are free text (`"1 1/2 oz "`); every value is a string; tags are comma-separated; free API key caps list results at 100 | Straight vs. curly apostrophes (`Tommy's` / `Tommy’s`); three spellings of "no garnish" (`N/A`, `N/A.`, `N/A, optional…`); one source bullet contains two ingredients; recipe variations hidden in method notes; method markup differs across pages |

**When sources disagree** (the survivorship rule): for the 102 IBA drinks, IBA wins on recipe, measures, method, garnish, and category. CocktailDB supplies image, glassware, and the alcoholic flag, which IBA doesn't provide. IBA drinks missing from CocktailDB are added with IBA as their only source.

---

**Project board:** https://github.com/users/CodyCookCodes/projects/3

## Architecture

```
 EXTRACT (Python)                 LOAD (Snowflake)                 TRANSFORM (dbt)                      SERVE
 ────────────────                 ────────────────                 ───────────────                      ─────
 cocktaildb_pull.py ──► JSON ──►  stage ──► COPY INTO ──► raw.* ──► staging ──► intermediate ──► marts ──► Streamlit app
 scraper.py (IBA)   ──► JSON ──►  (PUT)                            (clean,      (match, normalize,  (answer the
                                                                   one source)  parse, unify)       questions)
```

| Layer | Owner | Job |
|---|---|---|
| **Extract** | Python | Pull from the API and website; write JSON files. Structure only, no interpretation. |
| **Load** | Snowflake `PUT` + `COPY INTO` | Land each file untouched in a raw table. Each file is a full snapshot, so each load replaces the table's contents. |
| **Staging** | dbt | One model per source entity: rename, cast types, unpack JSON, unpivot. No business logic. |
| **Intermediate** | dbt | Match the two sources, normalize ingredient names, parse garnishes and instructions, apply survivorship. |
| **Marts** | dbt | Dimensions, facts, and question-specific tables the app reads. |
| **Serve** | Streamlit | Interactive "tick your bottles" interface on top of the marts. |

**v1** runs extraction and loading by hand. **v2** moves ingestion into Snowflake: Python stored procedures (Snowpark) with an External Access Integration, scheduled by Snowflake Tasks, feeding a `MERGE`-based change-tracking table.

---

## Data model

### Raw (Snowflake, loaded untouched)

| Table | Grain |
|---|---|
| `raw.cocktaildb_drinks` | One row per drink (full API response in a `VARIANT`) |
| `raw.cocktaildb_ingredients` | One row per ingredient |
| `raw.iba_cocktails` | One row per IBA cocktail, including removed ones (flagged) |

### Seeds (hand-maintained reference data)

| Seed | Grain | Purpose |
|---|---|---|
| `ingredient_aliases` | One row per spelling | Maps variants (`fresh lime juice`, `juice of 1 lime`) to a canonical ingredient |
| `drink_name_aliases` | One row per spelling | Resolves name mismatches between sources |
| `techniques` | One row per action verb | Controlled vocabulary for instruction parsing (shake, stir, muddle…) |
| `tools` | One row per bar tool | Shaker, mixing glass, muddler, strainer… |
| `ingredient_overrides` | One row per corrected line | Manual fixes for source errors the parser can't handle |
| `my_bar` (v1) | One row per bottle owned | The user's current bar cart |

### Staging

| Model | Grain |
|---|---|
| `stg_cocktaildb__drinks` | One row per CocktailDB drink |
| `stg_cocktaildb__drink_ingredients` | One row per drink per ingredient slot (unpivoted from 15 columns) |
| `stg_cocktaildb__ingredients` | One row per CocktailDB ingredient |
| `stg_iba__cocktails` | One row per IBA cocktail |
| `stg_iba__cocktail_ingredients` | One row per IBA cocktail per ingredient line |

### Intermediate

| Model | Grain | Job |
|---|---|---|
| `int_ingredients_normalized` | One row per source ingredient spelling | Maps every raw spelling to a canonical ingredient |
| `int_drink_crosswalk` | One row per matched CocktailDB ↔ IBA pair | Entity resolution, with `match_method` and `match_confidence` |
| `int_garnish_parsed` | One row per drink per garnish component | Splits garnish text into ingredient, form, and optional flags |
| `int_drink_ingredients_unified` | One row per drink per ingredient | Applies survivorship; adds `role`, `form`, `is_optional`, `alternative_group` |
| `int_instruction_steps` | One row per drink per instruction step | Parses instructions into action, item, and tool |

### Marts

| Model | Grain | Answers |
|---|---|---|
| `dim_drink` | One row per real-world drink | Source flags, `is_iba_official`, IBA category, glass, image |
| `dim_ingredient` | One row per canonical ingredient | Type, alcoholic flag |
| `dim_tool` | One row per bar tool | |
| `fct_drink_ingredient` | One row per drink per ingredient | Recipe composition (bridge table) |
| `bridge_drink_tool` | One row per drink per required tool | |
| `fct_recipe_step` | One row per drink per step | Q3: rendered, readable steps |
| `mart_makeable_drinks` | One row per drink (per user in v2) | Q1: makeable now, or missing *k* ingredients |
| `mart_next_bottle` | One row per ingredient not yet owned | Q2: new drinks unlocked by buying it |
| `mart_bottle_bundles` | One row per bundle per ingredient | Q2: top *N*-bottle bundles |
| `mart_missing_tools` | One row per tool not yet owned | Q4: drinks unlocked by acquiring it |

**Why `role`, `form`, `is_optional`, and `alternative_group`:** "garnish with an orange zest, optionally a lemon zest" is two rows: orange (form: zest, role: garnish) and lemon (zest, garnish, optional). "Orange or lemon" shares an `alternative_group`, so owning either one satisfies the recipe. Without these columns, a user with no lemons would wrongly be told they can't make the drink.

---

## Design decisions

| Decision | Alternative considered | Why |
|---|---|---|
| **ELT:** Python extracts, Snowflake loads, dbt transforms | Transform in Python before loading | Keeps a faithful raw copy; transformations are versioned, tested SQL that can be re-run without re-scraping |
| **Two sources, with IBA as authority** | Use one source only | CocktailDB has breadth; IBA has the official specs. Matching them is a realistic entity-resolution problem. |
| **Scraper extracts structure only**; interpretation happens in dbt | Parse garnishes and units in Python | Fix *my* extraction bugs at the source; clean the *source's* quirks downstream, with tests |
| **One master IBA file with lifecycle flags** | A dated snapshot file per run | Simple to inspect and diff; keeps first-seen and removed history. Trade-off: recipe edits overwrite the previous version. |
| **Weekly quick check + monthly `--full`** | Re-scrape every page weekly | Seconds instead of minutes, and polite to the IBA site. Recipe edits are rare. |
| **Safety guard on removals** | Trust whatever the site lists | If the site suddenly lists under half the known drinks, the run aborts instead of flagging everything as removed |
| **Garnishes modeled as ingredients** with role, form, optional, and alternative columns | Free-text garnish strings | Makes "what can I make" correct for optional and either/or ingredients |
| **Raw tables fully replaced on each load** | Append each load | Each file is a full snapshot; appending would duplicate every drink weekly. History comes from snapshots and lifecycle flags. |
| **Streamlit for the app** | Tableau, Power BI | The core feature is interactive input (tick your bottles); dashboards handle that poorly, and Tableau Public can't connect to Snowflake |
| **User's bar as a seed in v1** | App-writable table | Ships the pipeline first; persistent user state is a v2 product feature |

---

## Data quality

- **dbt tests** on every model: `unique` and `not_null` on keys, `relationships` between facts and dimensions, `accepted_values` on role, status, and category
- **Coverage tests:** every drink has at least one ingredient; every ingredient spelling maps to a canonical ingredient; every instruction step parses to an action, or is listed in the overrides seed
- **Source checks:** CocktailDB list endpoints must not return exactly 100 rows (a sign the free-key cap hit); the IBA scraper reports parse warnings and page failures on every run

---

## Out of scope (for now)

- **TheMealDB** food pairings
- **Non-English instructions** (CocktailDB provides DE, ES, FR, IT, and ZH; only English is modeled)
- **Recipe variations inside IBA method notes** (White Russian, Kir Royal, Tom Collins…): logged for a future release
- **Prices and store availability** for shopping suggestions

---

## Project structure

```
bar-cart/
├── README.md
├── ingestion/
│   ├── scraper.py            # IBA scraper: weekly quick check, monthly --full
│   └── cocktaildb_pull.py    # TheCocktailDB API pull (planned)
├── snowflake/
│   └── setup.sql             # database, stages, file formats, raw tables, COPY (planned)
├── dbt/
│   ├── seeds/
│   ├── macros/
│   ├── snapshots/
│   └── models/
│       ├── staging/
│       ├── intermediate/
│       └── marts/
└── app/
    └── streamlit_app.py      # (planned)
```

---

## Running it

**IBA scraper**
```bash
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pre-commit install
python ingestion/scraper.py          # weekly quick check: adds new drinks, flags removed ones
python ingestion/scraper.py --full   # bi-monthly: re-scrapes every recipe to catch edits
```

*Instructions for the CocktailDB pull, the Snowflake load, and dbt will be added as each stage is built.*

---

## Roadmap

- [x] IBA scraper with change tracking
- [ ] TheCocktailDB ingestion
- [ ] Snowflake raw layer (stages, file formats, `COPY INTO`)
- [ ] dbt staging models and source tests
- [ ] Ingredient normalization, source matching, survivorship
- [ ] Marts and data-quality tests
- [ ] Snapshots (SCD Type 2) for CocktailDB changes
- [ ] Instruction and garnish parsing (translation bridge)
- [ ] Streamlit app
- [ ] v2: ingestion as Snowflake Tasks; app-writable bar cart

---

## Credits

Recipe data and images from [TheCocktailDB](https://www.thecocktaildb.com/). Official recipes from the [International Bartenders Association](https://iba-world.com/). Both are used for a non-commercial portfolio project; all recipe content belongs to its respective owners.

## License
MIT - see [LICENSE](LICENSE).
