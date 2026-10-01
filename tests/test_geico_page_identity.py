"""Deterministic synthetic page ID collision; no browser or carrier calls."""
import gc
from unittest.mock import patch
from test_geico_pending_cancellation_noc import NavPage
from robie_job_engine import geico_pending_cancellation_noc as geico


def test_reused_page_integer_identity_cannot_inherit_selected_chip():
 first=NavPage(chip_mode='bare-empty');second=NavPage(chip_mode='bare-empty')
 # Simulates Python reusing an old object's integer ID, without allocator luck.
 with patch.object(geico,'id',return_value=123456,create=True):
  geico._click_pending_chip(first)
  assert geico.pending_view_selected(first)
  assert not geico.pending_view_selected(second)
  grid=geico.PlaywrightGeicoNocBrowser(second).load_pending_cancellations()
 assert len(geico.parse_alert_grid(grid))==3
 assert second.clicks==['Pending Cancellations (3)']


def test_page_registry_does_not_retain_dead_page_or_stale_marker():
 page=NavPage(chip_mode='bare-empty');key=id(page)
 geico._click_pending_chip(page)
 assert key in geico._PENDING_CHIP_CLICKED
 del page;gc.collect()
 assert key not in geico._PENDING_CHIP_CLICKED


def test_old_page_cleanup_cannot_remove_new_page_marker_on_collision():
 first=NavPage(chip_mode='bare-empty');second=NavPage(chip_mode='bare-empty')
 with patch.object(geico,'id',return_value=654321,create=True):
  geico._click_pending_chip(first);geico._click_pending_chip(second)
  del first;gc.collect()
  assert geico._pending_chip_was_clicked(second)


def test_nonweakref_page_does_not_get_unproven_selection_marker():
 geico._remember_pending_chip(object())
