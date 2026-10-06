import re

from .context import ScanContext

SCRIPT_ARGUMENT = r"""(?:-{1,2}[\w-]+\s+)*([^\s;&|"'<>]+)"""
WORKSPACE_PREFIXES = ("${workspaceFolder}/", "${workspaceFolder}\\", "${workspaceRoot}/", "./", ".\\")


def find_interpreter_running_non_code_file(ctx: ScanContext, command_lines) -> str | None:
    code_extensions = set(ctx.config.section("files")["code_extensions"])
    for command in command_lines:
        for script in scripts_passed_to_interpreters(ctx, command):
            extension = non_code_extension_of_repo_file(ctx, script, code_extensions)
            if extension:
                return f"{script} (.{extension})"
    return None


def scripts_passed_to_interpreters(ctx: ScanContext, command: str) -> list[str]:
    interpreter_then_script = re.compile(ctx.regex("commands.interpreter").pattern + SCRIPT_ARGUMENT)
    return [match.group(match.lastindex) for match in interpreter_then_script.finditer(command)]


def non_code_extension_of_repo_file(ctx: ScanContext, token: str, code_extensions: set[str]) -> str | None:
    rel = strip_workspace_prefix(token)
    if "." not in rel.rsplit("/", 1)[-1]:
        return None
    extension = rel.rsplit(".", 1)[-1].lower()
    known_data = extension in ctx.config.section("magic")["extensions"] or extension in ctx.config.section("files")["data_extensions"]
    in_repo = ctx.find_file(rel) is not None
    if extension not in code_extensions and (in_repo or known_data):
        return extension
    return None


def strip_workspace_prefix(token: str) -> str:
    for prefix in WORKSPACE_PREFIXES:
        token = token.removeprefix(prefix)
    return token.replace("\\", "/")
