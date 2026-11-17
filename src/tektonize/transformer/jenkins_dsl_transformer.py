"""Transform Jenkins DSL calls to Tekton task library equivalents.

Maps: script.sh() -> TaskStep(script=...), script.stage() -> task boundary,
withCredentials -> SecretRef, withEnv -> env dict, catchError -> on_error='continue',
echo -> print/logging. Unsupported steps -> TEKTONIZE-TODO markers.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from tektonize.markers import TODO_PREFIX, todo_comment


@dataclass
class JenkinsDSLCall:
    """A detected Jenkins DSL call."""

    step_name: str = ""
    arguments: str = ""
    has_closure: bool = False
    line: str = ""


@dataclass
class TektonMapping:
    """Result of mapping a Jenkins DSL call to Tekton."""

    python_code: str = ""
    todo_marker: str = ""
    secret_refs: list[str] = field(default_factory=list)
    is_stage_boundary: bool = False


# Jenkins steps -> Python/Tekton equivalents
STEP_MAPPINGS: dict[str, str] = {
    "sh": "TaskStep(script={args})",
    "echo": "print({args})",
    "error": "raise RuntimeError({args})",
    "sleep": "# sleep({args})",
    "writeFile": "# write_file({args})",
    "readFile": "# read_file({args})",
    "readJSON": "json.loads({args})",
    "archiveArtifacts": (
        f"# {TODO_PREFIX} archiveArtifacts -> workspace result\n# archive_artifacts({{args}})"
    ),
    "junit": f"# {TODO_PREFIX} junit -> Tekton task result\n# junit({{args}})",
    "cleanWs": f"# {TODO_PREFIX} cleanWs -> workspace cleanup\n# clean_workspace({{args}})",
    "checkout": f"# {TODO_PREFIX} checkout -> git-clone task\n# checkout({{args}})",
    "unstash": f"# {TODO_PREFIX} unstash -> workspace\n# unstash({{args}})",
    "stash": f"# {TODO_PREFIX} stash -> workspace\n# stash({{args}})",
}

# Jenkins / plugin steps that get TODO markers (exact names).
# Library-specific with*/sh* wrappers are handled structurally — see
# ``_WRAPPER_OPENER_RE`` — not by listing product method names here.
TODO_STEPS: dict[str, str] = {
    "build": "script.build(job:) requires job-invoker converter to create separate PipelineRun",
    "libraryResource": "script.libraryResource() -> map to Tekton configMap or workspace file",
    "httpRequest": "script.httpRequest() -> replace with curl in task step or dedicated HTTP task",
    "slackSend": "slackSend -> Tekton finally task or notification mechanism",
    "input": "input step -> manual approval task (Tekton CustomRun or Pipeline approval gate)",
    "emailext": "emailext -> notification task",
    "timeout": "timeout -> Tekton task timeout field",
    "retry": "retry -> Tekton task retries field",
    "parallel": "parallel -> Tekton Pipeline parallel tasks",
    "lock": "lock -> Tekton concurrency controls",
    "milestone": "milestone -> Tekton Pipeline ordering",
    "properties": "properties() -> Tekton PipelineRun params/triggers",
    "node": "node() -> Tekton task runs on any available pod by default",
    "podTemplate": "podTemplate -> Tekton task pod template",
    "container": "container() -> Tekton task step image",
    "ansiColor": "ansiColor -> no equivalent needed",
    "timestamps": "timestamps -> Tekton logs include timestamps natively",
    "warnError": "warnError -> try/except with continue-on-error",
    "catchError": "catchError -> onError: continue",
    "withVault": "withVault() -> map to Tekton Vault sidecar or External Secrets Operator",
    "wrap": "wrap([$class: 'BuildUser']) -> no equivalent; user info from trigger context",
    "addInfoBadge": "addInfoBadge -> no equivalent; use Tekton task result annotations",
    "removeBadges": "removeBadges -> no equivalent",
    "reportResults": "reportResults -> custom Tekton task for results upload",
    "stepReportResults": "stepReportResults -> custom Tekton task for results upload",
    # Non-with*/sh* library helpers still matched by exact name (vendor-neutral).
    "stageWrapper": "stageWrapper -> Tekton task / stage boundary",
}


class JenkinsDSLTransformer:
    """Transform Jenkins DSL calls in method bodies to Tekton equivalents."""

    # Optional ``self.script.`` / ``script.`` / bare step prefix.
    _SCRIPT_PREFIX = r"(?:self\.)?(?:script\.)?"

    # CPS-style wrappers: ``(self.|this.|steps.foo.)?(with\w+|sh\w+)(…) {``.
    # ``sh\w+`` requires chars after ``sh`` so bare Jenkins ``sh`` is excluded.
    # ``withCredentials`` / ``withEnv`` are handled specially before this runs.
    _WRAPPER_OPENER_RE = re.compile(
        r"^(?:(?:self|this)\.)?(?:(?:\w+\.)*)?"
        r"(?P<name>with\w+|sh\w+)\b"
        r".*?"
        r"(?:\(\s*\{\s*$|\(\s*\[\s*$|\{\s*$|\[\s*$|\(\s*$)"
    )

    # Core Jenkins steps that must not use the generic wrapper path.
    _WRAPPER_SPECIAL = frozenset({"withCredentials", "withEnv"})

    # ``self.script.junit`` / ``archiveArtifacts`` / … → TODO (consume, not GROOVY).
    _SCRIPT_TODO_STEPS = frozenset(
        {
            "junit",
            "archiveArtifacts",
            "writeYaml",
            "writeFile",
            "readFile",
            "readJSON",
            "readYaml",
            "writeJSON",
            "fileExists",
            "checkout",
            "unstash",
            "stash",
            "cleanWs",
            "usernamePassword",
            "sshagent",
            "string",
            "file",
            # Job DSL helpers often wrapped as ``return script.choice(…)``.
            "buildDiscarder",
            "logRotator",
            "parameters",
            "booleanParam",
            "choice",
            "text",
            "pipelineTriggers",
            "cron",
            "parameterizedCron",
            "disableConcurrentBuilds",
        }
    )

    _JOB_DSL_SCRIPT_RE = re.compile(
        r"^(?:return\s+)?(?:(?:self|context)\.)?script\.(?:"
        r"buildDiscarder|logRotator|parameters|booleanParam|choice|text|"
        r"pipelineTriggers|cron|parameterizedCron|disableConcurrentBuilds|"
        r"writeJSON"
        r")\s*\("
    )

    # IO / credential / artifact helpers — whole line → TODO even mid-expression.
    _SCRIPT_IO_CRED_RE = re.compile(
        r"\b(?:self\.)?script\.(?:"
        r"fileExists|readYaml|readFile|writeYaml|writeFile|readJSON|"
        r"string|usernamePassword|file|sshagent|"
        r"archiveArtifacts|junit|checkout|unstash|stash|cleanWs"
        r")\s*\("
    )

    # Shell / ansible command lines (line-start only — not string mentions).
    _SHELL_CMD_RE = re.compile(
        r"^(?:"
        r"ansible-[\w-]+|ansible\s|"
        r"source\s|sshagent\s*\(|"
        r"[\w.-]+\s+ansible_user="
        r")"
    )

    def __init__(self, dsl_hints: dict[str, str] | None = None) -> None:
        # When set, consuming lines of a multiline ``sh """…"""`` body.
        self._sh_quote: str | None = None
        # Held ``exitCode = script.sh(`` line awaiting ``script: """``.
        self._sh_held_opener: str | None = None
        # After closing ``"""``, consume trailing kwargs / ``)``.
        self._sh_trailer: bool = False
        # Consuming map-arg tails after ``withX({`` → TODO.
        self._with_map_todo: bool = False
        # Absorb leaked shell timing bodies (START=/END=) outside TaskStep.
        self._shell_leak_todo: bool = False
        # Job DSL / Declarative / GitSCM block absorb (brace depth).
        self._block_todo_depth: int = 0
        # Job DSL ``description = '''…'''`` body absorb.
        self._desc_quote: str | None = None
        # After with*/sh* + ``"""`` body, absorb the trailing closure ``}``.
        self._expect_wrapper_close: bool = False
        # Prior emit was TaskStep / with* TODO / ``# |`` — shell cmds become ``# |``.
        self._shell_body_ctx: bool = False
        # Explicit flag: last emit was a structural with*/sh* wrapper TODO.
        self._after_wrapper_todo: bool = False
        # Optional stepName -> TODO message overlays (wording only).
        self._dsl_hints: dict[str, str] = dict(dsl_hints or {})

    def transform_body(self, body_text: str) -> str:
        """Transform all Jenkins DSL calls in a method body."""
        self._sh_quote = None
        self._sh_held_opener = None
        self._sh_trailer = False
        self._with_map_todo = False
        self._shell_leak_todo = False
        self._block_todo_depth = 0
        self._desc_quote = None
        self._expect_wrapper_close = False
        self._shell_body_ctx = False
        self._after_wrapper_todo = False
        lines = body_text.split("\n")
        result_lines: list[str] = []

        for line in lines:
            for transformed in self._transform_line(line):
                result_lines.append(transformed)
                self._nudge_shell_body_ctx(transformed)

        # Flush a held opener that never saw ``script: """``.
        if self._sh_held_opener is not None:
            for transformed in self._emit_held_opener_as_normal():
                result_lines.append(transformed)
            self._sh_held_opener = None

        return "\n".join(result_lines)

    def _emit_held_opener_as_normal(self) -> list[str]:
        """Re-process a held ``sh(`` line without holding it again."""
        held = self._sh_held_opener
        self._sh_held_opener = None
        if held is None:
            return []
        return self._transform_line_inner(held, allow_hold=False)

    def _transform_line(self, line: str) -> list[str]:
        """Transform a line; may flush a previously held ``sh(`` opener first."""
        stripped = line.strip()

        # Held opener + ``script: """`` → drop opener, start TaskStep.
        if self._sh_held_opener is not None:
            if re.match(
                r"^script\s*[:=]\s*(\"\"\"|\'\'\')\s*$", stripped
            ) and not stripped.startswith(("//", "#")):
                self._sh_held_opener = None
                return self._transform_line_inner(line, allow_hold=False)
            # Not a named script opener — emit held, then this line.
            held = self._sh_held_opener
            self._sh_held_opener = None
            out = self._transform_line_inner(held, allow_hold=False)
            out.extend(self._transform_line_inner(line, allow_hold=True))
            return out

        return self._transform_line_inner(line, allow_hold=True)

    def _transform_line_inner(self, line: str, *, allow_hold: bool) -> list[str]:
        """Transform a single line containing potential Jenkins DSL calls."""
        stripped = line.strip()
        indent = self._get_indent(line)

        # Job DSL multiline description body.
        if self._desc_quote is not None:
            if not stripped:
                return []
            if self._desc_quote in stripped:
                self._desc_quote = None
            return [
                todo_comment(
                    "Job DSL description continuation (not converted)",
                    indent=indent,
                )
            ]

        # Job DSL / Declarative / GitSCM block body → TODO (consume nested lines).
        if self._block_todo_depth > 0:
            if not stripped:
                return []
            self._block_todo_depth += stripped.count("{") - stripped.count("}")
            if self._block_todo_depth < 0:
                self._block_todo_depth = 0
            return [
                todo_comment(
                    "Job DSL / Declarative / GitSCM block continuation",
                    indent=indent,
                )
            ]

        # Continue a multiline sh / GString body as TaskStep comment lines
        # (must run before shell-leak detection so START=/END= stay in the body).
        if self._sh_quote is not None:
            if self._sh_quote in stripped:
                self._sh_quote = None
                self._sh_trailer = True
                return [f"{indent}# TaskStep(... {stripped})"]
            if not stripped or stripped.startswith("//") or stripped.startswith("#"):
                if stripped.startswith("//"):
                    return [indent + stripped.replace("//", "#", 1)]
                return [line]
            return [f"{indent}# | {stripped}"]

        # Leaked shell timing / arithmetic outside an open TaskStep.
        if self._shell_leak_todo:
            if not stripped:
                return []
            if (
                re.match(
                    r"^(START|END|SECONDS)=",
                    stripped,
                )
                or "print($((" in stripped
                or stripped.startswith("echo $((")
                or re.match(r"^#\s*\|", stripped)
                or stripped in ('"""', "'''", '""")', "''')")
            ):
                if stripped in ('"""', "'''", '""")', "''')"):
                    self._shell_leak_todo = False
                return [
                    todo_comment(
                        "shell timing body -> part of prior TaskStep / wrapper TODO",
                        indent=indent,
                    )
                ]
            self._shell_leak_todo = False
            # Fall through.

        if re.match(
            r"^(?:(?:START|END)=\\?\$\(date|SECONDS=|print\(\\?\$\(\(|echo\s+\\?\$\(\()",
            stripped,
        ) and not stripped.startswith(("//", "#")):
            self._shell_leak_todo = True
            return [
                todo_comment(
                    "shell timing body -> TaskStep script fragment",
                    indent=indent,
                )
            ]

        # ansible-playbook / source / sshagent / inventory host lines.
        if self._SHELL_CMD_RE.match(stripped) and not stripped.startswith(("//", "#")):
            if stripped.rstrip().endswith("{") or ") {" in stripped:
                depth = stripped.count("{") - stripped.count("}")
                if depth > 0:
                    self._block_todo_depth = depth
            if self._shell_body_ctx or self._sh_quote is not None:
                return [f"{indent}# | {stripped}"]
            return [
                todo_comment(
                    "shell command body -> TaskStep script fragment",
                    indent=indent,
                )
            ]

        # Map-arg tails after ``withFailureNotification({`` / ``([`` TODO opener.
        if self._with_map_todo:
            if not stripped:
                return []
            # Entry lines, closers ``}) {`` / ``})`` / ``] ) {``, nested debris.
            if (
                re.match(r"^(['\"]).*\1\s*:", stripped)
                or re.match(r"^[\w.]+\s*:", stripped)
                or re.match(r"^\}?\s*\)\s*\{?\s*$", stripped)
                or re.match(r"^\]\s*\)\s*\{?\s*$", stripped)
                or stripped in ("})", "}) {", "])", "]) {", "},", "}")
                or stripped.startswith("#")
            ):
                done = bool(
                    re.match(r"^\}?\s*\)\s*\{?\s*$", stripped)
                    or re.match(r"^\]\s*\)\s*\{?\s*$", stripped)
                    or stripped in ("})", "}) {", "])", "]) {")
                )
                if done:
                    self._with_map_todo = False
                return [
                    todo_comment(
                        "with* map-arg continuation -> part of prior with* TODO",
                        indent=indent,
                    )
                ]
            self._with_map_todo = False
            # Triple-quoted shell body right after with*/sh* wrapper opener.
            if stripped in ('"""', "'''") or re.match(r"^(\"\"\"|\'\'\')\s*$", stripped):
                self._sh_quote = stripped[:3]
                self._expect_wrapper_close = True
                return [f"{indent}# TaskStep(script={self._sh_quote}"]
            # Fall through — body of the with* closure.

        # Complete ``withX(args) {`` / ``shY(…) {`` then bare ``"""`` → TaskStep.
        if self._after_wrapper_todo and (
            stripped in ('"""', "'''") or re.match(r"^(\"\"\"|\'\'\')\s*$", stripped)
        ):
            self._sh_quote = stripped[:3]
            self._expect_wrapper_close = True
            return [f"{indent}# TaskStep(script={self._sh_quote}"]

        if self._expect_wrapper_close and stripped in ("}", "})", "}) {"):
            self._expect_wrapper_close = False
            return [
                todo_comment(
                    "with*/sh* closure closer -> absorbed into prior wrapper TODO",
                    indent=indent,
                )
            ]

        # Trailing kwargs / closer after a named-arg multiline sh body.
        if self._sh_trailer:
            if not stripped:
                return []
            if re.match(r"^[A-Za-z_]\w*\s*=", stripped) or re.match(r"^\)\s*,?\s*$", stripped):
                if re.match(r"^\)\s*,?\s*$", stripped):
                    self._sh_trailer = False
                return [f"{indent}# TaskStep(... {stripped})"]
            self._sh_trailer = False
            # Fall through and re-process this line.

        sp = self._SCRIPT_PREFIX

        # Named-arg multiline: self.script.sh(script: """  / sh(script: '''
        # Also ``def exitCode = script.sh(script: """`` after span-join.
        m = re.match(
            rf"^(?:(?:def|String|int|Integer|Object|var)\s+)?"
            rf"(?:\w+\s*=\s*)?{sp}sh\b\s*\(\s*script\s*[:=]\s*(\"\"\"|\'\'\')\s*$",
            stripped,
        )
        if m and not stripped.startswith(("//", "#")):
            self._sh_quote = m.group(1)
            return [f"{indent}# TaskStep(script={m.group(1)}"]

        # Also: script.sh(\n    script: """  (after named-arg rewrite: script=""")
        m = re.match(r"^script\s*[:=]\s*(\"\"\"|\'\'\')\s*$", stripped)
        if m and not stripped.startswith(("//", "#")):
            self._sh_quote = m.group(1)
            return [f"{indent}# TaskStep(script={m.group(1)}"]

        # ``exitCode = self.script.sh(`` / ``script.sh(`` — hold until script: """
        if (
            allow_hold
            and not stripped.startswith(("//", "#"))
            and re.search(r"(?:self\.)?(?:script\.)?sh\b\s*\(\s*$", stripped)
            and '"""' not in stripped
            and "'''" not in stripped
        ):
            self._sh_held_opener = line
            return []

        # Multiline opener: sh """ / self.script.sh("""
        m = re.match(
            rf"^(?:\w+\s*=\s*)?{sp}sh\b\s*\(?\s*(\"\"\"|\'\'\')\s*\)?\s*$",
            stripped,
        )
        if m and not stripped.startswith("//") and not stripped.startswith("#"):
            self._sh_quote = m.group(1)
            return [f"{indent}# TaskStep(script={m.group(1)}"]

        # self.script.sh(...) / script.sh(...) -> TaskStep (single-line)
        m = re.match(
            rf"^(?:\w+\s*=\s*)?{sp}sh\b\s*\(?\s*(.+?)\s*\)?\s*$",
            stripped,
        )
        if m and not stripped.startswith("//") and not stripped.startswith("#"):
            args = m.group(1)
            # Do not treat a bare opener quote as a complete single-line sh.
            if args in ('"""', "'''"):
                self._sh_quote = args
                return [f"{indent}# TaskStep(script={args}"]
            # Named script: """ opener glued on one line with trailing junk
            m_named = re.match(r"script\s*[:=]\s*(\"\"\"|\'\'\')\s*$", args)
            if m_named:
                self._sh_quote = m_named.group(1)
                return [f"{indent}# TaskStep(script={m_named.group(1)}"]
            return [f"{indent}# TaskStep(script={args})"]

        # (context.|self.)?script.echo(...) / echo(...) -> print(...)
        m = re.match(
            rf"^(?:(?:self|context)\.)?{sp}echo\s*\(?(.+?)\)?\s*$",
            stripped,
        )
        if m and not stripped.startswith("//") and not stripped.startswith("#"):
            return [f"{indent}print({m.group(1)})"]

        # script.params.FOO / context.script.params['X'] → params… (names only)
        if re.search(
            r"\b(?:(?:self|context)\.)?script\.params\b", stripped
        ) and not stripped.startswith(("//", "#")):
            rewritten = re.sub(
                r"\b(?:(?:self|context)\.)?script\.params\b",
                "params",
                stripped,
            )
            return [f"{indent}{rewritten}"]

        # Job DSL: return script.choice(...) / script.buildDiscarder(...) → TODO
        if self._JOB_DSL_SCRIPT_RE.match(stripped) and not stripped.startswith(("//", "#")):
            step_m = re.search(r"script\.(\w+)\s*\(", stripped)
            step = step_m.group(1) if step_m else "jobDsl"
            if stripped.rstrip().endswith("{") or ") {" in stripped:
                depth = stripped.count("{") - stripped.count("}")
                if depth > 0:
                    self._block_todo_depth = depth
            return [
                todo_comment(
                    f"script.{step} -> Job DSL / Tekton param (not converted)",
                    indent=indent,
                )
            ]

        # ``def createInstanceOutput = script.sh( script="""`` (named-arg megline)
        # Allow ``def`` / typed decl prefixes left by span-join before type strip.
        _sh_assign = (
            r"^(?:(?:def|String|int|Integer|Object|var)\s+)?"
            r"(?:\w+\s*=\s*)?(?:(?:self|context)\.)?(?:script\.)?sh\b\s*\(\s*"
            r"script\s*=\s*(\"\"\"|\'\'\')"
        )
        m = re.match(_sh_assign + r"\s*$", stripped)
        if m and not stripped.startswith(("//", "#")):
            self._sh_quote = m.group(1)
            return [f"{indent}# TaskStep(script={m.group(1)}"]
        m = re.match(_sh_assign, stripped)
        if m and not stripped.startswith(("//", "#")):
            self._sh_quote = m.group(1)
            # If the opener megline continues past the quote, absorb as TODO.
            if not re.search(r"(\"\"\"|\'\'\')\s*$", stripped):
                return [
                    todo_comment(
                        "script.sh named-arg megline -> TaskStep",
                        indent=indent,
                    )
                ]
            return [f"{indent}# TaskStep(script={m.group(1)}"]

        # self.script.error(...) / script.error(...) -> raise
        m = re.match(rf"^{sp}error\s*\((.+?)\)\s*$", stripped)
        if m:
            return [f"{indent}raise RuntimeError({m.group(1)})"]

        # self.script.stage(...) / script.stage(...) / builder.stage(...) {
        m = re.match(
            r"^(?:self\.script\.|script\.|(?:\w+\.)?)?stage\s*\((.+)\)\s*\{?\s*$",
            stripped,
        )
        if m and not stripped.startswith(("//", "#")):
            label = m.group(1).strip().strip("'\"")
            return [f"{indent}# --- Stage: {label} ---"]

        # Mid-line IO / credential helpers → whole-line TODO (not GROOVY).
        if self._SCRIPT_IO_CRED_RE.search(stripped) and not stripped.startswith(("//", "#")):
            step_m = re.search(
                r"\b(?:self\.)?script\.(\w+)\s*\(",
                stripped,
            )
            step = step_m.group(1) if step_m else "binding"
            # Absorb trailing closure bodies (sshagent { … }).
            if stripped.rstrip().endswith("{") or ") {" in stripped:
                depth = stripped.count("{") - stripped.count("}")
                if depth > 0:
                    self._block_todo_depth = depth
            return [
                todo_comment(
                    f"script.{step} -> Tekton credentials / workspace step",
                    indent=indent,
                )
            ]

        # Megline setup: ``(def )?script = context.script def steps = …`` (+ optional stage)
        if (
            re.match(r"^(?:def\s+)?script\s*=\s*context\.script\b", stripped)
            and (
                " def " in f" {stripped} "
                or re.search(r"\b(?:self\.)?script\.stage\s*\(", stripped)
                or " if " in f" {stripped} "
            )
            and not stripped.startswith(("//", "#"))
        ):
            depth = stripped.count("{") - stripped.count("}")
            if depth > 0:
                self._block_todo_depth = depth
            return [
                todo_comment(
                    "script=context.script megline -> Tekton stage sequence",
                    indent=indent,
                )
            ]

        # Map-held stage: ``stages[name] = { script.stage(…) {``
        if (
            re.search(r"\b(?:self\.)?script\.stage\s*\(", stripped)
            and not re.match(r"^(?:self\.)?script\.stage\s*\(", stripped)
            and not stripped.startswith(("//", "#"))
        ):
            depth = stripped.count("{") - stripped.count("}")
            if depth > 0:
                self._block_todo_depth = depth
            return [
                todo_comment(
                    "embedded script.stage -> Tekton stage boundary (not converted)",
                    indent=indent,
                )
            ]

        # self.script.junit / archiveArtifacts / writeYaml / checkout → TODO
        m = re.match(
            rf"^{sp}(\w+)\s*\(",
            stripped,
        )
        if (
            m
            and m.group(1) in self._SCRIPT_TODO_STEPS
            and not stripped.startswith(("//", "#"))
            and (stripped.startswith("self.script.") or stripped.startswith("script."))
        ):
            step = m.group(1)
            return [
                todo_comment(
                    f"script.{step} -> Tekton task result / workspace step",
                    indent=indent,
                )
            ]

        # script.dir(...) { -> workingDir TODO
        if re.search(r"(?:^|\.)dir\s*\(", stripped) and (
            stripped.rstrip().endswith("{") or ") {" in stripped
        ):
            return [
                todo_comment(
                    "script.dir -> Tekton workingDir / workspace subpath",
                    indent=indent,
                )
            ]

        # Job DSL / Declarative top-level blocks — absorb nested body as TODO.
        if re.match(
            r"^(?:pipeline|namespaces|cleanup|failure|success|aborted|unstable)\s*\{\s*$",
            stripped,
        ):
            self._block_todo_depth = 1
            return [
                todo_comment(
                    "Job DSL / Declarative block -> Tekton task/pipeline structure "
                    "(not converted)",
                    indent=indent,
                )
            ]

        # Declarative sections — absorb until brace depth returns to 0.
        # Also meglines: ``stages { stage('X') { …`` (content after opener).
        m_decl = re.match(
            r"^(?:script|steps|always|post|stages|agent|options|parameters|"
            r"environment|when|node)\s*\{",
            stripped,
        )
        if m_decl and not stripped.startswith(("//", "#")):
            depth = stripped.count("{") - stripped.count("}")
            if depth > 0:
                self._block_todo_depth = depth
            return [
                todo_comment(
                    "declarative/script block -> Tekton task/pipeline structure " "(not converted)",
                    indent=indent,
                )
            ]

        # GitSCM / checkout scm: [ $class: 'GitSCM', … ]
        if (
            re.search(r"\$class\s*:\s*'GitSCM'", stripped)
            or re.search(r"\bcheckout\s+scm\s*:", stripped)
            or (
                "userRemoteConfigs" in stripped
                and (stripped.rstrip().endswith("{") or stripped.rstrip().endswith("["))
            )
        ) and not stripped.startswith(("//", "#")):
            if stripped.rstrip().endswith(("{", "[")):
                self._block_todo_depth = 1
            return [
                todo_comment(
                    "GitSCM / checkout -> git-clone task (not converted)",
                    indent=indent,
                )
            ]

        # Job DSL multiline description = '''…'''
        m_desc = re.match(r"^description\s*=\s*('''|\"\"\")(.*)$", stripped)
        if m_desc and not stripped.startswith(("//", "#")):
            quote = m_desc.group(1)
            rest = m_desc.group(2)
            if quote not in rest:
                self._desc_quote = quote
            return [
                todo_comment(
                    "Job DSL description -> pipeline annotation (not converted)",
                    indent=indent,
                )
            ]

        # stagesClosure={ ... } passed to a pipeline builder
        if re.search(r"\bstagesClosure\s*=\s*\{", stripped):
            return [
                todo_comment(
                    "stagesClosure -> Tekton Pipeline task sequence",
                    indent=indent,
                )
            ]

        # Leftover ``}) {`` after withFailureNotification({…}) {
        if re.match(r"^\)\}\s*\{\s*$", stripped) or stripped == "}) {":
            return [
                todo_comment(
                    "map-arg closure closer -> absorbed into prior with* TODO",
                    indent=indent,
                )
            ]

        # withCredentials([...]) { -> # SecretRef
        if "withCredentials" in stripped:
            self._mark_wrapper_context(stripped)
            return [todo_comment("withCredentials -> SecretRef in task step env", indent=indent)]

        # withEnv([...]) { -> env dict
        if "withEnv" in stripped:
            self._mark_wrapper_context(stripped)
            return [todo_comment("withEnv -> env dict in TaskStep", indent=indent)]

        # catchError -> on_error='continue'
        if "catchError" in stripped:
            return [f"{indent}# on_error='continue'"]

        # Structural with*/sh* wrappers (any library) — early TODO + shell/map ctx.
        wrapper = self._match_wrapper_opener(stripped)
        if wrapper is not None:
            name = wrapper
            self._mark_wrapper_context(stripped)
            todo_msg = self._dsl_hints.get(
                name,
                f"{name} -> Tekton wrapper / credentials / env / task boundary",
            )
            return [todo_comment(todo_msg, indent=indent)]

        # Exact-name Jenkins / plugin steps (and rare non-with*/sh* helpers).
        for step, todo_msg in TODO_STEPS.items():
            if re.search(rf"\b{re.escape(step)}\b", stripped) and not stripped.startswith(
                ("//", "#")
            ):
                if step.startswith(("with", "sh")) or step.endswith("Wrapper"):
                    self._mark_wrapper_context(stripped)
                msg = self._dsl_hints.get(step, todo_msg)
                return [todo_comment(msg, indent=indent)]

        # Remaining script.* / self.script.* / context.script.* / return script.*
        if re.match(
            r"^(?:return\s+)?(?:(?:self|context)\.)?script\.", stripped
        ) and not stripped.startswith(("//", "#")):
            if stripped.rstrip().endswith("{") or ") {" in stripped:
                depth = stripped.count("{") - stripped.count("}")
                if depth > 0:
                    self._block_todo_depth = depth
            return [
                todo_comment(
                    "script.* Jenkins binding -> Tekton context/env",
                    indent=indent,
                )
            ]

        return [line]

    def _match_wrapper_opener(self, stripped: str) -> str | None:
        """Return with*/sh* wrapper name when ``stripped`` is a closure opener."""
        if stripped.startswith(("//", "#")):
            return None
        m = self._WRAPPER_OPENER_RE.match(stripped)
        if not m:
            return None
        name = m.group("name")
        if name in self._WRAPPER_SPECIAL:
            return None
        return name

    def _mark_wrapper_context(self, stripped: str) -> None:
        """Enable shell-body context; map-tail absorb only for open map-arg forms."""
        self._after_wrapper_todo = True
        # Multiline map-arg openers only (line ends mid-map). Complete
        # ``withX(args) {`` / ``withX(a, [k: v]) {`` must not set this.
        if (
            re.search(r"\(\s*\{\s*$", stripped)
            or re.search(r"\(\s*\[\s*$", stripped)
            or re.search(r"[\(\[]\s*$", stripped)
        ):
            self._with_map_todo = True

    def _get_indent(self, line: str) -> str:
        return line[: len(line) - len(line.lstrip())]

    def _nudge_shell_body_ctx(self, emitted: str) -> None:
        """Track whether the next shell cmd should be ``# |`` (TaskStep / with*)."""
        stripped = emitted.strip()
        if not stripped:
            return
        if self._after_wrapper_todo or self._with_map_todo:
            self._shell_body_ctx = True
            # Keep flag until a live Python line clears it below.
        if stripped.startswith(
            (
                "# TaskStep",
                "# | ",
                "# TEKTONIZE-TODO: with",
                "# TEKTONIZE-TODO: sh",
                "# TEKTONIZE-TODO: shell command body",
                "# TEKTONIZE-TODO: shell timing body",
                "# TEKTONIZE-TODO: stageWrapper",
            )
        ):
            self._shell_body_ctx = True
            return
        # Live Python or unrelated TODO ends the shell-body context.
        if not stripped.startswith("#") or (
            stripped.startswith("# TEKTONIZE-TODO:")
            and not any(
                x in stripped
                for x in (
                    "-> Tekton wrapper",
                    "with* map-arg",
                    "with*/sh* closure",
                    "shell command",
                    "shell timing",
                    "sshagent",
                    "SecretRef",
                    "env dict",
                    "stageWrapper",
                )
            )
            and not stripped.startswith(("# TEKTONIZE-TODO: with", "# TEKTONIZE-TODO: sh"))
        ):
            self._shell_body_ctx = False
            self._after_wrapper_todo = False

    def detect_jenkins_calls(self, body_text: str) -> list[JenkinsDSLCall]:
        """Detect all Jenkins DSL calls in a method body."""
        calls = []

        # script.xxx(...) pattern
        for m in re.finditer(r"(?:script\.)(\w+)\s*\(([^)]*)\)", body_text):
            calls.append(
                JenkinsDSLCall(
                    step_name=m.group(1),
                    arguments=m.group(2),
                    line=m.group(0),
                )
            )

        # Bare step calls
        for step in list(STEP_MAPPINGS) + list(TODO_STEPS):
            for m in re.finditer(rf"(?<!\.)\b{re.escape(step)}\s*\(", body_text):
                calls.append(JenkinsDSLCall(step_name=step, line=m.group(0)))

        return calls


__all__ = [
    "STEP_MAPPINGS",
    "TODO_STEPS",
    "JenkinsDSLCall",
    "JenkinsDSLTransformer",
    "TektonMapping",
]
