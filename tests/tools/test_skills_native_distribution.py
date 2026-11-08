"""Native distribution regressions on the served 41447 source.

Full-directory and pinned-fetch cases adapt upstream 45d9c33d and 7e95b67a.
Catalog cases adapt ef3d8ba3/a24d7382 while preserving 41447's project-only
precedence, disabled gates, physical-path deduplication and public row schema.
"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools.skills_hub import GitHubAuth, GitHubSource

REVISION = "a" * 40
SKILL_MD = "---\nname: vault-backup\ndescription: A backup fixture.\n---\nRun skills/system/vault-backup/backup.py.\n"


def github_fixture(monkeypatch, files=None, modes=None):
    source = GitHubSource(GitHubAuth())
    files = files or {"SKILL.md": SKILL_MD, "backup.py": b"backup", "restore.py": b"restore", "vault_backup_format.py": b"format"}
    modes = modes or {}
    source._tree_cache["owner/repo"] = ("main", [
        {"path": "skill/" + path, "type": "blob", "mode": modes.get(path, "100644")}
        for path in files
    ])
    source._tree_revisions["owner/repo"] = REVISION
    reads = []
    def content(repo, path, ref=None):
        reads.append((path, ref))
        value = files.get(path.removeprefix("skill/"))
        return value.decode() if isinstance(value, bytes) else value
    def raw(repo, path, ref=None):
        reads.append((path, ref))
        value = files.get(path.removeprefix("skill/"))
        return value.encode() if isinstance(value, str) else value
    monkeypatch.setattr(source, "_fetch_file_content", content)
    monkeypatch.setattr(source, "_fetch_file_bytes", raw)
    return source, reads


def test_complete_directory_includes_unlinked_transitive_python_module(monkeypatch):
    source, reads = github_fixture(monkeypatch)
    bundle = source.fetch("owner/repo/skill")
    assert bundle is not None
    assert set(bundle.files) == {"SKILL.md", "backup.py", "restore.py", "vault_backup_format.py"}
    assert bundle.files["vault_backup_format.py"] == b"format"
    assert bundle.metadata["source_revision"] == REVISION
    assert {ref for _, ref in reads} == {REVISION}


def test_every_byte_is_pinned_even_when_only_skill_md_is_present(monkeypatch):
    source, reads = github_fixture(monkeypatch, {"SKILL.md": SKILL_MD})
    assert source.fetch("owner/repo/skill") is not None
    assert reads == [("skill/SKILL.md", REVISION)]


def test_missing_declared_blob_refuses_partial_bundle(monkeypatch):
    source, _ = github_fixture(monkeypatch, {"SKILL.md": SKILL_MD, "backup.py": None})
    assert source.fetch("owner/repo/skill") is None


@pytest.mark.parametrize("tree, revision", [(None, REVISION), (("main", []), None)])
def test_unresolved_tree_refuses_before_downloading_bytes(monkeypatch, tree, revision):
    source, reads = github_fixture(monkeypatch)
    monkeypatch.setattr(source, "_get_repo_tree", lambda repo: tree)
    source._tree_revisions["owner/repo"] = revision
    assert source.fetch("owner/repo/skill") is None
    assert reads == []


def test_skill_md_must_be_regular_member_of_resolved_tree(monkeypatch):
    source, reads = github_fixture(monkeypatch, modes={"SKILL.md": "120000"})
    assert source.fetch("owner/repo/skill") is None
    assert reads == []


def test_pinned_transport_preserves_exact_ref(monkeypatch):
    source = GitHubSource(GitHubAuth())
    calls = []
    def get(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(status_code=200, content=b"content")
    monkeypatch.setattr(source, "_github_get", get)
    assert source._fetch_file_bytes("owner/repo", "skill/backup.py", ref=REVISION) == b"content"
    assert calls[0][1]["params"] == {"ref": REVISION}


def test_full_directory_keeps_existing_hidden_symlink_and_path_fences(monkeypatch):
    files = {"SKILL.md": SKILL_MD, "reference/audit.md": b"audit", "LICENSE": b"license",
             ".hidden": b"hidden", "scripts/x.pyc": b"cache", "reference/link.md": b"link"}
    source, _ = github_fixture(monkeypatch, files, {"reference/link.md": "120000"})
    bundle = source.fetch("owner/repo/skill")
    assert bundle is not None
    assert set(bundle.files) == {"SKILL.md", "reference/audit.md", "LICENSE"}


def test_referenced_symlink_remains_a_hard_refusal(monkeypatch):
    md = "---\nname: fixture\ndescription: fixture\n---\nRead references/link.md.\n"
    source, _ = github_fixture(monkeypatch, {"SKILL.md": md, "references/link.md": b"link"}, {"references/link.md": "120000"})
    assert source.fetch("owner/repo/skill") is None


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    from agent import prompt_builder as prompt
    from agent import skill_utils
    from tools import skills_tool as tool
    home = tmp_path / "home"
    local = home / "skills"
    local.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.chdir(tmp_path)
    roots = {"project": [], "external": []}
    monkeypatch.setattr(skill_utils, "get_project_skills_dirs", lambda: roots["project"])
    monkeypatch.setattr(skill_utils, "get_external_skills_dirs", lambda: roots["external"])
    monkeypatch.setattr(prompt, "get_all_skills_dirs", lambda: [local, *roots["external"]])
    tool._SKILLS_CACHE.clear()
    prompt._SKILLS_PROMPT_CACHE.clear()
    def put(root, rel, name="duplicate", extra="", body=None):
        path = root / rel / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\nname: {name}\ndescription: {rel} fixture\n{extra}---\n{body or rel}\n")
        return path
    return SimpleNamespace(home=home, local=local, external=external, project=project, roots=roots, put=put, tool=tool, prompt=prompt)


def assert_loadable_rows(c):
    rows = c.tool._find_all_skills()
    assert all(set(row) == {"name", "description", "category"} for row in rows)
    for row in rows:
        loaded = json.loads(c.tool.skill_view(row["name"]))
        assert loaded["success"], (row, loaded)
    return rows


def test_duplicate_catalog_names_are_actual_load_identifiers(catalog):
    c = catalog
    c.put(c.local, "alpha/one")
    c.put(c.local, "beta/two")
    assert {r["name"] for r in assert_loadable_rows(c)} == {"alpha/one", "beta/two"}
    assert not json.loads(c.tool.skill_view("duplicate"))["success"]


def test_real_three_copy_shape_uses_all_exact_paths(catalog):
    c = catalog
    paths = ["system/vault-backup", "claude-vault-tools/vault/skills/system/vault-backup",
             "claude-vault-tools/vault/09-Agents/hermes-skills/system/vault-backup"]
    for rel in paths:
        c.put(c.local, rel, name="vault-backup")
    assert {r["name"] for r in assert_loadable_rows(c)} == set(paths)


def test_directory_alias_is_an_owner_even_with_a_different_declared_name(catalog):
    c = catalog
    c.put(c.local, "tools/duplicate", name="alpha")
    c.put(c.local, "tools/beta")
    assert {r["name"] for r in assert_loadable_rows(c)} == {"alpha", "tools/beta"}


def test_duplicate_relative_paths_across_roots_are_not_advertised(catalog):
    c = catalog
    c.roots["external"] = [c.external]
    c.put(c.local, "same-path")
    c.put(c.external, "same-path")
    assert assert_loadable_rows(c) == []


def test_legacy_flat_file_participates_in_lookup_ambiguity(catalog):
    c = catalog
    c.put(c.local, "category/one")
    (c.local / "duplicate.md").write_text("legacy skill\n")
    assert {r["name"] for r in assert_loadable_rows(c)} == {"category/one"}


def test_identical_payloads_are_still_distinct_skills_under_existing_loader(catalog):
    c = catalog
    first = c.put(c.local, "alpha/one")
    second = c.put(c.local, "beta/two")
    second.write_bytes(first.read_bytes())
    assert {r["name"] for r in assert_loadable_rows(c)} == {"alpha/one", "beta/two"}


def test_trusted_project_precedence_is_preserved(catalog):
    c = catalog
    c.roots["project"] = [c.project]
    c.put(c.local, "local/one")
    c.put(c.project, "project/two")
    rows = assert_loadable_rows(c)
    assert [r["name"] for r in rows] == ["duplicate"]
    assert "project/two" in json.loads(c.tool.skill_view("duplicate"))["content"]


def test_disabled_declared_name_does_not_reappear_as_exact_alias(catalog):
    c = catalog
    c.put(c.local, "alpha/one")
    c.put(c.local, "beta/two")
    (c.home / "config.yaml").write_text("skills:\n  disabled: [duplicate]\n")
    assert assert_loadable_rows(c) == []
    assert not json.loads(c.tool.skill_view("alpha/one"))["success"]


def test_offer_hidden_candidate_still_blocks_ambiguous_bare_lookup(catalog):
    c = catalog
    c.put(c.local, "alpha/one")
    c.put(c.local, "beta/two", extra="platforms: [unavailable-platform]\n")
    assert {r["name"] for r in assert_loadable_rows(c)} == {"alpha/one"}


def test_prompt_and_disk_snapshot_render_only_loadable_names(catalog):
    c = catalog
    c.put(c.local, "alpha/one")
    c.put(c.local, "beta/two")
    for _ in range(2):
        text = c.prompt.build_skills_system_prompt()
        assert "alpha/one" in text and "beta/two" in text
        c.prompt._SKILLS_PROMPT_CACHE.clear()
    assert_loadable_rows(c)


def test_duplicate_physical_root_does_not_invent_ambiguity(catalog):
    c = catalog
    c.put(c.local, "category/one", name="unique")
    link = c.external / "linked"
    link.symlink_to(c.local, target_is_directory=True)
    c.roots["external"] = [link]
    assert {r["name"] for r in assert_loadable_rows(c)} == {"unique"}

def test_org_collision_keeps_provenance_and_diagnoses_unloadable_copy(catalog):
    from agent import skill_utils
    c = catalog
    c.put(c.local, "duplicate")
    c.put(c.local, "_org/org-1/duplicate")
    (c.local / "_org" / skill_utils.ORG_ACTIVE_MARKER).write_text("org-1")
    rows = assert_loadable_rows(c)
    assert [row["name"] for row in rows] == ["_org/org-1/duplicate"]
    text = c.prompt.build_skills_system_prompt()
    assert "_org/org-1/duplicate" in text and "[org-shared" in text
    assert "no unique load path" in text
    assert not json.loads(c.tool.skill_view("duplicate"))["success"]


def test_quarantined_project_cannot_make_lower_bare_name_loadable(catalog, monkeypatch):
    from agent import skill_utils
    c = catalog
    c.roots["project"] = [c.project]
    c.put(c.local, "local/one")
    c.put(c.project, "project/two")
    monkeypatch.setattr(skill_utils, "iter_project_skill_files", lambda root: iter(()))
    monkeypatch.setattr(skill_utils, "is_quarantined_project_skill", lambda path: True)
    assert assert_loadable_rows(c) == []
    assert not json.loads(c.tool.skill_view("duplicate"))["success"]
    assert "local/one" not in c.prompt.build_skills_system_prompt()


def test_native_cli_lists_exact_load_names(catalog):
    from io import StringIO
    from rich.console import Console
    from hermes_cli.skills_hub import do_list
    c = catalog
    c.put(c.local, "alpha/one")
    c.put(c.local, "beta/two")
    sink = StringIO()
    do_list(console=Console(file=sink, force_terminal=False, width=150))
    assert "alpha/one" in sink.getvalue()
    assert "beta/two" in sink.getvalue()

def tree_api_fixture(monkeypatch, branch_commit=None, returned_tree=None, moving=False):
    """Real GitHub shape: commits and Git tree objects have different IDs."""
    source = GitHubSource(GitHubAuth())
    commit_id, tree_id = "b" * 40, "c" * 40
    branch_commit = branch_commit if branch_commit is not None else {
        "sha": commit_id, "commit": {"tree": {"sha": tree_id}}
    }
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        if url.endswith("/owner/repo"):
            data = {"default_branch": "release/known"}
        elif "/branches/" in url:
            data = {"name": "release/known", "commit": branch_commit}
        else:
            is_pinned = url.endswith("/" + tree_id)
            data = {"sha": returned_tree or tree_id, "truncated": False, "tree": [
                {"type": "blob", "mode": "100644", "path": "skill/at-snapshot" if is_pinned or not moving else "skill/after-move"}
            ]}
        return SimpleNamespace(status_code=200, json=lambda: data, headers={})
    monkeypatch.setattr("tools.skills_hub.httpx.get", get)
    return source, calls, commit_id, tree_id


def test_tree_resolution_records_commit_for_contents_api(monkeypatch):
    source, calls, commit_id, tree_id = tree_api_fixture(monkeypatch)
    assert source._get_repo_tree("owner/repo") is not None
    assert source._tree_revisions["owner/repo"] == commit_id
    assert any(url.endswith("/branches/release%2Fknown") for url in calls)
    assert calls[-1].endswith("/git/trees/" + tree_id)


@pytest.mark.parametrize("branch_commit", [{}, {"sha": "b" * 40, "commit": {"tree": {}}}])
def test_unresolved_commit_tree_binding_is_not_cached(monkeypatch, branch_commit):
    source, _, _, _ = tree_api_fixture(monkeypatch, branch_commit=branch_commit)
    assert source._get_repo_tree("owner/repo") is None
    assert "owner/repo" not in source._tree_cache
    assert "owner/repo" not in source._tree_revisions


def test_mismatched_tree_response_is_not_accepted(monkeypatch):
    source, _, _, _ = tree_api_fixture(monkeypatch, returned_tree="d" * 40)
    assert source._get_repo_tree("owner/repo") is None
    assert "owner/repo" not in source._tree_cache


def test_branch_movement_cannot_change_the_admitted_tree(monkeypatch):
    source, _, commit_id, _ = tree_api_fixture(monkeypatch, moving=True)
    tree = source._get_repo_tree("owner/repo")
    assert tree[1][0]["path"] == "skill/at-snapshot"
    assert source._tree_revisions["owner/repo"] == commit_id
