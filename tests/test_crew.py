import unittest
from driver.crew import CrewAccess, permitted


class CrewTests(unittest.TestCase):
    def test_expiry_revocation_and_no_token_listing(self):
        now = [100.]
        crew = CrewAccess(lambda: now[0])
        issued = crew.issue(role='editor', hours=.25)
        self.assertEqual(crew.resolve(issued['token'])['role'], 'editor')
        self.assertNotIn('token', crew.list()[0])
        self.assertNotIn(issued['token'], repr(crew._members))
        now[0] += 901
        self.assertIsNone(crew.resolve(issued['token']))
        issued = crew.issue()
        crew.revoke(issued['id'])
        self.assertIsNone(crew.resolve(issued['token']))

    def test_roles_fail_closed_for_mutation(self):
        viewer = dict(role='viewer', allow_ai=False)
        editor = dict(role='editor', allow_ai=False)
        operator = dict(role='operator', allow_ai=False)
        for route in ('/api/stick', '/api/grab', '/api/action', '/api/connect', '/api/move/roll', '/api/unknown'):
            self.assertFalse(permitted(viewer, 'POST', route))
            self.assertFalse(permitted(editor, 'POST', route))
        self.assertTrue(permitted(editor, 'POST', '/api/move'))
        self.assertFalse(permitted(operator, 'POST', '/api/settings/ai'))
        self.assertFalse(permitted(editor, 'POST', '/api/assistant/send'))
        self.assertTrue(permitted(dict(editor, allow_ai=True), 'POST', '/api/assistant/send'))

    def test_inputs(self):
        for value in (True, float('nan'), 0, 25):
            with self.assertRaises(ValueError):
                CrewAccess().issue(hours=value)
        with self.assertRaises(ValueError):
            CrewAccess().issue(role='owner')
        with self.assertRaises(ValueError):
            CrewAccess().issue(role='viewer', allow_ai=True)
