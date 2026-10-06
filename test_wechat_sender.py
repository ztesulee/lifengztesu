import unittest
from wechat_sender import exact_wechat_result, wake_wechat
from wechat_visual import line_box, search_lines, toolbar_point, WeChatVisualSender, choose_archive_file
from unittest.mock import Mock, patch
from pathlib import Path
from wechat_visual import archive_label_matches, green_send_box, zip_icon_boxes
from wechat_visual import chat_title_text
from PIL import Image, ImageDraw


class WeChatTargetTests(unittest.TestCase):
    def test_missing_whole_window_title_uses_header_ocr_fallback(self):
        window=Mock()
        window.rectangle.return_value.width.return_value=1019
        window.rectangle.return_value.height.return_value=750
        sender=WeChatVisualSender(window)
        sender.nickname='小沐'
        sender.snapshot=Mock(return_value=[{'text':'我是高大光717','words':[{'x':404,'y':133,'w':168,'h':25}]}])
        sender.read_chat_title=Mock(return_value='小沐')
        self.assertTrue(sender.check_target())
        sender.read_chat_title.assert_called_once_with()
        sender.read_chat_title.return_value='其他人'
        self.assertFalse(sender.check_target())

    def test_chat_title_can_be_left_of_one_third_of_window(self):
        def line(text,x,y,w=100):
            return {'text':text,'words':[{'x':x,'y':y,'w':w,'h':20}]}
        lines=[line('莉 莉 周',269,54),line('0',231,57,12),line('搜索网络结果',158,99),line('好滴呀',404,141)]
        self.assertEqual(chat_title_text(lines,896,860),'莉莉周')

    def test_chat_title_joins_adjacent_fragments_not_toolbar_or_messages(self):
        def line(text,x,y,w):
            return {'text':text,'words':[{'x':x,'y':y,'w':w,'h':20}]}
        lines=[line('苹果',270,54,40),line('C',314,55,15),line('菜单',650,55,40),line('消息',400,190,100)]
        self.assertEqual(chat_title_text(lines,896,860),'苹果C')

    def test_visible_wechat_does_not_toggle_shortcut(self):
        window=Mock()
        window.is_visible.return_value=True
        hotkey=Mock()
        with patch('wechat_sender.window_minimized',return_value=False):
            self.assertIs(wake_wechat(lambda:window,hotkey),window)
        hotkey.assert_not_called()

    def test_hidden_wechat_uses_shortcut_once(self):
        window=Mock()
        window.is_visible.side_effect=[False,True]
        hotkey=Mock()
        with patch('wechat_sender.window_minimized',return_value=False):
            self.assertIs(wake_wechat(lambda:window,hotkey),window)
        hotkey.assert_called_once_with('^%w')

    def test_missing_wechat_does_not_launch_new_process(self):
        hotkey=Mock()
        with patch('wechat_sender.wait_for',side_effect=RuntimeError('未呼出')):
            with self.assertRaises(RuntimeError):
                wake_wechat(lambda:None,hotkey)
        hotkey.assert_called_once_with('^%w')

    def test_zip_icon_detection_excludes_send_button(self):
        image=Image.new('RGB',(500,300),'white')
        draw=ImageDraw.Draw(image)
        draw.rectangle((300,100,329,144),fill=(225,143,45))
        draw.rectangle((400,250,455,280),fill=(7,185,95))
        self.assertEqual(zip_icon_boxes(image),[(300,100,330,145)])

    def test_archive_enter_focuses_editor_and_sends_only_once(self):
        window=Mock()
        sender=WeChatVisualSender(window)
        sender.account='wx_test'
        sender.nickname='苹果C'
        sender.input_area=Mock()
        sender.send_archive_enter()
        sender.input_area.assert_called_once_with()
        window.type_keys.assert_called_once_with('{ENTER}',set_foreground=False)

    def test_archive_enter_does_not_send_when_editor_target_check_fails(self):
        window=Mock()
        sender=WeChatVisualSender(window)
        sender.input_area=Mock(side_effect=ValueError('目标变化'))
        with self.assertRaises(ValueError):
            sender.send_archive_enter()
        window.type_keys.assert_not_called()

    def test_truncated_archive_name_matches_both_ends(self):
        name='苹果C2026-10-04_13-05-06.zip'
        self.assertTrue(archive_label_matches('苹果C2026-1...05-06.zip',name))
        self.assertTrue(archive_label_matches(name,name))
        self.assertFalse(archive_label_matches('苹果C2026-1...05-07.zip',name))
        self.assertFalse(archive_label_matches('其他人物...05-06.zip',name))

    def test_green_send_button_requires_unique_enabled_button(self):
        image=Image.new('RGB',(250,90),'white')
        draw=ImageDraw.Draw(image)
        draw.rectangle((170,40,224,69),fill=(7,185,95))
        self.assertEqual(green_send_box(image),(170,40,225,70))
        draw.rectangle((20,40,74,69),fill=(7,185,95))
        self.assertIsNone(green_send_box(image))
        self.assertIsNone(green_send_box(Image.new('RGB',(250,90),(230,230,230))))

    def test_file_picker_ignores_duplicate_open_split_button_controls(self):
        editor = Mock()
        editor.element_info.automation_id = '1148'
        editor.element_info.control_type = 'Edit'
        editor.get_value.return_value = 'C:\\人物\\苹果C.zip'
        buttons = []
        for kind in ('SplitButton', 'Button'):
            button = Mock()
            button.element_info.automation_id = '1'
            button.element_info.control_type = kind
            buttons.append(button)
        dialog = Mock()
        dialog.descendants.side_effect = lambda **kw: [editor] if kw.get('control_type') == 'Edit' else [editor]+buttons
        with patch('qq_sender.keyboard_activate') as activate:
            choose_archive_file(dialog, Path('C:\\人物\\苹果C.zip'))
        editor.set_edit_text.assert_called_once_with('C:\\人物\\苹果C.zip')
        activate.assert_called_once_with(editor)

    def test_input_without_ocr_send_label_uses_relative_editor_point(self):
        window = Mock()
        window.rectangle.return_value.width.return_value = 880
        window.rectangle.return_value.height.return_value = 850
        sender = WeChatVisualSender(window)
        sender.check_target = Mock(return_value=True)
        sender.snapshot = Mock(return_value=[])
        sender.input_area()
        window.click_input.assert_called_once_with(coords=(572,765))

    def test_file_toolbar_position_and_small_window(self):
        self.assertEqual(toolbar_point(880,850,'file'), (413,816))
        with self.assertRaises(ValueError):
            toolbar_point(400,350,'file')

    def test_requires_full_wechat_id_not_nickname_or_partial_id(self):
        self.assertTrue(exact_wechat_result('某某\n微信号：wx_test123', 'wx_test123'))
        self.assertTrue(exact_wechat_result('微信号: wx_test123\n好友', 'wx_test123'))
        for value in ('wx_test123', '某某wx_test123', '微信号：wx_test1234', '微信号：wx_test'):
            self.assertFalse(exact_wechat_result(value, 'wx_test123'))

    def test_ocr_line_bounds_come_from_recognized_words(self):
        self.assertEqual(line_box({'words':[{'x':10,'y':20,'w':30,'h':15}, {'x':50,'y':22,'w':40,'h':14}]}), (10,20,90,36))

    def test_search_ignores_chat_text_and_accepts_contact_placeholder(self):
        def line(text,x,y):
            return {'text':text,'words':[{'x':x,'y':y,'w':80,'h':20}]}
        search=line('搜索联系人',90,40)
        lines=[search,line('搜索联系人',500,40),line('搜索联系人',90,400)]
        self.assertEqual(search_lines(lines,800,800,{'搜索','搜索联系人'}),[search])
        query=line('wx_test123',90,40)
        self.assertEqual(search_lines([query,line('wx_test123',90,300)],800,800,{'wx_test123'}),[query])


if __name__ == '__main__':
    unittest.main()
