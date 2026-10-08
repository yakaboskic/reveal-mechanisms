"""Tests for the reference release (release_) stage: the cfg command, its keys and build_meta_yaml.py's meta keys.

The cfg checks encode LAP rules that a `--check` dry run does not flag: a bare `$dir_key` keeps its @instance
placeholder in command text, the JSON result is written only on success, the build fans in every trait's long file,
and every flag it passes exists in the backend CLI.
Run with:
  /humgen/diabetes2/users/chase/projects/pigean/.venv/bin/python -B -m unittest discover -s lap/scripts/tests
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

CFG = os.path.join(bm.LAP_DIR, "config", "cfde_projection.cfg")
META_YAML = os.path.join(bm.LAP_DIR, "config", "cfde_projection.meta.yaml")
META = os.path.join(bm.LAP_DIR, "config", "cfde_projection.meta")
LAP_COMMON_CFG = "/humgen/diabetes/users/chase/lap/trunk/config/common.cfg"
META_KEYS = {"reveal_repo_dir", "cfde_libraries", "pigean_gene_stats_file", "base_dir", "unix_out_dir", "log_dir", "raw_dir",
             "eaggl_share_dir", "cfde_snapshot", "cfde_dir", "kpn_dir", "lap_home", "web_out_dir", "default_umask"}
BACKEND_PYTHON = os.path.join(bm.REPO_DIR, "services", "backend", ".venv", "bin", "python")
COMMAND = "$reveal_python -B -m reveal_backend.reference_release build "


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


class ReleaseCfgTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.decl = cfg_declarations()
        cls.cmds = {key: value for key, value in cls.decl.items() if "cmd" in value[0]}
        cls.value = cls.cmds["release_build_cmd"][1]

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

    def test_release_build_is_the_only_release_command_and_runs_once_per_project(self):
        prefixes, _, postfix = self.cmds["release_build_cmd"]
        self.assertEqual((prefixes, postfix), (["cmd"], "class_level project rusage_mod $release_build_mem"))
        self.assertEqual(sorted(key for key in self.cmds if key.startswith("release_")), ["release_build_cmd"])
        self.assertEqual(sorted(key for key in self.cmds if key.startswith("db_")), [])  # the reload stage is gone
        self.assertNotRegex(read(CFG), r"\breload_target\b")

    def test_runner_is_the_backend_cli(self):
        self.assertEqual(self.decl["reveal_python"][1], "$reveal_repo_dir/services/backend/.venv/bin/python")
        self.assertTrue(self.value.startswith(COMMAND), self.value[:120])
        self.assertNotIn("publish", self.value)  # publishing is run by hand
        self.assertFalse(re.search(r"(PASSWORD|TOKEN|SECRET|API_KEY)\s*=", read(CFG)), "secrets belong in .env")

    def test_writes_its_json_result_only_on_success(self):
        result = "release_result_file"
        self.assertEqual(self.decl[result][1], "@project.release.json")
        self.assertTrue(self.value.endswith("| tee !{output::%s}.part && mv !{output::%s}.part !{output::%s}"
                                            % (result, result, result)), self.value[-160:])
        self.assertEqual(set(re.findall(r"!\{output:[^:}]*:([a-z0-9_]+)\}", self.value)), {result})

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

    def test_fans_in_every_trait_long_file(self):
        self.assertEqual(self.value.count("!{input:--long-file:trait_projection_long_file}"), 1)
        self.assertEqual(self.decl["trait_projection_long_file"][2].rsplit("class_level", 1)[1].strip(), "trait")

    def test_inputs_are_declared_files(self):
        inputs = re.findall(r"!\{input:(?:[^:}]*:)?([a-z0-9_]+)\}", self.value)
        self.assertGreaterEqual(len(inputs), 10)
        for key in inputs:
            with self.subTest(key=key):
                self.assertEqual(self.decl[key][0][-2:], ["path", "file"])

    def test_release_keys(self):
        self.assertEqual(self.decl["release_dir"][1], "$project_dir/release")
        self.assertEqual(self.decl["release_files_dir"][1], "$release_dir/files")
        self.assertIn("--out !{key::release_files_dir} ", self.value)
        self.assertEqual(self.decl["reference_vector_cache"][1], "$base_dir/raw/reference_vector_cache.sqlite")
        self.assertRegex(self.decl["release_build_workers"][1], r"^[1-9][0-9]*$")
        for key in ("reference_vector_cache", "top_n_gene_sets", "release_build_workers"):
            self.assertIn("$" + key + " ", self.value)

    def test_command_text_has_no_unsubstituted_instance_placeholders(self):
        self.assertNotIn("#", self.value)  # config.pm strips everything after an unescaped #
        self.assertNotRegex(self.value, r"(^|[^\\])\*[A-Za-z0-9_{]")  # *key expands to an unsubstituted path
        self.assertNotIn("@", re.sub(r"!\{[^{}]*\}", "", self.expand(self.value)))

    def test_every_flag_it_passes_exists_in_the_cli(self):
        if not os.path.isfile(BACKEND_PYTHON):
            self.skipTest("backend venv missing: %s" % BACKEND_PYTHON)
        # Only builds the argparse parser: nothing loads .env or connects anywhere.
        code = ("import argparse, json\n"
                "from reveal_backend.reference_release import parser\n"
                "sub = next(a for a in parser()._actions if isinstance(a, argparse._SubParsersAction))\n"
                "print(json.dumps(sorted(o for a in sub.choices['build']._actions for o in a.option_strings)))\n")
        env = dict(os.environ, PYTHONPATH=os.path.join(bm.REPO_DIR, "services", "backend", "src"), PYTHONDONTWRITEBYTECODE="1")
        out = subprocess.run([BACKEND_PYTHON, "-B", "-c", code], capture_output=True, text=True, env=env, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr[-2000:])
        options = set(json.loads(out.stdout))
        flags = set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", self.expand(self.value[len(COMMAND):])))
        self.assertGreaterEqual(len(flags), 8)
        self.assertEqual(sorted(flags - options), [])


class BetasCfgTest(unittest.TestCase):
    """The betas_ stage: `pigean betas` (no outer Gibbs) on each trait's existing PIGEAN gene stats."""
    COMMANDS = {"betas_index_cmd": (["short", "cmd"], "class_level project"),
                "betas_trait_gene_stats_cmd": (["short", "cmd"], "class_level trait"),
                "betas_trait_run_cmd": (["cmd"], "class_level trait rusage_mod $gene_set_stats_mem"),
                "betas_trait_annotate_cmd": (["short", "cmd"], "class_level trait")}
    PIGEAN_CLI = os.path.join(bm.LAP_DIR, "raw", "pigean_ca59661", "src", "pigean", "cli.py")

    @classmethod
    def setUpClass(cls):
        cls.decl = cfg_declarations()
        cls.cmds = {key: value for key, value in cls.decl.items() if "cmd" in value[0]}

    def test_the_stage_and_its_levels(self):
        self.assertEqual(sorted(key for key in self.cmds if key.startswith("betas_")), sorted(self.COMMANDS))
        for key, (prefixes, postfix) in self.COMMANDS.items():
            with self.subTest(cmd=key):
                self.assertEqual((self.cmds[key][0], self.cmds[key][2]), (prefixes, postfix))
        self.assertIn(self.decl["gene_set_stats_response"][1], ("log_bf", "combined"))
        self.assertEqual(self.decl["pigean_profile"][1], "$pigean_repo_dir/config/profiles/gwas.default.json")
        self.assertTrue(self.decl["pigean_cmd"][1].endswith("$python_cmd -B -m pigean"))
        self.assertIn("PYTHONPATH=$pigean_repo_dir/src ", self.decl["pigean_cmd"][1])

    def test_pigean_runs_betas_mode_on_the_trait_gene_stats_and_never_gibbs(self):
        value = self.cmds["betas_trait_run_cmd"][1]
        self.assertTrue(value.startswith("$helper_cmd record-pigean-commit --repo-dir $pigean_repo_dir --expected-commit $pigean_commit "))
        self.assertIn(" $pigean_cmd betas --config $pigean_profile ", value)
        self.assertNotIn("gibbs", value)
        for text in ("!{input:--X-in:annotations_gmt_file}", "!{input:--gene-stats-in:trait_gene_stats_file}",
                     "--gene-stats-log-bf-col $gene_set_stats_response", "--retain-all-beta-uncorrected", "--deterministic",
                     "!{output:--gene-set-stats-out:trait_gene_set_stats_raw_file}"):
            self.assertIn(text, value)
        self.assertIn("--response $gene_set_stats_response", self.cmds["betas_trait_annotate_cmd"][1])

    def test_every_pigean_flag_exists_in_the_pinned_cli(self):
        if not os.path.isfile(self.PIGEAN_CLI):
            self.skipTest("pinned pigean clone missing: %s" % self.PIGEAN_CLI)
        options = set(re.findall(r'add_option\("",\s*"(--[a-zA-Z0-9-]+)"', read(self.PIGEAN_CLI)))
        value = self.cmds["betas_trait_run_cmd"][1]
        flags = set(re.findall(r"(?<![\w-])--[a-zA-Z][a-zA-Z0-9-]*", value.split("$pigean_cmd betas", 1)[1]))
        self.assertGreaterEqual(len(flags), 10)
        self.assertEqual(sorted(flags - options), [])

    def test_every_helper_flag_exists(self):
        import projection_workflow
        sub = next(a for a in projection_workflow.build_parser()._actions if a.dest == "command")
        commands = {"betas_index_cmd": "gene-stats-index", "betas_trait_gene_stats_cmd": "trait-gene-stats",
                    "betas_trait_annotate_cmd": "annotate-gene-set-stats"}
        for key, command in commands.items():
            with self.subTest(cmd=key):
                value = self.cmds[key][1]
                self.assertTrue(value.startswith("$helper_cmd %s " % command), value[:60])
                options = {o for a in sub.choices[command]._actions for o in a.option_strings}
                self.assertEqual(sorted(set(re.findall(r"(?<![\w-])--[a-z][a-z0-9-]*", value)) - options), [])

    def test_the_release_build_takes_every_trait_gene_set_stats_file(self):
        self.assertIn("!{input:--gene-set-stats-file:trait_gene_set_stats_file}", self.cmds["release_build_cmd"][1])
        self.assertEqual(self.decl["trait_gene_set_stats_file"][1], "@trait.cfde_gene_set_stats.tsv.gz")


class GeneratedMetaTest(unittest.TestCase):
    """The committed meta files carry the keys the release stage reads and no reload targets."""

    def test_meta_carries_the_release_keys(self):
        meta = read(META)
        self.assertIn("!key reveal_repo_dir %s\n" % bm.REPO_DIR, meta)
        self.assertRegex(meta, r"\n!key cfde_libraries [A-Za-z0-9_,]+\n")
        self.assertNotIn("GaultonLab", re.search(r"\n!key cfde_libraries (\S+)\n", meta).group(1))
        self.assertEqual(set(re.findall(r"^!key (\S+)", meta, re.M)), META_KEYS)

    def test_no_reload_targets_remain(self):
        for path in (META, META_YAML):
            with self.subTest(path=os.path.basename(path)):
                self.assertNotRegex(read(path), r"reload_target|reload_from_generation|app_prefix|vector_env")


if __name__ == "__main__":
    unittest.main()
