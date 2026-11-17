"""Convert Groovy naming conventions to Python naming conventions."""

from __future__ import annotations

import re


def pascal_to_snake(name: str) -> str:
    """Convert PascalCase to snake_case for filenames and module names.

    >>> pascal_to_snake("DeploymentType")
    'deployment_type'
    >>> pascal_to_snake("InstallerSteps")
    'installer_steps'
    >>> pascal_to_snake("IJenkinsContext")
    'i_jenkins_context'
    >>> pascal_to_snake("UITestsRunner")
    'ui_tests_runner'
    """
    # Handle consecutive uppercase (acronyms): "UITests" -> "UI_Tests"
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    # Handle camelCase boundaries: "deployment" -> "deployment"
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def camel_to_snake(name: str) -> str:
    """Convert camelCase to snake_case for identifiers and methods.

    >>> camel_to_snake("buildAndExecute")
    'build_and_execute'
    >>> camel_to_snake("getVersionVarFile")
    'get_version_var_file'
    >>> camel_to_snake("shouldRunPreDeploymentChecks")
    'should_run_pre_deployment_checks'
    >>> camel_to_snake("fromMap")
    'from_map'
    """
    s = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s)
    return s.lower()


def is_upper_snake(name: str) -> bool:
    """Check if a name is already UPPER_SNAKE_CASE.

    >>> is_upper_snake("KUBERNETES")
    True
    >>> is_upper_snake("FRESH_INSTALL")
    True
    >>> is_upper_snake("camelCase")
    False
    """
    return bool(re.match(r"^[A-Z][A-Z0-9_]*$", name))


def groovy_filename_to_python(filename: str) -> str:
    """Convert a Groovy filename to Python filename.

    >>> groovy_filename_to_python("DeploymentType.groovy")
    'deployment_type.py'
    >>> groovy_filename_to_python("RpmFreshInstall.groovy")
    'rpm_fresh_install.py'
    """
    stem = filename.removesuffix(".groovy")
    return f"{pascal_to_snake(stem)}.py"


def groovy_package_to_python(package: str) -> str:
    """Convert a Groovy package path to Python package path.

    >>> groovy_package_to_python("mylib.pipelines.config")
    'mylib/pipelines/config'
    >>> groovy_package_to_python("mylib.steps.provisioner")
    'mylib/steps/provisioner'
    """
    return package.replace(".", "/")


def groovy_import_to_python_module(import_path: str) -> tuple[str, str]:
    """Convert a Groovy import path to (python_module, symbol_name).

    >>> groovy_import_to_python_module("mylib.pipelines.config.DeploymentType")
    ('mylib.pipelines.config.deployment_type', 'DeploymentType')
    >>> groovy_import_to_python_module("mylib.steps.BaseSteps")
    ('mylib.steps.base_steps', 'BaseSteps')
    """
    parts = import_path.split(".")
    if not parts:
        return ("", "")

    # Last part is the class name, rest is the package
    symbol = parts[-1]
    package_parts = parts[:-1]

    # Convert the last package part (which is the file/module) to snake_case
    if package_parts:
        module_parts = [*package_parts, pascal_to_snake(symbol)]
    else:
        module_parts = [pascal_to_snake(symbol)]

    return (".".join(module_parts), symbol)


def convert_identifier(name: str) -> str:
    """Convert a Groovy identifier to Python, preserving UPPER_SNAKE constants.

    >>> convert_identifier("deploymentType")
    'deployment_type'
    >>> convert_identifier("KUBERNETES")
    'KUBERNETES'
    >>> convert_identifier("FRESH_INSTALL")
    'FRESH_INSTALL'
    >>> convert_identifier("versionVarFile")
    'version_var_file'
    """
    if is_upper_snake(name):
        return name
    return camel_to_snake(name)
