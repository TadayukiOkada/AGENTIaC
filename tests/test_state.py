import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from agentiac.state import Store,stable_id

class StateTests(unittest.TestCase):
    def test_finish_refuses_the_wrong_attempt_without_losing_the_running_case(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite')
            key = store.register({'task': 'x'})
            owner, number = store.claim(key)
            before = self.rows(store), self.rows(store, 'attempts')
            with self.assertRaisesRegex(RuntimeError, 'lease owner changed'):
                store.finish(key, owner, number + 1, True, {})
            self.assertEqual((self.rows(store), self.rows(store, 'attempts')), before)
            store.finish(key, owner, number, True, {})

    def test_finish_an_unknown_case_refuses_without_a_type_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(Path(tmp) / 'state.sqlite')
            with self.assertRaisesRegex(RuntimeError, 'lease owner changed'):
                store.finish('missing', 'owner', 1, True, {})

    def rows(self,s,table='cases'):
        with s.connect() as db: return [tuple(r) for r in db.execute('SELECT * FROM '+table)]
    def test_resume_retry_and_stale_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'state.sqlite');k=s.register({'task':'x','revision':'a'})
            claim=s.claim(k,lease_seconds=-1);self.assertIsNotNone(claim)
            second=s.claim(k);self.assertIsNotNone(second)
            with self.assertRaises(RuntimeError):s.finish(k,*claim,True,{})
            s.finish(k,*second,False,{'passed':False})
            self.assertIsNone(s.claim(k));third=s.claim(k,retry_failed=True)
            s.finish(k,*third,True,{'passed':True})
            self.assertIsNone(s.claim(k,retry_failed=True))
            with s.connect() as db:
                states=[r[0] for r in db.execute('SELECT status FROM attempts ORDER BY number')]
            self.assertEqual(states,['interrupted','failed','complete'])
    def test_renew_extends_a_running_lease_and_never_shortens(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'state.sqlite');k=s.register({'task':'x'})
            owner,number=s.claim(k,lease_seconds=60)
            with s.connect() as db: before=db.execute('SELECT lease_until FROM cases WHERE id=?',(k,)).fetchone()[0]
            self.assertIs(s.renew(k,owner,number,180),True)
            with s.connect() as db: grown=db.execute('SELECT lease_until FROM cases WHERE id=?',(k,)).fetchone()[0]
            self.assertGreater(grown,before)
            self.assertIs(s.claim(k),None)                      # still ours: nobody else can take it
            self.assertIs(s.renew(k,owner,number,1),True)       # a shorter request must not pull it in
            with s.connect() as db: self.assertEqual(db.execute('SELECT lease_until FROM cases WHERE id=?',(k,)).fetchone()[0],grown)
            with s.connect() as db:
                self.assertEqual([r[0] for r in db.execute('SELECT status FROM attempts')],['running'])
    def test_renew_refuses_everyone_else_and_leaves_the_row_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'state.sqlite');k=s.register({'task':'x'})
            owner,number=s.claim(k,lease_seconds=60)
            before=self.rows(s)
            self.assertIs(s.renew(k,'someone-else',number),False)
            self.assertIs(s.renew(k,owner,number+1),False)
            self.assertIs(s.renew('missing',owner,number),False)
            self.assertEqual(self.rows(s),before)
            s.finish(k,owner,number,True,{})
            finished=self.rows(s)
            self.assertIs(s.renew(k,owner,number),False)
            self.assertEqual(self.rows(s),finished)
    def test_renew_follows_the_owner_after_an_expired_lease_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'state.sqlite');k=s.register({'task':'x'})
            owner,number=s.claim(k,lease_seconds=-1)            # already expired
            other,second=s.claim(k,lease_seconds=60)
            self.assertIs(s.renew(k,owner,number),False)        # the old owner lost the case
            self.assertIs(s.renew(k,other,second),True)
    def test_renew_rejects_a_duration_it_cannot_honour(self):
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'state.sqlite');k=s.register({'task':'x'})
            owner,number=s.claim(k,lease_seconds=60)
            before=self.rows(s)
            for bad in (0,-1,float('inf'),float('nan'),True,False,'60',None,1j):
                with self.assertRaises(ValueError):s.renew(k,owner,number,bad)
            self.assertEqual(self.rows(s),before)
    def test_identity_tracks_config_without_order_dependence(self):
        self.assertEqual(stable_id({'a':1,'b':2}),stable_id({'b':2,'a':1}))
        self.assertNotEqual(stable_id({'revision':'a'}),stable_id({'revision':'b'}))
    def test_every_operation_closes_its_connection(self):
        # sqlite3's context manager commits or rolls back but never closes (F14, T5.1)
        with tempfile.TemporaryDirectory() as tmp:
            s=Store(Path(tmp)/'state.sqlite');opened=[];connect=s.connect
            def recording():
                db=connect();opened.append(db);return db
            with mock.patch.object(s,'connect',recording):
                k=s.register({'task':'x'});owner,number=s.claim(k)
                s.renew(k,owner,number);s.finish(k,owner,number,True,{'ok':1});s.summary()
            self.assertEqual(len(opened),5)
            for db in opened:
                with self.assertRaises(sqlite3.ProgrammingError):db.execute('SELECT 1')

if __name__=='__main__':unittest.main()
