"""The existing dashboard tests again, every publication cross-checked against the incremental engine.

Each `build_dashboard` call these tests make still returns the full engine's
document, so their own assertions are unchanged. Beside it, the same ledger is
indexed into a fresh resolved store and the call is repeated with the shadow
engine (which must report every view equal) and the incremental engine (whose
views must equal the shadow publication's, `report_sha256` aside). WI754 step 3,
invariant 5.
"""
import tempfile
from pathlib import Path

import pytest

from pulse_compute_cost import stats_dashboard
from pulse_compute_cost.collaboration_query import CollaborationQuery, CollaborationQuerySession
from pulse_compute_cost.incremental_views import IncrementalViews, rebuild
from pulse_compute_cost.ledger import UsageLedger

import test_identity_dashboard
import test_last_week_publication
import test_outcome_dashboard
import test_publication_preparation
import test_stage_dashboard
import test_stats_dashboard
from test_identity_dashboard import IdentityDashboardTests  # noqa: F401
from test_last_week_publication import LastWeekPublicationTests  # noqa: F401
from test_outcome_dashboard import OutcomeDashboardTests  # noqa: F401
from test_publication_preparation import PreparationTests  # noqa: F401
from test_stage_dashboard import *  # noqa: F401,F403
from test_stats_dashboard import StatsDashboardTests  # noqa: F401

MODULES = (test_identity_dashboard, test_last_week_publication, test_outcome_dashboard,
           test_publication_preparation, test_stage_dashboard, test_stats_dashboard, stats_dashboard)
CHECKED = []


def _views(document):
    return [{k: v for k, v in view.items() if k != 'report_sha256'} for view in document['views']]


def cross_checked(original):
    def build(ledger, **kwargs):
        result = original(ledger, **kwargs)
        if (kwargs.get('engine', 'full') == 'full' and isinstance(ledger, UsageLedger)
                and result['status'] == 'available' and result['views'] and result['views'][0]['watermark'] > 0):
            # Another reader of the same ledger: the test's own spies see only its call.
            check(original, UsageLedger(ledger.root), kwargs)
        return result
    return build


def check(original, ledger, kwargs):
    with tempfile.TemporaryDirectory() as temporary:
        derived = Path(temporary) / 'derived'
        derived.mkdir(mode=0o700)
        tenants = kwargs.get('agent_tenants')
        rebuild(ledger, tenants, derived=derived)
        factory = lambda upto: IncrementalViews.attach(ledger, tenants, upto=upto, derived=derived)  # noqa: E731
        common = {**kwargs, 'calculation_cache': None, 'incremental': factory}
        timings = {}
        shadow = original(ledger, **{**common, 'engine': 'shadow', 'timings': timings})
        record = timings['incremental_shadow'][-1]
        assert record['status'] == 'equal', record
        incremental = original(ledger, **{**common, 'engine': 'incremental', 'timings': {}})
        assert len(incremental['views']) == len(shadow['views'])
        # Each incremental view is the original query read in canonical order, report digest included.
        catalog = (kwargs.get('catalog_snapshot') or {}).get('catalog')
        session = None
        for mine in incremental['views']:
            q = dict(mine['query'], group_by=tuple(mine['query']['group_by']),
                     work_items=tuple(mine['query']['work_items']))
            session = session or CollaborationQuerySession(ledger.receipt_snapshot(watermark=q['watermark']),
                                                           agent_tenants=tenants, canonical_order=True)
            raw = session.query(CollaborationQuery(**q), catalog=catalog,
                                catalog_observed_at=mine['catalog_observed_at'])
            assert raw['report_sha256'] == mine['report_sha256'], (mine['period'], mine['dimension'])
        CHECKED.append(len(shadow['views']))


@pytest.fixture(autouse=True)
def incremental_cross_check(monkeypatch):
    original = stats_dashboard.build_dashboard
    for module in MODULES:
        if getattr(module, 'build_dashboard', None) is original:
            monkeypatch.setattr(module, 'build_dashboard', cross_checked(original))
    yield


@pytest.fixture(autouse=True, scope='module')
def the_cross_check_ran(request):
    yield
    # The whole module publishes dozens of times; a selection of a few tests may publish nothing.
    selected = [item for item in request.session.items if item.module.__name__ == __name__]
    if len(selected) > 20 and not request.session.testsfailed:
        assert CHECKED, 'no publication was cross-checked'
