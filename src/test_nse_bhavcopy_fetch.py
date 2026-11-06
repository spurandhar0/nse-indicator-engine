"""Offline regression tests: no NSE/Telegram requests and no production writes."""
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

import nse_bhavcopy_fetch as fetch


def stamp(value):
    return datetime.fromisoformat(value)


def csv_for(day):
    return ("SYMBOL, SERIES, DATE1, CLOSE_PRICE, TTL_TRD_QNTY\n"
            f"TEST, EQ, {day:%d-%b-%Y}, 100.5, 1000\n").encode()


class Response:
    def __init__(self, status=200, content=b""):
        self.status_code, self.content = status, content


class Session:
    def __init__(self, responses):
        self.responses, self.urls = iter(responses), []

    def get(self, url, **kwargs):
        self.urls.append(url)
        if "products/content" not in url:
            return Response()
        return next(self.responses)


class DateTests(unittest.TestCase):
    def test_actual_delayed_nov5_run(self):
        self.assertEqual(fetch.publication_date(stamp("2026-11-05T18:45:23+00:00")), date(2026, 11, 5))

    def test_before_and_at_cutoff(self):
        self.assertEqual(fetch.publication_date(stamp("2026-11-06T18:29:59+05:30")), date(2026, 11, 5))
        self.assertEqual(fetch.publication_date(stamp("2026-11-06T18:30:00+05:30")), date(2026, 11, 6))

    def test_weekend_and_monday_morning(self):
        for value in ("2026-11-07T19:00:00+05:30", "2026-11-08T19:00:00+05:30", "2026-11-09T09:00:00+05:30"):
            self.assertEqual(fetch.publication_date(stamp(value)), date(2026, 11, 6))

    def test_known_holiday(self):
        self.assertEqual(fetch.publication_date(stamp("2026-01-26T19:00:00+05:30"), {date(2026, 1, 26)}), date(2026, 1, 23))

    def test_year_boundary(self):
        self.assertEqual(fetch.publication_date(stamp("2027-01-01T00:15:00+05:30")), date(2026, 12, 31))

    def test_naive_rejected(self):
        with self.assertRaises(ValueError):
            fetch.publication_date(datetime(2026, 11, 6))


class DownloadTests(unittest.TestCase):
    def test_validation_rejects_html_empty_wrong_date(self):
        day = date(2026, 11, 5)
        for content in (b"<html>not data</html>", csv_for(day).split(b"\n")[0], csv_for(date(2026, 11, 4))):
            with self.assertRaises(ValueError):
                fetch.validate_csv(content, day)
        self.assertEqual(fetch.validate_csv(csv_for(day), day), 1)

    def test_retries_keep_target_and_save_atomic(self):
        day = date(2026, 11, 5)
        session = Session([Response(404), Response(200, b"<html>bad</html>"), Response(200, csv_for(day))])
        delays = []
        with tempfile.TemporaryDirectory() as root, patch.object(fetch, "tg_send") as tg:
            path = fetch.fetch_bhav(stamp("2026-11-06T00:15:00+05:30"), session, set(), delays.append, root)
            self.assertEqual(path.read_bytes(), csv_for(day))
            self.assertFalse(path.with_suffix(".csv.tmp").exists())
            self.assertEqual(delays, [30, 90])
            self.assertEqual(len(set(u for u in session.urls if "products/content" in u)), 1)
            self.assertIn("05112026", session.urls[-1])
            tg.assert_called_once()

    def test_failure_does_not_save_or_send_document(self):
        session = Session([Response(404)] * 3)
        with tempfile.TemporaryDirectory() as root, patch.object(fetch, "tg_send") as tg:
            with self.assertRaises(RuntimeError):
                fetch.fetch_bhav(stamp("2026-11-06T00:15:00+05:30"), session, set(), lambda _: None, root)
            self.assertEqual(list(Path(root).rglob("*.csv")), [])
            tg.assert_not_called()

    def test_existing_valid_file_is_idempotent(self):
        with tempfile.TemporaryDirectory() as root, patch.object(fetch, "tg_send") as tg:
            path = Path(root) / "Nov-2026/sec_bhavdata_full_05112026.csv"
            path.parent.mkdir()
            path.write_bytes(csv_for(date(2026, 11, 5)))
            session = Session([])
            self.assertEqual(fetch.fetch_bhav(stamp("2026-11-06T00:15:00+05:30"), session, set(), root=root), path)
            self.assertEqual(session.urls, [])
            tg.assert_not_called()

    def test_failure_exit_code(self):
        with patch.object(fetch, "fetch_bhav", side_effect=RuntimeError("not available")), patch.object(fetch, "tg_msg"):
            self.assertEqual(fetch.main(), 1)


if __name__ == "__main__":
    unittest.main()
