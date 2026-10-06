import json
import tempfile
import unittest
from pathlib import Path
from history_scan import scan_history


class HistoryScanTest(unittest.TestCase):
    def test_accounts_conflicts_missing_sources_and_read_only_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            originals={}
            for name,contacts in [('A',{'qq':'123','wechat':'wx_a'}),('B',{'qq':'123','wechat':'wx_b'}),('C',{'wechat':'123'})]:
                folder=root/name
                folder.mkdir()
                data={'title':name,'groups':{'g':{'name':'人物','contacts':contacts}},
                      'items':[{'id':'1','path':str(folder/'missing.jpg'),'groups':['g']}],
                      'submissions':[{'status':'queued'}]}
                file=folder/'project.json'
                file.write_text(json.dumps(data),encoding='utf-8')
                originals[file]=file.read_bytes()
            report=scan_history(root,[root/'A'])
            self.assertEqual(report['projects'],3)
            self.assertEqual(len(report['duplicates']),1)
            match=report['duplicates'][0]
            self.assertEqual(match['channel'],'qq')
            self.assertTrue(match['conflict'])
            self.assertEqual(len(match['entries']),2)
            self.assertTrue(all(entry['missing']==1 for entry in match['entries']))
            for file,content in originals.items():self.assertEqual(file.read_bytes(),content)
            self.assertEqual(report['digest'],scan_history(root)['digest'])

    def test_same_account_in_one_project_is_not_cross_project_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            data={'groups':{str(i):{'contacts':{'wechat':'wx_same'}} for i in range(2)},'items':[]}
            (root/'project.json').write_text(json.dumps(data),encoding='utf-8')
            self.assertFalse(scan_history(root)['duplicates'])

    def test_invalid_project_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'project.json').write_text('bad',encoding='utf-8')
            report=scan_history(root)
            self.assertEqual(len(report['errors']),1)


if __name__=='__main__':unittest.main()
