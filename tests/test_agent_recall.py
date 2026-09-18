from __future__ import annotations

import io
import json
import os
import shlex
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import agent_recall as ar
from test_claude_recall import codex_meta, codex_user, user_msg


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {
            'CLAUDE_PROJECTS_DIR': str(self.root / 'missing-claude'),
            'AGENT_RECALL_CACHE': str(self.root / 'index.json'),
            'AGENT_RECALL_CODEX_HOMES': '',
            'AGENT_RECALL_PI_DIRS': '',
            'AGENT_RECALL_OPENCODE_DBS': '',
        }
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict(os.environ, self.env).start()

    def write(self, path, entries):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n')
        return path

    def pi_session(self):
        path = self.root / 'pi' / 'project' / 'timestamp_session.jsonl'
        self.write(path, [
            {'type': 'session', 'id': 'abcd1234-0000-0000-0000-000000000001', 'cwd': str(self.root), 'timestamp': '2026-09-18T09:00:00Z'},
            {'type': 'message', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'build auth middleware'}, {'type': 'image', 'data': 'irrelevant'}]}},
            {'type': 'message', 'message': {'role': 'toolResult', 'content': 'ignore tool output in user prompts'}},
            {'type': 'message', 'message': {'role': 'assistant', 'content': 'quasar implementation'}},
            {'type': 'session_info', 'name': 'Authentication'},
            {'type': 'message', 'timestamp': '2026-09-18T10:00:00Z', 'message': {'role': 'user', 'content': 'add tests'}},
            [], None,
        ])
        return path

    def database(self):
        path = self.root / 'data with spaces' / 'opencode' / 'opencode.db'
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path)
        self.addCleanup(db.close)
        db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE session (id TEXT PRIMARY KEY, parent_id TEXT, directory TEXT, title TEXT, time_created INTEGER, time_updated INTEGER);
            CREATE TABLE message (id TEXT PRIMARY KEY, session_id TEXT, time_created INTEGER, data TEXT);
            CREATE TABLE part (id TEXT PRIMARY KEY, message_id TEXT, session_id TEXT, time_created INTEGER, data TEXT);
        ''')
        for sid, parent in [('ses_main', None), ('ses_child', 'ses_main'), ('ses_other', None)]:
            db.execute('INSERT INTO session VALUES (?, ?, ?, ?, ?, ?)', (sid, parent, str(self.root), 'Authentication' if sid == 'ses_main' else 'Other topic', 1750000000000, 1750000001000))
            db.execute('INSERT INTO message VALUES (?, ?, 1, ?)', (sid+'-user', sid, json.dumps({'role': 'user'})))
            db.execute('INSERT INTO part VALUES (?, ?, ?, 1, ?)', (sid+'-text', sid+'-user', sid, json.dumps({'type': 'text', 'text': 'build auth middleware' if sid == 'ses_main' else 'gardening'})))
        db.execute('INSERT INTO part VALUES (?, ?, ?, 2, ?)', ('synthetic', 'ses_main-user', 'ses_main', json.dumps({'type': 'text', 'text': 'synthetic text', 'synthetic': True})))
        db.execute('INSERT INTO message VALUES (?, ?, 2, ?)', ('assistant', 'ses_main', json.dumps({'role': 'assistant'})))
        db.execute('INSERT INTO part VALUES (?, ?, ?, 2, ?)', ('assistant-text', 'assistant', 'ses_main', json.dumps({'type': 'text', 'text': 'quasar implementation'})))
        db.commit()
        return path, db

    def run_cli(self, *args):
        output = io.StringIO()
        with mock.patch('sys.stdout', output), mock.patch('sys.stderr', io.StringIO()):
            code = ar.main(list(args))
        return code, output.getvalue()

    def test_pi_messages_titles_and_resume(self):
        path = self.pi_session()
        session = ar.parse_pi_session(path)
        self.assertEqual(session.source, 'pi')
        self.assertEqual(session.humans, 2)
        self.assertEqual(session.title, 'Authentication')
        self.assertEqual(session.first_prompt, 'build auth middleware')
        self.assertNotIn('tool output', session.user_text)
        self.assertEqual(session.modified, '2026-09-18T10:00:00Z')
        self.assertEqual(ar.resume_command(session), ['pi', '--session', str(path)])
        self.assertEqual(ar.resume_command(session, fork=True), ['pi', '--fork', str(path)])

    def test_pi_cli_without_claude_or_agent_executables(self):
        self.pi_session()
        with mock.patch.dict(os.environ, {'AGENT_RECALL_PI_DIRS': str(self.root/'pi')}), mock.patch.object(ar.shutil, 'which', return_value=None):
            code, output = self.run_cli('auth middleware', '--json', '--source', 'pi', '--all')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['results'][0]['source'], 'pi')

    def test_codex_cli_without_claude_directory(self):
        home = self.root/'codex'
        self.write(home/'sessions'/'rollout.jsonl', [codex_meta('abcd1234-0000-0000-0000-000000000001', str(self.root)), codex_user('build auth middleware')])
        with mock.patch.dict(os.environ, {'AGENT_RECALL_CODEX_HOMES': str(home)}):
            code, output = self.run_cli('auth middleware', '--source', 'codex', '--json')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['results'][0]['source'], 'codex')

    def test_codex_human_forks_are_searchable_but_subagents_are_not(self):
        path = self.root/'rollout.jsonl'
        metadata = codex_meta('abcd1234', str(self.root), forked_from='parent')
        self.write(path, [metadata, codex_user('auth middleware')])
        self.assertIsNotNone(ar.parse_codex_rollout(path, self.root))
        metadata['payload']['source'] = {'subagent': {'thread_spawn': {'parent_thread_id': 'parent'}}}
        self.write(path, [metadata, codex_user('auth middleware')])
        self.assertIsNone(ar.parse_codex_rollout(path, self.root))

    def test_opencode_reads_user_parts_and_skips_child_sessions(self):
        path, _ = self.database()
        sessions = ar.read_opencode_sessions(path, {})
        self.assertEqual({s.session_id for s in sessions}, {'ses_main', 'ses_other'})
        session = next(s for s in sessions if s.session_id == 'ses_main')
        self.assertEqual(session.humans, 1)
        self.assertEqual(session.user_text, 'build auth middleware')
        self.assertEqual(ar.resume_command(session, fork=True), ['opencode', '--session', 'ses_main', '--fork'])
        self.assertEqual(ar.resume_prompt(session, ['where did', 'we stop?']), ['--prompt', 'where did we stop?'])
        self.assertEqual(ar.resume_env(session), {'XDG_DATA_HOME': str(path.parent.parent)})

    def test_opencode_cli_id_and_deep_search_do_not_match_other_sessions(self):
        path, _ = self.database()
        with mock.patch.dict(os.environ, {'AGENT_RECALL_OPENCODE_DBS': str(path)}):
            code, output = self.run_cli('ses_main', '--json', '--source', 'opencode')
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)['results'][0]['session_id'], 'ses_main')
            code, output = self.run_cli('quasar', '--json', '--source', 'opencode', '--all')
            self.assertEqual(code, 0)
            self.assertEqual([s['session_id'] for s in json.loads(output)['results']], ['ses_main'])

    def test_opencode_cache_sees_wal_writes_and_deletions(self):
        path, db = self.database()
        cache = self.root/'index.json'
        kwargs = {'opencode_dbs': [path]}
        initial = ar.build_index(self.root/'absent', cache, **kwargs)
        db.execute('UPDATE part SET data=? WHERE id=?', (json.dumps({'type':'text', 'text':'new topic'}), 'ses_main-text'))
        db.commit()
        updated = ar.build_index(self.root/'absent', cache, **kwargs)
        self.assertNotEqual(initial, updated)
        self.assertEqual(next(s for s in updated if s.session_id == 'ses_main').first_prompt, 'new topic')
        db.execute('DELETE FROM session WHERE id=?', ('ses_other',))
        db.commit()
        self.assertEqual(len(ar.build_index(self.root/'absent', cache, **kwargs)), 1)

    def test_corrupt_database_does_not_hide_pi_results(self):
        path = self.root/'broken.db'
        path.write_text('not sqlite')
        self.pi_session()
        with mock.patch('sys.stderr', io.StringIO()) as err:
            sessions = ar.build_index(self.root/'absent', self.root/'index.json', pi_roots=[self.root/'pi'], opencode_dbs=[path])
        self.assertEqual([s.source for s in sessions], ['pi'])
        self.assertIn('Could not read OpenCode', err.getvalue())

    def test_pi_cache_reused_then_invalidated(self):
        path = self.pi_session()
        cache = self.root/'index.json'
        kwargs = {'pi_roots': [self.root/'pi']}
        ar.build_index(self.root/'absent', cache, **kwargs)
        with mock.patch.object(ar, 'parse_pi_session', side_effect=AssertionError('cache miss')):
            self.assertEqual(len(ar.build_index(self.root/'absent', cache, **kwargs)), 1)
        with path.open('a') as output:
            output.write(json.dumps({'type': 'session_info', 'name': 'New title'})+'\n')
        self.assertEqual(ar.build_index(self.root/'absent', cache, **kwargs)[0].title, 'New title')
        path.unlink()
        self.assertEqual(ar.build_index(self.root/'absent', cache, **kwargs), [])

    def test_bad_cache_and_partial_lines_are_ignored(self):
        cache = self.root/'index.json'
        for raw in ['[]', 'null', '{broken', json.dumps({'version': ar.INDEX_VERSION, 'sessions': [None, [], {'bad': 'entry'}]})]:
            cache.write_text(raw)
            self.assertEqual(ar.load_cache(cache), {})
        path = self.write(self.root/'claude.jsonl', [None, [], user_msg('actual prompt')])
        with path.open('a') as output:
            output.write('{partial')
        self.assertEqual(ar.parse_transcript(path).first_prompt, 'actual prompt')

    def test_ambiguous_id_does_not_select_arbitrary_session(self):
        sessions = [ar.Session(str(i), 0, 0, 'abcd1234', source=source, first_prompt='auth middleware') for i, source in enumerate(['pi', 'codex'])]
        self.assertIsNone(ar.lookup_id(sessions, 'abcd1234'))
        pool, _ = ar.candidate_pool(sessions, 'auth', here=None, all_projects=True)
        self.assertEqual(len(pool), 2)
        hits = {'abcd1111': ar.Hit(sessions[0], 1), 'abcd2222': ar.Hit(sessions[1], 1)}
        self.assertIsNone(ar.resolve_hit(hits, 'abcd'))
        self.assertIsNone(ar.resolve_hit(hits, 'abcd1111-invalid'))

    def test_codex_default_home_overrides_inherited_custom_home(self):
        session = ar.Session('x', 0, 0, 'id', source='codex', agent_home=str(Path.home()/'.codex'))
        with mock.patch.dict(os.environ, {'CODEX_HOME': '/other'}):
            self.assertEqual(ar.resume_env(session), {'CODEX_HOME': str(Path.home()/'.codex')})
        self.assertEqual(ar.resume_command(session, fork=True), ['codex', 'fork', 'id'])

    def test_env_aliases_and_empty_override(self):
        with mock.patch.dict(os.environ, {'AGENT_RECALL_CODEX_HOMES': '', 'CLAUDE_RECALL_CODEX_HOMES': '/old'}):
            self.assertEqual(ar.codex_homes(), [])
        with mock.patch.dict(os.environ, {'AGENT_RECALL_DEEP_SECONDS': 'NaN'}):
            self.assertEqual(self.run_cli('--json')[0], 2)

    def test_missing_resume_cli_is_a_friendly_error(self):
        session = ar.Session('file', 0, 0, 'id', source='pi')
        with mock.patch.object(ar.shutil, 'which', return_value=None), mock.patch('sys.stderr', io.StringIO()) as err:
            self.assertEqual(ar.resume_session(session, [], fork=False), 2)
        self.assertIn('pi CLI not on PATH', err.getvalue())

    def test_invalid_limit_rejected(self):
        with mock.patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
            ar.parse_args(['--limit', '0'])

    def test_printed_resume_preserves_cwd_and_quoted_prompt(self):
        session = ar.Session('file', 0, 0, 'ses_main', source='opencode', cwd=str(self.root))
        command = ar.display_resume(session, extra=["what's next?"])
        self.assertEqual(shlex.split(command), ['cd', str(self.root), '&&', 'opencode', '--session', 'ses_main', '--prompt', "what's next?"])

    def test_local_ranking_never_calls_model(self):
        self.pi_session()
        with mock.patch.dict(os.environ, {'AGENT_RECALL_PI_DIRS': str(self.root/'pi')}), mock.patch.object(ar, 'claude_find', side_effect=AssertionError('model called')):
            self.assertEqual(self.run_cli('auth', '--all', '--json')[0], 0)

    def test_model_ranking_uses_distinct_candidate_ids(self):
        sessions = [ar.Session(str(i), 0, 0, 'duplicate', source=source) for i, source in enumerate(['pi', 'codex'])]
        hits = [ar.Hit(s, 1) for s in sessions]
        proc = subprocess.CompletedProcess([], 0, '{"results":[{"id":"candidate-2", "reason":"Built it"}]}', '')
        with mock.patch.object(ar.subprocess, 'run', return_value=proc) as run:
            ranked, error = ar.claude_find(hits, 'auth', limit=1)
        self.assertIsNone(error)
        self.assertEqual(ranked[0].session.source, 'codex')
        self.assertIn('candidate-1', run.call_args.args[0][-1])
        self.assertIn('--tools', run.call_args.args[0])

    def test_installer_links_both_commands_and_shared_skill(self):
        repo = Path(__file__).resolve().parents[1]
        env = dict(os.environ, HOME=str(self.root), CLAUDE_CONFIG_DIR=str(self.root/'.claude'))
        proc = subprocess.run(['bash', str(repo/'install.sh')], env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        for command in ['agent-recall', 'ccrecall']:
            target = self.root/'.local/bin'/command
            self.assertTrue(target.is_symlink())
            result = subprocess.run([str(target), '--version'], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(ar.__version__, result.stdout)
        for skills in ['.agents/skills', '.claude/skills']:
            self.assertEqual((self.root/skills/'find-session').resolve(), repo/'skills/find-session')

    def test_installer_cli_only_and_skill_conflict(self):
        repo = Path(__file__).resolve().parents[1]
        env = dict(os.environ, HOME=str(self.root), CLAUDE_CONFIG_DIR=str(self.root/'.claude'))
        proc = subprocess.run(['bash', str(repo/'install.sh'), '--cli-only'], env=env, capture_output=True)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse((self.root/'.agents').exists())
        conflict = self.root/'.agents/skills/find-session'
        conflict.mkdir(parents=True)
        (conflict/'existing').write_text('keep me')
        proc = subprocess.run(['bash', str(repo/'install.sh')], env=env, capture_output=True, text=True)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn('Refusing to replace', proc.stderr)
        self.assertEqual((conflict/'existing').read_text(), 'keep me')
        self.assertEqual(list(conflict.iterdir()), [conflict/'existing'])

    def test_legacy_module_is_compatible(self):
        import claude_recall
        self.assertIs(claude_recall.Session, ar.Session)

    def test_deep_scan_passes_remaining_budget_to_grep(self):
        proc = subprocess.CompletedProcess([], 1, '', '')
        with mock.patch.object(ar, '_shortlist_tool', return_value=['rg']), mock.patch.object(ar.time, 'monotonic', return_value=10), mock.patch.object(ar.subprocess, 'run', return_value=proc) as run:
            ar._grep_shortlist(['file'], ['auth'], deadline=10.5)
        self.assertEqual(run.call_args.kwargs['timeout'], 0.5)


if __name__ == '__main__':
    unittest.main()
