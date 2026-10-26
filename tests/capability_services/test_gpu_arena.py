"""Exercise the deployed GPU Arena's real expressions and accounting semantics."""
import copy
import json
from pathlib import Path
import re

import httpx
import pytest

from capability_services.grafana_http.server import GatewayError
from tests.capability_services.test_grafana_gateway import gateway, upstream

FIXTURE = Path(__file__).parents[1] / 'fixtures/gpu-arena-dashboard.json'
WINDOW = {'start': 1792500000, 'end': 1792586400, 'step': 120}


def service(tmp_path, results=None, dashboard=None, warnings=None):
    calls = []
    dashboard = copy.deepcopy(dashboard or json.loads(FIXTURE.read_text()))
    results = iter(results or [])

    def provider(request):
        calls.append(request)
        if request.url.path == '/api/dashboards/uid/gpu-arena':
            return httpx.Response(200, json={'dashboard': dashboard})
        if request.url.path == '/api/datasources':
            return httpx.Response(200, json=[{'uid': 'prometheus', 'name': 'Metrics', 'type': 'prometheus'}])
        assert request.url.path in ['/api/datasources/proxy/uid/prometheus/api/v1/query',
                                    '/api/datasources/proxy/uid/prometheus/api/v1/query_range']
        return httpx.Response(200, json=upstream(next(results), warnings=warnings,
                                               result_type='matrix' if request.url.path.endswith('query_range') else 'vector'))

    return gateway(tmp_path, provider, config={'api_origin': 'https://private.example',
                                             'browser_origin': 'https://public.example:7443'}), calls


def sample(uuid, value, namespace=None):
    labels = {'UUID': uuid}
    if namespace is not None:
        labels['exported_namespace'] = namespace
    return {'metric': labels, 'value': [WINDOW['end'], str(value)]}


def test_catalog_exposes_actual_panels_units_expressions_and_accounting_limitations(tmp_path):
    api, calls = service(tmp_path)
    result = api.handle('/v1/gpu-arena/describe', {})
    assert result['dashboard_version'] == 2 and result['max_range_days'] == 90
    assert result['dashboard_url'].startswith('https://public.example:7443/d/gpu-arena/gpu-arena?')
    assert result['default_range'] == {'from': 'now-7d', 'to': 'now'}
    panels = {p['id']: p for p in result['panels']}
    assert panels[11]['field_config']['defaults']['unit'] == 'percent'
    assert panels[12]['targets'][0]['queryable']
    assert not panels[13]['targets'][2]['queryable']
    assert 'SQL' not in panels[13]['targets'][0]['expression']
    assert len(calls) == 1
    api.close()


@pytest.mark.parametrize('panel,ref,mode', [(6, 'E', 'instant'), (7, 'A', 'range'),
                                         (8, 'A', 'range'), (9, 'A', 'range'), (10, 'A', 'range'),
                                         (11, 'A', 'range'), (12, 'A', 'range')])
def test_all_gpu_metrics_and_history_render_live_dashboard_expressions(tmp_path, panel, ref, mode):
    api, calls = service(tmp_path, results=[[]])
    result = api.handle('/v1/gpu-arena/query', {'panel_id': panel, 'ref_id': ref, **WINDOW})
    assert result['status'] == 'no_data' and result['data']['mode'] == mode
    expression = calls[-1].url.params['query']
    assert '${' not in expression and '$__' not in expression
    if panel == 12:
        assert '"$1"' in expression and 'Unknown' in expression and 'Shared namespaces' in expression
    if panel == 11:
        assert 'DCGM_FI_PROF_SM_ACTIVE' in expression and '* 100' in expression
    assert ('query_range' in calls[-1].url.path) == (mode == 'range')
    api.close()


def test_literal_filters_are_escaped_once_and_cannot_inject_promql(tmp_path):
    api, calls = service(tmp_path, results=[[]])
    node = 'node"},evil{a=".*\\${node:regex}'
    api.handle('/v1/gpu-arena/query', {'panel_id': 1, 'ref_id': 'A', 'node': [node, 'node-2'], **WINDOW})
    expression = calls[-1].url.params['query']
    encoded = re.search(r'hostname=~("(?:[^"\\]|\\.)*")', expression).group(1)
    pattern = json.loads(encoded)
    assert re.fullmatch(pattern, node) and re.fullmatch(pattern, 'node-2')
    assert not re.fullmatch(pattern, 'unselected')
    api.close()


@pytest.mark.parametrize('change', [{'node': []}, {'node': ['$__all', 'node-a']}, {'node': ['bad\nnode']},
                                    {'end': WINDOW['start']}, {'start': WINDOW['end'] - 90 * 86400 - 1},
                                    {'step': 0}, {'step': True}, {'step': 1}, {'mode': 'sql'}])
def test_invalid_windows_and_filters_fail_before_upstream_query(tmp_path, change):
    api, calls = service(tmp_path)
    with pytest.raises(GatewayError):
        api.handle('/v1/gpu-arena/query', {'panel_id': 1, 'ref_id': 'A', **WINDOW, **change})
    assert calls == []
    api.close()


def test_90_day_window_is_supported_with_bounded_sample_count(tmp_path):
    api, calls = service(tmp_path, results=[[]])
    result = api.handle('/v1/gpu-arena/query', {'panel_id': 12, 'ref_id': 'A',
                                               'start': WINDOW['end'] - 90 * 86400, 'end': WINDOW['end']})
    assert result['time_range']['step'] == 7776
    assert '[7776000s]' in calls[-1].url.params['query']
    api.close()


def test_arena_query_preserves_partial_warnings_and_native_series(tmp_path):
    points = [sample('GPU-a', '0')]
    api, _ = service(tmp_path, results=[points], warnings=['partial upstream data'])
    result = api.handle('/v1/gpu-arena/query', {'panel_id': 1, 'ref_id': 'A', **WINDOW})
    assert result['status'] == 'ok' and not result['complete']
    assert result['data']['result'] == points
    assert result['data']['warnings'] == ['partial upstream data']
    api.close()


@pytest.mark.parametrize('panel,ref,error', [(13, 'B', 'gpu_arena_prometheus_target_required'),
                                           (100, 'A', 'gpu_arena_panel_not_found'),
                                           (1, 'missing', 'gpu_arena_target_not_found')])
def test_unknown_or_sql_targets_are_explicit_errors(tmp_path, panel, ref, error):
    api, calls = service(tmp_path)
    with pytest.raises(GatewayError, match=error):
        api.handle('/v1/gpu-arena/query', {'panel_id': panel, 'ref_id': ref, **WINDOW})
    assert len(calls) == 1
    api.close()


def test_gpu_hours_follow_dashboard_sql_and_keep_missing_capacity_unranked(tmp_path):
    allocated = [sample('GPU-a', 2, 'student-a'), sample('GPU-b', 1, 'student-b'),
                 sample('GPU-a', 1, 'student-c'), sample('GPU-missing', 3, 'student-c')]
    capacity = [sample('GPU-a', 40), sample('GPU-b', 80)]
    api, calls = service(tmp_path, results=[allocated, capacity])
    result = api.handle('/v1/gpu-arena/gpu-hours', WINDOW)
    assert result['group_by'] == 'namespace' and result['accounting_step_seconds'] == 30
    assert result['rows'] == [
        {'namespace': 'student-a', 'gpu_hours': 2.0, 'a100_80gb_hours': 1.0, 'unrated_gpu_hours': 0.0, 'rank': 1},
        {'namespace': 'student-b', 'gpu_hours': 1.0, 'a100_80gb_hours': 1.0, 'unrated_gpu_hours': 0.0, 'rank': 2},
        {'namespace': 'student-c', 'gpu_hours': 4.0, 'a100_80gb_hours': None, 'unrated_gpu_hours': 3.0, 'rank': None}]
    assert not result['complete'] and result['warnings'] == ['missing_gpu_capacity:student-c']
    queries = [r.url.params['query'] for r in calls if r.url.path.endswith('/query')]
    assert '[86400s:30s]' in queries[0] and '* 30 / 3600' in queries[0]
    assert 'DCGM_FI_DEV_FB_RESERVED' in queries[1]
    assert result['evidence'][0]['result'] == allocated
    api.close()


@pytest.mark.parametrize('points', [[sample('GPU-a', 'NaN', 'ns')],
                                   [sample('GPU-a', -1, 'ns')],
                                   [sample('GPU-a', 1, 'ns'), sample('GPU-a', 1, 'ns')]])
def test_invalid_accounting_samples_never_become_rankings(tmp_path, points):
    api, _ = service(tmp_path, results=[points, [sample('GPU-a', 80)]])
    with pytest.raises(GatewayError, match='gpu_arena_accounting_sample_invalid'):
        api.handle('/v1/gpu-arena/gpu-hours', WINDOW)
    api.close()


def test_empty_accounting_data_is_no_data_and_query_warnings_survive(tmp_path):
    api, _ = service(tmp_path, results=[[], []], warnings=['partial data'])
    result = api.handle('/v1/gpu-arena/gpu-hours', WINDOW)
    assert result['status'] == 'no_data' and result['rows'] == [] and not result['complete']
    api.close()


def test_changed_accounting_formula_and_unknown_variables_fail_explicitly(tmp_path):
    dashboard = json.loads(FIXTURE.read_text())
    ranking = next(p for p in dashboard['panels'] if p['id'] == 13)
    ranking['targets'][2]['expression'] = ranking['targets'][2]['expression'].replace('80.0', '40.0')
    api, calls = service(tmp_path, dashboard=dashboard)
    with pytest.raises(GatewayError, match='gpu_arena_accounting_unsupported'):
        api.handle('/v1/gpu-arena/gpu-hours', WINDOW)
    assert len(calls) == 1
    api.close()
    dashboard['panels'][0]['targets'][0]['expr'] += ' + $unsupported'
    api, calls = service(tmp_path, dashboard=dashboard)
    with pytest.raises(GatewayError, match='gpu_arena_variable_unsupported'):
        api.handle('/v1/gpu-arena/query', {'panel_id': 1, 'ref_id': 'A', **WINDOW})
    assert len(calls) == 1
    api.close()
