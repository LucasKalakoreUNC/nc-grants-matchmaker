import json
import os
import re
import sqlite3
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import db
import notifications as n


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(':memory:')
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)
        self.now = datetime(2026, 11, 7, 14, tzinfo=timezone.utc)
        self.clock = patch('db.nc_today', return_value=self.now.astimezone(n.NC).date()).start()
        self.profiles = {'dept': {'name':'Department', 'category':'state', 'eligible_applicant_types':['State governments']},
                         'county': {'name':'County', 'category':'county', 'eligible_applicant_types':['County governments']}}
        patch('pipeline.load_agency_profiles', return_value=self.profiles).start()
        patch.dict(os.environ, {'SMTP_USER':'', 'SMTP_PASS':''}).start()
        self.messages = []

    def tearDown(self):
        patch.stopall()
        self.conn.close()

    def advance(self, **parts):
        self.now += timedelta(**parts)
        self.clock.return_value = self.now.astimezone(n.NC).date()

    def grant(self, oid, days=10, fit=70, eligibility=None):
        db.upsert_opportunity(self.conn, {'id':oid, 'title':oid, 'status':'posted',
            'close_date':(self.now.astimezone(n.NC).date()+timedelta(days=days)).isoformat(),
            'eligibility':eligibility or ['State governments'], 'url':'https://www.grants.gov/'})
        for office in self.profiles:
            db.save_matches(self.conn, office, [{'id':oid, 'total_score':50,
                'score_breakdown':{'relevancy':fit}, 'matched_goal_title':'Workforce'}])
        self.conn.commit()

    def send(self, *message):
        self.messages.append(message)

    def confirm_request(self, grant=None, office='dept', category='state'):
        if grant:
            db.save_decision(self.conn, office, {'opportunity_id':grant,'status':'Pursue'})
        n.request_subscription(self.conn, 'person@example.invalid', office, category, grant=grant, now=self.now)
        self.conn.commit()
        n.run_once(self.conn, self.now, sender=self.send)
        token = re.search(r'token=([A-Za-z0-9_-]+)', self.messages[-1][2]).group(1)
        account = n.confirm(self.conn, token, self.now)
        self.conn.commit()
        self.messages.clear()
        return account

    def test_confirmation_is_required_and_legacy_addresses_stay_inactive(self):
        self.conn.execute('CREATE TABLE subscribers(email TEXT, office_id TEXT)')
        self.conn.execute("INSERT INTO subscribers VALUES ('legacy@example.invalid','dept')")
        result = n.request_subscription(self.conn, 'person@example.invalid', 'dept', 'state', now=self.now)
        self.assertTrue(result['confirmation_required'])
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM notification_preferences').fetchone()[0], 0)
        self.conn.commit()
        n.run_once(self.conn, self.now, sender=self.send)
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.messages[0][0], 'person@example.invalid')
        self.assertIn('Confirm', self.messages[0][1])

    def test_confirmation_waits_for_sender_setup_without_expiring_in_queue(self):
        n.request_subscription(self.conn, 'person@example.invalid', 'dept', 'state', now=self.now)
        self.conn.commit()
        self.assertFalse(n.run_once(self.conn, self.now)['configured'])
        self.advance(days=5)
        n.run_once(self.conn, self.now, sender=self.send)
        token = re.search(r'token=([A-Za-z0-9_-]+)', self.messages[-1][2]).group(1)
        self.assertIsNotNone(n.confirm(self.conn, token, self.now))

    def test_personal_pursuit_reminders_are_sent_once_per_threshold(self):
        self.grant('pursuit', days=30)
        self.confirm_request(grant='pursuit')
        for delta, expected_days in [(0,30),(16,14),(4,10),(3,7),(4,3),(2,1)]:
            self.advance(days=delta)
            result = n.run_once(self.conn, self.now, sender=self.send)
            self.assertEqual(result['sent'], 1)
            left = '1 day left!' if expected_days == 1 else f'{expected_days} days left!'
            self.assertIn(left, self.messages[-1][1])
            self.assertEqual(n.run_once(self.conn, self.now, sender=self.send)['sent'], 0)
        self.assertEqual(len(self.messages), 6)
        self.advance(days=2)
        self.assertEqual(n.run_once(self.conn, self.now, sender=self.send)['sent'], 0)

    def test_new_pursuit_inside_a_window_gets_one_relevant_reminder(self):
        self.grant('late', days=5)
        self.confirm_request(grant='late')
        self.assertEqual(n.run_once(self.conn, self.now, sender=self.send)['sent'], 1)
        self.assertIn('5 days', self.messages[0][1])
        self.assertEqual(len(self.messages), 1)

    def test_pass_stops_a_queued_deadline_reminder(self):
        self.grant('watched')
        self.confirm_request(grant='watched')
        def fail(*args):
            raise OSError('Temporary failure')
        self.assertEqual(n.run_once(self.conn, self.now, sender=fail)['failed'], 1)
        db.save_decision(self.conn, 'dept', {'opportunity_id':'watched','status':'Pass'})
        self.conn.commit()
        self.advance(minutes=10)
        self.assertEqual(n.run_once(self.conn, self.now, sender=self.send)['sent'], 0)
        self.assertEqual(self.messages, [])

    def test_changed_deadline_cancels_old_alert_and_uses_new_date(self):
        self.grant('changed', days=14)
        self.confirm_request(grant='changed')
        def fail(*args):
            raise OSError('Temporary failure')
        n.run_once(self.conn, self.now, sender=fail)
        self.grant('changed', days=30)
        self.advance(minutes=10)
        self.assertEqual(n.run_once(self.conn, self.now, sender=self.send)['sent'], 1)
        self.assertIn('30 days', self.messages[0][1])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notification_outbox WHERE kind='reminder' AND state='cancelled'").fetchone()[0], 1)

    def test_transient_failure_retries_then_does_not_repeat(self):
        self.grant('retry')
        self.confirm_request(grant='retry')
        def fail(*args):
            raise OSError('Temporary failure')
        self.assertEqual(n.run_once(self.conn, self.now, sender=fail)['failed'], 1)
        self.advance(minutes=4)
        self.assertEqual(n.run_once(self.conn, self.now, sender=self.send)['sent'], 0)
        self.advance(minutes=2)
        self.assertEqual(n.run_once(self.conn, self.now, sender=self.send)['sent'], 1)
        self.assertEqual(n.run_once(self.conn, self.now, sender=self.send)['sent'], 0)

    def test_weekly_update_contains_real_new_matches_and_all_matching_30_day_deadlines(self):
        self.grant('existing-long', days=40)
        self.grant('existing-due', days=21)
        self.confirm_request()
        self.assertEqual(n.run_once(self.conn, self.now, sender=self.send)['sent'], 0)  # Saturday
        self.advance(hours=1)
        self.grant('new-match', days=10)
        self.grant('wrong-field', days=7, fit=10)
        self.grant('wrong-applicant', days=7, eligibility=['County governments'])
        n.sync_catalog(self.conn, self.now)
        self.conn.commit()
        self.advance(days=2)
        self.assertEqual(n.run_once(self.conn, self.now, sender=self.send)['sent'], 1)
        self.assertIn('1 new, 2 due', self.messages[0][1])
        self.assertIn('new-match', self.messages[0][2])
        self.assertIn('existing-due', self.messages[0][2])
        self.assertNotIn('wrong-field', self.messages[0][2])
        self.assertNotIn('wrong-applicant', self.messages[0][2])
        self.assertNotIn('existing-long', self.messages[0][2])
        self.assertEqual(n.run_once(self.conn, self.now, sender=self.send)['sent'], 0)
        self.advance(days=7)
        self.assertEqual(n.run_once(self.conn, self.now, sender=self.send)['sent'], 1)
        self.assertIn('0 new', self.messages[-1][1])

    def test_unsubscribe_stops_future_emails_and_preserves_staff_decisions(self):
        self.grant('stop')
        account = self.confirm_request(grant='stop')
        self.assertFalse(n.unsubscribe(self.conn, 'not-a-token'))
        self.assertTrue(n.unsubscribe(self.conn, account['token']))
        self.conn.commit()
        self.assertEqual(n.run_once(self.conn, self.now, sender=self.send)['sent'], 0)
        self.assertEqual(self.conn.execute('SELECT status FROM decisions').fetchone()[0], 'Pursue')

    def test_confirmation_requests_are_cooled_down_and_tokens_expire(self):
        for _ in range(3):
            n.request_subscription(self.conn, 'person@example.invalid', 'dept', 'state', now=self.now)
        self.assertEqual(self.conn.execute('SELECT COUNT(*) FROM notification_outbox').fetchone()[0], 1)
        self.conn.commit()
        n.run_once(self.conn, self.now, sender=self.send)
        token = re.search(r'token=([A-Za-z0-9_-]+)', self.messages[-1][2]).group(1)
        self.advance(hours=49)
        self.assertIsNone(n.confirm(self.conn, token, self.now))

    def test_verified_address_cannot_be_changed_without_its_token(self):
        account = self.confirm_request()
        result = n.request_subscription(self.conn, account['email'], 'county', 'county', now=self.now)
        self.assertTrue(result['confirmation_required'])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM notification_preferences WHERE office_id='county'").fetchone()[0], 0)
        result = n.request_subscription(self.conn, account['email'], 'county', 'county', token=account['token'], now=self.now)
        self.assertTrue(result['active'])

    def test_weekly_schedule_tracks_daylight_saving_time(self):
        winter = datetime(2026, 11, 9, 12, tzinfo=timezone.utc)
        summer = datetime(2026, 3, 9, 11, tzinfo=timezone.utc)
        self.assertEqual(n._week(winter), winter)
        self.assertEqual(n._week(summer), summer)

    def test_dated_state_applications_are_discovered_and_included(self):
        import state_programs
        self.conn.execute(state_programs.SCHEMA)
        n.sync_catalog(self.conn, self.now)
        self.conn.commit()
        self.advance(days=1)
        item = {'id':'nc-workforce', 'name':'NC Workforce', 'url':'https://www.nc.gov/',
                'kind':'North Carolina state program', 'fit':70, 'matched_goal_title':'Workforce',
                'deadlines':[{'date':(self.now.date()+timedelta(days=10)).isoformat(), 'quote':'Apply by this date'}]}
        self.conn.execute('INSERT INTO state_program_matches VALUES (?,?,?,?)', ('county',item['id'],70,json.dumps(item)))
        n.sync_catalog(self.conn, self.now)
        # Reading state matches must not commit the subscription transaction.
        self.assertTrue(self.conn.in_transaction)
        new, due = n.weekly_items(self.conn, 'county', 'county', self.now-timedelta(days=1), self.now)
        self.assertEqual([i['id'] for i in new], ['st-nc-workforce'])
        self.assertEqual([i['id'] for i in due], ['st-nc-workforce'])
        self.assertEqual(n._current_item(self.conn, 'st-nc-workforce', self.now.date(), 'county')['title'], 'NC Workforce')

    def test_gmail_transport_uses_tls_and_app_password(self):
        with patch.dict(os.environ, {'SMTP_USER':'sender@example.invalid','SMTP_PASS':'test app password','SMTP_HOST':'smtp.gmail.com','SMTP_PORT':'587'}), patch('smtplib.SMTP') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            n.send_email('person@example.invalid','Subject','Text','<p>Text</p>','key','https://example.invalid/unsubscribe')
        smtp.assert_called_once_with('smtp.gmail.com',587,timeout=20)
        client = smtp.return_value.__enter__.return_value
        client.starttls.assert_called_once()
        client.login.assert_called_once_with('sender@example.invalid','testapppassword')
        message = client.send_message.call_args.args[0]
        self.assertEqual(message['List-Unsubscribe-Post'],'List-Unsubscribe=One-Click')


if __name__ == '__main__':
    unittest.main()
