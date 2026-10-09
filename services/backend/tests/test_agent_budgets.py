"""Per-run dollar caps: research authoring and statement writing are budgeted separately."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from reveal_backend.agent_execution import agent_budget_usd
from reveal_backend.job_failures import authoring_failure


class AgentBudgetTests(unittest.TestCase):
    def test_defaults_give_research_five_dollars_and_statements_one(self):
        with patch.dict('os.environ', {}, clear=False) as environment:
            environment.pop('REVEAL_AGENT_MAX_BUDGET_USD', None); environment.pop('REVEAL_PARAGRAPH_MAX_BUDGET_USD', None)
            self.assertEqual(agent_budget_usd('research'), 5.0)
            self.assertEqual(agent_budget_usd('paragraph'), 1.0)

    def test_each_kind_reads_only_its_own_setting(self):
        with patch.dict('os.environ', {'REVEAL_AGENT_MAX_BUDGET_USD': '7.5', 'REVEAL_PARAGRAPH_MAX_BUDGET_USD': '0.4'}):
            self.assertEqual(agent_budget_usd('research'), 7.5)
            self.assertEqual(agent_budget_usd('paragraph'), 0.4)

    def failure(self, kind):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'runtime.json'
            path.write_text(json.dumps({'completion': {'provider_result_subtype': 'error_max_budget_usd', 'cost_usd': 1.0412, 'max_budget_usd': 1}}))
            return authoring_failure(SimpleNamespace(runtime_manifest_path=path), SimpleNamespace(max_budget_usd=1, kind=kind))

    def test_budget_failures_name_the_run_that_stopped(self):
        statement = self.failure('paragraph')
        self.assertEqual(statement['code'], 'AUTHORING_BUDGET_EXCEEDED')
        self.assertIn('statement writer stopped at its $1.00 budget', statement['message'])
        self.assertIn('scientific account is unchanged', statement['message'])
        self.assertEqual(statement['budget'], {'scope': 'authoring', 'limit_usd': 1, 'spent_usd': 1.0412, 'next_call_max_usd': None})
        self.assertIn('research agent stopped at its $1.00 authoring budget', self.failure('research')['message'])


if __name__ == '__main__':
    unittest.main()
