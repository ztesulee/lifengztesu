import copy
import unittest
from appearance_groups import assign_appearance_groups
from model_options import OPTIONS

def vector(index):
    values = [0.] * 512
    values[index] = 1.
    return values

def item(ident, records, model='hybrid', groups=None, reviewed=False):
    return {'id': ident, 'groups': groups or [], 'reviewed': reviewed, 'error': '', 'note': '',
            'analysis_model': OPTIONS[model]['id'], 'appearances': records}

def observation(cloth=0, face=None, frame=0):
    return {'appearance': vector(cloth), 'face': vector(face) if face is not None else None, 'frame': frame}

class AppearanceGroupsTest(unittest.TestCase):
    def group(self, items, mode='hybrid', groups=None):
        groups = groups or {}
        assign_appearance_groups(items, groups, OPTIONS[mode]['id'], mode == 'hybrid')
        return groups

    def test_back_view_matches_face_reference_by_appearance(self):
        items = [item('front', [observation(face=3)]), item('back', [observation()])]
        self.group(items)
        self.assertEqual(items[0]['groups'], items[1]['groups'])
        self.assertFalse(items[1]['reviewed'])

    def test_conflicting_faces_do_not_merge_even_same_clothes(self):
        items = [item('one', [observation(face=3)]), item('two', [observation(face=4)])]
        self.group(items)
        self.assertNotEqual(items[0]['groups'], items[1]['groups'])

    def test_two_people_in_frame_not_silently_merged(self):
        items = [item('pair', [observation(), observation()], model='outfit')]
        self.group(items, 'outfit')
        self.assertIn('冲突', items[0]['note'])
        self.assertIsNone(items[0]['appearances'][1].get('assigned_group'))

    def test_video_repeated_person_can_match_across_frames(self):
        items = [item('video', [observation(frame=0), observation(frame=1)], model='outfit')]
        self.group(items, 'outfit')
        self.assertEqual(len(items[0]['groups']), 1)
        self.assertEqual(items[0]['appearances'][0]['assigned_group'], items[0]['appearances'][1]['assigned_group'])

    def test_ambiguous_outfit_stays_pending(self):
        first, second = observation(0), observation(1)
        candidate = observation()
        candidate['appearance'] = [.70710678, .70710678] + [0.] * 510
        items = [item('a', [first], model='outfit'), item('b', [second], model='outfit'), item('c', [candidate], model='outfit')]
        self.group(items, 'outfit')
        self.assertEqual(items[2]['groups'], [])

    def test_manual_group_survives_and_is_reference(self):
        items = [item('a', [observation(face=3)], groups=['manual'], reviewed=True), item('b', [observation()])]
        self.group(items, groups={'manual': {'name': '模特A'}})
        self.assertEqual(items[1]['groups'], ['manual'])
        self.assertTrue(items[0]['reviewed'])

    def test_other_model_never_used_as_reference(self):
        items = [item('old', [observation()], model='outfit', groups=['old'], reviewed=True), item('new', [observation()])]
        self.group(items, groups={'old': {'name': '旧人物'}})
        self.assertNotIn('old', items[1]['groups'])

if __name__ == '__main__':
    unittest.main()
