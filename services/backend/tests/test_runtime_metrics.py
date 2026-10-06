"""Phase measurements report bounded metadata and preserve original failures."""
from unittest.mock import patch
import pytest

from reveal_backend import runtime_metrics


def test_measure_records_elapsed_phase_without_inputs():
    with patch.object(runtime_metrics.time, 'perf_counter', side_effect=[10, 10.25]), \
            patch.object(runtime_metrics, 'observe') as observe:
        with runtime_metrics.measure('research_query', 'source'):
            private_scientific_payload = {'private': 'never emitted'}
    observe.assert_called_once_with('research_query', 'source', 250, False)


def test_measure_records_failure_without_its_sensitive_message():
    error = RuntimeError('private path or backend credential must not enter metrics')
    with patch.object(runtime_metrics.time, 'perf_counter', side_effect=[10, 10.5]), \
            patch.object(runtime_metrics, 'observe') as observe:
        with pytest.raises(RuntimeError) as caught:
            with runtime_metrics.measure('research_artifact', 'store'):
                raise error
    assert caught.value is error
    observe.assert_called_once_with('research_artifact', 'store', 500, True)
