from __future__ import annotations

import ast
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

import yaml
from dulwich.ignore import IgnoreFilter
from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name


ROOT = Path(__file__).resolve().parents[1]
TESTED_DULWICH = "1.2.14"


def dependency_manifests():
    project = tomllib.loads((ROOT / "github_app_mcp/pyproject.toml").read_text(encoding="utf-8"))
    requirements = [Requirement(line) for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
                    if line.strip() and not line.lstrip().startswith("#")]
    return project, {"requirements.txt": requirements,
                     "github_app_mcp/pyproject.toml": [Requirement(value) for value in project["project"]["dependencies"]]}


def excluded(ignore, relative):
    return any(ignore.is_ignored(parent.as_posix() + "/") for parent in relative.parents if parent != Path(".")) or bool(
        ignore.is_ignored(relative.as_posix())
    )


ISOLATED_RUNTIME = r'''
import importlib.abc
import importlib.machinery
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

source, application, scratch = (Path(value).resolve() for value in sys.argv[1:])
sys.path.insert(0, str(source))
sys.dont_write_bytecode = True
tempfile.tempdir = str(scratch)
os.environ.clear()
os.environ["PATH"] = ""
check = unittest.TestCase()
blocked_native = set()
check.assertFalse(any(name == "dulwich" or name.startswith("dulwich.") for name in sys.modules))


class NoDulwichExtensions(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith("dulwich."):
            spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
            if spec is not None and isinstance(spec.loader, importlib.machinery.ExtensionFileLoader):
                blocked_native.add(fullname)
                raise ImportError("Dulwich native accelerator disabled by packaging test")
        return None


def protect_application(event, arguments):
    paths = ()
    if event == "open":
        path, mode, flags = arguments
        if (mode and any(character in mode for character in "wax+")) or flags & (
            os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        ):
            paths = (path,)
    elif event in {"os.mkdir", "os.remove", "os.rmdir", "os.chmod", "os.utime", "os.truncate"}:
        paths = arguments[:1]
    elif event in {"os.rename", "os.link", "os.symlink"}:
        paths = arguments[:2]
    for path in paths:
        if isinstance(path, (str, bytes, os.PathLike)):
            resolved = Path(os.fsdecode(path)).resolve()
            if resolved.is_relative_to(application) or resolved.is_relative_to(Path.cwd()):
                raise AssertionError("Runtime attempted to mutate application or working directory")


sys.meta_path.insert(0, NoDulwichExtensions())
sys.addaudithook(protect_application)
with patch.object(subprocess, "Popen", side_effect=AssertionError("no subprocess")), \
        patch.object(os, "system", side_effect=AssertionError("no shell")):
    import dulwich
    from dulwich.client import LocalGitClient
    from dulwich.config import ConfigDict, StackedConfig
    from dulwich.objects import Blob, Commit, Tree
    from dulwich.pack import apply_delta
    from dulwich.repo import MemoryRepo
    from issuelens_github_mcp import auth, github, policy, server, wiki as wiki_module

    for module in (auth, github, policy, server, wiki_module):
        check.assertTrue(Path(module.__file__).resolve().is_relative_to(source), module.__name__)
    check.assertNotIn(str(application), sys.path)
    for root_module in application.glob("*.py"):
        check.assertNotIn(root_module.stem, sys.modules)
    check.assertNotIn("wiki", sys.modules)
    check.assertIsNotNone(server.create_server(SimpleNamespace(writes_enabled=False, wiki_writes_enabled=False)))
    check.assertEqual(b"".join(apply_delta(b"abc", b"\x03\x04\x04abcd")), b"abcd")

    remote = MemoryRepo()
    blob = Blob.from_string(b"# Home\nWelcome\n")
    tree = Tree()
    tree.add(b"Home.md", 0o100644, blob.id)
    seed = Commit()
    seed.tree, seed.parents = tree.id, []
    seed.author = seed.committer = b"Fixture <fixture@example.test>"
    seed.author_time = seed.commit_time = 1
    seed.author_timezone = seed.commit_timezone = 0
    seed.message = b"Seed\n"
    for obj in (blob, tree, seed):
        remote.object_store.add_object(obj)
    branch = b"refs/heads/docs/wiki"
    remote.refs[branch] = seed.id
    remote.refs.set_symbolic_ref(b"HEAD", branch)

    class LocalWiki(wiki_module.WikiRepository):
        def _create_client(self):
            return LocalGitClient(config=ConfigDict()), "memory-only"

    with patch.object(LocalGitClient, "_open_repo", side_effect=lambda path: nullcontext(remote)), \
            patch.object(StackedConfig, "default", side_effect=AssertionError("no ambient Git config")):
        with LocalWiki("example/repository") as repository:
            temporary = repository._parent
            check.assertTrue(temporary.is_relative_to(scratch))
            check.assertIsInstance(repository._repo, MemoryRepo)
            check.assertEqual(repository.snapshot()["sha"], seed.id.decode("ascii"))
            check.assertEqual(repository.page("Home.md")["content"], "# Home\nWelcome\n")
            pages = {"Home.md": "# Home\nUpdated\n", "Notes/Build.md": "Package-only runtime\n"}
            identity = {"author_name": "Fixture", "author_email": "fixture@example.test"}
            with patch.object(remote.refs, "set_if_equals", wraps=remote.refs.set_if_equals) as cas:
                result = repository.write(pages, seed.id.decode("ascii"), "Update notes", **identity)
            check.assertEqual(result["status"], "updated")
            updated = result["sha"].encode("ascii")
            cas.assert_called_once_with(branch, seed.id, updated)
            check.assertEqual(remote.refs[branch], updated)
            check.assertEqual(remote.object_store[updated].parents, [seed.id])
            check.assertEqual(repository.page("Notes/Build.md")["content"], pages["Notes/Build.md"])
            check.assertEqual(repository.write(pages, result["sha"], "Same notes", **identity)["status"], "no-change")

            concurrent = Commit.from_string(remote.object_store[updated].as_raw_string())
            concurrent.parents = [updated]
            concurrent.commit_time += 1
            concurrent.message = b"Concurrent update\n"
            remote.object_store.add_object(concurrent)
            generate = repository._repo.object_store.generate_pack_data

            def race(*args, **kwargs):
                remote.refs[branch] = concurrent.id
                return generate(*args, **kwargs)

            with patch.object(repository._repo.object_store, "generate_pack_data", side_effect=race):
                with check.assertRaisesRegex(wiki_module.WikiError, "conflict or outcome unknown"):
                    repository.write({"Lost.md": "Must not win\n"}, result["sha"], "Race", **identity)
            check.assertEqual(remote.refs[branch], concurrent.id)
            check.assertEqual(repository.snapshot()["sha"], result["sha"])
            check.assertEqual(list(temporary.iterdir()), [])
        check.assertFalse(temporary.exists())
        with LocalWiki("example/repository") as repository:
            check.assertEqual(repository.snapshot()["sha"], concurrent.id.decode("ascii"))
            check.assertEqual(repository.page("Notes/Build.md")["content"], pages["Notes/Build.md"])
    remote.close()
    for name, module in tuple(sys.modules.items()):
        if name.startswith("dulwich."):
            check.assertNotIsInstance(getattr(module, "__loader__", None), importlib.machinery.ExtensionFileLoader, name)
    native_files = [path for directory in dulwich.__path__ for path in Path(directory).iterdir()
                    if any(path.name.endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES)]
    if native_files:
        check.assertTrue(blocked_native, "Installed native accelerators were not exercised")
check.assertEqual(list(scratch.iterdir()), [])
check.assertEqual(list(Path.cwd().iterdir()), [])
print(json.dumps({"package_only": True, "read_write_cas": True, "blocked_native": sorted(blocked_native)}))
'''


class WikiPackagingTests(unittest.TestCase):
    def test_dependency_manifests_pin_the_installed_tested_dulwich(self):
        project, manifests = dependency_manifests()
        for filename, requirements in manifests.items():
            with self.subTest(manifest=filename):
                matches = [requirement for requirement in requirements if canonicalize_name(requirement.name) == "dulwich"]
                self.assertEqual(len(matches), 1)
                requirement = matches[0]
                self.assertEqual({(specifier.operator, specifier.version) for specifier in requirement.specifier},
                                 {("==", TESTED_DULWICH)})
                self.assertIsNone(requirement.url)
                self.assertIsNone(requirement.marker)
                self.assertFalse(requirement.extras)
        self.assertEqual(importlib.metadata.version("dulwich"), TESTED_DULWICH)
        self.assertIn("src/issuelens_github_mcp", project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"])

    def test_remote_build_python_is_supported_by_declared_metadata(self):
        manifest = yaml.safe_load((ROOT / "azure.yaml").read_text(encoding="utf-8"))
        service = manifest["services"]["IssueLens"]
        configuration = service["codeConfiguration"]
        self.assertEqual(service["project"], ".")
        self.assertEqual(configuration["dependencyResolution"], "remote_build")
        self.assertEqual(configuration["runtime"], "python_3_13")
        self.assertTrue((ROOT / configuration["entryPoint"]).is_file())
        runtime = ".".join(configuration["runtime"].split("_")[1:])
        project, manifests = dependency_manifests()
        self.assertIn(runtime, SpecifierSet(project["project"]["requires-python"]))
        requires_python = importlib.metadata.metadata("dulwich")["Requires-Python"]
        self.assertTrue(requires_python)
        self.assertIn(runtime, SpecifierSet(requires_python))
        environment = {**default_environment(), "python_version": runtime, "python_full_version": runtime + ".0"}
        for filename, requirements in manifests.items():
            with self.subTest(manifest=filename):
                self.assertTrue(any(canonicalize_name(requirement.name) == "dulwich"
                                    and (requirement.marker is None or requirement.marker.evaluate(environment))
                                    for requirement in requirements))

    def test_wiki_runtime_has_no_process_or_gitpython_dependency(self):
        parsed = ast.parse((ROOT / "github_app_mcp/src/issuelens_github_mcp/wiki.py").read_text(encoding="utf-8"))
        aliases = {}
        for node in ast.walk(parsed):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for name in node.names:
                    imported = f"{node.module}.{name.name}" if isinstance(node, ast.ImportFrom) else name.name
                    self.assertNotIn(imported.split(".")[0], {"subprocess", "git", "gitdb"}, imported)
                    local = name.asname or (name.name if isinstance(node, ast.ImportFrom) else name.name.split(".")[0])
                    aliases[local] = imported if name.asname or isinstance(node, ast.ImportFrom) else local

        def qualified(node):
            if isinstance(node, ast.Name):
                return aliases.get(node.id, node.id)
            if isinstance(node, ast.Attribute):
                return qualified(node.value) + "." + node.attr
            return ""

        for node in ast.walk(parsed):
            if isinstance(node, ast.Call):
                name = qualified(node.func)
                self.assertNotIn(name, {"os.system", "os.popen", "Popen"})
                self.assertFalse(name.startswith(("subprocess.", "os.spawn", "os.exec")), name)
                for argument in node.args:
                    command = argument.elts[0] if isinstance(argument, (ast.List, ast.Tuple)) and argument.elts else argument
                    if isinstance(command, ast.Constant) and isinstance(command.value, str) and command.value.split():
                        executable = command.value.split()[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
                        self.assertNotIn(executable, {"git", "git.exe"}, f"Git command at line {node.lineno}")
        unused_project, manifests = dependency_manifests()
        for requirements in manifests.values():
            self.assertFalse({canonicalize_name(requirement.name) for requirement in requirements} & {"gitpython", "gitdb"})

    def test_clean_source_zip_runs_package_only_without_git_or_native_accelerators(self):
        files = set(ROOT.glob("*.py"))
        files.update(ROOT / name for name in ("agents.md", "requirements.txt", "azure.yaml", "github_app_mcp/pyproject.toml"))
        for directory, pattern in (("agents", "*.md"), ("skills", "*.md"), ("schemas", "*.json"),
                                   ("github_app_mcp/src", "*.py")):
            files.update((ROOT / directory).rglob(pattern))
        required = {"main.py", "github_app_mcp/src/issuelens_github_mcp/wiki.py",
                    "github_app_mcp/src/issuelens_github_mcp/policy.py",
                    "github_app_mcp/src/issuelens_github_mcp/server.py"}
        self.assertTrue(required <= {path.relative_to(ROOT).as_posix() for path in files})
        filters = {name: IgnoreFilter.from_path(ROOT / name) for name in (".agentignore", ".azdignore")}
        self.assertFalse(excluded(filters[".agentignore"], Path("agent.yaml")))
        for path in sorted(files):
            self.assertTrue(path.is_file(), str(path))
            for filename, ignore in filters.items():
                with self.subTest(ignore=filename, runtime_file=path.relative_to(ROOT).as_posix()):
                    self.assertFalse(excluded(ignore, path.relative_to(ROOT)))
        with tempfile.TemporaryDirectory(prefix="wiki-packaging-") as directory:
            temporary = Path(directory)
            application, working, scratch = (temporary / name for name in ("application", "remote-working", "scratch"))
            for path in (application, working, scratch):
                path.mkdir()
            archive = temporary / "application.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
                for path in sorted(files):
                    relative = path.relative_to(ROOT)
                    if not any(excluded(ignore, relative) for ignore in filters.values()):
                        bundle.write(path, relative.as_posix())
            with zipfile.ZipFile(archive) as bundle:
                self.assertTrue(required <= set(bundle.namelist()))
                self.assertFalse(any(".git" in Path(name).parts or "tests" in Path(name).parts for name in bundle.namelist()))
                bundle.extractall(application)
            before = {path.relative_to(application): path.read_bytes() for path in application.rglob("*") if path.is_file()}
            environment = {name: value for name, value in os.environ.items() if name.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
            environment["PATH"] = ""
            result = subprocess.run(
                [sys.executable, "-I", "-B", "-c", ISOLATED_RUNTIME,
                 str(application / "github_app_mcp/src"), str(application), str(scratch)],
                cwd=working, env=environment, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            report = json.loads(result.stdout)
            self.assertTrue(report["package_only"])
            self.assertTrue(report["read_write_cas"])
            self.assertEqual(before, {path.relative_to(application): path.read_bytes()
                                      for path in application.rglob("*") if path.is_file()})
            self.assertEqual(list(working.iterdir()), [])
            self.assertEqual(list(scratch.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
