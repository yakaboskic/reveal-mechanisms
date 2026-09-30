"""Tests for the reference-reload (db_) stage: build_meta_yaml.py's reload_target instances and the cfg commands.

The cfg checks encode LAP rules that a `--check` dry run does not flag: a bare `$dir_key` keeps its @instance
placeholder in command text, approval files must never be produced by a command, and every db_ command writes its
JSON result only on success. Run with:
  /humgen/diabetes2/users/chase/projects/reveal-mechanisms/services/backend/.venv/bin/python -B -m unittest discover -s lap/scripts/tests
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import build_meta_yaml as bm  # noqa: E402
from projection_workflow import WorkflowError  # noqa: E402

CFG = os.path.join(bm.LAP_DIR, "config", "cfde_projection.cfg")
META_YAML = os.path.join(bm.LAP_DIR, "config", "cfde_projection.meta.yaml")
META = os.path.join(bm.LAP_DIR, "config", "cfde_projection.meta")
LAP_COMMON_CFG = "/humgen/diabetes/users/chase/lap/trunk/config/common.cfg"
DB_CMDS = {  # cmd key -> (class level, JSON result file key)
    "db_build_cmd": ("project", "db_build_file"), "db_embed_cmd": ("project", "db_embed_file"),
    "db_load_cmd": ("project", "db_load_file"), "db_capture_cmd": ("project", "db_capture_file"),
    "db_snapshot_cmd": ("reload_target", "db_snapshot_file"), "db_plan_cmd": ("reload_target", "db_plan_file"),
    "db_apply_cmd": ("reload_target", "db_apply_file"), "db_verify_cmd": ("reload_target", "db_verify_file"),
    "db_purge_plan_cmd": ("project", "db_purge_plan_file"), "db_purge_cmd": ("project", "db_purge_file"),
}
META_KEYS = {"reveal_repo_dir", "cfde_embeddings_dir", "reload_from_generation", "base_dir", "unix_out_dir", "log_dir",
             "raw_dir", "eaggl_share_dir", "cfde_snapshot", "cfde_dir", "kpn_dir", "lap_home", "web_out_dir", "default_umask"}


def read_cfg(path=CFG):
    """LAP-style logical lines: `#` lines skipped, `\\`-continued lines joined (config.pm init)."""
    lines, prepend = [], ""
    with open(path) as fh:
        for raw in fh:
            line = raw.rstrip("\n")
            if line.startswith("#"):
                continue
            line = line.strip()
            if not line:
                continue
            if line.endswith("\\"):
                prepend += line[:-1]
                continue
            lines.append(prepend + line)
            prepend = ""
    return lines


def cfg_declarations(path=CFG):
    """{key: (prefix words, value, postfix text)} for every `[prefixes] key=value [; postfix]` line."""
    out = {}
    for line in read_cfg(path):
        if line.startswith("!"):
            continue
        match = re.match(r"^((?:[a-z_]+\s+)*?)([A-Za-z0-9_]+)=(.*)$", line)
        if not match:
            continue
        prefixes, key, rest = match.group(1).split(), match.group(2), match.group(3)
        value, postfix = rest, ""
        if "cmd" in prefixes:
            value, _, postfix = rest.rpartition(";")
        elif "path" in prefixes:  # `path [file] key=value dir ... disp "..." class_level ...`
            value, _, postfix = rest.partition(" ")
        out[key] = (prefixes, value.strip(), postfix.strip())
    return out


def read(path):
    with open(path) as fh:
        return fh.read()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)
    return path


class ReloadTargetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def targets(self, text):
        return bm.load_reload_targets(write(os.path.join(self.tmp.name, "targets.yaml"), text))

    def test_reads_targets_in_file_order(self):
        targets = self.targets("targets:\n"
                               "  zeta: {database: d, prefix: reveal_z, vector_environment: z, upstash_host: h, production: false}\n"
                               "  prod: {database: d, prefix: reveal, vector_environment: prod, upstash_host: h, production: true}\n")
        self.assertEqual(targets, [("zeta", "reveal_z", "z", False), ("prod", "reveal", "prod", True)])

    def test_rejects_invalid_entries(self):
        good = "database: d, prefix: p, vector_environment: e, upstash_host: h, production: false"
        for text in ("targets: {}\n", "other: 1\n",
                     "targets:\n  Bad: {%s}\n" % good,
                     "targets:\n  purge-retired: {%s}\n" % good,
                     "targets:\n  t: {database: d, prefix: p, upstash_host: h, production: false}\n",
                     "targets:\n  t: {database: d, prefix: 'p; rm', vector_environment: e, upstash_host: h, production: false}\n",
                     "targets:\n  t: {database: d, prefix: p, vector_environment: e, upstash_host: h, production: 'yes'}\n"):
            with self.subTest(text=text), self.assertRaises(WorkflowError):
                self.targets(text)

    def test_repository_allow_list_loads(self):
        targets = bm.load_reload_targets(bm.TARGETS_FILE)
        self.assertTrue(targets)
        self.assertEqual([name for name, _, _, production in targets if production], ["prod"])

    def test_instance_lines_are_yaml_with_string_props(self):
        import yaml
        lines = bm.reload_target_lines([("qa", "reveal_workflow_qa", "qa", False), ("prod", "reveal", "prod", True)], "proj")
        parsed = yaml.safe_load("classes:\n" + "\n".join(lines) + "\n")["classes"]
        self.assertEqual(parsed["prod"], {"class": "reload_target", "parent": "proj",
                                          "properties": {"app_prefix": "reveal", "vector_env": "prod", "production": "true"}})
        self.assertEqual(parsed["qa"]["properties"]["production"], "false")


class CfdeEmbeddingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def model(self, name, vectors="vectors.float16.npy"):
        write(os.path.join(self.root, "embeddings", name, "rows.tsv"), "row\n")
        if vectors:
            write(os.path.join(self.root, "embeddings", name, vectors), "")

    def test_single_model_is_found(self):
        self.model("org-Model")
        self.assertEqual(bm.find_cfde_embeddings(self.root), "org-Model")

    def test_several_models_need_a_choice(self):
        self.model("a-model")
        self.model("b-model", "vectors.float32.npy")
        with self.assertRaises(WorkflowError):
            bm.find_cfde_embeddings(self.root)
        self.assertEqual(bm.find_cfde_embeddings(self.root, "b-model"), "b-model")

    def test_missing_vectors_fail(self):
        self.model("a-model", vectors=None)
        with self.assertRaises(WorkflowError):
            bm.find_cfde_embeddings(self.root, "a-model")
        with self.assertRaises(WorkflowError):
            bm.find_cfde_embeddings(os.path.join(self.root, "nowhere"))


class ReloadCfgTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.decl = cfg_declarations()
        cls.cmds = {key: value for key, value in cls.decl.items() if "cmd" in value[0]}

    def expand(self, text, seen=()):
        """Resolve `$key` references the way config.pm expand_value does (escaped `\\$` stays literal)."""
        def one(match):
            key = match.group(2)
            if key in META_KEYS:
                return match.group(1) + "<" + key + ">"
            self.assertIn(key, self.decl, "undefined key $%s" % key)
            self.assertNotIn(key, seen, "recursive key $%s" % key)
            return match.group(1) + self.expand(self.decl[key][1], seen + (key,))
        return re.sub(r"(^|[^\\])\$([A-Za-z0-9_]+)", one, text)

    def test_every_db_command_is_local_and_at_its_level(self):
        for key, (level, _) in DB_CMDS.items():
            with self.subTest(cmd=key):
                prefixes, _, postfix = self.cmds[key]
                self.assertEqual(prefixes, ["local", "cmd"])
                self.assertEqual(postfix, "class_level %s" % level)
        self.assertEqual(sorted(k for k in self.cmds if k.startswith("db_")), sorted(DB_CMDS))

    def test_every_db_command_writes_its_json_result_only_on_success(self):
        for key, (_, result) in DB_CMDS.items():
            with self.subTest(cmd=key):
                value = self.cmds[key][1]
                self.assertTrue(self.decl[result][1].endswith(".json"))
                self.assertTrue(value.endswith("| tee !{output::%s}.part && mv !{output::%s}.part !{output::%s}"
                                               % (result, result, result)), value[-160:])
                outputs = set(re.findall(r"!\{output:[^:}]*:([a-z0-9_]+)\}", value))
                self.assertEqual(outputs - {result}, {"db_plan_cmd": {"reload_plan_file"},
                                                      "db_purge_plan_cmd": {"purge_plan_file"}}.get(key, set()))

    def test_approvals_are_manual_inputs_that_replanning_deletes(self):
        approvals = {"approval_file": ("db_apply_cmd", "db_plan_cmd", "@reload_target.approval.json"),
                     "purge_approval_file": ("db_purge_cmd", "db_purge_plan_cmd", "@project.purge.approval.json")}
        for file_key, (consumer, planner, name) in approvals.items():
            with self.subTest(file=file_key):
                self.assertEqual(self.decl[file_key][1], name)
                for key, (_, value, _) in self.cmds.items():
                    self.assertNotRegex(value, r"!\{output:[^}]*:%s\}" % file_key, "%s must never produce %s" % (key, file_key))
                self.assertIn("!{input:--approval:%s}" % file_key, self.cmds[consumer][1])
                self.assertTrue(self.cmds[planner][1].startswith("rm -f !{key::%s:dir}/!{key::%s} && " % (file_key, file_key)))

    def test_production_flag_only_for_production_targets(self):
        flag = "!{raw,,reload_target,--allow-production,if_prop=production:eq:true,allow_empty=1}"
        for key, (_, value, _) in self.cmds.items():
            with self.subTest(cmd=key):
                expected = 1 if key in ("db_apply_cmd", "db_purge_cmd") else 0
                self.assertEqual(value.count("--allow-production"), expected)
                self.assertEqual(value.count(flag), expected)

    def test_command_text_has_no_unsubstituted_instance_placeholders(self):
        for key in DB_CMDS:
            with self.subTest(cmd=key):
                value = self.cmds[key][1]
                self.assertNotIn("#", value)  # config.pm strips everything after an unescaped #
                self.assertNotRegex(value, r"(^|[^\\])\*[A-Za-z0-9_{]")  # *key expands to an unsubstituted path
                self.assertNotIn("@", re.sub(r"!\{[^{}]*\}", "", self.expand(value)))

    def test_runner_is_the_backend_cli(self):
        self.assertEqual(self.decl["reveal_python"][1], "$reveal_repo_dir/services/backend/.venv/bin/python")
        self.assertEqual(self.decl["reveal_cmd"][1], "$reveal_python -B -m reveal_backend.reference_reload")
        target = self.decl["reveal_target_cmd"][1]
        self.assertIn("REVEAL_APPLICATION_TABLE_PREFIX=!{prop::reload_target:app_prefix}", target)
        self.assertIn("REVEAL_VECTOR_ENVIRONMENT=!{prop::reload_target:vector_env}", target)
        for key, (level, _) in DB_CMDS.items():
            runner = "$reveal_target_cmd " if level == "reload_target" else "$reveal_project_cmd "
            self.assertIn(runner, self.cmds[key][1], key)
        self.assertFalse(re.search(r"(PASSWORD|TOKEN|SECRET|API_KEY)\s*=", read(CFG)), "secrets belong in .env")

    def test_json_getter_prints_one_field(self):
        getter = self.decl["reload_json_get"][1].replace("$reveal_python", sys.executable)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"command": "build", "generation_id": "ab" * 32, "bundle": "/x/y"}, fh)
        self.addCleanup(os.unlink, fh.name)
        out = subprocess.run("%s %s generation_id" % (getter, fh.name), shell=True, capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout), (0, "ab" * 32 + "\n"))
        missing = subprocess.run("%s %s nope" % (getter, fh.name), shell=True, capture_output=True, text=True)
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(missing.stdout, "")

    def test_capture_from_resolves_the_legacy_generation(self):
        value = self.cmds["db_capture_cmd"][1]
        expression = re.search(r"--from \\\$\((.*?)\) --prefixes", value).group(1)
        getter = self.decl["reload_json_get"][1].replace("$reveal_python", sys.executable)
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"generation_id": "1" * 64, "legacy_generation_id": "2" * 64}, fh)
        self.addCleanup(os.unlink, fh.name)
        for key_value, expected in (("legacy", "2" * 64), ("active", "active"), ("3" * 64, "3" * 64)):
            shell = (expression.replace("$reload_from_generation", key_value).replace("$reload_json_get", getter)
                     .replace("!{input::db_load_file}", fh.name))
            out = subprocess.run(["bash", "-c", shell], capture_output=True, text=True)
            self.assertEqual(out.stdout.strip(), expected)

    def test_tee_pattern_leaves_no_output_on_failure(self):
        if os.path.isfile(LAP_COMMON_CFG):  # LAP prefaces every command with this
            self.assertIn("preface_pipe_status=set -o pipefail", read(LAP_COMMON_CFG))
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "r.json")
            for code, exists in ((0, True), (2, False)):
                if os.path.exists(out):
                    os.unlink(out)
                shell = "set -o pipefail; (echo '{\"ok\": false}'; exit %d) | tee %s.part && mv %s.part %s" % (code, out, out, out)
                result = subprocess.run(["bash", "-c", shell], capture_output=True, text=True)
                self.assertEqual((result.returncode == 0, os.path.exists(out)), (exists, exists))
            self.assertTrue(os.path.exists(out + ".part"))


class GeneratedMetaTest(unittest.TestCase):
    """The committed meta files must be regenerated whenever the allow-list changes."""

    def instances(self):
        blocks, current = {}, None
        with open(META_YAML) as fh:
            for line in fh:
                match = re.match(r'^  "([^"]+)":$', line)
                if match:
                    current = blocks.setdefault(match.group(1), {})
                    continue
                match = re.match(r"^\s+(class|parent|app_prefix|vector_env|production): (.+)$", line)
                if match and current is not None:
                    value = match.group(2)
                    current[match.group(1)] = json.loads(value) if value.startswith('"') else (None if value == "null" else value)
        return {name: block for name, block in blocks.items() if block.get("class") == "reload_target"}

    def test_meta_yaml_matches_the_allow_list(self):
        expected = {name: {"class": "reload_target", "parent": "eaggl_capped__cfde_2026_09_28", "app_prefix": prefix,
                           "vector_env": environment, "production": "true" if production else "false"}
                    for name, prefix, environment, production in bm.load_reload_targets(bm.TARGETS_FILE)}
        self.assertEqual(self.instances(), expected)

    def test_meta_carries_the_reload_keys_and_instances(self):
        meta = read(META)
        self.assertIn("!key reveal_repo_dir %s\n" % bm.REPO_DIR, meta)
        self.assertRegex(meta, r"\n!key cfde_embeddings_dir \S+/embeddings/\S+\n")
        self.assertRegex(meta, r"\n!key reload_from_generation (active|legacy|[0-9a-f]{64})\n")
        for name, prefix, environment, production in bm.load_reload_targets(bm.TARGETS_FILE):
            for line in ("%s class reload_target" % name, "%s app_prefix %s" % (name, prefix),
                         "%s vector_env %s" % (name, environment), "%s production %s" % (name, str(production).lower())):
                self.assertRegex(meta, r"(^|\n)%s(\n|$)" % re.escape(line))


if __name__ == "__main__":
    unittest.main()
