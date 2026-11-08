"""The filesystem watcher (#29).

watchdog 7.1.0 rebuilds every recursive watch after an inotify queue overflow,
and on a library of a few thousand folders the rebuild overflows the queue
again, so it loops at 100% CPU for good. 1.8.2's image picked it up because
requirements.txt named watchdog with no version. The pin is the fix. The event
filter is a separate hardening: the kernel queues only what the handler acts on,
so qBittorrent's reads and writes no longer reach the watcher at all.
"""

import inspect
import os
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from watchdog.events import (
    FileSystemEventHandler,
    FileCreatedEvent, FileDeletedEvent, FileMovedEvent,
    DirCreatedEvent, DirDeletedEvent, DirMovedEvent,
)
from watchdog.observers import Observer

import watchdog_handler
from watchdog_handler import AuditDebounceHandler, WATCHED_EVENTS

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Which event classes reach each handler method.
_METHOD_EVENTS = {
    'on_created': {FileCreatedEvent, DirCreatedEvent},
    'on_deleted': {FileDeletedEvent, DirDeletedEvent},
    'on_moved':   {FileMovedEvent, DirMovedEvent},
}


class WatchdogPinTests(unittest.TestCase):
    def test_watchdog_is_pinned_exactly(self):
        with open(os.path.join(REPO, 'requirements.txt'), encoding='utf-8') as f:
            lines = [l.split('#', 1)[0].strip() for l in f]
        spec = [l for l in lines if re.match(r'watchdog\b', l, re.I)]
        self.assertEqual(len(spec), 1, spec)
        self.assertRegex(spec[0], r'^watchdog==\d+\.\d+\.\d+$',
                         'watchdog must be pinned with == (#29): an unpinned '
                         'rebuild is how 7.1.0 reached 1.8.2')
        self.assertNotEqual(spec[0], 'watchdog==7.1.0')


class EventFilterTests(unittest.TestCase):
    def setUp(self):
        self._saved = (watchdog_handler._observer, watchdog_handler._handler)

    def tearDown(self):
        watchdog_handler._observer, watchdog_handler._handler = self._saved

    def test_filter_is_exactly_what_the_handler_acts_on(self):
        # A handler method added without widening the filter would never be
        # called on Linux; an open/close/modify class added to the filter puts
        # every seeding read back into the kernel queue.
        overridden = {
            name for name in dir(AuditDebounceHandler)
            if name.startswith('on_')
            and getattr(AuditDebounceHandler, name) is not getattr(FileSystemEventHandler, name, None)
        }
        self.assertEqual(overridden, set(_METHOD_EVENTS))
        wanted = set().union(*(_METHOD_EVENTS[m] for m in overridden))
        self.assertEqual(set(WATCHED_EVENTS), wanted)
        self.assertEqual(len(WATCHED_EVENTS), len(set(WATCHED_EVENTS)))

    def test_installed_watchdog_accepts_event_filter(self):
        self.assertIn('event_filter', inspect.signature(Observer.schedule).parameters)

    def test_every_root_is_scheduled_with_the_filter(self):
        with tempfile.TemporaryDirectory() as tor, tempfile.TemporaryDirectory() as media:
            cfg = {'WATCHDOG_ENABLED': True, 'LOCAL_PATH': tor, 'MEDIA_PATH': media}
            fake = MagicMock()
            with patch.object(watchdog_handler, 'db_load_config', return_value=cfg), \
                 patch.object(watchdog_handler, 'Observer', return_value=fake):
                watchdog_handler.start_watchdog()
        calls = fake.schedule.call_args_list
        self.assertEqual(sorted(c.args[1] for c in calls), sorted([tor, media]))
        for c in calls:
            self.assertIs(c.kwargs.get('event_filter'), WATCHED_EVENTS)
            self.assertTrue(c.kwargs.get('recursive'))
        fake.start.assert_called_once()


if __name__ == '__main__':
    unittest.main()
