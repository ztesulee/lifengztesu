import unittest
from project import person_recipient, task_recipient


class DeliveryChannelsTest(unittest.TestCase):
    def setUp(self):
        self.group = {'name':'苹果C','contacts':{'qq':'123456','wechat':'wx_apple'},'default_channel':'wechat'}

    def test_dual_accounts_use_default_or_explicit_choice(self):
        self.assertEqual(person_recipient(self.group), {'wechat':'wx_apple'})
        self.assertEqual(person_recipient(self.group,'qq'), {'qq':'123456'})
        self.assertEqual(person_recipient(self.group,'wechat'), {'wechat':'wx_apple'})
        self.assertEqual(len(self.group['contacts']),2)

    def test_single_account_automatically_uses_available_channel(self):
        for choice in ('default','qq','wechat'):
            self.assertEqual(person_recipient({'name':'A','contacts':{'qq':'123456'},'default_channel':'wechat'},choice),{'qq':'123456'})
            self.assertEqual(person_recipient({'name':'A','contacts':{'wechat':'wx_a'},'default_channel':'qq'},choice),{'wechat':'wx_a'})

    def test_no_account_prompts_to_fill_binding(self):
        with self.assertRaisesRegex(ValueError,'未填写接收账号'):
            person_recipient({'name':'A','contacts':{}})

    def test_unstarted_task_can_switch_channel(self):
        record={'status':'waiting','recipient':{'qq':'123456'}}
        self.assertEqual(task_recipient(record,self.group,'wechat'),{'wechat':'wx_apple'})

    def test_started_tasks_cannot_switch_and_default_preserves_resume_target(self):
        for extra in ({'status':'submitted'},{'status':'needs_review'}, {'sent_files':['a']}, {'inflight':'a'}):
            record={'status':'error','recipient':{'qq':'123456'},**extra}
            with self.assertRaises(ValueError):
                task_recipient(record,self.group,'wechat')
            self.assertEqual(task_recipient(record,self.group),{'qq':'123456'})


if __name__ == '__main__':
    unittest.main()
