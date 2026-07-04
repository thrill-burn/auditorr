"""Two-phase Triage: phase 1 answers from audit-time data only (no torrent
client calls) and ships per-item verdict alternatives; /triage/verify does the
live tracker lookup in bounded batches. The audit persists a compact 'triage'
subset so phase 1 never deserializes the full torrent list."""

import unittest
from unittest.mock import patch

from audit import _is_triage_relevant
import app


def _rec(**over):
    """A minimal torrent file record as stored in file_results."""
    base = {
        'path': 'radarr/Movie.2020.1080p.WEB-DL/Movie.2020.1080p.WEB-DL.mkv',
        'size': 100, 'status': 'Seeding', 'imported': False, 'excluded': False,
        'hash': 'AAAA', 'instance_id': 1, 'trackers': ['tracker.example'],
        'tracker_health': 'unknown', 'tracker_msg': '',
    }
    base.update(over)
    return base


class TriageRelevantPredicateTests(unittest.TestCase):
    """_is_triage_relevant must select exactly the records the endpoint's
    filters read: not-imported, imported dead seeds, dead-sibling carriers."""

    def test_excluded_never_relevant(self):
        self.assertFalse(_is_triage_relevant(_rec(excluded=True)))
        self.assertFalse(_is_triage_relevant(_rec(
            excluded=True, dead_siblings=[{'hash': 'B'}])))

    def test_dead_sibling_carrier_relevant_regardless_of_status(self):
        self.assertTrue(_is_triage_relevant(_rec(
            imported=True, tracker_health='working',
            dead_siblings=[{'hash': 'B'}])))
        self.assertTrue(_is_triage_relevant(_rec(
            status='Orphaned', dead_siblings=[{'hash': 'B'}])))

    def test_orphaned_without_siblings_not_relevant(self):
        self.assertFalse(_is_triage_relevant(_rec(status='Orphaned')))

    def test_not_imported_relevant(self):
        self.assertTrue(_is_triage_relevant(_rec(imported=False)))

    def test_imported_dead_seed_relevant(self):
        self.assertTrue(_is_triage_relevant(_rec(
            imported=True, tracker_health='unregistered')))

    def test_imported_healthy_not_relevant(self):
        self.assertFalse(_is_triage_relevant(_rec(
            imported=True, tracker_health='working')))


class VerdictUnderTests(unittest.TestCase):
    ALTS = {'working': None, 'unregistered': 'dead_seed', 'other': 'dead_seed'}

    def test_working_selects_working_slot(self):
        self.assertIsNone(app._triage_verdict_under(self.ALTS, 'working'))

    def test_unregistered_selects_unregistered_slot(self):
        self.assertEqual(app._triage_verdict_under(self.ALTS, 'unregistered'), 'dead_seed')

    def test_everything_else_selects_other(self):
        for health in ('not_working', 'unknown', '', None):
            self.assertEqual(app._triage_verdict_under(self.ALTS, health), 'dead_seed')


def _phase1(records, has_subset=True):
    """Call GET /api/workflows/triage with stored records and no arr data.

    sources.fetch_torrent_details is patched to explode: phase 1 must never
    contact the torrent client.
    """
    def _load(tab):
        return records
    def _boom(*a, **k):
        raise AssertionError("phase 1 must not call the torrent client")
    with patch.object(app, 'db_load_config', return_value={}), \
         patch.object(app, 'db_has_file_results', return_value=has_subset), \
         patch.object(app, 'db_load_file_results', side_effect=_load) as load_mock, \
         patch.object(app, 'fetch_arr_media_index', return_value=[]), \
         patch.object(app, 'fetch_arr_all_titles', return_value=[]), \
         patch.object(app, 'normalize_arr_connections', return_value=[]), \
         patch.object(app.sources, 'fetch_torrent_details', side_effect=_boom):
        client = app.app.test_client()
        resp = client.get('/api/workflows/triage')
    return resp, load_mock


class TriagePhaseOneTests(unittest.TestCase):
    def test_not_imported_uses_stored_health_and_ships_alternatives(self):
        resp, _ = _phase1([_rec(tracker_health='unregistered',
                                tracker_msg='Unregistered torrent')])
        self.assertEqual(resp.status_code, 200)
        items = resp.get_json()['items']
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertEqual(it['verdict'], 'unregistered')
        self.assertEqual(it['tracker_msg'], 'Unregistered torrent')
        # No arr configured → the non-unregistered fallback is not_in_library
        self.assertEqual(it['verdict_alternatives'],
                         {'working': 'not_in_library',
                          'unregistered': 'unregistered',
                          'other': 'not_in_library'})
        # Live-only fields arrive via /verify, never phase 1
        self.assertIsNone(it['uploaded'])
        self.assertIsNone(it['seeding_time'])

    def test_dead_seed_kept_in_phase1_with_drop_alternative(self):
        # Live 'working' re-verification now happens client-side: phase 1 keeps
        # the row and marks it droppable via alternatives['working'] = None.
        resp, _ = _phase1([_rec(imported=True, tracker_health='unregistered',
                                tracker_msg='Torrent has been deleted.')])
        items = resp.get_json()['items']
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]['verdict'], 'dead_seed')
        self.assertIsNone(items[0]['verdict_alternatives']['working'])

    def test_dead_registration_from_dead_siblings(self):
        resp, _ = _phase1([_rec(imported=True, tracker_health='working',
                                dead_siblings=[{'hash': 'DEAD', 'instance_id': 1,
                                                'tracker_msg': 'unregistered'}])])
        items = resp.get_json()['items']
        self.assertEqual([i['verdict'] for i in items], ['dead_registration'])
        self.assertEqual(items[0]['hash'], 'DEAD')
        self.assertIsNone(items[0]['verdict_alternatives']['working'])
        self.assertTrue(items[0]['alive_sibling'])

    def test_loads_subset_when_present_full_list_when_not(self):
        _, load_mock = _phase1([], has_subset=True)
        self.assertEqual(load_mock.call_args[0][0], 'triage')
        _, load_mock = _phase1([], has_subset=False)
        self.assertEqual(load_mock.call_args[0][0], 'torrents')


class TriageVerifyEndpointTests(unittest.TestCase):
    def _post(self, payload, details=None, error=None):
        def _fetch(cfg, items):
            if error is not None:
                raise error
            return details or {}
        with patch.object(app, 'db_load_config', return_value={}), \
             patch.object(app.sources, 'fetch_torrent_details',
                          side_effect=_fetch) as fetch_mock:
            client = app.app.test_client()
            resp = client.post('/api/workflows/triage/verify', json=payload)
        return resp, fetch_mock

    def test_returns_details_for_batch(self):
        details = {'AAAA': {'tracker_health': 'working', 'tracker_msg': '',
                            'uploaded': 5, 'ratio': 1.0, 'seeding_time': 60,
                            'added_on': 1}}
        resp, fetch_mock = self._post(
            {'items': [{'hash': 'AAAA', 'instance_id': 1}]}, details=details)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['details'], details)
        self.assertEqual(fetch_mock.call_args[0][1],
                         [{'hash': 'AAAA', 'instance_id': 1}])

    def test_oversized_batch_rejected(self):
        resp, _ = self._post(
            {'items': [{'hash': f'H{i}'} for i in range(app._TRIAGE_VERIFY_BATCH_MAX + 1)]})
        self.assertEqual(resp.status_code, 400)

    def test_empty_or_hashless_items_short_circuit(self):
        def _boom(*a, **k):
            raise AssertionError("must not contact the client for an empty batch")
        with patch.object(app, 'db_load_config', return_value={}), \
             patch.object(app.sources, 'fetch_torrent_details', side_effect=_boom):
            client = app.app.test_client()
            resp = client.post('/api/workflows/triage/verify',
                               json={'items': [{'instance_id': 1}]})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['details'], {})

    def test_client_failure_is_surfaced_not_swallowed(self):
        resp, _ = self._post({'items': [{'hash': 'AAAA'}]},
                             error=app.sources.SourceConnectionError("qBittorrent error: down"))
        self.assertEqual(resp.status_code, 502)
        self.assertIn('down', resp.get_json()['message'])


if __name__ == '__main__':
    unittest.main()
