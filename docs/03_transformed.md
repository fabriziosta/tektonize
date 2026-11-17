# Step 3 — Transform (Groovy → Python / Tekton shapes) → `03_transformed/`

**Purpose:** Describe the **semantic rewrite** from Groovy/Jenkins idioms to
Python and Tekton-oriented abstractions. Transformers live under
`src/tektonize/transformer/` and are applied primarily while emitting (step 4);
this stage’s on-disk folder currently stores a **summary snapshot** for tracing.

**Primary code:**
- `transformer/name_converter.py` — identifiers and file names
- `transformer/type_mapper.py` — Groovy/Java types → Python types
- `transformer/class_transformer.py` — enum / interface / class → Python shapes
- `transformer/expression_transformer.py` — Elvis, safe-nav, GStrings, …
- `transformer/method_transformer.py` — `self`, body lines, getters/setters
- `transformer/import_transformer.py` — package imports → Python imports
- `transformer/jenkins_dsl_transformer.py` — Jenkins **DSL** → TaskStep / TODOs
- `transformer/jenkins_bindings.py` — Pipeline global binding names + unbound `params.*` harvest

**Output directory:** `03_transformed/` (JSON summary: source path, class names,
import count, top-level method names)

## Acronyms for this stage

| Term | Meaning here |
|------|----------------|
| **DSL** | Jenkins Pipeline Domain-Specific Language (`sh`, `echo`, `stage`, …). |
| **AST** | Transformers operate on typed AST nodes from step 2 (and on method body text). |
| **ABC** | Target for Groovy `interface` → Python `ABC`. |
| **POJO** | Target for simple classes → `@dataclass`. |
| **TEKTONIZE-TODO** | Marker left when a construct cannot be mapped cleanly. |

## What each transformer family does

### Name conversion

- `PascalCase` type/file names → `snake_case` Python modules
  (`DeploymentType.groovy` → `deployment_type.py`).
- `camelCase` members → `snake_case` where rewritten (declarations /
  method names). **Attribute access** in method bodies may stay camelCase
  (valid Python; often Map/YAML keys such as `config.unsupportedVersions`)
  and must **not** be commented as `# GROOVY:` solely for that reason.

### Type mapping

Examples: `String` → `str`, `boolean`/`Boolean` → `bool`, `Map` → `dict`,
`List<X>` → `list[X]`, `void` → `None` in return positions (see `TypeMapper`).

### Class transformation

- `enum` → Python `Enum`
- `interface` → `ABC` with `@abstractmethod`
- Ordinary classes / POJOs → `@dataclass` when they look like data holders;
  otherwise plain classes with methods

### Expression transformation

- `?:` (Elvis) and ternary → Python conditionals
- `?.` safe navigation → guarded access patterns / comments
- `"${x}"` GString → f-strings where recognized; shell-escaped `\${VAR}` stays
  a plain string (`"${VAR}"`), never invalid `\{VAR}`
- `null` → `None`, `this.` / bare `this` → `self`, `instanceof` → `isinstance`
- Groovy map literals → Python dicts (identifier **and** string keys):
  - `[:]` → `{}`
  - single-line `[a: 1, b: 2]` → `{"a": 1, "b": 2}`
  - string keys: `['total': 0]` / `['ui-controller': […]]` → dicts
  - multiline / nested maps rewritten recursively (`return [`, assignments,
    nested value maps, **and class/field defaults** via the emitter)
  - bare keys stay camelCase (quoted) for Map/YAML fidelity; already-quoted
    keys (including hyphenated names) are kept; line structure, `//`
    comments, and trailing commas are preserved
- Named args inside `(…)` → Python kwargs:
  `choice(name: 'X', choices: ['a'])` → `choice(name='X', choices=['a'])`
  (dict entries inside `{…}` are untouched)
- Collection / Map idioms:
  - `map.containsKey(k)` → `(k in map)`
  - `map.getOrDefault(k, d)` → `map.get(k, d)`
  - `map.putAll(other)` → `map.update(other)`
  - simple `.each { x ->` / `.each { k, v ->` → `for` loops
  - `list << x` → `list.append(x)`; `{…} << expr` / `ident << {…}` → `{**…, **expr}`; `mapA + mapB` / `map + {…}` → `{**a, **b}`
  - Elvis `?: [:]` → `or {}`; `(expr) ?: default` → `(expr or default)` (before ternary)
  - `x ==~ /re/` / `x ==~ "…"` (dotted LHS ok) → `re.match(…)`; GString `f` not applied for bare regex `$`; no `ff"` on re-entry
  - `coll.collect { it.prop }.join(sep)` → `sep.join(it.prop for it in coll)`
  - Typed locals with nested generics (`Map<String, List<Map>> name =`) → `name = …`
    (requires whitespace on **both** sides of `+`; shell lines like `set +e` /
    builtin LHS names are never rewritten)
  - Triple-quoted `"""…"""` / `'''…'''` blocks are left untouched by GString
    rewrite (avoids mangling `script: """` into `script=""f"`)

### Method transformation

- Inject `self` for instance methods
- Strip `@NonCPS` (Jenkins **CPS** opt-out) from the Python view
- Convert or comment body lines; wrap unconverted Groovy as `# GROOVY: …`
- Local / parameter camelCase names rewritten to snake_case on **declarations
  and bare uses** (e.g. `commandLogName` → `command_log_name` in assignments,
  `if`, returns, and f-string `{…}` interpolations). Locals are discovered from
  the original Groovy body (before Jenkins `sh` rewrite can drop
  `def exitCode = script.sh(`). Attribute access (`config.commandLogName`) and
  string map keys (`"archiveLog"`) stay camelCase
- Validity checks do **not** treat camelCase `.attr` access as Groovy leftover
  (that was a false positive for Map keys / unconverted field names)
- Do **not** flag mid-identifier PascalCase substrings via `[A-Z]\w*(` —
  that falsely rejected `getOrDefault`, `putAll`, `checkProvisionInventory`;
  `Yaml()`-style constructors stay live Python after `new` strip
- Allow **line-start** `PascalCase(` as live Python (entry-point / constructor
  calls such as `BuildPipeline(config)`, `Yaml()`); mid-identifier
  matches remain rejected only via other Groovy indicators
- Skip re-running `ExpressionTransformer` on already-emitted `# TaskStep` /
  `# | …` / TODO / GROOVY lines (prevents shell `set +e` → map-merge)
- After map→dict rewrite, accept `return {`, quoted `"key":` entry lines, and
  `}` / `},` closers as live Python; still reject unconverted `return [`
- Track paren/bracket depth so multiline `raise Foo(`, call args, and closers
  `)` / `])` stay live; orphan closers at depth 0 are still dropped
- Absorb orphan punctuation (`""")`, `}})`, `}}`, `}) {`, stub `pass` /
  `else:`) that only closes a prior `# GROOVY:` / `# TEKTONIZE-TODO:` /
  `# TaskStep` line or a finished live dict entry / statement
- Revive `# GROOVY: else:` to live `else:` when a matching live `if` exists at
  the same indent
- After stripping the method’s outer `{…}`, **uniformly dedent** the body (min
  indent of non-empty lines) so sibling statements are not left nested under the
  first `if` / `for` (a plain `strip()` only fixed the first line)
- Ensure block bodies / indent clamps so emitted Python is more likely to compile
- Leave `pass  # TEKTONIZE-TODO: …` when no valid body lines remain

### Import transformation

- Map Groovy/Java import paths to Python module paths for **library** packages
- Skip pure Jenkins / Groovy runtime imports that have no Python equivalent

### Logical-line spanning (before line-oriented rewrite)

`PythonEmitter._transform_method_body` first **unwraps** the method’s outer
`{…}` (`MethodTransformer._unwrap_method_body`), then runs
`parser.span_join.join_open_spans` **before** expression / Jenkins DSL / method
transformers. Joining before unwrap treated the leading `{` as an open map span
and flattened entire method bodies into meglines.
A string- and comment-aware scanner joins unfinished spans onto one line:

- Call / map-arg: unbalanced `(` or `[` (e.g. `foo([` … `])`)
- Map/dict literal: unbalanced `{` that is **not** a block opener (`) {`, `else {`, …)
- `if` / `for` / `while` conditions split across lines

Triple-quoted regions keep their newlines; a bare `"""` / `'''` opener after a
wrapper is not pulled into the preceding call span (TaskStep path owns sh bodies).
This reduces mid-span `# GROOVY:` map-entry cascades without minifying whole methods.

### Jenkins DSL transformation

| Jenkins | Typical mapping |
|---------|-----------------|
| `sh` / `script.sh` / `self.script.sh` | `# TaskStep(script=…)` (multiline `"""` bodies consumed; named-arg `script: """` / `script="""` form + trailing kwargs / `)`; optional `exitCode =` assignment) |
| `echo` / `script.echo` / `self.script.echo` | `print(…)` |
| `error` / `script.error` / `self.script.error` | `raise RuntimeError(…)` |
| `stage` / `script.stage` / `self.script.stage` / `builder.stage` | `# --- Stage: … ---` (any args) |
| `self.script.junit` / `archiveArtifacts` / `writeYaml` / `checkout` / … | `# TEKTONIZE-TODO: script.<step> → …` |
| Mid-line `script.fileExists` / `readYaml` / `readFile` / `write*` / `string` / `usernamePassword` / `file` / `sshagent` | whole-line `# TEKTONIZE-TODO:` (credentials / workspace); closure tails absorbed |
| `script.dir` / `stagesClosure={` | `# TEKTONIZE-TODO: …` |
| leftover `}) {` after with* map-args | `# TEKTONIZE-TODO: …` |
| `obj.method({…})` dict-arg calls | live Python when valid; else `# TEKTONIZE-TODO: call-with-map …` absorbing map-entry/closer GROOVY cascades |
| `builder.buildAndExecute(` / failed `foo(` openers | `# TEKTONIZE-TODO: call-with-args …` absorbing the argument/closure tail (prevents emit compile-sanitizer GROOVY cascades) |
| `(self.)?script.env.FOO` / `script.env[…]` / `script.currentBuild` | rewritten to live `env.FOO` / `env[…]` / `currentBuild` (bindings policy; no value substitution) |
| `script = context.script … script.stage(` meglines / embedded `script.stage` | `# TEKTONIZE-TODO:` (absorb brace depth) |
| other `script.*` / `self.script.*` | `# TEKTONIZE-TODO: script.* Jenkins binding` |
| `withCredentials` / `withEnv` | `# TEKTONIZE-TODO: …` (special-cased SecretRef / env) |
| Any `with\w+` / `sh\w+` closure opener (structural; not a product allowlist) | `# TEKTONIZE-TODO: <name> → Tekton wrapper…` early; map-arg tails / `}) {` absorbed; following `"""` → `# TaskStep`; shell lines → `# \|`; optional `--dsl-hints` only changes the message text |
| `stageWrapper` (exact; non-with*/sh* helper) | `# TEKTONIZE-TODO: …` |
| Other Jenkins/plugin exact names in `TODO_STEPS` (`build`, `withVault`, …) | `# TEKTONIZE-TODO: …` |
| `return script.choice/buildDiscarder/parameters/…` Job DSL helpers | `# TEKTONIZE-TODO: script.<step> → Job DSL / Tekton param` |
| `(context.)?script.params` | rewritten to live `params` (names only) |
| Leaked shell `START=$(date…` / `END=` / `print($((…))` outside TaskStep | `# TEKTONIZE-TODO: shell timing body …` |
| `ansible-*` / `ansible …` / `source …` / bare `sshagent(` / inventory `host ansible_user=` | after with*/TaskStep/`# \|` → `# \| …`; else `# TEKTONIZE-TODO: shell command body` (line-start only) |
| Ternary arms with typed decls / `return` / blocks / `valueOf… if {` meglines | `# TEKTONIZE-TODO: ternary-with-decls …` |
| Groovy `switch` / `case` / `default` (brace-style) | `# TEKTONIZE-TODO: switch/case → match/case` (whole span absorbed; not half-converted to live `match`/`case`) |
| `pipeline {` / `namespaces {` / `stages {` / `post {` / `always {` / `steps {` / `script {` / Job DSL `description = '''…'''` / GitSCM `$class` | `# TEKTONIZE-TODO: … (not converted)` — nested body lines absorbed as TODO until brace depth returns to 0 (including meglines with content after the opener) |

Unsupported steps are never silently dropped; they become searchable TODO
markers (`tektonize.markers`).

### Expression / method polish (brace maps, glued controls, `<<`, collect)

- Unquoted brace maps `{ deploymentType: X, … }` get quoted keys (string-aware,
  only when the `{…}` starts with a map entry — not control bodies).
- `{} + pipeline_params` / `mapA + mapB` → `{**{}, **pipeline_params}`-style merges.
- `list << f"…"` → `list.append(f"…")`; `map << call(…)` → `map.update(call(…))`.
- Glued `for x in xs:stmt` / `if cond:stmt` (no space after `:`) are split into
  header + indented body; multi-stmt residue becomes one TODO.
- `if (cond) { stmt }` one-liners split to `if cond:` + indented body; multi-stmt
  brace-if residue becomes TODO.
- `collect { "…/${it}" }` / `.map(lambda it: f"…{it}"}` close braces string-aware
  so f-string `{it}` is not corrupted to `{it)`.
- Thin vars `def some_pipeline_*(…):` stay live (`def x =` still flagged as Groovy).
- Pre-parse safe-nav `((lambda _o: getattr(_o, …) …))` expansions are allowlisted
  in method-body validity checks when delimiters are balanced.
- Bare `"""` / `'''` delimiters stay live so `x = """` … `"""` assignments close
  (commenting the closer as `# GROOVY: """` opens a string and the emit sanitizer
  cascades GROOVY across the file).
- While a live `x = """` assignment is open, body lines (YAML/shell/prose) stay
  live string content — never `# GROOVY:`.
- Juxtaposed typed locals (`String a = … String b = …` / `def x = … def y = …`)
  are split before validity checks.
- Orphan `# GROOVY: elif …:` is revived when a live `if` matches indent; otherwise
  `# TEKTONIZE-TODO: orphan elif`. Nested `def foo(self,…):` inside a method body
  becomes `# TEKTONIZE-TODO: nested method decl` (AST bleed).
- `map.findAll { k, v -> cond }` → dict comprehension; `collectEntries` /
  half-emitted `.filter(lambda it: k, v ->` → one TODO (not a GROOVY cascade).
- `x++` / `x--` → `x += 1` / `x -= 1`; `list << f"…"` → `list.append` (trailing
  `}` noise stripped). `@Library` inside f-strings is not flagged as Groovy `@`.
- Assignment ternaries `lhs = cond ? a : b` → `lhs = (a if cond else b)` (the
  `=` is not swallowed into the condition). Residue `lhs = mid else rhs`
  (missing `if`) is revived when `mid` is a simple name, else one TODO.
- Split-ternary TODO `… if` plus following `lhs = cond else rhs` are rejoined
  into one live ternary.
- Numeric `+` (`i + 1`, `result.pass + result.fail`) stays arithmetic; map
  `+` / `results + [k: v]` still becomes `{**…}`.
- `xs.find { pred }` → `next((x for x in xs if pred), None)`; `=~` →
  `re.search`; `x[0..-n]` → Python slice; `m[k]++` → `m[k] += 1`.
- `def x = script.sh( script="""` span-join meglines become `# TaskStep` +
  `# |` body (typed/`def` prefix allowed on the opener).
- Job DSL `definition {` / `buildDiscarder` / `stages[…] = {` / leftover
  `with*` GROOVY spans collapse to one `# TEKTONIZE-TODO` each.
- Emitter post-sanitize reclassifies field-default dict peels, shell
  fragments (`# |`), Job DSL leftovers, and prose/help text out of
  `# GROOVY:` (TODO / `#` / `# |`) so class-level literals are not counted
  as conversion failures.

### Jenkins global bindings (names only)

`transformer/jenkins_bindings.py` lists well-known Pipeline script globals
(`params`, `env`, `currentBuild`, `scm`, `docker`). Method-body validation still
masks `binding.attr` for those globals when needed; camelCase / UPPER_SNAKE
attribute access is otherwise left as live Python (not `# GROOVY:`).
`(self.)?script.env` / `script.currentBuild` are rewritten to those globals
before validity checks (see table above).

Convert does **not** accept or substitute Jenkins global/job parameter *values*.
Concrete binding is out of scope (job-invoker / manual PipelineRun).

## On-disk stage artifact vs runtime

Today `Converter` writes a **lightweight JSON summary** to `03_transformed/`
for each file (classes / imports / top-level methods). The heavy rewrite runs
inside `PythonEmitter.emit_file` (step 4). If transform persistence is expanded
later (full post-transform AST dumps), update this document and the README
mermaid in the same change.

## Downstream

Step 4 materializes Python source from the typed AST + these transformers.
