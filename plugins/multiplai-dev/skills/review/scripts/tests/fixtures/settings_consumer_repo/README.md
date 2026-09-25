The DB2038 regression fixture. `tests/fixture_repo.py` builds a two-commit
repository from these trees: `base/` is the first commit, `head/` the second.
`settings.py` defines CHANNEX_OC_OTA_NAME, `direct_booking.py` uses it, and
`rateplan_service.py` hardcodes the keyword the review is about.
