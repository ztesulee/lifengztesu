import unittest
from unittest.mock import Mock, patch
from qq_sender import QQSender, friend_name, chat_matches, controls_by_id, flash_record_state, flash_count_matches, collapsed_flash_title, flash_card_appeared
from pathlib import Path
from qq_sender import flash_batch_cards
from qq_sender import owned_flash_confirmation
from qq_sender import keyboard_activate, window_minimized, select_main_window, select_chat_window
from qq_sender import find_chat_input, message_input_point, recover_main_window
from types import SimpleNamespace


class QQCompatibilityTests(unittest.TestCase):
    def test_hidden_duplicate_chat_controls_do_not_block_visible_chat(self):
        window, header, hidden, file, messages = [Mock() for _ in range(5)]
        hidden.is_visible.return_value=False
        messages.window_text.return_value='消息列表'
        def descendants(**kwargs):
            if kwargs.get('title')=='aa':return [header,hidden]
            if kwargs.get('title')=='文件弹出菜单':return [file,hidden]
            return [messages]
        window.descendants.side_effect=descendants
        self.assertTrue(chat_matches(window,'aa'))

    def test_secondary_monitor_main_recovery_does_not_toggle_or_move_window(self):
        gui, process, desktop = Mock(), Mock(), Mock()
        gui.IsWindow.return_value = True
        gui.IsWindowVisible.return_value = False
        gui.IsIconic.return_value = False
        gui.GetWindowRect.return_value = (-1500, 20, -700, 700)
        process.GetWindowThreadProcessId.return_value = (1, 123)
        with patch.dict('sys.modules', {'win32gui': gui, 'win32process': process}), patch('qq_sender.process_executable', return_value='qq.exe'):
            recovered = recover_main_window(desktop, (123, 101))
            gui.ShowWindow.assert_called_once_with(101, 5)
            gui.SetWindowPos.assert_not_called()
            recovered.type_keys.assert_called_once_with('^f', set_foreground=False)
            gui.ShowWindow.reset_mock()
            gui.IsWindowVisible.return_value = True
            recover_main_window(desktop, (123, 101))
            gui.ShowWindow.assert_not_called()
            process.GetWindowThreadProcessId.return_value = (1, 456)
            self.assertIsNone(recover_main_window(desktop, (123, 101)))
    def test_message_click_uses_target_window_relative_coordinates(self):
        window, toolbar, send = Mock(), Mock(), Mock()
        send.element_info.control_type = 'SplitButton'
        send.is_enabled.return_value = False
        send.is_visible.return_value = True
        window.rectangle.return_value = SimpleNamespace(left=2500, right=3200, top=100, bottom=800)
        toolbar.rectangle.return_value = SimpleNamespace(left=2530, bottom=450)
        send.rectangle.return_value = SimpleNamespace(top=700)
        window.descendants.side_effect = lambda **k: [toolbar] if k.get('title') == '文件弹出菜单' else [send]
        self.assertEqual(message_input_point(window), (60, 475))
        send.rectangle.return_value = SimpleNamespace(top=400)
        with self.assertRaisesRegex(ValueError, '边界异常'):
            message_input_point(window)
    def test_unnamed_editor_below_toolbar_excludes_search_and_message_list(self):
        def rect(top, bottom):
            return SimpleNamespace(top=top, bottom=bottom, left=0, right=500,
                                   width=lambda: 500, height=lambda: bottom-top)
        window, toolbar, search, history, editor = [Mock() for _ in range(5)]
        window.rectangle.return_value = rect(0, 700)
        toolbar.rectangle.return_value = rect(400, 430)
        for control, kind, name, top, bottom in ((search, 'Edit', '搜索', 10, 40),
                (history, 'Document', '消息列表', 50, 390), (editor, 'Document', '', 440, 620)):
            control.element_info.control_type = kind
            control.window_text.return_value = name
            control.rectangle.return_value = rect(top, bottom)
            control.is_visible.return_value = True
            control.is_enabled.return_value = True
        window.descendants.side_effect = lambda **k: [toolbar] if k else [search, history, editor]
        self.assertIs(find_chat_input(window), editor)
        other = Mock()
        other.element_info.control_type = 'Edit'
        other.window_text.return_value = ''
        other.rectangle.return_value = rect(450, 600)
        window.descendants.side_effect = lambda **k: [toolbar] if k else [search, history, editor, other]
        with self.assertRaisesRegex(ValueError, '无法唯一定位'):
            find_chat_input(window)
    def test_multiple_matching_chats_require_activated_target(self):
        first, target = Mock(), Mock()
        first.handle, target.handle = 101, 202
        self.assertIs(select_chat_window([first, target], 202), target)
        with self.assertRaisesRegex(ValueError, '多个同名聊天区域'):
            select_chat_window([first, target], 303)
    def test_same_process_chat_search_is_not_a_second_account(self):
        main, chat = Mock(), Mock()
        for w, handle, title in ((main, 1116960, 'QQ'), (chat, 3679700, '好友聊天')):
            w.process_id.return_value = 57312
            w.handle = handle
            w.window_text.return_value = title
        self.assertIs(select_main_window([chat, main]), main)
        others = []
        for handle in range(30):
            other = Mock()
            other.process_id.return_value = 57312
            other.handle = handle + 2000000
            other.window_text.return_value = '其他群聊或好友'
            others.append(other)
        self.assertIs(select_main_window(others + [chat, main], (57312, 1116960)), main)
        main.window_text.return_value = '已有聊天'
        self.assertIs(select_main_window([chat, main], (57312, 1116960)), main)
        chat.process_id.return_value = 12345
        with self.assertRaisesRegex(ValueError, '多个 QQ 进程'):
            select_main_window([chat, main], (57312, 1116960))
    def test_chat_without_window_pattern_does_not_break_diagnostics(self):
        NoPatternInterfaceError = type('NoPatternInterfaceError', (Exception,), {})
        window = Mock()
        window.handle = None
        window.is_minimized.side_effect = NoPatternInterfaceError()
        self.assertFalse(window_minimized(window))
    def test_keyboard_activation_requires_actual_target_focus(self):
        control=Mock();control.has_keyboard_focus.return_value=True
        keyboard_activate(control)
        control.type_keys.assert_called_once_with('{ENTER}',set_foreground=False)
        control=Mock();control.has_keyboard_focus.return_value=False
        with patch('qq_sender.wait_for',side_effect=RuntimeError('no focus')):
            with self.assertRaises(RuntimeError):keyboard_activate(control)
        control.type_keys.assert_not_called()
    def test_owned_flash_modal_when_underlying_header_is_hidden(self):
        window,dialog,send,recipient=Mock(),Mock(),Mock(),Mock()
        window.descendants.return_value=[dialog]
        recipient.window_text.return_value='发送给 好友备注'
        send.is_visible.return_value=True
        dialog.descendants.side_effect=lambda **k:[send] if k.get('control_type')=='Button' else [recipient]
        self.assertTrue(owned_flash_confirmation(window))
        send.is_visible.return_value=False
        self.assertFalse(owned_flash_confirmation(window))
        send.is_visible.return_value=True
        window.descendants.return_value=[dialog,dialog]
        self.assertFalse(owned_flash_confirmation(window))

    def test_actual_qq_card_id_split_default_title_and_count(self):
        messages, card = Mock(), Mock()
        card.element_info.automation_id='rich-el__scene_0__123_0'
        messages.descendants.return_value=[card]
        values=[]
        for text in ['photo等6项文','件','24.34 MB · 6 项 · 14天后过期','QQ闪传']:
            item=Mock();item.window_text.return_value=text;values.append(item)
        card.descendants.return_value=values
        paths=[Path('photo.jpg')]+[Path(str(n)+'.jpg') for n in range(5)]
        self.assertEqual(set(flash_batch_cards(messages,paths,'PhotoReturn_6_123')),{'rich-el__scene_0__123_0'})
        self.assertFalse(flash_batch_cards(messages,[Path('photo.jpg')],'other'))
        paths=[Path('different.jpg')]+[Path(str(n)+'.jpg') for n in range(5)]
        self.assertFalse(flash_batch_cards(messages,paths,'other'))

    def test_new_flash_card_allows_queue_to_continue_without_old_card_match(self):
        with patch('qq_sender.flash_record_count',return_value=2), patch('qq_sender.flash_record_state',return_value='pending'):
            self.assertTrue(flash_card_appeared(Mock(),'batch',1))
            self.assertFalse(flash_card_appeared(Mock(),'batch',2))
        with patch('qq_sender.flash_record_count',return_value=2), patch('qq_sender.flash_record_state',return_value='failed'):
            self.assertFalse(flash_card_appeared(Mock(),'batch',1))

    def test_flash_confirmation_combined_summary_and_single_title(self):
        self.assertTrue(flash_count_matches(['1项 · 4.33 MB'],1))
        self.assertTrue(flash_count_matches(['25','项','99.92 MB'],25))
        self.assertFalse(flash_count_matches(['11项'],1))
        self.assertEqual(collapsed_flash_title(['photo.jpg'],[Path('photo.jpg')]),'photo.jpg')
        self.assertIsNone(collapsed_flash_title(['other.jpg'],[Path('photo.jpg')]))
        self.assertEqual(collapsed_flash_title(['photo等2项文件'],[Path('photo.jpg'),Path('two.jpg')]),'photo等2项文件')

    def test_flash_card_nested_title_and_transfer_states(self):
        messages, card = Mock(), Mock()
        messages.descendants.return_value = [card]
        def texts(*values):
            result = []
            for value in values:
                item = Mock()
                item.window_text.return_value = value
                result.append(item)
            card.descendants.return_value = result
        texts('PhotoReturn_', '25_123', '99.92 MB · 25 项', 'QQ闪传')
        self.assertEqual(flash_record_state(messages, 'PhotoReturn_25_123'), 'submitted')
        self.assertIsNone(flash_record_state(messages, 'PhotoReturn_25_456'))
        texts('PhotoReturn_25_123', 'QQ闪传', '上传中')
        self.assertEqual(flash_record_state(messages, 'PhotoReturn_25_123'), 'pending')
        texts('PhotoReturn_25_123', 'QQ闪传', '上传失败')
        self.assertEqual(flash_record_state(messages, 'PhotoReturn_25_123'), 'failed')
        texts('PhotoReturn_25_123')
        self.assertIsNone(flash_record_state(messages, 'PhotoReturn_25_123'))

    def test_file_dialog_ids_use_supported_descendant_filters(self):
        window, filename, other, confirm = [Mock() for _ in range(4)]
        filename.element_info.automation_id = '1148'
        other.element_info.automation_id = '999'
        confirm.element_info.automation_id = '1'
        confirm.element_info.control_type = 'SplitButton'
        def descendants(*, control_type=None):
            return [filename, other] if control_type == 'Edit' else [confirm]
        window.descendants.side_effect = descendants
        self.assertEqual(controls_by_id(window, '1148', 'Edit'), [filename])
        self.assertEqual(controls_by_id(window, '1'), [confirm])

    def test_local_contact_formats_and_exclusions(self):
        self.assertEqual(friend_name('面酱(907386044) 来自:我爱二刺猿', '907386044', contact_scope=True), '面酱')
        self.assertEqual(friend_name('面酱(907386044)', '907386044', contact_scope=True), '面酱')
        self.assertIsNone(friend_name('面酱(907386044) 来自:我爱二刺猿', '907386044'))
        self.assertIsNone(friend_name('面酱(9073860440)', '907386044', contact_scope=True))
        for text in ('aa (540879838) 联系人', 'aa (540879838) 来自:我的好友'):
            self.assertEqual(friend_name(text, '540879838'), 'aa')
        for text in ('群 (377人) 包含： 540879838', '540879838',
                     'aa (5408798380) 联系人', 'aa (540879838) 综合搜索'):
            self.assertIsNone(friend_name(text, '540879838'))

    def test_active_search_and_logged_in_window_are_reused(self):
        sender = QQSender.__new__(QQSender)
        edit, entry, marker, main, chat, desktop = [Mock() for _ in range(6)]
        edit.window_text.return_value = ''
        edit.get_value.return_value = '540879838'
        entry.window_text.return_value = 'aa (540879838) 联系人'
        marker.window_text.return_value = '会话列表'
        main.is_minimized.return_value = False
        main.is_visible.return_value = True
        edit.is_visible.return_value = True
        edit.is_enabled.return_value = True
        main.process_id.return_value = 123
        main.descendants.side_effect = lambda **k: [edit] if k.get('control_type') == 'Edit' else [edit, entry, marker]
        desktop.windows.side_effect = lambda **k: [main] if k.get('title') == 'QQ' else ([] if k.get('visible_only') is True else [main, chat])
        chat.window_text.return_value = 'aa等2个会话'
        chat.process_id.return_value = 123
        messages = Mock()
        messages.window_text.return_value = '消息列表'
        chat.descendants.side_effect = lambda **k: [] if k.get('title') == '闪传发送' else ([Mock()] if k else [messages])
        sender.desktop = desktop
        sender.hotkey = Mock()
        with patch('delivery.open_client') as launch, patch('qq_sender.process_executable', return_value='qq.exe'), patch('qq_sender.restore_hidden_qq'):
            sender.connect('540879838')
            launch.assert_not_called()
        edit.set_focus.assert_called_once()
        self.assertEqual(edit.type_keys.call_count, 2)
        edit.type_keys.assert_any_call('^a', set_foreground=True)
        edit.type_keys.assert_any_call('540879838', set_foreground=True, pause=.08)
        entry.type_keys.assert_called_once_with('{ENTER}',set_foreground=False)
        sender.hotkey.assert_not_called()
        main.type_keys.assert_called_once_with('^f',set_foreground=True)
        self.assertEqual(sender.nickname, 'aa')
        # A second queued task must reuse the expanded window, not toggle it shut.
        main.window_text.return_value = 'aa等2个会话'
        # An inactive search field is not evidence that the main window is hidden.
        edit.is_enabled.return_value = False
        main.descendants.side_effect = lambda **k: [] if k.get('control_type') == 'Edit' else [entry, marker]
        def focus_search(keys, **kwargs):
            edit.is_enabled.return_value = True
            main.descendants.side_effect = lambda **k: [edit] if k.get('control_type') == 'Edit' else [edit, entry, marker]
        main.type_keys.side_effect = focus_search
        desktop.windows.side_effect = lambda **k: ([] if k.get('title') == 'QQ' or k.get('visible_only') is True else [main, chat])
        with patch('qq_sender.process_executable', return_value='qq.exe'):
            sender.connect('540879838')
        sender.hotkey.assert_not_called()
        self.assertEqual(main.type_keys.call_count, 2)
        # Reproduce the real log: only the detached chat remains enumerated.
        main.is_visible.return_value = False
        chat.is_minimized.return_value = False
        chat.descendants.side_effect = lambda **k: [] if k.get('title') == '闪传发送' else ([Mock()] if k else [messages])
        desktop.windows.side_effect = lambda **k: [chat]
        def reveal(keys):
            main.is_visible.return_value = True
            desktop.windows.side_effect = lambda **k: [chat] if k.get('visible_only') is True else [main, chat]
        sender.hotkey.side_effect = reveal
        edit.get_value.return_value = '578407748'
        entry.window_text.return_value = 'bb (578407748) 联系人'
        with patch('qq_sender.process_executable', return_value='qq.exe'), patch('qq_sender.qq_process_running', return_value=True):
            sender.connect('578407748')
        sender.hotkey.assert_called_once_with('^%x')
        self.assertEqual(sender.account, '578407748')
        sender.hotkey.reset_mock()
        # If the same window is hidden later, wake it exactly once.
        main.is_visible.return_value = False
        desktop.windows.side_effect = lambda **k: [] if k.get('visible_only') is True else [main, chat]
        def reveal(keys):
            main.is_visible.return_value = True
        sender.hotkey.side_effect = reveal
        with patch('qq_sender.process_executable', return_value='qq.exe'):
            sender.connect('578407748')
        sender.hotkey.assert_called_once_with('^%x')

    def test_merged_window_requires_active_contact(self):
        window, messages = Mock(), Mock()
        window.window_text.return_value = 'aa等2个会话'
        messages.window_text.return_value = '消息列表'
        window.descendants.side_effect = lambda **k: [Mock()] if k else [messages]
        self.assertTrue(chat_matches(window, 'aa'))
        window.window_text.return_value = 'QQ'
        self.assertTrue(chat_matches(window, 'aa'))
        window.window_text.return_value = 'other等2个会话'
        self.assertTrue(chat_matches(window, 'aa'))
        window.descendants.side_effect = lambda **k: [] if k.get('title') == 'aa' else [messages]
        self.assertFalse(chat_matches(window, 'aa'))

    def test_login_window_does_not_launch_another_client(self):
        sender = QQSender.__new__(QQSender)
        login = Mock()
        login.process_id.return_value = 0
        login.descendants.return_value = []
        sender.desktop = Mock()
        sender.desktop.windows.return_value = [login]
        with patch('delivery.open_client') as launch, patch('qq_sender.qq_process_running',return_value=False):
            with self.assertRaisesRegex(ValueError, '不会重复启动'):
                sender.connect('540879838')
            launch.assert_not_called()

    def test_hidden_running_qq_is_restored_without_new_login_process(self):
        sender=QQSender.__new__(QQSender)
        sender.desktop=Mock()
        sender.desktop.windows.return_value=[]
        sender.hotkey=Mock()
        with patch('qq_sender.qq_process_running',return_value=True), patch('qq_sender.restore_hidden_qq') as restore, patch('qq_sender.wait_for',side_effect=RuntimeError('cannot restore')), patch('delivery.open_client') as launch:
            with self.assertRaises(RuntimeError):sender.connect('123456')
            sender.hotkey.assert_called_once_with('^%x')
            restore.assert_not_called()
            launch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
