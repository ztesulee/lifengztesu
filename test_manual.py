import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image
from app import App
from media import import_media, fingerprint, transcode
from project import Project


class ManualTest(unittest.TestCase):
    def test_qq_friend_match_excludes_groups_and_partial_numbers(self):
        from qq_sender import friend_name, file_record_count
        self.assertEqual(friend_name('aa (540879838) 来自: 我的好友', '540879838'), 'aa')
        self.assertIsNone(friend_name('群聊 (377人) 包含：540879838', '540879838'))
        self.assertIsNone(friend_name('aa (1540879838) 来自: 我的好友', '540879838'))
        class Text:
            def __init__(self, value): self.value = value
            def window_text(self): return self.value
        group = SimpleNamespace(children=lambda **_: [Text('qq-send-te'), Text('st.jpg')])
        messages = SimpleNamespace(descendants=lambda **_: [group])
        self.assertEqual(file_record_count(messages, 'qq-send-test.jpg'), 1)

    def test_batch_publish_isolates_empty_and_failed_people(self):
        from publishing import publish_people
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = Project(root / 'project', '批量发布测试')
            self.addCleanup(project.close)
            project.data['groups'] = {g:{'name':g, 'token':'token-'+g, 'published':False} for g in ('a','empty','bad','b')}
            source = root / 'photo.jpg'
            Image.new('RGB', (40,40), 'red').save(source)
            item = import_media(source, project.cache, 'a')
            item['groups'] = ['a','b']
            missing = dict(item, id='missing', name='missing.jpg', path=str(root/'missing.jpg'), groups=['bad'])
            project.data['items'] = [item, missing]
            results = publish_people(project, ['a','empty','bad','b'])
            self.assertEqual([r['status'] for r in results], ['已发布','已跳过','发布失败','已发布'])
            self.assertEqual(project.authenticate('token-a'), 'a')
            self.assertEqual(project.authenticate('token-b'), 'b')
            with self.assertRaises(ValueError):
                project.authenticate('token-bad')
            with self.assertRaises(ValueError):
                project.authenticate('token-empty')

    def test_delivery_uses_current_original_path_and_rejects_changes(self):
        from delivery import selected_files
        import copy
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = Project(root / 'project', '交付测试')
            self.addCleanup(project.close)
            project.data['groups'] = {'a': {'name':'A', 'published':False, 'token':'a'}}
            source = root / 'photo.jpg'
            Image.new('RGB', (40, 40), 'red').save(source)
            item = import_media(source, project.cache, 'a')
            item['groups'] = []
            project.data['items'] = [item]
            record = {'files': [copy.deepcopy(item)]}
            project.move_items({item['id']}, 'a', move_originals=True)
            self.assertEqual(selected_files(project, record), [Path(item['path'])])
            Path(item['path']).write_bytes(b'changed')
            with self.assertRaises(ValueError):
                selected_files(project, record)

    def test_physical_move_preserves_bytes_and_avoids_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = Project(root / 'project', '移动测试')
            self.addCleanup(project.close)
            project.data['groups'] = {'a': {'name': 'A', 'published': False, 'token': 'a'}}
            source = root / 'original.jpg'
            Image.new('RGB', (40, 40), 'red').save(source)
            original = source.read_bytes()
            item = import_media(source, project.cache, 'a')
            item['groups'] = []
            project.data['items'] = [item]
            folder = project.group_folder('a') / '原片'
            folder.mkdir()
            (folder / source.name).write_bytes(b'existing')
            self.assertEqual(project.move_items({item['id']}, 'a', move_originals=True), 1)
            self.assertFalse(source.exists())
            self.assertEqual(Path(item['path']).read_bytes(), original)
            self.assertEqual((folder / source.name).read_bytes(), b'existing')
            self.assertEqual(Path(item['path']).parent, folder)
            self.assertTrue(item['managed_original'])
            self.assertEqual(fingerprint(Path(item['path'])), {'size':item['size'], 'mtime':item['mtime']})

    def test_move_and_delete_person_preserve_media_and_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = Project(root / 'project', '整理测试')
            self.addCleanup(project.close)
            project.data['groups'] = {g: {'name': g, 'published': True, 'token': g} for g in ('a', 'b')}
            source = root / 'original.jpg'
            Image.new('RGB', (30, 30), 'red').save(source)
            original = source.read_bytes()
            item = import_media(source, project.cache, 'a')
            project.data['items'] = [item]
            self.assertEqual(project.move_items({item['id']}, 'b'), 1)
            self.assertEqual(item['groups'], ['b'])
            self.assertFalse(project.data['groups']['a']['published'])
            package = project.group_folder('b') / 'history.zip'
            package.write_bytes(b'historical-package')
            project.delete_group('b')
            self.assertEqual(item['groups'], [])
            self.assertFalse(item['reviewed'])
            self.assertEqual(source.read_bytes(), original)
            self.assertTrue(package.exists())
            self.assertTrue(list((project.folder / 'backups').glob('before_delete_person_*.json')))
            self.assertNotIn('b', project.data['groups'])

    def test_manual_import_dedup_and_shared_person(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = Project(root / 'project', '手动测试')
            self.addCleanup(project.close)
            project.data['groups'] = {g: {'name': g, 'published': False, 'token': g} for g in ('a', 'b')}
            folder = root / 'source'
            (folder / '照片').mkdir(parents=True)
            source = folder / '照片' / 'same.jpg'
            Image.new('RGB', (1800, 1300), 'red').save(source)
            outside = root / 'outside.jpg'
            Image.new('RGB', (50, 50), 'blue').save(outside)
            fake = SimpleNamespace(project=project, current_group='a', refresh=lambda: None,
                                   status=SimpleNamespace(set=lambda value: None))
            fake.run = lambda operation, done: done(operation(lambda _: None))
            App.import_paths(fake, 'a', folder=str(folder))
            App.import_paths(fake, 'a', folder=str(folder))
            self.assertEqual(len(project.data['items']), 1)
            item = project.data['items'][0]
            self.assertEqual(item['groups'], ['a'])
            self.assertTrue(item['reviewed'])
            self.assertNotIn('observations', item)
            with Image.open(project.cache / (item['id'] + '.jpg')) as image:
                self.assertLessEqual(max(image.size), 1200)
            App.import_paths(fake, 'b', paths=[str(source)])
            self.assertEqual(item['groups'], ['a', 'b'])
            Image.new('RGB', (120, 130), 'green').save(source)
            App.import_paths(fake, 'a', paths=[str(source)])
            updated = project.data['items'][0]
            self.assertEqual(updated['groups'], ['a', 'b'])
            self.assertEqual(fingerprint(source)['size'], updated['size'])

    def test_legacy_project_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Project(directory, '旧项目')
            project.data['groups'] = {'a': {'name': 'A', 'published': True, 'token': 'a'}}
            project.data['items'] = [
                {'id': str(n), 'name': str(n), 'path': str(n), 'groups': ['a'],
                 'reviewed': reviewed, 'appearances': [1], 'observations': [2]}
                for n, reviewed in enumerate((True, False))]
            project.save()
            project.close()
            variable = SimpleNamespace(set=lambda _: None)
            fake = SimpleNamespace(project=None, server=None, project_title=variable,
                                   public_url=variable, status=variable, auto_qq=variable, refresh=lambda: None,
                                   remember_project=lambda: None, return_mode=variable, change_return_mode=lambda:None)
            App.set_project(fake, directory)
            self.addCleanup(fake.project.close)
            data = fake.project.data
            self.assertEqual(data['items'][0]['groups'], ['a'])
            self.assertEqual(data['items'][1]['groups'], [])
            self.assertNotIn('appearances', data['items'][0])
            self.assertFalse(data['groups']['a']['published'])
            self.assertTrue(list((Path(directory) / 'backups').glob('*.json')))

    def test_video_cover_without_recognition(self):
        import subprocess
        import imageio_ffmpeg
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'clip.mp4'
            subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), '-loglevel', 'error', '-y',
                            '-f', 'lavfi', '-i', 'color=c=blue:s=320x240:d=0.2',
                            '-c:v', 'libx264', str(source)], check=True,
                           creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            item = import_media(source, root, 'a')
            self.assertEqual(item['kind'], 'video')
            self.assertEqual(item['groups'], ['a'])
            self.assertTrue((root / (item['id'] + '.webp')).exists())


if __name__ == '__main__':
    unittest.main()
