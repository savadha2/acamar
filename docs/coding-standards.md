# ACAMAR — coding standards & lint/type gotchas

Concrete rules distilled from friction already hit. Follow these while writing so
`ruff check` and `mypy` pass on the first run. Config lives in
[`pyproject.toml`](../pyproject.toml); this file is the *why* and the patterns.

## Docstrings (ruff `D`, google convention)

- **D205** — one-line summary, then a blank line, then the body. Never let the
  summary wrap straight into the description.
  ```python
  """Text handed to the embedder.

  Title + content, without repeating the title when content leads with it.
  """
  ```
- **D107** — do **not** docstring `__init__`; document constructor args in the
  class docstring. (D107 is in the ignore list — adding an `__init__` docstring
  is churn, not compliance.)
- Every public module, class, and function needs a docstring. Exempt in
  `per-file-ignores`: tests entirely; `scripts/**` (no `D`); `adapters/**`
  (no `D101`/`D102` — an adapter's contract is documented on the port it
  implements, not re-stated on the impl).

## Formatting

- **100-column limit** (E501). Wrap long function signatures across lines; wrap
  long f-strings by splitting the string.
- Let `ruff format` own whitespace/quotes — don't hand-format.

## Unused tuple-unpack targets (ruff `RUF059`)

`a, b, _ = f()` where `b` is unused is flagged. Prefix every unused slot with
`_`: `service, _, doc_repo = _make(...)`.

## `pytest.raises` / regex arguments (ruff `RUF043`)

- `match=` is a **regex**. `match="robots.txt disallows"` fails RUF043 because
  `.` is a metacharacter. Use a metacharacter-free substring (`match="disallows"`)
  or a raw string with escapes (`match=r"robots\.txt"`). Same for any
  `re.*` / `assertRaisesRegex` pattern.

## `# type: ignore` — add only after mypy actually complains

mypy runs with `strict = true`, which includes `warn_unused_ignores`. A
speculative `# type: ignore[code]` that turns out unnecessary is itself an
error (`unused-ignore`). Workflow: write the code with no ignores, run mypy,
add an ignore **only** for what it flags, with the specific code it reports.

Things that turned out **not** to need an ignore:
- `Settings(_env_file=None)` — pydantic-settings types the underscore kwargs.
- `RawDocument(**some_dict)` — the pydantic mypy plugin accepts `**dict`.

## mypy `no-any-return`

A typed function that returns the result of an `Any`-typed call (e.g.
`json.loads`, an untyped library) trips `no-any-return`. Wrap it:
```python
return cast("list[dict[str, Any]]", json.loads(raw))
```

## mypy `comparison-overlap` — don't compare an enum to a literal

Even for `StrEnum`, mypy rejects `Domain.SCHOOL == "school"` as non-overlapping.
Assert the value instead:
```python
assert Domain.SCHOOL.value == "school"
assert str(Category.EVENT) == "event"
```
Application code should compare `Domain` to `Domain`, never to a bare string.

## `S105`/`S106` on non-secret constants

ruff (bandit) flags any string literal assigned to a name containing `token`,
`secret`, `password`, `key`, etc. — even a public value. `TOKEN_PREFIX =
"acamar_"` is such a false positive. Suppress narrowly with a reason:
`# noqa: S105 - a public label, not a secret`. Real secrets never belong in
source at all (they come from env / the `subscribers` table).

## Broad `except` (ruff `BLE001`)

`except Exception` is allowed **when the handler re-raises** (our connector
boundary does: `raise ConnectorError(...) from exc`). BLE001 does not fire on
re-raising handlers, so **do not** add `# noqa: BLE001` there — `ruff --fix`
(and editor-on-save) will strip it as an unused noqa. A broad `except` that
*doesn't* re-raise is a real smell — narrow it.

## Project patterns that avoid whole classes of errors

- **`from __future__ import annotations`** at the top of every module — defers
  annotation evaluation, lets you use `X | None` and forward refs freely.
- **No module-level singletons that do I/O.** `config.py` exposes
  `@lru_cache def get_settings()`, not `settings = Settings()`. Importing a
  module must have no side effects; tests call `get_settings.cache_clear()`.
- **Test fixtures (HTML, JSON) live in `tests/fixtures/`,** not in the package —
  they must not ship in the wheel. Load them via a path relative to
  `conftest.py`.
- **pytest runs `--import-mode=importlib`** and `tests/` is a real package
  tree (`__init__.py` in every dir). Modules get full dotted names
  (`tests.unit.test_x`) so same-named files never collide, and shared helpers
  import cleanly (`from tests.fakes import FakeEmbedder`). mypy needs the
  package tree too, or it sees `tests/fakes` as both `fakes` and `tests.fakes`.
  `pythonpath = ["."]` puts the repo root on `sys.path` for those imports;
  `acamar` still resolves to the installed editable package.
- **`@runtime_checkable` Protocol `isinstance` is shallow** (attribute presence
  only). Real conformance is the connector contract suite
  (`tests/contract/`), not an `isinstance` assert.
- **All annotations required** (ruff `ANN`) in `src/` — including return types,
  fixture return types (`Iterator[None]` for a yield fixture), and `-> None`.
